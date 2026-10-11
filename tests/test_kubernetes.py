"""Kubernetes provider: manifest generation and claim flow against a fake CustomObjectsApi."""

import pytest

pytest.importorskip("kubernetes")

from hangar.config import Settings  # noqa: E402
from hangar.envspec import EnvSpec, render  # noqa: E402
from hangar.models import Resources, Sandbox  # noqa: E402
from hangar.providers.kubernetes import EXT_GROUP, KubernetesProvider  # noqa: E402


class FakeCRD:
    def __init__(self):
        self.objects: dict[tuple[str, str], dict] = {}

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        from kubernetes.client.rest import ApiException

        if (plural, name) not in self.objects:
            raise ApiException(status=404)
        return self.objects[(plural, name)]

    def create_namespaced_custom_object(self, group, version, ns, plural, body):
        body.setdefault("metadata", {})["resourceVersion"] = "1"
        self.objects[(plural, body["metadata"]["name"])] = body

    def replace_namespaced_custom_object(self, group, version, ns, plural, name, body):
        self.objects[(plural, name)] = body


def provider(**kw) -> KubernetesProvider:
    p = object.__new__(KubernetesProvider)
    p.settings = Settings(**kw)
    p.ns = p.settings.k8s_namespace
    p.crd = FakeCRD()
    p._known_images = set()
    p._synced = {}
    return p


def spec(**kw):
    return EnvSpec(name="py", resources=Resources(cpus=2, memory_gb=4), warm_pool=3, **kw)


def test_gvisor_pod_template():
    pod = provider()._pod_template(spec(), "reg/py:abc")["spec"]
    assert pod["runtimeClassName"] == "gvisor"
    assert pod["nodeSelector"] == {"sandbox.gke.io/runtime": "gvisor"}
    assert pod["automountServiceAccountToken"] is False
    c = pod["containers"][0]
    assert c["image"] == "reg/py:abc" and c["resources"]["limits"]["memory"] == "4096Mi"
    assert pod["securityContext"]["runAsUser"] == 1000


def test_kata_and_runc_templates():
    kata = provider(k8s_runtime_class="kata-qemu")._pod_template(spec(), "i")["spec"]
    assert kata["runtimeClassName"] == "kata-qemu" and kata["nodeSelector"] == {"hangar.dev/runtime": "kata"}
    runc = provider(k8s_runtime_class=None)._pod_template(spec(), "i")["spec"]
    assert "runtimeClassName" not in runc and "nodeSelector" not in runc


def test_environment_overrides_runtime_class():
    p = provider(k8s_pod_snapshots=True)  # cluster default gvisor, snapshots on
    kata = p._pod_template(spec(runtime_class="kata-qemu-runtime-rs"), "i")
    assert kata["spec"]["runtimeClassName"] == "kata-qemu-runtime-rs"
    assert kata["spec"]["tolerations"][0]["value"] == "kata"
    assert "serviceAccountName" not in kata["spec"], "Kata pods can't be snapshotted"
    runc = p._pod_template(spec(runtime_class=""), "i")["spec"]
    assert "runtimeClassName" not in runc
    assert p._pod_template(spec(), "i")["spec"]["serviceAccountName"] == "hangar-sandbox"


async def test_in_cluster_without_docker_daemon_uses_cloud_build(monkeypatch):
    import hangar.providers._build as b
    import hangar.providers.kubernetes as k

    calls = []
    monkeypatch.setattr(b, "_DAEMON", None)
    monkeypatch.setattr(b.shutil, "which", lambda name: f"/usr/bin/{name}")  # CLI present...

    async def no_daemon(*a, **kw):  # ...but `docker version` can't reach a server
        class P:
            async def wait(self):
                return 1
        return P()

    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", no_daemon)

    async def cloud_build(self, ctx, tag, log):
        calls.append(tag)

    monkeypatch.setattr(k.KubernetesProvider, "_cloud_build", cloud_build)
    monkeypatch.setattr(k.shutil, "which", lambda name: f"/usr/bin/{name}")
    p = provider(registry="r.example/x")
    ctx = render(spec())
    assert await p.build_template(spec(), ctx, "r.example/x/py:1") == "r.example/x/py:1"
    assert calls == ["r.example/x/py:1"]


async def test_warm_pool_and_claim():
    p = provider()
    sb = Sandbox(name="a", environment="py", provider="kubernetes", expires_at=2_000_000_000)
    await p.create(sb, "reg/py:abc", spec())
    tmpl = p.crd.objects[("sandboxtemplates", "hangar-py")]
    pool = p.crd.objects[("sandboxwarmpools", "hangar-py")]
    claim = p.crd.objects[("sandboxclaims", f"hangar-{sb.id}")]
    assert tmpl["apiVersion"] == f"{EXT_GROUP}/v1beta1"
    assert pool["spec"] == {"replicas": 3, "sandboxTemplateRef": {"name": "hangar-py"},
                            "updateStrategy": {"type": "Recreate"}}
    assert claim["spec"]["warmPoolRef"] == {"name": "hangar-py"}
    assert claim["spec"]["lifecycle"]["shutdownPolicy"] == "Delete"
    assert claim["spec"]["lifecycle"]["shutdownTime"].endswith("Z")
    assert sb.provider_ref == f"hangar-{sb.id}"
    # re-sync with a new image updates the template in place
    await p.sync_warm_pool(spec(), "reg/py:def")
    tmpl = p.crd.objects[("sandboxtemplates", "hangar-py")]
    assert tmpl["spec"]["podTemplate"]["spec"]["containers"][0]["image"] == "reg/py:def"


