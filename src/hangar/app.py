"""ASGI application: REST API + websocket terminals + dashboard + MCP endpoint, in one process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from importlib import resources
from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import hangar.identity as identity  # --- identity
import hangar.identity_api as identity_api  # --- identity
from hangar import drain as drain  # --- reconciler
from hangar import envbuilds as envbuilds  # --- envbuilds
from hangar import exchange, observe  # --- observe
from hangar import ghprojects as ghprojects  # --- intake: registers the GitHub Projects write-back
from hangar import modelpolicy as modelpolicy  # --- canary
from hangar import people as people  # --- teamcreds
from hangar import redeliver as redeliver  # --- reconciler
from hangar import teamcreds as teamcreds  # --- teamcreds
from hangar.a2a import A2AServer
from hangar.auth import SCOPES, TokenAuth, bearer_from_headers
from hangar.broker import Broker, CredentialError
from hangar.config import Settings, get_settings
from hangar.envspec import EnvCatalog
from hangar.fleet import Fleet, FleetError
from hangar.github import verify_signature
from hangar.harnesses import HARNESSES, PACK_ROOT
from hangar.jira import JiraFrontDoor  # --- intake
from hangar.jobs import Caller, JobService, SubmitJob, handle_github
from hangar.mcp_server import build_mcp, current_actor
from hangar.models import Budget, ContextItem, Credential, CredentialKind, Resources, Sandbox
from hangar.providers.base import ProviderError
from hangar.runtime import FastJSON, install_io_executor  # perf
from hangar.sessions import NewSession, SessionService
from hangar.slack import SlackFrontDoor  # workstream B
from hangar.store import Store
from hangar.strategies import SessionForkBody, fork_session  # workstream D: forks/best-of-N
from hangar.streams import credential as stream_credential  # --- stream
from hangar.streams import token_check
from hangar.triggers import ScheduleSpec, ScheduleUpdate, Triggers
from hangar.vault import Vault

log = logging.getLogger("hangar")


class Hangar:
    """Wires the components together; shared by the server and the CLI."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.store = Store(self.settings.db_path)
        # --- workstream setup: settings stored in the database (`hangar config`), under the environment's
        from hangar.settings_store import SettingsStore

        self.config = SettingsStore(self.store, self.settings)
        self.config.apply()
        from hangar import keys  # --- durable: a key ring (newest first), and its rotation

        self.vault = Vault(self.store, keys.load(self.settings, self.store))
        self.broker = Broker(self.store, self.vault, self.settings.sandbox_user)
        self.catalog = EnvCatalog(self.settings.env_dirs, save_dir=self.settings.saved_env_dir)
        self.fleet = Fleet(self.settings, self.store, self.broker, self.catalog)
        teamcreds.migrate(self.store, self.settings, self.fleet.audit)  # --- teamcreds: before people: the owner's
        self.auth = TokenAuth(self.store, self.settings.admin_token)
        self.sessions = SessionService(self.fleet, self.vault)
        self.jobs = JobService(self.fleet, self.sessions)
        from hangar.envrequests import EnvRequests  # --- computer: what agents ask their owners for

        self.env_requests = EnvRequests(self.fleet, self.sessions, self.jobs)
        self.triggers = Triggers(self.jobs)  # workstream A: approvals, inbound webhooks, schedules
        from hangar.backup import Backups, backup_store  # --- overnight/sweep: database backups

        self.backups = Backups(self.settings, self.vault, backup_store(self.settings, self.sessions.blobs))
        # --- durable: rotating the vault key over the vault, the database's other sealed rows and the stores
        from hangar import steer

        stores = [(self.sessions.blobs, False)] + ([(self.backups.blobs, True)]
                                                    if self.backups.blobs is not self.sessions.blobs else [])
        self.rotation = keys.Rotation(self.settings, self.store, self.vault.ring, stores,
                                      lambda: self.store.list_active(Sandbox), self.fleet.audit)
        self.rotation.resealers.append(steer.reseal)
        from hangar.retention import Retention  # --- scale: settled sandbox records move to the archive

        self.retention = Retention(self.store, self.settings, self.jobs, self.sessions)  # --- durable: + their memory
        from hangar.opstatus import Ops  # --- clouddoctor: SLO samples, record pruning, the nightly pass

        self.ops = Ops(self)


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
        # --- identity: the caller's principal (identity and scopes), not only its name; `mcp` tokens (hangar connect)
        who = self.auth.principal(token)
        if who is not None and not (who.admin or "mcp" in who.scopes or "user" in who.scopes):
            who = None
        if who is None and self.oauth is not None:
            who = await self.oauth.principal(token)
        actor = identity.actor_name(who) if who else None
        if actor is None:
            challenge = 'Bearer realm="hangar"'
            if self.resource_metadata_url:  # MCP auth discovery (RFC 9728)
                challenge += f', resource_metadata="{self.resource_metadata_url}"'
            resp = JSONResponse({"error": "unauthorized", "hint": "send Authorization: Bearer <hangar token>"},
                                status_code=401, headers={"WWW-Authenticate": challenge})
            return await resp(scope, receive, send)
        tok = current_actor.set(actor)
        ptok = identity.current.set(who)  # --- identity
        # --- stream: streams this request opens re-check the token while they stay open (revocation closes them)
        cred = stream_credential.set(token_check(self.auth, self.oauth, token, ("mcp", "user")))  # --- identity
        try:
            await self.app(scope, receive, send)
        finally:
            stream_credential.reset(cred)
            identity.current.reset(ptok)  # --- identity
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
    exposure: str | None = None  # --- identity: sandbox | control_plane (the broker checks it)
    person: str | None = None  # --- identity: whose own credential this is (admins; a member's is always theirs)
    team: bool = False  # --- teamcreds: a team credential (admins); an admin's without either is their own


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
    context: list[ContextItem] = Field(default_factory=list)  # workstream E
    output_schema: dict[str, Any] | None = None


