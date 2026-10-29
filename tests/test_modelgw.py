"""#177: the record-mode model gateway. A `model_gateway: record` sandbox reaches its provider through
`/model/<provider>/...` with a per-sandbox bearer; the gateway sends the vault's API key upstream, forwards the body
untouched, streams the answer back and keeps a normalized, redacted, hash-chained tape of every call.

The proxy tests use a mock upstream; the latency bench and the real Claude Code turn use the scripted model API
(fixtures/live/fake_models.py) behind a real listener. The Claude Code test runs only with HANGAR_TEST_CLAUDE set:

    HANGAR_TEST_CLAUDE=$(command -v claude) uv run pytest tests/test_modelgw.py
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import os
import socket
import statistics
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from hangar import modelgw, outbox
from hangar.fleet import FleetError
from hangar.gateway import _Server
from hangar.models import (
    ContextItem,
    CredentialKind,
    Job,
    Sandbox,
    SandboxState,
    Session,
    SessionTurn,
)
from tests.fixtures.live import fake_models

REAL_KEY = "sk-ant-api03-REALKEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
SEEDED = "hunter2-db-password-9f8e7d"
UPSTREAM = "http://upstream.test"


@pytest.fixture
def gw(fleet, settings, broker):
    settings.gateway_url = "http://gw.test:8788"
    settings.model_gateway_upstreams = {"anthropic": UPSTREAM, "openai": UPSTREAM}
    broker.put("anthropic-key", "claude-code", CredentialKind.API_KEY, REAL_KEY)
    broker.put("db", "generic", CredentialKind.GENERIC, SEEDED, env_var="DB_PASSWORD")  # a stored secret
    return modelgw.of(fleet)


def _session(store, sb_id: str = "sb-0000aaaa", job: Job | None = None) -> tuple[Sandbox, Session, SessionTurn]:
    turn = SessionTurn(message="go", run_id="run-1234abcd", harness="claude-code", started_at=time.time())
    s = Session(name="rec", environment="e", harness="claude-code", turns=[turn], sandbox_id=sb_id,
                job_id=job.id if job else None)
    store.put(s)
    sb = Sandbox(id=sb_id, name=sb_id, environment="e", provider="fake", state=SandboxState.RUNNING,
                 labels={"hangar.session": s.id}, created_by="alice")
    store.put(sb)
    return sb, s, turn


def _sse(events: list[dict]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def _answer(n: int) -> bytes:
    return _sse([
        {"type": "message_start", "message": {"id": f"msg_01ABCDEFGH{n}xyz", "type": "message", "role": "assistant",
                                              "content": [], "usage": {"input_tokens": 100 + n, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": f"toolu_01QWERTY{n}abcd", "name": "Bash", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta",
                                                              "partial_json": '{"command": "ls /tmp/x123"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20 + n}},
        {"type": "message_stop"}])


class Upstream:
    def __init__(self):
        self.requests: list[tuple[httpx.Request, bytes]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.requests.append((request, body))
        if request.url.path.endswith("/count_tokens"):
            async def small():
                yield b'{"input_tokens": 3}'

            return httpx.Response(200, content=small(), headers={"content-type": "application/json"})

        async def chunks():
            raw = _answer(len(self.requests))
            for i in range(0, len(raw), 97):  # the provider streams; the gateway relays as it comes
                yield raw[i:i + 97]

        return httpx.Response(200, content=chunks(), headers={"content-type": "text/event-stream",
                                                              "request-id": "req_123"})


def _upstream(fleet) -> Upstream:
    up = Upstream()
    fleet.gateway.transport = httpx.MockTransport(up)
    fleet.gateway._client = None
    return up


def _client(fleet) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=fleet.gateway.asgi_app()), base_url="http://gw.test")


def _conversation(n: int) -> dict:
    """Claude Code's n-th request of a turn: the same system prompt and tools, the messages growing."""
    msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "RUN fix it",
                                                      "cache_control": {"type": "ephemeral"}}]}]
    for i in range(1, n):
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"toolu_01QWERTY{i}abcd",
                                                     "name": "Bash", "input": {"command": "ls /tmp/x123"}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_01QWERTY{i}abcd",
                                               "content": f"DB_PASSWORD={SEEDED} at 2026-10-17T09:15:02Z "
                                                          f"in sb-0000aaaa"}]}]
    return {"model": "claude-opus-5-5", "max_tokens": 32000, "stream": True,
            "system": [{"type": "text", "text": "You are Claude Code. " * 200, "cache_control": {"type": "ephemeral"}}],
            "tools": [{"name": "Bash", "description": "Run a command. " * 100, "input_schema": {"type": "object"}}],
            "messages": msgs}


