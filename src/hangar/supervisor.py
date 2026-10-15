"""The supervisor's control-plane side (--- envbuilds): installs `hangar-svc` (svc_agent.py) in a sandbox, writes what
it supervises, starts it, and tells the agent about its environment.

Why a script and not hangar-envd. The supervisor runs the agent's own processes (dev servers, watchers), as the
agent's user, and the agent drives it (`hangar-agent svc restart web`). hangar-envd is hangar's authenticated transport
into the sandbox: it isn't on every provider (E2B has its own envd), it is replaced on upgrades and restarts, which
would take every dev server down with it, and agent-facing process control would widen what talks to it. A stdlib
python3 script (python3 is in the image contract, spec 0006) is installed per sandbox like `hangar-agent`, with no
image rebuild, and holds nothing secret. Under the uid split (isolation.py) it stays the agent's: hangar's own user
never runs it, and it never reaches hangar's runs.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hangar.harnesses import HARNESSES

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.envspec import EnvSpec
    from hangar.models import Sandbox
    from hangar.providers.base import Provider

SCRIPT = (Path(__file__).parent / "svc_agent.py").read_bytes()
BLOCK_START = "<!-- hangar:environment -->"
BLOCK_END = "<!-- /hangar:environment -->"
SKILL_NAME = "hangar-environment"
INSTRUCTIONS_MAX = 1024 * 1024


def _home(settings: Settings) -> str:
    return f"/home/{settings.sandbox_user}"


async def install(prov: Provider, sb: Sandbox, home: str) -> None:
    """~/.hangar/bin/hangar-svc, next to `hangar-agent` (which runs it for `hangar-agent svc`)."""
    await prov.write_file(sb, f"{home}/.hangar/bin/hangar-svc", SCRIPT, mode=0o755)


def manifest(sb: Sandbox, eff: EnvSpec, repo_path: str) -> dict[str, Any]:
    """What the supervisor runs and checks. Services come from the provider (envservices.py), with their hosts."""
    services = [s for s in sb.info.get("services") or [] if s.get("kind") == "service"]
    return {
        "version": 1,
        "repo": repo_path,
        "start": eff.start,
        "terminals": [t.model_dump(exclude_none=True) for t in eff.terminals],
        "services": [{"name": s["name"], "host": s.get("host") or "127.0.0.1", "port": s.get("port"),
                      "ready": s.get("check")} for s in services],
        "ports": [p.model_dump(exclude_none=True) for p in eff.ports],
    }


def programs_info(eff: EnvSpec) -> list[dict[str, Any]]:
    """sb.info["services"] entries for terminals and preview ports ({name, kind, port, check})."""
    out: list[dict[str, Any]] = [{"name": t.name, "kind": "terminal", "port": t.port, "check": t.ready}
                                 for t in eff.terminals]
    out += [{"name": p.name or f"port-{p.port}", "kind": "port", "port": p.port, "check": None} for p in eff.ports]
    return out


async def start(prov: Provider, sb: Sandbox, settings: Settings, eff: EnvSpec, repo_path: str) -> str | None:
    """Write the manifest and start the supervisor (it waits for services, runs `start`, starts the terminals). It
    returns at once: nothing here waits for a dev server. Returns why it didn't start, or None."""
    home = _home(settings)
    sb.info["services"] = [s for s in sb.info.get("services") or [] if s.get("kind") == "service"] + programs_info(eff)
    data = json.dumps(manifest(sb, eff, repo_path), indent=1).encode()
    await prov.write_file(sb, f"{home}/.hangar/svc/manifest.json", data, mode=0o600)
    res = await prov.exec(sb, f"chmod 700 {home}/.hangar/svc && python3 {home}/.hangar/bin/hangar-svc up",
                          timeout=30, cwd=repo_path)
    if res.exit_code != 0:
        return (res.stderr or res.stdout).strip()[-500:] or f"`hangar-svc up` exited {res.exit_code}"
    return None


