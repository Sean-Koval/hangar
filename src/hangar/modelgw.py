"""The model gateway, record mode (#177; docs/model-gateway.md): a harness's model API calls go through the control
plane, which keeps the API key and records every request and response as a tape.

Replay-as-fork (#179), steer-point replay and the nightly determinism report need what the model actually answered.
Automated runs use API keys (part A, #125), which makes a recording proxy legitimate; a subscription login can't go
through one, so `record` refuses it.

**Wiring.** A sandbox of an environment with `model_gateway: record` gets `ANTHROPIC_BASE_URL` (claude-code) or
`OPENAI_BASE_URL` (codex) pointing at `<HANGAR_GATEWAY_URL>/model/<provider>`, and a per-sandbox bearer (`hgm_…`,
minted like the git gateway's grants, revoked with them) in the harness's API key variable. The real key stays in the
vault; the gateway sends it upstream. So `record` also takes the API key out of the sandbox.

**Forwarding.** The request body is forwarded byte for byte, with every header except the hop-by-hop ones, the
bearer and `accept-encoding`, so prompt-caching fields and `anthropic-beta` reach the provider untouched and costs
don't change. Responses stream back as they arrive (SSE too); the gateway keeps a copy.

**Tapes.** Per run (a session's turn: `{session_id}.{run_id}`), in `<data>/tapes/<run>/tape/` and, at the turn's
end, the outbox (#163) under `v1/tapes/{date}/<run>/tape/` (the bucket deletes them after 30 days):

* `{seq:05d}.json`: `{seq, req_hash, req_delta, resp, resp_hash, prev_hash, at, usage}`. The request and response
  are redacted (the broker's values and the token formats secretscan knows) and normalized before hashing: volatile
  headers dropped, tool-call and message ids, timestamps, `/tmp/...` paths and hangar's ids replaced with stable
  placeholders. `req_delta` stores a request as the messages it appends to an earlier one of the run (`base`), since
  the conversation's prefix repeats; `full` when it extends none. SSE responses are kept as their events, usage from
  the final ones. `prev_hash` is the previous entry's hash (the first's is the run's genesis hash), so the chain
  breaks if any entry is changed, dropped or reordered.
* `index.json`: the chain's head, the run, provider, model, the harness manifest's digest, and `untrusted`: the job
  has an untrusted source or context item, or the turn's taint fired (#129). A tainted tape can replay an attacker's
  tool call: it isn't promotion evidence without review (flightdeck's rule) and replays run with egress `none`.

`validate(dir)` recomputes every request hash from the deltas, every response hash and the chain.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from hangar.auth import hash_token
from hangar.models import CredentialKind, ModelGrant, Sandbox, SandboxState, Session, now

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet
    from hangar.models import Credential, SessionTurn

log = logging.getLogger("hangar.modelgw")

PREFIX = "hgm_"
SCHEMA = "hangar.tape/v1"
MAX_REQUEST = 32 * 1024 * 1024  # a request body; Claude Code's biggest are a few MB of conversation
MAX_RECORDED = 64 * 1024 * 1024  # a response kept for the tape; past it the tape says `truncated`
RECENT = 8  # earlier requests of a run a new one may be a delta of (subagents interleave conversations)
RECORDED_PATHS = ("/messages", "/responses", "/chat/completions")


class ProviderInfo(NamedTuple):
    upstream: str  # the provider's API
    harness: str
    base_env: str  # what points the harness at the gateway
    base_suffix: str
    key_env: str  # where the harness reads its API key: the gateway's bearer, here
    auth: str  # how the real key goes upstream: x-api-key | bearer
    turns: str  # the request field the conversation grows in


PROVIDERS = {
    "anthropic": ProviderInfo("https://api.anthropic.com", "claude-code", "ANTHROPIC_BASE_URL", "",
                              "ANTHROPIC_API_KEY", "x-api-key", "messages"),
    "openai": ProviderInfo("https://api.openai.com", "codex", "OPENAI_BASE_URL", "/v1", "CODEX_API_KEY", "bearer",
                           "input"),
}
HARNESS_PROVIDER = {p.harness: name for name, p in PROVIDERS.items()}

_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
        "proxy-authorization", "proxy-connection", "x-api-key", "authorization", "accept-encoding"}
# Request headers a tape keeps (the rest are volatile: retries, timeouts, SDK and OS versions, request ids)
_KEEP_REQ = {"anthropic-version", "anthropic-beta", "openai-beta", "content-type"}
_KEEP_RESP = {"content-type"}
_PASS_RESP_DROP = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}


class ModelGatewayError(RuntimeError):
    pass


# ====================================================================== normalization and hashing
def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def genesis(run: str) -> str:
    return hashlib.sha256(f"{SCHEMA}\n{run}".encode()).hexdigest()


_IDS = re.compile(r"\b(toolu|srvtoolu|call|fc|msg|resp|rs|ws|ctc)_[A-Za-z0-9][A-Za-z0-9_-]{5,}")
_HANGAR_IDS = re.compile(r"\b(sb|ses|run|job|snap|mgt|gnt)-[0-9a-f]{8}\b")
_TS = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_TMP = re.compile(r"/tmp/[^\s\"'`)\]}>,;:]+")


class Normalizer:
    """Stable placeholders for one run: the same id always becomes the same `<kind:n>`, in requests and
    responses alike, so a tool call's id in a response matches its result in the next request."""

    def __init__(self, ids: dict[str, str] | None = None):
        self.ids: dict[str, str] = ids if ids is not None else {}

    def _id(self, m: re.Match) -> str:
        raw = m.group(0)
        if raw not in self.ids:
            kind = m.group(1)
            n = sum(1 for v in self.ids.values() if v.startswith(f"<{kind}:")) + 1
            self.ids[raw] = f"<{kind}:{n}>"
        return self.ids[raw]

    def text(self, s: str) -> str:
        s = _IDS.sub(self._id, s)
        s = _HANGAR_IDS.sub(self._id, s)
        s = _TS.sub("<ts>", s)
        return _TMP.sub("<tmp>", s)

    def __call__(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {self.text(k) if isinstance(k, str) else k: self(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self(v) for v in obj]
        return obj


def parse_sse(raw: bytes) -> list[dict[str, Any]]:
    """Server-sent events as [{"event": name, "data": parsed JSON or text}]."""
    out: list[dict[str, Any]] = []
    for block in re.split(rb"\r?\n\r?\n", raw):
        if not block.strip():
            continue
        name, data = None, []
        for line in block.splitlines():
            line = line.decode(errors="replace")
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        text = "\n".join(data)
        try:
            value: Any = json.loads(text) if text else None
        except ValueError:
            value = text
        out.append({"event": name, "data": value})
    return out


def usage_of(provider: str, resp: dict[str, Any]) -> dict[str, Any] | None:
    """The call's usage: Anthropic's message_start usage with message_delta's counts, OpenAI's response.completed
    (or a plain JSON body's `usage`)."""
    if "body" in resp:
        body = resp["body"]
        return body.get("usage") if isinstance(body, dict) else None
    usage: dict[str, Any] = {}
    for ev in resp.get("sse") or []:
        d = ev.get("data")
        if not isinstance(d, dict):
            continue
        if d.get("type") == "message_start" and isinstance((d.get("message") or {}).get("usage"), dict):
            usage.update(d["message"]["usage"])
        elif d.get("type") == "message_delta" and isinstance(d.get("usage"), dict):
            usage.update({k: v for k, v in d["usage"].items() if v is not None})
        elif d.get("type") == "response.completed" and isinstance((d.get("response") or {}).get("usage"), dict):
            usage.update(d["response"]["usage"])
    return usage or None


_MISSING = object()


def make_delta(recent: list[tuple[int, dict[str, Any]]], cur: dict[str, Any], turns: str) -> dict[str, Any]:
    """`cur` as what it appends to the earlier request whose `turns` list is its longest prefix; `full` if none."""
    best: tuple[int, int, dict[str, Any]] | None = None
    cb = cur.get("body")
    cl = cb.get(turns) if isinstance(cb, dict) else None
    if isinstance(cl, list):
        for seq, prev in recent:
            pb = prev.get("body")
            pl = pb.get(turns) if isinstance(pb, dict) else None
            if isinstance(pl, list) and len(pl) <= len(cl) and cl[:len(pl)] == pl and \
                    (best is None or len(pl) > best[0]):
                best = (len(pl), seq, prev)
    if best is None or best[0] == 0:
        return {"full": cur}
    n, seq, prev = best
    pb = prev["body"]
    d: dict[str, Any] = {"base": seq, "append": cl[n:]}  # type: ignore[index]
    if sets := {k: v for k, v in cb.items() if k != turns and pb.get(k, _MISSING) != v}:  # type: ignore[union-attr]
        d["set"] = sets
    if unset := sorted(k for k in pb if k not in cb):  # type: ignore[operator]
        d["unset"] = unset
    if outer := {k: v for k, v in cur.items() if k != "body" and prev.get(k) != v}:
        d["outer"] = outer
    return d


def apply_delta(by_seq: dict[int, dict[str, Any]], d: dict[str, Any], turns: str) -> dict[str, Any]:
    """The full (normalized) request a delta stands for."""
    if "full" in d:
        return d["full"]
    base = by_seq[d["base"]]
    body = {k: v for k, v in base["body"].items() if k not in set(d.get("unset", []))}
    body.update(d.get("set", {}))
    body[turns] = [*base["body"][turns], *d["append"]]
    return {**base, **d.get("outer", {}), "body": body}


def validate(tape: Path) -> dict[str, Any]:
    """Check a tape directory (`.../tape/`): every request hash recomputes from the deltas, every response hash
    recomputes, the chain is intact from the genesis to the index's head. Raises ValueError naming the first
    problem; returns the index."""
    index = json.loads((tape / "index.json").read_text())
    turns = PROVIDERS.get(index.get("provider") or "", PROVIDERS["anthropic"]).turns
    prev = index["genesis"]
    if prev != genesis(index["run"]):
        raise ValueError("the genesis hash is not this run's")
    by_seq: dict[int, dict[str, Any]] = {}
    files = sorted(p for p in tape.glob("[0-9]*.json"))
    if len(files) != index["entries"]:
        raise ValueError(f"{len(files)} entries on disk, the index says {index['entries']}")
    for n, p in enumerate(files, 1):
        e = json.loads(p.read_text())
        if e["seq"] != n or p.name != f"{n:05d}.json":
            raise ValueError(f"entry {p.name}: out of sequence")
        if e["prev_hash"] != prev:
            raise ValueError(f"entry {n}: the chain is broken (prev_hash)")
        req = apply_delta(by_seq, e["req_delta"], turns)
        if digest(req) != e["req_hash"]:
            raise ValueError(f"entry {n}: the request hash doesn't recompute")
        if digest(e["resp"]) != e["resp_hash"]:
            raise ValueError(f"entry {n}: the response hash doesn't recompute")
        by_seq[n] = req
        prev = digest(e)
    if prev != index["head"]:
        raise ValueError("the chain's head isn't the index's")
    return index


# ====================================================================== the recorder
class RunTape:
    """The tape of one run, being written."""

    def __init__(self, run: str, root: Path, provider: str, session_id: str | None, run_id: str | None,
                 sandbox_id: str, started_at: float):
        self.run, self.dir, self.provider = run, root / "tape", provider
        self.session_id, self.run_id, self.sandbox_id = session_id, run_id, sandbox_id
        self.started_at = started_at
        self.seq, self.head = 0, genesis(run)
        self.recent: list[tuple[int, dict[str, Any]]] = []
        self.norm = Normalizer()
        self.model: str | None = None
        self.bytes = 0
        self.lock = asyncio.Lock()

    def write(self, entry: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        data = json.dumps(entry, ensure_ascii=False, sort_keys=True).encode()
        (self.dir / f"{entry['seq']:05d}.json").write_bytes(data)
        self.bytes += len(data)


class ModelGateway:
    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self.settings = fleet.settings
        self.store = fleet.store
        self.broker = fleet.broker
        self.issued: dict[str, dict[str, str]] = {}  # sandbox id -> env, from attach to _apply_session; never stored
        self.tapes: dict[str, RunTape] = {}
        self._pending: set[asyncio.Task] = set()

    @property
    def root(self) -> Path:
        return Path(self.settings.data_dir) / "tapes"

    def upstream(self, provider: str) -> str:
        return (self.settings.model_gateway_upstreams.get(provider) or PROVIDERS[provider].upstream).rstrip("/")

    def routes(self) -> list[Route]:
        return [Route("/model/{provider}/{rest:path}", self.handle, methods=["GET", "POST"])]

    # ------------------------------------------------------------------ grants
    def mint(self, sb: Sandbox, provider: str, credential: str) -> str:
        self.revoke(sb.id)
        raw = PREFIX + secrets.token_urlsafe(32)
        self.store.put(ModelGrant(sandbox_id=sb.id, token_hash=hash_token(raw), provider=provider,
                                  credential=credential, expires_at=sb.expires_at))
        return raw

    def grants(self, sandbox_id: str | None = None) -> list[ModelGrant]:
        return [g for g in self.store.list(ModelGrant) if sandbox_id is None or g.sandbox_id == sandbox_id]

    def revoke(self, sandbox_id: str) -> None:
        self.issued.pop(sandbox_id, None)
        for g in self.grants(sandbox_id):
            self.store.delete(ModelGrant, g.id)

    def extend(self, sb: Sandbox) -> None:
        for g in self.grants(sb.id):
            g.expires_at = sb.expires_at
            self.store.put(g)

    def verify(self, raw: str | None) -> ModelGrant | None:
        if not raw or not raw.startswith(PREFIX) or len(raw) > 128:
            return None
        h = hash_token(raw)
        grant = next((g for g in self.store.list(ModelGrant) if hmac.compare_digest(h, g.token_hash)), None)
        if grant is None:
            return None
        if grant.expires_at is not None and grant.expires_at < now():
            self.store.delete(ModelGrant, grant.id)
            return None
        sb = self.store.get(Sandbox, grant.sandbox_id)
        if sb is None or sb.state not in (SandboxState.STARTING, SandboxState.RUNNING):
            return None
        return grant

    # ------------------------------------------------------------------ sandbox wiring
    def attach(self, sb: Sandbox, spec: EnvSpec, harness: str | None, creds: list[Credential]) -> list[Credential]:
        """For `model_gateway: record`: mint the sandbox's bearer and drop the harness's API key from what is written
        into the sandbox (its env comes from `sandbox_env`). Refuses subscription logins: they can't be proxied."""
        from hangar import delivery, teamcreds

        if spec.model_gateway != "record" or delivery.sealed(sb) or delivery.builds_repo_code(sb):
            return creds
        provider = HARNESS_PROVIDER.get(harness or "")
        if provider is None:
            sb.info.setdefault("warnings", []).append(
                f"model_gateway: record has no gateway path for harness {harness or '(none)'}; its calls aren't "
                "recorded")
            return creds
        mine = [c for c in creds if c.harness == harness]
        if subs := [c.name for c in mine if teamcreds.is_subscription(c)]:
            raise ModelGatewayError(
                f"environment {spec.name} records model traffic (model_gateway: record), which needs an API key; "
                f"{', '.join(subs)} is a subscription login, which can't go through a recording proxy. Attach an "
                f"API key for {harness}, or set model_gateway: off")
        keys = [c for c in mine if c.kind == CredentialKind.API_KEY]
        if not keys:
            raise ModelGatewayError(f"environment {spec.name} records model traffic (model_gateway: record) but the "
                                    f"sandbox has no {harness} API key for the gateway to use upstream")
        if not self.settings.gateway_url:
            raise ModelGatewayError(f"environment {spec.name} uses model_gateway: record but HANGAR_GATEWAY_URL is "
                                    "not set (docs/model-gateway.md)")
        key = keys[0]
        info = PROVIDERS[provider]
        token = self.mint(sb, provider, key.name)
        base = f"{self.settings.gateway_url.rstrip('/')}/model/{provider}{info.base_suffix}"
        self.issued[sb.id] = {info.base_env: base, info.key_env: token}
        sb.info["model_gateway"] = {"mode": "record", "provider": provider, "credential": key.name}
        self.fleet.audit(sb.created_by or "system", "modelgw.grant", sb.id, provider=provider, credential=key.name)
        return [c for c in creds if c.name != key.name]

    def sandbox_env(self, sb: Sandbox) -> dict[str, str]:
        """The base URL and bearer `attach` minted (once: the token is never stored in clear)."""
        return self.issued.pop(sb.id, {})

    # ------------------------------------------------------------------ the proxy
    @staticmethod
    def _token(request: Request) -> str | None:
        if key := request.headers.get("x-api-key"):
            return key.strip()
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def _error(self, provider: str, status: int, message: str) -> Response:
        """An error the harness reads like its provider's own (it retries a 5xx, as after a restart)."""
        if provider == "openai":
            body: dict[str, Any] = {"error": {"message": message, "type": "hangar_gateway", "code": status}}
        else:
            body = {"type": "error", "error": {"type": "api_error" if status >= 500 else "authentication_error",
                                               "message": message}}
        return JSONResponse(body, status)

    async def handle(self, request: Request) -> Response:
        provider = request.path_params.get("provider", "")
        if provider not in PROVIDERS:
            return JSONResponse({"error": f"hangar: no model gateway for '{provider}'"}, 404)
        grant = self.verify(self._token(request))
        if grant is None or grant.provider != provider:
            return self._error(provider, 401, "hangar: a valid model gateway bearer for this sandbox is required")
        try:
            body = await request.body()
        except ClientDisconnect:
            return Response(status_code=499)
        if len(body) > MAX_REQUEST:
            return self._error(provider, 413, "hangar: request too large for the model gateway")
        try:
            key = self.broker.secret_text(grant.credential)
        except Exception as e:  # noqa: BLE001 - never a secret in the message
            return self._error(provider, 502, f"hangar: the gateway's API key is unavailable ({type(e).__name__})")
        info = PROVIDERS[provider]
        rest = request.path_params.get("rest", "")
        url = f"{self.upstream(provider)}/{rest}" + (f"?{request.url.query}" if request.url.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        headers["accept-encoding"] = "identity"  # the tape reads the stream as it passes
        if info.auth == "x-api-key":
            headers["x-api-key"] = key
        else:
            headers["authorization"] = f"Bearer {key}"
        record = request.method == "POST" and rest.rstrip("/").endswith(RECORDED_PATHS)
        client = self.fleet.gateway._http()
        started = time.monotonic()
        try:
            resp = await client.send(client.build_request(request.method, url, headers=headers, content=body),
                                     stream=True)
        except Exception as e:  # noqa: BLE001 - like a provider 5xx: the harness retries, #166 classifies it
            log.warning("model gateway: %s %s failed: %s", provider, rest[:100], type(e).__name__)
            return self._error(provider, 502, f"hangar: {provider} didn't answer ({type(e).__name__})")
        out = {k: v for k, v in resp.headers.items() if k.lower() not in _PASS_RESP_DROP}
        kept = bytearray()
        truncated = False

        async def stream() -> Any:
            nonlocal truncated
            ok = False
            try:
                async for chunk in resp.aiter_raw():
                    if record:
                        if len(kept) + len(chunk) <= MAX_RECORDED:
                            kept.extend(chunk)
                        else:
                            truncated = True
                    yield chunk
                ok = True
            finally:
                await resp.aclose()
                if record:
                    self._spawn(self.record(grant, request.method, "/" + rest, dict(request.headers), body,
                                            resp.status_code, dict(resp.headers), bytes(kept), truncated or not ok,
                                            time.monotonic() - started))

        return StreamingResponse(stream(), status_code=resp.status_code, headers=out)

    def _spawn(self, coro: Any) -> None:
        t = asyncio.ensure_future(coro)
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    async def settle(self) -> None:
        """Wait for the entries being written (tests, a turn's end)."""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    # ------------------------------------------------------------------ writing the tape
    def _run_of(self, sandbox_id: str) -> tuple[str, str | None, str | None, float]:
        """(run key, session id, run id, started) for the sandbox's current turn."""
        sb = self.store.get(Sandbox, sandbox_id)
        sid = sb.labels.get("hangar.session") if sb is not None else None
        s = self.store.get(Session, sid) if sid else None
        turn = next((t for t in reversed(s.turns) if t.run_id), None) if s is not None else None
        if s is not None and turn is not None:
            return f"{s.id}.{turn.run_id}", s.id, turn.run_id, turn.started_at or now()
        return f"{sandbox_id}.sandbox", None, None, sb.created_at if sb is not None else now()

    def _scrub(self, obj: Any) -> Any:
        from hangar import outbox

        return outbox.of(self.fleet)._scrub(obj)

    async def record(self, grant: ModelGrant, method: str, path: str, req_headers: dict[str, str], body: bytes,
                     status: int, resp_headers: dict[str, str], raw: bytes, truncated: bool, seconds: float) -> None:
        try:
            run, session_id, run_id, started = self._run_of(grant.sandbox_id)
            tape = self.tapes.get(run)
            if tape is None:
                tape = self.tapes[run] = RunTape(run, self.root / run, grant.provider, session_id, run_id,
                                                 grant.sandbox_id, started)
            async with tape.lock:
                await asyncio.to_thread(self._append, tape, method, path, req_headers, body, status, resp_headers,
                                        raw, truncated)
        except Exception:  # noqa: BLE001 - a tape problem never fails the model call
            log.exception("model gateway: could not record a call")

    def _append(self, tape: RunTape, method: str, path: str, req_headers: dict[str, str], body: bytes, status: int,
                resp_headers: dict[str, str], raw: bytes, truncated: bool) -> None:
        info = PROVIDERS[tape.provider]
        try:
            parsed: Any = json.loads(body) if body else None
        except ValueError:
            parsed = body.decode(errors="replace")
        hdrs = {k.lower(): v for k, v in req_headers.items() if k.lower() in _KEEP_REQ}
        req = tape.norm(self._scrub({"method": method, "path": path, "headers": hdrs, "body": parsed}))
        if isinstance(parsed, dict) and isinstance(parsed.get("model"), str):
            tape.model = parsed["model"]
        ctype = resp_headers.get("content-type", "")
        resp: dict[str, Any] = {"status": status,
                                "headers": {k.lower(): v for k, v in resp_headers.items() if k.lower() in _KEEP_RESP}}
        if "text/event-stream" in ctype:
            resp["sse"] = parse_sse(raw)
        else:
            try:
                resp["body"] = json.loads(raw) if raw else None
            except ValueError:
                resp["body"] = raw.decode(errors="replace")
        if truncated:
            resp["truncated"] = True
        resp = tape.norm(self._scrub(resp))
        tape.seq += 1
        entry = {"seq": tape.seq, "req_hash": digest(req), "req_delta": make_delta(tape.recent, req, info.turns),
                 "resp": resp, "resp_hash": digest(resp), "prev_hash": tape.head, "at": now(),
                 "usage": usage_of(tape.provider, resp)}
        tape.write(entry)
        tape.head = digest(entry)
        tape.recent = [*tape.recent, (tape.seq, req)][-RECENT:]
        tape.dir.mkdir(parents=True, exist_ok=True)
        (tape.dir / "index.json").write_text(json.dumps(self._index(tape, None), indent=1, sort_keys=True))

    def _index(self, tape: RunTape, untrusted: tuple[bool, list[str]] | None) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema": SCHEMA, "run": tape.run, "session_id": tape.session_id, "run_id": tape.run_id,
            "sandbox_id": tape.sandbox_id, "provider": tape.provider, "model": tape.model,
            "entries": tape.seq, "genesis": genesis(tape.run), "head": tape.head, "started_at": tape.started_at,
            "bytes": tape.bytes, "final": untrusted is not None,
        }
        if untrusted is not None:
            d["untrusted"], d["untrusted_reasons"] = untrusted
        return d

    # ------------------------------------------------------------------ the turn's end
    def untrusted(self, s: Session, turn: SessionTurn | None, job: Any) -> tuple[bool, list[str]]:
        from hangar import taint

        why: list[str] = []
        if job is not None:
            if job.untrusted_source:
                why.append(f"untrusted source {job.untrusted_source}")
            if any(c.untrusted for c in job.context):
                why.append("untrusted context item")
            if taint.tainted(job):
                why.append("the job is tainted")
        if (turn is not None and taint.tainted(turn)) or taint.tainted(s):
            why.append("the turn read untrusted content (taint)")
        return bool(why), why

    async def turn_ended(self, s: Session, turn: SessionTurn, job: Any = None) -> Path | None:
        """Close the turn's tape: its final index (untrusted, the manifest), then the outbox. None: no tape."""
        await self.settle()
        run = f"{s.id}.{turn.run_id}"
        tape = self.tapes.pop(run, None)
        if tape is None:
            return None
        async with tape.lock:
            idx = self._index(tape, self.untrusted(s, turn, job))
            idx["harness"], idx["manifest"] = turn.harness or s.harness, turn.manifest
            idx["harness_digest"] = self._harness_digest(s, turn)
            idx["finished_at"] = turn.finished_at or now()
            (tape.dir / "index.json").write_text(json.dumps(idx, indent=1, sort_keys=True))
            self._export(tape, idx)
        self.prune()
        return tape.dir

    def _harness_digest(self, s: Session, turn: SessionTurn) -> str | None:
        from hangar import manifest

        try:
            n = s.turns.index(turn) + 1
            m = manifest.store(self.settings).get(s.id, n) if turn.manifest else None
            return m.get("harness_digest") if m else None
        except Exception:  # noqa: BLE001
            return None

    def _export(self, tape: RunTape, idx: dict[str, Any]) -> None:
        from hangar import outbox

        ob = outbox.of(self.fleet)
        if not ob.enabled:
            return
        files: dict[str, Any] = {f"tape/{p.name}": json.loads(p.read_text())
                                 for p in sorted(tape.dir.glob("[0-9]*.json"))}
        files["tape/index.json"] = idx
        prefix = f"{outbox.PREFIX}/tapes/{outbox.day(idx.get('finished_at'))}/{tape.run}/"
        ob._stage(f"tape:{tape.run}", prefix, files, session_id=tape.session_id, run_id=tape.run_id)
        ob._kick()

    def prune(self) -> int:
        """Local tapes older than HANGAR_MODEL_TAPE_DAYS go (the bucket's lifecycle rule does the same there)."""
        cutoff = time.time() - self.settings.model_tape_days * 86400
        n = 0
        with contextlib.suppress(FileNotFoundError):
            for d in self.root.iterdir():
                if d.is_dir() and d.stat().st_mtime < cutoff and d.name not in self.tapes:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
        return n


def of(fleet: Fleet) -> ModelGateway:
    """The fleet's model gateway (made on first use, so Fleet itself needs no change)."""
    g = fleet.__dict__.get("_modelgw")
    if g is None:
        g = fleet.__dict__["_modelgw"] = ModelGateway(fleet)
    return g
