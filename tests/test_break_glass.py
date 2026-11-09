"""Break-glass (KAN-33, spec 0015 §7): `hangar governance local`. While Tower can't answer, an admin's approval of a
job release or a landing in `enforce` is decided by hangar's own rules. One admin starts it, a second confirms it;
it lasts at most 24 hours and ends on its own; every step is audited and `hangar doctor` warns while it's on. A
Tower that answers still decides."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

import tests.test_govrelease as tg
from hangar import approvals, people
from hangar.governance import (
    BREAK_GLASS_LOG,
    BreakGlassError,
    Decision,
    active_break_glass,
    confirm_break_glass,
    end_break_glass,
    load_break_glass,
    parse_until,
    start_break_glass,
)
from hangar.govrelease import ApprovalPending, Releases, doctor_checks
from hangar.models import JobState
from tests.test_govrelease import Receiver, approved, audits, held, mode, unreachable

world, intake, tower = tg.world, tg.intake, tg.tower  # fixtures


def glass_on(store, t: float | None = None) -> None:
    t = time.time() if t is None else t
    start_break_glass(store, "ada", "Tower down, INC-12", t + 3600, t)
    confirm_break_glass(store, "owner", t)


def admin(store, pid: str = "ada") -> approvals.Approver:
    if people.get(store, pid) is None:
        people.add(store, pid, role="admin")
    return approvals.Approver(f"@{pid}:login", pid, "rest", frozenset({f"@{pid}:login"}))


# ====================================================================== the record
def test_two_admins_at_most_a_day_and_it_ends_on_its_own(store):
    t = 1_800_000_000.0
    assert parse_until("90m", t) == t + 5400 and parse_until("1h30m", t) == t + 5400
    with pytest.raises(BreakGlassError):
        parse_until("25h", t)
    with pytest.raises(BreakGlassError):
        parse_until("soon", t)
    bg = start_break_glass(store, "ada", "Tower down", t + 7200, t)
    assert not bg.active(t) and active_break_glass(store, t) is None  # not until a second admin confirms
    with pytest.raises(BreakGlassError, match="already started"):
        start_break_glass(store, "bob", "again", t + 60, t)
    with pytest.raises(BreakGlassError, match="second admin"):
        confirm_break_glass(store, "ada", t)
    confirm_break_glass(store, "owner", t)
    assert active_break_glass(store, t + 10).confirmed_by == "owner"
    with pytest.raises(BreakGlassError, match="already confirmed"):
        confirm_break_glass(store, "bob", t)
    assert active_break_glass(store, t + 7200) is None and load_break_glass(store, t + 7200) is None  # expired: gone
    assert load_break_glass(store, t) is None
    with pytest.raises(BreakGlassError):
        start_break_glass(store, "ada", "", t + 60, t)
    start_break_glass(store, "ada", "x", t + 60, t)
    assert end_break_glass(store) is not None and load_break_glass(store, t) is None


# ====================================================================== job release
async def test_an_admin_releases_a_job_locally_while_tower_is_down(intake):
    mode(intake, "enforce")
    rel = Releases(intake, Receiver(unreachable()))
    job_id = await held(intake, 81)
    with pytest.raises(ApprovalPending):  # no break-glass: fail closed, as before
        await rel.decide(job_id, admin(intake.store), "approve")
    glass_on(intake.store)
    job = await rel.decide(job_id, admin(intake.store), "approve")
    assert job.state == JobState.QUEUED and job.approval.decided_by == "@ada:login"
    g = job.approval.governance
    assert (g["mode"], g["decided_by"], g["rule"], g["tower"]["failure"]) == ("break_glass", "hangar", "break_glass",
                                                                               "unreachable")
    assert job.approval.receipt is None
    [a] = audits(intake, "governance.break_glass.decision")
    assert (a.actor, a.detail["kind"], a.detail["confirmed_by"]) == ("@ada:login", "job.release", "owner")
    rec = json.loads(intake.store.kv_get(BREAK_GLASS_LOG + f"hangar:job:{job_id}:release"))
    assert rec["decided_by"] == "@ada:login" and rec["failure"] == "unreachable"  # kept to replay to Tower


async def test_only_an_admin_and_never_against_an_answer_from_tower(intake, tower):
    mode(intake, "enforce")
    glass_on(intake.store)
    people.add(intake.store, "mia", role="member")
    rel = Releases(intake, Receiver(unreachable()))
    job_id = await held(intake, 82)
    member = approvals.Approver("@mia:login", "mia", "rest", frozenset({"@mia:login"}))
    with pytest.raises((ApprovalPending, approvals.ApprovalRefused)):
        await rel.decide(job_id, member, "approve")
    assert intake.get(job_id).state == JobState.AWAITING_APPROVAL
    # Tower answers: its deny stands, break-glass or not
    tower.answer = lambda req: Decision(outcome="deny", status="blocked", rule="no-outsiders", reason="no")
    rel2 = Releases(intake, Receiver(tower))
    job2 = await held(intake, 83)
    assert (await rel2.decide(job2, admin(intake.store), "approve")).state == JobState.CANCELLED
    assert audits(intake, "governance.break_glass.decision") == []


async def test_break_glass_waiting_for_confirmation_decides_nothing(intake):
    mode(intake, "enforce")
    start_break_glass(intake.store, "ada", "Tower down", time.time() + 3600)
    rel = Releases(intake, Receiver(unreachable()))
    job_id = await held(intake, 84)
    with pytest.raises(ApprovalPending):
        await rel.decide(job_id, admin(intake.store), "approve")


# ====================================================================== landing
async def test_a_landing_an_admin_approved_arms_under_break_glass(intake):
    mode(intake, "enforce", "landing.approve")
    rel = Releases(intake, Receiver(unreachable()))
    job = approved(intake)  # reviewer `lee`, GitHub id 31337
    assert "approval service unavailable" in await rel.landing(job)
    glass_on(intake.store)
    assert "approval service unavailable" in await rel.landing(job)  # lee is linked to nobody: not an admin
    assert audits(intake, "governance.break_glass.refused")
    people.add(intake.store, "lee", role="admin")
    people.link(intake.store, "lee", "github", "31337")
    assert await rel.landing(job) is None
    assert job.landing.governance["rule"] == "break_glass" and job.landing.receipt is None
    assert await rel.landing(job) is None  # decided for this head: not asked again
    assert len(audits(intake, "governance.break_glass.decision")) == 1


# ====================================================================== the command, REST and doctor
@pytest.fixture
def app_world(settings):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost:8787") as client:
        h = app.state.hangar
        tok = {}
        for pid, role in (("ada", "admin"), ("mia", "member")):
            tok[pid] = client.post("/api/people", json={"id": pid, "role": role},
                                   headers={"Authorization": "Bearer t0ken"}).json()["login"]["token"]
        ada2, _ = h.auth.create("ada-laptop", ["admin"], person="ada")
        yield {"c": client, "h": h, "owner": "t0ken", "ada": tok["ada"], "ada2": ada2, "mia": tok["mia"]}


def test_rest_needs_admins_two_people_and_is_audited(app_world):
    c, h = app_world["c"], app_world["h"]

    def call(who: str, path: str = "", **kw):
        return c.post(f"/api/governance/break-glass{path}", headers={"Authorization": f"Bearer {app_world[who]}"},
                      **kw)

    assert call("mia", json={"until": "2h", "reason": "x"}).status_code == 403
    assert call("ada", json={"until": "48h", "reason": "x"}).status_code == 400
    r = call("ada", json={"until": "2h", "reason": "Tower down, INC-12"})
    assert r.status_code == 200 and r.json()["state"] == "awaiting_confirmation"
    assert doctor_checks(h)[0]["status"] == "warn"
    r = call("ada2", "/confirm")  # the same person with another token
    assert r.status_code == 400 and "second admin" in r.json()["error"]
    r = call("owner", "/confirm")
    assert r.json()["state"] == "active" and r.json()["confirmed_by"] == "owner"
    [chk] = doctor_checks(h)
    assert chk["status"] == "warn" and "ACTIVE" in chk["detail"] and "--end" in chk["fix"]
    assert c.get("/api/governance/break-glass", headers={"Authorization": "Bearer t0ken"}).json()["state"] == "active"
    assert call("ada", "/end").json() == {"state": "off", "ended": True}
    assert doctor_checks(h) == []
    steps = [e.detail["step"] for e in h.store.audit_tail(50) if e.action == "governance.break_glass"]
    assert sorted(steps) == ["confirm", "end", "start"]


def test_the_cli(monkeypatch):
    from typer.testing import CliRunner

    from hangar import cli

    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if path.endswith("/end"):
            return {"state": "off", "ended": True}
        return {"state": "awaiting_confirmation", "started_by": "ada", "confirmed_by": None, "reason": "r",
                "until": time.time() + 3600, "left_seconds": 3600}

    monkeypatch.setattr(cli, "_call", fake)
    run = CliRunner().invoke
    assert run(cli.app, ["governance", "local", "--until", "2h", "--reason", "r"]).exit_code == 0
    assert run(cli.app, ["governance", "local", "--confirm"]).exit_code == 0
    r = run(cli.app, ["governance", "local", "--end"])
    assert r.exit_code == 0 and "ended" in r.output
    assert run(cli.app, ["governance", "local", "--confirm", "--end"]).exit_code != 0
    assert run(cli.app, ["governance", "local", "--until", "2h"]).exit_code != 0
    assert [c[1] for c in calls] == ["/api/governance/break-glass", "/api/governance/break-glass/confirm",
                                     "/api/governance/break-glass/end"]
    assert calls[0][2] == {"until": "2h", "reason": "r"}
