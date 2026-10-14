"""Requests an agent files with its owner (docs/computer-use.md): a missing secret or a blocked host reaches a person
(events, audit, an elicitation when an MCP client follows the work) and is never granted automatically. On the fake
provider with a scripted agent, real git and tmux."""

from __future__ import annotations

import asyncio
import json
import shutil

import mcp_types as types
import pytest
from fastapi.testclient import TestClient
from mcp import Client

import hangar.identity as identity
from hangar import egressapi
from hangar.auth import Principal
from hangar.envrequests import EnvRequest, EnvRequests, RequestError
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, SubmitJob
from hangar.mcp_server import build_mcp
from hangar.models import JobState
from hangar.sessions import NewSession
from tests import test_exchange as xchg
from tests.test_jobs import _until
from tests.test_mcp_modern import sc

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

world, svc, jobs = xchg.world, xchg.svc, xchg.jobs

AGENT = r"""#!/usr/bin/env bash
msg="$(cat "$HANGAR_PROMPT_FILE")"
case "$msg" in
  *TASK-NEEDS*)
    hangar-agent env request secret STRIPE_KEY --why "to run the payment tests"
    hangar-agent env request egress pypi.org:443 --why "uv sync needs the index"
    hangar-agent env request secret STRIPE_KEY --why "asking twice"
    printf 'not json' > "$HANGAR_IO/out/requests/1-deadbeef.json"
    case "$msg" in *SLOW*) sleep 4;; esac
    hangar-agent done "Did everything the secret doesn't block" ;;
  *TASK-SHOT*)
    printf '\211PNG\r\n' > after.png && hangar-agent artifact after.png screenshots/after.png
    echo x > f.txt && git add f.txt && git commit -qm "ui change"
    hangar-agent done "changed the page" ;;
esac
echo '{"type":"result","result":"ok"}'
"""

ALICE = Principal("alice-laptop", "tok-a", ["mcp"], person="alice", role="member")
BOB = Principal("bob-laptop", "tok-b", ["mcp"], person="bob", role="member")
ADMIN = Principal("ops", "tok-o", ["admin"], person="owner", role="admin")


class As:
    def __init__(self, p):
        self.p = p

    def __enter__(self):
        self.tok = identity.current.set(self.p)

    def __exit__(self, *exc):
        identity.current.reset(self.tok)


@pytest.fixture
def renv(world, monkeypatch, fleet):
    agent = world["root"] / "ragent.sh"
    agent.write_text(AGENT)
    monkeypatch.setitem(HARNESSES, "ragent", Harness(
        name="ragent", title="Requests", run=f"bash {agent}", session_dirs=[".fake/sessions"], resume="preset",
        resume_hint="ragent --resume {id}"))
    (world["envs"] / "renv.yaml").write_text("name: renv\nharnesses: [ragent]\n")
    fleet.catalog.reload()
    return "renv"


@pytest.fixture
def reqs(fleet, svc, jobs) -> EnvRequests:
    return EnvRequests(fleet, svc, jobs)


def box(fleet, s):
    return fleet.providers["docker"].boxes[s.sandbox_id] / "home/agent"


async def test_requests_reach_the_owner_and_wait_for_a_person(svc, fleet, renv, reqs, monkeypatch):
    decided: list[egressapi.EgressRequest] = []
    monkeypatch.setattr(egressapi, "on_decided", lambda r, f: decided.append(r) or "pypi.org is allowed now")
    svc.new(NewSession(name="needs", environment=renv, message="TASK-NEEDS go"), owner="admin", requester="admin")
    s = await xchg.settle(svc, "needs")
    rows = sorted(reqs.list(session_id=s.id), key=lambda r: r.kind)
    assert [(r.kind, r.target, r.state) for r in rows] == [("egress", "pypi.org:443", "pending"),
                                                           ("secret", "STRIPE_KEY", "pending")]
    assert rows[1].why == "to run the payment tests" and rows[1].requester == f"session:{s.id}"
    assert not decided, "filing a request applies nothing"
    t = s.turns[-1]
    assert t.summary == "Did everything the secret doesn't block" and t.outcome == "exited"  # the agent kept going
    assert any("1-deadbeef.json" in w and "not JSON" in w for w in t.warnings)
    assert not list((box(fleet, s) / f".hangar/sessions/{s.name}/out/requests").glob("*"))  # picked up
    events = [e for e in svc.activity.read(s.id, limit=500) if e.data.get("request")]
    assert {e.text for e in events} >= {"the agent asks for the secret STRIPE_KEY: to run the payment tests"}
    assert {"env.request.secret", "env.request.egress"} <= {e.action for e in fleet.store.audit_tail(50)}
    st = json.loads((box(fleet, s) / ".hangar/envstatus.json").read_text())
    assert {r["id"] for r in st["requests"]} == {r.id for r in rows} and st["io"].endswith(f"sessions/{s.name}")

    egress, secret = rows
    r = await reqs.decide(egress.id, True, "ui:sean", note="ok for this session")
    assert r.state == "approved" and r.message == "pypi.org is allowed now" and decided[0].host == "pypi.org"
    assert decided[0].port == 443 and decided[0].requester == f"session:{s.id}"
    with pytest.raises(RequestError, match="already approved"):
        await reqs.decide(egress.id, False, "ui:sean")
    await reqs.decide(secret.id, False, "ui:sean", note="use the Stripe test fixtures instead")
    st = json.loads((box(fleet, s) / ".hangar/envstatus.json").read_text())
    assert {r["state"] for r in st["requests"]} == {"approved", "denied"}
    out = (await fleet.exec(s.sandbox_id, "hangar-agent env requests")).stdout
    assert "use the Stripe test fixtures instead" in out and "pypi.org:443 is approved" in out


