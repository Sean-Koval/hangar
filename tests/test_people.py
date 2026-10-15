"""People: tokens, credentials and work belong to a person; members see and act on their own (identity; P10, B5).

The visibility tests are product P10's metric: person A's token gets "not found" on person B's sandbox, exec and
session. On main every MCP token was root over everyone's work.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest
from fastapi.testclient import TestClient
from mcp import Client

import hangar.identity as identity
from hangar import people
from hangar.auth import Principal
from hangar.broker import CredentialError
from hangar.mcp_server import build_mcp
from hangar.models import CredentialKind, Person
from hangar.sessions import NewSession
from tests import test_exchange as xchg
from tests.test_identity import _call, _mcp_session
from tests.test_mcp_modern import sc

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

world, svc, jobs = xchg.world, xchg.svc, xchg.jobs

ALICE = Principal("alice-laptop", "tok-a", ["mcp"], person="alice", role="member")
BOB = Principal("bob-laptop", "tok-b", ["mcp"], person="bob", role="member")
ADMIN = Principal("ops", "tok-o", ["mcp"], person="owner", role="admin")


class As:
    """Calls made inside run as `p` (the MCP and REST auth paths set this per request)."""

    def __init__(self, p: Principal | None):
        self.p = p

    def __enter__(self):
        self.tok = identity.current.set(self.p)

    def __exit__(self, *exc):
        identity.current.reset(self.tok)


@pytest.fixture(autouse=True)
def _saved_envs_in_tmp(request, tmp_path):
    if "fleet" in request.fixturenames:
        request.getfixturevalue("fleet").catalog.save_dir = tmp_path / "saved-envs"


def err(res) -> str:
    assert res.is_error, res.content
    return res.content[0].text


# ====================================================================== visibility over MCP
async def test_a_member_sees_and_touches_only_their_own_sandboxes(fleet):
    async with Client(build_mcp(fleet)) as c:
        with As(ALICE):
            a = sc(await c.call_tool("create_sandbox", {"environment": "plain", "name": "alice-box",
                                                        "labels": {"team": "x"}}))
        with As(BOB):
            b = sc(await c.call_tool("create_sandbox", {"environment": "plain", "name": "bob-box",
                                                        "labels": {"team": "x"}}))
            assert [s["id"] for s in sc(await c.call_tool("list_sandboxes", {}))] == [b["id"]]
            for tool, args in (("get_sandbox", {}), ("exec", {"command": "id"}), ("read_file", {"path": "/etc/hosts"}),
                               ("list_files", {}), ("terminal_list", {}), ("snapshot_sandbox", {}),
                               ("destroy_sandbox", {}), ("process", {"action": "list"})):
                for ref in (a["id"], a["name"]):
                    assert f"no sandbox '{ref}" in err(await c.call_tool(tool, {"sandbox": ref, **args})), tool
            assert "no sandbox" in err(await c.call_tool("fork_sandbox", {"source": a["id"]}))
            # a label reaches only bob's own
            assert sc(await c.call_tool("destroy_sandbox", {"label": "team=x"})) == [b["id"]]
        assert fleet.get(a["id"]).state.value == "running" and fleet.get(a["id"]).person == "alice"
        with As(ADMIN):  # admins see and act on everyone's
            listed = {s["id"] for s in sc(await c.call_tool("list_sandboxes", {"include_stopped": True}))}
            assert {a["id"], b["id"]} <= listed
            assert sc(await c.call_tool("exec", {"sandbox": a["id"], "command": "echo hi"}))["stdout"] == "hi\n"
        with As(ALICE):
            snap = sc(await c.call_tool("snapshot_sandbox", {"sandbox": a["id"], "kind": "workspace"}))
            assert snap["person"] == "alice"
        with As(BOB):
            assert sc(await c.call_tool("list_snapshots", {})) == []
            assert "no sandbox" in err(await c.call_tool("fork_sandbox", {"source": snap["id"]}))


async def test_legacy_records_are_the_admins(fleet):
    old = await fleet.create("plain", name="from-before")  # no person: made before people existed
    async with Client(build_mcp(fleet)) as c:
        with As(ALICE):
            assert sc(await c.call_tool("list_sandboxes", {})) == []
            assert "no sandbox" in err(await c.call_tool("exec", {"sandbox": old.id, "command": "id"}))
        with As(ADMIN):
            assert [s["id"] for s in sc(await c.call_tool("list_sandboxes", {}))] == [old.id]


async def test_sessions_and_jobs_are_their_persons(fleet, svc, jobs, world):
    async with Client(build_mcp(fleet, jobs, svc)) as c:
        with As(ALICE):
            s = sc(await c.call_tool("new_session", {"name": "alice-work", "environment": "xenv",
                                                     "repo": world["url"]}))
            j = sc(await c.call_tool("submit_job", {"instructions": "x", "environment": "xenv",
                                                    "repo": world["url"], "deliver": "none"}))
        assert svc.get("alice-work").person == "alice" and jobs.get(j["id"]).person == "alice"
        with As(BOB):
            for tool, args in (("get_session", {"session": "alice-work"}), ("suspend_session", {"session": s["id"]}),
                               ("send_to_session", {"session": "alice-work", "message": "hi"}),
                               ("get_session_patch", {"ref": "alice-work"}),
                               ("steer_turn", {"ref": "alice-work", "message": "stop"})):
                assert "no session" in err(await c.call_tool(tool, args)), tool
            for tool, args in (("get_job", {"job": j["id"]}), ("cancel_job", {"job": j["id"]}),
                               ("get_events", {"job": j["id"]}), ("wait_for_turn", {"ref": j["id"]}),
                               ("follow_up_job", {"job": j["id"], "message": "more"})):
                assert "no job" in err(await c.call_tool(tool, args)), tool
            assert sc(await c.call_tool("list_sessions", {})) == []
            assert sc(await c.call_tool("list_jobs", {})) == []
            for uri in ("hangar://sessions/alice-work", "hangar://sessions/alice-work/events",
                        "hangar://sessions/alice-work/patch", f"hangar://jobs/{j['id']}"):
                with pytest.raises(Exception, match="Error creating resource"):  # the SDK hides why
                    await c.read_resource(uri)
        with As(ALICE):
            names = {x["name"] for x in sc(await c.call_tool("list_sessions", {}))}
            assert "alice-work" in names and names <= {"alice-work", f"job-{j['id'].removeprefix('job-')}"}
            assert sc(await c.call_tool("get_job", {"job": j["id"]}))["id"] == j["id"]
            await c.read_resource("hangar://sessions/alice-work")
        # the job's own session and sandbox are alice's too, once it starts
        for _ in range(200):
            if (job := jobs.get(j["id"])).session_id and job.sandbox_id:
                break
            await asyncio.sleep(0.05)
        assert svc.get(job.session_id).person == "alice"
        assert fleet.get(job.sandbox_id).person == "alice"


async def test_a_member_changes_only_their_own_environments(fleet):
    async with Client(build_mcp(fleet)) as c:
        with As(ALICE):
            sc(await c.call_tool("create_environment", {"spec": {"name": "alice-env", "extends": "plain"}}))
            sc(await c.call_tool("create_environment", {"spec": {"name": "alice-env", "extends": "plain",
                                                                 "description": "v2"}}))
        with As(BOB):
            assert "isn't yours" in err(await c.call_tool("create_environment", {"spec": {
                "name": "alice-env", "extends": "plain", "mcp": {"x": {"url": "https://evil.example/mcp"}}}}))
            assert "isn't yours" in err(await c.call_tool("create_environment", {"spec": {"name": "plain"}}))
        with As(ADMIN):
            sc(await c.call_tool("create_environment", {"spec": {"name": "alice-env", "extends": "plain"}}))


# ====================================================================== per-person credentials
def test_credentials_for_picks_the_persons_own(store, broker):
    broker.put("claude-team", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-team-0123456789")
    broker.put("claude-alice", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-alice-0123456789",
               person="alice")
    broker.put("codex-bob", "codex", CredentialKind.API_KEY, "sk-bob-0123456789", person="bob")
    broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_0123456789abcdef")
    agent = {"claude-code", "codex"}.__contains__
    env = ["claude-team", "codex-bob", "gh"]
    assert people.credentials_for(store, env, "alice", "claude-code", False, agent) == ["gh", "claude-alice"]
    assert people.credentials_for(store, env, "carol", "claude-code", False, agent) == ["claude-team", "gh"]
    assert people.credentials_for(store, env, None, "claude-code", False, agent) == ["claude-team", "gh"]
    assert people.credentials_for(store, env, "bob", "codex", False, agent) == ["claude-team", "codex-bob", "gh"]
    with pytest.raises(CredentialError, match="alice's own"):
        people.credentials_for(store, ["claude-alice"], "bob", "claude-code", True, agent)


async def test_a_persons_sandbox_gets_their_own_login(fleet, broker, monkeypatch):
    async def no_clone(prov, sb, spec, work_branch, stage=None):
        sb.info["repo_path"] = "/workspace/proj"

    monkeypatch.setattr(fleet, "_setup_repo", no_clone)
    broker.put("claude-team", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-team-0123456789")
    broker.put("claude-alice", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-alice-0123456789",
               person="alice")
    from tests.conftest import ENVS

    fleet.catalog._raw["cc"] = ({"name": "cc", "extends": "agent", "credentials": ["claude-team"]}, ENVS)

    async def token(person: str | None) -> str:
        sb = await fleet.create("cc", harness="claude-code", person=person)
        return (await fleet.read_file(sb.id, "/home/agent/.hangar/env")).decode()

    assert "alice-0123456789" in await token("alice")
    assert "team-0123456789" in await token("bob")
    with pytest.raises(Exception, match="alice's own"):
        await fleet.create("cc", harness="claude-code", person="bob", credentials=["claude-alice"])


# ====================================================================== tokens, people and migration over REST
@pytest.fixture
def app_client(settings):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.public_url = "http://localhost:8787"
    with TestClient(create_app(settings), base_url="http://localhost:8787") as c:
        yield c


ADMIN_H = {"Authorization": "Bearer t0ken"}


def test_existing_tokens_belong_to_the_owner(app_client):
    c = app_client
    legacy = c.post("/api/tokens", json={"name": "old-laptop", "scopes": ["mcp"]}, headers=ADMIN_H).json()
    h = c.app.state.hangar
    assert h.auth.principal("t0ken").person == "owner" and h.auth.principal("t0ken").role == "admin"
    assert h.auth.principal(legacy["token"]).person == "owner"
    assert legacy["person"] == "owner"  # minted by the owner's admin token
    tok = h.store.get(type(h.auth.list()[0]), legacy["id"])
    tok.person = None  # as a token minted before people existed
    h.store.put(tok)
    assert h.auth.principal(legacy["token"]).person == "owner"
    assert [p["id"] for p in c.get("/api/people", headers=ADMIN_H).json()] == ["owner"]
    assert c.get("/api/me", headers=ADMIN_H).json()["person"] == "owner"


def test_people_and_their_tokens(app_client):
    c = app_client
    r = c.post("/api/people", json={"id": "alice", "name": "Alice"}, headers=ADMIN_H).json()
    assert r["role"] == "member" and r["login"]["scopes"] == ["user"] and r["login"]["person"] == "alice"
    alice = {"Authorization": f"Bearer {r['login']['token']}"}
    assert c.get("/api/me", headers=alice).json() == {"actor": "ui:login-alice", "person": "alice", "role": "member"}
    # `hangar connect` works for her: an mcp token of her own
    mcp_tok = c.post("/api/tokens", json={"name": "mcp-cc", "scopes": ["mcp"]}, headers=alice).json()
    assert mcp_tok["person"] == "alice"
    assert c.get("/api/connect", headers=alice).status_code == 200
    # never more than a member's scopes, never for someone else, never someone else's
    assert c.post("/api/tokens", json={"name": "x", "scopes": ["admin"]}, headers=alice).status_code == 400
    assert c.post("/api/tokens", json={"name": "x", "scopes": ["mcp"], "person": "owner"},
                  headers=alice).status_code == 403
    assert {t["id"] for t in c.get("/api/tokens", headers=alice).json()} == {r["login"]["id"], mcp_tok["id"]}
    owner_tok = c.post("/api/tokens", json={"name": "ci", "scopes": ["jobs"]}, headers=ADMIN_H).json()
    assert c.delete(f"/api/tokens/{owner_tok['id']}", headers=alice).status_code == 404
    # admin-only routes stay admin-only
    for method, path in (("GET", "/api/people"), ("GET", "/api/sandboxes"), ("GET", "/api/audit"),
                         ("POST", "/api/credentials/x/release")):
        assert c.request(method, path, headers=alice).status_code == 403, path
    # an admin may mint for her, within her role
    assert c.post("/api/tokens", json={"name": "y", "scopes": ["metrics"], "person": "alice"},
                  headers=ADMIN_H).status_code == 400
    # demoted, an admin's admin token stops being one; removed, a person's tokens stop working
    bob = c.post("/api/people", json={"id": "bob", "role": "admin"}, headers=ADMIN_H).json()["login"]
    bob_h = {"Authorization": f"Bearer {bob['token']}"}
    assert c.get("/api/people", headers=bob_h).status_code == 200
    c.patch("/api/people/bob", json={"role": "member"}, headers=ADMIN_H)
    assert c.get("/api/people", headers=bob_h).status_code == 403
    c.delete("/api/people/alice", headers=ADMIN_H)
    assert c.get("/api/me", headers=alice).status_code == 401
    assert c.delete("/api/people/owner", headers=ADMIN_H).status_code == 400


def test_members_store_only_their_own_credentials(app_client):
    c = app_client
    login = c.post("/api/people", json={"id": "alice"}, headers=ADMIN_H).json()["login"]
    alice = {"Authorization": f"Bearer {login['token']}"}
    shared = {"name": "claude-team", "harness": "claude-code", "kind": "api_key", "value": "sk-ant-api03-team-x"}
    assert c.post("/api/credentials", json={**shared, "team": True}, headers=ADMIN_H).json()["owner"] == "team"
    mine = {"name": "claude-alice", "harness": "claude-code", "kind": "oauth_token", "value": "sk-ant-oat01-alice"}
    assert c.post("/api/credentials", json=mine, headers=alice).json()["person"] == "alice"
    assert c.post("/api/credentials", json={**shared, "value": "mine now"}, headers=alice).status_code == 403
    assert c.post("/api/credentials", json={**mine, "name": "hook", "harness": "webhook", "kind": "generic"},
                  headers=alice).status_code == 403
    assert {x["name"] for x in c.get("/api/credentials", headers=alice).json()} == {"claude-team", "claude-alice"}
    assert c.delete("/api/credentials/claude-team", headers=alice).status_code == 403
    assert c.delete("/api/credentials/claude-alice", headers=alice).status_code == 200
    # an admin hands a credential to someone, and back to the team
    assert c.put("/api/credentials/claude-team/owner", json={"person": "alice"}, headers=ADMIN_H).json()["person"] \
        == "alice"
    assert c.put("/api/credentials/claude-team/owner", json={"person": None}, headers=ADMIN_H).json()["person"] is None
    # --- teamcreds: but never someone's own subscription login
    c.post("/api/credentials", json=mine, headers=alice)
    r = c.put("/api/credentials/claude-alice/owner", json={"person": None}, headers=ADMIN_H)
    assert r.status_code == 403 and "can't be a team credential" in r.json()["error"]


def test_a_members_mcp_token_is_scoped_over_http(app_client):
    """The whole path: alice's `hangar connect` token at /mcp sees her sandboxes only."""
    c = app_client
    h = c.app.state.hangar
    from tests.conftest import FakeProvider

    h.fleet.providers["docker"] = FakeProvider()
    alice_login = c.post("/api/people", json={"id": "alice"}, headers=ADMIN_H).json()["login"]
    alice_mcp = c.post("/api/tokens", json={"name": "mcp-cc", "scopes": ["mcp"]},
                       headers={"Authorization": f"Bearer {alice_login['token']}"}).json()["token"]
    ses = _mcp_session(c, "t0ken")
    theirs = _call(c, ses, "create_sandbox", {"environment": "plain", "name": "owners"})["structuredContent"]
    ses = _mcp_session(c, alice_mcp)
    mine = _call(c, ses, "create_sandbox", {"environment": "plain", "name": "alices"})["structuredContent"]
    listed = _call(c, ses, "list_sandboxes", {})["structuredContent"]["result"]
    assert [s["id"] for s in listed] == [mine["id"]]
    res = _call(c, ses, "exec", {"sandbox": theirs["id"], "command": "id"})
    assert res["isError"] and "no sandbox" in res["content"][0]["text"]


