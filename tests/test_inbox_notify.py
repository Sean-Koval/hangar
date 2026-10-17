"""Inbox delivery (#157, #158): stable item ids and native links, an immediate Slack DM for questions and approvals,
and the per-person digest twice a day. Slack and GitHub are faked with httpx.MockTransport."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import httpx
import pytest

from hangar import inbox, inbox_notify, people
from hangar.inbox_notify import Notifier, digest, digest_tick, due_slot, set_delivery
from hangar.jobs import JobService
from hangar.models import (
    Approval,
    CredentialKind,
    Job,
    JobState,
    Person,
    Session,
    SessionState,
    SessionTurn,
    now,
)
from tests.test_jobs import sessions  # noqa: F401 - fixture

TOKEN = "xoxb-1111-2222-slacktoken"
NY = ZoneInfo("America/New_York")


class FakeAPIs:
    """Slack (conversations.open, chat.postMessage, chat.getPermalink, auth.test) and GitHub's GET /user/{id}."""

    def __init__(self):
        self.dms: list[dict] = []  # {"user", "text"}
        self.opened: dict[str, str] = {}  # DM channel -> user
        self.permalinks = 0
        self.scopes = "commands,chat:write,im:write"
        self.github_users: dict[str, str] = {"583231": "octocat"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            uid = request.url.path.rsplit("/", 1)[-1]
            if uid in self.github_users:
                return httpx.Response(200, json={"id": int(uid), "login": self.github_users[uid], "type": "User"})
            return httpx.Response(404, json={"message": "Not Found"})
        assert request.url.host == "slack.com", request.url
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        method = request.url.path.rsplit("/", 1)[-1]
        form = dict(parse_qsl(request.content.decode()))
        h = {"x-oauth-scopes": self.scopes}
        if method == "conversations.open":
            ch = "D" + form["users"]
            self.opened[ch] = form["users"]
            return httpx.Response(200, headers=h, json={"ok": True, "channel": {"id": ch}})
        if method == "chat.postMessage":
            self.dms.append({"user": self.opened.get(form["channel"], form["channel"]), "text": form["text"]})
            return httpx.Response(200, headers=h, json={"ok": True, "ts": "1700000000.000100"})
        if method == "chat.getPermalink":
            self.permalinks += 1
            ts = form["message_ts"].replace(".", "")
            return httpx.Response(200, headers=h, json={
                "ok": True, "permalink": f"https://acme.slack.com/archives/{form['channel']}/p{ts}"})
        if method == "auth.test":
            return httpx.Response(200, headers=h, json={"ok": True, "user": "hangar", "team": "Acme"})
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})

    def to(self, user: str) -> list[str]:
        return [d["text"] for d in self.dms if d["user"] == user]


@pytest.fixture
def fake():
    return FakeAPIs()


@pytest.fixture
def svc(fleet, sessions, broker, settings, fake):  # noqa: F811
    broker.put("slack-bot", "webhook", CredentialKind.GENERIC, TOKEN)
    settings.slack_bot_token_credential = "slack-bot"
    settings.public_url = "https://hangar.example.com"
    settings.jira = {"url": "https://acme.atlassian.net"}
    s = JobService(fleet, sessions, transport=httpx.MockTransport(fake))
    people.add(s.store, "alice", "Alice")
    people.link(s.store, "alice", "slack", "T1/UALICE")
    people.add(s.store, "bob", "Bob")  # no Slack account
    people.add(s.store, "root", "Root", role="admin")
    people.link(s.store, "root", "slack", "T1/UROOT")
    return s


def _job(svc: JobService, person: str | None, state: JobState, **kw) -> Job:
    j = Job(title=kw.pop("title", "fix it"), instructions="x", environment="e", repo="https://github.com/o/r.git",
            state=state, person=person, **kw)
    svc._jobs[j.id] = j
    return j


def _ask(svc: JobService, person: str | None, question: str = "Use v1 or v2?", **kw) -> Job:
    j = _job(svc, person, JobState.NEEDS_INPUT, **kw)
    j.result.question = question
    svc._save(j)
    return j


