"""Egress allowlists (--- egress): the policy model, and the proxy end to end on loopback: authentication, the
allowlist, SNI and Host checks, private addresses, audit, session events, approvals, and the network each provider
puts a proxied sandbox on."""

from __future__ import annotations

import asyncio
import base64
import socket
import ssl
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hangar import egress, egress_proxy, egressapi
from hangar.config import Settings
from hangar.envspec import EnvCatalog, EnvSpec
from hangar.fleet import Fleet, FleetError
from hangar.models import AuditEvent, Job, Sandbox, SandboxState
from tests.conftest import ENVS, FakeProvider, free_port

# ============================================================================ rules and resolution


def test_rules_parse_hosts_ports_and_wildcards_and_refuse_the_rest():
    r = egress.parse_rule("Docs.Python.org.", "env")
    assert (r.host, r.wildcard, r.port) == ("docs.python.org", False, None)
    assert r.matches("docs.python.org", 443) and r.matches("docs.python.org", 80)
    assert not r.matches("docs.python.org", 8443) and not r.matches("x.docs.python.org", 443)
    w = egress.parse_rule("*.googleapis.com:8443", "env")
    assert w.matches("storage.googleapis.com", 8443) and not w.matches("googleapis.com", 8443)
    assert not w.matches("storage.googleapis.com", 443) and not w.matches("evilgoogleapis.com", 8443)
    assert egress.parse_rule("[2001:db8::1]:443", "x").text == "[2001:db8::1]:443"
    for bad in ("*", "*.com", "https://pypi.org", "pypi.org/simple", "a*.b.com", "*.1.2.3.4", "", "pypi.org:0",
                "pypi.org:99999", "user@pypi.org", "exämple.com"):
        with pytest.raises(egress.EgressError):
            egress.parse_rule(bad, "env")


def test_the_spec_validates_presets_and_rules():
    egress.EgressSpec(mode="allowlist", presets=["pypi", "npm"], allow=["*.example.com"])
    for bad in ({"presets": ["nope"]}, {"allow": ["*"]}, {"mode": "sometimes"}, {"extra": 1}):
        with pytest.raises(ValueError):
            egress.EgressSpec(**bad)


def spec(**egress_block) -> EnvSpec:
    return EnvSpec(name="api", harnesses=["claude-code"], egress=egress_block or None)


def test_open_is_the_default_and_allows_everything():
    pol = egress.resolve(Settings(), spec(), harness="claude-code")
    assert pol.mode == "open" and not pol.proxied and pol.enforced
    assert pol.allows("anything.example", 22) is not None


def test_allowlist_is_the_harness_the_packages_preset_and_the_listed_hosts():
    pol = egress.resolve(Settings(), spec(mode="allowlist", allow=["docs.python.org"]), harness="claude-code")
    assert pol.proxied and pol.enforced
    for host in ("api.anthropic.com", "platform.claude.com", "pypi.org", "files.pythonhosted.org",
                 "registry.npmjs.org", "index.crates.io", "proxy.golang.org", "github.com", "docs.python.org"):
        assert pol.allows(host, 443), host
    assert pol.allows("api.openai.com", 443) is None  # another harness's model API
    assert pol.allows("evil.example", 443) is None and pol.allows("pypi.org", 22) is None
    assert pol.allows("pypi.org", 443).source == "preset:packages"


def test_none_keeps_only_the_harness_and_approvals():
    pol = egress.resolve(Settings(), spec(mode="none", allow=["docs.python.org"]), harness="codex",
                         approved=[{"rule": "example.com", "request": "rq-1"}])
    assert [r.text for r in pol.rules] == ["api.openai.com", "chatgpt.com", "auth.openai.com", "example.com"]
    assert pol.allows("pypi.org", 443) is None and pol.allows("example.com", 443).source == "approved:rq-1"


def test_a_repo_policy_tightens_and_adds_but_never_widens():
    env_allow = spec(mode="allowlist")
    pol = egress.resolve(Settings(), env_allow, egress.EgressSpec(mode="none"), "claude-code")
    assert pol.mode == "none" and pol.proxied
    pol = egress.resolve(Settings(), env_allow, egress.EgressSpec(mode="open", allow=["x.example"]), "claude-code")
    assert pol.mode == "allowlist" and pol.allows("x.example", 443) and "only tighten" in pol.notes[0]
    # an open environment can't enforce what its repo asks: the network stays open, and the sandbox says so
    pol = egress.resolve(Settings(), spec(), egress.EgressSpec(mode="allowlist"), "claude-code")
    assert pol.mode == "allowlist" and not pol.proxied and not pol.enforced and "not enforced" in pol.notes[0]


