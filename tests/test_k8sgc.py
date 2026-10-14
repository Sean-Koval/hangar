"""The SandboxTemplates made per repo build and per set of services are deleted once no live sandbox uses them
(k8sgc.py; the #99 review's Low findings), and trimming a build's history leaves its template to that sweep."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

pytest.importorskip("kubernetes")

from hangar import envbuilds, k8sgc  # noqa: E402
from hangar.models import Sandbox, SandboxState  # noqa: E402
from tests.test_kubernetes import FakeCRD, provider  # noqa: E402

OLD = datetime.fromtimestamp(time.time() - 3600, UTC).isoformat().replace("+00:00", "Z")
NEW = datetime.fromtimestamp(time.time() - 30, UTC).isoformat().replace("+00:00", "Z")


class ListingCRD(FakeCRD):
    def __init__(self):
        super().__init__()
        self.deleted: list[tuple[str, str]] = []

    def list_namespaced_custom_object(self, group, version, ns, plural, label_selector=None):
        assert label_selector == "app.kubernetes.io/managed-by=hangar"
        return {"items": [v for (p, _), v in self.objects.items() if p == plural]}

    def delete_namespaced_custom_object(self, group, version, ns, plural, name):
        from kubernetes.client.rest import ApiException

        if (plural, name) not in self.objects:
            raise ApiException(status=404)
        del self.objects[(plural, name)]
        self.deleted.append((plural, name))


def _template(p, name: str, created: str = OLD) -> None:
    for plural in ("sandboxtemplates", "sandboxwarmpools"):
        p.crd.objects[(plural, name)] = {"metadata": {"name": name, "creationTimestamp": created,
                                                      "resourceVersion": "1",
                                                      "labels": {"app.kubernetes.io/managed-by": "hangar"}}}
    p._synced[name.removeprefix("hangar-")] = ("synced",)


@pytest.fixture
def k8s(fleet):
    p = provider()
    p.crd = ListingCRD()
    fleet.providers["kubernetes"] = p
    k8sgc._last.clear()
    yield p
    fleet.providers.pop("kubernetes", None)


def _live(fleet, **kw) -> Sandbox:
    sb = Sandbox(name="s", environment="plain", provider="kubernetes", state=SandboxState.RUNNING, **kw)
    fleet.store.put(sb)
    return sb


async def test_templates_no_live_sandbox_uses_are_deleted_with_their_warm_pools(fleet, k8s):
    from hangar.envservices import digest, specs

    _template(k8s, "hangar-plain")  # the environment's own: never
    _template(k8s, "hangar-plain-s0a1b2c3d")  # services nobody runs any more: gone
    _template(k8s, "hangar-plain-0a1b2c3d")  # a trimmed build nobody runs: gone
    _template(k8s, "hangar-plain-1a1b2c3d")  # a build a live session still runs on (recorded at its create)
    _template(k8s, "hangar-plain-2a1b2c3d", created=NEW)  # just applied: its claim may be on its way
    _template(k8s, "hangar-other-thing")  # not a name hangar derives
    _live(fleet, info={"k8s_template": "hangar-plain-1a1b2c3d"})
    # a sandbox from before sandboxes recorded their template: worked out from its services, as its create did
    older = _live(fleet, info={"service_specs": {"db": {"image": "postgres:17", "port": 5432}}})
    derived = f"hangar-plain-s{digest(specs(older))}"
    assert k8sgc.template_of(fleet, older) == derived
    _template(k8s, derived)
    fleet.store.put(Sandbox(name="t", environment="plain", provider="kubernetes", state=SandboxState.STOPPED,
                            info={"k8s_template": "hangar-plain-s0a1b2c3d"}))  # stopped: holds nothing

    gone = await k8sgc.sweep(fleet, force=True)
    assert sorted(gone) == ["hangar-plain-0a1b2c3d", "hangar-plain-s0a1b2c3d"]
    for n in gone:  # each with its warm pool, the pool first (it refers to the template)
        assert k8s.crd.deleted.index(("sandboxwarmpools", n)) < k8s.crd.deleted.index(("sandboxtemplates", n))
    left = {n for (p, n) in k8s.crd.objects if p == "sandboxtemplates"}
    assert left == {"hangar-plain", "hangar-plain-1a1b2c3d", "hangar-plain-2a1b2c3d", "hangar-other-thing", derived}
    assert "plain-0a1b2c3d" not in k8s._synced and "plain-1a1b2c3d" in k8s._synced  # applied again when needed
    assert await k8sgc.sweep(fleet) == []  # at most every 10 minutes


async def test_an_environment_whose_live_sandbox_cant_be_told_keeps_its_templates(fleet, k8s):
    _template(k8s, "hangar-gone-env-0a1b2c3d")
    _live(fleet).environment = "gone-env"
    fleet.store.put(Sandbox(name="u", environment="gone-env", provider="kubernetes", state=SandboxState.RUNNING))
    assert await k8sgc.sweep(fleet, force=True) == []


async def test_a_sandbox_records_the_template_it_was_created_from(k8s):
    from tests.test_kubernetes import spec

    sb = Sandbox(name="a", environment="py", provider="kubernetes")
    await k8s.create(sb, "reg/py:abc", spec().model_copy(update={"name": "py-0a1b2c3d", "warm_pool": 0}))
    assert sb.info["k8s_template"] == "hangar-py-0a1b2c3d"


async def test_trimming_a_build_leaves_its_template_to_the_sweep(fleet, k8s):
    """#99 review: a build out of the history had its SandboxTemplate deleted at once, under any session still
    running from it (after a rollback or an unpin)."""
    _template(k8s, "hangar-plain-0a1b2c3d")
    b = envbuilds.Build(id="bld-0a1b2c3d", seq=1, repo="o/r", repo_url="https://x/o/r.git", environment="plain",
                        provider="kubernetes", image="reg/plain-repo:x-0a1b2c3d")
    await envbuilds.of(fleet)._forget(b)
    assert k8s.crd.deleted == [] and ("sandboxtemplates", "hangar-plain-0a1b2c3d") in k8s.crd.objects


# ====================================================================== review: a sweep racing a create
def _blocking(k8s, plural: str, method: str):
    """The fake CRD's `method` on `plural` waits until released; returns (entered, release)."""
    import threading

    entered, release = threading.Event(), threading.Event()
    real = getattr(k8s.crd, method)

    def wrapped(*a, **kw):
        if plural in a:
            entered.set()
            release.wait(5)
        return real(*a, **kw)

    setattr(k8s.crd, method, wrapped)
    return entered, release


