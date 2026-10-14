"""hangar-envd end to end on the real Docker provider: started in a sandbox on first use, I/O through it, the
provider path when it goes away, nothing left when the sandbox is destroyed.

Opt-in (needs Docker and the binary from envd/build.sh):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_envd_docker.py -v
"""

import asyncio
import base64
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from hangar.envd import EnvdTerminal, find_binaries

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")

ROOT = Path(__file__).parents[1]


async def via_envd(fleet, ref: str) -> bool:
    """Whether a command's parent's parent (bash <- timeout <- ?) is the daemon."""
    res = await fleet.exec(ref, "p=$(awk '/^PPid/{print $2}' /proc/$PPID/status); readlink /proc/$p/exe")
    return "hangar-envd" in res.stdout


async def test_envd_in_a_docker_sandbox(tmp_path):
    from hangar.app import Hangar
    from hangar.config import Settings

    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    if not find_binaries(Settings(envd_dir=envd_dir)):
        pytest.skip("no hangar-envd binary (run envd/build.sh)")
    s = Settings(data_dir=tmp_path, env_dirs=[ROOT / "envs"], reaper_interval_seconds=3600, envd_dir=envd_dir,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    fleet = h.fleet
    await fleet.start()
    prov = fleet.provider("docker")
    try:
        sb = await fleet.create("minimal", name="e2e-envd", labels={"e2e-envd": "1"}, actor="test")
        ep = await prov.envd.ready(fleet.get(sb.id))
        assert ep is not None, "the daemon did not come up"
        assert await via_envd(fleet, sb.id)
        r = await fleet.exec(sb.id, "whoami; echo $HOME; test -z \"$HANGAR_ENVD_TOKEN\" && echo no-token-in-env")
        assert r.exit_code == 0 and r.stdout.split() == ["agent", "/home/agent", "no-token-in-env"], r

        # Files, with the provider's semantics (relative paths, limits, ownership).
        await fleet.write_file(sb.id, "envd/f.txt", b"hello")
        assert await fleet.read_file(sb.id, "envd/f.txt") == b"hello"
        assert await fleet.read_file(sb.id, "envd/f.txt", limit=2) == b"hel"
        r = await fleet.exec(sb.id, "stat -c '%U %a' /workspace/envd/f.txt")
        assert r.stdout.strip() == "agent 644"
        assert await fleet.read_file(sb.id, "envd/missing") is None

        # Output is capped like the provider's.
        r = await fleet.exec(sb.id, "head -c 3000000 /dev/zero | tr '\\0' x; echo; echo end")
        assert r.truncated and r.stdout.endswith("end\n") and len(r.stdout) < 300_000

        # Deny by default: from inside the sandbox, without the token, the daemon answers 401.
        r = await fleet.exec(sb.id, "exec 3<>/dev/tcp/127.0.0.1/49983; printf 'GET /info HTTP/1.1\\r\\nHost: x\\r\\n"
                                    "Connection: close\\r\\n\\r\\n' >&3; head -1 <&3")
        assert " 401 " in r.stdout, r

        # The dashboard's live terminal.
        term = await fleet.attach(sb.id, None, 100, 30)
        try:
            assert isinstance(term, EnvdTerminal)
            await term.write(b"stty size; echo $((6*7))\r")
            seen = b""
            while b"42" not in seen or b"30 100" not in seen:
                chunk = await asyncio.wait_for(term.read(), 10)
                assert chunk, seen
                seen += chunk
        finally:
            await term.close()
            fleet.detach(sb.id)

        # The daemon dies: the next command still runs (the provider's path) and the daemon comes back.
        c = prov.client.containers.get(sb.provider_ref)
        assert c.exec_run(["pkill", "-f", "hangar-envd"], user="root").exit_code == 0
        await asyncio.sleep(0.2)
        r = await fleet.exec(sb.id, "echo still-works")
        assert r.stdout == "still-works\n"
        assert await prov.envd.ready(fleet.get(sb.id)) is not None
        assert await via_envd(fleet, sb.id)

        # Destroying the sandbox ends what the daemon started, and hangar forgets the daemon.
        await fleet.exec(sb.id, "nohup sleep 4242 >/dev/null 2>&1 &")
        assert subprocess.run(["pgrep", "-f", "sleep 4242"], capture_output=True).returncode == 0
        await fleet.destroy(sb.id)
        assert sb.id not in prov.envd._eps
        await asyncio.sleep(0.5)
        assert subprocess.run(["pgrep", "-f", "sleep 4242"], capture_output=True).returncode == 1
    finally:
        await fleet.destroy_many(label="e2e-envd")
        await fleet.stop()


async def _exists(fleet, ref: str, path: str) -> bool:
    return (await fleet.exec(ref, f"test -e {shlex.quote(path)}")).exit_code == 0


async def _until(pred, what: str, timeout: float = 60.0):
    for _ in range(int(timeout / 0.1)):
        if got := await pred():
            return got
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.mark.parametrize("envd", ["auto", "off"])
async def test_a_live_turn_works_the_same_through_envd(tmp_path, monkeypatch, envd):
    """integrate (#59 x #60): a session's live turn under hangar's driver behaves the same through hangar-envd as
    through `docker exec`: the tmux launch, the driver's key and command files, and the driver's signed answers to
    an approval, a steer and an interrupt. A scripted Claude Code (tests/fixtures/live) stands in for the model."""
    from hangar import steer as steering
    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.harnesses import HARNESSES, Harness
    from hangar.models import SessionState
    from hangar.sessions import NewSession
    from tests.test_steer import SESSION_FLAGS, settle

    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    if envd == "auto" and not find_binaries(Settings(envd_dir=envd_dir)):
        pytest.skip("no hangar-envd binary (run envd/build.sh)")
    fake = base64.b64encode((ROOT / "tests" / "fixtures" / "live" / "fake_claude.py").read_bytes()).decode()
    monkeypatch.setitem(HARNESSES, "live-claude", Harness(
        name="live-claude", title="Claude (scripted)", run="false", live="claude", resume="preset",
        live_run=(f"bash -c 'echo {fake} | base64 -d > /tmp/fake_claude.py && exec python3 /tmp/fake_claude.py "
                  f"\"$@\"' fake --model x {SESSION_FLAGS}"), session_dirs=[".claude/projects"]))
    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "liveenv.yaml").write_text(
        "name: liveenv\nbase: mirror.gcr.io/library/ubuntu:24.04\nharnesses: [live-claude]\nharness_mode: bake\n"
        "resources: {cpus: 1, memory_gb: 1}\n")
    s = Settings(data_dir=tmp_path / "data", env_dirs=[envs, ROOT / "envs"], reaper_interval_seconds=3600,
                 envd=envd, envd_dir=envd_dir, session_poll_seconds=0.2,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    fleet, svc = h.fleet, h.sessions
    await fleet.start()
    await svc.start()
    prov = fleet.provider("docker")
    name = f"e2e-live-{envd}"
    sid = None

    async def live_inbox():
        x = svc.get(name)
        t = x.turns[-1]
        if x.state == SessionState.RUNNING and t.live and t.run_id:
            return await _exists(fleet, sid, f"{steering.ctl_dir(svc, t.run_id)}/in")
        return False

    async def pending():
        return next(iter(steering.pending(svc.get(name))), None)

    try:
        svc.new(NewSession(name=name, environment="liveenv", approvals="ask"), owner="admin", requester="sean")
        sid = (await settle(svc, name, 900)).sandbox_id  # the first run builds the image
        assert sid
        if envd == "auto":
            assert await prov.envd.ready(fleet.get(sid)) is not None
        assert await via_envd(fleet, sid) is (envd == "auto")

        # An approval, answered: the driver's signed ack comes back through the same transport.
        svc.send(name, "TOOL make the file", requester="sean")
        a = await _until(pending, "the approval request")
        assert "touch tool-ran.txt" in a.detail and not await _exists(fleet, sid, "/workspace/tool-ran.txt")
        assert (await steering.answer(svc, svc.get(name), a.id, "allow", "ana")).status == "allowed"
        t = (await settle(svc, name)).turns[-1]
        assert (t.outcome, t.live, [x.status for x in t.approvals]) == ("exited", True, ["allowed"]), t.summary
        assert await _exists(fleet, sid, "/workspace/tool-ran.txt")
        assert not await _exists(fleet, sid, f"{steering.ctl_dir(svc, t.run_id)}/key")  # the driver took its key

        # A steer folded into the running turn.
        svc.send(name, "SLOW work", requester="sean")
        await _until(live_inbox, "the second turn's driver")
        assert (await steering.steer(svc, svc.get(name), "in Rust", "ana")).status == "delivered"
        t = (await settle(svc, name)).turns[-1]
        assert (t.outcome, t.summary) == ("exited", "did: SLOW work | steered: in Rust")

        # An interrupt the driver confirms with a signed answer (unconfirmed, hangar would kill the run instead).
        async def unconfirmed(*_a):
            raise AssertionError("the driver's answer to the interrupt was not taken")

        monkeypatch.setattr(steering, "kill", unconfirmed)
        svc.send(name, "SLOW again", requester="sean")
        await _until(live_inbox, "the third turn's driver")
        assert (await steering.interrupt(svc, svc.get(name), "ana")).status == "delivered"
        t = (await settle(svc, name)).turns[-1]
        assert (t.outcome, t.exit_code) == ("interrupted", 130)
        assert await via_envd(fleet, sid) is (envd == "auto")
    finally:
        await svc.stop()
        if sid:
            await fleet.destroy(sid)
        await fleet.stop()
