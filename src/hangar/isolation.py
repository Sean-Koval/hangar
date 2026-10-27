"""The uid split: a turn's driver runs as hangar's own user, the agent as the sandbox user (workstream isolation;
docs/steering.md#the-uid-split).

A live turn's driver (driver.py) holds the run's key and speaks for the person answering approvals. In one user the
agent can get at all of it: code it runs (a test suite a person allowed) can read the next turn's key file before the
driver deletes it, edit the turn's script or the driver, or signal and trace the driver. With the split:

    /var/lib/hangar             hangar's home, 0755 hangar
      driver.py                 the driver (0644)
      runs/                     0755: <run>.sh, .env, .prompt (0644, written by hangar), .log, .exit, .pid (the run's)
        <run>.ctl/              0700: the key, in/, acks, approvals (the driver's copy of its requests and answers)
      tmux/                     hangar's tmux socket (0700)

The control plane writes there as hangar's user (`put`), and runs the turn as that user: root starts
the turn's tmux through setpriv with CAP_SETUID, CAP_SETGID and CAP_KILL in the ambient set, and the driver starts the
harness through setpriv as the agent, every capability dropped and no_new_privs set (driver.as_agent). The agent can
read the run's files but not change them, and can't signal, trace or read the memory of anything hangar's user runs.
Its own ~/.hangar/runs/<run>.{log,exit,pid,sh} are symlinks into runs/, so what reads a run as the agent reads it as
before; hangar itself reads a split run's status, log and approvals from runs/ as its own user (--- harden: the links
are the agent's to point anywhere). For a turn that asks, hangar also owns the harness's binary, environment and
settings (harden.py), where the setup found the harness and the managed settings out of the agent's reach.

Docker does this with one container: its exec picks the uid (runc, gVisor and Kata alike), with no setuid binary in
the sandbox. GKE's sandbox policy runs a container as one non-root user without capabilities, so there is no second
user to switch to. There the driver gets a container of its own instead, the driver sidecar (KAN-186; Kubernetes with
HANGAR_K8S_DRIVER_SIDECAR, docs/security/driver-sidecar.md): `sh`, `put`, `start`, `kill` and the terminals exec into
the `hangar-driver` container, and the harness starts in the agent's through relay.py. The layout is the same, except
that each run's control directory, and hangar's tmux socket, are in CTL, which the agent's container doesn't mount.
Only turns that ask (`ask`, `deny-risky`, `governed`) run there: an `auto` turn keeps its per-edit trees and verify
gate in the agent's container (KAN-187). E2B is not wired up.
A sandbox whose agent can become root (an environment that sets `sudo: true`; the default is false since #124)
gains nothing from it, unless the sandbox runs with no-new-privileges (a job's on Docker under
HANGAR_INTAKE_POSTURE=enforce, posture.no_new_privs), where sudo fails. Everything else runs as before, in one user,
with a warning; HANGAR_UID_SPLIT=required makes `ask` and `deny-risky` turns fail there instead.
Nothing is built into images: the user is added when a sandbox's first turn starts, so existing environments get it
without a rebuild.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shlex
import socket
import tarfile
import time
from typing import TYPE_CHECKING, Any

from hangar.driver import as_agent
from hangar.models import ExecResult, Sandbox

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.harnesses import Harness
    from hangar.providers.base import Provider

log = logging.getLogger("hangar.isolation")

USER = "hangar"
HOME = "/var/lib/hangar"
RUNS = f"{HOME}/runs"
TMUX = f"TMUX_TMPDIR={HOME}/tmux tmux"
DRIVER = f"{HOME}/driver.py"
CAPS = "+setuid,+setgid,+kill"
LINKED = ("log", "exit", "pid", "sh")  # what the agent's ~/.hangar/runs points at
# --- harden: the setup's checks changed (2: sudo under requiretty, the system PATH, the harness and managed settings;
# 3: the driver sidecar, KAN-186); a sandbox whose answer was cached under another version is probed again
VERSION = 3
# --- KAN-186: the driver sidecar (providers/kubernetes.py, add_driver_sidecar)
SIDECAR = "sidecar"  # sb.info["isolation"]["backend"]
CTL = "/var/lib/hangar-ctl"  # kubernetes.DRIVER_CTL: in memory, mounted only in the driver's container
CTL_RUNS = f"{CTL}/runs"  # each split run's <run>.ctl/ there: the key, relay-token, agent-env, in/, acks, approvals
RELAY = f"{HOME}/relay.py"  # the agent's end, read-only in its container
# What starts the relay, and what the relay starts the harness with, is found on this PATH in the agent's container:
# the probe checks the agent can't write any of it (Docker's SYSTEM_PATH, the image's own directories)
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
RELAY_TIMEOUT = 90  # the relay waits up to 60 s for the driver (driver.RELAY_WAIT), then connects
_RELAY_TOKENS: dict[str, str] = {}  # run id -> its relay token, from steer.prepare to start (one process, one turn)
# --- harden: a hardened turn's harness files hold its environment and MCP servers' secrets: only while it runs
CLEANUP = "rm -rf {r}.agent-env {r}.settings.json {r}.mcp.json {r}.memory"

# Run as root in the sandbox. Exit 3 with a reason: this sandbox stays single-user.
SETUP = r"""u={user}
uid=$(id -u "$u" 2>/dev/null) || {{ echo "there is no user $u"; exit 3; }}
[ "$uid" != 0 ] || {{ echo "the sandbox user is root"; exit 3; }}
for t in setpriv tmux bash; do command -v $t >/dev/null || {{ echo "the image has no $t"; exit 3; }}; done
as_agent() {{ setpriv --reuid="$u" --regid="$u" --init-groups "$@" </dev/null 2>&1; }}
# What root and hangar's user run is found on this PATH (the provider's SYSTEM_PATH): none of it may be the agent's.
for p in $(echo "$PATH" | tr : ' ') $(command -v setpriv tmux bash python3 tar timeout env tee); do
  [ ! -e "$p" ] || ! as_agent test -w "$p" >/dev/null || {{ echo "$u can write $p (hangar's commands)"; exit 3; }}
