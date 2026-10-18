"""Thawing harness locks (#134; docs/harness-lock.md#thaw-and-drift): resolving a lock from the registry, claiming
from it (the CLI version must match harnesses.lock.yaml), and turn 1 checked against it: no drift for the harness it
was frozen from, drift naming a skill changed in the image, HANGAR_HARNESS_DRIFT=fail stopping the turn.

On the fake provider with a scripted agent (tests/test_harness_lock.py's), real git and tmux."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from hangar import harness_lock, harness_thaw, manifest
from hangar.jobs import Caller, JobError, JobService, SubmitJob
from hangar.models import JobState
from hangar.sessions import NewSession, SessionError, SessionService
from hangar.vault import Vault
from tests import test_harness_lock as hl
from tests.test_harness_lock import DONE, GIT, _recorded
from tests.test_jobs import Hooks, _until

skillenv, world = hl.skillenv, hl.world  # its fixtures: the environment, the repo and the scripted agent
pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA = json.loads((ROOT / "docs/api/manifest.schema.json").read_text())


@pytest.fixture
async def jobs(fleet, world, settings, store):
    settings.session_poll_seconds = settings.job_poll_seconds = 0.1
    settings.harness_repo = str(world["root"] / "registry")
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    fake = fleet.providers["docker"]
    create = fake.create
    world["tamper"] = {}

    async def create_with_baked_skills(sandbox, image, spec=None):  # the image's skills, as a build bakes them
        await create(sandbox, image, spec)
        for name in ("alpha", "beta"):
            dst = fake.boxes[sandbox.provider_ref] / "home/agent/.tagent/skills" / name
            shutil.copytree(world["envs"] / "skills" / name, dst, dirs_exist_ok=True)
        for rel, text in world["tamper"].items():  # a skill file changed in the image after the freeze
            f = fake.boxes[sandbox.provider_ref] / "home/agent/.tagent/skills" / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)

    fake.create = create_with_baked_skills
    sessions = SessionService(fleet, Vault(store, b"0" * 43 + b"="))
    await sessions.start()
    svc = JobService(fleet, sessions, transport=httpx.MockTransport(Hooks()))
    await svc.start()
    yield svc
    await svc.stop()
    await sessions.stop()
    for box in fake.boxes.values():
        subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{box}/tmp", "PATH": "/usr/bin:/bin"},
                       capture_output=True)


def _commit(root: Path, msg: str) -> None:
    if not (root / ".git").exists():
        subprocess.run([*GIT, "init", "-q", str(root)], check=True)
    subprocess.run([*GIT, "-C", str(root), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(root), "commit", "-qm", msg], check=True)


async def _run(jobs, world, **kw):
    job = jobs.submit(SubmitJob(instructions="work", repo=world["url"], environment="lockenv", deliver="none",
                                keep_minutes=60, **kw), Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE, timeout=60)
    return job


async def _frozen_registry(jobs, world) -> tuple[Path, str]:
    """A registry holding `team` r1, frozen from a plain job's turn 1."""
    job = await _run(jobs, world)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    await _recorded(jobs, job, 1)
    b = harness_lock.freeze_job(jobs, job, 1, name="team")
    reg = Path(jobs.settings.harness_repo)
    harness_lock.write(reg / "harnesses" / "team", b["lock"], b["files"])
    (reg / "channels.yaml").write_text(yaml.safe_dump({"team": {"stable": b["digest"]}}))
    _commit(reg, "team r1")
    return reg, b["digest"]


