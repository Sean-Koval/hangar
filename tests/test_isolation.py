"""The uid split (workstream isolation; hangar/isolation.py): when a sandbox can run split, what hangar runs there and
as whom, and what happens where it can't. The real thing (Docker, the agent trying to reach the driver) is
tests/test_e2e_isolation_docker.py; the mechanism was also checked under gVisor (docs/steering.md)."""

from __future__ import annotations

import argparse
import shlex
from types import SimpleNamespace

import pytest

from hangar import driver, isolation
from hangar import steer as steering
from hangar.config import Settings
from hangar.harnesses import HARNESSES
from hangar.models import ExecResult, Sandbox, SessionTurn
from tests.test_steer import live, new, settle, svc  # noqa: F401 - fixtures

DROP = ["--inh-caps=-all", "--ambient-caps=-all"]


class Prov:
    def __init__(self, name="docker", out="split 999 998", code=0, boom=False):
        self.name, self.out, self.code, self.boom = name, out, code, boom
        self.calls: list[tuple[str, str | None]] = []

    async def exec(self, sb, command, timeout=120, cwd=None, user=None):
        self.calls.append((command, user))
        if self.boom:
            raise RuntimeError("no such container")
        return ExecResult(exit_code=self.code, stdout=self.out, stderr="", duration_ms=1)


def fleet(settings: Settings):
    puts: list[str] = []
    return SimpleNamespace(settings=settings, store=SimpleNamespace(put=lambda sb: puts.append(sb.id)), puts=puts)


async def test_a_docker_sandbox_is_set_up_once_as_root_and_remembered():
    f, p, sb = fleet(Settings()), Prov(), Sandbox(name="s", environment="e", provider="docker")
    assert await isolation.ensure(f, p, sb) is True
    assert sb.info["isolation"] == {"mode": "split", "uid": 999, "gid": 998} and f.puts == [sb.id]
    (script, user), = p.calls
    assert user == "root" and "useradd --system" in script and "sudo -n -l" in script
    assert await isolation.ensure(f, p, sb) is True and len(p.calls) == 1  # remembered


@pytest.mark.parametrize("prov,settings,why", [
    (Prov(name="kubernetes"), Settings(), "GKE Sandbox's policy"),
    (Prov(name="e2b"), Settings(), "the e2b provider doesn't support it yet"),
    (Prov(out="agent can use sudo (the environment has sudo: true)", code=3), Settings(), "agent can use sudo"),
    (Prov(out="the image has no setpriv", code=3), Settings(), "the image has no setpriv"),
    (Prov(out="split nope 1"), Settings(), "split nope 1"),  # what the sandbox printed doesn't parse: not split
    (Prov(boom=True), Settings(), "setting it up failed: no such container"),
])
async def test_sandboxes_that_cant_be_split_stay_single_user_and_say_why(prov, settings, why):
    sb = Sandbox(name="s", environment="e", provider=prov.name)
    assert await isolation.ensure(fleet(settings), prov, sb) is False
    assert sb.info["isolation"]["mode"] == "shared" and why in sb.info["isolation"]["why"]


async def test_off_applies_at_once():
    p, sb, settings = Prov(), Sandbox(name="s", environment="e", provider="docker"), Settings()
    assert await isolation.ensure(fleet(settings), p, sb) is True
    settings.uid_split = "off"
    assert await isolation.ensure(fleet(settings), p, sb) is False and len(p.calls) == 1
    assert isolation.why_not(settings, sb) == "HANGAR_UID_SPLIT is off"


def test_the_harness_starts_as_the_agent_with_no_capabilities():
    argv = driver.as_agent("agent", "/home/agent", "/var/lib/hangar/runs/r.env", "/workspace/x", ["claude", "-p"])
    assert argv[:7] == ["setpriv", "--reuid=agent", "--regid=agent", "--init-groups", *DROP, "--"]
    assert argv[7:11] == ["env", "HOME=/home/agent", "USER=agent", "LOGNAME=agent"]
    assert argv[-4:] == ["/var/lib/hangar/runs/r.env", "/workspace/x", "claude", "-p"]
    # The one-shot command in the turn's script is the same, as shell.
    parts = shlex.split(isolation.agent_command(Settings(), "run-0000beef", "/workspace/x", "pi -p hi"))
    assert parts[:7] == argv[:7]
    assert parts[-5:] == ["/var/lib/hangar/runs/run-0000beef.env", "/workspace/x", "bash", "-c", "pi -p hi"]
    assert isolation.driver_args(Settings(), "run-0000beef", "/w") == \
        "--as agent --run-env /var/lib/hangar/runs/run-0000beef.env --cwd /w"


