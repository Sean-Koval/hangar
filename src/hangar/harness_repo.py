"""The team harness repo (#138; docs/harness-lock.md#the-team-harness-repo): where `harness_lock: name@stable` resolves.

`HANGAR_HARNESS_REPO` is either a local directory (a checkout someone else keeps current, as before) or a git URL
with an optional `#ref` (`https://github.com/acme/team-harnesses#main`). For a URL, hangar keeps a full checkout of
its own under `<data>/harness-repo/<id>/` on the control plane (the history matters: `name@rN` and digests that aren't
current are found in it), refreshed every `HANGAR_HARNESS_REPO_REFRESH_SECONDS` (default 600) and at once on a
GitHub `push` webhook for that repo. A private repo is read with the control plane's git credential
(`HANGAR_GATEWAY_CREDENTIAL`, else the GitHub App, `contents: read`), never a sandbox's; the token goes to git in its
environment (`GIT_CONFIG_*`), never on a command line or in the checkout's config. A failed refresh keeps the last
good checkout and says why (`status`, `hangar doctor`).

`HANGAR_DEFAULT_HARNESS` (off by default; e.g. `team-default@stable`) is the lock intake jobs (GitHub, Jira, Slack,
Projects and webhook jobs) run from when they don't name a harness, model or lock themselves (`default_for`).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from hangar.fleet import Fleet

log = logging.getLogger("hangar.harness_repo")

DIR = "harness-repo"
GIT_TIMEOUT = 300
_URL = re.compile(r"^(https?|ssh|git|file)://|^[\w.-]+@[\w.-]+:")


def is_url(raw: str | None) -> bool:
    return bool(raw) and bool(_URL.match(str(raw).strip()))


def split(raw: str) -> tuple[str, str | None]:
    """`url#ref` -> (url, ref)."""
    url, _, ref = raw.strip().partition("#")
    return url, (ref or None)


def checkout_dir(settings: Any) -> Path:
    url, ref = split(str(settings.harness_repo))
    key = hashlib.sha256(f"{url}#{ref or ''}".encode()).hexdigest()[:16]
    return Path(settings.data_dir) / DIR / key


def _status_file(settings: Any) -> Path:
    return checkout_dir(settings).with_suffix(".json")


def status(settings: Any) -> dict[str, Any]:
    """{source, kind: path|git, ok, last_refresh, commit, error}: what the registry is and how fresh."""
    raw = getattr(settings, "harness_repo", None)
    if not raw:
        return {"source": None, "kind": None, "ok": False, "error": "HANGAR_HARNESS_REPO is not set"}
    if not is_url(raw):
        root = Path(str(raw)).expanduser()
        head = _git(root, "rev-parse", "HEAD") if root.is_dir() else None
        return {"source": str(root), "kind": "path", "ok": root.is_dir(), "last_refresh": None,
                "commit": head.strip() if head else None,
                "error": None if root.is_dir() else f"{root} is not a directory"}
    url, ref = split(str(raw))
    st: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        st = json.loads(_status_file(settings).read_text())
    have = (checkout_dir(settings) / ".git").exists()
    return {"source": _safe_url(url), "ref": ref, "kind": "git", "ok": bool(st.get("ok")) and have,
            "last_refresh": st.get("at"), "last_success": st.get("success_at"), "commit": st.get("commit"),
            "error": st.get("error") if not st.get("ok") else None, "checkout": have}


def _safe_url(url: str) -> str:
    return re.sub(r"//[^/@]*@", "//", url)  # never show a token someone put in the URL


def _git(root: Path, *args: str, env: dict[str, str] | None = None, timeout: int = 20) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _run(args: list[str], env: dict[str, str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=GIT_TIMEOUT,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **env})
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-400:]


async def _auth_env(fleet: Fleet, url: str) -> dict[str, str]:
    """Git's environment for reading a private GitHub repo with the control plane's credential ({} without one)."""
    from hangar.gateway import mint_installation_token, parse_app_credential, parse_remote
    from hangar.models import CredentialKind

    s = fleet.settings
    remote = parse_remote(url, [h.lower() for h in s.gateway_upstreams])
    name = s.gateway_credential or s.github_app_credential
    if remote is None or not name:
        return {}
    try:
        cred = fleet.broker.get(name)
        value = fleet.broker.secret_text(name)
    except Exception:  # noqa: BLE001 - a missing credential: try anonymously, the error says the rest
        return {}
    if cred.kind == CredentialKind.GITHUB_APP:
        import httpx

        async with httpx.AsyncClient(timeout=20) as c:
            value, _ = await mint_installation_token(c, s.github_api_url, parse_app_credential(value), remote[1],
                                                     {"contents": "read"})
    elif cred.kind != CredentialKind.GIT_TOKEN:
        return {}
    basic = base64.b64encode(f"x-access-token:{value}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}


def _sync(settings: Any, env: dict[str, str]) -> dict[str, Any]:
    """Clone or fetch, then check out the ref (or the remote's default branch) detached. Blocking."""
    url, ref = split(str(settings.harness_repo))
    d = checkout_dir(settings)
    d.parent.mkdir(parents=True, exist_ok=True)
    if not (d / ".git").exists():
        tmp = d.with_name(d.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        ok, out = _run(["git", "clone", "-q", "--no-checkout", url, str(tmp)], env)
        if not ok:
            shutil.rmtree(tmp, ignore_errors=True)
            return {"ok": False, "error": f"git clone failed: {out}"}
        tmp.rename(d)
    ok, out = _run(["git", "-C", str(d), "fetch", "-q", "--prune", "--tags", "origin",
                    "+refs/heads/*:refs/remotes/origin/*"], env)
    if not ok:
        return {"ok": False, "error": f"git fetch failed: {out}"}
    target = f"origin/{ref}" if ref else "origin/HEAD"
    if not ref:
        _run(["git", "-C", str(d), "remote", "set-head", "origin", "--auto"], env)
    if ref and _git(d, "rev-parse", "-q", "--verify", f"{target}^{{commit}}") is None:
        target = ref  # a tag or a commit
    ok, out = _run(["git", "-C", str(d), "checkout", "-q", "--force", "--detach", target], {})
    if not ok:
        return {"ok": False, "error": f"git checkout {target} failed: {out}"}
    _run(["git", "-C", str(d), "clean", "-qfdx"], {})
    head = (_git(d, "rev-parse", "HEAD") or "").strip() or None
    return {"ok": True, "commit": head, "error": None}


class HarnessRepo:
    """The fleet's harness repo checkout: refreshed in the background, on a push webhook, or on demand."""

    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def remote(self) -> bool:
        return is_url(getattr(self.fleet.settings, "harness_repo", None))

    async def refresh(self) -> dict[str, Any]:
        """Bring the checkout up to date; never raises. Returns `status`."""
        s = self.fleet.settings
        if not self.remote:
            return status(s)
        async with self._lock:
            url, _ = split(str(s.harness_repo))
            try:
                env = await _auth_env(self.fleet, url)
                res = await asyncio.to_thread(_sync, s, env)
            except Exception as e:  # noqa: BLE001 - the last good checkout stays
                res = {"ok": False, "error": str(e)[:400]}
            prev: dict[str, Any] = {}
            with contextlib.suppress(OSError, ValueError):
                prev = json.loads(_status_file(s).read_text())
            now = time.time()
            rec = {"at": now, "ok": res["ok"], "error": self.fleet.broker.redact(res.get("error")) if
                   res.get("error") else None, "commit": res.get("commit") or prev.get("commit"),
                   "success_at": now if res["ok"] else prev.get("success_at")}
            _status_file(s).parent.mkdir(parents=True, exist_ok=True)
            _status_file(s).write_text(json.dumps(rec))
            if not res["ok"]:
                log.warning("harness repo %s: %s", _safe_url(url), rec["error"])
        return status(s)

    def start(self) -> None:
        if not self.remote or self._task is not None:
            return
        every = max(30, int(getattr(self.fleet.settings, "harness_repo_refresh_seconds", 600) or 600))

        async def loop() -> None:
            while True:
                await self.refresh()
                await asyncio.sleep(every)

        self._task = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def on_push(self, full_name: str) -> bool:
        """A GitHub push webhook for `owner/repo`: refresh now when it is the harness repo."""
        from hangar.gateway import parse_remote

        if not self.remote:
            return False
        url, _ = split(str(self.fleet.settings.harness_repo))
        r = parse_remote(url, [h.lower() for h in self.fleet.settings.gateway_upstreams])
        if r is None or r[1] != full_name.lower():
            return False
        asyncio.get_running_loop().create_task(self.refresh())
        return True


def of(fleet: Fleet) -> HarnessRepo:
    r = fleet.__dict__.get("_harness_repo")
    if r is None:
        r = fleet.__dict__["_harness_repo"] = HarnessRepo(fleet)
    return r


def root(settings: Any) -> Path:
    """The registry directory resolution reads: the configured directory, or hangar's checkout of the URL."""
    from hangar.harness_thaw import ThawError

    raw = getattr(settings, "harness_repo", None)
    if not raw:
        raise ThawError("no harness registry: set HANGAR_HARNESS_REPO to the team harness repo (a git URL, or a "
                        "local checkout; harnesses/<name>/harness.lock)")
    if not is_url(raw):
        d = Path(str(raw)).expanduser()
        if not d.is_dir():
            raise ThawError(f"HANGAR_HARNESS_REPO {d} is not a directory (a checkout of the team harness repo)")
        return d
    d = checkout_dir(settings)
    if not (d / ".git").exists() or _git(d, "rev-parse", "-q", "--verify", "HEAD") is None:
        st = status(settings)
        raise ThawError(f"the harness repo {st['source']} hasn't been fetched yet"
                        + (f" ({st['error']})" if st.get("error") else " (it refreshes in the background)"))
    return d


# ====================================================================== listing
def names(reg: Path) -> list[str]:
    from hangar import harness_lock

    out = set()
    for base in (reg / "harnesses", reg):
        if base.is_dir():
            out |= {d.name for d in base.iterdir() if (d / harness_lock.LOCK_FILE).is_file()}
    return sorted(out)


def listing(settings: Any) -> dict[str, Any]:
    """`GET /api/harness-locks`: each harness's current revision and digest, and each channel's digest and revision."""
    from hangar import harness_lock, harness_thaw

    reg = root(settings)
    channels: dict[str, Any] = {}
    with contextlib.suppress(OSError, yaml.YAMLError):
        doc = yaml.safe_load((reg / harness_thaw.CHANNELS_FILE).read_text()) or {}
        channels = doc if isinstance(doc, dict) else {}
    out = []
    for name in names(reg):
        row: dict[str, Any] = {"name": name, "revision": None, "digest": None, "channels": {}, "error": None}
        try:
            cur = harness_thaw.resolve(settings, name)
            row.update(revision=cur.lock["revision"], digest=cur.digest, harness=cur.lock["runtime"]["harness"],
                       path=cur.path)
        except harness_thaw.ThawError as e:
            row["error"] = str(e)
        for ch, digest in sorted(((channels.get(name) or {}) if isinstance(channels.get(name), dict) else {}).items()):
            c: dict[str, Any] = {"digest": digest, "revision": None, "resolved": False}
            with contextlib.suppress(harness_thaw.ThawError):
                r = harness_thaw.resolve(settings, f"{name}@{ch}")
                c.update(revision=r.lock["revision"], resolved=True)
            row["channels"][ch] = c
        out.append(row)
    del harness_lock
    return {"registry": status(settings), "harnesses": out}


# ====================================================================== the intake default
def default_for(settings: Any, source: str | None, harness: str | None, model: str | None,
                lock: str | None) -> str | None:
    """The lock an intake job runs from when HANGAR_DEFAULT_HARNESS is set and the job names no harness, model or
    lock of its own."""
    from hangar import posture

    ref = getattr(settings, "default_harness", None)
    if not ref or lock or harness or model or source not in posture.INTAKE_SOURCES:
        return None
    return ref


# ====================================================================== doctor
def doctor_checks(h: Any) -> list[dict[str, Any]]:
    from hangar.doctor import _check

    s = h.settings
    if not getattr(s, "harness_repo", None):
        if getattr(s, "default_harness", None):
            return [_check("harness repo", False, "HANGAR_DEFAULT_HARNESS is set but HANGAR_HARNESS_REPO isn't",
                           "set HANGAR_HARNESS_REPO to the team harness repo (docs/harness-lock.md)")]
        return []
    st = status(s)
    when = (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(st["last_success"])) if st.get("last_success")
            else "never")
    if not st["ok"]:
        fix = ("give the control plane's git credential (HANGAR_GATEWAY_CREDENTIAL or the GitHub App) read access to "
               "it" if st["kind"] == "git" else "point HANGAR_HARNESS_REPO at a checkout")
        return [_check("harness repo", False, f"{st['source']}: {st.get('error') or 'not readable'}; last good "
                                              f"refresh {when}", fix)]
    out = [_check("harness repo", True, f"{st['source']} at {(st.get('commit') or '?')[:12]}"
                  + (f", refreshed {when}" if st["kind"] == "git" else ""))]
    try:
        rows = listing(s)["harnesses"]
    except Exception as e:  # noqa: BLE001
        return [*out, _check("harness channels", False, f"could not list the harnesses: {e}")]
    bad = [f"{r['name']}@{ch}" for r in rows for ch, c in r["channels"].items() if not c["resolved"]]
    bad += [r["name"] for r in rows if r["error"]]
    chans = sum(len(r["channels"]) for r in rows)
    out.append(_check("harness channels", not bad,
                      f"{len(rows)} harness(es), {chans} channel(s) resolved" if not bad else
                      f"don't resolve: {', '.join(bad[:8])}",
                      "fix channels.yaml or the lock it points at (`hangar harness check`)"))
    if (d := getattr(s, "default_harness", None)) and not any(d.split("@")[0] == r["name"] for r in rows):
        out.append(_check("default harness", False, f"HANGAR_DEFAULT_HARNESS={d}: no such harness in the repo",
                          "unset it or freeze that harness into the repo"))
    return out
