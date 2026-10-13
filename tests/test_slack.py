"""Slack front door (spec 0011 workstream B): signed requests in, thread replies out, follow-ups from the thread.

Slack's Web API and response_url are faked with httpx.MockTransport; every request is signed for real."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil
import time
import uuid
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
from fastapi import FastAPI

from hangar.jobs import Caller, JobService, SubmitJob
from hangar.models import CredentialKind, Job, JobEvent, JobState
from hangar.slack import (
    SlackAPI,
    SlackError,
    SlackFrontDoor,
    doctor_checks,
    escape,
    parse_command,
    plain,
    render,
    verify_signature,
)
from tests.test_jobs import DONE, _until, sessions, world  # noqa: F401 - fixtures

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
TOKEN = "xoxb-1111-2222-slacktoken"
needs_tools = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


class FakeSlack:
    """Slack's Web API (chat.postMessage, conversations.replies, auth.test) and command response_urls."""

    def __init__(self):
        self.posts: list[dict] = []
        self.responses: list[dict] = []
        self.replies_args: dict | None = None
        self.thread_messages: list[dict] = []
        self.fail: str | None = None
        self.ratelimit = 0
        self._n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "hooks.slack.com":
            self.responses.append(json.loads(request.content))
            return httpx.Response(200, text="ok")
        assert request.url.host == "slack.com", request.url
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        method = request.url.path.rsplit("/", 1)[-1]
        form = dict(parse_qsl(request.content.decode()))
        if self.ratelimit:
            self.ratelimit -= 1
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"ok": False, "error": "ratelimited"})
        if method == "chat.postMessage":
            if self.fail:
                return httpx.Response(200, json={"ok": False, "error": self.fail})
            self._n += 1
            ts = f"1700000000.{self._n:06d}"
            self.posts.append({**form, "ts": ts})
            return httpx.Response(200, json={"ok": True, "channel": form["channel"], "ts": ts})
        if method == "conversations.replies":
            self.replies_args = form
            return httpx.Response(200, json={"ok": True, "messages": self.thread_messages, "has_more": False})
        if method == "auth.test":
            return httpx.Response(200, json={"ok": True, "user": "hangar", "team": "Acme", "user_id": "UBOT",
                                             "team_id": "T1", "bot_id": "BBOT"})
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})

    def thread(self, ts: str) -> list[str]:
        return [p["text"] for p in self.posts if p.get("thread_ts") == ts]


def sign(body: bytes, ts: int | None = None, secret: str = SECRET) -> dict[str, str]:
    t = str(int(time.time()) if ts is None else ts)
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{t}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": t, "X-Slack-Signature": sig}


@pytest.fixture
async def slack(fleet, world, settings, sessions, broker):  # noqa: F811
    """A started JobService with Slack configured, the two Slack routes mounted, and a fake Slack."""
    settings.job_poll_seconds = 0.1
    settings.job_concurrency = 2
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    broker.put("slack-signing", "webhook", CredentialKind.GENERIC, SECRET)
    broker.put("slack-bot", "webhook", CredentialKind.GENERIC, TOKEN)
    settings.slack_signing_secret_credential = "slack-signing"
    settings.slack_bot_token_credential = "slack-bot"
    settings.github_repos = {world["url"]: "jobenv"}
    settings.slack_channels = {"C1": world["url"]}
    fake = FakeSlack()
    svc = JobService(fleet, sessions, transport=httpx.MockTransport(fake))
    await svc.start()
    app = FastAPI()
    door = SlackFrontDoor(svc)
    door.mount(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hangar") as client:
        yield {"svc": svc, "fake": fake, "client": client, "door": door, "world": world}
    await svc.stop()


async def command(client: httpx.AsyncClient, text: str, channel: str = "C1", user: str = "U1",
                  ts: int | None = None, secret: str = SECRET) -> httpx.Response:
    body = urlencode({"team_id": "T1", "channel_id": channel, "user_id": user, "command": "/hangar", "text": text,
                      "response_url": "https://hooks.slack.com/commands/T1/1/abc",
                      "trigger_id": uuid.uuid4().hex}).encode()  # Slack sends a fresh one per invocation
    return await client.post("/api/hooks/slack/commands", content=body,
                             headers={**sign(body, ts, secret), "Content-Type": "application/x-www-form-urlencoded"})


async def event(client: httpx.AsyncClient, ev: dict, event_id: str, retry: str | None = None) -> httpx.Response:
    payload = {"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event": ev, "event_id": event_id,
               "event_time": int(time.time()),
               "authorizations": [{"team_id": "T1", "user_id": "UBOT", "is_bot": True}]}
    body = json.dumps(payload).encode()
    headers = {**sign(body), "Content-Type": "application/json"}
    if retry:
        headers |= {"X-Slack-Retry-Num": retry, "X-Slack-Retry-Reason": "http_timeout"}
    return await client.post("/api/hooks/slack/events", content=body, headers=headers)


async def until_posted(fake: FakeSlack, pred, timeout: float = 20) -> None:
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"not posted: {[p['text'] for p in fake.posts]}")


