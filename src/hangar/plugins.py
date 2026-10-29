"""Claude Code plugins in a read-only seed directory: the harness's prefix layer (#136; docs/harnesses.md#plugins).

An environment declares the plugins its Claude Code sandboxes load (`plugins:`, each a plugin of a local marketplace
directory next to its spec). They are installed at image-build time into a **seed directory**, `SEED`, with Claude
Code's own `claude plugin marketplace add` / `claude plugin install` under `CLAUDE_CODE_PLUGIN_CACHE_DIR=<seed>`, and
the image carries it root's and read-only. At run time `CLAUDE_CODE_PLUGIN_SEED_DIR` points Claude Code at it, and
hangar's own `--settings` (HANGAR_CLAUDE_SETTINGS, or a hardened turn's settings file) enables exactly the declared
plugins. Verified with 2.1.296 (tests/test_harden_real_clis.py):

* the seed's layout is `~/.claude/plugins`'s: `known_marketplaces.json`, `marketplaces/<name>/`, `cache/...`. A
  `directory` marketplace is not copied into `marketplaces/`: its recorded location is where it was added from, and
  Claude Code skips a seed marketplace it can't find under `<seed>/marketplaces/`. So the build puts each marketplace
  at `<seed>/marketplaces/<name>` and adds it from there; its plugins (relative sources) then load in place, and the
  `init` event lists each with a path under the seed;
* a seed marketplace overrides the user's entry of the same name, whatever `~/.claude/plugins` (the agent's) says,
  and `claude plugin marketplace update|remove` on it fails, naming the seed;
* Claude Code never writes to the seed and forces `autoUpdate` off for seed marketplaces; `DISABLE_AUTOUPDATER=1`
  (the harness's env) stops plugin auto-update as well;
* `strictKnownMarketplaces` and `disableSideloadFlags` count only in managed settings, not in `--settings` (and
  `disableSideloadFlags` would refuse the hardened turn's own `--mcp-config`). Hangar doesn't write managed settings,
  so a hardened turn shuts the agent's plugins out another way: `--setting-sources ''` (only hangar's
  `enabledPlugins`) and `CLAUDE_CODE_PLUGIN_CACHE_DIR` on a fresh directory per turn (`RUN_CACHE`, harden.py), so
  the agent's `~/.claude/plugins` is never read at all. Claude Code must be able to write that directory (it records
  the seed's marketplaces there; with a read-only one, verified as a non-root user, no plugin loads), so it can't be
  one of hangar's: it is a new, unguessable path Claude Code creates as it starts, which nothing the agent left
  behind can be in.

A plugin can also come from a git marketplace (#222, KAN-76): `{git, ref, path?, name, marketplace?}`. The control
plane fetches it before the image is built (`prefetch`, from Fleet.build), from the git gateway's upstream for its
host (HANGAR_GATEWAY_UPSTREAMS) with the gateway's own credential, never a sandbox's, into `<data>/plugin-git/`.
`ref` must be a full commit id: a branch or a tag can move between two builds, so it is refused, naming the commit
it points at now, the way a floating model alias is (#167). The commit's tree is then a local marketplace like any
other, and the manifest records its `git` and `commit` beside the digest. The image build itself never runs git.

Codex has no seed: an environment's plugins load in Claude Code only, and Codex's skills stay in ~/.codex/skills
(#139). A Codex sandbox run from a harness lock installs the lock's plugins from their Codex forms (codex_plugins.py,
#244).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet

SEED = "/opt/hangar/claude-seed"
HARNESS = "claude-code"
# Seed directories and `syncClaudeAiPlugins` need Claude Code 2.1.273 or later (code.claude.com/docs/en/plugins/org);
# the canary refuses a lock below it (canary.FLOORS).
MIN_VERSION = "2.1.273"
# A hardened turn's plugin cache: expanded by bash builtins alone as the harness starts (driver.LAUNCH evaluates the
# environment, and nothing on the agent's PATH may run before the harness does), so each turn gets a new directory.
RUN_CACHE = "/tmp/hangar-claude-plugins.$RANDOM$RANDOM$RANDOM.$$"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_GIT_URL = re.compile(r"^(https://[^/@\s]+/|git@[^:/\s]+:|ssh://git@[^/\s]+/|file:///)\S+$")
GIT_TIMEOUT = 300


class Plugin(BaseModel):
    """One Claude Code plugin an environment loads from its seed."""

    # a marketplace directory (.claude-plugin/marketplace.json): relative to the spec file, or, with `git`, to the
    # repo's root (unset: the root itself)
    path: str | None = None
    # --- #222: a git marketplace, fetched before the image build through the git gateway's upstream, pinned to `ref`
    git: str | None = None
    ref: str | None = None  # with `git`: a full commit id (a branch or a tag is refused, naming its commit)
    name: str = Field(pattern=f"^{_NAME.pattern}$")  # the plugin's name in that marketplace
    # the marketplace's name; read from its marketplace.json when unset (and checked against it at build)
    marketplace: str | None = Field(None, pattern=f"^{_NAME.pattern}$")
    enabled: bool = True  # false: installed in the seed, not loaded (`"<name>@<marketplace>": false`)

    @model_validator(mode="after")
    def _source(self) -> Plugin:
        if self.git is None:
            if self.ref is not None:
                raise ValueError(f"plugin {self.name}: ref goes with git")
            if not self.path:
                raise ValueError(f"plugin {self.name}: give path (a marketplace directory) or git and ref")
            return self
        if not _GIT_URL.match(self.git.strip()):
            raise ValueError(f"plugin {self.name}: git must be an https://, git@host:, ssh://git@ or file:/// URL "
                             "with no credential in it (hangar fetches it with the git gateway's own)")
        if not self.ref:
            raise ValueError(f"plugin {self.name}: git needs ref, the full commit id to install")
        return self

    @property
    def id(self) -> str:
        return f"{self.name}@{self.marketplace}"


# ====================================================================== git marketplaces (#222)
_git_cache: Path | None = None


def configure_git(cache: Path) -> None:
    """Where fetched git marketplaces are kept (Fleet sets `<data>/plugin-git`)."""
    global _git_cache
    _git_cache = cache


def git_cache() -> Path:
    return _git_cache or Path.home() / ".hangar" / "plugin-git"


def safe_url(url: str) -> str:
    return re.sub(r"//[^/@]*@", "//", url)


def _key(url: str) -> str:
    return hashlib.sha256(url.strip().encode()).hexdigest()[:16]


def pinned(p: Plugin) -> bool:
    return bool(_SHA.fullmatch((p.ref or "").lower()))


def git_tree(p: Plugin) -> Path:
    """Where a git plugin's commit is laid out (complete once the directory exists)."""
    return git_cache() / "trees" / _key(p.git or "") / (p.ref or "").lower()


