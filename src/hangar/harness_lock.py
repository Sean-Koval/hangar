"""Harness locks: `hangar.harness-lock/v1` (#133; docs/harness-lock.md, docs/api/harness-lock.schema.json).

A harness is a git directory of native files (the global instructions file, `skills/<name>/`, `native/<runtime>/`
config fragments) plus a small `harness.lock` that pins each of them by content digest, the CLI version, the concrete
model and the binding layer (MCP servers, hooks and the approval mode: names, never values). `hangar harness freeze`
writes one from a turn that went well: a frozen observation of its `hangar.manifest/v1`.

Identity: `harness_digest(lock)` projects the lock onto the manifest's `HARNESS_SECTIONS` and digests that with
`manifest.section_digest`'s canonical form (spec 0014 §2), so a lock frozen from a turn has exactly that turn's
`harness_digest`, and a turn running from the lock reports it again. `name`, `revision`, paths and provenance are not
identity.

Two digests per skill: `digest`, the manifest's declared form (`manifest._dir_digest`: relative paths and contents),
and `tree`, what the sandbox probe reports for the same files (`skills.effective`), so a turn's effective skills can be
compared with a lock without the source (drift, #134).

Plugins (#221): a Claude Code seed plugin is `plugin:<name>@<marketplace>`, its directory at
`plugins/<marketplace>/<its source>` and its marketplace entry in
`plugins/<marketplace>/.claude-plugin/marketplace.json`, which lists exactly the lock's plugins of that marketplace, so
`plugins/<marketplace>` is a directory marketplace Claude Code installs from. Its `digest` is the manifest's
(`plugins.content_digest`: the entry and the directory), its `tree` what the probe reports for the directory
(`probe.plugins`).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from hangar import manifest, people

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet
    from hangar.jobs import JobService
    from hangar.models import Job, Session
    from hangar.sessions import SessionService

SCHEMA = "hangar.harness-lock/v1"
SCHEMA_FILE = "docs/api/harness-lock.schema.json"
LOCK_FILE = "harness.lock"
KINDS = ("guide", "skill", "plugin", "native")
# the runtimes whose native config fragments freeze knows; others are frozen best effort, with a warning
NATIVE_KNOWN = ("claude-code", "codex")
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 8_000_000
_MCP_FIELDS = (("endpoint", "url"), ("header_names", "headers"), ("env_names", "env"), ("command", "command"),
               ("args", "args"))


class FreezeError(Exception):
    """The turn can't be frozen: its content can't be recovered. `gaps` names each piece."""

    def __init__(self, gaps: list[str]):
        super().__init__("cannot freeze: " + "; ".join(gaps))
        self.gaps = gaps


# ====================================================================== digests
def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def files_digest(files: dict[str, bytes]) -> str:
    """`manifest._dir_digest` over files given as {relative posix path: content}: for each file in path order (as
    pathlib sorts them: by path components), the path, a NUL, and the raw SHA-256 of its content."""
    h = hashlib.sha256()
    for rel in sorted(files, key=lambda r: tuple(r.split("/"))):
        h.update(rel.encode() + b"\0")
        h.update(hashlib.sha256(files[rel]).digest())
    return "sha256:" + h.hexdigest()


def _sum_name(rel: str) -> str:
    return "./" + rel


def tree_digest(files: dict[str, bytes]) -> str:
    """What `manifest.probe_script` reports for a skill directory holding `files`: `sha256sum` over each file (in
    byte order of `./path`, as `sort -z` under LC_ALL=C), then the SHA-256 of that listing."""
    lines = []
    for rel in sorted(files, key=lambda r: _sum_name(r).encode()):
        hexd, name = hashlib.sha256(files[rel]).hexdigest(), _sum_name(rel)
        if "\\" in name or "\n" in name:  # GNU sha256sum escapes such names and marks the line
            lines.append("\\" + hexd + "  " + name.replace("\\", "\\\\").replace("\n", "\\n") + "\n")
        else:
            lines.append(hexd + "  " + name + "\n")
    return _sha("".join(lines).encode())


def read_dir(root: Path) -> dict[str, bytes]:
    """{relative posix path: content} of the regular files under `root` (symlinks skipped, as the digests do)."""
    out: dict[str, bytes] = {}
    for f in sorted(root.rglob("*")):
        if f.is_file() and not f.is_symlink():
            out[f.relative_to(root).as_posix()] = f.read_bytes()
    return out


def _kind(cid: str) -> str:
    return cid.partition(":")[0]


def _cname(cid: str) -> str:
    return cid.partition(":")[2]


def plugin_ref(cid: str) -> dict[str, str]:
    """`plugin:<name>@<marketplace>` -> {name, marketplace}."""
    name, _, mkt = _cname(cid).rpartition("@")
    return {"name": name, "marketplace": mkt}


def plugin_digest(entry: dict[str, Any], files: dict[str, bytes]) -> str:
    """A plugin's digest as the manifest computes it (plugins.content_digest), from its entry and its files."""
    from hangar.plugins import content_digest

    return content_digest(entry, files_digest(files))


