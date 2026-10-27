"""KAN-174, known gap 8: a custom session branch another person's sandbox created first.

The gateway records who created each branch through it (branchowners.py); a new session refuses a custom branch
outside hangar/session/ that a different person's sandbox created, unless an admin takes it. A branch hangar has no
record of, or one the same person's sandbox created, is continued as before. The gateway tests push with real git
through uvicorn to a `git http-backend` upstream (tests/test_gateway_git).
"""

from __future__ import annotations

import contextlib

import pytest

from hangar import branchowners, delivery, identity
from hangar.auth import Principal
from hangar.models import ExecResult, Sandbox, Session
from hangar.sessions import NewSession, SessionError
from tests.test_gateway_git import gw, upstream  # noqa: F401 - fixtures

REPO = "acme/widgets"
ADMIN = Principal("ops", "tok-o", ["admin"], person="owner", role="admin")
ALICE = Principal("alice-laptop", "tok-a", ["mcp"], person="alice", role="member")


@pytest.fixture
async def sessions(fleet, store, settings, gw, tmp_path):  # noqa: F811
    from hangar.sessions import SessionService
    from hangar.vault import Vault

    settings.session_poll_seconds = 0.1
    fleet.catalog.add({"name": "gwa", "harnesses": ["claude-code"], "credentials": ["gh-pat"],
                       "credential_mode": "gateway",
                       "repo": {"url": "https://github.com/acme/widgets.git", "branch": "main"}}, save_dir=tmp_path)
    svc = SessionService(fleet, Vault(store, b"0" * 43 + b"="))
    await svc.start()
    yield svc
    await svc.stop()


@contextlib.contextmanager
def _as(p: Principal):
    tok = identity.current.set(p)
    try:
        yield
    finally:
        identity.current.reset(tok)


def _audits(store, action: str) -> list:
    return [e for e in store.audit_tail(500) if e.action == action]


def _planted(store, branch: str, person: str | None, job: str | None = None) -> None:
    sb = Sandbox(name="other", environment="gw", provider="docker", person=person,
                 labels={"hangar.job": job} if job else {})
    branchowners.record(store, REPO, f"refs/heads/{branch}", sb, via="gateway")


# ============================================================================ the gateway records creators
async def test_gateway_records_the_person_whose_sandbox_created_a_branch(fleet, gw, upstream, store):  # noqa: F811
    sb = await fleet.create("gw", work_branch="hangar/job-1", person="bob")
    res = await fleet.exec(sb.id, "git commit -q --allow-empty -m one && "
                                  "git push -q origin HEAD:refs/heads/hangar/feature-x")
    assert res.exit_code == 0, res.stderr
    rec = branchowners.creator(store, REPO, "hangar/feature-x")
    assert rec is not None and rec["person"] == "bob" and rec["sandbox"] == sb.id and rec["via"] == "gateway"
    assert any(e.detail.get("refs", [{}])[0].get("ref") == "refs/heads/hangar/feature-x"
               for e in _audits(store, "gw.git.push"))
    # an update isn't a creation: the record stays, and a branch hangar never saw created has none
    res = await fleet.exec(sb.id, "git commit -q --allow-empty -m two && "
                                  "git push -q origin HEAD:refs/heads/hangar/feature-x")
    assert res.exit_code == 0, res.stderr
    assert branchowners.creator(store, REPO, "hangar/feature-x")["at"] == rec["at"]
    assert branchowners.creator(store, REPO, "keep") is None


async def test_a_refused_push_records_nothing(fleet, gw, upstream, store):  # noqa: F811
    sb = await fleet.create("gw", work_branch="hangar/job-1", person="bob")
    res = await fleet.exec(sb.id, "git push origin HEAD:refs/heads/elsewhere")
    assert res.exit_code != 0
    assert branchowners.creator(store, REPO, "elsewhere") is None


async def test_a_jobs_delivery_push_records_the_jobs_person(fleet, gw, upstream, store):  # noqa: F811
    """A job's sandbox is fetch-only; hangar pushes its branch with a delivery grant, through the gateway."""
    sb = await fleet.create("gw", work_branch="hangar/job-7", person="carol", labels={"hangar.job": "job-7"})
    await fleet.exec(sb.id, "git commit -q --allow-empty -m work")
    head = (await fleet.exec(sb.id, "git rev-parse HEAD")).stdout.strip()
    res = await delivery.push(fleet, sb.id, "hangar/job-7", head, "job:job-7")
    assert res.exit_code == 0, res.stderr
    rec = branchowners.creator(store, REPO, "hangar/job-7")
    assert rec["person"] == "carol" and rec["job"] == "job-7" and rec["via"] == "delivery"


def test_a_direct_mode_delivery_records_the_jobs_person_once(fleet, store):
    sb = Sandbox(name="j", environment="x", provider="docker", person="carol", labels={"hangar.job": "job-8"})
    url = "https://github.com/Acme/Widgets.git"
    delivery._record(fleet, sb, url, "refs/heads/hangar/job-8", ExecResult(exit_code=1, stdout="", stderr=""))
    assert branchowners.creator(store, REPO, "hangar/job-8") is None, "a failed push records nothing"
    delivery._record(fleet, sb, url, "refs/heads/hangar/job-8", ExecResult(exit_code=0, stdout="", stderr=""))
    assert branchowners.creator(store, REPO, "hangar/job-8")["person"] == "carol"
    other = Sandbox(name="k", environment="x", provider="docker", person="dave")
    delivery._record(fleet, other, url, "refs/heads/hangar/job-8", ExecResult(exit_code=0, stdout="", stderr=""))
    assert branchowners.creator(store, REPO, "hangar/job-8")["person"] == "carol", "the first record stays"


