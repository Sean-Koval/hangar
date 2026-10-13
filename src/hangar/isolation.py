"""The uid split: a turn's driver runs as hangar's own user, the agent as the sandbox user (workstream isolation;
docs/steering.md#the-uid-split).

A live turn's driver (driver.py) holds the run's key and speaks for the person answering approvals. In one user the
agent can get at all of it: code it runs (a test suite a person allowed) can read the next turn's key file before the
driver deletes it, edit the turn's script or the driver, or signal and trace the driver. With the split:

    /var/lib/hangar             hangar's home, 0755 hangar
      driver.py                 the driver (0644)
      runs/                     0755: <run>.sh, .env, .prompt (0644, written by hangar), .log, .exit, .pid (the run's)
        <run>.ctl/              0700: the key, in/, acks
      tmux/                     hangar's tmux socket (0700)

The control plane writes there as hangar's user (`put`), and runs the turn as that user: root starts
the turn's tmux through setpriv with CAP_SETUID, CAP_SETGID and CAP_KILL in the ambient set, and the driver starts the
harness through setpriv as the agent, every capability dropped (driver.as_agent). The agent can read the run's files
but not change them, and can't signal, trace or read the memory of anything hangar's user runs. Its own
~/.hangar/runs/<run>.{log,exit,pid,sh} are symlinks into runs/, so whatever reads a run as the agent (status, events,
logs, checkpoints) reads it as before.

Only Docker can do this: its exec picks the uid (runc, gVisor and Kata alike), with no setuid binary in the sandbox.
GKE's sandbox policy runs a pod as one non-root user without capabilities, so there is no second user to switch to;
E2B is not wired up. A sandbox whose agent can become root (an environment with `sudo: true`, the default) gains
nothing from it. Everything else runs as before, in one user, with a warning; HANGAR_UID_SPLIT=required makes `ask`
turns fail there instead. Nothing is built into images: the user is added when a sandbox's first turn starts, so
existing environments get it without a rebuild.
"""

from __future__ import annotations

import asyncio
import io
import logging
import shlex
import socket
import tarfile
import time
from typing import TYPE_CHECKING, Any

from hangar.driver import as_agent
from hangar.models import ExecResult, Sandbox

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.providers.base import Provider

log = logging.getLogger("hangar.isolation")

USER = "hangar"
HOME = "/var/lib/hangar"
RUNS = f"{HOME}/runs"
TMUX = f"TMUX_TMPDIR={HOME}/tmux tmux"
DRIVER = f"{HOME}/driver.py"
CAPS = "+setuid,+setgid,+kill"
LINKED = ("log", "exit", "pid", "sh")  # what the agent's ~/.hangar/runs points at

# Run as root in the sandbox. Exit 3 with a reason: this sandbox stays single-user.
SETUP = r"""u={user}
uid=$(id -u "$u" 2>/dev/null) || {{ echo "there is no user $u"; exit 3; }}
[ "$uid" != 0 ] || {{ echo "the sandbox user is root"; exit 3; }}
for t in setpriv tmux bash; do command -v $t >/dev/null || {{ echo "the image has no $t"; exit 3; }}; done
# any sudo rights at all without a password (sudo -l needs none then): the agent may be root whenever it likes
if setpriv --reuid="$u" --regid="$u" --init-groups sudo -n -l >/dev/null 2>&1; then
  echo "$u can use sudo (the environment has sudo: true)"; exit 3
fi
id -u {hangar} >/dev/null 2>&1 \
  || useradd --system --user-group --no-create-home --home-dir {home} --shell /usr/sbin/nologin {hangar} 2>/dev/null \
  || adduser -S -D -H -h {home} -s /sbin/nologin {hangar} >/dev/null 2>&1 \
  || id -u {hangar} >/dev/null 2>&1 \
  || {{ echo "could not add the user {hangar}"; exit 3; }}
h=$(id -u {hangar})
[ "$h" != 0 ] && [ "$h" != "$uid" ] || {{ echo "user {hangar} has uid $h"; exit 3; }}
for d in {home} {runs} {home}/tmux; do [ ! -L "$d" ] || {{ echo "$d is a symlink"; exit 3; }}; done
install -d -o {hangar} -g "$(id -g {hangar})" -m 0755 {home} {runs} || exit 3
install -d -o {hangar} -g "$(id -g {hangar})" -m 0700 {home}/tmux || exit 3
setpriv --reuid={hangar} --regid={hangar} --init-groups --inh-caps={caps} --ambient-caps={caps} true \
  || {{ echo "setpriv can't keep capabilities for {hangar} (the sandbox drops them)"; exit 3; }}
echo "split $h $(id -g {hangar})"
"""


