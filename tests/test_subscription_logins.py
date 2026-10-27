"""Subscription logins are interactive-only (#125; docs/team.md#subscription-logins).

Jobs (every front door), schedules, webhooks, experiment trials and graders never get an `oauth_token` or
`auth_file` login; a person's own sessions and sandboxes still may, with a warning. HANGAR_SUBSCRIPTION_LOGINS=any,
which let jobs use them for the safe release only, was removed (KAN-74): it is refused wherever it is set.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from types import SimpleNamespace

import pytest

from hangar import manifest, people, teamcreds
from hangar.broker import CredentialError
from hangar.experiments import EXP_LABEL, GRADER_LABEL
from hangar.fleet import FleetError
from hangar.jobs import Caller, JobError, SubmitJob, handle_github
from hangar.models import CredentialKind, JobState, ScheduledJob
from hangar.sessions import NewSession
from tests.conftest import ENVS
from tests.test_intake import DANA, door, fixture, post, sign, until  # noqa: F401 - fixtures
from tests.test_jobs import DONE, _until, jobs, sessions, world  # noqa: F401 - fixtures
from tests.test_slack import command, slack, until_posted  # noqa: F401 - fixtures
from tests.test_triggers import _issue, intake  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

REFUSED = ("job needs an API-key credential for {h}; subscription logins are interactive-only "
           "(docs/team.md#subscription-logins)")


def is_agent(h: str | None) -> bool:
    return h in ("claude-code", "codex", "scripted")


def _setup_token(broker, name: str, person: str | None, harness: str = "claude-code", **kw):
    """What `claude setup-token | hangar cred add NAME --kind oauth_token` stores."""
    return broker.put(name, harness, CredentialKind.OAUTH_TOKEN, f"sk-ant-oat01-{name}-0123456789abcdef",
                      person=person, **kw)


# ====================================================================== the resolver
def test_automated_resolutions_never_get_a_subscription_login(store, broker, settings):
    _setup_token(broker, "claude-alice", "alice")
    broker.put("codex-alice", "codex", CredentialKind.AUTH_FILE, '{"tokens": {"refresh_token": "rt-0123456789"}}',
               person="alice")

    def r(person, harness="claude-code", purpose="automated", names=(), explicit=False, strict=False):
        return teamcreds.resolve(store, list(names), person, harness, explicit, is_agent, settings=settings,
                                 purpose=purpose, strict=strict)

    # interactive: hers, with the warning
    got = r("alice", purpose="interactive")
    assert got.names == ["claude-alice"] and got.warnings == [teamcreds.INTERACTIVE_WARNING]
    # automated: refused, saying what to add
    for harness in ("claude-code", "codex"):
        with pytest.raises(teamcreds.SubscriptionRefused) as e:
            r("alice", harness)
        assert str(e.value).startswith(REFUSED.format(h=harness)) and "--kind api_key" in str(e.value)
    assert isinstance(e.value, CredentialError)  # what jobs and the MCP tools already report
    # named outright: refused the same way, though it's hers
    with pytest.raises(teamcreds.SubscriptionRefused, match="'claude-alice' is a subscription login: job needs"):
        r("alice", names=["claude-alice"], explicit=True)
    # with an API key of hers or the team's, that is used instead
    broker.put("claude-team", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-team-0123456789", team=True)
    assert r("alice").names == ["claude-team"] and r("alice").warnings == []
    broker.put("claude-alice-api", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-alice-0123456789",
               person="alice")
    assert r("alice").names == ["claude-alice-api"]
    assert r("alice", names=["claude-alice"]).names == ["claude-alice-api"]  # an environment's list: dropped
    # a person with no login at all is not this refusal (the team-credentials rules apply as before)
    broker.delete("claude-team")
    assert r("bob").names == []
    # trials and graders never do either
    broker.delete("claude-alice-api")
    with pytest.raises(teamcreds.SubscriptionRefused):
        r("alice", strict=True)
    # people.credentials_for passes the purpose through
    with pytest.raises(teamcreds.SubscriptionRefused):
        people.credentials_for(store, [], "alice", "claude-code", False, is_agent, purpose="automated",
                               settings=settings)
    assert people.credentials_for(store, [], "alice", "claude-code", False, is_agent) == ["claude-alice"]


def test_a_sandboxs_purpose():
    assert teamcreds.purpose_of(None, None) == ("interactive", False)
    assert teamcreds.purpose_of(None, {"hangar.job": "job-1"}) == ("automated", False)  # attempts, forks of one
    # --- KAN-74: a job's sandbox is automated even when its caller says interactive
    assert teamcreds.purpose_of("interactive", {"hangar.job": "job-1"}) == ("automated", False)
    assert teamcreds.purpose_of(None, {EXP_LABEL: "job-1"}) == ("automated", True)
    assert teamcreds.purpose_of("interactive", {GRADER_LABEL: "job-1"}) == ("automated", True)
    assert teamcreds.purpose_of(None, {teamcreds.PURPOSE_LABEL: "automated"}) == ("automated", False)  # replays
    with pytest.raises(ValueError):
        teamcreds.purpose_of("nightly", None)


# ====================================================================== sandboxes
async def test_trials_and_graders_never_receive_one_whatever_the_environment_lists(fleet, broker, settings,
                                                                                   monkeypatch):
    async def no_clone(prov, sb, spec, work_branch, stage=None):
        sb.info["repo_path"] = "/workspace/proj"

    monkeypatch.setattr(fleet, "_setup_repo", no_clone)
    _setup_token(broker, "claude-alice", "alice")
    fleet.catalog._raw["variant"] = ({"name": "variant", "extends": "agent", "credentials": ["claude-alice"]}, ENVS)
    for labels in ({EXP_LABEL: "job-x"}, {GRADER_LABEL: "job-x"}):
        with pytest.raises(teamcreds.SubscriptionRefused):
            await fleet.create("variant", harness="claude-code", person="alice", labels=labels)
        with pytest.raises(teamcreds.SubscriptionRefused):
            await fleet.create("variant", harness="claude-code", person="alice", labels=labels,
                               credentials=["claude-alice"])
    with pytest.raises(teamcreds.SubscriptionRefused, match="interactive-only"):  # a trial's submit
        teamcreds.check_job(fleet, "variant", "claude-code", "alice", None, strict=True)
    broker.put("claude-team", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-team-0123456789", team=True)
    sb = await fleet.create("variant", harness="claude-code", person="alice", labels={EXP_LABEL: "job-x"})
    env = (await fleet.read_file(sb.id, "/home/agent/.hangar/env") or b"").decode()
    assert sb.credentials == ["claude-team"] and "claude-alice" not in env and "team-0123456789" in env
    assert "credential_warnings" not in sb.info


async def test_a_persons_own_sandbox_warns_and_a_jobs_does_not_take_it(fleet, broker, settings, monkeypatch):
    async def no_clone(prov, sb, spec, work_branch, stage=None):
        sb.info["repo_path"] = "/workspace/proj"

    monkeypatch.setattr(fleet, "_setup_repo", no_clone)
    _setup_token(broker, "claude-alice", "alice")
    fleet.catalog._raw["cc"] = ({"name": "cc", "extends": "agent"}, ENVS)
    sb = await fleet.create("cc", harness="claude-code", person="alice")  # create_sandbox, by her
    assert sb.credentials == ["claude-alice"]
    assert sb.info["credential_warnings"] == [teamcreds.INTERACTIVE_WARNING]
    assert teamcreds.INTERACTIVE_WARNING in sb.info["warnings"]
    assert any(e.action == "sandbox.subscription_login" for e in fleet.store.audit_tail(20))
    with pytest.raises(teamcreds.SubscriptionRefused):  # a job's sandbox (and so its forks and attempts)
        await fleet.create("cc", harness="claude-code", person="alice", labels={"hangar.job": "job-1"})
    with pytest.raises(FleetError, match="interactive-only"):  # a replay or fork sandbox (part B)
        await fleet.fork(sb.id, 1, labels={teamcreds.PURPOSE_LABEL: "automated"})
    with pytest.raises(teamcreds.SubscriptionRefused):  # --- KAN-74: naming `interactive` doesn't get it either
        await fleet.create("cc", harness="claude-code", person="alice", labels={"hangar.job": "job-1"},
                           purpose="interactive")


# ====================================================================== front doors
async def test_a_github_job_for_someone_with_only_a_setup_token_fails_on_the_issue(intake, broker):  # noqa: F811
    people.add(intake.store, "alice")
    people.link(intake.store, "alice", "github", "101")
    _setup_token(broker, "scripted-alice", "alice", harness="scripted", env_var="SCRIPTED_KEY")
    labeled = {"action": "labeled", "label": {"name": "hangar"}, "repository": {"full_name": "o/r"}}
    res = await handle_github(intake, "issues", "d-1", {**labeled, "issue": _issue(1),
                                                        "sender": {"login": "sean", "id": 101}})
    assert "error" in res and REFUSED.format(h="scripted") in res["error"], res
    assert not intake.list()  # nothing was queued, so nothing provisions
    await asyncio.sleep(0.05)
    said = [c for c in intake.hooks.comments() if "couldn't start a job" in c]
    assert said and REFUSED.format(h="scripted") in said[0] and "--kind api_key" in said[0]
    # with an API key of hers, the same issue runs
    broker.put("scripted-alice-api", "scripted", CredentialKind.API_KEY, "sk-0123456789", person="alice",
               env_var="SCRIPTED_KEY")
    res = await handle_github(intake, "issues", "d-2", {**labeled, "issue": _issue(1),
                                                        "sender": {"login": "sean", "id": 101}})
    assert intake.get(res["job"]).state == JobState.QUEUED


async def test_a_slack_job_for_someone_with_only_a_setup_token_fails_in_the_thread(slack, broker):  # noqa: F811
    svc, fake = slack["svc"], slack["fake"]
    people.add(svc.store, "alice")
    people.link(svc.store, "alice", "slack", "T1/U1")
    _setup_token(broker, "scripted-alice", "alice", harness="scripted", env_var="SCRIPTED_KEY")
    await command(slack["client"], "add fix.txt")
    await until_posted(fake, lambda: any("couldn't start a job" in p["text"] for p in fake.posts))
    said = next(p["text"] for p in fake.posts if "couldn't start a job" in p["text"])
    assert "API-key credential for scripted" in said and "interactive-only" in said
    assert not svc.list()


async def test_a_jira_job_for_someone_with_only_a_setup_token_fails_on_the_ticket(door, fleet, broker):  # noqa: F811
    from tests.test_intake import configure

    svc, fake, client = door["svc"], door["fake"], door["client"]
    fleet.catalog._raw["cc"] = ({"name": "cc", "extends": "agent"}, ENVS)
    configure(svc, "https://github.com/acme/api.git", "cc")
    _setup_token(broker, "claude-dana", "dana")
    payload = fixture("jira_issue_updated.json")
    r = await post(client, payload, {**sign(json.dumps(payload).encode()), "X-Atlassian-Webhook-Identifier": "x"})
    assert REFUSED.format(h="claude-code") in r.json().get("error", ""), r.json()
    await until(lambda: fake.jira_comments(), what="the refusal on the ticket")
    assert REFUSED.format(h="claude-code") in fake.jira_comments()[0]


async def test_api_and_schedule_jobs_are_refused_at_submit(intake, broker):  # noqa: F811
    _setup_token(broker, "scripted-alice", "alice", harness="scripted", env_var="SCRIPTED_KEY")
    for source in ("api", "mcp", "schedule"):
        with pytest.raises(JobError, match="interactive-only"):
            intake.submit(SubmitJob(instructions="x", repo="https://github.com/o/r", environment="jobenv"),
                          Caller("alice", "alice", person="alice"), source=source)


def test_the_codex_refusal_names_the_openai_key_to_add():
    """--- KAN-74: a Codex job on a ChatGPT login says what to store instead, as a command."""
    msg = teamcreds.refusal("codex")
    assert msg.startswith(REFUSED.format(h="codex"))
    assert ("add an OpenAI API key: hangar cred add codex-api --harness codex --kind api_key --file openai-key.txt"
            in msg)


def test_any_is_refused_wherever_it_is_set(monkeypatch, tmp_path, store):
    """--- KAN-74: HANGAR_SUBSCRIPTION_LOGINS=any is gone. In the environment hangar doesn't start; `hangar config set`
    refuses it; stored before the upgrade it isn't applied and the doctor fails on it."""
    from pydantic import ValidationError

    from hangar.config import Settings
    from hangar.settings_store import SettingError, SettingsStore

    monkeypatch.setenv("HANGAR_SUBSCRIPTION_LOGINS", "any")
    with pytest.raises(ValidationError) as e:
        Settings(data_dir=tmp_path)
    assert "HANGAR_SUBSCRIPTION_LOGINS=any was removed" in str(e.value) and "--kind api_key" in str(e.value)
    monkeypatch.delenv("HANGAR_SUBSCRIPTION_LOGINS")
    s = Settings(data_dir=tmp_path)
    cfg = SettingsStore(store, s)
    with pytest.raises(SettingError, match="HANGAR_SUBSCRIPTION_LOGINS=any was removed"):
        cfg.set("HANGAR_SUBSCRIPTION_LOGINS", "any")
    store.kv_set("setting:subscription_logins", json.dumps("any"))  # stored by v0.2.0
    cfg.apply()
    assert s.subscription_logins == "interactive"
    h = SimpleNamespace(settings=s, store=store, config=cfg)
    got = teamcreds._subscription_check(h, [])
    assert got["status"] == "fail" and "hangar cred add codex-api --harness codex --kind api_key" in got["detail"]
    assert got["fix"] == "hangar config unset HANGAR_SUBSCRIPTION_LOGINS"


