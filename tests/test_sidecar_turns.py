"""Turns through the driver sidecar on Kubernetes (KAN-186; docs/security/driver-sidecar.md).

The probe (what a pod must look like before `required` lets a turn that asks run), the provider's pods/exec into the
driver's container, and live turns end to end on a fake Kubernetes provider with two containers: the agent's sees
hangar-home (/var/lib/hangar) but not hangar-ctl (/var/lib/hangar-ctl); the driver's sees both. Real tmux, the real
driver and relay, and the scripted harness of tests/test_steer.py. What a real pod enforces (read-only mounts, uids,
PID namespaces, gVisor) is the manual check on that page and scripts/k8s/check-sidecar-turn.sh.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hangar import harden, isolation
from hangar import steer as steering
from hangar.config import Settings
from hangar.harnesses import HARNESSES
from hangar.models import ExecResult, Sandbox, SessionTurn
from tests.conftest import FakeProvider, remap
from tests.test_steer import live, new, pending_approval, repo_file, settle, svc  # noqa: F401 - fixtures

PYTHON = shutil.which("python3") or sys.executable


# ====================================================================== the probe
class Scripted:
    """A Kubernetes provider whose pod spec and probe answers are given."""

    name = "kubernetes"

    def __init__(self, why=None, readonly=(HARNESSES["claude-code"].prefix,), driver="1001\n", agent=None,
                 agent_code=0):
        self.why, self.readonly, self.driver, self.agent_code = why, list(readonly), driver, agent_code
        b = isolation.live_bin(HARNESSES["claude-code"])
        self.agent = agent if agent is not None else f"python /usr/bin/python3\nharness ok\nreal {b}\nagent 1000\n"
        self.raw: list[tuple[list[str], str | None]] = []

    async def sidecar_pod(self, sb):
        return {"why": self.why, "readonly": self.readonly}

    async def exec_raw(self, sb, argv, timeout, stdin=None, user=None):
        self.raw.append((argv, user))
        if user == isolation.USER:
            return ExecResult(exit_code=0, stdout=self.driver, stderr="")
        return ExecResult(exit_code=self.agent_code, stdout=self.agent, stderr="")

    async def exec(self, *a, **kw):
        raise AssertionError("the probe never goes through envd or a login shell")


def _fleet(**kw):
    return SimpleNamespace(settings=Settings(k8s_driver_sidecar=True, **kw), store=SimpleNamespace(put=lambda sb: None))


def _sb():
    return Sandbox(name="s", environment="e", provider="kubernetes", info={"harness": "claude-code"})


async def test_a_pod_with_the_sidecar_runs_split_and_hangar_can_own_the_harness():
    p, sb = Scripted(), _sb()
    assert await isolation.ensure(_fleet(), p, sb) is True
    iso = sb.info["isolation"]
    assert iso == {"mode": "split", "backend": "sidecar", "uid": 1001, "agent_uid": 1000, "v": isolation.VERSION,
                   "python": "/usr/bin/python3", "harness": isolation.live_bin(HARNESSES["claude-code"])}
    assert isolation.sidecar(sb) and isolation.pinned(sb)
    (dargv, duser), (aargv, auser) = p.raw
    assert duser == isolation.USER and auser is None  # one check in each container, as its own user
    assert aargv[:2] == ["sh", "-c"] and f"[ ! -e {isolation.CTL} ]" in aargv[2] and "managed settings" in aargv[2]


@pytest.mark.parametrize("prov,why", [
    (Scripted(why="the pod shares one PID namespace between its containers"), "shares one PID namespace"),
    (Scripted(driver="1000\n", agent="python /p\nharness ok\nreal /r\nagent 1000\n"), "runs as uid 1000"),
    (Scripted(driver="0\n"), "runs as uid 0"),
    (Scripted(driver="1001\nthe driver's container has no tmux\n"), "has no tmux"),
    (Scripted(agent="the agent's container can see /var/lib/hangar-ctl\n", agent_code=3),
     "can see /var/lib/hangar-ctl"),
    (Scripted(agent="the agent can write /var/lib/hangar\n", agent_code=3), "can write /var/lib/hangar"),
])
async def test_without_the_sidecars_shape_turns_stay_single_user_and_say_why(prov, why):
    sb = _sb()
    assert await isolation.ensure(_fleet(), prov, sb) is False
    assert sb.info["isolation"]["mode"] == "shared" and why in sb.info["isolation"]["why"]
    assert "the driver sidecar can't be used" in isolation.why_not(Settings(), sb)


async def test_a_harness_off_its_read_only_pack_is_not_hangars_to_own():
    sb = _sb()
    assert await isolation.ensure(_fleet(), Scripted(readonly=[]), sb) is True  # split, but the harness isn't pinned
    assert not isolation.pinned(sb) and "isn't on a read-only volume" in sb.info["isolation"]["harness_why"]
    sb = _sb()
    b = isolation.live_bin(HARNESSES["claude-code"])
    agent = f"python /usr/bin/python3\nharness the agent can change /etc/claude-code (managed settings)\nreal {b}\n" \
            "agent 1000\n"
    assert await isolation.ensure(_fleet(), Scripted(agent=agent), sb) is True
    assert "managed settings" in sb.info["isolation"]["harness_why"]


async def test_the_sidecar_setting_off_leaves_kubernetes_single_user():
    sb = _sb()
    f = SimpleNamespace(settings=Settings(), store=SimpleNamespace(put=lambda sb: None))
    assert await isolation.ensure(f, Scripted(), sb) is False
    assert "HANGAR_K8S_DRIVER_SIDECAR is off" in sb.info["isolation"]["why"]


def test_graders_stay_off_the_sidecar():
    """KAN-188: the uid split's grade wrapper is Docker's; on Kubernetes a grade stays the sandbox user's."""
    assert not isolation.supported(Scripted())


