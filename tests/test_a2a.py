"""A2A (spec 0011 §3 G): hangar as an A2A agent, on the fake provider with test_jobs' scripted agent.

The official `a2a-sdk` client (a dev dependency) drives the 1.0 binding and, through its 0.3 compatibility
transport, the 0.3 wire format, so these tests prove interoperability rather than agreement with ourselves.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from hangar.a2a import (
    CANCELED,
    COMPLETED,
    INPUT_REQUIRED,
    SUBMITTED,
    WORKING,
    A2AServer,
    task_state,
)
from hangar.auth import TokenAuth
from hangar.models import JobEvent, JobState
from hangar.vault import Vault
from tests.test_jobs import DONE, _gh_settings, _until, jobs, sessions, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

BASE = "http://hangar.test"
ADMIN = "adm-t0ken"


def _http(app: FastAPI, token: str | None = ADMIN) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, headers=headers, timeout=60)


@pytest.fixture
async def a2a(jobs, world, settings, store):  # noqa: F811 - the fixtures imported above
    settings.public_url = BASE
    settings.a2a_poll_seconds = 0.05
    settings.github_repos = {world["url"]: "jobenv"}
    auth = TokenAuth(store, ADMIN)
    svc_token, _ = auth.create("svc", ["jobs"])
    server = A2AServer(jobs, auth, Vault(store, b"0" * 43 + b"="), settings)
    app = FastAPI()
    server.mount(app)
    await server.start()
    http = _http(app)
    yield SimpleNamespace(server=server, app=app, http=http, jobs=jobs, world=world, svc=svc_token,
                          meta={"repo": world["url"], "environment": "jobenv"})
    await http.aclose()
    await server.stop()


async def rpc(http: httpx.AsyncClient, method: str, params: dict | None = None, version: str | None = "1.0",
              **headers: str) -> dict:
    if version:
        headers["A2A-Version"] = version
    r = await http.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                        headers=headers)
    return r.json()


def msg(text: str, **kw) -> dict:
    return {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": text}], **kw}


async def _card(a2a) -> dict:
    r = await a2a.http.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    return r.json()


def _sdk_client(a2a, card: dict, streaming: bool = False):
    from a2a.client import ClientConfig, ClientFactory
    from a2a.client.card_resolver import parse_agent_card

    return ClientFactory(ClientConfig(httpx_client=_http(a2a.app), streaming=streaming)).create(
        parse_agent_card(copy.deepcopy(card)))


def _sdk_message(text: str, **kw):
    from a2a.types import Message, Part, Role

    return Message(message_id=uuid.uuid4().hex, role=Role.ROLE_USER, parts=[Part(text=text)], **kw)


def _struct(d: dict):
    from google.protobuf.struct_pb2 import Struct

    s = Struct()
    s.update(d)
    return s


# ---------------------------------------------------------------------------- the agent card
async def test_agent_card_validates_against_the_spec_schema(a2a):
    pytest.importorskip("a2a")
    from a2a.client.card_resolver import parse_agent_card
    from a2a.compat.v0_3 import types as v03
    from a2a.types import AgentCard
    from google.protobuf.json_format import ParseDict

    r = await _http(a2a.app, token=None).get("/.well-known/agent-card.json")  # public: discovery needs no token
    assert r.status_code == 200 and r.headers["etag"] and "max-age" in r.headers["cache-control"]
    card = r.json()
    # The 1.0 part of the card is exactly the 1.0 schema (strict: no unknown fields once the 0.3 extras are gone).
    strict = {k: v for k, v in card.items() if k not in ("url", "protocolVersion", "preferredTransport", "security")}
    strict["securitySchemes"] = {"bearer": {"httpAuthSecurityScheme": card["securitySchemes"]["bearer"][
        "httpAuthSecurityScheme"]}}
    ParseDict(strict, AgentCard())
    for field in ("name", "description", "supportedInterfaces", "version", "capabilities", "defaultInputModes",
                  "defaultOutputModes", "skills"):  # REQUIRED in a2a.proto
        assert card[field], field
    c = parse_agent_card(copy.deepcopy(card))  # what SDK clients (and ADK) do with it
    assert [s.id for s in c.skills] == ["code_change", "answer_question_about_repo"]
    assert all(s.name and s.description and s.tags for s in c.skills)
    assert c.capabilities.streaming and c.capabilities.push_notifications
    assert [(i.url, i.protocol_binding, i.protocol_version) for i in c.supported_interfaces] == [
        (f"{BASE}/a2a", "JSONRPC", "1.0"), (f"{BASE}/a2a", "JSONRPC", "0.3")]
    assert c.security_schemes["bearer"].http_auth_security_scheme.scheme.lower() == "bearer"
    old = v03.AgentCard.model_validate(card)  # and 0.3 clients
    assert old.url == f"{BASE}/a2a" and old.security_schemes["bearer"].root.scheme == "bearer"


async def test_wired_into_the_app(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    with TestClient(create_app(settings)) as c:
        assert c.get("/.well-known/agent-card.json").json()["name"] == "hangar"
        r = c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}})
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
        assert c.get("/api/connect", headers={"Authorization": "Bearer t0ken"}).json()["a2a_agent_card"]


# ---------------------------------------------------------------------------- send, get, follow up
async def test_sdk_client_gets_the_work_back_and_follows_up_in_the_same_context(a2a, broker):
    pytest.importorskip("a2a")
    from a2a.types import GetTaskRequest, SendMessageRequest, TaskState

    client = _sdk_client(a2a, await _card(a2a))
    req = SendMessageRequest(message=_sdk_message("Add fix.txt"),
                             metadata=_struct({**a2a.meta, "verify": "test -f fix.txt"}))
    [resp] = [r async for r in client.send_message(req)]  # blocking: comes back when the job is done
    task = resp.task
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, task.status
    job = a2a.jobs.get(task.id)
    assert job.source == "a2a" and task.context_id == job.id and job.verify == "test -f fix.txt"
    arts = {a.name: a for a in task.artifacts}
    assert arts["Summary"].parts[0].text == "Added fix.txt"
    assert arts["Branch"].parts[1].data.struct_value["branch"] == job.branch
    assert "fix.txt" in arts["Diffstat"].parts[0].text

    # Opened as a PR on GitHub (the fake GitHub from test_jobs), the PR link is an artifact.
    _gh_settings(a2a.jobs, broker)
    job.repo = "https://github.com/o/r.git"
    await a2a.jobs._ensure_pr(job, draft=True)
    got = await client.get_task(GetTaskRequest(id=task.id))
    pr = next(a for a in got.artifacts if a.name == "Pull request")
    assert pr.parts[0].text == "Pull request: https://github.com/o/r/pull/7"
    assert pr.parts[1].data.struct_value["prUrl"] == "https://github.com/o/r/pull/7"

    # A second message in the same context is a follow-up turn on the same job and branch.
    a2a.world["mode"].write_text("followup")
    req2 = SendMessageRequest(message=_sdk_message("also do Y", context_id=task.context_id))
    [resp2] = [r async for r in client.send_message(req2)]
    assert resp2.task.id == task.id and resp2.task.status.state == TaskState.TASK_STATE_COMPLETED
    assert len(job.turns) == 2 and job.turns[1].summary == "did Y" and len(job.result.commits) == 2
    asked = [m.parts[0].text for m in resp2.task.history if m.role == 1]  # ROLE_USER
    assert asked[0].startswith("Add fix.txt") and asked[1] == "also do Y"
    # a retried message (the first, or the follow-up) is the same job, not a second job or a third turn
    [again] = [r async for r in client.send_message(req)]
    [again2] = [r async for r in client.send_message(req2)]
    assert again.task.id == again2.task.id == task.id and len(job.turns) == 2
    # history can be trimmed
    assert len((await client.get_task(GetTaskRequest(id=task.id, history_length=1))).history) == 1


async def test_streaming_emits_status_updates_from_job_events(a2a):
    pytest.importorskip("a2a")
    from a2a.types import SendMessageRequest, TaskState

    client = _sdk_client(a2a, await _card(a2a), streaming=True)
    req = SendMessageRequest(message=_sdk_message("Add fix.txt"), metadata=_struct(a2a.meta))
    events = [e async for e in client.send_message(req)]
    kinds = [e.WhichOneof("payload") for e in events]
    assert kinds[0] == "task" and kinds[-1] == "status_update" and "artifact_update" in kinds
    assert events[-1].status_update.status.state == TaskState.TASK_STATE_COMPLETED
    updates = [e.status_update for e in events if e.WhichOneof("payload") == "status_update"]
    working = [u.status.message.parts[0].text for u in updates if u.status.state == TaskState.TASK_STATE_WORKING]
    assert any("agent turn 1 starting" in t for t in working), working
    arts = [e.artifact_update.artifact.name for e in events if e.WhichOneof("payload") == "artifact_update"]
    assert "Summary" in arts and kinds.index("artifact_update") < len(kinds) - 1  # artifacts before the outcome


async def test_v03_clients_on_the_same_endpoint(a2a):
    pytest.importorskip("a2a")
    from a2a.types import SendMessageRequest, TaskState

    # The SDK's 0.3 transport (message/send, kind discriminators) when the card offers only 0.3.
    card = await _card(a2a)
    card["supportedInterfaces"] = card["supportedInterfaces"][1:]
    client = _sdk_client(a2a, card)
    req = SendMessageRequest(message=_sdk_message("Add fix.txt"), metadata=_struct(a2a.meta))
    [resp] = [r async for r in client.send_message(req)]
    assert resp.task.status.state == TaskState.TASK_STATE_COMPLETED
    assert any(a.name == "Summary" for a in resp.task.artifacts)

    # A bare 0.3 client: no A2A-Version header, message/stream over SSE.
    params = {"message": {"kind": "message", "messageId": "m-03", "role": "user",
                          "parts": [{"kind": "text", "text": "Add fix.txt"},
                                    {"kind": "file", "file": {"name": "trace.txt", "bytes": "VHJhY2ViYWNr"}}]},
              "metadata": a2a.meta}
    r = await a2a.http.post("/a2a", json={"jsonrpc": "2.0", "id": 7, "method": "message/stream", "params": params})
    assert r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:])["result"] for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[0]["kind"] == "task" and events[0]["status"]["state"] in ("submitted", "working")
    assert {e["kind"] for e in events[1:]} == {"status-update", "artifact-update"}
    assert events[-1]["final"] is True and events[-1]["status"]["state"] == "completed"
    assert all(not e["final"] for e in events[1:-1] if e["kind"] == "status-update")
    job = a2a.jobs.get(events[0]["id"])
    assert [(c.name, c.kind) for c in job.context] == [("trace.txt", "file")]
    got = (await rpc(a2a.http, "tasks/get", {"id": job.id}, version=None))["result"]
    assert got["kind"] == "task" and got["status"]["state"] == "completed"
    assert got["artifacts"][0]["parts"][0] == {"kind": "text", "text": "Added fix.txt"}


# ---------------------------------------------------------------------------- cancel and resubscribe
async def test_cancel_and_resubscribe(a2a):
    a2a.world["mode"].write_text("sleep")
    res = await rpc(a2a.http, "SendMessage", {"message": msg("x"), "metadata": a2a.meta,
                                               "configuration": {"returnImmediately": True}})
    task = res["result"]["task"]
    assert task["status"]["state"] == SUBMITTED
    await _until(a2a.jobs, task["id"], {JobState.RUNNING})
    while not a2a.jobs.get(task["id"]).turns[0].run_id:
        await asyncio.sleep(0.05)

    async def subscribe():
        r = await a2a.http.post("/a2a", headers={"A2A-Version": "1.0"}, json={
            "jsonrpc": "2.0", "id": 2, "method": "SubscribeToTask", "params": {"id": task["id"]}})
        return [json.loads(line[6:])["result"] for line in r.text.splitlines() if line.startswith("data: ")]

    sub = asyncio.create_task(subscribe())
    await asyncio.sleep(0.2)
    res = await rpc(a2a.http, "CancelTask", {"id": task["id"]})
    assert res["result"]["status"]["state"] == CANCELED, res
    events = await asyncio.wait_for(sub, 10)
    assert events[0]["task"]["status"]["state"] == WORKING
    assert events[-1]["statusUpdate"]["status"]["state"] == CANCELED
    assert (await rpc(a2a.http, "CancelTask", {"id": task["id"]}))["error"]["code"] == -32002
    assert (await rpc(a2a.http, "SubscribeToTask", {"id": task["id"]}))["error"]["code"] == -32004
    assert (await rpc(a2a.http, "GetTask", {"id": task["id"]}))["result"]["status"]["state"] == CANCELED


# ---------------------------------------------------------------------------- auth
async def test_auth_and_jobs_token_fence(a2a):
    anon = _http(a2a.app, token=None)
    r = await anon.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}})
    assert r.status_code == 401 and r.headers["www-authenticate"] == 'Bearer realm="hangar"'
    r = await anon.post("/a2a", headers={"Authorization": "Bearer nope"}, json={})
    assert r.status_code == 401
    other, _ = a2a.server.auth.create("odd", ["something"])
    r = await anon.post("/a2a", headers={"Authorization": f"Bearer {other}"}, json={})
    assert r.status_code == 403

    svc = _http(a2a.app, token=a2a.svc)
    err = (await rpc(svc, "SendMessage", {"message": msg("x"), "metadata": {"environment": "jobenv"}}))["error"]
    assert err["code"] == -32602 and "HANGAR_GITHUB_REPOS" in err["message"]
    err = (await rpc(svc, "SendMessage", {"message": msg("x"), "metadata": {**a2a.meta, "environment": "plain"}}))
    assert "not allowed" in err["error"]["message"]
    mine = (await rpc(svc, "SendMessage", {"message": msg("Add fix.txt"), "metadata": {"repo": a2a.world["url"]},
                                           "configuration": {"returnImmediately": True}}))["result"]["task"]
    assert a2a.jobs.get(mine["id"]).owner != "admin"
    theirs = (await rpc(a2a.http, "SendMessage", {"message": msg("Add fix.txt"), "metadata": a2a.meta,
                                                  "configuration": {"returnImmediately": True}}))["result"]["task"]
    assert (await rpc(svc, "GetTask", {"id": theirs["id"]}))["error"]["code"] == -32001  # not theirs: not found
    assert (await rpc(a2a.http, "GetTask", {"id": mine["id"]}))["result"]["id"] == mine["id"]  # admin sees all
    listed = (await rpc(svc, "ListTasks", {}))["result"]
    assert [t["id"] for t in listed["tasks"]] == [mine["id"]] and listed["nextPageToken"] == ""
    assert "artifacts" not in listed["tasks"][0]
    # a contextId naming someone else's job doesn't reach it: it's just a new context for this caller
    res = (await rpc(svc, "SendMessage", {"message": msg("x", contextId=theirs["id"]),
                                          "metadata": {"repo": a2a.world["url"]},
                                          "configuration": {"returnImmediately": True}}))["result"]["task"]
    assert res["id"] not in (mine["id"], theirs["id"]) and res["contextId"] == theirs["id"]
    for t in (mine, theirs, res):
        await _until(a2a.jobs, t["id"], DONE)


# ---------------------------------------------------------------------------- mapping and validation
async def test_parts_metadata_and_errors(a2a):
    h = a2a.http
    bad = await h.post("/a2a", content=b"{nope")
    assert bad.json()["error"]["code"] == -32700
    assert (await h.post("/a2a", json=[])).json()["error"]["code"] == -32600
    assert (await rpc(h, "Nope"))["error"]["code"] == -32601
    assert (await rpc(h, "message/send", version="1.0"))["error"]["code"] == -32601  # 0.3 name under 1.0
    assert (await rpc(h, "GetTask", {"id": "x"}, version="2.0"))["error"]["code"] == -32009
    assert (await rpc(h, "GetTask", {"id": "job-nope"}))["error"]["code"] == -32001
    assert (await rpc(h, "GetExtendedAgentCard"))["error"]["code"] == -32004
    for m in (msg("x", role="ROLE_AGENT"), {**msg("x"), "parts": []}, {**msg("x"), "parts": [{"text": 1}]},
              {**msg("x"), "parts": [{"text": "a", "url": "b"}]}, {**msg("x"), "messageId": ""},
              {**msg("x"), "parts": [{"raw": "AAAA"}]}):  # no instructions
        err = (await rpc(h, "SendMessage", {"message": m, "metadata": a2a.meta}))["error"]
        assert err["code"] == -32602, (m, err)
    err = (await rpc(h, "SendMessage", {"message": msg("x"), "metadata": {**a2a.meta, "timeoutMinutes": "soon"}}))
    assert "timeout_minutes" in err["error"]["message"]
    err = (await rpc(h, "SendMessage", {"message": msg("x"), "metadata": {**a2a.meta, "outputSchema": {"type": 5}}}))
    assert err["error"]["code"] == -32602 and "output_schema" in err["error"]["message"]  # checked at submit
    a2a.server.settings.a2a_max_request_bytes = 1000
    big = await h.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x" * 2000}})
    assert big.status_code == 413
    a2a.server.settings.a2a_max_request_bytes = 4_000_000

    # text parts are the instructions, file/data/url parts are context; options come from metadata
    m = {**msg("Look at the trace."), "parts": [
        {"text": "Look at the trace."}, {"raw": "VHJhY2ViYWNr", "filename": "trace.txt", "mediaType": "text/plain"},
        {"data": {"ticket": 4411}}, {"url": "https://example.com/spec", "filename": "spec"}]}
    res = await rpc(h, "SendMessage", {"message": m, "metadata": {"hangar": {**a2a.meta, "deliver": "branch"}},
                                       "configuration": {"returnImmediately": True}})
    job = a2a.jobs.get(res["result"]["task"]["id"])
    assert job.instructions == "Look at the trace." and job.deliver == "branch"
    assert [(c.name, c.kind) for c in job.context] == [("trace.txt", "file"), ("data-3.json", "text"),
                                                         ("spec", "url")]
    assert json.loads(job.context[1].content) == {"ticket": 4411}
    # the job is busy: a message to it now is refused rather than queued behind it
    err = (await rpc(h, "SendMessage", {"message": msg("more", contextId=job.id)}))["error"]
    assert err["code"] == -32004 and "still" in err["message"]
    await _until(a2a.jobs, job.id, DONE)
    # a finished task takes follow-ups through its context, not its taskId
    err = (await rpc(h, "SendMessage", {"message": msg("more", taskId=job.id)}))["error"]
    assert err["code"] == -32004 and "contextId" in err["message"]
    err = (await rpc(h, "SendMessage", {"message": msg("more", taskId=job.id, contextId="other")}))["error"]
    assert err["code"] == -32602

    # the question skill changes nothing and delivers nothing
    res = await rpc(h, "SendMessage", {"message": msg("Where is the retry policy?"),
                                       "metadata": {**a2a.meta, "skill": "answer_question_about_repo"},
                                       "configuration": {"returnImmediately": True}})
    q = a2a.jobs.get(res["result"]["task"]["id"])
    assert q.deliver == "none" and "Don't change any files" in q.instructions
    await _until(a2a.jobs, q.id, DONE)
    listed = (await rpc(h, "ListTasks", {"contextId": q.id, "includeArtifacts": True}))["result"]
    assert [t["id"] for t in listed["tasks"]] == [q.id] and "artifacts" in listed["tasks"][0]


async def test_a_schema_on_the_text_part_returns_a_structured_result(a2a):
    """Spec §6.8: a schema on the text part asks for JSON back; the agent's valid result.json is an artifact."""
    schema = {"type": "object", "required": ["test"], "additionalProperties": False,
              "properties": {"test": {"type": "string"}, "flaky": {"type": "boolean"}}}
    agent = a2a.world["root"] / "agent.sh"
    agent.write_text(agent.read_text().replace(
        "  fail) exit 3 ;;",
        "  result) hangar-agent result '{\"test\": \"test_sync\", \"flaky\": true}'\n"
        "      echo \"Found it\" > \"$HANGAR_JOB_DIR/RESULT.md\" ;;\n  fail) exit 3 ;;"))
    a2a.world["mode"].write_text("result")
    m = {**msg("Which test is flaky?"), "parts": [{"text": "Which test is flaky?", "metadata": {"schema": schema}}]}
    res = await rpc(a2a.http, "SendMessage", {"message": m, "metadata": {**a2a.meta, "deliver": "none"},
                                              "configuration": {"returnImmediately": True}})
    job = a2a.jobs.get(res["result"]["task"]["id"])
    assert job.output_schema == schema
    job = await _until(a2a.jobs, job.id, DONE)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    assert job.result.data == {"test": "test_sync", "flaky": True} and job.result.result_error is None
    got = (await rpc(a2a.http, "GetTask", {"id": job.id}))["result"]
    assert got["status"]["state"] == COMPLETED
    arts = {a["name"]: a for a in got["artifacts"]}
    assert arts["Structured result"]["parts"] == [{"data": {"test": "test_sync", "flaky": True},
                                                   "mediaType": "application/json"}]
    assert arts["Summary"]["parts"][0]["text"] == "Found it"


