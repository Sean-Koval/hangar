"""Streaming to MCP and REST clients (workstream stream): exec output as progress notifications and SSE, background
processes, live turn events, file watches; their caps, owners and closes. Sandboxes are the fake provider's, with a
real hangar-envd daemon on this machine where the test needs one (`envd/build.sh`), as in test_envd.py."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import mcp_types as types
import pytest
from mcp import Client

from hangar import metrics, sandbox_streams, stream_api
from hangar.events import Activity, RunEvent, TurnWatcher
from hangar.mcp_server import build_mcp, current_actor
from hangar.models import Session, SessionTurn
from hangar.streams import StreamClosed, StreamLimit, hub
from tests.test_envd import BINARIES, LocalEnvd, free_port, kill_daemon, needs_binary
from tests.test_mcp_modern import AppThread

STREAMS = Path(__file__).parent / "fixtures" / "streams"
TICKS = "for i in $(seq 1 10); do echo tick$i; sleep 0.1; done"


class SharedEnvd(LocalEnvd):
    """One daemon on this machine for all of a test's sandboxes (so one token for them all)."""

    def token(self, sb):
        return super().token(type(sb)(id="sb-shared", name="x", environment="x", provider="local"))


@pytest.fixture
def envd_fleet(fleet, tmp_path):
    """The fleet's fake provider with a real daemon: commands run on this machine, in `tmp_path`."""
    s = fleet.settings
    s.workspace_dir = str(tmp_path)
    s.envd_dir = next(iter(BINARIES.values())).parent if BINARIES else None
    s.stream_recheck_seconds = 0.05
    port = free_port()
    fleet.providers["docker"].envd = SharedEnvd(s, port)
    yield fleet
    kill_daemon(port)


async def sandbox(fleet, name: str = "s1"):
    sb = await fleet.create("plain", name=name)
    envd = getattr(fleet.providers["docker"], "envd", None)
    if envd is not None:
        assert await envd.ready(fleet.get(sb.id)) is not None
    return sb


def text_of(res: types.CallToolResult) -> dict:
    assert not res.is_error, res.content[0].text
    return res.structured_content


# ---------------------------------------------------------------------------- exec(stream=true) over MCP
@needs_binary
async def test_mcp_exec_streams_output_in_order_before_it_ends(envd_fleet):
    fleet = envd_fleet
    sb = await sandbox(fleet)
    got: list[tuple[float, float, str]] = []
    t0 = time.monotonic()

    async def progress(p: float, total: float | None, message: str | None) -> None:
        got.append((time.monotonic() - t0, p, message or ""))

    async with Client(build_mcp(fleet)) as c:
        res = text_of(await c.call_tool("exec", {"sandbox": sb.id, "command": TICKS + "; echo oops >&2",
                                                 "stream": True}, progress_callback=progress))
    took = time.monotonic() - t0
    assert res["exit_code"] == 0 and res["streamed"] is True
    assert res["stdout"] == "".join(f"tick{i}\n" for i in range(1, 11)) and res["stderr"] == "oops\n"
    text = "".join(m for _, _, m in got)
    assert text == res["stdout"] + "oops\n"  # every chunk, in order, nothing twice
    assert [p for _, p, _ in got] == sorted(p for _, p, _ in got)  # progress only grows
    assert got[0][0] < took - 0.5, (got[0][0], took)  # the first line arrived while the command still ran