async def _wait(ev) -> None:
    import asyncio

    for _ in range(250):
        if ev.is_set():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("never got there")


async def test_a_template_is_kept_while_a_create_claims_from_it(fleet, k8s):
    """--- review: a create whose template was already applied (in the provider's cache) claims from it without
    applying it again; a sweep that listed the live sandboxes just before deleted it under the claim."""
    import asyncio

    from tests.test_kubernetes import spec

    _template(k8s, "hangar-plain-0a1b2c3d")  # applied an hour ago, unused since

    async def up_to_date(*_a):  # the provider's cache: applied already, nothing to change
        pass

    k8s.sync_warm_pool = up_to_date
    entered, release = _blocking(k8s, "sandboxclaims", "create_namespaced_custom_object")
    sb = Sandbox(name="a", environment="plain", provider="kubernetes")
    s = spec().model_copy(update={"name": "plain-0a1b2c3d", "warm_pool": 0})
    create = asyncio.create_task(k8s.create(sb, "reg/plain:x", s))
    await _wait(entered)
    try:
        assert await k8sgc.sweep(fleet, force=True) == []
    finally:
        release.set()
    await create
    assert ("sandboxwarmpools", "hangar-plain-0a1b2c3d") in k8s.crd.objects


async def test_a_create_waits_for_a_delete_of_its_template_then_applies_it_again(fleet, k8s):
    """--- review: a create that came while the sweep was deleting its template claimed from it, and the sweep then
    deleted it under the claim."""
    import asyncio

    from tests.test_kubernetes import spec

    _template(k8s, "hangar-plain-0a1b2c3d")
    entered, release = _blocking(k8s, "sandboxwarmpools", "delete_namespaced_custom_object")
    sweep = asyncio.create_task(k8sgc.sweep(fleet, force=True))
    await _wait(entered)
    sb = Sandbox(name="a", environment="plain", provider="kubernetes")
    create = asyncio.create_task(k8s.create(sb, "reg/plain:x", spec().model_copy(
        update={"name": "plain-0a1b2c3d", "warm_pool": 0})))
    await asyncio.sleep(0.1)
    release.set()
    assert await sweep == ["hangar-plain-0a1b2c3d"]
    await create
    assert ("sandboxwarmpools", "hangar-plain-0a1b2c3d") in k8s.crd.objects
    assert ("sandboxtemplates", "hangar-plain-0a1b2c3d") in k8s.crd.objects


async def test_a_sandbox_saved_after_the_sweep_listed_them_keeps_its_template(fleet, k8s, monkeypatch):
    """--- review: the live sandboxes were read once, before the deletes, each of which waits on the cluster."""
    _template(k8s, "hangar-plain-0a1b2c3d")
    real, calls = fleet.store.list_active, []

    def list_active(cls):
        out = real(cls)
        if cls is Sandbox and not calls:  # a create saves its sandbox right after this
            calls.append(1)
            _live(fleet, info={"k8s_template": "hangar-plain-0a1b2c3d"})
        return out

    monkeypatch.setattr(fleet.store, "list_active", list_active)
    assert await k8sgc.sweep(fleet, force=True) == []
