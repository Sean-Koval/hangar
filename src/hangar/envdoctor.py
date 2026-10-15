"""Agent self-diagnosis: what hangar tells a sandbox about its own environment (docs/computer-use.md).

Cloud agents' environment failures are silent: the output just gets weaker. So every sandbox carries
`~/.hangar/envstatus.json`, written at claim and at the start of each turn, and `hangar-env` (sandbox_env.py) reads
it and checks what it can from inside: the build version and whether it is a fallback, unhealthy services and
ports, secret *names* the environment expects that are missing, whether a host is reachable under the egress
policy, recent setup failures, and the requests the agent filed with its owner. The same report is a read-only MCP
server (`hangar-env`) rendered into the harness's config, so an agent can read it as resources.

The file holds names, versions and policy, never a secret value. What the build and the service supervisor
record about a sandbox (`sb.info["env_build"]`, `["services"]`, `["setup_failures"]`) is passed on as is; where
nothing is recorded, the doctor says "unknown" rather than guessing.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hangar import computer, egressapi
from hangar import heavy as heavy  # --- heavy
from hangar.models import now

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.envspec import EnvSpec
    from hangar.harnesses import Harness
    from hangar.models import Sandbox, Session

log = logging.getLogger("hangar.envdoctor")

SCRIPT = Path(__file__).with_name("sandbox_env.py")
VERSION = 1
# What is installed into a sandbox: a sandbox from an older hangar gets the new scripts at its next turn.
INSTALLED = hashlib.sha256(SCRIPT.read_bytes() + computer.SCRIPT.read_bytes() + computer.SKILL.read_bytes()
                           ).hexdigest()[:12]
STATUS_REL = ".hangar/envstatus.json"
MCP_NAME = "hangar-env"
_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def runtime(spec: EnvSpec, sb: Sandbox, settings: Settings) -> str:
    """gvisor | kata | runc | e2b | docker: what the sandbox runs under, for the doctor's caveats."""
    if sb.provider == "kubernetes":
        rc = (spec.runtime_class if spec.runtime_class is not None else settings.k8s_runtime_class) or ""
        if (sb.info.get("heavy") or {}).get("vm") or heavy.vm(spec):  # --- heavy: the heavy tier's VM runtime
            rc = settings.heavy_runtime_class
        return "gvisor" if "gvisor" in rc else "kata" if "kata" in rc else "runc"
    return sb.provider


def expected_secrets(spec: EnvSpec) -> list[dict[str, str]]:
    """Env var names the environment expects: its `secrets:` and the `${VAR}`s its MCP servers reference."""
    out: dict[str, str] = {var: f"secrets: (credential {cred})" for var, cred in spec.secrets.items()}
    for name, srv in spec.mcp.items():
        for var in _REF.findall(json.dumps(srv.model_dump(include={"url", "headers", "command", "args", "env"}))):
            out.setdefault(var, f"MCP server {name}")
    return [{"name": k, "for": v} for k, v in sorted(out.items())]


def build_status(settings: Settings, sb: Sandbox, spec: EnvSpec, s: Session | None = None,
                 requests: list[dict[str, Any]] | None = None, io: str | None = None,
                 fleet: Any = None) -> dict[str, Any]:
    info = sb.info or {}
    try:
        egress = egressapi.policy(spec, sandbox=sb, fleet=fleet, settings=settings)
    except Exception:  # noqa: BLE001 - the egress module's to get right; the doctor says "unknown" without it
        log.warning("egress policy for %s failed", sb.id, exc_info=True)
        egress = {"mode": "unknown", "allow": [], "proxy": None, "enforced": False}
    from hangar.envrequests import MAX_PENDING

    status = {
        "v": VERSION, "generated_at": now(), "sandbox": sb.id, "environment": spec.name,
        "harness": info.get("harness"), "provider": sb.provider, "runtime": runtime(spec, sb, settings),
        "session": s.name if s else None, "job": s.job_id if s else None, "io": io,
        "build": info.get("env_build") if isinstance(info.get("env_build"), dict) else None,
        "caches": info.get("caches") if isinstance(info.get("caches"), dict) else None,  # --- caches
        "services": [x for x in info.get("services") or [] if isinstance(x, dict)],
        "setup_failures": [x for x in info.get("setup_failures") or [] if isinstance(x, dict)][-20:],
        "warnings": [w for w in info.get("warnings") or [] if isinstance(w, str)][-20:],
        "secrets": expected_secrets(spec),
        "egress": egress,
        "computer": computer.status(spec, settings),
        "tier": heavy.status(sb, spec),  # --- heavy: the tier, the profile, and whether Docker answers
        "requests": requests or [],
        "request_limit": MAX_PENDING,  # pending at once; the sandbox side says so before filing more
        "artifact_max_mb": settings.exchange_max_artifact_mb,
    }
    # Build and setup messages, warnings and owners' notes come from logs and people: a build secret or a
    # credential pasted into one must not reach the agent through its own status file.
    return fleet.broker.redact(status) if fleet is not None else status