def test_job_states_map_to_task_states_including_ones_still_to_come():
    assert task_state(JobState.QUEUED) == SUBMITTED and task_state("awaiting_approval") == SUBMITTED
    for s in ("provisioning", "running", "verifying", "delivering", "some_new_state"):
        assert task_state(s) == WORKING
    assert task_state(JobState.SUCCEEDED) == COMPLETED and task_state("cancelled") == CANCELED
    assert task_state("needs_input") == INPUT_REQUIRED


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")  # "needs_input" isn't a JobState until E merges
async def test_needs_input_is_input_required_and_a_reply_by_task_id_follows_up(a2a, monkeypatch):
    res = await rpc(a2a.http, "SendMessage", {"message": msg("Add fix.txt"), "metadata": a2a.meta})
    job = a2a.jobs.get(res["result"]["task"]["id"])
    # What workstream E's needs_input looks like from here: the state and the agent's question.
    job.state = "needs_input"
    job.events.append(JobEvent(state="needs_input", message="Which database should I use?"))
    got = (await rpc(a2a.http, "GetTask", {"id": job.id}))["result"]
    assert got["status"]["state"] == INPUT_REQUIRED
    assert got["status"]["message"]["parts"][0]["text"] == "Which database should I use?"
    assert (await rpc(a2a.http, "tasks/get", {"id": job.id}, version="0.3"))["result"]["status"]["state"] == \
        "input-required"
    calls = []
    monkeypatch.setattr(a2a.jobs, "follow_up", lambda *a, **kw: calls.append((a, kw)))
    res = await rpc(a2a.http, "SendMessage", {"message": msg("Postgres", taskId=job.id),
                                              "configuration": {"returnImmediately": True}})
    assert res["result"]["task"]["id"] == job.id and calls[0][0] == (job.id, "Postgres")
    job.state = JobState.SUCCEEDED