def split(sb: Sandbox) -> bool:
    """The sandbox runs turns split (decided by `ensure`)."""
    return (sb.info.get("isolation") or {}).get("mode") == "split"


def run_split(sb: Sandbox, run_id: str | None) -> bool:
    """The run was started split. A run is looked up rather than the sandbox, so one that started before the
    sandbox was set up (a control plane upgraded mid-turn) is still stopped and steered where it runs."""
    return any(r.get("id") == run_id and r.get("split") for r in sb.info.get("runs") or [])


def why_not(settings: Settings, sb: Sandbox) -> str:
    """Why turns in the sandbox run single-user."""
    if settings.uid_split == "off":
        return "HANGAR_UID_SPLIT is off"
    return (sb.info.get("isolation") or {}).get("why") or "not set up"


def runs_dir(settings: Settings, split_run: bool) -> str:
    return RUNS if split_run else f"/home/{settings.sandbox_user}/.hangar/runs"


def ctl_dir(settings: Settings, run_id: str, split_run: bool) -> str:
    return f"{runs_dir(settings, split_run)}/{run_id}.ctl"


async def ensure(fleet: Any, prov: Provider, sb: Sandbox) -> bool:
    """Set the sandbox up for the split the first time a turn starts in it, and say whether turns there run split.
    The answer is kept in sb.info["isolation"] (two first turns at once both get the same answer: the setup is
    idempotent). HANGAR_UID_SPLIT=off applies at once, to sandboxes set up before too."""
    if fleet.settings.uid_split == "off":
        return False
    iso = sb.info.get("isolation")
    if iso is None:
        iso = await _probe(fleet.settings, prov, sb)
        sb.info["isolation"] = iso
        fleet.store.put(sb)
        # A turn that asks for approvals says so on the turn itself (steer.prepare); this is for the operator.
        log.info("sandbox %s: turns run %s", sb.id, "split" if iso["mode"] == "split" else
                 f"hangar's driver as the agent's own user ({iso.get('why')})")
    return iso["mode"] == "split"


async def _probe(settings: Settings, prov: Provider, sb: Sandbox) -> dict[str, Any]:
    if prov.name != "docker":
        why = ("Kubernetes sandboxes run as one non-root user without capabilities (GKE Sandbox's policy)"
               if prov.name == "kubernetes" else f"the {prov.name} provider doesn't support it yet")
        return {"mode": "shared", "why": why}
    script = SETUP.format(user=shlex.quote(settings.sandbox_user), hangar=USER, home=HOME, runs=RUNS, caps=CAPS)
    try:
        res = await prov.exec(sb, script, timeout=60, user="root", cwd="/")
    except Exception as e:  # noqa: BLE001 - any failure leaves the sandbox as it was: single-user
        return {"mode": "shared", "why": f"setting it up failed: {e}"[:300]}
    words = res.stdout.split()
    if res.exit_code == 0 and len(words) == 3 and words[0] == "split" and all(w.isdigit() for w in words[1:]):
        return {"mode": "split", "uid": int(words[1]), "gid": int(words[2])}
    why = (res.stdout.strip().splitlines() or [res.stderr.strip()[-200:] or f"exit {res.exit_code}"])[-1]
    return {"mode": "shared", "why": why[:300]}


