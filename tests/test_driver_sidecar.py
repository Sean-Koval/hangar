"""The driver sidecar's driver-side split (KAN-83; docs/security/driver-sidecar.md): the driver (--relay) and the
agent's relay (relay.py) as separate processes over loopback, as they'd run in two containers of one pod. The uid,
PID-namespace and volume separation is the pod's (test_kubernetes.py); this checks the protocol between them."""

from __future__ import annotations

import getpass
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hangar import driver, relay

DRIVER = Path(driver.__file__)
RELAY = Path(relay.__file__)

# A scripted Claude Code: one turn; with ASK_TOOL it asks to run a risky command first (deny-risky refuses it).
FAKE = r'''
import json, os, sys, time
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
if os.environ.get("PID_FILE"):
    open(os.environ["PID_FILE"], "w").write(str(os.getpid()))
sys.stdin.readline()
sys.stderr.write("fake: on stderr\n"); sys.stderr.flush()
if os.environ.get("HANG"):
    time.sleep(600)
emit({"type": "system", "subtype": "init", "session_id": "s", "model": "m"})
note = "run=" + os.environ.get("RUN_VAR", "-") + " base=" + os.environ.get("BASE_VAR", "-") + " cwd=" + os.getcwd()
if os.environ.get("ASK_TOOL"):
    emit({"type": "control_request", "request_id": "r1", "request": {
        "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "sudo rm -rf /"}}})
    note += " answer=" + json.loads(sys.stdin.readline())["response"]["response"]["behavior"]
if os.environ.get("EDIT"):  # an edit in the checkout: the driver hashes the tree after its result
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "w1", "name": "Write"}]}})
    open("edited.txt", "w").write("edit\n")
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "w1"}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": note, "usage": {}})
for line in sys.stdin:  # a message hangar steers in (the verify gate's failure) gets a result of its own
    if json.loads(line).get("type") == "user":
        emit({"type": "result", "subtype": "success", "is_error": False, "usage": {},
              "result": "again: " + json.loads(line)["message"]["content"]})
sys.exit(int(os.environ.get("EXIT_CODE", "0")))
'''


def _world(tmp: Path, token: str = "tok-" + "a" * 32) -> dict:
    (tmp / "ctl").mkdir()
    (tmp / "ctl" / "relay-token").write_text(token + "\n")
    (tmp / "home").mkdir()
    (tmp / "work").mkdir()
    (tmp / "run.env").write_text("export RUN_VAR=from-run-env\n")
    (tmp / "fake.py").write_text(FAKE)
    (tmp / "prompt").write_text("go")
    return {"tmp": tmp, "token": token, "port": tmp / "run.port"}


def _driver(w: dict, *extra: str, env: dict | None = None) -> subprocess.Popen:
    t = w["tmp"]
    argv = [sys.executable, str(DRIVER), "--harness", "claude", "--ctl", str(t / "ctl"), "--approvals", "deny-risky",
            "--relay", str(w["port"]), "--agent", getpass.getuser(), "--agent-home", str(t / "home"),
            "--run-env", str(t / "run.env"), "--cwd", str(t / "work"), *extra, "--", sys.executable, str(t / "fake.py")]
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=t,
                            env={**os.environ, "HANGAR_PROMPT_FILE": str(t / "prompt"), **(env or {})})


def _relay(w: dict, token: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(RELAY), str(w["port"]), "--wait", "20"],
                          input=(token if token is not None else w["token"]) + "\n", capture_output=True, text=True,
                          timeout=30, env={**os.environ, **(env or {})})


def _port(w: dict) -> int:
    end = time.time() + 20
    while not (w["port"].exists() and w["port"].read_text().strip().isdigit()):
        assert time.time() < end, "the driver never listened"
        time.sleep(0.05)
    return int(w["port"].read_text())


def _result(stdout: str) -> dict:
    return next(d for d in map(json.loads, filter(None, stdout.splitlines())) if d.get("type") == "result")


