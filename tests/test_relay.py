"""A relay bot acting for people (I-§3.3, design WP4): `auth.resolve_delegation` and the approval rules through it.

A `relay` token (the Symphony bot's) names, per request, the Symphony account it acts for in X-Hangar-On-Behalf-Of.
A linked account's request runs as that person, never with more than their `mcp` token would get; an unlinked one
is refused (or, opt-in, may only submit work held for approval); approving through a relay needs `relay_approve`
and is never the approver's own work. Every refusal path has a test here.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from fastapi.testclient import TestClient

import hangar.identity as identity
from hangar import approvals, auth, people
from hangar.auth import DelegationRefused, Principal, TokenAuth, resolve_delegation
from hangar.jobs import Caller, SubmitJob
from hangar.models import ApiToken, JobState
from hangar.triggers import new_approval
from tests.conftest import FakeProvider
from tests.test_identity import _call, _mcp_session

H = auth.RELAY_HEADER
REPO = "https://github.com/o/r"
ADMIN_H = {"Authorization": "Bearer t0ken"}


@pytest.fixture
def tokens(store):
    """Members alice (Symphony user 101) and bob (102), admin ada (103), member carol linked only on GitHub; a
    `relay` and a `relay_approve` token for an admin bot person, and alice's own mcp token."""
    a = TokenAuth(store)
    people.add(store, "bot", role="admin")
    for pid, role, sym in (("alice", "member", "101"), ("bob", "member", "102"), ("ada", "admin", "103")):
        people.add(store, pid, role=role)
        people.link(store, pid, "symphony", sym)
    people.add(store, "carol")
    people.link(store, "carol", "github", "77")
    relay, _ = a.create("sym-bot", ["relay"], person="bot")
    approver, _ = a.create("sym-approver", ["relay_approve"], person="bot")
    mcp, _ = a.create("cc-alice", ["mcp"], person="alice")
    return {"auth": a, "relay": relay, "approve": approver, "mcp": mcp}


def _as(t, token: str, claim: str | dict | None, **kw) -> Principal | None:
    a = t["auth"]
    headers = {} if claim is None else {H: claim if isinstance(claim, str) else json.dumps(claim)}
    return resolve_delegation(a, a.principal(token), headers, **kw)


# ====================================================================== who a relayed request runs as
def test_a_linked_account_runs_as_its_person_with_mcp_only(tokens):
    p = _as(tokens, tokens["relay"], "symphony:101")
    assert (p.person, p.role, p.scopes, p.via, p.on_behalf_of) == ("alice", "member", ["mcp"], "relay:sym-bot",
                                                                   "symphony:101")
    assert not p.admin and not p.held and not p.may_approve
    assert identity.actor_name(p) == "@alice:relay:sym-bot"  # both identities on every record and audit entry
    # the JSON form, with where the bot wants answers posted
    p = _as(tokens, tokens["relay"], {"source": "symphony", "id": 102, "reply_to": "symphony:lX1h-AK_k:msg1"})
    assert (p.person, p.reply_to) == ("bob", "symphony:lX1h-AK_k:msg1")
    # an admin person keeps their role (what their own mcp token sees), but never the admin scope
    p = _as(tokens, tokens["approve"], "symphony:103")
    assert (p.person, p.role, p.scopes, p.may_approve) == ("ada", "admin", ["mcp"], True)
    with _current(p):
        assert not identity.is_admin()
        with pytest.raises(identity.Forbidden):
            identity.require("admin")


def test_requests_without_a_claim_are_unchanged(tokens):
    a = tokens["auth"]
    who = a.principal(tokens["mcp"])
    assert resolve_delegation(a, who, {}) is who
    assert resolve_delegation(a, None, {}) is None
    assert resolve_delegation(a, None, {H: "symphony:101"}) is None  # no token: the 401 path, not a delegation


