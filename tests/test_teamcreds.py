"""Team credentials, the migration, linked identities and the doctor checks (the owner's decision 5; teamcreds.py).

The acceptance tests: the owner's Claude and Codex logins stored before people never reach a member's sandbox or a
job no person owns; those jobs run with the team's credential; a GitHub or Slack sender linked to a person is that
person's (caps, budget, visibility) and an unlinked one stays the team's and as trusted as before.
"""

from __future__ import annotations

import asyncio
import shutil
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import hangar.identity as identity
from hangar import fairness, jobbudget, people, teamcreds
from hangar.auth import Principal
from hangar.broker import Broker, CredentialError
from hangar.models import Credential, CredentialKind, JobState
from hangar.store import Store
from hangar.vault import Vault, load_or_create_key
from tests.conftest import ENVS
from tests.test_jobs import sessions  # noqa: F401 - fixture
from tests.test_slack import command, settle, slack, until_posted  # noqa: F401 - fixture
from tests.test_triggers import _issue, intake, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

ADMIN_H = {"Authorization": "Bearer t0ken"}
ALICE = Principal("alice-laptop", "tok-a", ["mcp"], person="alice", role="member")


def is_agent(h: str | None) -> bool:
    return h in ("claude-code", "codex")


class As:
    def __init__(self, p: Principal | None):
        self.p = p

    def __enter__(self):
        self.tok = identity.current.set(self.p)

    def __exit__(self, *exc):
        identity.current.reset(self.tok)


