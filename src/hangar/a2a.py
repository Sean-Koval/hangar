"""A2A server: other agents (Google ADK, agents on AX, any A2A client) hand hangar work (spec 0011 §3 G).

Hangar speaks the JSON-RPC binding of A2A 1.0 (https://a2a-protocol.org/v1.0.0/specification/) and, on the same
endpoint, the 0.3 wire format many clients still send (`message/send`, `kind` discriminators, lowercase states).

One A2A task is one hangar job, and the job is also its context: a later message with the same contextId is a
follow-up turn on the same branch, in the same agent conversation. Job events stream out as status updates; the
summary, PR, diffstat and structured result come back as artifacts.

Hand-written rather than built on `a2a-sdk`: the surface is a dozen methods, and the SDK brings protobuf, the
google-api client stack and a task store of its own that would shadow the job store. The tests use the SDK's
client to prove the two interoperate. See docs/a2a.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any, NamedTuple
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ValidationError

from hangar.auth import TokenAuth, bearer_from_headers
from hangar.config import Settings
from hangar.jobs import Caller, JobError, JobNotFound, JobService, SubmitJob, _internal
from hangar.models import ContextItem, Job, new_id
from hangar.vault import Vault

log = logging.getLogger("hangar.a2a")

V1, V03 = "1.0", "0.3"
CONTEXT_LABEL = "a2a.context"  # a contextId the client chose, kept on the job it started
MESSAGE_LABEL = "a2a.message"  # the messageId of the job's latest follow-up, so a retried send isn't run twice
SKILLS = ("code_change", "answer_question_about_repo")
MAX_PARTS = 100
KEEPALIVE_SECONDS = 15.0
_ID_RE = re.compile(r"^[\x21-\x7e]{1,200}$")  # printable ASCII, no spaces
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]{0,39}$")  # an HTTP auth-scheme token
_HEADER_RE = re.compile(r"^[\x20-\x7e]{1,4096}$")  # safe in a header value: no CR/LF
_PUSH_KEY = "a2a.push."

SUBMITTED, WORKING, COMPLETED = "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED"
FAILED, CANCELED, REJECTED = "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"
INPUT_REQUIRED, AUTH_REQUIRED = "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"
TERMINAL = {COMPLETED, FAILED, CANCELED, REJECTED}
PAUSED = {INPUT_REQUIRED, AUTH_REQUIRED}  # the spec's "interrupted" states: the client has to act

# Keyed by the job state's string value, so states other workstreams add (needs_input, awaiting_approval) map
# before they exist. awaiting_approval is `submitted`, not `auth-required`: a maintainer approves it, and an
# A2A client that sees auth-required would ask its user for credentials that can't help.
_STATES = {
    "queued": SUBMITTED, "awaiting_approval": SUBMITTED,
    "provisioning": WORKING, "running": WORKING, "verifying": WORKING, "delivering": WORKING,
    "succeeded": COMPLETED, "failed": FAILED, "cancelled": CANCELED, "needs_input": INPUT_REQUIRED,
}
_V03_STATES = {s: s.removeprefix("TASK_STATE_").lower().replace("_", "-")
               for s in (SUBMITTED, WORKING, COMPLETED, FAILED, CANCELED, REJECTED, INPUT_REQUIRED, AUTH_REQUIRED)}

_V1_METHODS = {
    "SendMessage": "send", "SendStreamingMessage": "stream", "GetTask": "get", "ListTasks": "list",
    "CancelTask": "cancel", "SubscribeToTask": "subscribe",
    "CreateTaskPushNotificationConfig": "push_set", "GetTaskPushNotificationConfig": "push_get",
    "ListTaskPushNotificationConfigs": "push_list", "DeleteTaskPushNotificationConfig": "push_delete",
    "GetExtendedAgentCard": "card",
}
_V03_METHODS = {
    "message/send": "send", "message/stream": "stream", "tasks/get": "get", "tasks/cancel": "cancel",
    "tasks/resubscribe": "subscribe",
    "tasks/pushNotificationConfig/set": "push_set", "tasks/pushNotificationConfig/get": "push_get",
    "tasks/pushNotificationConfig/list": "push_list", "tasks/pushNotificationConfig/delete": "push_delete",
    "agent/getAuthenticatedExtendedCard": "card",
}
# What a client may set in request or message metadata (top level or under "hangar"; snake_case or camelCase).
_OPTIONS = {"repo", "base_branch", "environment", "harness", "model", "verify", "deliver", "title",
            "timeout_minutes", "keep_minutes", "skill", "output_schema"}


class RpcError(Exception):
    """A JSON-RPC error. `reason` names the A2A error for the 1.0 ErrorInfo detail."""

    def __init__(self, code: int, message: str, reason: str | None = None):
        super().__init__(message)
        self.code, self.message, self.reason = code, message, reason


def _invalid(message: str) -> RpcError:
    return RpcError(-32602, message)


def _unsupported(message: str) -> RpcError:
    return RpcError(-32004, message, "UNSUPPORTED_OPERATION")


def _not_found(what: str) -> RpcError:
    return RpcError(-32001, f"{what} not found", "TASK_NOT_FOUND")


def task_state(job_state: Any) -> str:
    """The A2A task state for a job state (an enum member or its string value)."""
    return _STATES.get(str(getattr(job_state, "value", job_state)), WORKING)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _version() -> str:
    try:
        return package_version("hangar")
    except PackageNotFoundError:
        return "0.1.0"


def _text(s: str) -> dict:
    return {"text": s}


def _context_of(job: Job) -> str:
    return job.labels.get(CONTEXT_LABEL) or job.id


def _owner(caller: Caller) -> str | None:
    return caller.owner if caller.restricted else None


def _question(job: Job) -> str | None:
    """The agent's question when a job needs input (workstream E keeps it on the job or its last turn)."""
    for obj in (job, job.turns[-1] if job.turns else None, job.result):
        q = getattr(obj, "question", None)
        if isinstance(q, str) and q.strip():
            return q
    return None