async def _turn(gw, fleet, token: str, calls: int = 3) -> list[bytes]:
    out = []
    async with _client(fleet) as c:
        for n in range(1, calls + 1):
            body = json.dumps(_conversation(n)).encode()
            r = await c.post("/model/anthropic/v1/messages?beta=true", content=body, headers={
                "x-api-key": token, "anthropic-version": "2023-06-01", "anthropic-beta": "prompt-caching-scope-2026",
                "content-type": "application/json", "x-stainless-retry-count": str(n), "user-agent": "claude-cli/2"})
            assert r.status_code == 200, r.text
            out.append(r.content)
    await gw.settle()
    return out


# ====================================================================== the proxy and the tape
async def test_a_turn_through_the_gateway_records_a_valid_tape(gw, fleet, store, settings):
    up = _upstream(fleet)
    sb, s, turn = _session(store)
    token = gw.mint(sb, "anthropic", "anthropic-key")
    answers = await _turn(gw, fleet, token)
    # the provider got the vault's key, the body byte for byte (cache_control untouched) and the beta header
    for n, (req, body) in enumerate(up.requests, 1):
        assert req.headers["x-api-key"] == REAL_KEY and token not in str(req.headers)
        assert body == json.dumps(_conversation(n)).encode()
        assert req.headers["anthropic-beta"] == "prompt-caching-scope-2026"
        assert str(req.url) == f"{UPSTREAM}/v1/messages?beta=true"
        assert req.headers["accept-encoding"] == "identity"
    assert answers == [_answer(n) for n in (1, 2, 3)]  # streamed back unchanged
    turn.finished_at = time.time()
    tape = await gw.turn_ended(s, turn)
    assert tape == settings.data_dir / "tapes" / f"{s.id}.{turn.run_id}" / "tape"
    index = modelgw.validate(tape)
    assert (index["entries"], index["provider"], index["model"], index["untrusted"]) == (3, "anthropic",
                                                                                         "claude-opus-5-5", False)
    assert index["final"] and index["run"] == f"{s.id}.{turn.run_id}" and index["harness"] == "claude-code"
    entries = [json.loads(p.read_text()) for p in sorted(tape.glob("0*.json"))]
    assert "full" in entries[0]["req_delta"]
    assert entries[1]["req_delta"]["base"] == 1 and len(entries[1]["req_delta"]["append"]) == 2
    assert entries[2]["req_delta"]["base"] == 2 and "set" not in entries[2]["req_delta"]
    assert entries[0]["usage"] == {"input_tokens": 101, "output_tokens": 21}
    raw = "".join(p.read_text() for p in tape.glob("0*.json"))
    # redacted and normalized before hashing: the seeded secret, ids, timestamps, temp paths and hangar's ids
    assert SEEDED not in raw and REAL_KEY not in raw and token not in raw
    assert "toolu_01QWERTY" not in raw and "<toolu:1>" in raw and "msg_01ABCDEFGH" not in raw
    assert "2026-10-17T09:15:02Z" not in raw and "<ts>" in raw
    assert "/tmp/x123" not in raw and "<tmp>" in raw and "sb-0000aaaa" not in raw
    req0 = entries[0]["req_delta"]["full"]
    assert req0["headers"] == {"anthropic-version": "2023-06-01", "anthropic-beta": "prompt-caching-scope-2026",
                               "content-type": "application/json"}  # volatile ones dropped
    assert [e["event"] for e in entries[0]["resp"]["sse"]][-1] == "message_stop"