async def test_thaw_mounts_the_lock_drifts_and_fails(jobs, world, settings, monkeypatch):
    reg, digest = await _frozen_registry(jobs, world)

    # --- thawed from its own lock: turn 1's harness_digest is the lock's, no drift
    job = await _run(jobs, world, harness_lock="team@stable")
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert job.harness_lock["digest"] == digest and job.harness_lock["revision"] == 1
    s = await _recorded(jobs, job, 1)
    for _ in range(100):
        if s.turns[0].drift is not None:
            break
        await asyncio.sleep(0.05)
    m = manifest.for_job(jobs, job, 1)
    jsonschema.validate(m, MANIFEST_SCHEMA)
    assert m["harness_digest"] == digest == s.turns[0].harness_digest
    assert m["harness"]["lock"] == {"name": "team", "revision": 1, "digest": digest}
    assert s.turns[0].drift == []
    assert harness_thaw.packet_line(jobs, job).endswith("turn 1 matched the lock")

    # --- a skill file changed in the image after the freeze, and an extra skill the image bakes: the sandbox runs
    # the lock's files (mounted from it, #221), so there is no drift; the mounted skills are read-only to the agent
    world["tamper"] = {"alpha/SKILL.md": "Use alpha, but differently.\n", "gamma/SKILL.md": "Use gamma.\n"}
    job = await _run(jobs, world, harness_lock="team@r1")
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    s = await _settled(jobs, job)
    assert s.turns[0].drift == [] and manifest.for_job(jobs, job, 1)["harness_digest"] == digest
    box = _box(jobs, job)
    skills = box / "home/agent/.tagent/skills"
    assert sorted(p.name for p in skills.iterdir()) == ["alpha", "beta"]  # exactly the lock's
    if os.geteuid() != 0:  # root (a dev container) writes through a read-only mode; the agent in a sandbox isn't
        assert (skills / "alpha/SKILL.md").read_bytes() == (reg / "harnesses/team/skills/alpha/SKILL.md").read_bytes()
    assert (skills / "beta/ref/notes.md").read_bytes() == (reg / "harnesses/team/skills/beta/ref/notes.md").read_bytes()
    assert (box / "home/agent/.tagent/AGENTS.md").read_text().startswith("Run the tests first.")
    for f in (skills / "alpha/SKILL.md", skills / "alpha", box / "home/agent/.tagent/AGENTS.md"):
        assert not f.stat().st_mode & 0o222, f  # the agent's edit of alpha (agent.sh) couldn't land
    assert job.harness_lock["stale"] == [] and jobs.fleet.get(job.sandbox_id).info["thawed"]["digest"] == digest

    # --- the registry's skill edited without re-freezing (a stale thaw): the lock's digest is what was asked, what
    # ran differs, and turn 1 reports it as drift (warn: it goes on)
    world["tamper"] = {}
    (reg / "harnesses/team/skills/alpha/SKILL.md").write_text("Use alpha, edited in the registry.\n")
    job = await _run(jobs, world, harness_lock="team")
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert any("skill:alpha" in x for x in job.harness_lock["stale"])
    s = await _settled(jobs, job)
    t = s.turns[0]
    assert [d["component"] for d in t.drift] == ["skill:alpha", "harness_digest"], t.drift
    assert t.drift[0]["lock"] and t.drift[0]["effective"] and t.drift[0]["lock"] != t.drift[0]["effective"]
    assert any("harness drift from team@r1: skill:alpha" in w for w in t.warnings)
    ev = [e for e in jobs.sessions.activity.read(s.id, 0, 1000, False)
          if e.data.get("event") == harness_thaw.DRIFT_EVENT]
    assert len(ev) == 1 and ev[0].data["action"] == "warn" and ev[0].data["drift"] == t.drift
    assert "**drift** on turn 1 (skill:alpha)" in harness_thaw.packet_line(jobs, job)
    assert manifest.for_job(jobs, job, 1)["harness_digest"] != digest  # what ran, not what was asked

    # --- HANGAR_HARNESS_DRIFT=fail: a stale registry is refused at claim; drift found on turn 1 (the sandbox changed
    # after the lock was mounted) stops the turn: failed, harness_drift; nothing delivered
    settings.harness_drift = "fail"
    with pytest.raises(JobError, match="the registry's files differ from the lock"):
        jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="lockenv", deliver="none",
                              harness_lock="team"), Caller("svc", "svc"))
    subprocess.run([*GIT, "-C", str(reg), "checkout", "--", "."], check=True)
    mount = harness_thaw.mount_files

    async def mount_then_change(fleet, prov, sb, harness):
        await mount(fleet, prov, sb, harness)
        await prov.exec(sb, "chmod u+w ~/.tagent/skills/alpha ~/.tagent/skills/alpha/SKILL.md && "
                            "echo changed >> ~/.tagent/skills/alpha/SKILL.md")

    monkeypatch.setattr(harness_thaw, "mount_files", mount_then_change)
    job = await _run(jobs, world, harness_lock="team")
    assert job.state == JobState.FAILED and job.reason == harness_thaw.DRIFT_REASON, [e.message for e in job.events]
    s = jobs._session(job)
    assert s.turns[0].outcome == "failed" and s.turns[0].outcome_reason == harness_thaw.DRIFT_REASON
    assert any("drifted from its harness lock (skill:alpha)" in e.message for e in job.events)


