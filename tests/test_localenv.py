"""`hangar env local` (KAN-472): the runner's arguments, the policy it asks hangar for, and the egress sidecar on
loopback (the CONNECT proxy and the DNS server that refuses). A live run is `hangar env local check` with Docker."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from hangar import cli, egress, egress_proxy, egress_sidecar, localenv
from hangar.envspec import EnvSpec

ROOT = Path(__file__).parents[1]


def test_the_sidecar_image_is_pinned_and_matches_the_dockerfile():
    assert re.fullmatch(r"python:3\.12-slim@sha256:[0-9a-f]{64}", localenv.SIDECAR_IMAGE)
    base = re.search(r"^ARG BASE_IMAGE=(\S+)", (ROOT / "Dockerfile").read_text(), re.M)
    assert base and base.group(1) == localenv.SIDECAR_IMAGE


def test_the_sidecar_is_standard_library_only():
    """It runs as `python -c <file>` in a stock image: nothing from hangar or outside the standard library."""
    import sys

    tree = __import__("ast").parse(localenv.SIDECAR.read_text())
    mods = {a.name.split(".")[0] for n in tree.body if n.__class__.__name__ == "Import" for a in n.names}
    mods |= {n.module.split(".")[0] for n in tree.body if n.__class__.__name__ == "ImportFrom" and n.module}
    assert mods - {"__future__"} <= set(sys.stdlib_module_names), mods


@pytest.mark.parametrize("host,port", [("pypi.org", 443), ("pypi.org", 80), ("pypi.org", 8443), ("x.pypi.org", 443),
                                       ("api.github.com", 443), ("github.com", 443), ("evilgithub.com", 443),
                                       ("a.b.internal.example", 8443), ("internal.example", 8443)])
def test_the_sidecar_and_the_control_plane_answer_the_same(host, port):
    rules = [egress.parse_rule(t, "env") for t in ("pypi.org", "*.github.com", "*.internal.example:8443")]
    as_json = json.loads(json.dumps([[r.host, r.wildcard, r.port] for r in rules]))
    assert egress_sidecar.allowed(as_json, host, port) == any(r.matches(host, port) for r in rules)


def test_the_proxy_shares_the_address_checks():
    assert egress_proxy.public is egress_sidecar.public
    for a in ("10.0.0.1", "127.0.0.1", "169.254.169.254", "::ffff:10.0.0.1", "64:ff9b::a00:1", "2002:a00:1::1"):
        assert not egress_sidecar.public(ipaddress.ip_address(a)), a
    assert egress_sidecar.public(ipaddress.ip_address("64:ff9b::808:808"))


def _spec(**egress_block) -> EnvSpec:
    return EnvSpec(name="agent", harnesses=["claude-code"], egress=egress_block or None)


def test_the_policy_is_hangars():
    rules = localenv.egress_rules(_spec(mode="allowlist", presets=["github"], allow=["api.atlassian.com"]))
    hosts = {(h, w) for h, w, _ in rules}
    assert ("api.atlassian.com", False) in hosts and ("github.com", False) in hosts
    assert ("api.anthropic.com", False) in hosts  # the harness's own hosts, as a hangar sandbox gets them
    with pytest.raises(SystemExit, match="open"):
        localenv.egress_rules(_spec(mode="open"))


def test_docker_arguments_mount_nothing_and_name_the_dns():
    side = localenv.sidecar_args([["github.com", False, None]])
    assert localenv.SIDECAR_IMAGE in side and "DNS_PORT=53" in side
    agent = localenv.agent_args("172.30.0.2", "/tmp/x.env")
    assert agent[:2] == ["--dns", "172.30.0.2"]
    assert not {"-v", "--volume", "--mount", "--privileged"} & set(agent + side)
    assert "HTTPS_PROXY=http://egress:3128" in agent


def test_clones_and_contract(tmp_path):
    assert localenv.parse_clones(["code=CODE_REPO"]) == [("code", "CODE_REPO")]
    for bad in ("../x=CODE", "code=lower", "code"):
        with pytest.raises(Exception, match="DIR=VAR"):
            localenv.parse_clones([bad])
    c = tmp_path / "agent.env.example"
    c.write_text("# names only\nGH_TOKEN=\nTICKET_KEY=KAN-1\n lower=1\n")
    assert localenv.contract_names(c) == ["GH_TOKEN", "TICKET_KEY"]
    assert localenv.contract_names(None) == []


def test_policy_command_reads_the_catalog(tmp_path):
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump({
        "name": "agent", "harnesses": ["claude-code"], "egress": {"mode": "allowlist", "presets": [],
                                                                   "allow": ["*.atlassian.com:8443"]}}))
    r = CliRunner().invoke(cli.app, ["env", "local", "policy", "agent", "--env-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "*.atlassian.com:8443" in r.output.splitlines()


# ============================================================================ the harness (KAN-496)
def _agent_spec(**kw) -> EnvSpec:
    return EnvSpec(name="agent", harnesses=["claude-code"], model="claude-opus-5-5", env={"AGENT_ENV": "agent"}, mcp={
        "atlassian": {"url": "https://mcp.atlassian.com/v1/mcp",
                      "headers": {"Authorization": "${ATLASSIAN_MCP_AUTHORIZATION}"}},
        "ai-dlc": {"command": "ai-dlc", "args": ["mcp", "serve"], "env": {"KNOWLEDGE_REPO": "${KNOWLEDGE_REPO}"}},
        "github": {"url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "${GITHUB_MCP_TOKEN}"}},
        "codex-only": {"command": "x", "harnesses": ["codex"]}}, **kw)


def test_the_baked_harness_goes_first_on_path_with_its_pinned_cli():
    from hangar.harnesses import get_harness

    h = localenv.baked_harness(_agent_spec(), "bake")
    assert h is get_harness("claude-code") and h.version  # hangar's pinned, checksum-verified CLI
    env = localenv.harness_env(h, "/home/agent/.local/bin:/usr/bin")
    assert env["PATH"] == "/opt/hangar/harness/claude-code/bin:/home/agent/.local/bin:/usr/bin"
    assert env["DISABLE_AUTOUPDATER"] == "1" and env["HANGAR_HARNESS"] == "claude-code"
    assert localenv.baked_harness(_agent_spec(), "none") is None
    args = localenv.agent_args("172.30.0.2", "/tmp/x.env", env)
    assert f"PATH={env['PATH']}" in args


def test_mcp_servers_are_resolved_from_the_passed_variables_and_skipped_without_them():
    h = localenv.baked_harness(_agent_spec(), "bake")
    files, servers, skipped = localenv.harness_config(_agent_spec(), h, {
        "ATLASSIAN_MCP_AUTHORIZATION": "Basic abc", "KNOWLEDGE_REPO": "https://github.com/o/knowledge"})
    assert servers == ["ai-dlc", "atlassian"]
    assert skipped == ["MCP server 'github' skipped: needs GITHUB_MCP_TOKEN"]
    cfg = files[".claude.json"]
    assert cfg["hasCompletedOnboarding"] is True  # the harness's own config, as at a claim
    assert cfg["mcpServers"]["atlassian"] == {"type": "http", "url": "https://mcp.atlassian.com/v1/mcp",
                                              "headers": {"Authorization": "Basic abc"}}
    assert cfg["mcpServers"]["ai-dlc"]["env"] == {"KNOWLEDGE_REPO": "https://github.com/o/knowledge"}
    assert "codex-only" not in cfg["mcpServers"]


def test_a_cloned_input_reaches_mcp_servers_as_its_checkout():
    """ai-dlc's MCP server reads the knowledge checkout: KNOWLEDGE_REPO is /workspace/knowledge, not the clone URL."""
    h = localenv.baked_harness(_agent_spec(), "bake")
    env = {"ATLASSIAN_MCP_AUTHORIZATION": "Basic abc", "KNOWLEDGE_REPO": "https://github.com/o/knowledge"}
    files, servers, _ = localenv.harness_config(_agent_spec(), h, env, [("code", "CODE_REPO"),
                                                                        ("knowledge", "KNOWLEDGE_REPO")])
    assert files[".claude.json"]["mcpServers"]["ai-dlc"]["env"] == {"KNOWLEDGE_REPO": "/workspace/knowledge"}
    files, servers, _ = localenv.harness_config(_agent_spec(), h, {}, [("knowledge", "KNOWLEDGE_REPO")])
    assert "ai-dlc" in servers  # a clone needs no URL in the env to name its path