# ====================================================================== refusals
@pytest.mark.parametrize("case,claim,match", [
    ("relay_without_claim", None, "acts only on behalf of someone"),
    ("not_a_relay_source_github", "github:77", "vouches only for symphony"),
    ("not_a_relay_source_email", {"source": "email", "id": "a@b.co"}, "vouches only for symphony"),
    ("unknown_source", "teams:1", "vouches only for symphony"),
    ("non_numeric_id", "symphony:alice", "Symphony user id"),
    ("empty_id", "symphony:", "Symphony user id"),
    ("bool_id", {"source": "symphony", "id": True}, "string or a number"),
    ("extra_key", {"source": "symphony", "id": "101", "person": "ada"}, "JSON object with source, id"),
    ("not_an_object", "[1, 2]", "JSON object"),
    ("bad_json", "{nope", "isn't valid JSON"),
    ("too_long", "symphony:" + "1" * 2000, "longer than"),
    ("reply_to_elsewhere", {"source": "symphony", "id": "101", "reply_to": "slack:C1:2"}, "reply_to must be"),
    ("reply_to_junk", {"source": "symphony", "id": "101", "reply_to": "symphony:a b"}, "reply_to must be"),
    ("unlinked", "symphony:999", "isn't linked to a hangar person"),
])
def test_each_refusal(tokens, case, claim, match):
    with pytest.raises(DelegationRefused, match=match):
        _as(tokens, tokens["relay"], claim)


def test_only_a_relay_token_may_claim(tokens, store):
    a = tokens["auth"]
    # a person's own tokens (mcp, login) and the server's admin token can't name someone else
    with pytest.raises(DelegationRefused, match="needs a relay token"):
        _as(tokens, tokens["mcp"], "symphony:102")
    owner = TokenAuth(store, admin_token="t0ken")
    with pytest.raises(DelegationRefused, match="needs a relay token"):
        resolve_delegation(owner, owner.principal("t0ken"), {H: "symphony:102"})
    # a relay scope on a member's token is never honoured (members' tokens hold people.MEMBER_SCOPES only) ...
    raw = "hgr_member-relay"
    store.put(ApiToken(name="sneaky", token_hash=auth.hash_token(raw), scopes=["relay"], person="alice"))
    assert a.principal(raw).scopes == []
    with pytest.raises(DelegationRefused, match="needs a relay token"):
        _as(tokens, raw, "symphony:102")
    # ... and minting one is refused
    with pytest.raises(people.PeopleError, match="member"):
        people.check_token_scopes(people.get(store, "alice"), ["relay"])
    # an already-delegated principal can't delegate again
    p = _as(tokens, tokens["relay"], "symphony:101")
    with pytest.raises(DelegationRefused, match="delegate again"):
        resolve_delegation(a, p, {H: "symphony:102"})


def test_a_relay_token_holds_relay_scopes_only(tokens):
    for scopes in (["relay", "mcp"], ["relay_approve", "admin"], ["relay", "user"]):
        with pytest.raises(ValueError, match="holds only relay"):
            tokens["auth"].create("x", scopes, person="bot")
    tokens["auth"].create("ok", ["relay", "relay_approve"], person="bot")


def test_a_relay_token_never_acts_as_itself(tokens):
    """Not on MCP (no claim: refused), not as an OAuth consent login, not as a login token for approvals."""
    a = tokens["auth"]
    assert a.identify(tokens["relay"]) is None and a.identify(tokens["approve"]) is None
    assert a.verify(tokens["relay"], "mcp") is None and a.verify(tokens["relay"], "user") is None
    me = approvals.from_principal(a.principal(tokens["approve"]))
    assert me.token and "login token" in me.token


def test_revoked_token_and_removed_person(tokens, store):
    a = tokens["auth"]
    gone, tok = a.create("gone-bot", ["relay"], person="bot")
    a.revoke(tok.id)
    assert _as(tokens, gone, "symphony:101") is None
    people.remove(store, "bob")  # their link goes with them: an unlinked account now
    with pytest.raises(DelegationRefused, match="isn't linked"):
        _as(tokens, tokens["relay"], "symphony:102")
    people.remove(store, "bot")  # the relay's own person: its tokens are revoked
    assert a.principal(tokens["relay"]) is None


def test_refusals_are_audited(tokens):
    seen = []
    with pytest.raises(DelegationRefused):
        _as(tokens, tokens["relay"], "symphony:999", audit=lambda *a, **kw: seen.append((a, kw)))
    [(args, kw)] = seen
    assert args == ("@bot:sym-bot", "relay.refused", "symphony:999") and "isn't linked" in kw["why"]


