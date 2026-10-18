"""#179: replaying a recorded run through the model gateway. A scripted harness (it runs "tools" in-process and talks
the Anthropic Messages API) records a 30-step run against a scripted model, then replays it: the gateway serves the
taped answers, compares each request (which carries the tools' outputs) with the tape's, and reports the first that
doesn't match as the divergence, by kind.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest

from hangar import modelgw, posture, replay
from hangar.events import RunEvent
from hangar.models import CredentialKind, Sandbox, SandboxState, Session, SessionTurn

REAL_KEY = "sk-ant-api03-REALKEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
UPSTREAM = "http://upstream.test"
STEPS = 30


@pytest.fixture
def gw(fleet, settings, broker):
    settings.gateway_url = "http://gw.test:8788"
    settings.model_gateway_upstreams = {"anthropic": UPSTREAM}
    broker.put("anthropic-key", "claude-code", CredentialKind.API_KEY, REAL_KEY)
    return modelgw.of(fleet)


def _sse(events: list[dict]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


class Model:
    """The scripted model: step n (the request's assistant turns + 1) calls `tool` with `command` for n < STEPS, and
    ends the turn at STEPS."""

    def __init__(self, tools: dict[int, tuple[str, dict]] | None = None):
        self.calls: list[int] = []
        self.tools = tools or {}

    def step_of(self, body: dict) -> int:
        return 1 + sum(1 for m in body["messages"] if m["role"] == "assistant")

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        n = self.step_of(body)
        self.calls.append(n)
        start = {"type": "message_start", "message": {"id": f"msg_01STEP{n:03d}abcdef", "type": "message",
                                                      "role": "assistant", "content": [],
                                                      "usage": {"input_tokens": 10, "output_tokens": 1}}}
        if n >= STEPS:
            evs = [start, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
                   {"type": "content_block_stop", "index": 0},
                   {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
                   {"type": "message_stop"}]
        else:
            name, inp = self.tools.get(n, ("Bash", {"command": f"make step{n}"}))
            evs = [start, {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": f"toolu_01RUN{uuid.uuid4().hex[:10]}", "name": name, "input": {}}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "input_json_delta", "partial_json": json.dumps(inp)}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
                {"type": "message_stop"}]
        raw = _sse(evs)

        async def chunks():
            yield raw

        return httpx.Response(200, content=chunks(), headers={"content-type": "text/event-stream"})


def parse(raw: bytes) -> tuple[list[dict], str | None]:
    """The tool calls (id, name, input) and the stop reason of a streamed answer."""
    calls, stop, partial = [], None, {}
    for ev in modelgw.parse_sse(raw):
        d = ev["data"]
        if not isinstance(d, dict):
            continue
        if d.get("type") == "content_block_start" and d["content_block"]["type"] == "tool_use":
            calls.append({"id": d["content_block"]["id"], "name": d["content_block"]["name"]})
            partial[d["index"]] = ""
        elif d.get("type") == "content_block_delta" and d["delta"].get("type") == "input_json_delta":
            partial[d["index"]] += d["delta"]["partial_json"]
        elif d.get("type") == "message_delta":
            stop = d["delta"].get("stop_reason")
    for c, p in zip(calls, partial.values(), strict=False):
        c["input"] = json.loads(p or "{}")
    return calls, stop


class Harness:
    """A Claude Code stand-in: a system prompt with today's date and a per-run session id in `metadata`, the prompt,
    then each answer's tool calls run by `run_tool` and sent back."""

    def __init__(self, run_tool, date: str = "2026-10-17", mcp: bool = True):
        self.run_tool, self.date, self.mcp = run_tool, date, mcp
        self.session = str(uuid.uuid4())
        self.statuses: list[int] = []
        self.msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "Fix the flaky build"}]}]

    def body(self, msgs: list[dict]) -> dict:
        return {"model": "claude-opus-5-5", "max_tokens": 32000, "stream": True,
                "metadata": {"user_id": f"user_abc_account_x_session_{self.session}"},
                "system": [{"type": "text", "text": f"You are Claude Code. Today's date is {self.date}."}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}], "messages": msgs}

    async def run(self, fleet, token: str, max_steps: int = STEPS + 5) -> list[dict]:
        """Carries on from where its conversation is (a second call continues it)."""
        msgs = self.msgs
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fleet.gateway.asgi_app()),
                                     base_url="http://gw.test") as c:
            for _ in range(max_steps):
                r = await c.post("/model/anthropic/v1/messages", content=json.dumps(self.body(msgs)).encode(),
                                 headers={"x-api-key": token, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
                self.statuses.append(r.status_code)
                if r.status_code != 200:
                    break
                calls, stop = parse(r.content)
                if stop == "end_turn" or not calls:
                    break
                msgs.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": c_["id"], "name": c_["name"], "input": c_["input"]} for c_ in calls]})
                msgs.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": c_["id"], "content": self.tool(c_)} for c_ in calls]})
        return msgs

    def tool(self, call: dict) -> str:
        if call["name"].startswith("mcp__"):
            return "created JIRA-7" if self.mcp else "Error: No such tool available: " + call["name"]
        return self.run_tool(call["input"].get("command", ""))


def deterministic(cmd: str) -> str:
    return f"$ {cmd}\nok, 3 passed in 0.{len(cmd)}1s"


def _sandbox(store, labels: dict | None = None, sb_id: str | None = None) -> tuple[Sandbox, Session, SessionTurn]:
    turn = SessionTurn(message="Fix the flaky build", run_id=f"run-{uuid.uuid4().hex[:8]}", harness="claude-code",
                       started_at=time.time())
    s = Session(name=f"s{uuid.uuid4().hex[:6]}", environment="e", harness="claude-code", turns=[turn],
                sandbox_id=sb_id or f"sb-{uuid.uuid4().hex[:8]}")
    store.put(s)
    sb = Sandbox(id=s.sandbox_id, name=s.sandbox_id, environment="e", provider="fake", state=SandboxState.RUNNING,
                 labels={"hangar.session": s.id, **(labels or {})}, created_by="alice")
    store.put(sb)
    return sb, s, turn


async def record(gw, fleet, store, model: Model, run_tool=deterministic, **kw) -> str:
    fleet.gateway.transport = httpx.MockTransport(model)
    fleet.gateway._client = None
    sb, s, turn = _sandbox(store)
    token = gw.mint(sb, "anthropic", "anthropic-key")
    await Harness(run_tool, **kw).run(fleet, token)
    await gw.settle()
    turn.finished_at = time.time()
    await gw.turn_ended(s, turn)
    return f"{s.id}.{turn.run_id}"


async def replay_it(gw, fleet, store, run: str, run_tool=deterministic, **plan_kw) -> tuple[dict, Harness]:
    p = replay.plan(fleet, run, **plan_kw)
    sb, _, _ = _sandbox(store, {replay.REPLAY_LABEL: p["id"]})
    token = gw.mint(sb, "anthropic", "anthropic-key")
    replay.of(fleet).bind(sb)
    h = Harness(run_tool, date="2026-10-18", mcp=False)
    await h.run(fleet, token)
    await gw.settle()
    return replay.load_plan(fleet.settings, p["id"]), h


# ====================================================================== fork at a step
async def test_a_clean_30_step_run_forked_at_20_replays_the_prefix_and_goes_live(gw, fleet, store):
    model = Model()
    run = await record(gw, fleet, store, model)
    assert model.calls == list(range(1, STEPS + 1))
    model.calls.clear()
    p, h = await replay_it(gw, fleet, store, run, mode="replay-prefix", at_seq=20)
    assert p["divergence"] is None and p["status"] == "live"
    assert p["served"] == list(range(1, 21)) and p["verified_through"] == 21 and p["live_since"] == 21
    assert model.calls == list(range(21, STEPS + 1))  # steps 1-20 never reached the provider
    assert all(s == 200 for s in h.statuses)


async def test_a_test_that_reads_the_clock_diverges_as_a_tool_at_its_step(gw, fleet, store):
    run = await record(gw, fleet, store, Model())
    clock = iter(range(1000, 2000))

    def flaky(cmd: str) -> str:
        return f"$ {cmd}\nnow={next(clock)}" if cmd == "make step12" else deterministic(cmd)

    p, h = await replay_it(gw, fleet, store, run, flaky, mode="replay-prefix", at_seq=20)
    d = p["divergence"]
    assert (d["seq"], d["kind"], d["tool"], d["command"]) == (13, "tool", "Bash", "make step12")
    assert p["status"] == "diverged" and h.statuses[-1] == 400  # on_divergence: stop
    assert p["served"] == list(range(1, 13))


async def test_on_divergence_live_goes_live_at_the_divergence(gw, fleet, store):
    run = await record(gw, fleet, store, Model())
    p, _ = await replay_it(gw, fleet, store, run, lambda c: "different " + c if c == "make step3" else deterministic(c),
                           mode="replay-prefix", at_seq=20, on_divergence="live")
    assert p["divergence"]["seq"] == 4 and p["status"] == "live" and p["live_since"] == 4


async def test_a_network_step_diverges_as_network_without_egress(gw, fleet, store):
    run = await record(gw, fleet, store, Model({5: ("Bash", {"command": "curl -sf https://example.com/x"})}),
                       lambda c: "<html>ok</html>" if "curl" in c else deterministic(c))
    p, _ = await replay_it(gw, fleet, store, run,
                           lambda c: "curl: (6) Could not resolve host: example.com" if "curl" in c else
                           deterministic(c), mode="replay-prefix", at_seq=20)
    assert (p["divergence"]["seq"], p["divergence"]["kind"]) == (6, "network")


async def test_side_effecting_mcp_tools_are_served_from_the_tape(gw, fleet, store):
    model = Model({3: ("mcp__jira__create_issue", {"summary": "x"})})
    run = await record(gw, fleet, store, model)
    model.calls.clear()
    p, _ = await replay_it(gw, fleet, store, run, mode="replay-prefix", at_seq=10)
    assert p["divergence"] is None and p["live_since"] == 11
    # once live, the provider sees the taped Jira output, not the replay's "no such tool"
    r = replay.of(fleet).by_sandbox[p["sandbox_id"]]
    cid = next(raw for raw, ph in r.norm.ids.items() if ph in r.side)
    body = json.dumps({"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": cid, "content": "Error: No such tool available"}]}]}).encode()
    assert "created JIRA-7" in r.rewrite(body).decode()


