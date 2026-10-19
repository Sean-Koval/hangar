"""Slack front door (spec 0011 workstream B, spec 0009 M6).

`/hangar <instructions>` or `@hangar <instructions>` in a mapped channel starts a job on that channel's repo.
Hangar answers in a thread: when the agent starts, when it needs input, and when it finishes (outcome, summary,
PR, verification). A reply in that thread is a follow-up on the job.

Every request is checked against the app's signing secret before anything else. The bot token stays in the
control plane's vault; sandboxes never see it. See docs/slack.md for setup.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import parse_qsl

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from hangar import fairness as fairness  # --- review capacity (#159)
from hangar import (  # --- teamcreds: linked Slack users; --- trust: commit after handle
    people,
    redeliver,
    steer_class,  # --- #162: classifying follow-ups
)
from hangar.exchange import open_question
from hangar.ext import NoSettings  # --- plugin host
from hangar.github import parse_repo
from hangar.jobs import NOTIFIERS, Caller, JobError, JobNotFound, JobService, SubmitJob
from hangar.models import ContextItem, Credential, Job

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger("hangar.slack")

MAX_AGE_SECONDS = 300  # Slack: reject requests whose timestamp is more than five minutes from local time
MAX_BODY = 1_000_000
TEXT_LIMIT = 3900  # Slack advises at most 4,000 characters per message
THREAD_CONTEXT_MESSAGES = 15  # conversations.replies caps `limit` at 15 for some apps
THREAD_CONTEXT_CHARS = 20_000
SEEN_TTL = 3600  # Slack retries for about 5 minutes; keep delivery ids an hour

_ID = re.compile(r"^[A-Z0-9]{2,32}$")  # team, channel and user ids
_TS = re.compile(r"^\d{1,12}\.\d{1,9}$")
_KEY = re.compile(r"^[\w.:-]{1,120}$")
_OPT = re.compile(r"^(repo|env|harness|model)=(\S{1,200})$")
_REPO_SHORT = re.compile(r"^[\w.-]+/[\w.-]+$")
_LEAD_MENTION = re.compile(r"^\s*<@[A-Z0-9]+(?:\|[^>]*)?>[\s:,]*")
_ANGLE = re.compile(r"<([^<>|]*)(?:\|([^<>]*))?>")
_PR_URL = re.compile(r"^https://[^\s<>|]+$")
_REF = re.compile(r"^slack:([A-Z0-9]{2,32}):(\d{1,12}\.\d{1,9})$")

HELP = (
    "*hangar* runs a coding agent on this channel's repo and reports back in a thread.\n"
    "• `/hangar <instructions>` (or `@hangar <instructions>`): start a job\n"
    "• `/hangar repo=owner/repo env=NAME harness=NAME model=NAME <instructions>`: choose where and how it runs\n"
    "• `/hangar status JOB`: where a job is\n"
    "• Reply in a job's thread to follow up once the agent's turn is done. Start the reply with `fix:`, "
    "`redirect:` or `info:` to say whether it corrects the agent, changes the task or adds what it didn't know.\n"
    "• `@hangar adr [title]` in a thread: draft an ADR from the thread as a PR (`docs/decisions/`)"
)


# ============================================================================ helpers
def verify_signature(secret: str, timestamp: str | None, body: bytes, signature: str | None,
                     at: float | None = None) -> bool:
    """Slack's v0 scheme: `v0=` + hex HMAC-SHA256 of `v0:{timestamp}:{raw body}`, within five minutes."""
    if not timestamp or not signature or not signature.startswith("v0="):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if at is None else at) - ts) > MAX_AGE_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    want = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want.encode(), signature.encode("utf-8", "replace"))  # bytes: see github.py