done
# any sudo rights at all without a password (sudo -l needs none then): the agent may be root whenever it likes. Asked
# under a terminal too when sudo wants one (`Defaults requiretty`): the agent can get one (script, tmux).
if command -v sudo >/dev/null; then
  out=$(as_agent sudo -n -l) && {{ echo "$u can use sudo (the environment has sudo: true)"; exit 3; }}
  case $out in *tty*|*terminal*)
    command -v script >/dev/null || {{ echo "sudo wants a terminal, and the image has no script(1)"; exit 3; }}
    out=$(as_agent script -qec "sudo -n -l" /dev/null) && {{ echo "$u can use sudo (with a terminal)"; exit 3; }}
    case $out in *tty*|*terminal*) echo "sudo wants a terminal, and asking it under one failed"; exit 3;; esac;;
  esac
fi
id -u {hangar} >/dev/null 2>&1 \
  || useradd --system --user-group --no-create-home --home-dir {home} --shell /usr/sbin/nologin {hangar} 2>/dev/null \
  || adduser -S -D -H -h {home} -s /sbin/nologin {hangar} >/dev/null 2>&1 \
  || id -u {hangar} >/dev/null 2>&1 \
  || {{ echo "could not add the user {hangar}"; exit 3; }}
h=$(id -u {hangar})
[ "$h" != 0 ] && [ "$h" != "$uid" ] || {{ echo "user {hangar} has uid $h"; exit 3; }}
# --- harden: can hangar own the harness's settings here? Only if the agent can't change the harness's binary, a
# directory above it or anything in its prefix (codex's wrapper runs libexec/), nor the managed settings that outrank
# hangar's own (Claude Code's, Codex's requirements).
changeable() {{ local p=$1; while :; do [ ! -e "$p" ] || ! as_agent test -w "$p" >/dev/null || return 0; \
  [ "$p" = / ] && return 1; p=$(dirname "$p"); done; }}