async def test_a_changed_dropped_or_reordered_entry_breaks_the_chain(gw, fleet, store):
    _upstream(fleet)
    sb, s, turn = _session(store)
    await _turn(gw, fleet, gw.mint(sb, "anthropic", "anthropic-key"))
    tape = await gw.turn_ended(s, turn)
    modelgw.validate(tape)
    p2 = tape / "00002.json"
    good = p2.read_text()
    e = json.loads(good)
    e["resp"]["sse"][1]["data"]["content_block"]["name"] = "Edit"
    p2.write_text(json.dumps(e))
    with pytest.raises(ValueError, match="entry 2: the response hash"):
        modelgw.validate(tape)
    e = json.loads(good)
    e["resp_hash"] = modelgw.digest(e["resp"] | {"x": 1})
    p2.write_text(good)
    (tape / "00003.json").rename(tape / "00004.json")
    with pytest.raises(ValueError, match="out of sequence"):
        modelgw.validate(tape)
    (tape / "00004.json").rename(tape / "00003.json")
    e = json.loads((tape / "00003.json").read_text())
    e["req_delta"]["append"][0]["content"][0]["input"] = {"command": "rm -rf /"}
    (tape / "00003.json").write_text(json.dumps(e))
    with pytest.raises(ValueError, match="entry 3: the request hash"):
        modelgw.validate(tape)


async def test_only_a_live_grant_for_that_provider_gets_through(gw, fleet, store):
    up = _upstream(fleet)
    sb, s, _ = _session(store)
    token = gw.mint(sb, "anthropic", "anthropic-key")
    async with _client(fleet) as c:
        r = await c.post("/model/anthropic/v1/messages", json={"messages": []}, headers={"x-api-key": "hgm_nope"})
        assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
        assert (await c.post("/model/anthropic/v1/messages", json={})).status_code == 401
        assert (await c.post("/model/openai/v1/responses", json={},
                             headers={"authorization": f"Bearer {token}"})).status_code == 401
        assert (await c.post("/model/gemini/v1/x", json={}, headers={"x-api-key": token})).status_code == 404
        # count_tokens and model lists are forwarded, not recorded
        r = await c.post("/model/anthropic/v1/messages/count_tokens", json={"messages": []},
                         headers={"x-api-key": token})
        assert r.json() == {"input_tokens": 3}
        sb.state = SandboxState.STOPPED
        store.put(sb)
        assert (await c.post("/model/anthropic/v1/messages", json={},
                             headers={"x-api-key": token})).status_code == 401
    assert len(up.requests) == 1
    await gw.settle()
    assert not gw.tapes
    fleet.gateway.revoke(sb.id)  # the git gateway's revoke takes the model grant too
    assert gw.grants(sb.id) == []


# --- KAN-197: a call that ends early is still on the tape, and settle() can't hang
@pytest.mark.parametrize("how", ["cancelled", "disconnected"])
async def test_a_call_cut_off_mid_stream_is_recorded_before_settle_returns(gw, fleet, store, how):
    _upstream(fleet)
    sb, s, turn = _session(store)
    token = gw.mint(sb, "anthropic", "anthropic-key")
    body = json.dumps(_conversation(1)).encode()
    first_chunk = asyncio.Event()
    asked = False

    async def receive():
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()  # the client never says it left

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()
            if how == "disconnected":
                raise OSError("the client went away")
            await asyncio.Event().wait()  # the client stops reading; the relay blocks here until cancelled

    path = "/model/anthropic/v1/messages"
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 40000), "server": ("gw.test", 80),
             "headers": [(b"host", b"gw.test"), (b"x-api-key", token.encode()),
                         (b"anthropic-version", b"2023-06-01"), (b"content-type", b"application/json")]}
    call = asyncio.create_task(fleet.gateway.asgi_app()(scope, receive, send))
    await asyncio.wait_for(first_chunk.wait(), 5)
    if how == "cancelled":
        call.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await call
    await gw.settle(timeout=5)
    tape = await gw.turn_ended(s, turn)
    assert tape is not None, "the cut-off call never reached the tape"
    [entry] = [json.loads(p.read_text()) for p in sorted(tape.glob("0*.json"))]
    assert entry["resp"]["truncated"] is True and modelgw.validate(tape)["entries"] == 1


