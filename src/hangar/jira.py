"""Jira front door: an issue moved to a status, or given a label, in a mapped project becomes a job (docs/intake.md).

Two ways in, both at POST /api/hooks/jira:
* **An admin webhook** (Jira Cloud and Data Center) on "Issue created", "Issue updated" and "Comment created", with a
  JQL filter such as `project in (ABC, DEF)`. Jira signs the body with the webhook's secret: `X-Hub-Signature:
  sha256=<hex HMAC-SHA256 of the body>`. The payload's `timestamp` (signed, in the body) must be recent, and the body's
  digest is remembered, so a captured delivery can't be replayed.
* **An Automation rule's "Send web request"** (Jira Cloud) for teams that prefer a rule to a webhook. Automation
  doesn't sign requests, so the rule sends a hidden `X-Hangar-Token` header instead, and a body (docs/intake.md) whose
  `timestamp` is `{{now}}`.

Hangar answers on the issue through Jira's REST API with the control plane's own Jira credential (never in a
sandbox): a comment when the agent starts, the agent's question (a comment by the job's person answers it), and
the outcome with the PR, which is also added as a remote link. A configured status transition goes with each stage.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hangar import autoinstall as autoinstall  # --- autoinstall: an issue labelled for an env setup
from hangar import fairness as fairness  # --- review capacity (#159)
from hangar import intake
from hangar.exchange import open_question
from hangar.github import parse_repo, verify_signature
from hangar.jobs import NOTIFIERS, JobError, JobService, SubmitJob
from hangar.models import JOB_TERMINAL, AuditEvent, ContextItem, Job, JobState

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger("hangar.jira")

MAX_BODY = 1_000_000
# --- trust: past MAX_BODY a delivery is read on (not kept) up to Jira's own limit ("Webhooks over 25 MB aren't
# delivered", Atlassian's webhook docs), so it can be authenticated, audited and answered on its issue
LARGE_MAX = 25 * 1024 * 1024
LARGE_HEAD = 64 * 1024  # what is kept of it: the issue's key comes before its fields
LARGE_NOTE_SECONDS = 86400  # at most one comment a day per issue about it
_HEAD_KEY = re.compile(rb'"issue"\s*:\s*\{.*?"key"\s*:\s*"([A-Z][A-Z0-9_]{0,49}-[1-9][0-9]{0,9})"', re.S)
_HEAD_TIME = re.compile(rb'"timestamp"\s*:\s*(?:"([^"\\]{1,40})"|(-?[0-9][0-9.eE+]{0,30}))')
COMMENT_LIMIT = 30_000  # Jira caps a comment at 32,767 characters
OWN = "[hangar]"  # every comment hangar writes starts with this, so its own comments never count as replies
_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,49}-[1-9][0-9]{0,9}$")
_PROJECT = re.compile(r"^[A-Z][A-Z0-9_]{0,49}$")
_REPO_SHORT = re.compile(r"^[\w.-]+/[\w.-]+$")
_ACCOUNT = re.compile(r"^[\w:.@+-]{1,128}$")


# ============================================================================ configuration (HANGAR_JIRA)
class JiraProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str  # owner/repo (GitHub) or a git URL, as in HANGAR_GITHUB_REPOS
    components: dict[str, str] = Field(default_factory=dict)  # Jira component name -> repo, first match wins
    environment: str | None = None


class JiraTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None  # an issue moved to this status starts a job
    label: str | None = None  # an issue given this label starts a job

    @model_validator(mode="after")
    def _one(self) -> JiraTrigger:
        if not (self.status or self.label):
            raise ValueError("set trigger.status, trigger.label or both")
        return self


class JiraStatuses(BaseModel):
    """The status to move the issue to at each stage (by name; the transition into it is looked up). None leaves
    the status alone."""

    model_config = ConfigDict(extra="forbid")

    started: str | None = None
    needs_input: str | None = None
    delivered: str | None = None
    failed: str | None = None


class JiraConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str  # https://acme.atlassian.net, or a Data Center base URL
    deployment: Literal["cloud", "server"] = "cloud"
    email: str | None = None  # Cloud: the account the API token belongs to (basic auth); unused on Server/DC
    token_credential: str  # vault: a Cloud API token, or a Server/DC personal access token (Bearer)
    webhook_secret_credential: str | None = None  # vault: the admin webhook's secret (X-Hub-Signature)
    automation_token_credential: str | None = None  # vault: the X-Hangar-Token an Automation rule sends
    projects: dict[str, JiraProject]
    trigger: JiraTrigger
    statuses: JiraStatuses = Field(default_factory=JiraStatuses)
    max_age_minutes: int = Field(120, ge=5, le=1440)  # Jira retries a failed delivery for about 75 minutes
    # --- automation (#128): an Automation request starts a job only when the issue's history shows the trigger
    # transition (or label) within this many minutes of the request arriving; and at most this many per issue a day
    automation_max_transition_age_minutes: int = Field(10, ge=1, le=1440)
    automation_max_starts_per_issue_per_day: int = Field(3, ge=1, le=100)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("url must be https://")
        return v.rstrip("/")

    @field_validator("projects")
    @classmethod
    def _keys(cls, v: dict[str, JiraProject]) -> dict[str, JiraProject]:
        bad = [k for k in v if not _PROJECT.match(k)]
        if bad:
            raise ValueError(f"project keys are upper case, like ABC: {', '.join(bad)}")
        return v

    @model_validator(mode="after")
    def _check(self) -> JiraConfig:
        if self.deployment == "cloud" and not self.email:
            raise ValueError("Jira Cloud needs email (the account the API token belongs to)")
        if not (self.webhook_secret_credential or self.automation_token_credential):
            raise ValueError("set webhook_secret_credential, automation_token_credential or both")
        trig = (self.trigger.status or "").casefold()
        loops = [k for k, v in self.statuses.model_dump().items() if v and v.casefold() == trig]
        if loops:  # hangar moving the issue into the trigger status would start the job again
            raise ValueError(f"statuses.{loops[0]} is the trigger status")
        return self

    def credentials(self) -> list[str]:
        return [n for n in (self.token_credential, self.webhook_secret_credential,
                            self.automation_token_credential) if n]


class JiraConfigError(ValueError):
    pass


def load(settings: Any) -> JiraConfig | None:
    """HANGAR_JIRA, validated; None when Jira intake is off."""
    raw = settings.jira
    if not raw:
        return None
    try:
        return JiraConfig.model_validate(raw)
    except ValidationError as e:
        err = e.errors()[0]
        log.error("HANGAR_JIRA is invalid: %s: %s", ".".join(map(str, err["loc"])), err["msg"])
        raise JiraConfigError("Jira intake is misconfigured (HANGAR_JIRA); see hangar doctor") from e


# ============================================================================ text
def adf_text(node: Any) -> str:
    """Plain text of a comment or description: a string already (webhooks, Server/DC), or ADF (Cloud REST v3)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        return str(node.get("text") or "")
    if t == "hardBreak":
        return "\n"
    if t in ("mention", "emoji", "inlineCard"):
        a = node.get("attrs") or {}
        return str(a.get("text") or a.get("shortName") or a.get("url") or "")
    inner = adf_text(node.get("content"))
    return inner + "\n\n" if t in ("paragraph", "heading", "blockquote", "codeBlock", "listItem",
                                   "taskItem") else inner  # --- #156: a checklist item is a line of its own