# ---------------------------------------------------------------------------- inbound parsing
class _Part(NamedTuple):
    kind: str  # text | raw | url | data
    value: Any
    name: str | None
    media_type: str | None
    metadata: dict


class _Message(NamedTuple):
    id: str
    task_id: str | None
    context_id: str | None
    parts: list[_Part]
    metadata: dict


class _Config(NamedTuple):
    blocking: bool
    history_length: int | None
    push: dict | None


def _str_id(value: Any, what: str, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise _invalid(f"{what} is required")
        return None
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise _invalid(f"{what} must be a string of up to 200 printable characters")
    return value


def _parse_parts(raw: Any) -> list[_Part]:
    """Parts in either wire format: 1.0 names the type by its member (`text`, `raw`, `url`, `data`), 0.3 adds a
    `kind` and nests files under `file` (`bytes` or `uri`)."""
    if not isinstance(raw, list) or not raw:
        raise _invalid("message.parts must be a non-empty list")
    if len(raw) > MAX_PARTS:
        raise _invalid(f"a message can have at most {MAX_PARTS} parts")
    out = []
    for i, p in enumerate(raw):
        if not isinstance(p, dict):
            raise _invalid(f"part {i} must be an object")
        md = p.get("metadata") if isinstance(p.get("metadata"), dict) else {}
        if p.get("kind") == "file" and isinstance(p.get("file"), dict):
            f = p["file"]
            kind, value = ("raw", f.get("bytes")) if "bytes" in f else ("url", f.get("uri"))
            name, media = f.get("name"), f.get("mimeType")
        else:
            found = [k for k in ("text", "raw", "url", "data") if k in p]
            if len(found) != 1:
                raise _invalid(f"part {i} needs exactly one of text, raw, url or data")
            kind, value = found[0], p[found[0]]
            name, media = p.get("filename"), p.get("mediaType")
        if kind != "data" and not isinstance(value, str):
            raise _invalid(f"part {i}: {kind} must be a string")
        if name is not None and (not isinstance(name, str) or len(name) > 200):
            raise _invalid(f"part {i}: the file name must be a string of up to 200 characters")
        out.append(_Part(kind, value, name, media if isinstance(media, str) else None, md))
    return out


def _parse_message(params: dict) -> _Message:
    m = params.get("message")
    if not isinstance(m, dict):
        raise _invalid("params.message is required")
    if m.get("role") not in ("ROLE_USER", "user"):
        raise _invalid("message.role must be ROLE_USER")
    md = m.get("metadata")
    return _Message(
        id=_str_id(m.get("messageId"), "message.messageId", required=True),
        task_id=_str_id(m.get("taskId"), "message.taskId"),
        context_id=_str_id(m.get("contextId"), "message.contextId"),
        parts=_parse_parts(m.get("parts")),
        metadata=md if isinstance(md, dict) else {},
    )


def _history_length(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0 or value != int(value):
        raise _invalid("historyLength must be a non-negative integer")
    return int(value)


def _parse_config(params: dict, ver: str) -> _Config:
    c = params.get("configuration") or {}
    if not isinstance(c, dict):
        raise _invalid("configuration must be an object")
    # 1.0 blocks unless returnImmediately; 0.3 SDK servers block unless blocking is false.
    blocking = not c.get("returnImmediately", False) if ver == V1 else c.get("blocking") is not False
    push = c.get("taskPushNotificationConfig") or c.get("pushNotificationConfig")
    return _Config(bool(blocking), _history_length(c.get("historyLength")), push)


def _options(*sources: Any) -> dict:
    """hangar options from request and message metadata; later sources win."""
    out: dict = {}
    for md in sources:
        if not isinstance(md, dict):
            continue
        for d in (md, md.get("hangar")):
            if isinstance(d, dict):
                for k, v in d.items():
                    key = re.sub(r"(?<!^)([A-Z])", r"_\1", str(k)).lower()
                    if key in _OPTIONS:
                        out[key] = v
    return out


def _context_items(parts: list[_Part]) -> list[ContextItem]:
    """File and data parts become job context files; links stay links (listed in the prompt, not fetched)."""
    items = []
    for i, p in enumerate(parts, 1):
        if p.kind == "raw":
            b64 = p.value.strip().replace("-", "+").replace("_", "/")  # ProtoJSON bytes may be URL-safe
            items.append(ContextItem(name=p.name or f"attachment-{i}", kind="file",
                                     content=b64 + "=" * (-len(b64) % 4), encoding="base64"))
        elif p.kind == "url":
            items.append(ContextItem(name=p.name or f"link-{i}", kind="url", url=p.value))
        elif p.kind == "data":
            items.append(ContextItem(name=p.name or f"data-{i}.json", kind="text",
                                     content=json.dumps(p.value, indent=2)))
    return items


# ---------------------------------------------------------------------------- 0.3 output
def _v03_part(p: dict) -> dict:
    md = {"metadata": p["metadata"]} if p.get("metadata") else {}
    if "text" in p:
        return {"kind": "text", "text": p["text"], **md}
    if "data" in p:
        d = p["data"]
        return {"kind": "data", "data": d if isinstance(d, dict) else {"value": d}, **md}
    f = {k: v for k, v in (("name", p.get("filename")), ("mimeType", p.get("mediaType"))) if v}
    f["bytes" if "raw" in p else "uri"] = p.get("raw", p.get("url"))
    return {"kind": "file", "file": f, **md}


def _v03_message(m: dict) -> dict:
    return {**{k: v for k, v in m.items() if k not in ("role", "parts")}, "kind": "message",
            "role": "user" if m["role"] == "ROLE_USER" else "agent", "parts": [_v03_part(p) for p in m["parts"]]}


def _v03_status(s: dict) -> dict:
    out = {"state": _V03_STATES[s["state"]], "timestamp": s.get("timestamp")}
    if "message" in s:
        out["message"] = _v03_message(s["message"])
    return out


def _v03_artifact(a: dict) -> dict:
    return {**a, "parts": [_v03_part(p) for p in a["parts"]]}


def _v03_task(t: dict) -> dict:
    out = {**t, "kind": "task", "status": _v03_status(t["status"])}
    if "artifacts" in t:
        out["artifacts"] = [_v03_artifact(a) for a in t["artifacts"]]
    if "history" in t:
        out["history"] = [_v03_message(m) for m in t["history"]]
    return out


def _rpc_error(rid: Any, e: RpcError, ver: str) -> dict:
    err: dict[str, Any] = {"code": e.code, "message": e.message}
    if e.reason and ver == V1:
        err["data"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": e.reason,
                        "domain": "a2a-protocol.org"}]
    return {"jsonrpc": "2.0", "id": rid, "error": err}


# ---------------------------------------------------------------------------- push notification configs
class PushConfig(BaseModel):
    """A client's webhook for one task. Its token and credentials live in the vault, never in this record."""

    id: str
    task_id: str = ""
    url: str
    scheme: str | None = None
    version: str = V1
    sent: str | None = None  # the last state notified, so a restart doesn't notify it again


# ---------------------------------------------------------------------------- the server
class A2AServer:
    def __init__(self, jobs: JobService, auth: TokenAuth, vault: Vault, settings: Settings):
        self.jobs = jobs
        self.auth = auth
        self.vault = vault
        self.settings = settings
        self.store = jobs.store
        self._push: dict[str, list[PushConfig]] = {}  # job id -> its push configs, mirrored in the kv store
        self._loop_task: asyncio.Task | None = None
        self._open: dict[str, int] = {}  # front-door-auth: open streams and blocking sends per caller

    async def start(self) -> None:
        self._push = {k.removeprefix(_PUSH_KEY): [PushConfig.model_validate(c) for c in self.store.kv_get_json(k, [])]
                      for k in self.store.kv_keys(_PUSH_KEY)}
        self._loop_task = asyncio.create_task(self._push_loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except BaseException:  # noqa: BLE001 - cancellation, or whatever the loop died of
                pass
            self._loop_task = None

    def mount(self, app: FastAPI) -> None:
        app.add_api_route("/.well-known/agent-card.json", self.card_route, methods=["GET"])
        app.add_api_route("/a2a", self.handle, methods=["POST"])

    # ------------------------------------------------------------------ agent card
    def card(self) -> dict[str, Any]:
        url = self.settings.public_url.rstrip("/") + "/a2a"
        auth_help = "A hangar token: admin, or jobs scope (repos in HANGAR_GITHUB_REPOS only)."
        modes_in = ["text/plain", "application/json", "application/octet-stream"]
        modes_out = ["text/plain", "application/json"]
        return {
            "name": "hangar",
            "description": "Runs coding agents (Claude Code, Codex, pi, Antigravity) in sandboxes on its owner's "
                           "infrastructure. Send instructions and a repo; get back a verified branch and a pull "
                           "request. Reply in the same context to continue the work.",
            "supportedInterfaces": [{"url": url, "protocolBinding": "JSONRPC", "protocolVersion": V1},
                                    {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": V03}],
            "version": _version(),
            "capabilities": {"streaming": True, "pushNotifications": True, "extendedAgentCard": False},
            # One object in both shapes: 1.0 parsers read httpAuthSecurityScheme, 0.3 parsers read type/scheme.
            "securitySchemes": {"bearer": {
                "httpAuthSecurityScheme": {"scheme": "Bearer", "description": auth_help},
                "type": "http", "scheme": "bearer", "description": auth_help}},
            "securityRequirements": [{"schemes": {"bearer": {"list": []}}}],
            "defaultInputModes": modes_in,
            "defaultOutputModes": modes_out,
            "skills": [
                {"id": "code_change", "name": "Change code in a repository",
                 "description": "Makes a change (a fix, a feature, tests, a refactor) in a git repository inside a "
                                "sandbox, runs the repo's own check, and delivers a branch and a draft pull request. "
                                "Text parts are the instructions; file and data parts become context files. Name "
                                "the repo (and optionally environment, harness, model, verify) in the message "
                                "metadata.",
                 "tags": ["coding", "git", "pull-request"],
                 "examples": ["Fix the unicode bug in CSV export and add a regression test.",
                              "Upgrade the logging library and fix what breaks."],
                 "inputModes": modes_in, "outputModes": modes_out},
                {"id": "answer_question_about_repo", "name": "Answer a question about a repository",
                 "description": "Reads a repository in a sandbox and answers a question about it without changing "
                                "it. Set skill=answer_question_about_repo in the message metadata.",
                 "tags": ["coding", "question-answering"],
                 "examples": ["Where is the retry policy for outbound webhooks configured?"],
                 "inputModes": modes_in, "outputModes": ["text/plain"]},
            ],
            # 0.3 clients read these; 1.0 clients ignore them.
            "url": url, "protocolVersion": "0.3.0", "preferredTransport": "JSONRPC", "security": [{"bearer": []}],
        }

    async def card_route(self) -> Response:
        card = self.card()
        etag = '"' + hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest()[:32] + '"'
        return JSONResponse(card, headers={"Cache-Control": "public, max-age=300", "ETag": etag})

    # ------------------------------------------------------------------ JSON-RPC endpoint
    async def handle(self, request: Request) -> Response:
        caller = self._caller(request)
        if isinstance(caller, Response):
            return caller
        try:
            body = await self._read(request)
        except RpcError as e:
            return JSONResponse(_rpc_error(None, e, V1), status_code=413)
        try:
            req = json.loads(body)
        except (ValueError, RecursionError):  # RecursionError: absurdly nested JSON
            return JSONResponse(_rpc_error(None, RpcError(-32700, "Invalid JSON payload"), V1))
        rid = req.get("id") if isinstance(req, dict) else None
        if (not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str)
                or isinstance(rid, bool) or not isinstance(rid, str | int)):
            return JSONResponse(_rpc_error(rid, RpcError(
                -32600, "Request payload validation error: send one JSON-RPC 2.0 request with a method and an id"),
                V1))
        ver = V1
        try:
            ver, op = self._route(request, req["method"])
            params = req.get("params") or {}
            if not isinstance(params, dict):
                raise _invalid("params must be an object")
            if op in ("stream", "subscribe"):
                return await self._stream(caller, params, ver, op, rid, request)
            result = await getattr(self, f"_op_{op}")(caller, params, ver, request)
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})
        except Exception as e:  # noqa: BLE001 - every failure becomes a JSON-RPC error
            return JSONResponse(_rpc_error(rid, self._as_rpc(e, req["method"]), ver))

    @staticmethod
    def _as_rpc(e: Exception, method: str) -> RpcError:
        if isinstance(e, RpcError):
            return e
        if isinstance(e, JobNotFound):
            return _not_found("task")
        if isinstance(e, JobError):
            return _invalid(str(e))
        if isinstance(e, ValidationError):
            return _invalid("; ".join(f"{'.'.join(map(str, x['loc'])) or 'request'}: {x['msg']}"
                                      for x in e.errors()[:5]))
        log.exception("A2A %s failed", method, exc_info=e)
        return RpcError(-32603, "Internal error")

    def _caller(self, request: Request) -> Caller | Response:
        """The same rule as the jobs REST API: admin sees every job; a jobs-scoped token is fenced in."""
        p = self.auth.principal(bearer_from_headers(request.headers))
        if p is None:
            return JSONResponse(
                _rpc_error(None, RpcError(-32000, "unauthorized: send Authorization: Bearer <hangar token>"), V1),
                status_code=401, headers={"WWW-Authenticate": 'Bearer realm="hangar"'})
        if p.admin:
            return Caller(p.name, p.id)
        if "jobs" in p.scopes:  # front-door-auth: with the token's budget cap, as over REST
            return Caller(p.name, p.id, restricted=True, callback_secret=p.callback_secret, max_budget=p.max_budget)
        return JSONResponse(_rpc_error(None, RpcError(-32000, f"token scopes {p.scopes} do not allow jobs"), V1),
                            status_code=403)

    async def _read(self, request: Request) -> bytes:
        cap = self.settings.a2a_max_request_bytes
        too_big = RpcError(-32600, f"request is larger than {cap} bytes")
        try:
            if int(request.headers.get("content-length") or 0) > cap:
                raise too_big
        except ValueError:
            pass
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > cap:
                raise too_big
        return bytes(buf)

    @staticmethod
    def _route(request: Request, method: str) -> tuple[str, str]:
        """(version, operation). The A2A-Version header picks the version; without it the spec says 0.3, but a
        1.0 method name is plainly 1.0, so it's served as such."""
        raw = request.headers.get("a2a-version") or request.query_params.get("A2A-Version")
        if raw:
            ver = ".".join(raw.strip().split(".")[:2])
            if ver not in (V1, V03):
                raise RpcError(-32009, f"A2A version {raw[:20]} is not supported; use 1.0 or 0.3",
                               "VERSION_NOT_SUPPORTED")
            table = _V1_METHODS if ver == V1 else _V03_METHODS
            if method not in table:
                raise RpcError(-32601, f"Method not found: {method[:100]} (A2A {ver})")
            return ver, table[method]
        if method in _V03_METHODS:
            return V03, _V03_METHODS[method]
        if method in _V1_METHODS:
            return V1, _V1_METHODS[method]
        raise RpcError(-32601, f"Method not found: {method[:100]}")

    def _job(self, caller: Caller, task_id: Any) -> Job:
        tid = _str_id(task_id, "id", required=True)
        try:
            return self.jobs.get(tid, owner=_owner(caller))
        except JobNotFound:
            raise _not_found(f"task '{tid}'") from None

    # ------------------------------------------------------------------ tasks as A2A sees them
    def task(self, job: Job, history_length: int | None = None, artifacts: bool = True) -> dict[str, Any]:
        st = task_state(job.state)
        ctx = _context_of(job)
        t: dict[str, Any] = {"id": job.id, "contextId": ctx, "status": self._status(job, st, ctx)}
        if artifacts:
            t["artifacts"] = self._artifacts(job)
        history = self._history(job, ctx)
        if history_length is not None:
            history = history[-history_length:] if history_length else []
        if history:
            t["history"] = history
        info = {"jobId": job.id, "state": str(getattr(job.state, "value", job.state)), "reason": job.reason,
                "repo": job.repo, "branch": job.branch, "environment": job.environment, "harness": job.harness,
                "model": job.model, "turns": len(job.turns)}
        t["metadata"] = {"hangar": {k: v for k, v in info.items() if v is not None}}
        return self.jobs.broker.redact(t)

    def _agent_message(self, job: Job, ctx: str, mid: str, text: str) -> dict:
        return {"messageId": mid, "contextId": ctx, "taskId": job.id, "role": "ROLE_AGENT", "parts": [_text(text)]}

    def _status(self, job: Job, st: str, ctx: str) -> dict:
        last = job.events[-1] if job.events else None
        text = last.message if last else None
        if st == INPUT_REQUIRED:
            text = _question(job) or text
        elif st == FAILED:
            cause = next((e.message for e in reversed(job.events)
                          if not e.message.startswith(("failed", "succeeded", "cancelled"))), None)
            if cause and cause != text:
                text = f"{text}\nWhat went wrong: {cause}" if text else cause
        s: dict[str, Any] = {"state": st, "timestamp": _iso(last.ts if last else job.updated_at)}
        if text:
            s["message"] = self._agent_message(job, ctx, f"{job.id}-status-{int((last.ts if last else 0) * 1000)}",
                                               text)
        return s

    def _history(self, job: Job, ctx: str) -> list[dict]:
        """What was asked (the instructions, then each follow-up) and what the agent said back (each summary)."""
        out = []
        for n, turn in enumerate(job.turns, 1):
            asked = job.instructions if turn.kind == "initial" else turn.message
            if asked:
                out.append({"messageId": f"{job.id}-u{n}", "contextId": ctx, "taskId": job.id, "role": "ROLE_USER",
                            "parts": [_text(asked)]})
            if turn.summary:
                out.append(self._agent_message(job, ctx, f"{job.id}-a{n}", turn.summary))
        return out

    def _artifacts(self, job: Job) -> list[dict]:
        r = job.result
        out = []

        def art(name: str, title: str, parts: list[dict]) -> None:
            out.append({"artifactId": f"{job.id}-{name}", "name": title, "parts": parts})

        if r.summary:
            art("summary", "Summary", [_text(r.summary)])
        if r.pr_url or r.pushed:
            info = {"prUrl": r.pr_url, "prNumber": r.pr_number, "branch": job.branch, "baseBranch": job.base_branch,
                    "headSha": r.head_sha, "commits": r.commits or None, "verifyExitCode": r.verify_exit_code}
            line = f"Pull request: {r.pr_url}" if r.pr_url else f"Branch pushed: {job.branch}"
            art("pull_request" if r.pr_url else "branch", "Pull request" if r.pr_url else "Branch",
                [_text(line), {"data": {k: v for k, v in info.items() if v is not None},
                               "mediaType": "application/json"}])
        if r.diffstat:
            art("diffstat", "Diffstat", [_text(r.diffstat)])
        # Structured output (workstream E): on the job's result or the last turn, whichever exists.
        last = job.turns[-1] if job.turns else None
        structured = getattr(r, "structured", None)
        if structured is None:
            structured = getattr(last, "result", None)
        if structured is not None and not isinstance(structured, str | bytes):
            if isinstance(structured, BaseModel):
                structured = structured.model_dump(mode="json")
            art("result", "Structured result", [{"data": structured, "mediaType": "application/json"}])
        return out

    def _out_task(self, task: dict, ver: str, wrap: bool = False) -> dict:
        if ver == V03:
            return _v03_task(task)
        return {"task": task} if wrap else task

    def _out_event(self, kind: str, obj: dict, ver: str) -> dict:
        if ver == V1:
            return {kind: obj}
        if kind == "task":
            return _v03_task(obj)
        if kind == "statusUpdate":
            return {"kind": "status-update", "taskId": obj["taskId"], "contextId": obj["contextId"],
                    "status": _v03_status(obj["status"]), "final": obj["status"]["state"] in TERMINAL | PAUSED}
        return {**obj, "kind": "artifact-update", "artifact": _v03_artifact(obj["artifact"])}

    # ------------------------------------------------------------------ operations
    async def _op_send(self, caller: Caller, params: dict, ver: str, request: Request) -> dict:
        job, cfg = self._accept(caller, params, ver)
        if cfg.blocking and self._room(caller):  # front-door-auth: past the cap, answer at once instead of waiting
            with self._holding(caller):
                await self._wait(job, request, self.settings.a2a_max_wait_seconds)
        return self._out_task(self.task(job, cfg.history_length), ver, wrap=True)

    # --- front-door-auth: each caller holds at most HANGAR_A2A_MAX_STREAMS_PER_OWNER connections open
    def _room(self, caller: Caller) -> bool:
        cap = self.settings.a2a_max_streams_per_owner
        return cap <= 0 or self._open.get(caller.owner, 0) < cap

    @contextlib.contextmanager
    def _holding(self, caller: Caller):
        self._open[caller.owner] = self._open.get(caller.owner, 0) + 1
        try:
            yield
        finally:
            if (n := self._open.get(caller.owner, 1) - 1) > 0:
                self._open[caller.owner] = n
            else:
                self._open.pop(caller.owner, None)

    async def _op_get(self, caller: Caller, params: dict, ver: str, request: Request) -> dict:
        job = self._job(caller, params.get("id"))
        return self._out_task(self.task(job, _history_length(params.get("historyLength"))), ver)

    async def _op_list(self, caller: Caller, params: dict, ver: str, request: Request) -> dict:
        ctx = _str_id(params.get("contextId"), "contextId")
        status = params.get("status")
        if status in (None, "", "TASK_STATE_UNSPECIFIED"):
            status = None
        elif status not in _V03_STATES:
            raise _invalid(f"unknown status {str(status)[:40]}")
        try:
            size = max(1, min(int(params.get("pageSize") or 50), 100))
            offset = int(params.get("pageToken") or 0)
        except (TypeError, ValueError):
            raise _invalid("pageSize and pageToken must come from this server") from None
        hl = _history_length(params.get("historyLength"))
        found = [j for j in self.jobs.list(owner=_owner(caller), limit=500)
                 if (ctx is None or _context_of(j) == ctx) and (status is None or task_state(j.state) == status)]
        found.sort(key=lambda j: j.updated_at, reverse=True)
        page = found[max(offset, 0):max(offset, 0) + size]
        more = offset + size < len(found)
        return {"tasks": [self.task(j, hl, artifacts=bool(params.get("includeArtifacts"))) for j in page],
                "nextPageToken": str(offset + size) if more else "", "pageSize": size, "totalSize": len(found)}

    async def _op_cancel(self, caller: Caller, params: dict, ver: str, request: Request) -> dict:
        job = self._job(caller, params.get("id"))
        st = task_state(job.state)
        if st in TERMINAL:
            raise RpcError(-32002, f"task {job.id} is already {_V03_STATES[st]}", "TASK_NOT_CANCELABLE")
        try:
            self.jobs.cancel(job.id, requester=caller.requester, owner=_owner(caller))
        except JobError as e:
            raise RpcError(-32002, str(e), "TASK_NOT_CANCELABLE") from None
        await self._wait(job, request, 10)  # the agent stops within a poll or two; answer with the outcome
        return self._out_task(self.task(job), ver)

    async def _op_card(self, caller: Caller, params: dict, ver: str, request: Request) -> dict:
        raise _unsupported("hangar has no extended agent card; the public card at /.well-known/agent-card.json "
                           "is complete")

    async def _wait(self, job: Job, request: Request | None, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while task_state(job.state) not in TERMINAL | PAUSED and loop.time() < end:
            if request is not None and await request.is_disconnected():
                return
            await asyncio.sleep(self.settings.a2a_poll_seconds)

    # ------------------------------------------------------------------ messages: new jobs and follow-ups
    def _accept(self, caller: Caller, params: dict, ver: str) -> tuple[Job, _Config]:
        msg = _parse_message(params)
        cfg = _parse_config(params, ver)
        push = self._parse_push(cfg.push) if cfg.push else None  # reject a bad webhook before any job starts
        text = "\n\n".join(p.value for p in msg.parts if p.kind == "text").strip()
        context = _context_items(msg.parts)
        opts = _options(params.get("metadata"), msg.metadata)
        schema = next((p.metadata["schema"] for p in msg.parts
                       if p.kind == "text" and isinstance(p.metadata.get("schema"), dict)), None)
        if schema is not None:
            opts.setdefault("output_schema", schema)  # spec §6.8: a schema on the text part asks for JSON back
        job = self._existing(caller, msg)
        if job is not None and f"a2a:{msg.id}" in (f"a2a:{job.labels.get(MESSAGE_LABEL)}", job.idempotency_key):
            return job, cfg  # a retry of a message this job already took
        if push and job is not None:
            self._check_push_room(job.id, push[0].id)
        if job is None:
            job = self._submit(caller, msg, text, context, opts)
        else:
            self._follow_up(caller, job, msg, text, context)
        if push:
            self._save_push(job, push[0], push[1], ver)
        return job, cfg

    def _existing(self, caller: Caller, msg: _Message) -> Job | None:
        """The job a message continues: its taskId (only while the task waits for input), else its contextId."""
        if msg.task_id:
            job = self._job(caller, msg.task_id)
            if msg.context_id and msg.context_id != _context_of(job):
                raise _invalid(f"contextId {msg.context_id} is not the context of task {job.id}")
            st = task_state(job.state)
            if st in TERMINAL:
                raise _unsupported(f"task {job.id} is {_V03_STATES[st]}; to continue the work, send the message "
                                   "with its contextId and no taskId")
            return job
        if not msg.context_id:
            return None
        try:
            return self.jobs.get(msg.context_id, owner=_owner(caller))
        except JobNotFound:
            pass
        mine = [j for j in self.jobs.list(owner=caller.owner, limit=500)
                if j.labels.get(CONTEXT_LABEL) == msg.context_id]
        return mine[0] if mine else None

    def _submit(self, caller: Caller, msg: _Message, text: str, context: list[ContextItem], opts: dict) -> Job:
        skill = opts.pop("skill", None) or SKILLS[0]
        if skill not in SKILLS:
            raise _invalid(f"unknown skill '{str(skill)[:60]}' (choose {' or '.join(SKILLS)})")
        if not text:
            raise _invalid("the message needs a text part with the instructions")
        if skill == "answer_question_about_repo":
            text = ("Answer this question about the repository. Don't change any files; write the answer to "
                    f"RESULT.md.\n\n{text}")
            opts.setdefault("deliver", "none")
        if "output_schema" in opts and "output_schema" not in SubmitJob.model_fields:
            raise _invalid("output_schema needs structured results, which this hangar doesn't support yet")
        if not opts.get("repo") and self.settings.a2a_default_repo:
            opts["repo"] = self.settings.a2a_default_repo
        req = SubmitJob(instructions=text, context=context, idempotency_key=f"a2a:{msg.id}",
                        labels={CONTEXT_LABEL: msg.context_id} if msg.context_id else {}, **opts)
        return self.jobs.submit(req, caller, source="a2a")

    def _follow_up(self, caller: Caller, job: Job, msg: _Message, text: str, context: list[ContextItem]) -> None:
        st = task_state(job.state)
        if st not in TERMINAL | PAUSED:
            raise _unsupported(f"task {job.id} is still {_V03_STATES[st]}; send the message when it finishes, or "
                               "cancel it")
        if not text:
            raise _invalid("a follow-up needs a text part with the message")
        kw: dict[str, Any] = {}
        if context:
            if "context" not in inspect.signature(self.jobs.follow_up).parameters:
                raise _invalid("this hangar doesn't take attachments on follow-ups yet; put them in the text")
            kw["context"] = context
        self.jobs.follow_up(job.id, text, requester=caller.requester, owner=_owner(caller), **kw)
        job.labels[MESSAGE_LABEL] = msg.id
        self.store.put(job)

    # ------------------------------------------------------------------ streaming
    async def _stream(self, caller: Caller, params: dict, ver: str, op: str, rid: Any, request: Request) -> Response:
        if not self._room(caller):  # front-door-auth
            raise RpcError(-32000, f"too many open streams for this token (the limit is "
                                   f"{self.settings.a2a_max_streams_per_owner}); close one or poll with GetTask")
        if op == "stream":
            job, _ = self._accept(caller, params, ver)
        else:
            job = self._job(caller, params.get("id"))
            st = task_state(job.state)
            if st in TERMINAL:
                raise _unsupported(f"task {job.id} is {_V03_STATES[st]}; read it with GetTask")

        async def sse() -> AsyncIterator[str]:
            # Checked again where the stream is counted (no await in between): requests that arrive together all
            # pass the check above, before any of their streams has started.
            if not self._room(caller):
                err = RpcError(-32000, f"too many open streams for this token (the limit is "
                                       f"{self.settings.a2a_max_streams_per_owner}); close one or poll with GetTask")
                yield f"data: {json.dumps(_rpc_error(rid, err, ver))}\n\n"
                return
            try:
                with self._holding(caller):  # front-door-auth
                    async for kind, obj in self._events(job, request):
                        if kind == "keepalive":
                            yield ": keepalive\n\n"
                            continue
                        res = {"jsonrpc": "2.0", "id": rid, "result": self._out_event(kind, obj, ver)}
                        yield f"data: {json.dumps(res)}\n\n"
            except Exception as e:  # noqa: BLE001 - tell the client, don't just drop the stream
                yield f"data: {json.dumps(_rpc_error(rid, self._as_rpc(e, 'stream'), ver))}\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def _events(self, job: Job, request: Request | None) -> AsyncIterator[tuple[str, dict]]:
        """The task, then a status update for every job event until the job finishes or needs the caller.
        Artifacts go out just before that last status update."""
        task = self.task(job)
        ctx = task["contextId"]
        yield "task", task
        if task_state(job.state) in TERMINAL | PAUSED:
            return
        loop = asyncio.get_running_loop()
        last = job.events[-1] if job.events else None
        quiet_since = loop.time()
        while True:
            await asyncio.sleep(self.settings.a2a_poll_seconds)
            events = list(job.events)
            # Events are appended (and trimmed from the front), so what follows the last one sent is new.
            at = next((i for i in range(len(events) - 1, -1, -1) if events[i] is last), None)
            new = events[at + 1:] if at is not None else [e for e in events if last is None or e.ts > last.ts]
            for e in new:
                last = e
                quiet_since = loop.time()
                st = task_state(e.state)
                if st in TERMINAL | PAUSED:
                    for a in self._artifacts(job):
                        yield "artifactUpdate", self.jobs.broker.redact(
                            {"taskId": job.id, "contextId": ctx, "artifact": a, "append": False, "lastChunk": True})
                    yield "statusUpdate", self.jobs.broker.redact(
                        {"taskId": job.id, "contextId": ctx, "status": self._status(job, st, ctx)})
                    return
                status = {"state": st, "timestamp": _iso(e.ts),
                          "message": self._agent_message(job, ctx, f"{job.id}-status-{int(e.ts * 1000)}", e.message)}
                yield "statusUpdate", {"taskId": job.id, "contextId": ctx, "status": status}
            if request is not None and await request.is_disconnected():
                return
            if loop.time() - quiet_since > KEEPALIVE_SECONDS:
                quiet_since = loop.time()
                yield "keepalive", {}

    # ------------------------------------------------------------------ push notifications
    def _parse_push(self, raw: Any, task_id: str = "") -> tuple[PushConfig, dict]:
        """A push config in either shape (1.0 `authentication.scheme`, 0.3 `authentication.schemes`), checked
        like job callbacks: public https only unless the operator allowed the host."""
        if not isinstance(raw, dict):
            raise _invalid("the push notification config must be an object")
        url = raw.get("url")
        if not isinstance(url, str) or not url or len(url) > 2000:
            raise _invalid("the push notification config needs a url")
        try:
            self.jobs._check_callback(url)
        except JobError as e:
            raise _invalid(str(e).replace("callback_url", "the push notification url")) from None
        auth = raw.get("authentication") or {}
        if not isinstance(auth, dict):
            raise _invalid("authentication must be an object")
        schemes = auth.get("schemes") if isinstance(auth.get("schemes"), list) else []
        scheme = auth.get("scheme") or (schemes[0] if schemes else None)
        creds, token = auth.get("credentials"), raw.get("token")
        if scheme is not None and (not isinstance(scheme, str) or not _SCHEME_RE.match(scheme)):
            raise _invalid("authentication.scheme must be an HTTP auth scheme such as Bearer")
        for name, v in (("authentication.credentials", creds), ("token", token)):
            if v is not None and (not isinstance(v, str) or not _HEADER_RE.match(v)):
                raise _invalid(f"{name} must be printable text of up to 4096 characters")
        cid = _str_id(raw.get("id"), "id") or new_id("push")
        return PushConfig(id=cid, task_id=task_id, url=url, scheme=scheme), {"token": token, "credentials": creds}

    def _push_configs(self, job_id: str) -> list[PushConfig]:
        return list(self._push.get(job_id, []))

    def _put_push_configs(self, job_id: str, configs: list[PushConfig]) -> None:
        if configs:
            self.store.kv_set_json(_PUSH_KEY + job_id, [c.model_dump() for c in configs])
            self._push[job_id] = list(configs)
        else:
            self.store.kv_delete(_PUSH_KEY + job_id)
            self._push.pop(job_id, None)

    def _check_push_room(self, job_id: str, config_id: str) -> None:
        others = [c for c in self._push_configs(job_id) if c.id != config_id]
        if len(others) >= self.settings.a2a_max_push_configs:
            raise _invalid(f"a task can have at most {self.settings.a2a_max_push_configs} push notification configs")

    def _save_push(self, job: Job, cfg: PushConfig, secret: dict, ver: str) -> PushConfig:
        self._check_push_room(job.id, cfg.id)
        cfg = cfg.model_copy(update={"task_id": job.id, "version": ver, "sent": self._marker(job)})
        self.vault.put(f"{_PUSH_KEY}{job.id}.{cfg.id}", json.dumps(secret))
        self._put_push_configs(job.id, [*(c for c in self._push_configs(job.id) if c.id != cfg.id), cfg])
        return cfg

    @staticmethod
    def _marker(job: Job) -> str:
        return f"{task_state(job.state)}:{len(job.turns)}"

    @staticmethod
    def _push_view(c: PushConfig, ver: str) -> dict:
        """A config as the client set it, minus its token and credentials: hangar never echoes them back."""
        if ver == V1:
            return {"id": c.id, "taskId": c.task_id, "url": c.url,
                    **({"authentication": {"scheme": c.scheme}} if c.scheme else {})}
        inner = {"id": c.id, "url": c.url, **({"authentication": {"schemes": [c.scheme]}} if c.scheme else {})}
        return {"taskId": c.task_id, "pushNotificationConfig": inner}

    async def _op_push_set(self, caller: Caller, params: dict, ver: str, request: Request) -> dict:
        job = self._job(caller, params.get("taskId"))
        raw = params if ver == V1 else params.get("pushNotificationConfig")
        cfg, secret = self._parse_push(raw, job.id)
        return self._push_view(self._save_push(job, cfg, secret, ver), ver)

    def _find_push(self, caller: Caller, params: dict, ver: str) -> tuple[Job, list[PushConfig], PushConfig]:
        job = self._job(caller, params.get("taskId") if ver == V1 else params.get("id"))
        cid = params.get("id") if ver == V1 else params.get("pushNotificationConfigId")
        configs = self._push_configs(job.id)
        found = next((c for c in configs if c.id == cid), None) if cid else (configs[0] if configs else None)
        if found is None:
            raise _not_found("push notification config")
        return job, configs, found

    async def _op_push_get(self, caller: Caller, params: dict, ver: str, request: Request) -> dict:
        return self._push_view(self._find_push(caller, params, ver)[2], ver)

    async def _op_push_list(self, caller: Caller, params: dict, ver: str, request: Request) -> Any:
        job = self._job(caller, params.get("taskId") if ver == V1 else params.get("id"))
        views = [self._push_view(c, ver) for c in self._push_configs(job.id)]
        return {"configs": views, "nextPageToken": ""} if ver == V1 else views

    async def _op_push_delete(self, caller: Caller, params: dict, ver: str, request: Request) -> Any:
        job, configs, found = self._find_push(caller, params, ver)
        self.store.delete_secret(f"{_PUSH_KEY}{job.id}.{found.id}")
        self._put_push_configs(job.id, [c for c in configs if c.id != found.id])
        return {} if ver == V1 else None

    async def _push_loop(self) -> None:
        busy: dict[str, asyncio.Task] = {}  # one delivery round per job at a time; a slow webhook delays only its job
        try:
            while True:
                await asyncio.sleep(self.settings.a2a_poll_seconds)
                for job_id in list(self._push):
                    if job_id not in busy:
                        busy[job_id] = t = asyncio.create_task(self.push(job_id))
                        t.add_done_callback(lambda _, j=job_id: busy.pop(j, None))
        finally:
            for t in list(busy.values()):
                t.cancel()

    async def push(self, job_id: str) -> None:
        """Notify each of a job's push configs once per task state (and again when a follow-up reopens it)."""
        try:
            job = self.jobs.get(job_id)
        except JobNotFound:
            for c in self._push_configs(job_id):
                self.store.delete_secret(f"{_PUSH_KEY}{job_id}.{c.id}")
            self._put_push_configs(job_id, [])
            return
        marker = self._marker(job)
        due = [c for c in self._push_configs(job_id) if c.sent != marker]
        if not due:
            return
        results = await asyncio.gather(*(self._notify(job, c) for c in due), return_exceptions=True)
        for c, res in zip(due, results, strict=True):
            if isinstance(res, Exception):  # marked sent anyway: retrying every poll would only repeat the error
                log.error("A2A push notification %s for %s failed: %s", c.id, job_id, res)
        sent = {c.id for c in due}
        # Re-read: a config may have been deleted while the requests were out.
        self._put_push_configs(job_id, [c.model_copy(update={"sent": marker}) if c.id in sent else c
                                        for c in self._push_configs(job_id)])

    async def _notify(self, job: Job, c: PushConfig) -> None:
        task = self.task(job)
        st = task["status"]["state"]
        if c.version == V03:
            body: dict = _v03_task(task)  # 0.3 servers post the task itself
        elif st in TERMINAL | PAUSED:
            body = {"task": task}  # the outcome, with its artifacts, in one delivery
        else:
            body = {"statusUpdate": {"taskId": job.id, "contextId": task["contextId"], "status": task["status"]}}
        raw = json.dumps(body).encode()
        headers = {"Content-Type": "application/a2a+json" if c.version == V1 else "application/json",
                   "User-Agent": "hangar"}
        secret = json.loads(self.vault.get_text(f"{_PUSH_KEY}{job.id}.{c.id}") or "{}")
        if secret.get("token"):
            headers["X-A2A-Notification-Token"] = secret["token"]
        if c.scheme and secret.get("credentials"):
            headers["Authorization"] = f"{c.scheme} {secret['credentials']}"
        if job.callback_secret:  # a jobs token's bound key signs these like its job callbacks
            try:
                key = self.jobs.broker.secret_text(job.callback_secret)
                headers["X-Hangar-Signature"] = "sha256=" + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
            except Exception:  # noqa: BLE001
                log.warning("A2A push for %s not signed: credential %s unavailable", job.id, job.callback_secret)
        host = (urlparse(c.url).hostname or "").strip("[]")
        if not self.jobs._callback_allowed(host):  # the name may resolve somewhere else now (spec 0009 §5.5)
            try:
                addrs = await self.jobs.resolve(host)
            except OSError as e:
                self.jobs._event(job, f"A2A push notification not sent: cannot resolve {host} ({e})")
                return
            if not addrs or any(_internal(ipaddress.ip_address(a)) for a in addrs):
                self.jobs._event(job, f"A2A push notification not sent: {host} resolves to an internal address "
                                      "(allow it in HANGAR_JOB_CALLBACK_HOSTS)")
                return
        err = ""
        from hangar.frontdoor import outbound_transport  # front-door-auth: connect to the checked address

        async with httpx.AsyncClient(transport=self.jobs.transport or outbound_transport(self.jobs), timeout=10,
                                     follow_redirects=False) as client:
            for attempt in range(3):
                try:
                    r = await client.post(c.url, content=raw, headers=headers)
                    if r.status_code < 300:
                        return
                    err = f"HTTP {r.status_code}"
                except httpx.HTTPError as e:
                    err = type(e).__name__
                await asyncio.sleep(0 if self.jobs.transport else 2 ** attempt)
        self.jobs._event(job, f"A2A push notification to {host} failed after 3 attempts: {err}")
