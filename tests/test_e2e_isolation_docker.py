"""Sandbox isolation end to end on a real Docker engine (workstream isolation).

Network: two sandboxes on hangar's network can't reach each other's ports (an app port, hangar-envd's), while the
control plane still reaches both and a sandbox still reaches the host (where the git gateway listens).

The uid split: in a sandbox whose agent has no sudo, a live turn's driver runs as hangar's own user and the agent's
harness as the agent, without capabilities. From the agent's side: no reading a run's key, no writing the driver, the
turn's script or its control files, no signalling or tracing the driver, no way back to its uid. Approvals, steers,
interrupts and the dashboard's view of the run work as before. A sandbox that can't be split (sudo) runs single-user,
and HANGAR_UID_SPLIT=required makes an `ask` turn there fail.

Opt-in (needs Docker, and network for the first image builds; hangar-envd from envd/build.sh or HANGAR_ENVD_DIR for
the envd part):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_isolation_docker.py -v
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shlex
import socket
from pathlib import Path

import pytest

from hangar.envd import PORT as ENVD_PORT
from hangar.envd import find_binaries

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")

ROOT = Path(__file__).parents[1]

# Run as the agent while a split turn waits on an approval: everything it tries against the driver and its files.
PROBE = r"""
import ctypes, json, os, signal, sys
run, key = sys.argv[1], sys.argv[2]
libc = ctypes.CDLL(None, use_errno=True)
out = {}
def proc(name):
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                cmd = open(f"/proc/{p}/cmdline", "rb").read().split(b"\0")
            except OSError:
                continue
            if any(name.encode() in c for c in cmd[:3]):
                return int(p)
def status(pid):
    return {k: v.split()[0] for k, v in (l.split(":", 1) for l in open(f"/proc/{pid}/status")) if k in
            ("Uid", "CapEff", "CapPrm", "CapAmb")}
def attempt(what, fn):
    try:
        fn()
        out[what] = "allowed"
    except OSError as e:
        out[what] = e.strerror
def write(path):
    with open(path, "a") as f:
        f.write("# the agent was here\n")
d = proc("/var/lib/hangar/driver.py")
out["driver"] = status(d)
out["harness"] = status(proc("fake_claude.py"))
out["me"] = status(os.getpid())
attempt("read the key", lambda: open(key).read())
attempt("list the control dir", lambda: os.listdir(os.path.dirname(key)))
attempt("write a command", lambda: write(f"/var/lib/hangar/runs/{run}.ctl/in/forged.json"))
attempt("write the driver", lambda: write("/var/lib/hangar/driver.py"))
attempt("write the turn's script", lambda: write(f"/var/lib/hangar/runs/{run}.sh"))
attempt("write the turn's env", lambda: write(f"/var/lib/hangar/runs/{run}.env"))
attempt("signal the driver", lambda: os.kill(d, signal.SIGTERM))
attempt("read the driver's environ", lambda: open(f"/proc/{d}/environ", "rb").read())
attempt("read the driver's memory", lambda: open(f"/proc/{d}/mem", "rb").read(1))
attempt("setuid to the driver's", lambda: os.setuid(int(out["driver"]["Uid"])))
out["ptrace the driver"] = ("allowed" if libc.ptrace(16, d, None, None) == 0
                            else os.strerror(ctypes.get_errno()))  # PTRACE_ATTACH
