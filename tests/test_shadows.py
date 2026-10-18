"""Shadow runs (#183): a repo policy `shadow: {harness | pin_set, p, sources}` samples real intake jobs; each sampled
job's shadow runs the candidate harness at the job's base commit as a `source: shadow` trial with egress none, no
person, no tracker write-back, nothing posted and no PR, listed for nobody; p > 0 needs the review-wait p50 under 4 h,
and shadows pause when it rises.

On the fake provider with the scripted agent of tests/test_harness_lock.py, real git and tmux."""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from hangar import experiments, intake, posture, shadows
from hangar.jobs import Caller, SubmitJob
from hangar.models import JobState
from hangar.repopolicy import PolicyError, PolicyUpdate, RepoPolicy, ShadowPolicy, repo_key, update
from tests import test_harness_thaw as th
from tests.test_harness_lock import DONE
from tests.test_jobs import _until

skillenv, world, jobs = th.skillenv, th.world, th.jobs
needs_tools = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


def test_a_shadow_names_one_harness():
    with pytest.raises(ValueError, match="one of `pin_set` or `harness`"):
        ShadowPolicy(p=0.5)
    with pytest.raises(ValueError, match="one of `pin_set` or `harness`"):
        ShadowPolicy(p=0.5, pin_set="pin-" + "a" * 16, harness="team@sha256:" + "1" * 64)
    with pytest.raises(ValueError):
        ShadowPolicy(p=1.5, harness="team@sha256:" + "1" * 64)
    assert ShadowPolicy(harness="team@sha256:" + "1" * 64).sources == ["jira", "github", "slack"]


async def test_turning_shadows_on_needs_review_to_spare(jobs, monkeypatch):  # noqa: F811
    lock = "team@sha256:" + "1" * 64
    body = PolicyUpdate(repo="o/r", shadow=ShadowPolicy(harness=lock, p=0.2))
    monkeypatch.setattr(shadows, "review_wait_p50", lambda svc: None)
    with pytest.raises(PolicyError, match="no agent PR review to measure"):
        update(jobs, body, "admin")
    monkeypatch.setattr(shadows, "review_wait_p50", lambda svc: 6.5)
    with pytest.raises(PolicyError, match="review-wait p50 is 6.5 h, and shadows need it under 4 h"):
        update(jobs, body, "admin")
    update(jobs, PolicyUpdate(repo="o/r", shadow=ShadowPolicy(harness=lock, p=0)), "admin")  # off is always fine
    monkeypatch.setattr(shadows, "review_wait_p50", lambda svc: 2.0)
    assert update(jobs, body, "admin").shadow.p == 0.2


async def _real_job(jobs, world, source="jira", **kw):
    return jobs.submit(SubmitJob(instructions="Fix the bug from the ticket", repo=world["url"], environment="lockenv",
                                 deliver="none", keep_minutes=0, labels={intake.TRACKER: "jira:ABC-12"}, **kw),
                       Caller("@ann:login", "tok-ann", person="ann"), source=source,
                       source_ref="jira:ABC-12" if source == "jira" else None)


async def _shadowed(jobs, world, monkeypatch, p=1.0):
    _, digest = await th._frozen_registry(jobs, world)
    jobs.policies.put(RepoPolicy(repo=repo_key(world["url"]), shadow=ShadowPolicy(harness=f"team@{digest}", p=p,
                                                                                    sources=["jira"], owner="ivy")))
    monkeypatch.setattr(shadows, "review_wait_p50", lambda svc: 1.0)
    tracked: list[tuple[str, str]] = []

    async def tracker(svc, job, ticket, stage):
        tracked.append((job.id, stage))

    monkeypatch.setitem(intake.TRACKERS, "jira", tracker)
    from hangar import jobs as jobs_module

    async def notify(svc, job, ref, body, kind):  # Jira's comment API, faked: the real job's thread
        tracked.append((job.id, f"comment:{kind}"))

    monkeypatch.setitem(jobs_module.NOTIFIERS, "jira", notify)
    from hangar import egress

    async def proxy_url(fleet_, prov, sb):  # a shadow runs with egress none; the fake provider has no network
        return "http://127.0.0.1:1"

    monkeypatch.setattr(egress, "_proxy_url", proxy_url)
    return digest, tracked