async def put(prov: Provider, sb: Sandbox, parent: str, files: dict[str, tuple[bytes | None, int]]) -> None:
    """Write files (None: a directory) under `parent` as hangar's user, in one exec: `tar -x` reading the archive
    from its stdin. Written from inside the sandbox, not with Docker's put_archive, which writes the container's root
    filesystem from the host: gVisor (its rootfs overlay) and Kata (a block-device rootfs) may never show that to the
    sandbox. Nothing secret is ever on a command line or in a file the agent can read on the way."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.mode, info.mtime = mode, int(time.time())
            if data is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    from hangar.providers.docker import SYSTEM_PATH

    container = await prov._container(sb)  # type: ignore[attr-defined]
    api = prov.client.api  # type: ignore[attr-defined]
    archive = buf.getvalue()

    def run() -> tuple[int, bytes]:
        eid = api.exec_create(container.id, ["tar", "-xpf", "-", "-C", parent], stdin=True, user=USER, workdir=HOME,
                              environment={"PATH": SYSTEM_PATH})["Id"]
        sock = api.exec_start(eid, socket=True)
        raw = getattr(sock, "_sock", sock)
        try:
            raw.sendall(archive)
            raw.shutdown(socket.SHUT_WR)  # end of stdin: tar finishes
            out = b""
            while chunk := raw.recv(65536):  # tar's messages (multiplexed frames), bounded
                out = (out + chunk)[-65536:]
        finally:
            raw.close()
        for _ in range(100):  # the exit code lands a moment after the stream ends
            st = api.exec_inspect(eid)
            if not st.get("Running"):
                return int(st.get("ExitCode") or 0), out
            time.sleep(0.05)
        return -1, out

    code, out = await asyncio.wait_for(asyncio.to_thread(run), 60)
    if code != 0:
        raise RuntimeError(f"could not write into {parent} in sandbox {sb.id} (tar exit {code}): "
                           f"{out.decode(errors='replace')[-300:]}")


async def sh(prov: Provider, sb: Sandbox, command: str, timeout: int = 60) -> ExecResult:
    """A command as hangar's user (Docker's exec picks the uid; the agent's envd can't run it)."""
    return await prov.exec(sb, command, timeout=timeout, user=USER, cwd=HOME)


def agent_command(settings: Settings, run_id: str, wd: str, command: str) -> str:
    """A shell command for the turn's script that runs `command` (shell) as the agent: a one-shot harness."""
    argv = as_agent(settings.sandbox_user, f"/home/{settings.sandbox_user}", f"{RUNS}/{run_id}.env", wd,
                    ["bash", "-c", command])
    return shlex.join(argv)


def driver_args(settings: Settings, run_id: str, wd: str) -> str:
    """The driver's arguments for a split run: start the harness as the agent, with the run's exports, in `wd`."""
    q = shlex.quote
    return f"--as {q(settings.sandbox_user)} --run-env {q(f'{RUNS}/{run_id}.env')} --cwd {q(wd)}"


