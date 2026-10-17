"""Thaw a harness lock (#134; docs/harness-lock.md#thaw-and-drift): run a session or job *from* a lock, and check on
turn 1 that what the sandbox had matches it.

* **Resolve.** `name`, `name@rN`, `name@sha256:...` or `name@<channel>` against the harness registry: a checkout of
  the team harness repo (`HANGAR_HARNESS_REPO`; `harnesses/<name>/harness.lock`, channels in `channels.yaml`). A
  revision or digest that isn't the current one is looked up in the checkout's git history.
* **Claim.** The lock's CLI version must be the one `harnesses.lock.yaml` pins, or the claim is refused; the model is
  pinned to the lock's concrete id and the approval mode to its binding. The lock's directory is copied out of the
  registry into hangar's data directory (`materialize`: content-addressed, so a later edit to the registry never
  changes a claimed lock) and checked against the lock.
* **Mount (#221).** The sandbox gets the lock's files, not the image's: its instructions file, exactly its skills, and
  its plugins as the Claude Code seed (`mount_files`), root's and read-only to the agent. Everything that reads the
  environment's prefix layer (the manifest, the probe, hangar's Claude Code settings, a hardened turn) reads the lock's
  instead (`effective_spec`); the binding layer (MCP servers, hooks, approvals) is still the environment's, rendered at
  claim.
* **Verify turn 1.** After the turn's manifest is recorded: the lock's skills and plugins against the sandbox's
  effective digests (`tree`), its instructions file and plugins against the declared ones, and its `harness_digest`
  against the manifest's.
  A mismatch is drift: `SessionTurn.drift`, a `harness.drift` event, a warning and a review-packet line; under
  `HANGAR_HARNESS_DRIFT=fail` the turn stops (`failed`, reason `harness_drift`). Later turns aren't checked: what the
  agent changes is its behaviour, not drift.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from hangar import harness_lock

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet
    from hangar.jobs import JobService
    from hangar.models import Job, Sandbox, Session, SessionTurn
    from hangar.sessions import SessionService

log = logging.getLogger("hangar.harness_thaw")

HARNESSES_DIR = "harnesses"
CHANNELS_FILE = "channels.yaml"
DRIFT_EVENT = "harness.drift"
DRIFT_REASON = "harness_drift"
HANGAR_SKILLS = {"hangar-environment", "verify-ui"}  # hangar writes these itself (supervisor.py, computer.py)
_REF = re.compile(r"^(?P<name>[a-z0-9][a-z0-9._-]{0,62})(?:@(?P<at>[A-Za-z0-9:._-]{1,80}))?$")
VERIFY_WAIT_SECONDS = 40  # how long a finished turn 1 waits for its verification under HANGAR_HARNESS_DRIFT=fail
LOCKS_DIR = "harness-locks"  # under the data directory: claimed locks' files, one directory per content
INFO = "thawed"  # sb.info: the lock a sandbox's prefix layer was mounted from
MOUNT_SECONDS = 300


class ThawError(ValueError):
    """A lock that can't be resolved or claimed; the message says what to do."""


@dataclass
class Resolved:
    ref: str
    name: str
    lock: dict[str, Any]
    digest: str
    path: str  # the lock's path in the registry
    commit: str | None = None  # the registry commit it came from, when not the checkout's current file


# ====================================================================== resolve
def registry(settings: Any) -> Path:
    raw = getattr(settings, "harness_repo", None)
    if not raw:
        raise ThawError("no harness registry: set HANGAR_HARNESS_REPO to a checkout of the team harness repo "
                        "(harnesses/<name>/harness.lock)")
    root = Path(str(raw)).expanduser()
    if not root.is_dir():
        raise ThawError(f"HANGAR_HARNESS_REPO {root} is not a directory (a checkout of the team harness repo)")
    return root


def _git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _lock_dir(root: Path, name: str) -> Path:
    for d in (root / HARNESSES_DIR / name, root / name):
        if (d / harness_lock.LOCK_FILE).is_file():
            return d
    raise ThawError(f"no harness '{name}' in the registry ({root / HARNESSES_DIR}/{name}/harness.lock)")


