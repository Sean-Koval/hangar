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
from tests.conftest import ENVS, free_port

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


OK = (None, True, None)
BAD = (None, False, "self-signed certificate in certificate chain")
OFF = ("[Errno -2] Name or service not known", None, None)


def _probes(state):
    return dict.fromkeys(ld.TLS_HOSTS, state)


def test_proxy(tmp_path):
    pem = tmp_path / "corp.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    var, proxy = ld.CA_VAR, "http://user:secret@proxy.corp:3128"
    # an inspecting proxy, no bundle: ✗ naming the variable, with the certificate's reason
    c = ld.check_proxy(proxy, None, _probes(BAD))
    assert _status(c) == "fail" and var in c["fix"] and "self-signed" in c["detail"]
    # a proxy that inspects only github.com is caught too
    assert _status(ld.check_proxy(proxy, None, {"api.anthropic.com": OK, "github.com": BAD})) == "fail"
    # with a PEM that verifies: ✓, and the proxy's password never shown
    c = ld.check_proxy(proxy, str(pem), _probes(BAD), reprobe=lambda h, b: OK)
    assert _status(c) == "ok" and "proxy.corp:3128" in c["detail"] and "secret" not in json.dumps(c)
    # with a PEM that is the wrong CA: ✗
    wrong = ld.check_proxy(proxy, str(pem), _probes(BAD), reprobe=lambda h, b: BAD)
    assert _status(wrong) == "fail" and "isn't the CA" in wrong["detail"]
    # a bundle that isn't there, isn't a file, or isn't PEM: ✗
    assert _status(ld.check_proxy(None, str(tmp_path / "nope.pem"), _probes(OK))) == "fail"
    assert "isn't a file" in ld.check_proxy(None, str(tmp_path), _probes(OK))["detail"]
    (tmp_path / "x.der").write_bytes(b"\x30\x82")
    assert "no PEM" in ld.check_proxy(None, str(tmp_path / "x.der"), _probes(OK))["detail"]
    # no proxy variable, but TLS fails verification: inspected anyway
    assert _status(ld.check_proxy(None, None, _probes(BAD))) == "fail"
    # a proxy that only tunnels, and no proxy at all: ✓
    assert _status(ld.check_proxy("http://p:8080", None, _probes(OK))) == "ok"
    assert _status(ld.check_proxy(None, None, _probes(OK))) == "ok"
    # no network to tell, with or without a proxy: a ! with the reason
    for p in ("http://p:8080", None):
        c = ld.check_proxy(p, None, _probes(OFF))
        assert _status(c) == "warn" and "Name or service" in c["detail"] and var in c["fix"]