def test_the_repo_policy_carries_egress_and_sandboxes_of_the_repo_find_it(store):
    from hangar.repopolicy import PolicyUpdate, RepoPolicies, RepoPolicy

    with pytest.raises(ValueError):
        PolicyUpdate(repo="acme/app", egress={"allow": ["*"]})
    RepoPolicies(store).put(RepoPolicy(repo="acme/app", egress=egress.EgressSpec(mode="none")))
    assert egress.repo_egress(store, "https://github.com/Acme/app.git").mode == "none"
    assert egress.repo_egress(store, "https://github.com/acme/other.git") is None


def test_hangar_egress_default_applies_to_environments_that_dont_say():
    s = Settings(egress_default="allowlist")
    assert egress.proxied(s, spec()) and not egress.proxied(s, spec(mode="open"))


def test_extends_unions_the_allow_lists(tmp_path):
    (tmp_path / "child.yaml").write_text(yaml.safe_dump(
        {"name": "child", "extends": "parent", "egress": {"allow": ["b.example"]}}))
    (tmp_path / "parent.yaml").write_text(yaml.safe_dump(
        {"name": "parent", "egress": {"mode": "allowlist", "allow": ["a.example"]}}))
    e = EnvCatalog([tmp_path]).get("child").egress
    assert e.mode == "allowlist" and e.allow == ["a.example", "b.example"]


def test_setting_egress_on_an_environment_needs_an_admin():
    from hangar import identity

    assert "egress" in identity.ADMIN_FIELDS


def test_the_proxy_token_is_per_sandbox_and_checked_in_constant_time():
    key = b"k" * 32
    t = egress.token(key, "sb-abc123")
    assert egress.check_token(key, "sb-abc123", t)
    assert not egress.check_token(key, "sb-abc124", t) and not egress.check_token(b"x" * 32, "sb-abc123", t)
    assert not egress.check_token(key, "../etc", t) and not egress.check_token(key, "sb-abc123", t[:-1])


def test_proxy_env_sets_both_spellings_and_keeps_the_gateway_direct():
    env = egress.proxy_env("http://10.0.0.1:3128", "sb-1234", "p" * 64, ["host.docker.internal"])
    assert env["HTTPS_PROXY"] == env["https_proxy"] == env["http_proxy"] == f"http://sb-1234:{'p' * 64}@10.0.0.1:3128"
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,::1,host.docker.internal"


# ============================================================================ SNI parsing


def client_hello(name: str | None) -> bytes:
    ctx = ssl.create_default_context()
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(inc, out, server_hostname=name)
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    return out.read()


def test_sni_is_read_from_a_real_client_hello_and_garbage_gives_none():
    assert egress_proxy.sni(client_hello("pypi.org")) == "pypi.org"
    hello = client_hello("pypi.org")
    for junk in (b"", b"\x16\x03\x01", hello[:40], b"GET / HTTP/1.1\r\n\r\n", bytes(range(256)) * 4):
        assert egress_proxy.sni(junk) is None


def test_public_addresses_only():
    import ipaddress as ip

    for a in ("10.1.2.3", "172.16.0.1", "192.168.1.1", "100.64.0.1", "127.0.0.1", "169.254.169.254", "0.0.0.0",
              "::1", "fd20:ce::254", "fe80::1", "::ffff:169.254.169.254", "224.0.0.1"):
        assert not egress_proxy.public(ip.ip_address(a)), a
    assert egress_proxy.public(ip.ip_address("151.101.0.223"))
    assert not egress_proxy.reachable_private(ip.ip_address("169.254.169.254"))
    assert egress_proxy.reachable_private(ip.ip_address("10.1.2.3"))


# ============================================================================ the proxy, end to end on loopback
class Activity:
    def __init__(self):
        self.events: list[tuple[str, list]] = []

    def append(self, sid, events):
        self.events.append((sid, events))


async def _echo(reader, writer):
    """An upstream that answers what it got, so a test sees exactly what the proxy forwarded."""
    try:
        data = await reader.read(65536)
        writer.write(b"ECHO:" + data)
        await writer.drain()
    finally:
        writer.close()


