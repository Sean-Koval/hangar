"""`hangar doctor --local` (KAN-351): check this machine before any server, with no login.

`hangar doctor` asks a running hangar. On a fresh or locked-down laptop there is none yet, so this checks what
`hangar up` needs from the machine itself: python and uv, a container runtime, the port, a TLS-inspecting proxy,
reachability of the model and GitHub hosts, and whether jobs would find an API key. Each line carries its fix, with
the same marks and `--json` shape as `hangar doctor`. Its only network calls are one HTTPS request per host. Only
a ✗ stops `hangar up`: a check that can't run (no network, an odd proxy setting) is a `!` with the reason, never a
crash.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import httpx

from hangar.doctor import _check

GITHUB = "github.com"
TLS_HOSTS = ("api.anthropic.com", GITHUB)  # model calls and image builds: a proxy may inspect one and not the other
CA_VAR = "HANGAR_EXTRA_CA_BUNDLE"
TIMEOUT = httpx.Timeout(5.0, connect=3.0)
DEADLINE = 8.0  # every network probe together: `hangar up` never waits on a blackholed network for long
_PEM = b"-----BEGIN CERTIFICATE-----"
_PEM_MAX = 1 << 20
_CREDS = re.compile(r"(://)[^/@\s]+@")

# A probe's answer: (why the host can't be reached, or None; whether TLS verified against the public roots: True,
# False when a certificate failed (the host still answered), None when it couldn't be tried; the certificate error)
Probe = tuple[str | None, bool | None, str | None]


# ---------------------------------------------------------------------------- probes (tests replace these)
def _docker_info() -> tuple[dict[str, Any] | None, str | None]:
    """The daemon's `docker info`, or why it can't be reached."""
    try:
        import docker

        client = docker.from_env(timeout=5)
        try:
            return client.info(), None
        finally:
            client.close()
    except Exception as e:  # noqa: BLE001 - any failure means "not usable"
        return None, _redact(str(e).splitlines()[0][:200] if str(e) else type(e).__name__)


def _uv_version() -> str | None:
    if not (uv := shutil.which("uv")):
        return None
    try:
        out = subprocess.run([uv, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.split()[1] if len(out.split()) > 1 else out or "?"


def _port_free(port: int) -> bool:
    """Nothing answers on 127.0.0.1 or ::1 (a dev server may bind only `localhost`'s IPv6), and 127.0.0.1 binds."""
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((addr, port)) == 0:
                    return False
        except OSError:
            continue  # no IPv6 here
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":  # as uvicorn does: a stopped hangar's TIME_WAIT connections don't hold the port
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _local() -> httpx.Client:
    """For 127.0.0.1 only: never through a proxy, so the token sent to the local hangar stays on this machine."""
    return httpx.Client(trust_env=False, timeout=10)


def _hangar_on(url: str) -> bool:
    """A hangar answers: its /healthz says exactly {"ok": true} (a dev server's catch-all page doesn't)."""
    try:
        with _local() as c:
            r = c.get(f"{url}/healthz", timeout=2)
        return r.status_code == 200 and r.json() == {"ok": True}
    except Exception:  # noqa: BLE001 - not a hangar, whatever went wrong
        return False


def _probe(host: str, verify: str | None = None) -> Probe:
    """One HEAD to https://host, nothing sent but the request, verified against the public roots (certifi), or the
    file `verify`. Not this machine's own trust (SSL_CERT_FILE and the like): a company CA trusted here is still
    unknown inside hangar's images, which is what HANGAR_EXTRA_CA_BUNDLE fixes."""
    import certifi

    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        if verify:
            ctx.load_verify_locations(cafile=os.path.expanduser(verify))
        with httpx.Client(verify=ctx, timeout=TIMEOUT, follow_redirects=False) as c:
            c.head(f"https://{host}/")
        return None, True, None
    except Exception as e:  # noqa: BLE001 - a bad proxy URL or a missing SOCKS extra is a reason, not a crash
        if why := _cert_error(e):
            return None, False, why  # the host answered; its certificate didn't verify
        return _why(e), None, None


def probe_all(hosts: list[str], probe: Callable[[str], Probe] | None = None,
              deadline: float = DEADLINE) -> dict[str, Probe]:
    """Every host at once, under one deadline."""
    if not hosts:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(8, len(hosts)))
    futures = {pool.submit(probe or _probe, h): h for h in hosts}
    done, _ = wait(futures, timeout=deadline)
    pool.shutdown(wait=False, cancel_futures=True)
    out: dict[str, Probe] = {}
    for f, h in futures.items():
        if f not in done:
            out[h] = (f"no answer in {deadline:g} s", None, None)
            continue
        try:
            out[h] = f.result()
        except Exception as e:  # noqa: BLE001
            out[h] = (_why(e), None, None)
    return out


