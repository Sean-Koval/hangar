"""The NetworkPolicies in deploy/k8s, read together (workstream isolation): whatever a later manifest adds, sandboxes
must not reach each other, the metadata servers or hangar's API. NetworkPolicies only add allowances, so a rule in
any file can open what the baseline closes; this reads every file, the optional ones too."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import yaml

K8S = Path(__file__).parents[1] / "deploy" / "k8s"
SANDBOXES = "hangar-sandboxes"
# Ranges no sandbox may reach, checked as whole ranges and not sample addresses (an allowance for 10.8.0.0/14, a
# typical pod range, holds no sample): the GCE metadata and Workload Identity servers (link-local); every range a VPC
# subnet, and so GKE's nodes, pods and Services, may use besides privately used public IPs (cloud.google.com/vpc/docs/
# subnets, "valid ranges": RFC 1918, RFC 6598 CGNAT, RFC 6890 192.0.0.0/24, RFC 5737 documentation, RFC 7526 6to4
# relay, RFC 2544 benchmark, class E); loopback; IPv6 ULA (the metadata server's fd20:ce::254), link-local, loopback.
FORBIDDEN = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4", "127.0.0.0/8",
    "169.254.0.0/16", "fc00::/7", "fe80::/10", "::1/128",
    # IPv6 ranges that carry an IPv4 address (KAN-174 Q-2): NAT64, IPv4-mapped and -translated, 6to4, compatible
    "64:ff9b::/96", "64:ff9b:1::/48", "::ffff:0:0/96", "::ffff:0:0:0/96", "2002::/16", "::/96")]
NODELOCAL_DNS = ipaddress.ip_network("169.254.20.10/32")
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


def reachable(block: dict) -> list:
    """What an ipBlock allows: its cidr minus its exceptions."""
    nets = [ipaddress.ip_network(block["cidr"])]
    for x in block.get("except") or []:
        ex, out = ipaddress.ip_network(x), []
        for n in nets:
            if n.version != ex.version or not n.overlaps(ex):
                out.append(n)
            elif not n.subnet_of(ex):
                out.extend(n.address_exclude(ex))
        nets = out
    return nets


def forbidden_in(block: dict) -> list:
    """The parts of an ipBlock's allowance that fall in a forbidden range."""
    return [n for n in reachable(block) for f in FORBIDDEN if n.version == f.version and n.overlaps(f)]


def egress_isolated(spec: dict) -> bool:
    """policyTypes defaults to Ingress, plus Egress when the policy has egress rules (the NetworkPolicy API)."""
    types = spec.get("policyTypes") or (["Ingress"] + (["Egress"] if spec.get("egress") else []))
    return "Egress" in types


def test_the_checker_reads_ranges_not_sample_addresses():
    assert forbidden_in({"cidr": "10.8.0.0/14"})  # no sample address in it, still a pod range
    assert forbidden_in({"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8"]})
    assert not forbidden_in({"cidr": "8.8.8.0/24"})
    assert [str(n) for n in forbidden_in({"cidr": "169.254.20.10/32"})] == ["169.254.20.10/32"]


def test_every_manifest_parses_and_the_sandbox_namespace_has_policies():
    names = {p["metadata"]["name"] for p in sandbox_policies()}
    assert {"sandbox-egress-baseline", "sandbox-deny-ingress", "allow-hangar-envd", "allow-hangar-gateway"} <= names


def test_every_policy_names_its_namespace():
    """One without a namespace lands wherever `kubectl apply` points, and isn't held to the checks below."""
    for p in policies():
        assert p["metadata"].get("namespace"), f"{p['_file']}: {p['metadata']['name']} has no namespace"


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


def test_every_sandbox_is_egress_isolated():
    """Without a policy selecting every sandbox pod for Egress, a pod that no egress policy selects may reach
    anything, and the allowances below would mean nothing."""
    assert any(p["spec"]["podSelector"] == {} and egress_isolated(p["spec"]) for p in sandbox_policies())


def test_sandbox_egress_reaches_no_pod_metadata_or_private_address():
    for p in sandbox_policies():
        for rule in p["spec"].get("egress") or []:
            assert rule.get("to"), f"{p['_file']}: an egress rule without `to` allows everything"
            for peer in rule["to"]:
                if "ipBlock" in peer:
                    for n in forbidden_in(peer["ipBlock"]):
                        # NodeLocal DNSCache: one link-local address, DNS only
                        dns = all(x.get("port") == 53 and "endPort" not in x for x in rule.get("ports") or [{}])
                        assert n.subnet_of(NODELOCAL_DNS) and dns, \
                            f"{p['_file']}: egress into a forbidden range ({n}) through {peer['ipBlock']}"
                    continue
                # A podSelector without a namespaceSelector means this namespace: the other sandboxes.
                ns = (peer.get("namespaceSelector") or {}).get("matchLabels") or {}
                assert "namespaceSelector" in peer and ns, f"{p['_file']}: egress to {peer} can reach sandboxes"
                assert ns.get("kubernetes.io/metadata.name") not in (None, SANDBOXES), f"{p['_file']}: {peer}"
                if ns["kubernetes.io/metadata.name"] == "hangar":
                    # hangar's pods: the gateway port, or (--- egress) the egress proxy's; never the API/dashboard/MCP
                    ports = [(x.get("port"), x.get("endPort")) for x in rule.get("ports") or []]
                    assert ports in ([(8788, None)], [(3128, None)]), f"{p['_file']}: {rule}"


def test_the_baseline_still_lets_sandboxes_reach_the_internet_and_dns():
    base = next(p for p in sandbox_policies() if p["metadata"]["name"] == "sandbox-egress-baseline")
    blocks = [peer["ipBlock"] for r in base["spec"]["egress"] for peer in r["to"] if "ipBlock" in peer]
    for addr in ("8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"):
        ip = ipaddress.ip_address(addr)
        assert any(ip in n for b in blocks for n in reachable(b)), addr
    assert any(peer.get("podSelector", {}).get("matchLabels") == {"k8s-app": "kube-dns"}
               for r in base["spec"]["egress"] for peer in r["to"])