@needs_tools
async def test_a_jira_job_gets_exactly_one_shadow_that_leaves_no_trace(jobs, world, monkeypatch, settings, tmp_path):
    settings.outbox_url = str(tmp_path / "bucket")
    jobs.fleet.__dict__.pop("_outbox", None)
    digest, tracked = await _shadowed(jobs, world, monkeypatch)
    real = await _real_job(jobs, world)
    real = await _until(jobs, real.id, DONE, timeout=120)
    assert real.state == JobState.SUCCEEDED, [e.message for e in real.events]
    exp_id = real.labels[shadows.PICK_LABEL]
    exp = await _until(jobs, exp_id, DONE, timeout=180)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    st = experiments.load(exp)
    assert len(st.trials) == 1, st.trials
    shadow = jobs.get(st.trials[0].job)
    # exactly one shadow, linked to the real job, at the base the real job started on
    assert exp.source == "shadow" and exp.labels[shadows.SHADOW_OF] == real.id and st.spec["source"] == "shadow"
    assert shadow.source == "shadow" and shadow.labels[shadows.SHADOW_OF] == real.id
    assert jobs._session(shadow).base_sha == real.result.base_sha
    assert shadow.harness_lock["digest"] == digest and shadow.queue_class == "eval"
    assert [j for j in jobs._jobs.values() if j.source == "shadow" and j.id != exp.id] == [shadow]
    # no trace on real work: no tracker, nothing to reply to, no PR, no person, egress none, the owner's budget
    assert intake.TRACKER not in shadow.labels and shadow.source_ref is None
    assert all(tn.reply_to is None for tn in shadow.turns) and shadow.callback_url is None
    assert shadow.deliver == "none" and shadow.result.pr_url is None and shadow.person is None
    assert shadow.labels[posture.FLOOR_LABEL] == "none" and shadow.labels[experiments.AUTHOR_LABEL] == "ivy"
    assert {jid for jid, _ in tracked} == {real.id}  # Jira hears about the real job only
    # listed for nobody but an admin who asks
    assert shadow.id not in [j.id for j in jobs.list(limit=100)]
    assert exp.id not in [j.id for j in jobs.list(limit=100)]
    assert shadow.id in [j.id for j, _ in jobs.page(limit=100, source="shadow")[0]]
    from hangar import inbox

    assert not [x for x in inbox.items(jobs, None) if x.get("job") in (shadow.id, exp.id)]
    # the run capture says what it is
    from hangar import outbox

    for _ in range(100):
        runs = [json.loads(p.read_text()) for p in (tmp_path / "bucket").rglob("run.json")] \
            if (tmp_path / "bucket").exists() else []
        await outbox.of(jobs.fleet).drain_once()
        if any(r["source"] == "shadow" for r in runs):
            break
        await asyncio.sleep(0.1)
    shadow_runs = [r for r in runs if r["source"] == "shadow"]
    assert shadow_runs and shadow_runs[0]["shadow_of"] == real.id and shadow_runs[0]["job_id"] == shadow.id


@needs_tools
async def test_shadows_pause_when_review_wait_rises_and_skip_other_sources(jobs, world, monkeypatch):
    await _shadowed(jobs, world, monkeypatch)
    monkeypatch.setattr(shadows, "review_wait_p50", lambda svc: 5.0)
    paused = await _real_job(jobs, world)
    assert shadows.PICK_LABEL not in paused.labels
    again = await _real_job(jobs, world)  # still paused: audited once, not per job
    assert shadows.PICK_LABEL not in again.labels
    assert [a.action for a in jobs.store.audit_tail(200)].count("shadow.paused") == 1
    monkeypatch.setattr(shadows, "review_wait_p50", lambda svc: 1.0)
    api = await _real_job(jobs, world, source="api")  # not an intake source of the policy
    assert shadows.PICK_LABEL not in api.labels
    monkeypatch.setattr(shadows, "rand", lambda: 0.99)
    jobs.policies.put(jobs.policies.get(repo_key(world["url"])).model_copy(
        update={"shadow": jobs.policies.get(repo_key(world["url"])).shadow.model_copy(update={"p": 0.5})}))
    unlucky = await _real_job(jobs, world)
    assert shadows.PICK_LABEL not in unlucky.labels  # sampled with probability p
    for j in (paused, again, api, unlucky):
        await _until(jobs, j.id, DONE, timeout=120)
    assert not [j for j in jobs._jobs.values() if j.source == "shadow"]
