"""Tracker intake (docs/intake.md): Jira, and what it shares with GitHub issues and Slack.

Jira's REST API and GitHub's REST and GraphQL APIs are faked with httpx.MockTransport. The inbound payloads are
recorded shapes (tests/fixtures/intake), following Atlassian's webhook docs and GitHub's webhook and GraphQL schemas;
every delivery is signed for real."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from hangar import identity, intake, jira, people, redeliver
from hangar.frontdoor import ReplayGuard
from hangar.github import verify_signature
from hangar.jobs import Caller, JobService, SubmitJob
from hangar.models import Credential, CredentialKind, Job, JobState, Person
from hangar.triggers import new_approval

FIX = Path(__file__).parent / "fixtures" / "intake"
SECRET = "jira-webhook-secret-0123456789"
API_TOKEN = "ATATT3xFfGF0-jira-api-token"
AUTO_TOKEN = "automation-shared-token-xyz"
GH_TOKEN = "ghp_controlplanetoken"
DANA = "5b10ac8d82e05b22cc7d4ef5"
REAL_RESOLVE = people.resolve_external  # integration: #91's, before any test stands in for it
needs_tools = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


# ============================================================================ fakes
class FakeAPIs:
    """Jira Cloud's REST v3 (acme.atlassian.net) and GitHub's REST API (api.github.com)."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.jira_fail: int | None = None
        self.ratelimit = 0
        self.statuses = ["In Progress", "Blocked", "In Review", "Done"]
        # --- review: the issue's history (Cloud's changelog, oldest first): who moved it where
        # (#128: just before the fixture deliveries' timestamp, so an Automation request finds a recent transition)
        self.history = [{"id": "1", "created": "2025-10-14T11:06:00.000+0000",
                         "author": {"accountId": DANA, "displayName": "Dana Lee"},
                         "items": [{"field": "status", "fromString": "To Do", "toString": "Ready for agent"}]}]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == "acme.atlassian.net":
            return self.jira(request)
        if request.url.host == "api.github.com":
            return self.github(request)
        return httpx.Response(404)

    def jira(self, r: httpx.Request) -> httpx.Response:
        want = "Basic " + base64.b64encode(f"hangar@acme.com:{API_TOKEN}".encode()).decode()
        if r.headers.get("authorization") != want:
            return httpx.Response(401, json={"errorMessages": ["Client must be authenticated"]})
        if self.ratelimit:
            self.ratelimit -= 1
            return httpx.Response(429, headers={"Retry-After": "1"})
        if self.jira_fail:
            return httpx.Response(self.jira_fail, json={"errorMessages": ["Issue does not exist"]})
        p = r.url.path
        if p.endswith("/transitions") and r.method == "GET":
            return httpx.Response(200, json={"transitions": [
                {"id": str(i + 11), "name": f"Move to {s}", "to": {"name": s}} for i, s in enumerate(self.statuses)]})
        if p.endswith("/transitions") and r.method == "POST":
            return httpx.Response(204)
        if p.endswith("/comment") and r.method == "POST":
            return httpx.Response(201, json={"id": "1"})
        if p.endswith("/remotelink"):
            return httpx.Response(201, json={"id": 1})
        if p.endswith("/changelog"):  # --- review
            start, n = int(r.url.params.get("startAt", 0)), int(r.url.params.get("maxResults", 100))
            return httpx.Response(200, json={"startAt": start, "maxResults": n, "total": len(self.history),
                                             "values": self.history[start:start + n]})
        if p.endswith("/myself"):
            return httpx.Response(200, json={"accountId": "hangar-bot", "displayName": "Hangar"})
        if p.startswith("/rest/api/3/issue/"):
            issue = fixture("jira_issue_updated.json")["issue"]
            issue["fields"]["description"] = {"type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "From the API, in ADF."}]}]}
            return httpx.Response(200, json=issue)
        return httpx.Response(404, json={"errorMessages": ["nope"]})

    def github(self, r: httpx.Request) -> httpx.Response:
        if r.headers.get("authorization") != f"Bearer {GH_TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if r.url.path.endswith("/comments") and r.method == "POST":
            return httpx.Response(201, json={"id": 1})
        if r.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "Not Found"})

    # what hangar did
    def jira_comments(self) -> list[str]:
        return [jira.adf_text(json.loads(c.content)["body"]).strip() for c in self.calls
                if c.url.host == "acme.atlassian.net" and c.method == "POST" and c.url.path.endswith("/comment")]

    def transitions(self) -> list[str]:
        return [json.loads(c.content)["transition"]["id"] for c in self.calls
                if c.url.host == "acme.atlassian.net" and c.method == "POST" and c.url.path.endswith("/transitions")]



def link(monkeypatch, links: dict[tuple[str, str], str]) -> None:
    """Stand in for the people links (teamcreds): (source, external id) -> person."""
    def resolve(store, source, ext):
        who = links.get((source, ext))
        return Person(id=who, name=who) if who else None

    monkeypatch.setattr(people, "resolve_external", resolve)


