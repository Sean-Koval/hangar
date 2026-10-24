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
