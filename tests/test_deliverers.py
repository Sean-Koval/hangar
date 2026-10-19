"""Deliverers (ext/deliverers.py, hangar.deliverers): the built-in `command` and `image-ready`, their templates, and
registering a plugin's own kind."""

from __future__ import annotations

import pytest

from hangar.ext import deliverers as ext
from hangar.models import ChangeSet, ExecResult


def url(role: str) -> str:
    return f"https://github.com/acme/{role}.git"


def _ctx(**kw) -> ext.DeliverCtx:
    cs = ChangeSet(profile="p", profile_digest="sha256:x", parent_job="job-1", primary="code", ticket="jira:PAY-1")
    merged = {"code": ext.Merged(repo=url("code"), pr=13, merge_sha="ab" * 20)}
    return ext.DeliverCtx(changeset=cs, role="deploy", merged=merged, environment="dev", **kw)


async def test_command_deliverer_runs_its_line_with_quoted_values():
    ran: list[str] = []

    async def run(cmd: str) -> ExecResult:
        ran.append(cmd)
        return ExecResult(exit_code=0 if "fail" not in cmd else 2, stdout="boom")

    d = ext.get("command")
    assert await d.ready(_ctx(spec={"kind": "command"})) is not None
    await d.apply(_ctx(spec={"kind": "command", "run": "./bump.sh {environment} {code.merge_sha} {ticket_key}"},
                       exec=run, checkout="/workspace/deploy"), ext.Ready())
    assert ran == [f"./bump.sh dev {'ab' * 20} PAY-1"]
    with pytest.raises(ext.DeliverError, match="exited 2: boom"):
        await d.apply(_ctx(spec={"kind": "command", "run": "fail {code.pr}"}, exec=run), ext.Ready())
    with pytest.raises(ext.DeliverError, match=r"\{infra.merge_sha\} has no value"):
        await d.apply(_ctx(spec={"kind": "command", "run": "x {infra.merge_sha}"}, exec=run), ext.Ready())
    with pytest.raises(ext.DeliverError, match="one command line"):
        await d.apply(_ctx(spec={"kind": "command", "run": "a\nb"}, exec=run), ext.Ready())
    c = _ctx(spec={})
    c.changeset.ticket = "jira:PAY-1; rm -rf /"
    assert c.render("x {ticket}", quote=True) == "x 'jira:PAY-1; rm -rf /'"


class FakeRegistry:
    tags: dict[str, str] = {}
    asked: list[str] = []

    async def digest(self, ref: str) -> str | None:
        FakeRegistry.asked.append(ref)
        if ref == "registry.example.com/down:x":
            from hangar.registry import RegistryError

            raise RegistryError("HTTP 503")
        return FakeRegistry.tags.get(ref)

    async def close(self) -> None:
        pass


async def test_image_ready_waits_for_the_tag():
    from hangar.deliverers.image_ready import ImageReady

    FakeRegistry.tags, FakeRegistry.asked = {}, []
    d = ImageReady(registry=FakeRegistry)
    spec = {"kind": "image-ready", "image": "registry.example.com/payments-bot", "tag": "{code.merge_sha}"}
    assert await d.ready(_ctx(spec=spec)) is None
    FakeRegistry.tags[f"registry.example.com/payments-bot:{'ab' * 20}"] = "sha256:" + "d" * 64
    r = await d.ready(_ctx(spec=spec))
    assert r.detail == {"image": f"registry.example.com/payments-bot:{'ab' * 20}", "digest": "sha256:" + "d" * 64}
    notes: list[str] = []
    assert await d.ready(_ctx(spec={"image": "registry.example.com/down", "tag": "x"}, note=notes.append)) is None
    assert "couldn't say" in notes[0]
    with pytest.raises(ext.DeliverError, match="without a tag"):
        await d.ready(_ctx(spec={"image": "registry.example.com/app:v1"}))
    with pytest.raises(ext.DeliverError, match="ready check"):
        await d.apply(_ctx(spec={}), r)


def test_add_deliverer_checks_the_plugin():
    class Bump:
        kind = "acme-argocd-bump"

        async def ready(self, ctx):
            return ext.Ready()

        async def apply(self, ctx, ready):
            return None

    b = Bump()
    try:
        ext.add_deliverer(b)
        ext.add_deliverer(b)  # the same object again: fine
        assert ext.get("acme-argocd-bump") is b
        with pytest.raises(ValueError, match="already registered"):
            ext.add_deliverer(Bump())
        with pytest.raises(ValueError, match="kind must match"):
            bad = Bump()
            bad.kind = "Bad Kind"
            ext.add_deliverer(bad)

        class NoApply:
            kind = "half"

            async def ready(self, ctx):
                return None

        with pytest.raises(TypeError, match="needs async ready"):
            ext.add_deliverer(NoApply())
    finally:
        ext.DELIVERERS.pop("acme-argocd-bump", None)


def test_the_built_ins_are_plugins_and_a_hosts_deliverer_reaches_change_sets():
    from types import SimpleNamespace

    from hangar import pluginhost
    from hangar.deliverers import command, image_ready

    got: list = []
    for mod in (command, image_ready):
        mod.PLUGIN.register(SimpleNamespace(add_deliverer=got.append), mod.PLUGIN.Settings())
    assert got == [command.INSTANCE, image_ready.INSTANCE]
    assert ext.get("command") is command.INSTANCE and ext.get("image-ready") is image_ready.INSTANCE

    class Bump:
        kind = "acme-bump-argocd"

        async def ready(self, ctx):
            return ext.Ready()

        async def apply(self, ctx, ready):
            return None

    host = SimpleNamespace(deliverers={}, _added=lambda what: None, _who=lambda: "acme-bump")
    b = Bump()
    try:
        pluginhost.Host.add_deliverer(host, b)  # what a plugin's register calls
        assert host.deliverers["acme-bump-argocd"] is b and ext.get("acme-bump-argocd") is b
    finally:
        ext.DELIVERERS.pop("acme-bump-argocd", None)