async def _until(pred, timeout: float = 10) -> None:
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


@pytest.fixture
async def notifier(svc):
    n = Notifier(svc)
    n.coalesce, n.recheck = 0.05, 3600
    n.start()
    yield n
    await n.stop()


# ====================================================================== item ids and links
def test_items_carry_stable_ids_and_native_links(svc):
    jira = _ask(svc, "alice", labels={"intake.tracker": "jira:ABC-12", "jira.issue": "ABC-12"},
                source="jira", source_ref="jira:ABC-12")
    gh = _ask(svc, "alice", "Which file?", source="github", source_ref="github:o/r#7")
    sl = _ask(svc, "alice", "Which env?", source="slack", source_ref="slack:C1:1700000000.000200")
    sl.result.pr_url = "https://github.com/o/r/pull/9"
    one = {x["job"]: x for x in inbox.items(svc, inbox_notify.principal_for(people.get(svc.store, "alice")))}
    for x in one.values():  # the fields the inbox always had are all still there
        assert {"kind", "reason", "action", "since", "job", "title", "repo", "id", "links"} <= set(x)
        assert set(x["links"]) == {"pr", "ticket", "thread", "job"}
    assert one[jira.id]["links"]["ticket"] == "https://acme.atlassian.net/browse/ABC-12"
    assert one[gh.id]["links"]["ticket"] == "https://github.com/o/r/issues/7"
    assert one[sl.id]["links"]["thread"] == "https://slack.com/archives/C1/p1700000000000200"
    assert one[sl.id]["links"]["pr"] == "https://github.com/o/r/pull/9"
    assert one[sl.id]["links"]["job"] == f"https://hangar.example.com/#jobs?job={sl.id}"
    assert inbox.native(one[sl.id]) == one[sl.id]["links"]["thread"]
    # read again: the same ids; a different question on the same job: a new one
    again = {x["job"]: x["id"] for x in inbox.items(svc, inbox_notify.principal_for(people.get(svc.store, "alice")))}
    assert again == {k: v["id"] for k, v in one.items()} and len(set(again.values())) == 3
    jira.result.question = "Something else?"
    now_ids = {x["job"]: x["id"] for x in inbox.items(svc, inbox_notify.principal_for(people.get(svc.store, "alice")))}
    assert now_ids[jira.id] != one[jira.id]["id"] and now_ids[gh.id] == one[gh.id]["id"]


# ====================================================================== the DM at once
async def test_a_question_dms_its_person_once(svc, fake, notifier):
    j = _ask(svc, "alice", labels={"intake.tracker": "jira:ABC-12"}, source="jira", source_ref="jira:ABC-12")
    await _until(lambda: fake.to("UALICE"))
    [text] = fake.to("UALICE")
    assert "the agent asks: Use v1 or v2?" in text
    assert "Answer here: <https://acme.atlassian.net/browse/ABC-12>" in text
    assert f"Or: `hangar job follow-up {j.id}" in text
    # re-reading and re-evaluating the inbox doesn't send it again
    svc._save(j)
    await asyncio.sleep(0.2)
    assert await notifier.deliver(None) == {}
    assert len(fake.to("UALICE")) == 1 and not fake.to("UROOT")


async def test_thread_link_is_slacks_permalink(svc, fake, notifier):
    _ask(svc, "alice", source="slack", source_ref="slack:C1:1700000000.000200")
    await _until(lambda: fake.to("UALICE"))
    assert "Answer here: <https://acme.slack.com/archives/C1/p1700000000000200>" in fake.to("UALICE")[0]
    assert fake.permalinks == 1
    x = inbox.items(svc, inbox_notify.principal_for(people.get(svc.store, "alice")))[0]
    assert x["links"]["thread"] == "https://acme.slack.com/archives/C1/p1700000000000200"  # cached