async def settle(svc: JobService, job_id: str) -> Job:
    """Wait until the job is done and its outcome report has been posted (it follows the state change)."""
    job = await _until(svc, job_id, DONE)
    while job.id in svc._active:
        await asyncio.sleep(0.05)
    return job


# ---------------------------------------------------------------------------- pure functions
def test_signature_matches_slacks_documented_example():
    body = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V"
            b"&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text="
            b"&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
            b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c")
    sig = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"
    at = 1531420618
    assert verify_signature(SECRET, "1531420618", body, sig, at=at + 60)
    assert not verify_signature(SECRET, "1531420618", body, sig, at=at + 301)  # older than five minutes
    assert not verify_signature(SECRET, "1531420618", body, sig, at=at - 301)  # from the future
    assert not verify_signature(SECRET, "1531420618", body + b"x", sig, at=at)
    assert not verify_signature("other", "1531420618", body, sig, at=at)
    for ts, s in [(None, sig), ("1531420618", None), ("nope", sig), ("1531420618", sig[3:])]:
        assert not verify_signature(SECRET, ts, body, s, at=at)


def test_grammar_and_text():
    assert parse_command("repo=o/r env=py harness=codex model=gpt-5.5 fix it\nplease x=y") == (
        {"repo": "o/r", "env": "py", "harness": "codex", "model": "gpt-5.5"}, "fix it\nplease x=y")
    assert parse_command("fix x=y") == ({}, "fix x=y")
    assert plain("see <https://x.io/a|the doc> and <https://y.io> &lt;b&gt; &amp; <@U1> <#C2|general>") == \
        "see the doc (https://x.io/a) and https://y.io <b> & @U1 #general"
    assert escape("<!channel> & <https://evil|ok>") == "&lt;!channel&gt; &amp; &lt;https://evil|ok&gt;"


def test_render_outcomes_and_needs_input(fleet):
    job = Job(title="t", instructions="x", environment="e", branch="hangar/j", state=JobState.FAILED,
              reason="error", repo="https://github.com/o/r.git",
              events=[JobEvent(state="failed", message="error: sandbox build failed: <denied>")])
    job.result.summary = "tried <!here>"
    text = render(job, "ignored", "final")
    assert text.startswith("*Finished with problems*: hangar job") and "(error)" in text
    assert "What went wrong: error: sandbox build failed: &lt;denied&gt;" in text
    assert "&lt;!here&gt;" in text and "<!here>" not in text  # agent text can't ping the channel
    job.state, job.reason = JobState.SUCCEEDED, None
    job.result.pr_url, job.result.pr_number = "https://github.com/o/r/pull/7", 7
    job.verify, job.result.verify_exit_code = "pytest -q", 0
    text = render(job, "ignored", "final")
    assert "*Done*" in text and "PR: <https://github.com/o/r/pull/7|o/r#7>" in text
    assert "Verification `pytest -q`: passed" in text and "What went wrong" not in text
    assert render(job, "Agent started on **job**", "note") == "Agent started on *job*"
    # workstream E's needs_input state (compared as a string; it may not exist yet)
    job.state = "needs_input"
    job.result.summary = "Should I drop Python 3.9 support?"
    text = render(job, "", "final")
    assert text.startswith("*Needs input*") and "> Should I drop Python 3.9 support?" in text
    assert "Reply in this thread to answer" in text


async def test_slack_api_retries_rate_limits_and_never_leaks_the_token():
    fake = FakeSlack()
    api = SlackAPI(TOKEN, transport=httpx.MockTransport(fake))
    fake.ratelimit = 2
    assert await api.post("C1", "hi") == "1700000000.000001"
    fake.fail = "not_in_channel"
    with pytest.raises(SlackError, match="not_in_channel") as e:
        await api.post("C1", "hi")
    assert TOKEN not in str(e.value)
    await api.close()
    bad = SlackAPI("xoxb-wrong", transport=httpx.MockTransport(fake))
    with pytest.raises(SlackError, match="invalid_auth"):
        await bad.auth_test()
    await bad.close()