# ====================================================================== the pod, read through the API
def _pod(**settings):
    pytest.importorskip("kubernetes")
    from hangar.providers.kubernetes import DRIVER_CONTAINER
    from tests.test_kubernetes import provider, spec

    tmpl = provider(k8s_driver_sidecar=True, **settings)._pod_template(spec(packs={"claude-code": "reg/p@sha256:1"}),
                                                                        "i")
    return {"spec": tmpl["spec"], "status": {"containerStatuses": [{"name": "sandbox", "ready": True},
                                                                   {"name": DRIVER_CONTAINER, "ready": True}]}}


def test_the_pod_hangar_makes_has_the_shape_the_probe_wants():
    from hangar.harnesses import PACK_ROOT
    from hangar.providers.kubernetes import sidecar_shape

    got = sidecar_shape(_pod())
    assert got["why"] is None and f"{PACK_ROOT}/claude-code" in got["readonly"]
    assert isolation.HOME in got["readonly"]


def _break(fn):
    pod = _pod()
    fn(pod["spec"], pod["status"])
    return pod


@pytest.mark.parametrize("change,why", [
    (lambda s, st: s["containers"].pop(), "has no hangar-driver container"),
    (lambda s, st: s.update(shareProcessNamespace=True), "one PID namespace"),
    (lambda s, st: s["containers"][0]["volumeMounts"].append({"name": "hangar-ctl", "mountPath": "/x"}),
     "over the control volume"),
    (lambda s, st: s["containers"][0]["volumeMounts"][-1].update(readOnly=False), "without readOnly"),
    (lambda s, st: s["containers"][1]["securityContext"].update(runAsUser=1000), "isn't its own"),
    (lambda s, st: s["containers"][0]["securityContext"].update(allowPrivilegeEscalation=True), "may escalate"),
    (lambda s, st: st["containerStatuses"][1].update(ready=False), "isn't ready"),
    (lambda s, st: s["volumes"][-1].pop("emptyDir"), "aren't emptyDirs"),
])
def test_a_pod_out_of_shape_is_refused(change, why):
    from hangar.providers.kubernetes import sidecar_shape

    assert why in (sidecar_shape(_break(change))["why"] or "")