async def test_a_tool_approval_dms_the_turns_owner_until_answered(svc, fake, notifier):
    a1 = Approval(id="apr-1", tool="Bash", detail="git push --force", expires_at=now() + 600)
    turn = SessionTurn(message="m", live=True, approvals=[a1])
    s = Session(name="alice-work", environment="e", person="alice", state=SessionState.RUNNING, turns=[turn])
    svc.sessions._sessions[s.id] = s
    svc.store.put(s)
    await _until(lambda: fake.to("UALICE"))
    assert "the agent wants to run Bash: git push --force" in fake.to("UALICE")[0]
    assert "--allow apr-1" in fake.to("UALICE")[0]
    a1.status = "allowed"  # answered: never re-sent
    svc.store.put(s)
    await asyncio.sleep(0.2)
    assert len(fake.to("UALICE")) == 1
    turn.approvals.append(Approval(id="apr-2", tool="Edit", detail="secrets.py"))  # a new one is new
    svc.store.put(s)
    await _until(lambda: len(fake.to("UALICE")) == 2)
    assert "apr-2" in fake.to("UALICE")[1] and "apr-1" not in fake.to("UALICE")[1]


async def test_a_burst_is_one_dm(svc, fake):
    n = Notifier(svc)
    n.coalesce, n.recheck = 0.3, 3600
    n.start()
    try:
        for i in range(5):
            _ask(svc, "alice", f"question {i}?")
            await asyncio.sleep(0.02)
        await _until(lambda: fake.to("UALICE"))
        await asyncio.sleep(0.4)
        [text] = fake.to("UALICE")
        assert text.startswith("*hangar*: 5 things need you") and all(f"question {i}?" in text for i in range(5))
    finally:
        await n.stop()


async def test_no_slack_account_gets_nothing_and_doctor_says_so(svc, fake, notifier, settings):
    _ask(svc, "bob")
    await asyncio.sleep(0.3)
    assert fake.dms == []
    h = SimpleNamespace(settings=settings, store=svc.store)
    [check] = inbox_notify.doctor_checks(h)
    assert check["status"] == "warn" and "inbox not delivered" in check["detail"] and "bob" in check["detail"]
    assert "alice" not in check["detail"]


async def test_dm_off_stops_dms_but_not_the_inbox(svc, fake, notifier):
    set_delivery(svc.store, "alice", dm="off")
    j = _ask(svc, "alice")
    await asyncio.sleep(0.3)
    assert fake.dms == []
    assert [x["job"] for x in inbox.items(svc, inbox_notify.principal_for(people.get(svc.store, "alice")))] == [j.id]


async def test_team_questions_go_only_to_admins_who_opted_in(svc, fake, notifier):
    _ask(svc, None, "team question?")  # a Slack or GitHub job: nobody's in particular
    await asyncio.sleep(0.3)
    assert fake.dms == []  # root is an admin, but on the default (their own work)
    set_delivery(svc.store, "root", dm="all")
    assert await notifier.deliver(None) == {"root": 1}
    assert "team question?" in fake.to("UROOT")[0] and not fake.to("UALICE")


async def test_dm_text_is_escaped_and_redacted(svc, fake, notifier, broker):
    broker.put("prod-db", "webhook", CredentialKind.GENERIC, "s3cr3t-value-123456")
    _ask(svc, "alice", "Ping <!channel> and use s3cr3t-value-123456?")
    await _until(lambda: fake.to("UALICE"))
    text = fake.to("UALICE")[0]
    assert "<!channel>" not in text and "&lt;!channel&gt;" in text
    assert "s3cr3t-value-123456" not in text


async def test_quiet_window_holds_all_but_expiring_approvals(svc, fake):
    set_delivery(svc.store, "alice", quiet="20:00-08:00")
    n = Notifier(svc)
    night = datetime(2026, 10, 14, 23, 0, tzinfo=NY).timestamp()
    _ask(svc, "alice", "can wait?")
    soon = Approval(id="apr-9", tool="Bash", detail="deploy", expires_at=night + 1800)
    s = Session(name="alice-night", environment="e", person="alice", state=SessionState.RUNNING,
                turns=[SessionTurn(message="m", live=True, approvals=[soon])])
    svc.sessions._sessions[s.id] = s
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(inbox, "now", lambda: night)
        import hangar.steer as steer

        mp.setattr(steer, "now", lambda: night)
        assert await n.deliver(None, t=night) == {"alice": 1}
    assert "deploy" in fake.to("UALICE")[0] and "can wait?" not in fake.to("UALICE")[0]
    morning = datetime(2026, 10, 15, 8, 1, tzinfo=NY).timestamp()
    assert await n.deliver(None, t=morning) == {"alice": 1}
    assert "can wait?" in fake.to("UALICE")[1]