def _gitrun(args: list[str], env: dict[str, str] | None = None) -> tuple[bool, bytes]:
    try:
        r = subprocess.run(["git", *args], capture_output=True, timeout=GIT_TIMEOUT,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e).encode()
    return r.returncode == 0, r.stdout if r.returncode == 0 else r.stderr.strip()[-400:]


def pin_error(p: Plugin, url: str, env: dict[str, str] | None = None) -> str:
    """Why `p.ref` isn't a pin, naming the commit it points at now when the remote says (#167's way for models)."""
    ref = p.ref or ""
    ok, out = _gitrun(["ls-remote", "--", url, f"refs/heads/{ref}", f"refs/tags/{ref}", ref], env)
    found: dict[str, str] = {}
    for line in out.decode(errors="replace").splitlines() if ok else []:
        sha, _, name = line.partition("\t")
        found[name.strip()] = sha.strip()
    head = found.get(f"refs/heads/{ref}") or (found.get(ref) if ref.startswith("refs/heads/") else None)
    tag = found.get(f"refs/tags/{ref}^{{}}") or found.get(f"refs/tags/{ref}")
    where = f"plugin {p.name}: ref `{ref}` of {safe_url(p.git or url)}"
    if head:
        return (f"{where} is a branch, which can move between two builds: an environment pins a plugin to a "
                f"commit. Pin the one it names now: ref: {head}")
    if tag:
        return (f"{where} is a tag, which can be moved: an environment pins a plugin to a commit. Pin the one it "
                f"names now: ref: {tag}")
    if re.fullmatch(r"[0-9a-fA-F]{4,39}", ref):
        return f"{where} is an abbreviated commit: pin the full commit id (git rev-parse {ref})"
    return f"{where} isn't a full commit id: an environment pins a plugin to a commit (git rev-parse <ref>)"