class _WS:
    """The exec websocket: records its container, command and stdin; answers with a scripted exit."""

    def __init__(self, kw, code=0, out=""):
        self.kw, self.code, self.out, self.stdin, self.open = kw, code, out, [], True
        self.returncode = None

    def write_stdin(self, data):
        self.stdin.append(data)

    def update(self, timeout=0):
        self.open, self.returncode = False, self.code

    def is_open(self):
        return self.open

    def peek_stdout(self):
        return bool(self.out)

    def read_stdout(self):
        out, self.out = self.out, ""
        return out

    def peek_stderr(self):
        return False

    def write_channel(self, *a):
        pass

    def close(self):
        pass


@pytest.fixture
def k8s(monkeypatch):
    pytest.importorskip("kubernetes")
    import hangar.providers.kubernetes as k
    from tests.test_kubernetes import provider

    p = provider(k8s_driver_sidecar=True)
    p.core_exec = type("Core", (), {"connect_get_namespaced_pod_exec": "exec"})()

    class Envd:
        calls = 0

        async def exec(self, *a):
            Envd.calls += 1
            return ExecResult(exit_code=0, stdout="envd", stderr="")

        async def open_terminal(self, *a):
            Envd.calls += 1

    p.envd = Envd()
    streams: list[_WS] = []

    def fake_stream(fn, pod, ns, **kw):
        streams.append(_WS(kw, out="hangar-relay: started 7\n"))
        return streams[-1]

    monkeypatch.setattr(k, "k8s_stream", fake_stream)
    return SimpleNamespace(p=p, streams=streams, envd=Envd, sb=Sandbox(name="a", environment="py",
                                                                         provider="kubernetes", info={"pod": "pod-1"}))


async def test_hangars_user_is_the_driver_container_and_never_envd(k8s):
    from hangar.providers.kubernetes import DRIVER_CONTAINER, DRIVER_USER

    assert DRIVER_USER == isolation.USER
    res = await isolation.sh(k8s.p, k8s.sb, "id -u")
    assert res.exit_code == 0 and k8s.envd.calls == 0
    (ws,) = k8s.streams
    assert ws.kw["container"] == DRIVER_CONTAINER and ws.kw["command"][-1].startswith("cd /var/lib/hangar ")
    assert (await k8s.p.exec(k8s.sb, "true")).stdout == "envd" and k8s.envd.calls == 1  # the agent's: as before
    await isolation.open_terminal(k8s.p, k8s.sb, ["tmux", "attach"], 80, 24)
    assert k8s.streams[-1].kw["container"] == DRIVER_CONTAINER and k8s.envd.calls == 1


async def test_put_streams_a_tar_into_the_driver_container(k8s):
    from hangar.providers.kubernetes import DRIVER_CONTAINER

    await isolation.put(k8s.p, k8s.sb, isolation.CTL_RUNS, {"r.ctl": (None, 0o700), "r.ctl/key": (b"k" * 64, 0o600)})
    (ws,) = k8s.streams
    assert ws.kw["container"] == DRIVER_CONTAINER and ws.kw["stdin"] is True
    script = ws.kw["command"][-1]
    assert f"-C {isolation.CTL_RUNS}" in script and "head -c" in script and "k" * 64 not in script
    names = tarfile.open(fileobj=io.BytesIO(base64.b64decode("".join(ws.stdin)))).getnames()
    assert names == ["r.ctl", "r.ctl/key"]


async def test_the_relay_gets_its_token_on_stdin_only(k8s):
    from hangar.providers.kubernetes import CONTAINER

    res = await k8s.p.exec_raw(k8s.sb, ["/usr/bin/python3", "-I", isolation.RELAY, "/p"], 5, stdin=b"tok\n")
    (ws,) = k8s.streams
    assert ws.kw["container"] == CONTAINER and ws.kw["command"] == ["/usr/bin/python3", "-I", isolation.RELAY, "/p"]
    assert ws.stdin == ["tok\n"] and res.stdout.startswith("hangar-relay: started") and k8s.envd.calls == 0


