"""`hangar doctor --local` (KAN-351): each condition faked, no server, no login, no network."""

import json
import socket
import ssl
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import hangar.cli as cli
from hangar import localdoctor as ld
from tests.conftest import ENVS

runner = CliRunner()


def _status(c):
    return c["status"]


def test_python_and_uv():
    ok, uv = ld.check_python((3, 12, 4), lambda: "0.9.1")
    assert _status(ok) == "ok" and _status(uv) == "ok" and uv["detail"] == "0.9.1"
    old, no_uv = ld.check_python((3, 11, 9), lambda: None)
    assert _status(old) == "fail" and "3.12+" in old["detail"] and "uv python install" in old["fix"]
    assert _status(no_uv) == "warn" and "docs.astral.sh/uv" in no_uv["fix"]


@pytest.mark.parametrize(("info", "host", "name"), [
    ({"OperatingSystem": "Docker Desktop", "Name": "docker-desktop"}, "", "Docker Desktop"),
    ({"OperatingSystem": "Ubuntu 24.04 LTS", "Name": "colima"}, "", "Colima"),
    ({"OperatingSystem": "OrbStack", "Name": "orbstack"}, "", "OrbStack"),
    ({"OperatingSystem": "Alpine Linux v3.20", "Name": "lima-rancher-desktop"}, "", "Rancher Desktop"),
    ({"OperatingSystem": "Ubuntu 24.04", "Name": "x"}, "unix:///Users/me/.colima/default/docker.sock", "Colima"),
    ({"OperatingSystem": "Debian GNU/Linux 12", "Name": "box"}, "", "dockerd (Debian GNU/Linux 12)"),
])
def test_runtime_names(info, host, name):
    assert ld.runtime_name(info, host) == name


def test_runtime_checks():
    down = ld.check_runtime(lambda: (None, "connection refused"), "Darwin", "")
    assert _status(down) == "fail" and "connection refused" in down["detail"] and "Colima" in down["fix"]
    desktop = ld.check_runtime(lambda: ({"OperatingSystem": "Docker Desktop"}, None), "Darwin", "")
    assert _status(desktop) == "warn" and "licence" in desktop["fix"] and "OrbStack" in desktop["fix"]
    assert "Kata" in desktop["detail"]  # macOS: no Kata
    linux = ld.check_runtime(lambda: ({"OperatingSystem": "Ubuntu 24.04"}, None), "Linux", "")
    assert _status(linux) == "ok" and "Kata" not in linux["detail"]


def test_port_free_held_by_hangar_or_taken():
    assert _status(ld.check_port(8787, lambda p: True, lambda u: False)) == "ok"
    held = ld.check_port(8787, lambda p: False, lambda u: u == "http://127.0.0.1:8787")
    assert _status(held) == "ok" and "running hangar" in held["detail"]
    taken = ld.check_port(8787, lambda p: False, lambda u: False)
    assert _status(taken) == "fail" and "--port 8788" in taken["fix"]


def test_port_probe_sees_a_listener():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert not ld._port_free(s.getsockname()[1])


def test_proxy(tmp_path):
    pem = tmp_path / "corp.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    var = ld.CA_VAR
    # an inspecting proxy, no bundle: ✗ naming the variable
    c = ld.check_proxy({"HTTPS_PROXY": "http://user:secret@proxy.corp:3128"}, lambda h: False)
    assert _status(c) == "fail" and var in c["fix"]
    # with a readable PEM: ✓, and the proxy's password never shown
    c = ld.check_proxy({"HTTPS_PROXY": "http://user:secret@proxy.corp:3128", var: str(pem)}, lambda h: False)
    assert _status(c) == "ok" and "proxy.corp:3128" in c["detail"] and "secret" not in json.dumps(c)
    # a bundle that isn't there, or isn't PEM: ✗
    assert _status(ld.check_proxy({var: str(tmp_path / "nope.pem")}, lambda h: True)) == "fail"
    (tmp_path / "x.der").write_bytes(b"\x30\x82")
    assert "no PEM" in ld.check_proxy({var: str(tmp_path / "x.der")}, lambda h: True)["detail"]
    # no proxy variable, but TLS fails verification: inspected anyway
    assert _status(ld.check_proxy({}, lambda h: False)) == "fail"
    # a proxy that doesn't inspect, no proxy at all, and a proxy with no network to tell
    assert _status(ld.check_proxy({"https_proxy": "http://p:8080"}, lambda h: True)) == "ok"
    assert _status(ld.check_proxy({}, lambda h: True)) == "ok"
    assert _status(ld.check_proxy({}, lambda h: None)) == "ok"
    offline = ld.check_proxy({"HTTPS_PROXY": "http://p:8080"}, lambda h: None)
    assert _status(offline) == "warn" and var in offline["fix"]