def test_quiet_until():
    p = Person(id="a", quiet="20:00-08:00", tz="America/New_York")
    at = lambda h, m=0, d=14: datetime(2026, 10, d, h, m, tzinfo=NY).timestamp()  # noqa: E731
    assert inbox_notify.quiet_until(p, at(23)) == at(8, d=15)
    assert inbox_notify.quiet_until(p, at(7, 59)) == at(8)
    assert inbox_notify.quiet_until(p, at(12)) is None
    assert inbox_notify.quiet_until(Person(id="b", quiet="12:00-13:00"), at(12, 30)) == at(13)


def test_delivery_settings_are_validated(svc):
    with pytest.raises(people.PeopleError):
        set_delivery(svc.store, "alice", dm="loud")
    with pytest.raises(people.PeopleError):
        set_delivery(svc.store, "alice", tz="Mars/Base")
    with pytest.raises(people.PeopleError):
        set_delivery(svc.store, "alice", digest=["25:00"])
    with pytest.raises(people.PeopleError):
        set_delivery(svc.store, "alice", quiet="evening")
    p = set_delivery(svc.store, "alice", tz="Europe/Berlin", digest=["16:00", "9:30"], quiet="19:00-07:00",
                     digest_weekends=True)
    assert (p.tz, p.digest, p.quiet, p.digest_weekends) == ("Europe/Berlin", ["09:30", "16:00"], "19:00-07:00", True)
    assert set_delivery(svc.store, "alice", digest=[], quiet="").digest == []
    assert people.get(svc.store, "alice").quiet is None


async def test_slack_doctor_warns_without_im_write(svc, fake, settings):
    from hangar.slack import doctor_checks

    fake.scopes = "commands,chat:write"
    h = SimpleNamespace(settings=settings, store=svc.store, broker=svc.broker, jobs=svc)
    checks = {c["name"]: c for c in await doctor_checks(h, httpx.MockTransport(fake))}
    assert checks["slack dm scope"]["status"] == "warn" and "im:write" in checks["slack dm scope"]["detail"]
    fake.scopes = "commands,chat:write,im:write"
    assert "slack dm scope" not in {c["name"] for c in await doctor_checks(h, httpx.MockTransport(fake))}


async def test_delivery_route_self_or_admin(svc, settings, fleet):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from hangar import identity
    from hangar.auth import Principal

    who = {"p": Principal("alice", "tok-a", ["user"], person="alice", role="member")}

    async def actor():
        identity.current.set(who["p"])
        return who["p"].name

    app = FastAPI()
    h = SimpleNamespace(store=svc.store, fleet=fleet)
    inbox_notify.mount(app, h, Depends(actor))

    @app.exception_handler(people.PeopleError)
    async def _bad(_r, e):
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": str(e)}, 400)

    c = TestClient(app)
    r = c.put("/api/people/alice/delivery", json={"dm": "off", "digest": ["10:00"]})
    assert r.status_code == 200 and r.json()["notify"] == "off" and r.json()["digest"] == ["10:00"]
    assert c.put("/api/people/root/delivery", json={"dm": "off"}).status_code == 403
    assert c.put("/api/people/alice/delivery", json={"dm": "loud"}).status_code == 400
    who["p"] = Principal("ops", "tok-o", ["admin"], person="root", role="admin")
    assert c.put("/api/people/alice/delivery", json={"dm": "questions"}).json()["notify"] == "questions"


# ====================================================================== the digest
def _h(svc: JobService):
    return SimpleNamespace(jobs=svc)


