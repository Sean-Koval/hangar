"""Change sets landing (changesets.py, landing.py): the parts land in the profile's order under their own repos'
policies, and a part closed without merging breaks the set. Driven by webhooks against the fake GitHub of
test_changesets, after a real four-repo delivery on the fake provider."""

from __future__ import annotations

import pytest

from hangar import changesets
from hangar.jobs import JobService
from hangar.models import Job
from tests.test_changesets import (  # noqa: F401 - fixtures
    MultiGH,
    cjobs,
    deliver_three,
    multi,
    sessions,
)
from tests.test_landing import settle

pytestmark = pytest.mark.skipif(not __import__("shutil").which("tmux"), reason="needs tmux and git")


async def approve(svc: JobService, part: Job, login: str = "alice") -> None:
    """A code owner's approval of the part's head. These sandboxes push without the git gateway (its grants are
    test_changesets' and test_gateway's), so the part is marked as pushed through it, or landing would refuse it."""
    part = svc.get(part.id)
    assert part.landing.push_mode == "direct"
    part.landing.push_mode = "gateway"
    svc._save(part)
    repo, n, sha = f"acme/{part.role}", part.result.pr_number, part.result.head_sha
    svc.gh.reviews.setdefault((repo, n), []).append({"user": {"login": login}, "state": "APPROVED", "commit_id": sha})
    await svc.landing.handle("pull_request_review", {
        "action": "submitted", "repository": {"full_name": repo},
        "pull_request": {"number": n, "node_id": f"PR_{repo}_{n}", "head": {"sha": sha}},
        "review": {"id": 700 + n, "user": {"login": login, "id": 77}, "state": "approved", "commit_id": sha,
                   "submitted_at": "2026-10-19T12:00:00Z"}})
    await settle(svc)


async def close(svc: JobService, part: Job, merged: bool = True) -> str:
    repo, n, sha = f"acme/{part.role}", part.result.pr_number, part.result.head_sha
    merge_sha = (f"{n:02d}" * 20)[:40]
    svc.gh.prs[(repo, n)]["state"] = "closed"
    svc.gh.armed.pop((repo, n), None)
    await svc.landing.handle("pull_request", {
        "action": "closed", "repository": {"full_name": repo}, "sender": {"login": "alice"},
        "pull_request": {"number": n, "node_id": f"PR_{repo}_{n}", "head": {"sha": sha}, "merged": merged,
                         "merged_at": "2026-10-19T12:05:00Z" if merged else None,
                         "merged_by": {"login": "web-flow"} if merged else None,
                         "merge_commit_sha": merge_sha if merged else None}})
    await settle(svc)
    return merge_sha


def armed(svc: JobService, role: str) -> str | None:
    return svc.gh.armed.get((f"acme/{role}", MultiGH.NUMBERS[f"acme/{role}"]))


def pr_parts(svc: JobService, cs) -> dict[str, Job]:
    return {r: svc.get(j) for r, j in cs.parts.items()}


async def test_parts_land_in_order(cjobs, multi):  # noqa: F811
    job, cs = await deliver_three(cjobs)
    p = pr_parts(cjobs, cs)
    # the code PR, approved first, waits for the database PR
    await approve(cjobs, p["code"])
    code = cjobs.get(p["code"].id)
    assert code.landing.pr_state == "approved" and not armed(cjobs, "code")
    assert code.landing.note == f"waiting for database #12 to merge first (change set {cs.id})"
    # the database PR lands on its own approval; the parent's sandbox is checkpointed away before it arms
    await approve(cjobs, p["database"])
    assert armed(cjobs, "database") == p["database"].result.head_sha
    assert cjobs.get(job.id).sandbox_id is None and cjobs.sessions.get(job.session_id).checkpoints
    # infra approved: arms now that database merged? not yet: database is armed, not merged
    await approve(cjobs, p["infra"])
    assert not armed(cjobs, "infra")
    assert "waiting for database #12" in cjobs.get(p["infra"].id).landing.note
    await close(cjobs, p["database"])
    # database merged: infra (approved and waiting) arms by itself; code still waits for infra
    assert armed(cjobs, "infra") == p["infra"].result.head_sha and not armed(cjobs, "code")
    assert "waiting for infra #11" in cjobs.get(p["code"].id).landing.note
    await close(cjobs, p["infra"])
    assert armed(cjobs, "code") == p["code"].result.head_sha
    assert changesets.of(cjobs).get(cs.id).state == "landing"
    await close(cjobs, p["code"])
    cs = changesets.of(cjobs).get(cs.id)
    assert cs.state == "done", [e.message for e in cjobs.get(job.id).events]
    assert any(e.message.startswith("changeset.done") for e in cjobs.get(job.id).events)


async def test_a_closed_part_breaks_the_set(cjobs, multi):  # noqa: F811
    job, cs = await deliver_three(cjobs)
    p = pr_parts(cjobs, cs)
    await approve(cjobs, p["database"])
    await close(cjobs, p["database"])
    await approve(cjobs, p["infra"])
    assert armed(cjobs, "infra")
    await approve(cjobs, p["code"])
    await close(cjobs, p["infra"], merged=False)  # someone closed infra without merging
    cs = changesets.of(cjobs).get(cs.id)
    assert cs.state == "broken" and "infra PR #11 was closed without merging" in cs.reason
    code = cjobs.get(p["code"].id)
    assert not armed(cjobs, "code") and "is broken" in code.landing.note
    await cjobs.landing.evaluate(code)  # a later look changes nothing
    assert not armed(cjobs, "code")
    await changesets.tick(cjobs)
    assert cjobs.gh.pr("acme/deploy") is None and "deploy" not in cs.parts
    assert any(e.message.startswith("changeset.broken") for e in cjobs.get(job.id).events)


async def test_an_agent_turn_disarms_every_part(cjobs, multi):  # noqa: F811
    job, cs = await deliver_three(cjobs)
    p = pr_parts(cjobs, cs)
    await approve(cjobs, p["database"])
    assert armed(cjobs, "database")
    multi["mode"].write_text("code-only")
    cjobs.follow_up(job.id, "one more thing", requester="sean")
    from tests.test_jobs import DONE, _until

    await _until(cjobs, job.id, DONE, timeout=60)
    assert not armed(cjobs, "database")
    assert any("an agent turn is starting on its change set" in e.message
               for e in cjobs.get(p["database"].id).events)


async def test_the_parents_session_ends_once_every_part_ended(cjobs, multi):  # noqa: F811
    job, cs = await deliver_three(cjobs)
    p = pr_parts(cjobs, cs)
    sid = job.session_id
    for role in ("database", "infra"):
        await approve(cjobs, p[role])
        await close(cjobs, p[role])
    assert all(cjobs.get(p[r].id).landing.ended for r in ("database", "infra"))
    assert not any("every PR of change set" in e.message for e in cjobs.get(job.id).events)
    await approve(cjobs, p["code"])
    await close(cjobs, p["code"])
    await settle(cjobs)
    assert any(f"every PR of change set {cs.id} ended" in e.message for e in cjobs.get(job.id).events)
    assert cjobs.sessions.get(sid).sandbox_id is None
