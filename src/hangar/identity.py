"""Who is calling, what their token may do, and which credentials may ever reach a sandbox.

* **The principal.** The REST and MCP auth paths put the caller's `Principal` (token identity and scopes) in a
  context variable, so a check deep in a tool or service sees the same caller the front door saw. Unset means an
  in-process caller (the job service, the scheduler, a test): every network entry point sets it, and the MCP app
  is mounted only behind `MCPAuthMiddleware`.
* **Scopes on capabilities.** `require("admin")` refuses a narrower token; `scoped` wraps a whole MCP tool. Saving
  an environment that sets one of `ADMIN_FIELDS` needs an admin principal.
* **Credential exposure.** Control-plane credentials (webhook signing keys, GitHub App keys, Slack bot and signing
  secrets, the control plane's own GitHub token) never reach a sandbox: not through `secrets:`, an attached
  credential's env var or file, or MCP config resolution, which only sees what those wrote (hardening B4, H3).
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from typing import TYPE_CHECKING, Any, Literal

from hangar.models import Credential, CredentialKind

if TYPE_CHECKING:
    from hangar.auth import Principal
    from hangar.config import Settings
    from hangar.store import Store

Exposure = Literal["sandbox", "control_plane"]

current: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("hangar_principal", default=None)

# Environment fields that widen what a sandbox reaches or weaken its isolation: vault secrets mapped into it, an
# arbitrary registry image (prebuilt or as the build's base), a runtime class (runc instead of gVisor), and the git
# credential mode (direct puts the git token in the sandbox instead of the gateway).
ADMIN_FIELDS = ("secrets", "image", "base", "runtime_class", "credential_mode",
                "egress", "build_secrets", "cloud")  # --- egress: reach, build secrets, cloud identity


class Forbidden(PermissionError):
    """The caller's token doesn't allow this. REST answers 403; MCP reports it to the client."""


def principal() -> Principal | None:
    return current.get()


def person() -> str | None:
    """The calling person's id: the stable key for ownership and per-person quotas (None: in-process)."""
    p = current.get()
    return p.person if p else None


def sees_all() -> bool:
    """True for admins (by role, whatever the token's scopes) and in-process callers: they see everyone's work."""
    p = current.get()
    return p is None or p.role == "admin"


def actor_name(p: Principal) -> str:
    """The MCP actor: what audit entries, task handles and stream caps are keyed by. Token names aren't unique, so
    a teammate's are prefixed with their person (`:` can't occur in a token name); the owner's stay as they were.
    The `@` keeps them apart from requesters such as `github:octocat`, so per-person caps (fairness.person_of)
    find the person in them (review)."""
    return p.name if p.person in (None, "owner") else f"@{p.person}:{p.name}"


def is_admin() -> bool:
    """True for admin tokens and for in-process callers (no principal set)."""
    p = current.get()
    return p is None or p.admin


def require(scope: str = "admin", what: str = "this") -> None:
    """Refuse the current caller unless its token holds `scope` (admin holds every scope)."""
    p = current.get()
    if p is None or p.admin or scope in p.scopes:
        return
    raise Forbidden(f"{what} needs a token with the '{scope}' scope; this one has {sorted(p.scopes)}")


def scoped(fn: Any, scope: str) -> Any:
    """`fn` (sync or async) refusing callers without `scope` before it runs."""
    what = f"{fn.__name__}"
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapped(*a, **kw):
            require(scope, what)
            return await fn(*a, **kw)
        return awrapped

    @functools.wraps(fn)
    def wrapped(*a, **kw):
        require(scope, what)
        return fn(*a, **kw)
    return wrapped


# ---------------------------------------------------------------------------- credential exposure
def _signing_names(settings: Settings) -> set[str]:
    """Credentials the settings name for verifying inbound requests or speaking for hangar: never in a sandbox."""
    names = {settings.github_webhook_secret_credential, settings.slack_signing_secret_credential,
             settings.slack_bot_token_credential}
    for cfg in (settings.inbound_webhooks or {}).values():
        if isinstance(cfg, dict) and isinstance(cfg.get("secret"), str):
            names.add(cfg["secret"])
    jira = getattr(settings, "jira", None) or {}  # --- intake: Jira's API token, webhook secret, Automation token
    for key in ("token_credential", "webhook_secret_credential", "automation_token_credential"):
        if isinstance(jira, dict) and isinstance(jira.get(key), str):
            names.add(jira[key])
    return {n for n in names if n}


