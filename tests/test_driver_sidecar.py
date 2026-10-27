"""The driver sidecar's driver-side split (KAN-83; docs/security/driver-sidecar.md): the driver (--relay) and the
agent's relay (relay.py) as separate processes over loopback, as they'd run in two containers of one pod. The uid,
PID-namespace and volume separation is the pod's (test_kubernetes.py); this checks the protocol between them."""

from __future__ import annotations

import getpass
import json
import os
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
emit({"type": "result", "subtype": "success", "is_error": False, "result": note, "usage": {}})
for _ in sys.stdin:
    pass
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
    assert "verify gate doesn't run under the driver sidecar" in err  # the agent's check never runs as the driver
    assert not (tmp_path / "ctl" / "relay-token").exists()  # read and deleted, like the key
    assert not w["port"].exists()  # stopped listening once the relay was in


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
