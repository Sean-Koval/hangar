"""Agent self-diagnosis (docs/computer-use.md): what hangar tells a sandbox about itself, `hangar-agent env doctor`
and its read-only MCP server, which run inside the sandbox with only the standard library."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from hangar import egressapi, envdoctor
from hangar import sandbox_env as se
from hangar.envspec import EnvSpec
from hangar.models import Sandbox

SCRIPT = Path(se.__file__)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A sandbox home for the script run in-process (paths are module constants, so they are pointed here)."""
    monkeypatch.setattr(se, "HOME", str(tmp_path))
    monkeypatch.setattr(se, "STATUS", str(tmp_path / ".hangar/envstatus.json"))
    monkeypatch.setattr(se, "ENV_FILE", str(tmp_path / ".hangar/env"))
    monkeypatch.setattr(se, "SVC", str(tmp_path / ".hangar/bin/hangar-svc"))
    monkeypatch.delenv("HANGAR_IO", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    (tmp_path / ".hangar").mkdir()
    return tmp_path


def write_status(home: Path, **kw) -> None:
    (home / ".hangar/envstatus.json").write_text(json.dumps({"v": 1, "generated_at": 0, "environment": "web",
                                                             "sandbox": "sb-1", **kw}))


def listener() -> socket.socket:
    s = socket.create_server(("127.0.0.1", 0))

    def accept():
        with contextlib.suppress(OSError):
            while True:
                s.accept()[0].close()
    threading.Thread(target=accept, daemon=True).start()
    return s


def closed_port() -> int:
    s = socket.create_server(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ====================================================================== the checks
def test_doctor_reports_fallback_builds_unhealthy_services_missing_secrets_and_setup_failures(home, monkeypatch):
    up = listener()
    monkeypatch.setenv("PRESENT_TOKEN", "x")
    (home / ".hangar/env").write_text("export FROM_FILE='v'\n")
    write_status(home, build={"version": "b-42", "fallback": True, "reason": "npm ci failed"},
                 services=[{"name": "db", "port": up.getsockname()[1], "kind": "service"},
                           {"name": "web", "port": closed_port(), "kind": "terminal"},
                           {"name": "worker", "check": "exit 3"}],
                 secrets=[{"name": "PRESENT_TOKEN", "for": "secrets:"}, {"name": "FROM_FILE", "for": "secrets:"},
                          {"name": "STRIPE_KEY", "for": "MCP server pay"}],
                 setup_failures=[{"ts": 1, "step": "start", "message": "port 3000 never opened"}])
    rep = se.doctor()
    by = {c["name"]: c for c in rep["checks"]}
    assert not rep["ok"]
    assert by["build"]["status"] == "fail" and "FALLBACK" in by["build"]["message"]
    assert "npm ci" in by["build"]["message"]
    bad = {e["name"] for e in by["services"]["data"]["unhealthy"]}
    assert bad == {"web", "worker"} and by["services"]["data"]["healthy"][0]["name"] == "db"
    assert [m["name"] for m in by["secrets"]["data"]["missing"]] == ["STRIPE_KEY"]  # names only, never values
    assert by["setup"]["status"] == "fail" and "port 3000" in by["setup"]["message"]
    text = se.render(rep)
    assert "[FAIL] build" in text and "- web: nothing answers" in text
    up.close()


def test_doctor_is_clear_when_hangar_said_nothing(home):
    rep = se.doctor()
    assert rep["checks"][0]["name"] == "status" and rep["checks"][0]["status"] == "warn"
    assert {c["name"]: c["status"] for c in rep["checks"]}["build"] == "unknown"
    write_status(home, build={"version": "b-1", "fallback": False}, secrets=[])
    rep = se.doctor()
    assert rep["ok"] and {c["name"]: c["status"] for c in rep["checks"]} == {
        "build": "ok", "services": "ok", "secrets": "ok", "setup": "ok"}


def test_supervisor_state_counts(home, monkeypatch):
    up = listener()
    svc = home / ".hangar/bin/hangar-svc"
    svc.parent.mkdir(parents=True)
    svc.write_text('#!/bin/sh\necho \'{"start": "running", "services": [{"name": "db", "ready": true}], '
                   '"programs": [{"name": "web", "kind": "terminal", "state": "backoff", "ready": false, '
                   '"log": "/x.log"}]}\'\n')
    svc.chmod(0o755)
    write_status(home, services=[{"name": "db", "port": up.getsockname()[1]},
                                 {"name": "web", "port": up.getsockname()[1], "kind": "terminal"}])
    [bad] = {c["name"]: c for c in se.doctor()["checks"]}["services"]["data"]["unhealthy"]
    assert bad["name"] == "web" and bad["state"] == "backoff" and bad["log"] == "/x.log"
    assert "not ready (backoff)" in bad["problem"]
    up.close()


def test_host_probes_tell_blocked_from_down(home, monkeypatch):
    up = listener()
    write_status(home, egress={"mode": "allowlist", "allow": ["*.example.com", "127.0.0.1"], "enforced": True})
    ok = se.probe_host(se.load_status(), f"127.0.0.1:{up.getsockname()[1]}")
    assert ok["reachable"] and ok["via"] == "direct" and ok["policy"]["rule"] == "127.0.0.1"
    down = se.probe_host(se.load_status(), f"localhost:{closed_port()}")
    assert not down["reachable"] and down["verdict"] == "blocked by policy"  # enforced, and no rule matches
    # through the egress proxy: its 403 is the policy talking
    proxy = socket.create_server(("127.0.0.1", 0))

    def answer():
        conn, _ = proxy.accept()
        with conn:
            req = conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n" if b"api.example.com" in req else b"HTTP/1.1 403 Forbidden\r\n\r\n")
    for _ in range(2):
        threading.Thread(target=answer, daemon=True).start()
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.getsockname()[1]}")
    res = [se.probe_host(se.load_status(), h) for h in ("https://API.example.com./v1", "evil.test:8443")]
    by = {r["host"]: r for r in res}
    assert by["api.example.com"]["reachable"] and by["api.example.com"]["port"] == 443
    assert by["evil.test"]["verdict"] == "blocked by policy" and "env request egress evil.test:8443" in \
        by["evil.test"]["detail"]
    with pytest.raises(se.Usage):
        se.probe_host({}, "not a host!")
    proxy.close()
    up.close()


# ====================================================================== requests
def test_requests_are_validated_deduplicated_and_never_need_a_value(home, monkeypatch):
    io = home / "io"
    write_status(home, io=str(io), requests=[{"id": "rq-1", "kind": "egress", "host": "pypi.org", "port": None,
                                             "state": "pending"}])
    rec = se.file_request("secret", "STRIPE_KEY", "  to run the\n payment tests ")
    [f] = list((io / "out/requests").iterdir())
    data = json.loads(f.read_text())
    assert data["kind"] == "secret" and data["name"] == "STRIPE_KEY" and data["why"] == "to run the payment tests"
    assert rec["state"] == "pending" and "nothing is granted automatically" in se.describe(rec)
    again = se.file_request("secret", "STRIPE_KEY", "again")
    assert again["duplicate"] and len(list((io / "out/requests").iterdir())) == 1
    assert se.file_request("egress", "pypi.org", "deps")["id"] == "rq-1"  # already pending with the owner
    for kind, target, why, msg in (("secret", "bad name", "x", "variable name"), ("secret", "OK", " ", "--why"),
                                   ("egress", "a b", "x", "host"), ("egress", "h:99999", "x", "port"),
                                   ("magic", "x", "x", "request what")):
        with pytest.raises(se.Usage, match=msg):
            se.file_request(kind, target, why)
    (home / ".hangar/envstatus.json").unlink()
    with pytest.raises(se.Usage, match="HANGAR_IO"):
        se.file_request("secret", "OTHER", "x")


def test_the_agent_hears_at_once_when_too_many_requests_wait(home):
    io = home / "io"
    waiting = [{"id": f"rq-{i}", "kind": "egress", "host": f"h{i}.example.com", "state": "pending"} for i in range(5)]
    write_status(home, io=str(io), request_limit=5, requests=waiting)
    with pytest.raises(se.Usage, match="5 requests already wait for the owner"):
        se.file_request("secret", "STRIPE_KEY", "tests")
    assert not (io / "out/requests").exists()
    assert se.file_request("egress", "h1.example.com", "x")["duplicate"]  # asking for one that waits still answers
    write_status(home, io=str(io), request_limit=5, requests=[{**waiting[0], "state": "denied"}, *waiting[1:]])
    assert se.file_request("secret", "STRIPE_KEY", "tests")["state"] == "pending"


def test_the_cli_runs_in_a_bare_python(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "HANGAR_IO": str(tmp_path / "io")}
    r = subprocess.run([sys.executable, "-I", str(SCRIPT), "request", "egress", "registry.npmjs.org", "--why",
                        "npm ci"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "requested egress registry.npmjs.org" in r.stdout
    r = subprocess.run([sys.executable, "-I", str(SCRIPT), "doctor", "--json"], capture_output=True, text=True,
                       env=env)
    assert json.loads(r.stdout)["checks"][0]["name"] == "status"
    r = subprocess.run([sys.executable, "-I", str(SCRIPT), "request", "secret", "X"], capture_output=True, text=True,
                       env=env)
    assert r.returncode == 2 and "--why" in r.stderr


# ====================================================================== the MCP server
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_server_serves_the_report_read_only(tmp_path, mode):
    (tmp_path / ".hangar").mkdir()
    (tmp_path / ".hangar/envstatus.json").write_text(json.dumps({"environment": "web", "io": str(tmp_path / "io"),
                                                                 "secrets": [{"name": "NOT_SET_ANYWHERE"}]}))
    params = StdioServerParameters(command=sys.executable, args=["-I", str(SCRIPT), "mcp"],
                                   env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    async with Client(params, mode=mode) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert tools["env_doctor"].annotations.read_only_hint is True
        assert {str(r.uri) for r in (await c.list_resources()).resources} == {
            "hangar-env://doctor", "hangar-env://status", "hangar-env://requests"}
        doc = json.loads((await c.read_resource("hangar-env://doctor")).contents[0].text)
        assert {c_["name"]: c_["status"] for c_ in doc["checks"]}["secrets"] == "fail"
        probe = json.loads((await c.read_resource(f"hangar-env://egress/127.0.0.1:{closed_port()}")).contents[0].text)
        assert probe["reachable"] is False
        res = await c.call_tool("env_request", {"kind": "secret", "target": "NOT_SET_ANYWHERE", "why": "tests"})
        assert not res.is_error and "requested secret" in res.content[0].text
        bad = await c.call_tool("env_request", {"kind": "secret", "target": "x y", "why": "tests"})
        assert bad.is_error
    assert len(list((tmp_path / "io/out/requests").iterdir())) == 1  # found the turn's directory without HANGAR_IO


# ====================================================================== what hangar writes
def test_status_has_names_and_policy_never_values(settings, monkeypatch):
    spec = EnvSpec(name="web", secrets={"GH_TOKEN": "gh-cred"}, computer=True,
                   mcp={"pay": {"url": "https://pay/mcp", "headers": {"X-Key": "${STRIPE_KEY}"}}})
    sb = Sandbox(name="s", environment="web", provider="kubernetes",
                 info={"harness": "codex", "env_build": {"version": "b-7", "fallback": False},
                       "services": [{"name": "db", "port": 5432}], "warnings": ["MCP server 'x' skipped: needs Y"]})
    from hangar.config import get_settings

    get_settings.cache_clear()
    st = envdoctor.build_status(settings, sb, spec)
    # integration: the egress policy comes from these settings, never from (and never fills) the process-wide cache,
    # which a later test's `hangar admin restore --local` then read with the wrong data dir
    assert get_settings.cache_info().currsize == 0
    assert envdoctor.build_status(settings.model_copy(update={"egress_default": "none"}), sb, spec)["egress"][
        "mode"] == "none"
    assert st["runtime"] == "gvisor" and st["build"]["version"] == "b-7" and st["services"][0]["name"] == "db"
    assert st["secrets"] == [{"name": "GH_TOKEN", "for": "secrets: (credential gh-cred)"},
                             {"name": "STRIPE_KEY", "for": "MCP server pay"}]
    # integration: #97's real egressapi.policy adds `notes`, and an open network is what it says (enforced)
    assert st["egress"] == {"mode": "open", "allow": [], "proxy": None, "enforced": True, "notes": []}
    assert st["computer"]["display"] == ":99" and st["computer"]["record_max_mb"] == 24
    # a broken egress module degrades to "unknown" rather than failing the claim
    monkeypatch.setattr(egressapi, "policy", lambda *a, **k: 1 / 0)
    assert envdoctor.build_status(settings, sb, spec)["egress"]["mode"] == "unknown"
    assert envdoctor.runtime(spec.model_copy(update={"runtime_class": ""}), sb, settings) == "runc"


async def test_a_new_sandbox_can_diagnose_itself(fleet, broker):
    from hangar.models import CredentialKind

    broker.put("claude", "claude-code", CredentialKind.API_KEY, "sk-claude")
    broker.put("oai", "codex", CredentialKind.API_KEY, "sk-codex")
    broker.put("orkey", "pi", CredentialKind.API_KEY, "sk-or")
    broker.put("ghtok", "generic-secret", CredentialKind.GENERIC, "ghp_123", env_var="UNUSED_GH")
    sb = await fleet.create("agents")
    box = fleet.providers["docker"].boxes[sb.id] / "home/agent"
    st = json.loads((box / ".hangar/envstatus.json").read_text())
    assert st["environment"] == "agents" and {s["name"] for s in st["secrets"]} == {"GH_TOKEN", "NOPE"}
    assert "ghp_123" not in json.dumps(st)
    assert os.access(box / ".hangar/bin/hangar-env", os.X_OK) and os.access(box / ".hangar/bin/hangar-computer",
                                                                            os.X_OK)
    r = await fleet.exec(sb.id, "hangar-agent env doctor --json")
    rep = json.loads(r.stdout)
    by = {c["name"]: c for c in rep["checks"]}
    assert [m["name"] for m in by["secrets"]["data"]["missing"]] == ["NOPE"]  # GH_TOKEN is in ~/.hangar/env
    assert r.exit_code == 1  # something fails, so the agent's script can branch on it


def test_jobs_are_told_to_keep_going(fleet, tmp_path):
    lines = envdoctor.autonomy_lines(fleet, "plain")
    assert len(lines) == 1 and "hangar-agent env request" in lines[0] and "keep working" in lines[0]
    fleet.catalog.add({"name": "ui", "extends": "plain", "computer": True}, save_dir=tmp_path)
    assert "record a short demo" in " ".join(envdoctor.autonomy_lines(fleet, "ui"))
    assert envdoctor.autonomy_lines(fleet, "missing-env") == lines