async def test_two_digests_a_weekday_across_a_dst_change(svc, fake, monkeypatch):
    """Every minute from Thursday to Wednesday over the end of DST (2026-11-01): exactly 10:15 and 16:00 local on
    each weekday, none on the weekend."""
    sent: list[float] = []

    async def fake_send(_svc, user, text, api=None):
        sent.append(clock["t"])
        return True

    async def fake_digest(_svc, p, since=None, t=None):
        return "*hangar digest*\n• something"

    clock = {"t": 0.0}
    monkeypatch.setattr(inbox_notify, "send_dm", fake_send)
    monkeypatch.setattr(inbox_notify, "digest", fake_digest)
    t = datetime(2026, 10, 29, 0, 0, tzinfo=NY).timestamp()
    end = datetime(2026, 11, 4, 23, 59, tzinfo=NY).timestamp()
    while t <= end:
        clock["t"] = t
        await digest_tick(_h(svc), t)
        t += 60
    local = [datetime.fromtimestamp(x, NY) for x in sent]
    by_day: dict[str, list[str]] = {}
    for d in local:
        by_day.setdefault(d.strftime("%a %d"), []).append(d.strftime("%H:%M"))
    # alice and root both have Slack: two each per weekday
    assert by_day == {d: ["10:15", "10:15", "16:00", "16:00"] for d in
                      ("Thu 29", "Fri 30", "Mon 02", "Tue 03", "Wed 04")}


async def test_restart_before_a_slot_neither_skips_nor_doubles(svc, fake, monkeypatch):
    sends: list[str] = []

    async def fake_send(_svc, user, text, api=None):
        sends.append(user)
        return True

    async def fake_digest(_svc, p, since=None, t=None):
        return "digest"

    monkeypatch.setattr(inbox_notify, "send_dm", fake_send)
    monkeypatch.setattr(inbox_notify, "digest", fake_digest)
    set_delivery(svc.store, "root", dm="off")
    at = lambda h, m, s=0: datetime(2026, 10, 14, h, m, s, tzinfo=NY).timestamp()  # noqa: E731 - a Wednesday
    await digest_tick(_h(svc), at(10, 14, 30))  # first look after a restart: yesterday's 16:00 is too late
    assert sends == []
    await digest_tick(_h(svc), at(10, 15, 30))  # the next minute: the 10:15 slot
    await digest_tick(_h(svc), at(10, 16, 30))  # ... once
    assert sends == ["UALICE"]
    # down from 15:00 to 18:30: the 16:00 slot is under two hours late, so it goes out on recovery
    assert await digest_tick(_h(svc), at(17, 59)) == {"alice": "sent"}
    # down past 2 hours: the slot is recorded and skipped
    assert await digest_tick(_h(svc), datetime(2026, 10, 15, 12, 30, tzinfo=NY).timestamp()) == {"alice": "skipped"}
    assert sends == ["UALICE", "UALICE"]


def test_due_slot_weekends_and_off():
    wed = datetime(2026, 10, 14, 10, 20, tzinfo=NY).timestamp()
    sat = datetime(2026, 10, 17, 10, 20, tzinfo=NY).timestamp()
    p = Person(id="a")
    assert due_slot(p, wed, None)[1] is True
    assert due_slot(p, sat, None) == (datetime(2026, 10, 16, 16, 0, tzinfo=NY).timestamp(), False)
    assert due_slot(Person(id="a", digest_weekends=True), sat, None)[1] is True
    assert due_slot(Person(id="a", digest=[]), wed, None) == (None, False)