# ====================================================================== the migration
def _before_people(settings) -> Broker:
    """A data directory as a hangar from before people left it: every credential has no person."""
    store = Store(settings.db_path)
    b = Broker(store, Vault(store, load_or_create_key(settings.secret_key, settings.data_dir)))
    b.put("claude-sean", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-sean-0123456789abcdef")
    b.put("codex-sean", "codex", CredentialKind.AUTH_FILE, '{"tokens": {"refresh_token": "rt-sean-0123456789"}}')
    b.put("gh-pat", "git", CredentialKind.GIT_TOKEN, "ghp_sean0123456789abcdef")
    b.put("gh-hook", "webhook", CredentialKind.GENERIC, "whsec-0123456789abcdef")
    b.put("gh-control", "git", CredentialKind.GIT_TOKEN, "ghp_control0123456789")
    return b


@pytest.fixture
def upgraded(settings):
    from hangar.app import create_app

    _before_people(settings)
    settings.admin_token = "t0ken"
    settings.public_url = "http://localhost:8787"
    settings.github_token_credential = "gh-control"  # hangar's own GitHub token: control plane
    settings.github_webhook_secret_credential = "gh-hook"
    with TestClient(create_app(settings), base_url="http://localhost:8787") as c:
        yield c


def test_credentials_from_before_people_become_the_owners_once(upgraded, settings):
    c, h = upgraded, upgraded.app.state.hangar
    owners = {x.name: x.person for x in h.store.list(Credential)}
    assert owners == {"claude-sean": "owner", "codex-sean": "owner", "gh-pat": "owner", "gh-hook": None,
                      "gh-control": None}
    rec = c.get("/api/credentials/migration", headers=ADMIN_H).json()
    assert rec["state"] == "done" and rec["to"] == "owner"
    assert rec["moved"] == ["claude-sean", "codex-sean", "gh-pat"] and rec["control_plane"] == ["gh-control", "gh-hook"]
    assert any(e.action == "credential.migrate" for e in h.store.audit_tail(50))
    listed = {x["name"]: x["owner"] for x in c.get("/api/credentials", headers=ADMIN_H).json()}
    assert listed["claude-sean"] == "owner" and listed["gh-hook"] == "control-plane"
    # it ran once: a team credential added since stays the team's on the next start
    h.broker.put("claude-team", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-team-0123456789", team=True)
    assert teamcreds.migrate(h.store, settings)["moved"] == rec["moved"]
    from hangar.app import Hangar

    Hangar(settings)
    assert h.store.get(Credential, "claude-team").person is None
    # a member can't read the record
    login = c.post("/api/people", json={"id": "alice"}, headers=ADMIN_H).json()
    assert login["shared_credentials"] == ["claude-team"]  # not the owner's own logins
    alice = {"Authorization": f"Bearer {login['login']['token']}"}
    assert c.get("/api/credentials/migration", headers=alice).status_code == 403
    assert {x["name"] for x in c.get("/api/credentials", headers=alice).json()} >= {"claude-team"}
    assert "claude-sean" not in {x["name"] for x in c.get("/api/credentials", headers=alice).json()}


def test_a_crash_part_way_finishes_the_recorded_list(store, broker, settings):
    broker.put("a", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-a-0123456789")
    broker.put("b", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-b-0123456789")
    store.kv_set(teamcreds.MIGRATION_KEY, '{"state": "running", "to": "owner", "moved": ["a", "b"], '
                                          '"control_plane": []}')
    broker.put("a", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-a-0123456789", person="alice")  # reassigned
    broker.put("c", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-c-0123456789")  # after: not in the list
    rec = teamcreds.migrate(store, settings)
    assert rec["state"] == "done"
    assert {x.name: x.person for x in store.list(Credential)} == {"a": "alice", "b": "owner", "c": None}


async def test_the_owners_logins_never_reach_a_members_or_a_team_jobs_sandbox(fleet, broker, store, settings,
                                                                                monkeypatch):
    async def no_clone(prov, sb, spec, work_branch, stage=None):
        sb.info["repo_path"] = "/workspace/proj"

    monkeypatch.setattr(fleet, "_setup_repo", no_clone)
    broker.put("claude-sean", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-sean-0123456789abcdef")
    teamcreds.migrate(store, settings)
    fleet.catalog._raw["cc"] = ({"name": "cc", "extends": "agent", "credentials": ["claude-sean"]}, ENVS)

    async def env(person: str | None) -> str:
        sb = await fleet.create("cc", harness="claude-code", person=person)
        return (await fleet.read_file(sb.id, "/home/agent/.hangar/env") or b"").decode()

    assert "sean-0123456789" in await env("owner")
    for person in ("alice", None):  # a member; a GitHub or Slack sender linked to nobody, a webhook, a schedule
        assert "sean-0123456789" not in await env(person)
    with pytest.raises(Exception, match="owner's own"):
        await fleet.create("cc", harness="claude-code", person="alice", credentials=["claude-sean"])
    # the team's credential is what they get instead, though the environment lists the owner's
    broker.put("claude-team", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-team-0123456789", team=True)
    for person in ("alice", None):
        got = await env(person)
        assert "team-0123456789" in got and "sean-0123456789" not in got
    assert "sean-0123456789" in await env("owner")  # the owner keeps their own


# ====================================================================== which credentials a sandbox gets
def test_modes(store, broker):
    broker.put("claude-team", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-team-0123456789")
    broker.put("claude-alice", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-alice-0123456789",
               person="alice")
    broker.put("gh-owner", "git", CredentialKind.GIT_TOKEN, "ghp_owner0123456789", person="owner")
    broker.put("gh-team", "git", CredentialKind.GIT_TOKEN, "ghp_team0123456789")
    env = ["gh-owner"]

    def r(person, mode):
        return sorted(teamcreds.resolve(store, env, person, "claude-code", False, is_agent, mode).names)

    # person_then_team: hers, else the team's; a git token the environment lists is swapped the same way
    assert r("alice", "person_then_team") == ["claude-alice", "gh-team"]
    assert r("bob", "person_then_team") == ["claude-team", "gh-team"]
    assert r(None, "person_then_team") == ["claude-team", "gh-team"]
    assert r("owner", "person_then_team") == ["claude-team", "gh-owner"]
    assert r("alice", "team") == ["claude-team", "gh-team"]
    assert r("alice", "person") == ["claude-alice"]
    with pytest.raises(CredentialError, match="bob has no claude-code credential"):
        r("bob", "person")
    with pytest.raises(CredentialError, match="runs for no one: link its sender"):
        r(None, "person")
    # an environment's own pick among team credentials is kept
    broker.put("claude-team-2", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-team2-0123456789")
    assert teamcreds.resolve(store, ["claude-team-2"], None, "claude-code", False, is_agent).names == \
        ["claude-team-2"]


def test_the_repo_policy_says_whose_login_jobs_use(store):
    from hangar.repopolicy import RepoPolicies, RepoPolicy

    RepoPolicies(store).put(RepoPolicy(repo="acme/app", credentials="team"))
    assert teamcreds.mode_for(store, "https://github.com/Acme/App.git") == "team"
    assert teamcreds.mode_for(store, "acme/other") == "person_then_team"
    with pytest.raises(ValueError):
        RepoPolicy(repo="acme/app", credentials="everyone")


async def test_a_pool_of_codex_logins_and_a_queue_when_all_are_in_use(fleet, broker, settings, monkeypatch):
    """A ChatGPT login is leased to one sandbox. Two of a person's logins serve two of their sandboxes at once; a
    third job waits, saying which are busy, and starts when one is released. A direct create fails at once with the
    same words. (A pool is someone's own: a subscription login is never a team credential.)"""
    monkeypatch.setattr(teamcreds, "POLL_SECONDS", 0.05)
    fleet.catalog._raw["cx"] = ({"name": "cx", "extends": "plain", "harnesses": ["codex"]}, ENVS)
    for n in (1, 2):
        broker.put(f"codex-{n}", "codex", CredentialKind.AUTH_FILE,
                   f'{{"tokens": {{"refresh_token": "rt-{n}-0123456789"}}}}', person="alice")
    a = await fleet.create("cx", harness="codex", person="alice")
    b = await fleet.create("cx", harness="codex", person="alice")
    assert sorted([*a.credentials, *b.credentials]) == ["codex-1", "codex-2"]
    with pytest.raises(CredentialError, match=r"alice's codex logins codex-1, codex-2 \(all in use\)"):
        await fleet.create("cx", harness="codex", person="alice")
    said: list[str] = []
    waiting = asyncio.create_task(fleet.create("cx", harness="codex", person="alice", progress=said.append))
    for _ in range(200):
        if said:
            break
        await asyncio.sleep(0.02)
    assert said and said[0].startswith("waiting for a free login (queued): alice's codex logins")
    assert not waiting.done()
    await fleet.destroy(a.id)
    c = await asyncio.wait_for(waiting, 10)
    assert c.credentials == a.credentials and broker.get(a.credentials[0]).leased_to == c.id
    settings.login_wait_seconds = 0  # past the wait: it fails, saying why
    with pytest.raises(CredentialError, match="add another login to the pool, or use an API key"):
        await fleet.create("cx", harness="codex", person="alice", progress=said.append)


# ====================================================================== storing credentials (REST)
@pytest.fixture
def app_client(settings):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.public_url = "http://localhost:8787"
    with TestClient(create_app(settings), base_url="http://localhost:8787") as c:
        yield c


def test_an_admins_credential_is_their_own_unless_they_say_team(app_client):
    c = app_client
    key = {"harness": "claude-code", "kind": "api_key", "value": "sk-ant-api03-0123456789"}
    assert c.post("/api/credentials", json={"name": "mine", **key}, headers=ADMIN_H).json()["owner"] == "owner"
    r = c.post("/api/credentials", json={"name": "ours", **key, "team": True}, headers=ADMIN_H).json()
    assert r["owner"] == "team"
    # a subscription login is never a team credential (the owner's decision): not new, and not moved
    for login in ({"harness": "claude-code", "kind": "oauth_token", "value": "sk-ant-oat01-0123456789"},
                  {"harness": "codex", "kind": "auth_file", "value": '{"tokens": {"refresh_token": "rt-0123456789"}}'}):
        r = c.post("/api/credentials", json={"name": "ours-login", **login, "team": True}, headers=ADMIN_H)
        assert r.status_code == 403 and "can't be a team credential" in r.json()["error"], r.text
        assert "--kind api_key" in r.json()["error"]
        assert c.post("/api/credentials", json={"name": "ours-login", **login}, headers=ADMIN_H).json()["owner"] \
            == "owner"
        r = c.put("/api/credentials/ours-login/owner", json={"person": None}, headers=ADMIN_H)
        assert r.status_code == 403 and "can't be a team credential" in r.json()["error"]
        r = c.post("/api/credentials", json={"name": "ours-login", **login, "team": True}, headers=ADMIN_H)
        assert r.status_code == 403  # nor by storing over it with `team`
        assert {x["name"]: x["owner"] for x in c.get("/api/credentials", headers=ADMIN_H).json()}["ours-login"] \
            == "owner"
        c.delete("/api/credentials/ours-login", headers=ADMIN_H)
    assert c.post("/api/credentials", json={"name": "x", **key, "team": True, "person": "owner"},
                  headers=ADMIN_H).status_code == 400
    # control-plane kinds stay the control plane's
    r = c.post("/api/credentials", json={"name": "hook", "harness": "webhook", "kind": "generic",
                                         "value": "whsec-0123456789"}, headers=ADMIN_H).json()
    assert r["person"] is None and r["owner"] == "control-plane"
    login = c.post("/api/people", json={"id": "alice"}, headers=ADMIN_H).json()["login"]
    alice = {"Authorization": f"Bearer {login['token']}"}
    r = c.post("/api/credentials", json={"name": "a", **key, "team": True}, headers=alice)
    assert r.status_code == 403 and "added by an admin" in r.json()["error"]
    # an existing one keeps its owner; `team` moves it (an API key may become the team's)
    assert c.post("/api/credentials", json={"name": "mine", **key}, headers=ADMIN_H).json()["owner"] == "owner"
    assert c.post("/api/credentials", json={"name": "mine", **key, "team": True},
                  headers=ADMIN_H).json()["owner"] == "team"


def test_cli(monkeypatch):
    from typer.testing import CliRunner

    from hangar import cli

    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw.get("json"), kw.get("params")))
        if path == "/api/credentials":
            return {"name": "t", "harness": "claude-code", "kind": "api_key", "exclusive": False, "owner": "team"}
        if path == "/api/credentials/migration":
            return {"state": "done", "finished_at": 1, "to": "owner", "moved": ["claude-sean"],
                    "control_plane": ["gh-hook"]}
        return {"id": "alice", "links": ["github:alice-gh"], "name": "t", "person": None, "owner": "team"}

    monkeypatch.setattr(cli, "_call", fake)
    run = CliRunner().invoke
    res = run(cli.app, ["cred", "add", "t", "--harness", "claude-code", "--kind", "api_key", "--value",
                        "sk-ant-api03-0123456789abcdefghij", "--team"])
    assert res.exit_code == 0 and "claude-code/api_key, team" in res.output, res.output
    assert calls[-1][2]["team"] is True
    res = run(cli.app, ["cred", "migration"])
    assert "claude-sean" in res.output and "gh-hook" in res.output, res.output
    res = run(cli.app, ["cred", "owner", "t", "team"])
    assert calls[-1][2] == {"person": None} and "t: team" in res.output
    res = run(cli.app, ["person", "link", "alice", "github", "alice-gh"])
    assert res.exit_code == 0 and calls[-1][:3] == ("POST", "/api/people/alice/links",
                                                     {"source": "github", "id": "alice-gh"})
    res = run(cli.app, ["person", "unlink", "alice", "github", "alice-gh"])
    assert calls[-1][1] == "/api/people/alice/links" and calls[-1][3] == {"source": "github", "id": "alice-gh"}


# ====================================================================== linked identities
def test_linking_and_resolving(store):
    people.add(store, "alice")
    people.add(store, "bob")
    people.link(store, "alice", "github", "583231")  # a GitHub user id (the REST route looks up a login)
    people.link(store, "alice", "slack", "t0123abcd/u012ab3cd")
    people.link(store, "alice", "jira", "5b10ac8d82e05b22cc7d4ef5")
    people.link(store, "bob", "email", "Bob@Example.com")
    people.link(store, "owner", "github", "9919")  # the owner too, though they were never added
    r = people.resolve_external
    assert r(store, "github", "583231").id == "alice" and r(store, "github", 583231).id == "alice"
    assert r(store, "github_projects", 583231).id == "alice"  # a GitHub Projects item's actor
    assert r(store, "slack", "T0123ABCD/U012AB3CD").id == "alice"
    assert r(store, "jira", "5b10ac8d82e05b22cc7d4ef5").id == "alice"
    assert r(store, "jira", "bob@example.com").id == "bob"  # a Jira user known by email
    assert r(store, "github", "9919").id == "owner"
    for source, ext in (("github", "stranger"), ("github", "dependabot[bot]"), ("github", ""), ("linear", "x"),
                        ("slack", "not an id"), ("github", None), ("github", True), ("slack", "U012AB3CD"),
                        ("slack", "T9/U012AB3CD")):
        assert r(store, source, ext) is None
    with pytest.raises(people.PeopleError, match="linked to alice"):
        people.link(store, "bob", "github", "583231")
    with pytest.raises(people.PeopleError, match="isn't a GitHub user id"):
        people.link(store, "bob", "github", "alice-gh")  # logins are renamed and re-registered: ids only
    with pytest.raises(people.PeopleError, match="Slack workspace and user id"):
        people.link(store, "bob", "slack", "U012AB3CD")  # unique only within a workspace
    with pytest.raises(KeyError):
        people.link(store, "carol", "github", "1")
    people.unlink(store, "alice", "github", "583231")
    assert r(store, "github", "583231") is None
    people.remove(store, "bob")
    assert r(store, "email", "bob@example.com") is None


def test_linking_over_rest_is_for_admins(app_client):
    c = app_client
    login = c.post("/api/people", json={"id": "alice"}, headers=ADMIN_H).json()["login"]
    alice = {"Authorization": f"Bearer {login['token']}"}
    r = c.post("/api/people/alice/links", json={"source": "github", "id": "583231"}, headers=ADMIN_H)
    assert r.json()["links"] == ["github:583231"]
    assert c.post("/api/people/alice/links", json={"source": "github", "id": "9919"},
                  headers=alice).status_code == 403  # else a member could claim someone else's jobs
    assert c.post("/api/people/alice/links", json={"source": "linear", "id": "x"}, headers=ADMIN_H).status_code == 400
    # a login needs a lookup, and this hangar has no GitHub token: it says how to link by id instead
    r = c.post("/api/people/alice/links", json={"source": "github", "id": "alice-gh"}, headers=ADMIN_H)
    assert r.status_code == 400 and "api.github.com/users/alice-gh" in r.text
    assert c.get("/api/people", headers=ADMIN_H).json()[1]["links"] == ["github:583231"]
    r = c.delete("/api/people/alice/links", params={"source": "github", "id": "583231"}, headers=ADMIN_H)
    assert r.json()["links"] == []
    assert c.delete("/api/people/alice/links", params={"source": "github", "id": "583231"},
                    headers=ADMIN_H).status_code == 404


async def test_github_jobs_run_for_the_linked_person(intake, settings):  # noqa: F811
    """A linked sender's job is theirs: their caps, their daily budget, their lists, and (review) their trust: a
    linked account acts as its person, who could start the job over MCP anyway. An unlinked one runs for the team,
    trusted as before."""
    from hangar.jobs import handle_github

    people.add(intake.store, "alice")
    people.add(intake.store, "bob")
    people.link(intake.store, "alice", "github", "101")  # sean: trusted (a collaborator on the fake)
    people.link(intake.store, "bob", "github", "102")  # rando: not trusted by GitHub's rule
    labeled = {"action": "labeled", "label": {"name": "hangar"}, "repository": {"full_name": "o/r"}}
    res = await handle_github(intake, "issues", "d-1", {**labeled, "issue": _issue(1),
                                                        "sender": {"login": "sean", "id": 101}})
    job = intake.get(res["job"])
    assert job.person == "alice" and job.state == JobState.QUEUED and job.requester == "github:sean"
    assert fairness.person_of(job.requester, job.person) == "alice"
    settings.person_max_usd_per_day = 50
    assert [c.name for c in jobbudget.caps(intake, job.repo, job.person)] == ["alice's daily cap"]
    with As(ALICE):
        assert people.mine([job]) == [job]
    res = await handle_github(intake, "issues", "d-2", {**labeled, "issue": _issue(2),
                                                        "sender": {"login": "rando", "id": 102}})
    bobs = intake.get(res["job"])
    assert bobs.person == "bob" and bobs.state == JobState.QUEUED  # linked: acts as bob, no approval
    # whoever registers a login a linked person renamed away from is not that person: links are by user id
    res = await handle_github(intake, "issues", "d-2b", {**labeled, "issue": _issue(4),
                                                         "sender": {"login": "rando", "id": 666}})
    squatter = intake.get(res["job"])
    assert squatter.person is None and squatter.state == JobState.AWAITING_APPROVAL
    people.unlink(intake.store, "bob", "github", "102")
    res = await handle_github(intake, "issues", "d-3", {**labeled, "issue": _issue(3),
                                                        "sender": {"login": "rando", "id": 102}})
    team = intake.get(res["job"])
    assert team.person is None and team.state == JobState.AWAITING_APPROVAL
    assert fairness.person_of(team.requester, team.person) == "github:rando"
    with As(ALICE):
        assert people.mine([team]) == []


async def test_slack_jobs_run_for_the_linked_person(slack):  # noqa: F811
    svc, fake = slack["svc"], slack["fake"]
    people.add(svc.store, "alice")
    people.add(svc.store, "bob")
    people.link(svc.store, "alice", "slack", "T1/U1")
    people.link(svc.store, "bob", "slack", "T2/U2")  # --- review: U2 of another workspace is not T1's U2
    await command(slack["client"], "add fix.txt")
    await command(slack["client"], "add fix.txt", user="U2")
    await until_posted(fake, lambda: sum("Queued as hangar job" in p["text"] for p in fake.posts) == 2)
    jobs = {j.requester: j for j in svc.list()}
    assert jobs["slack:T1/U1"].person == "alice" and jobs["slack:T1/U2"].person is None
    for j in jobs.values():
        await settle(svc, j.id)


async def test_only_a_jobs_person_continues_it_from_a_front_door(intake):  # noqa: F811
    """Review: a person's job runs with their own login. A trusted collaborator who is someone else (bob, or a
    collaborator linked to nobody) must not steer it, by a PR comment or by answering its question: their text would
    run with alice's Claude login. Alice herself may; an admin uses hangar itself, since in direct mode an agent can
    comment as the account behind its git token."""
    from hangar.jobs import handle_github

    for p in ("alice", "bob"):
        people.add(intake.store, p)
    people.link(intake.store, "alice", "github", "101")
    people.link(intake.store, "bob", "github", "102")
    people.link(intake.store, "owner", "github", "103")
    labeled = {"action": "labeled", "label": {"name": "hangar"}, "repository": {"full_name": "o/r"}}
    res = await handle_github(intake, "issues", "d-1", {**labeled, "issue": _issue(7),
                                                        "sender": {"login": "alice-gh", "id": 101}})
    job = intake.get(res["job"])
    assert job.person == "alice"

    def comment(on: dict, login: str, uid: int | None, body: str = "/hangar push it", assoc: str = "COLLABORATOR"):
        return {"action": "created", "repository": {"full_name": "o/r"}, "issue": on,
                "comment": {"body": body, "author_association": assoc,
                            "user": {"login": login, **({"id": uid} if uid else {})}}}

    # the question on the issue: bob's answer and an unlinked collaborator's are refused, with the reason
    job.state = JobState.NEEDS_INPUT
    for login, uid in (("bob-gh", 102), ("collab", None)):
        res = await handle_github(intake, "issue_comment", f"q-{login}", comment(_issue(7), login, uid, "/hangar yes"))
        assert "alice's job" in res["ignored"], res
    # the PR the job opened: likewise
    job.state, job.result.pr_number = JobState.SUCCEEDED, 9
    pr = {**_issue(9), "pull_request": {}}
    for login, uid in (("bob-gh", 102), ("collab", None), ("sean", 103)):
        res = await handle_github(intake, "issue_comment", f"pr-{login}", comment(pr, login, uid))
        assert "alice's job" in res["ignored"], res
    assert len(job.turns) == 1
    # alice gets past it (this fake job has no sandbox to continue in)
    res = await handle_github(intake, "issue_comment", "pr-ok", comment(pr, "alice-gh", 101))
    assert "no sandbox or checkpoint" in res["ignored"], res
    # over MCP and REST the Guard decides (owner scoping), as before; admins continue anyone's there
    with pytest.raises(Exception, match="no sandbox or checkpoint"):
        intake.follow_up(job.id, "go on", requester="admin")


async def test_link_id_looks_up_a_github_login():
    import httpx

    from hangar.github import GitHub

    def gh(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/alice-gh":
            return httpx.Response(200, json={"login": "Alice-GH", "id": 583231, "type": "User"})
        if request.url.path == "/users/acme":
            return httpx.Response(200, json={"login": "acme", "id": 5, "type": "Organization"})
        return httpx.Response(404, json={"message": "Not Found"})

    jobs = SimpleNamespace(_github=lambda: GitHub("ghp_x", transport=httpx.MockTransport(gh)))
    h = SimpleNamespace(jobs=jobs)
    assert await teamcreds.link_id(h, "github", "@alice-gh") == ("583231", "Alice-GH")
    assert await teamcreds.link_id(h, "github_projects", "583231") == ("583231", None)
    assert await teamcreds.link_id(h, "slack", "T1/U1") == ("T1/U1", None)
    for bad, why in (("nobody", "Not Found"), ("acme", "no user id"), ("no spaces", "isn't a GitHub login")):
        with pytest.raises(people.PeopleError, match=why):
            await teamcreds.link_id(h, "github", bad)
    with pytest.raises(people.PeopleError, match="api.github.com/users/alice-gh"):
        await teamcreds.link_id(SimpleNamespace(jobs=SimpleNamespace(_github=lambda: None)), "github", "alice-gh")


async def test_a_linked_slack_user_acts_as_their_person(slack, settings):  # noqa: F811
    """Review: past HANGAR_SLACK_USERS too (they could start the job over MCP), keyed by their own workspace."""
    from hangar.slack import SlackFrontDoor

    door = SlackFrontDoor(slack["svc"])
    settings.slack_users = ["U9"]
    people.add(slack["svc"].store, "carol")
    people.link(slack["svc"].store, "carol", "slack", "T7/U5")
    assert door._refusal("T1", "C1", "U5") is not None  # T1's U5 is someone else
    assert door._refusal("T1", "C1", "U5", user_team="T7") is None  # carol, from her own workspace (Slack Connect)
    assert door._refusal("T7", "C1", "U5") is None


# ====================================================================== hangar doctor
def _h(svc, settings):
    return SimpleNamespace(settings=settings, store=svc.store, jobs=svc, fleet=svc.fleet)


async def test_doctor(intake, settings, broker):  # noqa: F811
    from hangar.jobs import handle_github

    h = _h(intake, settings)
    teamcreds.migrate(intake.store, settings)
    checks = {c["name"]: c for c in teamcreds.doctor_checks(h)}
    assert checks["personal credentials shared"]["status"] == "ok"
    # o/r maps to jobenv (the scripted harness): no team credential for it
    missing = checks["team credentials for jobs"]
    assert missing["status"] == "warn" and "jobs on o/r from GitHub and Slack: jobenv (scripted)" in missing["detail"]
    assert "--team" in missing["fix"]
    broker.put("scripted-team", "scripted", CredentialKind.API_KEY, "sk-scripted-0123456789", env_var="SCRIPTED_KEY")
    broker.put("claude-max", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-max-0123456789")
    checks = {c["name"]: c for c in teamcreds.doctor_checks(h)}
    assert checks["team credentials for jobs"]["status"] == "ok"
    shared = checks["personal credentials shared"]  # one stored around the API's refusal fails the doctor
    assert shared["status"] == "fail" and "claude-max" in shared["detail"]
    assert "can't be a team credential" in shared["fix"]
    # repos whose policy says `person` warn about unlinked senders instead
    from hangar.repopolicy import RepoPolicy

    intake.policies.put(RepoPolicy(repo="o/r", credentials="person"))
    broker.delete("scripted-team")
    assert "credentials: person" in {c["name"]: c for c in teamcreds.doctor_checks(h)}[
        "team credentials for jobs"]["detail"]
    # senders who start jobs often but are linked to nobody
    for n in range(3):
        await handle_github(intake, "issue_comment", f"d-{n}", {
            "action": "created", "repository": {"full_name": "o/r"}, "issue": _issue(40 + n),
            "comment": {"body": "/hangar", "author_association": "NONE", "user": {"login": "rando"}}})
    unlinked = {c["name"]: c for c in teamcreds.doctor_checks(h)}["unlinked senders"]
    assert unlinked["status"] == "warn" and "github:rando (3 jobs)" in unlinked["detail"]
    assert unlinked["fix"] == "hangar person link PERSON github rando"
    people.add(intake.store, "randy")
    people.link(intake.store, "randy", "github", "103")
    for n in range(3):  # jobs from now on are randy's; the old ones stay the team's until they age out
        await handle_github(intake, "issue_comment", f"e-{n}", {
            "action": "created", "repository": {"full_name": "o/r"}, "issue": _issue(50 + n),
            "comment": {"body": "/hangar", "author_association": "NONE", "user": {"login": "rando", "id": 103}}})
    later = [j for j in intake.list() if j.labels.get("github.issue", "").startswith("5")]
    assert len(later) == 3 and {j.person for j in later} == {"randy"}
