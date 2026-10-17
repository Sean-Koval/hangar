"""The team harness repo (#138; harness_repo.py, docs/harness-lock.md#the-team-harness-repo): HANGAR_HARNESS_REPO as a
git URL with a `#ref`, kept as a checkout hangar refreshes (and on a push webhook); `name@stable` resolving against
it, history included; `GET /api/harnesses`; `hangar doctor`'s report; HANGAR_DEFAULT_HARNESS for intake jobs.

On the fake provider with the scripted agent of tests/test_harness_thaw.py."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from hangar import harness_lock, harness_repo, harness_thaw
from hangar.jobs import Caller, SubmitJob
from hangar.models import JobState
from tests import test_harness_lock as hl
from tests.test_harness_lock import DONE, GIT
from tests.test_harness_thaw import _commit, _frozen_registry, jobs  # noqa: F401 - fixture
from tests.test_jobs import _until

skillenv, world = hl.skillenv, hl.world


def _bare(src, root):
    bare = root / "team-harnesses.git"
    subprocess.run([*GIT, "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


async def test_a_git_url_registry_is_fetched_refreshed_and_listed(jobs, world, settings, tmp_path):  # noqa: F811
    reg, digest = await _frozen_registry(jobs, world)
    bare = _bare(reg, tmp_path)
    settings.harness_repo = f"file://{bare}#main"
    with pytest.raises(harness_thaw.ThawError, match="hasn't been fetched yet"):
        harness_thaw.resolve(settings, "team")
    repo = harness_repo.of(jobs.fleet)
    st = await repo.refresh()
    assert st["ok"] and st["kind"] == "git" and st["commit"] and st["last_success"], st
    assert harness_thaw.resolve(settings, "team@stable").digest == digest
    got = harness_repo.listing(settings)
    assert got["harnesses"] == [{"name": "team", "revision": 1, "digest": digest, "channels": {
        "stable": {"digest": digest, "revision": 1, "resolved": True}}, "error": None, "harness": "tagent",
        "path": "harnesses/team/harness.lock"}]

    # a new revision pushed to the repo: the next refresh has it, and r1 still resolves from history
    lock = harness_lock.load(reg / "harnesses/team")
    lock2 = {**lock, "revision": 2, "model": {"id": "m-two", "policy": None}}
    (reg / "harnesses/team/harness.lock").write_text(harness_lock.dump(lock2))
    (reg / "channels.yaml").write_text(yaml.safe_dump({"team": {"stable": digest,
                                                                "canary": harness_lock.harness_digest(lock2)}}))
    _commit(reg, "team r2")
    subprocess.run([*GIT, "-C", str(reg), "push", "-q", str(bare), "HEAD:refs/heads/main"], check=True)
    assert harness_thaw.resolve(settings, "team").lock["revision"] == 1  # not refreshed yet
    await repo.refresh()
    assert harness_thaw.resolve(settings, "team").lock["revision"] == 2
    assert harness_thaw.resolve(settings, "team@r1").digest == digest  # from the checkout's history
    chans = harness_repo.listing(settings)["harnesses"][0]["channels"]
    assert chans["stable"]["revision"] == 1 and chans["canary"]["revision"] == 2

    # doctor: reachable, refreshed, channels resolved; then a channel that points nowhere
    checks = {c["name"]: c for c in harness_repo.doctor_checks(jobs)}
    assert checks["harness repo"]["status"] == "ok" and "refreshed" in checks["harness repo"]["detail"]
    assert checks["harness channels"]["status"] == "ok" and "2 channel(s) resolved" in checks["harness channels"][
        "detail"]
    (reg / "channels.yaml").write_text(yaml.safe_dump({"team": {"stable": "sha256:" + "0" * 64}}))
    _commit(reg, "bad channel")
    subprocess.run([*GIT, "-C", str(reg), "push", "-q", str(bare), "HEAD:refs/heads/main"], check=True)
    await repo.refresh()
    checks = {c["name"]: c for c in harness_repo.doctor_checks(jobs)}
    assert checks["harness channels"]["status"] == "fail" and "team@stable" in checks["harness channels"]["detail"]

    # unreachable: the last good checkout stays, and doctor says why
    settings.harness_repo = f"file://{tmp_path}/nope.git"
    st = await harness_repo.of(jobs.fleet).refresh()
    assert not st["ok"] and "git clone failed" in st["error"]
    checks = {c["name"]: c for c in harness_repo.doctor_checks(jobs)}
    assert checks["harness repo"]["status"] == "fail" and "never" in checks["harness repo"]["detail"]


async def test_the_api_and_the_push_webhook(jobs, world, settings, tmp_path, monkeypatch):  # noqa: F811
    reg, digest = await _frozen_registry(jobs, world)
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.env_dirs = [world["envs"], *settings.env_dirs]
    with TestClient(create_app(settings)) as c:
        admin = {"Authorization": "Bearer t0ken"}
        got = c.get("/api/harness-locks", headers=admin).json()
        assert got["registry"]["kind"] == "path" and got["harnesses"][0]["channels"]["stable"]["digest"] == digest
        assert c.get("/api/harness-locks", headers={}).status_code == 401
        assert c.post("/api/harness-locks/refresh", headers=admin).json()["kind"] == "path"
    # the push webhook refreshes only for the harness repo itself
    settings.harness_repo = "https://github.com/acme/team-harnesses#main"
    refreshed = []

    async def fake_refresh():
        refreshed.append(1)
        return {}

    repo = harness_repo.of(jobs.fleet)
    monkeypatch.setattr(repo, "refresh", fake_refresh)
    assert not repo.on_push("acme/other")
    assert repo.on_push("Acme/Team-Harnesses")
    import asyncio

    await asyncio.sleep(0)
    assert refreshed == [1]
    assert harness_repo.split("https://x/y.git#v2") == ("https://x/y.git", "v2")
    assert harness_repo.is_url("git@github.com:a/b.git") and not harness_repo.is_url("/srv/team-harnesses")
    assert harness_repo._safe_url("https://x-access-token:abc@github.com/a/b") == "https://github.com/a/b"


async def test_intake_jobs_run_from_the_default_harness(jobs, world, settings):  # noqa: F811
    reg, digest = await _frozen_registry(jobs, world)
    settings.intake_posture = "off"
    settings.default_harness = "team@stable"

    def submit(source, **kw):
        return jobs.submit(SubmitJob(instructions="work", repo=world["url"], environment="lockenv", deliver="none",
                                     keep_minutes=60, **kw), Caller("svc", "svc"), source=source)

    job = submit("jira")
    assert job.harness_lock and job.harness_lock["digest"] == digest and job.harness_lock["ref"] == "team@stable"
    assert any("runs harness team@stable (HANGAR_DEFAULT_HARNESS) at r1" in e.message for e in job.events)
    job = await _until(jobs, job.id, DONE, timeout=60)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert jobs._session(job).turns[0].harness_digest == digest
    # not for a job from the API, nor one that names its own harness or model
    assert submit("api").harness_lock is None
    assert submit("github", model="claude-sonnet-5-5").harness_lock is None
    # a default that doesn't resolve never blocks intake: the job runs as before, and says so
    settings.default_harness = "nope@stable"
    job = submit("slack")
    assert job.harness_lock is None
    assert any("HANGAR_DEFAULT_HARNESS=nope@stable not used" in e.message for e in job.events)
    checks = {c["name"]: c for c in harness_repo.doctor_checks(jobs)}
    assert checks["default harness"]["status"] == "fail"
    settings.harness_repo = None
    assert harness_repo.doctor_checks(jobs)[0]["status"] == "fail"  # a default without a repo
