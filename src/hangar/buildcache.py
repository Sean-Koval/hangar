"""Warm dependency and compile caches for sandboxes (--- caches; docs/caches.md, docs/security.md#build-caches).

Every new sandbox used to install dependencies and compile from nothing. A repo build (#93) has the checkout's
`node_modules` or `.venv`, but not what lives in $HOME (the npm, uv, Cargo, Go and Maven caches), not a branch whose
lockfile moved on, and not sandboxes on providers without builds. This module keeps those caches across sandboxes:

* **Dependency caches** (KINDS: npm, pnpm, yarn, node_modules, uv, pip, .venv, Cargo, target, Go modules and build
  cache, Maven, Gradle), keyed by the repo, the toolchain that built them (when the files depend on it) and the git
  blob ids of the lockfiles. A lookup takes the exact key, else the newest entry for the same toolchain, as GitHub
  Actions' `restore-keys` do. They are restored after the clone by `hangar-cache` in the sandbox, in the background:
  the sandbox starts at once, and the agent is told the caches are warming (`hangar-cache wait`).
* **Remote compile caches**, served on the git gateway's port in the protocols the tools already speak: sccache's
  WebDAV backend, Bazel's HTTP remote cache, and the Turborepo remote cache API. Sandboxes get the variables and a
  ~/.bazelrc block that point them there; nothing in the repo changes.
* **Who.** A sandbox authenticates with its git gateway grant (`hgs_…`), which names its repo: a sandbox reads its
  own repo's caches only. Writes to a repo's shared (trusted) layer come only from hangar's own repo builds, which
  run the default branch's (or the environment's configured branch's) `install`. Every other sandbox reads the
  trusted layer and writes, when HANGAR_BUILD_CACHE_PRIVATE_WRITES is on, to a private layer of its own session (or
  sandbox) and branch, which it reads first and nothing else ever reads. Nothing is promoted from a private layer.
* **Storage**, retention and integrity: cachestore.py.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hashlib
import json
import logging
import re
import shlex
import time
import urllib.parse
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from hangar.cachestore import PRIVATE, TRUSTED, CacheError, CacheStore, Entry, Index, location, shared, total_cap_bytes
from hangar.models import Sandbox, SandboxGrant, Session, now

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet
    from hangar.gateway import Gateway
    from hangar.providers.base import Provider

log = logging.getLogger("hangar.caches")


@dataclass(frozen=True)
class Kind:
    """One dependency cache: directories under $HOME or the checkout, keyed by these lockfiles and, when the files
    depend on it, the toolchain's version line."""

    name: str
    base: str  # "home" or "repo"
    paths: tuple[str, ...]
    lockfiles: tuple[str, ...]
    toolchain: str = ""  # a command printing the version the files were built with; "" = any version
    env: tuple[tuple[str, str], ...] = ()  # variables (paths under $HOME) that point the tool at `paths`


_NODE_LOCKS = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb")
_PY_LOCKS = ("uv.lock", "poetry.lock", "Pipfile.lock", "requirements.txt")
_GRADLE = ("gradle/wrapper/gradle-wrapper.properties", "gradle.lockfile", "settings.gradle.kts", "settings.gradle",
           "build.gradle.kts", "build.gradle")