def test_a_member_approves_an_oauth_app_as_themselves(app_client):
    from tests import test_oauth

    c = app_client
    login = c.post("/api/people", json={"id": "alice"}, headers=ADMIN_H).json()["login"]
    reg = test_oauth.register(c)
    verifier, challenge = test_oauth.pkce()
    ok = test_oauth.authorize(c, reg, challenge, token=login["token"])
    from urllib.parse import parse_qs, urlparse

    code = parse_qs(urlparse(ok.headers["location"]).query)["code"][0]
    tok = c.post("/token", data={"grant_type": "authorization_code", "code": code,
                                 "redirect_uri": "https://chatgpt.com/cb", "client_id": reg["client_id"],
                                 "code_verifier": verifier}).json()
    p = asyncio.run(c.app.state.oauth.principal(tok["access_token"]))
    assert p.person == "alice" and p.role == "member" and not p.admin
    assert test_oauth._mcp(c, tok["access_token"]) == 200


def test_person_ids(store):
    for bad in ("Alice", "", "a" * 41, "-x", "a b"):
        with pytest.raises(people.PeopleError):
            people.add(store, bad)
    people.add(store, "alice")
    with pytest.raises(people.PeopleError, match="already exists"):
        people.add(store, "alice")
    with pytest.raises(people.PeopleError, match="already exists"):
        people.add(store, "owner")
    assert isinstance(store.get(Person, "alice"), Person)