async def test_without_the_setting_nothing_execs_as_hangars_user(k8s):
    from hangar.providers.base import ProviderError

    k8s.p.settings.k8s_driver_sidecar = False
    with pytest.raises(ProviderError, match="needs the driver sidecar"):
        await isolation.sh(k8s.p, k8s.sb, "true")


def test_a_sidecar_runs_control_files_and_tmux_are_out_of_the_agents_reach():
    sb = Sandbox(name="s", environment="e", provider="kubernetes",
                 info={"isolation": {"mode": "split", "backend": "sidecar"},
                       "runs": [{"id": "run-0000beef", "split": True, "sidecar": True}]})
    assert isolation.ctl_dir(Settings(), "run-0000beef", True, True) == f"{isolation.CTL_RUNS}/run-0000beef.ctl"
    assert isolation.run_sidecar(sb, "run-0000beef") and not isolation.run_sidecar(sb, "run-0000cafe")
    assert isolation.tmux(sb) == f"TMUX_TMPDIR={isolation.CTL}/tmux tmux"
    assert isolation.attach_command(sb, "run-0000beef")[1] == f"TMUX_TMPDIR={isolation.CTL}/tmux"


# ====================================================================== live turns on two fake containers
class FakeSidecar(FakeProvider):
    """Two containers per fake sandbox. The agent's: /var/lib/hangar is the box's hangar-home, /var/lib/hangar-ctl
    doesn't exist. The driver's (exec as hangar's user, or pods/exec with user=hangar): both volumes. The harness
    prefix maps to a directory whose bin/python3 is this Python."""

    name = "kubernetes"

    def __init__(self, prefix: Path):
        super().__init__()
        self.prefix = prefix
        self.log: list[tuple[str, str, bytes | None]] = []  # (container, command or argv, stdin)
        self.ctl_dirs: list[Path] = []  # each box's hangar-ctl, outside the box (see create)

    async def create(self, sandbox, image, spec=None):
        await super().create(sandbox, image, spec)
        box = self.boxes[sandbox.id]
        (box / "vol/hangar").mkdir(parents=True)
        # hangar-ctl holds the driver's tmux socket, and a unix socket path can't pass 107 bytes: under a long test
        # root (CI's ~/.cache/hangar-tests/gw0-…/fake-…/sb-…) the box path alone uses most of that. So the volume is
        # a symlink to a short directory; tmux resolves TMUX_TMPDIR's real path before naming its socket.
        ctl = Path(tempfile.mkdtemp(prefix="hc-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None))
        ctl.chmod(0o755)
        self.ctl_dirs.append(ctl)
        (box / "vol/hangar-ctl").symlink_to(ctl)

    def view(self, sb: Sandbox, text: str, driver: bool) -> str:
        box = self.boxes[sb.provider_ref]
        ctl = f"{box}/vol/hangar-ctl" if driver else f"{box}/agent-root/var/lib/hangar-ctl"
        text = remap(text, str(box))
        text = text.replace(isolation.CTL, "\x02").replace(isolation.HOME, f"{box}/vol/hangar").replace("\x02", ctl)
        return text.replace(HARNESSES["live-claude"].prefix, str(self.prefix))

    def _map(self, sb, path):
        if path.startswith(isolation.HOME) or path.startswith(isolation.CTL):
            return Path(self.view(sb, path, False))
        return super()._map(sb, path)

    def _env(self, sb, driver):
        box = self.boxes[sb.provider_ref]
        return {"HOME": f"{box}/vol/hangar" if driver else f"{box}/home/agent", "PATH": "/usr/bin:/bin:/usr/local/bin",
                "TMUX_TMPDIR": f"{box}/tmp"}

    async def _run(self, sb, argv, driver, stdin=None, cwd=None):
        p = await asyncio.create_subprocess_exec(*argv, env=self._env(sb, driver), stdin=subprocess.PIPE,
                                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                 cwd=cwd or str(self.boxes[sb.provider_ref]))
        out, err = await p.communicate(stdin)
        return ExecResult(exit_code=p.returncode, stdout=out.decode(errors="replace"),
                          stderr=err.decode(errors="replace"))

    async def exec(self, sandbox, command, timeout=120, cwd=None, user=None):
        if user != isolation.USER:
            self.log.append(("sandbox", command, None))
            return await super().exec(sandbox, command, timeout, cwd, user)
        self.log.append(("hangar-driver", command, None))
        return await self._run(sandbox, ["bash", "-c", self.view(sandbox, command, True)], True,
                               cwd=self.view(sandbox, cwd or isolation.HOME, True))

    async def exec_raw(self, sandbox, argv, timeout, stdin=None, user=None):
        driver = user == isolation.USER
        self.log.append(("hangar-driver" if driver else "sandbox", " ".join(argv), stdin))
        script = argv[-1]
        if driver and script == isolation.SIDECAR_DRIVER:
            return ExecResult(exit_code=0, stdout="1001\n", stderr="")  # one user here: say what a pod would
        if not driver and "hangar-probe" in script:
            b = isolation.live_bin(HARNESSES["live-claude"])
            return ExecResult(exit_code=0, stdout=f"python {PYTHON}\nharness ok\nreal {b}\nagent 1000\n", stderr="")
        if driver and "tar -xpf" in script:  # isolation.put: unpacked as the driver's container would, paths mapped
            parent = Path(self.view(sandbox, script.rsplit("-C ", 1)[1].strip("'"), True))
            parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(stdin or b""))) as tf:
                for m in tf.getmembers():
                    dest = parent / m.name
                    if m.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        data = tf.extractfile(m).read()  # type: ignore[union-attr]
                        try:
                            data = self.view(sandbox, data.decode(), True).encode()
                        except UnicodeDecodeError:
                            pass
                        dest.write_bytes(data)
                    dest.chmod(m.mode)
            return ExecResult(exit_code=0, stdout="", stderr="")
        return await self._run(sandbox, [self.view(sandbox, a, driver) for a in argv], driver, stdin)

    async def sidecar_pod(self, sb):
        return {"why": None, "readonly": [HARNESSES["live-claude"].prefix, isolation.HOME]}

    def cleanup(self) -> None:
        for tmp in self.root.glob("*/vol/hangar-ctl/tmux"):
            subprocess.run(["tmux", "kill-server"], capture_output=True, timeout=10,
                           env={"TMUX_TMPDIR": str(tmp), "PATH": "/usr/bin:/bin:/usr/local/bin"})
        super().cleanup()
        for ctl in self.ctl_dirs:
            shutil.rmtree(ctl, ignore_errors=True)