# ====================================================================== unlinked accounts (HANGAR_RELAY_UNLINKED)
def test_unlinked_accounts_are_refused_or_held(tokens, monkeypatch):
    with pytest.raises(DelegationRefused, match="isn't linked"):
        _as(tokens, tokens["relay"], "symphony:999", unlinked="refuse")
    with pytest.raises(DelegationRefused, match="one of refuse, approve"):
        _as(tokens, tokens["relay"], "symphony:999", unlinked="allow")
    # approve: only while the submit path attaches approvals.relay_hold (an unheld team job must never run)
    assert approvals.HOLD_SUBMIT is True
    monkeypatch.setattr(approvals, "HOLD_SUBMIT", False)
    with pytest.raises(DelegationRefused, match="can't hold a relayed job"):
        _as(tokens, tokens["relay"], "symphony:999", unlinked="approve")
    monkeypatch.setattr(approvals, "HOLD_SUBMIT", True)
    p = _as(tokens, tokens["relay"], "symphony:999", unlinked="approve")
    assert (p.person, p.held, p.scopes, p.on_behalf_of) == (None, True, ["mcp"], "symphony:999")
    assert identity.actor_name(p) == "relay:sym-bot(symphony:999)"
    # a linked account is never held
    assert not _as(tokens, tokens["relay"], "symphony:101", unlinked="approve").held


def test_a_held_principal_may_only_submit(tokens, monkeypatch):
    monkeypatch.setattr(approvals, "HOLD_SUBMIT", True)
    p = _as(tokens, tokens["relay"], "symphony:999", unlinked="approve")
    guard = people.Guard(fleet=None)
    sig = inspect.signature(lambda **kw: None)
    with _current(p):
        guard.args("submit_job", sig, (), {"instructions": "x"})
        for tool in ("create_sandbox", "exec", "list_jobs", "follow_up_job", "approve_job", "answer_approval"):
            with pytest.raises(identity.Forbidden, match="only submit work for approval"):
                guard.args(tool, sig, (), {})


def test_relay_hold_is_requested_by_the_account(tokens, monkeypatch):
    monkeypatch.setattr(approvals, "HOLD_SUBMIT", True)
    jobs = type("J", (), {"settings": type("S", (), {"job_approval_ttl_hours": 24})()})()
    p = _as(tokens, tokens["relay"], "symphony:999", unlinked="approve")
    hold = approvals.relay_hold(jobs, p)
    assert hold.requested_by == "symphony:999" and "relay:sym-bot" in hold.reason and hold.expires_at
    assert approvals.relay_hold(jobs, _as(tokens, tokens["relay"], "symphony:101")) is None
    assert approvals.relay_hold(jobs, None) is None


# ====================================================================== approvals through a relay
def test_approver_from_a_relayed_principal(tokens, monkeypatch):
    plain = approvals.from_principal(_as(tokens, tokens["relay"], "symphony:103"))
    assert plain.relay and "relay_approve" in plain.token  # a relay-only token decides nothing
    ok = approvals.from_principal(_as(tokens, tokens["approve"], "symphony:103"))
    assert (ok.actor, ok.person, ok.token, ok.relay, ok.front_door) == ("@ada:relay:sym-approver", "ada", None, True,
                                                                        True)
    assert "symphony:103" in ok.identities
    monkeypatch.setattr(approvals, "HOLD_SUBMIT", True)
    held = approvals.from_principal(_as(tokens, tokens["approve"], "symphony:999", unlinked="approve"))
    assert held.token and "decides nothing" in held.token


def test_the_rule_through_a_relay(settings, tokens, store):
    ctx = type("Ctx", (), {"settings": settings, "store": store})()
    ada = approvals.from_principal(_as(tokens, tokens["approve"], "symphony:103"))
    alice = approvals.from_principal(_as(tokens, tokens["approve"], "symphony:101"))
    bobs = approvals.Subject("bob", frozenset({"@bob:cc"}), REPO)
    team = approvals.Subject(None, frozenset({"github:rando"}), REPO)
    assert approvals.can_decide(ctx, ada, bobs) == approvals.Decision(True, "", "admin")
    # a member through the relay has a member's rights: not an approver
    assert not approvals.can_decide(ctx, alice, bobs).ok
    # never their own: their own job, a job they asked for through the relay, or one their Symphony account asked for
    for own in (approvals.Subject("ada", frozenset(), REPO),
                approvals.Subject("bob", frozenset({"@ada:relay:sym-approver"}), REPO),
                approvals.Subject(None, frozenset({"symphony:103"}), REPO)):
        d = approvals.can_decide(ctx, ada, own)
        assert not d.ok and d.self_approved and "nobody approves their own" in d.why, own
    # a rejection releases nothing: fine
    assert approvals.can_decide(ctx, ada, approvals.Subject("ada", frozenset(), REPO), "reject").ok
    # a front door's trust on a team job, as Slack's: the bot vouches for a linked account
    assert approvals.can_decide(ctx, alice, team).rule == "front_door"


