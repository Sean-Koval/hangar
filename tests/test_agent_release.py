"""KAN-480: only a person releases a Jira ticket to agents (docs/intake/agent-release.md).

Before hangar starts a job for a ticket labelled `exec:agent` (or `lane:agent`), it reads the issue's changelog. The
change adding the label, and the last move into the trigger status, must be by an account on HANGAR_JIRA's
`agent_release.people`. Anything else, or a changelog that can't be read, refuses the job with a comment. Jira is the
fake from test_intake, with its changelog replaced per test."""

from __future__ import annotations

import copy
import json

import httpx
import pytest

from hangar import jira
from tests.test_intake import DANA, JIRA, FakeAPIs, door, fixture, post, until  # noqa: F401 - fixtures

BOT = "557058:automation-bot"
MOVE = {"field": "status", "fromString": "To Do", "toString": "Ready for agent"}


def label(before: str, after: str) -> dict:
    return {"field": "labels", "fromString": before, "toString": after}


def change(account: str, at: str, *items: dict) -> dict:
    return {
        "id": at,
        "created": f"2025-10-14T11:0{at}:00.000+0000",
        "author": {"accountId": account, "displayName": "Dana Lee" if account == DANA else "Automation"},
        "items": list(items),
    }


def released(payload: dict, *labels: str) -> dict:
    payload = copy.deepcopy(payload)
    payload["issue"]["fields"]["labels"] = ["backend", *labels]
    return payload


class ReleasedJira(FakeAPIs):
    """The fake Jira, whose API returns the issue with the release label (as the webhook payload carries it)."""

    def __init__(self, labels: list[str]):
        super().__init__()
        self.labels = labels
        self.changelog_fails = False

    def jira(self, r):
        if self.changelog_fails and r.url.path.endswith("/changelog"):
            return httpx.Response(403, json={"errorMessages": ["You do not have permission"]})
        res = super().jira(r)
        if r.method == "GET" and r.url.path.startswith("/rest/api/3/issue/") and r.url.path.count("/") == 5:
            body = json.loads(res.content)
            body["fields"]["labels"] = ["backend", *self.labels]
            body["fields"]["creator"] = {"accountId": DANA, "displayName": "Dana Lee"}
            return httpx.Response(200, json=body)
        return res


@pytest.fixture
async def released_door(door):  # noqa: F811 - the fixture from test_intake
    svc = door["svc"]
    fake = ReleasedJira(["exec:agent"])
    svc.transport.handler = fake  # same MockTransport, a Jira that serves the release label
    svc.settings.jira = {**svc.settings.jira, "agent_release": {"people": [DANA]}}
    return {**door, "fake": fake}


# ============================================================================ the rule (no HTTP)
def cfg(**release) -> jira.JiraConfig:
    return jira.JiraConfig.model_validate({**JIRA, **({"agent_release": release} if release else {})})


def test_agent_release_allow_list_comes_from_config_with_no_default():
    with pytest.raises(ValueError):
        cfg(labels=["exec:agent"])  # no people
    with pytest.raises(ValueError):
        cfg(people=[])
    with pytest.raises(ValueError):
        cfg(people=["not an id; rm"])
    c = cfg(people=[DANA])
    assert c.agent_release is not None and c.agent_release.labels == ["exec:agent", "lane:agent"]
    assert jira.JiraConfig.model_validate(JIRA).agent_release is None


def test_agent_release_rule_by_the_changelog():
    c = cfg(people=[DANA])
    fields = {"labels": ["exec:agent"], "status": {"name": "Ready for agent"}, "creator": {"accountId": DANA}}
    person = [change(DANA, "1", label("", "exec:agent")), change(DANA, "2", MOVE)]
    assert jira.release_refusal(c, person, fields) is None
    # a service account added the label, or made the move
    why = jira.release_refusal(c, [change(BOT, "1", label("", "exec:agent")), change(DANA, "2", MOVE)], fields)
    assert why and "added the `exec:agent` label" in why and "allow-list" in why
    why = jira.release_refusal(c, [change(DANA, "1", label("", "exec:agent")), change(BOT, "2", MOVE)], fields)
    assert why and "moved it to Ready for agent" in why
    # the newest change counts: a person's older release doesn't cover a bot's re-add
    later = [*person, change(DANA, "3", label("exec:agent", "")), change(BOT, "4", label("", "exec:agent"))]
    assert jira.release_refusal(c, later, fields)
    # a change with no author (anonymous) refuses
    anon = [{**change(DANA, "1", label("", "exec:agent")), "author": None}, change(DANA, "2", MOVE)]
    assert jira.release_refusal(c, anon, fields)
    # lane:agent is a release label too
    lane = {**fields, "labels": ["lane:agent"]}
    assert jira.release_refusal(c, [change(BOT, "1", label("", "lane:agent")), change(DANA, "2", MOVE)], lane)
    # a ticket without a release label is not this rule's
    assert jira.release_refusal(c, [], {**fields, "labels": ["backend"]}) is None