class SendBody(BaseModel):
    message: str
    context: list[ContextItem] = Field(default_factory=list)  # workstream E
    output_schema: dict[str, Any] | None = None
    model: str | None = Field(None, max_length=200)  # --- overnight/cache: switch model (warned: a cold cache)


class OpenBody(BaseModel):
    harness: str | None = None


class IdleBody(BaseModel):
    idle_minutes: int = Field(ge=0, le=10080)  # 0 = stay up until suspended by hand


class TokenBody(BaseModel):
    name: str
    scopes: list[str] = Field(default_factory=lambda: ["admin"])
    callback_secret: str | None = None  # jobs tokens: vault `webhook` credential that signs their callbacks
    max_budget: Budget | None = None  # jobs tokens: the most a job's per-turn budget may allow (workstream C)
    person: str | None = None  # --- identity: whose token (admins may name anyone; default: the caller)


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
    from hangar.backup import apply_pending  # --- overnight/sweep: a staged restore, before the database opens

    settings = settings or get_settings()
    apply_pending(settings)
    h = Hangar(settings)
    # --- workstream mcp-modern: legacy MCP clients keep a session unless HANGAR_MCP_STATELESS_HTTP (docs/mcp.md)
    stateless = h.settings.mcp_stateless_http
    mcp = build_mcp(h.fleet, h.jobs, h.sessions, triggers=h.triggers, legacy_sessions=not stateless)
    mcp_asgi = mcp.streamable_http_app(streamable_http_path="/", stateless_http=stateless, host="0.0.0.0")
    a2a = A2AServer(h.jobs, h.auth, h.vault, h.settings) if h.settings.a2a_enabled else None  # workstream G

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
        install_io_executor(h.settings)  # perf: before anything starts blocking provider calls
        await observe.start(h.settings, redact=h.broker.redact)  # --- observe: OTEL_* traces, /metrics
        await h.fleet.start()
        envbuilds.of(h.fleet).recover()  # --- envbuilds: builds a restart cut short
        await h.fleet.gateway.start()  # workstream F: the git gateway's own listener (HANGAR_GATEWAY_PORT)
        h.fleet.egress.activity = h.sessions.activity  # --- egress: denied connections show in the session
        await h.sessions.start()  # first: jobs look up their sessions
        await h.jobs.start()
        await h.triggers.start()  # after jobs: it expires approvals and submits scheduled jobs
        if a2a:
            await a2a.start()
        pre = h.settings.prebuild_environments
        if pre is None:
            pre = [e.strip() for v in h.settings.github_repos.values() for e in v.split(",") if e.strip()]
        prebuild = asyncio.create_task(h.fleet.prebuild(pre)) if pre else None
        try:
            h.fleet.refresh_stale_packs()  # background builds; cancelled by fleet.stop()
        except Exception:  # noqa: BLE001 - never block startup on this
            log.warning("could not check for stale harness packs", exc_info=True)
        # --- overnight/sweep: long-polls answer on SIGTERM, before uvicorn drains them (shutdown.py); backups
        from hangar import shutdown

        restore_signals = shutdown.on_signal(asyncio.get_running_loop(), h.fleet.closing.set)
        h.backups.start()
        h.rotation.resume()  # --- durable: a key rotation a restart interrupted carries on
        h.retention.start()  # --- scale
        h.ops.start()  # --- clouddoctor
        modelpolicy.of(h.fleet).start()  # --- canary: providers' model lists, fetched now and kept fresh
        missed = redeliver.start(h)  # --- reconciler: GitHub deliveries that failed while hangar was down
        async with mcp.session_manager.run():
            yield
        h.fleet.closing.set()
        restore_signals()
        missed.cancel()  # --- reconciler
        await h.backups.stop()
        await h.rotation.stop()  # --- durable
        await h.retention.stop()  # --- scale
        await h.ops.stop()  # --- clouddoctor
        await modelpolicy.of(h.fleet).stop()  # --- canary
        if prebuild:
            prebuild.cancel()
        await h.triggers.stop()
        if a2a:
            await a2a.stop()
        await h.jobs.stop()
        await h.sessions.stop()
        await h.fleet.gateway.stop()  # workstream F
        if e2b := getattr(app.state, "e2b", None):  # --- e2b-api
            await e2b.close()
        await h.fleet.stop()
        await observe.stop()  # --- observe: flush spans

    app = FastAPI(title="hangar", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.hangar = h
    oauth, rm_url = _setup_oauth(app, h)
    app.state.oauth = oauth
    app.mount("/mcp", MCPAuthMiddleware(mcp_asgi, h.auth, oauth, rm_url))
    app.add_middleware(MCPPathMiddleware)
    # --- front-door-auth: body caps before anything parses a body, and an audit entry for refused requests
    from hangar.frontdoor import FrontDoorMiddleware, RateLimited

    app.add_middleware(FrontDoorMiddleware, hangar=h, oauth=oauth)
    app.add_middleware(observe.HTTPMiddleware)  # --- observe: outermost, so refused requests are seen too

    @app.exception_handler(RateLimited)
    async def _rate_limited(_: Request, exc: RateLimited):
        return JSONResponse({"error": str(exc)}, status_code=429,
                            headers={"Retry-After": str(max(1, math.ceil(exc.retry_after)))})

    from hangar.fairness import CapReached  # --- scale: a person at one of their caps

    @app.exception_handler(CapReached)
    async def _cap_reached(_: Request, exc: CapReached):
        return JSONResponse({"error": str(exc)}, status_code=429)
    # --- workstream F: with HANGAR_GATEWAY_PORT=0 the git gateway shares the main port (grant tokens only).
    if h.settings.gateway_url and not h.settings.gateway_port:
        for i, r in enumerate(h.fleet.gateway.routes()):
            app.router.routes.insert(i, r)
    if a2a:
        a2a.mount(app)  # /.well-known/agent-card.json and JSON-RPC at /a2a (docs/a2a.md)
    # --- e2b-api: the E2B-compatible API at HANGAR_E2B_API_PATH, and sandbox URLs in front of everything (docs/e2b.md)
    if e2b_path := h.settings.e2b_api_path.rstrip("/"):
        from hangar.e2b_api import E2BService, build_api
        from hangar.sandbox_router import SandboxRouter

        app.state.e2b = E2BService(h)
        h.rotation.resealers.append(app.state.e2b.reseal)  # --- durable: its sandboxes' envVars are sealed
        app.mount(e2b_path, build_api(app.state.e2b))
        app.add_middleware(SandboxRouter, svc=app.state.e2b)  # outermost: sandbox traffic skips the body caps
    if h.settings.preview_domain:  # --- envbuilds: preview ports, for each sandbox's owner (preview.py)
        from hangar.preview import PreviewRouter

        app.add_middleware(PreviewRouter, h=h)

    @app.exception_handler(identity.Forbidden)  # --- identity
    async def _forbidden(_: Request, exc: identity.Forbidden):
        return JSONResponse({"error": str(exc)}, status_code=403)

    @app.exception_handler(FleetError)
    @app.exception_handler(CredentialError)
    @app.exception_handler(ProviderError)
    @app.exception_handler(KeyError)
    @app.exception_handler(ValueError)
    async def _domain_errors(_: Request, exc: Exception):
        status = 404 if isinstance(exc, KeyError) or "no sandbox" in str(exc) else 400
        return JSONResponse({"error": str(exc).strip("'\"")}, status_code=status)

    async def actor(request: Request) -> str:  # perf: async = no thread hop
        p = h.auth.principal(bearer_from_headers(request.headers))
        if p is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if not p.admin:
            raise HTTPException(403, f"token scopes {p.scopes} do not allow this route")
        identity.current.set(p)  # --- identity: shared code (environment saves) sees the REST caller too
        return f"ui:{p.name}"

    async def person_actor(request: Request) -> str:  # --- identity: an admin, or a person with their `user` token
        p = h.auth.principal(bearer_from_headers(request.headers))
        if p is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if not (p.admin or "user" in p.scopes):
            raise HTTPException(403, f"token scopes {p.scopes} do not allow this route")
        identity.current.set(p)
        return f"ui:{p.name}"

    async def job_caller(request: Request) -> Caller:  # perf: async
        """Admin sees every job; a `jobs`-scoped token is restricted and sees only jobs it submitted."""
        p = h.auth.principal(bearer_from_headers(request.headers))
        if p is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if p.admin:
            return Caller(p.name, p.id, person=p.person)  # --- identity: person
        if "jobs" in p.scopes:
            return Caller(p.name, p.id, restricted=True, callback_secret=p.callback_secret,
                          max_budget=p.max_budget, person=p.person)
        raise HTTPException(403, f"token scopes {p.scopes} do not allow jobs")

    def owner_filter(c: Caller) -> str | None:
        return c.owner if c.restricted else None

    A = Depends(actor)
    U = Depends(person_actor)  # --- identity: routes a member's own `user` token reaches too
    J = Depends(job_caller)
    fleet = h.fleet

    def sbd(s: Sandbox) -> dict[str, Any]:
        return s.model_dump(mode="json")

    # ------------------------------------------------------------------ meta
    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/me")
    async def me(who: str = U):
        p = identity.principal()  # --- identity
        return {"actor": who, "person": p.person if p else None, "role": p.role if p else None}

    @app.get("/api/doctor")
    async def doctor_route(_: str = A):
        from hangar import doctor

        return await doctor.run(h, h.jobs.transport)  # --- workstream setup: tests inject GitHub/Slack fakes

    @app.get("/api/overview")
    async def overview(_: str = A):
        return fleet.overview()

    @app.get("/api/connect")
    async def connect(_: str = U):
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
            "a2a_agent_card": (h.settings.public_url.rstrip("/") + "/.well-known/agent-card.json") if a2a else None,
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
        return {"harness": harness,  # --- images: registering resolves the digest sandboxes mount
                "image": await fleet.register_pack(harness, body.image, body.provider, actor=who)}

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
        return FastJSON([sbd(s) for s in fleet.list(include_stopped, label)])  # perf: no jsonable_encoder

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
            try:
                out_task.cancel()
                with contextlib.suppress(Exception):
                    await term.close()
            finally:
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
             # --- images: the CLI version each provider's pack installed (the lock's is `version`)
             "installed": {p: v for p in fleet.providers if x.install and (v := fleet.harness_version(p, x.name))},
             "deprecated": x.deprecated, "pack": f"{PACK_ROOT}/{x.name}" if x.install else None,
             "credential_kinds": [{"kind": s.kind.value, "env_var": s.env_var, "file_path": s.file_path,
                                   "exclusive": s.exclusive} for s in x.credentials],
             "logins": [{"command": lg.command, "captures": lg.captures.value, "notes": lg.notes} for lg in x.logins]}
            for x in HARNESSES.values()
        ]

    @app.get("/api/credentials")
    async def creds(_: str = U):
        return [{**c.model_dump(mode="json"), "exposure": identity.exposure(c, h.settings),  # --- identity: in effect
                 "owner": teamcreds.owner(c, h.settings)}  # --- teamcreds
                for c in identity_api.credentials(h)]

    @app.post("/api/credentials")
    async def put_cred(body: CredentialBody, who: str = U):
        person = identity_api.credential_person(h, body.name, body.harness, body.kind, body.person, body.exposure,
                                                body.team)  # --- teamcreds
        c = h.broker.put(body.name, body.harness, body.kind, body.value, body.description, body.env_var,
                         body.file_path, exposure=body.exposure, person=person, team=body.team)  # --- identity
        fleet.audit(who, "credential.put", c.name, harness=c.harness, kind=c.kind.value, person=c.person)
        return {**c.model_dump(mode="json"), "owner": teamcreds.owner(c, h.settings)}  # --- teamcreds

    @app.delete("/api/credentials/{name}")
    async def del_cred(name: str, who: str = U):
        identity_api.check_credential_delete(h, name)  # --- identity: a member deletes only their own
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
        lg = fleet.logins.get(login_id) or {}  # --- teamcreds: a new login is its admin's own, not the team's
        new = h.store.get(Credential, lg.get("credential") or "") is None
        person = people.token_person(identity.person()) if new else None
        return (await fleet.complete_login(login_id, actor=who, person=person)).model_dump(mode="json")

    # ------------------------------------------------------------------ tokens / audit
    @app.get("/api/tokens")
    async def tokens(_: str = U):
        return [t.model_dump(mode="json", exclude={"token_hash"}) for t in identity_api.tokens(h)]  # --- identity

    @app.post("/api/tokens")
    async def new_token(body: TokenBody, who: str = U):
        bad = [x for x in body.scopes if x not in SCOPES]
        if bad or not body.scopes:
            raise HTTPException(400, f"unknown scopes {bad}; choose from {list(SCOPES)}")
        person = identity_api.token_person(h, body.person, body.scopes, body.callback_secret)  # --- identity
        if body.callback_secret:
            cred = h.store.get(Credential, body.callback_secret)
            if cred is None or cred.harness != "webhook":
                raise HTTPException(400, f"callback_secret '{body.callback_secret}' must name a 'webhook' credential")
        try:
            raw, tok = h.auth.create(body.name, body.scopes, callback_secret=body.callback_secret,
                                     max_budget=body.max_budget, person=person)  # --- identity
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        fleet.audit(who, "token.create", tok.id, name=body.name, scopes=body.scopes)
        return {"token": raw, **tok.model_dump(mode="json", exclude={"token_hash"})}

    @app.delete("/api/tokens/{token_id}")
    async def del_token(token_id: str, who: str = U):
        identity_api.check_token_revoke(h, token_id)  # --- identity: a member revokes only their own
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
    async def job_list(state: str | None = None, limit: int = 50, cursor: str | None = None, caller: Caller = J):
        # --- durable: a page, each job with its `cursor`; X-Next-Cursor when there are more
        page, nxt = jobs.page(state=state, owner=owner_filter(caller), limit=limit, person=people.scope(),
                              cursor=cursor)
        return FastJSON([{**jobs.view(j), "cursor": c} for j, c in page],
                        headers={"X-Next-Cursor": nxt} if nxt else None)

    @app.get("/api/jobs/{job_id}")
    async def job_get(job_id: str, caller: Caller = J):
        return jobs.view(jobs.get(job_id, owner=owner_filter(caller)))

    @app.post("/api/jobs/{job_id}/follow-up")
    async def job_follow_up(job_id: str, body: FollowUpBody, caller: Caller = J):
        return jobs.view(jobs.follow_up(job_id, body.message, requester=caller.requester,
                                        owner=owner_filter(caller), context=body.context,
                                        output_schema=body.output_schema))

    @app.post("/api/jobs/{job_id}/cancel")
    async def job_cancel(job_id: str, caller: Caller = J):
        return jobs.view(jobs.cancel(job_id, requester=caller.requester, owner=owner_filter(caller)))

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, since: int = 0, limit: int = 200, tail: int | None = None, caller: Caller = J):
        """Activity events (workstream C), oldest first. Poll with `since` = the last `seq` you saw."""
        evs = jobs.events(job_id, owner=owner_filter(caller), since=since, limit=limit, tail=tail)
        return {"events": evs, "next": evs[-1]["seq"] if evs else since}

    @app.post("/api/hooks/github")
    async def github_hook(request: Request):
        name = h.settings.github_webhook_secret_credential
        if not name:
            raise HTTPException(404, "GitHub webhook is not configured (HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL)")
        body = await request.body()
        try:  # front-door-auth: say nothing about the vault to an anonymous caller
            secret = h.broker.secret_text(name)
        except Exception as e:  # noqa: BLE001
            log.error("GitHub webhook secret credential %s is not usable", name)
            raise HTTPException(503, "GitHub webhook secret is not stored") from e
        if not verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
            raise HTTPException(401, "bad signature")
        try:
            payload = json.loads(body)
        except ValueError as e:
            raise HTTPException(400, "body is not JSON") from e
        # --- front-door-auth: GitHub signs only the body, so the same body is the same delivery, whatever its
        # X-GitHub-Delivery header says. A redelivery or a replay is acknowledged and does nothing.
        # Only the events that can start or steer work are remembered; replaying anything else does nothing.
        event = request.headers.get("x-github-event", "")
        replays, digest = jobs.admission.replays, jobs.admission.replays.digest(body)
        ttl = h.settings.webhook_replay_hours * 3600
        # --- reconciler: marked pending while handled and done after, so a pod that dies in between doesn't make
        # GitHub's redelivery look like a replay (redeliver.py)
        if event in ("issues", "issue_comment", "projects_v2_item") and \
                not redeliver.claim(replays, digest, ttl):  # --- intake: + project items
            delivery = (request.headers.get("x-github-delivery") or "")[:100]
            jobs.admission.refused("github:replay", "github", "hook.replay", delivery or None)
            return JSONResponse({"ignored": "duplicate delivery"}, status_code=200)
        try:
            res = await handle_github(jobs, event, request.headers.get("x-github-delivery"), payload)
        except BaseException:  # --- reconciler: a shutdown mid-handler too
            replays.forget("github", digest)  # nothing happened: let GitHub's redelivery try again
            raise
        if res.get("error") or res.get("rate_limited"):
            replays.forget("github", digest)  # a redelivery after the fix (or the wait) should run
            if res.get("rate_limited"):
                return JSONResponse(res, status_code=429)
        elif event in ("issues", "issue_comment", "projects_v2_item"):  # --- intake
            redeliver.done(replays, digest, ttl)  # --- reconciler: handled; from now on a replay
        return JSONResponse(res, status_code=202 if res.get("ok") else 200)

    # ------------------------------------------------------------------ triggers (workstream A)
    triggers = h.triggers

    def admin_caller(caller: Caller) -> Caller:
        if caller.restricted:
            raise HTTPException(403, "this needs an admin token")
        return caller

    @app.post("/api/jobs/{job_id}/approve")
    async def job_approve(job_id: str, caller: Caller = J):
        return jobs.view(triggers.approve(job_id, admin_caller(caller).requester))

    @app.post("/api/jobs/{job_id}/reject")
    async def job_reject(job_id: str, caller: Caller = J):
        return jobs.view(triggers.reject(job_id, admin_caller(caller).requester))

    @app.post("/api/hooks/inbound/{source}")
    async def inbound_hook(source: str, request: Request):
        limit = h.settings.inbound_max_body_bytes
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            return JSONResponse({"error": "body too large"}, status_code=413)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                return JSONResponse({"error": "body too large"}, status_code=413)
        res = triggers.handle_inbound(source, request.headers, body)
        return JSONResponse(res.body, status_code=res.status)

    @app.get("/api/schedules")
    async def schedule_list(_: str = A):
        return [triggers.view_schedule(s) for s in triggers.list_schedules()]

    @app.post("/api/schedules")
    async def schedule_create(body: ScheduleSpec, caller: Caller = J):
        c = admin_caller(caller)
        return triggers.view_schedule(triggers.create_schedule(body, owner=c.owner, requester=c.requester))

    @app.get("/api/schedules/{ref}")
    async def schedule_get(ref: str, _: str = A):
        return triggers.view_schedule(triggers.get_schedule(ref))

    @app.patch("/api/schedules/{ref}")
    async def schedule_update(ref: str, body: ScheduleUpdate, who: str = A):
        return triggers.view_schedule(triggers.update_schedule(ref, body, requester=who))

    @app.delete("/api/schedules/{ref}")
    async def schedule_delete(ref: str, who: str = A):
        return {"deleted": triggers.delete_schedule(ref, requester=who).id}

    @app.post("/api/schedules/{ref}/run")
    async def schedule_run(ref: str, who: str = A):
        return jobs.view(triggers.run_now(ref, requester=who))
    # --- workstream B: slack (/api/hooks/slack/commands and /api/hooks/slack/events; docs/slack.md)
    SlackFrontDoor(jobs).mount(app)
    JiraFrontDoor(jobs).mount(app)  # --- intake: /api/hooks/jira (docs/intake.md)

    # ------------------------------------------------------------------ sessions (spec 0010)
    sessions = h.sessions

    @app.post("/api/sessions")
    async def session_new(body: NewSession, who: str = A):
        return sessions.view(sessions.new(body, owner=who, requester=who))

    @app.get("/api/sessions")
    async def session_list(include_archived: bool = False, limit: int = 100, cursor: str | None = None,
                           _: str = A):
        page, nxt = sessions.page(include_archived=include_archived, limit=limit, cursor=cursor)  # durable
        return FastJSON([{**sessions.view(s), "cursor": c} for s, c in page],
                        headers={"X-Next-Cursor": nxt} if nxt else None)

    @app.get("/api/sessions/{ref}")
    async def session_get(ref: str, _: str = A):
        return sessions.view(sessions.get(ref))

    @app.get("/api/sessions/{ref}/events")
    async def session_events(ref: str, since: int = 0, limit: int = 200, tail: int | None = None, _: str = A):
        """Activity events (workstream C), oldest first. Poll with `since` = the last `seq` you saw."""
        evs = sessions.events(ref, since=since, limit=limit, tail=tail)
        return {"events": evs, "next": evs[-1]["seq"] if evs else since}

    @app.post("/api/sessions/{ref}/open")
    async def session_open(ref: str, body: OpenBody | None = None, who: str = A):
        return sessions.view(sessions.open(ref, requester=who, harness=body.harness if body else None))

    @app.post("/api/sessions/{ref}/send")
    async def session_send(ref: str, body: SendBody, who: str = A):
        return sessions.view(sessions.send(ref, body.message, requester=who, context=body.context,
                                           output_schema=body.output_schema, model=body.model))

    @app.post("/api/sessions/{ref}/idle")
    async def session_idle(ref: str, body: IdleBody, who: str = A):
        return sessions.view(sessions.set_idle(ref, body.idle_minutes, requester=who))

    @app.post("/api/sessions/{ref}/suspend")
    async def session_suspend(ref: str, who: str = A):
        return sessions.view(sessions.suspend(ref, requester=who))

    @app.post("/api/sessions/{ref}/checkpoint")
    async def session_checkpoint(ref: str, who: str = A):
        ck = await sessions.checkpoint_now(ref, requester=who)
        return {"checkpoint": ck.model_dump(mode="json") if ck else None}

    @app.post("/api/sessions/{ref}/fork")
    async def session_fork(ref: str, body: SessionForkBody, who: str = A):
        children = await fork_session(sessions, ref, body.n, requester=who, harness=body.harness,
                                      names=body.names, message=body.message)
        return {"parent": sessions.get(ref).name, "forks": [sessions.view(c) for c in children]}

    @app.post("/api/sessions/{ref}/archive")
    async def session_archive(ref: str, who: str = A):
        return sessions.view(sessions.archive(ref, requester=who))

    @app.delete("/api/sessions/{ref}")
    async def session_delete(ref: str, who: str = A):
        sessions.delete(ref, requester=who)
        return {"deleted": ref}

    # ------------------------------------------------------------------ workstream E: context exchange (spec 0011)
    from fastapi.responses import Response

    def _job_session(job_id: str, caller: Caller):
        s = jobs._session(jobs.get(job_id, owner=owner_filter(caller)))
        if s is None:
            raise KeyError(f"job {job_id} has not started yet")
        return s

    async def _artifact(s, turn: int, path: str) -> Response:
        a, data = await exchange.read_artifact(sessions, s, turn, path)
        media, headers = exchange.download_headers(a)
        return Response(data, media_type=media, headers=headers)

    @app.get("/api/sessions/{ref}/artifacts")
    async def session_artifacts(ref: str, _: str = A):
        return exchange.list_artifacts(sessions.get(ref))

    @app.get("/api/sessions/{ref}/artifacts/{turn}/{path:path}")
    async def session_artifact(ref: str, turn: int, path: str, _: str = A):
        return await _artifact(sessions.get(ref), turn, path)

    @app.get("/api/sessions/{ref}/patch")
    async def session_patch(ref: str, max_bytes: int | None = None, _: str = A):
        return await exchange.session_patch(sessions, sessions.get(ref), max_bytes)

    @app.get("/api/sessions/{ref}/pull")
    async def session_pull(ref: str, _: str = A):
        return exchange.pull_manifest(sessions.get(ref))

    @app.get("/api/sessions/{ref}/pull/{checkpoint}/{part}")
    async def session_pull_part(ref: str, checkpoint: str, part: str, _: str = A):
        data = await exchange.pull_part(sessions, sessions.get(ref), checkpoint, part)
        return Response(data, media_type="application/octet-stream")

    @app.get("/api/jobs/{job_id}/artifacts")
    async def job_artifacts(job_id: str, caller: Caller = J):
        return exchange.list_artifacts(_job_session(job_id, caller))

    @app.get("/api/jobs/{job_id}/artifacts/{turn}/{path:path}")
    async def job_artifact(job_id: str, turn: int, path: str, caller: Caller = J):
        return await _artifact(_job_session(job_id, caller), turn, path)

    @app.get("/api/jobs/{job_id}/patch")
    async def job_patch(job_id: str, max_bytes: int | None = None, caller: Caller = J):
        return await exchange.session_patch(sessions, _job_session(job_id, caller), max_bytes)

    # --- workstream steer: steer, interrupt and tool approvals for running turns (docs/steering.md)
    from hangar import steer_api

    steer_api.mount(app, h, A, J, _job_session)
    # --- stream: exec output, background processes, file watches and events as Server-Sent Events
    from hangar import stream_api

    stream_api.mount(app, h, A, J, owner_filter)

    # --- workstream setup: database-stored settings (hangar config) and the setup wizards (hangar setup)
    from hangar import settings_store, setup_api

    settings_store.mount(app, h, A)
    identity_api.mount(app, h, A)  # --- identity: people, credential exposure and owners
    from hangar import computer_api  # --- computer: the agent's screen, media links, env requests

    computer_api.mount(app, h, U)
    setup_api.mount(app, h, A)
    from hangar import repopolicy  # --- frontdoor: per-repo policy (hangar repo)

    repopolicy.mount(app, h, A)
    envbuilds.mount(app, h, A, U)  # --- envbuilds: repo builds (hangar env builds) and config checks
    from hangar import preview  # --- envbuilds: links to a sandbox's preview ports

    preview.mount(app, h, U)
    from hangar import inbox  # --- landing: GET /api/inbox, what needs the caller

    inbox.mount(app, h, U)
    from hangar import backup  # --- overnight/sweep: database backups (hangar admin)

    backup.mount(app, h, A)
    from hangar import keys  # --- durable: /api/admin/rotate-key

    keys.mount(app, h, A)
    drain.mount(app, h, A)  # --- reconciler: POST/GET/DELETE /api/admin/drain (power.sh down)
    from hangar import egress  # --- egress: what a sandbox may reach; approved hosts, and revoking them

    egress.mount(app, h, A)
    from hangar import cloudcreds  # --- egress: OIDC discovery and JWKS for sandboxes' cloud tokens

    cloudcreds.mount(app, h, A)
    from hangar import power  # --- ops: /api/admin/power (power.sh, `hangar power`) and /api/admin/deploys

    power.mount(app, h, A)
    from hangar import credhealth, flightrec, opstatus  # --- clouddoctor: status, flight records, credential health

    opstatus.mount(app, h, A)
    flightrec.mount(app, h, A, U)
    credhealth.mount(app, h, A)
    credhealth.register_inbox()
    from hangar import clouddoctor  # --- clouddoctor: the Cloud Doctor (docs/doctor.md)

    clouddoctor.mount(app, h, A)
    clouddoctor.register(h)
    # --- observe: GET /metrics (admin or `metrics` tokens) and GET /api/usage
    from hangar import metrics, usage

    metrics.mount(app, h)
    usage.mount(app, h, A)

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