def test_the_driver_launches_through_setpriv_under_as(monkeypatch):
    seen = {}

    class Popen:
        def __init__(self, argv, **kw):
            seen["argv"] = argv

    monkeypatch.setattr(driver.subprocess, "Popen", Popen)
    monkeypatch.setattr(driver.pwd, "getpwnam", lambda u: SimpleNamespace(pw_dir=f"/home/{u}"))
    args = argparse.Namespace(approvals="auto", approval_timeout=60, as_user="agent", run_env="/r.env", cwd="/w")
    driver.Harness(driver.Driver(args, driver.Out(), None), ["claude"])
    assert seen["argv"][0] == "setpriv" and seen["argv"][-3:] == ["/r.env", "/w", "claude"]
    assert driver.Driver(args, driver.Out(), None).cwd() == "/w"
    driver.Harness(driver.Driver(argparse.Namespace(approvals="auto", approval_timeout=60), driver.Out(), None),
                   ["claude"])
    assert seen["argv"] == ["claude"]  # single-user: as before


async def test_a_split_turn_gets_its_key_and_driver_as_hangars_user(svc, live, monkeypatch):  # noqa: F811
    new(svc, live, "sp", "hello")
    s = await settle(svc, "sp")
    sb = svc.fleet.get(s.sandbox_id)
    puts: dict[str, dict] = {}

    async def ensure(fleet, prov, sbx):
        sbx.info["isolation"] = {"mode": "split", "uid": 999, "gid": 999}
        return True

    async def put(prov, sbx, parent, files):
        puts.setdefault(parent, {}).update(files)

    monkeypatch.setattr(isolation, "ensure", ensure)
    monkeypatch.setattr(isolation, "put", put)
    turn = SessionTurn(message="x")
    run_id, command = await steering.prepare(svc, s, turn, sb, HARNESSES["live-claude"])
    ctl = f"{run_id}.ctl"
    assert puts[isolation.RUNS][ctl] == (None, 0o700) and puts[isolation.RUNS][f"{ctl}/key"][1] == 0o600
    assert puts[isolation.HOME]["driver.py"] == (steering.DRIVER.read_bytes(), 0o644)
    assert f"python3 -I {isolation.DRIVER} " in command and f"--ctl {isolation.RUNS}/{ctl} " in command
    assert f"--as agent --run-env {isolation.RUNS}/{run_id}.env --cwd {sb.info['repo_path']} -- " in command
    assert not turn.warnings
    steering.forget(svc, run_id)


async def test_ask_in_a_single_user_sandbox_warns_or_refuses(svc, live, monkeypatch, settings):  # noqa: F811
    new(svc, live, "sh", "hello", approvals="ask")
    s = await settle(svc, "sh")
    sb = svc.fleet.get(s.sandbox_id)
    assert sb.info["isolation"] == {"mode": "shared", "why": "the fake provider doesn't support it yet"}
    assert "can tamper with later turns" in s.turns[0].warnings[0]
    settings.uid_split = "required"
    with pytest.raises(steering.SteerError, match="HANGAR_UID_SPLIT=required"):
        await steering.prepare(svc, s, SessionTurn(message="x"), sb, HARNESSES["live-claude"])
    s.approvals = "auto"  # nothing to protect: runs as before
    run_id, command = await steering.prepare(svc, s, SessionTurn(message="x"), sb, HARNESSES["live-claude"])
    assert "--as " not in command
    steering.forget(svc, run_id)


async def test_a_split_run_is_stopped_as_hangars_user():
    p, settings = Prov(out=""), Settings()
    sb = Sandbox(name="s", environment="e", provider="docker", info={"runs": [{"id": "run-0000beef", "split": True},
                                                                    {"id": "run-0000cafe"}]})
    await isolation.kill(p, sb, settings, "run-0000beef", interrupted=True)
    await isolation.kill(p, sb, settings, "run-0000cafe")
    (split_cmd, split_user), (shared_cmd, shared_user) = p.calls
    assert split_user == "hangar" and split_cmd.startswith(f"{isolation.TMUX} kill-session -t run-0000beef")
    assert f"echo 130 > {isolation.RUNS}/run-0000beef.exit" in split_cmd
    assert shared_user is None and shared_cmd == "tmux kill-session -t run-0000cafe 2>/dev/null"
    assert isolation.attach_command(sb, "run-0000beef")[-4:] == ["attach-session", "-r", "-t", "run-0000beef"]
    assert isolation.attach_command(sb, "run-0000cafe") is None