async def test_digest_content(svc, fake, fleet, settings):
    people.link(svc.store, "alice", "github", "583231")
    settings.github_token_credential = "gh"
    svc.broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_x")
    settings.github_app_slug = "acme-hangar"
    t = now()
    q_old = _ask(svc, "alice", "older?")
    q_old.updated_at = t - 7200
    q_new = _ask(svc, "alice", "newer?")
    q_new.updated_at = t - 60
    failed = _job(svc, "alice", JobState.FAILED, reason="verify_failed")
    failed.updated_at = t - 600
    for n in (11, 12, 13):  # waiting for review: counted, never listed
        r = _job(svc, "alice", JobState.SUCCEEDED, title=f"pr {n}")
        r.result.pr_number, r.result.pr_url, r.landing.pr_state = n, f"https://github.com/o/r/pull/{n}", "open"
    stuck = _job(svc, "alice", JobState.SUCCEEDED, title="stuck")
    stuck.result.pr_number, stuck.result.pr_url = 14, "https://github.com/o/r/pull/14"
    stuck.landing.pr_state, stuck.landing.note = "approved", "CI is red on main"
    p = people.get(svc.store, "alice")
    text = await digest(svc, p, since=t - 3600, t=t)
    assert text is not None
    assert text.index("older?") < text.index("newer?") < text.index("verify_failed") < text.index("CI is red")
    assert "2h ago" in text
    assert "pull/11" not in text and "pull/12" not in text and "pr 11" not in text  # never itemized
    assert "3 agent PRs await your review: <https://github.com/pulls?q=is:open+is:pr+review-requested:octocat" \
           "+author:app/acme-hangar>" in text
    assert "Open agent PRs: 4 of 5" in text  # --- #159: approved-but-not-landing counts; the default cap is 5
    assert "Sandboxes not up yet" not in text
    # failures from before the last digest aren't repeated
    assert "verify_failed" not in (await digest(svc, p, since=t - 60, t=t) or "")
    # nothing waiting: no digest
    assert await digest(svc, people.get(svc.store, "root"), t=t) is None


async def test_digest_caps_items_and_says_when_sandboxes_are_down(svc, fake, fleet, monkeypatch):
    for i in range(25):
        _ask(svc, "alice", f"q{i}?")
    monkeypatch.setattr(type(fleet.power), "off", property(lambda _s: True))
    monkeypatch.setattr(fleet.power, "status_line", lambda: "sandboxes power on at 10:00")
    text = await digest(svc, people.get(svc.store, "alice"))
    assert text is not None and text.count("\n• ") == 20 and "…and 5 more: `hangar inbox`" in text
    assert "Sandboxes not up yet: sandboxes power on at 10:00" in text
    assert "on GitHub" not in text  # no reviews, no review line


async def test_digest_tick_sends_through_slack_and_honours_dm_off(svc, fake):
    _ask(svc, "alice", "digest me?")
    at = datetime(2026, 10, 14, 10, 15, 5, tzinfo=NY).timestamp()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(inbox, "now", lambda: at)
        assert await digest_tick(_h(svc), at) == {"alice": "sent", "root": "empty"}
        assert "*hangar digest*" in fake.to("UALICE")[0] and "digest me?" in fake.to("UALICE")[0]
        set_delivery(svc.store, "alice", dm="off")
        assert await digest_tick(_h(svc), at + 6 * 3600) == {"root": "empty"}
    assert len(fake.to("UALICE")) == 1 and json.loads(svc.store.kv_get("digest:last:root") or "0")


async def test_the_digest_lists_closed_agent_prs_without_an_abandon_reason(svc, fake, fleet, monkeypatch):
    """--- #168: the inbox's `abandon` items (weekly.inbox_source) reach the digest."""
    from hangar import inbox, weekly

    monkeypatch.setattr(inbox, "SOURCES", [*inbox.SOURCES, weekly.inbox_source])
    j = _job(svc, "alice", JobState.SUCCEEDED, title="closed")
    j.result.pr_number, j.result.pr_url = 21, "https://github.com/o/r/pull/21"
    j.landing.pr_state, j.landing.closed_at = "closed", now() - 600
    svc.store.put(j)
    text = await digest(svc, people.get(svc.store, "alice"), t=now())
    assert text is not None and "Closed without a reason" in text and "/hangar abandon" in text
    j.landing.abandon_reason = "superseded"
    svc.store.put(j)
    assert "Closed without a reason" not in (await digest(svc, people.get(svc.store, "alice"), t=now()) or "")