def sign(body: bytes, secret: str = SECRET) -> dict[str, str]:
    return {"X-Hub-Signature": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


JIRA = {"url": "https://acme.atlassian.net", "email": "hangar@acme.com", "token_credential": "jira-api",
        "webhook_secret_credential": "jira-hook", "automation_token_credential": "jira-auto",
        "projects": {"ABC": {"repo": "REPO", "components": {"Web": "acme/web"}}},
        "trigger": {"status": "Ready for agent", "label": "hangar"},
        "statuses": {"started": "In Progress", "needs_input": "Blocked", "delivered": "In Review"}}


def configure(svc: JobService, repo: str, env: str) -> None:
    s, b = svc.settings, svc.broker
    b.put("jira-api", "webhook", CredentialKind.GENERIC, API_TOKEN)
    b.put("jira-hook", "webhook", CredentialKind.GENERIC, SECRET)
    b.put("jira-auto", "webhook", CredentialKind.GENERIC, AUTO_TOKEN)
    b.put("gh", "git", CredentialKind.GIT_TOKEN, GH_TOKEN)
    s.github_token_credential = "gh"
    cfg = copy.deepcopy(JIRA)
    cfg["projects"]["ABC"]["repo"] = repo
    s.jira = cfg
    s.github_repos = {repo: env, "acme/api": env}


async def until(pred, timeout: float = 20.0, what: str = "") -> None:
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out: {what}")


async def idle(svc: JobService, job_id: str) -> Job:
    while job_id in svc._active:
        await asyncio.sleep(0.05)
    for _ in range(20):  # background write-backs
        await asyncio.sleep(0.02)
    return svc.get(job_id)


# ============================================================================ unit
def test_atlassian_signature_test_vector():
    """Atlassian's documented example: the same `sha256=<hex HMAC>` over the body that GitHub uses."""
    body = b"Hello World!"
    sig = "sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9"
    assert verify_signature("It's a Secret to Everybody", body, sig)
    assert not verify_signature("It's a Secret to Everybody", body + b" ", sig)


def test_adf_round_trip_and_wiki():
    doc = jira.to_adf("[hangar] Done: job `job-1`\nPR: https://github.com/o/r/pull/7.\n\n> Use A\n> or B?\n\nBye")
    assert doc["type"] == "doc" and doc["version"] == 1
    p1, quote, p3 = doc["content"]
    texts = [n for n in p1["content"] if n["type"] == "text"]
    assert {"type": "text", "text": "job-1", "marks": [{"type": "code"}]} in texts
    assert {"type": "text", "text": "https://github.com/o/r/pull/7",
            "marks": [{"type": "link", "attrs": {"href": "https://github.com/o/r/pull/7"}}]} in texts
    assert {"type": "text", "text": "."} in texts and {"type": "hardBreak"} in p1["content"]
    assert quote["type"] == "blockquote" and "Use A" in jira.adf_text(quote)
    assert all(n.get("text") != "" for n in json.loads(json.dumps(doc)) if isinstance(n, dict))
    assert jira.adf_text(doc).startswith("[hangar] Done: job job-1\nPR:")
    assert jira.adf_text("plain") == "plain" and jira.adf_text(None) == ""
    assert jira.to_wiki("run `make test`\n> quoted") == "run {{make test}}\nbq. quoted"


def test_config_validation(settings):
    ok = copy.deepcopy(JIRA)
    assert jira.JiraConfig.model_validate(ok).url == "https://acme.atlassian.net"
    for change, msg in [
        ({"statuses": {"failed": "ready for AGENT"}}, "trigger status"),
        ({"email": None}, "needs email"),
        ({"url": "http://jira.local"}, "https"),
        ({"trigger": {}}, "trigger.status"),
        ({"webhook_secret_credential": None, "automation_token_credential": None}, "webhook_secret_credential"),
        ({"projects": {"abc": {"repo": "o/r"}}}, "upper case"),
    ]:
        with pytest.raises(ValueError, match=msg):
            jira.JiraConfig.model_validate({**ok, **change})
    assert jira.JiraConfig.model_validate({**ok, "deployment": "server", "email": None}).deployment == "server"
    settings.jira = {**ok, "email": None}
    with pytest.raises(jira.JiraConfigError):
        jira.load(settings)


def test_jira_credentials_never_reach_a_sandbox(settings):
    """#81's exposure class: the Jira API token, webhook secret and Automation token are control-plane only."""
    settings.jira = copy.deepcopy(JIRA)
    for name in ("jira-api", "jira-hook", "jira-auto"):
        cred = Credential(name=name, harness="git", kind=CredentialKind.GENERIC, exposure="sandbox")
        assert identity.locked(cred, settings) and identity.exposure(cred, settings) == "control_plane"
    other = Credential(name="claude", harness="claude-code", kind=CredentialKind.GENERIC)
    assert identity.exposure(other, settings) == "sandbox"


def test_ticket_keys():
    assert intake.ticket_key({"jira.issue": "ABC-12"}) == "ABC-12"
    assert intake.ticket_key({"jira": "KAN-35"}) == "KAN-35" and intake.ticket_key({}, "jira:ZZ9-1") == "ZZ9-1"
    assert intake.ticket_key({intake.TRACKER: "jira:OPS-7"}) == "OPS-7"
    assert intake.ticket_key({}, None, "jira:PAY-9") == "PAY-9" and intake.ticket_key({}, None, "github:a/b#3") is None
    for labels, ref in (({"jira": "kan-35"}, None), ({"jira": "KAN-35; rm"}, None), ({}, "github:a/b#3"),
                        ({intake.TRACKER: "ghproject:p:i"}, None), ({"jira": "K-1"}, None), (None, None)):
        assert intake.ticket_key(labels, ref) is None, (labels, ref)
    assert intake.ticket_title("Make apply atomic", "KAN-35") == "KAN-35: Make apply atomic"
    assert intake.ticket_title("KAN-35: Make apply atomic", "KAN-35") == "KAN-35: Make apply atomic"
    assert intake.ticket_title("KAN-350 is another", "KAN-35") == "KAN-35: KAN-350 is another"
    assert intake.ticket_title("Make apply atomic", None) == "Make apply atomic"


def test_claims_are_scoped(store):
    g = ReplayGuard(store)
    assert redeliver.claim(g, "d1", 60, scope="jira") and not redeliver.claim(g, "d1", 60, scope="jira")
    assert redeliver.claim(g, "d1", 60)  # GitHub's scope is separate
    redeliver.done(g, "d1", 60, scope="jira")
    assert g.get("jira", "d1") == redeliver.DONE


def test_stages():
    job = Job(id="j", title="t", instructions="x", requester="r", owner="r", branch="b", environment="e")
    for state, kind, want in [(JobState.RUNNING, "note", "started"), (JobState.NEEDS_INPUT, "note", "needs_input"),
                              (JobState.SUCCEEDED, "final", "delivered"), (JobState.FAILED, "final", "failed"),
                              (JobState.CANCELLED, "final", "failed"), (JobState.QUEUED, "note", None)]:
        job.state = state
        assert intake.stage(job, kind) == want


def test_person_for_fails_closed(monkeypatch, store):
    assert intake.person_for(store, "jira", "") is None and intake.person_for(store, "jira", None) is None
    assert intake.person_for(store, "jira", "x") is None  # the stub: nobody is linked yet

    def boom(*a):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(people, "resolve_external", boom)
    assert intake.person_for(store, "github", "dana") is None


async def test_jira_api_retries_and_never_leaks_the_token():
    fake = FakeAPIs()
    cfg = jira.JiraConfig.model_validate(JIRA)
    api = jira.JiraAPI(cfg, API_TOKEN, transport=httpx.MockTransport(fake))
    fake.ratelimit = 2
    await api.comment("ABC-12", "hello `x`")
    assert len(fake.calls) == 3
    body = json.loads(fake.calls[-1].content)["body"]
    assert body["type"] == "doc" and fake.calls[-1].url.path == "/rest/api/3/issue/ABC-12/comment"
    fake.jira_fail = 404
    with pytest.raises(jira.JiraError) as e:
        await api.transition("ABC-12", "Done")
    assert "Issue does not exist" in str(e.value) and API_TOKEN not in str(e.value)
    await api.close()
    server = jira.JiraConfig.model_validate({**JIRA, "deployment": "server", "url": "https://jira.acme.internal"})
    seen = []
    api = jira.JiraAPI(server, "pat-123", transport=httpx.MockTransport(
        lambda r: seen.append(r) or httpx.Response(201, json={})))
    await api.comment("ABC-12", "run `make`")
    assert seen[0].url.path == "/rest/api/2/issue/ABC-12/comment" and seen[0].headers["authorization"] == \
        "Bearer pat-123" and json.loads(seen[0].content) == {"body": "run {{make}}"}
    await api.close()


# ============================================================================ Jira (no agent runs)
@pytest.fixture
async def door(fleet, settings, monkeypatch):
    """A JobService that queues but doesn't run jobs, with Jira configured, and the Jira route mounted."""
    fake = FakeAPIs()
    svc = JobService(fleet, None, transport=httpx.MockTransport(fake))
    configure(svc, "https://github.com/acme/api.git", "base")
    link(monkeypatch, {("jira", DANA): "dana", ("github", "4242"): "dana"})  # --- review: GitHub user ids
    ts = fixture("jira_issue_updated.json")["timestamp"] / 1000
    app = FastAPI()
    front = jira.JiraFrontDoor(svc, clock=lambda: ts + 60)
    front.mount(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hangar") as client:
        yield {"svc": svc, "fake": fake, "client": client, "front": front}
    for t in list(svc._background):
        t.cancel()


async def post(client, payload: dict, headers: dict | None = None, raw: bytes | None = None) -> httpx.Response:
    body = raw if raw is not None else json.dumps(payload).encode()
    return await client.post("/api/hooks/jira", content=body, headers=headers if headers is not None else sign(body))


async def test_jira_status_change_queues_a_job_for_the_linked_person(door):
    svc, fake, client = door["svc"], door["fake"], door["client"]
    payload = fixture("jira_issue_updated.json")
    r = await post(client, payload, {**sign(json.dumps(payload).encode()),
                                     "X-Atlassian-Webhook-Identifier": "wh-1"})
    assert r.status_code == 200 and r.json().get("ok"), r.json()
    job = svc.get(r.json()["job"])
    assert job.state == JobState.QUEUED and job.person == "dana" and job.source == "jira"
    assert job.source_ref == "jira:ABC-12" and job.requester == f"jira:{DANA}" and job.untrusted_source is None
    assert job.labels[intake.TRACKER] == "jira:ABC-12" and job.idempotency_key == "jira-delivery:wh-1"
    assert job.title == "ABC-12: Retry uploads that time out" and job.repo == "https://github.com/acme/api.git"
    assert job.branch == f"hangar/ABC-12-{job.id}"  # --- ticket keys: the title already starts with the key
    [ctx] = job.context
    # --- review: anyone who can edit the issue can rewrite its text, so it is data even from a linked reporter,
    # and the summary isn't part of the instructions
    assert ctx.name == "jira-ABC-12.md" and ctx.untrusted and "never retried" in ctx.content
    assert "Retry uploads" not in job.instructions and "`jira-ABC-12.md`" in job.instructions
    assert "https://acme.atlassian.net/browse/ABC-12" in ctx.content
    await until(lambda: fake.jira_comments(), what="queued comment")
    assert fake.jira_comments()[0].startswith(f"[hangar] Queued as hangar job {job.id}")

    # Jira's retry of the same delivery, and a replay of it, do nothing
    again = await post(client, payload)
    assert again.status_code == 200 and again.json() == {"ignored": "duplicate delivery"}
    # a new delivery for the same issue while its job is open doesn't start a second
    later = {**payload, "timestamp": payload["timestamp"] + 1000}
    r2 = await post(client, later)
    assert r2.status_code == 200 and "already" in r2.json()["ignored"] and len(svc._jobs) == 1


async def test_jira_refuses_bad_signatures_stale_and_untriggered(door):
    svc, client = door["svc"], door["client"]
    payload = fixture("jira_issue_updated.json")
    body = json.dumps(payload).encode()
    assert (await post(client, payload, sign(body, "wrong"))).status_code == 401
    assert (await post(client, payload, {"Content-Type": "application/json"})).status_code == 401
    assert (await post(client, payload, {"X-Hangar-Token": "nope"})).status_code == 401
    stale = {**payload, "timestamp": payload["timestamp"] - 3 * 3600 * 1000}
    assert (await post(client, stale)).json() == {"ignored": "missing or stale timestamp"}
    assert (await post(client, {k: v for k, v in payload.items() if k != "timestamp"})).json()["ignored"] \
        == "missing or stale timestamp"
    other = copy.deepcopy(payload)
    other["changelog"]["items"][0]["toString"] = "In Progress"
    assert (await post(client, other)).json() == {"ignored": "not the trigger status or label"}
    unmapped = copy.deepcopy(payload)
    unmapped["issue"]["key"], unmapped["issue"]["fields"]["project"]["key"] = "ZZZ-1", "ZZZ"
    assert "not mapped" in (await post(client, unmapped)).json()["ignored"]
    assert (await post(client, {}, raw=b"[1]", headers=sign(b"[1]"))).status_code == 400
    assert (await post(client, {}, raw=b"x" * 1_100_000, headers=sign(b"x"))).status_code == 413
    assert not svc._jobs


async def test_jira_label_and_component_routing(door):
    svc, client = door["svc"], door["client"]
    svc.settings.github_repos["https://github.com/acme/web.git"] = svc.settings.github_repos["acme/api"]
    payload = fixture("jira_issue_updated.json")
    payload["changelog"]["items"] = [{"field": "labels", "fromString": "backend", "toString": "backend hangar"}]
    payload["issue"]["fields"]["components"] = [{"name": "Web"}]
    r = await post(client, payload)
    assert r.json().get("ok"), r.json()
    assert svc.get(r.json()["job"]).repo == "https://github.com/acme/web.git"
    # a label that was already there isn't a new trigger
    payload["timestamp"] += 1
    payload["issue"]["key"] = "ABC-13"
    payload["changelog"]["items"] = [{"field": "labels", "fromString": "hangar", "toString": "hangar x"}]
    assert (await post(client, payload)).json() == {"ignored": "not the trigger status or label"}


async def test_jira_unlinked_sender_waits_for_a_linked_approval(door, monkeypatch):
    svc, fake, client = door["svc"], door["fake"], door["client"]
    payload = fixture("jira_issue_updated.json")
    payload["user"] = {"accountId": "557058:outsider", "displayName": "Outside Contractor"}
    payload["issue"]["fields"]["reporter"] = payload["user"]
    r = await post(client, payload)
    job = svc.get(r.json()["job"])
    assert r.json()["awaiting_approval"] and job.state == JobState.AWAITING_APPROVAL
    assert job.person is None and job.untrusted_source == "jira:557058:outsider" and job.context[0].untrusted
    await until(lambda: any("/hangar approve" in c for c in fake.jira_comments()), what="approval note")

    comment = fixture("jira_comment_created.json")
    comment["comment"]["body"] = "/hangar approve"
    comment["comment"]["author"] = payload["user"]  # the outsider can't approve their own job
    res = await post(client, comment)
    assert "not linked" in res.json()["ignored"] and job.state == JobState.AWAITING_APPROVAL
    comment["comment"]["author"] = {"accountId": DANA, "displayName": "Dana Lee"}
    comment["timestamp"] += 1
    people.add(svc.store, "dana", "Dana Lee")  # --- approvals (#121): a decider's person must exist
    res = await post(client, comment)
    assert res.json() == {"ok": True, "approved": job.id} and job.state == JobState.QUEUED
    assert (job.approval.rule, job.approval.decided_person, job.approval.self_approved) == ("front_door", "dana", False)
    assert job.approval.decided_by == "jira:dana"
    await until(lambda: any("Approved by dana" in c for c in fake.jira_comments()), what="approved note")


async def test_jira_approval_by_the_jobs_own_person_is_refused(door, monkeypatch):
    """#121: a linked person's `/hangar approve` on their own held job (a spent daily cap) is refused with a comment
    that says why, and audited as job.approve.refused; an admin's linked account approves it."""
    svc, fake, client = door["svc"], door["fake"], door["client"]
    people.add(svc.store, "dana", "Dana Lee")
    job = svc.submit(SubmitJob(instructions="x", environment="base", deliver="none"),
                     Caller(f"jira:{DANA}", f"jira:{DANA}", person="dana"), source="jira",
                     source_ref=jira.ref_of("ABC-12"), approval=new_approval(svc, f"jira:{DANA}", "budget: cap spent"))
    comment = fixture("jira_comment_created.json")
    comment["comment"]["body"] = "/hangar approve"  # by Dana herself
    res = await post(client, comment)
    assert "nobody approves their own" in res.json()["ignored"] and job.state == JobState.AWAITING_APPROVAL
    await until(lambda: any("dana can't approve" in c for c in fake.jira_comments()), what="refusal note")
    [refused] = [e for e in svc.store.audit_tail(20) if e.action == "job.approve.refused"]
    assert refused.target == job.id and refused.detail["self_approved"] is True
    people.add(svc.store, "ada", role="admin")
    link(monkeypatch, {("jira", DANA): "dana", ("jira", "557058:ada"): "ada"})
    comment["comment"]["author"] = {"accountId": "557058:ada", "displayName": "Ada"}
    comment["timestamp"] += 1
    res = await post(client, comment)
    assert res.json() == {"ok": True, "approved": job.id} and job.approval.rule == "admin"


async def test_jira_comment_rules(door):
    svc, fake, client = door["svc"], door["fake"], door["client"]
    comment = fixture("jira_comment_created.json")
    # hangar's own comments, and comments that aren't /hangar with no question open, do nothing
    comment["comment"]["body"] = "[hangar] Queued as hangar job"
    assert "own" in (await post(client, comment)).json()["ignored"]
    comment["comment"]["body"], comment["timestamp"] = "looks good", comment["timestamp"] + 1
    assert (await post(client, comment)).json() == {"ignored": "not a /hangar comment"}
    # `/hangar <more>` from a linked person on an issue with no job starts one, with their instructions
    comment["comment"]["body"] = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "/hangar also cover resumable uploads"}]}]}
    comment["timestamp"] += 1
    del comment["issue"]["fields"]  # a slim comment payload: the issue is read through the API
    res = await post(client, comment)
    assert res.json().get("ok"), res.json()
    job = svc.get(res.json()["job"])
    assert "Additional instructions from Dana Lee:\nalso cover resumable uploads" in job.instructions
    assert any(c.url.path == "/rest/api/3/issue/ABC-12" for c in fake.calls)  # read the issue as it is now