def _cert_error(e: BaseException) -> str | None:
    """The certificate failure behind `e`, if that's what it is."""
    seen: BaseException | None = e
    while seen is not None:
        if isinstance(seen, ssl.SSLCertVerificationError):
            return getattr(seen, "verify_message", None) or "certificate verify failed"
        if "CERTIFICATE_VERIFY_FAILED" in str(seen):
            return "certificate verify failed"
        seen = seen.__cause__ or seen.__context__
    return None


def _why(e: BaseException) -> str:
    if isinstance(e, ImportError) and "socks" in str(e).lower():
        return "the SOCKS proxy in HTTPS_PROXY needs httpx's SOCKS support: pip install 'httpx[socks]'"
    text = str(e).splitlines()[0] if str(e) else ""
    return _redact((text or type(e).__name__)[:160])


def _redact(text: str) -> str:
    return _CREDS.sub(r"\1…@", text)


# ---------------------------------------------------------------------------- checks
def check_python(version: tuple[int, int, int] | None = None, uv: Callable[[], str | None] | None = None
                 ) -> list[dict[str, Any]]:
    v = version or sys.version_info[:3]
    shown = ".".join(str(x) for x in v)
    out = [_check("python", v[:2] >= (3, 12), f"{shown}" if v[:2] >= (3, 12) else f"{shown}; hangar needs 3.12+",
                  "install Python 3.12 or newer (uv python install 3.12), then reinstall hangar with it")]
    got = (uv or _uv_version)()
    out.append(_check("uv", True, got) if got else _check(
        "uv", None, "not on PATH; hangar runs without it, but installing and updating from a checkout use it",
        "install uv: https://docs.astral.sh/uv/getting-started/installation/"))
    return out


def runtime_name(info: dict[str, Any], docker_host: str = "") -> str:
    """Which container runtime answers: Docker Desktop, Colima, OrbStack, Rancher Desktop or Linux dockerd."""
    os_name, name = str(info.get("OperatingSystem") or ""), str(info.get("Name") or "")
    hint = f"{os_name} {name} {docker_host}".lower()
    for key, label in (("orbstack", "OrbStack"), ("colima", "Colima"), ("rancher", "Rancher Desktop"),
                       ("docker desktop", "Docker Desktop")):
        if key in hint:
            return label
    return f"dockerd ({os_name})" if os_name else "dockerd"


def check_runtime(info: Callable[[], tuple[dict[str, Any] | None, str | None]] | None = None,
                  system: str | None = None, docker_host: str | None = None) -> dict[str, Any]:
    got, why = (info or _docker_info)()
    if got is None:
        return _check("container runtime", False, f"Docker isn't reachable ({why})",
                      "start Docker Desktop, OrbStack or Colima (macOS), or the docker service (Linux: sudo "
                      "systemctl start docker), then run this again")
    system = system or platform.system()
    name = runtime_name(got, os.environ.get("DOCKER_HOST", "") if docker_host is None else docker_host)
    kata = "" if system == "Linux" else "; Kata microVM sandboxes need Linux or WSL2"
    if name == "Docker Desktop":
        return _check("container runtime", None, f"Docker Desktop{kata}",
                      "company use may need a paid Docker Desktop licence; Colima or OrbStack run hangar the same "
                      "way")
    return _check("container runtime", True, name + kata)


def check_port(port: int, free: Callable[[int], bool] | None = None,
               hangar_on: Callable[[str], bool] | None = None) -> dict[str, Any]:
    if (free or _port_free)(port):
        return _check("port", True, f"{port} is free")
    if (hangar_on or _hangar_on)(f"http://127.0.0.1:{port}"):
        return _check("port", True, f"{port} is held by a running hangar")
    return _check("port", False, f"{port} is in use by something that isn't hangar",
                  f"stop what holds {port}, or pick another port: hangar up --port {port + 1}")