@pytest.fixture
async def proxied(settings, store, broker, tmp_path, monkeypatch):
    """A fleet with an `allowlist` environment allowing upstream.test on the echo server's port, a running sandbox
    of it, and the proxy listening on loopback. upstream.test resolves to 127.0.0.1, which counts as public here."""
    up_port, px_port = free_port(), free_port()
    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "locked.yaml").write_text(yaml.safe_dump({"name": "locked", "harnesses": ["claude-code"], "egress": {
        "mode": "allowlist", "presets": [], "allow": [f"upstream.test:{up_port}"]}}))
    settings.egress_port = px_port
    f = Fleet(settings, store, broker, EnvCatalog([ENVS, envs]))
    f.providers["docker"] = FakeProvider()
    sb = Sandbox(name="s", environment="locked", provider="docker", state=SandboxState.RUNNING, person="ann",
                 labels={"hangar.session": "ses-1"}, info={"harness": "claude-code"})
    store.put(sb)
    real = socket.getaddrinfo

    def fake_dns(host, *a, **k):
        if host in ("upstream.test", "other.test"):
            return real("127.0.0.1", *a, **k)
        if host == "internal.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.8.7", a[0] if a else 0))]
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(egress_proxy, "public", lambda ip: str(ip) == "127.0.0.1")
    server = await asyncio.start_server(_echo, "127.0.0.1", up_port)
    f.egress.activity = Activity()
    await f.egress.listen("127.0.0.1", relay=False)
    yield SimpleNamespace(fleet=f, sb=sb, up=up_port, px=px_port, store=store)
    await f.egress.stop()
    server.close()


def auth(f: Fleet, sb: Sandbox, password: str | None = None) -> str:
    cred = f"{sb.id}:{password or egress.token(f.egress.key, sb.id)}"
    return "Proxy-Authorization: Basic " + base64.b64encode(cred.encode()).decode()


async def ask(px: int, head: str, then: bytes = b"") -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    r, w = await asyncio.open_connection("127.0.0.1", px)
    w.write(head.encode() + b"\r\n\r\n" + then)
    await w.drain()
    first = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
    return first, r, w


def audits(store, action: str) -> list[AuditEvent]:
    return [e for e in store.audit_tail(500) if e.action == action]