def escape(text: str) -> str:
    """Slack treats &, < and > as control characters: escaped, text can't ping @channel or fake links."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plain(text: str) -> str:
    """Slack message text as plain text for the agent: links, mentions and entities decoded."""
    def sub(m: re.Match) -> str:
        target, label = m.group(1), m.group(2)
        if target[:1] in ("@", "#", "!"):
            return ("#" if target[0] == "#" else "@") + (label or target[1:])
        return target if not label or label == target else f"{label} ({target})"

    return _ANGLE.sub(sub, text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _quote(text: str, limit: int = 1000) -> str:
    return "\n".join(f"> {ln}" for ln in _clip(text.strip(), limit).splitlines()[:20]) or "> (empty)"


def parse_command(text: str) -> tuple[dict[str, str], str]:
    """`repo=o/r env=x harness=y model=z free text...` -> ({repo, env, harness, model}, text).

    Options are only read from the start of the first line, so instructions can mention `x=y` freely."""
    first, _, more = text.strip().partition("\n")
    words = first.split()
    opts: dict[str, str] = {}
    while words and (m := _OPT.match(words[0])):
        opts[m.group(1)] = m.group(2)
        words.pop(0)
    rest = " ".join(words)
    if more.strip():
        rest = f"{rest}\n{more.strip()}".strip()
    return opts, rest


def thread_ref(channel: str, ts: str) -> str:
    return f"slack:{channel}:{ts}"


def parse_ref(ref: str) -> tuple[str, str] | None:
    m = _REF.match(ref)
    return (m.group(1), m.group(2)) if m else None


def _repo_url(value: str) -> str:
    """`owner/repo` means GitHub; anything else is a git URL matched exactly in HANGAR_GITHUB_REPOS."""
    v = value.strip()
    return f"https://github.com/{v}.git" if _REPO_SHORT.match(v) else v


def _repo_name(url: str | None) -> str:
    return parse_repo(url) or url or "(no repo)"


def _thread_key(channel: str, ts: str) -> str:
    return f"slack:thread:{channel}:{ts}"


# ============================================================================ Web API client
class SlackError(RuntimeError):
    pass


class SlackAPI:
    """The few Web API methods hangar uses. Retries rate limits (honouring Retry-After) and transient errors."""

    def __init__(self, token: str, api_url: str = "https://slack.com/api",
                 transport: httpx.AsyncBaseTransport | None = None):
        self._fast = transport is not None  # tests: don't sleep between retries
        self.scopes: set[str] | None = None  # the token's scopes, from the last answer's X-OAuth-Scopes (doctor)
        self._client = httpx.AsyncClient(base_url=api_url.rstrip("/"), transport=transport, timeout=15,
                                         headers={"Authorization": f"Bearer {token}", "User-Agent": "hangar"})

    async def close(self) -> None:
        await self._client.aclose()

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(0 if self._fast else seconds)

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        data = {k: ("true" if v is True else "false" if v is False else str(v))
                for k, v in params.items() if v is not None}
        err = "no attempt"
        for attempt in range(3):
            try:
                r = await self._client.post(f"/{method}", data=data)
            except httpx.HTTPError as e:
                err = type(e).__name__  # never the request (it carries the token)
                await self._sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                err = f"HTTP {r.status_code}"
                await self._sleep(_retry_after(r, 2 ** attempt))
                continue
            if (granted := r.headers.get("x-oauth-scopes")) is not None:
                self.scopes = {x.strip() for x in granted.split(",") if x.strip()}
            try:
                body = r.json()
            except ValueError as e:
                raise SlackError(f"Slack {method}: HTTP {r.status_code}, not JSON") from e
            if not body.get("ok"):
                err = str(body.get("error") or "unknown error")[:200]
                if err == "ratelimited":
                    await self._sleep(_retry_after(r, 1))
                    continue
                raise SlackError(f"Slack {method}: {err}")
            return body
        raise SlackError(f"Slack {method}: {err} (gave up after 3 attempts)")

    async def post(self, channel: str, text: str, thread_ts: str | None = None,
                   blocks: list[dict[str, Any]] | None = None) -> str:
        res = await self.call("chat.postMessage", channel=channel, text=_clip(text), thread_ts=thread_ts,
                              unfurl_links=False, unfurl_media=False,
                              blocks=json.dumps(blocks) if blocks else None)
        return str(res.get("ts") or "")

    async def thread(self, channel: str, ts: str, before: str, limit: int) -> list[dict[str, Any]]:
        res = await self.call("conversations.replies", channel=channel, ts=ts, latest=before, inclusive=False,
                              limit=limit)
        return [m for m in res.get("messages") or [] if isinstance(m, dict)]

    async def auth_test(self) -> dict[str, Any]:
        return await self.call("auth.test")

    async def open_dm(self, user: str) -> str:
        """--- inbox delivery (#157): the app's DM channel with a user (conversations.open, needs `im:write`)."""
        res = await self.call("conversations.open", users=user, return_im=False)
        channel = str((res.get("channel") or {}).get("id") or "")
        if not _ID.match(channel):
            raise SlackError("Slack conversations.open: no channel id")
        return channel

    async def permalink(self, channel: str, ts: str) -> str | None:
        """A message's permalink (chat.getPermalink), or None."""
        res = await self.call("chat.getPermalink", channel=channel, message_ts=ts)
        url = str(res.get("permalink") or "")
        return url if _PR_URL.match(url) else None


def _retry_after(r: httpx.Response, default: float) -> float:
    try:
        return min(float(r.headers.get("retry-after", default)), 30.0)
    except ValueError:
        return default


def client_for(svc: JobService) -> SlackAPI | None:
    """A Web API client with the bot token, or None if it isn't configured or stored."""
    name = svc.settings.slack_bot_token_credential
    if not name:
        return None
    try:
        token = svc.broker.secret_text(name)
    except Exception:  # noqa: BLE001
        log.warning("Slack bot token credential %s is not usable", name)
        return None
    return SlackAPI(token, svc.settings.slack_api_url, transport=svc.transport)


# ============================================================================ thread messages
def _cause(job: Job) -> str | None:
    """What went wrong: the last event that isn't the outcome line or a routine delivery step."""
    if str(job.state) != "failed" or job.reason in (None, "verify_failed", "no_changes"):
        return None
    return next((e.message for e in reversed(job.events)
                 if not e.message.startswith(("failed", "succeeded", "cancelled", "delivering", "verifying"))), None)


def render(job: Job | None, body: str, kind: str) -> str:
    """Slack text for a job notification. `body` is GitHub-flavoured; the outcome report is rebuilt for Slack."""
    if job is not None and str(job.state) == "needs_input":
        q = open_question(job) or body  # --- interactions: the one place the question is read from
        return (f"*Needs input*: hangar job `{job.id}` has a question.\n{_quote(escape(q), 2500)}\n"
                "Reply in this thread to answer; the agent picks up where it left off.")
    if job is None or kind != "final":
        return _clip(re.sub(r"\*\*(.+?)\*\*", r"*\1*", escape(body)))
    r = job.result
    state = str(job.state)
    head = {"succeeded": "Done", "failed": "Finished with problems", "cancelled": "Cancelled"}.get(state, state)
    lines = [f"*{head}*: hangar job `{job.id}`" + (f" ({escape(job.reason)})" if job.reason else "")]
    if r.pr_url and _PR_URL.match(r.pr_url):
        lines.append(f"PR: <{r.pr_url}|{escape(_repo_name(job.repo))}#{r.pr_number or ''}>")
    elif r.pushed:
        lines.append(f"Branch: `{escape(job.branch)}`")
    if job.verify and r.verify_exit_code is not None:
        lines.append(f"Verification `{escape(job.verify)}`: {'passed' if r.verify_exit_code == 0 else 'failed'}")
    if cause := _cause(job):
        lines.append(f"What went wrong: {escape(cause[:1000])}")
    if r.summary:
        lines.append(_quote(escape(r.summary), 2500))
    lines.append("Reply in this thread to continue where it left off.")
    return _clip("\n".join(lines))


async def notify(svc: JobService, job: Job | None, ref: str, body: str, kind: str) -> None:
    """The `slack:` notifier: post into the job's thread. Failures land on the job's timeline, never raise."""
    where = parse_ref(ref)
    if where is None:
        return
    api = client_for(svc)
    if api is None:
        if job is not None:
            svc._event(job, f"could not post to Slack thread {ref}: HANGAR_SLACK_BOT_TOKEN_CREDENTIAL is not set "
                            "or not stored")
        return
    try:
        await api.post(where[0], svc.broker.redact(render(job, body, kind)), thread_ts=where[1])
    except SlackError as e:
        if job is not None:
            svc._event(job, f"could not post to Slack thread {ref}: {e}")
        else:
            log.warning("could not post to Slack thread %s: %s", ref, e)
    finally:
        await api.close()


# The thread notifier is registered at import too, so a JobService works without the app (the CLI, tests); the
# plugin's register() is what the app runs, and adds the door (plugin host, docs/plugins.md).
NOTIFIERS["slack"] = notify


# ============================================================================ intake
class _Refused(ValueError):
    pass


class _Plan(NamedTuple):
    repo: str
    env: str | None
    harness: str | None
    model: str | None
    instructions: str


class SlackFrontDoor:
    """The two Slack endpoints. Both answer within Slack's 3 seconds and do the slow part in the background."""

    def __init__(self, jobs: JobService):
        self.jobs = jobs
        self.settings = jobs.settings
        self.store = jobs.store
        for k in self.store.kv_keys("slack:seen:"):  # --- trust: the old marks; the replay guard keeps them now
            self.store.kv_delete(k)

    def mount(self, app: FastAPI) -> None:
        @app.post("/api/hooks/slack/commands")
        async def slack_command(request: Request):
            body = await self._verified(request)
            # --- front-door-auth: Slack doesn't retry slash commands, so a second copy of a signed one (inside the
            # five-minute window) is a replay. Events are deduplicated by event_id instead.
            replays = self.jobs.admission.replays
            digest = replays.digest(request.headers.get("x-slack-signature", "").encode())
            if replays.get("slack:command", digest) is not None:
                self.jobs.admission.refused("slack:replay", "slack", "hook.replay", "/api/hooks/slack/commands")
                return JSONResponse(_ephemeral("That request was already handled."))
            replays.put("slack:command", digest, "", 2 * MAX_AGE_SECONDS)
            form = dict(parse_qsl(body.decode(errors="replace"), keep_blank_values=True))
            return JSONResponse(self.command(form))

        @app.post("/api/hooks/slack/events")
        async def slack_events(request: Request):
            body = await self._verified(request)
            try:
                payload = json.loads(body)
            except ValueError as e:
                raise HTTPException(400, "body is not JSON") from e
            if not isinstance(payload, dict):
                raise HTTPException(400, "body is not a JSON object")
            if payload.get("type") == "url_verification":
                return JSONResponse({"challenge": str(payload.get("challenge") or "")[:500]})
            return JSONResponse(self.event(payload, request.headers.get("x-slack-retry-num")))

        @app.post("/api/hooks/slack/interactions")
        async def slack_interactions(request: Request):
            """--- #162: block actions (the steer-class buttons), verified and replay-guarded like commands."""
            body = await self._verified(request)
            replays = self.jobs.admission.replays
            digest = replays.digest(request.headers.get("x-slack-signature", "").encode())
            if replays.get("slack:interaction", digest) is not None:
                self.jobs.admission.refused("slack:replay", "slack", "hook.replay", "/api/hooks/slack/interactions")
                return JSONResponse({"ignored": "replay"}, status_code=200)
            replays.put("slack:interaction", digest, "", 2 * MAX_AGE_SECONDS)
            form = dict(parse_qsl(body.decode(errors="replace"), keep_blank_values=True))
            try:
                payload = json.loads(form.get("payload") or "")
            except ValueError as e:
                raise HTTPException(400, "payload is not JSON") from e
            if not isinstance(payload, dict):
                raise HTTPException(400, "payload is not a JSON object")
            return JSONResponse(self.interaction(payload))

    async def _verified(self, request: Request) -> bytes:
        name = self.settings.slack_signing_secret_credential
        if not name:
            raise HTTPException(404, "Slack is not configured (HANGAR_SLACK_SIGNING_SECRET_CREDENTIAL)")
        body = b""
        async for chunk in request.stream():  # front-door-auth: stop reading at the cap, not after the whole body
            body += chunk
            if len(body) > MAX_BODY:
                raise HTTPException(413, "request too large")
        try:
            secret = self.jobs.broker.secret_text(name)
        except Exception as e:  # noqa: BLE001
            log.error("Slack signing secret credential %s is not usable", name)
            raise HTTPException(503, "Slack signing secret is not stored") from e
        if not verify_signature(secret, request.headers.get("x-slack-request-timestamp"), body,
                                request.headers.get("x-slack-signature")):
            raise HTTPException(401, "bad signature")
        return body

    # ------------------------------------------------------------------ policy
    def _person(self, team: str, user: str, user_team: str | None = None) -> str | None:
        """--- review: the person this Slack user is linked to, keyed by their own workspace (Slack user ids are
        unique only within one, and a Slack Connect channel carries other organizations' users)."""
        p = people.resolve_external(self.store, "slack", f"{user_team or team}/{user}")
        return p.id if p is not None else None

    def _refusal(self, team: str, channel: str, user: str, user_team: str | None = None) -> str | None:
        if channel not in self.settings.slack_channels:
            return ("This channel isn't connected to a repo. An admin can map it in HANGAR_SLACK_CHANNELS "
                    "(docs/slack.md).")
        if self._person(team, user, user_team) is not None:  # --- review: a linked account acts as its person
            return None
        users = self.settings.slack_users
        # --- front-door-auth: in a Slack Connect channel, members of other organizations post too. They count
        # only when listed by id, even when the allowlist is otherwise empty.
        if user_team and user_team != team and user_team not in self.settings.slack_teams:
            if user not in users and f"{user_team}/{user}" not in users:
                return "You're not on hangar's Slack allowlist (HANGAR_SLACK_USERS)."
            return None
        if users and user not in users and f"{team}/{user}" not in users:
            return "You're not on hangar's Slack allowlist (HANGAR_SLACK_USERS)."
        return None

    def _plan(self, channel: str, text: str) -> _Plan:
        opts, instructions = parse_command(text)
        value = opts.get("repo") or self.settings.slack_channels.get(channel) or ""
        url = _repo_url(value)
        envs = self.jobs.repo_envs(url)
        if not envs:
            raise _Refused(f"`{escape(value)}` isn't in HANGAR_GITHUB_REPOS, so hangar can't run jobs on it.")
        env = opts.get("env")
        if env and env not in envs:
            raise _Refused(f"Environment `{escape(env)}` isn't allowed for {escape(_repo_name(url))} "
                           f"(allowed: {escape(', '.join(envs))}).")
        if not instructions.strip():
            raise _Refused("Say what to do, e.g. `/hangar fix the flaky test in tests/test_api.py`.")
        return _Plan(url, env, opts.get("harness"), opts.get("model"), instructions)

    def _help(self, channel: str) -> str:
        repo = self.settings.slack_channels.get(channel)
        return HELP + (f"\nThis channel works on `{escape(repo)}`." if repo else "")

    def _status(self, job_id: str) -> str:
        job_id = job_id.strip().strip("`")
        try:
            job = self.jobs.get(job_id) if job_id else None
        except JobNotFound:
            job = None
        if job is None or not self.jobs.repo_envs(job.repo):  # only jobs on repos Slack may use
            return f"No job `{escape(job_id[:80])}`." if job_id else "Usage: `/hangar status JOB`"
        why = f" ({escape(job.reason)})" if job.reason else ""
        lines = [f"`{job.id}` *{escape(job.title)}* is *{job.state}*{why}",
                 f"Repo: {escape(_repo_name(job.repo))} · requested by {escape(job.requester)} via {job.source}"]
        if job.result.pr_url and _PR_URL.match(job.result.pr_url):
            lines.append(f"PR: <{job.result.pr_url}>")
        if job.events:
            lines.append(f"Latest: {escape(job.events[-1].message[:500])}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ dedupe
    # --- trust: commit after handle, as #84 did for GitHub (redeliver.claim/done). An event is marked pending by
    # this process while it is handled and done only after; a pending mark another process left means it died
    # handling it, so Slack's retry runs it. Kept in the store, so a retry after a restart is still a repeat.
    def _claim(self, key: str, claimed: list[str]) -> bool:
        """False when `key` was handled already, or is being handled now."""
        if not _KEY.match(key):
            return True
        replays = self.jobs.admission.replays
        digest = replays.digest(key.encode())
        if not redeliver.claim(replays, digest, SEEN_TTL, scope="slack"):
            return False
        claimed.append(digest)
        return True

    def _done(self, claimed: list[str]) -> None:
        for digest in claimed:
            redeliver.done(self.jobs.admission.replays, digest, SEEN_TTL, scope="slack")

    def _forget(self, claimed: list[str]) -> None:
        for digest in claimed:
            self.jobs.admission.replays.forget("slack", digest)  # nothing happened: a retry runs it

    async def _handled(self, coro, claimed: list[str]) -> None:
        try:
            await coro
        except BaseException:  # a shutdown or failure mid-handler too
            self._forget(claimed)
            raise
        self._done(claimed)

    # ------------------------------------------------------------------ slash command
    def command(self, form: dict[str, str]) -> dict[str, Any]:
        """Answer the slash command at once (ephemeral); post and submit in the background."""
        team, channel, user = form.get("team_id", ""), form.get("channel_id", ""), form.get("user_id", "")
        if not (_ID.match(team) and _ID.match(channel) and _ID.match(user)):
            return _ephemeral("That request is missing its team, channel or user.")
        text = plain(form.get("text", ""))
        word, _, rest = text.strip().partition(" ")
        if word.lower() in ("", "help"):
            return _ephemeral(self._help(channel))
        if refusal := self._refusal(team, channel, user):
            return _ephemeral(refusal)
        if word.lower() == "status":
            return _ephemeral(self._status(rest))
        try:
            plan = self._plan(channel, text)
        except _Refused as e:
            return _ephemeral(str(e))
        self.jobs._spawn(self._guard(self._start_from_command(team, channel, user, plan, form.get("response_url"))))
        return _ephemeral(f"Starting a hangar job on {escape(_repo_name(plan.repo))}. I'll post in this channel and "
                          "reply in that thread.")

    async def _start_from_command(self, team: str, channel: str, user: str, plan: _Plan,
                                  response_url: str | None) -> None:
        api = client_for(self.jobs)
        if api is None:
            await self._respond(response_url, "hangar can't post to Slack: HANGAR_SLACK_BOT_TOKEN_CREDENTIAL is not "
                                              "set or not stored. Ask an admin to run `hangar doctor`.")
            return
        try:
            try:
                ts = await api.post(channel, f"<@{user}> asked hangar to work on "
                                             f"{escape(_repo_name(plan.repo))}:\n{_quote(escape(plan.instructions))}")
            except SlackError as e:
                await self._respond(response_url, f"hangar couldn't post in this channel ({escape(str(e))}). Invite "
                                                  "the app to the channel and try again.")
                return
            if not _TS.match(ts):
                await self._respond(response_url, "hangar posted in this channel but Slack didn't return the "
                                                  "message's timestamp, so there's no thread to report in.")
                return
            await self._submit(api, team, channel, ts, user, plan, context=[], key=None)
        finally:
            await api.close()

    async def _respond(self, response_url: str | None, text: str) -> None:
        """Ephemeral reply through the command's response_url (only Slack's own hooks host, never anywhere else)."""
        if not response_url or not response_url.startswith("https://hooks.slack.com/"):
            log.warning("Slack command failed and there is no response_url to say so: %s", text)
            return
        try:
            async with httpx.AsyncClient(transport=self.jobs.transport, timeout=10, follow_redirects=False) as c:
                await c.post(response_url, json={"response_type": "ephemeral", "replace_original": False,
                                                 "text": text})
        except httpx.HTTPError as e:
            log.warning("could not answer a Slack command via response_url: %s", type(e).__name__)

    # ------------------------------------------------------------------ Events API
    def event(self, payload: dict[str, Any], retry_num: str | None = None) -> dict[str, Any]:
        """Classify an event and acknowledge it; anything that talks to Slack or starts work runs in the background,
        and marks the event handled when it is done (--- trust)."""
        claimed: list[str] = []
        try:
            res = self._event(payload, retry_num, claimed)
        except BaseException:
            self._forget(claimed)
            raise
        if not res.get("ok"):
            self._done(claimed)  # ignored or refused: that is its handling
        return res

    def _event(self, payload: dict[str, Any], retry_num: str | None, claimed: list[str]) -> dict[str, Any]:
        if payload.get("type") != "event_callback":
            return {"ignored": f"callback type {str(payload.get('type'))[:40]}"}
        ev = payload.get("event")
        if not isinstance(ev, dict):
            return {"ignored": "no event"}
        event_id = str(payload.get("event_id") or "")
        if event_id and not self._claim(f"event:{event_id}", claimed):
            return {"ignored": "duplicate delivery" + (f" (retry {retry_num[:4]})" if retry_num else "")}
        kind = ev.get("type")
        if kind not in ("app_mention", "message"):
            return {"ignored": f"event {str(kind)[:40]}"}
        bots = {a.get("user_id") for a in payload.get("authorizations") or []
                if isinstance(a, dict) and a.get("is_bot") and a.get("user_id")}
        if ev.get("bot_id") or ev.get("subtype") not in (None, "thread_broadcast") or ev.get("user") in bots:
            return {"ignored": "bot, edited or system message"}
        team = str(payload.get("team_id") or ev.get("team") or "")
        channel, user, ts = str(ev.get("channel") or ""), str(ev.get("user") or ""), str(ev.get("ts") or "")
        thread_ts = ev.get("thread_ts")
        user_team = str(ev.get("user_team") or "") or None  # front-door-auth: the poster's own workspace
        if not (_ID.match(team) and _ID.match(channel) and _ID.match(user) and _TS.match(ts)) or \
                (thread_ts is not None and not _TS.match(str(thread_ts))):
            return {"ignored": "malformed event"}
        raw = str(ev.get("text") or "")
        job_id = self.store.kv_get(_thread_key(channel, thread_ts)) if thread_ts and thread_ts != ts else None
        if kind == "message":
            if not job_id:
                return {"ignored": "not a reply in a hangar thread"}
            if any(f"<@{b}" in raw for b in bots):
                return {"ignored": "mentions the app; handled as app_mention"}
            if self._refusal(team, channel, user, user_team):
                return {"ignored": "channel or user not allowed"}
        # A mention in a thread also arrives as a message event: act on whichever comes first.
        if not self._claim(f"msg:{channel}:{ts}", claimed):
            return {"ignored": "message already handled"}
        text = plain(_LEAD_MENTION.sub("", raw, count=1))
        self.jobs._spawn(self._guard(self._handled(self._on_message(
            team, channel, user, ts, thread_ts, job_id, text, mention=kind == "app_mention", bots=bots,
            user_team=user_team), claimed)))
        return {"ok": True}

    async def _on_message(self, team: str, channel: str, user: str, ts: str, thread_ts: str | None,
                          job_id: str | None, text: str, mention: bool, bots: set[str],
                          user_team: str | None = None) -> None:
        api = client_for(self.jobs)
        if api is None:
            log.warning("Slack event ignored: HANGAR_SLACK_BOT_TOKEN_CREDENTIAL is not set or not stored")
            return
        reply_in = thread_ts or ts
        try:
            word, _, rest = text.strip().partition(" ")
            if mention and word.lower() in ("", "help"):
                await self._say(api, channel, reply_in, self._help(channel))
            elif mention and (refusal := self._refusal(team, channel, user, user_team)):
                await self._say(api, channel, reply_in, refusal)
            elif mention and word.lower() == "status":
                await self._say(api, channel, reply_in, self._status(rest))
            elif mention and word.lower() == "adr":  # --- #170: the thread, drafted into an ADR PR
                await self._adr(api, team, channel, user, ts, thread_ts, rest, bots, user_team)
            elif job_id:
                await self._follow_up(api, team, channel, str(thread_ts), user, job_id, text, user_team)
            else:
                try:
                    plan = self._plan(channel, text)
                except _Refused as e:
                    await self._say(api, channel, reply_in, str(e))
                    return
                context = await self._thread_context(api, channel, thread_ts, ts, bots) if thread_ts else []
                await self._submit(api, team, channel, reply_in, user, plan, context, user_team=user_team,
                                   key=f"slack:{channel}:{ts}")
        finally:
            await api.close()

    async def _thread_context(self, api: SlackAPI, channel: str, thread_ts: str, before: str,
                              bots: set[str]) -> list[ContextItem]:
        """The thread the mention was made in (parent first, then earlier replies, capped) as a context file.
        Other apps' messages stay (an alert is often the parent); hangar's own don't."""
        try:
            msgs = await api.thread(channel, thread_ts, before=before, limit=THREAD_CONTEXT_MESSAGES)
        except SlackError as e:
            log.warning("could not read Slack thread %s/%s for context: %s", channel, thread_ts, e)
            return []
        parts, total = [], 0
        for m in msgs[:THREAD_CONTEXT_MESSAGES]:
            mts = str(m.get("ts") or "")
            if not _TS.match(mts) or float(mts) >= float(before) or m.get("user") in bots:
                continue
            who = m.get("user") or (m.get("bot_profile") or {}).get("name") or m.get("username") or "bot"
            body = plain(str(m.get("text") or ""))[:4000]
            part = f"## {who} ({m.get('ts', '')})\n\n{body}"
            if total + len(part) > THREAD_CONTEXT_CHARS:
                break
            parts.append(part)
            total += len(part)
        if not parts:
            return []
        # front-door-auth: other people (and other apps' alerts) wrote this, not the requester: fence it.
        return [ContextItem(name="slack-thread.md", kind="text", untrusted=True,
                            content="# Slack thread the request was made in\n\n" + "\n\n".join(parts))]

    async def _adr(self, api: SlackAPI, team: str, channel: str, user: str, ts: str, thread_ts: str | None,
                   title: str, bots: set[str], user_team: str | None) -> None:
        """--- #170: `@hangar adr [title]` in a thread: a job that drafts docs/decisions/NNNN-<slug>.md from the
        thread (untrusted, redacted), egress none, delivered as an agent-drafted PR (adr.py)."""
        from hangar import adr

        reply_in = thread_ts or ts
        if not thread_ts or thread_ts == ts:
            await self._say(api, channel, reply_in, "`@hangar adr [title]` drafts an ADR from a thread: mention me "
                                                    "in a reply in the thread where the decision was made.")
            return
        try:
            plan = self._plan(channel, f"adr {title}")
        except _Refused as e:
            await self._say(api, channel, reply_in, str(e))
            return
        if await adr.repo_ready(self.jobs, plan.repo) is False:
            await self._say(api, channel, reply_in,
                            f"{escape(_repo_name(plan.repo))} has no `{adr.DECISIONS}/` directory, so hangar won't "
                            "draft ADRs into it. A team opts in by adding the directory and an ADR template "
                            f"(hangar's `{adr.TEMPLATE_DOC}` is a starting point).")
            return
        context = adr.redacted(self.jobs, await self._thread_context(api, channel, thread_ts, ts, bots))
        if not context:
            await self._say(api, channel, reply_in, "There's nothing in this thread for an ADR yet.")
            return
        digest = adr.context_digest(context)
        try:
            permalink = await api.permalink(channel, thread_ts)
        except SlackError:
            permalink = None
        req = SubmitJob(instructions=adr.instructions(title, permalink, digest), title=adr.title(title),
                        repo=plan.repo, environment=plan.env, deliver="pr", verify=adr.VERIFY, context=context,
                        idempotency_key=f"slack:{channel}:{ts}",
                        labels=adr.labels(channel, thread_ts, user, permalink, digest))
        await self._submit(api, team, channel, reply_in, user, plan, context, key=None, user_team=user_team, req=req)

    # ------------------------------------------------------------------ actions
    async def _say(self, api: SlackAPI, channel: str, thread_ts: str, text: str) -> None:
        try:
            await api.post(channel, self.jobs.broker.redact(text), thread_ts=thread_ts)
        except SlackError as e:
            log.warning("could not post to Slack thread %s: %s", thread_ref(channel, thread_ts), e)

    async def _submit(self, api: SlackAPI, team: str, channel: str, thread_ts: str, user: str, plan: _Plan,
                      context: list[ContextItem], key: str | None, user_team: str | None = None,
                      req: SubmitJob | None = None) -> None:
        ref = thread_ref(channel, thread_ts)
        requester = f"slack:{team}/{user}"
        try:
            req = req or SubmitJob(instructions=plan.instructions, repo=plan.repo, environment=plan.env,
                                   harness=plan.harness, model=plan.model, context=context, idempotency_key=key,
                                   labels={"slack.channel": channel, "slack.thread": thread_ts, "slack.user": user})
            # Restricted like a `jobs` token: mapped repos and environments only, no provider choice.
            person = self._person(team, user, user_team)  # --- teamcreds: theirs if linked
            job = self.jobs.submit(req, Caller(requester, requester, restricted=True, person=person),
                                   source="slack", source_ref=ref)
        except ValueError as e:  # JobError and pydantic's ValidationError
            await self._say(api, channel, thread_ts, f"hangar couldn't start a job: {escape(str(e)[:500])}")
            return
        self.store.kv_set(_thread_key(channel, thread_ts), job.id)
        if job.approval is not None:  # --- frontdoor: held by a daily spend cap (Slack is never otherwise approved)
            from hangar.jobbudget import held_message

            await self._say(api, channel, thread_ts, escape(held_message(job)))
            return
        await self._say(api, channel, thread_ts,
                        f"Queued as hangar job `{job.id}` in `{escape(job.environment)}`. I'll reply in this thread "
                        "when the agent starts and when it's done."
                        + escape(fairness.queued_note(self.jobs, job)))  # --- review capacity (#159)

    async def _follow_up(self, api: SlackAPI, team: str, channel: str, thread_ts: str, user: str, job_id: str,
                         text: str, user_team: str | None = None) -> None:
        cls, text = steer_class.split_prefix(text)  # --- #162: `fix:` / `redirect:` / `info:` classify on arrival
        try:
            job = self.jobs.follow_up(job_id, text.strip() or "Continue with the task.",
                                      requester=f"slack:{team}/{user}", reply_to=thread_ref(channel, thread_ts),
                                      front_door=True, sender_person=self._person(team, user, user_team),  # review
                                      steer_class=cls)
        except JobNotFound:
            await self._say(api, channel, thread_ts, f"Job `{job_id}` no longer exists; start a new one.")
            return
        except JobError as e:
            await self._say(api, channel, thread_ts, f"Can't continue yet: {escape(str(e))}")
            return
        ack = f"Continuing hangar job `{job.id}` with your reply. I'll post here when it's done."
        turn = job.turns[-1]
        if turn.steer_class != "unclassified":
            await self._say(api, channel, thread_ts, f"{ack}\nClassified as *{steer_class.LABELS[turn.steer_class]}*.")
            return
        # --- #162: one click says what the reply was (only its sender's click counts)
        value = f"{job.id}|{len(job.turns)}"
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": ack}},
                  {"type": "context", "elements": [{"type": "mrkdwn", "text": "Was your reply a correction, a "
                                                    "redirect or new info? (Or start a reply with `fix:`, "
                                                    "`redirect:` or `info:`.)"}]},
                  {"type": "actions", "block_id": "hangar_steer_class", "elements": [
                      {"type": "button", "action_id": f"hangar_class_{c}", "value": f"{value}|{c}",
                       "text": {"type": "plain_text", "text": label}}
                      for c, label in steer_class.LABELS.items()]}]
        try:
            await api.post(channel, self.jobs.broker.redact(ack), thread_ts=thread_ts, blocks=blocks)
        except SlackError as e:
            log.warning("could not post to Slack thread %s: %s", thread_ref(channel, thread_ts), e)

    # ------------------------------------------------------------------ interactions (#162)
    def interaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A click on a steer-class button: classify the follow-up it was posted for, if the clicker sent it.
        Answered at once; the clicker hears the outcome privately through the action's response_url."""
        if payload.get("type") != "block_actions":
            return {"ignored": f"interaction {str(payload.get('type'))[:40]}"}
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        team = str((payload.get("team") or {}).get("id") or user.get("team_id") or "")
        uid = str(user.get("id") or "")
        actions = [a for a in payload.get("actions") or [] if isinstance(a, dict)
                   and str(a.get("action_id") or "").startswith("hangar_class_")]
        response_url = str(payload.get("response_url") or "")
        if not (_ID.match(team) and _ID.match(uid)) or not actions:
            return {"ignored": "not a steer-class click"}
        job_id, _, rest = str(actions[0].get("value") or "").partition("|")
        n, _, cls = rest.partition("|")
        if not n.isdigit() or cls not in steer_class.LABELS:
            return {"ignored": "malformed button value"}
        by = f"slack:{team}/{uid}"
        senders = {by, *([f"slack:{user['team_id']}/{uid}"] if _ID.match(str(user.get("team_id") or "")) else [])}
        try:
            job = self.jobs.get(job_id)
            t = steer_class.follow_up_turn(job, int(n))
            if t.requester not in senders:
                raise steer_class.ClassifyError("only the person who sent that reply can classify it")
            steer_class.classify_turn(self.jobs, job, int(n), cls, by)
        except (JobNotFound, KeyError, steer_class.ClassifyError) as e:
            self.jobs._spawn(self._respond(response_url, f"Not classified: {escape(str(e).strip(chr(39)))}"))
            return {"ignored": str(e)[:200]}
        self.jobs._spawn(self._respond(response_url, f"Classified your reply as *{steer_class.LABELS[cls]}*. "
                                                     "Thanks: only corrections count against the agent."))
        return {"ok": True, "class": cls}

    @staticmethod
    async def _guard(coro) -> None:
        """Background work must not die silently."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Slack background task failed")


def _ephemeral(text: str) -> dict[str, Any]:
    return {"response_type": "ephemeral", "text": _clip(text)}


# ============================================================================ hangar doctor
async def doctor_checks(h, transport: httpx.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
    """Slack configured, its credentials stored, the bot token working, and channels mapped to allowed repos."""
    from hangar.doctor import _check

    s = h.settings
    if not (s.slack_signing_secret_credential or s.slack_bot_token_credential or s.slack_channels):
        return []
    out: list[dict[str, Any]] = []
    creds = {c.name for c in h.store.list(Credential)}
    for label, name, var, default in (
            ("slack signing secret", s.slack_signing_secret_credential, "HANGAR_SLACK_SIGNING_SECRET_CREDENTIAL",
             "slack-signing"),
            ("slack bot token", s.slack_bot_token_credential, "HANGAR_SLACK_BOT_TOKEN_CREDENTIAL", "slack-bot")):
        if not name:
            out.append(_check(label, False, f"{var} is not set", f"hangar cred add {default} --harness webhook "
                              f"--kind generic, then set {var}={default} in the hangar-site config map"))
        elif name not in creds:
            out.append(_check(label, False, f"credential '{name}' is missing",
                              f"hangar cred add {name} --harness webhook --kind generic   (paste it, then Ctrl-D)"))
        else:
            out.append(_check(label, True, "stored"))
    if s.slack_bot_token_credential in creds:
        try:
            token = h.broker.secret_text(s.slack_bot_token_credential)
        except Exception as e:  # noqa: BLE001
            token = None
            out.append(_check("slack api", False, f"bot token unusable: {e}", "store the token again"))
        if token:
            api = SlackAPI(token, s.slack_api_url, transport=transport)
            try:
                me = await api.auth_test()
                out.append(_check("slack api", True, f"bot token works: {me.get('user', '?')} in "
                                                     f"{me.get('team', me.get('team_id', '?'))}"))
                if api.scopes is not None and "im:write" not in api.scopes:  # --- inbox delivery (#157)
                    out.append(_check("slack dm scope", None, "the bot token lacks im:write, so inbox questions, "
                                      "approvals and digests can't be sent as DMs",
                                      "add im:write to the app's bot scopes (docs/slack-app-manifest.yaml) and "
                                      "reinstall it"))
            except SlackError as e:
                out.append(_check("slack api", False, f"bot token rejected: {e}",
                                  "reinstall the app and store its Bot User OAuth Token (xoxb-…)"))
            finally:
                await api.close()
    if not s.slack_channels:
        out.append(_check("slack channels", False, "HANGAR_SLACK_CHANNELS is empty, so no channel can start jobs",
                          "set HANGAR_SLACK_CHANNELS='{\"C0123ABCD\": \"owner/repo\"}' in the hangar-site config map"))
    for channel, repo in s.slack_channels.items():
        ok = bool(h.jobs.repo_envs(_repo_url(repo)))
        out.append(_check(f"slack channel {channel}", ok,
                          f"-> {repo}" if ok else f"mapped to {repo}, which isn't in HANGAR_GITHUB_REPOS",
                          f"add {repo} to HANGAR_GITHUB_REPOS with its environment, or fix the mapping"))
    return out


# ============================================================================ plugin host (docs/plugins.md)
class _SlackDoor:
    name = "slack"
    reply = staticmethod(notify)

    def __init__(self, jobs: JobService):
        self.door = SlackFrontDoor(jobs)

    def mount(self, app: FastAPI) -> None:
        self.door.mount(app)

    def doctor(self) -> list[dict[str, Any]]:
        return []  # hangar doctor runs doctor_checks itself


class _SlackPlugin:
    """Built in: /api/hooks/slack/*, and replies in `slack:` threads. Answers "not configured" until
    HANGAR_SLACK_SIGNING_SECRET_CREDENTIAL is set, as before."""

    name = "slack"
    api = "hangar.plugin/v1"
    Settings = NoSettings

    def register(self, host: Any, settings: Any) -> None:
        host.add_door(_SlackDoor(host.jobs))


PLUGIN = _SlackPlugin()