class FakeDocker:
    """`docker` and `sh` for the container: a dict of files, and canned command results."""

    def __init__(self, results=None):
        self.files: dict[str, str] = {"/home/agent/.claude.json": json.dumps({"userID": "u", "mcpServers": {"x": {}}})}
        self.calls: list[tuple] = []
        self.results = results or {}

    def docker(self, *args, input=None, check=True):
        import subprocess

        self.calls.append(args)
        if args[0] == "exec" and args[2] == "cat":
            ok = args[3] in self.files
            return subprocess.CompletedProcess(args, 0 if ok else 1, (self.files.get(args[3]) or "").encode(), b"")
        if args[:2] == ("exec", "-i"):
            self.files[args[-1]] = input.decode()
            return subprocess.CompletedProcess(args, 0, b"", b"")
        raise AssertionError(args)

    def sh(self, container, script):
        import subprocess

        self.calls.append(("sh", script))
        for key, (code, out, err) in self.results.items():
            if key in script:
                return subprocess.CompletedProcess(script, code, out.encode(), err.encode())
        raise AssertionError(script)


def test_the_config_is_merged_into_the_container_and_no_value_reaches_argv(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(localenv, "docker", fake.docker)
    spec = _agent_spec()
    lines = localenv.setup_harness("c", spec, localenv.baked_harness(spec, "bake"),
                                   {"ATLASSIAN_MCP_AUTHORIZATION": "Basic s3cret", "KNOWLEDGE_REPO": "k"})
    assert lines[0] == "claude-code: MCP servers configured: ai-dlc, atlassian"
    cfg = json.loads(fake.files["/home/agent/.claude.json"])
    assert cfg["userID"] == "u" and set(cfg["mcpServers"]) == {"x", "ai-dlc", "atlassian"}  # merged, not replaced
    assert cfg["mcpServers"]["atlassian"]["headers"]["Authorization"] == "Basic s3cret"
    assert not any("s3cret" in a for call in fake.calls for a in call)
    write = next(c for c in fake.calls if c[:2] == ("exec", "-i"))
    assert "umask 077" in write[5] and "chmod 600" in write[5]


OK = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})