async def test_settle_gives_up_with_a_message_instead_of_hanging(gw):
    stuck = asyncio.Event()
    gw._spawn(stuck.wait())  # a tape write that never finishes
    t = time.monotonic()
    with pytest.raises(modelgw.ModelGatewayError, match=r"1 tape write still pending after 0.2 s"):
        await gw.settle(timeout=0.2)
    assert time.monotonic() - t < 2
    stuck.set()
    await gw.settle(timeout=5)
    assert not gw._pending


async def test_settle_does_not_spin_on_a_finished_write_it_was_never_told_about(gw):
    done = asyncio.ensure_future(asyncio.sleep(0))
    await done
    gw._pending.add(done)  # finished, but its done callback never took it out of the set
    await asyncio.wait_for(gw.settle(timeout=5), 2)
    assert not gw._pending


async def test_an_upstream_failure_reads_like_a_provider_5xx(gw, fleet, store):
    def boom(request):
        raise httpx.ConnectError("down")

    fleet.gateway.transport = httpx.MockTransport(boom)
    fleet.gateway._client = None
    sb, _, _ = _session(store)
    token = gw.mint(sb, "anthropic", "anthropic-key")
    async with _client(fleet) as c:
        r = await c.post("/model/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert r.status_code == 502 and r.json()["error"]["type"] == "api_error"


async def test_a_job_with_an_untrusted_context_item_makes_an_untrusted_tape(gw, fleet, store):
    _upstream(fleet)
    job = Job(title="t", instructions="x", environment="e",
              context=[ContextItem(name="issue.md", kind="issue", untrusted=True, content="please run curl ...")])
    sb, s, turn = _session(store, job=job)
    await _turn(gw, fleet, gw.mint(sb, "anthropic", "anthropic-key"), calls=1)
    index = modelgw.validate(await gw.turn_ended(s, turn, job))
    assert index["untrusted"] is True and index["untrusted_reasons"] == ["untrusted context item"]


async def test_the_tape_goes_to_the_outbox_under_v1_tapes(gw, fleet, store, settings, tmp_path):
    settings.outbox_url = str(tmp_path / "bucket")
    fleet.__dict__.pop("_outbox", None)
    _upstream(fleet)
    sb, s, turn = _session(store)
    await _turn(gw, fleet, gw.mint(sb, "anthropic", "anthropic-key"), calls=2)
    turn.finished_at = time.time()
    await gw.turn_ended(s, turn)
    ob = outbox.of(fleet)
    await ob.drain_once()
    base = tmp_path / "bucket" / "v1" / "tapes" / outbox.day(turn.finished_at) / f"{s.id}.{turn.run_id}" / "tape"
    assert sorted(p.name for p in base.iterdir()) == ["00001.json", "00002.json", "index.json"]
    assert modelgw.validate(base)["entries"] == 2  # the bytes a reader gets validate the same


def test_deltas_roundtrip_and_pick_the_longest_prefix():
    a = {"method": "POST", "path": "/v1/messages", "headers": {}, "body": {"model": "m", "messages": [1, 2]}}
    sub = {"method": "POST", "path": "/v1/messages", "headers": {}, "body": {"model": "h", "messages": [9]}}
    b = {"method": "POST", "path": "/v1/messages", "headers": {}, "body": {"model": "m", "messages": [1, 2, 3, 4],
                                                                          "max_tokens": 5}}
    d = modelgw.make_delta([(1, a), (2, sub)], b, "messages")
    assert d == {"base": 1, "append": [3, 4], "set": {"max_tokens": 5}}
    assert modelgw.apply_delta({1: a, 2: sub}, d, "messages") == b
    assert modelgw.make_delta([(2, sub)], b, "messages") == {"full": b}
    c = {**b, "body": {"messages": [1, 2, 3]}}
    d = modelgw.make_delta([(1, b)], c, "messages")
    assert "full" in d  # not an extension
    n = modelgw.Normalizer()
    assert n("call_abc123XYZ then call_abc123XYZ and toolu_9xyzwvu1") == "<call:1> then <call:1> and <toolu:1>"


# ====================================================================== the sandbox's side
def _record_env(fleet, tmp_path):
    fleet.catalog.save_dir = tmp_path
    fleet.catalog.add({"name": "rec", "harnesses": ["claude-code"], "model_gateway": "record"})


async def test_the_sandbox_gets_the_gateway_and_a_bearer_never_the_key(gw, fleet, tmp_path):
    _record_env(fleet, tmp_path)
    sb = await fleet.create("rec", credentials=["anthropic-key"])
    env = (await fleet.read_file(sb.id, "/home/agent/.hangar/env")).decode()
    assert "ANTHROPIC_BASE_URL=" in env and "http://gw.test:8788/model/anthropic" in env
    assert "hgm_" in env and REAL_KEY not in env
    assert sb.info["model_gateway"] == {"mode": "record", "provider": "anthropic", "credential": "anthropic-key"}
    [grant] = gw.grants(sb.id)
    assert grant.provider == "anthropic" and grant.credential == "anthropic-key"
    await fleet.destroy(sb.id)
    assert gw.grants(sb.id) == []


async def test_record_refuses_subscription_logins_and_needs_the_gateway(gw, fleet, broker, settings, tmp_path):
    _record_env(fleet, tmp_path)
    broker.put("sub", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-" + "x" * 40)
    with pytest.raises(FleetError, match="subscription login, which can't go through a recording proxy"):
        await fleet.create("rec", credentials=["sub"])
    settings.gateway_url = None
    with pytest.raises(FleetError, match="HANGAR_GATEWAY_URL"):
        await fleet.create("rec", credentials=["anthropic-key"])


# ====================================================================== benches on a real listener
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def live(gw, fleet, settings):
    srv, up_port = fake_models.serve()
    settings.model_gateway_upstreams = {"anthropic": f"http://127.0.0.1:{up_port}"}
    port = _free_port()
    server = _Server(fleet.gateway.asgi_app(), "127.0.0.1", port)
    server.start()
    for _ in range(100):
        try:
            async with httpx.AsyncClient() as c:
                await c.get(f"http://127.0.0.1:{port}/healthz")
            break
        except httpx.HTTPError:
            await asyncio.sleep(0.05)
    yield {"gw": f"http://127.0.0.1:{port}", "upstream": f"http://127.0.0.1:{up_port}", "fake": srv}
    await server.stop()
    srv.shutdown()


async def test_calls_through_a_real_listener_all_reach_the_tape(live, gw, fleet, store):
    """The bench's path without its timing, so it runs in every parallel shard too (KAN-197)."""
    sb, s, turn = _session(store)
    token = gw.mint(sb, "anthropic", "anthropic-key")
    body = json.dumps(_conversation(4)).encode()
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json", "x-api-key": token}
    async with httpx.AsyncClient(timeout=30) as c:
        for _ in range(5):
            r = await c.post(live["gw"] + "/model/anthropic/v1/messages", content=body, headers=headers)
            assert r.status_code == 200
    await gw.settle()
    tape = await gw.turn_ended(s, turn)
    assert modelgw.validate(tape)["entries"] == 5


@pytest.mark.needs("quiet_cpu")  # KAN-197: a p50 taken beside other xdist workers times the load
async def test_bench_the_gateway_adds_under_50ms_p50(live, gw, fleet, store):
    sb, s, turn = _session(store)
    token = gw.mint(sb, "anthropic", "anthropic-key")
    body = json.dumps(_conversation(4)).encode()
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}

    async def times(base: str, key: str) -> list[float]:
        out = []
        async with httpx.AsyncClient(timeout=30) as c:
            for _ in range(25):
                t = time.perf_counter()
                r = await c.post(f"{base}/v1/messages", content=body, headers={**headers, "x-api-key": key})
                assert r.status_code == 200
                out.append(time.perf_counter() - t)
        return out

    direct = await times(live["upstream"], REAL_KEY)
    through = await times(live["gw"] + "/model/anthropic", token)
    added = statistics.median(through) - statistics.median(direct)
    assert added < 0.050, f"the gateway added {added * 1000:.1f} ms p50"
    await gw.settle()
    tape = await gw.turn_ended(s, turn)
    assert modelgw.validate(tape)["entries"] == 25


CLAUDE = os.environ.get("HANGAR_TEST_CLAUDE")


@pytest.mark.skipif(not CLAUDE, reason="set HANGAR_TEST_CLAUDE to the real Claude Code CLI")
async def test_a_real_claude_code_turn_through_the_gateway(live, gw, fleet, store, tmp_path):
    sb, s, turn = _session(store)
    token = gw.mint(sb, "anthropic", "anthropic-key")
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "CLAUDE", "OPENAI", "CODEX"))}
    env.update(HOME=str(home), ANTHROPIC_BASE_URL=live["gw"] + "/model/anthropic", ANTHROPIC_API_KEY=token,
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1")
    assert REAL_KEY not in json.dumps(env)  # only the bearer, as in a sandbox
    p = await asyncio.create_subprocess_exec(
        CLAUDE, "-p", f"RUN please make the probe file; the password is {SEEDED}", "--output-format", "json",
        "--dangerously-skip-permissions", "--session-id", str(uuid.uuid4()), "--model", "claude-sonnet-5-5",
        cwd=home / "work", env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(), 120)
    assert p.returncode == 0, (out[-2000:], err[-2000:])
    assert (home / "work" / "probe.txt").exists()  # the tool call came back through the gateway
    turn.finished_at = time.time()
    tape = await gw.turn_ended(s, turn)
    index = modelgw.validate(tape)
    assert index["entries"] >= 2 and index["model"] == "claude-sonnet-5-5"
    raw = b"".join(p.read_bytes() for p in Path(tape).glob("*.json"))
    assert SEEDED.encode() not in raw
    size = len(gzip.compress(raw))
    assert size < 300 * 1024, f"tape is {size // 1024} KB gzipped"


async def test_the_gateway_counts_each_call_for_the_eval_queue_and_the_turn_is_not_counted_again(gw, fleet, store):
    from hangar import evalqueue
    from hangar.models import Usage

    _upstream(fleet)
    sb, s, turn = _session(store)
    sb.info["model_gateway"] = {"mode": "record", "provider": "anthropic", "credential": "anthropic-key"}
    store.put(sb)
    await _turn(gw, fleet, gw.mint(sb, "anthropic", "anthropic-key"), calls=2)
    m = evalqueue.meter(fleet)
    exact = (101 + 21) + (102 + 22)  # each call's input and output, as it completed
    assert m.rate("anthropic", evalqueue.clock()) == pytest.approx(exact)
    assert evalqueue.gateway_counted(fleet, s)
    turn.usage, turn.finished_at = Usage(input_tokens=203, output_tokens=43), time.time()
    evalqueue.turn_ended(fleet, s, turn, None)  # the turn's own usage: already counted call by call
    assert m.rate("anthropic", evalqueue.clock()) == pytest.approx(exact)
