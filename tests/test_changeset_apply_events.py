"""Change sets in config as code and in events out (validation gaps 2 and 5): `hangar apply -f` applies
`hangar.changeset-profile/v1` idempotently, reports drift with --check and removes undeclared profiles with --prune;
a change set's progress (created, PR opened, waiting, landed, deployed, done, broken) reaches every notifier with
stable ids and the parent's reply_to, and every part job carries the parent's relay.reply_to."""

from __future__ import annotations

import asyncio
import json

import pytest

from hangar import changesets, events_out
from hangar.ext import deliverers as ext
from hangar.ext.notify import REPLY_TO_LABEL
from hangar.jobs import Caller, SubmitJob
from tests.test_apply_cli import _dir, api, run, writes  # noqa: F401 - fixtures
from tests.test_changeset_landing import approve, close
from tests.test_changesets import cjobs, multi, sessions  # noqa: F401 - fixtures
from tests.test_deliverers import FakeRegistry
from tests.test_jobs import DONE, _until
from tests.test_landing import settle

PROFILE = """\
schema: hangar.changeset-profile/v1
name: svc
environment: plain
repos:
  infra: {repo: https://github.com/acme/infra.git}
  code:  {repo: https://github.com/acme/code.git}
land_order: [infra, code]
"""


# ============================================================================ hangar apply
def test_apply_change_set_profiles(api, tmp_path):  # noqa: F811
    c, calls = api
    d = _dir(tmp_path, **{"changesets/svc.yaml": PROFILE})
    r = run("-f", str(d))
    assert r.exit_code == 0 and "changeset svc: created" in r.output, r.output
    live = c.get("/api/changesets/profiles/svc").json()
    assert live["land_order"] == ["infra", "code"] and live["primary"] == "code"
    calls.clear()
    r = run("-f", str(d))  # idempotent: nothing written the second time
    assert r.exit_code == 0 and writes(calls) == [] and "1 object(s), all up to date" in r.output, r.output
    assert run("-f", str(d), "--check").exit_code == 0
    # someone edits it by hand, and adds another
    body = {**{k: v for k, v in live.items() if k not in ("digest", "updated_at", "updated_by")},
            "land_order": ["code", "infra"]}
    assert c.put("/api/changesets/profiles", json=body).status_code == 200
    assert c.put("/api/changesets/profiles", json={**body, "name": "other"}).status_code == 200
    calls.clear()
    r = run("-f", str(d), "--check")
    assert r.exit_code == 1 and writes(calls) == [], r.output
    assert "changeset svc: would change" in r.output
    assert """land_order: ["code", "infra"] -> ["infra", "code"]""" in r.output
    assert "changeset other: live, but no file declares it" in r.output
    r = run("-f", str(d))
    assert r.exit_code == 0 and c.get("/api/changesets/profiles/svc").json()["land_order"] == ["infra", "code"]
    assert c.get("/api/changesets/profiles/other").status_code == 200  # left alone without --prune
    r = run("-f", str(d), "--prune")
    assert r.exit_code == 0 and "changeset other: deleted" in r.output, r.output
    assert c.get("/api/changesets/profiles/other").status_code == 404
    assert run("-f", str(d), "--check").exit_code == 0


def test_apply_refuses_a_bad_profile_before_applying_anything(api, tmp_path):  # noqa: F811
    c, calls = api
    bad = PROFILE.replace("land_order: [infra, code]", "land_order: [infra]")
    d = _dir(tmp_path, **{"a.yaml": PROFILE.replace("name: svc", "name: fine"), "b.yaml": bad})
    r = run("-f", str(d))
    assert r.exit_code != 0 and "missing: code" in r.output, r.output
    assert writes(calls) == []


# ============================================================================ events out
class Room:
    name = "room"

    def __init__(self):
        self.events: list = []

    def wants(self, person, event) -> bool:
        return True

    async def send(self, event, person) -> bool:
        self.events.append(event)
        return True