def test_cert_errors_are_told_from_no_network():
    try:
        raise httpx.ConnectError("boom") from ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")
    except httpx.ConnectError as e:
        assert ld._cert_error(e)
    assert not ld._cert_error(httpx.ConnectError("[Errno -2] Name or service not known"))


def test_harnesses_come_from_the_environments(tmp_path):
    shipped = Path(__file__).parents[1] / "envs"  # what `hangar up` builds
    assert ld.env_harnesses(["claude-python"], shipped) == ["claude-code"]
    assert ld.env_harnesses(["codex-node", "claude-python"], shipped) == ["codex", "claude-code"]
    assert ld.env_harnesses(["agents"], ENVS) == ["claude-code", "codex", "pi"]
    assert ld.env_harnesses(["missing"], tmp_path) == ["claude-code"]
    assert ld.env_harnesses(["claude-python"], None) == ["claude-code"]


def test_reach_is_one_line_per_group_and_only_warns():
    seen = []

    def reach(host):
        seen.append(host)
        return "timed out" if host == "claude.ai" else None

    out = ld.check_reach(["claude-code"], reach)
    assert [c["name"] for c in out] == ["reach github.com", "reach claude-code"]
    assert _status(out[0]) == "ok" and _status(out[1]) == "warn" and "claude.ai (timed out)" in out[1]["detail"]
    assert sorted(seen) == ["api.anthropic.com", "claude.ai", "github.com", "platform.claude.com"]
    assert all(_status(c) == "warn" for c in ld.check_reach(["codex"], lambda h: "no network"))


def test_reach_sends_a_bare_head(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "head", lambda url, **kw: calls.append((url, kw)) or httpx.Response(403))
    assert ld._reach("api.anthropic.com") is None  # any answer means reachable
    url, kw = calls[0]
    assert url == "https://api.anthropic.com/" and "headers" not in kw and "auth" not in kw


def test_model_login():
    def get(login):
        return lambda url, **kw: httpx.Response(200, json={"login": login, "harness": "claude-code"})

    none = ld.check_login(8787, ["claude-python"], None, lambda u: True, get(True))
    assert _status(none) == "warn" and "setup-token" in none["fix"]  # no token: can't ask
    nobody = ld.check_login(8787, ["claude-python"], "t", lambda u: False, get(True))
    assert _status(nobody) == "warn" and "no hangar is running" in nobody["detail"]
    assert _status(ld.check_login(8787, ["claude-python"], "t", lambda u: True, get(True))) == "ok"
    assert _status(ld.check_login(8787, ["claude-python"], "t", lambda u: True, get(None))) == "ok"
    missing = ld.check_login(8787, ["claude-python"], "t", lambda u: True, get(False))
    assert _status(missing) == "warn" and "claude-python (claude-code)" in missing["detail"]
    refused = ld.check_login(8787, ["claude-python"], "t", lambda u: True,
                             lambda url, **kw: httpx.Response(403, json={"detail": "admins"}))
    assert _status(refused) == "warn" and "couldn't ask" in refused["detail"]


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """No server, no login, no network: every probe fails the way a bare laptop would."""
    monkeypatch.setenv("HANGAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HANGAR_CLI_CONFIG", str(tmp_path / "cli.json"))
    for v in ("HANGAR_URL", "HANGAR_TOKEN", "HTTPS_PROXY", "https_proxy", ld.CA_VAR):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(ld, "_docker_info", lambda: ({"OperatingSystem": "OrbStack"}, None))
    monkeypatch.setattr(ld, "_reach", lambda h: "no network")
    monkeypatch.setattr(ld, "_tls_verifies", lambda h: None)
    monkeypatch.setattr(ld, "_hangar_on", lambda u: False)
    monkeypatch.setattr(ld, "_uv_version", lambda: "0.9.1")
    return monkeypatch


def test_cli_runs_with_no_server_login_or_network(offline):
    r = runner.invoke(cli.app, ["doctor", "--local", "--port", "8799"])
    assert r.exit_code == 0, r.output
    for name in ("python", "uv", "container runtime", "port", "TLS proxy", "reach github.com", "reach claude-code",
                 "model login", "offline tools"):
        assert f" {name}: " in r.output, name
    assert "Not logged in" not in r.output and "trace import" in r.output


def test_cli_json_and_exit_1_only_on_a_fail(offline):
    r = runner.invoke(cli.app, ["doctor", "--local", "--json"])
    checks = json.loads(r.output)
    assert {c["status"] for c in checks} <= {"ok", "warn"} and r.exit_code == 0
    offline.setattr(ld, "_docker_info", lambda: (None, "connection refused"))
    r = runner.invoke(cli.app, ["doctor", "--local"])
    assert r.exit_code == 1 and "✗ container runtime" in r.output
    assert runner.invoke(cli.app, ["doctor", "--local", "--fix"]).exit_code == 2