def fetch(p: Plugin, url: str | None = None, env: dict[str, str] | None = None) -> Path:
    """Fetch a git plugin's commit into the cache and lay out its tree (git_tree); returns that directory. Blocking.
    `url`: where to fetch from (the gateway's upstream for the spec's URL); `env`: git's environment, carrying the
    control plane's credential (never on a command line or in a config file)."""
    url = url or (p.git or "").strip()
    ref = (p.ref or "").lower()
    if not pinned(p):
        raise ValueError(pin_error(p, url, env))
    tree = git_tree(p)
    if tree.is_dir():
        return tree
    mirror = git_cache() / "repos" / f"{_key(p.git or '')}.git"
    if not (mirror / "HEAD").exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        ok, out = _gitrun(["init", "-q", "--bare", str(mirror)])
        if not ok:
            raise ValueError(f"plugin {p.name}: could not make its git cache: {out.decode(errors='replace')}")
    have = ["-C", str(mirror), "cat-file", "-e", f"{ref}^{{commit}}"]
    ok, out = True, b""
    if not _gitrun(have)[0]:
        ok, out = _gitrun(["-C", str(mirror), "fetch", "-q", "--depth", "1", "--", url, ref], env)
        if not _gitrun(have)[0]:  # a server that won't serve a commit by id: its branches and tags, then look again
            ok, out = _gitrun(["-C", str(mirror), "fetch", "-q", "--", url, "+refs/heads/*:refs/hangar/heads/*",
                               "+refs/tags/*:refs/hangar/tags/*"], env)
    if not _gitrun(have)[0]:
        why = f"git fetch failed: {out.decode(errors='replace')}" if not ok else "no such commit"
        raise ValueError(f"plugin {p.name}: {safe_url(p.git or url)} has no commit {ref} ({why})")
    ok, out = _gitrun(["-C", str(mirror), "archive", "--format=tar", ref])
    if not ok:
        raise ValueError(f"plugin {p.name}: could not read commit {ref}: {out.decode(errors='replace')}")
    tmp = tree.with_name(f"{tree.name}.tmp{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(out)) as t:
        t.extractall(tmp, filter="data")  # nothing outside the tree, no device files
    try:
        tmp.rename(tree)
    except OSError:  # another build laid it out first: the same commit, the same tree
        shutil.rmtree(tmp, ignore_errors=True)
    return tree


def upstream_url(fleet: Fleet, p: Plugin) -> str:
    """The URL the git gateway forwards the plugin's repo to (its host one of HANGAR_GATEWAY_UPSTREAMS), or why not."""
    from hangar.gateway import parse_remote

    ups = {h.lower(): base for h, base in fleet.settings.gateway_upstreams.items()}
    remote = parse_remote(p.git, list(ups))
    if remote is None:
        raise ValueError(f"plugin {p.name}: {safe_url(p.git or '')} isn't on one of the git gateway's upstreams "
                         f"({', '.join(sorted(ups)) or 'none'}; HANGAR_GATEWAY_UPSTREAMS): hangar fetches plugins "
                         "only through it")
    return f"{ups[remote[0]].rstrip('/')}/{remote[1]}.git"


async def prefetch(fleet: Fleet, spec: EnvSpec) -> None:
    """Before an image build: each git plugin's commit, fetched on the control plane from the gateway's upstream with
    the gateway's credential (the team harness repo's way, harness_repo.py), never a sandbox's token. A local
    `file:///` repo is read as it is. Raises ValueError: a floating ref, a host the gateway doesn't serve, a missing
    commit."""
    from hangar.harness_repo import _auth_env

    for p in spec.plugins:
        if p.git is None or (pinned(p) and git_tree(p).is_dir()):
            continue
        if p.git.startswith("file:///"):
            url, env = p.git, {}
        else:
            url = upstream_url(fleet, p)
            env = await _auth_env(fleet, p.git)
        await asyncio.to_thread(fetch, p, url, env)


def pending(spec: EnvSpec) -> list[str]:
    """The remote git plugins whose commit isn't fetched yet (prefetch fetches them when the image is built)."""
    return [p.name for p in spec.plugins
            if p.git and not p.git.startswith("file:///") and pinned(p) and not git_tree(p).is_dir()]


def applies(spec: EnvSpec, harness: str | None) -> bool:
    return bool(spec.plugins) and harness == HARNESS


def _root(spec: EnvSpec, p: Plugin) -> Path:
    if p.git is not None:  # --- #222: the commit's tree, laid out by prefetch (a local file:/// repo: here)
        tree = git_tree(p)
        if not tree.is_dir():
            if not pinned(p):
                raise ValueError(pin_error(p, p.git))
            if not p.git.startswith("file:///"):
                raise ValueError(f"plugin {p.name}: {safe_url(p.git)}@{p.ref} isn't fetched yet: hangar fetches it "
                                 "through the git gateway when it builds the image")
            tree = fetch(p)
        base = tree.resolve()
    else:
        base = (spec.source_dir or Path(".")).resolve()
    rel = p.path or "."
    if Path(rel).is_absolute():
        raise ValueError(f"plugin {p.name}: path must be relative to the spec directory: {p.path}")
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"plugin {p.name}: path escapes the spec directory: {p.path}")
    return target


