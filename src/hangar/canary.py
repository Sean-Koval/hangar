"""The harness contract checks and the nightly canary (docs/harnesses.md, harness P4).

The real-CLI tests (`tests/test_*real_clis.py`) run each agent CLI offline against scripted model APIs. CI runs them
against the versions harnesses.lock.yaml pins, on every PR that touches harness, driver or isolation code. Each night
the canary resolves the newest stable upstream release of every harness from the vendors' own release sources (the
ones the lock was verified against), checks the published checksums against the artifacts, runs the same tests and
the protocol diff against those versions, and proposes a lock bump. A human approves the bump; nothing here merges.

    python -m hangar.canary install --dest DIR [--lock FILE] [--only claude-code,codex]
    python -m hangar.canary resolve --out CANDIDATE.yaml --report resolve.json [--no-download]
    python -m hangar.canary protocol --dest DIR --out DIR      # each CLI's protocol surface, as the driver uses it
    python -m hangar.canary diff --locked DIR --candidate DIR --report diff.json
    python -m hangar.canary packet --resolve resolve.json --diff diff.json --tests junit.xml [--failed]

Standard library plus what hangar already depends on (httpx, pyyaml), so it runs on a bare CI runner after `uv sync`.
It never talks to a hangar control plane, so it works while the cluster is powered down.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import yaml

from hangar.harnesses import (
    HARNESSES,
    LOCK_FILE,
    VERSION_FILE,
    Harness,
    apply_lock,
    pack_digest,
    pack_script,
    version_from_log,
)

# The agent CLIs the contract tests run (gemini is deprecated and has no tests; `install --only gemini` still works).
CONTRACT = ("claude-code", "codex", "pi", "antigravity")
TEST_ENV = {"claude-code": "HANGAR_TEST_CLAUDE", "codex": "HANGAR_TEST_CODEX", "pi": "HANGAR_TEST_PI",
            "antigravity": "HANGAR_TEST_AGY"}
BINARY = {"claude-code": "claude", "codex": "codex", "pi": "pi", "antigravity": "agy", "gemini": "gemini"}
STAMP = ".hangar-pack"  # the pack digest an install came from: a cached install of another recipe is redone

# The vendors' release sources (the same ones harnesses.lock.yaml was verified against, 2026-10-14).
CLAUDE_RELEASES = "https://downloads.claude.ai/claude-code-releases"
AGY_UPDATER = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
AGY_ARCHIVES = "https://storage.googleapis.com/antigravity-public/antigravity-cli"
NPM = "https://registry.npmjs.org"
NPM_PACKAGES = {"codex": "@openai/codex", "pi": "@earendil-works/pi-coding-agent", "gemini": "@google/gemini-cli"}
CLAUDE_PLATFORMS = {"x86_64": "linux-x64", "aarch64": "linux-arm64"}  # the lock's arch -> the manifest's platform
AGY_PLATFORMS = {"x86_64": ("linux_amd64", "linux-x64/cli_linux_x64"), "aarch64": ("linux_arm64",
                                                                               "linux-arm/cli_linux_arm64")}
# A checksum is only as good as where it comes from, so each source is checked against something the release server
# alone can't forge (--- review #105):
# - Claude Code: each release's manifest.json is signed with Anthropic's release key (code.claude.com/docs/en/setup,
#   "Binary integrity and code signing", 2.1.89 and later). The key is pinned here, with the fingerprint those docs
#   publish; it is never fetched from the release server.
# - npm (Codex, pi): `npm audit signatures` checks the registry's signatures and the Sigstore provenance attestations
#   of what it would install, and each package the recipe installs must carry SLSA provenance from its own repository.
# - Antigravity publishes no signature (checked 2026-10-14: no .sig, .asc or checksum file beside the archives). Its
#   sha512 comes from the updater's manifest, a separate service from the bucket the archives are in; that is all.
CLAUDE_KEY = Path(__file__).parent / "keys" / "claude-code.asc"
CLAUDE_KEY_FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
NPM_REPOS = {"codex": "https://github.com/openai/codex", "pi": "https://github.com/earendil-works/pi"}
NPM_CPUS = {"codex": ("x64", "arm64"), "pi": ("x64",), "gemini": ("x64",)}  # codex ships a package per platform


# ====================================================================== the lock
def load_lock(path: Path = LOCK_FILE) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def harnesses_for(lock: dict[str, Any]) -> dict[str, Harness]:
    """hangar's harness profiles, pinned to `lock` instead of the checked-in one (validated the same way)."""
    out = {k: v.model_copy(deep=True) for k, v in HARNESSES.items()}
    apply_lock(out, copy.deepcopy(lock))
    return out


def version_key(v: str) -> tuple[Any, ...]:
    """Order release versions (semver-like): the numeric parts numerically, then a pre-release before its release."""
    main, _, pre = v.lstrip("v").split("+")[0].partition("-")
    nums = tuple(int(p) if p.isdigit() else -1 for p in main.split("."))
    tail = tuple((1, int(p)) if p.isdigit() else (0, p) for p in pre.split(".")) if pre else ()
    return nums, 0 if pre else 1, tail


def newer(candidate: str, locked: str) -> bool:
    return version_key(candidate) > version_key(locked)


def _scalar(v: Any) -> str:
    s = str(v)
    return f'"{s}"' if re.fullmatch(r"[0-9]+", s) else s  # a numeric id stays a string


def dump_lock(lock: dict[str, Any], header: str) -> str:
    """The lock in its checked-in layout (two levels, no flow style), under its own comment header."""
    lines = [header.rstrip("\n")] if header.strip() else []
    for name, entry in lock.items():
        lines.append(f"{name}:")
        for k, v in (entry or {}).items():
            if isinstance(v, dict):
                lines.append(f"  {k}:")
                lines += [f"    {kk}: {_scalar(vv)}" for kk, vv in v.items()]
            else:
                lines.append(f"  {k}: {_scalar(v)}")
    return "\n".join(lines) + "\n"


def lock_header(text: str) -> str:
    """The comment block above the first entry."""
    out = []
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            break
        out.append(line)
    return "\n".join(out)


def bumped_header(header: str, changes: dict[str, tuple[str, str]], day: str) -> str:
    """The header with one `# Canary:` line naming the latest bump (replaced, not appended, on the next one)."""
    line = (f"# Canary {day}: " + ", ".join(f"{h} {a} -> {b}" for h, (a, b) in changes.items())
            + " (contract tests and the protocol diff: its PR).")
    kept = [x for x in header.splitlines() if not x.startswith("# Canary ")]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join([*kept, line])