@needs_binary
async def test_streamed_exec_is_killed_when_the_caller_goes_away(envd_fleet, tmp_path):
    fleet = envd_fleet
    sb = await sandbox(fleet)
    st_cm = hub(fleet).open("alice", "exec", sb.id)
    st = await st_cm.__aenter__()
    seen = asyncio.Event()

    async def emit(kind, data):
        seen.set()

    task = asyncio.create_task(sandbox_streams.run(
        fleet, sb.id, f"echo started; sleep 30; touch {tmp_path}/survived", cwd=None, timeout=60, actor="t",
        st=st, emit=emit))
    await asyncio.wait_for(seen.wait(), 10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await st_cm.__aexit__(None, None, None)
    ep = await fleet.providers["docker"].envd.ready(fleet.get(sb.id))
    for _ in range(40):  # review(#68): the daemon lists a killed process until it has reaped it
        listed = await fleet.providers["docker"].envd.unary(ep, "process.Process/List", {})
        if not [p for p in listed.get("processes", []) if "sleep 30" in json.dumps(p)]:
            break
        await asyncio.sleep(0.05)
    assert not [p for p in listed.get("processes", []) if "sleep 30" in json.dumps(p)]


# ---------------------------------------------------------------------------- caps, closes and owners
@needs_binary
async def test_caps_per_token_bytes_and_rate(envd_fleet):
    fleet = envd_fleet
    s = fleet.settings
    sb = await sandbox(fleet)
    s.stream_max_per_token = 1
    async with hub(fleet).open("alice", "exec", sb.id):
        with pytest.raises(StreamLimit, match="already has 1 open streams"):
            async with hub(fleet).open("alice", "exec", sb.id):
                pass
        async with hub(fleet).open("bob", "exec", sb.id):  # another token has its own allowance
            pass

    s.stream_max_bytes = 1 << 16
    sent: list[bytes] = []

    async def emit(kind, data):
        sent.append(data)

    async with hub(fleet).open("alice", "exec", sb.id) as st:
        res = await sandbox_streams.run(fleet, sb.id, "head -c 300000 /dev/zero | tr '\\0' x; echo; echo end",
                                        cwd=None, timeout=30, actor="t", st=st, emit=emit)
    assert sum(map(len, sent)) <= 1 << 16 and "HANGAR_STREAM_MAX_BYTES" in res["note"]
    assert res["exit_code"] == 0 and res["stdout"].endswith("end\n")  # the result still has the end

    s.stream_max_bytes, s.stream_bytes_per_second = 64 << 20, 64 << 10
    t0 = time.monotonic()
    async with hub(fleet).open("alice", "exec", sb.id) as st:
        await sandbox_streams.run(fleet, sb.id, "head -c 200000 /dev/zero", cwd=None, timeout=30, actor="t", st=st,
                                  emit=emit)
    assert time.monotonic() - t0 >= 1.8  # 200 KB at 64 KB/s, with a one-second burst


@needs_binary
async def test_streams_close_on_destroy_shutdown_and_revocation(envd_fleet):
    fleet = envd_fleet
    sb = await sandbox(fleet)
    started = asyncio.Event()

    async def long_exec(st):
        async def emit(kind, data):
            started.set()
        return await sandbox_streams.run(fleet, sb.id, "echo hi; sleep 20", cwd=None, timeout=60, actor="t", st=st,
                                         emit=emit)

    async def under_way(st):
        """The command, once its first output has reached hangar. A fixed 0.5 s sleep raced the command's start on
        a loaded runner (#250): closed before `hi` arrived, the stream rightly answered with no output."""
        started.clear()
        task = asyncio.create_task(long_exec(st))
        await asyncio.wait_for(started.wait(), 10)
        return task

    # destroy: the stream answers at once with what it has
    async with hub(fleet).open("alice", "exec", sb.id) as st:
        task = await under_way(st)
        t0 = time.monotonic()
        await fleet.destroy(sb.id)
        res = await asyncio.wait_for(task, 10)
    assert res["closed"] == "destroyed" and res["stdout"] == "hi\n" and time.monotonic() - t0 < 5

    # revocation: the token's check fails, the guard closes the stream within a recheck interval
    sb = await sandbox(fleet, "s2")
    valid = [True]

    async def check():
        return valid[0]

    async with hub(fleet).open("alice", "exec", sb.id, check=check) as st:
        task = await under_way(st)
        valid[0] = False
        res = await asyncio.wait_for(task, 10)
    assert res["closed"] == "revoked" and "revoked" in res["note"]

    # shutdown (#57): every open stream ends, and no new one opens
    async with hub(fleet).open("alice", "exec", sb.id) as st:
        task = await under_way(st)
        fleet.closing.set()
        res = await asyncio.wait_for(task, 10)
    assert res["closed"] == "shutdown" and "keeps running" in res["note"]
    with pytest.raises(StreamLimit, match="restarting"):
        async with hub(fleet).open("alice", "exec", sb.id):
            pass


async def test_stream_race_and_metrics(fleet):
    m = metrics.setup(True)
    try:
        async with hub(fleet).open("alice", "events") as st:
            assert await st.race(asyncio.sleep(0, "ok")) == "ok"
            assert await st.send(10)
            st.close("revoked")
            with pytest.raises(StreamClosed, match="revoked"):
                await st.race(asyncio.sleep(10))
        assert m.streams.labels("events", "revoked")._value.get() == 1
        assert m.stream_bytes.labels("events")._value.get() == 10
        assert m.streams_open.labels("events")._value.get() == 0
        assert not hub(fleet).streams
    finally:
        metrics.setup(False)


# ---------------------------------------------------------------------------- background processes
ATTACH_DEADLINE = 10.0  # how long the tests wait for a background process's output to reach hangar (#120)


async def _attach_until(procs, p, want: str, fleet) -> str:
    """The process's output, attaching again from `next` until it contains `want` or ATTACH_DEADLINE passes. A
    single fixed window raced the daemon on a loaded CI runner (#118, #120)."""
    out, since, deadline = "", None, time.monotonic() + ATTACH_DEADLINE
    while want not in out and time.monotonic() < deadline:
        async with hub(fleet).open("alice", "process_attach", p.sandbox_id) as st:
            a = await procs.attach(p, since, 1, st, None)
        out, since = out + a["output"], a["next"]
    return out


@needs_binary
async def test_background_processes_over_mcp(envd_fleet):
    fleet = envd_fleet
    sb = await sandbox(fleet)
    tok = current_actor.set("alice")
    try:
        async with Client(build_mcp(fleet)) as c:
            p = text_of(await c.call_tool("process", {"sandbox": sb.id, "action": "start",
                                                      "command": "echo ready; while read l; do echo got:$l; done"}))
            assert p["state"] == "running" and p["transport"] == "envd" and p["id"].startswith("bg-")
            pid = p["id"]
            text_of(await c.call_tool("process", {"sandbox": sb.id, "action": "input", "process_id": pid,
                                                  "input": "one\n"}))
            got: list[str] = []

            async def progress(p_, total, message):
                got.append(message or "")

            # Attach again from `next` until the output is in: on a loaded runner it can take longer than one short
            # attach for the first bytes to reach hangar (#120), and an attach that sees none is correctly empty.
            out, a, deadline = "", {"next": None}, time.monotonic() + ATTACH_DEADLINE
            while out != "ready\ngot:one\n" and time.monotonic() < deadline:
                a = text_of(await c.call_tool("process", {"sandbox": sb.id, "action": "attach", "process_id": pid,
                                                          "since": a["next"], "wait_seconds": 1},
                                              progress_callback=progress))
                out += a["output"]
            assert out == "ready\ngot:one\n" and "".join(got) == out and a["state"] == "running"
            text_of(await c.call_tool("process", {"sandbox": sb.id, "action": "input", "process_id": pid,
                                                  "input": "two\n", "eof": True}))
            b = text_of(await c.call_tool("process", {"sandbox": sb.id, "action": "attach", "process_id": pid,
                                                      "since": a["next"], "wait_seconds": 10}))  # ends at exit
            assert b["output"] == "got:two\n" and b["state"] == "exited" and b["exit_code"] == 0
            listed = text_of(await c.call_tool("process", {"sandbox": sb.id}))["processes"]
            assert [x["id"] for x in listed] == [pid]

            long = text_of(await c.call_tool("process", {"sandbox": sb.id, "action": "start",
                                                         "command": "trap '' TERM; sleep 60"}))
            killed = text_of(await c.call_tool("process", {"sandbox": sb.id, "action": "kill",
                                                           "process_id": long["id"]}))
            assert killed["state"] == "exited"  # SIGTERM ignored: SIGKILL after the grace
        current_actor.set("bob")  # another token sees none of alice's processes
        async with Client(build_mcp(fleet)) as c:
            assert text_of(await c.call_tool("process", {"sandbox": sb.id}))["processes"] == []
            res = await c.call_tool("process", {"sandbox": sb.id, "action": "kill", "process_id": pid})
            assert res.is_error and "no background process" in res.content[0].text
    finally:
        current_actor.reset(tok)


@needs_binary
async def test_background_process_is_found_again_after_a_restart(envd_fleet):
    fleet = envd_fleet
    sb = await sandbox(fleet)
    procs = sandbox_streams.processes(fleet)
    p = await procs.start("alice", sb.id, "while true; do echo beat; sleep 0.2; done")
    await procs.close()  # the control plane goes away; the process keeps running in the sandbox
    sandbox_streams._PROCS.pop(fleet)
    again = sandbox_streams.processes(fleet)
    assert await again.list("bob", sb.id) == []
    [found] = await again.list("alice", sb.id)
    assert found["id"] == p.id and found["adopted"] and found["state"] == "running"
    assert "beat" in await _attach_until(again, again.procs[p.id], "beat", fleet)
    # a pause ends hangar's following of it; after the resume it is found again, still running
    await fleet.pause(sb.id)
    await asyncio.sleep(0.2)
    assert p.id not in again.procs
    await fleet.resume(sb.id)
    [found] = await again.list("alice", sb.id)
    assert found["id"] == p.id and found["state"] == "running"
    assert (await again.kill(again.procs[p.id]))["state"] == "exited"


async def test_background_processes_without_a_daemon(fleet):
    """No envd: the command runs detached with its output in a file, polled; input needs the daemon."""
    sb = await fleet.create("plain", name="nod")
    procs = sandbox_streams.processes(fleet)
    p = await procs.start("alice", sb.id, "echo hi; sleep 0.3; echo bye; exit 3")
    assert p.transport == "exec"
    async with hub(fleet).open("alice", "process_attach", sb.id) as st:
        a = await procs.attach(p, None, 10, st, None)
    assert a["output"] == "hi\nbye\n" and a["state"] == "exited" and a["exit_code"] == 3
    q = await procs.start("alice", sb.id, "sleep 30")
    with pytest.raises(Exception, match="needs hangar-envd"):
        await procs.send_input(q, b"x")
    assert (await procs.kill(q, grace=1))["state"] == "exited"
    fleet.settings.background_max_per_token = 1
    await procs.start("alice", sb.id, "sleep 30")
    with pytest.raises(StreamLimit, match="background processes"):
        await procs.start("alice", sb.id, "sleep 30")
    await fleet.destroy(sb.id)
    await asyncio.sleep(0.1)
    assert all(x.state != "running" for x in procs.procs.values())


async def test_exec_stream_falls_back_without_a_daemon(fleet):
    sb = await fleet.create("plain", name="nod")
    got: list[str] = []

    async def progress(p, total, message):
        got.append(message or "")

    async with Client(build_mcp(fleet)) as c:
        res = text_of(await c.call_tool("exec", {"sandbox": sb.id, "command": "echo a; echo b", "stream": True},
                                        progress_callback=progress))
    assert res["streamed"] is False and res["stdout"] == "a\nb\n" and got == ["a\nb\n"]


# ---------------------------------------------------------------------------- turn events, live
def _session(fleet, sb_id: str) -> tuple[Session, SessionTurn]:
    turn = SessionTurn(message="m", run_id="run-0000beef", harness="claude-code")
    s = Session(name="live", environment="plain", sandbox_id=sb_id, turns=[turn], harness="claude-code")
    return s, turn


@needs_binary
async def test_turn_events_are_recorded_as_the_log_is_written(envd_fleet, tmp_path):
    """The watcher's first poll starts following the log through the daemon; later lines become events without
    another poll, and nothing is recorded twice when polling takes over at the end."""
    fleet = envd_fleet
    sb = await sandbox(fleet)
    activity = Activity(fleet.settings, lambda x: x)
    seen: list[RunEvent] = []
    activity.listeners.append(lambda key, evs: seen.extend(evs))
    s, turn = _session(fleet, sb.id)
    w = TurnWatcher(activity, fleet, s, turn, lambda: None)
    log = tmp_path / "run.log"
    w.log_path = str(log)
    lines = (STREAMS / "claude-code.jsonl").read_bytes().splitlines(keepends=True)
    log.write_bytes(b"".join(lines[:2]))
    await w.poll()  # reads what is there and starts following
    first = len(seen)
    assert w._follow is not None and not w._follow.done()
    with log.open("ab") as f:
        for line in lines[2:]:
            f.write(line)
            f.flush()
    for _ in range(100):
        if len(seen) > first + 3:
            break
        await asyncio.sleep(0.05)
    assert len(seen) > first + 3, "no events arrived between polls"
    (tmp_path / "run.exit").write_text("0\n")
    await w.finish()
    expected = [e.kind for e in activity.read(s.id, limit=1000)]
    from hangar.events import parse_log

    assert expected == [e.kind for e in parse_log(b"".join(lines), "claude-code")[0]]  # each once, in order


async def test_event_stream_replays_then_follows_live(fleet):
    activity = Activity(fleet.settings, lambda x: x)
    sessions = type("S", (), {"activity": activity})()
    feed = stream_api.event_feed(sessions)
    activity.append("ses-1", [RunEvent(kind="progress", text=f"old{i}") for i in range(3)])

    def read(after):
        return [e.model_dump(mode="json", exclude={"off"}) for e in activity.read("ses-1", since=after, limit=500)]

    async with hub(fleet).open("alice", "events") as st:
        gen = stream_api._event_stream(st, feed, "ses-1", 1, read)
        chunks = [await anext(gen) for _ in range(2)]
        assert [json.loads(c.split(b"data: ")[1])["text"] for c in chunks] == ["old1", "old2"]
        assert chunks[0].startswith(b"id: 2\n")
        nxt = asyncio.create_task(anext(gen))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        activity.append("ses-1", [RunEvent(kind="message", text="new")])
        live = await asyncio.wait_for(nxt, 2)
        assert json.loads(live.split(b"data: ")[1])["text"] == "new" and time.monotonic() - t0 < 0.5
        st.close("shutdown")
        end = await anext(gen)
        assert b"event: close" in end and b"restarting" in end
        await gen.aclose()


async def test_mcp_event_subscribers_hear_of_new_events_at_once(fleet):
    from tests.test_exchange import SessionService, Vault

    svc = SessionService(fleet, Vault(fleet.store, b"0" * 43 + b"="))
    s = Session(name="evs", environment="plain")
    svc._sessions[s.id] = s
    async with Client(build_mcp(fleet, None, svc)) as c:
        async with c.listen(resource_subscriptions=["hangar://sessions/evs/events"]) as sub:
            svc.activity.append(s.id, [RunEvent(kind="message", text="hello")])
            ev = await asyncio.wait_for(anext(aiter(sub)), 5)
            assert ev.uri == "hangar://sessions/evs/events"
        svc.activity.append(s.id, [RunEvent(kind="message", text="again")])
        got = json.loads((await c.read_resource("hangar://sessions/evs/events?since=1")).contents[0].text)
        assert [e["text"] for e in got["events"]] == ["again"]


# ---------------------------------------------------------------------------- file watches
@needs_binary
async def test_watch_resource_subscriptions(envd_fleet, tmp_path):
    fleet = envd_fleet
    sb = await sandbox(fleet)
    (tmp_path / "w").mkdir()
    uri = f"hangar://sandboxes/{sb.id}/watch?path={tmp_path}/w&recursive=true"
    async with Client(build_mcp(fleet)) as c:
        async with c.listen(resource_subscriptions=[uri]) as sub:
            for _ in range(100):  # the watch is up once it says so
                v = json.loads((await c.read_resource(uri)).contents[0].text)
                if v["state"] == "watching":
                    break
                await asyncio.sleep(0.05)
            assert v["state"] == "watching", v
            (tmp_path / "w" / "new.txt").write_text("x")
            ev = await asyncio.wait_for(anext(aiter(sub)), 5)
            while ev.uri != uri:
                ev = await asyncio.wait_for(anext(aiter(sub)), 5)
            v = json.loads((await c.read_resource(uri)).contents[0].text)
            assert any(e["name"] == "new.txt" and e["type"] == "create" for e in v["events"]), v
        await asyncio.sleep(0.2)
        v = json.loads((await c.read_resource(uri)).contents[0].text)
        assert v["state"] == "not watching"  # the subscription ended, and so did the watch
    assert not hub(fleet).streams


@pytest.mark.filterwarnings("ignore:resources/(un)?subscribe is removed")
async def test_watch_without_a_daemon_says_why(fleet):
    from tests.test_exchange import SessionService, Vault

    sb = await fleet.create("plain", name="nod")
    uri = f"hangar://sandboxes/{sb.id}/watch?path=/workspace"
    svc = SessionService(fleet, Vault(fleet.store, b"0" * 43 + b"="))
    async with Client(build_mcp(fleet, None, svc), mode="legacy") as c:  # a 2025-era client: resources/subscribe
        await c.session.subscribe_resource(uri)
        for _ in range(50):
            v = json.loads((await c.read_resource(uri)).contents[0].text)
            if v.get("error"):
                break
            await asyncio.sleep(0.05)
        assert "needs hangar-envd" in v["error"]
        with pytest.raises(Exception, match="not a watch URI"):
            await c.session.subscribe_resource(f"hangar://sandboxes/{sb.id}/watch?recursive=true")


# ---------------------------------------------------------------------------- over HTTP: a real MCP client and SSE
@needs_binary
async def test_http_mcp_client_and_rest_sse_stream(settings, tmp_path):
    import httpx

    from hangar.app import create_app
    from tests.conftest import FakeProvider

    settings.admin_token = "t0ken"
    settings.workspace_dir = str(tmp_path)
    settings.envd_dir = next(iter(BINARIES.values())).parent
    settings.stream_recheck_seconds = 0.05
    app = create_app(settings)
    h = app.state.hangar
    srv = AppThread(app)
    port = free_port()
    prov = FakeProvider()
    prov.envd = SharedEnvd(h.settings, port)
    h.fleet.providers["docker"] = prov
    h.fleet.provider_errors.pop("docker", None)
    bob, _ = h.auth.create("bob", scopes=["admin"])
    base = f"http://127.0.0.1:{srv.port}"
    try:
        async with httpx.AsyncClient(base_url=base, headers={"Authorization": "Bearer t0ken"}, timeout=30) as http:
            sb = (await http.post("/api/sandboxes", json={"environment": "plain", "name": "web"})).json()[0]
            # REST: Server-Sent Events, in order, the first one long before the command ends
            events, t0, first = [], time.monotonic(), None
            async with http.stream("POST", f"/api/sandboxes/{sb['id']}/exec/stream",
                                   json={"command": TICKS}) as r:
                assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
                kind = None
                async for line in r.aiter_lines():
                    if line.startswith("event: "):
                        kind = line[7:]
                    elif line.startswith("data: "):
                        first = first or time.monotonic() - t0
                        events.append((kind, json.loads(line[6:])))
            took = time.monotonic() - t0
            assert "".join(d["data"] for k, d in events if k == "stdout") == "".join(f"tick{i}\n" for i in
                                                                                    range(1, 11))
            assert events[-1][0] == "result" and events[-1][1]["exit_code"] == 0
            assert first < took - 0.5

            # a real MCP client over streamable HTTP (2026-07-28): progress notifications on the call's stream
            from mcp.client.streamable_http import streamable_http_client

            got = []

            async def progress(p, total, message):
                got.append(message)

            async with httpx.AsyncClient(headers={"Authorization": "Bearer t0ken"}, timeout=60) as mh:
                async with Client(streamable_http_client(f"{base}/mcp", http_client=mh)) as c:
                    res = await c.call_tool("exec", {"sandbox": sb["id"], "command": TICKS, "stream": True},
                                            progress_callback=progress)
            assert not res.is_error and "".join(got) == res.structured_content["stdout"]
            assert len(got) >= 5  # chunk by chunk, not one piece at the end

            # revoking the token that opened a stream closes it
            async with httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {bob}"},
                                         timeout=30) as bh:
                async with bh.stream("POST", f"/api/sandboxes/{sb['id']}/exec/stream",
                                     json={"command": "echo hi; sleep 20"}) as r:
                    lines = r.aiter_lines()
                    async for line in lines:
                        if "hi" in line:
                            break
                    tok = next(t for t in h.auth.list() if t.name == "bob")
                    assert (await http.delete(f"/api/tokens/{tok.id}")).status_code == 200
                    rest = [ln async for ln in lines]
            result = json.loads(next(ln for ln in rest if ln.startswith("data: ") and "closed" in ln)[6:])
            assert result["closed"] == "revoked"

            # session events, live, resumable with Last-Event-ID
            s = h.sessions
            ses = Session(name="sse", environment="plain")
            s._sessions[ses.id] = ses
            s.activity.append(ses.id, [RunEvent(kind="progress", text="one")])
            async with http.stream("GET", "/api/sessions/sse/events/stream", headers={"Last-Event-ID": "0"}) as r:
                lines = r.aiter_lines()
                datas = []
                async for line in lines:
                    if line.startswith("data: "):
                        datas.append(json.loads(line[6:]))
                        if len(datas) == 1:  # recorded after the stream started: it arrives live
                            h.sessions.activity.append(ses.id, [RunEvent(kind="message", text="two")])
                        if len(datas) == 2:
                            break
            assert [d["text"] for d in datas] == ["one", "two"] and datas[1]["seq"] == 2
    finally:
        srv.stop()
        kill_daemon(port)