def _pem_readable(path: str) -> str | None:
    """None when `path` is a readable PEM certificate file, else why not."""
    p = Path(path).expanduser()
    try:
        if not p.is_file():
            return f"{path} isn't a file"
        with p.open("rb") as f:
            data = f.read(_PEM_MAX)
    except OSError as e:
        return f"can't read {path} ({e.strerror or type(e).__name__})"
    return None if _PEM in data else f"{path} holds no PEM certificate"


def _bundle_loads(path: str) -> str | None:
    """None when ssl loads `path` as CA certificates (a PEM header over a broken body doesn't), else why not."""
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cafile=os.path.expanduser(path))
    except (ssl.SSLError, OSError, ValueError) as e:
        return f"{path} doesn't load as a CA certificate ({_why(e)})"
    return None


def ca_bundle(data_dir: Path | None = None) -> tuple[str | None, str]:
    """HANGAR_EXTRA_CA_BUNDLE as the server `hangar up` starts would see it: the environment or ./.env first (as
    Settings reads them), else stored with `hangar config set` in this machine's hangar database."""
    try:
        from hangar.config import Settings

        if (v := Settings().extra_ca_bundle) is not None:
            return str(v), CA_VAR
    except Exception:  # noqa: BLE001 - another setting that doesn't parse is the server's problem, not this check's
        if v := os.environ.get(CA_VAR):
            return v, CA_VAR
    db = (data_dir or Path.home() / ".hangar") / "hangar.db"
    if db.is_file():
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2) as conn:
                row = conn.execute("SELECT value FROM kv WHERE key=?", ("setting:extra_ca_bundle",)).fetchone()
            if row and isinstance(v := json.loads(row[0] or "null"), str) and v:
                return v, f"{CA_VAR} (stored with hangar config)"
        except (sqlite3.Error, ValueError):
            pass
    return None, CA_VAR


def check_proxy(proxy: str | None, bundle: str | None, probes: dict[str, Probe], source: str = CA_VAR,
                reprobe: Callable[[str, str], Probe] | None = None) -> dict[str, Any]:
    """A TLS-inspecting proxy breaks every image build and model call unless hangar trusts its CA.

    ✗ when TLS to a host that answered fails the public roots and no bundle fixes it; a proxy that only tunnels
    (TLS verifies through it) needs no CA. ! when TLS couldn't be tried."""
    fix = f"set {CA_VAR} to your company's root CA as a PEM file (ask IT for it), then run this again"
    via = f" through {_host(proxy)}" if proxy else ""
    failed = {h: probes[h][2] for h in TLS_HOSTS if h in probes and probes[h][1] is False}
    if bundle:
        if why := _pem_readable(bundle) or _bundle_loads(bundle):
            return _check("TLS proxy", False, f"{source} is set, but {why}", fix)
        again = {h: (reprobe or _reprobe)(h, bundle) for h in failed}
        still = {h: p[2] for h, p in again.items() if p[1] is False}
        if still:
            return _check("TLS proxy", False, f"TLS{via} to {', '.join(still)} fails even with {bundle} "
                          f"({next(iter(still.values()))}), so it isn't the CA that signs what the proxy presents",
                          fix)
        if untried := {h: p[0] for h, p in again.items() if p[1] is None}:
            return _check("TLS proxy", None, f"TLS{via} to {', '.join(untried)} fails the public roots, and trying "
                          f"it again with {bundle} couldn't run ({next(iter(untried.values()))})",
                          f"run this again with the network up; if it still fails: {fix}")
        return _check("TLS proxy", True, f"{source} trusts {bundle}" + (f" (proxy {_host(proxy)})" if proxy else ""))
    if failed:
        why = next(iter(failed.values()))
        return _check("TLS proxy", False, f"TLS{via} to {', '.join(failed)} fails certificate checks ({why}), so "
                      "something on the network inspects it (or this machine's clock is wrong)", fix)
    untried = [h for h in TLS_HOSTS if not probes.get(h) or probes[h][1] is None]
    if untried:
        reason = (probes.get(untried[0]) or ("not tried", None, None))[0]
        what = f"HTTPS_PROXY is {_host(proxy)}, and whether it inspects TLS" if proxy else "Whether TLS is inspected"
        return _check("TLS proxy", None, f"{what} is unknown: TLS to {', '.join(untried)} couldn't be tried "
                      f"({reason})", f"run this again with the network up; if a proxy inspects TLS: {fix}")
    if proxy:
        return _check("TLS proxy", True, f"HTTPS_PROXY is {_host(proxy)}; TLS through it verifies, so it doesn't "
                      "inspect")
    return _check("TLS proxy", True, "none: TLS verifies")