async def test_requests_are_picked_up_while_the_turn_runs(svc, fleet, renv, reqs, settings, monkeypatch):
    settings.env_request_poll_seconds = 0.2
    svc.new(NewSession(name="busy", environment=renv, message="TASK-NEEDS SLOW"), owner="admin", requester="admin")
    for _ in range(200):
        s = svc.get("busy")
        if reqs.pending_for(s):
            break
        await asyncio.sleep(0.05)
    assert len(reqs.pending_for(s)) == 2 and s.turns[-1].finished_at is None, "picked up while the turn runs"
    await xchg.settle(svc, "busy")


async def test_only_the_owner_or_an_admin_decides(svc, fleet, reqs):
    from hangar.models import Session

    s = Session(name="alices", environment="xenv", branch="b", person="alice")
    svc._sessions[s.id] = s
    rows = [EnvRequest(kind="egress", environment="xenv", host="example.com", why="w", requester=f"session:{s.id}",
                       session_id=s.id, session=s.name, owner="alice") for _ in range(3)]
    secret = EnvRequest(kind="secret", environment="xenv", name="TOKEN", why="w", requester=f"session:{s.id}",
                        session_id=s.id, session=s.name, owner="alice")
    for r in [*rows, secret]:
        fleet.store.put(r)
    with As(BOB):
        assert reqs.list() == []
        with pytest.raises(KeyError):
            await reqs.decide(rows[0].id, True, "bob")
    with As(ALICE):
        assert len(reqs.list()) == 4
        with pytest.raises(identity.Forbidden):
            await reqs.decide(rows[0].id, True, "alice", scope="environment")
        assert (await reqs.decide(rows[0].id, True, "alice")).scope == "session"
    with As(ADMIN):
        with pytest.raises(RequestError, match="only egress"):
            await reqs.decide(secret.id, True, "ops", scope="environment")
        assert (await reqs.decide(rows[1].id, True, "ops", scope="environment")).scope == "environment"
        with pytest.raises(identity.Forbidden):  # widening is for approvals
            await reqs.decide(rows[2].id, False, "ops", scope="environment")


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_wait_for_turn_puts_a_request_to_the_person_following(svc, fleet, jobs, renv, mode):
    asked: list[str] = []

    async def cb(ctx, params):
        asked.append(params.message)
        if "STRIPE_KEY" in params.message:
            return types.ElicitResult(action="accept", content={"decision": "deny", "note": "use fixtures"})
        return types.ElicitResult(action="decline")

    async with Client(build_mcp(fleet, jobs, svc), mode=mode, elicitation_callback=cb) as c:
        sc(await c.call_tool("new_session", {"name": f"follow-{mode}", "environment": renv,
                                              "message": "TASK-NEEDS go"}))
        t = sc(await c.call_tool("wait_for_turn", {"ref": f"follow-{mode}", "timeout_seconds": 60}))
        assert t["done"]
        if not any("STRIPE_KEY" in a for a in asked):  # picked up as the turn ended: ask on the next call
            t = sc(await c.call_tool("wait_for_turn", {"ref": f"follow-{mode}", "timeout_seconds": 10}))
        assert any("the agent asks for the secret STRIPE_KEY" in a and "never paste its value" in a for a in asked)
        listed = sc(await c.call_tool("list_env_requests", {"ref": f"follow-{mode}", "state": "all"}))
        by = {r["kind"]: r for r in listed}
        assert by["secret"]["state"] == "denied" and by["secret"]["note"] == "use fixtures"
        assert by["egress"]["state"] == "pending"  # declined to answer here: it waits for someone
        r = sc(await c.call_tool("answer_env_request", {"request": by["egress"]["id"], "decision": "approve"}))
        assert r["state"] == "approved" and r["decided_by"].startswith("mcp:")


async def test_jobs_are_prompted_toward_autonomy_and_attach_their_media(fleet, svc, jobs, world, renv):
    job = jobs.submit(SubmitJob(instructions="TASK-SHOT change the page", repo=world["url"], environment=renv,
                                deliver="branch"), Caller("t", "t"))
    done = await _until(jobs, job.id, {JobState.SUCCEEDED, JobState.FAILED})
    assert done.state == JobState.SUCCEEDED, [e.message for e in done.events]
    s = jobs._session(done)
    prompt = next((box(fleet, s) / ".hangar/runs").glob("*.prompt")).read_text()
    assert "Work autonomously" in prompt and "hangar-agent env request secret" in prompt
    body = jobs._pr_body(done)
    assert "Screenshots and demos" in body and "![after.png](" in body and "/api/media/" in body


def test_rest_lists_and_answers_requests(settings):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    with TestClient(create_app(settings)) as c:
        h = c.app.state.hangar
        r = EnvRequest(kind="secret", environment="plain", name="TOKEN", why="w", requester="session:x",
                       session_id="x", session="x", owner=None)
        h.store.put(r)
        auth = {"Authorization": "Bearer t0ken"}
        assert c.get("/api/env-requests").status_code == 401
        [row] = c.get("/api/env-requests", headers=auth).json()
        assert row["id"] == r.id and row["target"] == "TOKEN" and "local" not in row
        got = c.post(f"/api/env-requests/{r.id}", json={"decision": "deny", "note": "no"}, headers=auth).json()
        assert got["state"] == "denied" and got["decided_by"].startswith("ui:") and got["note"] == "no"
        assert c.post(f"/api/env-requests/{r.id}", json={"decision": "approve"}, headers=auth).status_code == 409
        assert c.post("/api/env-requests/rq-nope", json={"decision": "approve"}, headers=auth).status_code == 404
        assert c.get("/api/env-requests?state=", headers=auth).json()[0]["state"] == "denied"