def _probe(monkeypatch, results, passed=("CLAUDE_CODE_OAUTH_TOKEN",)):
    fake = FakeDocker(results)
    monkeypatch.setattr(localenv, "sh", fake.sh)
    spec = _agent_spec()
    return localenv.claude_probe("c", spec, localenv.baked_harness(spec, "bake"), list(passed)), fake


def test_the_probe_asks_claude_for_one_answer(monkeypatch):
    from hangar.harnesses import get_harness

    v = get_harness("claude-code").version
    bad, fake = _probe(monkeypatch, {"--version": (0, f"{v} (Claude Code)\n", ""), "claude -p": (0, OK, "")})
    assert bad == []
    run = fake.calls[-1][1]
    assert "claude -p 'say ok' --output-format json --model claude-opus-5-5" in run
    assert _probe(monkeypatch, {"--version": (0, f"{v}\n", ""), "claude -p": (0, OK, "")},
                  passed=["ANTHROPIC_API_KEY"])[0] == []


def test_the_probe_fails_clearly_without_a_credential(monkeypatch):
    bad, fake = _probe(monkeypatch, {}, passed=["GH_TOKEN"])
    assert len(bad) == 1 and all(w in bad[0] for w in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "setup-token"))
    assert fake.calls == []  # nothing ran


@pytest.mark.parametrize("results,expect", [
    ({"--version": (127, "", "bash: claude: command not found")}, "not on PATH"),
    ({"--version": (0, "0.0.1 (Claude Code)", "")}, "but hangar pins"),
    ({"--version": (0, "{v}", ""), "claude -p": (1, json.dumps({"type": "result", "is_error": True,
                                                               "result": "Invalid API key · Please run /login"}), "")},
     "Invalid API key"),
    ({"--version": (0, "{v}", ""), "claude -p": (124, "", "")}, "exit 124"),
])
def test_the_probe_names_what_failed(monkeypatch, results, expect):
    from hangar.harnesses import get_harness

    v = get_harness("claude-code").version
    results = {k: (c, o.replace("{v}", v), e) for k, (c, o, e) in results.items()}
    bad, _ = _probe(monkeypatch, results)
    assert len(bad) == 1 and expect in bad[0], bad