async def test_a_different_system_prompt_is_a_model_divergence_at_step_1(gw, fleet, store):
    run = await record(gw, fleet, store, Model())
    p = replay.plan(fleet, run, mode="replay-prefix", at_seq=5)
    sb, _, _ = _sandbox(store, {replay.REPLAY_LABEL: p["id"]})
    token = gw.mint(sb, "anthropic", "anthropic-key")
    replay.of(fleet).bind(sb)
    h = Harness(deterministic)
    h.body = lambda msgs, b=h.body: {**b(msgs), "system": [{"type": "text", "text": "You are Codex."}]}
    await h.run(fleet, token)
    d = replay.load_plan(fleet.settings, p["id"])["divergence"]
    assert (d["seq"], d["kind"], d["detail"]) == (1, "model", "`system`")


async def test_strict_with_tolerate_tool_records_every_mismatch_and_serves_the_whole_tape(gw, fleet, store):
    model = Model()
    run = await record(gw, fleet, store, model)
    model.calls.clear()
    clock = iter(range(1000, 2000))

    def flaky(cmd: str) -> str:
        return f"$ {cmd}\nnow={next(clock)}" if cmd in ("make step4", "make step17") else deterministic(cmd)

    p, h = await replay_it(gw, fleet, store, run, flaky, mode="replay-strict", tolerate="tool")
    assert model.calls == [] and p["divergence"] is None
    assert [(m["seq"], m["command"]) for m in p["mismatches"]] == [(5, "make step4"), (18, "make step17")]
    assert p["served"] == list(range(1, STEPS + 1))