# ====================================================================== sessions
async def test_her_session_works_and_warns(sessions, fleet, world, broker):  # noqa: F811
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    _setup_token(broker, "scripted-alice", "alice", harness="scripted", env_var="SCRIPTED_KEY")
    s = sessions.new(NewSession(name="mine", environment="jobenv", repo=world["url"], message="hello"),
                     owner="alice", requester="alice", person="alice")
    for _ in range(400):
        s = sessions.get("mine")
        if s.turns and s.turns[0].manifest and not sessions.busy(s.id):
            break
        await asyncio.sleep(0.05)
    # the turn ran on her login (the scripted agent is a job's; outside a job its exit code says nothing)
    assert s.turns[0].run_id and s.turns[0].exit_code is not None, (s.state, s.reason)
    assert fleet.get(s.sandbox_id).credentials == ["scripted-alice"]
    view = sessions.view(s)
    assert view["warnings"] == [teamcreds.INTERACTIVE_WARNING]  # get_session
    assert any(e["data"].get("warning") == "subscription_login" for e in sessions.events("mine"))
    assert teamcreds.INTERACTIVE_WARNING in manifest.for_session(sessions, s, 1)["warnings"]


# ====================================================================== hangar doctor
def test_doctor(intake, settings, broker):  # noqa: F811
    h = SimpleNamespace(settings=settings, store=intake.store, jobs=intake, fleet=intake.fleet)

    def check() -> dict:
        return {c["name"]: c for c in teamcreds.doctor_checks(h)}["subscription logins"]

    assert check()["status"] == "ok"
    # o/r's jobs (jobenv, scripted) would find only a subscription login stored as the team's
    _setup_token(broker, "scripted-shared", None, harness="scripted", env_var="SCRIPTED_KEY")
    got = check()
    assert got["status"] == "warn" and "jobs on o/r from GitHub and Slack: jobenv (scripted)" in got["detail"]
    broker.delete("scripted-shared")
    # a person's schedule, and a person with only subscription logins
    _setup_token(broker, "scripted-alice", "alice", harness="scripted", env_var="SCRIPTED_KEY")
    intake.store.put(ScheduledJob(name="nightly", cron="0 3 * * *", owner="alice", person="alice",
                                  template={"instructions": "x", "environment": "jobenv"}))
    got = check()
    assert got["status"] == "warn" and "schedule nightly: jobenv (scripted)" in got["detail"]
    assert "alice (scripted: scripted-alice)" in got["detail"] and "--kind api_key" in got["fix"]
    broker.put("scripted-alice-api", "scripted", CredentialKind.API_KEY, "sk-0123456789", person="alice",
               env_var="SCRIPTED_KEY")
    assert check()["status"] == "ok"
