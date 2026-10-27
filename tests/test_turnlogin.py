"""A turn whose harness gets no login is refused before it starts (#293; turnlogin.py).

On the first live GKE qualification the acceptance `steer` session belonged to no person (the deploy's admin token),
so no personal login reached it, and there was no team credential for claude-code: Claude Code ended the turn in
seconds with "Not logged in", recorded as the agent's exit 1. hangar now refuses that turn up front, says what to
add, and records it as `infra_error` / `no_login`; GET /api/credentials/login answers the same question beforehand.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hangar import turnlogin
from hangar.harnesses import HARNESSES, CredentialSlot
from hangar.jobs import Caller, SubmitJob
from hangar.models import CredentialKind, JobState
from hangar.sessions import NewSession
from tests.test_exchange import jobs, settle, svc, world  # noqa: F401 - fixtures
from tests.test_jobs import _until

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


def _needs_key(monkeypatch) -> None:
    """The scripted harness logs in with $XAGENT_KEY, like claude-code with $ANTHROPIC_API_KEY."""
    h = HARNESSES["xagent"]
    monkeypatch.setitem(HARNESSES, "xagent", h.model_copy(update={"credentials": [
        CredentialSlot(kind=CredentialKind.API_KEY, env_var="XAGENT_KEY")]}))


# ---------------------------------------------------------------------------- the probe
def _probe(home: Path, harness: str = "claude-code") -> int:
    return subprocess.run(["bash", "-c", turnlogin.probe_command(harness, str(home))], env={"PATH": "/usr/bin:/bin"},
                          capture_output=True).returncode


def test_the_probe_finds_a_login_variable_or_file(tmp_path):
    assert _probe(tmp_path) == 3
    (tmp_path / ".hangar").mkdir()
    (tmp_path / ".hangar/env").write_text("FOO=bar\n")
    assert _probe(tmp_path) == 3
    (tmp_path / ".hangar/env").write_text("ANTHROPIC_API_KEY=sk-ant-x\n")
    assert _probe(tmp_path) == 0
    (tmp_path / ".hangar/env").write_text("CLAUDE_CODE_USE_VERTEX=1\n")  # a cloud provider is a login too
    assert _probe(tmp_path) == 0
    (tmp_path / ".hangar/env").write_text("")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/.credentials.json").write_text("{}")  # an interactive /login
    assert _probe(tmp_path) == 0
    assert _probe(tmp_path, "codex") == 3
    assert not turnlogin.needs_login("git") and not turnlogin.needs_login(None)


# ---------------------------------------------------------------------------- sessions and jobs
async def test_a_turn_with_no_login_is_refused_as_configuration(svc, world, fleet, monkeypatch):  # noqa: F811
    _needs_key(monkeypatch)
    svc.new(NewSession(name="nolog", environment="xenv", repo=world["url"], message="hello"), owner="admin",
            requester="sean")
    s = await settle(svc, "nolog")
    t = s.turns[0]
    assert t.run_id is None, "the harness must not have started"
    assert t.outcome == "error" and t.outcome_class == "infra_error" and t.infra_reason == "no_login"
    assert "no login for xagent" in t.outcome_reason and "--team --harness xagent --kind api_key" in t.outcome_reason
    assert "start the session as a person" in t.outcome_reason  # it belongs to no one
    assert s.state.value == "idle" and "no login for xagent" in (s.reason or "")
    assert any(e.action == "session.turn_refused" for e in fleet.store.audit_tail(50))
    # with a login in the turn's environment, the next turn runs as the agent's
    box = fleet.providers["docker"].boxes[fleet.get(s.sandbox_id).provider_ref]
    with (box / "home/agent/.hangar/env").open("a") as f:
        f.write("XAGENT_KEY=k\n")
    svc.send("nolog", "again", requester="sean")
    s = await settle(svc, "nolog")
    t = s.turns[1]
    assert t.run_id and t.outcome == "exited" and t.outcome_class == "agent", (t.outcome, t.outcome_reason)


async def test_a_credential_for_the_harness_skips_the_probe(svc, world, fleet, broker, monkeypatch):  # noqa: F811
    _needs_key(monkeypatch)
    broker.put("xkey", "xagent", CredentialKind.API_KEY, "xk-0123456789abcdef", team=True)
    (world["envs"] / "xenv.yaml").write_text("name: xenv\nharnesses: [xagent]\ncredentials: [xkey]\n")
    fleet.catalog.reload()
    calls = []
    real = fleet.exec

    async def exec_(ref, command, *a, **kw):
        calls.append(command)
        return await real(ref, command, *a, **kw)

    monkeypatch.setattr(fleet, "exec", exec_)
    svc.new(NewSession(name="haskey", environment="xenv", repo=world["url"], message="hello"), owner="admin",
            requester="sean")
    s = await settle(svc, "haskey")
    assert s.turns[0].outcome_class == "agent" and s.turns[0].run_id
    assert not [c for c in calls if "exit 3" in c]


async def test_a_job_with_no_login_fails_as_configuration(jobs, world, monkeypatch):  # noqa: F811
    _needs_key(monkeypatch)
    job = jobs.submit(SubmitJob(instructions="hello", repo=world["url"], environment="xenv", deliver="none"),
                      Caller("admin", "admin"))
    job = await _until(jobs, job.id, {JobState.FAILED, JobState.SUCCEEDED})
    while job.id in jobs._active:
        await asyncio.sleep(0.05)
    job = jobs.get(job.id)
    assert job.state == JobState.FAILED
    assert job.result.outcome_class == "infra_error" and job.result.infra_reason == "no_login"
    assert any("no login for xagent" in e.message for e in job.events)


# ---------------------------------------------------------------------------- GET /api/credentials/login
ADMIN_H = {"Authorization": "Bearer t0ken"}


def test_the_login_question_answers_before_a_session_starts(settings):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    with TestClient(create_app(settings), base_url="http://localhost:8787") as c:
        h = c.app.state.hangar

        def ask(**params):
            r = c.get("/api/credentials/login", params={"environment": "agent", **params}, headers=ADMIN_H)
            assert r.status_code == 200, r.text
            return r.json()

        none = ask()
        assert none["harness"] == "claude-code" and none["login"] is False
        assert "hangar cred add NAME --team --harness claude-code --kind api_key" in none["fix"]
        c.post("/api/people", json={"id": "sean", "role": "admin", "token": False}, headers=ADMIN_H)
        h.broker.put("claude-sean", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-sean-0123456789abcdef",
                     person="sean")
        assert ask(person="sean")["login"] is True and ask(person="sean")["credentials"] == ["claude-sean"]
        assert ask()["login"] is False  # a personal login never reaches work no person owns
        assert ask(person="sean", purpose="automated")["login"] is False  # nor a job (#125)
        h.broker.put("claude-team", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-team-0123456789", team=True)
        team = ask()
        assert team["login"] is True and team["credentials"] == ["claude-team"] and team["fix"] is None
        assert ask(harness="codex")["login"] is False
        assert c.get("/api/credentials/login", params={"environment": "nope"}, headers=ADMIN_H).status_code == 404
        assert c.get("/api/credentials/login", params={"environment": "agent"}).status_code == 401


# ---------------------------------------------------------------------------- --- KAN-178: a login the vendor refused
async def test_a_job_whose_login_the_vendor_refused_fails_as_a_credential_to_renew(jobs, world, broker,  # noqa: F811
                                                                                     monkeypatch):
    """job-7339173d: codex couldn't refresh its ChatGPT login and failed the turn with a 401, recorded as the
    agent's failure while the credential's health said ok."""
    from hangar import credhealth
    from hangar.models import Credential

    _needs_key(monkeypatch)
    script = world["root"] / "refused.sh"
    script.write_text(
        "#!/bin/bash\n"
        "echo 'ERROR codex_login::auth::manager: Failed to refresh token: Your access token could not be refreshed."
        " Please log out and sign in again.' >&2\n"
        "echo '{\"type\": \"turn.failed\", \"error\": {\"message\": \"workspace routing discovery unauthorized "
        "(401)\"}}'\n"
        "exit 1\n")
    monkeypatch.setitem(HARNESSES, "xagent", HARNESSES["xagent"].model_copy(update={"run": f"bash {script}"}))
    broker.put("xlogin", "xagent", CredentialKind.API_KEY, "xk-0123456789abcdef", team=True)
    (world["envs"] / "xenv.yaml").write_text("name: xenv\nharnesses: [xagent]\ncredentials: [xlogin]\n")
    jobs.fleet.catalog.reload()
    job = jobs.submit(SubmitJob(instructions="hello", repo=world["url"], environment="xenv", deliver="none"),
                      Caller("admin", "admin"))
    job = await _until(jobs, job.id, {JobState.FAILED, JobState.SUCCEEDED})
    while job.id in jobs._active:
        await asyncio.sleep(0.05)
    job = jobs.get(job.id)
    assert job.state == JobState.FAILED and job.reason == "login_rejected", [e.message for e in job.events]
    assert job.result.outcome_class == "infra_error" and job.result.infra_reason == "login_rejected"
    assert any(e.message.startswith(credhealth.REFUSED_PREFIXES) and "xlogin needs renewing" in e.message
               for e in job.events)
    c = jobs.store.get(Credential, "xlogin")
    assert c.health["status"] == "fail" and c.health["method"] == credhealth.REFUSED_METHOD