# ====================================================================== install
def install(lock: dict[str, Any], dest: Path, only: list[str] | None = None, log: Any = print) -> dict[str, str]:
    """Install the CLIs `lock` pins into dest/<harness>, with the packs' own recipes (so the version check and the
    checksums are the packs'), as an unprivileged user. An install whose stamp names the same pack digest is kept:
    CI caches `dest` across runs. Returns {harness: what its --version said}."""
    hs = harnesses_for(lock)
    names = [n for n, h in hs.items() if h.install] if only == ["all"] else only or list(CONTRACT)
    if unknown := [n for n in names if n not in hs or not hs[n].install]:
        raise ValueError(f"nothing to install for {', '.join(unknown)}")
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name in names:
        h, prefix = hs[name], (dest / name).resolve()
        digest = f"{pack_digest(h)} {prefix}"  # an install can name its own prefix (pi's launcher does)
        stamp, vfile = prefix / STAMP, prefix / VERSION_FILE
        if stamp.exists() and vfile.exists() and stamp.read_text().strip() == digest:
            out[name] = vfile.read_text().strip()
            log(f"{name} {h.version}: cached ({out[name]})")
            continue
        if prefix.exists():
            subprocess.run(["rm", "-rf", str(prefix)], check=True)
        script = pack_script(h, prefix=str(prefix), version_file=str(vfile))
        log(f"{name} {h.version}: installing into {prefix}")
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(script)
        try:
            res = subprocess.run(["bash", f.name], capture_output=True, text=True, timeout=900)
        finally:
            os.unlink(f.name)
        if res.returncode != 0:
            tail = "\n".join((res.stdout + res.stderr).splitlines()[-30:])
            raise RuntimeError(f"installing {name} {h.version} failed (exit {res.returncode}):\n{tail}")
        installed = version_from_log(res.stdout) or ""
        stamp.write_text(digest + "\n")
        out[name] = installed
        log(f"{name} {h.version}: installed ({installed})")
    return out


def binary(dest: Path, harness: str) -> Path:
    return dest.resolve() / harness / "bin" / BINARY[harness]


def test_env(dest: Path, names: list[str] | None = None) -> dict[str, str]:
    """HANGAR_TEST_* for the installed CLIs, which the real-CLI tests read."""
    return {TEST_ENV[n]: str(binary(dest, n)) for n in (names or CONTRACT)
            if n in TEST_ENV and binary(dest, n).exists()}


# ====================================================================== resolve: the newest stable upstream releases
def _sha(algo: str, client: httpx.Client, url: str) -> str:
    h = hashlib.new(algo)
    with client.stream("GET", url) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _get(client: httpx.Client, url: str) -> httpx.Response:
    """A GET that a network blip or a 5xx doesn't fail (three tries): a red canary should mean a real failure."""
    for attempt in range(3):
        try:
            r = client.get(url)
            if r.status_code < 500 or attempt == 2:
                r.raise_for_status()
                return r
        except httpx.TransportError:
            if attempt == 2:
                raise
        time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def _json(client: httpx.Client, url: str) -> Any:
    return _get(client, url).json()


def _dearmor(text: str) -> bytes:
    """An ASCII-armored OpenPGP block's bytes (gpgv reads only binary keyrings)."""
    body = text.split("-----BEGIN PGP PUBLIC KEY BLOCK-----", 1)[1].split("-----END", 1)[0]
    lines = [x.strip() for x in body.strip("\n").splitlines()]
    if "" in lines:  # armor headers end at the first blank line
        lines = lines[lines.index("") + 1:]
    return base64.b64decode("".join(x for x in lines if x and not x.startswith("=")))


