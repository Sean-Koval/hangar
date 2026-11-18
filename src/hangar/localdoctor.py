"""`hangar doctor --local` (KAN-351): check this machine before any server, with no login.

`hangar doctor` asks a running hangar. On a fresh or locked-down laptop there is none yet, so this checks what
`hangar up` needs from the machine itself: python and uv, a container runtime, the port, a TLS-inspecting proxy,
reachability of the model and GitHub hosts, and whether jobs would find an API key. Each line carries its fix, with
the same marks and `--json` shape as `hangar doctor`. Only a ✗ stops `hangar up`: a check that can't run offline is a
`!` with the reason.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from hangar.doctor import _check

GITHUB = "github.com"
TLS_PROBE = "api.anthropic.com"
CA_VAR = "HANGAR_EXTRA_CA_BUNDLE"
TIMEOUT = 5.0
_PEM = b"-----BEGIN CERTIFICATE-----"


# ---------------------------------------------------------------------------- probes (tests replace these)
def _docker_info() -> tuple[dict[str, Any] | None, str | None]:
    """The daemon's `docker info`, or why it can't be reached."""
    try:
        import docker

        return docker.from_env(timeout=5).info(), None
    except Exception as e:  # noqa: BLE001 - any failure means "not usable"
        return None, (str(e).splitlines()[0][:200] if str(e) else type(e).__name__)


def _uv_version() -> str | None:
    if not (uv := shutil.which("uv")):
        return None
    try:
        out = subprocess.run([uv, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.split()[1] if len(out.split()) > 1 else out or "?"


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _hangar_on(url: str) -> bool:
    try:
        return httpx.get(f"{url}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _reach(host: str) -> str | None:
    """None when https://host answers at all (any status), else why not. HEAD only, nothing sent but the request."""
    try:
        httpx.head(f"https://{host}/", timeout=TIMEOUT, follow_redirects=False)
        return None
    except httpx.HTTPError as e:
        return _why(e)


def _tls_verifies(host: str) -> bool | None:
    """True when TLS to `host` verifies against the public roots (certifi), False when a certificate fails, None
    when it couldn't be tried (no network). Not this machine's own trust (SSL_CERT_FILE and the like): a company CA
    trusted here is still unknown inside hangar's images, which is what HANGAR_EXTRA_CA_BUNDLE fixes."""
    import certifi

    try:
        httpx.head(f"https://{host}/", timeout=TIMEOUT, follow_redirects=False,
                   verify=ssl.create_default_context(cafile=certifi.where()))
        return True
    except httpx.ConnectError as e:
        return False if _cert_error(e) else None
    except httpx.HTTPError:
        return None


def _cert_error(e: BaseException) -> bool:
    seen: BaseException | None = e
    while seen is not None:
        if isinstance(seen, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(seen):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _why(e: BaseException) -> str:
    text = str(e).splitlines()[0] if str(e) else ""
    return (text or type(e).__name__)[:160]


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
    try:
        data = Path(path).expanduser().read_bytes()
    except OSError as e:
        return f"can't read {path} ({e.strerror or type(e).__name__})"
    return None if _PEM in data else f"{path} holds no PEM certificate"


def check_proxy(env: dict[str, str] | None = None, verifies: Callable[[str], bool | None] | None = None
                ) -> dict[str, Any]:
    """A TLS-inspecting proxy breaks every image build and model call unless hangar trusts its CA."""
    env = dict(os.environ) if env is None else env
    proxy = env.get("HTTPS_PROXY") or env.get("https_proxy")
    bundle = env.get(CA_VAR)
    fix = f"set {CA_VAR} to your company's root CA as a PEM file (ask IT for it), then run this again"
    if bundle:
        if why := _pem_readable(bundle):
            return _check("TLS proxy", False, f"{CA_VAR} is set, but {why}", fix)
        return _check("TLS proxy", True, f"{CA_VAR} trusts {bundle}" + (f" (proxy {_host(proxy)})" if proxy else ""))
    ok = (verifies or _tls_verifies)(TLS_PROBE)
    if ok is False:
        return _check("TLS proxy", False, f"TLS to {TLS_PROBE} fails certificate checks, so something on the "
                      "network inspects it", fix)
    if proxy and ok is None:
        return _check("TLS proxy", None, f"HTTPS_PROXY is {_host(proxy)}, and TLS through it couldn't be tried "
                      "(no network), so whether it inspects TLS is unknown", f"if it does: {fix}")
    if proxy:
        return _check("TLS proxy", True, f"HTTPS_PROXY is {_host(proxy)}; TLS through it verifies, so it doesn't "
                      "inspect")
    return _check("TLS proxy", True, "none" if ok else "none set (TLS not tried: no network)")


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


def check_reach(harnesses: list[str], reach: Callable[[str], str | None] | None = None) -> list[dict[str, Any]]:
    from hangar.egress import HARNESS_HOSTS

    groups = [(GITHUB, [GITHUB])] + [(h, list(HARNESS_HOSTS[h])) for h in harnesses if h in HARNESS_HOSTS]
    hosts = list(dict.fromkeys(x for _, hs in groups for x in hs))
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as pool:
        why = dict(zip(hosts, pool.map(reach or _reach, hosts), strict=True))
    out = []
    for label, hs in groups:
        bad = [f"{x} ({why[x]})" for x in hs if why[x]]
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
    if not (hangar_on or _hangar_on)(url) or not token:
        return _check("model login", None, "no hangar is running here yet, so this can't tell; jobs need an API key",
                      key)
    missing, unknown = [], []
    for env in envs:
        try:
            r = (get or httpx.get)(f"{url}/api/credentials/login", params={"environment": env, "purpose": "automated"},
                                   headers={"Authorization": f"Bearer {token}"}, timeout=10)
            data = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
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
        envs_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every check, in order. Runs with no server, no login and no network."""
    envs = envs or ["claude-python"]
    return [*check_python(), check_runtime(), check_port(port), check_proxy(),
            *check_reach(env_harnesses(envs, envs_dir)), check_login(port, envs, token), check_offline()]