async def test_strict_without_tolerance_stops_at_the_first_miss(gw, fleet, store):
    run = await record(gw, fleet, store, Model())
    p, h = await replay_it(gw, fleet, store, run, lambda c: "x" if c == "make step2" else deterministic(c),
                           mode="replay-strict")
    assert p["divergence"]["seq"] == 3 and p["status"] == "diverged" and h.statuses[-1] == 400


async def test_a_replay_survives_a_control_plane_restart(gw, fleet, store):
    model = Model()
    run = await record(gw, fleet, store, model)
    p = replay.plan(fleet, run, mode="replay-prefix", at_seq=20)
    sb, _, _ = _sandbox(store, {replay.REPLAY_LABEL: p["id"]})
    token = gw.mint(sb, "anthropic", "anthropic-key")
    replay.of(fleet).bind(sb)
    h = Harness(deterministic, date="2026-10-18")
    await h.run(fleet, token, max_steps=7)
    replay.of(fleet).by_sandbox.clear()  # a restart: the plan on disk carries the served steps and the id map
    assert replay.of(fleet).get(sb.id).served == set(range(1, 8))
    await h.run(fleet, token)  # the harness carries on with its own conversation
    p = replay.load_plan(fleet.settings, p["id"])
    assert p["divergence"] is None and p["live_since"] == 21 and model.calls[-10:] == list(range(21, STEPS + 1))