def test_check_runs_the_probe_in_the_first_instance(monkeypatch, tmp_path):
    """`check` with docker faked: the probe's failure fails the check, and --no-agent-probe skips it."""
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump({"name": "agent", "harnesses": ["claude-code"],
                                                         "egress": {"mode": "allowlist", "presets": ["github"]}}))
    monkeypatch.setattr(localenv, "build", lambda *a: "img")
    monkeypatch.setattr(localenv, "image_path", lambda image: "/usr/bin")
    started = []

    def start(spec, image, instance, passed, environ, env=None):
        started.append(env)
        return SimpleNamespace(**vars(localenv.names(instance)), passed=[])

    monkeypatch.setattr(localenv, "start", start)
    monkeypatch.setattr(localenv, "setup_harness", lambda *a: [])
    monkeypatch.setattr(localenv, "report", lambda *a: (0, "same\n"))
    monkeypatch.setattr(localenv, "egress_failures", lambda *a: [])
    monkeypatch.setattr(localenv, "down", lambda *a: None)
    monkeypatch.setattr(localenv, "docker", lambda *a, **k: SimpleNamespace(stdout=b"[]", returncode=0))
    args = ["env", "local", "check", "agent", "--env-dir", str(tmp_path)]
    r = CliRunner().invoke(cli.app, args)
    assert r.exit_code == 1 and "no Claude credential was passed" in r.output, r.output
    assert started[0]["PATH"].startswith("/opt/hangar/harness/claude-code/bin:")
    r = CliRunner().invoke(cli.app, [*args, "--no-agent-probe"])
    assert r.exit_code == 0, r.output