def builtin_mcp(spec: EnvSpec, settings: Settings, home: str) -> dict[str, Any]:
    """The MCP servers hangar adds for every harness: its own doctor, and the browser when asked for. A server
    of the same name in the environment's `mcp:` wins."""
    from hangar.harnesses import McpServer

    out: dict[str, Any] = {}
    if settings.env_doctor_mcp:
        out[MCP_NAME] = McpServer(command=f"{home}/.hangar/bin/hangar-env", args=["mcp"])
    out.update(computer.builtin_mcp(spec, home))
    return {k: v for k, v in out.items() if k not in spec.mcp}


async def install(fleet: Any, prov: Any, sb: Sandbox, spec: EnvSpec, h: Harness | None,
                  extra_env: dict[str, str]) -> None:
    """At claim: the doctor and computer scripts, the status file, the display's env, and the display itself."""
    home = f"/home/{fleet.settings.sandbox_user}"
    extra_env.update(computer.session_env(spec))
    await asyncio.gather(  # one round trip each on most providers: side by side, not one after another
        prov.write_file(sb, f"{home}/.hangar/bin/hangar-env", SCRIPT.read_bytes(), mode=0o755),
        computer.install(prov, sb, spec, h, home),
        write_status(prov, sb, home, build_status(fleet.settings, sb, spec, fleet=fleet)))
    sb.info["envdoctor"] = INSTALLED
    if spec.computer is not None and spec.computer.autostart:
        with contextlib.suppress(Exception):
            await prov.exec(sb, computer.start_cmd(home), timeout=30)


async def write_status(prov: Any, sb: Sandbox, home: str, status: dict[str, Any]) -> None:
    await prov.write_file(sb, f"{home}/{STATUS_REL}", json.dumps(status, indent=1).encode(), mode=0o644)


async def refresh(svc: Any, s: Session, sb: Sandbox, io: str | None = None) -> None:
    """At the start of a turn (and when a request is answered): the status as it is now, the scripts if this
    sandbox predates them, and the display if it went away (a restore or a resume). Never raises."""
    try:
        fleet = svc.fleet
        spec = fleet.catalog.get(sb.environment)
        _, prov = fleet._running(sb.id)
        home = f"/home/{svc.settings.sandbox_user}"
        if sb.info.get("envdoctor") != INSTALLED:
            await prov.write_file(sb, f"{home}/.hangar/bin/hangar-env", SCRIPT.read_bytes(), mode=0o755)
            h = None
            with contextlib.suppress(KeyError):
                from hangar.harnesses import get_harness

                h = get_harness(s.harness) if s.harness else None
            await computer.install(prov, sb, spec, h, home)
            sb.info["envdoctor"] = INSTALLED
        reqs = svc.env_requests.for_sandbox(s) if getattr(svc, "env_requests", None) is not None else []
        if io is None:
            from hangar.exchange import io_dir

            io = io_dir(svc.settings, s)
        await write_status(prov, sb, home, build_status(svc.settings, sb, spec, s, reqs, io, fleet))
        if spec.computer is not None and spec.computer.autostart:
            await prov.exec(sb, computer.start_cmd(home), timeout=30)
    except Exception:  # noqa: BLE001 - diagnosis must never be why a turn fails
        log.warning("session %s: could not refresh the environment status", s.id, exc_info=True)


# ====================================================================== prompting
AUTONOMY = (
    "Work autonomously: nobody is watching this run live, and a blocked agent sits idle for hours. If the "
    "environment blocks something (a missing secret, a host the network refuses, a service that won't start), run "
    "`hangar-agent env doctor` (add `--host <host>` for a network problem), file the blocker with "
    "`hangar-agent env request secret <NAME> --why \"...\"` or `hangar-agent env request egress <host> --why "
    "\"...\"`, and keep working on everything it doesn't block. Requests are never granted automatically; don't "
    "wait for them, and say in your summary what is still blocked."
)
VERIFY_UI = (
    "This sandbox has a computer (a display and Chromium). If your change affects anything a person sees, verify it "
    "end to end in the browser, then record a short demo: `hangar-agent record start`, drive the UI (Playwright, or "
    "`hangar-agent browse <url>`), `hangar-agent record stop`; add `hangar-agent screenshot <url>` for key states. "
    "They are attached to the PR."
)


def autonomy_lines(fleet: Any, environment: str) -> list[str]:
    """Lines for a job's "How to finish" list."""
    lines = [AUTONOMY]
    with contextlib.suppress(Exception):
        if fleet.catalog.get(environment).computer is not None:
            lines.append(VERIFY_UI)
    return lines