async def test_jira_automation_web_request(door):
    svc = door["svc"]
    ts = fixture("jira_issue_updated.json")["timestamp"] / 1000
    from datetime import UTC, datetime

    now = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.0+0000")  # what {{now}} renders
    body = json.dumps({"webhookEvent": "hangar:automation", "timestamp": now, "issue": {"key": "ABC-12"},
                       "user": {"accountId": DANA, "displayName": "Dana Lee"}}).encode()
    r = await post(door["client"], {}, raw=body, headers={"X-Hangar-Token": AUTO_TOKEN,
                                                          "Content-Type": "application/json"})
    assert r.status_code == 200 and r.json().get("ok"), r.json()
    job = svc.get(r.json()["job"])
    assert job.person == "dana" and "From the API, in ADF." in job.context[0].content
    assert [c.url.path for c in door["fake"].calls if c.url.path.endswith("/changelog")] == [
        "/rest/api/3/issue/ABC-12/changelog"] * 2  # the total, then the last page


async def test_jira_automation_body_cant_say_who_acted(door):
    """Review: the Automation token isn't a signature, and anyone who can edit the rule writes its body (and can
    point it, hidden header and all, at a server of their own). A body naming Dana doesn't make the job Dana's
    (her trust, budget and own Claude login): who moved the issue comes from Jira's history."""
    svc, fake = door["svc"], door["fake"]
    ts = fixture("jira_issue_updated.json")["timestamp"] / 1000
    from datetime import UTC, datetime

    fake.history = [*fake.history, {"id": "2", "created": "2025-10-14T11:06:30.000+0000",
                                     "author": {"accountId": "557058:mallory", "displayName": "Mallory"},
                                     "items": [{"field": "status", "fromString": "In Progress",
                                                "toString": "Ready for agent"}]}]
    now = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.0+0000")
    body = json.dumps({"webhookEvent": "hangar:automation", "timestamp": now, "issue": {"key": "ABC-12"},
                       "user": {"accountId": DANA, "displayName": "Dana Lee"}}).encode()
    r = await post(door["client"], {}, raw=body, headers={"X-Hangar-Token": AUTO_TOKEN,
                                                          "Content-Type": "application/json"})
    job = svc.get(r.json()["job"])
    assert job.person is None and job.state == JobState.AWAITING_APPROVAL
    assert job.requester == "jira:557058:mallory" and job.untrusted_source == "jira:557058:mallory"
    # nobody in the history, and not created that way: nobody acted, so nobody is trusted
    assert jira.mover([], {"status": {"name": "To Do"}}, jira.JiraTrigger(status="Ready for agent")) is None
    creator = {"accountId": DANA}
    assert jira.mover([], {"status": {"name": "Ready for agent"}, "creator": creator, "reporter": {"accountId": "x"}},
                      jira.JiraTrigger(status="Ready for agent"))["author"] == creator
    labeled = [{"created": "1", "author": {"accountId": "a"}, "items": [{"field": "labels", "fromString": "",
                                                                        "toString": "hangar"}]},
               {"created": "2", "author": {"accountId": "b"}, "items": [{"field": "labels", "fromString": "hangar",
                                                                        "toString": "hangar x"}]}]
    assert jira.mover(labeled, {}, jira.JiraTrigger(label="hangar"))["author"] == {"accountId": "a"}