# ============================================================================ the sidecar on loopback
@pytest.fixture
def proxy():
    egress_sidecar.Proxy.rules = [["localhost", False, None], ["example.invalid", False, None]]
    server = egress_sidecar.Server(("127.0.0.1", 0), egress_sidecar.Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _ask(port: int, head: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(head)
        return s.recv(4096)


def test_proxy_refuses_plain_http_off_list_private_and_unresolvable(proxy):
    assert _ask(proxy, b"GET http://localhost/ HTTP/1.1\r\nHost: localhost\r\n\r\n").startswith(b"HTTP/1.1 405")
    assert _ask(proxy, b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert _ask(proxy, b"CONNECT localhost:8443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")  # port not allowed
    assert _ask(proxy, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")  # loopback
    assert _ask(proxy, b"CONNECT example.invalid:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")  # no address


QUERY = bytes.fromhex("123401000001000000000000") + b"\x07example\x03com\x00\x00\x01\x00\x01"


def test_dns_answers_refused_with_the_question():
    a = egress_sidecar.refused(QUERY)
    assert a is not None and a[:2] == b"\x12\x34" and a[2] & 0x80 and a[3] & 0x0F == 5
    assert a[4:6] == b"\0\1" and a[6:12] == bytes(6) and a[12:] == QUERY[12:]
    assert egress_sidecar.qname(QUERY) == "example.com"
    assert egress_sidecar.refused(QUERY[:5]) is None
    assert egress_sidecar.refused(a) is None  # a response is never answered (no reflection loops)


def test_dns_server_refuses_over_udp_and_tcp():
    udp = egress_sidecar.UdpServer(("127.0.0.1", 0), egress_sidecar.DnsUdp)
    tcp = egress_sidecar.Server(("127.0.0.1", 0), egress_sidecar.DnsTcp)
    for s in (udp, tcp):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.settimeout(5)
            u.sendto(QUERY, udp.server_address)
            assert u.recv(512)[3] & 0x0F == 5
        with socket.create_connection(tcp.server_address, timeout=5) as t:
            t.sendall(len(QUERY).to_bytes(2, "big") + QUERY)
            size = int.from_bytes(t.recv(2), "big")
            assert t.recv(size)[3] & 0x0F == 5
    finally:
        for s in (udp, tcp):
            s.shutdown()
            s.server_close()


# ============================================================================ the agent's identity (KAN-497)
SEAN = "712020:73fc02a4-9871-42c9-9b8c-0e1b5bfd84e4"
AGENT = "712020:00000000-0000-0000-0000-00000000a6e7"


def _jira_cfg(**kw):
    from hangar.jira import JiraConfig

    return JiraConfig.model_validate({"url": "https://example.atlassian.net", "token_credential": "jira",
                                      "email": "hangar@example.com", "webhook_secret_credential": "jira-webhook",
                                      "projects": {"KAN": {"repo": "o/r"}}, "trigger": {"status": "Ready for agent"},
                                      "agent_release": {"people": [SEAN], "github_people": ["Sean-Koval"]}, **kw})


def _jira(account: str, seen: list | None = None, status: int = 200):
    """A fake Jira: `myself` answers with `account`, only for the Authorization it was asked with."""
    import httpx

    def handle(req: httpx.Request) -> httpx.Response:
        (seen if seen is not None else []).append((str(req.url), req.headers.get("authorization")))
        if req.url.path == "/_edge/tenant_info":
            return httpx.Response(200, json={"cloudId": "c10ud"})
        if req.url.path.endswith("/myself"):
            return httpx.Response(status, json={"accountId": account, "displayName": "Some One"})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def test_a_person_on_the_release_list_is_refused_as_the_agent():
    seen: list = []
    why = localenv.identity_refusal(_jira_cfg(), {"ATLASSIAN_MCP_AUTHORIZATION": "Basic c2VhbjpzM2NyZXQ="},
                                    transport=_jira(SEAN, seen))
    assert why and "Some One" in why and SEAN in why and "agent_release.people" in why and "own Atlassian" in why
    assert seen == [("https://example.atlassian.net/rest/api/3/myself", "Basic c2VhbjpzM2NyZXQ=")]  # a GET only


def test_an_account_of_its_own_passes_and_a_service_account_is_read_through_the_gateway():
    assert localenv.identity_refusal(_jira_cfg(), {"ATLASSIAN_MCP_AUTHORIZATION": "Basic YWdlbnQ6dA=="},
                                     transport=_jira(AGENT)) is None
    seen: list = []
    assert localenv.identity_refusal(_jira_cfg(), {"ATLASSIAN_MCP_AUTHORIZATION": "Bearer key"},
                                     transport=_jira(AGENT, seen)) is None
    assert seen[-1] == ("https://api.atlassian.com/ex/jira/c10ud/rest/api/3/myself", "Bearer key")


@pytest.mark.parametrize("cfg,environ,status,expect", [
    ({"agent_release": None}, {"ATLASSIAN_MCP_AUTHORIZATION": "Basic x"}, 200, "no agent_release.people"),
    ({}, {}, 200, "is not set"),
    ({}, {"ATLASSIAN_MCP_AUTHORIZATION": "Basic x"}, 401, "refused ATLASSIAN_MCP_AUTHORIZATION (401)"),
])
def test_it_fails_closed(cfg, environ, status, expect):
    why = localenv.identity_refusal(_jira_cfg(**cfg), environ, transport=_jira(AGENT, status=status))
    assert why and expect in why, why


def test_the_release_config_reads_hangar_config_list_json(tmp_path):
    raw = _jira_cfg().model_dump(mode="json")
    for content in (raw, json.dumps(raw), [{"key": "HANGAR_X", "value": "1"}, {"key": "HANGAR_JIRA", "value": raw}]):
        f = tmp_path / "jira.json"
        f.write_text(json.dumps(content))
        assert localenv.load_release_config(f).agent_release.people == [SEAN]
    f.write_text("[]")
    with pytest.raises(SystemExit, match="not a HANGAR_JIRA config"):
        localenv.load_release_config(f)


def test_up_refuses_before_building_when_the_agent_is_a_person(monkeypatch, tmp_path):
    f = tmp_path / "jira.json"
    f.write_text(json.dumps(_jira_cfg().model_dump(mode="json")))
    monkeypatch.setattr(localenv, "myself", lambda cfg, auth, transport=None: {"accountId": SEAN})
    monkeypatch.setattr(localenv, "github_user", lambda token, transport=None: {"login": "agent-bot", "id": 9})
    monkeypatch.setattr(localenv, "build", lambda *a: pytest.fail("built"))
    monkeypatch.setattr(localenv, "down", lambda *a: pytest.fail("touched docker"))
    monkeypatch.setenv("ATLASSIAN_MCP_AUTHORIZATION", "Basic x")
    for cmd in ("up", "check"):
        r = CliRunner().invoke(cli.app, ["env", "local", cmd, "agent", "--env-dir", str(tmp_path),
                                         "--release-config", str(f)])
        assert r.exit_code != 0 and "refused" in str(r.exception) + r.output, (cmd, r.output, r.exception)


def _github(login: str | None, uid: int = 1, status: int = 200, seen: list | None = None):
    """A fake GitHub: `GET /user` answers with `login`; an App installation token gets 403."""
    import httpx

    def handle(req: httpx.Request) -> httpx.Response:
        (seen if seen is not None else []).append((req.method, str(req.url), req.headers.get("authorization")))
        if req.url.path != "/user":
            return httpx.Response(404)
        if req.headers["authorization"].startswith("Bearer ghs_"):
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return httpx.Response(status, json={"login": login, "id": uid, "type": "User"})

    return httpx.MockTransport(handle)


def test_a_releasers_github_token_is_refused():
    seen: list = []
    why = localenv.github_identity_refusal(_jira_cfg(), {"GH_TOKEN": "github_pat_x"},
                                           transport=_github("sean-koval", 4807, seen=seen))
    assert why and "sean-koval" in why and "agent_release.github_people" in why and "App or bot" in why
    assert seen == [("GET", "https://api.github.com/user", "Bearer github_pat_x")]  # read-only
    cfg = _jira_cfg(agent_release={"people": [SEAN], "github_people": ["4807"]})  # by numeric id too
    assert localenv.github_identity_refusal(cfg, {"GH_TOKEN": "t"}, transport=_github("renamed", 4807))


def test_a_bot_or_app_token_passes():
    assert localenv.github_identity_refusal(_jira_cfg(), {"GH_TOKEN": "github_pat_bot"},
                                            transport=_github("agent-bot", 99)) is None
    assert localenv.github_identity_refusal(_jira_cfg(), {"GH_TOKEN": "ghs_app"}, transport=_github(None)) is None


@pytest.mark.parametrize("release,environ,status,expect", [
    ({"people": [SEAN]}, {"GH_TOKEN": "t"}, 200, "no agent_release.github_people"),
    ({"people": [SEAN], "github_people": ["Sean-Koval"]}, {}, 200, "GH_TOKEN is not set"),
    ({"people": [SEAN], "github_people": ["Sean-Koval"]}, {"GH_TOKEN": "t"}, 401, "refused GH_TOKEN (401)"),
])
def test_the_github_check_fails_closed(release, environ, status, expect):
    why = localenv.github_identity_refusal(_jira_cfg(agent_release=release), environ,
                                           transport=_github("agent-bot", status=status))
    assert why and expect in why, why


def test_up_refuses_a_releasers_github_token_before_building(monkeypatch, tmp_path):
    f = tmp_path / "jira.json"
    f.write_text(json.dumps(_jira_cfg().model_dump(mode="json")))
    monkeypatch.setattr(localenv, "myself", lambda cfg, auth, transport=None: {"accountId": AGENT})
    monkeypatch.setattr(localenv, "github_user", lambda token, transport=None: {"login": "Sean-Koval", "id": 1})
    monkeypatch.setattr(localenv, "build", lambda *a: pytest.fail("built"))
    monkeypatch.setenv("ATLASSIAN_MCP_AUTHORIZATION", "Basic x")
    monkeypatch.setenv("GH_TOKEN", "t")
    r = CliRunner().invoke(cli.app, ["env", "local", "up", "agent", "--env-dir", str(tmp_path), "--release-config",
                                     str(f)])
    assert r.exit_code != 0 and "github_people" in str(r.exception), (r.output, r.exception)