def _marketplace(root: Path) -> dict[str, Any] | None:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
        return data if isinstance(data, dict) else None
    return None


def resolved(spec: EnvSpec, p: Plugin, strict: bool = False) -> dict[str, Any]:
    """What one declared plugin is: its marketplace's name, its entry, version and source directory (control plane).
    `strict` (the image build): a marketplace or plugin that can't be read is an error, not a gap."""
    root = _root(spec, p)
    data = _marketplace(root)
    if data is None and strict:
        raise ValueError(f"plugin {p.name}: {p.path} has no readable .claude-plugin/marketplace.json")
    data = data or {}
    mkt = data.get("name") if isinstance(data.get("name"), str) else None
    if p.marketplace and mkt and p.marketplace != mkt:
        raise ValueError(f"plugin {p.name}: {p.path} is marketplace '{mkt}', not '{p.marketplace}'")
    mkt = p.marketplace or mkt
    if not mkt or not _NAME.fullmatch(mkt):
        raise ValueError(f"plugin {p.name}: {p.path}: no valid marketplace name")
    entry = next((e for e in data.get("plugins") or [] if isinstance(e, dict) and e.get("name") == p.name), None)
    if entry is None and strict:
        raise ValueError(f"plugin {p.name}: marketplace '{mkt}' ({p.path}) has no plugin of that name")
    entry = entry or {}
    src = entry.get("source")
    rel = src.removeprefix("./").rstrip("/") if isinstance(src, str) and src.startswith("./") else None
    if rel is not None and (Path(rel).is_absolute() or ".." in Path(rel).parts):
        raise ValueError(f"plugin {p.name}: its source {src!r} leaves the marketplace")
    pdir = root / rel if rel else None
    version = entry.get("version")
    if pdir is not None and not version:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            version = json.loads((pdir / ".claude-plugin" / "plugin.json").read_text()).get("version")
    version = str(version) if version else None
    if rel is not None:  # loaded in place from the seed's copy of the marketplace (verified with 2.1.296)
        path = f"{SEED}/marketplaces/{mkt}" + (f"/{rel}" if rel else "")
    else:  # fetched at build: from the seed's plugin cache
        path = f"{SEED}/cache/{mkt}/{p.name}" + (f"/{version}" if version else "")
    return {"id": f"{p.name}@{mkt}", "name": p.name, "marketplace": mkt, "version": version, "path": path,
            "root": root, "dir": pdir, "entry": entry}


def enabled(spec: EnvSpec, harness: str | None = HARNESS) -> dict[str, bool]:
    """`enabledPlugins` for hangar's settings: every declared plugin, true or false."""
    if not applies(spec, harness):
        return {}
    out: dict[str, bool] = {}
    for p in spec.plugins:
        try:
            out[resolved(spec, p)["id"]] = p.enabled
        except ValueError:
            if p.marketplace:
                out[p.id] = p.enabled
    return out