def test_allow_solo_never_applies_through_a_relay(settings, store):
    """With one person, HANGAR_SELF_APPROVAL=allow_solo lets them approve their own job; never through a bot."""
    settings.self_approval = "allow_solo"
    ctx = type("Ctx", (), {"settings": settings, "store": store})()
    a = TokenAuth(store, admin_token="t0ken")
    people.link(store, "owner", "symphony", "1")
    relay, _ = a.create("sym", ["relay_approve"])  # the owner's
    assert len(people.list_people(store)) == 1
    own = approvals.Subject("owner", frozenset(), REPO)
    direct = approvals.from_principal(a.principal("t0ken"))
    assert approvals.can_decide(ctx, direct, own).rule == "solo"
    via = approvals.from_principal(resolve_delegation(a, a.principal(relay), {H: "symphony:1"}))
    assert via.actor == "@owner:relay:sym"
    d = approvals.can_decide(ctx, via, own)
    assert not d.ok and "not through a relay either" in d.why


# ====================================================================== end to end over /mcp
@pytest.fixture
def world(settings, monkeypatch):
    """The real app: MCPAuthMiddleware runs every /mcp request through resolve_delegation (app.py)."""
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.public_url = "http://localhost:8787"
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost:8787") as client:
        h = app.state.hangar
        h.fleet.providers["docker"] = FakeProvider()
        h.jobs._queue = asyncio.Queue()  # keep jobs from running in this HTTP-only test
        for pid, role, sym in (("alice", "member", "101"), ("bob", "member", "102"), ("ada", "admin", "103")):
            client.post("/api/people", json={"id": pid, "role": role}, headers=ADMIN_H)
            client.post(f"/api/people/{pid}/links", json={"source": "symphony", "id": sym}, headers=ADMIN_H)
        client.post("/api/people", json={"id": "bot", "role": "admin"}, headers=ADMIN_H)
        mint = {k: client.post("/api/tokens", json={"name": k, "scopes": [s], "person": "bot"}, headers=ADMIN_H)
                for k, s in (("sym-bot", "relay"), ("sym-approver", "relay_approve"))}
        yield {"client": client, "h": h, **{k: r.json()["token"] for k, r in mint.items()}}


def _session(client, token: str, claim: str | None) -> dict[str, str]:
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
         "Authorization": f"Bearer {token}", **({H: claim} if claim else {})}
    r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bot", "version": "1"}}})
    assert r.status_code == 200, r.text
    h |= {"mcp-session-id": r.headers["mcp-session-id"], "mcp-protocol-version": "2025-06-18"}
    client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return h


def test_the_bot_submits_alices_job_as_alice(world):
    c, h = world["client"], world["h"]
    res = _call(c, _session(c, world["sym-bot"], "symphony:101"), "submit_job",
                {"instructions": "fix it", "repo": REPO, "environment": "plain", "deliver": "none"})
    assert not res.get("isError"), res
    job = h.jobs.get(json.loads(res["content"][0]["text"])["id"])
    assert (job.person, job.requester) == ("alice", "@alice:relay:sym-bot")  # hers, and it says who carried it
    # alice sees it from her own client; bob, through the same bot, doesn't
    login = c.post("/api/people/alice/tokens", json={"name": "cc"}, headers=ADMIN_H).json()["token"]
    mine = _call(c, _mcp_session(c, login), "list_jobs", {})
    assert [j["id"] for j in mine["structuredContent"]["result"]] == [job.id], mine
    got = _call(c, _session(c, world["sym-bot"], "symphony:102"), "get_job", {"job": job.id})
    assert got.get("isError") and "no job" in got["content"][0]["text"]