# ---------------------------------------------------------------------------- acceptance
@needs_tools
async def test_slash_command_runs_a_job_and_the_thread_drives_follow_ups(slack):
    """Signed /hangar starts a job; the thread gets start and finish; a thread reply runs a follow-up."""
    svc, fake, client = slack["svc"], slack["fake"], slack["client"]
    t0 = time.monotonic()
    r = await command(client, "add fix.txt <https://x.io/spec|see spec>")
    assert r.status_code == 200 and time.monotonic() - t0 < 3  # acknowledged inside Slack's 3 seconds
    assert r.json()["response_type"] == "ephemeral" and "Starting a hangar job" in r.json()["text"]
    await until_posted(fake, lambda: any("Queued as hangar job" in p["text"] for p in fake.posts))
    top = fake.posts[0]
    assert top["channel"] == "C1" and "thread_ts" not in top and top["text"].startswith("<@U1> asked hangar")
    thread = top["ts"]
    [job] = svc.list()
    assert job.source == "slack" and job.source_ref == f"slack:C1:{thread}" and job.requester == "slack:T1/U1"
    assert "see spec (https://x.io/spec)" in job.instructions
    job = await settle(svc, job.id)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    await until_posted(fake, lambda: any(t.startswith("*Done*") for t in fake.thread(thread)))
    msgs = fake.thread(thread)
    assert any(t.startswith("Agent started on hangar job") for t in msgs)
    done = next(t for t in msgs if t.startswith("*Done*"))
    assert "Added fix.txt" in done and f"Branch: `{job.branch}`" in done and "Reply in this thread" in done

    # a reply in the thread continues the job
    slack["world"]["mode"].write_text("followup")
    reply = {"type": "message", "channel": "C1", "user": "U2", "text": "also do Y", "ts": "1700000100.000001",
             "thread_ts": thread, "channel_type": "channel"}
    r = await event(client, reply, "Ev1")
    assert r.status_code == 200 and r.json() == {"ok": True}
    await until_posted(fake, lambda: any("Continuing hangar job" in t for t in fake.thread(thread)))
    job = await _until(svc, job.id, {JobState.QUEUED, JobState.RUNNING, *DONE})
    job = await settle(svc, job.id)
    assert job.state == JobState.SUCCEEDED and len(job.turns) == 2
    assert job.turns[1].requester == "slack:T1/U2" and job.turns[1].reply_to == f"slack:C1:{thread}"
    assert job.turns[1].summary == "did Y"
    await until_posted(fake, lambda: sum(t.startswith("*Done*") for t in fake.thread(thread)) == 2)
    # Slack retrying the same delivery, and the message-event twin of an app mention, don't run it again
    assert "duplicate" in (await event(client, reply, "Ev1", retry="1")).json()["ignored"]
    assert "already handled" in (await event(client, {**reply, "type": "app_mention"}, "Ev2")).json()["ignored"]
    assert len(svc.get(job.id).turns) == 2


async def test_bad_signatures_and_stale_requests_are_rejected(slack):
    client, svc = slack["client"], slack["svc"]
    assert (await command(client, "do it", secret="wrong")).status_code == 401
    assert (await command(client, "do it", ts=int(time.time()) - 301)).status_code == 401  # older than 5 minutes
    body = b'{"type": "url_verification", "challenge": "abc"}'
    assert (await client.post("/api/hooks/slack/events", content=body)).status_code == 401  # unsigned
    r = await client.post("/api/hooks/slack/events", content=body, headers=sign(body))
    assert r.status_code == 200 and r.json() == {"challenge": "abc"}
    bad = b"not json"
    assert (await client.post("/api/hooks/slack/events", content=bad, headers=sign(bad))).status_code == 400
    assert svc.list() == []
    svc.settings.slack_signing_secret_credential = None
    assert (await command(client, "do it")).status_code == 404  # off until configured


async def test_command_policy(slack):
    client, svc, fake = slack["client"], slack["svc"], slack["fake"]
    say = lambda r: r.json()["text"]  # noqa: E731
    assert "isn't connected to a repo" in say(await command(client, "do it", channel="C9"))
    assert "/hangar status JOB" in say(await command(client, "help", channel="C9"))  # help works anywhere
    assert "isn't in HANGAR_GITHUB_REPOS" in say(await command(client, "repo=evil/repo do it"))
    assert "isn't allowed" in say(await command(client, "env=prod do it"))
    assert "Say what to do" in say(await command(client, "env=jobenv"))
    svc.settings.slack_users = ["U7", "T1/U8"]
    assert "allowlist" in say(await command(client, "do it", user="U1"))
    assert "No job `job-nope`" in say(await command(client, "status job-nope", user="U8"))
    await asyncio.sleep(0.1)
    assert svc.list() == [] and fake.posts == []
    # the bot isn't in the channel: the user hears why through response_url, and no job starts
    svc.settings.slack_users = []
    fake.fail = "not_in_channel"
    await command(client, "do it")
    await until_posted(fake, lambda: bool(fake.responses))
    assert "not_in_channel" in fake.responses[0]["text"] and fake.responses[0]["response_type"] == "ephemeral"
    assert svc.list() == []