def test_a_turn_runs_through_the_relay_and_its_approvals_stay_the_drivers(tmp_path):
    w = _world(tmp_path)
    drv = _driver(w, env={"HANGAR_VERIFY_GATE": json.dumps({"command": "true", "cwd": "/", "rounds": 1,
                                                            "deadline": 2**31, "timeout": 5})})
    rel = _relay(w, env={"ASK_TOOL": "1"})
    assert rel.returncode == 0 and rel.stdout.startswith("hangar-relay: started"), rel.stderr
    out, err = drv.communicate(timeout=60)
    assert drv.returncode == 0, err
    res = _result(out)["result"]
    assert "run=from-run-env" in res and f"cwd={tmp_path / 'work'}" in res
    assert "answer=deny" in res  # the driver answered from its rules, over the relay
    events = [json.loads(ln) for ln in out.splitlines() if ln.startswith('{"type": "hangar.')]
    resolved = [e for e in events if e["type"] == "hangar.approval_resolved"]
    assert [(e["decision"], e["by"]) for e in resolved] == [("deny", "policy")], out
    assert (tmp_path / "ctl" / "approvals").exists()  # the driver's own copy, in its control directory
    assert "fake: on stderr" in err  # the harness's stderr came back on ctl
    assert any(e["type"] == "hangar.verify" and e["passed"] for e in events)  # the gate ran, in the agent's place
    assert not (tmp_path / "ctl" / "relay-token").exists()  # read and deleted, like the key
    assert not w["port"].exists()  # stopped listening once the relay was in


def _git_work(w: dict) -> Path:
    work = w["tmp"] / "work"
    for argv in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", "-C", str(work), *argv], check=True, capture_output=True)
    (work / "README").write_text("hi\n")
    subprocess.run(["git", "-C", str(work), "add", "README"], check=True, capture_output=True)
    return work


def _events(out: str) -> list:
    return [json.loads(ln) for ln in out.splitlines() if ln.startswith('{"type": "hangar.')]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_an_ask_turn_under_the_sidecar_records_trees_from_the_agents_container(tmp_path):
    w = _world(tmp_path)
    work = _git_work(w)
    drv = _driver(w, "--approvals", "ask", env={"HANGAR_RUN_ID": "r1"})
    assert _relay(w, env={"EDIT": "1"}).returncode == 0
    out, err = drv.communicate(timeout=60)
    assert drv.returncode == 0, err
    trees = [e for e in _events(out) if e["type"] == "hangar.tree"]
    assert [e.get("phase") or e.get("tool_use_id") for e in trees] == ["start", "w1", "end"], out
    assert all(e["by"] == "agent" and len(e["tree"]) == 40 for e in trees)  # the agent's hashes: never trusted
    assert trees[0]["tree"] != trees[1]["tree"] == trees[2]["tree"]  # the edit changed it, nothing after it did
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)  # the repo's own ids
    want = subprocess.run(["git", "-C", str(work), "write-tree"], capture_output=True, text=True).stdout.strip()
    assert trees[2]["tree"] == want
    assert not (tmp_path / "home" / ".hangar" / "runs" / "r1.tree").exists()  # its scratch dir went at the end
    assert not (tmp_path / "ctl" / "tree").exists()  # nothing of the agent's tree in the driver's directory


def test_trees_hashed_by_the_agent_are_recorded_untrusted():
    from hangar.events import tree_events

    ev, = tree_events("a" * 40, phase="start", by="agent")
    assert ev.data["by"] == "agent"
    assert "by" not in tree_events("a" * 40, phase="start", by="something")[0].data


