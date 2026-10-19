"""Relayed work from an account linked to nobody (WP4 + change sets): under HANGAR_RELAY_UNLINKED=approve the bot's
submit_job (single-repo or a change set) waits in awaiting_approval for a trusted person; their approval releases it;
the bot can't approve it; under `refuse` the account gets 403. End to end over /mcp with the real middleware."""

from __future__ import annotations

import json

from hangar import approvals, changesets
from hangar.ext.notify import REPLY_TO_LABEL
from hangar.models import JobState
from tests.test_identity import _call
from tests.test_relay import REPO, H, _session, world  # noqa: F401 - fixtures

PROFILE = {"schema": "hangar.changeset-profile/v1", "name": "svc", "environment": "plain",
           "repos": {"infra": {"repo": "https://github.com/o/infra"}, "code": {"repo": REPO}},
           "land_order": ["infra", "code"]}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bot", "version": "1"}}}


def _submit(world, claim: str, **args) -> dict:  # noqa: F811
    c = world["client"]
    res = _call(c, _session(c, world["sym-bot"], claim), "submit_job",
                {"instructions": "PAY-9: refunds", "deliver": "none", **args})
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _approve(world, token: str, claim: str, job: str) -> tuple[bool, str]:  # noqa: F811
    c = world["client"]
    res = _call(c, _session(c, world[token], claim), "approve_job", {"job": job})
    return not res.get("isError"), res["content"][0]["text"]


def test_an_unlinked_accounts_change_set_is_held_and_an_approval_releases_it(world):  # noqa: F811
    h = world["h"]
    h.settings.relay_unlinked = "approve"
    changesets.profiles(h.jobs).put(PROFILE, "admin")
    claim = json.dumps({"source": "symphony", "id": 999, "reply_to": "symphony:42:7"})
    out = _submit(world, claim, changeset="svc", ticket="jira:PAY-9")
    job = h.jobs.get(out["id"])
    assert job.state == JobState.AWAITING_APPROVAL and job.person is None
    assert job.approval.requested_by == "symphony:999" and "linked to no hangar person" in job.approval.reason
    assert job.untrusted_source == "symphony:999" and job.labels[REPLY_TO_LABEL] == "symphony:42:7"
    cs = changesets.of(h.jobs).of(job)
    assert cs.parent_job == job.id and cs.state == "working" and out["changeset"]["id"] == cs.id
    assert h.jobs._queue.empty()  # held: nothing queued
    # the bot can't approve it: not as the held account (it may only submit), nor with a relay-only token
    ok, why = _approve(world, "sym-bot", "symphony:999", job.id)
    assert not ok and "only submit work for approval" in why
    ok, why = _approve(world, "sym-bot", "symphony:103", job.id)
    assert not ok and "relay_approve" in why
    assert h.jobs.get(job.id).state == JobState.AWAITING_APPROVAL
    # a trusted person (ada, an admin, through the approving relay) releases it
    ok, why = _approve(world, "sym-approver", "symphony:103", job.id)
    assert ok, why
    job = h.jobs.get(job.id)
    assert job.state == JobState.QUEUED and job.approval.decided_person == "ada" and not h.jobs._queue.empty()


def test_an_unlinked_accounts_single_repo_job_is_held(world):  # noqa: F811
    h = world["h"]
    h.settings.relay_unlinked = "approve"
    assert approvals.HOLD_SUBMIT is True
    job = h.jobs.get(_submit(world, "symphony:999", repo=REPO, environment="plain")["id"])
    assert job.state == JobState.AWAITING_APPROVAL and job.approval.requested_by == "symphony:999"
    assert job.changeset is None
    # a linked account through the same bot is not held: it runs as its person, under their own rules
    mine = h.jobs.get(_submit(world, "symphony:101", repo=REPO, environment="plain")["id"])
    assert mine.state == JobState.QUEUED and mine.approval is None and mine.person == "alice"


def test_an_unlinked_account_is_refused_under_refuse(world):  # noqa: F811
    c, h = world["client"], world["h"]
    assert h.settings.relay_unlinked == "refuse"
    r = c.post("/mcp", json=INIT, headers={"Accept": "application/json, text/event-stream",
                                           "Authorization": f"Bearer {world['sym-bot']}", H: "symphony:999"})
    assert r.status_code == 403 and "isn't linked to a hangar person" in r.json()["error"]
    assert not [j for j in h.jobs.list() if j.approval and j.approval.requested_by == "symphony:999"]
