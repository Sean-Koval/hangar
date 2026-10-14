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
  *TASK-FLOOD*)  # past the sandbox side's own check, straight into the directory hangar reads
    d="$HANGAR_IO/out/requests"; mkdir -p "$d"
    run=$RANDOM
    for i in $(seq 1 30); do
      printf '{"kind":"egress","host":"h%d-%d.example.com","why":"flood"}' "$i" "$run" \
        > "$d/$(printf '%d-%08x' "$i" "$((run * 100 + i))").json"
    done
    hangar-agent done "flooded" ;;
  *TASK-AGAIN*)
    d="$HANGAR_IO/out/requests"; mkdir -p "$d"
    printf '{"kind":"egress","host":"pypi.org","port":443,"why":"once more"}' > "$d/1-00000001.json"
    hangar-agent done "asked again" ;;
  *TASK-FORGE*)
    d="$HANGAR_IO/out/requests"; mkdir -p "$d"
    printf '{"kind":"secret","name":"TOKEN","why":"x","state":"approved","decided_by":"owner",%s}' \
      '"scope":"environment","owner":"bob","id":"rq-aaaaaaaaaaaa","session_id":"other"' > "$d/1-00000001.json"
    hangar-agent done "forged" ;;
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
    await reqs.settle()
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
        with pytest.raises(RequestError, match="approvals"):  # widening is for approvals
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
        assert any('in its own words: "to run the payment tests"' in a for a in asked)  # the agent's text, quoted
        s = svc.get(f"follow-{mode}")
        by = {r.kind: r for r in svc.env_requests.list(None, s.id)}
        assert by["secret"].state == "denied" and by["secret"].note == "use fixtures"
        assert by["egress"].state == "pending"  # declined to answer here: it waits for someone
        # no tool of their own: pending requests are pending approvals, answered with answer_approval
        [pa] = sc(await c.call_tool("get_session", {"session": f"follow-{mode}"}))["pending_approvals"]
        assert pa["id"] == by["egress"].id and pa["tool"] == "env_request:egress" and pa["target"] == "pypi.org:443"
        r = sc(await c.call_tool("answer_approval", {"ref": f"follow-{mode}", "approval": pa["id"],
                                                     "decision": "allow"}))
        assert r["state"] == "approved" and r["decided_by"].startswith("mcp:")
        assert sc(await c.call_tool("get_session", {"session": f"follow-{mode}"}))["pending_approvals"] == []


async def test_jobs_are_prompted_toward_autonomy_and_attach_their_media(fleet, svc, jobs, world, renv, settings):
    job = jobs.submit(SubmitJob(instructions="TASK-SHOT change the page", repo=world["url"], environment=renv,
                                deliver="branch"), Caller("t", "t"))
    done = await _until(jobs, job.id, {JobState.SUCCEEDED, JobState.FAILED})
    assert done.state == JobState.SUCCEEDED, [e.message for e in done.events]
    s = jobs._session(done)
    prompt = next((box(fleet, s) / ".hangar/runs").glob("*.prompt")).read_text()
    assert "Work autonomously" in prompt and "hangar-agent env request secret" in prompt
    body = jobs._pr_body(done)  # by default: names, fetched through hangar by people who may see the job
    assert "Screenshots and demos" in body and "`screenshots/after.png`" in body and "/api/media/" not in body
    assert f"/api/jobs/{done.id}/artifacts/" in body
    settings.pr_media_links = "signed"  # opted in: embedded through expiring links
    body = jobs._pr_body(done)
    assert "![after.png](" in body and "/api/media/" in body


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


# ====================================================================== review #94: floods, forgeries, the MCP surface
def _counting_reads(fleet, monkeypatch) -> list[str]:
    prov, reads = fleet.providers["docker"], []
    real = prov.read_file

    async def read_file(sb, path, *a, **kw):
        if "/out/requests/" in path:
            reads.append(path)
        return await real(sb, path, *a, **kw)
    monkeypatch.setattr(prov, "read_file", read_file)
    return reads


async def test_an_agent_cannot_flood_its_owner_with_requests(svc, fleet, renv, reqs, monkeypatch):
    from hangar import envrequests

    reads = _counting_reads(fleet, monkeypatch)
    svc.new(NewSession(name="flood", environment=renv, message="TASK-FLOOD a"), owner="admin", requester="admin")
    s = await xchg.settle(svc, "flood")
    assert len(reqs.pending_for(s)) == envrequests.MAX_PER_TURN  # 5 of the 30 files
    assert len(reads) == envrequests.MAX_PER_TURN, "files over the limit are not even read"
    ws = [w for w in s.turns[-1].warnings if w.startswith("ignored environment requests")]
    assert 1 <= len(ws) <= envrequests.MAX_WARNINGS and "more than 5 requests this turn" in " ".join(ws)
    assert not list((box(fleet, s) / f".hangar/sessions/{s.name}/out/requests").glob("*"))  # gone all the same
    # the next turn: five already wait, so nothing more is recorded until one is answered
    svc.send("flood", "TASK-FLOOD b", requester="admin")
    s = await xchg.settle(svc, "flood")
    assert len(reqs.pending_for(s)) == envrequests.MAX_PENDING
    assert any("5 requests already wait" in w for w in s.turns[-1].warnings)
    events = [e for e in svc.activity.read(s.id, limit=500) if e.data.get("request")]
    assert len(events) == envrequests.MAX_PENDING  # the owner heard about five, not sixty