def automation(ts: float, key: str = "ABC-12", **extra) -> bytes:
    from datetime import UTC, datetime

    now = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.0+0000")  # what {{now}} renders
    return json.dumps({"webhookEvent": "hangar:automation", "timestamp": now, "issue": {"key": key}, **extra}).encode()


AUTO = {"X-Hangar-Token": AUTO_TOKEN, "Content-Type": "application/json"}


async def test_jira_automation_forged_for_an_old_transition_is_ignored_audited_and_alerted(door):
    """#128: the token alone doesn't start a job. A request for an issue whose trigger transition was two days ago
    (anyone holding the token, at any time) is ignored, audited with the mover and the age, and the admins hear."""
    from hangar import inbox
    from hangar.auth import Principal

    svc, fake, front = door["svc"], door["fake"], door["front"]
    fake.history[0]["created"] = "2025-10-12T11:00:00.000+0000"  # two days before the request
    ts = front.clock()
    r = await post(door["client"], {}, raw=automation(ts - 60), headers=AUTO)
    assert r.status_code == 200 and r.json() == {"ignored": "no recent transition"}, r.json()
    assert not svc._jobs
    [ev] = [e for e in svc.store.audit_tail() if e.action == "hook.automation.stale_transition"]
    assert ev.target == "jira:ABC-12" and ev.detail["mover"] == DANA and ev.detail["issue"] == "ABC-12"
    assert ev.detail["transition_age_seconds"] > 2 * 86400 - 3600
    jira.register_inbox()
    admin = Principal(name="t", id="t", role="admin", person="owner", scopes=["admin"])
    member = Principal(name="m", id="m", role="member", person="dana", scopes=["user"])
    [item] = [i for i in jira.inbox_items(svc, admin)]
    assert item["kind"] == "alert" and "ABC-12" in item["reason"] and "2 days" in item["reason"]
    assert jira.inbox_items(svc, member) == []
    assert any(i["kind"] == "alert" for i in inbox.items(svc, admin))
    # one alert per issue per hour: a second forged request is audited, not alerted again
    r = await post(door["client"], {}, raw=automation(ts - 30), headers=AUTO)
    assert r.json() == {"ignored": "no recent transition"}
    assert len(jira.inbox_items(svc, admin)) == 1
    assert len([e for e in svc.store.audit_tail() if e.action == "hook.automation.stale_transition"]) == 2
    # nobody in the history at all: the same
    fake.history = []
    r = await post(door["client"], {}, raw=automation(ts - 20, "ABC-12"), headers=AUTO)
    assert r.json() == {"ignored": "no recent transition"} and not svc._jobs