@pytest.fixture
def side(fleet, settings, tmp_path):
    """The fleet's provider becomes FakeSidecar, with the sidecar setting on (request it before `svc`)."""
    pfx = tmp_path / "pfx"
    (pfx / "bin").mkdir(parents=True)
    (pfx / "bin" / "python3").symlink_to(PYTHON)
    settings.k8s_driver_sidecar = True
    old = fleet.providers["docker"]
    fleet.providers["docker"] = prov = FakeSidecar(pfx)
    yield prov
    fleet.providers["docker"] = old


def _agent_visible(prov: FakeSidecar, sb: Sandbox) -> list[Path]:
    box = prov.boxes[sb.id]
    return [p for p in box.rglob("*") if p.is_file() and not p.is_relative_to(box / "vol/hangar-ctl")]


async def _running(svc, prov: FakeSidecar, ref: str, timeout: float = 40.0):  # noqa: F811
    """The session's turn runs under its driver: the inbox in the driver's control directory exists."""
    for _ in range(int(timeout / 0.05)):
        s = svc.get(ref)
        t = s.turns[-1] if s.turns else None
        if t and t.run_id and s.state.value == "running" and s.sandbox_id:
            if (prov.boxes[s.sandbox_id] / "vol/hangar-ctl/runs" / f"{t.run_id}.ctl/in").is_dir():
                return s
        await asyncio.sleep(0.05)
    raise AssertionError(f"{ref} never started a turn: {svc.get(ref).reason}")