def test_the_verify_gate_runs_in_the_agents_container_through_the_relay(tmp_path):
    w = _world(tmp_path)
    check = 'echo "where=${WHERE:-} cwd=$(pwd)"; exit 3'
    gate = {"command": check, "cwd": str(tmp_path / "work"), "rounds": 1, "deadline": 2**31, "timeout": 20}
    drv = _driver(w, "--approvals", "ask", env={"HANGAR_RUN_ID": "r1", "WHERE": "driver",
                                                "HANGAR_VERIFY_GATE": json.dumps(gate)})
    assert _relay(w, env={"WHERE": "relay"}).returncode == 0
    out, err = drv.communicate(timeout=60)
    assert drv.returncode == 0, err
    verify = [e for e in _events(out) if e["type"] == "hangar.verify"]
    assert [(e["exit_code"], e["passed"]) for e in verify] == [(3, False)]
    again = [d["result"] for d in map(json.loads, filter(None, out.splitlines())) if d.get("type") == "result"][-1]
    assert again.startswith("again: hangar: the job's check") and "exit 3" in again
    assert f"where=relay cwd={tmp_path / 'work'}" in again  # run by the relay, as the agent, from the checkout
    assert (tmp_path / "home" / ".hangar" / "runs" / "r1.verify").read_text() == "fail 3\n"  # the agent's file


def test_a_run_that_outlives_its_timeout_is_killed_and_says_so(tmp_path):
    w = _world(tmp_path)
    gate = {"command": "echo started; sleep 60", "cwd": str(tmp_path / "work"), "rounds": 1, "deadline": 2**31,
            "timeout": 1}
    t0 = time.time()
    drv = _driver(w, env={"HANGAR_VERIFY_GATE": json.dumps(gate)})
    assert _relay(w).returncode == 0
    out, err = drv.communicate(timeout=60)
    assert drv.returncode == 0, err
    assert [e["exit_code"] for e in _events(out) if e["type"] == "hangar.verify"] == [124]
    again = [d["result"] for d in map(json.loads, filter(None, out.splitlines())) if d.get("type") == "result"][-1]
    assert "started" in again and "timed out after 1 s" in again
    assert time.time() - t0 < 30


