"""`hangar env local` (KAN-472): the runner's arguments, the policy it asks hangar for, and the egress sidecar on
loopback (the CONNECT proxy and the DNS server that refuses). A live run is `hangar env local check` with Docker."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
from pathlib import Path

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