def _box(jobs, job) -> Path:
    fake = jobs.fleet.providers["docker"]
    return fake.boxes[jobs.fleet.get(job.sandbox_id).provider_ref]


async def _settled(jobs, job):
    s = await _recorded(jobs, job, 1)
    for _ in range(100):
        if s.turns[0].drift is not None:
            break
        await asyncio.sleep(0.05)
    return s


async def test_claims_are_refused_with_what_to_do(jobs, world, settings):
    reg, digest = await _frozen_registry(jobs, world)
    lock = harness_lock.load(reg / "harnesses/team")
    # a lock on another CLI version than harnesses.lock.yaml pins: refused at claim, saying how to fix it
    lock2 = {**lock, "revision": 2, "runtime": {**lock["runtime"], "version": "9.9.9"}}
    (reg / "harnesses/team/harness.lock").write_text(harness_lock.dump(lock2))
    _commit(reg, "team r2")
    with pytest.raises(JobError, match=r"pins tagent 9\.9\.9, but this hangar runs tagent \(baked into the image\)"
                                       r".*re-freeze the harness"):
        jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="lockenv", deliver="none",
                              harness_lock="team@r2"), Caller("svc", "svc"))
    # r1 is still claimable: it comes from the registry's history
    p = harness_thaw.pin(jobs.fleet, "team@r1", "lockenv")
    assert p["digest"] == digest and p["commit"]
    assert harness_thaw.pin(jobs.fleet, f"team@{digest}", "lockenv")["revision"] == 1
    # the model and approvals are the lock's
    lock3 = {**lock, "revision": 3, "model": {"id": "m-locked", "policy": None},
             "binding": {**lock["binding"], "permissions": {"approvals": "auto"}}}
    (reg / "harnesses/team/harness.lock").write_text(harness_lock.dump(lock3))
    with pytest.raises(JobError, match="pins model m-locked"):
        jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="lockenv", deliver="none",
                              harness_lock="team@r3", model="some-other-model"), Caller("svc", "svc"))
    with pytest.raises(SessionError, match="pins approvals auto"):
        jobs.sessions.new(NewSession(name="s-appr", environment="lockenv", harness_lock="team@r3",
                                     approvals="ask"), owner="svc", requester="svc")
    for ref, msg in (("team@r7", r"not in the registry; team is at r3"), ("nope", "no harness 'nope'"),
                     ("team@canary", "no 'canary' digest"), ("Team!", "use name, name@rN")):
        with pytest.raises(harness_thaw.ThawError, match=msg):
            harness_thaw.resolve(settings, ref)
    # a lock binding an MCP server the environment doesn't define, or enabling a plugin (#136)
    bad = {**lock, "binding": {**lock["binding"], "mcp": [*lock["binding"]["mcp"],
                                                         {"name": "elsewhere", "transport": "http"}]}}
    (reg / "harnesses/team/harness.lock").write_text(harness_lock.dump(bad))
    with pytest.raises(harness_thaw.ThawError, match="MCP server.*elsewhere"):
        harness_thaw.pin(jobs.fleet, "team", "lockenv")
    settings.harness_repo = None
    with pytest.raises(harness_thaw.ThawError, match="HANGAR_HARNESS_REPO"):
        harness_thaw.resolve(settings, "team")