def reserved_names(settings: Settings) -> set[str]:
    """Every credential the control plane names for its own use: the signing keys and bot tokens, and the GitHub
    tokens it calls GitHub with (control-plane unless an admin marks them `sandbox`)."""
    return _signing_names(settings) | {n for n in (settings.github_token_credential, settings.gateway_credential,
                                                   settings.github_app_credential) if n} \
        | {n for n in (settings.model_list_credentials or {}).values() if n}  # --- landing; --- canary: list keys


def locked(cred: Credential, settings: Settings | None) -> bool:
    """Control-plane whatever its `exposure` says: `webhook` credentials, GitHub App keys and the signing secrets
    and bot tokens the settings name."""
    if cred.harness == "webhook" or cred.kind == CredentialKind.GITHUB_APP:
        return True
    return settings is not None and cred.name in _signing_names(settings)


def exposure(cred: Credential, settings: Settings | None) -> Exposure:
    """Where a credential may go. An explicit `exposure` wins, except over the locked kinds."""
    if locked(cred, settings):
        return "control_plane"
    if cred.exposure:
        return cred.exposure
    if settings is not None and cred.name in reserved_names(settings):
        return "control_plane"
    return "sandbox"


def refusal(cred: Credential, settings: Settings | None) -> str:
    """Why `cred` can't go into a sandbox, and what to do instead."""
    msg = f"credential '{cred.name}' is control-plane only and never goes into a sandbox"
    if locked(cred, settings):
        return msg + " (a signing key, bot token or GitHub App key)"
    if cred.exposure == "control_plane":
        return msg + " (an admin marked it control_plane)"
    return (msg + " (hangar uses it for its own GitHub calls); give sandboxes a token of their own, or, to share "
            f"this one, an admin can run `hangar cred exposure {cred.name} sandbox`")


# ---------------------------------------------------------------------------- environments
def check_environment(data: dict[str, Any], store: Store, settings: Settings | None) -> None:
    """Refuse an environment save that a non-admin may not make, or that maps a control-plane credential."""
    if not is_admin():
        bad = [f for f in ADMIN_FIELDS if data.get(f) is not None and data[f] != {}  # "" is a runtime class (runc)
               and not (f == "credential_mode" and data[f] == "gateway")]
        if bad:
            raise Forbidden(f"setting {', '.join(bad)} on an environment needs an admin token; extend an environment "
                            "that already has what you need, or ask an admin")
    secrets = data.get("secrets")
    for var, name in (secrets.items() if isinstance(secrets, dict) else ()):
        cred = store.get(Credential, str(name)) if isinstance(name, str) else None
        if cred is not None and exposure(cred, settings) == "control_plane":
            raise Forbidden(f"secrets: {var}: {refusal(cred, settings)}")


# ---------------------------------------------------------------------------- doctor (review)
def doctor_check(catalog: Any, store: Store, settings: Settings) -> dict[str, Any] | None:
    """`hangar doctor`: environments whose sandboxes fail to start because they attach or map a control-plane
    credential (an upgrade from before exposure: a direct-mode environment listing hangar's own GitHub token).
    Gateway mode drops git credentials before they would be written, so those don't count there."""
    from hangar.gateway import GIT_KINDS

    creds = {c.name: c for c in store.list(Credential)}
    gateway_jobs = bool(settings.gateway_url and settings.gateway_for_jobs)
    fails: list[str] = []
    warns: list[str] = []
    names: set[str] = set()
    for env in catalog.names():
        try:
            spec = catalog.get(env)
        except Exception:  # noqa: BLE001 - a broken spec is reported where it is used
            continue
        for name in [*spec.credentials, *spec.secrets.values()]:
            c = creds.get(name)
            if c is None or exposure(c, settings) != "control_plane":
                continue
            mapped = name in spec.secrets.values()
            if not mapped and c.kind in GIT_KINDS and spec.credential_mode == "gateway":
                continue
            names.add(name)
            # with the gateway on for jobs, only sandboxes made directly (create_sandbox) from it fail
            gw = not mapped and c.kind in GIT_KINDS and spec.credential_mode is None and gateway_jobs
            (warns if gw else fails).append(f"{env}: {name}")
    if not fails and not warns:
        return {"name": "control-plane credentials", "status": "ok", "fix": None,
                "detail": "no environment attaches a credential that stays out of sandboxes"}
    shared = " or ".join(f"hangar cred exposure {n} sandbox" for n in sorted(names))
    return {"name": "control-plane credentials", "status": "fail" if fails else "warn",
            "detail": f"sandboxes from these environments fail to start, the credential is control-plane only: "
                      f"{', '.join(fails + warns)}",
            "fix": "list a git token of the sandboxes' own instead (hangar cred add NAME --harness git --kind "
                   f"git_token), use credential_mode: gateway, or share it with sandboxes on purpose: {shared}"}