def test_the_bundle_is_read_as_the_server_would(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.delenv(ld.CA_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    assert ld.ca_bundle(data) == (None, ld.CA_VAR)
    # stored with `hangar config set extra_ca_bundle`
    with sqlite3.connect(data / "hangar.db") as db:
        db.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO kv VALUES ('setting:extra_ca_bundle', ?)", (json.dumps("/etc/stored.pem"),))
    value, source = ld.ca_bundle(data)
    assert value == "/etc/stored.pem" and "hangar config" in source
    # ./.env, as the server `hangar up` starts reads it, wins over the database
    (tmp_path / ".env").write_text("HANGAR_EXTRA_CA_BUNDLE=/etc/dotenv.pem\n")
    assert ld.ca_bundle(data)[0] == "/etc/dotenv.pem"
    monkeypatch.setenv(ld.CA_VAR, "/etc/env.pem")  # and the environment over both
    assert ld.ca_bundle(data)[0] == "/etc/env.pem"


def test_probes_never_raise():
    # a SOCKS proxy without the extra, and a malformed proxy URL: reasons, not tracebacks
    def socks(host):
        raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")

    got = ld.probe_all(["github.com"], socks)
    assert "httpx[socks]" in got["github.com"][0]
    got = ld.probe_all(["github.com"], lambda h: (_ for _ in ()).throw(httpx.InvalidURL("bad port")))
    assert got["github.com"] == ("bad port", None, None)


def test_probe_with_a_bad_proxy_is_a_reason(monkeypatch):
    for v in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(v, "http://proxy:abc")
    why, tls, _ = ld._probe("github.com")
    assert why and tls is None


def test_probes_share_one_deadline():
    import threading

    gate = threading.Event()
    got = ld.probe_all(["a", "b"], lambda h: OK if h == "a" else (gate.wait(5), OK)[1], deadline=0.2)
    gate.set()
    assert got["a"] == OK and "no answer in 0.2 s" in got["b"][0]


def test_cert_errors_are_told_from_no_network():
    try:
        raise httpx.ConnectError("boom") from ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")
    except httpx.ConnectError as e:
        assert ld._cert_error(e)
    assert not ld._cert_error(httpx.ConnectError("[Errno -2] Name or service not known"))


def test_secrets_in_reasons_are_redacted():
    assert ld._redact("tcp://user:pw@docker.corp:2376 refused") == "tcp://…@docker.corp:2376 refused"


def test_harnesses_come_from_the_environments(tmp_path):
    shipped = Path(__file__).parents[1] / "envs"  # what `hangar up` builds
    assert ld.env_harnesses(["claude-python"], shipped) == ["claude-code"]
    assert ld.env_harnesses(["codex-node", "claude-python"], shipped) == ["codex", "claude-code"]
    assert ld.env_harnesses(["agents"], ENVS) == ["claude-code", "codex", "pi"]
    assert ld.env_harnesses(["missing"], tmp_path) == ["claude-code"]
    assert ld.env_harnesses(["claude-python"], None) == ["claude-code"]


def test_reach_is_one_line_per_group_and_only_warns():
    groups = ld.reach_groups(["claude-code", "git"])  # git: no hosts of its own
    assert [g for g, _ in groups] == ["github.com", "claude-code"]
    probes = {h: OK for _, hs in groups for h in hs} | {"claude.ai": ("timed out", None, None),
                                                        "platform.claude.com": BAD}
    out = ld.check_reach(groups, probes)
    assert [c["name"] for c in out] == ["reach github.com", "reach claude-code"]
    assert _status(out[0]) == "ok" and _status(out[1]) == "warn" and "claude.ai (timed out)" in out[1]["detail"]
    assert "platform.claude.com" not in out[1]["detail"]  # it answered; its certificate is the TLS line's business
    assert all(_status(c) == "warn" for c in ld.check_reach(ld.reach_groups(["codex"]), {}))


def test_probe_sends_a_bare_head_without_this_machines_trust(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, **kw):
            seen["kw"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def head(self, url, **kw):
            seen["url"], seen["head"] = url, kw
            return httpx.Response(403)  # any answer means reachable

    monkeypatch.setattr(httpx, "Client", Client)
    assert ld._probe("api.anthropic.com") == OK
    assert seen["url"] == "https://api.anthropic.com/" and seen["head"] == {}
    assert isinstance(seen["kw"]["verify"], ssl.SSLContext) and "auth" not in seen["kw"]


def test_loopback_never_goes_through_a_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.corp:3128")
    with ld._local() as c:
        assert not c._mounts or all(t is None for t in c._mounts.values())


def test_only_a_hangar_counts_as_one(monkeypatch):
    import http.server
    import threading

    body = {"value": b'{"ok": true}'}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body["value"])

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        assert ld._hangar_on(url)
        body["value"] = b"<html>a dev server's catch-all</html>"
        assert not ld._hangar_on(url)
    finally:
        srv.shutdown()


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
    monkeypatch.chdir(tmp_path)  # no ./.env
    monkeypatch.setattr(ld, "_probe", lambda h: OFF)
    monkeypatch.setattr(ld, "_hangar_on", lambda u: False)
    monkeypatch.setattr(ld, "_uv_version", lambda: "0.9.1")
    return monkeypatch


def test_cli_runs_with_no_server_login_or_network(offline):
    r = runner.invoke(cli.app, ["doctor", "--local", "--port", str(free_port())])
    assert r.exit_code == 0, r.output
    for name in ("python", "uv", "container runtime", "port", "TLS proxy", "reach github.com", "reach claude-code",
                 "model login", "offline tools"):
        assert f" {name}: " in r.output, name
    assert "Not logged in" not in r.output and "trace import" in r.output


def test_cli_env_picks_the_harnesses(offline):
    r = runner.invoke(cli.app, ["doctor", "--local", "--port", str(free_port()), "--env", "codex-node"])
    assert " reach codex: " in r.output and " reach claude-code: " not in r.output
    assert runner.invoke(cli.app, ["doctor", "--port", "9"]).exit_code == 2  # --port without --local


def test_cli_json_and_exit_1_only_on_a_fail(offline):
    port = str(free_port())
    r = runner.invoke(cli.app, ["doctor", "--local", "--json", "--port", port])
    checks = json.loads(r.output)
    assert {c["status"] for c in checks} <= {"ok", "warn"} and r.exit_code == 0
    offline.setattr(ld, "_docker_info", lambda: (None, "connection refused"))
    r = runner.invoke(cli.app, ["doctor", "--local", "--port", port])
    assert r.exit_code == 1 and "✗ container runtime" in r.output
    r = runner.invoke(cli.app, ["doctor", "--local", "--json", "--port", port])
    assert r.exit_code == 1 and json.loads(r.output)[2]["status"] == "fail"
    assert runner.invoke(cli.app, ["doctor", "--local", "--fix"]).exit_code == 2


def test_local_checks_ask_with_the_saved_login_or_the_bootstrap_token(offline, tmp_path):
    from hangar import setup_cli

    seen = []
    offline.setattr(ld, "check_login", lambda port, envs, token: seen.append(token) or ld.check_offline())
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "bootstrap-token").write_text("hgr_boot\n")
    setup_cli.local_checks(8787)
    (tmp_path / "cli.json").write_text(json.dumps({"url": "http://127.0.0.1:8787", "token": "hgr_saved"}))
    setup_cli.local_checks(8787)
    (tmp_path / "cli.json").write_text(json.dumps({"url": "https://team.example", "token": "hgr_team"}))
    setup_cli.local_checks(8787)  # another hangar's token never goes to this machine's
    assert seen == ["hgr_boot", "hgr_saved", "hgr_boot"]
