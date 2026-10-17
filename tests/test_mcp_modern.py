"""Hangar as an MCP server for modern clients (workstream mcp-modern): tasks, elicitation, typed results, tool
hints, resources with subscriptions and prompts. Driven by the SDK's own in-process client in both protocol eras,
over real sessions and jobs (fake provider, scripted agent, real git and tmux)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar, Literal

import mcp_types as types
import pytest
from mcp import Client
from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim
from pydantic import ConfigDict, TypeAdapter

from hangar import mcp_live, mcp_tasks
from hangar.mcp_server import build_mcp
from hangar.models import JobState, SessionState
from tests import test_exchange as xchg
from tests.test_jobs import _until

world, svc, jobs, settle = xchg.world, xchg.svc, xchg.jobs, xchg.settle  # the exchange tests' scripted world

pytestmark = pytest.mark.usefixtures("world")


# ---------------------------------------------------------------------------- a tasks-extension client
class CreateTask(types.Result):
    model_config = ConfigDict(extra="allow")
    result_type: Literal["task"] = "task"
    task_id: str


class TaskRequest(types.Request[mcp_tasks.TaskIdParams, str]):
    name_param: ClassVar[str | None] = "taskId"


class UpdateRequest(types.Request[mcp_tasks.TaskUpdateParams, str]):
    name_param: ClassVar[str | None] = "taskId"


ANY = TypeAdapter(dict[str, Any])


async def task_call(session, method: str, task_id: str, **extra) -> dict[str, Any]:
    if method == "tasks/update":
        req = UpdateRequest(method=method, params=mcp_tasks.TaskUpdateParams(taskId=task_id, **extra))
    else:
        req = TaskRequest(method=method, params=mcp_tasks.TaskIdParams(taskId=task_id))
    return await session.send_request(req, ANY)


class TasksClient(ClientExtension):
    """What a client implementing SEP-2663 does: poll tasks/get, answer input_required via tasks/update."""

    identifier = mcp_tasks.TASKS_EXT

    def __init__(self, answer: str | None = None):
        self.answer = answer
        self.seen: list[dict[str, Any]] = []

    def claims(self):
        async def resolve(created: CreateTask, ctx: ClaimContext) -> types.CallToolResult:
            for _ in range(600):
                t = await task_call(ctx.session, "tasks/get", created.task_id)
                self.seen.append(t)
                if t["status"] == "completed":
                    return types.CallToolResult.model_validate(t["result"])
                if t["status"] in ("failed", "cancelled"):
                    raise AssertionError(t)
                if t["status"] == "input_required" and self.answer is not None:
                    responses = {k: {"action": "accept", "content": {"answer": self.answer}}
                                 for k in t["inputRequests"]}
                    await task_call(ctx.session, "tasks/update", created.task_id, inputResponses=responses)
                await asyncio.sleep(0.05)
            raise AssertionError("task never finished")

        return [ResultClaim(result_type="task", model=CreateTask, resolve=resolve)]


def answering(text: str | None, action: str = "accept", asked: list[str] | None = None):
    async def cb(ctx, params):
        if asked is not None:
            asked.append(params.message)
        if action != "accept":
            return types.ElicitResult(action=action)
        return types.ElicitResult(action="accept", content={"answer": text})

    return cb


def sc(res: types.CallToolResult) -> Any:
    assert not res.is_error, res.content[0].text
    d = res.structured_content
    return d["result"] if isinstance(d, dict) and set(d) == {"result"} else d


async def new_asking_session(c: Client, name: str) -> None:
    sc(await c.call_tool("new_session", {"name": name, "environment": "xenv", "message": "TASK-ASK pick one"}))


# ---------------------------------------------------------------------------- tools/list: hints, schemas, size
def _model_view(tools) -> str:
    return json.dumps([{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                       for t in tools], separators=(",", ":"))


# --- #140: one budget per server, measured on the model-visible listing (name, description, input schema). The single
# server's cap was 24,700 bytes: 31,157 before the mcp-modern workstream slimmed it, then +0.65 KB for exec(stream) and
# the process tool (review #68 trimmed their descriptions and kept the cap just over what they needed), and features
# went to resources instead of tools because the list was at its cap. The split gives each server room again, and
# each budget must keep 15% headroom so the next tool doesn't force another trim.
BUDGETS = {mcp_live.WORK: 14_000, mcp_live.SANDBOX: 16_000}
HEADROOM = 0.15


async def _listing(fleet, svc, jobs, surface: str):
    from unittest.mock import MagicMock

    mcp = build_mcp(fleet, jobs, svc, triggers=MagicMock(), surface=surface)
    async with Client(mcp) as c:
        tools = (await c.list_tools()).tools
        again = (await c.list_tools()).tools
    assert [t.name for t in tools] == [t.name for t in again]  # deterministic order (clients cache listings)
    return mcp, tools


async def test_each_server_lists_its_own_tools_within_its_budget(fleet, svc, jobs):
    work_mcp, work = await _listing(fleet, svc, jobs, "work")
    drive_mcp, drive = await _listing(fleet, svc, jobs, "sandbox")
    names = {mcp_live.WORK: [t.name for t in work], mcp_live.SANDBOX: [t.name for t in drive]}
    both = set(names[mcp_live.WORK]) & set(names[mcp_live.SANDBOX])
    assert not both, f"listed on both servers: {sorted(both)}"
    for server, listed in names.items():  # every tool is listed on the server mcp_live.server_of says, only there
        assert all(mcp_live.server_of(n) == server for n in listed), (server, listed)
        missing = [n for n in listed if n not in mcp_live.TOOL_HINTS]
        assert not missing, f"add these tools to TOOL_HINTS in mcp_live.py: {missing}"
    every = {t.name for t in work_mcp._tool_manager.list_tools()} - mcp_live.HIDDEN_TOOLS
    assert every == set(names[mcp_live.WORK]) | set(names[mcp_live.SANDBOX])  # hangar still answers all of them
    assert work_mcp.moved == frozenset(n for n in mcp_live.SANDBOX_TOOLS
                                       if n in {t.name for t in work_mcp._tool_manager.list_tools()})
    assert not ({t.name for t in drive_mcp._tool_manager.list_tools()} & set(names[mcp_live.WORK]))
    for server, tools in ((mcp_live.WORK, work), (mcp_live.SANDBOX, drive)):
        size, cap = len(_model_view(tools)), BUDGETS[server]
        assert size < cap * (1 - HEADROOM), f"{server}: {size} bytes; the budget is {cap} with 15% headroom"


async def test_tool_listing_has_hints_slim_schemas_and_typed_results(fleet, svc, jobs):
    _, work = await _listing(fleet, svc, jobs, "work")
    _, drive = await _listing(fleet, svc, jobs, "sandbox")
    tools = [*work, *drive]
    names = [t.name for t in tools]
    by = {t.name: t for t in tools}
    for n in ("get_job", "list_sessions", "get_events", "read_file", "get_session_patch"):
        assert by[n].annotations.read_only_hint is True, n
    for n in ("destroy_sandbox", "cancel_job", "terminal_close", "delete_schedule"):
        assert by[n].annotations.destructive_hint is True and by[n].annotations.read_only_hint is False, n
    assert by["submit_job"].annotations.open_world_hint is True
    assert by["wait_for_turn"].annotations.destructive_hint is False
    # consolidated tools are gone; their jobs are done by create_environment(dry_run) and destroy_sandbox
    assert not {"validate_environment", "clone_environment", "destroy_sandboxes"} & set(names)
    # typed results for the main tools
    for n in ("submit_job", "get_job", "list_jobs", "new_session", "get_session", "send_to_session", "fork_session",
              "wait_for_turn", "get_events", "create_sandbox", "list_sandboxes", "exec", "run_agent"):
        assert by[n].output_schema and by[n].output_schema.get("properties"), n
    assert "pr_url" in json.dumps(by["get_job"].output_schema)
    raw = json.dumps([t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools])
    assert '"title": "' not in json.dumps([t.input_schema for t in tools])  # pydantic titles stripped
    assert "\n        " not in raw  # docstring indentation stripped from descriptions
    # optional arguments are plain types (nullable still accepted by the tool)
    assert by["submit_job"].input_schema["properties"]["repo"] == {"type": "string"}


async def test_server_identity_and_capabilities(fleet, svc, jobs):
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        caps = c.server_capabilities
        assert mcp_tasks.TASKS_EXT in (caps.extensions or {})
        assert caps.resources and caps.resources.subscribe and caps.prompts
        assert c.server_info.name == "hangar"
    async with Client(build_mcp(fleet, jobs, svc), mode="legacy") as c:
        assert c.server_capabilities.resources.subscribe  # resources/subscribe for 2025-era sessions
        assert c.session.initialize_result.server_info.icons


# ---------------------------------------------------------------------------- structured output
async def test_structured_results_validate_and_link_resources(fleet, svc, jobs, world):
    async with Client(build_mcp(fleet, jobs, svc)) as c:  # the SDK client validates structuredContent
        s = sc(await c.call_tool("new_session", {"name": "typed", "environment": "xenv", "repo": world["url"],
                                                 "message": "TASK-USE-B now"}))
        assert s["name"] == "typed" and s["state"] in ("starting", "running")
        turn = await c.call_tool("wait_for_turn", {"ref": "typed", "timeout_seconds": 60})
        t = sc(turn)
        assert t["done"] and t["summary"] == "Went with B" and t["outcome"] == "exited"
        assert any(b.type == "resource_link" and b.uri == "hangar://sessions/typed" for b in turn.content)
        listed = sc(await c.call_tool("list_sessions", {}))
        assert listed[0]["name"] == "typed"
        ev = sc(await c.call_tool("get_events", {"session": "typed"}))
        assert ev["events"] and ev["next"] == ev["events"][-1]["seq"]
        patch = await c.call_tool("get_session_patch", {"ref": "typed"})
        assert "choice.txt" in sc(patch)["patch"]
        link = next(b for b in patch.content if b.type == "resource_link")
        assert link.uri == "hangar://sessions/typed/patch"
        assert "choice.txt" in (await c.read_resource(link.uri)).contents[0].text
        sb = sc(await c.call_tool("get_sandbox", {"sandbox": svc.get("typed").sandbox_id}))
        assert sb["state"] == "running" and sb["environment"] == "xenv"
        r = sc(await c.call_tool("exec", {"sandbox": sb["id"], "command": "echo hi"}))
        assert r["exit_code"] == 0 and r["stdout"].strip() == "hi"
        gone = sc(await c.call_tool("destroy_sandbox", {"sandboxes": [sb["id"]]}))
        assert gone == [sb["id"]]
        res = await c.call_tool("destroy_sandbox", {})
        assert res.is_error and "refusing" in res.content[0].text
        env = sc(await c.call_tool("create_environment", {"spec": {"name": "xenv-2", "extends": "xenv"},
                                                          "dry_run": True}))
        assert "dockerfile" in json.dumps(env).lower() and "xenv-2" not in fleet.catalog.names()


# ---------------------------------------------------------------------------- elicitation in wait_for_turn
async def test_wait_for_turn_asks_the_question_and_continues_modern(fleet, svc, jobs):
    asked: list[str] = []
    async with Client(build_mcp(fleet, jobs, svc), elicitation_callback=answering("TASK-USE-B", asked=asked)) as c:
        assert c.protocol_version == "2026-07-28"  # multi round-trip: the question comes back in the result
        await new_asking_session(c, "ask-m")
        t = sc(await c.call_tool("wait_for_turn", {"ref": "ask-m", "timeout_seconds": 60}))
    assert asked == ["Session ask-m asks: Use A or B?"]
    assert t["done"] and t["elicitation"] == "answered" and t["summary"] == "Went with B", t
    s = svc.get("ask-m")
    assert [tn.message for tn in s.turns] == ["TASK-ASK pick one", "TASK-USE-B"] and s.needs_input is None
    assert s.turns[1].mode == "resume"  # the same conversation continued


async def test_wait_for_turn_asks_the_question_and_continues_legacy(fleet, svc, jobs):
    asked: list[str] = []
    async with Client(build_mcp(fleet, jobs, svc), mode="legacy",
                      elicitation_callback=answering("TASK-USE-B", asked=asked)) as c:
        assert c.protocol_version == "2025-11-25"  # elicitation/create on the session
        await new_asking_session(c, "ask-l")
        t = sc(await c.call_tool("wait_for_turn", {"ref": "ask-l", "timeout_seconds": 60}))
    assert asked == ["Session ask-l asks: Use A or B?"]
    assert t["elicitation"] == "answered" and t["summary"] == "Went with B"


async def test_wait_for_turn_declined_and_unsupported(fleet, svc, jobs):
    async with Client(build_mcp(fleet, jobs, svc), elicitation_callback=answering(None, "decline")) as c:
        await new_asking_session(c, "ask-d")
        t = sc(await c.call_tool("wait_for_turn", {"ref": "ask-d", "timeout_seconds": 60}))
    assert t["elicitation"] == "declined" and t["question"] == "Use A or B?"
    assert svc.get("ask-d").needs_input == "Use A or B?" and len(svc.get("ask-d").turns) == 1  # nothing sent

    for mode in ("auto", "legacy"):  # no elicitation capability: the question comes back, as before
        async with Client(build_mcp(fleet, jobs, svc), mode=mode) as c:
            t = sc(await c.call_tool("wait_for_turn", {"ref": "ask-d", "timeout_seconds": 5}))
        assert t["done"] and t["question"] == "Use A or B?" and "elicitation" not in t


async def test_wait_for_turn_answers_a_job(fleet, svc, jobs, world):
    async with Client(build_mcp(fleet, jobs, svc), elicitation_callback=answering("TASK-USE-B")) as c:
        job = sc(await c.call_tool("submit_job", {"instructions": "TASK-ASK pick one", "repo": world["url"],
                                                  "environment": "xenv", "deliver": "branch"}))
        t = sc(await c.call_tool("wait_for_turn", {"ref": job["id"], "timeout_seconds": 60}))
        assert t["elicitation"] == "answered" and t["job"] == job["id"], t
    done = await _until(jobs, job["id"], {JobState.SUCCEEDED, JobState.FAILED})
    assert done.state == JobState.SUCCEEDED and done.turns[1].message == "TASK-USE-B"


# ---------------------------------------------------------------------------- tasks
async def test_job_as_a_task_with_a_question(fleet, svc, jobs, world):
    ext = TasksClient(answer="TASK-USE-B")
    async with Client(build_mcp(fleet, jobs, svc), extensions=[ext], elicitation_callback=answering("unused")) as c:
        res = await c.call_tool("submit_job", {"instructions": "TASK-ASK pick one", "repo": world["url"],
                                               "environment": "xenv", "deliver": "branch"})
    out = sc(res)
    assert out["state"] == "succeeded" and out["turn"]["summary"] == "Went with B", out
    assert any(b.type == "resource_link" and b.uri == f"hangar://jobs/{out['id']}" for b in res.content)
    statuses = [t["status"] for t in ext.seen]
    assert statuses[0] == "working" and "input_required" in statuses and statuses[-1] == "completed"
    ask = next(t for t in ext.seen if t["status"] == "input_required")
    req = ask["inputRequests"]["answer-1"]
    assert req["method"] == "elicitation/create" and "Use A or B?" in req["params"]["message"]
    assert all(t["resultType"] == "complete" and t["pollIntervalMs"] == 5000 for t in ext.seen)
    # terminal state is stored: polling again returns the same result
    async with Client(build_mcp(fleet, jobs, svc), extensions=[TasksClient()]) as c:
        again = await task_call(c.session, "tasks/get", ext.seen[-1]["taskId"])
    assert again["status"] == "completed" and again["result"] == ext.seen[-1]["result"]


async def test_session_task_handle_update_and_cancel(fleet, svc, jobs):
    async with Client(build_mcp(fleet, jobs, svc), extensions=[TasksClient()]) as c:
        created = await c.session.call_tool("new_session", {"name": "tsk", "environment": "xenv",
                                                            "message": "TASK-ASK pick one"}, allow_claimed=True)
        assert isinstance(created, CreateTask) and created.status == "working"
        tid = created.task_id
        for _ in range(400):
            t = await task_call(c.session, "tasks/get", tid)
            if t["status"] != "working":
                break
            await asyncio.sleep(0.05)
        # this client declared no elicitation: the question is the result
        assert t["status"] == "completed" and t["result"]["structuredContent"]["turn"]["question"] == "Use A or B?"

        # a client that can answer: input_required -> tasks/update -> the same conversation continues
    async with Client(build_mcp(fleet, jobs, svc), extensions=[TasksClient()],
                      elicitation_callback=answering("unused")) as c:
        created = await c.session.call_tool("send_to_session", {"session": "tsk", "message": "TASK-ASK again"},
                                            allow_claimed=True)
        tid = created.task_id
        for _ in range(400):
            t = await task_call(c.session, "tasks/get", tid)
            if t["status"] == "input_required":
                break
            await asyncio.sleep(0.05)
        assert set(t["inputRequests"]) == {"answer-2"}
        bogus = {"answer-9": {"action": "accept", "content": {"answer": "x"}}}
        assert (await task_call(c.session, "tasks/update", tid, inputResponses=bogus))["resultType"] == "complete"
        assert len(svc.get("tsk").turns) == 2  # unknown keys are ignored
        await task_call(c.session, "tasks/update", tid,
                        inputResponses={"answer-2": {"action": "accept", "content": {"answer": "TASK-USE-B"}}})
        # answering twice (a client retry) sends nothing more
        await task_call(c.session, "tasks/update", tid,
                        inputResponses={"answer-2": {"action": "accept", "content": {"answer": "TASK-USE-B"}}})
        for _ in range(400):
            t = await task_call(c.session, "tasks/get", tid)
            if t["status"] == "completed":
                break
            await asyncio.sleep(0.05)
        assert t["result"]["structuredContent"]["turn"]["summary"] == "Went with B"
        assert [x.message for x in svc.get("tsk").turns][-1] == "TASK-USE-B" and len(svc.get("tsk").turns) == 3

        # cancel: the task stops following the turn
        created = await c.session.call_tool("send_to_session", {"session": "tsk", "message": "sleep a bit"},
                                            allow_claimed=True)
        await task_call(c.session, "tasks/cancel", created.task_id)
        assert (await task_call(c.session, "tasks/get", created.task_id))["status"] == "cancelled"
        with pytest.raises(Exception, match="unknown task"):
            await task_call(c.session, "tasks/get", "task-nope")
    await settle(svc, "tsk")


async def test_tasks_belong_to_their_caller_and_need_the_extension(fleet, svc, jobs, world):
    store = fleet.store
    who = {"a": "alice"}
    tasks = mcp_tasks.Tasks(store, fleet, svc, jobs, lambda: who["a"])
    job = jobs.submit(mcp_tasks_job(world), mcp_caller())
    res = types.CallToolResult(content=[], structured_content=jobs.view(job))
    created = tasks.create("submit_job", res, {})
    assert created["resultType"] == "task" and created["taskId"].startswith("task-")
    assert (await tasks.get(created["taskId"], None))["status"] in ("working", "completed")
    who["a"] = "mallory"
    with pytest.raises(Exception, match="unknown task"):
        await tasks.get(created["taskId"], None)
    with pytest.raises(Exception, match="unknown task"):
        await tasks.cancel(created["taskId"])
    # nothing to wait for: no task
    assert tasks.create("new_session", types.CallToolResult(content=[], structured_content={"name": "x"}), {}) is None
    assert tasks.create("get_job", res, {}) is None
    await _until(jobs, job.id, {JobState.SUCCEEDED, JobState.FAILED})

    # a client without the extension, or on the legacy protocol, gets the plain result
    async with Client(build_mcp(fleet, jobs, svc), mode="legacy") as c:
        out = sc(await c.call_tool("submit_job", {"instructions": "TASK-USE-B", "repo": world["url"],
                                                  "environment": "xenv", "deliver": "branch"}))
    assert out["state"] in ("queued", "provisioning") and out.get("turn") is None
    await _until(jobs, out["id"], {JobState.SUCCEEDED, JobState.FAILED})


def mcp_tasks_job(world):
    from hangar.jobs import SubmitJob

    return SubmitJob(instructions="TASK-USE-B", repo=world["url"], environment="xenv", deliver="branch")


def mcp_caller():
    from hangar.jobs import Caller

    return Caller("alice", "alice")


async def test_run_agent_and_fork_session_as_tasks(fleet, svc, jobs):
    async with Client(build_mcp(fleet, jobs, svc), extensions=[TasksClient()]) as c:
        sb = sc(await c.call_tool("create_sandbox", {"environment": "xenv", "name": "runner"}))
        run = sc(await c.call_tool("run_agent", {"sandbox": sb["id"], "prompt": "hello there"}))
        assert run["state"] == "finished" and run["exit_code"] == 0 and "did: hello there" in run["output_tail"]

        sc(await c.call_tool("new_session", {"name": "base", "environment": "xenv", "message": "TASK-USE-B"}))
        await settle(svc, "base")
        forks = sc(await c.call_tool("fork_session", {"session": "base", "n": 2, "message": "TASK-USE-B again"}))
    assert forks["parent"] == "base" and len(forks["forks"]) == 2
    assert all(f["turn"]["done"] and f["turn"]["outcome"] == "exited" for f in forks["forks"]), forks
    for f in forks["forks"]:
        await settle(svc, f["name"])


# ---------------------------------------------------------------------------- resources and subscriptions
async def test_resources_and_modern_subscriptions(fleet, svc, jobs, world):
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        templates = {t.uri_template for t in (await c.list_resource_templates()).resource_templates}
        assert {"hangar://sessions/{name}", "hangar://sessions/{name}/events{?since}", "hangar://sessions/{name}/patch",
                "hangar://jobs/{job_id}", "hangar://jobs/{job_id}/events{?since}"} <= templates  # --- stream: ?since
        async with c.listen(resources_list_changed=True) as sub:  # a new session is a new resource
            sc(await c.call_tool("new_session", {"name": "watch", "environment": "xenv", "message": "TASK-USE-B"}))
            ev = await asyncio.wait_for(anext(aiter(sub)), 10)
            assert type(ev).__name__ == "ResourcesListChanged"
        assert "hangar://sessions/watch" in {r.uri for r in (await c.list_resources()).resources}
        await settle(svc, "watch")
        view = json.loads((await c.read_resource("hangar://sessions/watch")).contents[0].text)
        assert view["name"] == "watch" and view["turns"][0]["summary"] == "Went with B"
        evs = json.loads((await c.read_resource("hangar://sessions/watch/events")).contents[0].text)
        assert evs["events"]

        async with c.listen(resource_subscriptions=["hangar://sessions/watch"]) as sub:
            sc(await c.call_tool("send_to_session", {"session": "watch", "message": "TASK-USE-B again"}))
            ev = await asyncio.wait_for(anext(aiter(sub)), 10)
            assert ev.uri == "hangar://sessions/watch"
        await settle(svc, "watch")

        job = sc(await c.call_tool("submit_job", {"instructions": "TASK-USE-B", "repo": world["url"],
                                                  "environment": "xenv", "deliver": "branch"}))
        async with c.listen(resource_subscriptions=[f"hangar://jobs/{job['id']}"]) as sub:
            ev = await asyncio.wait_for(anext(aiter(sub)), 10)
            assert ev.uri == f"hangar://jobs/{job['id']}"
        done = await _until(jobs, job["id"], {JobState.SUCCEEDED, JobState.FAILED})
        jv = json.loads((await c.read_resource(f"hangar://jobs/{done.id}")).contents[0].text)
        assert jv["state"] == "succeeded"


@pytest.mark.filterwarnings("ignore:resources/(un)?subscribe is removed")  # deliberately the 2025-era path
async def test_legacy_resource_subscriptions(fleet, svc, jobs):
    got: list[str] = []

    async def on_message(msg):
        if isinstance(msg, types.ResourceUpdatedNotification):
            got.append(msg.params.uri)

    async with Client(build_mcp(fleet, jobs, svc), mode="legacy", message_handler=on_message) as c:
        sc(await c.call_tool("new_session", {"name": "old", "environment": "xenv", "message": "TASK-USE-B"}))
        await settle(svc, "old")
        await c.session.subscribe_resource("hangar://sessions/old")
        with pytest.raises(Exception, match="not a hangar resource"):
            await c.session.subscribe_resource("file:///etc/passwd")
        sc(await c.call_tool("send_to_session", {"session": "old", "message": "TASK-USE-B again"}))
        for _ in range(200):
            if got:
                break
            await asyncio.sleep(0.05)
        assert "hangar://sessions/old" in got
        await settle(svc, "old")
        await c.session.unsubscribe_resource("hangar://sessions/old")
        got.clear()
        sc(await c.call_tool("send_to_session", {"session": "old", "message": "TASK-USE-B once more"}))
        await settle(svc, "old")
        await asyncio.sleep(0.5)
        assert got == []


# ---------------------------------------------------------------------------- prompts
async def test_prompts(fleet, svc, jobs):
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        names = {p.name for p in (await c.list_prompts()).prompts}
        assert names == {"hand_off_to_cloud", "best_of_n", "pick_up_session"}
        sc(await c.call_tool("new_session", {"name": "later", "environment": "xenv", "message": "TASK-ASK pick"}))
        await settle(svc, "later")
        p = await c.get_prompt("pick_up_session", {"session": "later"})
        text = p.messages[0].content.text
        assert "later" in text and "Use A or B?" in text and "send_to_session" in text
        b = await c.get_prompt("best_of_n", {"issue": "fix the flake", "repo": "o/r", "n": "3"})
        assert 'strategy="best_of_n"' in b.messages[0].content.text
        h = await c.get_prompt("hand_off_to_cloud", {"task": "finish the parser"})
        assert "--from-local" in h.messages[0].content.text
    assert svc.get("later").state in (SessionState.IDLE, SessionState.SUSPENDED)


# ---------------------------------------------------------------------------- over HTTP: legacy clients keep a session
class AppThread:
    """The whole app (lifespan included) on its own uvicorn server and event loop."""

    def __init__(self, app):
        import socket
        import threading
        import time

        import uvicorn

        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        # integrate (#53): the fake provider's exec is an asyncio subprocess now. uvloop learns of child exits
        # through SIGCHLD, which only the main thread receives, so on this thread's loop they would never finish.
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", loop="asyncio"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


async def test_http_legacy_session_elicits_and_attributes_each_request(settings, world):
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    from hangar.app import create_app
    from tests.conftest import ENVS, FakeProvider

    settings.admin_token = "t0ken"
    settings.env_dirs = [world["envs"], ENVS]
    settings.session_poll_seconds = 0.1
    app = create_app(settings)
    h = app.state.hangar
    bob, _ = h.auth.create("bob")
    srv = AppThread(app)
    h.fleet.providers["docker"] = FakeProvider()  # after startup, which sets up the real providers
    h.fleet.provider_errors.pop("docker", None)
    try:
        url = f"http://127.0.0.1:{srv.port}/mcp"
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer t0ken"}, timeout=60) as http:
            async with Client(streamable_http_client(url, http_client=http), mode="legacy",
                              elicitation_callback=answering("TASK-USE-B")) as c:
                assert c.protocol_version == "2025-11-25"
                sc(await c.call_tool("new_session", {"name": "web", "environment": "xenv",
                                                     "message": "TASK-ASK pick one"}))
                t = sc(await c.call_tool("wait_for_turn", {"ref": "web", "timeout_seconds": 60}))
                assert t.get("elicitation") == "answered" and t["summary"] == "Went with B", json.dumps(t)
                http.headers["Authorization"] = f"Bearer {bob}"  # the same MCP session, another caller
                sc(await c.call_tool("suspend_session", {"session": "web"}))
            # a 2026-07-28 client over HTTP: the question comes back in the result; long tools become tasks
            http.headers["Authorization"] = "Bearer t0ken"
            ext = TasksClient(answer="TASK-USE-B")
            async with Client(streamable_http_client(url, http_client=http), extensions=[ext],
                              elicitation_callback=answering("TASK-USE-B")) as c:
                assert c.protocol_version == "2026-07-28"
                s = sc(await c.call_tool("new_session", {"name": "web2", "environment": "xenv",
                                                         "message": "TASK-ASK pick one"}))
                assert s["turn"]["summary"] == "Went with B" and "input_required" in [t["status"] for t in ext.seen]
                sc(await c.call_tool("send_to_session", {"session": "web2", "message": "TASK-ASK again"}))
        actors = {(e.actor, e.action) for e in h.store.audit_tail(50)}
        assert ("admin", "session.new") in actors and ("bob", "session.suspend") in actors, actors
    finally:
        srv.stop()


# ---------------------------------------------------------------------------- review: spec, fallbacks, compatibility
async def test_task_methods_refuse_clients_that_did_not_declare_the_extension(fleet, svc, jobs, world):
    """SEP-2663: -32021 for non-declaring clients issuing tasks/get, tasks/update and tasks/cancel."""
    from mcp.shared.exceptions import MCPError

    async with Client(build_mcp(fleet, jobs, svc), extensions=[TasksClient()]) as c:
        created = await c.session.call_tool("submit_job", {"instructions": "TASK-USE-B", "repo": world["url"],
                                                           "environment": "xenv", "deliver": "branch"},
                                            allow_claimed=True)
    async with Client(build_mcp(fleet, jobs, svc)) as c:  # the same caller, without the extension
        for method, extra in (("tasks/get", {}), ("tasks/cancel", {}), ("tasks/update", {"inputResponses": {}})):
            with pytest.raises(MCPError) as e:
                await task_call(c.session, method, created.task_id, **extra)
            assert e.value.code == -32021, (method, e.value)
            assert mcp_tasks.TASKS_EXT in e.value.data["requiredCapabilities"]["extensions"]
    [job] = jobs.list()
    assert not job.cancel_requested  # the refused cancel did nothing
    await _until(jobs, job.id, {JobState.SUCCEEDED, JobState.FAILED})


async def test_a_session_task_follows_its_session_not_a_later_one_with_the_same_name(fleet, svc, jobs):
    async with Client(build_mcp(fleet, jobs, svc), extensions=[TasksClient()]) as c:
        created = await c.session.call_tool("new_session", {"name": "dup", "environment": "xenv",
                                                            "message": "TASK-USE-B"}, allow_claimed=True)
        first = await settle(svc, "dup")
        svc.suspend("dup", requester="t")
        await settle(svc, "dup")
        svc.archive("dup", requester="t")
        sc(await c.call_tool("new_session", {"name": "dup", "environment": "xenv", "message": "TASK-ASK x"}))
        await settle(svc, "dup")
        t = await task_call(c.session, "tasks/get", created.task_id)
    assert t["status"] == "completed", t
    out = t["result"]["structuredContent"]
    assert out["id"] == first.id and out["turn"]["summary"] == "Went with B", out


async def test_old_tool_names_and_destroy_output_still_work(fleet, svc, jobs):
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        listed = {t.name for t in (await c.list_tools()).tools}
        assert not {"validate_environment", "clone_environment", "destroy_sandboxes"} & listed  # not advertised
        v = sc(await c.call_tool("validate_environment", {"spec": {"name": "xenv-v", "extends": "xenv"}}))
        assert "dockerfile" in json.dumps(v).lower()
        cl = sc(await c.call_tool("clone_environment", {"source": "xenv", "name": "xenv-c"}))
        assert cl["name"] == "xenv-c" and cl["extends"] == "xenv"
        a = sc(await c.call_tool("create_sandbox", {"environment": "xenv", "name": "old-a"}))
        b = sc(await c.call_tool("create_sandbox", {"environment": "xenv", "name": "old-b"}))
        assert sc(await c.call_tool("destroy_sandboxes", {"sandboxes": [b["id"]]})) == [b["id"]]
        assert sc(await c.call_tool("destroy_sandbox", {"sandbox": a["id"]})) == [a["id"]]
    assert "xenv-c" in fleet.catalog.names() and "xenv-v" not in fleet.catalog.names()


async def test_stateless_legacy_client_with_elicitation_gets_the_question(settings, world):
    """HANGAR_MCP_STATELESS_HTTP=true: a 2025-era request has no back-channel, so the question comes back."""
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    from hangar.app import create_app
    from tests.conftest import ENVS, FakeProvider

    settings.admin_token = "t0ken"
    settings.env_dirs = [world["envs"], ENVS]
    settings.session_poll_seconds = 0.1
    settings.mcp_stateless_http = True
    app = create_app(settings)
    h = app.state.hangar
    srv = AppThread(app)
    h.fleet.providers["docker"] = FakeProvider()
    h.fleet.provider_errors.pop("docker", None)
    asked: list[str] = []
    try:
        url = f"http://127.0.0.1:{srv.port}/mcp"
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer t0ken"}, timeout=60) as http:
            async with Client(streamable_http_client(url, http_client=http), mode="legacy",
                              elicitation_callback=answering("TASK-USE-B", asked=asked)) as c:
                sc(await c.call_tool("new_session", {"name": "nob", "environment": "xenv",
                                                     "message": "TASK-ASK pick one"}))
                t = sc(await asyncio.wait_for(c.call_tool("wait_for_turn", {"ref": "nob", "timeout_seconds": 60}),
                                              90))
        assert t["done"] and t["question"] == "Use A or B?" and asked == [], t
    finally:
        srv.stop()