def verify_signed(data: bytes, sig: bytes) -> None:
    """Raise ValueError unless `sig` is a good signature of `data` by the pinned Claude Code release key."""
    gpgv = shutil.which("gpgv")
    if not gpgv:
        raise ValueError("gpgv isn't installed, so the release manifest's signature can't be checked")
    with tempfile.TemporaryDirectory() as d:
        keyring, data_f, sig_f = Path(d, "key.gpg"), Path(d, "manifest.json"), Path(d, "manifest.json.sig")
        keyring.write_bytes(_dearmor(CLAUDE_KEY.read_text()))
        data_f.write_bytes(data)
        sig_f.write_bytes(sig)
        res = subprocess.run([gpgv, "--homedir", d, "--status-fd", "1", "--keyring", str(keyring), str(sig_f),
                              str(data_f)], capture_output=True, text=True, timeout=60)
    valid = [line.split() for line in res.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    if res.returncode != 0 or not any(v[-1].upper() == CLAUDE_KEY_FINGERPRINT for v in valid):
        raise ValueError("the release manifest's signature doesn't verify with Anthropic's release key "
                         f"({CLAUDE_KEY_FINGERPRINT})")


def _claude(client: httpx.Client, download: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    version = _get(client, f"{CLAUDE_RELEASES}/stable").text.strip()
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)+", version):
        raise ValueError(f"the stable channel answered {version[:80]!r}, not a version")
    raw = _get(client, f"{CLAUDE_RELEASES}/{version}/manifest.json").content
    verify_signed(raw, _get(client, f"{CLAUDE_RELEASES}/{version}/manifest.json.sig").content)
    manifest = json.loads(raw)
    if manifest.get("version") != version:
        raise ValueError(f"{version}/manifest.json names version {manifest.get('version')!r}")
    sums: dict[str, str] = {}
    for arch, platform in CLAUDE_PLATFORMS.items():
        want = ((manifest.get("platforms") or {}).get(platform) or {}).get("checksum") or ""
        if not re.fullmatch(r"[0-9a-f]{64}", want):
            raise ValueError(f"{version}/manifest.json has no sha256 for {platform}")
        if download and (got := _sha("sha256", client, f"{CLAUDE_RELEASES}/{version}/{platform}/claude")) != want:
            raise ValueError(f"{version}/{platform}/claude has sha256 {got}, but the manifest says {want}")
        sums[arch] = want
    return ({"version": version, "sha256": sums},
            {"source": f"{CLAUDE_RELEASES}/stable", "checksums": "sha256 from the release manifest, signed by "
             "Anthropic's release key" + (", checked against each download" if download else
                                          " (downloads not checked)")})


def _agy(client: httpx.Client, download: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    releases = _json(client, f"{AGY_UPDATER}/releases")
    builds = {str(r.get("version")): str(r.get("execution_id")) for r in releases if isinstance(r, dict)}
    version, sums, build = None, {}, None
    for arch, (platform, path) in AGY_PLATFORMS.items():
        m = _json(client, f"{AGY_UPDATER}/manifests/{platform}.json")
        v, url, want = str(m.get("version") or ""), str(m.get("url") or ""), str(m.get("sha512") or "")
        if version not in (None, v):
            raise ValueError(f"the updater serves {version} on one platform and {v} on {platform}")
        version, build = v, builds.get(v)
        if not build or not re.fullmatch(r"[0-9]+", build):
            raise ValueError(f"{v} isn't in the updater's release list")
        # the lock's recipe builds this URL from the version and the build id: it must be the one the updater serves
        if url != (expected := f"{AGY_ARCHIVES}/{v}-{build}/{path}.tar.gz"):
            raise ValueError(f"the updater serves {url} for {platform}; the install recipe would fetch {expected}")
        if not re.fullmatch(r"[0-9a-f]{128}", want):
            raise ValueError(f"the {platform} manifest has no sha512")
        if download and (got := _sha("sha512", client, url)) != want:
            raise ValueError(f"{url} has sha512 {got[:16]}…, but the updater's manifest says {want[:16]}…")
        sums[arch] = want
    return ({"version": version, "build": build, "sha512": sums},
            {"source": f"{AGY_UPDATER}/manifests (and /releases for the build id)",
             "checksums": "sha512 from the updater's manifests (Google publishes no signature)"
             + (", checked against each archive" if download else " (archives not checked)")})


def npm_audit(pkg: str, version: str, cpu: str) -> None:
    """npm's own check of what installing pkg@version on linux/<cpu> fetches: every package's registry signature,
    and the Sigstore provenance attestations of those that have them. --ignore-scripts: nothing in them runs."""
    npm = shutil.which("npm")
    if not npm:
        raise ValueError("npm isn't installed, so its signatures can't be checked")
    with tempfile.TemporaryDirectory() as d:
        Path(d, "package.json").write_text('{"name": "hangar-canary-check", "private": true}\n')
        for args in (["install", "--ignore-scripts", "--no-audit", "--no-fund", "--os=linux", f"--cpu={cpu}",
                      f"{pkg}@{version}"], ["audit", "signatures"]):
            res = subprocess.run([npm, *args], cwd=d, capture_output=True, text=True, timeout=600)
            if res.returncode != 0:
                tail = (res.stdout + res.stderr).strip()[-400:]
                raise ValueError(f"npm {args[0]} for {pkg}@{version} (linux-{cpu}) failed: {tail}")


def _provenance(client: httpx.Client, pkg: str, version: str, repo: str) -> None:
    """pkg@version must carry SLSA provenance naming it, built from `repo` (npm audit has checked its signature)."""
    try:
        doc = _json(client, f"{NPM}/-/npm/v1/attestations/{pkg.replace('/', '%2f')}@{version}")
    except httpx.HTTPStatusError as e:
        raise ValueError(f"npm {pkg}@{version} has no provenance (HTTP {e.response.status_code})") from e
    subject = f"pkg:npm/{pkg.replace('@', '%40', 1)}@{version}"
    for a in doc.get("attestations") or []:
        if not str(a.get("predicateType") or "").startswith("https://slsa.dev/provenance/"):
            continue
        with contextlib.suppress(ValueError, KeyError, TypeError, AttributeError):
            st = json.loads(base64.b64decode(a["bundle"]["dsseEnvelope"]["payload"]))
            src = st["predicate"]["buildDefinition"]["externalParameters"]["workflow"]["repository"]
            if subject in [x.get("name") for x in st.get("subject") or []] and src == repo:
                return
    raise ValueError(f"npm {pkg}@{version} has no provenance from {repo}")


def _npm(client: httpx.Client, name: str, verify: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    pkg = NPM_PACKAGES[name]
    doc = _json(client, f"{NPM}/{pkg.replace('/', '%2f')}")
    version = (doc.get("dist-tags") or {}).get("latest") or ""
    meta = (doc.get("versions") or {}).get(version) or {}
    if not meta.get("dist", {}).get("integrity"):
        raise ValueError(f"npm {pkg}: dist-tag latest = {version!r}, which has no published integrity")
    detail: dict[str, Any] = {"source": f"npm {pkg} (dist-tag latest)",
                              "checksums": "npm checks every tarball against the registry's sha512 integrity at "
                                           "install", "integrity": meta["dist"]["integrity"]}
    if name == "codex":  # the binary is in a per-platform package: the recipe needs the linux ones published
        missing = [p for p in ("linux-x64", "linux-arm64") if f"{version}-{p}" not in (doc.get("versions") or {})]
        if missing:
            raise ValueError(f"npm {pkg} {version} has no {', '.join(missing)} build yet")
    if verify:
        if repo := NPM_REPOS.get(name):
            for v in [version] + ([f"{version}-linux-x64", f"{version}-linux-arm64"] if name == "codex" else []):
                _provenance(client, pkg, v, repo)
        for cpu in NPM_CPUS.get(name, ("x64",)):
            npm_audit(pkg, version, cpu)
        detail["checksums"] = ("npm's registry signatures" + (" and Sigstore provenance" if repo else "")
                               + " (npm audit signatures)" + (f"; provenance from {repo} for every package the "
                                                              "recipe installs" if repo else
                                                              "; it publishes no provenance"))
    return {"version": version}, detail


def resolve(lock: dict[str, Any], client: httpx.Client | None = None, download: bool = True,
            only: list[str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(candidate lock, report). A harness moves only forward: when its upstream release is the locked one or older
    (Claude Code's stable channel can trail the lock), its entry stays as it is. An error leaves it as it is too, and
    is in the report."""
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30, read=300), follow_redirects=True,
                                    headers={"user-agent": "hangar-canary"})
    candidate = copy.deepcopy(lock)
    report: dict[str, Any] = {"harnesses": {}, "changes": {}}
    try:
        for name in only or list(lock):
            locked = str((lock.get(name) or {}).get("version") or "")
            row: dict[str, Any] = {"locked": locked}
            try:
                if name == "claude-code":
                    entry, detail = _claude(client, download=False)
                    if download and newer(entry["version"], locked):
                        entry, detail = _claude(client, download=True)
                elif name == "antigravity":
                    entry, detail = _agy(client, download=False)
                    if download and newer(entry["version"], locked):
                        entry, detail = _agy(client, download=True)
                elif name in NPM_PACKAGES:
                    entry, detail = _npm(client, name)
                    if download and newer(entry["version"], locked):
                        entry, detail = _npm(client, name, verify=True)
                else:
                    raise ValueError("no release source known")
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                row.update(status="error", detail=f"{type(e).__name__}: {e}"[:500])
                report["harnesses"][name] = row
                continue
            row.update(upstream=entry["version"], **detail)
            if newer(entry["version"], locked):
                row["status"] = "bump"
                candidate[name] = entry
                report["changes"][name] = [locked, entry["version"]]
            else:
                row["status"] = "current" if entry["version"] == locked else "behind"
            report["harnesses"][name] = row
    finally:
        if own:
            client.close()
    harnesses_for(candidate)  # the candidate must pass the same validation as the checked-in lock
    return candidate, report


# ====================================================================== protocol surfaces
# What the driver (driver.py) speaks with each CLI. A change in these is what breaks a live turn; the rest of each
# surface is shown in the review packet for information.
CODEX_DRIVER = {
    "client": ["initialize", "thread/start", "thread/resume", "thread/fork", "turn/start", "turn/steer",
               "turn/interrupt"],
    "server_notifications": ["turn/started", "item/started", "item/completed", "thread/tokenUsage/updated",
                             "turn/completed", "error", "serverRequest/resolved"],
    "server_requests": ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"],
}
# the answers the driver reads (or writes, for the server's approval requests), by the schema file that describes them
CODEX_ANSWERS = {"initialize": "InitializeResponse", "thread/start": "ThreadStartResponse",
                 "thread/resume": "ThreadResumeResponse", "thread/fork": "ThreadForkResponse",
                 "turn/start": "TurnStartResponse", "turn/steer": "TurnSteerResponse",
                 "turn/interrupt": "TurnInterruptResponse",
                 "item/commandExecution/requestApproval": "CommandExecutionRequestApprovalResponse",
                 "item/fileChange/requestApproval": "FileChangeRequestApprovalResponse"}
# What driver.Codex sends and reads, each as (union, method, params | answer, path, values it must still take). Paths
# step through properties ("[]": an array's items) across every branch of a union, e.g. each ThreadItem variant.
_ITEM = ("id", "text", "summary", "command", "aggregatedOutput", "exitCode", "status", "changes[].path",
         "changes[].kind", "server", "tool", "arguments", "result", "error", "query")
_DECISIONS = ("accept", "acceptForSession", "decline")
_OPEN = ("thread/start", "thread/resume", "thread/fork")
CODEX_USES: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    ("client", "initialize", "params", "clientInfo.name", ()),
    ("client", "initialize", "params", "clientInfo.version", ()),
    *[("client", m, "params", f, ()) for m in _OPEN for f in ("cwd", "model")],
    *[("client", m, "params", "sandbox", ("danger-full-access",)) for m in _OPEN],
    *[("client", m, "params", "approvalPolicy", ("never", "untrusted")) for m in _OPEN],
    *[("client", m, "params", "approvalsReviewer", ("user",)) for m in _OPEN],
    ("client", "thread/resume", "params", "threadId", ()),
    ("client", "thread/fork", "params", "threadId", ()),
    *[("client", m, "answer", "thread.id", ()) for m in _OPEN],
    ("client", "turn/start", "params", "threadId", ()),
    ("client", "turn/start", "params", "input[].type", ("text",)),
    ("client", "turn/start", "params", "input[].text", ()),
    ("client", "turn/start", "answer", "turn.id", ()),
    ("client", "turn/start", "answer", "turn.status", ("inProgress",)),
    ("client", "turn/steer", "params", "threadId", ()),
    ("client", "turn/steer", "params", "expectedTurnId", ()),
    ("client", "turn/steer", "params", "input[].text", ()),
    ("client", "turn/interrupt", "params", "threadId", ()),
    ("client", "turn/interrupt", "params", "turnId", ()),
    ("server_notifications", "turn/started", "params", "turn.id", ()),
    *[("server_notifications", m, "params", "item.type",
       ("agentMessage", "reasoning", "commandExecution", "fileChange", "mcpToolCall", "webSearch"))
      for m in ("item/started", "item/completed")],
    *[("server_notifications", m, "params", f"item.{f}", ()) for m in ("item/started", "item/completed")
      for f in _ITEM],
    *[("server_notifications", "thread/tokenUsage/updated", "params", f"tokenUsage.{w}.{f}", ())
      for w in ("total", "last") for f in ("inputTokens", "cachedInputTokens", "outputTokens",
                                           "reasoningOutputTokens")],
    ("server_notifications", "turn/completed", "params", "turn.id", ()),
    ("server_notifications", "turn/completed", "params", "turn.status", ("completed", "failed", "interrupted")),
    ("server_notifications", "turn/completed", "params", "turn.error.message", ()),
    ("server_notifications", "error", "params", "error.message", ()),
    ("server_notifications", "serverRequest/resolved", "params", "requestId", ()),
    ("server_requests", "item/commandExecution/requestApproval", "params", "command", ()),
    ("server_requests", "item/commandExecution/requestApproval", "answer", "decision", _DECISIONS),
    ("server_requests", "item/fileChange/requestApproval", "params", "itemId", ()),
    ("server_requests", "item/fileChange/requestApproval", "params", "grantRoot", ()),
    ("server_requests", "item/fileChange/requestApproval", "params", "reason", ()),
    ("server_requests", "item/fileChange/requestApproval", "answer", "decision", _DECISIONS),
]
CLAUDE_DRIVER = {  # message type -> the fields the driver reads (top level, then one level into the named ones)
    "system/init": ["session_id", "model"],
    "control_request/can_use_tool": ["request_id", "request.subtype", "request.tool_name", "request.input"],
    "control_response/success": ["response.subtype", "response.request_id"],
    "result/success": ["subtype", "is_error", "usage", "modelUsage", "total_cost_usd", "session_id",
                       "queued_turn_count"],
    "command_lifecycle": ["command_uuid", "state"],
    "control_cancel_request": ["request_id"],
}


_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # a field name, not a key of a map (a model id, a tool name)


def _walk(d: Any, prefix: str = "", depth: int = 2) -> Iterator[str]:
    if isinstance(d, dict):
        for k in sorted(d):
            if not _FIELD.fullmatch(str(k)):
                continue
            path = f"{prefix}.{k}" if prefix else str(k)
            yield path
            if depth > 1:
                yield from _walk(d[k], path, depth - 1)


def claude_shape(messages: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The stream-json messages a session wrote, as `type/subtype` -> the field paths seen (values dropped)."""
    out: dict[str, set[str]] = {}
    for m in messages:
        if not isinstance(m, dict):
            continue
        sub = m.get("subtype") or (m.get("request") or {}).get("subtype") or (m.get("response") or {}).get("subtype")
        key = f"{m.get('type')}/{sub}" if sub else str(m.get("type"))
        out.setdefault(key, set()).update(_walk(m))
    return {k: sorted(v) for k, v in sorted(out.items())}


def _schema_methods(schema: Any) -> dict[str, Any]:
    """method -> its params schema, from one of the generated JSON-RPC union schemas (ClientRequest, ...)."""
    out: dict[str, Any] = {}
    for variant in (schema or {}).get("oneOf") or (schema or {}).get("anyOf") or []:
        props = variant.get("properties") or {}
        method = props.get("method") or {}
        names = method.get("enum") or ([method["const"]] if "const" in method else [])
        for n in names:
            out[n] = props.get("params") or {}
    return out


def _strip(schema: Any, defs: dict[str, Any], out: dict[str, Any]) -> Any:
    """`schema` without prose (description, title), its local $refs shortened to the definition's name, and every
    definition it reaches copied into `out` once (the same way), so each type is listed, and diffed, once."""
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            name = ref.rsplit("/", 1)[-1]
            if name not in out and name in defs:
                out[name] = None  # cycles
                out[name] = _strip(defs[name], defs, out)
            return {"$ref": name}
        return {k: _strip(v, defs, out) for k, v in schema.items() if k not in ("description", "title")}
    if isinstance(schema, list):
        return [_strip(v, defs, out) for v in schema]
    return schema


def _load_schema(schema_dir: Path, name: str) -> dict[str, Any] | None:
    """One generated schema file by name (top level first, then v2/, v1/), or None."""
    for p in (schema_dir / f"{name}.json", schema_dir / "v2" / f"{name}.json", schema_dir / "v1" / f"{name}.json"):
        with contextlib.suppress(OSError, ValueError):
            doc = json.loads(p.read_text())
            if isinstance(doc, dict):
                return doc
    return None


def _defs(doc: dict[str, Any]) -> dict[str, Any]:
    return {**(doc.get("$defs") or {}), **(doc.get("definitions") or {})}


def codex_surface(schema_dir: Path) -> dict[str, Any]:
    """The app-server methods the driver uses, with their parameter and answer schemas resolved, from the directory
    `codex app-server generate-json-schema --out DIR` writes; plus the names of every method, for information. The
    types they reach are under `types`, each once."""
    surface: dict[str, Any] = {}
    types: dict[str, Any] = {}
    for kind, union in (("client", "ClientRequest"), ("server_notifications", "ServerNotification"),
                        ("server_requests", "ServerRequest")):
        doc = _load_schema(schema_dir, union) or {}
        defs = _defs(doc)
        methods = _schema_methods(doc)
        driver: dict[str, Any] = {}
        for m in CODEX_DRIVER[kind]:
            if m not in methods:
                driver[m] = None
                continue
            driver[m] = {"params": _strip(methods[m], defs, types)}
            if (answer := CODEX_ANSWERS.get(m)) and (adoc := _load_schema(schema_dir, answer)):
                body = {k: v for k, v in adoc.items() if k not in ("definitions", "$defs", "$schema")}
                driver[m]["answer"] = _strip(body, _defs(adoc), types)
        surface[kind] = {"all": sorted(methods), "driver": driver}
    surface["types"] = dict(sorted(types.items()))
    return surface


def _flat(x: Any, prefix: str = "") -> dict[str, str]:
    """A nested value as path -> JSON leaf, for a readable diff."""
    if isinstance(x, dict):
        out: dict[str, str] = {}
        for k in sorted(x):
            out.update(_flat(x[k], f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(x, list) and x and all(isinstance(i, str) for i in x):  # names, enum values: one line each
        return {f"{prefix}[{i}]": "true" for i in x}
    if isinstance(x, list):
        out = {}
        for i, v in enumerate(x):
            out.update(_flat(v, f"{prefix}[{i}]"))
        return out
    return {prefix: json.dumps(x, sort_keys=True)}


def dump_surface(surface: Any) -> str:
    """A recorded surface as checked in under tests/fixtures/protocol: sorted, one field per line, so a bump's diff
    of the fixture is readable on its own."""
    return json.dumps(surface, indent=1, sort_keys=True) + "\n"


def _expand(node: Any, types: dict[str, Any], seen: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """The object schemas `node` can be: through $refs to `types`, and every branch of oneOf/anyOf/allOf."""
    if node is True:  # JSON Schema's "anything"
        return [{}]
    if not isinstance(node, dict):
        return []
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref in seen or not isinstance(types.get(ref), dict):
            return []
        return _expand(types[ref], types, seen | {ref})
    out = [node]
    for key in ("oneOf", "anyOf", "allOf"):
        for branch in node.get(key) or []:
            out += _expand(branch, types, seen)
    return out


def _at(node: Any, path: str, types: dict[str, Any]) -> list[dict[str, Any]]:
    """The schemas at a dotted path (`[]` steps into an array's items), across every branch on the way."""
    nodes = _expand(node, types)
    for step in [s for s in path.split(".") if s]:
        items = step.endswith("[]")
        name = step[:-2] if items else step
        nxt: list[dict[str, Any]] = []
        for n in nodes:
            prop = (n.get("properties") or {}).get(name) if name else n
            if prop is None:
                continue
            for p in _expand(prop, types):
                nxt += _expand(p.get("items"), types) if items else [p]
        nodes = nxt
    return nodes


def _values(nodes: list[dict[str, Any]], types: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for n in nodes:
        for e in _expand(n, types):
            out.update(str(v) for v in e.get("enum") or [])
            if "const" in e:
                out.add(str(e["const"]))
    return out


def codex_missing(surface: dict[str, Any]) -> list[str]:
    """What driver.Codex sends or reads that this app-server schema no longer has (empty: all there)."""
    types = surface.get("types") or {}
    missing: list[str] = []
    for kind, method, part, path, values in CODEX_USES:
        entry = ((surface.get(kind) or {}).get("driver") or {}).get(method)
        if not entry:
            missing.append(f"{kind}: {method} is gone")
            continue
        nodes = _at(entry.get(part), path, types)
        where = f"{kind}: {method} {part}" + (f" .{path}" if path else "")
        if not nodes:
            missing.append(f"{where} is gone")
        elif values and (lost := sorted(set(values) - _values(nodes, types))):
            missing.append(f"{where} no longer takes {', '.join(lost)}")
    return sorted(set(missing))


def claude_missing(shape: dict[str, list[str]]) -> list[str]:
    """What driver.Claude reads that this Claude Code no longer writes (empty: all there)."""
    missing = []
    for key, fields in CLAUDE_DRIVER.items():
        if key not in shape:
            missing.append(f"{key}: not written any more")
            continue
        missing += [f"{key}: {f} is gone" for f in fields if f not in shape[key]]
    return missing


def diff_surface(name: str, locked: Any, candidate: Any) -> dict[str, Any]:
    """{breaking, added, changed} between the locked CLI's surface and a candidate's. Breaking: something the driver
    sends or reads is gone (codex_missing, claude_missing; an unrecorded lock counts everything as new). Added and
    changed: every other difference, for the reviewer."""
    locked, candidate = locked or {}, candidate or {}
    missing = codex_missing if name == "codex" else claude_missing
    was = set(missing(locked)) if locked else set()
    breaking = [m for m in missing(candidate) if m not in was]
    fa = _flat({k: v for k, v in locked.items()} if name == "codex" else locked)
    fb = _flat({k: v for k, v in candidate.items()} if name == "codex" else candidate)
    added = [f"{p} = {fb[p]}" for p in sorted(set(fb) - set(fa))]
    changed = [f"{p}: {fa[p]} -> {fb[p]}" for p in sorted(set(fa) & set(fb)) if fa[p] != fb[p]]
    changed += [f"{p} = {fa[p]} is gone" for p in sorted(set(fa) - set(fb))]
    return {"breaking": breaking, "added": added, "changed": changed}


def diff_dirs(locked: Path, candidate: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, file in (("codex", "codex-app-server.json"), ("claude-code", "claude-stream-json.json")):
        a, b = locked / file, candidate / file
        if not b.exists():
            out[name] = {"missing": f"the candidate run wrote no {file}"}
            continue
        out[name] = diff_surface("codex" if name == "codex" else "claude",
                                 json.loads(a.read_text()) if a.exists() else None, json.loads(b.read_text()))
    return out


# ====================================================================== the review packet
def junit_results(paths: list[Path]) -> dict[str, Any]:
    """Pass/fail/skip counts and the failures, from pytest's --junitxml files. --- testresults: through hangar's one
    report parser (testreports.py), which reads them safely and caps them."""
    from hangar import testreports

    res: dict[str, Any] = {"passed": 0, "failed": 0, "skipped": 0, "failures": []}
    for p in paths:
        try:
            r = testreports.parse_junit(p.read_bytes(), p.name)
        except OSError:
            r = testreports.Report(source=p.name, error="unreadable")
        if r.error or r.kind != "junit":
            res["failures"].append({"test": p.name, "message": "no readable test report"})
            res["failed"] += 1
            continue
        for case in r.cases:
            if case.outcome in ("failed", "error"):
                res["failed"] += 1
                res["failures"].append({"test": f"{case.classname}::{case.name}", "message": case.message[-1500:]})
            elif case.outcome == "skipped":
                res["skipped"] += 1
            else:
                res["passed"] += 1
    return res


def _bullets(items: list[str], limit: int = 40) -> str:
    if not items:
        return "_none_\n"
    more = f"- … and {len(items) - limit} more\n" if len(items) > limit else ""
    return "".join(f"- `{i}`\n" for i in items[:limit]) + more


def packet(resolved: dict[str, Any], diff: dict[str, Any], tests: dict[str, Any], run_url: str = "") -> str:
    """The markdown for the lock-bump PR (or, when something failed, the issue)."""
    lines = ["## Harness canary", ""]
    if run_url:
        lines += [f"Run: {run_url}", ""]
    lines += ["| harness | locked | upstream | status | source | checksums |", "|---|---|---|---|---|---|"]
    for name, row in resolved.get("harnesses", {}).items():
        lines.append(f"| {name} | {row.get('locked', '')} | {row.get('upstream', '—')} | {row.get('status')}"
                     f"{': ' + row['detail'] if row.get('detail') else ''} | {row.get('source', '')} "
                     f"| {row.get('checksums', '')} |")
    lines += ["", "### Contract tests against the candidate versions", "",
              f"{tests.get('passed', 0)} passed, {tests.get('failed', 0)} failed, {tests.get('skipped', 0)} skipped.",
              ""]
    for f in tests.get("failures") or []:
        lines += [f"<details><summary><code>{f['test']}</code></summary>", "", "```", f["message"], "```", "",
                  "</details>", ""]
    lines += ["### Protocol diff against the locked versions", ""]
    for name, d in diff.items():
        if d.get("missing"):
            lines += [f"**{name}**: {d['missing']}", ""]
            continue
        lines += [f"**{name}**: {len(d['breaking'])} breaking, {len(d['added'])} added, {len(d['changed'])} "
                  "other changes", "", "Breaking (what the driver relies on):", "", _bullets(d["breaking"]),
                  "<details><summary>Added and other changes</summary>", "", _bullets(d["added"] + d["changed"], 80),
                  "</details>", ""]
    return "\n".join(lines)


def failed(resolved: dict[str, Any], diff: dict[str, Any], tests: dict[str, Any]) -> list[str]:
    """Why the canary can't propose this bump (empty: it can)."""
    why = [f"{n}: {r.get('detail')}" for n, r in resolved.get("harnesses", {}).items() if r.get("status") == "error"]
    if tests.get("failed"):
        why.append(f"{tests['failed']} contract test(s) failed")
    if tests.get("skipped"):
        why.append(f"{tests['skipped']} contract test(s) skipped: a CLI is missing")
    if not tests.get("passed"):
        why.append("no contract test ran")
    for name, d in diff.items():
        if d.get("missing"):
            why.append(f"{name}: {d['missing']}")
        elif d.get("breaking"):
            why.append(f"{name}: {len(d['breaking'])} breaking protocol change(s)")
    return why


# ====================================================================== landing the bump (#96's rule, in CI)
# The bump PR lands like an agent's PR with `land: auto_after_approval`: one person's approval of its current head
# arms auto-merge for exactly that commit, and branch protection stays the gate. It runs in CI rather than in the
# control plane because it must work while the cluster is down; the rule is the same. Only a diff limited to the
# lock and the recorded protocols lands this way (hardening-r2): anything else needs a person to merge it.
CANARY_BRANCH = "canary/harness-lock"
# Exactly these files (--- review #105): a prefix would let a conftest.py or test_*.py in among the fixtures, and
# pytest collects tests/ recursively.
LANDABLE = ("src/hangar/harnesses.lock.yaml", "tests/fixtures/protocol/codex-app-server.json",
            "tests/fixtures/protocol/claude-stream-json.json")
_ARM = ("mutation($id: ID!, $sha: GitObjectID!, $method: PullRequestMergeMethod!) {"
        " enablePullRequestAutoMerge(input: {pullRequestId: $id, expectedHeadOid: $sha, mergeMethod: $method})"
        " { pullRequest { number } } }")
_DISARM = "mutation($id: ID!) { disablePullRequestAutoMerge(input: {pullRequestId: $id}) { pullRequest { number } } }"


class LandRefused(Exception):
    pass


def github_client(token: str, api: str = "https://api.github.com") -> httpx.Client:
    return httpx.Client(base_url=api, timeout=30, headers={
        "authorization": f"Bearer {token}", "accept": "application/vnd.github+json",
        "x-github-api-version": "2022-11-28", "user-agent": "hangar-canary"})


def _ok(r: httpx.Response) -> Any:
    if r.status_code >= 400:
        raise LandRefused(f"GitHub {r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}")
    return r.json() if r.content else None


def _graphql(gh: httpx.Client, query: str, variables: dict[str, Any]) -> Any:
    d = _ok(gh.post("/graphql", json={"query": query, "variables": variables})) or {}
    if d.get("errors"):
        raise LandRefused("; ".join(str(e.get("message")) for e in d["errors"])[:500])
    return d.get("data")


def _pages(gh: httpx.Client, path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 11):
        got = _ok(gh.get(path, params={"per_page": 100, "page": page})) or []
        out += [x for x in got if isinstance(x, dict)]
        if len(got) < 100:
            return out
    raise LandRefused(f"{path} has more than 1000 entries")


def land(gh: httpx.Client, repo: str, number: int, approvers: list[str], merge_method: str = "squash") -> str:
    """Arm auto-merge on the bump PR for its approved head, or merge it when it is already mergeable. Raises
    LandRefused (nothing armed) unless every condition holds; returns what it did. GitHub's own record decides,
    not the event that started the run: webhooks come late and out of order."""
    pr = _ok(gh.get(f"/repos/{repo}/pulls/{number}")) or {}
    head = pr.get("head") or {}
    sha = str(head.get("sha") or "")
    if pr.get("state") != "open" or pr.get("merged"):
        raise LandRefused("the PR isn't open")
    if head.get("ref") != CANARY_BRANCH or (head.get("repo") or {}).get("full_name") != repo:
        raise LandRefused(f"not the canary's PR (head {head.get('ref')!r} of "
                          f"{(head.get('repo') or {}).get('full_name')})")
    author = str((pr.get("user") or {}).get("login") or "").lower()
    allowed = {a.strip().lower() for a in approvers if a.strip()} - {author}
    latest: dict[str, tuple[str, str]] = {}
    for r in _pages(gh, f"/repos/{repo}/pulls/{number}/reviews"):  # oldest first: the last one per person stands
        login, state = str((r.get("user") or {}).get("login") or "").lower(), str(r.get("state") or "").upper()
        if login and state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = (state, str(r.get("commit_id") or ""))
    if blockers := sorted(n for n, (st, _) in latest.items() if st == "CHANGES_REQUESTED"):
        raise LandRefused("changes requested by " + ", ".join(f"@{n}" for n in blockers))
    by = sorted(n for n, (st, c) in latest.items() if st == "APPROVED" and c == sha and n in allowed)
    if not by:
        raise LandRefused(f"no approval of the head {sha[:12]} from an approver "
                          f"({', '.join(sorted(allowed)) or 'none configured'})")
    files = _pages(gh, f"/repos/{repo}/pulls/{number}/files")
    names = [str(f.get(k) or "") for f in files for k in ("filename", "previous_filename")]
    if other := sorted(n for n in names if n and n not in LANDABLE):
        raise LandRefused(f"the PR changes more than the lock and the recorded protocols: {', '.join(other[:5])}")
    if gone := sorted(str(f.get("filename")) for f in files if f.get("status") == "removed"):
        raise LandRefused(f"the PR removes {', '.join(gone)}")
    # The approver isn't the author (a bot), but could have pushed the head: then they would approve their own
    # change. GitHub's record of the branch's pushes says who pushed it; only the canary's own push lands this way.
    pushes = _ok(gh.get(f"/repos/{repo}/activity", params={"ref": f"refs/heads/{CANARY_BRANCH}",
                                                           "per_page": 100})) or []
    push = next((p for p in pushes if isinstance(p, dict) and p.get("after") == sha), None)
    if push is None:
        raise LandRefused(f"no record of who pushed {sha[:12]} to {CANARY_BRANCH}")
    actor = push.get("actor") or {}
    if actor.get("type") != "Bot":
        raise LandRefused(f"{sha[:12]} was pushed by @{actor.get('login')}, not by the canary: a person's change "
                          "to the bump is landed by hand, by someone else")
    try:
        _graphql(gh, _ARM, {"id": pr.get("node_id"), "sha": sha, "method": merge_method.upper()})
    except LandRefused as e:
        if "clean status" not in str(e).lower():
            raise
        # already mergeable, so GitHub won't arm: merge now, only if the head is still the approved one
        _ok(gh.put(f"/repos/{repo}/pulls/{number}/merge", json={"sha": sha, "merge_method": merge_method}))
        return f"merged {sha[:12]} after @{by[0]}'s approval (it was already mergeable)"
    # A push while GitHub was arming (the canary's next candidate) found nothing armed to undo, and GitHub keeps
    # auto-merge on after a push by anyone with write access (#96's finding 2): look again, and undo it.
    now_head = str(((_ok(gh.get(f"/repos/{repo}/pulls/{number}")) or {}).get("head") or {}).get("sha") or "")
    if now_head != sha:
        disarm(gh, repo, number)
        raise LandRefused(f"the head moved to {now_head[:12]} while arming; it needs a new approval")
    return f"auto-merge armed for {sha[:12]} after @{by[0]}'s approval; GitHub merges it once its branch rules are met"


def disarm(gh: httpx.Client, repo: str, number: int) -> None:
    """Before the canary pushes a newer candidate onto an open bump PR: nothing armed for the old head rides along
    (branch protection also dismisses the stale approval; this doesn't depend on it)."""
    pr = _ok(gh.get(f"/repos/{repo}/pulls/{number}")) or {}
    if pr.get("state") == "open" and pr.get("auto_merge") is not None:
        _graphql(gh, _DISARM, {"id": pr.get("node_id")})


# ====================================================================== CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hangar.canary", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="install the CLIs a lock pins, without root")
    p.add_argument("--dest", type=Path, required=True)
    p.add_argument("--lock", type=Path, default=LOCK_FILE)
    p.add_argument("--only", default="", help="comma-separated harnesses, or `all` (default: the contract four)")
    p.add_argument("--env-file", type=Path, help="append HANGAR_TEST_*=<binary> lines here (e.g. $GITHUB_ENV)")
    p = sub.add_parser("resolve", help="the newest stable upstream releases, as a candidate lock")
    p.add_argument("--lock", type=Path, default=LOCK_FILE)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--no-download", action="store_true", help="trust the published checksums without downloading")
    p = sub.add_parser("diff", help="protocol surfaces: the locked CLIs' against the candidates'")
    p.add_argument("--locked", type=Path, required=True)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p = sub.add_parser("junit", help="the contract tests' results; fails on a failure, a skip, or nothing run")
    p.add_argument("files", type=Path, nargs="+")
    p = sub.add_parser("check", help="a candidate lock and protocol records, before they are committed")
    p.add_argument("--lock", type=Path, required=True)
    p.add_argument("--protocol", type=Path, required=True)
    p = sub.add_parser("packet", help="the review packet (markdown) and whether the canary passed")
    p.add_argument("--resolve", type=Path, required=True)
    p.add_argument("--diff", type=Path, required=True)
    p.add_argument("--tests", type=Path, nargs="*", default=[])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--run-url", default="")
    for name, text in (("land", "arm auto-merge on the approved bump PR"),
                       ("disarm", "turn auto-merge off on the bump PR before a push")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--repo", required=True)
        p.add_argument("--pr", type=int, required=True)
        if name == "land":
            p.add_argument("--approvers", default="", help="comma-separated GitHub logins")
            p.add_argument("--merge-method", default="squash", choices=("squash", "merge", "rebase"))
    a = ap.parse_args(argv)

    if a.cmd == "install":
        only = [x for x in a.only.split(",") if x] or None
        try:
            install(load_lock(a.lock), a.dest, only)
        except (RuntimeError, ValueError) as e:
            print(e, file=sys.stderr)
            return 1
        if a.env_file:
            with a.env_file.open("a") as f:
                f.writelines(f"{k}={v}\n" for k, v in test_env(a.dest, None if only == ["all"] else only).items())
        return 0
    if a.cmd == "resolve":
        text = a.lock.read_text()
        lock = yaml.safe_load(text) or {}
        candidate, report = resolve(lock, download=not a.no_download)
        changes = {k: (v[0], v[1]) for k, v in report["changes"].items()}
        header = bumped_header(lock_header(text), changes, datetime.date.today().isoformat()) if changes \
            else lock_header(text)
        a.out.write_text(dump_lock(candidate, header))
        a.report.write_text(json.dumps(report, indent=2))
        errors = {k: r["detail"] for k, r in report["harnesses"].items() if r["status"] == "error"}
        print(json.dumps({"changes": report["changes"], "errors": errors}))
        return 0
    if a.cmd == "check":  # --- review #105: what `publish` commits was written where the candidates ran
        try:
            harnesses_for(yaml.safe_load(a.lock.read_text()) or {})
            for name in LANDABLE[1:]:
                if not isinstance(json.loads((a.protocol / Path(name).name).read_text()), dict):
                    raise ValueError(f"{Path(name).name} isn't a protocol record")
        except (OSError, ValueError, yaml.YAMLError) as e:
            print(f"refusing the candidate: {e}", file=sys.stderr)
            return 1
        return 0
    if a.cmd == "diff":
        a.report.write_text(json.dumps(diff_dirs(a.locked, a.candidate), indent=2))
        return 0
    if a.cmd == "junit":
        res = junit_results(a.files)
        print(f"{res['passed']} passed, {res['failed']} failed, {res['skipped']} skipped")
        for f in res["failures"]:
            print(f"FAILED {f['test']}: {f['message'][:300]}")
        if res["skipped"]:
            print("a contract test skipped: every CLI the lock pins must be installed (HANGAR_TEST_*)")
        return 1 if res["failed"] or res["skipped"] or not res["passed"] else 0
    if a.cmd in ("land", "disarm"):
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        if not token:
            print("set GH_TOKEN", file=sys.stderr)
            return 1
        with github_client(token, os.environ.get("GITHUB_API_URL") or "https://api.github.com") as gh:
            try:
                if a.cmd == "disarm":
                    disarm(gh, a.repo, a.pr)
                    return 0
                print(land(gh, a.repo, a.pr, a.approvers.split(","), a.merge_method))
            except LandRefused as e:
                print(f"not landing #{a.pr}: {e}")
                return 3
            except httpx.HTTPError as e:
                print(f"GitHub unreachable: {e}", file=sys.stderr)
                return 1
        return 0
    resolved = json.loads(a.resolve.read_text())
    diff = json.loads(a.diff.read_text()) if a.diff.exists() else {}
    tests = junit_results(a.tests)
    why = failed(resolved, diff, tests)
    body = packet(resolved, diff, tests, a.run_url)
    if why:
        body = "**The canary failed:**\n\n" + "".join(f"- {w}\n" for w in why) + "\n" + body
    a.out.write_text(body)
    print(json.dumps({"ok": not why, "why": why, "changes": resolved.get("changes", {})}))
    return 0 if not why else 2


if __name__ == "__main__":
    sys.exit(main())
