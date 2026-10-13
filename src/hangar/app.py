"""ASGI application: REST API + websocket terminals + dashboard + MCP endpoint, in one process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from importlib import resources
from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hangar.auth import SCOPES, TokenAuth, bearer_from_headers
from hangar.broker import Broker, CredentialError
from hangar.config import Settings, get_settings
from hangar.envspec import EnvCatalog
from hangar.fleet import Fleet, FleetError
from hangar.github import verify_signature
from hangar.harnesses import HARNESSES, PACK_ROOT
from hangar.jobs import Caller, JobService, SubmitJob, handle_github
from hangar.mcp_server import build_mcp, current_actor
from hangar.models import Credential, CredentialKind, Resources, Sandbox
from hangar.providers.base import ProviderError
from hangar.sessions import NewSession, SessionService
from hangar.store import Store
from hangar.vault import Vault, load_or_create_key

log = logging.getLogger("hangar")


class Hangar:
    """Wires the components together; shared by the server and the CLI."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.store = Store(self.settings.db_path)
        self.vault = Vault(self.store, load_or_create_key(self.settings.secret_key, self.settings.data_dir))
        self.broker = Broker(self.store, self.vault, self.settings.sandbox_user)
        self.catalog = EnvCatalog(self.settings.env_dirs, save_dir=self.settings.saved_env_dir)
        self.fleet = Fleet(self.settings, self.store, self.broker, self.catalog)
        self.auth = TokenAuth(self.store, self.settings.admin_token)
        self.sessions = SessionService(self.fleet, self.vault)
        self.jobs = JobService(self.fleet, self.sessions)


# ---------------------------------------------------------------------------- MCP auth
class MCPAuthMiddleware:
    """Accepts static hangar tokens and (when enabled) OAuth access tokens issued by hangar."""

    def __init__(self, app, auth: TokenAuth, oauth=None, resource_metadata_url: str | None = None):
        self.app = app
        self.auth = auth
        self.oauth = oauth
        self.resource_metadata_url = resource_metadata_url

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        token = bearer_from_headers(headers)
        actor = self.auth.verify(token)
        if actor is None and self.oauth is not None:
            actor = await self.oauth.verify(token)
        if actor is None:
            challenge = 'Bearer realm="hangar"'
            if self.resource_metadata_url:  # MCP auth discovery (RFC 9728)
                challenge += f', resource_metadata="{self.resource_metadata_url}"'
            resp = JSONResponse({"error": "unauthorized", "hint": "send Authorization: Bearer <hangar token>"},
                                status_code=401, headers={"WWW-Authenticate": challenge})
            return await resp(scope, receive, send)
        tok = current_actor.set(actor)
        try:
            await self.app(scope, receive, send)
        finally:
            current_actor.reset(tok)


