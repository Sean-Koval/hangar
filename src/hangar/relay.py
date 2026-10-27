"""The agent's end of the driver sidecar (KAN-83; docs/security/driver-sidecar.md).

On Kubernetes the agent's container can't hold a second user, so a turn's driver (driver.py --relay) runs in a
container of its own, as a uid of its own, and can't start the harness in the agent's container. Hangar starts this
relay there instead, as the agent, through the pods/exec API, with the run's relay token on its stdin: never on a
command line or in a file the agent can read. Usage: `python3 -I relay.py PORTFILE < token`. The relay:

* makes itself non-dumpable before it reads the token, so the agent's other processes can't read it from memory;
* connects to the driver on the pod's loopback (the port is in PORTFILE, which the driver writes) twice, `io` and
  `ctl`, each saying the token; the driver takes the first two that do and stops listening;
* reads what to start (the harness's argv, built as a split run's is, and hangar's copy of the agent's environment
  for a hardened turn), detaches from the exec, and starts it with the `io` connection as its stdin and stdout;
* forwards the harness's stderr and exit status on `ctl`, and the driver's `term` and `kill` to the harness. When
  the driver goes away, so does the harness.

The relay and the harness are the agent's processes: the agent can stop them, or lie through them, as it can lie
through the harness under the Docker split. What it can't reach is the driver: its key, its control files, its
memory and the approvals it relays. Standard library only, Python 3.8+.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time

HELLO = "hangar-relay"  # driver.RELAY_HELLO
MAX_SPEC = 4 << 20
PR_SET_DUMPABLE = 4


def nondumpable() -> str | None:
    """No ptrace, /proc/<pid>/mem or environ for the agent's other processes (driver.private). Why it failed."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            return os.strerror(ctypes.get_errno())
    except (ImportError, OSError, AttributeError) as e:
        return str(e) or type(e).__name__
    return None


def readline(sock: socket.socket, limit: int) -> bytes:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(1 << 16)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise ValueError("the driver's message is too long")
    return buf.split(b"\n", 1)[0]


def port_of(portfile: str, wait: float) -> int:
    end = time.time() + wait
    while True:
        try:
            with open(portfile) as f:
                text = f.read().strip()
            if text.isdigit():
                return int(text)
        except OSError:
            pass
        if time.time() > end:
            raise TimeoutError(f"the driver didn't start listening within {int(wait)} s ({portfile})")
        time.sleep(0.05)


def connect(port: int, role: str, token: str) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(f"{HELLO} {role} {token}\n".encode())
    s.settimeout(None)
    return s


def _feed(fd: int, data: bytes) -> None:
    try:
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        pass
    finally:
        os.close(fd)


def run(io: socket.socket, ctl: socket.socket, spec: dict) -> int:
    """Start the harness, relay until it exits, and report its status. The harness's exit, not its stderr's end,
    finishes the run: something it left running in the background may hold stderr open."""
    env, fds = dict(os.environ), ()
    base = spec.get("base_env")
    r = w = None
    if base is not None:  # --- harden: hangar's copy of the agent's env, on a pipe (driver.agent_env)
        r, w = os.pipe()
        env["HANGAR_ENV_FD"], fds = str(r), (r,)
    proc = subprocess.Popen(spec["argv"], stdin=io.fileno(), stdout=io.fileno(), stderr=subprocess.PIPE,
                            pass_fds=fds, env=env, cwd="/")
    io.close()
    if r is not None and w is not None:
        os.close(r)
        threading.Thread(target=_feed, args=(w, base64.b64decode(base)), daemon=True).start()
    assert proc.stderr is not None
    err: int | None = proc.stderr.fileno()
    buf = b""

    def say(line: bytes) -> None:
        try:
            ctl.sendall(line + b"\n")
        except OSError:
            pass

    while True:
        ready, _, _ = select.select([ctl] + ([err] if err is not None else []), [], [], 0.2)
        if err is not None and err in ready:
            chunk = os.read(err, 1 << 16)
            if chunk:
                say(b"err " + base64.b64encode(chunk))
            else:
                err = None
        if ctl in ready:
            try:
                data = ctl.recv(4096)
            except OSError:
                data = b""
            if not data:  # the driver is gone: nothing would read the harness now
                proc.kill()
                break
            buf += data
            while b"\n" in buf:
                word, buf = buf.split(b"\n", 1)
                if word == b"term" and proc.poll() is None:
                    proc.terminate()
                elif word == b"kill" and proc.poll() is None:
                    proc.kill()
        if proc.poll() is not None:
            while err is not None and select.select([err], [], [], 0)[0]:
                chunk = os.read(err, 1 << 16)
                if not chunk:
                    break
                say(b"err " + base64.b64encode(chunk))
            break
    code = proc.wait()
    code = 128 - code if code < 0 else code  # a signal, as a shell reports it
    say(f"exit {code}".encode())
    ctl.close()
    return code


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Start a turn's harness for hangar's driver sidecar (module docstring).")
    ap.add_argument("portfile", help="where the driver writes its port")
    ap.add_argument("--wait", type=float, default=60.0, help="seconds to wait for the driver")
    ap.add_argument("--foreground", action="store_true", help="don't detach from the exec once connected")
    args = ap.parse_args(argv)
    why = nondumpable()  # before the token is read
    if why:
        sys.stderr.write(f"hangar-relay: can't keep the token from the agent's other processes ({why})\n")
        return 1
    token = sys.stdin.readline().strip()
    if not token or len(token.split()) != 1:
        sys.stderr.write("hangar-relay: no token on stdin\n")
        return 1
    try:
        port = port_of(args.portfile, args.wait)
        io = connect(port, "io", token)
        ctl = connect(port, "ctl", token)
        spec = json.loads(readline(ctl, MAX_SPEC) or b"null")
    except (OSError, ValueError, TimeoutError) as e:
        sys.stderr.write(f"hangar-relay: {e}\n")
        return 1
    del token
    if not (isinstance(spec, dict) and isinstance(spec.get("argv"), list) and spec["argv"]
            and all(isinstance(a, str) for a in spec["argv"])):
        sys.stderr.write("hangar-relay: the driver sent nothing to start\n")
        return 1
    if not args.foreground:
        pid = os.fork()
        if pid:
            sys.stdout.write(f"hangar-relay: started {pid}\n")
            sys.stdout.flush()
            os._exit(0)
        os.setsid()  # the exec that started it can end now
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.close(null)
    return run(io, ctl, spec)


if __name__ == "__main__":
    sys.exit(main())