# ---------------------------------------------------------------------------- review(#68)
@needs_binary
async def test_turn_log_follower_stops_at_the_byte_cap(envd_fleet, tmp_path):
    """Past HANGAR_STREAM_MAX_BYTES a stream is no longer paced, so the follower must stop there and leave the log
    to polling's bounded reads, not feed the parser everything the sandbox writes as fast as it comes."""
    fleet = envd_fleet
    sb = await sandbox(fleet)
    fleet.settings.stream_max_bytes = 1 << 16
    log = tmp_path / "big.log"
    log.write_bytes((b"x" * 99 + b"\n") * 20_000)  # 2 MB
    (tmp_path / "big.exit").write_text("0\n")
    fed: list[bytes] = []
    why = await asyncio.wait_for(sandbox_streams.follow(fleet, sb.id, str(log), str(tmp_path / "big.exit"), 0,
                                                        fed.append), 20)
    assert why == "capped" and sum(map(len, fed)) <= 1 << 16, (why, sum(map(len, fed)))
    assert not hub(fleet).streams

    # and the turn watcher doesn't start another follower (with a fresh allowance) after one was capped
    s, turn = _session(fleet, sb.id)
    w = TurnWatcher(Activity(fleet.settings, lambda x: x), fleet, s, turn, lambda: None)
    done = asyncio.get_running_loop().create_future()
    done.set_result("capped")
    w._follow = done
    w._start_follow()
    assert w._follow is done


