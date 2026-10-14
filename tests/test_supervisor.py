"""--- envbuilds: the in-sandbox supervisor (`hangar-agent svc`, svc_agent.py), run for real with a scratch $HOME,
and its control-plane side (supervisor.py): the manifest, the agent's instructions and the helper's dispatch."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from hangar import exchange, supervisor
from hangar.envspec import EnvSpec
from hangar.models import Sandbox

from .conftest import free_port, scratch_root

SCRIPT = Path(supervisor.__file__).parent / "svc_agent.py"


@pytest.fixture
def home():
    h = Path(tempfile.mkdtemp(prefix="svc-", dir=scratch_root()))
    (h / ".hangar/svc").mkdir(parents=True)
    (h / "repo").mkdir()
    yield h
    state = h / ".hangar/svc/state.json"
    if state.exists():  # the daemon stops its programs on SIGTERM
        try:
            os.kill(json.loads(state.read_text())["pid"], signal.SIGTERM)
            time.sleep(0.5)
        except (ProcessLookupError, ValueError, KeyError):
            pass
    shutil.rmtree(h, ignore_errors=True)


def svc(home: Path, *args: str, timeout: float = 30, **env: str) -> subprocess.CompletedProcess:
    e = {"HOME": str(home), "PATH": os.environ["PATH"], "HANGAR_SVC_SERVICE_WAIT": "5", **env}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=e,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def manifest(home: Path, **m) -> None:
    (home / ".hangar/svc/manifest.json").write_text(json.dumps({"repo": str(home / "repo"), **m}))


def status(home: Path) -> dict:
    r = svc(home, "status", "--json")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def prog(st: dict, name: str) -> dict:
    return next(p for p in st["programs"] if p["name"] == name)


def until(fn, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (v := fn()):
            return v
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_help_needs_no_daemon(home):
    r = svc(home, "--help")
    assert r.returncode == 0 and "hangar-agent svc COMMAND" in r.stdout and "logs NAME" in r.stdout
    assert not (home / ".hangar/svc/state.json").exists()


def test_start_then_terminals_ready_on_their_port_and_logs_are_files(home):
    port = free_port()
    manifest(home, start="echo seeding > seeded.txt",
             terminals=[{"name": "web", "command": f"exec {sys.executable} -m http.server {port}", "port": port},
                        {"name": "later", "command": "sleep 300", "autostart": False}])
    assert svc(home, "up").returncode == 0
    r = svc(home, "wait", "web", "--timeout", "20")
    assert r.returncode == 0, r.stdout + r.stderr
    st = status(home)
    assert prog(st, "web")["ready"] and prog(st, "web")["state"] == "running"
    assert prog(st, "start")["state"] == "exited" and prog(st, "start")["last_exit"] == 0
    assert (home / "repo/seeded.txt").exists()  # start ran in the repo checkout
    assert prog(st, "later")["state"] == "stopped"  # autostart: false
    assert (home / ".hangar/svc/logs/start.log").read_text().count("seeding") == 1
    out = svc(home, "logs", "web", "-n", "5")
    assert out.returncode == 0 and "starting" in out.stdout
    assert (home / ".hangar/svc").stat().st_mode & 0o777 == 0o700  # the socket's directory is the agent's alone


def test_a_crashing_terminal_is_restarted_with_backoff(home):
    manifest(home, terminals=[{"name": "flaky", "command": "echo boom; exit 3"}])
    svc(home, "up")
    st = until(lambda: (s := status(home)) and prog(s, "flaky")["restarts"] >= 2 and s)
    p = prog(st, "flaky")
    assert p["last_exit"] == 3 and p["state"] in ("backoff", "running")
    assert (home / ".hangar/svc/logs/flaky.log").read_text().count("boom") >= 2


def test_restart_never_and_on_failure(home):
    manifest(home, terminals=[{"name": "once", "command": "exit 0", "restart": "never"},
                              {"name": "ok", "command": "exit 0", "restart": "on-failure"}])
    svc(home, "up")
    st = until(lambda: (s := status(home)) and all(p["state"] == "exited" for p in s["programs"]) and s)
    assert all(p["restarts"] == 0 for p in st["programs"])


def test_services_gate_start_and_wait_reports_them(home):
    port = free_port()
    manifest(home, start="echo started", services=[{"name": "db", "host": "127.0.0.1", "port": port}],
             terminals=[{"name": "web", "command": "sleep 300"}])
    svc(home, "up")
    time.sleep(1.5)
    st = status(home)
    assert st["start"] == "waiting-services" and st["services"][0]["ready"] is False
    assert prog(st, "web")["state"] == "waiting"
    text = svc(home, "status").stdout
    assert "waiting for db to answer" in text
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(port)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        r = svc(home, "wait", "--timeout", "20")
        assert r.returncode == 0, r.stdout
        st = status(home)
        assert st["services"][0]["ready"] and prog(st, "web")["state"] == "running"
    finally:
        srv.terminate()
        srv.wait()


def test_services_that_never_answer_dont_hold_terminals_forever(home):
    manifest(home, services=[{"name": "db", "host": "127.0.0.1", "port": free_port()}],
             terminals=[{"name": "web", "command": "sleep 300"}])
    svc(home, "up")  # HANGAR_SVC_SERVICE_WAIT=5 in these tests
    until(lambda: prog(status(home), "web")["state"] == "running", timeout=20)


def test_stop_start_restart_add_rm(home):
    manifest(home, terminals=[{"name": "web", "command": "sleep 300"}])
    svc(home, "up")
    until(lambda: prog(status(home), "web")["pid"])
    pid = prog(status(home), "web")["pid"]
    assert svc(home, "restart", "web").returncode == 0
    until(lambda: (p := prog(status(home), "web"))["pid"] and p["pid"] != pid)
    assert svc(home, "stop", "--all").returncode == 0
    until(lambda: prog(status(home), "web")["state"] == "stopped")
    with pytest.raises(ProcessLookupError):  # the whole process group went, not only bash
        os.kill(pid, 0)
    assert svc(home, "start", "web").returncode == 0
    until(lambda: prog(status(home), "web")["state"] == "running")
    port = free_port()
    r = svc(home, "add", "srv", "--port", str(port), "--", sys.executable, "-m", "http.server", str(port))
    assert r.returncode == 0, r.stderr
    assert svc(home, "wait", "srv", "--timeout", "20").returncode == 0
    assert svc(home, "add", "web", "--", "true").returncode == 1  # the environment's own name
    assert svc(home, "rm", "web").returncode == 1
    assert svc(home, "rm", "srv").returncode == 0
    assert "srv" not in {p["name"] for p in status(home)["programs"]}
    r = svc(home, "start", "nope")
    assert r.returncode == 1 and "no program named nope" in r.stderr


def test_wait_fails_on_a_program_that_gave_up_or_isnt_ready(home):
    manifest(home, terminals=[{"name": "web", "command": "sleep 300", "port": free_port()}])
    svc(home, "up")
    r = svc(home, "wait", "web", "--timeout", "2")
    assert r.returncode == 1 and "still not ready after 2s: web" in r.stdout


def test_a_second_up_reuses_the_daemon(home):
    manifest(home, terminals=[{"name": "web", "command": "sleep 300"}])
    svc(home, "up")
    first = json.loads((home / ".hangar/svc/state.json").read_text())["pid"]
    procs = [subprocess.Popen([sys.executable, str(SCRIPT), "up"], env={"HOME": str(home), "PATH": os.environ["PATH"]},
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for _ in range(3)]
    for p in procs:
        assert p.wait(timeout=20) == 0
    assert json.loads((home / ".hangar/svc/state.json").read_text())["pid"] == first


def test_the_daemon_stops_with_its_directory(home):
    manifest(home, terminals=[{"name": "web", "command": "sleep 300"}])
    svc(home, "up")
    child = until(lambda: prog(status(home), "web")["pid"])
    daemon = json.loads((home / ".hangar/svc/state.json").read_text())["pid"]
    shutil.rmtree(home / ".hangar/svc")

    def gone(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True
    until(lambda: gone(daemon) and gone(child), timeout=25)


# ---------------------------------------------------------------------- hangar-agent svc
def test_hangar_agent_dispatches_svc_outside_a_turn(home):
    bin_ = home / ".hangar/bin"
    bin_.mkdir(parents=True)
    (bin_ / "hangar-agent").write_text(exchange.HELPER)
    (bin_ / "hangar-svc").write_bytes(supervisor.SCRIPT)
    env = {"HOME": str(home), "PATH": f"{bin_}:{os.environ['PATH']}"}  # no HANGAR_IO
    r = subprocess.run(["sh", str(bin_ / "hangar-agent"), "svc", "--help"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "hangar-agent svc COMMAND" in r.stdout
    r = subprocess.run(["sh", str(bin_ / "hangar-agent"), "progress", "x"], capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "HANGAR_IO is not set" in r.stderr  # the rest still needs a turn
    assert "hangar-agent svc status|wait|logs" in exchange.HELPER  # in the helper's usage


# ---------------------------------------------------------------------- the control plane's side
def _sb(**info) -> Sandbox:
    return Sandbox(name="s", environment="e", provider="docker", info=info)


def test_manifest_and_info_entries():
    eff = EnvSpec(name="e", start="./seed", terminals=[{"name": "web", "command": "npm run dev", "port": 3000,
                                                        "ready": "curl -fs localhost:3000"}],
                  ports=[{"port": 3000, "name": "web"}])
    sb = _sb(services=[{"name": "db", "kind": "service", "host": "db", "port": 5432, "check": None},
                       {"name": "old", "kind": "terminal", "port": 1}])
    m = supervisor.manifest(sb, eff, "/workspace/app")
    assert m["repo"] == "/workspace/app" and m["start"] == "./seed"
    assert m["terminals"][0] == {"name": "web", "command": "npm run dev", "env": {}, "port": 3000,
                                 "ready": "curl -fs localhost:3000", "autostart": True, "restart": "always"}
    assert m["services"] == [{"name": "db", "host": "db", "port": 5432, "ready": None}]
    info = supervisor.programs_info(eff)
    assert {"name": "web", "kind": "terminal", "port": 3000, "check": "curl -fs localhost:3000"} in info
    assert {"name": "web", "kind": "port", "port": 3000, "check": None} in info


def test_instructions_warn_on_the_base_image_and_splice_is_idempotent():
    eff = EnvSpec(name="e", install="uv sync", terminals=[{"name": "web", "command": "x", "port": 3000}])
    sb = _sb(env_build={"fallback": True, "reason": "the repo has no build yet"},
             setup_failures=[{"ts": 1, "step": "config", "message": "bad field"}])
    block = supervisor.instructions(sb, eff, "/workspace/app")
    assert "Warning: this sandbox runs the environment's base image" in block
    assert "cd /workspace/app && uv sync" in block and "hangar-agent svc restart --all" in block
    assert "config: bad field" in block
    own = "You are working inside a disposable hangar sandbox.\n"
    once = supervisor.splice(own, block)
    twice = supervisor.splice(once, supervisor.instructions(_sb(env_build={"version": "v3", "commit": "abc"}),
                                                            eff, "/workspace/app"))
    assert twice.startswith(own) and twice.count(supervisor.BLOCK_START) == 1
    assert "build v3" in twice and "Warning" not in twice


def test_skill_has_frontmatter():
    head = supervisor.SKILL.split("---")[1]
    assert "name: hangar-environment" in head and "description:" in head
    assert "hangar-agent svc" in supervisor.SKILL
