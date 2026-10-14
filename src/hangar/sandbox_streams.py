"""Commands whose output streams as it is written, background processes, followed files and directory watches
(workstream stream; docs/streaming.md).

hangar-envd (and E2B's own envd) run commands through Connect's `process.Process`: `Start` streams a command's
output as it comes, `Connect` re-attaches to a running one by pid or tag, `SendInput`, `SendSignal`, `CloseStdin`
and `List` do the rest; `filesystem.Filesystem/WatchDir` streams inotify events. This module puts those behind
hangar's sandbox refs:
- `run`: one command, its output handed to the caller chunk by chunk, then the same result as `exec`;
- `Processes`: background commands (E2B's `commands.run(background=True)`, `connect`, `send_stdin`, `kill`, `list`),
  each followed by a pump that keeps the last BUFFER bytes of its output for whoever attaches;
- `follow`: a growing file (a turn's run log) through `tail -F`, as it is written;
- `Watches`: directory watches behind MCP resource subscriptions.

Everything the sandbox sends is untrusted: chunks are capped by envd.py's frame limit, buffers keep a bounded tail,
and every stream is registered with the stream hub (streams.py), which bounds and closes them. Without a daemon (no
binary, an unreachable sandbox, HANGAR_ENVD=off) `run` returns the output when the command ends, background
processes run detached and are polled from a log file, `follow` reports there is nothing to follow (the turn watcher
keeps polling), and a watch says it needs the daemon.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import collections
import contextlib
import hashlib
import hmac
import logging
import re
import secrets
import shlex
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from hangar import metrics
from hangar.envd import EXEC_GRACE, MAX_OUTPUT, Endpoint, Envd, EnvdError, _b64, _text
from hangar.fleet import FleetError
from hangar.models import Sandbox
from hangar.providers.base import OutputTail, Provider, ProviderError
from hangar.streams import Stream, StreamClosed, StreamLimit, hub
from hangar.vault import load_or_create_key

log = logging.getLogger("hangar.streams")

READY_WAIT = 5.0  # a stream asked for explicitly waits this long for a sandbox's daemon to come up
FEED = 128 * 1024  # a followed log reaches its reader in pieces this size, each paced (events.CHUNK, as a poll reads)
BUFFER = 256 * 1024  # output a background process keeps for attaching clients
RETAIN = 600.0  # seconds an exited background process stays listed (and attachable for its last output)
ATTACH_TAIL = 64 * 1024  # output an attach call returns in its result (more streams as progress)
POLL_ATTACHED, POLL_IDLE = 0.5, 5.0  # fallback background processes: log polls with and without a client
WATCH_EVENTS = 200  # events a directory watch keeps for readers
_ID = re.compile(r"^bg-[0-9a-f]{12}$")
_TAG = "hangar:"
Emit = Callable[[str, bytes], Awaitable[None]]


def router(prov: Provider) -> Envd | None:
    """The provider's envd client: hangar-envd on Docker and Kubernetes, E2B's own envd on e2b."""
    r = getattr(prov, "dataplane", None)  # e2b keeps its envd client under another name (e2b.py)
    if r is None:
        r = getattr(prov, "envd", None)
    return r if isinstance(r, Envd) and r.enabled else None


async def daemon(fleet: Any, ref: str, wait: float = READY_WAIT) -> tuple[Sandbox, Provider, Envd | None,
                                                                         Endpoint | None]:
    """The running sandbox, its provider and its daemon (None: use the provider's exec), waiting up to `wait` for a
    daemon that is starting: a client that asked to stream would rather wait a moment than not stream."""
    sb, prov = fleet._running(ref)
    env = router(prov)
    if env is None:
        return sb, prov, None, None
    ep = await env.ready(sb, wait) if wait else await env.endpoint(sb)
    return sb, prov, env, ep


async def _signal(env: Envd, ep: Endpoint, selector: dict[str, Any], sig: str) -> None:
    with contextlib.suppress(EnvdError, asyncio.TimeoutError):
        await asyncio.wait_for(env.unary(ep, "process.Process/SendSignal", {"process": selector,
                                                                            "signal": f"SIGNAL_{sig}"}, timeout=5), 8)


def _data(ev: dict[str, Any]) -> tuple[str, bytes] | None:
    d = ev.get("data")
    if not isinstance(d, dict):
        return None
    for k in ("stdout", "stderr", "pty"):
        if isinstance(d.get(k), str):
            return ("stdout" if k == "pty" else k), _b64(d[k])
    return None


def _exit(end: dict[str, Any]) -> int:
    code = end.get("exitCode", 0) if end.get("exited") is True else -1
    return code if isinstance(code, int) else -1


# ====================================================================== one command, streamed
async def run(fleet: Any, ref: str, command: str, *, cwd: str | None, timeout: int, actor: str, st: Stream,
              emit: Emit) -> dict[str, Any]:
    """`exec`, with each chunk of output handed to `emit(stream, data)` as the sandbox writes it (paced and capped
    by `st`). The result is exec's (`exit_code`, the last 256 KiB of `stdout` and `stderr`) plus `streamed`.

    A command whose caller goes away, or whose token is revoked, is killed; one cut off by a shutdown or a
    destroyed sandbox is not (it ends at its timeout, as an unstreamed exec would)."""
    timeout = max(1, min(int(timeout), 24 * 3600))
    sb, prov, env, ep = await daemon(fleet, ref)
    fleet._touch(sb)
    cwd = cwd or sb.info.get("repo_path") or fleet.settings.workspace_dir
    if env is not None and ep is not None:
        res = await _run_envd(fleet, sb, env, ep, command, cwd, timeout, st, emit)
        if res is not None:
            fleet.touch(sb.id, 0)
            fleet.audit(actor, "sandbox.exec", sb.id, command=command[:500], exit_code=res["exit_code"], stream=True)
            return res
    # No daemon (or it refused the command before starting it): the provider runs it; output comes at the end.
    try:
        r = await st.race(fleet.exec(ref, command, cwd=cwd, timeout=timeout, actor=actor))
    except StreamClosed as e:
        return {"exit_code": -1, "stdout": "", "stderr": "", "truncated": False, "streamed": False,
                "closed": e.code, "note": f"stream closed: {e}; the command may still be running"}
    for kind, text in (("stdout", r.stdout), ("stderr", r.stderr)):
        data = text.encode()
        if data and await st.send(len(data)):
            await st.race(emit(kind, data))
    return {**r.model_dump(), "streamed": False}


async def _run_envd(fleet: Any, sb: Sandbox, env: Envd, ep: Endpoint, command: str, cwd: str, timeout: int,
                    st: Stream, emit: Emit) -> dict[str, Any] | None:
    """The command through the daemon; None if the daemon refused it before it started (the provider runs it)."""
    body = {"process": {"cmd": "timeout", "args": ["--kill-after=5", str(timeout), "bash", "-lc", command],
                        "cwd": cwd}, "stdin": False}
    out, err = OutputTail(4 * MAX_OUTPUT, b""), OutputTail(4 * MAX_OUTPUT, b"")
    end: dict[str, Any] | None = None
    pid = 0
    closed: StreamClosed | None = None
    cancelled = False
    t0 = time.monotonic()
    events = env.stream(ep, "process.Process/Start", body, timeout + 15)
    try:
        async with asyncio.timeout(timeout + 15 + EXEC_GRACE):
            while True:
                msg = await st.race(anext(events, None))
                if msg is None:
                    break
                ev = msg.get("event") or {}
                if (s := ev.get("start")) is not None:
                    pid = int((s or {}).get("pid") or 0)
                elif (d := _data(ev)) is not None:
                    kind, data = d
                    (out if kind == "stdout" else err).add(data)
                    if await st.send(len(data)):
                        await st.race(emit(kind, data))
                elif (e := ev.get("end")) is not None:
                    end = e if isinstance(e, dict) else {}
    except StreamClosed as e:
        closed = e
    except TimeoutError as e:
        env._lost(sb, "a streamed command outlived its timeout")
        if end is None:
            raise ProviderError("exec failed: no exit status from the sandbox's daemon in time; the command may "
                                "still be running") from e
    except EnvdError as e:
        if e.code in ("transport", "unauthenticated", "impostor"):
            env._lost(sb, str(e))
        if not e.started and not pid:
            return None
        if end is None:
            raise ProviderError(f"exec failed: lost the command's output ({e}); it may still be running") from e
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        with contextlib.suppress(Exception):
            await asyncio.shield(events.aclose())
        if end is None and pid and (cancelled or (closed is not None and closed.code in ("revoked", "lifetime"))):
            # the caller gave up on it, or lost the right to it: a foreground command doesn't outlive its caller
            await asyncio.shield(_signal(env, ep, {"pid": pid}, "SIGKILL"))
    metrics.sandbox_exec("envd", time.monotonic() - t0)
    o, t1 = _text(out)
    e_, t2 = _text(err)
    res: dict[str, Any] = {"exit_code": _exit(end) if end is not None else -1, "stdout": o, "stderr": e_,
                           "duration_ms": int((time.monotonic() - t0) * 1000), "truncated": t1 or t2,
                           "streamed": True}
    if res["exit_code"] == 124:
        res["stderr"] += f"\n[hangar] command timed out after {timeout}s"
    if closed is not None and end is None:
        res["closed"] = closed.code
        res["note"] = f"stream closed: {closed}" + {
            "revoked": "; the command was killed", "lifetime": "; the command was killed",
            "shutdown": "; the command keeps running until it ends or times out"}.get(closed.code, "")
    if st.capped:
        res["note"] = (res.get("note", "") + "; " if res.get("note") else "") + \
            "output over HANGAR_STREAM_MAX_BYTES was not streamed (the result keeps its end)"
    return res


# ====================================================================== background processes
@dataclass
class Proc:
    """A background process and the end of its output (stdout and stderr together, as a terminal shows them)."""

    id: str
    owner: str
    sandbox_id: str
    command: str
    cwd: str
    transport: str  # envd | exec
    tag: str = ""  # its envd tag (Processes.tag)
    started_at: float = field(default_factory=time.time)
    pid: int | None = None
    state: str = "running"  # running | exited | lost
    exit_code: int | None = None
    note: str | None = None
    ended_at: float | None = None
    stdin: bool = True
    adopted: bool = False  # found running after a control-plane restart: earlier output is gone
    buf: bytearray = field(default_factory=bytearray)
    base: int = 0  # offset of buf[0] in the whole output
    attached: int = 0
    tick: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    @property
    def end(self) -> int:
        return self.base + len(self.buf)

    def add(self, data: bytes) -> None:
        self.buf += data
        if len(self.buf) > BUFFER:
            drop = len(self.buf) - BUFFER
            del self.buf[:drop]
            self.base += drop
        self._wake()

    def finish(self, state: str, exit_code: int | None = None, note: str | None = None) -> None:
        if self.state == "running":
            self.state, self.exit_code, self.note, self.ended_at = state, exit_code, note, time.time()
        self._wake()

    def _wake(self) -> None:
        self.tick.set()
        self.tick = asyncio.Event()

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "sandbox": self.sandbox_id, "command": self.command[:500], "pid": self.pid,
                "state": self.state, "exit_code": self.exit_code, "started_at": self.started_at,
                "ended_at": self.ended_at, "transport": self.transport, "output_bytes": self.end,
                **({"note": self.note} if self.note else {}),
                **({"adopted": True} if self.adopted else {})}


class Processes:
    """Background processes started by clients, per control plane (`processes(fleet)`). Each belongs to the token
    that started it: nobody else can list, attach, send input or kill it."""

    def __init__(self, fleet: Any):
        self.fleet = fleet
        self.hub = hub(fleet)
        self.procs: dict[str, Proc] = {}
        self.starting: collections.Counter[str] = collections.Counter()  # starts under way, per owner (the cap)
        self.hub.on_close_sandbox.append(self._sandbox_gone)
        self._key: bytes | None = None

    def tag(self, owner: str, sandbox_id: str, proc_id: str) -> str:
        """A process's envd tag: its id, and a MAC of its owner, sandbox and id under the control plane's key. The
        agent can start processes through its own daemon with any tag, so the tag must not be one it can make up
        for a token (review #68): a name's plain hash let it put processes in any token's list, using up its cap."""
        if self._key is None:
            s = self.fleet.settings
            self._key = load_or_create_key(s.secret_key, s.data_dir)
        mac = hmac.new(self._key, f"hangar-process\n{sandbox_id}\n{owner}\n{proc_id}".encode(), hashlib.sha256)
        return f"{_TAG}{proc_id}:{mac.hexdigest()[:24]}"

    # ------------------------------------------------------------------ lookup
    def _sweep(self) -> None:
        """Forget processes that ended RETAIN ago, and past a few dozen per token the oldest ended ones sooner (each
        holds up to BUFFER of output)."""
        cut = time.time() - RETAIN
        keep = max(16, self.fleet.settings.background_max_per_token)
        ended: dict[str, list[Proc]] = collections.defaultdict(list)
        for pid_, p in list(self.procs.items()):
            if p.state != "running" and (p.ended_at or 0) < cut:
                del self.procs[pid_]
            elif p.state != "running":
                ended[p.owner].append(p)
        for ps in ended.values():
            for p in sorted(ps, key=lambda p: p.ended_at or 0)[:-keep]:
                self.procs.pop(p.id, None)

    def _sandbox_gone(self, sandbox_id: str, code: str) -> None:
        for p in self.procs.values():
            if p.sandbox_id == sandbox_id and code == "destroyed":
                p.finish("lost", note="the sandbox was destroyed")

    async def get(self, owner: str, ref: str, proc_id: str) -> Proc:
        sb = self.fleet.get(ref)
        if not _ID.match(proc_id or ""):
            raise FleetError(f"invalid process id '{proc_id}' (bg-<12 hex>, from process start or list)")
        p = self.procs.get(proc_id)
        if p is None:
            await self._adopt(owner, sb.id)
            p = self.procs.get(proc_id)
        if p is None or p.owner != owner or p.sandbox_id != sb.id:
            raise FleetError(f"no background process {proc_id} of yours in {sb.name or sb.id}")
        return p

    async def list(self, owner: str, ref: str) -> list[dict[str, Any]]:
        sb = self.fleet.get(ref)
        self._sweep()
        with contextlib.suppress(FleetError):
            await self._adopt(owner, sb.id)
        return [p.view() for p in sorted(self.procs.values(), key=lambda p: p.started_at)
                if p.owner == owner and p.sandbox_id == sb.id]

    async def _adopt(self, owner: str, sandbox_id: str) -> None:
        """After a control-plane restart, the owner's processes still running in the sandbox (found by their envd
        tag, which names the owner) are followed again. Their earlier output is gone."""
        sb, _, env, ep = await daemon(self.fleet, sandbox_id, wait=0)
        if env is None or ep is None:
            return
        try:
            listed = await env.unary(ep, "process.Process/List", {}, timeout=10)
        except EnvdError:
            return
        for info in (listed.get("processes") or [])[:1024]:
            tag = str((info or {}).get("tag") or "")
            parts = tag.split(":")
            if len(parts) != 3 or parts[0] + ":" != _TAG or not _ID.match(parts[1]) or parts[1] in self.procs:
                continue
            if not hmac.compare_digest(tag, self.tag(owner, sb.id, parts[1])):
                continue
            cfg = info.get("config") or {}
            args = cfg.get("args") or []
            p = Proc(id=parts[1], owner=owner, sandbox_id=sb.id, command=str(args[-1] if args else cfg.get("cmd", ""))
                     [:2000], cwd=str(cfg.get("cwd") or ""), transport="envd", tag=tag,
                     pid=int(info.get("pid") or 0) or None, adopted=True,
                     note="followed again after a control-plane restart; earlier output is gone")
            try:
                self._check_cap(owner)
            except StreamLimit:
                return
            self.procs[p.id] = p
            p.task = asyncio.create_task(self._pump_envd(p, env, ep, {"process": {"tag": p.tag}}, connect=True))

    def _check_cap(self, owner: str) -> None:
        running = sum(1 for p in self.procs.values() if p.owner == owner and p.state == "running")
        running += self.starting[owner]  # starts still waiting on the sandbox (review #68: concurrent starts)
        if running >= self.fleet.settings.background_max_per_token:
            raise StreamLimit(f"{owner} already has {running} background processes running "
                              "(HANGAR_BACKGROUND_MAX_PER_TOKEN); kill one first")

    # ------------------------------------------------------------------ start
    async def start(self, owner: str, ref: str, command: str, cwd: str | None = None, actor: str = "mcp") -> Proc:
        if not command.strip():
            raise ValueError("command is empty")
        self._sweep()
        self._check_cap(owner)
        if self.fleet.closing.is_set():
            raise StreamLimit("hangar is restarting; try again in a moment")
        self.hub.check_total()  # its pump is a stream: refuse now, not once the command runs unfollowed
        self.starting[owner] += 1  # held until it is in self.procs, so concurrent starts see it
        try:
            p, sb = await self._start(owner, ref, command, cwd)
        finally:
            self.starting[owner] -= 1
            if self.starting[owner] <= 0:
                del self.starting[owner]
        self.fleet._touch(sb)
        self.fleet.audit(actor, "sandbox.process.start", sb.id, process=p.id, command=command[:500])
        return p

    async def _start(self, owner: str, ref: str, command: str, cwd: str | None) -> tuple[Proc, Sandbox]:
        sb, prov, env, ep = await daemon(self.fleet, ref)
        cwd = cwd or sb.info.get("repo_path") or self.fleet.settings.workspace_dir
        pid_ = f"bg-{secrets.token_hex(6)}"
        p = Proc(id=pid_, owner=owner, sandbox_id=sb.id, command=command, cwd=cwd,
                 transport="envd" if ep is not None else "exec", tag=self.tag(owner, sb.id, pid_))
        if env is not None and ep is not None:
            body = {"process": {"cmd": "bash", "args": ["-lc", command], "cwd": cwd}, "tag": p.tag, "stdin": True}
            events = env.stream(ep, "process.Process/Start", body, None, keepalive=30)
            try:
                first = await asyncio.wait_for(anext(events), 30)
                p.pid = int(((first.get("event") or {}).get("start") or {}).get("pid") or 0) or None
            except (EnvdError, StopAsyncIteration, TimeoutError, ValueError, TypeError) as e:
                await events.aclose()
                if isinstance(e, EnvdError) and not e.started:
                    raise ProviderError(f"could not start the process: {e}") from e
                raise ProviderError(f"the process may have started, but its daemon stopped answering ({e}); "
                                    "list processes to find it") from e
            self.procs[p.id] = p
            p.task = asyncio.create_task(self._pump_envd(p, env, ep, None, events=events))
        else:
            await self._start_exec(sb, prov, p)
            self.procs[p.id] = p
            p.task = asyncio.create_task(self._pump_exec(p, prov))
        return p, sb

    async def _start_exec(self, sb: Sandbox, prov: Provider, p: Proc) -> None:
        """Fallback: the command runs detached in its own session, its output and exit code go to files under
        ~/.hangar/bg, and the pump polls them."""
        d = '"$HOME/.hangar/bg"'
        inner = 'bash -lc "$1" > "$2.out" 2>&1 < /dev/null; echo $? > "$2.exit"'
        cmd = (f"mkdir -p {d} && cd {shlex.quote(p.cwd)} || exit 3; setsid bash -c {shlex.quote(inner)} _ "
               f"{shlex.quote(p.command)} {d}/{p.id} > /dev/null 2>&1 < /dev/null & echo $!")
        r = await prov.exec(sb, cmd, timeout=30)
        if r.exit_code != 0 or not r.stdout.strip().isdigit():
            raise ProviderError(f"could not start the process ({r.exit_code}): {(r.stderr or r.stdout)[-300:]}")
        p.pid = int(r.stdout.strip())
        p.stdin = False

    # ------------------------------------------------------------------ pumps
    async def _pump_envd(self, p: Proc, env: Envd, ep: Endpoint, selector: dict[str, Any] | None,
                         events: Any = None, connect: bool = False) -> None:
        """Read the process's output into its buffer until it ends. A broken stream is re-attached by tag (the
        process keeps running in the sandbox) a few times before the process is given up as lost."""
        try:
            async with self.hub.open(p.owner, "process", p.sandbox_id, capped=False) as st:
                await self._follow_envd(p, env, ep, selector, st, events, connect)
        except StreamLimit as e:  # (review #68) refused between start's check and here: don't leave it unfollowed
            if events is not None:
                with contextlib.suppress(Exception):
                    await events.aclose()
            if connect:  # found again after a restart: it is the user's, leave it to be found on the next list
                if self.procs.get(p.id) is p:
                    del self.procs[p.id]
                p.finish("lost", note=str(e))
            else:
                await asyncio.shield(_signal(env, ep, {"tag": p.tag}, "SIGKILL"))
                p.finish("lost", note=f"killed: {e}")

    async def _follow_envd(self, p: Proc, env: Envd, ep: Endpoint, selector: dict[str, Any] | None, st: Stream,
                           events: Any, connect: bool) -> None:
        tries = 0
        while True:
            if events is None:
                events = env.stream(ep, "process.Process/Connect", selector or {"process": {"tag": p.tag}},
                                    None, keepalive=30)
            try:
                while True:
                    msg = await st.race(anext(events, None))
                    if msg is None:
                        break
                    ev = msg.get("event") or {}
                    if (s := ev.get("start")) is not None and connect:
                        p.pid = int((s or {}).get("pid") or 0) or p.pid
                    elif (d := _data(ev)) is not None:
                        p.add(d[1])
                        tries = 0
                    elif (e := ev.get("end")) is not None:
                        e = e if isinstance(e, dict) else {}
                        p.finish("exited", _exit(e), e.get("error") or None)
                        return
            except StreamClosed as e:
                if e.code in ("revoked", "lifetime") and p.state == "running":
                    await asyncio.shield(_signal(env, ep, {"tag": p.tag}, "SIGKILL"))
                    p.finish("lost", note=f"killed: {e}")
                elif e.code == "paused":  # it carries on after a resume: found again by its tag then
                    if self.procs.get(p.id) is p:
                        del self.procs[p.id]
                    p.finish("lost", note=str(e))
                elif p.state == "running" and e.code != "shutdown":
                    p.finish("lost", note=str(e))
                return
            except EnvdError as e:
                if e.code == "not_found":  # exited while we were away; its end event is gone too
                    p.finish("lost", note="the process ended while hangar wasn't following it")
                    return
                log.info("background process %s in %s: %s", p.id, p.sandbox_id, e)
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.shield(events.aclose())
                events = None
            tries += 1
            connect = True
            if tries > 3:
                p.finish("lost", note="lost the process's output stream; it may still be running")
                return
            with contextlib.suppress(StreamClosed):  # closed meanwhile: the next race says so, and is handled above
                await st.race(asyncio.sleep(0.5 * tries))

    async def _pump_exec(self, p: Proc, prov: Provider) -> None:
        try:
            async with self.hub.open(p.owner, "process", p.sandbox_id, capped=False) as st:
                await self._follow_exec(p, prov, st)
        except StreamLimit as e:  # (review #68) refused between start's check and here
            await self._kill_exec(p, prov, "KILL")
            p.finish("lost", note=f"killed: {e}")

    async def _follow_exec(self, p: Proc, prov: Provider, st: Stream) -> None:
        d = '"$HOME/.hangar/bg"'
        errors = 0
        while p.state == "running":
            try:
                await st.race(asyncio.wait_for(p.tick.wait(), POLL_ATTACHED if p.attached else POLL_IDLE))
            except TimeoutError:
                pass
            except StreamClosed as e:
                if e.code in ("revoked", "lifetime"):
                    await self._kill_exec(p, prov, "KILL")
                    p.finish("lost", note=f"killed: {e}")
                elif e.code != "shutdown":
                    p.finish("lost", note=str(e))
                return
            if p.state != "running":
                return
            cmd = (f"printf '%s---\\n' \"$(cat {d}/{p.id}.exit 2>/dev/null)\"; "
                   f"tail -c +{p.end + 1} {d}/{p.id}.out 2>/dev/null | head -c {BUFFER} | base64 -w0")
            try:
                sb = self.fleet.get(p.sandbox_id)
                r = await st.race(prov.exec(sb, cmd, timeout=30))
                head, sep, b64 = r.stdout.partition("---\n")
                if not sep:
                    raise ProviderError(f"unexpected answer ({r.exit_code})")
                data = base64.b64decode(b64.strip() or b"")
                errors = 0
            except StreamClosed:
                continue  # handled at the top
            except Exception as e:  # noqa: BLE001 - the sandbox is busy or going away: try again, then give up
                errors += 1
                if errors >= 10:
                    p.finish("lost", note=f"could not read the process's output: {e}")
                continue
            if data:
                p.add(data)
            status = head.strip()
            if status and not data:  # exited and everything read
                p.finish("exited", int(status) if re.fullmatch(r"-?\d{1,9}", status) else -1)  # agent-written

    # ------------------------------------------------------------------ attach / input / kill
    async def attach(self, p: Proc, since: int | None, wait: float | None, st: Stream,
                     emit: Callable[[int, bytes], Awaitable[None]] | None) -> dict[str, Any]:
        """The output from `since` (an offset from an earlier `next`; default: all still buffered), then whatever
        comes for up to `wait` seconds (None: until the process ends), each piece passed to `emit(next, data)` as
        it arrives (`next`: the offset after it)."""
        pos = p.base if since is None else max(0, int(since))
        dropped = max(0, p.base - pos)
        pos = min(max(pos, p.base), p.end)
        got = OutputTail(ATTACH_TAIL, b"")
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        deadline = float("inf") if wait is None else time.monotonic() + max(0.0, float(wait))
        p.attached += 1
        closed: StreamClosed | None = None
        try:
            while True:
                if pos < p.base:  # the buffer moved past us while we were sending
                    dropped += p.base - pos
                    pos = p.base
                chunk = bytes(p.buf[pos - p.base:])
                if chunk:
                    pos += len(chunk)
                    got.add(chunk)
                    if emit is not None and await st.send(len(chunk)):
                        await st.race(emit(pos, chunk))
                    continue
                if p.state != "running" or time.monotonic() >= deadline:
                    break
                with contextlib.suppress(TimeoutError):
                    await st.race(asyncio.wait_for(p.tick.wait(), min(60.0, max(0.01, deadline - time.monotonic()))))
        except StreamClosed as e:
            closed = e
        finally:
            p.attached -= 1
        text = dec.decode(got.value()[-ATTACH_TAIL:], final=True)
        out = {**p.view(), "output": text, "next": pos, "truncated": got.cut}
        if dropped:
            out["dropped"] = dropped
        if closed is not None:
            out["closed"], out["note"] = closed.code, f"stream closed: {closed}"
        return out

    async def send_input(self, p: Proc, data: bytes, eof: bool = False) -> None:
        if p.state != "running":
            raise FleetError(f"process {p.id} is {p.state}")
        if p.transport != "envd" or not p.stdin:
            raise FleetError(f"process {p.id} takes no input: sending input needs hangar-envd in the sandbox (it "
                             "runs detached here)")
        _, _, env, ep = await daemon(self.fleet, p.sandbox_id, wait=0)
        if env is None or ep is None:
            raise FleetError(f"the daemon in {p.sandbox_id} isn't answering; try again in a moment")
        try:
            if data:
                await env.unary(ep, "process.Process/SendInput",
                                {"process": {"tag": p.tag}, "input": {"stdin": base64.b64encode(data).decode()}})
            if eof:
                await env.unary(ep, "process.Process/CloseStdin", {"process": {"tag": p.tag}})
        except EnvdError as e:
            raise FleetError(f"could not send input to {p.id}: {e}") from e

    async def kill(self, p: Proc, grace: float = 3.0) -> dict[str, Any]:
        """SIGTERM to the process group, then SIGKILL if it is still running after `grace` seconds."""
        if p.state == "running":
            sb, prov, env, ep = await daemon(self.fleet, p.sandbox_id, wait=0)
            if p.transport == "envd" and env is not None and ep is not None:
                await _signal(env, ep, {"tag": p.tag}, "SIGTERM")
                if not await self._ended(p, grace):
                    await _signal(env, ep, {"tag": p.tag}, "SIGKILL")
                    await self._ended(p, 5)
            elif p.transport == "exec":
                await self._kill_exec(p, prov, "TERM")
                if not await self._ended(p, grace):
                    await self._kill_exec(p, prov, "KILL")
            else:
                raise FleetError(f"the daemon in {sb.id} isn't answering; try again in a moment")
            if p.state == "running":
                p.finish("exited", -1, "killed")
        return p.view()

    async def _kill_exec(self, p: Proc, prov: Provider, sig: str) -> None:
        if p.pid:
            with contextlib.suppress(Exception):
                await prov.exec(self.fleet.get(p.sandbox_id), f"kill -{sig} -- -{int(p.pid)} 2>/dev/null; true",
                                timeout=10)
                p._wake()  # the pump looks again now

    @staticmethod
    async def _ended(p: Proc, wait: float) -> bool:
        deadline = time.monotonic() + wait
        while p.state == "running" and time.monotonic() < deadline:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(p.tick.wait(), max(0.01, deadline - time.monotonic()))
        return p.state != "running"

    async def close(self) -> None:
        for p in self.procs.values():
            if p.task is not None:
                p.task.cancel()


# ====================================================================== following a file
async def follow(fleet: Any, sandbox_id: str, path: str, done_path: str, offset: int,
                 on_data: Callable[[bytes], None]) -> str:
    """Pass the file's bytes from `offset` on to `on_data` as they are written, until `done_path` exists (the
    run's exit file) and the rest has been read, or the caller cancels. Returns why it stopped; "no-daemon" means
    nothing was followed. Anything unexpected (the file truncated or replaced, which `tail` reports on stderr) ends
    it, so the caller's own poll, which handles those, takes over from where this stopped."""
    try:
        sb, _, env, ep = await daemon(fleet, sandbox_id, wait=0)
    except FleetError:
        return "gone"
    if env is None or ep is None:
        return "no-daemon"
    q, done = shlex.quote(path), shlex.quote(done_path)
    # -s 0.1: in a Docker container GNU tail polls the file (no inotify on the overlayfs root, it seems), once a
    # second by default: events were 714 ms late at the median, against 15 ms with -s 0.1 (scripts/bench/streams.py).
    script = (f"[ -e {q} ] || exit 3; tail -s 0.1 -c +{int(offset) + 1} -F {q} & t=$!; "
              "trap 'kill $t 2>/dev/null' EXIT; "
              f"while kill -0 $t 2>/dev/null; do [ -s {done} ] && {{ sleep 1; break; }}; sleep 0.5; done")
    # A backstop for a tail nobody stops (the control plane died): a day, or the turn's own exit file.
    body = {"process": {"cmd": "timeout", "args": ["86400", "bash", "-c", script], "cwd": "/"}, "stdin": False}
    try:
        async with hub(fleet).open("system", "turn_log", sb.id, capped=False, system=True) as st:
            return await _tail(st, env, ep, body, on_data, f"{path} in {sb.id}")
    except StreamLimit:
        return "limit"


async def _tail(st: Stream, env: Envd, ep: Endpoint, body: dict[str, Any], on_data: Callable[[bytes], None],
                what: str) -> str:
    pid, why = 0, "ended"
    events = env.stream(ep, "process.Process/Start", body, None, keepalive=30)
    try:
        while True:
            msg = await st.race(anext(events, None))
            if msg is None:
                break
            ev = msg.get("event") or {}
            if (s := ev.get("start")) is not None:
                pid = int((s or {}).get("pid") or 0)
            elif (d := _data(ev)) is not None:
                if d[0] == "stderr":
                    why = "changed"
                    break
                # (review #68) a piece at a time, each paced; past HANGAR_STREAM_MAX_BYTES a stream is no longer
                # paced, so stop there: polling's bounded reads go on from what was fed (and no follower restarts)
                for i in range(0, len(d[1]), FEED):
                    if not await st.send(len(piece := d[1][i:i + FEED])):
                        why = "capped"
                        break
                    on_data(piece)
                if why == "capped":
                    break
            elif (e := ev.get("end")) is not None:
                why = "ended" if _exit(e if isinstance(e, dict) else {}) == 0 else "missing"
                pid = 0
    except StreamClosed as e:
        why = e.code
    except EnvdError as e:
        why = "error"
        log.info("following %s: %s", what, e)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.shield(events.aclose())
        if pid:
            await asyncio.shield(_signal(env, ep, {"pid": pid}, "SIGKILL"))
    return why


# ====================================================================== directory watches
@dataclass
class Watch:
    owner: str
    sandbox_id: str
    path: str
    recursive: bool
    uris: collections.Counter[str] = field(default_factory=collections.Counter)  # who holds it, by URI
    events: collections.deque[dict[str, Any]] = field(default_factory=lambda: collections.deque(maxlen=WATCH_EVENTS))
    seq: int = 0
    state: str = "starting"  # starting | watching | stopped
    error: str | None = None
    task: asyncio.Task | None = None
    tick: asyncio.Event = field(default_factory=asyncio.Event)  # set (and replaced) on every change

    def wake(self) -> None:
        self.tick.set()
        self.tick = asyncio.Event()

    def view(self, since: int = 0) -> dict[str, Any]:
        evs = [e for e in self.events if e["seq"] > since]
        return {"sandbox": self.sandbox_id, "path": self.path, "recursive": self.recursive, "state": self.state,
                "events": evs, "next": self.seq, **({"error": self.error} if self.error else {}),
                **({"dropped": True} if self.events and since and self.events[0]["seq"] > since + 1 else {})}


class Watches:
    """Directory watches held by subscribers (MCP resource subscriptions). One envd WatchDir per owner, sandbox,
    path and depth, however many subscriptions share it; `notify(uris)` is called when events arrive."""

    def __init__(self, fleet: Any, notify: Callable[[list[str]], None]):
        self.fleet, self.notify = fleet, notify
        self.hub = hub(fleet)
        self.watches: dict[tuple[str, str, str, bool], Watch] = {}

    def find(self, owner: str, ref: str, path: str, recursive: bool) -> Watch | None:
        sb = self.fleet.get(ref)
        return self.watches.get((owner, sb.id, self.fleet._abs(sb, path), recursive))

    def check(self, owner: str, ref: str, path: str,
              recursive: bool) -> tuple[tuple[str, str, str, bool], Watch | None]:
        """The watch's key and the watch if it is held already; ValueError or StreamLimit if it can't be."""
        sb = self.fleet.get(ref)
        if "\0" in path or len(path) > 4096:
            raise ValueError("invalid path")
        key = (owner, sb.id, self.fleet._abs(sb, path), recursive)
        w = self.watches.get(key)
        if w is None:
            mine = sum(1 for k in self.watches if k[0] == owner)
            if mine >= self.fleet.settings.watch_max_per_token:
                raise StreamLimit(f"{owner} already watches {mine} directories (HANGAR_WATCH_MAX_PER_TOKEN)")
        return key, w

    def acquire(self, owner: str, ref: str, path: str, recursive: bool, uri: str) -> Watch:
        key, w = self.check(owner, ref, path, recursive)
        if w is None:
            w = self.watches[key] = Watch(owner, key[1], key[2], recursive)
            w.task = asyncio.create_task(self._pump(key, w))
        elif w.state == "stopped" and w.task is not None and w.task.done():
            # (review #68) it stopped (a pause, a daemon restart, an error) while held: a new holder tries again
            w.state, w.error = "starting", None
            w.task = asyncio.create_task(self._pump(key, w))
        w.uris[uri] += 1
        return w

    def release(self, w: Watch, uri: str) -> None:
        w.uris[uri] -= 1
        if w.uris[uri] <= 0:
            del w.uris[uri]
        if not w.uris:
            self._stop(w)

    def _stop(self, w: Watch) -> None:
        key = (w.owner, w.sandbox_id, w.path, w.recursive)
        if self.watches.get(key) is w:
            del self.watches[key]
        if w.task is not None:
            w.task.cancel()

    def _add(self, w: Watch, ev: dict[str, Any]) -> None:
        w.seq += 1
        ev["seq"] = w.seq
        w.events.append(ev)
        w.wake()
        self.notify(list(w.uris))

    async def _pump(self, key: tuple[str, str, str, bool], w: Watch) -> None:
        try:
            await self._watch(w)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a watch that fails says why to its readers
            w.error = str(e)[:300] or type(e).__name__
        finally:
            w.state = "stopped"
            w.wake()
            if self.watches.get(key) is w:
                self.notify(list(w.uris))

    async def _watch(self, w: Watch) -> None:
        try:
            _, _, env, ep = await daemon(self.fleet, w.sandbox_id)
        except FleetError as e:
            w.error = str(e)
            return
        if env is None or ep is None:
            w.error = "watching files needs hangar-envd (or E2B's envd) in the sandbox, and it isn't answering"
            return
        async with self.hub.open(w.owner, "watch", w.sandbox_id) as st:
            events = env.stream(ep, "filesystem.Filesystem/WatchDir", {"path": w.path, "recursive": w.recursive},
                                None, keepalive=30)
            try:
                while True:
                    msg = await st.race(anext(events, None))
                    if msg is None:
                        w.error = "the watch ended (the directory was removed?)"
                        return
                    if "start" in msg:
                        w.state = "watching"
                        w.wake()
                        self.notify(list(w.uris))
                    elif isinstance(fs := msg.get("filesystem"), dict):
                        kind = str(fs.get("type") or "").removeprefix("EVENT_TYPE_").lower() or "unknown"
                        self._add(w, {"ts": time.time(), "name": str(fs.get("name") or "")[:4096], "type": kind})
            except StreamClosed as e:
                w.error = str(e)
            except EnvdError as e:
                w.error = f"could not watch {w.path}: {e}"
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.shield(events.aclose())

    async def close(self) -> None:
        for w in list(self.watches.values()):
            self._stop(w)


_PROCS: weakref.WeakKeyDictionary[Any, Processes] = weakref.WeakKeyDictionary()


def processes(fleet: Any) -> Processes:
    """The fleet's background processes, made on first use."""
    p = _PROCS.get(fleet)
    if p is None:
        p = _PROCS[fleet] = Processes(fleet)
    return p