def test_relay_end_to_end_linked_runs_as_the_person_unlinked_is_refused(world):
    """Over /mcp with the real middleware: a relay-only token (scope `relay`, nothing else) reaches the resolver, a
    linked Symphony account's job is that person's, and an unlinked one is refused with 403 and audited."""
    c, h = world["client"], world["h"]
    claim = json.dumps({"source": "symphony", "id": 102, "reply_to": "symphony:42:7"})
    res = _call(c, _session(c, world["sym-bot"], claim), "submit_job",
                {"instructions": "bump it", "repo": REPO, "environment": "plain", "deliver": "none"})
    assert not res.get("isError"), res
    job = h.jobs.get(json.loads(res["content"][0]["text"])["id"])
    assert (job.person, job.requester) == ("bob", "@bob:relay:sym-bot")
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bot", "version": "1"}}}
    r = c.post("/mcp", json=init, headers={"Accept": "application/json, text/event-stream",
                                           "Authorization": f"Bearer {world['sym-bot']}", H: "symphony:999"})
    assert r.status_code == 403 and "isn't linked to a hangar person" in r.json()["error"]
    refused = [e for e in h.store.audit_tail(50) if e.action == "relay.refused"]
    assert [e.target for e in refused] == ["symphony:999"]
    # HANGAR_RELAY_UNLINKED=approve is read by the middleware: the unlinked account gets in, held (tests below)
    h.settings.relay_unlinked = "approve"
    r = c.post("/mcp", json=init, headers={"Accept": "application/json, text/event-stream",
                                           "Authorization": f"Bearer {world['sym-bot']}", H: "symphony:999"})
    assert r.status_code == 200, r.text


def test_over_http_the_refusals_are_403(world):
    c = world["client"]
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bot", "version": "1"}}}
    hdr = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {world['sym-bot']}"}
    for claim in (None, "symphony:999", "github:1", "{bad"):
        r = c.post("/mcp", json=init, headers=hdr | ({H: claim} if claim else {}))
        assert r.status_code == 403, (claim, r.text)
    r = c.post("/mcp", json=init, headers={**hdr, "Authorization": "Bearer t0ken", H: "symphony:101"})
    assert r.status_code == 403 and "needs a relay token" in r.json()["error"]
    # REST never accepts a relay token, with or without a claim
    for path in ("/api/tokens", "/api/jobs", "/api/me"):
        assert c.get(path, headers={"Authorization": f"Bearer {world['sym-bot']}", H: "symphony:103"}
                     ).status_code in (401, 403), path


def test_approving_through_the_bot(world):
    c, h = world["client"], world["h"]

    def held(person: str | None, requester: str) -> str:
        return h.jobs.submit(SubmitJob(instructions="x", environment="plain", deliver="none", repo=REPO),
                             Caller(requester, requester, person=person), source="mcp",
                             approval=new_approval(h.jobs, requester, "budget: daily cap is spent")).id

    def approve(token: str, claim: str, job: str) -> tuple[bool, str]:
        res = _call(c, _session(c, token, claim), "approve_job", {"job": job})
        return not res.get("isError"), res["content"][0]["text"]

    bobs = held("bob", "@bob:cc")
    ok, why = approve(world["sym-bot"], "symphony:103", bobs)  # relay-only: can't approve
    assert not ok and "relay_approve" in why and h.jobs.get(bobs).state == JobState.AWAITING_APPROVAL
    ok, why = approve(world["sym-approver"], "symphony:101", bobs)  # alice: a member, not an approver
    assert not ok and h.jobs.get(bobs).state == JobState.AWAITING_APPROVAL
    adas = held("ada", "@ada:relay:sym-approver")
    ok, why = approve(world["sym-approver"], "symphony:103", adas)  # ada's own
    assert not ok and "nobody approves their own" in why
    ok, _ = approve(world["sym-approver"], "symphony:103", bobs)  # ada, someone else's: yes, as ada via the bot
    assert ok
    ap = h.jobs.get(bobs).approval
    assert (ap.decided_by, ap.decided_person, ap.rule) == ("@ada:relay:sym-approver", "ada", "admin")
    refused = [e for e in h.store.audit_tail(50) if e.action == "job.approve.refused"]
    assert {e.target for e in refused} == {bobs, adas}


# ---------------------------------------------------------------------------- helpers
class _current:
    def __init__(self, p):
        self.p = p

    def __enter__(self):
        self.tok = identity.current.set(self.p)

    def __exit__(self, *exc):
        identity.current.reset(self.tok)