_INLINE = re.compile(r"`([^`\n]+)`|(https?://[^\s<>()\"']+)")


def _inline(line: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def text(s: str, marks: list[dict[str, Any]] | None = None) -> None:
        if s:
            out.append({"type": "text", "text": s, **({"marks": marks} if marks else {})})

    pos = 0
    for m in _INLINE.finditer(line):
        text(line[pos:m.start()])
        if m.group(1):
            text(m.group(1), [{"type": "code"}])
        else:
            url = m.group(2).rstrip(".,;:!?")
            text(url, [{"type": "link", "attrs": {"href": url}}])
            text(m.group(2)[len(url):])
        pos = m.end()
    text(line[pos:])
    return out


def to_adf(text: str) -> dict[str, Any]:
    """Atlassian Document Format for Jira Cloud's REST v3: paragraphs, `code`, links, and `> ` lines as a quote."""
    content: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        quote = all(ln.startswith(">") for ln in lines)
        para: list[dict[str, Any]] = []
        for i, ln in enumerate(lines):
            if i:
                para.append({"type": "hardBreak"})
            para += _inline(ln[1:].lstrip() if quote else ln)
        node = {"type": "paragraph", "content": para} if para else None
        if node is None:
            continue
        content.append({"type": "blockquote", "content": [node]} if quote else node)
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": []}]}


def to_wiki(text: str) -> str:
    """Server/Data Center (REST v2) comments are wiki markup: `code` becomes {{code}}; bare URLs link themselves."""
    return re.sub(r"`([^`\n]+)`", r"{{\1}}", text).replace("\n>", "\nbq.").removeprefix(">").strip()


def _clip(text: str, n: int = COMMENT_LIMIT) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _quote(text: str, n: int = 4000) -> str:
    return "\n".join(f"> {ln}" for ln in _clip(text.strip(), n).splitlines() if ln.strip()) or "> (empty)"


# ============================================================================ REST client
class JiraError(RuntimeError):
    pass


class JiraAPI:
    """The few REST calls hangar makes. Cloud: REST v3 with basic auth (email + API token); Server/DC: REST v2 with a
    personal access token. Retries rate limits (Retry-After) and transient errors; messages never carry the token."""

    def __init__(self, cfg: JiraConfig, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.cloud = cfg.deployment == "cloud"
        self.api = "/rest/api/3" if self.cloud else "/rest/api/2"
        auth = ("Basic " + base64.b64encode(f"{cfg.email}:{token}".encode()).decode()) if self.cloud \
            else f"Bearer {token}"
        self._fast = transport is not None  # tests: don't sleep between retries
        self._client = httpx.AsyncClient(base_url=cfg.url, transport=transport, timeout=15, follow_redirects=False,
                                         headers={"Authorization": auth, "Accept": "application/json",
                                                  "User-Agent": "hangar"})

    async def close(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        err = "no attempt"
        for attempt in range(3):
            try:
                r = await self._client.request(method, path, **kw)
            except httpx.HTTPError as e:
                err = type(e).__name__
                await asyncio.sleep(0 if self._fast else 2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                err = f"HTTP {r.status_code}"
                try:
                    wait = min(float(r.headers.get("retry-after", 2 ** attempt)), 30.0)
                except ValueError:
                    wait = 2 ** attempt
                await asyncio.sleep(0 if self._fast else wait)
                continue
            if r.status_code >= 400:
                try:
                    j = r.json()
                    msg = "; ".join([*j.get("errorMessages", []), *map(str, (j.get("errors") or {}).values())])
                except (ValueError, AttributeError):
                    msg = ""
                raise JiraError(f"Jira {method} {path}: HTTP {r.status_code} {msg}".strip()[:500])
            return r.json() if r.content else None
        raise JiraError(f"Jira {method} {path}: {err} (gave up after 3 attempts)")

    async def issue(self, key: str) -> dict[str, Any]:
        return await self._req("GET", f"{self.api}/issue/{key}", params={
            "fields": "summary,description,labels,components,project,status,reporter,creator,issuetype,priority,"
                      "created"})

    async def history(self, key: str) -> list[dict[str, Any]]:
        """--- review: the issue's latest changes (oldest first), each with its `author` and `items`. Cloud pages
        its changelog oldest first, so read the total, then the last page; Data Center expands it on the issue."""
        if not self.cloud:
            res = await self._req("GET", f"{self.api}/issue/{key}", params={"expand": "changelog",
                                                                            "fields": "status"})
            return list(((res or {}).get("changelog") or {}).get("histories") or [])
        path = f"{self.api}/issue/{key}/changelog"
        first = await self._req("GET", path, params={"startAt": 0, "maxResults": 1}) or {}
        total = first.get("total")
        start = max(0, total - 100) if isinstance(total, int) and not isinstance(total, bool) else 0
        res = await self._req("GET", path, params={"startAt": start, "maxResults": 100}) or {}
        return list(res.get("values") or [])

    async def comment(self, key: str, text: str) -> None:
        text = _clip(text)
        await self._req("POST", f"{self.api}/issue/{key}/comment",
                        json={"body": to_adf(text) if self.cloud else to_wiki(text)})

    async def transition(self, key: str, status: str) -> str | None:
        """Move the issue into `status` (by the transition's target status, or its own name). Returns the status it
        is now in, or None when the workflow offers no such transition from where the issue is."""
        res = await self._req("GET", f"{self.api}/issue/{key}/transitions")
        want = status.casefold()
        for t in (res or {}).get("transitions") or []:
            to = str(((t.get("to") or {}).get("name")) or "")
            if want in (to.casefold(), str(t.get("name") or "").casefold()):
                await self._req("POST", f"{self.api}/issue/{key}/transitions", json={"transition": {"id": t["id"]}})
                return to or status
        return None

    async def remote_link(self, key: str, url: str, title: str) -> None:
        """Link the PR on the issue. The globalId makes a second call update the same link instead of adding one."""
        await self._req("POST", f"{self.api}/issue/{key}/remotelink", json={
            "globalId": f"hangar:{url}"[:255], "object": {"url": url, "title": title[:255]}})

    async def myself(self) -> dict[str, Any]:
        return await self._req("GET", f"{self.api}/myself")


def client_for(svc: JobService, cfg: JiraConfig | None = None) -> JiraAPI | None:
    cfg = cfg or load(svc.settings)
    if cfg is None:
        return None
    try:
        token = svc.broker.secret_text(cfg.token_credential)
    except Exception:  # noqa: BLE001
        log.warning("Jira token credential %s is not usable", cfg.token_credential)
        return None
    return JiraAPI(cfg, token, transport=svc.transport)


# ============================================================================ write-back
def ref_of(key: str) -> str:
    return f"jira:{key}"


def key_of(ref: str) -> str | None:
    key = ref.split(":", 1)[1] if ref.startswith("jira:") else ""
    return key if _KEY.match(key) else None


def job_link(svc: JobService) -> str:
    return f"{svc.settings.public_url.rstrip('/')}/#jobs"


def render(svc: JobService, job: Job | None, body: str, kind: str) -> str:
    """The comment for a job report. `body` is GitHub-flavoured; questions and outcomes are written for Jira."""
    if job is not None and job.state == JobState.NEEDS_INPUT and body.startswith("**Question**"):
        q = open_question(job) or body
        return (f"{OWN} Question: hangar job `{job.id}` needs your input before it can go on.\n\n{_quote(q)}\n\n"
                "Answer in a comment on this issue; the agent picks up where it left off. The work so far is kept "
                "with the job.")
    plain = re.sub(r"\*\*(.+?)\*\*", r"\1", body).replace("<!-- hangar -->", "").strip()
    if job is None or kind != "final":
        if job is not None and plain.startswith("Agent started"):
            plain += f"\n\nFollow it in hangar: {job_link(svc)} (job `{job.id}`)."
        return f"{OWN} {plain}"
    r = job.result
    head = {"succeeded": "Done", "failed": "Finished with problems", "cancelled": "Cancelled"}.get(
        job.state.value, job.state.value)
    lines = [f"{OWN} {head}: hangar job `{job.id}`" + (f" ({job.reason})" if job.reason else "")]
    if r.pr_url:
        lines.append(f"PR: {r.pr_url}")
    elif r.pushed:
        lines.append(f"Branch: `{job.branch}`")
    if job.verify and r.verify_exit_code is not None:
        lines.append(f"Verification `{job.verify}`: {'passed' if r.verify_exit_code == 0 else 'failed'}")
    if job.state == JobState.FAILED and job.reason not in (None, "verify_failed", "no_changes"):
        cause = next((e.message for e in reversed(job.events)
                      if not e.message.startswith(("failed", "succeeded", "cancelled", "delivering", "verifying"))),
                     None)
        if cause:
            lines.append(f"What went wrong: {cause[:1500]}")
    out = "\n".join(lines)
    if r.summary:
        out += "\n\n" + _quote(r.summary)
    return out + "\n\nComment `/hangar <message>` here to continue where it left off."


async def notify(svc: JobService, job: Job | None, ref: str, body: str, kind: str) -> None:
    """The `jira:` notifier: a comment on the issue. Failures land on the job's timeline, never raise."""
    key = key_of(ref)
    try:
        api = client_for(svc) if key else None
    except JiraConfigError as e:
        api, why = None, str(e)
    else:
        why = "HANGAR_JIRA is not set, or its token credential is not stored"
    if api is None:
        if job is not None and key:
            svc._event(job, f"could not comment on Jira issue {key}: {why}")
        return
    try:
        await api.comment(key, svc.broker.redact(render(svc, job, body, kind)))
    except (JiraError, httpx.HTTPError) as e:
        if job is not None:
            svc._event(job, f"could not comment on Jira issue {key}: {e}")
        else:
            log.warning("could not comment on Jira issue %s: %s", key, e)
    finally:
        await api.close()


async def update(svc: JobService, job: Job, ref: str, stage: str) -> None:
    """The `jira:` tracker: the configured transition for the stage, and the PR as a remote link once delivered."""
    key = key_of(ref)
    cfg = load(svc.settings)
    if key is None or cfg is None:
        return
    target = getattr(cfg.statuses, stage, None)
    pr = job.result.pr_url if stage == "delivered" else None
    if not target and not pr:
        return
    api = client_for(svc, cfg)
    if api is None:
        raise JiraError("its token credential is not stored")
    try:
        if pr:
            await api.remote_link(key, pr, f"PR #{job.result.pr_number}: {job.title}" if job.result.pr_number
                                  else job.title)
        if target:
            now = await api.transition(key, target)
            svc._event(job, f"Jira issue {key} moved to {now}" if now else
                       f"Jira issue {key}: the workflow has no transition to '{target}' from its current status")
    finally:
        await api.close()


NOTIFIERS["jira"] = notify
intake.TRACKERS["jira"] = update


# ============================================================================ intake
def _repo_url(value: str) -> str:
    v = value.strip()
    return f"https://github.com/{v}.git" if _REPO_SHORT.match(v) else v


def _account(user: Any, cloud: bool) -> str | None:
    """The account id to link a Jira user by: Cloud's accountId; Server/DC's user key (stable across renames)."""
    if not isinstance(user, dict):
        return None
    v = str((user.get("accountId") or user.get("accoundId")) if cloud else (user.get("key") or user.get("name"))
            or "")
    return v if _ACCOUNT.match(v) else None


def mover(histories: list[dict[str, Any]], fields: dict[str, Any], trigger: JiraTrigger) -> dict[str, Any] | None:
    """--- review: the change that last moved the issue into the trigger status, or gave it the trigger label, by the
    issue's own history: the history entry itself (`author`, `created`, `items`), so a caller can tell who and when
    (#128). For an issue created that way with no such change, an entry made of its creator and creation time. None
    when nobody did (fail closed: the job then waits for approval, or an Automation request is ignored)."""
    status = (trigger.status or "").casefold()
    for h in sorted((h for h in histories if isinstance(h, dict)), key=lambda h: str(h.get("created") or ""),
                    reverse=True):
        for item in h.get("items") or []:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").casefold()
            if (field == "status" and status and str(item.get("toString") or "").casefold() == status) or (
                    field == "labels" and trigger.label and trigger.label in str(item.get("toString") or "").split()
                    and trigger.label not in str(item.get("fromString") or "").split()):
                return {**h, "author": h.get("author") if isinstance(h.get("author"), dict) else None}
    now = str(((fields.get("status") or {}).get("name")) or "").casefold()
    if (status and now == status) or (trigger.label and trigger.label in (fields.get("labels") or [])):
        creator = fields.get("creator")  # not the reporter, which whoever creates an issue may set
        return {"author": creator if isinstance(creator, dict) else None, "created": fields.get("created"),
                "items": [], "creation": True}
    return None


def _jira_time(value: Any) -> float | None:
    """Epoch seconds from a Jira timestamp (`2026-10-14T09:00:00.000+0000`), or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", v):  # +0000 -> +00:00 (fromisoformat takes both since 3.11; be explicit)
        v = v[:-2] + ":" + v[-2:]
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t.timestamp() if t.tzinfo is not None else None


def _who(user: Any, account: str | None) -> str:
    name = str((user or {}).get("displayName") or "") if isinstance(user, dict) else ""
    return " ".join(name.split())[:80] or account or "someone"


def _number(raw: bytes) -> float | None:
    try:
        return float(raw)
    except ValueError:
        return None


def _timestamp(value: Any) -> float | None:
    """Epoch seconds from a webhook's `timestamp` (milliseconds) or an Automation rule's `{{now}}` (ISO 8601)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):  # json.loads takes NaN and Infinity, and NaN compares as fresh
            return None
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _ago(seconds: float) -> str:
    s = abs(seconds)
    if s < 3600:
        return f"{max(1, round(s / 60))} min"
    if s < 2 * 86400:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.0f} days"


# ============================================================================ alerts (--- automation, #128)
ALERT_PREFIX = "jira:alert:"  # kv: what admins see in the inbox, for ALERT_KEEP
ALERT_KEEP = 7 * 86400
ALERT_EVERY = 3600  # at most one alert an hour per issue (and one an hour for bad tokens)
ALERT_MAX = 200
STARTS_PREFIX = "jira:automation-starts:"  # kv: when Automation started each issue's jobs, the last day of them
SIGNED_ISSUE_EVENT_KEY = "jira:signed-issue-event"  # kv: when the signed webhook last delivered an issue event
SIGNED_RECENT_DAYS = 30
AUTOMATION_USED_KEY = "jira:automation-used"  # kv: when an Automation request last got through the token check


def alert(svc: JobService, scope: str, reason: str, **detail: Any) -> bool:
    """Tell the admins: an inbox item (`kind: alert`) and, when the Slack app is configured, a DM to each admin with
    a linked Slack account. At most one per `scope` per ALERT_EVERY. True when it went out."""
    replays = svc.admission.replays
    if replays.get("jira:alerted", scope) is not None:
        return False
    replays.put("jira:alerted", scope, "", ALERT_EVERY)
    t = time.time()
    svc.store.kv_set(f"{ALERT_PREFIX}{t:.6f}:{scope}", json.dumps({"at": t, "scope": scope, "reason": reason,
                                                                   **detail}), durable=True)
    keys = sorted(svc.store.kv_keys(ALERT_PREFIX))
    for k in keys[:-ALERT_MAX] if len(keys) > ALERT_MAX else []:
        svc.store.kv_delete(k)
    svc.store.audit(AuditEvent(actor="jira:automation", action="jira.alert", target=scope,
                               detail=svc.broker.redact({"reason": reason, **detail})))
    svc._spawn(_dm_admins(svc, reason))
    return True


async def _dm_admins(svc: JobService, reason: str) -> None:
    from hangar import people, slack

    api = slack.client_for(svc)
    if api is None:
        return
    text = (f"hangar security alert (Jira intake): {reason}. If no Automation rule explains it, rotate the token: "
            "hangar cred add <automation_token_credential> --rotate (docs/intake.md).")
    try:
        for p in people.list_people(svc.store):
            if p.role != "admin":
                continue
            for link in p.links:
                if link.startswith("slack:"):
                    try:
                        await api.post(link.rsplit("/", 1)[-1], text)  # a user id as the channel: the app's DM
                    except slack.SlackError as e:
                        log.warning("could not DM %s about a Jira alert: %s", p.id, e)
    finally:
        await api.close()


def inbox_items(svc: Any, p: Any) -> list[dict[str, Any]]:
    """inbox.SOURCES: Jira intake alerts from the last week wait on the admins."""
    from hangar.inbox import _item

    if p is not None and p.role != "admin":
        return []
    out, t = [], time.time()
    for k in svc.store.kv_keys(ALERT_PREFIX):
        try:
            a = json.loads(svc.store.kv_get(k) or "{}")
            at = float(a["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if t - at > ALERT_KEEP:
            svc.store.kv_delete(k)
            continue
        out.append(_item("alert", str(a.get("reason") or "Jira intake alert")[:500],
                         "check the Jira Automation rule; if it doesn't explain this, rotate the token: hangar cred "
                         "add <automation_token_credential> --rotate", at, issue=a.get("issue")))
    return out


def register_inbox() -> None:
    from hangar import inbox

    if inbox_items not in inbox.SOURCES:
        inbox.SOURCES.append(inbox_items)


class _Large(NamedTuple):
    """What hangar keeps of a body over MAX_BODY (--- trust)."""

    head: bytes  # its first LARGE_HEAD bytes
    size: int
    digest: str  # sha256 of all of it, for the replay guard
    signature: str | None  # "sha256=<hex>" of all of it with the webhook secret, when the request is signed


class JiraFrontDoor:
    def __init__(self, jobs: JobService, clock=time.time):
        self.jobs = jobs
        self.settings = jobs.settings
        self.clock = clock

    def mount(self, app: FastAPI) -> None:
        @app.post("/api/hooks/jira")
        async def jira_hook(request: Request):
            status, res = await self.receive(request)
            return JSONResponse(res, status_code=status)

    # ------------------------------------------------------------------ the request
    def _authentic(self, cfg: JiraConfig, request: Request, body: bytes) -> tuple[bool, str | None]:
        """(authentic, why not). A signature is checked when one is sent; the Automation token otherwise."""
        sig = request.headers.get("x-hub-signature")
        token = request.headers.get("x-hangar-token")
        broker = self.jobs.broker
        if sig is not None:
            if not cfg.webhook_secret_credential:
                return False, "signed, but no webhook_secret_credential is configured"
            return verify_signature(broker.secret_text(cfg.webhook_secret_credential), body, sig), "bad signature"
        if token is not None:
            if not cfg.automation_token_credential:
                return False, "a token, but no automation_token_credential is configured"
            want = broker.secret_text(cfg.automation_token_credential)
            return hmac.compare_digest(want.encode(), token.encode("utf-8", "replace")), "bad token"
        return False, "neither X-Hub-Signature nor X-Hangar-Token"

    async def receive(self, request: Request) -> tuple[int, dict[str, Any]]:
        """Jira counts only a 200 as delivered and retries 408, 409, 425, 429 and 5xx (Atlassian's webhook docs):
        work that can't run yet answers 429 or 503; everything else, ignored or refused, answers 200 or 4xx."""
        try:
            cfg = load(self.settings)
        except JiraConfigError as e:
            return 503, {"error": str(e)}
        if cfg is None:
            return 404, {"error": "Jira intake is not configured (HANGAR_JIRA)"}
        # --- trust: what may let a body over the cap be read on: the webhook secret, or a good Automation token
        sig, token = request.headers.get("x-hub-signature"), request.headers.get("x-hangar-token")
        try:
            secret = self.jobs.broker.secret_text(cfg.webhook_secret_credential) \
                if sig is not None and cfg.webhook_secret_credential else None
            token_ok = sig is None and token is not None and self._authentic(cfg, request, b"")[0]
        except Exception:  # noqa: BLE001 - say nothing about the vault to an anonymous caller
            log.error("Jira webhook credentials are not usable")
            return 503, {"error": "Jira intake is misconfigured"}
        body = await self._read(request, secret, token_ok)  # --- trust: or what it keeps of one over the cap
        if body is None:
            return 413, {"error": "body too large"}
        if isinstance(body, _Large):
            return await self._too_large(cfg, request, body, token_ok)
        try:
            ok, why = self._authentic(cfg, request, body)
        except Exception:  # noqa: BLE001 - say nothing about the vault to an anonymous caller
            log.error("Jira webhook credentials are not usable")
            return 503, {"error": "Jira intake is misconfigured"}
        if not ok:
            log.warning("Jira webhook rejected: %s", why)
            if why == "bad token":  # --- automation (#128): someone has the URL and is guessing, or the rule is stale
                alert(self.jobs, "bad-token", "a request to /api/hooks/jira carried a wrong X-Hangar-Token (a "
                                              "guess, or an Automation rule still sending a rotated token)",
                      client=request.client.host if request.client else None)
            return 401, {"error": "bad signature"}
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, {"error": "body is not JSON"}
        if not isinstance(payload, dict):
            return 400, {"error": "body must be a JSON object"}
        # The timestamp is in the signed body (or the token-authenticated one), so a replay can't refresh it; the
        # body's digest is remembered for longer than it stays fresh.
        ts = _timestamp(payload.get("timestamp"))
        if ts is None or abs(self.clock() - ts) > cfg.max_age_minutes * 60:
            self.jobs.admission.refused("jira:stale", "jira", "hook.stale", None, timestamp=str(ts))
            return 200, {"ignored": "missing or stale timestamp"}
        delivery = (request.headers.get("x-atlassian-webhook-identifier") or "").strip()[:100]
        digest = self.jobs.admission.replays.digest(body)
        signed = request.headers.get("x-hub-signature") is not None
        res = await intake.once(self.jobs, "jira", digest, lambda: self.handle(cfg, payload, delivery, signed))
        if res is None:
            self.jobs.admission.refused("jira:replay", "jira", "hook.replay", delivery or None)
            return 200, {"ignored": "duplicate delivery"}
        return (429 if res.get("rate_limited") else 200), res

    # ------------------------------------------------------------------ deliveries over the cap (--- trust)
    @staticmethod
    async def _read(request: Request, secret: str | None, token_ok: bool) -> bytes | _Large | None:
        """The body. Past MAX_BODY, one that may be authentic (signed with a configured secret, or with the right
        Automation token) is read on to Jira's own 25 MB limit without being kept, so it can be checked and answered
        (#92: Jira doesn't retry a 413, so the trigger was lost without a word). None for anything else past the cap."""
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > LARGE_MAX):
            return None
        body, head, size = b"", b"", 0
        sha: Any = None
        mac: Any = None
        async for chunk in request.stream():
            size += len(chunk)
            if size > LARGE_MAX:
                return None
            if sha is not None:
                sha.update(chunk)
                if mac is not None:
                    mac.update(chunk)
                continue
            body += chunk
            if len(body) > MAX_BODY:
                if secret is None and not token_ok:
                    return None  # unsigned, and no good token: nothing to read on for
                sha = hashlib.sha256(body)
                mac = hmac.new(secret.encode(), body, hashlib.sha256) if secret is not None else None
                head, body = body[:LARGE_HEAD], b""
        if sha is None:
            return body
        return _Large(head, size, sha.hexdigest(), ("sha256=" + mac.hexdigest()) if mac is not None else None)

    async def _too_large(self, cfg: JiraConfig, request: Request, big: _Large,
                         token_ok: bool) -> tuple[int, dict[str, Any]]:
        """A delivery over the cap: audited, and when it is authentic and its issue is waiting for an agent, a
        comment on the issue says why no job started. Still a 413: hangar didn't take it."""
        sig = request.headers.get("x-hub-signature")
        if sig is not None:
            ok = big.signature is not None and \
                hmac.compare_digest(big.signature.encode(), sig.encode("utf-8", "replace"))
        else:
            ok = token_ok  # the Automation token: the body plays no part
        res: dict[str, Any] = {"error": "body too large"}
        if not ok:
            log.warning("Jira webhook over %d bytes rejected: not authentic", MAX_BODY)
            return 413, res
        t = _HEAD_TIME.search(big.head)
        raw: Any = None
        if t is not None:
            raw = t.group(1).decode("utf-8", "replace") if t.group(1) is not None else _number(t.group(2))
        ts = _timestamp(raw)
        if ts is None or abs(self.clock() - ts) > cfg.max_age_minutes * 60:
            self.jobs.admission.refused("jira:stale", "jira", "hook.stale", None, timestamp=str(ts))
            return 413, res
        m = _HEAD_KEY.search(big.head)
        key = m.group(1).decode() if m else None
        delivery = (request.headers.get("x-atlassian-webhook-identifier") or "").strip()[:100]
        res["issue"] = key
        svc = self.jobs

        async def handle() -> dict[str, Any]:
            svc.admission.refused("jira:too-large", "jira", "jira.too_large", ref_of(key) if key else None,
                                  bytes=big.size, delivery=delivery or None, limit=MAX_BODY)
            if key and key.rsplit("-", 1)[0] in cfg.projects and intake.open_job(svc, ref_of(key)) is None:
                svc._spawn(self._say_too_large(cfg, key))
            return {"ok": True}

        await intake.once(svc, "jira:too-large", big.digest, handle)  # Jira's retry of it: audited once
        return 413, res

    async def _say_too_large(self, cfg: JiraConfig, key: str) -> None:
        """The comment, only when the issue is in the trigger status or has the trigger label now (a large edit of
        an issue nobody asked an agent about says nothing), and at most once a day per issue."""
        replays = self.jobs.admission.replays
        if replays.get("jira:too-large-note", key) is not None:
            return
        api = client_for(self.jobs, cfg)
        if api is None:
            return
        try:
            fields = (await api.issue(key) or {}).get("fields") or {}
            t, status = cfg.trigger, str((fields.get("status") or {}).get("name") or "").casefold()
            if not ((t.status and status == t.status.casefold()) or (t.label and t.label in (fields.get("labels")
                                                                                           or []))):
                return
            replays.put("jira:too-large-note", key, "", LARGE_NOTE_SECONDS)
            await api.comment(key, f"{OWN} hangar couldn't start a job for this issue: Jira's webhook for it was "
                                   f"larger than hangar takes ({MAX_BODY // 1_000_000} MB), most likely because the "
                                   "description is too large. Shorten the description (attach long logs as files "
                                   "instead), then move the issue here again.")
        except (JiraError, httpx.HTTPError) as e:
            log.warning("could not comment on Jira issue %s about its large delivery: %s", key, e)
        finally:
            await api.close()

    async def handle(self, cfg: JiraConfig, payload: dict[str, Any], delivery: str = "",
                     signed: bool = True) -> dict[str, Any]:
        event = str(payload.get("webhookEvent") or "")
        if not signed and event != "hangar:automation":
            # --- automation (#128): the token isn't tied to the body, so a token-authenticated request is only ever
            # an Automation request. Otherwise its holder could send an "issue updated" naming any `user`, or a
            # "comment created" with `/hangar approve` from a linked teammate.
            self.jobs.admission.refused("jira:token-event", "jira:automation", "hook.automation.wrong_event", None,
                                        event=event[:60])
            alert(self.jobs, "wrong-event", f"a request with the Automation token claimed to be a signed webhook "
                                            f"event ('{event[:60]}'), which only the admin webhook sends")
            return {"ignored": "the Automation token only sends hangar:automation requests"}
        if event == "comment_created":
            return await self.on_comment(cfg, payload)
        if event == "hangar:automation":  # an Automation rule: it decided; read the issue as it is now
            key = str((payload.get("issue") or {}).get("key") or "")
            if not _KEY.match(key):
                return {"ignored": "no issue key"}
            api = client_for(self.jobs, cfg)
            if api is None:
                return {"error": f"Jira token credential '{cfg.token_credential}' is not stored"}
            try:
                issue = await api.issue(key)
                # --- review: the token proves the request came through hangar's rule, not who acted: anyone who
                # can edit the rule writes its body (and can send the hidden header elsewhere by changing the
                # URL). Who moved the issue is read from Jira's own history; the body's `user` is ignored.
                change = mover(await api.history(key), (issue.get("fields") or {}), cfg.trigger)
            except (JiraError, httpx.HTTPError) as e:
                return {"error": f"could not read {key}: {e}"}
            finally:
                await api.close()
            refused = self._automation_refusal(cfg, key, change)
            if refused is not None:
                return refused
            res = await self.start(cfg, issue, (change or {}).get("author"), delivery)
            if res.get("ok") and res.get("job"):
                self._count_start(key)
            return res
        if event not in ("jira:issue_updated", "jira:issue_created"):
            return {"ignored": f"event '{event[:60]}'"}
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
        # --- automation (#128): the signed webhook carries issue events: hangar doctor checks they arrive
        self.jobs.store.kv_set(SIGNED_ISSUE_EVENT_KEY, json.dumps(time.time()))
        if not self._triggered(cfg, event, issue, payload.get("changelog")):
            return {"ignored": "not the trigger status or label"}
        return await self.start(cfg, issue, payload.get("user"), delivery)

    # ------------------------------------------------------------------ the Automation fallback (#128)
    def _automation_refusal(self, cfg: JiraConfig, key: str, change: dict[str, Any] | None) -> dict[str, Any] | None:
        """--- automation (#128): the token alone doesn't start a job. Whoever holds it could otherwise start one at
        any time for any issue a trusted person once moved, on team credentials and in that person's name. So the
        issue's history must show the trigger transition (or label) within `automation_max_transition_age_minutes`
        of now (Jira's clock may run up to `max_age_minutes` ahead of hangar's), and each issue starts at most
        `automation_max_starts_per_issue_per_day` jobs a day. None to go on; else the answer, audited (and a stale
        transition alerts the admins)."""
        svc, t = self.jobs, self.clock()
        svc.store.kv_set(AUTOMATION_USED_KEY, json.dumps(time.time()))
        ref = ref_of(key)
        author = (change or {}).get("author")
        account = _account(author, cfg.deployment == "cloud")
        at = _jira_time((change or {}).get("created"))
        age = None if at is None else t - at
        window = cfg.automation_max_transition_age_minutes * 60
        if age is None or age > window or age < -cfg.max_age_minutes * 60:
            detail = {"issue": key, "mover": account, "transition_age_seconds": None if age is None else round(age)}
            svc.admission.refused(f"jira:stale-transition:{key}", "jira:automation",
                                  "hook.automation.stale_transition", ref, **detail)
            what = "no trigger transition in its history" if age is None else \
                f"its last trigger transition was {_ago(age)} ago"
            alert(svc, f"issue:{key}", f"Jira Automation request for {key} ignored: {what} (the rule fires right "
                                       "after the transition, so the token may be in someone else's hands)",
                  issue=key, mover=account, transition_age_seconds=detail["transition_age_seconds"])
            return {"ignored": "no recent transition"}
        starts = self._starts(key, t)
        cap = cfg.automation_max_starts_per_issue_per_day
        if len(starts) >= cap:
            svc.admission.refused(f"jira:automation-cap:{key}", "jira:automation", "hook.automation.capped", ref,
                                  issue=key, mover=account, starts_today=len(starts), cap=cap)
            return {"ignored": f"{key} already started {len(starts)} job(s) from Automation today (cap {cap})"}
        return None

    def _starts(self, key: str, t: float) -> list[float]:
        raw = self.jobs.store.kv_get(f"{STARTS_PREFIX}{key}")
        try:
            xs = [float(x) for x in json.loads(raw)] if raw else []
        except (ValueError, TypeError):
            xs = []
        return [x for x in xs if t - x < 86400]

    def _count_start(self, key: str) -> None:
        t = self.clock()
        self.jobs.store.kv_set(f"{STARTS_PREFIX}{key}", json.dumps([*self._starts(key, t), t]), durable=True)

    @staticmethod
    def _triggered(cfg: JiraConfig, event: str, issue: dict[str, Any], changelog: Any) -> bool:
        t = cfg.trigger
        status = (t.status or "").casefold()
        if event == "jira:issue_created":
            f = issue.get("fields") or {}
            return bool((t.label and t.label in (f.get("labels") or []))
                        or (status and str((f.get("status") or {}).get("name") or "").casefold() == status))
        for item in (changelog or {}).get("items") or [] if isinstance(changelog, dict) else []:
            field = str(item.get("field") or "").casefold()
            if field == "status" and status and str(item.get("toString") or "").casefold() == status:
                return True
            if field == "labels" and t.label:
                before = str(item.get("fromString") or "").split()
                if t.label in str(item.get("toString") or "").split() and t.label not in before:
                    return True
        return False

    # ------------------------------------------------------------------ a new job
    def _repo(self, cfg: JiraConfig, fields: dict[str, Any], project: str) -> tuple[str | None, JiraProject | None]:
        p = cfg.projects.get(project)
        if p is None:
            return None, None
        for c in fields.get("components") or []:
            if isinstance(c, dict) and (repo := p.components.get(str(c.get("name") or ""))):
                return repo, p
        return p.repo, p

    async def start(self, cfg: JiraConfig, issue: dict[str, Any], actor: Any, delivery: str = "",
                    extra: str = "") -> dict[str, Any]:
        svc = self.jobs
        key = str(issue.get("key") or "")
        if not _KEY.match(key):
            return {"ignored": "no issue key"}
        fields = issue.get("fields") or {}
        project = str((fields.get("project") or {}).get("key") or key.rsplit("-", 1)[0])
        repo, proj = self._repo(cfg, fields, project)
        if repo is None or proj is None:
            return {"ignored": f"Jira project {project} is not mapped to a repo (HANGAR_JIRA projects)"}
        url = _repo_url(repo)
        ref = ref_of(key)
        if not svc.repo_envs(url):
            svc._spawn(notify(svc, None, ref, f"hangar can't start a job for this issue: {repo} isn't in "
                                              "HANGAR_GITHUB_REPOS. An admin can add it (docs/intake.md).", "note"))
            return {"error": f"{repo} is not in HANGAR_GITHUB_REPOS"}
        cloud = cfg.deployment == "cloud"
        account = _account(actor, cloud)
        who = _who(actor, account)
        person = intake.person_for(svc.store, "jira", account)
        reporter = fields.get("reporter")
        summary = " ".join(str(fields.get("summary") or "").split())[:300] or key
        labels = ", ".join(str(x) for x in fields.get("labels") or [])
        comps = ", ".join(str(c.get("name")) for c in fields.get("components") or [] if isinstance(c, dict))
        meta = [f"Reported by {_who(reporter, _account(reporter, cloud))}",
                *([f"type {(fields.get('issuetype') or {}).get('name')}"] if fields.get("issuetype") else []),
                *([f"labels: {labels}"] if labels else []), *([f"components: {comps}"] if comps else [])]
        description = adf_text(fields.get("description")).strip() or "(no description)"
        # --- review: always untrusted: anyone with Edit Issues can rewrite the summary and description, whoever
        # reported it; and the summary stays out of the instructions
        context = [ContextItem(name=f"jira-{key}.md", kind="issue", untrusted=True, content=(
            f"# {key}: {summary}\n\n{description[:50_000]}\n\n---\n{cfg.url}/browse/{key}\n" + "; ".join(meta)))]
        instructions = (f"Resolve Jira issue {key}. Its summary, description and details are in the context file "
                        f"`jira-{key}.md`.")
        if extra and person:
            instructions += f"\n\nAdditional instructions from {who}:\n{extra}"
        requester = f"jira:{account or 'unknown'}"
        req = SubmitJob(instructions=instructions, title=f"{key}: {summary}"[:200], repo=url,
                        environment=proj.environment, context=context,
                        idempotency_key=f"jira-delivery:{delivery}" if delivery else None,
                        labels={"jira.issue": key, "jira.project": project, intake.TRACKER: ref,
                                **autoinstall.kind_labels(svc.settings, [  # --- autoinstall: an env setup
                                    str(x) for x in fields.get("labels") or []])})
        intake.apply_ticket(svc, req, description, key)  # --- #156: acceptance criteria and a suggested check
        res = intake.submit(svc, req, source="jira", ref=ref, requester=requester, person=person,
                            trusted=person is not None, who=who)
        job = svc._jobs.get(res.get("job") or "")
        if res.get("error"):
            svc._spawn(notify(svc, None, ref, f"hangar couldn't start a job for this issue: {res['error']}", "note"))
        elif res.get("ok") and job is not None:
            if job.state == JobState.AWAITING_APPROVAL and job.approval and job.untrusted_source:
                note = (f"Hangar job `{job.id}` is waiting for approval: {who} isn't linked to a hangar person. A "
                        "linked teammate can comment `/hangar approve` to run it, or `/hangar reject` to drop it.")
            elif job.state == JobState.AWAITING_APPROVAL:
                from hangar.jobbudget import held_message

                note = held_message(job)
            else:
                note = (f"Queued as hangar job `{job.id}` in `{job.environment}` on {parse_repo(url) or url}. "
                        "I'll comment here when the agent starts and again with the result."
                        + fairness.queued_note(svc, job)  # --- review capacity (#159)
                        + intake.queued_notes(svc, job, "jira"))  # --- #156
            svc._spawn(notify(svc, job, ref, note, "note"))
        return res

    # ------------------------------------------------------------------ comments
    async def on_comment(self, cfg: JiraConfig, payload: dict[str, Any]) -> dict[str, Any]:
        """The job's person's comment answers its question (a team job's: any linked teammate's), a linked
        teammate's `/hangar approve|reject` decides an approval, and the job's person's `/hangar <message>` continues
        the last job (anyone linked: starts one). Nobody else's comment steers anything."""
        svc = self.jobs
        comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
        key = str(issue.get("key") or "")
        if not _KEY.match(key):
            return {"ignored": "no issue key"}
        text = adf_text(comment.get("body")).strip()
        if not text or text.startswith(OWN):
            return {"ignored": "hangar's own or an empty comment"}
        author = comment.get("author") or comment.get("updateAuthor")
        account = _account(author, cfg.deployment == "cloud")
        who = _who(author, account)
        person = intake.person_for(svc.store, "jira", account)
        ref = ref_of(key)
        command = text.startswith("/hangar")
        rest = text[len("/hangar"):].strip() if command else text
        job = intake.open_job(svc, ref)
        if person is None:
            if command or (job is not None and job.state == JobState.NEEDS_INPUT):
                svc.admission.refused(f"owner:jira:{account}", f"jira:{account}", "jira.refused", ref,
                                      why="not linked to a person")
            return {"ignored": f"{who} is not linked to a hangar person"}
        requester = f"jira:{account}"
        word = (rest.split() or [""])[0].lower()
        if command and word in ("approve", "reject"):
            return await self._decide(job, ref, word, person, account)
        if job is not None and job.state == JobState.NEEDS_INPUT:
            return self._follow_up(job, rest or "Continue with the task.", requester, ref, person)
        if not command:
            return {"ignored": "not a /hangar comment"}
        if job is not None:
            svc._spawn(notify(svc, job, ref, f"Job `{job.id}` is {job.state}; comment again once it's done.",
                              "note"))
            return {"ignored": f"job {job.id} is already {job.state} for {ref}"}
        last = intake.latest_job(svc, ref)
        if last is not None and last.state in JOB_TERMINAL:
            return self._follow_up(last, rest or "Continue with the task.", requester, ref, person)
        issue_now = issue
        if not (issue.get("fields") or {}).get("project"):
            api = client_for(svc, cfg)
            if api is None:
                return {"error": f"Jira token credential '{cfg.token_credential}' is not stored"}
            try:
                issue_now = await api.issue(key)
            except (JiraError, httpx.HTTPError) as e:
                return {"error": f"could not read {key}: {e}"}
            finally:
                await api.close()
        return await self.start(cfg, issue_now, author, extra=rest)

    async def _decide(self, job: Job | None, ref: str, word: str, person: str, account: str) -> dict[str, Any]:
        from hangar import approvals

        if job is None or job.state != JobState.AWAITING_APPROVAL:
            return {"ignored": f"no job is waiting for approval on {ref}"}
        try:  # --- approvals (#121): never the job's own person or requester
            await approvals.decide(self.jobs, job.id, approvals.from_jira(account, person), word)  # type: ignore[arg-type]
        except approvals.ApprovalRefused as e:  # audited by decide; say why on the issue
            self.jobs._spawn(notify(self.jobs, job, ref, f"{person} can't {word} hangar job `{job.id}`: {e}", "note"))
            return {"ignored": str(e)}
        except JobError as e:
            return {"ignored": str(e)}
        msg = (f"Approved by {person}. Hangar job `{job.id}` is queued." if word == "approve"
               else f"Rejected by {person}. Hangar job `{job.id}` will not run.")
        self.jobs._spawn(notify(self.jobs, job, ref, msg, "note"))
        return {"ok": True, {"approve": "approved", "reject": "rejected"}[word]: job.id}

    def _follow_up(self, job: Job, text: str, requester: str, ref: str, person: str) -> dict[str, Any]:
        svc = self.jobs
        try:  # integration: the front door's check is #91's, in one place: only a job's person continues it here
            svc.follow_up(job.id, text, requester=requester, reply_to=ref, front_door=True, sender_person=person)
        except JobError as e:
            svc._spawn(notify(svc, job, ref, f"Can't continue `{job.id}`: {e}", "note"))
            return {"ignored": str(e)}
        svc._spawn(notify(svc, job, ref, f"Continuing hangar job `{job.id}` with your comment. I'll comment here "
                                         "when it's done.", "note"))
        return {"ok": True, "follow_up": job.id}


# ============================================================================ hangar doctor
async def doctor_checks(h: Any, transport: httpx.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
    """HANGAR_JIRA valid, its credentials stored, the token accepted, and each project's repo allowlisted."""
    from hangar.doctor import _check
    from hangar.models import Credential

    if not h.settings.jira:
        return []
    try:
        cfg = load(h.settings)
    except JiraConfigError as e:
        return [_check("jira config", False, str(e), "fix HANGAR_JIRA (docs/intake.md)")]
    assert cfg is not None
    out: list[dict[str, Any]] = []
    stored = {c.name for c in h.store.list(Credential)}
    for name in cfg.credentials():
        out.append(_check(f"jira credential {name}", name in stored, "stored" if name in stored else "missing",
                          f"hangar cred add {name} --harness webhook --kind generic   (paste it, then Ctrl-D)"))
    if cfg.token_credential in stored:
        try:
            api = JiraAPI(cfg, h.broker.secret_text(cfg.token_credential), transport=transport)
        except Exception as e:  # noqa: BLE001
            out.append(_check("jira api", False, f"token unusable: {e}", "store the token again"))
        else:
            try:
                me = await api.myself()
                out.append(_check("jira api", True, f"token works: {me.get('displayName') or me.get('name') or '?'}"))
            except (JiraError, httpx.HTTPError) as e:
                out.append(_check("jira api", False, f"token rejected: {e}",
                                  "store a current API token (Cloud) or personal access token (Data Center)"))
            finally:
                await api.close()
    out += automation_checks(h, cfg, stored)
    door = " or ".join(f"{k} '{v}'" for k, v in (("status", cfg.trigger.status), ("label", cfg.trigger.label)) if v)
    stages = ", ".join(f"{k} -> {v}" for k, v in cfg.statuses.model_dump().items() if v) or "the status is left alone"
    out.append(_check("jira trigger", True, f"an issue reaching {door} starts a job; {stages}"))  # --- #156
    for project, p in cfg.projects.items():
        for name, repo in [(project, p.repo), *((f"{project} component {c}", r) for c, r in p.components.items())]:
            ok = bool(h.jobs.repo_envs(_repo_url(repo)))
            out.append(_check(f"jira project {name}", ok, f"-> {repo}" if ok else
                              f"mapped to {repo}, which isn't in HANGAR_GITHUB_REPOS",
                              f"add {repo} to HANGAR_GITHUB_REPOS with its environment, or fix the mapping"))
    return out


def automation_checks(h: Any, cfg: JiraConfig, stored: set[str] | None = None) -> list[dict[str, Any]]:
    """--- automation (#128): the Automation token is a fallback. Warn when it is older than credhealth.ROTATE_DAYS,
    and when it is configured while issue events don't come through the signed webhook."""
    from hangar import credhealth
    from hangar.doctor import _check
    from hangar.models import Credential

    name = cfg.automation_token_credential
    if not name or cfg.deployment != "cloud":
        return []
    out: list[dict[str, Any]] = []
    c = h.store.get(Credential, name)
    if c is not None:
        days = credhealth.age_days(c)
        old = days > credhealth.ROTATE_DAYS
        out.append(_check("jira automation token age", None if old else True,
                          f"{name} was stored {days:.0f} days ago" + (f" (over {credhealth.ROTATE_DAYS})" if old
                                                                      else ""),
                          f"hangar cred add {name} --rotate, then update the rule's X-Hangar-Token header"))
    raw = h.store.kv_get(SIGNED_ISSUE_EVENT_KEY)
    try:
        signed_at = float(json.loads(raw)) if raw else None
    except (ValueError, TypeError):
        signed_at = None
    recent = signed_at is not None and time.time() - signed_at < SIGNED_RECENT_DAYS * 86400
    if not cfg.webhook_secret_credential:
        why = "no webhook_secret_credential: issue events come only through the unsigned Automation fallback"
    elif not recent:
        why = (f"the signed webhook delivered no issue event in {SIGNED_RECENT_DAYS} days: issue events seem to come "
               "only through the unsigned Automation fallback")
    else:
        why = ""
    out.append(_check("jira signed issue events", None if why else True,
                      why or "issue events arrive through the signed webhook; the Automation token is a fallback",
                      "send Issue created/updated through the admin webhook with a secret (docs/intake.md), then "
                      "remove automation_token_credential"))
    return out