@pytest.fixture
async def room(cjobs, monkeypatch):  # noqa: F811
    from hangar.deliverers.image_ready import ImageReady

    FakeRegistry.tags, FakeRegistry.asked = {}, []
    monkeypatch.setitem(ext.DELIVERERS, "image-ready", ImageReady(registry=FakeRegistry))
    r = Room()
    d = events_out.of(cjobs)
    d.coalesce = 0
    d.start([r])
    yield r
    await d.stop()


async def _flush(svc):
    await settle(svc)
    await events_out.of(svc).flush()


async def test_a_change_sets_progress_reaches_the_room_that_asked(cjobs, multi, room):  # noqa: F811
    job = cjobs.submit(SubmitJob(instructions="Add the pay endpoint", changeset="payments-bot", deliver="pr",
                                 ticket="jira:PAY-412", labels={REPLY_TO_LABEL: "symphony:ROOM:MSG1"}),
                       Caller("sean", "sean"))
    job = await _until(cjobs, job.id, DONE, timeout=60)
    while job.id in cjobs._active:
        await asyncio.sleep(0.05)
    cs = changesets.of(cjobs).of(job)
    await _flush(cjobs)
    by_id = {e.id: e for e in room.events}
    assert f"{cs.id}:changeset.created" in by_id
    for role, n in (("database", 12), ("infra", 11), ("code", 13)):
        part = cjobs.get(cs.parts[role])
        assert part.labels[REPLY_TO_LABEL] == "symphony:ROOM:MSG1"  # every child job answers in the parent's room
        ev = by_id[f"{cs.id}:changeset.pr_opened:{role}:{n}"]
        assert ev.reply_to == "symphony:ROOM:MSG1" and ev.job == job.id and ev.data["part_job"] == part.id
        assert by_id[f"{part.id}:pr.opened:{n}"].reply_to == "symphony:ROOM:MSG1"  # the part's own PR event
    assert by_id[f"{cs.id}:changeset.landing"].data["changeset"] == cs.id
    p = {r: cjobs.get(j) for r, j in cs.parts.items()}
    await approve(cjobs, p["code"])  # approved first: waits for database
    await _flush(cjobs)
    w = next(e for e in room.events if e.id == f"{cs.id}:changeset.waiting:code:database")
    assert w.data["waiting_for"] == "database" and w.reply_to == "symphony:ROOM:MSG1"
    for role in ("database", "infra"):
        await approve(cjobs, p[role])
        await close(cjobs, p[role])
    code_sha = await close(cjobs, p["code"])
    await _flush(cjobs)
    ids = [e.id for e in room.events]
    for role in ("database", "infra", "code"):
        assert f"{cs.id}:changeset.landed:{role}" in ids
    assert f"{cs.id}:changeset.deploying" in ids
    FakeRegistry.tags[f"registry.example.com/payments-bot:{code_sha}"] = "sha256:" + "e" * 64
    await changesets.tick(cjobs)
    deploy = cjobs.get(cs.parts["deploy"])
    assert deploy.labels[REPLY_TO_LABEL] == "symphony:ROOM:MSG1"
    await approve(cjobs, deploy)
    await close(cjobs, deploy)
    await _flush(cjobs)
    ids = [e.id for e in room.events]
    assert f"{cs.id}:changeset.pr_opened:deploy:14" in ids and f"{cs.id}:changeset.deployed:deploy" in ids
    assert f"{cs.id}:changeset.done" in ids
    assert len(ids) == len(set(ids))  # each fact once, however often the records were written
    body = json.loads(next(e for e in room.events if e.type == "changeset.done").body())
    assert body["schema"] == "hangar.event/v1" and body["reply_to"] == "symphony:ROOM:MSG1"


async def test_a_broken_set_is_announced(cjobs, multi, room):  # noqa: F811
    from tests.test_changesets import deliver_three

    job, cs = await deliver_three(cjobs)
    p = {r: cjobs.get(j) for r, j in cs.parts.items()}
    await close(cjobs, p["database"], merged=False)
    await _flush(cjobs)
    ev = next(e for e in room.events if e.id == f"{cs.id}:changeset.broken")
    assert "database PR #12 was closed without merging" in ev.summary and ev.job == job.id