def test_a_run_that_shares_the_specs_read_is_still_served(tmp_path):
    """The driver's first `run` (the start tree) can reach the relay in the same read as what to start."""
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(2)
    port = tmp_path / "run.port"
    port.write_text(f"{lsock.getsockname()[1]}\n")
    rel = subprocess.Popen([sys.executable, "-I", str(RELAY), str(port), "--foreground"], stdin=subprocess.PIPE,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rel.stdin.write(b"tok\n")
    rel.stdin.close()
    conns = {}
    lsock.settimeout(20)
    for _ in range(2):
        c, _ = lsock.accept()
        conns[c.recv(64).split()[1].decode()] = c
    ctl = conns["ctl"]
    spec = {"argv": [sys.executable, "-c", "import time; time.sleep(30)"], "base_env": None}
    run = {"id": "first", "argv": ["sh", "-c", "echo hi; exit 4"], "timeout": 10}
    ctl.sendall(json.dumps(spec).encode() + b"\nrun " + json.dumps(run).encode() + b"\n")
    proc = driver.RemoteProc(conns["io"], ctl)
    end = time.time() + 20
    while "first" not in proc.ran and time.time() < end:
        proc._read(1)
    assert proc.ran.get("first", {}).get("code") == 4
    assert proc.run(["sh", "-c", "echo again"], 10)[:2] == (0, b"again\n")
    proc.kill()
    proc.wait(20)
    proc.close()
    assert rel.wait(20) == 137  # killed, as a shell reports it; and gone once the driver closed ctl


def test_a_run_gets_none_once_the_relay_is_gone():
    a, b = socket.socketpair()
    io, _ = socket.socketpair()
    proc = driver.RemoteProc(io, a)
    b.sendall(b"exit 0\n")
    b.shutdown(socket.SHUT_WR)  # the relay closed its end: no `ran` will come
    assert proc.run(["true"], 1) is None
    assert proc.returncode == 0  # its exit status came first


def test_the_harness_exit_status_and_hangars_copy_of_the_env_cross_over(tmp_path):
    w = _world(tmp_path)
    base = tmp_path / "base.env"
    base.write_text("BASE_VAR=from-hangar\n")
    drv = _driver(w, "--base-env", str(base))
    assert _relay(w, env={"EXIT_CODE": "7"}).returncode == 0
    out, err = drv.communicate(timeout=60)
    assert drv.returncode == 7, err
    assert "base=from-hangar" in _result(out)["result"]
    assert not base.exists()


def test_a_connection_without_the_token_is_dropped_and_the_relay_still_gets_in(tmp_path):
    w = _world(tmp_path)
    drv = _driver(w)
    port = _port(w)
    refused = []
    for hello in (b"hangar-relay io wrong-token\n", b"hangar-relay ctl " + b"b" * 36 + b"\n", b"GET / HTTP/1.0\n"):
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(hello)
        s.settimeout(10)
        refused.append(s.recv(10))  # closed by the driver: b""
        s.close()
    assert refused == [b"", b"", b""]
    assert _relay(w).returncode == 0
    out, err = drv.communicate(timeout=60)
    assert drv.returncode == 0, err
    assert "run=from-run-env" in _result(out)["result"]


def test_a_relay_with_the_wrong_token_never_starts_the_harness(tmp_path):
    w = _world(tmp_path)
    t0 = time.time()
    rel = subprocess.Popen([sys.executable, "-I", str(RELAY), str(w["port"]), "--wait", "20"], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    drv = _driver(w)
    _port(w)
    _, rerr = rel.communicate("not-the-token\n", timeout=30)
    assert rel.returncode == 1  # the driver closed on it: no spec
    drv.send_signal(signal.SIGTERM)
    drv.communicate(timeout=30)
    assert time.time() - t0 < 30


def test_the_relay_refuses_to_start_without_a_token(tmp_path):
    w = _world(tmp_path)
    rel = _relay(w, token="")
    assert rel.returncode == 1 and "no token" in rel.stderr


def test_the_driver_refuses_without_its_relay_token(tmp_path):
    w = _world(tmp_path)
    (tmp_path / "ctl" / "relay-token").unlink()
    drv = _driver(w)
    _, err = drv.communicate(timeout=30)
    assert drv.returncode == 1 and "relay's token" in err
    assert (tmp_path / "ctl" / "done").exists()


def test_the_driver_gives_up_when_no_relay_comes(tmp_path):
    rl = driver.Relay(str(tmp_path / "run.port"), b"t")
    assert (tmp_path / "run.port").read_text().strip().isdigit()
    with pytest.raises(driver.RelayError, match="didn't connect"):
        rl.start(["true"], None, wait=0.3)
    assert not (tmp_path / "run.port").exists()


def test_when_the_driver_goes_the_harness_goes_with_it(tmp_path):
    w = _world(tmp_path)
    pid_file = tmp_path / "harness.pid"
    drv = _driver(w)
    assert _relay(w, env={"HANG": "1", "PID_FILE": str(pid_file)}).returncode == 0
    end = time.time() + 20
    while not (pid_file.exists() and pid_file.read_text().strip()):
        assert time.time() < end
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    drv.kill()  # SIGKILL: no `term` sent; the relay sees its ctl connection close
    drv.communicate(timeout=30)
    end = time.time() + 20
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        assert time.time() < end, "the harness outlived its driver"
        time.sleep(0.1)


@pytest.mark.parametrize("args", [
    ["--agent", "a", "--run-env", "/r.env", "--as", "a"],  # --as would setpriv in the driver's container
    ["--agent", "a"],  # no run env
    ["--run-env", "/r.env"],  # no agent
])
def test_relay_arguments(tmp_path, args):
    with pytest.raises(SystemExit):
        driver.main(["--harness", "claude", "--ctl", str(tmp_path), "--relay", str(tmp_path / "p"), *args, "--",
                     "true"])


def test_agent_launch_is_the_split_command_without_setpriv():
    split = driver.as_agent("u", "/home/u", "/r.env", "/w", ["claude", "-p"])
    assert split[:split.index("--") + 1][0] == "setpriv"
    assert split[split.index("--") + 1:] == driver.agent_launch("u", "/home/u", "/r.env", "/w", ["claude", "-p"])


@pytest.mark.parametrize("path", [DRIVER, RELAY])
def test_the_in_sandbox_scripts_parse_as_python_38(path):
    import ast

    ast.parse(path.read_text(), feature_version=(3, 8))