def settings(spec: EnvSpec, harness: str | None = HARNESS) -> dict[str, Any]:
    """What hangar adds to Claude Code's `--settings` for the seed's plugins ({} without any): exactly the declared
    plugins, and none synced from a claude.ai account (`syncClaudeAiPlugins`, 2.1.273+)."""
    ids = enabled(spec, harness)
    return {"enabledPlugins": ids, "syncClaudeAiPlugins": False} if ids else {}


def env(spec: EnvSpec, harness: str | None) -> dict[str, str]:
    """The harness's environment for the seed: where it is, and wait for its plugins before turn 1 (a `-p` run
    installs in the background otherwise)."""
    if not applies(spec, harness):
        return {}
    return {"CLAUDE_CODE_PLUGIN_SEED_DIR": SEED, "CLAUDE_CODE_SYNC_PLUGIN_INSTALL": "1"}


def turn_env(spec: EnvSpec, harness: str | None, run_env: dict[str, str]) -> dict[str, str]:
    """HANGAR_CLAUDE_SETTINGS with the seed's plugins merged in (the run commands pass it as `--settings`), for a
    turn whose harness reads it; {} when there is nothing to add."""
    from hangar.verifyloop import CLAUDE_ENV

    add = settings(spec, harness)
    if not add:
        return {}
    cur: dict[str, Any] = {}
    if raw := run_env.get(CLAUDE_ENV):
        with contextlib.suppress(ValueError):
            loaded = json.loads(raw)
            cur = loaded if isinstance(loaded, dict) else {}
    return {CLAUDE_ENV: json.dumps({**cur, **add, "enabledPlugins": {**(cur.get("enabledPlugins") or {}),
                                                                      **add["enabledPlugins"]}})}


def pin_marketplaces(obj: Any) -> Any:
    """A Claude Code settings document with `"autoUpdate": false` on each `extraKnownMarketplaces` entry: a
    marketplace update must not change the treatment mid-experiment (the seed's are off already)."""
    if not isinstance(obj, dict) or not isinstance(obj.get("extraKnownMarketplaces"), dict):
        return obj
    pinned = {k: ({**v, "autoUpdate": False} if isinstance(v, dict) else v)
              for k, v in obj["extraKnownMarketplaces"].items()}
    return {**obj, "extraKnownMarketplaces": pinned}


# ====================================================================== the image build
def _files(root: Path) -> list[Path]:
    return sorted(f for f in root.rglob("*") if f.is_file() and not f.is_symlink() and f.resolve().is_relative_to(
        root.resolve()))