harness="ok"
b={bin}
if [ -z "$b" ]; then harness="the harness has no structured mode"
elif [ ! -x "$b" ]; then harness="$b is not there (the harness isn't in its pack or baked prefix)"
elif changeable "$b" || changeable "$(readlink -f "$b")"; then harness="$u can change $b"
elif w=$(as_agent sh -c 'find "$0" -writable -print -quit 2>/dev/null' "${{b%/bin/*}}") && [ -n "$w" ]; then
  harness="$u can change $w"
elif [ "$(head -c 2 "$b")" = "#!" ] && grep -q '\$(' "$b"; then  # e.g. codex's wrapper from before
  harness="$b runs commands through the agent's PATH to find its way (an older pack: a new sandbox has the fixed one)"
else
  for p in /etc/claude-code /etc/claude-code/* /etc/claude-code/*/* /etc/codex /etc/codex/*; do
    ! changeable "$p" || {{ harness="$u can change $p (managed settings)"; break; }}
  done
fi
echo "harness $harness"
for d in {home} {runs} {home}/tmux; do [ ! -L "$d" ] || {{ echo "$d is a symlink"; exit 3; }}; done
install -d -o {hangar} -g "$(id -g {hangar})" -m 0755 {home} {runs} || exit 3
install -d -o {hangar} -g "$(id -g {hangar})" -m 0700 {home}/tmux || exit 3
setpriv --reuid={hangar} --regid={hangar} --init-groups --inh-caps={caps} --ambient-caps={caps} true \
  || {{ echo "setpriv can't keep capabilities for {hangar} (the sandbox drops them)"; exit 3; }}
echo "split $h $(id -g {hangar})"
"""


def pinned(sb: Sandbox) -> str | None:
    """--- harden: the harness binary hangar starts by its path in a split sandbox (one the agent can't change), or
    None: hangar can't own the harness's settings there (`harness_why` says why)."""
    iso = sb.info.get("isolation") or {}
    return iso.get("harness") if iso.get("mode") == "split" else None


def live_bin(h: Harness | None) -> str:
    """Where the harness's structured-mode binary is in its pack or baked prefix ("" when it has none)."""
    word = (h.live_run.split() or [""])[0] if h is not None and h.live else ""
    return f"{h.prefix}/bin/{word}" if h is not None and re.fullmatch(r"[A-Za-z0-9._-]+", word) else ""


def split(sb: Sandbox) -> bool:
    """The sandbox runs turns split (decided by `ensure`)."""
    return (sb.info.get("isolation") or {}).get("mode") == "split"


def run_split(sb: Sandbox, run_id: str | None) -> bool:
    """The run was started split. A run is looked up rather than the sandbox, so one that started before the
    sandbox was set up (a control plane upgraded mid-turn) is still stopped and steered where it runs."""
    return any(r.get("id") == run_id and r.get("split") for r in sb.info.get("runs") or [])


def sidecar(sb: Sandbox) -> bool:
    """--- KAN-186: the sandbox's split runs are the driver sidecar's (a Kubernetes pod with `hangar-driver`)."""
    iso = sb.info.get("isolation") or {}
    return iso.get("mode") == "split" and iso.get("backend") == SIDECAR


def run_sidecar(sb: Sandbox, run_id: str | None) -> bool:
    """--- KAN-186: the run was started through the driver sidecar."""
    return any(r.get("id") == run_id and r.get("split") and r.get("sidecar") for r in sb.info.get("runs") or [])


def sidecar_wanted(prov: Provider, settings: Settings) -> bool:
    """--- KAN-186: sandboxes of this provider get the driver sidecar (whether a pod has it is the probe's to say)."""
    return prov.name == "kubernetes" and bool(settings.k8s_driver_sidecar)


def tmux(sb: Sandbox) -> str:
    """hangar's own tmux, for a split run: its socket in CTL under the sidecar, where the agent can't reach it."""
    return f"TMUX_TMPDIR={CTL}/tmux tmux" if sidecar(sb) else TMUX


def relay_token(run_id: str) -> str:
    """--- KAN-186: a new relay token for a sidecar run, kept here until `start` hands it to the relay's stdin."""
    import secrets

    _RELAY_TOKENS[run_id] = secrets.token_hex(32)
    return _RELAY_TOKENS[run_id]


def has_relay(run_id: str | None) -> bool:
    return bool(run_id) and run_id in _RELAY_TOKENS


def drop_relay(run_id: str | None) -> None:
    """The run never started (steer.forget)."""
    _RELAY_TOKENS.pop(run_id or "", None)


def why_not(settings: Settings, sb: Sandbox) -> str:
    """Why turns in the sandbox run single-user."""
    if settings.uid_split == "off":
        return "HANGAR_UID_SPLIT is off"
    return (sb.info.get("isolation") or {}).get("why") or "not set up"


def runs_dir(settings: Settings, split_run: bool) -> str:
    return RUNS if split_run else f"/home/{settings.sandbox_user}/.hangar/runs"


def ctl_dir(settings: Settings, run_id: str, split_run: bool, side: bool = False) -> str:
    """`side`: a sidecar run's, in CTL (KAN-186)."""
    if split_run and side:
        return f"{CTL_RUNS}/{run_id}.ctl"
    return f"{runs_dir(settings, split_run)}/{run_id}.ctl"


async def ensure(fleet: Any, prov: Provider, sb: Sandbox) -> bool:
    """Set the sandbox up for the split the first time a turn starts in it, and say whether turns there run split.
    The answer is kept in sb.info["isolation"] (two first turns at once both get the same answer: the setup is
    idempotent). HANGAR_UID_SPLIT=off applies at once, to sandboxes set up before too."""
    if fleet.settings.uid_split == "off":
        return False
    iso = sb.info.get("isolation")
    if iso is None or iso.get("v") != VERSION:  # --- harden: set up before the checks changed: probe again
        iso = await _probe(fleet.settings, prov, sb)
        sb.info["isolation"] = iso
        fleet.store.put(sb)
        # A turn that asks for approvals says so on the turn itself (steer.prepare); this is for the operator.
        log.info("sandbox %s: turns run %s", sb.id, "split" if iso["mode"] == "split" else
                 f"hangar's driver as the agent's own user ({iso.get('why')})")
    return iso["mode"] == "split"


def supported(prov: Provider) -> bool:
    """Only Docker's exec picks the uid (see the module docstring)."""
    return prov.name == "docker"


async def _probe(settings: Settings, prov: Provider, sb: Sandbox) -> dict[str, Any]:
    if sidecar_wanted(prov, settings):  # --- KAN-186
        return await _probe_sidecar(prov, sb)
    if not supported(prov):
        why = ("Kubernetes sandboxes run as one non-root user without capabilities (GKE Sandbox's policy), and "
               "HANGAR_K8S_DRIVER_SIDECAR is off" if prov.name == "kubernetes"
               else f"the {prov.name} provider doesn't support it yet")
        return {"mode": "shared", "why": why, "v": VERSION}
    from hangar.harnesses import HARNESSES

    binary = live_bin(HARNESSES.get(sb.info.get("harness") or ""))
    script = SETUP.format(user=shlex.quote(settings.sandbox_user), hangar=USER, home=HOME, runs=RUNS, caps=CAPS,
                          bin=shlex.quote(binary))
    try:
        res = await prov.exec(sb, script, timeout=60, user="root", cwd="/")
    except Exception as e:  # noqa: BLE001 - any failure leaves the sandbox as it was: single-user
        return {"mode": "shared", "why": f"setting it up failed: {e}"[:300], "v": VERSION}
    lines = res.stdout.strip().splitlines()
    words = (lines or [""])[-1].split()
    if res.exit_code == 0 and len(words) == 3 and words[0] == "split" and all(w.isdigit() for w in words[1:]):
        harness = next((ln[8:].strip() for ln in lines if ln.startswith("harness ")), "not checked")
        return {"mode": "split", "uid": int(words[1]), "gid": int(words[2]), "v": VERSION,
                **({"harness": binary} if harness == "ok" else {"harness_why": harness[:300]})}
    why = (lines or [res.stderr.strip()[-200:] or f"exit {res.exit_code}"])[-1]
    return {"mode": "shared", "why": why[:300], "v": VERSION}


# --- KAN-186: the driver sidecar's checks, in the agent's container, as the agent, through a plain pods/exec (no
# login shell, no envd: nothing of the agent's runs first). Exit 3 with a reason: turns that ask stay refused.
SIDECAR_AGENT = r"""PATH={path}; export PATH
changeable() {{ p=$1; while :; do if [ -e "$p" ] && [ -w "$p" ]; then return 0; fi; [ "$p" = / ] && return 1; \
p=$(dirname "$p"); done; }}
u=$(id -u)
[ "$u" != 0 ] || {{ echo "the agent's container runs as root"; exit 3; }}
[ ! -e {ctl} ] || {{ echo "the agent's container can see {ctl}"; exit 3; }}
[ -d {home} ] || {{ echo "the agent's container doesn't mount {home}"; exit 3; }}
if ( : > {home}/.hangar-probe-$$ ) 2>/dev/null; then rm -f {home}/.hangar-probe-$$; echo "the agent can write {home}"; \
exit 3; fi
for p in $(echo "$PATH" | tr : ' '); do
  ! changeable "$p" || {{ echo "the agent can write $p (hangar's PATH)"; exit 3; }}
done
for t in python3 env bash; do
  x=$(command -v $t) || {{ echo "the agent's container has no $t"; exit 3; }}
  ! changeable "$x" && ! changeable "$(readlink -f "$x")" || {{ echo "the agent can change $x"; exit 3; }}
done
echo "python $(command -v python3)"
harness=ok
b={bin}
if [ -z "$b" ]; then harness="the harness has no structured mode"
elif [ ! -x "$b" ]; then harness="$b is not there (the harness isn't in its pack)"
elif changeable "$b" || changeable "$(readlink -f "$b")"; then harness="the agent can change $b"
elif w=$(find "${{b%/bin/*}}" -writable -print -quit 2>/dev/null) && [ -n "$w" ]; then harness="the agent can change $w"
elif [ "$(head -c 2 "$b")" = "#!" ] && grep -q '\$(' "$b"; then
  harness="$b runs commands through the agent's PATH to find its way (an older pack: a new sandbox has the fixed one)"
else
  for p in /etc/claude-code /etc/claude-code/* /etc/claude-code/*/* /etc/codex /etc/codex/*; do
    ! changeable "$p" || {{ harness="the agent can change $p (managed settings)"; break; }}
  done
fi
echo "harness $harness"
echo "real $(readlink -f "$b" 2>/dev/null)"
echo "agent $u"
"""
SIDECAR_DRIVER = (f"PATH={SAFE_PATH}; id -u; for t in python3 tmux bash tar base64 head; do command -v $t >/dev/null "
                  f"|| echo \"the driver's container has no $t\"; done; [ -w {HOME} ] && [ -w {CTL} ] "
                  f"|| echo \"the driver's container can't write {HOME} and {CTL}\"")


def _under(path: str, mounts: list[str]) -> bool:
    return bool(path) and any(path == m or path.startswith(m.rstrip("/") + "/") for m in mounts)


async def _probe_sidecar(prov: Provider, sb: Sandbox) -> dict[str, Any]:
    """--- KAN-186: split when the pod has the driver sidecar in the shape that keeps the agent out (read from the
    pod's spec through the API, not from inside it), the two containers run as different users, and the agent's
    container can neither see CTL nor write HOME. Whether hangar can own the harness's settings: the harness binary
    is on a read-only volume (its pack), and the agent can't change it, what starts it, or the managed settings."""
    from hangar.harnesses import HARNESSES

    def shared(why: str) -> dict[str, Any]:
        return {"mode": "shared", "why": f"the driver sidecar can't be used: {why}"[:300], "v": VERSION}

    try:
        pod = await prov.sidecar_pod(sb)  # type: ignore[attr-defined]
        if pod.get("why"):
            return shared(pod["why"])
        drv = await prov.exec_raw(sb, ["sh", "-c", SIDECAR_DRIVER], 30, user=USER)  # type: ignore[attr-defined]
        binary = live_bin(HARNESSES.get(sb.info.get("harness") or ""))
        script = SIDECAR_AGENT.format(path=SAFE_PATH, ctl=CTL, home=HOME, bin=shlex.quote(binary))
        agent = await prov.exec_raw(sb, ["sh", "-c", script], 60)  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001 - any failure leaves the sandbox as it was: single-user
        return shared(f"checking it failed: {e}")
    dlines = drv.stdout.strip().splitlines()
    if drv.exit_code != 0 or not dlines or not dlines[0].strip().isdigit():
        return shared(f"the driver's container didn't answer (exit {drv.exit_code}: "
                      f"{(drv.stderr or drv.stdout).strip()[-200:]})")
    if len(dlines) > 1:
        return shared(dlines[1].strip())
    alines = agent.stdout.strip().splitlines()
    if agent.exit_code != 0 or not alines or not alines[-1].startswith("agent "):
        return shared((alines or [agent.stderr.strip()[-200:] or f"exit {agent.exit_code}"])[-1])
    got = {ln.split(" ", 1)[0]: ln.split(" ", 1)[1].strip() if " " in ln else "" for ln in alines}
    duid, auid = int(dlines[0]), got.get("agent", "")
    if not auid.isdigit() or duid == 0 or duid == int(auid):
        return shared(f"the driver's container runs as uid {duid}, the agent's as {auid}")
    harness = got.get("harness", "not checked")
    readonly = list(pod.get("readonly") or [])
    if harness == "ok" and not (_under(binary, readonly) and _under(got.get("real", ""), readonly)):
        harness = f"{binary} isn't on a read-only volume in the agent's container (a harness pack)"
    return {"mode": "split", "backend": SIDECAR, "uid": duid, "agent_uid": int(auid), "v": VERSION,
            "python": got.get("python") or "/usr/bin/python3",
            **({"harness": binary} if harness == "ok" else {"harness_why": harness[:300]})}


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
    if prov.name == "kubernetes":  # --- KAN-186: the driver sidecar: into its container, through pods/exec's stdin
        await _put_exec(prov, sb, parent, buf.getvalue())
        return
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


async def _put_exec(prov: Provider, sb: Sandbox, parent: str, archive: bytes) -> None:
    """--- KAN-186: `put` into the driver's container. The exec websocket can't end stdin, so tar reads an exact
    count of base64 (as KubernetesProvider._upload_stream_blocking does)."""
    import base64

    data = base64.b64encode(archive)
    q = shlex.quote
    # mkdir: RUNS 0755 (the agent reads its run's files through the read-only mount); CTL isn't mounted there at all
    script = (f"PATH={SAFE_PATH}; umask 022; mkdir -p {q(parent)} && head -c {len(data)} | base64 -d "
              f"| tar -xpf - -C {q(parent)}")
    res = await prov.exec_raw(sb, ["sh", "-c", script], 60, stdin=data, user=USER)  # type: ignore[attr-defined]
    if res.exit_code != 0:
        raise RuntimeError(f"could not write into {parent} in sandbox {sb.id} (tar exit {res.exit_code}): "
                           f"{(res.stderr or res.stdout)[-300:]}")


async def sh(prov: Provider, sb: Sandbox, command: str, timeout: int = 60) -> ExecResult:
    """A command as hangar's user (Docker's exec picks the uid; the agent's envd can't run it). On Kubernetes, in
    the driver sidecar's container (KubernetesProvider.exec maps the user to it)."""
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


def relay_args(settings: Settings, run_id: str, wd: str) -> str:
    """--- KAN-186: the driver's arguments for a sidecar run: the harness starts in the agent's container through
    relay.py, which finds the driver's port in <run>.port (hangar-home, read-only there)."""
    q = shlex.quote
    u = settings.sandbox_user
    return (f"--relay {q(f'{RUNS}/{run_id}.port')} --agent {q(u)} --agent-home {q(f'/home/{u}')} "
            f"--run-env {q(f'{RUNS}/{run_id}.env')} --cwd {q(wd)}")


def _script(settings: Settings, run_id: str, command: str, path: str, ctl: str | None = None) -> str:
    """`ctl`: a sidecar run's control directory, whose `agent-env` goes too when the run ends (KAN-186)."""
    r = f"{RUNS}/{run_id}"
    extra = f"rm -f {shlex.quote(ctl + '/agent-env')}\n" if ctl else ""
    return (
        "#!/bin/bash\n"
        f"# hangar run {run_id}, as hangar's own user (the uid split): the agent's harness runs as "
        f"{settings.sandbox_user}\n"
        "umask 022\n"
        f"set -a; . {r}.env; set +a\n"
        f"export PATH={path}\n"
        f"echo $$ > {r}.pid\n"
        f"{{ {command}; }} 2>&1 | tee {r}.log\n"
        f"echo ${{PIPESTATUS[0]}} > {r}.exit\n"
        f"{CLEANUP.format(r=r)}\n"  # --- harden
        f"{extra}"
    )


async def _links(prov: Provider, sb: Sandbox, settings: Settings, run_id: str) -> ExecResult:
    """The agent's ~/.hangar/runs/<run>.{log,exit,pid,sh}: links to the split run's own files."""
    r = f"{RUNS}/{run_id}"
    home_runs = runs_dir(settings, False)
    links = " ".join(f"&& ln -sfn {r}.{x} {run_id}.{x}" for x in LINKED)
    return await prov.exec(sb, f"mkdir -p {home_runs} && cd {home_runs} {links}", timeout=20)


async def start(prov: Provider, sb: Sandbox, settings: Settings, run_id: str, prompt: str, exports: str,
                command: str) -> ExecResult:
    """Start a split run: its files under RUNS, the agent's links to them, and its tmux session as hangar's user.
    `exports` are the run's `export` lines; `command` the turn's (the driver, or `agent_command`)."""
    if sidecar(sb):  # --- KAN-186
        return await _start_sidecar(prov, sb, settings, run_id, prompt, exports, command)
    from hangar.providers.docker import SYSTEM_PATH

    q = shlex.quote
    r = f"{RUNS}/{run_id}"
    # The agent's PATH is the image's (its tools: ~/.local/bin, mise's shims); hangar's side never resolves through it.
    container = await prov._container(sb)  # type: ignore[attr-defined]
    image_path = next((e[5:] for e in (container.attrs.get("Config") or {}).get("Env") or [] if e.startswith("PATH=")),
                      SYSTEM_PATH)
    exports += f"export HANGAR_AGENT_PATH={q(image_path)}\n"
    script = _script(settings, run_id, command, SYSTEM_PATH)
    await put(prov, sb, RUNS, {f"{run_id}.prompt": (prompt.encode(), 0o644), f"{run_id}.env": (exports.encode(), 0o644),
                               f"{run_id}.sh": (script.encode(), 0o644)})
    res = await _links(prov, sb, settings, run_id)
    if res.exit_code != 0:
        return res
    pane = f"bash {r}.sh; exec sleep infinity"
    # SHELL: tmux runs the pane's command with it, else with the user's login shell (nologin).
    start_tmux = (f"cd {HOME} && TMUX_TMPDIR={HOME}/tmux setpriv --reuid={USER} --regid={USER} --init-groups "
                  f"--inh-caps={CAPS} --ambient-caps={CAPS} -- env HOME={HOME} USER={USER} LOGNAME={USER} "
                  f"SHELL=/bin/sh tmux new-session -d -s {run_id} -x 200 -y 50 -c {HOME} {q(pane)}")
    return await prov.exec(sb, start_tmux, timeout=20, user="root", cwd="/")


async def _start_sidecar(prov: Provider, sb: Sandbox, settings: Settings, run_id: str, prompt: str, exports: str,
                         command: str) -> ExecResult:
    """--- KAN-186: a sidecar run. Its files go to hangar-home (read-only in the agent's container) and its tmux
    session runs in the driver's container, as the driver's uid, its socket in CTL. Then hangar starts relay.py in
    the agent's container, as the agent, through pods/exec with the run's relay token on stdin: never on a command
    line or in a file. The relay is started by its interpreter's path (the probe checked the agent can't change it),
    with no shell or envd in between that could read its stdin first. It connects to the driver and detaches.
    HANGAR_AGENT_PATH isn't set: the relay's environment is the container's own, so the harness gets the image's
    PATH."""
    q = shlex.quote
    token = _RELAY_TOKENS.pop(run_id, None)
    if token is None:
        return ExecResult(exit_code=1, stdout="", stderr=f"run {run_id} has no relay token (start it through steer)")
    r = f"{RUNS}/{run_id}"
    await put(prov, sb, RUNS, {f"{run_id}.prompt": (prompt.encode(), 0o644), f"{run_id}.env": (exports.encode(), 0o644),
                               f"{run_id}.sh": (_script(settings, run_id, command, SAFE_PATH,
                                                          f"{CTL_RUNS}/{run_id}.ctl").encode(), 0o644)})
    res = await _links(prov, sb, settings, run_id)
    if res.exit_code != 0:
        return res
    pane = f"bash {r}.sh; exec sleep infinity"
    res = await sh(prov, sb, f"mkdir -p -m 0700 {CTL}/tmux && cd {HOME} && SHELL=/bin/sh {tmux(sb)} new-session -d "
                             f"-s {run_id} -x 200 -y 50 -c {HOME} {q(pane)}", timeout=20)
    if res.exit_code != 0:
        return res
    python = (sb.info.get("isolation") or {}).get("python") or "/usr/bin/python3"
    try:
        rel = await prov.exec_raw(sb, [python, "-I", RELAY, f"{r}.port"], RELAY_TIMEOUT,  # type: ignore[attr-defined]
                                  stdin=f"{token}\n".encode())
    except Exception as e:  # noqa: BLE001 - reported as the run failing to start
        rel = ExecResult(exit_code=-1, stdout="", stderr=str(e))
    del token
    if rel.exit_code != 0 or "hangar-relay: started" not in rel.stdout:
        await kill(prov, sb, settings, run_id, interrupted=True, side=True)
        return ExecResult(exit_code=rel.exit_code or 1, stdout=rel.stdout,
                          stderr=f"the agent's relay didn't start: {(rel.stderr or rel.stdout).strip()[-300:]}")
    return res


async def kill(prov: Provider, sb: Sandbox, settings: Settings, run_id: str, interrupted: bool = False,
               side: bool = False) -> None:
    """Stop a run's tmux session, wherever it runs; `interrupted`: its exit status is 130 unless it had finished.
    `side`: a sidecar run not recorded yet (it failed to start)."""
    s = side or run_split(sb, run_id)
    q = shlex.quote
    ended = q(f"{runs_dir(settings, s)}/{run_id}.exit")
    cmd = f"{tmux(sb) if s else 'tmux'} kill-session -t {q(run_id)} 2>/dev/null"
    if interrupted:
        cmd += f"; [ -s {ended} ] || echo 130 > {ended}"
    if s:
        if sidecar(sb):  # --- KAN-186: hangar's copy of the agent's environment is in the run's CTL directory
            cmd += f"; rm -f {q(f'{CTL_RUNS}/{run_id}.ctl/agent-env')}"
        await sh(prov, sb, f"{cmd}; {CLEANUP.format(r=q(f'{RUNS}/{run_id}'))}", timeout=30)  # --- harden
    else:
        await prov.exec(sb, cmd, timeout=30)


def attach_command(sb: Sandbox, session: str) -> list[str] | None:
    """The dashboard's terminal on a split run: a read-only view of its tmux session, as hangar's user."""
    if not run_split(sb, session):
        return None
    return ["env", f"TMUX_TMPDIR={CTL if sidecar(sb) else HOME}/tmux", "tmux", "attach-session", "-r", "-t", session]


async def open_terminal(prov: Provider, sb: Sandbox, command: list[str], cols: int, rows: int) -> Any:
    """A terminal as hangar's user (Docker exec; the agent's envd runs only as the agent). On Kubernetes, in the
    driver sidecar's container (KAN-186)."""
    if prov.name == "kubernetes":
        return await prov.open_terminal(sb, command, cols, rows, user=USER)  # type: ignore[call-arg]
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