@needs_binary
async def test_agent_cannot_plant_a_process_under_a_tokens_name(envd_fleet):
    """The agent can reach its own daemon (it runs as the sandbox user), so it can start processes with any tag. A
    tag made from a token's name alone lets it put processes in that token's list and use up its background cap."""
    import hashlib

    fleet = envd_fleet
    sb = await sandbox(fleet)
    env = fleet.providers["docker"].envd
    ep = await env.ready(fleet.get(sb.id))
    forged = f"hangar:bg-0123456789ab:{hashlib.sha256(b'alice').hexdigest()[:12]}"
    events = env.stream(ep, "process.Process/Start", {"process": {"cmd": "sleep", "args": ["30"], "cwd": "/"},
                                                     "tag": forged, "stdin": False}, None, keepalive=30)
    await anext(events)
    try:
        procs = sandbox_streams.processes(fleet)
        assert await procs.list("alice", sb.id) == []
        assert not procs.procs
    finally:
        await sandbox_streams._signal(env, ep, {"tag": forged}, "SIGKILL")
        await events.aclose()


async def test_concurrent_starts_keep_to_the_background_cap(fleet):
    sb = await fleet.create("plain", name="nod")
    fleet.settings.background_max_per_token = 2
    procs = sandbox_streams.processes(fleet)
    res = await asyncio.gather(*(procs.start("alice", sb.id, "sleep 30") for _ in range(6)),
                               return_exceptions=True)
    started = [r for r in res if not isinstance(r, BaseException)]
    try:
        assert len(started) == 2, res
        assert all(isinstance(r, StreamLimit) for r in res if isinstance(r, BaseException)), res
    finally:
        for p in started:
            await procs.kill(p, grace=0.2)
        await fleet.destroy(sb.id)