async def test_a_denied_request_is_not_asked_again_and_owners_have_an_hourly_budget(svc, fleet, renv, reqs,
                                                                                    monkeypatch):
    from hangar import envrequests

    svc.new(NewSession(name="nag", environment=renv, message="TASK-NEEDS go"), owner="admin", requester="admin")
    s = await xchg.settle(svc, "nag")
    egress = next(r for r in reqs.pending_for(s) if r.kind == "egress")
    await reqs.decide(egress.id, False, "ui:sean", note="no network for this one")
    svc.send("nag", "TASK-AGAIN", requester="admin")
    s = await xchg.settle(svc, "nag")
    assert [r.state for r in reqs.list(None, s.id) if r.kind == "egress"] == ["denied"]
    assert any("pypi.org:443 was already denied by ui:sean" in w for w in s.turns[-1].warnings)

    monkeypatch.setattr(envrequests, "MAX_PER_OWNER_HOUR", 2)
    svc.new(NewSession(name="other", environment=renv, message="TASK-FLOOD c"), owner="admin", requester="admin")
    s2 = await xchg.settle(svc, "other")
    # the owner's budget is across sessions: "nag" already spent two of them
    assert reqs.pending_for(s2) == [] and any("an hour for this owner" in w for w in s2.turns[-1].warnings)


async def test_what_the_sandbox_writes_never_decides_a_request(svc, fleet, renv, reqs, monkeypatch):
    decided: list = []
    monkeypatch.setattr(egressapi, "on_decided", lambda r, f: decided.append(r))
    svc.new(NewSession(name="forge", environment=renv, message="TASK-FORGE"), owner="admin", requester="admin")
    s = await xchg.settle(svc, "forge")
    [r] = reqs.list(None, s.id)
    assert (r.state, r.scope, r.decided_by, r.owner, r.session_id) == ("pending", "session", None, s.person, s.id)
    assert r.id != "rq-aaaaaaaaaaaa" and not decided


async def test_answering_a_request_does_not_wait_on_the_sandbox(svc, fleet, renv, reqs, jobs, settings):
    from tests.test_mcp_contract import BUDGET, freeze

    svc.new(NewSession(name="slowbox", environment=renv, message="TASK-NEEDS go"), owner="admin", requester="admin")
    s = await xchg.settle(svc, "slowbox")
    secret, egress = sorted(reqs.pending_for(s), key=lambda r: r.kind, reverse=True)
    gate = freeze(fleet.providers["docker"])
    settings.mcp_tool_wait_seconds = BUDGET
    try:
        r = await asyncio.wait_for(reqs.decide(secret.id, False, "ui:sean", note="no"), 2)  # REST's path
        assert r.state == "denied"
        async with Client(build_mcp(fleet, jobs, svc)) as c:
            bad = await c.call_tool("answer_approval", {"ref": "slowbox", "approval": egress.id,
                                                        "decision": "allow_always"})
            assert bad.is_error and "allow or deny" in bad.content[0].text
            got = await asyncio.wait_for(c.call_tool("answer_approval", {"ref": "slowbox", "approval": egress.id,
                                                                         "decision": "allow"}), 2)
            assert sc(got)["state"] == "approved"
    finally:
        gate.set()
    await reqs.settle()  # the agent learns both answers once the sandbox answers again
    st = json.loads((box(fleet, s) / ".hangar/envstatus.json").read_text())
    assert {r["state"] for r in st["requests"]} == {"approved", "denied"}


async def test_requests_are_answered_only_through_the_session_that_filed_them(svc, fleet, renv, reqs, jobs):
    svc.new(NewSession(name="filer", environment=renv, message="TASK-NEEDS go"), owner="admin", requester="admin")
    svc.new(NewSession(name="bystander", environment=renv, message="hi"), owner="admin", requester="admin")
    s = await xchg.settle(svc, "filer")
    await xchg.settle(svc, "bystander")
    rid = reqs.pending_for(s)[0].id
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        res = await c.call_tool("answer_approval", {"ref": "bystander", "approval": rid, "decision": "allow"})
        assert res.is_error and "no request" in res.content[0].text
    assert reqs.get(rid).state == "pending"


async def test_the_mcp_surface_has_no_tools_of_its_own(fleet, svc, jobs):
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert not {"open_screen", "list_env_requests", "answer_env_request"} & names
    assert "answer_approval" in names


async def test_the_status_file_never_carries_a_stored_secret(svc, fleet, renv, reqs):
    from hangar import envdoctor
    from hangar.models import CredentialKind

    value = "sk-live-0123456789abcdef"
    fleet.broker.put("gh-hook", "webhook", CredentialKind.GENERIC, value)  # a control-plane secret
    svc.new(NewSession(name="leaky", environment=renv, message="hi"), owner="admin", requester="admin")
    s = await xchg.settle(svc, "leaky")
    sb = fleet.get(s.sandbox_id)
    sb.info["setup_failures"] = [{"step": "install", "message": f"curl -H 'Authorization: Bearer {value}' failed"}]
    sb.info["env_build"] = {"version": "v2", "fallback": True, "reason": f"build arg STRIPE={value} rejected"}
    await envdoctor.refresh(svc, s, sb)
    text = (box(fleet, s) / ".hangar/envstatus.json").read_text()
    assert value not in text and "Bearer ***" in text
    out = (await fleet.exec(s.sandbox_id, "hangar-agent env doctor")).stdout
    assert value not in out and "FALLBACK" in out