# ---------------------------------------------------------------------------- push notifications
async def test_push_notifications(a2a):
    h = a2a.http
    push = {"url": "https://hooks.example/a2a", "token": "tok-1",
            "authentication": {"scheme": "Bearer", "credentials": "s3cret-cred"}}
    res = await rpc(h, "SendMessage", {"message": msg("Add fix.txt"), "metadata": a2a.meta,
                                       "configuration": {"returnImmediately": True,
                                                         "taskPushNotificationConfig": push}})
    tid = res["result"]["task"]["id"]
    await _until(a2a.jobs, tid, DONE)

    def pushed():
        return [c for c in a2a.jobs.hooks.calls if c.url.host == "hooks.example" and c.url.path == "/a2a"]

    for _ in range(100):
        if pushed() and "task" in json.loads(pushed()[-1].content):
            break
        await asyncio.sleep(0.05)
    calls = pushed()
    assert calls and all(c.headers["authorization"] == "Bearer s3cret-cred" and
                         c.headers["x-a2a-notification-token"] == "tok-1" for c in calls)
    bodies = [json.loads(c.content) for c in calls]
    final = bodies[-1]["task"]
    assert final["status"]["state"] == COMPLETED and any(a["name"] == "Summary" for a in final["artifacts"])
    assert all("statusUpdate" in b for b in bodies[:-1])
    n = len(calls)
    await asyncio.sleep(0.3)
    assert len(pushed()) == n  # each state once

    # configs come back without their token or credentials
    listed = await h.post("/a2a", headers={"A2A-Version": "1.0"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "ListTaskPushNotificationConfigs", "params": {"taskId": tid}})
    assert "s3cret" not in listed.text and "tok-1" not in listed.text
    [cfg] = listed.json()["result"]["configs"]
    assert cfg["url"] == push["url"] and cfg["authentication"] == {"scheme": "Bearer"}
    got = (await rpc(h, "GetTaskPushNotificationConfig", {"taskId": tid, "id": cfg["id"]}))["result"]
    assert got == cfg
    # internal targets are refused like job callbacks
    err = (await rpc(h, "CreateTaskPushNotificationConfig", {"taskId": tid, "url": "https://10.0.0.5/x"}))["error"]
    assert err["code"] == -32602 and "push notification url" in err["message"]
    # the 0.3 shapes on the same store
    made = (await rpc(h, "tasks/pushNotificationConfig/set", {"taskId": tid, "pushNotificationConfig": {
        "id": "p2", "url": "https://hooks.example/v03", "authentication": {"schemes": ["Bearer"],
                                                                           "credentials": "c2"}}},
                      version="0.3"))["result"]
    assert made == {"taskId": tid, "pushNotificationConfig": {"id": "p2", "url": "https://hooks.example/v03",
                                                              "authentication": {"schemes": ["Bearer"]}}}
    assert len((await rpc(h, "tasks/pushNotificationConfig/list", {"id": tid}, version="0.3"))["result"]) == 2
    assert (await rpc(h, "DeleteTaskPushNotificationConfig", {"taskId": tid, "id": cfg["id"]}))["result"] == {}
    assert (await rpc(h, "tasks/pushNotificationConfig/delete", {"id": tid, "pushNotificationConfigId": "p2"},
                      version="0.3"))["result"] is None
    assert (await rpc(h, "ListTaskPushNotificationConfigs", {"taskId": tid}))["result"]["configs"] == []
    assert (await rpc(h, "GetTaskPushNotificationConfig", {"taskId": tid, "id": "p2"}))["error"]["code"] == -32001