async def test_member_sessions_named_by_another_person_answer_not_found(fleet, svc, world):
    s = svc.new(NewSession(name="bobs", environment="xenv", repo=world["url"]), owner="b", requester="b",
                person="bob")
    async with Client(build_mcp(fleet, None, svc)) as c:
        with As(ALICE):
            assert "no session" in err(await c.call_tool("open_session", {"session": s.id}))
            assert "no session" in err(await c.call_tool("fork_session", {"session": "bobs"}))


def test_teammates_actor_names_cannot_collide():
    """Task handles, background processes and stream caps are keyed by the MCP actor; two people may well name
    their tokens alike (`hangar connect` labels by client and host)."""
    same = "mcp-claude-code-laptop-abc123"
    a = identity.actor_name(ALICE._replace(name=same))
    b = identity.actor_name(BOB._replace(name=same))
    assert a != b and a == f"@alice:{same}"
    assert identity.actor_name(ADMIN._replace(name=same)) == same  # the owner's stay as they were


def test_person_cli(monkeypatch):
    from typer.testing import CliRunner

    from hangar import cli

    calls = []

    def fake_call(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        return {"id": "alice", "role": "member", "login": {"id": "tok-1", "scopes": ["user"], "token": "hgr_x"}}

    monkeypatch.setattr(cli, "_call", fake_call)
    res = CliRunner().invoke(cli.app, ["person", "add", "alice", "--name", "Alice"])
    assert res.exit_code == 0, res.output
    assert calls == [("POST", "/api/people", {"id": "alice", "name": "Alice", "role": "member", "token": True})]
    assert "hgr_x" in res.output and "hangar connect" in res.output


# ====================================================================== review (#88)
async def test_secrets_never_map_another_persons_credential(fleet, broker):
    """An environment's `secrets:` went around credential ownership: once an admin mapped their own key into an
    environment, any member could start a sandbox from it and read the key there."""
    broker.put("openai-alice", "pi", CredentialKind.API_KEY, "sk-alice-0123456789abcdef", person="alice")
    fleet.catalog._raw["alices"] = ({"name": "alices", "extends": "plain", "secrets": {"K": "openai-alice"}}, None)
    for person in ("bob", None):  # a teammate; a GitHub- or Slack-triggered job's sandbox
        with pytest.raises(CredentialError, match="alice's own"):
            await fleet.create("alices", person=person)
    assert all(sb.environment != "alices" or sb.state.value == "error" for sb in fleet.list(include_stopped=True))
    sb = await fleet.create("alices", person="alice")
    assert "sk-alice-0123456789abcdef" in (await fleet.read_file(sb.id, "/home/agent/.hangar/env")).decode()


def test_members_cannot_bind_a_signing_key_or_plant_a_control_plane_credential(app_client):
    """Minting tokens and storing credentials became member self-service. A `jobs` token bound to a webhook key
    signs its callbacks with it, to a URL the member picks; a credential stored under a name the settings
    reserve but that isn't stored yet becomes the webhook secret, Slack secret or GitHub token hangar uses."""
    from hangar.models import Credential

    c = app_client
    h = c.app.state.hangar
    h.broker.put("gh-webhook", "webhook", CredentialKind.GENERIC, "whsec-0123456789abcdef")
    login = c.post("/api/people", json={"id": "alice"}, headers=ADMIN_H).json()["login"]
    alice = {"Authorization": f"Bearer {login['token']}"}
    r = c.post("/api/tokens", json={"name": "ci", "scopes": ["jobs"], "callback_secret": "gh-webhook"}, headers=alice)
    assert r.status_code == 403, r.text
    assert c.post("/api/tokens", json={"name": "ci", "scopes": ["jobs"]}, headers=alice).status_code == 200
    for setting in ("slack_signing_secret_credential", "github_webhook_secret_credential", "gateway_credential"):
        setattr(h.settings, setting, "planted")
        r = c.post("/api/credentials", json={"name": "planted", "harness": "git", "kind": "git_token",
                                             "value": "attacker-chosen"}, headers=alice)
        assert r.status_code == 403 and h.store.get(Credential, "planted") is None, (setting, r.text)
        setattr(h.settings, setting, None)
    # admins still may
    assert c.post("/api/tokens", json={"name": "ci2", "scopes": ["jobs"], "callback_secret": "gh-webhook"},
                  headers=ADMIN_H).status_code == 200


def test_removing_someone_stops_their_schedules(store):
    """Their tokens stop at once; their schedules kept starting jobs as them, with their own credentials."""
    from hangar.models import ScheduledJob

    people.add(store, "alice")
    s = ScheduledJob(name="nightly", cron="0 3 * * *", template={"instructions": "x"}, owner="@alice:laptop",
                     person="alice")
    other = ScheduledJob(name="team", cron="0 4 * * *", template={"instructions": "y"})
    store.put(s)
    store.put(other)
    people.remove(store, "alice")
    got = store.get(ScheduledJob, s.id)
    assert not got.enabled and "alice" in (got.last_error or "")
    assert store.get(ScheduledJob, other.id).enabled


def test_adding_a_member_names_the_shared_credentials_they_will_get(app_client, monkeypatch):
    """Team credentials go into a new member's sandboxes, where they can read them. Adding someone says which
    (--- teamcreds: the owner's own logins from before people are no longer among them, test_teamcreds)."""
    c = app_client
    h = c.app.state.hangar
    h.broker.put("claude-max", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-owner-0123456789")
    h.broker.put("claude-carol", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-carol-0123456789",
                 person="owner")
    h.broker.put("gh-hook", "webhook", CredentialKind.GENERIC, "whsec-0123456789abcdef")
    r = c.post("/api/people", json={"id": "alice"}, headers=ADMIN_H).json()
    assert r["shared_credentials"] == ["claude-max"]
    assert "shared_credentials" not in c.post("/api/people", json={"id": "bob", "role": "admin"},
                                              headers=ADMIN_H).json()

    from typer.testing import CliRunner

    from hangar import cli

    monkeypatch.setattr(cli, "_call", lambda *a, **kw: {**r, "login": None})
    res = CliRunner().invoke(cli.app, ["person", "add", "alice"])
    assert res.exit_code == 0 and "claude-max" in res.output and "hangar cred owner" in res.output, res.output


async def test_the_rest_of_the_tool_surface_answers_not_found_to_a_teammate(fleet, svc, jobs, world):
    """The tools the visibility tests above leave out: schedules, approvals, artifacts, idle, the prompt, the
    artifact resource and sandbox watches."""
    from hangar.triggers import Triggers

    triggers = Triggers(jobs)
    async with Client(build_mcp(fleet, jobs, svc, triggers)) as c:
        with As(ALICE):
            sc(await c.call_tool("new_session", {"name": "alice-work", "environment": "xenv", "repo": world["url"]}))
            sch = sc(await c.call_tool("create_schedule", {"name": "alice-nightly", "cron": "0 3 * * *",
                                                           "instructions": "x", "environment": "xenv",
                                                           "repo": world["url"], "deliver": "none",
                                                           "enabled": False}))
            box = sc(await c.call_tool("create_sandbox", {"environment": "plain", "name": "alice-box"}))
        j = jobs.submit(xchg.SubmitJob(instructions="x", environment="xenv", repo=world["url"], deliver="none"),
                        xchg.Caller("a", "a", person="alice"))
        with As(BOB):
            assert sc(await c.call_tool("list_schedules", {})) == []
            for tool in ("delete_schedule", "run_schedule_now"):
                assert "no schedule" in err(await c.call_tool(tool, {"schedule": sch["id"]})), tool
                assert "no schedule" in err(await c.call_tool(tool, {"schedule": "alice-nightly"})), tool
            for tool in ("approve_job", "reject_job"):
                assert "no job" in err(await c.call_tool(tool, {"job": j.id})), tool
            for tool, args in (("keep_session", {"session": "alice-work", "idle_minutes": 600}),
                               ("get_events", {"session": "alice-work"}),
                               ("get_artifact", {"ref": "alice-work", "turn": 0, "path": "x"}),
                               ("answer_approval", {"ref": "alice-work", "approval": "a1", "decision": "allow"}),
                               ("open_session", {"session": "alice-work"})):
                assert "no session" in err(await c.call_tool(tool, args)), tool
            for tool, args in (("write_file", {"path": "/tmp/x", "content": "x"}), ("extend_sandbox", {"minutes": 5}),
                               ("pause_sandbox", {}), ("run_agent", {"prompt": "x"}),
                               ("agent_status", {"run_id": "r1"}), ("terminal_open", {"session": "t"}),
                               ("list_snapshots", {})):
                assert "no sandbox" in err(await c.call_tool(tool, {"sandbox": box["id"], **args})), tool
            with pytest.raises(Exception, match="Internal server error"):  # as for a missing one: the SDK hides why
                await c.get_prompt("pick_up_session", {"session": "alice-work"})
            with pytest.raises(Exception, match="Error creating resource"):
                await c.read_resource("hangar://sessions/alice-work/artifacts/0/x")
            assert all("alice" not in str(r.uri) for r in (await c.list_resources()).resources)
        assert triggers.get_schedule(sch["id"]).person == "alice" and fleet.get(box["id"]).state.value == "running"


def test_a_members_tokens_share_their_caps():
    """The per-person caps (#89, fairness.person_of) keyed on the actor string, one per token. A member mints
    their own tokens now, so each new one was a fresh set of caps."""
    from hangar import fairness

    laptop, desktop = identity.actor_name(ALICE), identity.actor_name(ALICE._replace(name="desktop"))
    oauth = identity.actor_name(ALICE._replace(name="oauth:claude-ai(login-alice)"))
    assert {fairness.person_of(f"mcp:{laptop}"), fairness.person_of(desktop), fairness.person_of(f"mcp:{oauth}")} \
        == {"alice"}
    assert fairness.person_of("mcp:laptop") == "laptop"  # the owner's, as before


async def test_caps_count_identitys_person_not_the_token(fleet, settings):
    """--- integration: the caps count the person identity records on the work (`owner` for the admin's tokens),
    so the owner's laptop, CI and desktop tokens share one set of caps too; the actor string is only the fallback
    for records from before people. Hangar's own actors stay exempt on a record that carries a person."""
    from hangar import fairness

    settings.person_max_sandboxes = 2
    await fleet.create("plain", actor="mcp:laptop", person="owner")
    await fleet.create("plain", actor="ui:ci", person="owner")
    with pytest.raises(fairness.CapReached, match="owner already has 2 sandboxes"):
        await fleet.create("plain", actor="desktop", person="owner")
    await fleet.create("plain", actor="@alice:laptop", person="alice")
    await fleet.create("plain", actor="session:ses-1", person="owner")  # the owner's session's sandbox
    assert fairness.person_of("laptop", "owner") == "owner"
    assert fairness.person_of("job:job-1", "alice") is None
    assert fairness.person_of("github:octocat") == "github:octocat"  # triggered jobs: no person yet