def seed_commands(claude: str, marketplaces: list[str], installs: list[str], home: str) -> list[str]:
    """Shell that turns `<seed>/marketplaces/<name>/` (each already in place) into Claude Code's seed: each
    marketplace added from there and the plugins installed, with Claude Code at `claude` and a throwaway `home`; the
    seed then root's (when run as root) and read-only to everyone else. The image build's seed stage and a thawed
    harness lock's claim (harness_thaw.mount, #221) run the same commands."""
    q = shlex.quote
    lines = [f"export HOME={home} CLAUDE_CODE_PLUGIN_CACHE_DIR={SEED} DISABLE_AUTOUPDATER=1 "
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", 'mkdir -p "$HOME"']
    lines += [f"{q(claude)} plugin marketplace add {q(f'{SEED}/marketplaces/{mkt}')}" for mkt in marketplaces]
    lines += [f"{q(claude)} plugin install {q(i)}" for i in installs]
    lines += [f"test -f {SEED}/known_marketplaces.json", 'rm -rf "$HOME"',
              f'if [ "$(id -u)" = 0 ]; then chown -R root:root {SEED}; fi', f"chmod -R u+rwX,go+rX,go-w {SEED}"]
    return lines


def build(spec: EnvSpec, claude: str) -> tuple[str, dict[str, bytes]]:
    """(Dockerfile stage `hangar-claude-seed`, its build-context files): each marketplace copied to
    `<seed>/marketplaces/<name>`, added from there and its declared plugins installed, with Claude Code at `claude`
    (the pack stage's), nothing of it left in the stage but the seed."""
    files: dict[str, bytes] = {}
    roots: dict[str, Path] = {}
    installs: list[str] = []
    for p in spec.plugins:
        r = resolved(spec, p, strict=True)
        if roots.setdefault(r["marketplace"], r["root"]) != r["root"]:
            raise ValueError(f"plugins: marketplace '{r['marketplace']}' comes from two directories")
        if r["id"] not in installs:
            installs.append(r["id"])
    copies: list[str] = []
    for mkt, root in roots.items():
        for f in _files(root):
            files[f"plugins/{mkt}/{f.relative_to(root).as_posix()}"] = f.read_bytes()
        copies.append(f"COPY plugins/{mkt}/ {SEED}/marketplaces/{mkt}/")
    lines = ["#!/bin/bash", "set -euxo pipefail", *seed_commands(claude, list(roots), installs, "/tmp/seed-home")]
    files["plugins/seed.sh"] = ("\n".join(lines) + "\n").encode()
    stage = (f"FROM pack-{HARNESS} AS hangar-claude-seed\n" + "\n".join(copies) + "\n"
             "COPY plugins/seed.sh /tmp/seed.sh\nRUN bash /tmp/seed.sh\n")
    return stage, files


# ====================================================================== the manifest
def _digest(r: dict[str, Any]) -> tuple[str | None, int | None]:
    """The plugin's content digest over its declared inputs: its marketplace entry and its directory (the whole
    marketplace's when the entry points elsewhere). Claude Code's writable caches in ~/.claude are not inputs."""
    from hangar.manifest import _dir_digest

    d = r["dir"] if r["dir"] is not None else r["root"]
    if not d.is_dir():
        return None, None
    dig, n = _dir_digest(str(d.resolve()))
    return content_digest(r["entry"], dig), n


def content_digest(entry: dict[str, Any], dir_digest: str) -> str:
    """A plugin's digest from its marketplace entry and its directory's `_dir_digest` (a harness lock recomputes it
    the same way from its files, #221)."""
    e = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(f"{e}\n{dir_digest}".encode()).hexdigest()


def _origin(p: Plugin) -> dict[str, Any]:
    """--- #222: where a git plugin came from: its repo (no credential) and the commit it is pinned to."""
    return {"git": safe_url(p.git), "commit": (p.ref or "").lower() or None} if p.git else {}


def manifest(spec: EnvSpec, harness: str | None) -> list[dict[str, Any]] | None:
    """hangar.manifest/v1 `plugins` (part of harness_digest): null without any, else each declared plugin. `effective`
    is whether hangar's settings load it for the harness (a hardened turn's included: they come from the seed)."""
    if not spec.plugins:
        return None
    out = []
    for p in spec.plugins:
        try:
            r = resolved(spec, p)
        except ValueError as e:
            out.append({"name": p.name, "marketplace": p.marketplace, "version": None, "path": None, "digest": None,
                        "enabled": p.enabled, "effective": False, "source": "seed", **_origin(p),
                        "error": str(e)[:200]})
            continue
        dig, n = _digest(r)
        out.append({"name": r["name"], "marketplace": r["marketplace"], "version": r["version"], "path": r["path"],
                    "digest": dig, "files": n, "enabled": p.enabled, "effective": p.enabled and harness == HARNESS,
                    "source": "seed", **_origin(p)})
    return out


def probe_lines(spec: EnvSpec, harness: str | None) -> list[str]:
    """Shell for manifest.probe_script: whether each declared plugin's directory is in the sandbox's seed, with its
    files and a digest (as the skills' are read)."""
    if not applies(spec, harness):
        return []
    out = []
    for p in spec.plugins:
        with contextlib.suppress(ValueError):
            r = resolved(spec, p)
            d = shlex.quote(r["path"])
            out.append(f"if [ -d {d} ]; then c=$(cd {d} && find . -type f | wc -l); "
                       f"g=$(cd {d} && find . -type f -print0 | sort -z | xargs -0r sha256sum | dig); "
                       f"printf 'plugin\\t%s\\t%s\\t%s\\n' {shlex.quote(r['id'])} \"$g\" \"$c\"; "
                       f"else printf 'plugin\\t%s\\t-\\t0\\n' {shlex.quote(r['id'])}; fi")
    return out