def instructions(sb: Sandbox, eff: EnvSpec, repo_path: str) -> str:
    """The "Your environment" section of the agent's instructions (between BLOCK_START and BLOCK_END)."""
    info = sb.info.get("env_build") or {}
    q = shlex.quote
    lines = [BLOCK_START, "## Your environment (hangar)", ""]
    if info.get("fallback"):
        lines += [
            f"**Warning: this sandbox runs the environment's base image, not a build of this repo** "
            f"({info.get('reason') or 'no build'}). Nothing from `install` has run here.",
        ]
        if eff.install:
            lines += ["Before building or testing, install the dependencies yourself, then restart what depends on "
                      "them:", "", "```", f"cd {q(repo_path)} && {eff.install}", "hangar-agent svc restart --all",
                      "```"]
        lines.append("")
    elif info.get("version"):
        lines.append(f"This sandbox starts from build {info['version']} of the repo ({(info.get('commit') or '')[:9]}"
                     + (f", install: `{eff.install}`" if eff.install else "") + "): its dependencies are installed.")
        if info.get("stale"):
            lines.append(f"It is behind: {info.get('reason')}. If something fails for a missing or outdated "
                         f"dependency, run the install yourself (`{eff.install}` in {repo_path}).")
        lines.append("")
    services = [s for s in sb.info.get("services") or [] if s.get("kind") == "service"]
    if eff.start or eff.terminals or services:
        lines += ["A supervisor runs the environment's services and dev servers and restarts them if they exit. "
                  "Use it rather than starting them yourself in the background:"]
        if services:
            lines.append("- Services: " + ", ".join(f"`{s['name']}` at `{s.get('host') or '127.0.0.1'}:{s['port']}`"
                                                   for s in services) + ".")
        if eff.start:
            lines.append(f"- `start` ran once at sandbox start: `{eff.start}`.")
        for t in eff.terminals:
            bits = [f"`{t.command}`"] + ([f"port {t.port}"] if t.port else [])
            bits += [] if t.autostart else ["not started: `hangar-agent svc start` it when you need it"]
            lines.append(f"- `{t.name}`: " + ", ".join(bits) + ".")
        lines.append("")
    lines += [
        "`hangar-agent svc status` shows what runs and whether it's ready; `hangar-agent svc wait NAME` blocks until "
        "it is; `hangar-agent svc logs NAME` prints its output (files in `~/.hangar/svc/logs/`, search them with rg); "
        "`hangar-agent svc restart NAME` after changes it doesn't reload by itself; `hangar-agent svc add NAME -- "
        "CMD` supervises a process of your own. `hangar-agent svc --help` has the rest.",
    ]
    if failures := sb.info.get("setup_failures"):
        lines += ["", "Setup problems hangar ran into:"]
        lines += [f"- {f['step']}: {f['message'][:400]}" for f in failures[-5:]]
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


def splice(current: str, block: str) -> str:
    """The instructions file with hangar's block replaced (or appended): the rest is the environment's own."""
    a, b = current.find(BLOCK_START), current.find(BLOCK_END)
    if a != -1 and b > a:
        return current[:a] + block + current[b + len(BLOCK_END):].lstrip("\n")
    return current + ("\n\n" if current and not current.endswith("\n\n") else "") + block


SKILL_DESCRIPTION = (
    "Use when you need the project's services or dev servers (databases, a web server, watchers), when a build or "
    "test fails for a missing dependency or a service that isn't there, or to read a running process's logs. Covers "
    "`hangar-agent svc` (the supervisor), the repo's .hangar/environment.yaml, and what to do when hangar says the "
    "sandbox runs the base image.")
SKILL = f"---\nname: {SKILL_NAME}\ndescription: {json.dumps(SKILL_DESCRIPTION)}\n---\n" + """

# Your hangar environment

Hangar builds this repo's environment from `.hangar/environment.yaml` (fields: `install`, `start`, `terminals`,
`services`, `ports`, `verify`). A build ran `install` ahead of time, so dependencies are already in the checkout.
The "Your environment" section of your instructions says which build you run, and warns you when it is the base
image instead (then run the install command it gives you yourself).

## The supervisor: `hangar-agent svc`

- `hangar-agent svc status` (`--json`): every service, `start` and terminal, whether it is ready, restarts, its log.
- `hangar-agent svc wait [NAME...] --timeout 120`: block until ready; exit 1 says which failed.
- `hangar-agent svc logs NAME -n 200` (`-f` to follow). Logs are files in `~/.hangar/svc/logs/`: `rg -n error
  ~/.hangar/svc/logs/` searches them all.
- `hangar-agent svc restart NAME` after changing code or config a process doesn't reload; `stop`, `start`, `--all`.
- `hangar-agent svc add NAME [--port P] [--ready CMD] -- COMMAND` keeps a process of your own up (a worker, a second
  server). `rm NAME` removes it.

Don't start long-running servers with `&` or `nohup`: they die with your shell, and nobody restarts them. Don't
`sleep` waiting for a port: use `svc wait`.

## When something is missing

1. `hangar-agent svc status`: is the service up? If not, `hangar-agent svc logs NAME`.
2. A dependency missing: run the repo's `install` (in `.hangar/environment.yaml`) in the checkout.
3. Something the environment should provide for every run (a tool, a service, a new step): change
   `.hangar/environment.yaml` in your commit, and say so in your summary. The next build picks it up.
"""


async def tell_agent(prov: Provider, sb: Sandbox, settings: Settings, harness: str | None, eff: EnvSpec,
                     repo_path: str) -> None:
    """Write the environment section into the harness's global instructions file and the hangar-environment skill
    into its skills directory: every harness and every turn read them (MCP runs and interactive terminals too)."""
    h = HARNESSES.get(harness or "")
    if h is None:
        return
    home = _home(settings)
    if h.instructions_file:
        path = f"{home}/{h.instructions_file}"
        cur = await prov.read_file(sb, path, limit=INSTRUCTIONS_MAX)
        if cur is None or len(cur) <= INSTRUCTIONS_MAX:
            text = splice((cur or b"").decode(errors="replace"), instructions(sb, eff, repo_path))
            await prov.write_file(sb, path, text.encode(), mode=0o644)
    if h.skills_dir:
        await prov.write_file(sb, f"{home}/{h.skills_dir}/{SKILL_NAME}/SKILL.md", SKILL.encode(), mode=0o644)
