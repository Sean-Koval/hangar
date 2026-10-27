"""Security defaults the Kubernetes manifests ship with (KAN-165).

Kubernetes has no uid split (isolation.supported is Docker's only), so an `ask` or `deny-risky` turn's driver would
run as the agent's own user. The manifests set HANGAR_UID_SPLIT=required, so such a turn is refused instead of
running unbound, until the driver sidecar (KAN-83) gives it a user of its own."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("path", ["deploy/k8s/config.yaml", "deploy/kustomize/base/config.yaml"])
def test_kubernetes_refuses_turns_it_cant_bind(path):
    maps = [d for d in yaml.safe_load_all((ROOT / path).read_text()) if d and d.get("kind") == "ConfigMap"]
    data = next(m["data"] for m in maps if m["metadata"]["name"] == "hangar")
    assert data["HANGAR_UID_SPLIT"] == "required"


def test_kubernetes_has_no_uid_split_yet():
    """When this fails, the sidecar landed: the manifests may go back to `auto` (and this test with them)."""
    from hangar import isolation
    from hangar.providers.kubernetes import KubernetesProvider

    assert not isolation.supported(KubernetesProvider.__new__(KubernetesProvider))


# --- KAN-174 (L-9): GKE ships with allowlist egress; Docker on a laptop keeps `open`
@pytest.mark.parametrize("path", ["deploy/k8s/config.yaml", "deploy/kustomize/base/config.yaml"])
def test_kubernetes_ships_with_allowlist_egress(path):
    maps = [d for d in yaml.safe_load_all((ROOT / path).read_text()) if d and d.get("kind") == "ConfigMap"]
    data = next(m["data"] for m in maps if m["metadata"]["name"] == "hangar")
    assert data["HANGAR_EGRESS_DEFAULT"] == "allowlist"
    assert data["HANGAR_EGRESS_URL"] == "http://hangar-gateway.hangar.svc:3128"  # the proxy it needs
    assert data["HANGAR_GATEWAY_URL"]  # and the gateway, for git and the package mirror


def test_nothing_in_the_gke_install_writes_open_egress():
    for path in [ROOT / "deploy/gke/up.sh", *(ROOT / "deploy/terraform").rglob("*.tf"),
                 *(ROOT / "deploy/kustomize").rglob("*.yaml")]:
        text = path.read_text()
        assert "HANGAR_EGRESS_DEFAULT: \"open\"" not in text and "HANGAR_EGRESS_DEFAULT=open" not in text, path


def test_the_built_in_default_stays_open_for_local_docker():
    from hangar.config import Settings

    assert Settings.model_fields["egress_default"].default == "open"