def _channel(root: Path, name: str, channel: str) -> str:
    try:
        doc = yaml.safe_load((root / CHANNELS_FILE).read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ThawError(f"{name}@{channel}: the registry has no readable {CHANNELS_FILE} ({e})") from None
    digest = ((doc.get(name) or {}) if isinstance(doc, dict) else {}).get(channel)
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ThawError(f"{name}@{channel}: {CHANNELS_FILE} has no '{channel}' digest for {name}")
    return digest


def resolve(settings: Any, ref: str) -> Resolved:
    """A lock by `name`, `name@rN`, `name@sha256:...` or `name@<channel>`."""
    m = _REF.match(ref.strip())
    if m is None:
        raise ThawError(f"harness_lock '{ref}': use name, name@rN, name@sha256:<digest> or name@<channel>")
    name, at = m["name"], m["at"]
    root = registry(settings)
    d = _lock_dir(root, name)
    rel = (d / harness_lock.LOCK_FILE).relative_to(root).as_posix()
    try:
        current = harness_lock.load(d)
    except ValueError as e:
        raise ThawError(f"{name}: {e}") from None
    want_rev: int | None = None
    want_digest: str | None = None
    if at is None:
        pass
    elif re.fullmatch(r"r\d+", at):
        want_rev = int(at[1:])
    elif at.startswith("sha256:"):
        want_digest = at
    else:
        want_digest = _channel(root, name, at)

    def ok(lock: dict[str, Any]) -> bool:
        return (want_rev is None or lock.get("revision") == want_rev) and (
            want_digest is None or harness_lock.harness_digest(lock) == want_digest)

    if ok(current):
        return Resolved(ref, name, current, harness_lock.harness_digest(current), rel)
    for commit in (_git(root, "log", "--format=%H", "--", rel) or "").split():  # the registry's history
        text = _git(root, "show", f"{commit}:{rel}")
        with contextlib.suppress(yaml.YAMLError, ValueError, TypeError):
            old = yaml.safe_load(text or "")
            if not harness_lock.problems(old) and ok(old):
                return Resolved(ref, name, old, harness_lock.harness_digest(old), rel, commit)
    raise ThawError(f"{ref}: not in the registry; {name} is at r{current['revision']} "
                    f"({harness_lock.harness_digest(current)})")


# ====================================================================== claim
def pin(fleet: Fleet, ref: str, environment: str, harness: str | None = None, model: str | None = None,
        approvals: str | None = None) -> dict[str, Any]:
    """Resolve and check a lock for a claim in `environment`: what the session or job is pinned to. Raises
    ThawError with what to do when the lock can't run here."""
    from hangar.harnesses import HARNESSES

    r = resolve(fleet.settings, ref)
    lock = r.lock
    h = lock["runtime"]["harness"]
    if harness and harness != h:
        raise ThawError(f"{ref} is a {h} harness; drop harness={harness} or thaw a {harness} lock")
    try:
        spec = fleet.catalog.get(environment)
        spec.pick_harness(h)
    except (KeyError, ValueError) as e:
        raise ThawError(f"{ref}: environment {environment} can't run it: {str(e).strip(chr(39) + chr(34))}") from None
    hd = HARNESSES.get(h)
    locked = hd.version if hd is not None and hd.install else None
    if lock["runtime"].get("version") != locked:
        raise ThawError(f"{ref} pins {h} {lock['runtime'].get('version') or '(no version)'}, but this hangar runs "
                        f"{h} {locked or '(baked into the image)'} (harnesses.lock.yaml): re-freeze the harness from a "
                        f"turn on {locked}, or bump the lock's runtime.version and check it again")
    from hangar import plugins as plugins_mod

    if (pl := [c["id"] for c in lock["components"] if c["id"].startswith("plugin:")]) and h != plugins_mod.HARNESS:
        raise ThawError(f"{ref} enables {', '.join(pl)}, but plugins load only in {plugins_mod.HARNESS}")
    lock_model = (lock.get("model") or {}).get("id")
    if model and lock_model and model != lock_model:
        raise ThawError(f"{ref} pins model {lock_model}; drop model={model} (the model is part of the harness)")
    lock_appr = (lock["binding"].get("permissions") or {}).get("approvals")
    if approvals and lock_appr and approvals != lock_appr:
        raise ThawError(f"{ref} pins approvals {lock_appr}; drop approvals={approvals}")
    have = {n for n, s in spec.mcp.items() if s.harnesses is None or h in s.harnesses}
    if spec.computer and spec.computer.browser_mcp:
        have.add("playwright")
    if missing := [x["name"] for x in lock["binding"].get("mcp") or [] if x.get("name") not in have]:
        raise ThawError(f"{ref} binds MCP server(s) {', '.join(missing)}, which environment {environment} doesn't "
                        "define (credentials and endpoints live in the environment)")
    d, stale = materialize(fleet.settings, r)
    if stale and fleet.settings.harness_drift == "fail":
        raise ThawError(f"{ref}: the registry's files differ from the lock ({'; '.join(stale[:5])}); fix the harness "
                        "directory (`hangar harness check`) or re-freeze it (HANGAR_HARNESS_DRIFT=fail)")
    return {"ref": ref, "name": r.name, "revision": lock["revision"], "digest": r.digest, "harness": h,
            "version": locked, "model": lock_model, "approvals": lock_appr, "commit": r.commit,
            "dir": d.name, "stale": stale,
            "components": [{k: c[k] for k in ("id", "digest", "tree", "enabled") if c.get(k) is not None}
                           for c in lock["components"]]}


# ====================================================================== the lock's files, out of the registry
def _needed(lock: dict[str, Any]) -> list[str]:
    """The paths of a lock's directory a claim needs: its components' and their marketplaces' entries."""
    out = [c["path"] for c in lock["components"] if c.get("path")]
    for c in lock["components"]:
        if c["id"].startswith("plugin:"):
            out.append(f"plugins/{harness_lock.plugin_ref(c['id'])['marketplace']}/.claude-plugin/marketplace.json")
    return out


def _registry_files(root: Path, r: Resolved) -> dict[str, bytes]:
    """{path relative to the harness directory: content} of the files a claim needs, from the checkout's working tree
    (the current lock) or the registry commit the lock was found in."""
    base = Path(r.path).parent.as_posix()
    want = _needed(r.lock)

    def needed(rel: str) -> bool:
        return any(rel == p or rel.startswith(p + "/") for p in want)

    out: dict[str, bytes] = {}
    if r.commit is None:
        hdir = root / base
        for p in want:
            t = hdir / p
            if t.is_dir() and not t.is_symlink():
                out.update({f"{p}/{k}": v for k, v in harness_lock.read_dir(t).items()})
            elif t.is_file() and not t.is_symlink():
                out[p] = t.read_bytes()
        return out
    listing = _git(root, "ls-tree", "-r", "-z", "--name-only", r.commit, "--", base + "/") or ""
    for full in listing.split("\0"):
        rel = full[len(base) + 1:] if full.startswith(base + "/") else None
        if not rel or not needed(rel) or not harness_lock.safe_path(rel):
            continue
        try:
            p = subprocess.run(["git", "-C", str(root), "show", f"{r.commit}:{full}"], capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if p.returncode == 0:
            out[rel] = p.stdout
    return out


def materialize(settings: Any, r: Resolved) -> tuple[Path, list[str]]:
    """Copy a resolved lock's files out of the registry into `<data>/harness-locks/<digest>-<content>/` (written
    once, never changed: a claim keeps what it was given whatever happens to the registry), with the resolved
    `harness.lock`. Returns the directory and what `harness_lock.check` finds wrong with it (empty: the files are
    the lock's; else they were edited without re-freezing, and a thaw reports the difference as drift)."""
    files = _registry_files(registry(settings), r)
    content = hashlib.sha256(harness_lock.files_digest(files).encode()).hexdigest()[:16]
    dest = Path(settings.data_dir) / LOCKS_DIR / f"{r.digest.removeprefix('sha256:')[:32]}-{content}"
    if not (dest / harness_lock.LOCK_FILE).is_file():
        tmp = dest.parent / f".{dest.name}.{uuid.uuid4().hex[:8]}"
        try:
            harness_lock.write(tmp, r.lock, files)
            try:
                os.rename(tmp, dest)
            except OSError:  # another claim wrote the same content first
                if not (dest / harness_lock.LOCK_FILE).is_file():
                    raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    res = harness_lock.check(dest)
    return dest, list(res["problems"])


def lock_dir(settings: Any, p: dict[str, Any]) -> Path | None:
    """A claimed lock's files (None for a pin from before #221, which mounts nothing)."""
    name = p.get("dir")
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{32}-[0-9a-f]{16}", name):
        return None
    d = Path(settings.data_dir) / LOCKS_DIR / name
    return d if (d / harness_lock.LOCK_FILE).is_file() else None


@functools.lru_cache(maxsize=64)
def _load(d: str) -> dict[str, Any]:
    return harness_lock.load(Path(d))  # immutable once written: cached by path


# ====================================================================== the prefix layer, from the lock (#221)
def mounted(fleet: Fleet, sb: Sandbox | None) -> tuple[Path, dict[str, Any]] | None:
    """(directory, lock) when the sandbox's prefix layer comes from a lock."""
    p = (sb.info.get(INFO) if sb is not None else None) or None
    d = lock_dir(fleet.settings, p) if isinstance(p, dict) else None
    if d is None:
        return None
    try:
        return d, _load(str(d))
    except ValueError:
        return None


def effective_spec(fleet: Fleet, sb: Sandbox | None, spec: EnvSpec) -> EnvSpec:
    """The environment as a sandbox mounted from a lock has it: the lock's instructions, exactly its skills (a
    reference, such as hangar's own or a pinned git skill, stays the environment's of that name) and its plugins, read
    from the lock's directory. MCP servers, hooks, credentials and the rest stay the environment's. Code that reads
    the prefix layer from the spec (the manifest, the probe, hangar's Claude Code settings, a hardened turn) goes
    through this; the environment's identity (`spec_digest`) doesn't."""
    from hangar.envspec import Skill, skill_name
    from hangar.plugins import Plugin

    got = mounted(fleet, sb)
    if got is None:
        return spec
    d, lock = got
    comps = lock["components"]
    guide = next((c for c in comps if c["id"].startswith("guide:")), None)
    text = (d / guide["path"]).read_text() if guide and guide.get("path") else None
    by_name = {skill_name(x): x for x in spec.skills}
    skills = []
    for c in comps:
        if not c["id"].startswith("skill:"):
            continue
        name = c["id"][6:]
        if c.get("path"):
            skills.append(Skill(path=c["path"], name=name))
        elif name in by_name:
            skills.append(by_name[name])
    pl = [Plugin(path=f"plugins/{ref['marketplace']}", name=ref["name"], marketplace=ref["marketplace"],
                 enabled=c.get("enabled", True))
          for c in comps if c["id"].startswith("plugin:") and (ref := harness_lock.plugin_ref(c["id"]))]
    return spec.model_copy(update={"source_dir": d, "skills": skills, "plugins": pl,
                                   "agent_config": spec.agent_config.model_copy(update={"instructions": text})})


async def mount_files(fleet: Fleet, prov: Any, sb: Sandbox, harness: str | None) -> None:
    """From fleet.create, after the environment's own setup: write the lock's files into the sandbox. Exactly the
    lock's skills (the environment's others removed; hangar's own stay), its instructions file, and for Claude Code
    its plugins as the seed (`plugins.SEED`, replacing the image's, installed with the sandbox's Claude Code as the
    image build does). Skills and the seed are root's and read-only to the agent; the instructions file follows in
    `seal`, once hangar has added its environment notes. Raises when the sandbox can't be given the lock."""
    from hangar import plugins as plugins_mod
    from hangar.harnesses import HARNESSES

    got = mounted(fleet, sb)
    h = HARNESSES.get(harness or "")
    if got is None or h is None:
        return
    d, lock = got
    comps = lock["components"]
    home = f"/home/{fleet.settings.sandbox_user}"
    q = shlex.quote
    lock_skills = [c for c in comps if c["id"].startswith("skill:")]
    root_cmds: list[str] = []
    if h.skills_dir:
        sroot = f"{home}/{h.skills_dir}"
        keep = {c["id"][6:] for c in lock_skills} | HANGAR_SKILLS
        res = await prov.exec(sb, f"mkdir -p {q(sroot)} && cd {q(sroot)} && for d in */; do "
                                  "[ -d \"$d\" ] && printf '%s\\0' \"${d%/}\"; done; true", timeout=60)
        if res.exit_code != 0:
            raise RuntimeError(f"could not read the sandbox's skills: {res.stderr[-300:]}")
        drop = sorted(x for x in res.stdout.split("\0") if x and x not in keep)
        drop += [c["id"][6:] for c in lock_skills if c.get("path") and c["id"][6:] not in drop]
        if drop:
            res = await prov.exec(sb, f"cd {q(sroot)} && rm -rf -- {' '.join(q(x) for x in drop)}", timeout=60)
            if res.exit_code != 0:
                raise RuntimeError(f"could not remove the environment's skills: {res.stderr[-300:]}")
        for c in lock_skills:
            if not c.get("path"):
                continue
            dst = f"{sroot}/{c['id'][6:]}"
            for rel, data in harness_lock.read_dir(d / c["path"]).items():
                await prov.write_file(sb, f"{dst}/{rel}", data, mode=0o644)
            root_cmds.append(_read_only(dst))
    guide = next((c for c in comps if c["id"].startswith("guide:")), None)
    if h.instructions_file:
        path = f"{home}/{h.instructions_file}"
        if guide and guide.get("path"):
            await prov.write_file(sb, path, (d / guide["path"]).read_bytes(), mode=0o644)
        else:  # the lock has none: neither does the turn
            await prov.exec(sb, f"rm -f -- {q(path)}", timeout=30)
    pl = [c for c in comps if c["id"].startswith("plugin:")]
    if pl and h.name == plugins_mod.HARNESS:
        root_cmds.append(await _seed(prov, sb, d, pl, h))
    if root_cmds:
        res = await prov.exec(sb, "set -eu\n" + "\n".join(root_cmds), timeout=MOUNT_SECONDS, user="root", cwd="/")
        if res.exit_code != 0:
            raise RuntimeError(f"could not mount harness lock {lock['name']}@r{lock['revision']}: "
                               f"{(res.stderr or res.stdout)[-400:]}")


def _read_only(path: str) -> str:
    q = shlex.quote(path)
    return f'if [ "$(id -u)" = 0 ]; then chown -R 0:0 {q}; fi; chmod -R a-w,a+rX {q}'


async def _seed(prov: Any, sb: Sandbox, d: Path, pl: list[dict[str, Any]], h: Any) -> str:
    """Stage the lock's marketplaces in the sandbox and return the root shell that makes them the seed."""
    from hangar import plugins as plugins_mod

    q = shlex.quote
    stage = f"/tmp/hangar-thaw-{uuid.uuid4().hex}"
    mkts: list[str] = []
    for c in pl:
        mkt = harness_lock.plugin_ref(c["id"])["marketplace"]
        if mkt not in mkts:
            mkts.append(mkt)
    for mkt in mkts:
        for rel, data in harness_lock.read_dir(d / "plugins" / mkt).items():
            await prov.write_file(sb, f"{stage}/{mkt}/{rel}", data, mode=0o644)
    seed = plugins_mod.SEED
    claude = f"{h.prefix}/bin/claude" if h.install else "claude"
    lines = [f"rm -rf {seed}", f"mkdir -p {seed}/marketplaces"]
    lines += [f"cp -R {q(f'{stage}/{mkt}')} {q(f'{seed}/marketplaces/{mkt}')}" for mkt in mkts]
    lines.append(f"rm -rf {q(stage)}")
    lines += plugins_mod.seed_commands(claude, mkts, [harness_lock._cname(c["id"]) for c in pl],
                                       f"/tmp/hangar-thaw-home-{uuid.uuid4().hex[:12]}")
    return "\n".join(lines)


async def seal(fleet: Fleet, prov: Any, sb: Sandbox, harness: str | None) -> None:
    """From fleet.create, once hangar has written its environment notes into the instructions file: that file too
    becomes root's and read-only to the agent."""
    from hangar.harnesses import HARNESSES

    h = HARNESSES.get(harness or "")
    got = mounted(fleet, sb)
    if got is None or h is None or not h.instructions_file:
        return
    path = f"/home/{fleet.settings.sandbox_user}/{h.instructions_file}"
    res = await prov.exec(sb, f"if [ -e {shlex.quote(path)} ]; then {_read_only(path)}; fi", timeout=60, user="root",
                          cwd="/")
    if res.exit_code != 0:
        raise RuntimeError(f"could not make the instructions file read-only: {(res.stderr or res.stdout)[-300:]}")


def info(p: dict[str, Any] | None) -> dict[str, Any] | None:
    """What fleet.create records on a sandbox mounted from a claimed lock (sb.info["thawed"])."""
    if not p or not p.get("dir"):
        return None
    return {k: p.get(k) for k in ("name", "revision", "digest", "dir")}


def apply(s: Session, p: dict[str, Any]) -> None:
    """Pin a session to a claimed lock (its harness, model and approvals; the lock itself for turn 1's check)."""
    s.harness_lock = p
    s.harness = p["harness"]
    s.model = p.get("model") or s.model
    if p.get("approvals"):
        s.approvals = p["approvals"]


def manifest_section(s: Session | None) -> dict[str, Any] | None:
    """`harness.lock` in a turn's manifest: which lock the session was thawed from."""
    p = getattr(s, "harness_lock", None) if s is not None else None
    return {"name": p.get("name"), "revision": p.get("revision"), "digest": p.get("digest")} if p else None


# ====================================================================== verify turn 1
def drift(p: dict[str, Any], m: dict[str, Any], turn: SessionTurn | None = None) -> list[dict[str, Any]] | None:
    """What differs between a lock (as pinned) and a turn's manifest: [{component, lock, effective}]; None when it
    can't be told (the sandbox wasn't read)."""
    out: list[dict[str, Any]] = []
    comps = p.get("components") or []
    skills = {c["id"][6:]: c for c in comps if c["id"].startswith("skill:")}
    probe_ok = bool((m.get("probe") or {}).get("ok"))
    if probe_ok:
        eff = {x.get("name"): x.get("digest") for x in (m.get("skills") or {}).get("effective") or []}
        for name, c in skills.items():
            if c.get("tree") is None:
                continue  # a reference (hangar's own, a pinned git skill): its digest isn't the lock's to check
            if eff.get(name) != c["tree"]:
                out.append({"component": f"skill:{name}", "lock": c["tree"], "effective": eff.get(name)})
        for name in sorted(set(eff) - set(skills) - HANGAR_SKILLS - {None}):
            out.append({"component": f"skill:{name}", "lock": None, "effective": eff[name]})
    lock_pl = {c["id"][7:]: c for c in comps if c["id"].startswith("plugin:")}
    decl_pl = {f"{x.get('name')}@{x.get('marketplace')}": x for x in m.get("plugins") or [] if isinstance(x, dict)}
    for pid in sorted(set(lock_pl) | set(decl_pl)):  # what hangar loaded, against the lock
        lk, de = lock_pl.get(pid) or {}, decl_pl.get(pid) or {}
        if lk.get("digest") != de.get("digest") or bool(lk.get("enabled", True)) != bool(de.get("enabled", True)):
            out.append({"component": f"plugin:{pid}", "lock": lk.get("digest"), "effective": de.get("digest")})
    if probe_ok and lock_pl and m.get("harness", {}).get("name") == "claude-code":
        seen = {x.get("id"): x for x in (m.get("probe") or {}).get("plugins") or [] if isinstance(x, dict)}
        for pid, c in lock_pl.items():  # what the sandbox's seed has, against the lock
            got = (seen.get(pid) or {}).get("digest")
            if c.get("tree") and got != c["tree"] and not any(x["component"] == f"plugin:{pid}" for x in out):
                out.append({"component": f"plugin:{pid}", "lock": c["tree"], "effective": got})
    guide = next((c for c in comps if c["id"].startswith("guide:")), None)
    declared = (((m.get("instructions") or {}).get("global") or {}).get("declared") or {}).get("digest")
    if (guide or {}).get("digest") != declared:
        out.append({"component": guide["id"] if guide else "guide", "lock": (guide or {}).get("digest"),
                    "effective": declared})
    reported = turn.harness_version if turn is not None else None
    if reported and p.get("version") and reported != p["version"]:
        out.append({"component": "runtime", "lock": p["version"], "effective": reported})
    if m.get("harness_digest") != p.get("digest"):
        out.append({"component": "harness_digest", "lock": p.get("digest"), "effective": m.get("harness_digest")})
    if not probe_ok and not out:
        return None
    return out


def _line(d: list[dict[str, Any]]) -> str:
    parts = [x["component"] for x in d if x["component"] != "harness_digest"] or ["harness_digest"]
    return ", ".join(parts[:8]) + (f" and {len(parts) - 8} more" if len(parts) > 8 else "")


def verify(svc: SessionService, s: Session, turn: SessionTurn, n: int, m: dict[str, Any]) -> None:
    """From manifest.record, once turn `n`'s manifest is stored: turn 1 of a session thawed from a lock is compared
    with it. Records the result on the turn; never raises."""
    from hangar.events import RunEvent

    p = s.harness_lock
    if not p or n != 1:
        return
    try:
        d = drift(p, m, turn)
        if d is None:
            turn.drift = []
            turn.warnings.append(f"harness {p['name']}@r{p['revision']}: turn 1's sandbox couldn't be read, so drift "
                                 "from the lock wasn't checked")
            return
        turn.drift = d
        if not d:
            return
        fail = svc.settings.harness_drift == "fail"
        text = (f"harness drift from {p['name']}@r{p['revision']}: {_line(d)} differ from the lock"
                + ("; stopping the turn (HANGAR_HARNESS_DRIFT=fail)" if fail else ""))
        turn.warnings.append(text)
        svc.activity.append(s.id, [RunEvent(kind="progress", turn=n, run_id=turn.run_id or "", text=text,
                                            data={"source": "hangar", "event": DRIFT_EVENT, "warning": DRIFT_REASON,
                                                  "harness": p["name"], "revision": p["revision"],
                                                  "lock_digest": p.get("digest"), "drift": d,
                                                  "action": "fail" if fail else "warn"})])
        log.info("session %s: %s", s.id, text)
    except Exception:  # noqa: BLE001 - checking never fails the turn by accident
        log.warning("session %s: could not compare turn 1 with its harness lock", s.id, exc_info=True)


def stops(svc: SessionService, s: Session, turn: SessionTurn) -> bool:
    """Whether the turn must stop for drift (HANGAR_HARNESS_DRIFT=fail)."""
    return bool(s.harness_lock and turn.drift and svc.settings.harness_drift == "fail")


async def stops_after(svc: SessionService, s: Session, turn: SessionTurn) -> bool:
    """For a turn 1 that finished before its check ran: wait for it (bounded) under HANGAR_HARNESS_DRIFT=fail."""
    if not s.harness_lock or svc.settings.harness_drift != "fail" or not s.turns or s.turns[0] is not turn:
        return False
    for _ in range(int(VERIFY_WAIT_SECONDS / 0.1)):
        if turn.drift is not None:
            break
        await asyncio.sleep(0.1)
    return stops(svc, s, turn)


# ====================================================================== jobs and the review packet
def job_line(turn: SessionTurn) -> str:
    return f"stopped: the sandbox drifted from its harness lock ({_line(turn.drift or [])}); HANGAR_HARNESS_DRIFT=fail"


def packet_line(jobs: JobService, job: Job) -> str | None:
    """The review packet's harness line: the lock the job ran from, and turn 1's drift."""
    p = getattr(job, "harness_lock", None)
    if not p:
        return None
    s = jobs._session(job)
    t = s.turns[0] if s is not None and s.turns else None
    head = f"- **Harness:** `{p['name']}@r{p['revision']}` ({p['digest'][:19]}…)"
    if t is None or t.drift is None:
        return head + ": turn 1 not checked against the lock"
    if not t.drift:
        return head + ": turn 1 matched the lock"
    return head + f": **drift** on turn 1 ({_line(t.drift)})"


# ====================================================================== upstream drift: `hangar harness outdated`
def outdated(fleet: Fleet, ref: str) -> dict[str, Any]:
    """Report only: the lock's CLI version against harnesses.lock.yaml, and each skill against its source in the
    environments that declare it (a `path:` skill whose files there no longer hash to the lock's)."""
    from hangar.envspec import _contained_path
    from hangar.harnesses import HARNESSES

    r = resolve(fleet.settings, ref)
    lock = r.lock
    h = lock["runtime"]["harness"]
    hd = HARNESSES.get(h)
    current = hd.version if hd is not None and hd.install else None
    out: dict[str, Any] = {"name": r.name, "revision": lock["revision"], "digest": r.digest,
                           "runtime": {"harness": h, "lock": lock["runtime"].get("version"), "current": current,
                                       "outdated": lock["runtime"].get("version") != current},
                           "components": []}
    specs = []
    with contextlib.suppress(Exception):
        specs = [fleet.catalog.get(n) for n in fleet.catalog.names()]
    for c in lock["components"]:
        if not c["id"].startswith("skill:"):
            continue
        src = c.get("source") or ""
        row: dict[str, Any] = {"id": c["id"], "source": src or None, "status": "not checked"}
        if src.startswith("path:"):
            seen = []
            for spec in specs:
                if not any(sk.path == src[5:] for sk in spec.skills):
                    continue
                with contextlib.suppress(ValueError, FileNotFoundError, OSError):
                    d = harness_lock.files_digest(harness_lock.read_dir(_contained_path(spec.source_dir or Path("."),
                                                                                        src[5:])))
                    seen.append({"environment": spec.name, "digest": d, "changed": d != c.get("digest")})
            row["environments"] = seen
            row["status"] = ("changed upstream" if any(x["changed"] for x in seen) else "up to date") if seen else \
                "no environment declares it"
        elif src.startswith("git:"):
            row["status"] = "pinned git ref (not fetched)"
        out["components"].append(row)
    out["outdated"] = out["runtime"]["outdated"] or any(x["status"] == "changed upstream" for x in out["components"])
    return out


# ====================================================================== REST
def mount(app: Any, h: Any, U: Any) -> None:
    """GET /api/harness-locks/{ref} (a lock resolved from the registry, with its harness_digest) and
    GET /api/harness-locks/{ref}/outdated. Team content: any person's login reads them."""
    from fastapi.responses import JSONResponse

    @app.get("/api/harness-locks/{ref}")
    async def get_lock(ref: str, _: str = U):
        try:
            r = resolve(h.settings, ref)
        except ThawError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return {"ref": ref, "name": r.name, "harness_digest": r.digest, "path": r.path, "commit": r.commit,
                "lock": r.lock}

    @app.get("/api/harness-locks/{ref}/outdated")
    async def get_outdated(ref: str, _: str = U):
        try:
            return outdated(h.fleet, ref)
        except ThawError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