def marketplace_entries(root: Path, mkt: str) -> dict[str, Any] | None:
    """{plugin name: entry} of `plugins/<mkt>/.claude-plugin/marketplace.json` under a harness directory; None when
    it can't be read or names another marketplace."""
    try:
        data = json.loads((root / "plugins" / mkt / ".claude-plugin" / "marketplace.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("name") != mkt or not isinstance(data.get("plugins"), list):
        return None
    return {e["name"]: e for e in data["plugins"] if isinstance(e, dict) and isinstance(e.get("name"), str)}


def identity(lock: dict[str, Any]) -> dict[str, Any]:
    """The lock projected onto `manifest.HARNESS_SECTIONS`: {section: value}, as `manifest.section_digest` picks
    them from a manifest. Only what identifies the harness: no name, revision, paths or provenance."""
    runtime, model, binding = lock.get("runtime") or {}, lock.get("model") or {}, lock.get("binding") or {}
    comps = lock.get("components") or []
    guide = next((c for c in comps if _kind(c.get("id", "")) == "guide"), None)
    plugins = [plugin_ref(c["id"]) | {"digest": c.get("digest"), "enabled": c.get("enabled", True)}
               for c in comps if _kind(c.get("id", "")) == "plugin"]
    mcp = []
    for x in binding.get("mcp") or []:
        d = {"name": x.get("name"), "transport": x.get("transport")}
        for ours, theirs in _MCP_FIELDS:
            d[theirs] = x.get(ours)
        mcp.append(d)
    out = {
        "harness.name": runtime.get("harness"),
        "harness.locked": runtime.get("version"),
        "model.id": model.get("id"),
        "instructions.global.declared.digest": guide.get("digest") if guide else None,
        "skills.declared[*].{name,digest}": [{"name": _cname(c["id"]), "digest": c.get("digest")}
                                             for c in comps if _kind(c.get("id", "")) == "skill"],
        "plugins[*].{name,marketplace,digest,enabled}": plugins or None,  # null without any, as the manifest's
        "mcp_servers[*].{name,transport,url,headers,env,command,args}": mcp,
        "hooks[*].{event,source,digest}": [{"event": x.get("event"), "source": x.get("source"),
                                           "digest": x.get("digest")} for x in binding.get("hooks") or []],
        "permissions.approvals": (binding.get("permissions") or {}).get("approvals"),
    }
    assert tuple(out) == manifest.HARNESS_SECTIONS, "harness_lock.identity is out of step with HARNESS_SECTIONS"
    return out


def harness_digest(lock: dict[str, Any]) -> str:
    """The lock's identity, `harness_digest`: the same canonical form and value as `manifest.section_digest` over
    `HARNESS_SECTIONS` of a turn that ran this harness."""
    return manifest._sha(manifest._canon(identity(lock)))


# ====================================================================== files
def dump(lock: dict[str, Any]) -> str:
    """The lock as YAML, keys in the schema's order (so diffs stay small)."""
    order = ("schema", "name", "revision", "runtime", "model", "components", "binding", "plugins", "remote")
    body = {k: lock[k] for k in order if k in lock} | {k: v for k, v in lock.items() if k not in order}
    return "# hangar harness lock (docs/harness-lock.md). Written by `hangar harness freeze`; identity is\n" \
           "# harness_digest, printed by `hangar harness check`.\n" + yaml.safe_dump(body, sort_keys=False,
                                                                                    allow_unicode=True, width=120)


def load(path: Path) -> dict[str, Any]:
    """A lock file (or a directory holding `harness.lock`), parsed and checked against the schema."""
    if path.is_dir():
        path = path / LOCK_FILE
    try:
        lock = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e.strerror}") from None
    except yaml.YAMLError as e:
        raise ValueError(f"{path} is not YAML: {e}") from None
    validate(lock)
    return lock




_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^(guide|skill|plugin|native):\S{1,200}$")
_PLUGIN_ID = re.compile(r"^plugin:[A-Za-z0-9][A-Za-z0-9._-]{0,99}@[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def safe_path(p: Any) -> bool:
    """A relative path inside the harness directory: no absolute paths, `..`, backslashes or empty parts."""
    return isinstance(p, str) and bool(p) and not p.startswith("/") and "\\" not in p and "\0" not in p \
        and all(part not in ("", ".", "..") for part in p.split("/"))


def problems(lock: Any) -> list[str]:
    """What's wrong with a lock document (empty: it is a valid `hangar.harness-lock/v1`). The JSON schema in
    docs/api says the same; this is what hangar checks at run time."""
    if not isinstance(lock, dict):
        return ["a harness lock is a mapping"]
    out: list[str] = []
    if lock.get("schema") != SCHEMA:
        out.append(f"schema must be {SCHEMA} (got {lock.get('schema')!r})")
    if not isinstance(lock.get("name"), str) or not _NAME.match(lock["name"]):
        out.append("name: lowercase letters, digits, '.', '_' or '-', at most 63")
    rev = lock.get("revision")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
        out.append("revision: an integer >= 1")
    rt = lock.get("runtime")
    if not isinstance(rt, dict) or not isinstance(rt.get("harness"), str) or not rt["harness"]:
        out.append("runtime.harness: the harness name (claude-code, codex, ...)")
    elif not all(isinstance(rt.get(k), (str, type(None))) for k in ("version", "pack")):
        out.append("runtime.version and runtime.pack: strings or null")
    md = lock.get("model")
    if not isinstance(md, dict) or not all(isinstance(md.get(k), (str, type(None))) for k in ("id", "policy")):
        out.append("model: {id, policy}, strings or null")
    comps = lock.get("components")
    seen_ids: set[str] = set()
    seen_paths: list[str] = []
    if not isinstance(comps, list):
        out.append("components: a list")
        comps = []
    for i, c in enumerate(comps):
        where = f"components[{i}]"
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not _ID.match(c["id"]):
            out.append(f"{where}.id: kind:name, kind one of {', '.join(KINDS)}")
            continue
        where = f"component {c['id']}"
        if c["id"] in seen_ids:
            out.append(f"{where}: listed twice")
        seen_ids.add(c["id"])
        d = c.get("digest")
        if d is None:
            if not c.get("source") or c.get("path") is not None:
                out.append(f"{where}: a component without a digest is a reference: give its source, no path")
        elif not isinstance(d, str) or not _DIGEST.match(d):
            out.append(f"{where}.digest: sha256:<64 hex>")
        if c.get("path") is not None:
            if not safe_path(c["path"]):
                out.append(f"{where}.path: a relative path inside the harness directory")
            elif any(c["path"] == p or c["path"].startswith(p + "/") or p.startswith(c["path"] + "/")
                     for p in seen_paths):
                out.append(f"{where}.path overlaps another component's")
            else:
                seen_paths.append(c["path"])
        elif d is not None:
            out.append(f"{where}: a component with a digest needs its path")
        if c.get("tree") is not None and (_kind(c["id"]) not in ("skill", "plugin")
                                          or not _DIGEST.match(str(c["tree"]))):
            out.append(f"{where}.tree: sha256:<64 hex>, skills and plugins only")
        if _kind(c["id"]) == "plugin":
            if not _PLUGIN_ID.match(c["id"]):
                out.append(f"{where}: plugin:<name>@<marketplace>")
            elif c.get("path") is not None and safe_path(c["path"]):
                mkt = f"plugins/{plugin_ref(c['id'])['marketplace']}"
                if c["path"] != mkt and not c["path"].startswith(mkt + "/"):
                    out.append(f"{where}.path: under {mkt}/ (its marketplace's directory)")
        if "enabled" in c and not isinstance(c["enabled"], bool):
            out.append(f"{where}.enabled: true or false")
    if sum(1 for x in seen_ids if _kind(x) == "guide") > 1:
        out.append("components: at most one guide (the harness's global instructions file)")
    b = lock.get("binding")
    if not isinstance(b, dict):
        out.append("binding: {mcp, hooks, permissions}")
    else:
        for i, x in enumerate(b.get("mcp") or []):
            if not isinstance(x, dict) or not isinstance(x.get("name"), str) or x.get("transport") not in (
                    "http", "stdio"):
                out.append(f"binding.mcp[{i}]: {{name, transport: http|stdio, ...}}")
            elif any(k in x for k in ("headers", "env", "url", "token")):
                out.append(f"binding.mcp[{i}]: names only (endpoint, header_names, env_names), never values")
        for i, x in enumerate(b.get("hooks") or []):
            if not isinstance(x, dict) or not x.get("event") or not x.get("source"):
                out.append(f"binding.hooks[{i}]: {{event, source, digest?}}")
        if not isinstance(b.get("permissions") or {}, dict):
            out.append("binding.permissions: {approvals}")
    pl = lock.get("plugins")
    if not isinstance(pl, dict) or pl.get("autoUpdate") is not False:
        out.append("plugins.autoUpdate must be false: a marketplace update would change the harness under its lock")
    rm = lock.get("remote")
    if not isinstance(rm, list) or not all(isinstance(x, dict) and x.get("id") for x in rm):
        out.append("remote: a list of {id, version}")
    return out


def validate(lock: Any) -> None:
    if errs := problems(lock):
        raise ValueError("invalid harness lock: " + "; ".join(errs))


# ====================================================================== check
def overwrite_problems(root: Path) -> list[str]:
    """Why writing a freeze into `root` would lose work (#251): the directory's own `check` against the lock it holds
    (a file edited by hand, a component missing, a skill directory the lock doesn't pin). `write` replaces component
    paths whole and drops the old lock's other components, so uncommitted edits there would go. Empty when `root`
    holds no lock yet, or matches it."""
    if not (root / LOCK_FILE).exists():
        return []
    return list(check(root)["problems"] or [])


def check(root: Path) -> dict[str, Any]:
    """Recompute every component's digest in a harness directory against its lock (`hangar harness check`; CI in
    the team harness repo). `ok` is false with `problems` naming each mismatch."""
    try:
        lock = load(root)
    except ValueError as e:
        return {"ok": False, "digest": None, "problems": [str(e)], "components": 0}
    errs: list[str] = []
    for c in lock["components"]:
        if c.get("path") is None:
            continue
        target = root / c["path"]
        kind = _kind(c["id"])
        if kind in ("skill", "plugin"):
            if not target.is_dir():
                errs.append(f"{c['id']}: {c['path']}/ is missing")
                continue
            files = read_dir(target)
            if kind == "plugin":
                ref = plugin_ref(c["id"])
                entry = (marketplace_entries(root, ref["marketplace"]) or {}).get(ref["name"])
                if entry is None:
                    errs.append(f"{c['id']}: plugins/{ref['marketplace']}/.claude-plugin/marketplace.json has no "
                                f"entry for {ref['name']}")
                    continue
                got = plugin_digest(entry, files)
            else:
                got = files_digest(files)
            if got != c["digest"]:
                errs.append(f"{c['id']}: {c['path']}/ has {got}, the lock says {c['digest']}")
            elif c.get("tree") and tree_digest(files) != c["tree"]:
                errs.append(f"{c['id']}: its tree digest is {tree_digest(files)}, the lock says {c['tree']}")
        else:
            if not target.is_file():
                errs.append(f"{c['id']}: {c['path']} is missing")
                continue
            got = _sha(target.read_bytes())
            if got != c["digest"]:
                errs.append(f"{c['id']}: {c['path']} has {got}, the lock says {c['digest']}")
    paths = {c["path"] for c in lock["components"] if c.get("path")}
    if (root / "skills").is_dir():  # a directory the lock doesn't pin would be shipped unpinned
        for d in sorted((root / "skills").iterdir()):
            if f"skills/{d.name}" not in paths:
                errs.append(f"skills/{d.name} is not in the lock")
    errs += _unpinned_plugins(root, lock)
    if (root / "native").is_dir():
        for f in sorted((root / "native").rglob("*")):
            if f.is_file() and f.relative_to(root).as_posix() not in paths:
                errs.append(f"{f.relative_to(root).as_posix()} is not in the lock")
    return {"ok": not errs, "digest": harness_digest(lock), "name": lock["name"], "revision": lock["revision"],
            "problems": errs, "components": len(lock["components"])}


def _unpinned_plugins(root: Path, lock: dict[str, Any]) -> list[str]:
    """`plugins/`: each directory a marketplace of the lock's plugins, holding only their directories and a
    marketplace.json that lists exactly them (anything else would be installed unpinned)."""
    if not (root / "plugins").is_dir():
        return []
    errs = []
    by_mkt: dict[str, dict[str, str]] = {}
    for c in lock["components"]:
        if _kind(c["id"]) == "plugin" and c.get("path"):
            ref = plugin_ref(c["id"])
            by_mkt.setdefault(ref["marketplace"], {})[ref["name"]] = c["path"]
    for d in sorted((root / "plugins").iterdir()):
        mine = by_mkt.get(d.name)
        if mine is None:
            errs.append(f"plugins/{d.name} is not in the lock")
            continue
        if f"plugins/{d.name}" in mine.values():
            continue  # the plugin is the marketplace's root directory: its whole content is pinned
        for x in sorted(d.iterdir()):
            rel = f"plugins/{d.name}/{x.name}"
            if x.name != ".claude-plugin" and not any(p == rel or p.startswith(rel + "/") for p in mine.values()):
                errs.append(f"{rel} is not in the lock")
        for f in sorted((d / ".claude-plugin").rglob("*")) if (d / ".claude-plugin").is_dir() else []:
            if f.is_file() and f.name != "marketplace.json":
                errs.append(f"{f.relative_to(root).as_posix()} is not in the lock")
        entries = marketplace_entries(root, d.name)
        if entries is None:
            errs.append(f"plugins/{d.name}/.claude-plugin/marketplace.json is missing or isn't marketplace {d.name}")
        elif extra := sorted(set(entries) - set(mine)):
            errs.append(f"plugins/{d.name}/.claude-plugin/marketplace.json lists {', '.join(extra)}, which the lock "
                        "doesn't pin")
    return errs


# ====================================================================== writing a frozen harness
def _material(lock: dict[str, Any]) -> str:
    """Everything but name and revision: equal means freeze writes the same harness (the revision stays)."""
    return manifest._canon({k: v for k, v in lock.items() if k not in ("name", "revision")})


def next_revision(prev: dict[str, Any] | None, new: dict[str, Any]) -> int:
    """Monotonic: 1 for a new harness; the previous revision when nothing changed, else one more."""
    if prev is None:
        return 1
    return int(prev.get("revision") or 0) + (0 if _material(prev) == _material(new) else 1)


def write(root: Path, lock: dict[str, Any], files: dict[str, bytes], prev: dict[str, Any] | None = None) -> None:
    """Write a frozen harness into `root`: every component's files (a skill's directory replaced whole, so a file
    it no longer has goes), the previous lock's components this one dropped removed, then `harness.lock`. Nothing
    else in `root` is touched, and nothing is committed."""
    validate(lock)
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()

    def inside(rel: str) -> Path:
        if not safe_path(rel):
            raise ValueError(f"refusing to write outside {root}: {rel!r}")
        p = (root / rel).resolve()
        if p != resolved and resolved not in p.parents:
            raise ValueError(f"refusing to write outside {root}: {rel!r}")
        return p

    stale = {c["path"] for c in (prev or {}).get("components") or [] if safe_path(c.get("path"))}
    for c in lock["components"]:
        if c.get("path"):
            stale.add(c["path"])
    for c in [*((prev or {}).get("components") or []), *lock["components"]]:  # their marketplaces' entries too
        if _kind(str(c.get("id"))) == "plugin" and _PLUGIN_ID.match(str(c.get("id"))):
            stale.add(f"plugins/{plugin_ref(c['id'])['marketplace']}/.claude-plugin")
    for rel in sorted(stale):
        p = inside(rel)
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
    if (root / "plugins").is_dir():  # a marketplace the lock no longer has leaves no empty directory behind
        for d in sorted((root / "plugins").rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if d.is_dir() and not d.is_symlink() and not any(d.iterdir()):
                d.rmdir()
    for rel, data in sorted(files.items()):
        p = inside(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / LOCK_FILE).write_text(dump(lock))


# ====================================================================== freeze: a turn's manifest -> a harness
def _text_ok(redact: Any, rel: str, data: bytes, gaps: list[str]) -> None:
    """Every text file freeze writes goes through the broker's redaction again: one it would change holds a secret,
    and is a gap rather than a file in a harness repo."""
    try:
        text = data.decode()
    except UnicodeDecodeError:
        return  # binary: nothing the redaction reads
    if redact(text) != text:
        gaps.append(f"{rel}: holds a value hangar redacts (a stored secret); remove it from the source first")


def _overlay(fleet: Fleet, job_id: str | None) -> dict[str, Any] | None:
    """The experiment overlay a trial job ran with (its skills' files as the experiment read them), if any."""
    from hangar import experiments
    from hangar.models import Job

    if not job_id:
        return None
    job = fleet.store.get(Job, job_id)
    if job is None:
        return None
    exp, var = job.labels.get(experiments.EXP_LABEL), job.labels.get(experiments.VARIANT_LABEL)
    if not exp or not var or fleet.store.kv_get(experiments.KV_TRIAL + job_id) != exp:
        return None
    raw = fleet.store.kv_get(f"{experiments.KV_OVERLAY}{exp}:{var}")
    with contextlib.suppress(ValueError, TypeError):
        return json.loads(raw) if raw else None
    return None


def _source_files(spec: EnvSpec, rel: str) -> dict[str, bytes] | None:
    from hangar.envspec import _contained_path

    try:
        src = _contained_path(spec.source_dir or Path("."), rel)
    except (ValueError, FileNotFoundError, OSError):
        return None
    return read_dir(src) if src.is_dir() else None


def _config_root(h: Any) -> str | None:
    """The harness's config directory under $HOME (`.claude`, `.codex`, `.pi/agent`, `.gemini`)."""
    f = getattr(h, "instructions_file", None) or getattr(h, "skills_dir", None)
    return f.rsplit("/", 1)[0] if f and "/" in f else None


def freeze(fleet: Fleet, m: dict[str, Any], spec: EnvSpec, *, name: str | None = None,
           overlay: dict[str, Any] | None = None) -> dict[str, Any]:
    """A harness directory from a turn's manifest `m`: {lock, digest, files: {path: bytes}, warnings}. Content comes
    from where the manifest says it came from (the environment's skill paths, an experiment overlay's stored files,
    the declared instructions) and must hash to what the turn declared; raises FreezeError naming every piece that
    can't be recovered (a skill the sandbox had changed, truncated instructions, a source edited since)."""
    from hangar.harnesses import HARNESSES

    if (m.get("subject") or {}).get("kind") != "turn":
        raise FreezeError(["freeze needs a turn's captured manifest (this one is what a session or environment "
                           "would run with now)"])
    redact = fleet.broker.redact
    gaps: list[str] = []
    warnings: list[str] = []
    files: dict[str, bytes] = {}
    comps: list[dict[str, Any]] = []
    harness = (m.get("harness") or {}).get("name")
    h = HARNESSES.get(harness or "")
    if not harness:
        gaps.append("harness: the manifest names no harness")
    elif harness not in NATIVE_KNOWN:
        warnings.append(f"{harness}: native config fragments are frozen best effort (claude-code and codex are "
                        "supported)")

    # --- the global instructions file
    glob = ((m.get("instructions") or {}).get("global") or {})
    declared = glob.get("declared")
    if declared:
        want = declared.get("digest")
        candidates = []
        if not declared.get("truncated") and isinstance(declared.get("text"), str):
            candidates.append(declared["text"])
        if overlay and isinstance(overlay.get("instructions"), str):
            candidates.append(overlay["instructions"])
        if spec.agent_config.instructions:
            candidates.append(spec.agent_config.instructions)
        text = next((t for t in candidates if manifest._sha(t) == want), None)
        fname = (h.instructions_file.rsplit("/", 1)[-1] if h and h.instructions_file else "AGENTS.md")
        if text is None:
            why = (f"its text was truncated in the manifest (more than {manifest.TEXT_MAX} characters)"
                   if declared.get("truncated") else "its text in the manifest was redacted or changed")
            gaps.append(f"guide:{fname}: {why}, and no source has the declared digest {want} (the environment's "
                        "instructions changed since the turn)")
        else:
            data = text.encode()
            _text_ok(redact, fname, data, gaps)
            files[fname] = data
            comps.append({"id": f"guide:{fname}", "digest": want, "path": fname})
        eff = glob.get("effective")
        if eff and eff.get("digest") and eff["digest"] != want:
            warnings.append(f"guide:{fname}: the sandbox's file differed from the declared one at turn start "
                            "(hangar's environment notes, an overlay, or a change in the sandbox); the lock pins the "
                            "declared file")

    # --- skills
    probe_ok = bool((m.get("probe") or {}).get("ok"))
    effective = {x.get("name"): x for x in ((m.get("skills") or {}).get("effective") or [])}
    ov_skills = {x.get("source"): x for x in (overlay or {}).get("skills") or [] if x.get("files")}
    declared_names = set()
    for sk in (m.get("skills") or {}).get("declared") or []:
        sname, src, want = sk.get("name"), sk.get("source") or "", sk.get("digest")
        declared_names.add(sname)
        cid = f"skill:{sname}"
        if not sname or not re.fullmatch(r"[\w.-]{1,100}", sname):
            gaps.append(f"skill {sname!r}: not a usable directory name")
            continue
        if src.startswith("git:") and want is None:
            if sk.get("pinned"):
                comps.append({"id": cid, "digest": None, "source": src})
                warnings.append(f"{cid}: a pinned git skill; the lock references {src} rather than holding its files")
            else:
                gaps.append(f"{cid}: an unpinned git skill ({src}) can't be frozen; pin it (@<ref>) in the "
                            "environment and run the turn again")
            continue
        if src.startswith("hangar:"):
            comps.append({"id": cid, "digest": None, "source": src})
            continue
        if not src.startswith("path:") or want is None:
            gaps.append(f"{cid}: its source ({src or 'unknown'}) has no recorded digest to freeze from")
            continue
        got = None
        if (o := ov_skills.get(src)) is not None:  # the experiment's snapshot of the files
            with contextlib.suppress(ValueError, TypeError):
                got = {k: base64.b64decode(v) for k, v in o["files"].items()}
        if got is None or files_digest(got) != want:
            got = _source_files(spec, src[5:])
        if got is None:
            gaps.append(f"{cid}: its source {src[5:]} is gone from environment {spec.name}")
            continue
        if files_digest(got) != want:
            gaps.append(f"{cid}: its source {src[5:]} changed since the turn (declared {want}, now "
                        f"{files_digest(got)})")
            continue
        tree = tree_digest(got)
        eff = effective.get(sname)
        if eff is not None and eff.get("digest") != tree:
            gaps.append(f"{cid}: the sandbox's copy differs from its declared source (changed in the sandbox, e.g. "
                        "by the agent in an earlier turn); freeze a turn from before the change, or restore it")
            continue
        if eff is None and probe_ok and h is not None and h.skills_dir:
            warnings.append(f"{cid}: the sandbox didn't have it at turn start; the lock pins the declared files")
        for rel, data in got.items():
            if len(data) > MAX_FILE_BYTES:
                gaps.append(f"{cid}: {rel} is larger than {MAX_FILE_BYTES // 1_000_000} MB")
            _text_ok(redact, f"skills/{sname}/{rel}", data, gaps)
            files[f"skills/{sname}/{rel}"] = data
        comps.append({"id": cid, "digest": want, "tree": tree, "path": f"skills/{sname}", "source": src})
    for extra in sorted(set(effective) - declared_names - {None}):
        warnings.append(f"skill:{extra}: in the sandbox but not declared (hangar's own, or added there); not frozen")
    if not probe_ok:
        warnings.append("the turn's sandbox wasn't read at turn start, so freeze couldn't check that the skills it "
                        "had were the declared ones")

    # --- plugins (#221): each seed plugin's directory and marketplace entry, from the environment's marketplace
    comps += _freeze_plugins(m, spec, redact, files, gaps, warnings)

    # --- native config fragments: the environment's config files under the harness's own directory
    root = _config_root(h)
    hook_digests = {x.get("source", "").removeprefix("environment:"): x.get("digest")
                    for x in m.get("hooks") or [] if str(x.get("source") or "").startswith("environment:")}
    native = {**{k: v for k, v in spec.files.items() if not k.startswith("/")}, **spec.agent_config.files}
    for path, content in sorted(native.items()):
        if not root or not path.startswith(root + "/") or not harness:
            continue
        rest = path[len(root) + 1:]
        rel = f"native/{harness}/{rest}"
        if not safe_path(rel):
            gaps.append(f"{path}: not a usable path")
            continue
        data = content.encode()
        if path in hook_digests and hook_digests[path] != manifest._sha(content):
            gaps.append(f"native:{harness}/{rest}: changed in environment {spec.name} since the turn (its hooks' "
                        "digest differs)")
            continue
        if path not in hook_digests:
            warnings.append(f"native:{harness}/{rest}: read from environment {spec.name} as it is now (the manifest "
                            "records only settings files' hooks)")
        _text_ok(redact, rel, data, gaps)
        files[rel] = data
        comps.append({"id": f"native:{harness}/{rest}", "digest": _sha(data), "path": rel})

    total = sum(len(v) for v in files.values())
    if total > MAX_TOTAL_BYTES:
        gaps.append(f"the harness is {total // 1_000_000} MB; at most {MAX_TOTAL_BYTES // 1_000_000} MB")

    # --- the binding layer: names, never values (as the manifest has them)
    mcp = []
    for x in m.get("mcp_servers") or []:
        d: dict[str, Any] = {"name": x.get("name"), "transport": x.get("transport")}
        for ours, theirs in _MCP_FIELDS:
            if x.get(theirs) is not None:
                d[ours] = x[theirs]
        if x.get("source") and x["source"] != "environment":
            d["source"] = x["source"]
        mcp.append(d)
    hooks = []
    for x in m.get("hooks") or []:
        d = {"event": x.get("event"), "source": x.get("source")}
        if x.get("digest") is not None:
            d["digest"] = x["digest"]
        hooks.append(d)
    hd, md = m.get("harness") or {}, m.get("model") or {}
    if not hd.get("locked"):
        warnings.append(f"runtime: {harness} has no locked CLI version (baked into the image); the lock can't pin it")
    if not md.get("id"):
        warnings.append("model: the turn ran the harness's own default model; the lock can't pin a concrete id")
    lock = {
        "schema": SCHEMA,
        "name": name or "",
        "revision": 1,
        "runtime": {"harness": harness, "version": hd.get("locked"), "pack": hd.get("pack")},
        "model": {"id": md.get("id"), "policy": md.get("policy")},
        "components": comps,
        "binding": {"mcp": mcp, "hooks": hooks,
                    "permissions": {"approvals": (m.get("permissions") or {}).get("approvals")}},
        "plugins": {"autoUpdate": False},
        "remote": [],
    }
    if not name or not _NAME.match(name):
        gaps.append(f"name {name!r}: lowercase letters, digits, '.', '_' or '-' (--name)")
    if gaps:
        raise FreezeError(gaps)
    redacted = redact(lock)
    if manifest._canon(redacted) != manifest._canon(lock):
        raise FreezeError(["the lock itself holds a value hangar redacts (a stored secret)"])
    validate(lock)
    d = harness_digest(lock)
    if m.get("harness_digest") and d != m["harness_digest"]:  # same canonical form: a mismatch is a hangar bug
        raise FreezeError([f"the frozen lock's harness_digest {d} differs from the turn's {m['harness_digest']}"])
    return {"lock": lock, "digest": d, "files": files, "warnings": warnings}


def _freeze_plugins(m: dict[str, Any], spec: EnvSpec, redact: Any, files: dict[str, bytes], gaps: list[str],
                    warnings: list[str]) -> list[dict[str, Any]]:
    """The `plugin:` components of a turn's seed plugins, their files added to `files` under
    `plugins/<marketplace>/`, with a marketplace.json per marketplace listing exactly the frozen plugins."""
    from hangar import plugins as plugins_mod

    comps: list[dict[str, Any]] = []
    mkts: dict[str, dict[str, Any]] = {}
    seen = {x.get("id"): x for x in (m.get("probe") or {}).get("plugins") or [] if isinstance(x, dict)}
    for pl in m.get("plugins") or []:
        pname, mkt, want = pl.get("name"), pl.get("marketplace"), pl.get("digest")
        cid = f"plugin:{pname}@{mkt}"
        if pl.get("error") or not want or not _PLUGIN_ID.match(cid):
            gaps.append(f"{cid}: the turn recorded no digest for it ({pl.get('error') or 'unreadable'})")
            continue
        decl = next((p for p in spec.plugins if p.name == pname and p.marketplace in (None, mkt)), None)
        try:
            r = plugins_mod.resolved(spec, decl) if decl is not None else None
        except ValueError as e:
            r, why = None, str(e)
        else:
            why = f"environment {spec.name} no longer declares it"
        if r is None or r["marketplace"] != mkt:
            gaps.append(f"{cid}: {why}")
            continue
        if r["dir"] is None:
            gaps.append(f"{cid}: its marketplace entry fetches it from elsewhere at build; only a plugin with a "
                        "relative (./) source in a directory marketplace can be frozen")
            continue
        got = read_dir(r["dir"]) if r["dir"].is_dir() else {}
        if plugin_digest(r["entry"], got) != want:
            gaps.append(f"{cid}: its marketplace changed since the turn (declared {want}, now "
                        f"{plugin_digest(r['entry'], got)})")
            continue
        rel = r["dir"].resolve().relative_to(r["root"].resolve()).as_posix()
        path = f"plugins/{mkt}" + ("" if rel == "." else f"/{rel}")
        mk = mkts.setdefault(mkt, {"owner": (plugins_mod._marketplace(r["root"]) or {}).get("owner"),
                                   "entries": [], "root": False})
        if not safe_path(path) or mk["root"] or (rel == "." and mk["entries"]):
            gaps.append(f"{cid}: {path} is not a usable directory (a marketplace whose root is a plugin holds only it)")
            continue
        mk["root"] = rel == "."
        mk["entries"].append(r["entry"])
        tree = tree_digest(got)
        eff = seen.get(f"{pname}@{mkt}")
        if eff is not None and not eff.get("present"):
            warnings.append(f"{cid}: the sandbox's seed didn't have it (an image built before it was added); the lock "
                            "pins the declared files")
        elif eff is not None and eff.get("digest") != tree:
            warnings.append(f"{cid}: the sandbox's seed copy differed from the declared files; the lock pins the "
                            "declared files")
        for frel, data in got.items():
            if len(data) > MAX_FILE_BYTES:
                gaps.append(f"{cid}: {frel} is larger than {MAX_FILE_BYTES // 1_000_000} MB")
            _text_ok(redact, f"{path}/{frel}", data, gaps)
            files[f"{path}/{frel}"] = data
        comps.append({"id": cid, "digest": want, "tree": tree, "path": path, "enabled": bool(pl.get("enabled", True))})
    for mkt, mk in mkts.items():
        if mk["root"]:
            continue  # the plugin's own directory carries the marketplace's marketplace.json
        doc = {"name": mkt, "owner": mk["owner"] or {"name": mkt}, "plugins": mk["entries"]}
        data = (json.dumps(doc, indent=2) + "\n").encode()
        _text_ok(redact, f"plugins/{mkt}/.claude-plugin/marketplace.json", data, gaps)
        files[f"plugins/{mkt}/.claude-plugin/marketplace.json"] = data
    return comps


def _default_name(env: str | None) -> str:
    n = re.sub(r"[^a-z0-9._-]+", "-", (env or "harness").lower()).strip("-._")
    return (n or "harness")[:63]


def freeze_session(svc: SessionService, s: Session, turn: int | None = None, name: str | None = None
                   ) -> dict[str, Any]:
    """Freeze a session's turn (default: its latest captured one)."""
    m = manifest.for_session(svc, s, turn)
    return _freeze_manifest(svc.fleet, m, s.environment, name)


def freeze_job(jobs: JobService, job: Job, turn: int | None = None, name: str | None = None) -> dict[str, Any]:
    """Freeze a job's turn (1-based in the job's own turns; default its latest)."""
    m = manifest.for_job(jobs, job, turn)
    return _freeze_manifest(jobs.fleet, m, (m.get("environment") or {}).get("name") or job.environment, name)


def _freeze_manifest(fleet: Fleet, m: dict[str, Any], env: str | None, name: str | None) -> dict[str, Any]:
    env_name = (m.get("environment") or {}).get("name") or env
    try:
        spec = fleet.catalog.get(env_name or "")
    except Exception as e:  # noqa: BLE001 - the environment was deleted or renamed
        raise FreezeError([f"environment {env_name}: {e}"]) from None
    return freeze(fleet, m, spec, name=name or _default_name(env_name),
                  overlay=_overlay(fleet, (m.get("subject") or {}).get("job")))


def wire(bundle: dict[str, Any], subject: dict[str, Any] | None = None) -> dict[str, Any]:
    """A frozen harness as JSON: files base64-encoded."""
    return {"lock": bundle["lock"], "harness_digest": bundle["digest"], "warnings": bundle["warnings"],
            "files": {k: base64.b64encode(v).decode() for k, v in sorted(bundle["files"].items())},
            "subject": subject}


def unwire(d: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bytes]]:
    files = {}
    for k, v in (d.get("files") or {}).items():
        if not safe_path(k):
            raise ValueError(f"refusing a file outside the harness directory: {k!r}")
        files[k] = base64.b64decode(v)
    return d["lock"], files


# ====================================================================== REST
def mount(app: Any, h: Any, U: Any, J: Any, owner_filter: Any) -> None:
    """GET /api/sessions/{ref}/harness?turn=N&name=NAME and GET /api/jobs/{id}/harness?turn=N&name=NAME: a turn
    frozen as a harness (lock + files). Owner-scoped like the manifests: a member freezes only their own."""
    from fastapi.responses import JSONResponse

    def gap(e: FreezeError) -> JSONResponse:
        return JSONResponse({"error": str(e), "gaps": e.gaps}, status_code=422)

    @app.get("/api/sessions/{ref}/harness")
    async def session_harness(ref: str, turn: int | None = None, name: str | None = None, _: str = U):
        s = h.sessions.get(ref)
        if not people.visible(s.person):
            raise KeyError(f"no session '{ref}'")
        try:
            b = freeze_session(h.sessions, s, turn, name)
        except FreezeError as e:
            return gap(e)
        return wire(b, {"session": s.name, "turn": turn})

    @app.get("/api/jobs/{job_id}/harness")
    async def job_harness(job_id: str, turn: int | None = None, name: str | None = None, caller: Any = J):
        j = h.jobs.get(job_id, owner=owner_filter(caller))
        if not people.visible(j.person):
            raise KeyError(f"no job '{job_id}'")
        try:
            b = freeze_job(h.jobs, j, turn, name)
        except FreezeError as e:
            return gap(e)
        return wire(b, {"job": j.id, "turn": turn})