async def test_an_allowed_host_with_matching_sni_is_tunnelled(proxied):
    p = proxied
    first, r, w = await ask(p.px, f"CONNECT upstream.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert first.startswith(b"HTTP/1.1 200")
    hello = client_hello("upstream.test")
    w.write(hello)
    await w.drain()
    got = await asyncio.wait_for(r.read(65536), 5)
    assert got == b"ECHO:" + hello  # the ClientHello went through untouched: no TLS termination
    w.close()
    await asyncio.sleep(0.05)
    p.fleet.egress.flush()
    [a] = audits(p.store, "egress.allow")
    assert a.target == p.sb.id and a.detail["host"] == "upstream.test" and a.detail["connections"] == 1
    assert a.detail["person"] == "ann" and a.detail["session"] == "ses-1" and a.detail["bytes_out"] == len(hello)


async def test_a_host_off_the_list_is_refused_audited_and_shown_in_the_session(proxied):
    p = proxied
    first, r, w = await ask(p.px, f"CONNECT other.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    body = await r.read()
    assert first.startswith(b"HTTP/1.1 403") and b"hangar-agent env request egress" in body
    [d] = audits(p.store, "egress.deny")
    assert d.detail["host"] == "other.test" and d.detail["reason"] == "not_allowed" and d.detail["person"] == "ann"
    [(sid, [ev])] = p.fleet.egress.activity.events
    assert sid == "ses-1" and ev.data["egress"] == "deny" and "other.test" in ev.text


async def test_sni_that_names_another_host_is_cut_off(proxied):
    """Domain fronting: CONNECT to an allowed name, then a ClientHello for another one on the same CDN."""
    p = proxied
    first, r, w = await ask(p.px, f"CONNECT upstream.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}",
                            client_hello("evil.example"))
    assert first.startswith(b"HTTP/1.1 200")
    assert await asyncio.wait_for(r.read(), 5) == b""  # closed: nothing reached the upstream
    [d] = audits(p.store, "egress.deny")
    assert d.detail["reason"] == "sni_mismatch"


async def test_plain_http_inside_connect_must_carry_the_host(proxied):
    p = proxied
    first, r, w = await ask(p.px, f"CONNECT upstream.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}",
                            b"GET / HTTP/1.1\r\nHost: evil.example\r\n\r\n")
    assert await asyncio.wait_for(r.read(), 5) == b""
    assert audits(p.store, "egress.deny")[0].detail["reason"] == "host_mismatch"


async def test_absolute_form_http_is_forwarded_one_request_at_a_time_without_the_credential(proxied):
    p = proxied
    first, r, w = await ask(p.px, f"GET http://upstream.test:{p.up}/simple/x?y=1 HTTP/1.1\r\nHost: upstream.test:"
                                  f"{p.up}\r\n{auth(p.fleet, p.sb)}\r\nProxy-Connection: keep-alive\r\nAccept: */*")
    got = first + await asyncio.wait_for(r.read(), 5)
    assert got.startswith(b"ECHO:GET /simple/x?y=1 HTTP/1.1\r\n")
    assert b"Proxy-Authorization" not in got and b"keep-alive" not in got and b"Connection: close" in got
    assert b"Accept: */*" in got


async def test_no_or_wrong_credentials_get_407_and_a_stopped_sandbox_gets_nothing(proxied):
    p = proxied
    for creds in ("", auth(p.fleet, p.sb, "0" * 64), "Proxy-Authorization: Bearer x"):
        first, r, w = await ask(p.px, f"CONNECT upstream.test:{p.up} HTTP/1.1\r\n{creds}".rstrip())
        assert first.startswith(b"HTTP/1.1 407"), creds
    p.sb.state = SandboxState.STOPPED
    p.store.put(p.sb)
    first, r, w = await ask(p.px, f"CONNECT upstream.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert first.startswith(b"HTTP/1.1 407")


async def test_a_name_that_resolves_to_a_private_address_is_refused(proxied):
    p = proxied
    egress.approve(p.store, "session", "ses-1", "internal.test", None, "ann")
    p.fleet.egress.invalidate()
    first, r, w = await ask(p.px, f"CONNECT internal.test:443 HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert first.startswith(b"HTTP/1.1 403")
    assert audits(p.store, "egress.deny")[0].detail["reason"] == "private_address"


async def test_an_owner_approval_opens_the_host_for_the_session(proxied):
    p = proxied
    first, *_ = await ask(p.px, f"CONNECT other.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert first.startswith(b"HTTP/1.1 403")
    req = egressapi.EgressRequest(id="rq-0123456789ab", environment="locked", host="other.test", port=p.up,
                                  why="docs", requester="session:ses-1", owner="ann", state="approved",
                                  created_at=0, decided_by="ann")
    msg = egressapi.on_decided(req, p.fleet)
    assert "now allowed for this session" in msg
    assert audits(p.store, "egress.approve")[0].detail["rule"] == f"other.test:{p.up}"
    first, r, w = await ask(p.px, f"CONNECT other.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert first.startswith(b"HTTP/1.1 200")
    v = egressapi.check(p.fleet.catalog.get("locked"), "other.test", p.up, sandbox=p.sb, fleet=p.fleet)
    assert v.allowed and v.mode == "allowlist" and v.rule == f"other.test:{p.up}"
    # another session of the same environment doesn't get it
    other = p.sb.model_copy(update={"id": "sb-other1", "labels": {"hangar.session": "ses-2"}})
    p.store.put(other)
    first, *_ = await ask(p.px, f"CONNECT other.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, other)}")
    assert first.startswith(b"HTTP/1.1 403")


def test_a_denied_request_changes_nothing_and_a_job_request_lands_on_its_session(fleet):
    req = egressapi.EgressRequest(id="rq-1", environment="plain", host="x.example", why="", requester="job:job-1",
                                  owner=None, state="denied", created_at=0, note="no")
    assert egressapi.on_decided(req, fleet) == "egress to x.example was denied: no"
    assert egress.approvals(fleet.store, environment="plain", session="ses-9") == []
    fleet.store.put(Job(id="job-1", title="t", instructions="x", environment="plain", session_id="ses-9"))
    req = req.model_copy(update={"state": "approved", "decided_by": "boss"})
    egressapi.on_decided(req, fleet)
    assert egress.approvals(fleet.store, session="ses-9")[0]["rule"] == "x.example"


async def test_denials_are_audited_at_a_bounded_rate(proxied, monkeypatch):
    p = proxied
    monkeypatch.setattr(egress_proxy, "DENY_AUDITS_PER_MINUTE", 3)
    for _ in range(6):
        await ask(p.px, f"CONNECT other.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert len(audits(p.store, "egress.deny")) == 3
    p.fleet.egress._deny_log.clear()
    await ask(p.px, f"CONNECT other.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert audits(p.store, "egress.deny")[0].detail["suppressed_before"] == 3


async def test_a_sandbox_has_a_bounded_number_of_tunnels(proxied):
    p = proxied
    p.fleet.settings.egress_max_connections_per_sandbox = 1
    first, r, w = await ask(p.px, f"CONNECT upstream.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert first.startswith(b"HTTP/1.1 200")
    second, *_ = await ask(p.px, f"CONNECT upstream.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert second.startswith(b"HTTP/1.1 429")
    w.close()


async def test_garbage_gets_400_and_the_listener_survives(proxied):
    p = proxied
    for junk in (b"\x00\x01\x02\r\n\r\n", b"GET / HTTP/1.1\r\n\r\n", b"CONNECT a b c d\r\n\r\n"):
        r, w = await asyncio.open_connection("127.0.0.1", p.px)
        w.write(junk)
        await w.drain()
        out = await asyncio.wait_for(r.read(), 5)
        assert out.startswith(b"HTTP/1.1 4"), junk
    first, *_ = await ask(p.px, f"CONNECT upstream.test:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert first.startswith(b"HTTP/1.1 200")


# ============================================================================ wiring a sandbox
async def test_an_open_environment_gets_no_proxy(fleet):
    sb = Sandbox(name="s", environment="plain", provider="docker")
    env = await egress.attach(fleet, fleet.providers["docker"], sb, fleet.catalog.get("plain"))
    assert env == {} and sb.info["egress"] == {"mode": "open", "enforced": True}


async def test_a_proxied_environment_fails_closed_where_it_cant_be_enforced(fleet):
    sb = Sandbox(name="s", environment="plain", provider="docker", info={"harness": "claude-code"})
    locked = fleet.catalog.get("plain").model_copy(update={"egress": egress.EgressSpec(mode="allowlist")})
    prov = SimpleNamespace(name="e2b")
    with pytest.raises(FleetError, match="isn't supported on the e2b provider"):
        await egress.attach(fleet, prov, sb, locked)
    with pytest.raises(FleetError, match="HANGAR_EGRESS_URL"):
        await egress.attach(fleet, SimpleNamespace(name="kubernetes"), sb, locked)
    fleet.settings.egress_url = "http://hangar-gateway.hangar.svc:3128"
    fleet.settings.gateway_url = "http://hangar-gateway.hangar.svc:8788"
    env = await egress.attach(fleet, SimpleNamespace(name="kubernetes"), sb, locked)
    assert env["HTTPS_PROXY"].endswith("@hangar-gateway.hangar.svc:3128")
    assert "hangar-gateway.hangar.svc" in env["NO_PROXY"].split(",")
    assert sb.info["egress"]["mode"] == "allowlist" and sb.info["egress"]["enforced"]


async def test_docker_puts_a_proxied_environment_on_the_internal_network(fleet, monkeypatch):
    calls = []

    class Net:
        async def egress_gateway(self, create=True):
            return "172.31.0.1"

    async def listen(host, relay=True):
        calls.append(host)

    monkeypatch.setattr(fleet.egress, "listen", listen)
    sb = Sandbox(name="s", environment="plain", provider="docker", info={"harness": "claude-code"})
    locked = fleet.catalog.get("plain").model_copy(update={"egress": egress.EgressSpec(mode="none")})
    env = await egress.attach(fleet, SimpleNamespace(name="docker", network=Net()), sb, locked)
    assert calls == ["172.31.0.1"] and env["https_proxy"].endswith(f"@172.31.0.1:{fleet.settings.egress_port}")

    async def no_bind(host, relay=True):
        raise OSError(99, "Cannot assign requested address")

    monkeypatch.setattr(fleet.egress, "listen", no_bind)
    with pytest.raises(FleetError, match="Docker Desktop"):
        await egress.attach(fleet, SimpleNamespace(name="docker", network=Net()), sb, locked)


def test_docker_run_kwargs_for_the_egress_network():
    from hangar.providers.docker import DockerProvider
    from hangar.providers.docker_net import EGRESS_NAME, network_for

    s = Settings(gateway_url="http://host.docker.internal:8788")
    assert network_for(s, spec(mode="allowlist")) == EGRESS_NAME and network_for(s, spec()) == "hangar-sandboxes"
    prov = DockerProvider.__new__(DockerProvider)
    prov.settings = s
    kw = prov._run_kwargs("n", "img", SimpleNamespace(cpus=1, memory_gb=1), {}, "h", net=EGRESS_NAME,
                          egress_ip="172.31.0.1")
    assert kw["network"] == EGRESS_NAME and kw["extra_hosts"] == {"host.docker.internal": "172.31.0.1"}
    assert "ports" not in kw


async def test_the_docker_egress_network_is_internal_and_checked():
    from docker.errors import NotFound

    from hangar.providers.base import ProviderError
    from hangar.providers.docker_net import EGRESS_NAME, EGRESS_OPTIONS, Network

    class Api:
        def __init__(self, nets=None):
            self.nets, self.created = dict(nets or {}), []

        def inspect_network(self, name):
            if name not in self.nets:
                raise NotFound("no")
            return self.nets[name]

        def create_network(self, name, driver=None, options=None, labels=None, internal=False):
            self.created.append((name, internal, options))
            self.nets[name] = {"Driver": driver, "Options": options, "Internal": internal,
                               "IPAM": {"Config": [{"Subnet": "172.31.0.0/16", "Gateway": "172.31.0.1"}]}}

    api = Api()
    n = Network(SimpleNamespace(api=api), Settings())
    assert await n.egress_gateway(create=False) is None
    assert await n.egress_gateway() == "172.31.0.1"
    assert api.created == [(EGRESS_NAME, True, EGRESS_OPTIONS)]
    bad = Api({EGRESS_NAME: {"Driver": "bridge", "Options": EGRESS_OPTIONS, "Internal": False}})
    with pytest.raises(ProviderError, match="isn't internal"):
        await Network(SimpleNamespace(api=bad), Settings()).egress_gateway()


def test_kubernetes_labels_proxied_pods_and_gives_their_template_its_own_policy():
    pytest.importorskip("kubernetes")
    from hangar.providers.kubernetes import KubernetesProvider

    prov = KubernetesProvider.__new__(KubernetesProvider)
    prov.settings = Settings(k8s_runtime_class="")
    assert egress.K8S_LABEL not in prov._pod_template(spec(), "img")["metadata"]["labels"]
    assert prov._pod_template(spec(mode="allowlist"), "img")["metadata"]["labels"][egress.K8S_LABEL] == "proxied"
    pol = egress.k8s_template_policy()
    assert pol["ingress"] == []
    ports = sorted(p["port"] for r in pol["egress"] for p in r["ports"])
    assert ports == [53, 53, 3128, 8788]
    assert not any("ipBlock" in peer and peer["ipBlock"]["cidr"] != "169.254.20.10/32"
                   for r in pol["egress"] for peer in r["to"])


def test_the_manifests_give_proxied_pods_dns_the_gateway_and_the_proxy_and_nothing_else():
    k8s = Path(__file__).parents[1] / "deploy" / "k8s"
    docs = [d for f in sorted(k8s.rglob("*.yaml")) for d in yaml.safe_load_all(f.read_text())
            if isinstance(d, dict) and d.get("kind") == "NetworkPolicy"
            and d["metadata"].get("namespace") == "hangar-sandboxes"]
    proxied_labels = {egress.K8S_LABEL: "proxied"}

    def selects(sel: dict) -> bool:
        if any(proxied_labels.get(k) != v for k, v in (sel.get("matchLabels") or {}).items()):
            return False
        for e in sel.get("matchExpressions") or []:
            v = proxied_labels.get(e["key"])
            if e["operator"] == "NotIn" and v in e["values"] or e["operator"] == "In" and v not in e["values"]:
                return False
        return True

    rules = [r for d in docs if "Egress" in (d["spec"].get("policyTypes") or []) and selects(d["spec"]["podSelector"])
             for r in d["spec"].get("egress") or []]
    assert rules
    for r in rules:
        for peer in r["to"]:
            if "ipBlock" in peer:  # NodeLocal DNS only: no address block reaches the internet
                assert peer["ipBlock"]["cidr"] == "169.254.20.10/32", peer
            else:  # in-cluster services only (hangar's pods, kube-dns, an OTel collector)
                assert peer.get("namespaceSelector"), peer
    ports = {p["port"] for r in rules for p in r["ports"]}
    assert {53, 3128, 8788} <= ports
