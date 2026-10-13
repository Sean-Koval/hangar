"""The NetworkPolicies in deploy/k8s, read together (workstream isolation): whatever a later manifest adds, sandboxes
must not reach each other, the metadata servers or hangar's API. NetworkPolicies only add allowances, so a rule in
any file can open what the baseline closes; this reads every file, the optional ones too."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import yaml

K8S = Path(__file__).parents[1] / "deploy" / "k8s"
SANDBOXES = "hangar-sandboxes"
# Addresses no sandbox may reach: the GCE metadata server, GKE's Workload Identity server, and the ranges pods,
# Services and nodes get (RFC1918, CGNAT, class E), IPv4 and IPv6 (ULA, link-local).
FORBIDDEN = ["169.254.169.254", "169.254.169.252", "10.0.0.7", "172.16.3.4", "192.168.1.1", "100.64.0.9",
             "240.0.0.5", "127.0.0.1", "fd20:ce::254", "fe80::1", "::1"]
HANGAR_PODS = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "hangar"}},
               "podSelector": {"matchLabels": {"app": "hangar"}}}


def policies() -> list[dict]:
    out = []
    for f in sorted(K8S.rglob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text()):
            if isinstance(doc, dict) and doc.get("kind") == "NetworkPolicy":
                out.append({**doc, "_file": f.relative_to(K8S).as_posix()})
    return out


def sandbox_policies() -> list[dict]:
    return [p for p in policies() if p["metadata"].get("namespace") == SANDBOXES]


def covers(block: dict, addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    net = ipaddress.ip_network(block["cidr"])
    if ip.version != net.version or ip not in net:
        return False
    return not any(ip in ipaddress.ip_network(x) for x in block.get("except") or [] if
                   ipaddress.ip_network(x).version == ip.version)


def test_every_manifest_parses_and_the_sandbox_namespace_has_policies():
    names = {p["metadata"]["name"] for p in sandbox_policies()}
    assert {"sandbox-egress-baseline", "sandbox-deny-ingress", "allow-hangar-envd", "allow-hangar-gateway"} <= names


def test_sandboxes_take_no_ingress_but_from_hangar_pods():
    """A deny-all ingress policy selects every sandbox pod, and every allowance comes from hangar's pods: no other
    sandbox, no address block, no 'from anywhere'."""
    ps = sandbox_policies()
    assert any(p["spec"]["podSelector"] == {} and p["spec"].get("policyTypes") == ["Ingress"]
               and not p["spec"].get("ingress") for p in ps)
    for p in ps:
        for rule in p["spec"].get("ingress") or []:
            assert rule.get("from"), f"{p['_file']}: an ingress rule without `from` admits everyone"
            for peer in rule["from"]:
                assert peer == HANGAR_PODS, f"{p['_file']}: ingress from {peer}"


def test_sandbox_egress_reaches_no_pod_metadata_or_private_address():
    for p in sandbox_policies():
        for rule in p["spec"].get("egress") or []:
            assert rule.get("to"), f"{p['_file']}: an egress rule without `to` allows everything"
            for peer in rule["to"]:
                if "ipBlock" in peer:
                    ports = {x.get("port") for x in rule.get("ports") or []}
                    for addr in FORBIDDEN:
                        if covers(peer["ipBlock"], addr):
                            # NodeLocal DNSCache: one link-local address, DNS only
                            assert peer["ipBlock"]["cidr"] == "169.254.20.10/32" and ports == {53}, \
                                f"{p['_file']}: egress to {addr} through {peer['ipBlock']}"
                    continue
                # A podSelector without a namespaceSelector means this namespace: the other sandboxes.
                ns = (peer.get("namespaceSelector") or {}).get("matchLabels") or {}
                assert "namespaceSelector" in peer and ns, f"{p['_file']}: egress to {peer} can reach sandboxes"
                assert ns.get("kubernetes.io/metadata.name") not in (None, SANDBOXES), f"{p['_file']}: {peer}"
                if ns["kubernetes.io/metadata.name"] == "hangar":
                    # hangar's pods: the gateway port only, never the API/dashboard/MCP port
                    assert [x.get("port") for x in rule.get("ports") or []] == [8788], f"{p['_file']}: {rule}"


def test_the_baseline_still_lets_sandboxes_reach_the_internet_and_dns():
    base = next(p for p in sandbox_policies() if p["metadata"]["name"] == "sandbox-egress-baseline")
    blocks = [peer["ipBlock"] for r in base["spec"]["egress"] for peer in r["to"] if "ipBlock" in peer]
    assert any(covers(b, "8.8.8.8") for b in blocks) and any(covers(b, "2001:4860:4860::8888") for b in blocks)
    assert any(peer.get("podSelector", {}).get("matchLabels") == {"k8s-app": "kube-dns"}
               for r in base["spec"]["egress"] for peer in r["to"])