async def _sandbox(svc, live, name: str):  # noqa: F811
    new(svc, live, name, "hello")
    s = await settle(svc, name)
    return s, svc.fleet.get(s.sandbox_id)


async def test_a_turn_that_asks_writes_its_key_where_only_the_driver_can_read_it(side, svc, live,  # noqa: F811
                                                                                 settings):
    s, sb = await _sandbox(svc, live, "sk")
    assert not s.turns[0].approval_policy or s.turns[0].approval_policy == "auto"
    assert not any(r.get("split") for r in sb.info["runs"])  # an `auto` turn: the agent's container, as before
    svc.fleet.broker.vault.put(harden.VAULT + sb.id, json.dumps({"env": "export SECRET_ENV=s3cret\n",
                                                                 "claude_mcp": None}))
    settings.uid_split = "required"
    s.approvals = "ask"
    turn = SessionTurn(message="x")
    run_id, command = await steering.prepare(svc, s, turn, sb, HARNESSES["live-claude"])
    try:
        assert isolation.sidecar(sb) and not turn.warnings
        ctl = f"{isolation.CTL_RUNS}/{run_id}.ctl"
        assert f"--ctl {ctl} " in command and f"--relay {isolation.RUNS}/{run_id}.port --agent agent " in command
        assert f"--base-env {ctl}/agent-env" in command and "--setting-sources ''" in command
        key = svc.vault.decrypt(svc.store.kv_get(steering.KV + run_id).encode())
        box = side.boxes[sb.id]
        assert (box / "vol/hangar-ctl/runs" / f"{run_id}.ctl/key").read_bytes() == key  # the driver's container
        token = (box / "vol/hangar-ctl/runs" / f"{run_id}.ctl/relay-token").read_text().strip()
        assert token and isolation.has_relay(run_id)
        res = await side.exec(sb, f"cat {ctl}/key {ctl}/relay-token {ctl}/agent-env")  # the agent's container
        assert res.exit_code != 0 and key.decode() not in res.stdout
        for p in _agent_visible(side, sb):  # nowhere the agent's container can see
            data = p.read_bytes()
            assert key not in data and token.encode() not in data and b"s3cret" not in data, p
        # hangar's harness settings are on hangar-home, which the agent's container mounts read-only
        assert (box / "vol/hangar/runs" / f"{run_id}.settings.json").exists()
        assert (box / "vol/hangar/relay.py").read_bytes() == steering.RELAY.read_bytes()
    finally:
        steering.forget(svc, run_id)
    assert not isolation.has_relay(run_id)


async def test_auto_turns_stay_in_the_agents_container_under_the_sidecar(side, svc, live):  # noqa: F811
    """KAN-187: per-edit trees and the verify gate need the checkout: an `auto` turn keeps them."""
    s, sb = await _sandbox(svc, live, "sa")
    run_id, command = await steering.prepare(svc, s, SessionTurn(message="x"), sb, HARNESSES["live-claude"])
    steering.forget(svc, run_id)
    assert "--relay" not in command and "--as " not in command and isolation.sidecar(sb)


