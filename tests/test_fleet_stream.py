"""KAN-140 S5: `GET /api/fleet/stream`, the fleet read as it changes: coalescing, resume, visibility, auth, limits."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from hangar import fleet_stream
from hangar.auth import ApiToken, Principal
from hangar.fleet_stream import Changes, follow
from tests.test_fleet_api import ADMIN, _world, ev

ADMIN_P = Principal("ops", "tok", ["admin"], person="owner", role="admin")
BOB_P = Principal("bob", "tok-bob", ["user"], person="bob", role="member")


class FakeStream:
    """What `follow` needs of a hub stream: a race and the byte accounting."""

    def __init__(self, budget: int = 1 << 30) -> None:
        self.budget = budget

    async def race(self, aw):
        return await aw

    async def send(self, n: int) -> bool:
        self.budget -= n
        return self.budget >= 0


def parse(chunk: bytes) -> tuple[str | None, str | None, object]:
    eid = event = data = None
    for line in chunk.decode().splitlines():
        if line.startswith("id: "):
            eid = line[4:]
        elif line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
    return eid, event, data


class Pump:
    """Reads a stream's generator in a task of its own, so a test can wait on it with a timeout without cancelling
    the generator (a cancelled __anext__ would end it)."""

    def __init__(self, gen) -> None:
        self.gen = gen
        self.q: asyncio.Queue = asyncio.Queue()
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        async for chunk in self.gen:
            await self.q.put((time.monotonic(), *parse(chunk)))
        await self.q.put(None)

    async def take(self, timeout: float = 3.0):
        x = await asyncio.wait_for(self.q.get(), timeout)
        if x is None:
            raise StopAsyncIteration
        return x[1:]

    async def drain(self, seconds: float) -> list[tuple[float, str | None, str | None, object]]:
        out, end = [], time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            try:
                x = await asyncio.wait_for(self.q.get(), left)
            except TimeoutError:
                break
            if x is None:
                break
            out.append(x)
        return out

    async def close(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.gen.aclose()


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token, settings.public_url = "t0ken", "http://localhost:8787"
    with TestClient(create_app(settings), base_url="http://localhost:8787") as c:
        yield c


# ====================================================================== Changes
def test_changes_number_each_change_and_tell_what_a_resume_needs(client):
    h = client.app.state.hangar
    ch: Changes = h.fleet_changes
    start = ch.seq
    ch.mark("sb-1")
    ch.mark(None)
    ch.mark("sb-2")
    assert ch.since(ch.id(start)) == {"sb-1", None, "sb-2"}
    assert ch.since(ch.id(start + 2)) == {"sb-2"} and ch.since(ch.id(ch.seq)) == set()
    assert ch.since(f"other.{start}") is None  # another epoch: a restart
    assert ch.since(ch.id(ch.seq + 5)) is None and ch.since("garbage") is None
    for _ in range(fleet_stream.RING + 5):
        ch.mark("sb-x")
    assert ch.since(ch.id(start)) is None  # older than the ring remembers: a fresh snapshot


# ====================================================================== the stream
async def test_snapshot_then_coalesced_agent_updates(client, monkeypatch):
    monkeypatch.setattr(fleet_stream, "COALESCE_S", 0.4)
    h, sbs, job_a, ses_b = _world(client)
    gen = Pump(follow(FakeStream(), h.fleet_read, h.fleet_changes, ADMIN_P, None, None))
    eid, event, body = await gen.take()
    assert event == "snapshot" and eid.startswith(h.fleet_changes.epoch + ".")
    assert {u["name"] for u in body["units"]} == {"refunds-1", "bob-1", "bare"} and "inbox" in body
    ses_a = h.sessions.get(job_a.session_id)
    live = h.sessions.activity.live

    async def chatter():  # an agent busy for a second: an event every 20 ms
        for k in range(50):
            live.observe(ses_a, 1, ses_a.turns[0], [ev("file_change", f"f{k}.py", path=f"f{k}.py")])
            await asyncio.sleep(0.02)

    task = asyncio.create_task(chatter())
    got = await gen.drain(1.6)
    await task
    agent = [(t, d) for t, _, e, d in got if e == "agent"]
    assert agent and all(d["id"] == sbs["alice"].id for _, d in agent)  # only the agent that changed
    assert 2 <= len(agent) <= 4  # 50 changes, coalesced to one per 0.4 s
    assert all(b[0] - a[0] >= 0.35 for a, b in zip(agent, agent[1:], strict=False))
    assert agent[-1][1]["now"].startswith("editing f") and agent[-1][1]["files_changed"] > 1
    await gen.close()


async def test_inbox_and_gone_and_resume(client, monkeypatch):
    monkeypatch.setattr(fleet_stream, "COALESCE_S", 0.05)
    h, sbs, job_a, ses_b = _world(client)
    gen = Pump(follow(FakeStream(), h.fleet_read, h.fleet_changes, BOB_P, None, None))
    eid0, event, body = await gen.take()
    assert event == "snapshot" and [u["name"] for u in body["units"]] == ["bob-1"]  # bob's own fleet
    assert body["inbox"]["counts"] == {"approval": 1}
    # bob's approval is decided: his unit changes and his inbox empties
    ses_b.turns[0].approvals[0].status = "allowed"
    h.store.put(ses_b)
    got = await gen.drain(0.6)
    kinds = {e for _, _, e, _ in got}
    assert {"agent", "inbox"} <= kinds
    inbox_ev = next(d for _, _, e, d in got if e == "inbox")
    assert inbox_ev["counts"] == {}
    last_id = got[-1][1]
    # alice's agent changes: nothing for bob (he can't see it)
    ses_a = h.sessions.get(job_a.session_id)
    h.sessions.activity.live.observe(ses_a, 1, ses_a.turns[0], [ev("command", "pytest -q -k refunds")])
    assert [e for _, _, e, _ in await gen.drain(0.3)] == []
    await gen.close()
    # bob's sandbox goes; he reconnects from his last event: only what changed since, and the inbox
    sb = sbs["bob"]
    from hangar.models import SandboxState

    sb.state = SandboxState.STOPPED
    h.store.put(sb)
    gen = Pump(follow(FakeStream(), h.fleet_read, h.fleet_changes, BOB_P, None, last_id))
    got = await gen.drain(0.3)
    assert [(e, d.get("id")) for _, _, e, d in got if e in ("agent", "gone")] == [("gone", sb.id)]
    assert {"inbox", "summary"} <= {e for _, _, e, _ in got} and "snapshot" not in {e for _, _, e, _ in got}
    await gen.close()
    # an id from before a restart: a fresh snapshot
    gen = Pump(follow(FakeStream(), h.fleet_read, h.fleet_changes, BOB_P, None, "deadbeef.3"))
    assert (await gen.take())[1] == "snapshot"
    await gen.close()


async def test_an_admin_sees_every_agent_and_the_owner_filter_holds(client, monkeypatch):
    monkeypatch.setattr(fleet_stream, "COALESCE_S", 0.05)
    h, sbs, job_a, ses_b = _world(client)
    gen = Pump(follow(FakeStream(), h.fleet_read, h.fleet_changes, ADMIN_P, "bob", None))
    assert [u["name"] for u in (await gen.take())[2]["units"]] == ["bob-1"]
    ses_a = h.sessions.get(job_a.session_id)
    h.sessions.activity.live.observe(ses_a, 1, ses_a.turns[0], [ev("command", "pytest -q -k refunds")])
    assert [e for _, _, e, _ in await gen.drain(0.3)] == []  # alice's: not in ?owner=bob
    await gen.close()


async def test_the_byte_cap_closes_the_stream(client):
    h, *_ = _world(client)
    gen = Pump(follow(FakeStream(budget=100), h.fleet_read, h.fleet_changes, ADMIN_P, None, None))
    _, event, data = await gen.take()
    assert event == "close" and data["code"] == "cap"
    with pytest.raises(StopAsyncIteration):
        await gen.take()


# ====================================================================== the front door
def test_auth_tickets_and_limits(client, monkeypatch):
    h, sbs, *_ = _world(client)
    assert client.get("/api/fleet/stream").status_code == 401
    assert client.get("/api/fleet/stream?ticket=ftk_nope").status_code == 401
    assert client.get("/api/fleet/stream?owner=", headers=ADMIN).status_code == 400
    svc = client.post("/api/tokens", json={"name": "ci", "scopes": ["jobs"]}, headers=ADMIN).json()["token"]
    assert client.get("/api/fleet/stream", headers={"Authorization": f"Bearer {svc}"}).status_code == 403
    assert client.post("/api/fleet/tickets", headers={"Authorization": f"Bearer {svc}"}).status_code == 403
    # a member's ticket: their fleet, once; revoking their token ends the stream
    bob = client.post("/api/people", json={"id": "bob"}, headers=ADMIN).json()["login"]["token"]
    t = client.post("/api/fleet/tickets", headers={"Authorization": f"Bearer {bob}"}).json()
    assert t["ticket"].startswith("ftk_") and t["ttl_seconds"] == 60 and t["stream"].endswith(t["ticket"])
    tok = next(x for x in h.store.list(ApiToken) if x.person == "bob")
    h.store.delete(ApiToken, tok.id)
    h.settings.stream_recheck_seconds = 0.05
    r = client.get(t["stream"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [parse(c.encode()) for c in r.text.split("\n\n") if c.strip() and not c.startswith(":")]
    assert events[0][1] == "snapshot" and [u["name"] for u in events[0][2]["units"]] == ["bob-1"]
    assert events[-1][1] == "close" and events[-1][2]["code"] == "revoked"
    assert client.get(t["stream"]).status_code == 401  # one use
    # counted against the per-token stream cap
    h.settings.stream_max_per_token = 1

    class Held:
        owner, lifetime = "bob", True

    from hangar.streams import hub

    hub(h.fleet).streams["held"] = Held()  # type: ignore[assignment]
    try:
        t2 = h.fleet_tickets.issue(BOB_P, None)
        r = client.get(f"/api/fleet/stream?ticket={t2}")
        assert r.status_code == 429 and "HANGAR_STREAM_MAX_PER_TOKEN" in r.text
    finally:
        hub(h.fleet).streams.pop("held", None)