async def start(prov: Provider, sb: Sandbox, settings: Settings, run_id: str, prompt: str, exports: str,
                command: str) -> ExecResult:
    """Start a split run: its files under RUNS, the agent's links to them, and its tmux session as hangar's user.
    `exports` are the run's `export` lines; `command` the turn's (the driver, or `agent_command`)."""
    from hangar.providers.docker import SYSTEM_PATH

    q = shlex.quote
    r = f"{RUNS}/{run_id}"
    # The agent's PATH is the image's (its tools: ~/.local/bin, mise's shims); hangar's side never resolves through it.
    container = await prov._container(sb)  # type: ignore[attr-defined]
    image_path = next((e[5:] for e in (container.attrs.get("Config") or {}).get("Env") or [] if e.startswith("PATH=")),
                      SYSTEM_PATH)
    exports += f"export HANGAR_AGENT_PATH={q(image_path)}\n"
    script = (
        "#!/bin/bash\n"
        f"# hangar run {run_id}, as hangar's own user (the uid split): the agent's harness runs as "
        f"{settings.sandbox_user}\n"
        "umask 022\n"
        f"set -a; . {r}.env; set +a\n"
        f"export PATH={SYSTEM_PATH}\n"
        f"echo $$ > {r}.pid\n"
        f"{{ {command}; }} 2>&1 | tee {r}.log\n"
        f"echo ${{PIPESTATUS[0]}} > {r}.exit\n"
    )
    await put(prov, sb, RUNS, {f"{run_id}.prompt": (prompt.encode(), 0o644), f"{run_id}.env": (exports.encode(), 0o644),
                               f"{run_id}.sh": (script.encode(), 0o644)})
    home_runs = runs_dir(settings, False)
    links = " ".join(f"&& ln -sfn {r}.{x} {run_id}.{x}" for x in LINKED)
    res = await prov.exec(sb, f"mkdir -p {home_runs} && cd {home_runs} {links}", timeout=20)
    if res.exit_code != 0:
        return res
    pane = f"bash {r}.sh; exec sleep infinity"
    # SHELL: tmux runs the pane's command with it, else with the user's login shell (nologin).
    start_tmux = (f"cd {HOME} && TMUX_TMPDIR={HOME}/tmux setpriv --reuid={USER} --regid={USER} --init-groups "
                  f"--inh-caps={CAPS} --ambient-caps={CAPS} -- env HOME={HOME} USER={USER} LOGNAME={USER} "
                  f"SHELL=/bin/sh tmux new-session -d -s {run_id} -x 200 -y 50 -c {HOME} {q(pane)}")
    return await prov.exec(sb, start_tmux, timeout=20, user="root", cwd="/")


async def kill(prov: Provider, sb: Sandbox, settings: Settings, run_id: str, interrupted: bool = False) -> None:
    """Stop a run's tmux session, wherever it runs; `interrupted`: its exit status is 130 unless it had finished."""
    s = run_split(sb, run_id)
    q = shlex.quote
    ended = q(f"{runs_dir(settings, s)}/{run_id}.exit")
    cmd = f"{TMUX if s else 'tmux'} kill-session -t {q(run_id)} 2>/dev/null"
    if interrupted:
        cmd += f"; [ -s {ended} ] || echo 130 > {ended}"
    if s:
        await sh(prov, sb, cmd, timeout=30)
    else:
        await prov.exec(sb, cmd, timeout=30)


def attach_command(sb: Sandbox, session: str) -> list[str] | None:
    """The dashboard's terminal on a split run: a read-only view of its tmux session, as hangar's user."""
    if not run_split(sb, session):
        return None
    return ["env", f"TMUX_TMPDIR={HOME}/tmux", "tmux", "attach-session", "-r", "-t", session]


async def open_terminal(prov: Provider, sb: Sandbox, command: list[str], cols: int, rows: int) -> Any:
    """A terminal as hangar's user (Docker exec; the agent's envd runs only as the agent)."""
    from hangar.providers.docker import SYSTEM_PATH, DockerTerminal

    container = await prov._container(sb)  # type: ignore[attr-defined]
    api = prov.client.api  # type: ignore[attr-defined]
    ex = await asyncio.to_thread(api.exec_create, container.id, command, stdin=True, tty=True, user=USER, workdir=HOME,
                                 environment={"TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows),
                                              "PATH": SYSTEM_PATH})
    sock = await asyncio.to_thread(api.exec_start, ex["Id"], tty=True, socket=True)
    term = DockerTerminal(prov.client, ex["Id"], sock)  # type: ignore[attr-defined]
    await term.resize(cols, rows)
    return term
