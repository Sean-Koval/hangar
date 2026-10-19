"""Events out (events_out.py): facts derived from job and session writes, each sent once per notifier, retried
when refused, other producers' events, and the built-in webhook's HMAC signature and replay window."""

from __future__ import annotations

import json

import httpx
import pytest

from hangar import events_out
from hangar.events_out import Dispatcher, WebhookNotifier, WebhookSettings, facts, session_facts, sign, verify
from hangar.ext.notify import REPLY_TO_LABEL, Event
from hangar.jobs import JobService
from hangar.models import (
    Approval,
    CredentialKind,
    Job,
    JobResult,
    JobState,
    Session,
    SessionState,
    SessionTurn,
    Turn,
    now,
)
from tests.test_jobs import sessions  # noqa: F401 - fixture

KEY = "whsec-0123456789abcdef"
URL = "https://bot.example.com/hangar/events"


class Hook:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={})

    def events(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


class Recorder:
    def __init__(self, name="rec", patterns=("*",), answers=None):
        self.name = name
        self.patterns = patterns
        self.got: list[Event] = []
        self.answers = list(answers or [])

    def wants(self, person, event):
        import fnmatch

        return any(fnmatch.fnmatchcase(event.type, p) for p in self.patterns)

    async def send(self, event, person):
        ok = self.answers.pop(0) if self.answers else True
        if ok:
            self.got.append(event)
        return ok


@pytest.fixture
def hook():
    return Hook()


@pytest.fixture
def svc(fleet, sessions, broker, settings, hook):  # noqa: F811
    settings.public_url = "https://hangar.example.com"
    broker.put("bot-hook-key", "webhook", CredentialKind.GENERIC, KEY)
    return JobService(fleet, sessions, transport=httpx.MockTransport(hook))


def _job(**kw) -> Job:
    return Job(title=kw.pop("title", "fix the login bug"), instructions="x", environment="e",
               repo="https://github.com/acme/api.git", **kw)


def _dispatcher(svc: JobService, *notifiers) -> Dispatcher:
    d = Dispatcher(svc)
    d.notifiers = list(notifiers)
    return d


def test_a_jobs_life_reads_as_events_with_stable_ids(svc):
    j = _job(state=JobState.QUEUED, person="alice")
    assert [e.type for e in facts(svc, j)] == ["job.queued"]
    j.state = JobState.RUNNING
    j.turns = [Turn(started_at=now())]
    assert [e.type for e in facts(svc, j)] == ["job.queued", "job.started"]
    j.state = JobState.NEEDS_INPUT
    j.turns[0].finished_at = now()
    j.result.question = "Postgres or SQLite?"
    evs = {e.type: e for e in facts(svc, j)}
    assert evs["job.needs_input"].summary == "fix the login bug: Postgres or SQLite?"
    assert evs["job.needs_input"].actions[0].tool == "follow_up_job"
    j.state = JobState.SUCCEEDED
    j.result = JobResult(pushed=True, head_sha="a" * 40, pr_url="https://github.com/acme/api/pull/12", pr_number=12)
    j.landing.approved_sha, j.landing.approved_by = "a" * 40, "dana"
    j.landing.checks_sha, j.landing.checks = "a" * 40, {"ci": "success"}
    j.landing.pr_state = "merged"
    types = [e.type for e in facts(svc, j)]
    assert types == ["job.queued", "job.started", "job.delivered", "pr.opened", "pr.approved", "pr.checks",
                     "pr.landed", "job.finished"]
    again = {e.type: e.id for e in facts(svc, j)}
    assert again["pr.opened"] == f"{j.id}:pr.opened:12" and again["pr.landed"] == f"{j.id}:pr.landed:12"
    ev = next(e for e in facts(svc, j) if e.type == "pr.opened")
    assert ev.links["pr"] == "https://github.com/acme/api/pull/12"
    assert ev.links["job"] == f"https://hangar.example.com/#jobs?job={j.id}"
    assert ev.person == "alice" and ev.data["pr_number"] == 12


def test_awaiting_approval_offers_the_decision_and_failures_say_why(svc):
    j = _job(state=JobState.AWAITING_APPROVAL)
    [e] = facts(svc, j)
    assert e.type == "job.awaiting_approval" and [a.tool for a in e.actions] == ["approve_job", "reject_job"]
    j = _job(state=JobState.FAILED, reason="verify failed", turns=[Turn(started_at=now(), finished_at=now())])
    assert [e.type for e in facts(svc, j)][-2:] == ["job.failed", "job.finished"]
    j.landing.checks_sha, j.landing.checks = "b" * 40, {"ci": "failure", "lint": "success"}
    j.result.pr_url, j.result.pr_number = "https://github.com/acme/api/pull/3", 3
    checks = next(e for e in facts(svc, j) if e.type == "pr.checks")
    assert checks.data["checks"] == "failed" and checks.data["failed"] == ["ci"]


def test_reply_to_prefers_the_relays_label(svc):
    j = _job(state=JobState.QUEUED, source_ref="slack:C1:1700000000.000100")
    assert facts(svc, j)[0].reply_to == "slack:C1:1700000000.000100"
    j.labels[REPLY_TO_LABEL] = "symphony:stream1:msg9"
    assert facts(svc, j)[0].reply_to == "symphony:stream1:msg9"


def test_summaries_are_redacted(svc, broker):
    broker.put("leaky", "webhook", CredentialKind.GENERIC, "s3cr3t-value-123456")
    j = _job(state=JobState.FAILED, reason="token s3cr3t-value-123456 rejected")
    assert all("s3cr3t-value-123456" not in e.summary for e in facts(svc, j))


def test_a_pending_tool_approval_is_an_event_with_answers(svc):
    a = Approval(id="apr-1", tool="Bash", detail="git push --force", expires_at=now() + 600)
    s = Session(name="alice-work", environment="e", person="alice", state=SessionState.RUNNING,
                turns=[SessionTurn(message="m", live=True, approvals=[a])])
    [e] = session_facts(svc, s)
    assert e.type == "turn.tool_approval" and e.id == "alice-work:turn.tool_approval:apr-1"
    assert {x.args["decision"] for x in e.actions} == {"allow", "deny"}
    a.status = "allowed"
    assert session_facts(svc, s) == []


async def test_each_event_goes_once_also_after_a_restart(svc):
    rec = Recorder()
    d = _dispatcher(svc, rec)
    j = _job(state=JobState.QUEUED)
    d.on_put(j)
    svc.store.put(j)
    await d.flush()
    d.on_put(j)
    await d.flush()
    assert [e.type for e in rec.got] == ["job.queued"]
    j.result.pr_url, j.result.pr_number = "https://github.com/acme/api/pull/7", 7
    svc.store.put(j)
    restarted = _dispatcher(svc, rec)  # same store: what was sent is remembered
    restarted.on_put(j)
    await restarted.flush()
    assert [e.type for e in rec.got] == ["job.queued", "pr.opened"]


async def test_a_refused_event_is_retried_then_dropped(svc, monkeypatch):
    rec = Recorder(answers=[False, True])
    d = _dispatcher(svc, rec)
    j = _job(state=JobState.QUEUED)
    svc._jobs[j.id] = j
    d.on_put(j)
    assert await d.flush() == {}
    assert await d.flush() == {"rec": 1}  # the retry, without another write
    monkeypatch.setattr(events_out, "MAX_ATTEMPTS", 2)
    never = Recorder("never", answers=[False] * 5)
    d2 = _dispatcher(svc, never)
    j2 = _job(state=JobState.QUEUED)
    svc._jobs[j2.id] = j2
    d2.on_put(j2)
    await d2.flush()
    await d2.flush()
    assert d2._retry == {} and any("not delivered to notifier never" in e.message for e in j2.events)


async def test_facts_from_before_a_notifier_was_enabled_are_not_sent(svc):
    old = _job(state=JobState.QUEUED, created_at=now() - 3600)
    svc.store.kv_set(events_out.SINCE_PREFIX + "rec", f"{now():.3f}")
    rec = Recorder()
    d = _dispatcher(svc, rec)
    d.on_put(old)
    svc.store.put(old)
    await d.flush()
    assert rec.got == []


async def test_other_producers_emit_their_own_events(svc):
    rec = Recorder(patterns=("changeset.*",))
    d = _dispatcher(svc, rec)
    d.emit(Event(id="cs_1:changeset.done", type="changeset.done", at=now(), summary="payments-bot landed"))
    d.on_put(_job(state=JobState.QUEUED))  # not wanted
    await d.flush()
    assert [e.id for e in rec.got] == ["cs_1:changeset.done"]


async def test_the_dispatcher_follows_store_writes(svc):
    rec = Recorder()
    d = events_out.of(svc)
    d.coalesce = 0
    d.start([rec])
    try:
        j = _job(state=JobState.QUEUED)
        svc.store.put(j)
        import asyncio

        for _ in range(100):
            if rec.got:
                break
            await asyncio.sleep(0.02)
        assert [e.type for e in rec.got] == ["job.queued"]
    finally:
        await d.stop()
    assert d.on_put not in svc.store.on_put


# ====================================================================== the webhook
def test_signatures_verify_and_expire():
    body = b'{"id":"x"}'
    ts = 1_760_000_000
    headers = {"x-hangar-timestamp": str(ts), "x-hangar-signature": sign(KEY, body, ts)}
    assert verify(KEY, body, headers, now=ts + 10)
    assert not verify(KEY, body + b" ", headers, now=ts + 10)  # tampered
    assert not verify("other-key", body, headers, now=ts + 10)
    assert not verify(KEY, body, headers, now=ts + 301)  # a replay outside the window
    assert not verify(KEY, body, {**headers, "x-hangar-timestamp": str(ts + 1)}, now=ts)  # the stamp is signed


async def test_the_webhook_posts_signed_events(svc, hook):
    n = WebhookNotifier(svc, WebhookSettings(url=URL, secret_credential="bot-hook-key", events=["pr.*"]))
    j = _job(state=JobState.SUCCEEDED, result=JobResult(pr_url="https://github.com/acme/api/pull/12", pr_number=12))
    ev = next(e for e in facts(svc, j) if e.type == "pr.opened")
    finished = next(e for e in facts(svc, j) if e.type == "job.finished")
    assert n.wants(None, ev) and not n.wants(None, finished)
    assert await n.send(ev, None)
    [r] = hook.requests
    assert str(r.url) == URL and r.headers["x-hangar-event"] == "pr.opened"
    assert r.headers["x-hangar-delivery"] == f"{j.id}:pr.opened:12"
    assert verify(KEY, r.content, r.headers)
    body = json.loads(r.content)
    assert body["schema"] == "hangar.event/v1" and body["type"] == "pr.opened" and body["job"] == j.id
    assert KEY not in r.content.decode()
    hook.status = 500
    assert not await n.send(ev, None)  # retried by the dispatcher


async def test_the_webhook_never_sends_unsigned(svc, hook):
    n = WebhookNotifier(svc, WebhookSettings(url=URL, secret_credential="missing"))
    assert not await n.send(Event(id="x", type="job.queued", at=now()), None)
    assert hook.requests == []
    assert n.doctor()[0]["status"] == "fail"