print(json.dumps(out))
"""


async def _reach(fleet, ref: str, host: str, port: int) -> bool:
    """Whether a TCP connection from the sandbox to host:port opens within 3 s."""
    r = await fleet.exec(ref, f"timeout 3 bash -c 'exec 3<>/dev/tcp/{host}/{port}' && echo open || echo shut")
    return r.stdout.strip() == "open"


async def test_sandboxes_cant_reach_each_other_on_docker(tmp_path):
    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.providers.docker_net import NAME, isolates

    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    s = Settings(data_dir=tmp_path, env_dirs=[ROOT / "envs"], reaper_interval_seconds=3600, envd_dir=envd_dir,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    fleet = h.fleet
    await fleet.start()
    prov = fleet.provider("docker")
    host_srv = None
    try:
        a = await fleet.create("minimal", name="e2e-iso-a", labels={"e2e-iso": "1"}, actor="test")
        b = await fleet.create("minimal", name="e2e-iso-b", labels={"e2e-iso": "1"}, actor="test")
        assert isolates(prov.client.api.inspect_network(NAME))
        ips = {}
        for sb in (a, b):
            attrs = prov.client.api.inspect_container(fleet.get(sb.id).provider_ref)
            assert list(attrs["NetworkSettings"]["Networks"]) == [NAME]
            ips[sb.id] = attrs["NetworkSettings"]["Networks"][NAME]["IPAddress"]
        await fleet.exec(a.id, "nohup python3 -m http.server 8000 >/dev/null 2>&1 &")
        for _ in range(50):  # the control plane (this host) reaches it: the server is up
            try:
                socket.create_connection((ips[a.id], 8000), timeout=1).close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise AssertionError("the app in sandbox a never answered the host")

        assert not await _reach(fleet, b.id, ips[a.id], 8000)
        assert await _reach(fleet, a.id, "127.0.0.1", 8000)  # its own app, from inside: fine

        if find_binaries(s):  # hangar-envd: reachable by hangar, not by the other sandbox
            assert await prov.envd.ready(fleet.get(a.id)) is not None
            assert not await _reach(fleet, b.id, ips[a.id], ENVD_PORT)

        # The host stays reachable (the git gateway listens there, on the default bridge's address).
        gw = prov.client.api.inspect_network("bridge")["IPAM"]["Config"][0]["Gateway"]
        host_srv = await asyncio.start_server(lambda r, w: w.close(), gw, 0)
        port = host_srv.sockets[0].getsockname()[1]
        assert await _reach(fleet, b.id, shlex.quote(gw), port)
    finally:
        if host_srv is not None:
            host_srv.close()
        await fleet.destroy_many(label="e2e-iso")
        await fleet.stop()


async def _until(pred, what: str, timeout: float = 60.0):
    for _ in range(int(timeout / 0.1)):
        if got := await pred():
            return got
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def test_the_agent_cant_reach_the_driver_on_docker(tmp_path, monkeypatch):
    from hangar import isolation
    from hangar import steer as steering
    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.harnesses import HARNESSES, Harness
    from hangar.models import SessionState, SessionTurn
    from hangar.sessions import NewSession
    from tests.test_steer import SESSION_FLAGS, settle

    fake = base64.b64encode((ROOT / "tests" / "fixtures" / "live" / "fake_claude.py").read_bytes()).decode()
    # One-shot runs (no driver) report who they ran as.
    monkeypatch.setitem(HARNESSES, "live-claude", Harness(
        name="live-claude", title="Claude (scripted)", live="claude", resume="preset",
        run="echo \"{\\\"type\\\":\\\"result\\\",\\\"result\\\":\\\"one-shot as $(id -un)\\\"}\"",
        live_run=(f"bash -c 'echo {fake} | base64 -d > /tmp/fake_claude.py && exec python3 /tmp/fake_claude.py "
                  f"\"$@\"' fake --model x {SESSION_FLAGS}"), session_dirs=[".claude/projects"]))
    envs = tmp_path / "envs"
    envs.mkdir()
    spec = ("base: mirror.gcr.io/library/ubuntu:24.04\nharnesses: [live-claude]\nharness_mode: bake\n"
            "resources: {cpus: 1, memory_gb: 1}\n")
    (envs / "isosplit.yaml").write_text("name: isosplit\nsudo: false\n" + spec)
    (envs / "isosudo.yaml").write_text("name: isosudo\n" + spec)
    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    s = Settings(data_dir=tmp_path / "data", env_dirs=[envs, ROOT / "envs"], reaper_interval_seconds=3600,
                 envd_dir=envd_dir, session_poll_seconds=0.2,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    fleet, svc = h.fleet, h.sessions
    await fleet.start()
    await svc.start()
    sids = []

    async def pending():
        return next(iter(steering.pending(svc.get("iso"))), None)

    async def live_inbox():
        x = svc.get("iso")
        t = x.turns[-1]
        if x.state == SessionState.RUNNING and t.live and t.run_id:
            sbx = fleet.get(x.sandbox_id)
            inbox = f"{steering.ctl_dir(svc, t.run_id, sbx)}/in"
            res = await isolation.sh(fleet.provider("docker"), sbx, f"test -d {inbox}")
            return res.exit_code == 0
        return False

    try:
        svc.new(NewSession(name="iso", environment="isosplit", approvals="ask"), owner="admin", requester="sean")
        sid = (await settle(svc, "iso", 900)).sandbox_id  # the first run builds the image
        sids.append(sid)
        # The image's PATH starts with ~/.local/bin, which the agent owns: commands planted there must never run as
        # root or as hangar's user (they log who ran them, then run the real one).
        plant = "".join(
            f"printf '#!/bin/sh\\necho \"$(/usr/bin/id -un) {c}\" >> /tmp/planted.log\\nexec {p} \"$@\"\\n' "
            f"> ~/.local/bin/{c} && chmod +x ~/.local/bin/{c}; "
            for c, p in (("timeout", "/usr/bin/timeout"), ("setpriv", "/usr/bin/setpriv"), ("tmux", "/usr/bin/tmux"),
                         ("env", "/usr/bin/env"), ("python3", "/usr/bin/python3"), ("id", "/usr/bin/id")))
        r = await fleet.exec(sid, "mkdir -p ~/.local/bin && : > /tmp/planted.log && chmod 666 /tmp/planted.log; "
                                  + plant)
        assert r.exit_code == 0, r

        # A turn that waits on an approval: the driver is up, the harness waits for its answer.
        svc.send("iso", "TOOL make the file", requester="sean")
        a = await _until(pending, "the approval request")
        t = svc.get("iso").turns[-1]
        sb = fleet.get(sid)
        assert isolation.split(sb) and isolation.run_split(sb, t.run_id), sb.info.get("isolation")
        assert not t.warnings

        # A key as the next turn gets it (the running driver deleted its own at start).
        run_id, _ = await steering.prepare(svc, svc.get("iso"), SessionTurn(message="x"), sb, HARNESSES["live-claude"])
        key = f"{isolation.ctl_dir(s, run_id, True)}/key"
        probe = base64.b64encode(PROBE.encode()).decode()
        r = await fleet.exec(sid, f"echo {probe} | base64 -d > /tmp/probe.py && python3 /tmp/probe.py "
                                  f"{shlex.quote(t.run_id)} {shlex.quote(key)}")
        assert r.exit_code == 0, r
        got = json.loads(r.stdout)
        steering.forget(svc, run_id)
        iso = sb.info["isolation"]
        assert got["driver"]["Uid"] == str(iso["uid"]) and got["driver"]["CapAmb"] != "0000000000000000"
        assert got["harness"]["Uid"] == got["me"]["Uid"] == "1000"
        assert got["harness"]["CapEff"] == got["harness"]["CapPrm"] == got["harness"]["CapAmb"] == "0000000000000000"
        for what in ("read the key", "list the control dir", "write a command", "write the driver",
                     "write the turn's script", "write the turn's env", "read the driver's environ",
                     "read the driver's memory"):
            assert got[what] == "Permission denied", (what, got)
        for what in ("signal the driver", "setuid to the driver's", "ptrace the driver"):
            assert got[what] == "Operation not permitted", (what, got)

        # The approval still works, and the agent's tool ran as the agent.
        assert (await steering.answer(svc, svc.get("iso"), a.id, "allow", "ana")).status == "allowed"
        t = (await settle(svc, "iso")).turns[-1]
        assert (t.outcome, t.live, [x.status for x in t.approvals]) == ("exited", True, ["allowed"]), t.summary
        r = await fleet.exec(sid, f"stat -c %U /workspace/tool-ran.txt; readlink ~/.hangar/runs/{t.run_id}.log")
        assert r.stdout.split() == ["agent", f"{isolation.RUNS}/{t.run_id}.log"], r

        # A ~/.hangar/env the agent rewrote runs as the agent, in the harness's environment only.
        await fleet.exec(sid, "echo 'id -un >> /tmp/env-ran-as' >> ~/.hangar/env")

        # Steer and interrupt reach the split driver (through hangar's user), and the dashboard can watch the run.
        svc.send("iso", "SLOW work", requester="sean")
        await _until(live_inbox, "the second turn's driver")
        run = svc.get("iso").turns[-1].run_id
        assert "command_lifecycle" in await fleet.terminal_read(sid, run)
        term = await fleet.attach(sid, run, 100, 30)
        try:
            seen = b""
            while b"command_lifecycle" not in seen:
                chunk = await asyncio.wait_for(term.read(), 10)
                assert chunk, seen
                seen += chunk
        finally:
            await term.close()
            fleet.detach(sid)
        assert (await steering.steer(svc, svc.get("iso"), "in Rust", "ana")).status == "delivered"
        t = (await settle(svc, "iso")).turns[-1]
        assert (t.outcome, t.summary) == ("exited", "did: SLOW work | steered: in Rust")
        svc.send("iso", "SLOW again", requester="sean")
        await _until(live_inbox, "the third turn's driver")
        assert (await steering.interrupt(svc, svc.get("iso"), "ana")).status == "delivered"
        t = (await settle(svc, "iso")).turns[-1]
        assert (t.outcome, t.exit_code) == ("interrupted", 130)
        r = await fleet.exec(sid, "sort -u /tmp/env-ran-as; cut -d' ' -f1 /tmp/planted.log | sort -u")
        assert r.stdout.split() == ["agent", "agent"], r  # nothing planted ran as root or hangar

        # A one-shot run (no driver) runs as the agent too.
        res = await fleet.run_agent(sid, "hi")
        st = await fleet.agent_status(sid, res["run_id"], wait_seconds=20)
        assert st["state"] == "finished" and "one-shot as agent" in st["output_tail"], st

        # A sandbox whose agent has sudo stays single-user; with HANGAR_UID_SPLIT=required an `ask` turn fails there.
        s.uid_split = "required"
        svc.new(NewSession(name="iso-sudo", environment="isosudo", approvals="ask", message="TOOL x"), owner="admin",
                requester="sean")
        x = await settle(svc, "iso-sudo", 900)
        if x.sandbox_id:
            sids.append(x.sandbox_id)
            assert fleet.get(x.sandbox_id).info["isolation"] == {
                "mode": "shared", "why": "agent can use sudo (the environment has sudo: true)"}
        assert "HANGAR_UID_SPLIT=required" in (x.reason or ""), x.reason
    finally:
        await svc.stop()
        for sid in sids:
            with contextlib.suppress(Exception):
                await fleet.destroy(sid)
        await fleet.stop()
