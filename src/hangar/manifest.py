"""The effective harness manifest: `hangar.manifest/v1` (--- interfaces; docs/api/manifest.schema.json, spec 0014).

The exact configuration an agent ran with, as one JSON document a tool outside hangar can read, diff and reproduce
from: the harness and its locked version, the resolved model, the instructions and skills hangar put in front of it
(the environment's global instructions file, its skills, and the repo's AGENTS.md / CLAUDE.md by reference and
digest), its MCP servers (names, transports and where they point, never credentials), the hooks hangar adds, the
approval and permission mode, egress, the environment and its build, resources, and the turn's budget and policy.

A turn's manifest is captured as the turn starts and stored with it (`<data>/manifests/<session>/<turn>.json`); the
turn records its digest (`SessionTurn.manifest`). Four digests: `digest` over everything but when and for what it was
captured; `config_digest` over the harness configuration and its environment together (no task, repo or prompt),
which is what two trials of one experiment variant share and two variants don't; and its two halves,
`harness_digest` (what a harness author controls: the prefix and binding layer) and `environment_digest` (what the
sandbox is: image, build, egress, resources, isolation), so a harness change and an environment rebuild can be told
apart. The halves are computed by `section_digest` over `HARNESS_SECTIONS` and `ENVIRONMENT_SECTIONS`; spec 0014 §2
writes the canonical form out so a harness lock (#133) produces the identical value.

What the sandbox actually had (the instruction files' and skills' digests) is read from it by one command as the turn
starts; what hangar meant it to have comes from the environment spec. Both are recorded: they differ when something
changed the files (an experiment's overlay, the agent in an earlier turn).

Owner-scoped like the session it describes, and redacted with the broker's redaction before it is stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from hangar import people, plugins
from hangar.harnesses import HARNESSES, McpServer, pack_tag
from hangar.models import Session, SessionTurn, now

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet
    from hangar.jobs import JobService
    from hangar.models import Job, Sandbox
    from hangar.sessions import SessionService

log = logging.getLogger("hangar.manifest")

SCHEMA = "hangar.manifest/v1"
SCHEMA_FILE = "docs/api/manifest.schema.json"
REPO_FILES = ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md", "GEMINI.md")  # read from the checkout by digest
# The sections config_digest covers: what configures the harness, not the task it was given.
CONFIG_SECTIONS = ("harness", "model", "instructions.global", "skills", "mcp_servers", "permissions", "egress",
                   "environment.name", "environment.image", "environment.spec_digest", "resources")
# harness_digest: the prefix and binding layer, everything a harness author controls. Declared skills and
# instructions, not effective ones: drift is effective != declared. model.id is the concrete id (never a policy).
# `plugins` (#136): the environment's seed plugins, null without any. `a[*].{x,y}` keeps fields x and y of each item
# of list a.
HARNESS_SECTIONS = ("harness.name", "harness.locked", "model.id", "instructions.global.declared.digest",
                    "skills.declared[*].{name,digest}", "plugins",
                    "mcp_servers[*].{name,transport,url,headers,env,command,args}", "hooks[*].{event,source,digest}",
                    "permissions.approvals")
# environment_digest: what the sandbox is.
ENVIRONMENT_SECTIONS = ("environment.name", "environment.image", "environment.spec_digest", "environment.build.id",
                        "egress.effective", "resources", "permissions.uid_split", "permissions.isolation")
_SECTION = re.compile(r"^(?P<path>[\w.]+?)(?:\[\*\](?:\.\{(?P<fields>[\w,]+)\})?)?$")
PROVIDERS = {"claude-code": "anthropic", "codex": "openai", "antigravity": "gcp.gemini"}
TEXT_MAX = 16_000
PROBE_SECONDS = 20


# ====================================================================== pieces
def _sha(data: bytes | str) -> str:
    return "sha256:" + hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _url(url: str) -> str:
    """Where an MCP server is, without credentials: no user info, query or fragment (tokens live there)."""
    try:
        u = urlsplit(url)
    except ValueError:
        return "(unparseable)"
    host = u.hostname or ""
    port = f":{u.port}" if u.port else ""
    return f"{u.scheme}://{host}{port}{u.path}" if u.scheme else "(relative)"


def mcp_servers(spec: EnvSpec, harness: str | None) -> list[dict[str, Any]]:
    """The environment's MCP servers this harness gets, as written into its config at claim: names, transports and
    endpoints. Header and env *names* only (their values are where credentials go); `${VAR}` references are kept as
    references, never resolved. Tools are listed when hangar knows them (it doesn't proxy MCP yet: spec 0007 §5.1)."""
    servers: dict[str, McpServer] = {n: s for n, s in spec.mcp.items() if s.harnesses is None or harness in s.harnesses}
    out = []
    for name, s in sorted(servers.items()):
        d: dict[str, Any] = {"name": name, "transport": "http" if s.url else "stdio", "source": "environment",
                             "tools": None}
        if s.url:
            d["url"] = _url(s.url)
            d["headers"] = sorted(s.headers)
        else:
            d["command"] = s.command
            d["args"] = list(s.args)
            d["env"] = sorted(s.env)
        out.append(d)
    if spec.computer and spec.computer.browser_mcp:  # computer.py: Playwright's server for every harness
        out.append({"name": "playwright", "transport": "stdio", "source": "computer", "command": "playwright-mcp",
                    "args": [], "env": [], "tools": None})
    return out


def _dir_digest(path: str, mtime_ns: int = 0) -> tuple[str, int]:
    """(digest, files) of a skill directory on the control plane, over relative paths and contents. Cached by every
    file's size and mtime, not the directory's (`mtime_ns`, kept for callers): editing a file in place leaves the
    directory's mtime alone, and a stale digest would hide a harness change (#131)."""
    root = Path(path)
    stats: list[tuple[str, int, int]] = []
    for f in root.rglob("*"):
        with contextlib.suppress(OSError):
            if f.is_file() and not f.is_symlink():
                st = f.stat()
                stats.append((f.relative_to(root).as_posix(), st.st_size, st.st_mtime_ns))
    return _dir_digest_of(path, tuple(sorted(stats)))


@functools.lru_cache(maxsize=256)
def _dir_digest_of(path: str, stats: tuple[tuple[str, int, int], ...]) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    root = Path(path)
    for f in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        h.update(f.relative_to(root).as_posix().encode() + b"\0")
        with contextlib.suppress(OSError):
            h.update(hashlib.sha256(f.read_bytes()).digest())
        n += 1
    return "sha256:" + h.hexdigest(), n


def declared_skills(spec: EnvSpec, harness: str | None) -> list[dict[str, Any]]:
    """The environment's skills for this harness, as its spec declares them (baked into its image)."""
    h = HARNESSES.get(harness or "")
    out = []
    if h is None or not h.skills_dir:
        return out
    for sk in spec.skills:
        if sk.harnesses is not None and h.name not in sk.harnesses:
            continue
        d: dict[str, Any] = {"name": sk.name, "source": None, "digest": None}
        if sk.path:
            d["source"] = f"path:{sk.path}"
            p = (spec.source_dir or Path(".")) / sk.path
            with contextlib.suppress(OSError):
                d["name"] = d["name"] or p.name
                d["digest"], d["files"] = _dir_digest(str(p.resolve()), p.stat().st_mtime_ns)
        elif sk.git:
            d["source"] = f"git:{sk.git}"
            d["name"] = d["name"] or sk.git.partition("#")[2].partition("@")[0].rstrip("/").rsplit("/", 1)[-1] \
                or sk.git.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            d["pinned"] = "@" in sk.git.partition("#")[2] or "@" in sk.git.rpartition("/")[2]
        out.append(d)
    if spec.computer:
        out.append({"name": "verify-ui", "source": "hangar:computer", "digest": None})
    return out


def _hooks(spec: EnvSpec, harness: str | None, env: dict[str, str]) -> list[dict[str, Any]]:
    """Hooks the harness runs: hangar's verify gate (a Stop hook in --settings for Claude Code, the driver for
    Codex), and any the environment's own config files declare (Claude Code's settings.json `hooks`)."""
    from hangar.verifyloop import CLAUDE_ENV, GATE_ENV

    out: list[dict[str, Any]] = []
    if raw := env.get(CLAUDE_ENV):
        with contextlib.suppress(ValueError, AttributeError):
            for event, entries in (json.loads(raw).get("hooks") or {}).items():
                out.append({"event": event, "source": "hangar:verify_gate", "harness": harness,
                            "count": len(entries) if isinstance(entries, list) else 1})
    if raw := env.get(GATE_ENV):
        with contextlib.suppress(ValueError, AttributeError):
            g = json.loads(raw)
            out.append({"event": "turn.completed", "source": "hangar:verify_gate", "harness": harness,
                        "command": g.get("command"), "rounds": g.get("rounds")})
    files = {**spec.agent_config.files, **{k: v for k, v in spec.files.items() if not k.startswith("/")}}
    for path, content in sorted(files.items()):
        if not path.endswith("settings.json"):
            continue
        with contextlib.suppress(ValueError, AttributeError):
            for event, entries in (json.loads(content).get("hooks") or {}).items():
                out.append({"event": event, "source": f"environment:{path}",
                            "count": len(entries) if isinstance(entries, list) else 1, "digest": _sha(content)})
    return out


def _spec_digest(spec: EnvSpec) -> str:
    return _sha(_canon(spec.model_dump(mode="json", exclude={"source_dir", "packs"})))


def _get(d: dict[str, Any], dotted: str) -> Any:
    for part in dotted.split("."):
        d = d.get(part) if isinstance(d, dict) else None  # type: ignore[assignment]
    return d


def _no_new_privs(sb: Sandbox | None, pname: str | None) -> bool | None:
    """--- #124: whether the sandbox's processes can't gain privileges (sudo fails): GKE pods always
    (allowPrivilegeEscalation: false), except a heavy one with Docker (privileged in its VM); Docker as recorded at
    create. None when unknown (no sandbox, another provider)."""
    if sb is None:
        return None
    if pname == "kubernetes":
        return not (sb.info.get("heavy") or {}).get("docker")
    v = sb.info.get("no_new_privs")
    return bool(v) if v is not None else (False if pname == "docker" else None)


def pick(m: dict[str, Any], section: str) -> Any:
    """One section of a manifest: `a.b` (null when absent), or `a[*].{x,y}` (each item of list `a` with only its
    fields x and y, a missing one null; null when `a` isn't a list)."""
    got = _SECTION.match(section)
    if got is None:
        raise ValueError(f"bad manifest section {section!r}")
    v = _get(m, got["path"])
    if "[*]" not in section:
        return v
    if not isinstance(v, list):
        return None
    if not got["fields"]:
        return v
    fields = got["fields"].split(",")
    return [{f: x.get(f) if isinstance(x, dict) else None for f in fields} for x in v]


def section_digest(m: dict[str, Any], sections: tuple[str, ...]) -> str:
    """`"sha256:" + hex(sha256(canon({section: pick(m, section)})))`, canon being JSON with sorted keys, no
    whitespace (spec 0014 §2: a harness lock computes harness_digest the same way)."""
    return _sha(_canon({k: pick(m, k) for k in sections}))


DIGESTS = ("digest", "config_digest", "harness_digest", "environment_digest")


def seal(m: dict[str, Any], redact: Any) -> dict[str, Any]:
    """Redact, then add the digests (over the redacted document, which is what anyone can see)."""
    m = redact(m)
    body = {k: v for k, v in m.items() if k not in (*DIGESTS, "captured_at", "subject")}
    m["digest"] = _sha(_canon(body))
    m["config_digest"] = _sha(_canon({k: _get(m, k) for k in CONFIG_SECTIONS}))
    m["harness_digest"] = section_digest(m, HARNESS_SECTIONS)
    m["environment_digest"] = section_digest(m, ENVIRONMENT_SECTIONS)
    return m


# ====================================================================== building
def build(fleet: Fleet, spec: EnvSpec, harness: str | None, *, s: Session | None = None,
          turn: SessionTurn | None = None, n: int | None = None, sb: Sandbox | None = None, job: Job | None = None,
          probe: dict[str, Any] | None = None, prompt: str | None = None,
          env: dict[str, str] | None = None) -> dict[str, Any]:
    """The manifest of what `harness` in `spec` runs with: for a turn (with its session and sandbox) when they are
    given, else what a new sandbox of the environment would get."""
    from hangar import modelpolicy
    from hangar.steer import policy as approval_policy

    settings = fleet.settings
    env_spec = spec  # the environment's identity stays the environment's
    if sb is not None:  # an experiment trial's variant: the skills and instructions it declares (#132)
        from hangar import experiments

        spec = experiments.effective_spec(fleet, sb, spec)
    h = HARNESSES.get(harness or "")
    info = sb.info if sb is not None else {}
    # model: the session's pinned id; else the environment's, resolved as a new sandbox would be
    if s is not None:
        model = {"id": s.model, "policy": s.model_policy, "source": "session"}
    else:
        r = modelpolicy.of(fleet).resolve(harness, spec.model_for(harness), record=False)
        model = {"id": r.model, "policy": r.policy, "source": r.source}
    model["provider"] = PROVIDERS.get(harness or "")
    # --- #167: false for a floating alias (`opus`, `*-latest`) or no id: evaluation leaves such runs out
    model["concrete"] = modelpolicy.of(fleet).concreteness(harness, model["id"])[0]
    model["cache_ttl"] = turn.cache_ttl if turn is not None else None
    pname = (sb.provider if sb is not None else None) or (s.provider if s is not None else None) \
        or settings.default_provider
    pack = (info.get("packs") or {}).get(harness or "") if sb is not None else None
    if pack is None and h is not None and h.install and sb is None:
        pack = pack_tag(h, settings.image_prefix, settings.registry if pname in ("kubernetes", "e2b") else None)
    locked = h.version if h is not None and h.install else None
    harness_d = {"name": harness, "version": (turn.harness_version if turn else None) or info.get("harness_version"),
                 "locked": (s.harness_version if s is not None else None) or locked,
                 "pack": pack, "mode": "pack" if pack else ("bake" if h is not None and h.install else "image"),
                 "live": turn.live if turn is not None else None}
    # instructions
    glob: dict[str, Any] = {"path": f"~/{h.instructions_file}" if h and h.instructions_file else None,
                            "declared": None, "effective": None}
    if spec.agent_config.instructions:
        text = spec.agent_config.instructions
        glob["declared"] = {"source": "environment", "digest": _sha(text), "bytes": len(text.encode()),
                            "text": text[:TEXT_MAX], "truncated": len(text) > TEXT_MAX}
    repo_files: list[dict[str, Any]] = []
    effective_skills: list[dict[str, Any]] | None = None
    head = None
    if probe is not None:
        glob["effective"] = probe.get("global")
        repo_files = probe.get("repo") or []
        effective_skills = probe.get("skills")
        head = probe.get("head")
    instructions: dict[str, Any] = {"global": glob, "repo": {"repo": s.repo if s else None, "commit": head,
                                                            "files": repo_files}}
    if prompt is not None:
        instructions["prompt"] = {"digest": _sha(prompt), "bytes": len(prompt.encode()), "text": prompt[:TEXT_MAX],
                                  "truncated": len(prompt) > TEXT_MAX}
    # permissions and approvals
    approvals = (turn.approval_policy if turn is not None and turn.approval_policy else None) or (
        approval_policy(_SvcView(fleet), s) if s is not None else spec.approvals or settings.approvals)
    iso = info.get("isolation") or {}
    split = bool(sb is not None and turn is not None and turn.run_id and _split(sb, turn.run_id))
    permissions = {"approvals": approvals, "live": turn.live if turn is not None else None,
                   "harness_permission_checks": "off" if approvals == "auto" else "on",
                   "uid_split": split if turn is not None else None,
                   "hardened": bool(split and approvals != "auto") if turn is not None else None,
                   "isolation": {k: iso.get(k) for k in ("mode", "split") if k in iso} or None,
                   # --- #124: the environment's sudo, and whether the sandbox ran with no-new-privileges (Docker
                   # jobs under HANGAR_INTAKE_POSTURE=enforce; GKE pods always, allowPrivilegeEscalation: false)
                   "sudo": spec.sudo,
                   "no_new_privs": _no_new_privs(sb, pname)}
    egress: dict[str, Any] = {"declared": spec.egress.model_dump(mode="json") if spec.egress else None,
                              "default": settings.egress_default, "effective": info.get("egress")}
    if floor := (info.get("egress") or {}).get("floor"):  # --- intake posture (#123): raised for an intake job
        egress.update({"floor": floor, "floor_reason": (info.get("egress") or {}).get("floor_reason") or "intake"})
    build_info = info.get("env_build")
    environment = {"name": spec.name, "image": sb.image if sb is not None else spec.image,
                   "spec_digest": _spec_digest(env_spec), "provider": pname, "tier": spec.tier,
                   "runtime_class": spec.runtime_class if spec.runtime_class is not None else settings.k8s_runtime_class
                   if pname == "kubernetes" else None,
                   "build": {k: build_info.get(k) for k in ("version", "id", "commit", "config_digest", "fallback",
                                                            "reason", "stale")} if build_info else None,
                   "services": [{"name": k, "image": v.image} for k, v in sorted(spec.services.items())],
                   "secret_env": sorted(spec.secrets)}  # names only: which variables hold a secret, never values
    res = sb.resources if sb is not None else spec.resources
    resources = {"cpus": res.cpus, "memory_gb": res.memory_gb, "disk_gb": res.disk_gb}
    policy: dict[str, Any] = {"turn_budget": (s.budget.model_dump(mode="json") if s is not None and s.budget else None),
                              "credential_mode": spec.credential_mode, "cache": s.cache if s is not None else None}
    if job is not None:
        policy.update({"job": job.id, "verify": job.verify, "verify_source": job.verify_source,
                       "verify_in_loop": job.verify_in_loop, "max_verify_rounds": job.max_verify_rounds,
                       "job_budget": job.job_budget.model_dump(mode="json") if job.job_budget else None,
                       "deliver": job.deliver, "strategy": job.strategy, "timeout_minutes": job.timeout_minutes,
                       "autofix": job.autofix})
    from hangar.traces import hangar_version

    m: dict[str, Any] = {
        "schema": SCHEMA,
        "captured_at": now(),
        "subject": {"kind": "turn" if turn is not None else ("session" if s is not None else "environment"),
                    "session": s.name if s else None, "session_id": s.id if s else None, "turn": n,
                    "run_id": turn.run_id if turn else None,
                    "job": (s.job_id if s else None) or (job.id if job else None),
                    "person": s.person if s else None, "sandbox": sb.id if sb is not None else None},
        "hangar": {"version": hangar_version()},
        "harness": harness_d,
        "model": model,
        "instructions": instructions,
        "skills": {"declared": declared_skills(spec, harness), "effective": effective_skills,
                   "dir": f"~/{h.skills_dir}" if h and h.skills_dir else None},
        "plugins": plugins.manifest(spec, harness),  # --- #136: the read-only seed's
        "mcp_servers": mcp_servers(spec, harness),
        "hooks": _hooks(spec, harness, env or {}),
        "permissions": permissions,
        "egress": egress,
        "environment": environment,
        "resources": resources,
        "policy": policy,
        "probe": {"ok": probe is not None and not probe.get("error"), "error": (probe or {}).get("error"),
                  "plugins": (probe or {}).get("plugins")}  # --- #136: the seed plugins the sandbox has
        if turn is not None else None,
        # --- #125: a subscription login in the sandbox (teamcreds), with the turn's own
        "warnings": list(dict.fromkeys([*(turn.warnings if turn is not None else []),
                                        *(info.get("credential_warnings") or []),
                                        *(s.warnings if s is not None else [])])),
    }
    if s is not None:
        m["repo"] = {"url": _url(s.repo) if s.repo and "://" in s.repo else s.repo, "base_branch": s.base_branch,
                     "base_sha": s.base_sha, "branch": s.branch}
    return seal(m, fleet.broker.redact)


class _SvcView:
    """What steer.policy reads from a SessionService: the fleet's catalog and settings."""

    def __init__(self, fleet: Fleet):
        self.fleet, self.settings = fleet, fleet.settings


def _split(sb: Sandbox, run_id: str) -> bool:
    from hangar import isolation

    with contextlib.suppress(Exception):
        return isolation.run_split(sb, run_id)
    return False


# ====================================================================== the probe: what the sandbox has
def probe_script(repo_path: str | None, home: str, harness: str | None, spec: EnvSpec | None = None) -> str:
    h = HARNESSES.get(harness or "")
    q = shlex.quote
    lines = ["export LC_ALL=C GIT_OPTIONAL_LOCKS=0",
             "dig() { sha256sum | cut -d' ' -f1; }"]
    if repo_path:
        files = " ".join(q(f) for f in REPO_FILES)
        lines.append(f"if cd {q(repo_path)} 2>/dev/null; then "
                     "printf 'head\\t%s\\n' \"$(git rev-parse -q --verify HEAD)\";"
                     f" for f in {files}; do [ -f \"$f\" ] && printf 'repo\\t%s\\t%s\\t%s\\n' \"$f\" "
                     "\"$(wc -c < \"$f\")\" \"$(dig < \"$f\")\"; done; fi")
    if h is not None and h.instructions_file:
        f = q(f"{home}/{h.instructions_file}")
        lines.append(f"[ -f {f} ] && printf 'global\\t%s\\t%s\\n' \"$(wc -c < {f})\" \"$(dig < {f})\"")
    if h is not None and h.skills_dir:
        d = q(f"{home}/{h.skills_dir}")
        lines.append(f"for s in {d}/*/; do [ -d \"$s\" ] || continue; n=$(basename \"$s\"); "
                     "c=$(cd \"$s\" && find . -type f | wc -l); "
                     "g=$(cd \"$s\" && find . -type f -print0 | sort -z | xargs -0r sha256sum | dig); "
                     "printf 'skill\\t%s\\t%s\\t%s\\n' \"$n\" \"$g\" \"$c\"; done")
    if spec is not None:  # --- #136
        lines += plugins.probe_lines(spec, harness)
    lines.append("true")
    return "\n".join(lines)


def parse_probe(out: str) -> dict[str, Any]:
    r: dict[str, Any] = {"head": None, "global": None, "repo": [], "skills": []}
    for line in out.splitlines():
        parts = line.split("\t")
        try:
            if parts[0] == "head" and len(parts) == 2:
                r["head"] = parts[1].strip() or None
            elif parts[0] == "repo" and len(parts) == 4:
                r["repo"].append({"path": parts[1], "bytes": int(parts[2].strip()), "digest": "sha256:" + parts[3]})
            elif parts[0] == "global" and len(parts) == 3:
                r["global"] = {"bytes": int(parts[1].strip()), "digest": "sha256:" + parts[2]}
            elif parts[0] == "plugin" and len(parts) == 4:  # --- #136
                r.setdefault("plugins", []).append({
                    "id": parts[1][:200], "present": parts[2] != "-",
                    "digest": "sha256:" + parts[2] if parts[2] != "-" else None, "files": int(parts[3].strip())})
            elif parts[0] == "skill" and len(parts) == 4:
                r["skills"].append({"name": parts[1][:100], "digest": "sha256:" + parts[2],
                                    "files": int(parts[3].strip())})
        except ValueError:
            continue
    return r


def start_probe(svc: SessionService, s: Session, sb: Sandbox) -> asyncio.Task[dict[str, Any]]:
    """Read the instruction files and skills the turn will see, as it starts (one command, in the background)."""

    async def run() -> dict[str, Any]:
        home = f"/home/{svc.settings.sandbox_user}"
        try:
            try:  # --- #136: the environment's seed plugins too
                spec: EnvSpec | None = svc.fleet.catalog.get(s.environment)
            except Exception:  # noqa: BLE001 - no plugins to look for, the rest is read
                spec = None
            res = await svc.fleet.exec(sb.id, probe_script(sb.info.get("repo_path"), home, s.harness, spec),
                                       timeout=PROBE_SECONDS, actor=f"session:{s.id}", quiet=True)
        except Exception as e:  # noqa: BLE001 - the manifest says it couldn't look; the turn goes on
            return {"error": f"could not read the sandbox: {str(e)[:200]}"}
        return parse_probe(res.stdout)

    return asyncio.create_task(run())


async def record(svc: SessionService, s: Session, turn: SessionTurn, sb: Sandbox,
                 probe: asyncio.Task[dict[str, Any]] | None, prompt: str | None, env: dict[str, str]) -> None:
    """From SessionService.begin_turn, in the background once the agent is started (the probe was started before
    it): build, store and point the turn at its manifest. Never fails the turn."""
    try:
        got: dict[str, Any] | None = None
        if probe is not None:
            try:
                got = await asyncio.wait_for(asyncio.shield(probe), PROBE_SECONDS + 5)
            except TimeoutError:
                probe.cancel()
                got = {"error": "reading the sandbox timed out"}
        spec = svc.fleet.catalog.get(s.environment)
        from hangar.models import Job

        job = svc.fleet.store.get(Job, s.job_id) if s.job_id else None
        n = next((i for i, t in enumerate(s.turns, 1) if t is turn), len(s.turns))
        m = build(svc.fleet, spec, turn.harness or s.harness, s=s, turn=turn, n=n, sb=sb, job=job, probe=got,
                  prompt=prompt, env=env)
        await asyncio.to_thread(store(svc.settings).put, s.id, n, m)
        turn.manifest = m["digest"]
        turn.harness_digest, turn.environment_digest = m.get("harness_digest"), m.get("environment_digest")
        svc._save(s)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.warning("session %s: could not record the turn's manifest", s.id, exc_info=True)


# ====================================================================== storage
class ManifestStore:
    """One JSON file per turn, `<data>/manifests/<session id>/<turn>.json`; dropped with the session."""

    def __init__(self, root: Path):
        self.root = root

    def _dir(self, sid: str) -> Path:
        return self.root / re.sub(r"[^A-Za-z0-9._-]", "_", sid)

    def put(self, sid: str, n: int, m: dict[str, Any]) -> None:
        d = self._dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{n}.json.tmp"
        tmp.write_text(json.dumps(m, indent=1, default=str))
        os.replace(tmp, d / f"{n}.json")

    def get(self, sid: str, n: int) -> dict[str, Any] | None:
        try:
            return json.loads((self._dir(sid) / f"{n}.json").read_text())
        except (OSError, ValueError):
            return None

    def drop(self, sid: str) -> None:
        import shutil

        shutil.rmtree(self._dir(sid), ignore_errors=True)


def store(settings: Any) -> ManifestStore:
    return ManifestStore(settings.data_dir / "manifests")


# ====================================================================== reading
def for_session(svc: SessionService, s: Session, turn: int | None = None) -> dict[str, Any]:
    """A turn's stored manifest (`turn`, else the latest turn that has one). Without one, what the session would run
    with now (`subject.kind: session`, not captured)."""
    st = store(svc.settings)
    redact = svc.fleet.broker.redact  # --- review(#114): again on the way out: a secret stored since is masked too
    if turn is not None:
        if not 1 <= turn <= len(s.turns):
            raise KeyError(f"session {s.name} has no turn {turn} (it has {len(s.turns)})")
        m = st.get(s.id, turn)
        if m is None:
            raise KeyError(f"turn {turn} of session {s.name} has no manifest (it started before hangar recorded "
                           "them, or recording it failed)")
        return redact(m)
    for n in range(len(s.turns), 0, -1):
        if s.turns[n - 1].manifest and (m := st.get(s.id, n)) is not None:
            return redact(m)
    spec = svc.fleet.catalog.get(s.environment)
    sb = None
    with contextlib.suppress(Exception):
        sb = svc.fleet.get(s.sandbox_id) if s.sandbox_id else None
    return build(svc.fleet, spec, s.harness or spec.pick_harness(), s=s, sb=sb)


def for_job(jobs: JobService, job: Job, turn: int | None = None) -> dict[str, Any]:
    """A job turn's manifest (`turn`: 1-based, in the job's own turns), else its latest."""
    s = jobs._session(job)
    if s is None:
        raise KeyError(f"job {job.id} has not started yet")
    if turn is None:
        return for_session(jobs.sessions, s)
    if not 1 <= turn <= len(job.turns):
        raise KeyError(f"job {job.id} has no turn {turn} (it has {len(job.turns)})")
    run = job.turns[turn - 1].run_id
    for cand in [s, *(x for x in jobs.sessions.of_job(job.id) if x.id != s.id)]:
        for n, t in enumerate(cand.turns, 1):
            if run and t.run_id == run:
                return for_session(jobs.sessions, cand, n)
    raise KeyError(f"turn {turn} of job {job.id} hasn't started")


def for_environment(fleet: Fleet, name: str, harness: str | None = None) -> dict[str, Any]:
    """What a new sandbox of the environment would run `harness` (default: its default) with."""
    spec = fleet.catalog.get(name)
    return build(fleet, spec, spec.pick_harness(harness))


# ====================================================================== REST, MCP
def mount(app: Any, h: Any, U: Any, J: Any, owner_filter: Any) -> None:
    """GET /api/sessions/{ref}/manifest?turn=N, GET /api/jobs/{id}/manifest?turn=N,
    GET /api/environments/{name}/manifest?harness=H; and the traces: GET /api/sessions/{ref}/trace?turn=N,
    GET /api/jobs/{id}/trace (docs/api/traces.md)."""
    from fastapi.responses import JSONResponse

    from hangar import traces

    def session(ref: str) -> Session:
        s = h.sessions.get(ref)
        if not people.visible(s.person):
            raise KeyError(f"no session '{ref}'")
        return s

    def job(job_id: str, caller: Any) -> Job:
        j = h.jobs.get(job_id, owner=owner_filter(caller))
        if not people.visible(j.person):
            raise KeyError(f"no job '{job_id}'")
        return j

    @app.get("/api/sessions/{ref}/manifest")
    async def session_manifest(ref: str, turn: int | None = None, _: str = U):
        return for_session(h.sessions, session(ref), turn)

    @app.get("/api/jobs/{job_id}/manifest")
    async def job_manifest(job_id: str, turn: int | None = None, caller: Any = J):
        return for_job(h.jobs, job(job_id, caller), turn)

    @app.get("/api/environments/{name}/manifest")
    async def env_manifest(name: str, harness: str | None = None, _: str = U):
        return for_environment(h.fleet, name, harness)

    @app.get("/api/sessions/{ref}/trace")
    async def session_trace(ref: str, turn: int | None = None, _: str = U):
        return JSONResponse(await traces.export_session(h.sessions, session(ref), turn))

    @app.get("/api/jobs/{job_id}/trace")
    async def job_trace(job_id: str, caller: Any = J):
        return JSONResponse(await traces.export_job(h.jobs, job(job_id, caller)))


def register(mcp: Any, sessions: Any, jobs: Any, guard: Any) -> None:
    """Resources, not tools (the tool list is at its cap): hangar://sessions/<name>/manifest and .../trace,
    hangar://jobs/<id>/manifest and .../trace."""
    from mcp.server.mcpserver.exceptions import ResourceError

    from hangar import traces

    def check(kind: str, ref: str) -> None:
        try:
            guard.check(kind, ref)
        except (KeyError, PermissionError) as e:
            raise ResourceError(str(e).strip("'\"")) from None

    def dump(d: Any) -> str:
        return json.dumps(d, indent=1, default=str)

    @mcp.resource("hangar://sessions/{name}/manifest", name="session-manifest", mime_type="application/json",
                  description="The effective harness manifest (hangar.manifest/v1) of a session's latest turn: "
                              "harness and version, model, instructions, skills, MCP servers, hooks, approvals, "
                              "egress, environment build, resources, budget.")
    async def session_manifest(name: str) -> str:
        check("session", name)
        return dump(for_session(sessions, sessions.get(name)))

    @mcp.resource("hangar://sessions/{name}/trace", name="session-trace", mime_type="application/json",
                  description="The session as OTLP/JSON traces (hangar.trace/v1: GenAI semantic conventions).")
    async def session_trace(name: str) -> str:
        check("session", name)
        return dump(await traces.export_session(sessions, sessions.get(name)))

    if jobs is None:
        return

    @mcp.resource("hangar://jobs/{job_id}/manifest", name="job-manifest", mime_type="application/json",
                  description="The effective harness manifest (hangar.manifest/v1) of a job's latest turn.")
    async def job_manifest(job_id: str) -> str:
        check("job", job_id)
        return dump(for_job(jobs, jobs.get(job_id)))

    @mcp.resource("hangar://jobs/{job_id}/trace", name="job-trace", mime_type="application/json",
                  description="The job as OTLP/JSON traces (hangar.trace/v1): turns, attempts, tools, approvals, "
                              "verify, git, PR and CI.")
    async def job_trace(job_id: str) -> str:
        check("job", job_id)
        return dump(await traces.export_job(jobs, jobs.get(job_id)))