async def test_a_start_refused_by_the_total_cap_leaves_nothing_running(fleet):
    from types import SimpleNamespace

    sb = await fleet.create("plain", name="nod")
    fleet.settings.stream_max_total = 1
    h = hub(fleet)
    h.streams["other"] = SimpleNamespace(owner="bob", lifetime=True, kind="events", sandbox_id=None)
    procs = sandbox_streams.processes(fleet)
    try:
        with pytest.raises(StreamLimit, match="HANGAR_STREAM_MAX_TOTAL"):
            await procs.start("alice", sb.id, "sleep 30")
        assert not [p for p in procs.procs.values() if p.state == "running"]
    finally:
        h.streams.pop("other")
        await fleet.destroy(sb.id)


def test_send_charges_each_message_for_pacing(fleet):
    """A command that prints a byte at a time makes a message per byte; pacing by payload alone would let it send
    a flood of notifications (each far bigger on the wire than its one byte)."""

    async def go():
        fleet.settings.stream_bytes_per_second = 64 << 10
        async with hub(fleet).open("alice", "exec") as st:
            t0 = time.monotonic()
            for _ in range(2000):
                await st.send(1)
            return time.monotonic() - t0, st.bytes

    took, sent = asyncio.run(go())
    assert sent == 2000 and took >= 0.8, took