async def test_an_ask_turn_runs_through_the_sidecar_end_to_end(side, svc, live, settings):  # noqa: F811
    settings.uid_split = "required"
    new(svc, live, "se", "TOOL make the file", approvals="ask")
    a = await pending_approval(svc, "se")
    s = svc.get("se")
    sb = svc.fleet.get(s.sandbox_id)
    t = s.turns[0]
    assert t.live and isolation.run_sidecar(sb, t.run_id) and not t.warnings
    assert "touch tool-ran.txt" in a.detail and not repo_file(svc, s, "tool-ran.txt").exists()
    # the relay was started as the agent, by its interpreter's path, its token on stdin and nowhere else
    relay = [x for x in side.log if x[0] == "sandbox" and isolation.RELAY in x[1]]
    assert len(relay) == 1 and relay[0][1].startswith(f"{PYTHON} -I {isolation.RELAY} ") and relay[0][2]
    token = relay[0][2].strip()
    assert not any(token.decode() in cmd for _, cmd, _ in side.log)
    # the run's tmux is the driver's, its socket in hangar-ctl: the dashboard's view of it reads there
    screen = await svc.fleet.terminal_read(sb.id, t.run_id)
    assert isinstance(screen, str)
    done = await steering.answer(svc, s, a.id, "allow", "ana")  # a signed command into the driver's inbox
    assert (done.status, done.by) == ("allowed", "ana")
    s = await settle(svc, "se")
    t = s.turns[0]
    assert repo_file(svc, s, "tool-ran.txt").exists()  # the harness ran in the agent's checkout
    assert t.outcome == "exited" and t.exit_code == 0 and [x.status for x in t.approvals] == ["allowed"], t.summary
    kinds = [(e["kind"], e["data"].get("status")) for e in svc.events("se") if e["kind"] == "approval_request"]
    assert ("approval_request", "pending") in kinds, kinds  # from the driver's own file in hangar-ctl
    trees = [e["data"] for e in svc.events("se") if e["data"].get("action") == "tree"]
    assert any(d.get("skipped") == "sidecar" for d in trees), trees  # KAN-187: said, not silently off
    box = side.boxes[sb.id]
    assert (box / "vol/hangar/runs" / f"{t.run_id}.exit").read_text().strip() == "0"  # hangar-home, the driver's
    assert not (box / "vol/hangar-ctl/runs" / f"{t.run_id}.ctl/agent-env").exists()  # gone with the run
    assert not (box / "vol/hangar-ctl/runs" / f"{t.run_id}.ctl/key").exists()  # read once, then in memory only


async def test_steer_and_interrupt_reach_a_sidecar_turn(side, svc, live, settings):  # noqa: F811
    settings.uid_split = "required"
    new(svc, live, "ss", "SLOW build the thing", approvals="deny-risky")
    s = await _running(svc, side, "ss")
    sb = svc.fleet.get(s.sandbox_id)
    assert isolation.run_sidecar(sb, s.turns[-1].run_id)
    st = await steering.steer(svc, s, "also add a test", "ana")
    assert st.status == "delivered"
    s = await settle(svc, "ss")
    assert s.turns[0].summary == "did: SLOW build the thing | steered: also add a test", s.turns[0].summary
    svc.send("ss", "SLOW second", requester="ana")
    s = await _running(svc, side, "ss")
    await steering.interrupt(svc, s, "ana")
    s = await settle(svc, "ss")
    assert (s.turns[-1].outcome, s.turns[-1].exit_code) == ("interrupted", 130), s.turns[-1].summary


async def test_a_relay_that_cant_start_fails_the_turn_and_stops_its_driver(side, svc, live, settings,  # noqa: F811
                                                                           monkeypatch):
    s, sb = await _sandbox(svc, live, "sf")
    sb.info.pop("isolation", None)
    await isolation.ensure(svc.fleet, side, sb)
    sb.info["isolation"]["python"] = "/nonexistent/python3"
    svc.fleet.store.put(sb)
    s.approvals = "ask"
    svc.send("sf", "TOOL make the file", requester="ana")
    s = await settle(svc, "sf", timeout=60)
    t = s.turns[-1]
    assert t.run_id is None and "the agent's relay didn't start" in (s.reason or ""), (s.state, s.reason)
    assert not os.listdir(side.boxes[sb.id] / "vol/hangar-ctl/tmux") or subprocess.run(
        ["tmux", "list-sessions"], capture_output=True, text=True,
        env={"TMUX_TMPDIR": str(side.boxes[sb.id] / "vol/hangar-ctl/tmux"), "PATH": "/usr/bin:/bin"}).stdout == ""