async def test_write_file_uses_exec_in_chunks(monkeypatch):
    p = provider()
    calls = []

    def fake_exec(pod, argv, timeout):
        calls.append(argv[-1])
        return 0, "", ""

    p._exec_blocking = fake_exec
    sb = Sandbox(name="a", environment="py", provider="kubernetes", info={"pod": "pod-1"})
    await p.write_file(sb, "/home/agent/.codex/auth.json", b"x" * 200_000, mode=0o600)
    assert calls[0].startswith(": > /tmp/.hangar-upload-")
    assert sum(c.startswith("printf %s") for c in calls) >= 2  # base64 split into chunks
    assert "base64 -d" in calls[-1] and "chmod 600" in calls[-1]


class SnapCRD(FakeCRD):
    """Completes any PodSnapshotManualTrigger as soon as it is read back."""

    def __init__(self):
        super().__init__()
        self.deleted = []

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        obj = super().get_namespaced_custom_object(group, version, ns, plural, name)
        if plural == "podsnapshotmanualtriggers":
            obj["status"] = {"conditions": [{"type": "Triggered", "status": "True", "reason": "Complete"}],
                             "snapshotCreated": {"name": "ps-abc123"}}
        return obj

    def delete_namespaced_custom_object(self, group, version, ns, plural, name, **kw):
        self.deleted.append((plural, name))


class FakeCore:
    """Pod API: the pod disappears on suspend and comes back (restored or fresh) on resume."""

    def __init__(self, crd, restored=True):
        from types import SimpleNamespace as NS

        self.NS, self.crd, self.restored, self.gen = NS, crd, restored, 1

    def read_namespaced_pod(self, name, ns):
        from kubernetes.client.rest import ApiException

        sb = self.crd.objects.get(("sandboxes", "sbx-1"), {})
        if sb.get("spec", {}).get("operatingMode") == "Suspended":
            raise ApiException(status=404)
        NS = self.NS
        conds = [NS(type="Ready", status="True", message="")]
        if self.gen > 1 and self.restored:
            conds.append(NS(type="PodRestored", status="True", message="restored from snapshot ps-abc123"))
        return NS(metadata=NS(uid=f"uid-{self.gen}"), status=NS(phase="Running", conditions=conds))


class SnapCRD2(SnapCRD):
    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        obj = self.objects.setdefault((plural, name), {"metadata": {"name": name}})
        obj.setdefault("spec", {}).update(body["spec"])
        if body["spec"].get("operatingMode") == "Running":
            self.core.gen += 1


async def _paused_provider(restored=True):
    p = provider(k8s_pod_snapshots=True, k8s_snapshot_timeout_seconds=5)
    p.crd = SnapCRD2()
    p.core = FakeCore(p.crd, restored)
    p.crd.core = p.core
    sb = Sandbox(name="a", environment="py", provider="kubernetes", provider_ref="hangar-x",
                 info={"pod": "sbx-1", "k8s_sandbox": "sbx-1"})
    await p.pause(sb)
    return p, sb


async def test_pause_snapshots_then_suspends_and_resume_verifies_restore():
    p, sb = await _paused_provider()
    assert p.capabilities.pause and not p.capabilities.full_snapshots
    trig = next(v for (pl, _), v in p.crd.objects.items() if pl == "podsnapshotmanualtriggers")
    assert trig["spec"] == {"targetPod": "sbx-1"}
    assert p.crd.objects[("sandboxes", "sbx-1")]["spec"]["operatingMode"] == "Suspended"
    assert sb.info["paused_snapshot"] == "ps-abc123" and "pod" not in sb.info
    await p.resume(sb)
    assert p.crd.objects[("sandboxes", "sbx-1")]["spec"]["operatingMode"] == "Running"
    assert "paused_snapshot" not in sb.info


async def test_resume_as_fresh_pod_is_an_error():
    from hangar.providers.base import ProviderError

    p, sb = await _paused_provider(restored=False)
    with pytest.raises(ProviderError, match="fresh pod"):
        await p.resume(sb)


def test_pod_snapshots_require_gvisor():
    from hangar.providers.base import ProviderError

    with pytest.raises(ProviderError, match="gvisor"):
        KubernetesProvider(Settings(k8s_pod_snapshots=True, k8s_runtime_class="kata-qemu"))


def test_snapshot_failure_is_reported():
    from hangar.providers.base import ProviderError
    from hangar.providers.kubernetes import _snapshot_result

    assert _snapshot_result({"status": {}}) is None
    with pytest.raises(ProviderError, match="disk full"):
        _snapshot_result({"status": {"conditions": [{"type": "Triggered", "status": "False", "reason": "Failed",
                                                     "message": "disk full"}]}})


def test_snapshots_off_by_default():
    assert not provider().capabilities.pause
    assert "serviceAccountName" not in provider()._pod_template(spec(), "i")["spec"]


def test_e2b_registry_auth_selection(monkeypatch):
    pytest.importorskip("e2b")
    from hangar.providers.base import ProviderError
    from hangar.providers.e2b import E2BProvider

    p = object.__new__(E2BProvider)
    p.settings = Settings(e2b_api_key="k")
    with pytest.raises(ProviderError, match="private registry"):
        p._base("us-central1-docker.pkg.dev/proj/hangar/py:abc")
    p._base("ghcr.io/me/py:abc")  # public registries need no auth
    p.settings = Settings(e2b_api_key="k", e2b_registry_username="u", e2b_registry_password="pw")
    assert p._base("registry.example.com/py:abc")._template._registry_config["type"] == "registry"
