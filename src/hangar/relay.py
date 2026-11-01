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
  the driver goes away, so does the harness;
* runs what the driver asks for with `run` on `ctl` (KAN-187: the verify gate's check and per-edit trees, which need
  the checkout), as the agent, and sends the exit status and the end of the output back as `ran`. It keeps doing so
  after the harness exits, until the driver closes `ctl` (the turn's end tree comes then).

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
MAX_RUN_OUT = 64 << 10  # bytes of a `run`'s output sent back: its end (the driver keeps less)
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


def readline(sock: socket.socket, limit: int) -> tuple:
    """The first line, and what came after it (the driver's next requests may share its read)."""
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(1 << 16)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise ValueError("the driver's message is too long")
    line, _, rest = buf.partition(b"\n")
    return line, rest


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


def _env(spec: dict) -> tuple:
    """The environment and inherited fds for what the driver starts, and the pipe end hangar's copy of the agent's
    env goes into (--- harden: driver.agent_env), or None."""
    env, fds = dict(os.environ), ()
    base = spec.get("base_env")
    if base is None:
        return env, fds, None, None
    r, w = os.pipe()
    env["HANGAR_ENV_FD"] = str(r)
    return env, (r,), r, w


def _started(r: int | None, w: int | None, base: str | None) -> None:
    if r is not None and w is not None and base is not None:
        os.close(r)
        threading.Thread(target=_feed, args=(w, base64.b64decode(base)), daemon=True).start()


class Runs:
    """--- KAN-187: the driver's `run` requests: each a process of the agent's, from `/`, in its own process group,
    killed at its timeout or when the driver goes away. `ran` carries {id, code, out (base64, its end), timed_out}."""

    def __init__(self, say, base_env: str | None):
        self.say, self.base_env = say, base_env
        self.procs: dict = {}
        self.lock = threading.Lock()

    def start(self, raw: bytes) -> None:
        try:
            req = json.loads(raw)
            rid, argv, timeout = str(req["id"]), req["argv"], float(req.get("timeout") or 60)
            if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
                raise ValueError("argv")
        except (ValueError, KeyError, TypeError):
            return
        threading.Thread(target=self._run, args=(rid, argv, timeout), daemon=True).start()

    def _run(self, rid: str, argv: list, timeout: float) -> None:
        env, fds, r, w = _env({"base_env": self.base_env})
        code, out, timed_out = 127, b"", False
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    pass_fds=fds, env=env, cwd="/", start_new_session=True)
        except OSError as e:
            if r is not None and w is not None:
                os.close(r)
                os.close(w)
            out = f"hangar-relay: {e}".encode()
        else:
            _started(r, w, self.base_env)
            with self.lock:
                self.procs[rid] = proc
            try:
                out, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                out, _ = proc.communicate()
                timed_out = True
            with self.lock:
                self.procs.pop(rid, None)
            code = proc.returncode
            code = 128 - code if code < 0 else code
        self.say(b"ran " + json.dumps({"id": rid, "code": code, "timed_out": timed_out,
                                       "out": base64.b64encode((out or b"")[-MAX_RUN_OUT:]).decode()}).encode())

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()

    def stop(self) -> None:
        with self.lock:
            procs = list(self.procs.values())
        for p in procs:
            self._kill(p)


def run(io: socket.socket, ctl: socket.socket, spec: dict, buf: bytes = b"") -> int:
    """Start the harness, relay until it exits, and report its status. The harness's exit, not its stderr's end,
    finishes the run: something it left running in the background may hold stderr open. Then serve the driver's
    `run` requests until it closes `ctl`."""
    env, fds, r, w = _env(spec)
    proc = subprocess.Popen(spec["argv"], stdin=io.fileno(), stdout=io.fileno(), stderr=subprocess.PIPE,
                            pass_fds=fds, env=env, cwd="/")
    io.close()
    _started(r, w, spec.get("base_env"))
    assert proc.stderr is not None
    err: int | None = proc.stderr.fileno()
    lock = threading.Lock()

    def say(line: bytes) -> None:
        try:
            with lock:
                ctl.sendall(line + b"\n")
        except OSError:
            pass

    runs = Runs(say, spec.get("base_env"))
    code: int | None = None
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
                if code is None:
                    proc.kill()
                break
            buf += data
        while b"\n" in buf:  # with what came in the spec's read
            line, buf = buf.split(b"\n", 1)
            word, _, rest = line.partition(b" ")
            if word == b"term" and proc.poll() is None:
                proc.terminate()
            elif word == b"kill" and proc.poll() is None:
                proc.kill()
            elif word == b"run":
                runs.start(rest)
        if code is None and proc.poll() is not None:
            while err is not None and select.select([err], [], [], 0)[0]:
                chunk = os.read(err, 1 << 16)
                if not chunk:
                    break
                say(b"err " + base64.b64encode(chunk))
            err = None
            code = proc.wait()
            code = 128 - code if code < 0 else code  # a signal, as a shell reports it
            say(f"exit {code}".encode())
    runs.stop()
    ctl.close()
    if code is None:
        code = proc.wait()
        code = 128 - code if code < 0 else code
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
        line, rest = readline(ctl, MAX_SPEC)
        spec = json.loads(line or b"null")
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
    return run(io, ctl, spec, rest)


if __name__ == "__main__":
    sys.exit(main())
