"""Steer classification (#162): `correction` / `redirect` / `new_info` in the Slack and Jira reply paths and over
REST, recorded as `steer.classified` events, with `hangar.steer.class` and `hangar.seq` on the exported trace."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from hangar import steer_class, traces
from hangar.events import Activity, RunEvent
from hangar.models import CredentialKind, Job, JobState, Session, SessionTurn, Steer, Turn
from tests.test_intake import DANA, fixture
from tests.test_intake import door as door  # noqa: F401 - fixture
from tests.test_intake import post as jira_post
from tests.test_interfaces import _attrs, _spans
from tests.test_jobs import _until, sessions, world  # noqa: F401 - fixtures
from tests.test_slack import command as slack_command
from tests.test_slack import event, settle, sign, until_posted
from tests.test_slack import slack as slack  # noqa: F401 - fixture


# ====================================================================== text
@pytest.mark.parametrize("text,want", [
    ("fix: use the v2 client", ("correction", "use the v2 client")),
    ("Correction:  wrong file", ("correction", "wrong file")),
    ("redirect : do the CLI instead", ("redirect", "do the CLI instead")),
    ("INFO: the API is rate limited", ("new_info", "the API is rate limited")),
    ("fixed it, thanks", (None, "fixed it, thanks")),
    ("please fix: this", (None, "please fix: this")),
])
def test_split_prefix(text, want):
    assert steer_class.split_prefix(text) == want


def test_parse():
    assert [steer_class.parse(w) for w in ("correction", "fix", "redirect", "info", "new_info", "nope", None)] == \
        ["correction", "correction", "redirect", "new_info", "new_info", None, None]


# ====================================================================== Slack
async def _interact(client: httpx.AsyncClient, payload: dict, ts: int | None = None, secret: str | None = None,
                    headers: dict | None = None) -> httpx.Response:
    body = urlencode({"payload": json.dumps(payload)}).encode()
    h = headers or {**(sign(body, ts) if secret is None else sign(body, ts, secret)),
                    "Content-Type": "application/x-www-form-urlencoded"}
    return await client.post("/api/hooks/slack/interactions", content=body, headers=h)


def _click(job_id: str, n: int, cls: str, user: str = "U2") -> dict:
    return {"type": "block_actions", "user": {"id": user, "team_id": "T1"}, "team": {"id": "T1"},
            "response_url": "https://hooks.slack.com/actions/T1/1/xyz", "trigger_id": str(time.time()),
            "actions": [{"action_id": f"hangar_class_{cls}", "block_id": "hangar_steer_class",
                         "value": f"{job_id}|{n}|{cls}", "type": "button"}]}


async def test_slack_prefix_classifies_and_buttons_classify(slack):  # noqa: F811
    svc, fake, client = slack["svc"], slack["fake"], slack["client"]
    await slack_command(client, "add fix.txt")
    await until_posted(fake, lambda: any("Queued as hangar job" in p["text"] for p in fake.posts))
    thread = fake.posts[0]["ts"]
    [job] = svc.list()
    job = await settle(svc, job.id)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]

    # `fix: use the v2 client`: follow-up text without the prefix, classified `correction` on arrival
    slack["world"]["mode"].write_text("followup")
    reply = {"type": "message", "channel": "C1", "user": "U2", "text": "fix: use the v2 client",
             "ts": "1700000100.000001", "thread_ts": thread, "channel_type": "channel"}
    assert (await event(client, reply, "Ev1")).json() == {"ok": True}
    await until_posted(fake, lambda: any("Classified as *Correction*" in t for t in fake.thread(thread)))
    job = svc.get(job.id)
    t = job.turns[1]
    assert t.message == "use the v2 client" and t.steer_class == "correction" and t.classified_by == "slack:T1/U2"
    job = await settle(svc, job.id)

    # an unprefixed reply gets the three buttons, only on hangar's acknowledgement
    reply2 = {**reply, "text": "also handle empty files", "ts": "1700000200.000001"}
    assert (await event(client, reply2, "Ev2")).json() == {"ok": True}
    await until_posted(fake, lambda: any(p.get("blocks") for p in fake.posts))
    [ack] = [p for p in fake.posts if p.get("blocks")]
    blocks = json.loads(ack["blocks"])
    buttons = blocks[-1]["elements"]
    assert [b["text"]["text"] for b in buttons] == ["Correction", "Redirect", "New info"]
    assert buttons[1]["value"] == f"{job.id}|3|redirect" and ack["thread_ts"] == thread
    assert svc.get(job.id).turns[2].steer_class == "unclassified"

    # someone else's click: refused privately, nothing changes
    r = await _interact(client, _click(job.id, 3, "redirect", user="U9"))
    assert r.status_code == 200 and "ignored" in r.json()
    await until_posted(fake, lambda: bool(fake.responses))
    assert "only the person who sent that reply" in fake.responses[-1]["text"]
    assert fake.responses[-1]["response_type"] == "ephemeral" and fake.responses[-1]["replace_original"] is False
    assert svc.get(job.id).turns[2].steer_class == "unclassified"
    # the sender's click classifies it, and records steer.classified in the session's activity
    r = await _interact(client, _click(job.id, 3, "redirect"))
    assert r.json() == {"ok": True, "class": "redirect"}
    t = svc.get(job.id).turns[2]
    assert t.steer_class == "redirect" and t.classified_by == "slack:T1/U2" and t.classified_at
    evs = svc.sessions.activity.read(job.session_id, limit=1000)
    got = [e.data for e in evs if e.data.get("action") == "steer.classified"]
    assert {"class": "redirect", "ref": 3, "by": "slack:T1/U2"}.items() <= got[-1].items()
    assert {"class": "correction", "ref": 2}.items() <= got[0].items()
    await settle(svc, job.id)


async def test_interactions_route_rejects_bad_signatures_stale_and_replays(slack):  # noqa: F811
    svc, client = slack["svc"], slack["client"]
    job = Job(title="t", instructions="x", environment="e", state=JobState.SUCCEEDED,
              turns=[Turn(), Turn(kind="follow_up", message="m", requester="slack:T1/U2")])
    svc._jobs[job.id] = job
    assert (await _interact(client, _click(job.id, 2, "correction"), secret="wrong")).status_code == 401
    assert (await _interact(client, _click(job.id, 2, "correction"), ts=int(time.time()) - 301)).status_code == 401
    body = urlencode({"payload": json.dumps(_click(job.id, 2, "correction"))}).encode()
    headers = {**sign(body), "Content-Type": "application/x-www-form-urlencoded"}
    assert (await _interact(client, {}, headers=headers)).status_code in (400, 401)  # signature is for another body
    first = await client.post("/api/hooks/slack/interactions", content=body, headers=headers)
    assert first.json() == {"ok": True, "class": "correction"}
    replay = await client.post("/api/hooks/slack/interactions", content=body, headers=headers)
    assert replay.json() == {"ignored": "replay"}
    bad = urlencode({"payload": "{not json"}).encode()
    r = await client.post("/api/hooks/slack/interactions", content=bad,
                          headers={**sign(bad), "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400
    assert svc.get(job.id).turns[1].steer_class == "correction"


# ====================================================================== Jira
async def test_jira_class_command_classifies_the_senders_last_unclassified_follow_up(door):  # noqa: F811
    svc, client = door["svc"], door["client"]
    me, other = f"jira:{DANA}", "jira:someone-else"
    job = Job(title="t", instructions="x", environment="base", state=JobState.SUCCEEDED, source="jira",
              source_ref="jira:ABC-12", person="dana",
              turns=[Turn(), Turn(kind="follow_up", message="a", requester=me),
                     Turn(kind="follow_up", message="b", requester=me),
                     Turn(kind="follow_up", message="c", requester=other),
                     Turn(kind="follow_up", message="d", requester=me, trigger="verify")])
    svc._jobs[job.id] = job
    svc._save(job)
    comment = fixture("jira_comment_created.json")
    comment["comment"]["body"] = "/hangar class redirect"
    r = await jira_post(client, comment)
    assert r.json() == {"ok": True, "classified": job.id, "turn": 3, "class": "redirect"}, r.json()
    assert [t.steer_class for t in svc.get(job.id).turns] == ["unclassified", "unclassified", "redirect",
                                                              "unclassified", "unclassified"]
    comment["comment"]["body"], comment["timestamp"] = "/hangar class fix", comment["timestamp"] + 1
    assert (await jira_post(client, comment)).json()["turn"] == 2
    comment["comment"]["body"], comment["timestamp"] = "/hangar class info", comment["timestamp"] + 1
    assert (await jira_post(client, comment)).json() == {"ignored": "nothing to classify"}
    comment["comment"]["body"], comment["timestamp"] = "/hangar class maybe", comment["timestamp"] + 1
    assert (await jira_post(client, comment)).json() == {"ignored": "no class given"}
    assert svc.get(job.id).turns[2].classified_by == me


async def test_jira_reply_prefix_is_stripped_and_classified(door, monkeypatch):  # noqa: F811
    svc = door["svc"]
    job = Job(title="t", instructions="x", environment="base", state=JobState.NEEDS_INPUT, source="jira",
              source_ref="jira:ABC-12", person="dana", turns=[Turn()])
    svc._jobs[job.id] = job
    seen = {}

    def follow_up(job_id, message, requester, reply_to=None, front_door=False, sender_person=None,
                  steer_class=None):
        seen.update(message=message, steer_class=steer_class)
        job.turns.append(Turn(kind="follow_up", message=message, requester=requester,
                              steer_class=steer_class or "new_info"))
        return job

    monkeypatch.setattr(svc, "follow_up", follow_up)
    res = door["front"]._follow_up(job, "info: the bucket is in eu-west-1", f"jira:{DANA}", "jira:ABC-12", "dana")
    assert res == {"ok": True, "follow_up": job.id}
    assert seen == {"message": "the bucket is in eu-west-1", "steer_class": "new_info"}


# ====================================================================== the job service and REST
def test_an_answer_to_a_question_defaults_to_new_info_and_others_stay_unclassified():
    t = Turn(kind="follow_up", message="x")
    assert t.steer_class == "unclassified" and Steer(text="x").steer_class == "unclassified"
    steer_class.mark(t, "new_info", "hangar")
    assert (t.steer_class, t.classified_by) == ("new_info", "hangar") and t.classified_at


def test_classify_turn_rules():
    saved = []
    svc = SimpleNamespace(_save=saved.append, sessions=None, _session=lambda j: None)
    job = Job(title="t", instructions="x", environment="e",
              turns=[Turn(), Turn(kind="follow_up", message="m", requester="alice"),
                     Turn(kind="follow_up", message="fix CI", trigger="autofix:ci")])
    with pytest.raises(steer_class.ClassifyError):
        steer_class.classify_turn(svc, job, 1, "correction", "alice")  # the initial turn
    with pytest.raises(steer_class.ClassifyError):
        steer_class.classify_turn(svc, job, 3, "correction", "alice")  # hangar started it
    with pytest.raises(KeyError):
        steer_class.classify_turn(svc, job, 9, "correction", "alice")
    with pytest.raises(steer_class.ClassifyError):
        steer_class.classify_turn(svc, job, 2, "oops", "alice")
    with pytest.raises(steer_class.ClassifyError):
        steer_class.classify_turn(svc, job, 2, "redirect", "bob", sender="bob")
    assert steer_class.classify_turn(svc, job, 2, "fix", "alice").steer_class == "correction" and saved


async def test_rest_routes(fleet, sessions, settings):  # noqa: F811
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from hangar.jobs import JobService

    svc = JobService(fleet, sessions, transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    s = Session(name="work", environment="plain", turns=[SessionTurn(message="go", steers=[
        Steer(id="str-1", text="stop that", kind="interrupt")])])
    sessions._sessions[s.id] = s
    job = Job(title="t", instructions="x", environment="e", session_id=s.id,
              turns=[Turn(), Turn(kind="follow_up", message="m", requester="tok-a")])
    s.job_id = job.id
    svc._jobs[job.id] = job
    app = FastAPI()
    h = SimpleNamespace(jobs=svc, sessions=sessions)

    def job_session(job_id, caller):
        return s

    caller = SimpleNamespace(requester="tok-a")
    steer_class.mount(app, h, Depends(lambda: "admin"), Depends(lambda: caller), job_session)
    c = TestClient(app)
    r = c.post(f"/api/jobs/{job.id}/turns/2/class", json={"class": "new_info"})
    assert r.status_code == 200 and r.json()["class"] == "new_info" and r.json()["classified_by"] == "tok-a"
    assert c.post(f"/api/jobs/{job.id}/turns/1/class", json={"class": "redirect"}).status_code == 400
    r = c.post("/api/sessions/work/steers/str-1/class", json={"class": "correction"})
    assert r.status_code == 200 and s.turns[0].steers[0].steer_class == "correction"
    evs = sessions.activity.read(s.id)
    assert [(e.data["class"], e.data["ref"]) for e in evs if e.data.get("action") == "steer.classified"] == \
        [("new_info", 2), ("correction", "str-1")]


# ====================================================================== traces
def test_trace_export_carries_class_and_seq(settings, broker):
    broker.put("seeded", "webhook", CredentialKind.GENERIC, "x" * 20)
    activity = Activity(settings, broker.redact)
    svc = SimpleNamespace(activity=activity, fleet=SimpleNamespace(broker=broker))
    st1 = Steer(id="str-1", text="use v2", requester="alice", ts=1030.0, status="delivered")
    st2 = Steer(id="str-2", text="stop", kind="interrupt", requester="bob", ts=1040.0, status="delivered")
    t = SessionTurn(message="go", run_id="run-1", harness="claude-code", started_at=1000.0, finished_at=1100.0,
                    outcome="exited", exit_code=0, live=True, steers=[st1, st2])
    s = Session(name="s1", environment="plain", harness="claude-code", turns=[t])
    [e1, e2] = activity.append(s.id, [
        RunEvent(turn=1, run_id="run-1", kind="progress", text="steer from alice", ts=1030.0,
                 data={"source": "hangar", "action": "steer.received", "ref": "str-1"}),
        RunEvent(turn=1, run_id="run-1", kind="progress", text="interrupt from bob", ts=1040.0,
                 data={"source": "hangar", "action": "interrupt.received", "ref": "str-2"})])
    st1.seq, st2.seq = e1.seq, e2.seq
    steer_class.mark(st1, "correction", "alice")
    activity.append(s.id, [RunEvent(turn=1, run_id="run-1", kind="progress", text="steer classified", ts=1050.0,
                                    data={"source": "hangar", "action": "steer.classified", "class": "correction",
                                          "ref": "str-1", "by": "alice"})])
    x = traces.session_export(svc, s, turn=1)
    turn = next(sp for sp in _spans(x) if _attrs(sp)["hangar.kind"] == "turn")
    evs = [(e["name"], _attrs(e)) for e in turn["events"]]
    assert [n for n, _ in evs] == ["steer", "interrupt", "steer.classified"]  # the received events aren't repeated
    assert evs[0][1]["hangar.steer.class"] == "correction" and int(evs[0][1]["hangar.seq"]) == e1.seq
    assert evs[1][1]["hangar.steer.class"] == "unclassified" and int(evs[1][1]["hangar.seq"]) == e2.seq
    assert evs[2][1]["hangar.steer.ref"] == "str-1" and evs[2][1]["hangar.steer.class"] == "correction"
    # a run's span takes its job turn's class (the outbox's per-run trace)
    jt = Turn(kind="follow_up", message="m", requester="alice", steer_class="redirect")
    x = traces.session_export(svc, s, turn=1, extra=steer_class.turn_attrs(jt))
    turn = next(sp for sp in _spans(x) if _attrs(sp)["hangar.kind"] == "turn")
    assert _attrs(turn)["hangar.steer.class"] == "redirect"
    assert steer_class.turn_attrs(Turn()) == {} and steer_class.turn_attrs(Turn(kind="follow_up", trigger="v")) == {}
    assert steer_class.turn_attrs(Turn(kind="follow_up"))["hangar.steer.class"] == "unclassified"


async def test_job_export_puts_the_class_on_follow_up_turn_spans(fleet, sessions, settings):  # noqa: F811
    from hangar.jobs import JobService

    svc = JobService(fleet, sessions, transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    s = Session(name="j1", environment="plain", harness="codex", turns=[
        SessionTurn(message="go", run_id="run-a", started_at=10.0, finished_at=20.0),
        SessionTurn(message="more", run_id="run-b", started_at=30.0, finished_at=40.0)])
    job = Job(title="t", instructions="x", environment="plain", session_id=s.id, turns=[
        Turn(run_id="run-a"), Turn(kind="follow_up", message="more", run_id="run-b", steer_class="correction")])
    s.job_id = job.id
    x = traces.job_export(svc, job, sessions=[s])
    turns = sorted((sp for sp in _spans(x) if _attrs(sp)["hangar.kind"] == "turn"),
                   key=lambda sp: int(sp["startTimeUnixNano"]))
    assert "hangar.steer.class" not in _attrs(turns[0]) and _attrs(turns[1])["hangar.steer.class"] == "correction"


async def test_steers_and_interrupts_record_their_seq(fleet, sessions, monkeypatch):  # noqa: F811
    from hangar import steer as steering

    s = Session(name="live", environment="plain", turns=[SessionTurn(message="go", run_id="run-1", live=True)])
    sessions._sessions[s.id] = s
    st = Steer(text="look at x", requester="alice")
    s.turns[-1].steers.append(st)
    steering.received(sessions, s, st)
    [ev] = sessions.activity.read(s.id)
    assert st.seq == ev.seq and ev.data == {"source": "hangar", "action": "steer.received", "ref": st.id}
    assert ev.run_id == "run-1"