# Default locations checked 2026-10-15: pnpm.io/settings/store (Linux: ~/.local/share/pnpm/store), the tools' own
# variables for the rest (UV_CACHE_DIR, PIP_CACHE_DIR, npm_config_cache, GOMODCACHE, GOCACHE).
KINDS: tuple[Kind, ...] = (
    Kind("npm", "home", (".npm",), ("package-lock.json", "npm-shrinkwrap.json"), env=(("npm_config_cache", ".npm"),)),
    Kind("pnpm", "home", (".local/share/pnpm/store",), ("pnpm-lock.yaml",)),
    Kind("yarn", "home", (".cache/yarn", ".yarn/berry/cache"), ("yarn.lock",)),
    Kind("node_modules", "repo", ("node_modules",), _NODE_LOCKS, "node --version"),
    Kind("uv", "home", (".cache/uv",), ("uv.lock",), env=(("UV_CACHE_DIR", ".cache/uv"),)),
    Kind("pip", "home", (".cache/pip",), ("requirements.txt", "poetry.lock", "Pipfile.lock"),
         env=(("PIP_CACHE_DIR", ".cache/pip"),)),
    Kind("venv", "repo", (".venv",), _PY_LOCKS, "python3 --version"),
    Kind("cargo", "home", (".cargo/registry", ".cargo/git"), ("Cargo.lock",)),
    Kind("cargo-target", "repo", ("target",), ("Cargo.lock",), "rustc --version"),
    Kind("go-mod", "home", ("go/pkg/mod",), ("go.sum",), env=(("GOMODCACHE", "go/pkg/mod"),)),
    Kind("go-build", "home", (".cache/go-build",), ("go.sum",), "go version", env=(("GOCACHE", ".cache/go-build"),)),
    Kind("maven", "home", (".m2/repository",), ("pom.xml",)),
    Kind("gradle", "home", (".gradle/caches", ".gradle/wrapper"), _GRADLE),
)
KIND_NAMES = {k.name for k in KINDS}
BUILD_LABEL = "hangar.build"  # envbuilds.LABEL (a build's sandbox)
VALIDATE_LABEL = "hangar.env-validate"  # envbuilds.VALIDATE_LABEL (agent-led setup's validation: untrusted)
_KEY = re.compile(r"^[A-Za-z0-9._+-]{1,64}/[0-9a-f]{8,64}$")
_PREFIX = re.compile(r"^[A-Za-z0-9._+-]{1,64}/$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TURBO_HASH = re.compile(r"^[A-Fa-f0-9]{1,128}$")
_DAV_PATH = re.compile(r"^[A-Za-z0-9._~/+-]{0,512}$")
TRANSFER_TIMEOUT = 1800  # seconds for one dependency archive to come down or go up (`curl --max-time`)
INFLIGHT_PER_SANDBOX = 64  # Bazel and turbo open many connections at once; sccache one per compile
SEEN_TTL = 5.0  # seconds a verified grant is reused: thousands of cache requests a build, one grant lookup each 5 s
TURBO_QUERY_MAX = 1000
BAZELRC_START = "# >>> hangar build caches (docs/caches.md)"
BAZELRC_END = "# <<< hangar build caches"
WRAPPER = ("#!/bin/sh\n# hangar: rustc through sccache when the image has it (docs/caches.md)\n"
           "if command -v sccache >/dev/null 2>&1; then exec sccache \"$@\"; fi\nexec \"$@\"\n")


# ====================================================================== who may read and write what
@dataclass(frozen=True)
class Access:
    sandbox: Sandbox
    repo: str  # the cache's repo: the grant's
    read: tuple[str, ...]  # layers, in lookup order
    write: str | None  # the layer writes go to, or None (read-only)


def grant_repo(grant: SandboxGrant) -> str | None:
    """The repo a grant names, as repopolicy.repo_key spells it (`owner/name` on GitHub)."""
    from hangar.repopolicy import repo_key

    if not grant.repos or grant.pins:  # a delivery grant pushes one commit; it reads no caches
        return None
    host = grant.host.lower()
    return repo_key(grant.repos[0] if host == "github.com" else f"https://{host}/{grant.repos[0]}")


def trusted_build(fleet: Fleet, sb: Sandbox, repo: str | None) -> bool:
    """Whether the sandbox is one of hangar's own repo builds, running now, of this repo in its environment. Labels
    are anyone's to set on a sandbox they create, so the label alone proves nothing: the build it names must be
    running, of the same repo and environment, and the sandbox must be the one hangar created for it (actor
    `hangar:build:<id>`, a name no principal can have)."""
    from hangar import envbuilds

    bid = sb.labels.get(BUILD_LABEL)
    if not bid or not repo or VALIDATE_LABEL in sb.labels or sb.created_by != f"hangar:build:{bid}":
        return False
    try:
        b = envbuilds.of(fleet).get(bid)
    except (KeyError, ValueError):
        return False
    return b.state == "running" and b.repo == repo and b.environment == sb.environment and sb.repo is not None


def private_layer(fleet: Fleet, sb: Sandbox) -> str:
    """The sandbox's private layer: its session's (so a resumed session finds what it wrote), else its own, and its
    branch. The session label is the creator's to set too, so it counts only for a session of the same person."""
    scope = sb.id
    if sid := sb.labels.get("hangar.session"):
        s = fleet.store.get(Session, sid)
        if s is not None and s.person == sb.person:
            scope = s.id
    return f"{PRIVATE}{scope}:{sb.branch or '-'}"


def policy(fleet: Fleet, sb: Sandbox, repo: str | None) -> tuple[tuple[str, ...], str | None]:
    """(layers read, in order; the layer written, or None). The shared layer is the repo's builds' in the sandbox's
    own environment: an environment's `setup` and image run before `install` in its builds, and members make
    environments, so one member's environment never writes what another environment's sandboxes read."""
    common = shared(sb.environment)
    if trusted_build(fleet, sb, repo):
        return (common,), common
    mine = private_layer(fleet, sb)
    return (mine, common), (mine if fleet.settings.build_cache_private_writes else None)


def write_mode(layer: str | None) -> str:
    return "none" if layer is None else "trusted" if layer.startswith(TRUSTED) else "private"


# ====================================================================== the sandbox's side
HELPER = r"""#!/usr/bin/env bash
# hangar-cache: this sandbox's dependency caches (written by hangar; docs/caches.md).
#   hangar-cache status            what was restored (or "warming" while the restore runs)
#   hangar-cache wait [SECONDS]    block until the restore is done (default 600)
#   hangar-cache save [KIND...]    save the caches as they are now (to your private layer; a repo build: the repo's)
#   hangar-cache kinds             the caches this repo has and their keys
set -u
CONF="$HOME/.hangar/cache.conf"
DIR="$HOME/.hangar/cache"
[ -r "$CONF" ] && . "$CONF"
: "${HANGAR_CACHE_URL:=}" "${HANGAR_CACHE_TOKEN:=}" "${HANGAR_CACHE_WRITE:=none}" "${HANGAR_CACHE_TIMEOUT:=1800}"
REPO="$(cat "$DIR/repo" 2>/dev/null)"
NO_REPO=0
# name|base|paths|lockfiles|toolchain
KINDS='@KINDS@'

enc() {
  local s="$1" o="" c i
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in [A-Za-z0-9._~-]) o+="$c" ;; *) printf -v c '%%%02X' "'$c"; o+="$c" ;; esac
  done
  printf '%s' "$o"
}

key() {  # LOCKFILES TOOLCHAIN -> <toolchain>/<lockfiles' digest>; fails when the repo has none of the lockfiles
  local locks="$1" tool="$2" list="" f sum t=any
  for f in $locks; do [ -f "$REPO/$f" ] && list+="$f "; done
  [ -n "$list" ] || return 1
  sum=$(for f in $list; do
          printf '%s %s\n' "$f" "$(git -C "$REPO" hash-object --no-filters -- "$f" 2>/dev/null \
                                   || sha256sum < "$REPO/$f" | cut -c1-64)"
        done | sha256sum | cut -c1-32)
  if [ -n "$tool" ]; then
    command -v "${tool%% *}" >/dev/null 2>&1 || return 1
    t=$($tool 2>&1 < /dev/null | head -n1 | tr -c 'A-Za-z0-9._+-' '_' | cut -c1-48 | sed 's/_*$//')
    [ -n "$t" ] || t=unknown
  fi
  printf '%s/%s' "$t" "$sum"
}

note() { printf '%s %s %s\n' "$1" "$2" "$3" >> "$DIR/results"; }
hdr() {  # NAME FILE: a response header's value (the last one, if there were redirects)
  tr -d '\r' < "$2" | awk -v k="$1" 'index(tolower($0), k ": ") == 1 { v = substr($0, length(k) + 3) } END { print v }'
}

restore_one() {  # NAME BASE PATHS LOCKS TOOL
  local name="$1" base="$2" paths="$3" k root p tmp code dig got matched t0=$SECONDS
  k=$(key "$4" "$5") || return 0
  if [ "$base" = repo ]; then
    if [ "$NO_REPO" = 1 ]; then note "$name" skip "the sandbox started from a build of the repo"; return 0; fi
    root="$REPO"
    for p in $paths; do
      git -C "$REPO" check-ignore -q -- "$p" 2>/dev/null || { note "$name" skip "$p isn't ignored by git"; return 0; }
    done
  else
    root="$HOME"
  fi
  tmp="$DIR/$name.tgz"
  code=$(curl -sS --max-time "$HANGAR_CACHE_TIMEOUT" -o "$tmp" -D "$tmp.h" -w '%{http_code}' \
         -H "Authorization: Bearer $HANGAR_CACHE_TOKEN" \
         "$HANGAR_CACHE_URL/deps/$name?key=$(enc "$k")&restore=$(enc "${k%%/*}/")" 2> "$tmp.err") || code=000
  if [ "$code" = 404 ]; then
    note "$name" miss "$k"
  elif [ "$code" != 200 ]; then
    note "$name" error "HTTP $code $(head -c 200 "$tmp.err" 2>/dev/null | tr '\n' ' ')"
  else
    dig=$(hdr x-hangar-cache-digest "$tmp.h")
    matched=$(hdr x-hangar-cache-key "$tmp.h")
    got=$(sha256sum < "$tmp" | cut -c1-64)
    if [ -z "$dig" ] || [ "$got" != "$dig" ]; then
      note "$name" error "the download didn't match its digest"
    elif mkdir -p "$root" && tar -xzf "$tmp" -C "$root" --skip-old-files 2> "$tmp.err"; then
      note "$name" "$([ "$matched" = "$k" ] && echo hit || echo partial)" \
           "$matched $(stat -c %s "$tmp") bytes in $((SECONDS - t0))s"
    else
      note "$name" error "tar: $(tail -c 200 "$tmp.err" | tr '\n' ' ')"
    fi
  fi
  rm -f "$tmp" "$tmp.h" "$tmp.err"
}

each() {  # FUNCTION [ARGS...]: run it for every kind, all at once
  local fn="$1" name base paths locks tool
  shift
  while IFS='|' read -r -u 3 name base paths locks tool; do
    [ -n "$name" ] || continue
    "$fn" "$name" "$base" "$paths" "$locks" "$tool" "$@" &
  done 3<<< "$KINDS"
  wait
}

cmd_restore() {  # REPO_DIR [1: the sandbox started from a build, leave the checkout's directories alone]
  REPO="$1"
  NO_REPO="${2:-0}"
  mkdir -p "$DIR" && chmod 700 "$DIR"
  printf '%s\n' "$REPO" > "$DIR/repo"
  : > "$DIR/results"
  echo $$ > "$DIR/pid"
  echo warming > "$DIR/state"
  if [ -z "$HANGAR_CACHE_URL" ] || [ -z "$HANGAR_CACHE_TOKEN" ]; then echo off > "$DIR/state"; return 0; fi
  each restore_one
  echo warm > "$DIR/state"
}

cmd_status() {
  local st
  st=$(cat "$DIR/state" 2>/dev/null || echo off)
  if [ "$st" = warming ] && ! kill -0 "$(cat "$DIR/pid" 2>/dev/null || echo 0)" 2>/dev/null; then st=interrupted; fi
  echo "state: $st (writes: $HANGAR_CACHE_WRITE)"
  [ -s "$DIR/results" ] && sort "$DIR/results"
  return 0
}

cmd_wait() {
  local n="${1:-600}" i=0
  while [ "$(cat "$DIR/state" 2>/dev/null)" = warming ] && kill -0 "$(cat "$DIR/pid" 2>/dev/null || echo 0)" 2>/dev/null
  do
    if [ "$i" -ge "$n" ]; then echo "hangar-cache: still warming after ${n}s"; cmd_status; return 1; fi
    sleep 1
    i=$((i + 1))
  done
  cmd_status
}

cmd_save() {
  [ "$HANGAR_CACHE_WRITE" != none ] || { echo "hangar-cache: this sandbox's caches are read-only" >&2; return 1; }
  [ -n "$HANGAR_CACHE_URL" ] && [ -n "$HANGAR_CACHE_TOKEN" ] || { echo "hangar-cache: no cache here" >&2; return 1; }
  if [ "${1:-}" = --repo ]; then REPO="$2"; shift 2; fi
  [ -n "$REPO" ] || { echo "hangar-cache: which checkout? (--repo DIR)" >&2; return 1; }
  mkdir -p "$DIR" && chmod 700 "$DIR" && : > "$DIR/saved"
  each save_kind "$@"
  cat "$DIR/saved"
  ! grep -q ' failed ' "$DIR/saved"
}

save_kind() {  # NAME BASE PATHS LOCKS TOOL [ONLY...]
  local name="$1" base="$2" paths="$3" locks="$4" tool="$5" k root p have="" url code out
  shift 5
  if [ $# -gt 0 ]; then case " $* " in *" $name "*) ;; *) return 0 ;; esac; fi
  k=$(key "$locks" "$tool") || return 0
  if [ "$base" = repo ]; then root="$REPO"; else root="$HOME"; fi
  for p in $paths; do
    [ -e "$root/$p" ] || continue
    [ "$base" = repo ] && ! git -C "$REPO" check-ignore -q -- "$p" 2>/dev/null && continue
    have+="$p "
  done
  [ -n "$have" ] || return 0
  url="$HANGAR_CACHE_URL/deps/$name?key=$(enc "$k")"
  code=$(curl -sS -o /dev/null -I -w '%{http_code}' -H "Authorization: Bearer $HANGAR_CACHE_TOKEN" "$url&write=1")
  if [ "$code" = 200 ]; then echo "$name exists $k" >> "$DIR/saved"; return 0; fi
  # shellcheck disable=SC2086 # $have is a list of hangar's own paths
  out=$(cd "$root" && tar -cf - $have 2>/dev/null | gzip -1 \
        | curl -sS --max-time "$HANGAR_CACHE_TIMEOUT" -T - -H "Authorization: Bearer $HANGAR_CACHE_TOKEN" \
               -H 'Content-Type: application/gzip' -w '\n%{http_code}' "$url" 2>&1)
  code="${out##*$'\n'}"
  case "$code" in
    200 | 201) echo "$name saved $k" >> "$DIR/saved" ;;
    409) echo "$name exists $k" >> "$DIR/saved" ;;
    *) echo "$name failed HTTP $code: $(printf '%s' "${out%$'\n'*}" | head -c 300 | tr '\n' ' ')" >> "$DIR/saved" ;;
  esac
}

cmd_kinds() {
  local name base paths locks tool k
  while IFS='|' read -r -u 3 name base paths locks tool; do
    [ -n "$name" ] || continue
    k=$(key "$locks" "$tool") && echo "$name $k ($base: $paths)"
  done 3<<< "$KINDS"
}

case "${1:-status}" in
  restore) shift; cmd_restore "$@" ;;
  status) cmd_status ;;
  wait) shift; cmd_wait "$@" ;;
  save) shift; cmd_save "$@" ;;
  kinds) cmd_kinds ;;
  *) sed -n '2,7p' "$0"; exit 2 ;;
esac
"""


def helper() -> str:
    """The `hangar-cache` script, with KINDS as its table."""
    table = "\n".join(f"{k.name}|{k.base}|{' '.join(k.paths)}|{' '.join(k.lockfiles)}|{k.toolchain}" for k in KINDS)
    return HELPER.replace("@KINDS@", table)


def _needles(values: dict[str, bytes]) -> list[bytes]:
    """What of each build secret is looked for in a trusted upload: the value, and its longest line (as
    envbuilds.archive_leaks does), each 8 bytes or more."""
    out: list[bytes] = []
    for v in values.values():
        for n in (v.strip(), max(v.strip().splitlines() or [b""], key=len).strip()):
            # review(#115): and as the value is written where it is used: percent-encoded in a URL's userinfo, and
            # base64 (a Basic auth header, an .npmrc `_auth`) at each of the three alignments it can have in a longer
            # string, keeping only the characters that don't depend on the bytes around it
            for x in (n, urllib.parse.quote(n, safe="").encode(), *_b64_cores(n)):
                if len(x) >= 8 and x not in out:
                    out.append(x)
    return out


def _b64_cores(n: bytes) -> list[bytes]:
    out = []
    for offset in range(3):
        enc = base64.b64encode(b"\0" * offset + n)
        core = enc[-(-offset * 8 // 6):(offset + len(n)) * 8 // 6]  # skip what the prefix touches, and the tail
        out += [core, core.replace(b"+", b"-").replace(b"/", b"_")]
    return out


# ====================================================================== the fleet's caches
class BuildCaches:
    """The fleet's build caches. `of(fleet)` gives them."""

    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self.settings = fleet.settings
        self._store: CacheStore | None = None
        self.needles: dict[str, list[bytes]] = {}  # sandbox id -> build secret values its uploads must not carry

    @property
    def enabled(self) -> bool:
        """Caches need the gateway: sandboxes reach them there, with their grant."""
        return bool(self.settings.build_caches and self.settings.gateway_url)

    @property
    def store(self) -> CacheStore:
        if self._store is None:
            from hangar.checkpoints import GCSStore, LocalStore

            loc = location(self.settings)
            blobs = GCSStore(loc) if loc and loc.startswith("gs://") else \
                LocalStore(Path(loc) if loc else self.settings.data_dir / "caches")
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
            self._store = CacheStore(blobs, Index(self.settings.data_dir / "caches.db"), self.fleet.broker.vault,
                                     self.settings)
        return self._store

    # ------------------------------------------------------------------ at create
    async def attach(self, prov: Provider, sb: Sandbox, spec: EnvSpec) -> dict[str, str]:
        """In _apply_session, after the gateway minted the grant: the sandbox's cache config (~/.hangar/cache.conf,
        `hangar-cache`, the rustc wrapper, the ~/.bazelrc block), and the variables for the cache clients. A sandbox
        without a grant gets none, and a fork loses its parent's."""
        token = self.fleet.gateway.issued.pop(sb.id, None)
        home = f"/home/{self.settings.sandbox_user}"
        grant = next((g for g in self.fleet.gateway.grants(sb.id) if not g.pins), None) if token else None
        repo = grant_repo(grant) if grant else None
        if not self.enabled or not token or not repo:
            if sb.parent:  # a fork's filesystem has its parent's grant in these files: never usable here
                await asyncio.gather(prov.write_file(sb, f"{home}/.hangar/cache.conf", b"", mode=0o600),
                                     self._bazelrc(prov, sb, home, None))
            return {}
        _, write = policy(self.fleet, sb, repo)
        mode = write_mode(write)
        base = self.settings.gateway_url.rstrip("/") + "/cache"  # type: ignore[union-attr]
        q = shlex.quote
        conf = (f"HANGAR_CACHE_URL={q(base)}\nHANGAR_CACHE_TOKEN={q(token)}\nHANGAR_CACHE_WRITE={q(mode)}\n"
                f"HANGAR_CACHE_TIMEOUT={TRANSFER_TIMEOUT}\n")
        await asyncio.gather(
            prov.write_file(sb, f"{home}/.hangar/cache.conf", conf.encode(), mode=0o600),
            prov.write_file(sb, f"{home}/.hangar/bin/hangar-cache", helper().encode(), mode=0o755),
            prov.write_file(sb, f"{home}/.hangar/bin/hangar-rustc-wrapper", WRAPPER.encode(), mode=0o755),
            self._bazelrc(prov, sb, home, (base, token, write is not None)))
        sb.info["caches"] = {"state": "configured", "write": mode, "repo": repo}
        env = {
            # sccache's WebDAV backend (docs/Webdav.md in mozilla/sccache, checked 2026-10-15)
            "SCCACHE_WEBDAV_ENDPOINT": f"{base}/sccache/", "SCCACHE_WEBDAV_TOKEN": token,
            "RUSTC_WRAPPER": f"{home}/.hangar/bin/hangar-rustc-wrapper",
            # the Turborepo remote cache (turborepo.dev: system environment variables, the remote cache spec)
            "TURBO_API": f"{base}/turbo", "TURBO_TOKEN": token, "TURBO_TEAM": "hangar",
            **{var: f"{home}/{rel}" for k in KINDS for var, rel in k.env},
        }
        if write is None:
            env.update(SCCACHE_WEBDAV_RW_MODE="READ_ONLY", TURBO_REMOTE_CACHE_READ_ONLY="1",
                       TURBO_CACHE="local:rw,remote:r")
        # An environment that sets one of these itself (its own remote cache, say) keeps its own.
        return {k: v for k, v in env.items() if k not in spec.secrets and k not in spec.env}

    async def _bazelrc(self, prov: Provider, sb: Sandbox, home: str, to: tuple[str, str, bool] | None) -> None:
        """hangar's block in ~/.bazelrc (Bazel's home rc file, read after the workspace's: bazel.build/run/bazelrc),
        the rest of the file kept. Basic auth in the URL, as bazel.build/remote/caching documents it."""
        path = f"{home}/.bazelrc"
        raw = await prov.read_file(sb, path, limit=1 << 20) or b""
        if len(raw) > 1 << 20:
            return
        cur = raw.decode(errors="replace")
        a, b = cur.find(BAZELRC_START), cur.find(BAZELRC_END)
        if a != -1 and b > a:
            cur = cur[:a] + cur[b + len(BAZELRC_END):].lstrip("\n")
        elif to is None:
            return
        if to is not None:
            base, token, writes = to
            scheme, _, rest = base.partition("://")
            lines = [BAZELRC_START, f"build --remote_cache={scheme}://hangar:{token}@{rest}/bazel"]
            if not writes:
                lines.append("build --remote_upload_local_results=false")
            cur = (cur.rstrip("\n") + "\n\n" if cur.strip() else "") + "\n".join([*lines, BAZELRC_END]) + "\n"
        await prov.write_file(sb, path, cur.encode(), mode=0o600)

    async def restore(self, prov: Provider, sb: Sandbox, stage: Any = None) -> None:
        """After the clone (or a session's restore): start `hangar-cache restore` in the background and return. The
        sandbox is ready now; the agent is told the caches are warming. Never fails the create."""
        c = sb.info.get("caches")
        path = sb.info.get("repo_path")
        if not c or not path or not self.enabled:
            return
        from hangar.envbuilds import EnvBuilds

        d = '"$HOME/.hangar/cache"'
        cmd = (f'mkdir -p {d} && chmod 700 {d} && echo warming > {d}/state && : > {d}/results && '
               f'{{ nohup "$HOME/.hangar/bin/hangar-cache" restore {shlex.quote(path)} '
               f'{1 if EnvBuilds.prebuilt(sb) else 0} > {d}/restore.log 2>&1 < /dev/null & echo $! > {d}/pid; }}')
        try:
            res = await prov.exec(sb, cmd, timeout=30)
            ok = res.exit_code == 0
            why = None if ok else (res.stderr or res.stdout)[-300:]
        except Exception as e:  # noqa: BLE001 - the sandbox works without its caches
            ok, why = False, f"{type(e).__name__}: {e}"[:300]
        c.update(state="warming" if ok else "off", started_at=now())
        if not ok:
            c["reason"] = f"couldn't start the restore: {why}"
            log.warning("build cache restore for %s didn't start: %s", sb.id, why)
        elif stage:
            stage("restoring dependency caches in the background")

    # ------------------------------------------------------------------ in a repo build (envbuilds._build)
    async def before_install(self, prov: Provider, sb: Sandbox, stage: Any = None,
                             values: dict[str, bytes] | None = None) -> None:
        """A build installs only once its restore is done (or HANGAR_BUILD_CACHE_WAIT_MINUTES passed): install and
        the restore write the same directories.

        review(#115): `values`, the build's build secrets, are what its uploads are scanned for from here on, not only
        after install: sccache, Bazel and turbo upload while `install` runs, and a secret compiled into an output
        would reach every sandbox of the environment."""
        from hangar.fleet import ACTIVE

        for sid in list(self.needles):  # builds that failed before after_install
            if (x := self.fleet.store.get(Sandbox, sid)) is None or x.state not in ACTIVE:
                self.needles.pop(sid, None)
        if values and (ns := _needles(values)):
            self.needles[sb.id] = ns
        c = sb.info.get("caches") or {}
        if c.get("state") != "warming":
            return
        wait = int(self.settings.build_cache_wait_minutes * 60)
        if stage:
            stage("restoring the build caches")
        try:
            res = await prov.exec(sb, f'"$HOME/.hangar/bin/hangar-cache" wait {wait}', timeout=wait + 60)
            c["restored"] = res.stdout[-2000:]
        except Exception as e:  # noqa: BLE001 - install runs either way
            c["restored"] = f"couldn't wait for the restore: {type(e).__name__}"

    async def after_install(self, prov: Provider, sb: Sandbox, path: str, values: dict[str, bytes],
                            stage: Any = None) -> str:
        """Hangar's own build step saves the repo's trusted caches after a successful install. An upload holding a
        build secret is refused by the server (cachestore._Scanner). Returns lines for the build's log; a failure
        never fails the build."""
        c = sb.info.get("caches") or {}
        out = [f"restore: {c['restored'].strip()}"] if c.get("restored") else []
        if c.get("write") != "trusted":
            return "\n".join(out)
        if stage:
            stage("saving the build caches")
        if values:
            self.needles[sb.id] = _needles(values)
        try:
            res = await prov.exec(sb, f'"$HOME/.hangar/bin/hangar-cache" save --repo {shlex.quote(path)}',
                                  timeout=TRANSFER_TIMEOUT + 300)
            out.append("save: " + (res.stdout.strip() or res.stderr.strip()[-500:] or "nothing to save"))
        except Exception as e:  # noqa: BLE001
            out.append(f"save: failed ({type(e).__name__}: {e})"[:500])
        finally:
            self.needles.pop(sb.id, None)
        return "\n".join(out)

    # ------------------------------------------------------------------ admin
    def status(self) -> dict[str, Any]:
        """`hangar status`'s `caches` section: sizes per repo against the caps, and what happened since start."""
        if not self.settings.build_caches:
            return {"enabled": False, "why": "HANGAR_BUILD_CACHES is off"}
        if not self.settings.gateway_url:
            return {"enabled": False, "why": "HANGAR_GATEWAY_URL isn't set (sandboxes reach caches at the gateway)"}
        st = self.store
        usage = st.index.usage()
        return {"enabled": True, "store": st.description, "bytes": st.index.stored(),
                "cap_bytes": total_cap_bytes(self.settings),
                "repo_cap_bytes": int(self.settings.build_cache_repo_max_gb * (1 << 30)),
                "private_writes": self.settings.build_cache_private_writes,
                "repos": usage[:20], "repo_count": len(usage), "since_start": dict(st.stats)}

    async def nightly(self) -> dict[str, Any]:
        """The nightly pass: entries past retention, the caps, and objects nothing points at."""
        if not self.enabled:
            return {"skipped": "off"}
        st = self.store
        return {"expired": await st.expire(), "evicted": await st.evict(), "swept": await st.sweep()}


def of(fleet: Fleet) -> BuildCaches:
    """The fleet's caches (made on first use, so Fleet itself needs no change)."""
    bc = fleet.__dict__.get("_buildcaches")
    if bc is None:
        bc = fleet.__dict__["_buildcaches"] = BuildCaches(fleet)
    return bc


def agent_lines(sb: Sandbox) -> list[str]:
    """For the "Your environment" block of the agent's instructions (svc_control.instructions)."""
    c = sb.info.get("caches") or {}
    if c.get("state") not in ("warming", "configured"):
        return []
    writes = {"private": "to your own private layer (nobody else reads it)",
              "trusted": "to the repo's shared caches (this is a repo build)"}.get(c.get("write") or "",
                                                                                   "nowhere: they're read-only here")
    lines = ["Build caches: sccache, Bazel and Turborepo are set up with a remote cache for this repo (nothing in the "
             f"repo needs to change); writes go {writes}."]
    if c.get("state") == "warming":
        lines.append("The repo's dependency caches (npm, uv, Cargo, Go and the like, whichever it uses) are being "
                     "restored in the background. Run `hangar-cache wait` before installing dependencies (it returns "
                     "at once when they're done); `hangar-cache status` says what was restored.")
    if c.get("write") == "private":
        lines.append("After a long dependency install, `hangar-cache save` keeps the result for your next sandbox.")
    return [*lines, ""]


# ====================================================================== the server, on the gateway's port
class Server:
    """/cache/... on the git gateway's app: dependency caches, sccache (WebDAV), Bazel (HTTP cache), Turborepo."""

    def __init__(self, gw: Gateway):
        self.gw, self.fleet, self.settings = gw, gw.fleet, gw.settings
        self._seen: dict[str, tuple[float, Access]] = {}
        self._inflight: collections.Counter[str] = collections.Counter()

    @property
    def caches(self) -> BuildCaches:
        return of(self.fleet)

    def routes(self) -> list[Route]:
        w = self._wrap
        return [
            Route("/cache/deps/{kind}", w(self.deps), methods=["GET", "HEAD", "PUT"]),
            Route("/cache/bazel/{part}/{digest}", w(self.bazel), methods=["GET", "HEAD", "PUT"]),
            Route("/cache/sccache/{path:path}", w(self.webdav),
                  methods=["GET", "HEAD", "PUT", "MKCOL", "PROPFIND", "DELETE", "MOVE", "COPY"]),
            Route("/cache/turbo/v8/artifacts/status", w(self.turbo_status), methods=["GET"]),
            Route("/cache/turbo/v8/artifacts/events", w(self.turbo_events), methods=["POST"]),
            Route("/cache/turbo/v8/artifacts", w(self.turbo_query), methods=["POST"]),
            Route("/cache/turbo/v8/artifacts/{hash}", w(self.turbo), methods=["GET", "HEAD", "PUT"]),
        ]

    # ------------------------------------------------------------------ who
    @staticmethod
    def _token(request: Request) -> str | None:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            return value.strip()
        if scheme.lower() == "basic":
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                return base64.b64decode(value.strip(), validate=True).decode().partition(":")[2]
        return None

    def access(self, request: Request) -> Access | None:
        """The sandbox behind the request's grant, and what it may read and write. A verified grant is reused for
        SEEN_TTL seconds: a build makes thousands of cache requests."""
        token = self._token(request)
        if not token or not token.startswith("hgs_") or len(token) > 128:
            return None
        k = hashlib.sha256(token.encode()).hexdigest()
        t = time.monotonic()
        if (hit := self._seen.get(k)) and t - hit[0] < SEEN_TTL:
            return hit[1]
        grant = self.gw.verify(token)
        repo = grant_repo(grant) if grant else None
        sb = self.fleet.store.get(Sandbox, grant.sandbox_id) if grant else None
        if grant is None or repo is None or sb is None:
            self._seen.pop(k, None)
            return None
        read, write = policy(self.fleet, sb, repo)
        acc = Access(sandbox=sb, repo=repo, read=read, write=write)
        if len(self._seen) > 5000:
            self._seen = {x: v for x, v in self._seen.items() if t - v[0] < SEEN_TTL}
        self._seen[k] = (t, acc)
        return acc

    def _wrap(self, fn: Any) -> Any:
        async def handle(request: Request) -> Response:
            src = request.client.host if request.client else "unknown"
            if not self.caches.enabled:
                return PlainTextResponse("hangar: build caches are off (HANGAR_BUILD_CACHES)\n", 404)
            if self.gw._failures.blocked(src):
                return PlainTextResponse("hangar: too many requests with invalid credentials; wait a minute\n", 429)
            acc = self.access(request)
            if acc is None:
                if request.headers.get("authorization") and self.gw._failures.add(src):
                    self.fleet.audit("gateway", "gw.auth.blocked", None, source=src)
                return PlainTextResponse("hangar: build caches are for hangar's sandboxes, with their git grant "
                                         "(the variables hangar sets: docs/caches.md)\n", 401,
                                         headers={"WWW-Authenticate": 'Basic realm="hangar caches"'})
            sid = acc.sandbox.id
            if self._inflight[sid] >= INFLIGHT_PER_SANDBOX:
                return PlainTextResponse("hangar: too many cache requests at once from this sandbox\n", 429,
                                         headers={"Retry-After": "1"})
            self._inflight[sid] += 1
            try:
                return await fn(request, acc)
            except CacheError as e:
                return PlainTextResponse(f"hangar: {e}\n", e.status)
            except ClientDisconnect:
                return Response(status_code=400)
            finally:
                self._inflight[sid] -= 1
                if self._inflight[sid] <= 0:
                    del self._inflight[sid]

        return handle

    # ------------------------------------------------------------------ shared
    async def _serve(self, request: Request, e: Entry | None, headers: dict[str, str] | None = None,
                     missing: Response | None = None) -> Response:
        st = self.caches.store
        head = request.method == "HEAD"  # an existence check: nothing is read (a GET checks what it sends)
        first = await st.first(e) if e is not None and not head else None
        if e is None or (first is None and not head):
            st.stats["misses"] += 1
            return missing or Response(status_code=404)
        st.stats["hits"] += 1
        st.index.touch(e, time.time())
        h = {"Content-Length": str(e.size), **(headers or {})}
        if head or first is None:
            return Response(status_code=200, headers=h)
        return StreamingResponse(st.stream(e, first), media_type="application/octet-stream", headers=h)

    async def _write(self, request: Request, acc: Access, ns: str, key: str, *, expect: str | None = None,
                     immutable: bool = False, meta: dict[str, Any] | None = None) -> Entry | Response:
        st = self.caches.store
        if acc.write is None:
            st.stats["denied"] += 1
            return PlainTextResponse("hangar: this sandbox's build caches are read-only: only the repo's own builds "
                                     "write the shared caches (docs/caches.md)\n", 403)
        if immutable and st.index.get(acc.repo, acc.write, ns, key) is not None:
            return PlainTextResponse("hangar: already saved under this key\n", 409)
        cap = self.settings.build_cache_object_max_mb * 1024 * 1024
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > cap):
            raise CacheError(413, f"over HANGAR_BUILD_CACHE_OBJECT_MAX_MB ({cap >> 20} MB)")
        digest, size, chunks, sizes = await st.ingest(acc.repo, request.stream(), cap, expect=expect,
                                                      needles=self.caches.needles.get(acc.sandbox.id),
                                                      size_hint=int(length) if length is not None else None)
        return st.commit(acc.repo, acc.write, ns, key, digest, size, chunks, sizes, writer=acc.sandbox.id, meta=meta)

    # ------------------------------------------------------------------ dependency caches
    async def deps(self, request: Request, acc: Access) -> Response:
        kind = request.path_params["kind"]
        key = request.query_params.get("key", "")
        if kind not in KIND_NAMES or not _KEY.match(key):
            return PlainTextResponse("hangar: unknown cache kind or malformed key\n", 400)
        ns = f"deps/{kind}"
        st = self.caches.store
        if request.method == "PUT":
            e = await self._write(request, acc, ns, key, immutable=True)
            if isinstance(e, Response):
                return e
            self.fleet.audit(acc.sandbox.id, "cache.write", acc.repo, kind=kind, key=key, layer=write_mode(e.layer),
                             bytes=e.size)
            return JSONResponse({"key": key, "bytes": e.size, "sha256": e.digest}, status_code=201)
        if request.query_params.get("write") == "1":  # `hangar-cache save`: is it in my write layer already?
            found = acc.write is not None and st.index.get(acc.repo, acc.write, ns, key) is not None
            return Response(status_code=200 if found else 404)
        prefixes = [p for p in request.query_params.getlist("restore")[:4] if _PREFIX.match(p)]
        e = st.index.lookup(acc.repo, list(acc.read), ns, key, prefixes)
        headers = {"X-Hangar-Cache-Key": e.key, "X-Hangar-Cache-Digest": e.digest,
                   "X-Hangar-Cache-Layer": write_mode(e.layer)} if e else None
        return await self._serve(request, e, headers)

    # ------------------------------------------------------------------ Bazel's HTTP cache (bazel.build/remote/caching)
    async def bazel(self, request: Request, acc: Access) -> Response:
        part, digest = request.path_params["part"], request.path_params["digest"]
        if part not in ("ac", "cas") or not _HEX64.match(digest):
            return PlainTextResponse("hangar: /cache/bazel/{ac,cas}/<sha256>\n", 404)
        ns = f"bazel/{part}"
        if request.method == "PUT":  # the CAS is content-addressed: its body must hash to its name
            e = await self._write(request, acc, ns, digest, expect=digest if part == "cas" else None)
            return e if isinstance(e, Response) else Response(status_code=200)
        return await self._serve(request, self.caches.store.index.lookup(acc.repo, list(acc.read), ns, digest))

    # ------------------------------------------------------------------ sccache: WebDAV (opendal's webdav service)
    async def webdav(self, request: Request, acc: Access) -> Response:
        path = request.path_params.get("path", "")
        if not _DAV_PATH.match(path) or any(s in ("..", ".") for s in path.split("/")):
            return PlainTextResponse("hangar: bad path\n", 400)
        m = request.method
        if m == "MKCOL":  # keys are flat; every "directory" exists
            return Response(status_code=201)
        if m in ("DELETE", "MOVE", "COPY"):
            return PlainTextResponse("hangar: the build cache only adds and reads entries\n", 403)
        st = self.caches.store
        key = path.strip("/")
        if m == "PROPFIND":
            if not key or path.endswith("/"):
                return _multistatus(request.url.path, None)
            e = st.index.lookup(acc.repo, list(acc.read), "sccache", key)
            return _multistatus(request.url.path, e) if e else Response(status_code=404)
        if not key or path.endswith("/"):
            return Response(status_code=404)
        if m == "PUT":
            e = await self._write(request, acc, "sccache", key)
            return e if isinstance(e, Response) else Response(status_code=201)
        return await self._serve(request, st.index.lookup(acc.repo, list(acc.read), "sccache", key))

    # ------------------------------------------------------------------ Turborepo (turborepo.dev/api/remote-cache-spec)
    async def turbo_status(self, request: Request, acc: Access) -> Response:
        return JSONResponse({"status": "enabled"})

    async def turbo_events(self, request: Request, acc: Access) -> Response:
        await _read_small(request, 1 << 20)
        return Response(status_code=200)

    async def turbo_query(self, request: Request, acc: Access) -> Response:
        try:
            body = json.loads(await _read_small(request, 256 * 1024))
            hashes = body["hashes"]
            if not isinstance(hashes, list) or len(hashes) > TURBO_QUERY_MAX:
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return _turbo_error(400, "bad_request", f'expected {{"hashes": [...]}} (at most {TURBO_QUERY_MAX})')
        idx = self.caches.store.index
        out: dict[str, Any] = {}
        for h in hashes:
            if not isinstance(h, str) or not _TURBO_HASH.match(h):
                continue
            e = idx.lookup(acc.repo, list(acc.read), "turbo", h.lower())
            out[h] = {"size": e.size, "taskDurationMs": int(e.meta.get("duration") or 0),
                      **({"tag": e.meta["tag"]} if e.meta.get("tag") else {})} if e else None
        return JSONResponse(out)

    async def turbo(self, request: Request, acc: Access) -> Response:
        h = request.path_params["hash"]
        if not _TURBO_HASH.match(h):
            return _turbo_error(400, "bad_request", "the hash must be hex")
        key = h.lower()
        if request.method == "PUT":
            if request.headers.get("content-length") is None:
                return _turbo_error(400, "bad_request", "Content-Length is required")
            dur = request.headers.get("x-artifact-duration", "")
            tag = request.headers.get("x-artifact-tag", "")
            meta = {"duration": int(dur) if dur.isdigit() else 0, **({"tag": tag} if 0 < len(tag) <= 600 else {})}
            e = await self._write(request, acc, "turbo", key, meta=meta)
            if isinstance(e, Response):
                return _turbo_error(e.status_code, "forbidden", bytes(e.body).decode().strip())
            return JSONResponse({"urls": [str(request.url.remove_query_params(("teamId", "slug")))]}, 202)
        e = self.caches.store.index.lookup(acc.repo, list(acc.read), "turbo", key)
        headers = {"x-artifact-duration": str(int(e.meta.get("duration") or 0)),
                   **({"x-artifact-tag": e.meta["tag"]} if e.meta.get("tag") else {})} if e else None
        return await self._serve(request, e, headers, missing=_turbo_error(404, "not_found", "no such artifact"))


async def _read_small(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for piece in request.stream():
        body += piece
        if len(body) > limit:
            raise CacheError(413, "request body too large")
    return bytes(body)


def _turbo_error(status: int, code: str, message: str) -> JSONResponse:
    """The remote cache spec's Error body."""
    return JSONResponse({"code": code, "message": message}, status_code=status)


def _multistatus(href: str, e: Entry | None) -> Response:
    """A PROPFIND answer (RFC 4918 multistatus) for a collection, or for one entry."""
    if e is None:
        props = "<D:resourcetype><D:collection/></D:resourcetype>"
    else:
        props = (f"<D:resourcetype/><D:getcontentlength>{e.size}</D:getcontentlength>"
                 f"<D:getlastmodified>{formatdate(e.created, usegmt=True)}</D:getlastmodified>"
                 f"<D:getetag>\"{e.digest[:32]}\"</D:getetag>"
                 "<D:getcontenttype>application/octet-stream</D:getcontenttype>")
    body = ('<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:"><D:response>'
            f"<D:href>{escape(href)}</D:href><D:propstat><D:prop>{props}</D:prop>"
            "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>\n")
    return Response(body, status_code=207, media_type='application/xml; charset="utf-8"')


# ====================================================================== wiring
def routes(gw: Gateway) -> list[Route]:
    """The /cache routes on the gateway's app (Gateway.routes)."""
    srv = gw.__dict__.get("_cache_server")
    if srv is None:
        srv = gw.__dict__["_cache_server"] = Server(gw)
    return srv.routes()


def register(h: Any) -> None:
    """`hangar status`'s `caches` section and the nightly pass (once)."""
    from hangar import opstatus

    opstatus.EXTRA["caches"] = lambda fleet, _s, _j: of(fleet).status()
    if not any(n == "caches" for n, _ in opstatus.NIGHTLY):
        opstatus.NIGHTLY.append(("caches", lambda hh, _by: of(hh.fleet).nightly()))


def mount(app: Any, h: Any, admin: Any) -> None:
    """GET /api/caches (sizes per repo) and DELETE /api/caches?repo=&layer= (purge; admins, audited)."""
    from fastapi import HTTPException, Query

    @app.get("/api/caches")
    async def caches_view(_: str = admin):
        return await asyncio.to_thread(of(h.fleet).status)

    @app.delete("/api/caches")
    async def caches_purge(repo: str | None = Query(None), layer: str | None = Query(None),
                           all_repos: bool = Query(False, alias="all"), who: str = admin):
        from hangar.repopolicy import repo_key

        if not repo and not all_repos:
            raise HTTPException(400, "name a repo (owner/repo or a git URL), or all=true for every repo's caches")
        if layer not in (None, "trusted", "private"):
            raise HTTPException(400, "layer: trusted or private (default: both)")
        key = repo_key(repo) if repo else None
        n = await of(h.fleet).store.purge(key, layer)
        h.fleet.audit(who, "cache.purge", key or "*", layer=layer or "all", entries=n)
        return {"repo": key, "layer": layer or "all", "entries": n}


__all__ = ["KINDS", "Access", "BuildCaches", "Server", "agent_lines", "of", "policy", "routes"]