@needs_tools
async def test_app_mention_in_a_thread_brings_the_thread_as_context(slack):
    svc, fake, client = slack["svc"], slack["fake"], slack["client"]
    fake.thread_messages = [
        {"type": "message", "bot_id": "BALERT", "username": "alerts", "text": "CSV export 500s &amp; <https://s.io/1|trace>",
         "ts": "1700000001.000001", "thread_ts": "1700000001.000001"},
        {"type": "message", "user": "U3", "text": "repro: export twice", "ts": "1700000002.000001",
         "thread_ts": "1700000001.000001"},
        {"type": "message", "user": "UBOT", "bot_id": "BBOT", "text": "hangar chatter", "ts": "1700000003.000001",
         "thread_ts": "1700000001.000001"},
        {"type": "message", "user": "U1", "text": "<@UBOT> fix this", "ts": "1700000004.000001",
         "thread_ts": "1700000001.000001"},
    ]
    mention = {"type": "app_mention", "channel": "C1", "user": "U1", "text": "<@UBOT> fix this",
               "ts": "1700000004.000001", "thread_ts": "1700000001.000001"}
    r = await event(client, mention, "Ev10")
    assert r.json() == {"ok": True}
    await until_posted(fake, lambda: any("Queued as hangar job" in p["text"] for p in fake.posts))
    [job] = svc.list()
    assert job.instructions == "fix this" and job.source_ref == "slack:C1:1700000001.000001"
    assert fake.replies_args["latest"] == "1700000004.000001" and fake.replies_args["limit"] == "15"
    [ctx] = job.context
    assert ctx.name == "slack-thread.md"
    assert "CSV export 500s & trace (https://s.io/1)" in ctx.content and "repro: export twice" in ctx.content
    assert "hangar chatter" not in ctx.content and "fix this" not in ctx.content
    # the message-event twin of the mention, a redelivery, and the app's own posts are all ignored
    twin = {**mention, "type": "message"}
    assert "ignored" in (await event(client, twin, "Ev11")).json()
    assert "duplicate" in (await event(client, mention, "Ev10", retry="2")).json()["ignored"]
    own = {"type": "message", "channel": "C1", "user": "UBOT", "bot_id": "BBOT", "text": "Queued",
           "ts": "1700000009.000001", "thread_ts": "1700000001.000001"}
    assert "bot" in (await event(client, own, "Ev12")).json()["ignored"]
    edit = {"type": "message", "subtype": "message_changed", "channel": "C1", "ts": "1700000010.000001"}
    assert "ignored" in (await event(client, edit, "Ev13")).json()
    await settle(svc, job.id)
    assert len(svc.list()) == 1


@needs_tools
async def test_thread_reply_while_the_agent_works_says_to_wait(slack):
    svc, fake, client = slack["svc"], slack["fake"], slack["client"]
    slack["world"]["mode"].write_text("slow")
    await command(client, "take your time")
    await until_posted(fake, lambda: any("Queued as hangar job" in p["text"] for p in fake.posts))
    [job] = svc.list()
    await _until(svc, job.id, {JobState.RUNNING})
    thread = job.source_ref.rsplit(":", 1)[1]
    reply = {"type": "message", "channel": "C1", "user": "U2", "text": "and Z", "ts": "1700000011.000001",
             "thread_ts": thread}
    assert (await event(client, reply, "Ev20")).json() == {"ok": True}
    await until_posted(fake, lambda: any("Can't continue yet" in t for t in fake.thread(thread)))
    job = await settle(svc, job.id)
    assert len(job.turns) == 1
    # a reply from someone off the allowlist is ignored silently (threads are conversations)
    svc.settings.slack_users = ["U1"]
    assert "not allowed" in (await event(client, {**reply, "ts": "1700000012.000001"}, "Ev21")).json()["ignored"]