# ============================================================================ a new session decides by creator
async def test_branch_another_persons_sandbox_created_is_refused(sessions, gw, store):  # noqa: F811
    _planted(store, "hangar/feature-x", "bob", job="job-1")
    with pytest.raises(SessionError, match="created by bob's hangar sandbox \\(job job-1\\)"):
        sessions.new(NewSession(name="mine", environment="gwa", branch="hangar/feature-x"), "alice", "alice",
                     person="alice")
    assert not sessions.taken(name="mine")
    (e,) = _audits(store, "session.branch_refused")
    assert e.actor == "alice" and e.detail["branch"] == "hangar/feature-x" and e.detail["created_by"] == "bob"
    assert e.detail["repo"] == REPO


async def test_branch_nobodys_sandbox_created_is_refused(sessions, gw, store):  # noqa: F811
    """A GitHub or webhook job's sandbox has no person: its branch is nobody's, so it is no one's to continue."""
    _planted(store, "hangar/from-issue", None)
    with pytest.raises(SessionError, match="created by a hangar sandbox"):
        sessions.new(NewSession(name="mine", environment="gwa", branch="hangar/from-issue"), "alice", "alice",
                     person="alice")


async def test_branch_without_a_record_is_continued(sessions, gw, store):  # noqa: F811
    """Pushed by a person from their machine, or before the record existed: continue my branch, as before."""
    s = sessions.new(NewSession(name="mine", environment="gwa", branch="feature/pushed-by-hand"), "alice", "alice",
                     person="alice")
    assert s.branch == "feature/pushed-by-hand"
    assert not _audits(store, "session.branch_refused")


async def test_branch_the_same_persons_sandbox_created_is_continued(sessions, gw, store):  # noqa: F811
    _planted(store, "hangar/feature-x", "alice")
    s = sessions.new(NewSession(name="mine", environment="gwa", branch="hangar/feature-x"), "alice", "alice",
                     person="alice")
    assert s.branch == "hangar/feature-x"
    with _as(ALICE):  # the caller's person when the caller passes none
        s2 = sessions.new(NewSession(name="mine2", environment="gwa", branch="hangar/feature-x"), "a", "a")
    assert s2.branch == "hangar/feature-x"
    assert not _audits(store, "session.branch_refused")


async def test_session_branches_and_other_repos_are_not_checked(sessions, gw, store):  # noqa: F811
    _planted(store, "hangar/session/mine", "bob")
    s = sessions.new(NewSession(name="mine", environment="gwa"), "alice", "alice", person="alice")
    assert s.branch == "hangar/session/mine"
    other = Sandbox(name="o", environment="gw", provider="docker", person="bob")
    branchowners.record(store, "acme/other", "refs/heads/hangar/feature-x", other, via="gateway")
    s = sessions.new(NewSession(name="mine2", environment="gwa", branch="hangar/feature-x"), "alice", "alice",
                     person="alice")
    assert s.branch == "hangar/feature-x"


async def test_admin_takes_another_persons_branch_only_on_purpose(sessions, gw, store):  # noqa: F811
    _planted(store, "hangar/feature-x", "bob")
    with _as(ADMIN), pytest.raises(SessionError, match="take_branch"):
        sessions.new(NewSession(name="mine", environment="gwa", branch="hangar/feature-x"), "ui:ops", "ui:ops")
    with _as(ADMIN):
        s = sessions.new(NewSession(name="mine", environment="gwa", branch="hangar/feature-x", take_branch=True),
                         "ui:ops", "ui:ops")
    assert s.branch == "hangar/feature-x"
    (e,) = _audits(store, "session.branch_override")
    assert e.target == s.id and e.actor == "ui:ops" and e.detail["created_by"] == "bob"


async def test_a_member_cannot_take_another_persons_branch(sessions, gw, store):  # noqa: F811
    _planted(store, "hangar/feature-x", "bob")
    with _as(ALICE), pytest.raises(identity.Forbidden, match="only an admin"):
        sessions.new(NewSession(name="mine", environment="gwa", branch="hangar/feature-x", take_branch=True),
                     "a", "a", person="alice")
    (e,) = _audits(store, "session.branch_refused")
    assert e.detail["take_branch"] is True
    assert not _audits(store, "session.branch_override")


# ============================================================================ end to end
async def test_a_branch_planted_through_the_gateway_is_refused_and_a_new_one_is_reserved(
        sessions, fleet, gw, upstream, store):  # noqa: F811
    bob = await fleet.create("gw", work_branch="hangar/job-1", person="bob")
    res = await fleet.exec(bob.id, "git commit -q --allow-empty -m planted && "
                                   "git push -q origin HEAD:refs/heads/hangar/feature-x")
    assert res.exit_code == 0, res.stderr
    with pytest.raises(SessionError, match="created by bob's"):
        sessions.new(NewSession(name="mine", environment="gwa", branch="hangar/feature-x"), "alice", "alice",
                     person="alice")
    # a branch nobody created yet is the session's from the moment it is created: bob's sandbox can't take it
    s = sessions.new(NewSession(name="mine", environment="gwa", branch="hangar/feature-y"), "alice", "alice",
                     person="alice")
    assert store.get(Session, s.id) is not None
    res = await fleet.exec(bob.id, "git push origin HEAD:refs/heads/hangar/feature-y")
    # the session's record reserves it at once; once its sandbox has booted, that sandbox's grant does too
    assert res.exit_code != 0 and ("belongs to session mine" in res.stderr
                                   or "belongs to another sandbox" in res.stderr), res.stderr
    assert "feature-y" not in upstream.refs()