def _reprobe(host: str, bundle: str) -> Probe:
    return _probe(host, verify=os.path.expanduser(bundle))


def _host(url: str | None) -> str:
    """A proxy's host and port, never a user or password written into its URL."""
    try:
        u = httpx.URL(url or "")
        return f"{u.host}:{u.port}" if u.port else (u.host or "set")
    except Exception:  # noqa: BLE001
        return "set"


def env_harnesses(envs: list[str], envs_dir: Path | None) -> list[str]:
    """The harnesses the named environments run, read from their YAML (claude-code when none can be read)."""
    import yaml

    found: list[str] = []
    for name in envs:
        try:
            spec = yaml.safe_load((envs_dir / f"{name}.yaml").read_text()) if envs_dir else None
        except (OSError, yaml.YAMLError):
            spec = None
        hs = spec.get("harnesses") if isinstance(spec, dict) else None
        found += [h for h in hs or [] if isinstance(h, str)]
    return list(dict.fromkeys(found)) or ["claude-code"]


def reach_groups(harnesses: list[str]) -> list[tuple[str, list[str]]]:
    from hangar.egress import HARNESS_HOSTS

    return [(GITHUB, [GITHUB])] + [(h, list(HARNESS_HOSTS[h])) for h in harnesses if h in HARNESS_HOSTS]


def check_reach(groups: list[tuple[str, list[str]]], probes: dict[str, Probe]) -> list[dict[str, Any]]:
    """One line per group. A host whose certificate fails still answered: the TLS line reports that."""
    out = []
    for label, hs in groups:
        bad = [f"{x} ({why})" for x in hs if (why := (probes.get(x) or ("not tried", None, None))[0])]
        name = "reach github.com" if label == GITHUB else f"reach {label}"
        out.append(_check(name, None, "can't reach " + "; ".join(bad),
                          "check the network, VPN or proxy; ask IT to allow these hosts") if bad
                   else _check(name, True, ", ".join(hs)))
    return out


def check_login(port: int, envs: list[str], token: str | None,
                hangar_on: Callable[[str], bool] | None = None,
                get: Callable[..., httpx.Response] | None = None) -> dict[str, Any]:
    """Whether jobs would find an API key: asked of the hangar already running here, else a reminder."""
    url = f"http://127.0.0.1:{port}"
    key = "store an API key for each harness: hangar cred add --team (not `claude setup-token`: a subscription " \
          "login runs sessions, never jobs); docs/team/subscription-logins.md"
    if not token or not (hangar_on or _hangar_on)(url):
        return _check("model login", None, "no hangar is running here yet, so this can't tell; jobs need an API key",
                      key)
    missing, unknown = [], []
    for env in envs:
        try:
            if get is None:
                with _local() as c:
                    r = c.get(f"{url}/api/credentials/login", params={"environment": env, "purpose": "automated"},
                              headers={"Authorization": f"Bearer {token}"})
            else:
                r = get(f"{url}/api/credentials/login", params={"environment": env, "purpose": "automated"},
                        headers={"Authorization": f"Bearer {token}"})
            data = r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            data = None
        if not isinstance(data, dict):
            unknown.append(env)
        elif data.get("login") is False:
            missing.append(f"{env} ({data.get('harness')})")
    if missing:
        return _check("model login", None, "jobs on these would find no API key: " + ", ".join(missing), key)
    if unknown:
        return _check("model login", None, "couldn't ask the running hangar about " + ", ".join(unknown), key)
    return _check("model login", True, "jobs on " + ", ".join(envs) + " would find an API key")


def check_offline() -> dict[str, Any]:
    return _check("offline tools", True, "hangar trace import works with none of the above: no server, Docker or "
                  "network")


def run(port: int = 8787, envs: list[str] | None = None, token: str | None = None,
        envs_dir: Path | None = None, data_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every check, in order. Runs with no server and no login; the network is used only for the host probes."""
    envs = envs or ["claude-python"]
    groups = reach_groups(env_harnesses(envs, envs_dir))
    hosts = list(dict.fromkeys([*TLS_HOSTS, *(x for _, hs in groups for x in hs)]))
    probes = probe_all(hosts)
    bundle, source = ca_bundle(data_dir)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    return [*check_python(), check_runtime(), check_port(port), check_proxy(proxy, bundle, probes, source),
            *check_reach(groups, probes), check_login(port, envs, token), check_offline()]