def test_history_resolves_when_the_registry_is_a_subdirectory_of_a_repo(settings, tmp_path):
    """HANGAR_HARNESS_REPO inside a repo (#228; agent-factory's examples/team-harness was one before it moved to
    Sean-Koval/team-harness): `git log -- <path>` reads the path from the registry, but `git show <commit>:<path>`
    reads it from the repo's root, so every older revision was missed and `team@stable` failed whenever stable
    wasn't the current lock."""
    repo = tmp_path / "suite"
    reg = repo / "examples" / "team-harness"

    def freeze(rev: int, text: str) -> dict:
        lock = hl._lock(name="team", revision=rev, components=[
            {"id": "skill:review", "path": "skills/review", "digest": harness_lock.files_digest({"SKILL.md":
                                                                                                text.encode()})}])
        harness_lock.write(reg / "harnesses" / "team", lock, {"skills/review/SKILL.md": text.encode()})
        return lock

    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "index.md").write_text("the suite\n")  # the repo holds more than the registry
    digest = harness_lock.harness_digest(freeze(1, "Review it.\n"))
    (reg / "channels.yaml").write_text(yaml.safe_dump({"team": {"stable": digest}}))
    _commit(repo, "team r1")
    freeze(2, "Review it twice.\n")
    _commit(repo, "team r2")
    settings.harness_repo = str(reg)

    assert harness_thaw.resolve(settings, "team").lock["revision"] == 2  # the current lock: the working tree
    for ref in ("team@stable", "team@r1", f"team@{digest}"):
        r = harness_thaw.resolve(settings, ref)
        assert (r.lock["revision"], r.digest, r.path) == (1, digest, "harnesses/team/harness.lock"), ref
        assert r.commit
    d, stale = harness_thaw.materialize(settings, harness_thaw.resolve(settings, "team@stable"))
    assert stale == [] and (d / "skills/review/SKILL.md").read_text() == "Review it.\n"  # r1's files, from history


async def test_a_thawed_session_keeps_its_harness_and_the_cli_thaws_and_reports(jobs, world, settings, monkeypatch):
    reg, digest = await _frozen_registry(jobs, world)
    s = jobs.sessions.new(NewSession(name="thawed", environment="lockenv", harness_lock="team@r1"),
                          owner="svc", requester="svc")
    assert s.harness == "tagent" and s.harness_lock["digest"] == digest and s.model == s.harness_lock["model"]
    with pytest.raises(SessionError, match="runs harness lock team@r1"):
        jobs.sessions.open("thawed", requester="svc", harness="codex")
    # upstream drift is reported, not acted on
    r = harness_thaw.outdated(jobs.fleet, "team")
    assert not r["outdated"] and r["runtime"]["outdated"] is False
    assert {c["id"]: c["status"] for c in r["components"]} == {"skill:alpha": "up to date", "skill:beta": "up to date"}
    (world["envs"] / "skills/beta/SKILL.md").write_text("Use beta, v2.\n")
    r = harness_thaw.outdated(jobs.fleet, "team")
    assert r["outdated"] and {c["id"]: c["status"] for c in r["components"]}["skill:beta"] == "changed upstream"

    # REST and the CLI
    from fastapi.testclient import TestClient

    from hangar import cli
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.env_dirs = [world["envs"], *settings.env_dirs]
    with TestClient(create_app(settings)) as c:
        admin = {"Authorization": "Bearer t0ken"}
        got = c.get("/api/harness-locks/team@stable", headers=admin).json()
        assert got["harness_digest"] == digest and got["lock"]["name"] == "team"
        assert c.get("/api/harness-locks/nope", headers=admin).status_code == 404
        assert c.get("/api/harness-locks/team", headers={}).status_code == 401
        import contextlib

        @contextlib.contextmanager
        def client():
            yield TestClient(c.app, headers=admin)

        monkeypatch.setattr(cli, "_client", client)
        runner = CliRunner()
        out = runner.invoke(cli.app, ["harness", "outdated", "team"])
        assert out.exit_code == 1 and "skill:beta: changed upstream" in out.output
        out = runner.invoke(cli.app, ["harness", "thaw", "team@r1", "--env", "lockenv", "--session", "cli-thaw"])
        assert out.exit_code == 0, out.output
        assert "cli-thaw" in out.output and f"team@r1 ({digest})" in out.output
