"""The "needs you" inbox (--- landing): per person, from the job and session records, as an MCP resource, REST
and the CLI."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from hangar import inbox
from hangar.auth import Principal
from hangar.jobs import JobService
from hangar.models import (
    Approval,
    Job,
    JobApproval,
    JobState,
    Session,
    SessionState,
    SessionTurn,
    now,
)
from tests.test_jobs import sessions  # noqa: F401 - fixture

ALICE = Principal("alice-laptop", "tok-a", ["mcp"], person="alice", role="member")
BOB = Principal("bob-laptop", "tok-b", ["mcp"], person="bob", role="member")
OWNER = Principal("ops", "tok-o", ["admin"], person="owner", role="admin")


def _job(svc: JobService, person: str | None, state: JobState, **kw) -> Job:
    j = Job(title=kw.pop("title", "t"), instructions="x", environment="e", repo="https://github.com/o/r.git",
            state=state, person=person, **kw)
    svc._jobs[j.id] = j
    return j


def _pr(j: Job, number: int, state: str = "open") -> Job:
    j.result.pr_number, j.result.pr_url, j.result.pushed = number, f"https://github.com/o/r/pull/{number}", True
    j.landing.pr_state = state
    return j


@pytest.fixture
def svc(fleet, sessions):  # noqa: F811
    return JobService(fleet, sessions, transport=httpx.MockTransport(lambda r: httpx.Response(404)))


def _kinds(xs: list[dict]) -> set[tuple[str, str | None]]:
    return {(x["kind"], x.get("job") or x.get("session")) for x in xs}


def test_what_needs_each_person(svc):
    review = _pr(_job(svc, "alice", JobState.SUCCEEDED), 7)
    asked = _job(svc, "alice", JobState.NEEDS_INPUT)
    asked.result.question = "A or B?"
    failed = _job(svc, "alice", JobState.FAILED, reason="verify_failed")
    red_pr = _pr(_job(svc, "alice", JobState.FAILED, reason="verify_failed"), 9)  # its PR is the item
    red_pr.landing.draft = True
    old = _job(svc, "alice", JobState.FAILED)
    old.updated_at = now() - 30 * 86400
    merged = _pr(_job(svc, "alice", JobState.SUCCEEDED), 8, "merged")
    manual = _pr(_job(svc, "alice", JobState.SUCCEEDED), 10, "approved")
    manual.landing.note = "the repo's policy lands PRs by hand (land: manual): merge it on GitHub"
    bobs = _pr(_job(svc, "bob", JobState.SUCCEEDED), 11)
    held = _job(svc, None, JobState.AWAITING_APPROVAL,
                approval=JobApproval(requested_by="github:rando", reason="rando is not trusted to start jobs"))
    turn = SessionTurn(message="m", live=True, approvals=[Approval(id="apr-1", tool="Bash", detail="rm -rf build")])
    tool = Session(name="alice-ses", environment="e", person="alice", state=SessionState.RUNNING, turns=[turn])
    q = Session(name="alice-q", environment="e", person="alice", state=SessionState.IDLE, needs_input="Which DB?")
    svc.sessions._sessions.update({tool.id: tool, q.id: q})

    mine = inbox.items(svc, ALICE)
    assert _kinds(mine) == {("review", review.id), ("question", asked.id), ("failed", failed.id),
                            ("review", red_pr.id), ("review", manual.id), ("approval", "alice-ses"),
                            ("question", "alice-q")}
    by = {x.get("job") or x.get("session"): x for x in mine}
    assert "draft" in by[red_pr.id]["reason"] and "verify_failed" in by[red_pr.id]["reason"]
    assert "land: manual" in by[manual.id]["reason"]
    assert by[asked.id]["reason"] == "the agent asks: A or B?" and asked.id in by[asked.id]["action"]
    assert "--allow apr-1" in by["alice-ses"]["action"] and "rm -rf build" in by["alice-ses"]["reason"]
    assert merged.id not in by and old.id not in by and bobs.id not in by and held.id not in by
    assert _kinds(inbox.items(svc, BOB)) == {("review", bobs.id)}
    # an admin gets their own and what nobody in particular owns (GitHub, Slack), not a teammate's
    admin = inbox.items(svc, OWNER)
    assert _kinds(admin) == {("approval", held.id)} and "hangar job approve" in admin[0]["action"]
    assert inbox.summary(mine) == {"review": 3, "question": 2, "failed": 1, "approval": 1}
    # a running job isn't waiting on anyone
    svc._active.add(review.id)
    assert review.id not in {x.get("job") for x in inbox.items(svc, ALICE)}


def test_other_modules_add_requests(svc, monkeypatch):
    def egress(s, p):
        return [{"kind": "approval", "reason": "egress to pypi.org", "action": "hangar egress allow …",
                 "since": now()}] if inbox.yours("alice", p) else []

    monkeypatch.setattr(inbox, "SOURCES", [egress])
    assert [x["reason"] for x in inbox.items(svc, ALICE)] == ["egress to pypi.org"]
    assert inbox.items(svc, BOB) == []


async def test_the_inbox_is_a_resource_not_a_tool(fleet, svc):
    from hangar.mcp_server import build_mcp

    _pr(_job(svc, "alice", JobState.SUCCEEDED), 7)
    async with Client(build_mcp(fleet, svc, svc.sessions)) as c:
        assert "hangar://inbox" in {str(r.uri) for r in (await c.list_resources()).resources}
        assert not [t for t in (await c.list_tools()).tools if "inbox" in t.name]
        got = json.loads((await c.read_resource("hangar://inbox")).contents[0].text)
        assert got["counts"] == {"review": 1} and got["items"][0]["pr"] == "https://github.com/o/r/pull/7"


def test_rest_and_cli(settings, monkeypatch):
    from typer.testing import CliRunner

    from hangar import cli
    from hangar.app import create_app

    settings.admin_token, settings.public_url = "t0ken", "http://localhost:8787"
    admin = {"Authorization": "Bearer t0ken"}
    with TestClient(create_app(settings), base_url="http://localhost:8787") as c:
        login = c.post("/api/people", json={"id": "alice"}, headers=admin).json()["login"]
        jobs = c.app.state.hangar.jobs
        _pr(_job(jobs, "alice", JobState.SUCCEEDED, title="fix csv"), 7)
        r = c.get("/api/inbox", headers={"Authorization": f"Bearer {login['token']}"}).json()
        assert r["person"] == "alice" and r["counts"] == {"review": 1}
        assert c.get("/api/inbox", headers=admin).json()["items"] == []  # the owner's inbox: not alice's PR
        svc_tok = c.post("/api/tokens", json={"name": "ci", "scopes": ["jobs"]}, headers=admin).json()["token"]
        assert c.get("/api/inbox", headers={"Authorization": f"Bearer {svc_tok}"}).status_code == 403
    monkeypatch.setattr(cli, "_call", lambda method, path, **kw: r)
    out = CliRunner().invoke(cli.app, ["inbox"])
    assert out.exit_code == 0 and "PR #7 waits for your review" in out.output and "review and approve" in out.output
    monkeypatch.setattr(cli, "_call", lambda method, path, **kw: {"items": [], "counts": {}})
    assert "Nothing needs you" in CliRunner().invoke(cli.app, ["inbox"]).output