async def test_tainted_tapes_make_tainted_replays(gw, fleet, store):
    run = await record(gw, fleet, store, Model())
    idx_path = replay.tape_dir(fleet.settings, run) / "index.json"
    idx = json.loads(idx_path.read_text())
    idx["untrusted"], idx["untrusted_reasons"] = True, ["the turn read untrusted content (taint)"]
    idx_path.write_text(json.dumps(idx))
    p = replay.plan(fleet, run, mode="replay-prefix", at_seq=3)
    assert p["tainted"] is True and p["untrusted_reasons"]


async def test_planning_refuses_a_missing_or_unfinished_tape_and_a_step_outside_it(gw, fleet, store):
    with pytest.raises(replay.ReplayError, match="has no tape"):
        replay.plan(fleet, "ses-00000000.run-00000000", mode="replay-prefix", at_seq=1)
    run = await record(gw, fleet, store, Model())
    with pytest.raises(replay.ReplayError, match="outside"):
        replay.plan(fleet, run, mode="replay-prefix", at_seq=STEPS + 1)
    with pytest.raises(replay.ReplayError, match="mode"):
        replay.plan(fleet, run, mode="replay-fast", at_seq=1)


async def test_the_prompt_comes_from_the_tape(gw, fleet, store):
    run = await record(gw, fleet, store, Model())
    assert replay.plan(fleet, run, mode="replay-prefix", at_seq=1)["prompt"] == "Fix the flaky build"


async def test_the_tape_index_maps_tool_ids_for_tree_checks(gw, fleet, store):
    run = await record(gw, fleet, store, Model())
    idx = json.loads((replay.tape_dir(fleet.settings, run) / "index.json").read_text())
    assert len(idx["tool_ids"]) == STEPS - 1 and all(v.startswith("<toolu:") for v in idx["tool_ids"].values())


# ====================================================================== trees
def _tree(tree, id=None, phase=None):
    return RunEvent(kind="progress", text="tree", data={"action": "tree", "tree": tree, "id": id, "phase": phase})


