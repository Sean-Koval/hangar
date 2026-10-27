"""The portable deploy package (--- company-deploy, deploy/kustomize, docs/deploy-company.md): a base and components
for a cluster someone else runs. Nothing in it names the owner's project or registry, every component builds onto
the base with the values an overlay gives it (and refuses to build without them), and the files it shares with the
owner's deploy/k8s stay identical, so tests/test_k8s_network.py's checks hold for both."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
KUST = ROOT / "deploy" / "kustomize"
K8S = ROOT / "deploy" / "k8s"
SHARED = ["namespace.yaml", "gateway.yaml", "control-plane-network.yaml", "sandbox-network.yaml", "envd-network.yaml",
          "heavy-policy.yaml"]
COMPONENTS = {  # component -> the hangar-deploy keys it reads
    "gke-workload-identity": {"gcp_service_account": "hangar@example-project.iam.gserviceaccount.com"},
    "ingress-gce": {"domain": "hangar.example.com", "static_ip_name": "hangar-ip"},
    "ingress-nginx": {"domain": "hangar.example.com"},
    "external-secrets": {"secret_store": "example-store"},
    "outbox": {"outbox_url": "gs://example-project-hangar-runs"},
}
PERSONAL = ["PROJECT_ID", "REGION-docker", "docker.pkg.dev", "sslip.io", "seankoval"]


def test_shared_manifests_match_the_owners_deploy():
    for f in SHARED:
        assert (KUST / "base" / f).read_text() == (K8S / f).read_text(), f"{f} drifted from deploy/k8s/{f}"


def test_every_component_is_in_the_tree():
    assert sorted(p.name for p in (KUST / "components").iterdir()) == sorted(COMPONENTS)


def test_nothing_names_the_owners_install():
    for f in KUST.rglob("*.yaml"):
        text = f.read_text()
        for word in PERSONAL:
            assert word not in text, f"{f.relative_to(ROOT)} names {word!r}"
        for line in text.splitlines():
            if "sean-koval" in line.lower():  # only the released image and the repository itself
                assert "ghcr.io/sean-koval/hangar" in line or "github.com/Sean-Koval/hangar" in line, line


def test_files_parse_and_components_are_components():
    for f in KUST.rglob("*.yaml"):
        list(yaml.safe_load_all(f.read_text()))
    for c in COMPONENTS:
        assert yaml.safe_load((KUST / "components" / c / "kustomization.yaml").read_text())["kind"] == "Component"


def _kustomize() -> list[str] | None:
    if k := shutil.which("kustomize"):
        return [k, "build"]
    if k := shutil.which("kubectl"):
        return [k, "kustomize"]
    return None


KUSTOMIZE = _kustomize()
needs_kustomize = pytest.mark.skipif(KUSTOMIZE is None, reason="neither kustomize nor kubectl is installed")


def build(path: Path) -> subprocess.CompletedProcess:
    assert KUSTOMIZE is not None
    return subprocess.run([*KUSTOMIZE, str(path)], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "KUBECONFIG": os.devnull})


def docs(out: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(out) if d]


def overlay(tmp_path: Path, components: list[str], params: dict[str, str] | None) -> Path:
    d = tmp_path / "overlay"
    d.mkdir(exist_ok=True)
    k: dict = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
               "resources": [os.path.relpath(KUST / "base", d)],
               "components": [os.path.relpath(KUST / "components" / c, d) for c in components]}
    if params is not None:
        k["configMapGenerator"] = [{
            "name": "hangar-deploy", "namespace": "hangar", "literals": [f"{a}={b}" for a, b in params.items()],
            "options": {"disableNameSuffixHash": True,
                        "annotations": {"config.kubernetes.io/local-config": "true"}}}]
    (d / "kustomization.yaml").write_text(yaml.safe_dump(k))
    return d


@needs_kustomize
def test_the_base_builds_alone():
    r = build(KUST / "base")
    assert r.returncode == 0, r.stderr
    kinds = {(d["kind"], d["metadata"]["name"]) for d in docs(r.stdout)}
    assert {("Deployment", "hangar"), ("Service", "hangar-gateway"), ("NetworkPolicy", "sandbox-egress-baseline"),
            ("ConfigMap", "hangar")} <= kinds
    dep = next(d for d in docs(r.stdout) if d["kind"] == "Deployment")
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == "ghcr.io/sean-koval/hangar"


@needs_kustomize
@pytest.mark.parametrize("component", sorted(COMPONENTS))
def test_each_component_builds_with_its_values_and_not_without(tmp_path, component):
    r = build(overlay(tmp_path, [component], COMPONENTS[component]))
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "set-by-hangar-deploy" not in out and "hangar-deploy" not in out  # filled in, and never applied
    for v in COMPONENTS[component].values():
        assert v in out
    r = build(overlay(tmp_path, [component], None))
    assert r.returncode != 0 and "hangar-deploy" in r.stderr


@needs_kustomize
def test_the_example_overlay_is_complete_and_pinned():
    r = build(KUST / "example")
    assert r.returncode == 0, r.stderr
    for word in [*PERSONAL, "set-by-hangar-deploy"]:
        assert word not in r.stdout
    ds = docs(r.stdout)
    dep = next(d for d in ds if d["kind"] == "Deployment")
    assert "@sha256:" in dep["spec"]["template"]["spec"]["containers"][0]["image"]
    assert not [d for d in ds if d["metadata"]["name"] == "hangar-deploy"]
    site = next(d for d in ds if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "hangar-site")
    assert site["data"]["HANGAR_PUBLIC_URL"] == "https://hangar.example.com"


@needs_kustomize
def test_the_owners_deploy_still_builds():
    r = build(K8S)
    assert r.returncode == 0, r.stderr
    assert "REGION-docker.pkg.dev/PROJECT_ID/hangar/control-plane:latest" in r.stdout  # what up.sh seds
