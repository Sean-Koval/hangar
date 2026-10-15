"""What the agent's environment tools (`hangar-agent env ...`, envrequests.py) ask of egress (--- egress).

The shapes were agreed with the computer workstream, which files requests and runs the one decision path
(`envrequests.decide`): the agent asks for a host with `hangar-agent env request egress <host> --why ...`, the
session's or job's owner (or an admin) approves or denies it, and only then is `on_decided` called. Requests are
never granted automatically. An approval adds the host to the session's allowlist, or, when an admin or the
environment's owner widened it, the environment's.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from hangar import egress
from hangar.models import Job, Sandbox

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet

log = logging.getLogger("hangar.egress")


class EgressRequest(BaseModel):  # what `hangar-agent env request egress <host> --why` files
    id: str  # "rq-<12 hex>" (shared id space with secret requests)
    environment: str
    host: str  # lowercased hostname or IP, no scheme/path
    port: int | None = None
    why: str  # agent text, redacted, <= 2000 chars
    requester: str  # "session:<id>" | "job:<id>"
    owner: str | None  # people.py person id of the session/job owner
    state: Literal["pending", "approved", "denied"] = "pending"
    scope: Literal["session", "environment"] = "session"
    created_at: float
    decided_by: str | None = None
    decided_at: float | None = None
    note: str | None = None


class Verdict(BaseModel):
    allowed: bool | None  # None = no policy enforced (mode open)
    mode: str  # "open" | "allowlist" | "none"
    rule: str | None = None  # the allowlist entry that matched, for the agent to read


def _settings(settings: Any = None, fleet: Fleet | None = None) -> Any:
    """The caller's settings (integration: never the process-wide cached ones when the caller has its own)."""
    if settings is not None:
        return settings
    if fleet is not None:
        return fleet.settings
    from hangar.config import get_settings

    return get_settings()


def policy(spec: EnvSpec, *, sandbox: Sandbox | None = None, fleet: Fleet | None = None,
           settings: Any = None) -> dict[str, Any]:
    """What a sandbox may know about its egress policy (no secrets): {mode, allow, proxy, enforced}. With the
    sandbox (and its fleet), approvals for its session and its repo's policy count too."""
    if sandbox is not None and fleet is not None:
        pol = egress.for_sandbox(fleet, sandbox, spec)
    else:
        pol = egress.resolve(_settings(settings), spec, harness=(sandbox.info.get("harness") if sandbox else None))
    proxy = ((sandbox.info.get("egress") or {}).get("proxy") if sandbox is not None else None)
    return {"mode": pol.mode, "allow": [r.text for r in pol.rules], "proxy": f"http://{proxy}" if proxy else None,
            "enforced": pol.enforced, "notes": list(pol.notes),
            "mirrors": list(pol.mirrors)}  # --- egress2: ecosystems read through hangar's package mirror


def check(spec: EnvSpec, host: str, port: int | None = None, *, sandbox: Sandbox | None = None,
          fleet: Fleet | None = None, settings: Any = None) -> Verdict:
    """Would the proxy let this sandbox (or one of this environment) reach host:port? `allowed` is None when the
    mode is open (nothing to check)."""
    try:
        h = egress.normalize_host(host)
    except egress.EgressError:
        return Verdict(allowed=False, mode=egress.env_mode(_settings(settings, fleet), spec), rule=None)
    if sandbox is not None and fleet is not None:
        pol = egress.for_sandbox(fleet, sandbox, spec)
    else:
        pol = egress.resolve(_settings(settings), spec, harness=(sandbox.info.get("harness") if sandbox else None))
    if pol.mode == "open":
        return Verdict(allowed=None, mode="open")
    rule = pol.allows(h, port or 443)
    return Verdict(allowed=rule is not None, mode=pol.mode, rule=rule.text if rule else None)


def may_widen(actor: Any, spec: EnvSpec | None) -> bool:
    """Whether `actor` (an auth Principal, or None for in-process callers, as the agreed interface says) may approve
    a host for the whole environment rather than one session: an admin, by role or token. Decided on the argument,
    not the context (review). Environments record no owner yet."""
    return actor is None or getattr(actor, "role", None) == "admin" or bool(getattr(actor, "admin", False))


def on_decided(req: EgressRequest, fleet: Fleet) -> str | None:
    """Apply an owner's decision: an approval adds the host to the session's (or environment's) allowlist, audited,
    and live sandboxes pick it up on their next connection. Returns the message for the agent."""
    if req.state != "approved":
        return f"egress to {req.host} was denied" + (f": {req.note}" if req.note else "")
    if req.scope == "environment":
        key = req.environment
    else:
        kind, _, rid = req.requester.partition(":")
        key = rid if kind == "session" else _session_of_job(fleet, rid)
        if not key:
            log.warning("egress request %s: no session for %s; not applied", req.id, req.requester)
            return f"egress to {req.host} was approved, but its session is gone; ask again from the new session"
    try:
        rule = egress.approve(fleet.store, req.scope, key, req.host, req.port, req.decided_by or "?", request=req.id)
    except egress.EgressError as e:
        return f"egress to {req.host} was approved but can't be applied: {e}"
    fleet.audit(req.decided_by or "system", "egress.approve", req.id, scope=req.scope, key=key, rule=rule.text,
                environment=req.environment, requester=req.requester, owner=req.owner)
    fleet.egress.invalidate()
    where = "this session" if req.scope == "session" else f"environment {req.environment}"
    return f"egress to {rule.text} is now allowed for {where}"


def _session_of_job(fleet: Fleet, job_id: str) -> str | None:
    """A job runs in a session; approvals for it last as long as that session (follow-ups included)."""
    job = fleet.store.get(Job, job_id)
    return job.session_id if job is not None else None