async def test_a_stopped_watch_restarts_for_its_next_holder(fleet):
    sb = await fleet.create("plain", name="nod")
    ws = sandbox_streams.Watches(fleet, lambda uris: None)
    w = ws.acquire("alice", sb.id, "/workspace", False, "u1")
    await asyncio.wait_for(w.task, 5)
    assert w.state == "stopped" and "needs hangar-envd" in w.error
    first = w.task
    again = ws.acquire("alice", sb.id, "/workspace", False, "u2")
    assert again is w and w.task is not first  # tried again, rather than handing out a dead watch
    await asyncio.wait_for(w.task, 5)
    ws.release(w, "u1")
    ws.release(w, "u2")
    assert not ws.watches


def _rest_app(settings, tmp_path):
    """The whole app on uvicorn, its sandboxes the fake provider's with a real daemon (as the HTTP test above)."""
    from hangar.app import create_app
    from tests.conftest import FakeProvider

    settings.admin_token = "t0ken"
    settings.workspace_dir = str(tmp_path)
    settings.envd_dir = next(iter(BINARIES.values())).parent
    settings.stream_recheck_seconds = 0.05
    app = create_app(settings)
    h = app.state.hangar
    srv = AppThread(app)  # started before the provider is swapped in, as above
    port = free_port()
    prov = FakeProvider()
    prov.envd = SharedEnvd(h.settings, port)
    h.fleet.providers["docker"] = prov
    h.fleet.provider_errors.pop("docker", None)
    return srv, h, port