def test_agent_release_fails_closed():
    fields = {"labels": ["exec:agent"], "status": {"name": "Ready for agent"}, "creator": {"accountId": DANA}}
    person = [change(DANA, "1", label("", "exec:agent")), change(DANA, "2", MOVE)]
    # no allow-list configured: a release label refuses
    assert "no allow-list" in (jira.release_refusal(cfg(), person, fields) or "")
    c = cfg(people=[DANA])
    # no move in the history, and the issue isn't in the trigger status
    assert "nobody who moved it" in (jira.release_refusal(c, person[:1], {**fields, "status": {"name": "To Do"}}) or "")
    # set at creation by a listed person counts, but only when the whole history was read
    assert jira.release_refusal(c, [], fields) is None
    filler = [change(DANA, "9", {"field": "summary", "fromString": "a", "toString": "b"})] * jira.HISTORY_PAGE
    assert "nobody who added" in (jira.release_refusal(c, filler, fields) or "")
    assert jira.release_refusal(c, [], {**fields, "creator": {"accountId": BOT}})


# ============================================================================ the door
async def test_agent_release_by_a_service_account_starts_no_job_and_comments(released_door):
    svc, fake, client = released_door["svc"], released_door["fake"], released_door["client"]
    fake.history = [change(BOT, "1", label("backend", "backend exec:agent")), change(DANA, "6", MOVE)]
    r = await post(client, released(fixture("jira_issue_updated.json"), "exec:agent"))
    assert r.status_code == 200 and "allow-list" in r.json()["refused"], r.json()
    assert not svc._jobs
    await until(lambda: fake.jira_comments(), what="refusal comment")
    [note] = fake.jira_comments()
    assert note.startswith("[hangar] hangar didn't start a job") and "exec:agent" in note
    assert any(e.action == "jira.agent_release.refused" for e in svc.store.audit_tail(20))


async def test_agent_release_by_a_listed_person_starts_the_job(released_door):
    svc, fake, client = released_door["svc"], released_door["fake"], released_door["client"]
    fake.history = [change(DANA, "1", label("backend", "backend exec:agent")), change(DANA, "6", MOVE)]
    r = await post(client, released(fixture("jira_issue_updated.json"), "exec:agent"))
    assert r.json().get("ok"), r.json()
    assert svc.get(r.json()["job"]).person == "dana"


async def test_agent_release_unreadable_changelog_refuses(released_door):
    svc, fake, client = released_door["svc"], released_door["fake"], released_door["client"]
    fake.changelog_fails = True
    r = await post(client, released(fixture("jira_issue_updated.json"), "exec:agent"))
    assert "could not check who released it" in r.json()["refused"], r.json()
    assert not svc._jobs
    await until(lambda: fake.jira_comments(), what="refusal comment")


async def test_agent_release_without_an_allow_list_refuses(released_door):
    svc, fake, client = released_door["svc"], released_door["fake"], released_door["client"]
    svc.settings.jira = {k: v for k, v in svc.settings.jira.items() if k != "agent_release"}
    fake.history = [change(DANA, "1", label("backend", "backend exec:agent")), change(DANA, "6", MOVE)]
    r = await post(client, released(fixture("jira_issue_updated.json"), "exec:agent"))
    assert "no allow-list" in r.json()["refused"] and not svc._jobs


async def test_agent_release_automation_path_uses_the_same_check(released_door):
    svc, fake, client = released_door["svc"], released_door["fake"], released_door["client"]
    fake.history = [change(BOT, "1", label("backend", "backend exec:agent")), change(BOT, "6", MOVE)]
    payload = {
        "webhookEvent": "hangar:automation",
        "timestamp": fixture("jira_issue_updated.json")["timestamp"],
        "issue": {"key": "ABC-12"},
    }
    r = await post(
        client, payload, {"X-Hangar-Token": "automation-shared-token-xyz", "Content-Type": "application/json"}
    )
    assert "allow-list" in r.json().get("refused", ""), r.json()
    assert not svc._jobs


async def test_agent_release_does_not_touch_unlabelled_tickets(released_door):
    """A ticket with no release label goes through the door as before."""
    fake, client = released_door["fake"], released_door["client"]
    fake.history = [change(BOT, "6", MOVE)]
    r = await post(client, fixture("jira_issue_updated.json"))
    assert r.json().get("ok"), r.json()