async def test_jira_automation_within_ten_minutes_starts_and_skew_is_allowed(door):
    svc, fake, front = door["svc"], door["fake"], door["front"]
    from datetime import UTC, datetime

    t = front.clock()
    fake.history[0]["created"] = datetime.fromtimestamp(t - 9 * 60, UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    r = await post(door["client"], {}, raw=automation(t - 5), headers=AUTO)
    assert r.json().get("ok"), r.json()
    # Jira's clock a little ahead of hangar's: a transition "in the future" within max_age_minutes still counts
    t2 = jira.JiraFrontDoor(svc, clock=lambda: t)
    cfg = jira.load(svc.settings)
    ahead = {"author": {"accountId": DANA},
             "created": datetime.fromtimestamp(t + 300, UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")}
    assert t2._automation_refusal(cfg, "ABC-99", ahead) is None
    far = {**ahead, "created": datetime.fromtimestamp(t + 3 * 3600, UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")}
    assert t2._automation_refusal(cfg, "ABC-99", far) == {"ignored": "no recent transition"}
    eleven = {**ahead, "created": datetime.fromtimestamp(t - 11 * 60, UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")}
    assert t2._automation_refusal(cfg, "ABC-98", eleven) == {"ignored": "no recent transition"}
    svc.settings.jira = {**svc.settings.jira, "automation_max_transition_age_minutes": 15}
    assert t2._automation_refusal(jira.load(svc.settings), "ABC-98", eleven) is None


async def test_jira_automation_per_issue_cap_holds_and_is_audited(door):
    svc, front = door["svc"], door["front"]
    from datetime import UTC, datetime

    svc.settings.jira = {**svc.settings.jira, "automation_max_starts_per_issue_per_day": 2}
    cfg = jira.load(svc.settings)
    t = front.clock()
    change = {"author": {"accountId": DANA},
              "created": datetime.fromtimestamp(t - 60, UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")}
    for _ in range(2):
        assert front._automation_refusal(cfg, "ABC-12", change) is None
        front._count_start("ABC-12")
    res = front._automation_refusal(cfg, "ABC-12", change)
    assert res is not None and "cap 2" in res["ignored"]
    [ev] = [e for e in svc.store.audit_tail() if e.action == "hook.automation.capped"]
    assert ev.target == "jira:ABC-12" and ev.detail["starts_today"] == 2 and ev.detail["cap"] == 2
    # per issue: another issue (a bulk move) isn't held by it
    assert front._automation_refusal(cfg, "ABC-13", change) is None
    # a day later the count has rolled off
    later = jira.JiraFrontDoor(svc, clock=lambda: t + 86400 + 1)
    change2 = {**change, "created": datetime.fromtimestamp(t + 86400, UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")}
    assert later._automation_refusal(cfg, "ABC-12", change2) is None
    # end to end: a started job counts
    svc.store.kv_delete(f"{jira.STARTS_PREFIX}ABC-12")
    r = await post(door["client"], {}, raw=automation(t - 5), headers=AUTO)
    assert r.json().get("ok"), r.json()
    assert len(front._starts("ABC-12", t)) == 1


async def test_jira_bad_token_alerts_the_admins_once_an_hour(door, monkeypatch):
    from hangar import people as people_mod
    from hangar.auth import Principal

    svc = door["svc"]
    sent: list[tuple[str, str]] = []

    class FakeSlack:
        async def post(self, channel, text, thread_ts=None):
            sent.append((channel, text))
            return "1"

        async def close(self):
            pass

    from hangar import slack

    monkeypatch.setattr(slack, "client_for", lambda s: FakeSlack())
    monkeypatch.setattr(people_mod, "list_people", lambda store: [
        Person(id="owner", role="admin", links=["slack:T01ABCDEF/U01ADMIN1"]),
        Person(id="dana", role="member", links=["slack:T01ABCDEF/U01DANA11"])])
    for _ in range(2):
        r = await post(door["client"], {}, raw=automation(door["front"].clock()), headers={
            "X-Hangar-Token": "guess", "Content-Type": "application/json"})
        assert r.status_code == 401
    await until(lambda: sent, what="admin DM")
    await asyncio.sleep(0.05)
    assert [c for c, _ in sent] == ["U01ADMIN1"] and "X-Hangar-Token" in sent[0][1]
    admin = Principal(name="t", id="t", role="admin", person="owner", scopes=["admin"])
    [item] = jira.inbox_items(svc, admin)
    assert "wrong X-Hangar-Token" in item["reason"]


async def test_doctor_warns_on_an_old_automation_token_and_unsigned_issue_events(door):
    from hangar.models import Credential

    svc = door["svc"]
    h = type("H", (), {"store": svc.store, "settings": svc.settings})()
    cfg = jira.load(svc.settings)
    checks = {c["name"]: c for c in jira.automation_checks(h, cfg)}
    assert checks["jira automation token age"]["status"] == "ok"
    # configured with a secret, but no issue event has come through the signed webhook yet
    assert checks["jira signed issue events"]["status"] == "warn"
    c = svc.store.get(Credential, "jira-auto")
    c.updated_at = c.created_at = c.updated_at - 91 * 86400
    svc.store.put(c)
    checks = {c["name"]: c for c in jira.automation_checks(h, cfg)}
    assert checks["jira automation token age"]["status"] == "warn" and "--rotate" in \
        checks["jira automation token age"]["fix"]
    # a signed issue event arrives: that warning clears
    payload = fixture("jira_issue_updated.json")
    await post(door["client"], payload)
    checks = {c["name"]: c for c in jira.automation_checks(h, cfg)}
    assert checks["jira signed issue events"]["status"] == "ok"
    # no webhook secret at all: issue events can only be the Automation fallback
    svc.settings.jira = {**svc.settings.jira, "webhook_secret_credential": None}
    checks = {c["name"]: c for c in jira.automation_checks(h, jira.load(svc.settings))}
    assert checks["jira signed issue events"]["status"] == "warn" and "no webhook_secret_credential" in \
        checks["jira signed issue events"]["detail"]
    # no Automation token: nothing to say
    svc.settings.jira = {**svc.settings.jira, "webhook_secret_credential": "jira-hook",
                         "automation_token_credential": None}
    assert jira.automation_checks(h, jira.load(svc.settings)) == []


async def test_jira_automation_token_cant_pose_as_a_webhook_event(door):
    """#128: the token isn't a signature. With it, a forged "issue updated" naming Dana, or a "comment created" with
    `/hangar approve` from her, would skip the history check; a token request is only ever hangar:automation."""
    svc = door["svc"]
    payload = fixture("jira_issue_updated.json")
    r = await post(door["client"], {}, raw=json.dumps(payload).encode(), headers=AUTO)
    assert r.status_code == 200 and "only sends hangar:automation" in r.json()["ignored"] and not svc._jobs
    comment = fixture("jira_comment_created.json")
    comment["comment"]["body"] = "/hangar approve"
    r = await post(door["client"], {}, raw=json.dumps(comment).encode(), headers=AUTO)
    assert "only sends hangar:automation" in r.json()["ignored"]
    assert [e for e in svc.store.audit_tail() if e.action == "hook.automation.wrong_event"]


def test_cred_add_rotate_prints_the_rule_steps(monkeypatch):
    from typer.testing import CliRunner

    from hangar import cli

    calls: list[tuple[str, str, dict]] = []

    def fake_call(method, path, **kw):
        calls.append((method, path, kw))
        if method == "GET":
            return [{"name": "jira-auto", "harness": "webhook", "kind": "generic"}]
        return {"name": "jira-auto", "harness": "webhook", "kind": "generic", "exclusive": False}

    monkeypatch.setattr(cli, "_call", fake_call)
    monkeypatch.setattr(cli, "_client", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    res = CliRunner().invoke(cli.app, ["cred", "add", "jira-auto", "--rotate"])
    assert res.exit_code == 0, res.output
    [(_, _, put)] = [c for c in calls if c[0] == "POST"]
    token = put["json"]["value"]
    assert len(token) >= 40 and put["json"]["harness"] == "webhook" and put["json"]["kind"] == "generic"
    assert token in res.output and "X-Hangar-Token" in res.output and "Hidden" in res.output
    res = CliRunner().invoke(cli.app, ["cred", "add", "nope"])
    assert res.exit_code == 2


async def test_only_the_jobs_person_continues_it_from_jira(door, monkeypatch):
    """Review: a linked teammate's comment must not answer or continue someone else's job: it runs with that
    person's own login and budget."""
    svc, fake, client = door["svc"], door["fake"], door["client"]
    link(monkeypatch, {("jira", DANA): "dana", ("jira", "557058:eve"): "eve"})
    payload = fixture("jira_issue_updated.json")
    job = svc.get((await post(client, payload)).json()["job"])
    assert job.person == "dana"
    job.state = JobState.NEEDS_INPUT
    comment = fixture("jira_comment_created.json")
    comment["comment"]["author"] = comment["comment"]["updateAuthor"] = {"accountId": "557058:eve",
                                                                         "displayName": "Eve"}
    res = (await post(client, comment)).json()
    assert "dana's job" in res["ignored"] and len(job.turns) == 1
    await until(lambda: any("Can't continue" in c and "dana's job" in c for c in fake.jira_comments()),
                what="refusal on the issue")
    job.state = JobState.SUCCEEDED  # `/hangar <more>` after it's done: likewise
    comment["comment"]["body"], comment["timestamp"] = "/hangar now do more", comment["timestamp"] + 1
    assert "dana's job" in (await post(client, comment)).json()["ignored"]


async def test_intake_uses_the_real_people_links(door, monkeypatch):
    """Integration (#91 + #92): Jira senders resolve through #91's people links, not a stub that would silently
    shadow them."""
    monkeypatch.setattr(people, "resolve_external", REAL_RESOLVE)
    svc, client = door["svc"], door["client"]
    people.add(svc.store, "dana")
    people.link(svc.store, "dana", "jira", DANA)
    job = svc.get((await post(client, fixture("jira_issue_updated.json"))).json()["job"])
    assert job.person == "dana" and job.state == JobState.QUEUED


async def test_jira_write_back_failures_land_on_the_job(door):
    svc, fake = door["svc"], door["fake"]
    job = svc.submit(SubmitJob(instructions="x", repo="https://github.com/acme/api.git",
                               labels={intake.TRACKER: "jira:ABC-12"}),
                     Caller("jira:x", "jira:x"), source="jira", source_ref="jira:ABC-12")
    fake.jira_fail = 404
    job.state = JobState.RUNNING
    await svc._comment(job, "jira:ABC-12", "Agent started on hangar job", "note")
    await until(lambda: any("could not update jira:ABC-12 (started)" in e.message for e in job.events),
                what="tracker failure on the job")
    assert any("could not comment on Jira issue ABC-12" in e.message for e in job.events)
    assert job.labels[intake.STAGE] == "started"
    fake.jira_fail = None
    fake.statuses = ["Done"]  # the workflow offers no way into "In Review" from here
    job.state, job.result.pr_url, job.result.pr_number = JobState.SUCCEEDED, "https://github.com/acme/api/pull/9", 9
    await svc._comment(job, "jira:ABC-12", "done", "final")
    await until(lambda: any("no transition to 'In Review'" in e.message for e in job.events), what="no transition")
    link_ = next(c for c in fake.calls if c.url.path.endswith("/remotelink"))
    assert json.loads(link_.content)["object"]["url"] == "https://github.com/acme/api/pull/9"
    final = fake.jira_comments()[-1]
    assert final.startswith(f"[hangar] Done: hangar job {job.id}") and "PR: https://github.com/acme/api/pull/9" in final


# ============================================================================ Jira end to end (a scripted agent runs)
@needs_tools
async def test_jira_question_answered_by_a_comment_then_delivered(fleet, xsessions, world, monkeypatch):  # noqa: F811
    """Status change -> job; the agent's question goes to the issue and the issue moves to Blocked; a linked
    teammate's comment answers it; the job finishes and the issue moves to In Review."""
    fake = FakeAPIs()
    jobs = JobService(fleet, xsessions, transport=httpx.MockTransport(fake))
    configure(jobs, world["url"], "xenv")
    link(monkeypatch, {("jira", DANA): "dana"})
    await jobs.start()
    try:
        ts = fixture("jira_issue_updated.json")["timestamp"] / 1000
        front = jira.JiraFrontDoor(jobs, clock=lambda: ts + 60)
        payload = fixture("jira_issue_updated.json")
        payload["issue"]["fields"]["summary"] = "TASK-ASK pick one"
        res = await front.handle(jira.load(jobs.settings), payload)
        job = jobs.get(res["job"])
        await until(lambda: jobs.get(job.id).state in (JobState.NEEDS_INPUT, JobState.FAILED), what="question")
        job = await idle(jobs, job.id)
        assert job.state == JobState.NEEDS_INPUT, [e.message for e in job.events]
        await until(lambda: any("Question" in c for c in fake.jira_comments()), what="question comment")
        comments = fake.jira_comments()
        started = next(c for c in comments if c.startswith("[hangar] Agent started"))
        assert "Follow it in hangar: http://localhost:8787/#jobs" in started
        q = next(c for c in comments if "Question" in c)
        assert "Use A or B?" in q and "Answer in a comment on this issue" in q
        await until(lambda: fake.transitions() == ["11", "12"], what="In Progress, then Blocked")

        answer = fixture("jira_comment_created.json")
        res = await front.handle(jira.load(jobs.settings), answer)
        assert res == {"ok": True, "follow_up": job.id}
        await until(lambda: jobs.get(job.id).state in (JobState.SUCCEEDED, JobState.FAILED), what="done")
        job = await idle(jobs, job.id)
        assert job.state == JobState.SUCCEEDED and job.turns[1].requester == f"jira:{DANA}"
        assert job.turns[1].reply_to == "jira:ABC-12" and job.turns[1].summary == "Went with B"
        await until(lambda: any(c.startswith("[hangar] Done") for c in fake.jira_comments()), what="final")
        await until(lambda: fake.transitions() == ["11", "12", "13"], what="... and In Review")
        done = next(c for c in fake.jira_comments() if c.startswith("[hangar] Done"))
        assert f"Branch: {job.branch}" in done and "/hangar <message>" in done
    finally:
        await jobs.stop()


def test_routes_doctor_and_ignores_project_items(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    app = create_app(settings)
    fake = FakeAPIs()
    with TestClient(app) as c:
        h = app.state.hangar
        body = json.dumps(fixture("jira_issue_updated.json")).encode()
        assert c.post("/api/hooks/jira", content=body, headers=sign(body)).status_code == 404  # off until set
        h.jobs.transport = httpx.MockTransport(fake)
        configure(h.jobs, "acme/api", "base")
        link(monkeypatch, {("github", "dana"): "dana"})
        r = c.post("/api/hooks/jira", content=body, headers=sign(body))  # recorded a while ago: too old now
        assert r.status_code == 200 and r.json() == {"ignored": "missing or stale timestamp"}
        assert c.post("/api/hooks/jira", content=body, headers=sign(body, "x")).status_code == 401
        big = fixture("jira_issue_updated.json")
        big["issue"]["fields"]["description"] = "x" * 200_000  # past the 64 KB anonymous cap
        big = json.dumps(big).encode()
        assert c.post("/api/hooks/jira", content=big, headers=sign(big)).json() == {
            "ignored": "missing or stale timestamp"}
        checks = {x["name"]: x for x in c.get("/api/doctor", headers={"Authorization": "Bearer t0ken"}).json()}
        assert checks["jira api"]["status"] == "ok" and "Hangar" in checks["jira api"]["detail"]
        assert checks["jira project ABC"]["status"] == "ok" and checks["jira credential jira-hook"]["status"] == "ok"

        h.broker.put("gh-hook", "webhook", CredentialKind.GENERIC, "gh-secret")
        settings.github_webhook_secret_credential = "gh-hook"
        # --- KAN-217: the GitHub Projects door is gone: an organization's projects_v2_item starts nothing
        body = json.dumps({"action": "edited", "projects_v2_item": {"content_type": "Issue"}}).encode()
        headers = {"X-Hub-Signature-256": "sha256=" + hmac.new(b"gh-secret", body, hashlib.sha256).hexdigest(),
                   "X-GitHub-Event": "projects_v2_item", "X-GitHub-Delivery": "g-1",
                   "Content-Type": "application/json"}
        r = c.post("/api/hooks/github", content=body, headers=headers)
        assert "GitHub Projects intake was removed" in r.json()["ignored"], r.json()


# test_exchange's scripted agent asks "Use A or B?" for TASK-ASK and answers TASK-USE-B
from tests.test_exchange import svc as xsessions  # noqa: E402, F401 - a fixture, registered under this name
from tests.test_exchange import world  # noqa: E402, F401