def test_tree_divergence_finds_the_first_edit_whose_tree_differs():
    entries = [{"seq": n, "resp": {"sse": [{"event": "content_block_start", "data": {
        "content_block": {"type": "tool_use", "id": f"<toolu:{n}>"}}}]}} for n in range(1, 6)]
    a, b = "a" * 40, "b" * 40
    orig = [_tree(a, phase="start"), _tree("1" * 40, "toolu_o1"), _tree("2" * 40, "toolu_o3")]
    o_ids = {"toolu_o1": "<toolu:1>", "toolu_o3": "<toolu:3>"}
    r_ids = {"toolu_r1": "<toolu:1>", "toolu_r3": "<toolu:3>"}
    same = [_tree(a, phase="start"), _tree("1" * 40, "toolu_r1"), _tree("2" * 40, "toolu_r3")]
    assert replay.tree_divergence(orig, same, o_ids, r_ids, entries, 5) is None
    other = [_tree(a, phase="start"), _tree("1" * 40, "toolu_r1"), _tree("9" * 40, "toolu_r3")]
    d = replay.tree_divergence(orig, other, o_ids, r_ids, entries, 5)
    assert (d["seq"], d["kind"], d["expected"], d["got"]) == (4, "tree", "2" * 40, "9" * 40)
    assert replay.tree_divergence(orig, other, o_ids, r_ids, entries, 3) is None  # past the prefix: not checked
    missing = [_tree(a, phase="start"), _tree("1" * 40, "toolu_r1")]  # the replay's tree didn't change there
    assert replay.tree_divergence(orig, missing, o_ids, r_ids, entries, 5)["got"] is None
    start = [_tree(b, phase="start")]
    assert replay.tree_divergence(orig, start, o_ids, r_ids, entries, 5)["detail"] == "the starting tree"


# ====================================================================== isolation
async def test_a_replay_sandbox_has_egress_none_and_no_credentials(gw, fleet, broker, store, tmp_path, monkeypatch):
    from hangar import egress

    async def proxy(fleet, prov, sb):  # the fake provider has no proxy-only network; Docker's and GKE's do
        return "http://egress.test:3128"

    monkeypatch.setattr(egress, "_proxy_url", proxy)
    fleet.catalog.save_dir = tmp_path
    fleet.catalog.add({"name": "rec", "harnesses": ["claude-code"], "model_gateway": "record",
                       "secrets": {"DB_PASSWORD": "db"}, "mcp": {"jira": {"url": "https://jira.example/mcp"}}})
    broker.put("db", "generic", CredentialKind.GENERIC, "hunter2-db-password-9f8e7d", env_var="DB_PASSWORD")
    broker.put("gh", "generic", CredentialKind.GENERIC, "ghp_" + "x" * 36, env_var="GH_TOKEN")
    run = await record(gw, fleet, store, Model())
    p = replay.plan(fleet, run, mode="replay-prefix", at_seq=3)
    sb = await fleet.create("rec", credentials=["anthropic-key", "db", "gh"], labels={replay.REPLAY_LABEL: p["id"]})
    assert sb.labels[posture.FLOOR_LABEL] == "none"
    assert egress.for_sandbox(fleet, sb).mode == "none"
    assert sb.credentials == ["anthropic-key"]  # created with the API key only, which the gateway took out
    env = (await fleet.read_file(sb.id, "/home/agent/.hangar/env")).decode()
    assert REAL_KEY not in env and "hunter2" not in env and "ghp_" not in env and "DB_PASSWORD" not in env
    assert "ANTHROPIC_BASE_URL=" in env and "hgm_" in env
    assert "mcp" not in sb.info
    assert sb.info["model_gateway"]["mode"] == "replay-prefix" and sb.info["model_gateway"]["replay"] == p["id"]
    assert replay.load_plan(fleet.settings, p["id"])["sandbox_id"] == sb.id