@needs_tools
async def test_failures_say_what_went_wrong_in_the_thread(slack):
    svc, fake, client = slack["svc"], slack["fake"], slack["client"]
    slack["world"]["mode"].write_text("fail")
    await command(client, "break it")
    await until_posted(fake, lambda: any("Queued as hangar job" in p["text"] for p in fake.posts))
    [job] = svc.list()
    job = await settle(svc, job.id)
    assert job.state == JobState.FAILED and job.reason == "agent_failed"
    thread = job.source_ref.rsplit(":", 1)[1]
    await until_posted(fake, lambda: any(t.startswith("*Finished with problems*") for t in fake.thread(thread)))
    final = next(t for t in fake.thread(thread) if t.startswith("*Finished with problems*"))
    assert "(agent_failed)" in final and "What went wrong: agent turn finished: exited, exit 3" in final


async def test_posting_failures_land_on_the_job(fleet, settings, broker, world):  # noqa: F811
    """No bot token, or Slack refusing the post: the job's timeline says so (and the token is never in it)."""
    fake = FakeSlack()
    svc = JobService(fleet, None, transport=httpx.MockTransport(fake))
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    settings.github_repos = {world["url"]: "jobenv"}
    job = svc.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv"),
                     Caller("slack:T1/U1", "slack:T1/U1", restricted=True), source="slack",
                     source_ref="slack:C1:1700000000.000001")
    assert job.turns[0].reply_to == "slack:C1:1700000000.000001"
    await svc._comment(job, job.turns[0].reply_to, "hello")
    assert "HANGAR_SLACK_BOT_TOKEN_CREDENTIAL" in job.events[-1].message
    broker.put("slack-bot", "webhook", CredentialKind.GENERIC, TOKEN)
    settings.slack_bot_token_credential = "slack-bot"
    fake.fail = "channel_not_found"
    await svc._comment(job, job.turns[0].reply_to, f"leak {TOKEN}?")
    assert "channel_not_found" in job.events[-1].message and TOKEN not in json.dumps(svc.view(job))
    fake.fail = None
    await svc._comment(job, job.turns[0].reply_to, f"leak {TOKEN}?")
    assert fake.posts[-1]["thread_ts"] == "1700000000.000001" and TOKEN not in fake.posts[-1]["text"]


async def test_doctor_checks(fleet, settings, broker, world):  # noqa: F811
    class H:
        pass

    h = H()
    h.settings, h.store, h.broker = settings, fleet.store, broker
    h.jobs = JobService(fleet, None)
    assert await doctor_checks(h) == []  # not configured: nothing to report
    settings.slack_signing_secret_credential = "slack-signing"
    settings.slack_bot_token_credential = "slack-bot"
    settings.slack_channels = {"C1": "o/r", "C2": "o/missing"}
    settings.github_repos = {"o/r": "jobenv"}
    checks = {c["name"]: c for c in await doctor_checks(h, httpx.MockTransport(FakeSlack()))}
    sig = checks["slack signing secret"]
    assert sig["status"] == "fail" and "hangar cred add" in sig["fix"]
    assert checks["slack bot token"]["status"] == "fail"
    assert checks["slack channel C1"]["status"] == "ok"
    c2 = checks["slack channel C2"]
    assert c2["status"] == "fail" and "HANGAR_GITHUB_REPOS" in c2["detail"]
    broker.put("slack-signing", "webhook", CredentialKind.GENERIC, SECRET)
    broker.put("slack-bot", "webhook", CredentialKind.GENERIC, TOKEN)
    checks = {c["name"]: c for c in await doctor_checks(h, httpx.MockTransport(FakeSlack()))}
    assert checks["slack signing secret"]["status"] == "ok" and checks["slack bot token"]["status"] == "ok"
    assert checks["slack api"]["status"] == "ok" and "hangar in Acme" in checks["slack api"]["detail"]
    assert TOKEN not in json.dumps(checks)


def test_routes_are_mounted_and_doctor_reports_slack(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        body = b'{"type": "url_verification", "challenge": "abc"}'
        assert c.post("/api/hooks/slack/events", content=body, headers=sign(body)).status_code == 404  # off
        app.state.hangar.broker.put("slack-signing", "webhook", CredentialKind.GENERIC, SECRET)
        settings.slack_signing_secret_credential = "slack-signing"
        settings.slack_channels = {"C1": "o/r"}
        r = c.post("/api/hooks/slack/events", content=body, headers=sign(body))
        assert r.status_code == 200 and r.json() == {"challenge": "abc"}
        checks = {x["name"]: x for x in c.get("/api/doctor", headers={"Authorization": "Bearer t0ken"}).json()}
        assert checks["slack signing secret"]["status"] == "ok" and checks["slack bot token"]["status"] == "fail"
        assert checks["slack channel C1"]["status"] == "fail"