class MCPPathMiddleware:
    """Serve POST /mcp directly instead of a 307 to /mcp/ (clients drop auth on redirects behind TLS proxies)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope, path="/mcp/", raw_path=b"/mcp/")
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------- request models
class CreateSandbox(BaseModel):
    environment: str
    name: str | None = None
    repo: str | None = None
    branch: str | None = None
    work_branch: str | None = None
    credentials: list[str] | None = None
    provider: str | None = None
    ttl_minutes: int | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    resources: Resources | None = None
    harness: str | None = None
    count: int = 1


class ExecBody(BaseModel):
    command: str
    cwd: str | None = None
    timeout: int = 120


class AgentBody(BaseModel):
    prompt: str
    harness: str | None = None
    cwd: str | None = None
    model: str | None = None


class ForkBody(BaseModel):
    count: int = 1
    name_prefix: str | None = None
    work_branch_prefix: str | None = None
    credentials: list[str] | None = None
    provider: str | None = None
    ttl_minutes: int | None = None


class SnapshotBody(BaseModel):
    label: str | None = None
    kind: str = "auto"


class CredentialBody(BaseModel):
    name: str
    harness: str
    kind: CredentialKind
    value: str
    description: str | None = None
    env_var: str | None = None
    file_path: str | None = None


class LoginBody(BaseModel):
    harness: str
    credential: str
    environment: str | None = None
    provider: str | None = None


class BuildBody(BaseModel):
    provider: str | None = None
    force: bool = False


class RegisterImage(BaseModel):
    image: str
    provider: str | None = None


class FollowUpBody(BaseModel):
    message: str


class SendBody(BaseModel):
    message: str


class OpenBody(BaseModel):
    harness: str | None = None


class TokenBody(BaseModel):
    name: str
    scopes: list[str] = Field(default_factory=lambda: ["admin"])
    callback_secret: str | None = None  # jobs tokens: vault `webhook` credential that signs their callbacks


def _setup_oauth(app: FastAPI, h: Hangar):
    """Mount the OAuth 2.1 endpoints the MCP auth spec expects, at the server root."""
    if not h.settings.oauth_enabled:
        return None, None
    from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    from pydantic import AnyHttpUrl
    from starlette.routing import Route

    from hangar.oauth import HangarOAuthProvider, consent_get, consent_post

    base = h.settings.public_url.rstrip("/")
    from urllib.parse import urlparse

    if urlparse(base).path not in ("", "/"):
        log.warning("OAuth disabled: HANGAR_PUBLIC_URL must not contain a path (serve hangar at a host root)")
        return None, None
    provider = HangarOAuthProvider(h.store, h.auth, base, h.settings.oauth_redirect_hosts,
                                   h.settings.oauth_max_clients, h.settings.oauth_grant_days)
    try:
        routes = create_auth_routes(
            provider, issuer_url=AnyHttpUrl(base),
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["hangar"],
                                                                  default_scopes=["hangar"]),
            revocation_options=RevocationOptions(enabled=True),
        )
        routes += create_protected_resource_routes(
            resource_url=AnyHttpUrl(f"{base}/mcp"), authorization_servers=[AnyHttpUrl(base)],
            scopes_supported=["hangar"], resource_name="hangar",
        )
    except Exception as e:  # noqa: BLE001 - e.g. a non-https public URL
        log.warning("OAuth disabled: %s (set HANGAR_PUBLIC_URL to an https URL to enable it)", e)
        return None, None
    async def _consent_get(request):
        return await consent_get(request, provider)

    async def _consent_post(request):
        return await consent_post(request, provider)

    routes.append(Route("/oauth/consent", _consent_get, methods=["GET"]))
    routes.append(Route("/oauth/consent", _consent_post, methods=["POST"]))
    for i, r in enumerate(routes):
        app.router.routes.insert(i, r)
    u = urlparse(base)
    return provider, f"{u.scheme}://{u.netloc}/.well-known/oauth-protected-resource{u.path}/mcp"


def create_app(settings: Settings | None = None) -> FastAPI:
    h = Hangar(settings)
    mcp = build_mcp(h.fleet, h.jobs, h.sessions)
    mcp_asgi = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, host="0.0.0.0")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        boot = h.auth.ensure_bootstrap()
        if boot:
            (h.settings.data_dir / "bootstrap-token").write_text(boot + "\n")
            (h.settings.data_dir / "bootstrap-token").chmod(0o600)
            log.warning("=" * 72)
            log.warning("Bootstrap admin token (also saved to %s/bootstrap-token):", h.settings.data_dir)
            log.warning("    %s", boot)
            log.warning("=" * 72)
        await h.fleet.start()
        await h.sessions.start()  # first: jobs look up their sessions
        await h.jobs.start()
        async with mcp.session_manager.run():
            yield
        await h.jobs.stop()
        await h.sessions.stop()
        await h.fleet.stop()

    app = FastAPI(title="hangar", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.hangar = h
    oauth, rm_url = _setup_oauth(app, h)
    app.state.oauth = oauth
    app.mount("/mcp", MCPAuthMiddleware(mcp_asgi, h.auth, oauth, rm_url))
    app.add_middleware(MCPPathMiddleware)

    @app.exception_handler(FleetError)
    @app.exception_handler(CredentialError)
    @app.exception_handler(ProviderError)
    @app.exception_handler(KeyError)
    @app.exception_handler(ValueError)
    async def _domain_errors(_: Request, exc: Exception):
        status = 404 if isinstance(exc, KeyError) or "no sandbox" in str(exc) else 400
        return JSONResponse({"error": str(exc).strip("'\"")}, status_code=status)

    def actor(request: Request) -> str:
        res = h.auth.verify_scopes(bearer_from_headers(request.headers))
        if res is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if "admin" not in res[1]:
            raise HTTPException(403, f"token scopes {res[1]} do not allow this route")
        return f"ui:{res[0]}"

    def job_caller(request: Request) -> Caller:
        """Admin sees every job; a `jobs`-scoped token is restricted and sees only jobs it submitted."""
        p = h.auth.principal(bearer_from_headers(request.headers))
        if p is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if p.admin:
            return Caller(p.name, p.id)
        if "jobs" in p.scopes:
            return Caller(p.name, p.id, restricted=True, callback_secret=p.callback_secret)
        raise HTTPException(403, f"token scopes {p.scopes} do not allow jobs")

    def owner_filter(c: Caller) -> str | None:
        return c.owner if c.restricted else None

    A = Depends(actor)
    J = Depends(job_caller)
    fleet = h.fleet

    def sbd(s: Sandbox) -> dict[str, Any]:
        return s.model_dump(mode="json")

    # ------------------------------------------------------------------ meta
    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/me")
    async def me(who: str = A):
        return {"actor": who}

    @app.get("/api/overview")
    async def overview(_: str = A):
        return fleet.overview()

    @app.get("/api/connect")
    async def connect(_: str = A):
        url = h.settings.public_url.rstrip("/") + "/mcp"
        return {
            "mcp_url": url,
            "codex_config_toml": (
                f'[mcp_servers.hangar]\nurl = "{url}"\nbearer_token_env_var = "HANGAR_TOKEN"\n'
                "tool_timeout_sec = 900\n"
            ),
            "codex_cli": f"codex mcp add hangar --url {url} --bearer-token-env-var HANGAR_TOKEN",
            "claude_code_cli": (
                f'claude mcp add --transport http hangar {url} --header "Authorization: Bearer $HANGAR_TOKEN"'
            ),
            "oauth": app.state.oauth is not None,
            "chatgpt": (
                f"In ChatGPT: Settings → Connectors → Create. MCP server URL: {url}. Authentication: OAuth. "
                "ChatGPT registers itself and opens Hangar's approval page, where you paste a hangar token."
                if app.state.oauth is not None else
                "OAuth is off: set HANGAR_PUBLIC_URL to the https URL clients reach hangar at to enable it."
            ),
        }

    @app.get("/api/oauth/clients")
    async def oauth_clients(_: str = A):
        return app.state.oauth.clients() if app.state.oauth else []

    @app.delete("/api/oauth/clients/{client_id}")
    async def oauth_revoke(client_id: str, who: str = A):
        if not app.state.oauth:
            raise HTTPException(404, "OAuth is disabled")
        app.state.oauth.revoke_client(client_id)
        fleet.audit(who, "oauth.revoke", client_id)
        return {"revoked": client_id}

    # ------------------------------------------------------------------ environments
    @app.get("/api/environments")
    async def envs(_: str = A):
        return fleet.environments()

    @app.get("/api/environments/{name}")
    async def env_detail(name: str, _: str = A):
        d = fleet.environment_detail(name)
        return {**d, "yaml": yaml.safe_dump(d["raw"], sort_keys=False)}

    @app.put("/api/environments/{name}")
    async def env_save(name: str, request: Request, who: str = A):
        body = await request.body()
        data = yaml.safe_load(body.decode()) if body else {}
        if not isinstance(data, dict):
            raise HTTPException(400, "body must be a YAML/JSON mapping")
        data["name"] = name
        spec = fleet.save_environment(data, actor=who)
        return spec.model_dump(mode="json")

    @app.post("/api/environments/validate")
    async def env_validate(request: Request, _: str = A):
        body = await request.body()
        data = yaml.safe_load(body.decode()) if body else {}
        if not isinstance(data, dict):
            raise HTTPException(400, "body must be a YAML/JSON mapping")
        return fleet.validate_environment(data)

    @app.post("/api/environments/{name}/clone")
    async def env_clone(name: str, request: Request, who: str = A):
        body = await request.body()
        data = yaml.safe_load(body.decode()) if body else {}
        if not isinstance(data, dict) or not data.get("name"):
            raise HTTPException(400, "body must be a mapping with the new environment's `name`")
        new = data.pop("name")
        return fleet.clone_environment(name, new, data, actor=who).model_dump(mode="json")

    @app.post("/api/harnesses/{harness}/pack")
    async def pack_build(harness: str, body: BuildBody, who: str = A):
        return {"harness": harness, "image": await fleet.build_pack(harness, body.provider, body.force, actor=who)}

    @app.get("/api/harnesses/{harness}/context")
    async def pack_context(harness: str, provider: str | None = None, _: str = A):
        from fastapi.responses import Response

        tag, ctx = fleet.pack_context(harness, provider)
        return Response(ctx.tar(), media_type="application/x-tar", headers={"X-Hangar-Image": tag})

    @app.post("/api/harnesses/{harness}/images")
    async def pack_register(harness: str, body: RegisterImage, who: str = A):
        return {"harness": harness, "image": fleet.register_pack(harness, body.image, body.provider, actor=who)}

    @app.post("/api/environments/{name}/build")
    async def env_build(name: str, body: BuildBody, who: str = A):
        job = await fleet.build(name, body.provider, force=body.force, actor=who, wait=False)
        return job.summary()

    @app.get("/api/environments/{name}/context")
    async def env_context(name: str, provider: str | None = None, _: str = A):
        from fastapi.responses import Response

        tag, tar = fleet.build_context(name, provider)
        return Response(tar, media_type="application/x-tar", headers={"X-Hangar-Image": tag})

    @app.post("/api/environments/{name}/images")
    async def env_register(name: str, body: RegisterImage, who: str = A):
        return await fleet.register_image(name, body.image, body.provider, actor=who)

    @app.get("/api/builds")
    async def builds(_: str = A):
        return [j.summary() for j in sorted(fleet.builds.values(), key=lambda j: j.started_at, reverse=True)]

    @app.post("/api/tickets")
    async def ticket(who: str = A):
        return {"ticket": h.auth.issue_ticket(who.removeprefix("ui:")), "ttl_seconds": 60}

    @app.get("/api/builds/{job_id}/stream")
    async def build_stream(job_id: str, request: Request, ticket: str | None = Query(None)):
        if not (h.auth.redeem_ticket(ticket) or h.auth.verify(bearer_from_headers(request.headers))):
            raise HTTPException(401)
        job = fleet.builds.get(job_id)
        if not job:
            raise HTTPException(404, "no such build")

        async def gen():
            async for line in job.follow():
                yield f"data: {json.dumps(line)}\n\n"
            yield f"event: done\ndata: {json.dumps(job.summary())}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ------------------------------------------------------------------ sandboxes
    @app.get("/api/sandboxes")
    async def sandboxes(include_stopped: bool = False, label: str | None = None, _: str = A):
        return [sbd(s) for s in fleet.list(include_stopped, label)]

    @app.post("/api/sandboxes")
    async def create_sb(body: CreateSandbox, who: str = A):
        kw = body.model_dump(exclude={"count"})
        if body.count <= 1:
            return [sbd(await fleet.create(**kw, actor=who))]

        async def one(i: int):
            k = dict(kw)
            k["name"] = f"{body.name or body.environment}-{i + 1}"
            if body.work_branch:
                k["work_branch"] = f"{body.work_branch}-{i + 1}"
            return await fleet.create(**k, actor=who)

        res = await asyncio.gather(*(one(i) for i in range(min(body.count, 20))), return_exceptions=True)
        return [sbd(r) if isinstance(r, Sandbox) else {"error": str(r)} for r in res]

    @app.get("/api/sandboxes/{ref}")
    async def get_sb(ref: str, _: str = A):
        return sbd(fleet.get(ref))

    @app.delete("/api/sandboxes/{ref}")
    async def del_sb(ref: str, who: str = A):
        return sbd(await fleet.destroy(ref, actor=who))

    @app.post("/api/sandboxes/{ref}/pause")
    async def pause_sb(ref: str, who: str = A):
        return sbd(await fleet.pause(ref, actor=who))

    @app.post("/api/sandboxes/{ref}/resume")
    async def resume_sb(ref: str, who: str = A):
        return sbd(await fleet.resume(ref, actor=who))

    @app.post("/api/sandboxes/{ref}/extend")
    async def extend_sb(ref: str, minutes: int = 60, who: str = A):
        return sbd(await fleet.extend(ref, minutes, actor=who))

    @app.post("/api/sandboxes/{ref}/exec")
    async def exec_sb(ref: str, body: ExecBody, who: str = A):
        return (await fleet.exec(ref, body.command, body.cwd, body.timeout, actor=who)).model_dump()

    @app.get("/api/sandboxes/{ref}/terminals")
    async def terms(ref: str, _: str = A):
        return await fleet.terminal_list(ref)

    @app.post("/api/sandboxes/{ref}/agent")
    async def agent(ref: str, body: AgentBody, who: str = A):
        return await fleet.run_agent(ref, body.prompt, body.harness, body.cwd, model=body.model, actor=who)

    @app.get("/api/sandboxes/{ref}/runs/{run_id}")
    async def agent_st(ref: str, run_id: str, _: str = A):
        return await fleet.agent_status(ref, run_id)

    @app.get("/api/sandboxes/{ref}/files")
    async def files(ref: str, path: str = ".", depth: int = 1, _: str = A):
        return {"listing": await fleet.list_files(ref, path, depth)}

    @app.post("/api/sandboxes/{ref}/snapshot")
    async def snap(ref: str, body: SnapshotBody, who: str = A):
        return (await fleet.snapshot(ref, body.label, body.kind, actor=who)).model_dump(mode="json")

    @app.post("/api/sandboxes/{ref}/fork")
    async def fork(ref: str, body: ForkBody, who: str = A):
        sbs = await fleet.fork(ref, body.count, body.name_prefix, body.credentials, body.work_branch_prefix,
                               body.provider, body.ttl_minutes, actor=who)
        return [sbd(s) for s in sbs]

    @app.websocket("/api/sandboxes/{ref}/attach")
    async def attach(ws: WebSocket, ref: str, ticket: str = Query(""), session: str | None = Query(None),
                     cols: int = Query(120), rows: int = Query(32)):
        who = h.auth.redeem_ticket(ticket) or h.auth.verify(bearer_from_headers(ws.headers))
        if not who:
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            term = await fleet.attach(ref, session, cols, rows)
        except Exception as e:  # noqa: BLE001
            await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
            await ws.close()
            return
        fleet.audit(f"ui:{who}", "sandbox.attach", ref, session=session)

        async def pump_out():
            while True:
                chunk = await term.read()
                if not chunk:
                    break
                await ws.send_bytes(chunk)
            with contextlib.suppress(Exception):
                await ws.close()

        out_task = asyncio.create_task(pump_out())
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                fleet.touch(ref)  # someone is using this terminal: keep its session awake
                if msg.get("bytes") is not None:
                    await term.write(msg["bytes"])
                elif msg.get("text") is not None:
                    try:
                        ctl = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        await term.write(msg["text"].encode())
                        continue
                    if ctl.get("type") == "resize":
                        await term.resize(int(ctl["cols"]), int(ctl["rows"]))
                    elif ctl.get("type") == "input":
                        await term.write(ctl["data"].encode())
        except WebSocketDisconnect:
            pass
        finally:
            out_task.cancel()
            await term.close()
            with contextlib.suppress(Exception):
                fleet.detach(ref)

    # ------------------------------------------------------------------ snapshots
    @app.get("/api/snapshots")
    async def snaps(sandbox: str | None = None, _: str = A):
        return [s.model_dump(mode="json") for s in fleet.snapshots(sandbox)]

    @app.delete("/api/snapshots/{snap_id}")
    async def del_snap(snap_id: str, who: str = A):
        await fleet.delete_snapshot(snap_id, actor=who)
        return {"deleted": snap_id}

    @app.post("/api/snapshots/{snap_id}/fork")
    async def fork_snap(snap_id: str, body: ForkBody, who: str = A):
        sbs = await fleet.fork(snap_id, body.count, body.name_prefix, body.credentials, body.work_branch_prefix,
                               body.provider, body.ttl_minutes, actor=who)
        return [sbd(s) for s in sbs]

    # ------------------------------------------------------------------ credentials
    @app.get("/api/harnesses")
    async def harnesses(_: str = A):
        return [
            {"name": x.name, "title": x.title, "agent": x.agent, "version": x.version or "latest",
             "deprecated": x.deprecated, "pack": f"{PACK_ROOT}/{x.name}" if x.install else None,
             "credential_kinds": [{"kind": s.kind.value, "env_var": s.env_var, "file_path": s.file_path,
                                   "exclusive": s.exclusive} for s in x.credentials],
             "logins": [{"command": lg.command, "captures": lg.captures.value, "notes": lg.notes} for lg in x.logins]}
            for x in HARNESSES.values()
        ]

    @app.get("/api/credentials")
    async def creds(_: str = A):
        return [c.model_dump(mode="json") for c in h.broker.list()]

    @app.post("/api/credentials")
    async def put_cred(body: CredentialBody, who: str = A):
        c = h.broker.put(body.name, body.harness, body.kind, body.value, body.description, body.env_var,
                         body.file_path)
        fleet.audit(who, "credential.put", c.name, harness=c.harness, kind=c.kind.value)
        return c.model_dump(mode="json")

    @app.delete("/api/credentials/{name}")
    async def del_cred(name: str, who: str = A):
        h.broker.delete(name)
        fleet.audit(who, "credential.delete", name)
        return {"deleted": name}

    @app.post("/api/credentials/{name}/release")
    async def release_cred(name: str, who: str = A):
        fleet.audit(who, "credential.force_release", name)
        return h.broker.force_release(name).model_dump(mode="json")

    @app.post("/api/logins")
    async def start_login(body: LoginBody, who: str = A):
        return await fleet.start_login(body.harness, body.credential, body.environment, body.provider, actor=who)

    @app.post("/api/logins/{login_id}/complete")
    async def complete_login(login_id: str, who: str = A):
        return (await fleet.complete_login(login_id, actor=who)).model_dump(mode="json")

    # ------------------------------------------------------------------ tokens / audit
    @app.get("/api/tokens")
    async def tokens(_: str = A):
        return [t.model_dump(mode="json", exclude={"token_hash"}) for t in h.auth.list()]

    @app.post("/api/tokens")
    async def new_token(body: TokenBody, who: str = A):
        bad = [x for x in body.scopes if x not in SCOPES]
        if bad or not body.scopes:
            raise HTTPException(400, f"unknown scopes {bad}; choose from {list(SCOPES)}")
        if body.callback_secret:
            cred = h.store.get(Credential, body.callback_secret)
            if cred is None or cred.harness != "webhook":
                raise HTTPException(400, f"callback_secret '{body.callback_secret}' must name a 'webhook' credential")
        try:
            raw, tok = h.auth.create(body.name, body.scopes, callback_secret=body.callback_secret)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        fleet.audit(who, "token.create", tok.id, name=body.name, scopes=body.scopes)
        return {"token": raw, **tok.model_dump(mode="json", exclude={"token_hash"})}

    @app.delete("/api/tokens/{token_id}")
    async def del_token(token_id: str, who: str = A):
        h.auth.revoke(token_id)
        fleet.audit(who, "token.revoke", token_id)
        return {"revoked": token_id}

    @app.post("/api/gc")
    async def gc(delete: bool = False, who: str = A):
        return await fleet.gc(delete=delete, actor=who)

    # ------------------------------------------------------------------ jobs (spec 0009)
    jobs = h.jobs

    @app.post("/api/jobs")
    async def job_submit(body: SubmitJob, caller: Caller = J):
        return jobs.view(jobs.submit(body, caller, source="api"))

    @app.get("/api/jobs")
    async def job_list(state: str | None = None, limit: int = 50, caller: Caller = J):
        return [jobs.view(j) for j in jobs.list(state=state, owner=owner_filter(caller), limit=limit)]

    @app.get("/api/jobs/{job_id}")
    async def job_get(job_id: str, caller: Caller = J):
        return jobs.view(jobs.get(job_id, owner=owner_filter(caller)))

    @app.post("/api/jobs/{job_id}/follow-up")
    async def job_follow_up(job_id: str, body: FollowUpBody, caller: Caller = J):
        return jobs.view(jobs.follow_up(job_id, body.message, requester=caller.requester,
                                        owner=owner_filter(caller)))

    @app.post("/api/jobs/{job_id}/cancel")
    async def job_cancel(job_id: str, caller: Caller = J):
        return jobs.view(jobs.cancel(job_id, requester=caller.requester, owner=owner_filter(caller)))

    @app.post("/api/hooks/github")
    async def github_hook(request: Request):
        name = h.settings.github_webhook_secret_credential
        if not name:
            raise HTTPException(404, "GitHub webhook is not configured (HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL)")
        body = await request.body()
        if not verify_signature(h.broker.secret_text(name), body, request.headers.get("x-hub-signature-256")):
            raise HTTPException(401, "bad signature")
        try:
            payload = json.loads(body)
        except ValueError as e:
            raise HTTPException(400, "body is not JSON") from e
        res = await handle_github(jobs, request.headers.get("x-github-event", ""),
                                  request.headers.get("x-github-delivery"), payload)
        return JSONResponse(res, status_code=202 if res.get("ok") else 200)

    # ------------------------------------------------------------------ sessions (spec 0010)
    sessions = h.sessions

    @app.post("/api/sessions")
    async def session_new(body: NewSession, who: str = A):
        return sessions.view(sessions.new(body, owner=who, requester=who))

    @app.get("/api/sessions")
    async def session_list(include_archived: bool = False, limit: int = 100, _: str = A):
        return [sessions.view(s) for s in sessions.list(include_archived=include_archived, limit=limit)]

    @app.get("/api/sessions/{ref}")
    async def session_get(ref: str, _: str = A):
        return sessions.view(sessions.get(ref))

    @app.post("/api/sessions/{ref}/open")
    async def session_open(ref: str, body: OpenBody | None = None, who: str = A):
        return sessions.view(sessions.open(ref, requester=who, harness=body.harness if body else None))

    @app.post("/api/sessions/{ref}/send")
    async def session_send(ref: str, body: SendBody, who: str = A):
        return sessions.view(sessions.send(ref, body.message, requester=who))

    @app.post("/api/sessions/{ref}/suspend")
    async def session_suspend(ref: str, who: str = A):
        return sessions.view(sessions.suspend(ref, requester=who))

    @app.post("/api/sessions/{ref}/checkpoint")
    async def session_checkpoint(ref: str, who: str = A):
        ck = await sessions.checkpoint_now(ref, requester=who)
        return {"checkpoint": ck.model_dump(mode="json") if ck else None}

    @app.post("/api/sessions/{ref}/archive")
    async def session_archive(ref: str, who: str = A):
        return sessions.view(sessions.archive(ref, requester=who))

    @app.delete("/api/sessions/{ref}")
    async def session_delete(ref: str, who: str = A):
        sessions.delete(ref, requester=who)
        return {"deleted": ref}

    @app.get("/api/audit")
    async def audit(limit: int = 200, _: str = A):
        return [e.model_dump(mode="json") for e in h.store.audit_tail(limit)]

    # ------------------------------------------------------------------ dashboard
    ui_dir = resources.files("hangar") / "ui"

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return FileResponse(str(ui_dir / "index.html"))

    app.mount("/ui", StaticFiles(directory=str(ui_dir)), name="ui")
    return app
