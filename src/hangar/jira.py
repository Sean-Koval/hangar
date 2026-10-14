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
sandbox): a comment when the agent starts, the agent's question (a comment by a linked teammate answers it), and
the outcome with the PR, which is also added as a remote link. A configured status transition goes with each stage.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import math
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hangar import intake
from hangar.exchange import open_question
from hangar.github import parse_repo, verify_signature
from hangar.jobs import NOTIFIERS, JobError, JobService, SubmitJob
from hangar.models import JOB_TERMINAL, ContextItem, Job, JobState

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger("hangar.jira")

MAX_BODY = 1_000_000
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
    return inner + "\n\n" if t in ("paragraph", "heading", "blockquote", "codeBlock", "listItem") else inner


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
            "fields": "summary,description,labels,components,project,status,reporter,issuetype,priority"})

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


def _who(user: Any, account: str | None) -> str:
    name = str((user or {}).get("displayName") or "") if isinstance(user, dict) else ""
    return " ".join(name.split())[:80] or account or "someone"


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
        body = await intake.read_body(request, MAX_BODY)
        if body is None:
            return 413, {"error": "body too large"}
        try:
            ok, why = self._authentic(cfg, request, body)
        except Exception:  # noqa: BLE001 - say nothing about the vault to an anonymous caller
            log.error("Jira webhook credentials are not usable")
            return 503, {"error": "Jira intake is misconfigured"}
        if not ok:
            log.warning("Jira webhook rejected: %s", why)
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
        res = await intake.once(self.jobs, "jira", digest, lambda: self.handle(cfg, payload, delivery))
        if res is None:
            self.jobs.admission.refused("jira:replay", "jira", "hook.replay", delivery or None)
            return 200, {"ignored": "duplicate delivery"}
        return (429 if res.get("rate_limited") else 200), res

    async def handle(self, cfg: JiraConfig, payload: dict[str, Any], delivery: str = "") -> dict[str, Any]:
        event = str(payload.get("webhookEvent") or "")
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
            except (JiraError, httpx.HTTPError) as e:
                return {"error": f"could not read {key}: {e}"}
            finally:
                await api.close()
            return await self.start(cfg, issue, payload.get("user"), delivery)
        if event not in ("jira:issue_updated", "jira:issue_created"):
            return {"ignored": f"event '{event[:60]}'"}
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
        if not self._triggered(cfg, event, issue, payload.get("changelog")):
            return {"ignored": "not the trigger status or label"}
        return await self.start(cfg, issue, payload.get("user"), delivery)

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
        reporter_linked = intake.person_for(svc.store, "jira", _account(reporter, cloud)) is not None
        summary = " ".join(str(fields.get("summary") or "").split())[:300] or key
        labels = ", ".join(str(x) for x in fields.get("labels") or [])
        comps = ", ".join(str(c.get("name")) for c in fields.get("components") or [] if isinstance(c, dict))
        meta = [f"Reported by {_who(reporter, _account(reporter, cloud))}",
                *([f"type {(fields.get('issuetype') or {}).get('name')}"] if fields.get("issuetype") else []),
                *([f"labels: {labels}"] if labels else []), *([f"components: {comps}"] if comps else [])]
        description = adf_text(fields.get("description")).strip() or "(no description)"
        context = [ContextItem(name=f"jira-{key}.md", kind="issue", untrusted=not reporter_linked, content=(
            f"# {key}: {summary}\n\n{description[:50_000]}\n\n---\n{cfg.url}/browse/{key}\n" + "; ".join(meta)))]
        instructions = (f"Resolve Jira issue {key}: {summary}\n\nThe issue text is in the context file "
                        f"`jira-{key}.md`.")
        if extra and person:
            instructions += f"\n\nAdditional instructions from {who}:\n{extra}"
        requester = f"jira:{account or 'unknown'}"
        req = SubmitJob(instructions=instructions, title=f"{key}: {summary}"[:200], repo=url,
                        environment=proj.environment, context=context,
                        idempotency_key=f"jira-delivery:{delivery}" if delivery else None,
                        labels={"jira.issue": key, "jira.project": project, intake.TRACKER: ref})
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
                        "I'll comment here when the agent starts and again with the result.")
            svc._spawn(notify(svc, job, ref, note, "note"))
        return res

    # ------------------------------------------------------------------ comments
    async def on_comment(self, cfg: JiraConfig, payload: dict[str, Any]) -> dict[str, Any]:
        """A linked teammate's comment answers the job's question, `/hangar approve|reject` decides an approval,
        and `/hangar <message>` continues the last job (or starts one). Nobody else's comment steers anything."""
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
            return self._decide(job, ref, word, person)
        if job is not None and job.state == JobState.NEEDS_INPUT:
            return self._follow_up(job, rest or "Continue with the task.", requester, ref)
        if not command:
            return {"ignored": "not a /hangar comment"}
        if job is not None:
            svc._spawn(notify(svc, job, ref, f"Job `{job.id}` is {job.state}; comment again once it's done.",
                              "note"))
            return {"ignored": f"job {job.id} is already {job.state} for {ref}"}
        last = intake.latest_job(svc, ref)
        if last is not None and last.state in JOB_TERMINAL:
            return self._follow_up(last, rest or "Continue with the task.", requester, ref)
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

    def _decide(self, job: Job | None, ref: str, word: str, person: str) -> dict[str, Any]:
        from hangar.triggers import approve, reject

        if job is None or job.state != JobState.AWAITING_APPROVAL:
            return {"ignored": f"no job is waiting for approval on {ref}"}
        try:
            (approve if word == "approve" else reject)(self.jobs, job.id, f"jira:{person}")
        except JobError as e:
            return {"ignored": str(e)}
        msg = (f"Approved by {person}. Hangar job `{job.id}` is queued." if word == "approve"
               else f"Rejected by {person}. Hangar job `{job.id}` will not run.")
        self.jobs._spawn(notify(self.jobs, job, ref, msg, "note"))
        return {"ok": True, {"approve": "approved", "reject": "rejected"}[word]: job.id}

    def _follow_up(self, job: Job, text: str, requester: str, ref: str) -> dict[str, Any]:
        svc = self.jobs
        try:
            svc.follow_up(job.id, text, requester=requester, reply_to=ref)
        except JobError as e:
            svc._spawn(notify(svc, job, ref, f"Can't continue yet: {e}", "note"))
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
    for project, p in cfg.projects.items():
        for name, repo in [(project, p.repo), *((f"{project} component {c}", r) for c, r in p.components.items())]:
            ok = bool(h.jobs.repo_envs(_repo_url(repo)))
            out.append(_check(f"jira project {name}", ok, f"-> {repo}" if ok else
                              f"mapped to {repo}, which isn't in HANGAR_GITHUB_REPOS",
                              f"add {repo} to HANGAR_GITHUB_REPOS with its environment, or fix the mapping"))
    return out