@needs_binary
async def test_rest_watch_refused_by_the_total_cap_is_released(settings, tmp_path):
    from types import SimpleNamespace

    import httpx

    srv, h, port = _rest_app(settings, tmp_path)
    h.settings.watch_max_per_token = 1
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{srv.port}", timeout=30,
                                     headers={"Authorization": "Bearer t0ken"}) as http:
            sb = (await http.post("/api/sandboxes", json={"environment": "plain", "name": "w"})).json()[0]
            h.settings.stream_max_total = 1
            hub(h.fleet).streams["other"] = SimpleNamespace(owner="bob", lifetime=True, kind="events",
                                                           sandbox_id=None)
            r = await http.get(f"/api/sandboxes/{sb['id']}/watch", params={"path": str(tmp_path)})
            assert "HANGAR_STREAM_MAX_TOTAL" in r.text
            hub(h.fleet).streams.pop("other")
            h.settings.stream_max_total = 512
            (tmp_path / "other").mkdir()
            async with http.stream("GET", f"/api/sandboxes/{sb['id']}/watch",
                                   params={"path": str(tmp_path / "other")}) as r:
                # not "already watches 1 directories": the refused stream left nothing held
                assert r.status_code == 200, await r.aread()
    finally:
        srv.stop()
        kill_daemon(port)


@needs_binary
async def test_rest_background_process_ends_when_its_token_is_revoked(settings, tmp_path):
    import httpx

    srv, h, port = _rest_app(settings, tmp_path)
    bob, tok = h.auth.create("bob", scopes=["admin"])
    base = f"http://127.0.0.1:{srv.port}"
    try:
        async with httpx.AsyncClient(base_url=base, timeout=30, headers={"Authorization": "Bearer t0ken"}) as http:
            sb = (await http.post("/api/sandboxes", json={"environment": "plain", "name": "p"})).json()[0]
            async with httpx.AsyncClient(base_url=base, timeout=30, headers={"Authorization": f"Bearer {bob}"}) as b:
                p = (await b.post(f"/api/sandboxes/{sb['id']}/processes", json={"command": "sleep 60"})).json()
            assert (await http.delete(f"/api/tokens/{tok.id}")).status_code == 200
            proc = sandbox_streams.processes(h.fleet).procs[p["id"]]
            for _ in range(100):
                if proc.state != "running":
                    break
                await asyncio.sleep(0.05)
            assert proc.state == "lost" and "revoked" in (proc.note or ""), proc.view()
    finally:
        srv.stop()
        kill_daemon(port)
