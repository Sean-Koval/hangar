"""Sandbox latency on the real Docker provider: what a person or an MCP client waits for.

    uv run python scripts/bench/sandbox.py [--runs 10] [--out bench/results/docker.json]

Measures, p50/p95 over --runs:
  create_cold / create_warm      fleet.create -> RUNNING with the repo cloned (no pool / claimed from the pool)
  first_exec                     the first command after create
  exec / read_file / write_file  round trips (64 KiB and 1 MiB files), terminal_send (send + read back)
  checkpoint_turn                session checkpoint after a turn that changed a little
  checkpoint_unchanged           periodic checkpoint when nothing changed (fingerprint only)
  resume_total / resume_restore  suspended session -> sandbox ready; the restore part alone
  turn_first_output              send -> first agent event recorded
  turn_end_detect                agent process exited -> hangar noticed (session_poll_seconds matters here)
  *_provider / *_envd            the same provider calls through `docker exec` and through hangar-envd: exec,
                                 write/read 64 KiB, 1 MiB and 16 MiB, a burst of 1000 execs 50 at a time, and PTY
                                 echo (a keystroke into `cat` until it comes back). Needs envd/build.sh first.

The session has a realistic shape: a 1.5 MB JSON-lines conversation that grows each turn, three commits, a modified
tracked file, ~100 untracked text files and 3 MiB of incompressible untracked data. Needs Docker and git; reuses
the `sesenv` image the Docker e2e tests build (builds it on first use).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from _common import Recorder, emit

from tests.test_e2e_jobs_docker import _bridge_gateway, _free_port

ENV = ("name: sesenv\nbase: mirror.gcr.io/library/ubuntu:24.04\nharnesses: [scripted]\nharness_mode: bake\n"
       "resources: {cpus: 1, memory_gb: 1}\n")

# A scripted harness: keeps a conversation file like Claude Code does and changes the repo a little each turn.
AGENT = r"""set -e
conv="$HOME/.fake/sessions"; mkdir -p "$conv"
id="${HANGAR_SESSION_RESUME:-$HANGAR_SESSION_NEW}"
echo '{"type":"system","subtype":"init"}'
msg="$(head -1 "$HANGAR_PROMPT_FILE")"
case "$msg" in
  setup*)  # conversation: JSON lines quoting code, which compresses like a real transcript
    cat src/*.py | head -c 1500000 | sed 's/^/{"type":"assistant","text":"/; s/$/"}/' > "$conv/$id.jsonl"
    for i in 1 2 3; do echo "change $i" >> "f$i.txt"; git add "f$i.txt"; git commit -qm "step $i"; done
    echo "edited" >> README.md
    mkdir -p notes && for i in $(seq 1 100); do echo "note $i $(date +%s%N)" > "notes/n$i.md"; done
    head -c 3000000 /dev/urandom > blob.bin ;;
  *) cat src/m1.py | head -c 50000 | sed 's/^/{"type":"user","text":"/; s/$/"}/' >> "$conv/$id.jsonl"
     echo "turn" >> README.md
     sleep "${BENCH_AGENT_SLEEP:-2}" ;;
esac
echo '{"type":"result","result":"done"}'
"""


class Calls:
    """Counts provider round trips (exec, read_file, write_file) per phase, and where the time went (those
    calls, encryption, the checkpoint store): fewer calls is the main lever."""

    def __init__(self, prov, svc):
        self.n: dict[str, int] = {}
        self.s: dict[str, float] = {}
        for obj, names in ((prov, ("exec", "read_file", "write_file")), (svc.blobs, ("put", "get")),
                           (svc.vault, ("encrypt", "decrypt"))):
            for name in names:
                self._wrap(obj, name)

    def _wrap(self, obj, name: str) -> None:
        orig = getattr(obj, name)
        if asyncio.iscoroutinefunction(orig):
            async def wrapped(*a, **kw):
                t = time.perf_counter()
                try:
                    return await orig(*a, **kw)
                finally:
                    self._add(name, time.perf_counter() - t)
        else:
            def wrapped(*a, **kw):
                t = time.perf_counter()
                try:
                    return orig(*a, **kw)
                finally:
                    self._add(name, time.perf_counter() - t)
        setattr(obj, name, wrapped)

    def _add(self, name: str, secs: float) -> None:
        self.n[name] = self.n.get(name, 0) + 1
        self.s[name] = self.s.get(name, 0.0) + secs

    def take(self) -> dict[str, str]:
        out = {k: f"{v}x {self.s[k] * 1000:.0f}ms" for k, v in sorted(self.n.items())}
        self.n, self.s = {}, {}
        return out


def git_remote(root: Path) -> tuple[str, subprocess.Popen]:
    bare, seed = root / "repos" / "app.git", root / "seed"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run([*git, "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("bench\n")
    src = seed / "src"
    src.mkdir()
    for i in range(200):  # ~2 MB of source, so clone and diff do real work
        (src / f"m{i}.py").write_text("".join(f"def f{j}():\n    return {i * j}\n" for j in range(300)))
    subprocess.run([*git, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "-C", str(seed), "push", "-q", str(bare), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    port = _free_port()
    daemon = subprocess.Popen(
        ["git", "daemon", "--reuseaddr", "--export-all", f"--port={port}", "--listen=0.0.0.0",
         f"--base-path={root / 'repos'}", str(root / "repos")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    return f"git://{_bridge_gateway()}:{port}/app.git", daemon


async def main(args: argparse.Namespace) -> dict:
    from hangar.broker import Broker
    from hangar.config import Settings
    from hangar.envspec import EnvCatalog
    from hangar.fleet import Fleet
    from hangar.harnesses import HARNESSES, Harness
    from hangar.models import SessionState
    from hangar.sessions import NewSession, SessionService
    from hangar.store import Store
    from hangar.vault import Vault, load_or_create_key

    rec = Recorder("sandbox-docker")
    phases = set(args.phases.split(","))
    root = Path(tempfile.mkdtemp(prefix="hangar-bench-"))
    url, daemon = git_remote(root)
    script = base64.b64encode(AGENT.encode()).decode()
    HARNESSES["scripted"] = Harness(
        name="scripted", title="Scripted", run=f"echo {script} | base64 -d > /tmp/agent.sh && bash /tmp/agent.sh",
        session_dirs=[".fake/sessions"], resume="preset", resume_hint="scripted --resume {id}")
    envs = root / "envs"
    envs.mkdir()
    (envs / "sesenv.yaml").write_text(ENV)
    import os

    s = Settings(data_dir=root / "data", env_dirs=[envs], reaper_interval_seconds=3600,
                 session_poll_seconds=args.poll, enabled_providers=[], default_provider="docker",
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.data_dir.mkdir(parents=True)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    store = Store(s.db_path)
    vault = Vault(store, load_or_create_key(None, s.data_dir))
    fleet = Fleet(s, store, Broker(store, vault, s.sandbox_user), EnvCatalog([envs]))
    await fleet.start()
    svc = SessionService(fleet, vault)
    await svc.start()
    prov = fleet.provider("docker")
    created: list[str] = []
    try:
        spec = fleet.catalog.get("sesenv")
        image = await fleet._ensure_image(spec, "docker", "bench", "scripted")
        rec.note("image", image)
        calls = Calls(prov, svc)

        if "create" in phases:
            # ---- create: cold, then warm (pool of one, refilled between runs)
            for i in range(args.runs):
                calls.take()
                with rec.time("create_cold"):
                    sb = await fleet.create("sesenv", repo=url, harness="scripted", actor="bench")
                if i == 0:
                    rec.note("calls_create", calls.take())
                created.append(sb.id)
                with rec.time("first_exec"):
                    await fleet.exec(sb.id, "true")
                await fleet.destroy(sb.id)
            (envs / "sesenv.yaml").write_text(ENV + "warm_pool: 1\n")
            fleet.catalog.reload()
            wspec = fleet.catalog.get("sesenv")
            assert fleet._tag_for(wspec, "docker")[0] == fleet._tag_for(spec, "docker")[0], "warm_pool changed the tag"
            for _ in range(args.runs):
                await prov.sync_warm_pool(wspec, image)
                with rec.time("create_warm"):
                    sb = await fleet.create("sesenv", repo=url, harness="scripted", actor="bench")
                assert sb.info.get("warm"), "expected a warm claim"
                created.append(sb.id)
                await fleet.destroy(sb.id)
            (envs / "sesenv.yaml").write_text(ENV + "warm_pool: 0\n")
            fleet.catalog.reload()
            await prov.sync_warm_pool(fleet.catalog.get("sesenv"), image)

        if "io" in phases:
            # ---- I/O round trips on one sandbox
            sb = await fleet.create("sesenv", repo=url, harness="scripted", actor="bench")
            created.append(sb.id)
            small, big = b"x" * 65536, bytes(range(256)) * 4096
            for _ in range(args.runs * 3):
                with rec.time("exec"):
                    await fleet.exec(sb.id, "true")
                with rec.time("write_file_64k"):
                    await fleet.write_file(sb.id, "bench/small.bin", small)
                with rec.time("read_file_64k"):
                    await fleet.read_file(sb.id, "bench/small.bin")
                with rec.time("write_file_1m"):
                    await fleet.write_file(sb.id, "bench/big.bin", big)
                with rec.time("read_file_1m"):
                    await fleet.read_file(sb.id, "bench/big.bin")
            await fleet.terminal_open(sb.id, "bench")
            for i in range(args.runs * 3):
                with rec.time("terminal_send"):
                    await fleet.terminal_send(sb.id, "bench", f"echo mark-{i}", wait_ms=5000, wait_for=f"^mark-{i}")
            await fleet.destroy(sb.id)

        if "envd" in phases:  # --- envd: the same calls through `docker exec` and through hangar-envd
            sb = await fleet.create("sesenv", repo=url, harness="scripted", actor="bench")
            created.append(sb.id)
            await envd_phase(rec, prov, fleet.get(sb.id), args.runs)
            await fleet.destroy(sb.id)

        # ---- a session: checkpoint after turns, then suspend/resume
        s0 = svc.new(NewSession(name="bench", environment="sesenv", repo=url, message="setup the work",
                                idle_minutes=0), owner="bench", requester="bench")
        await settle(svc, s0.id)
        sess = svc.get(s0.id)
        assert sess.turns[-1].exit_code == 0, sess.turns[-1]
        rec.note("checkpoint_parts_bytes", dict(sess.checkpoints[-1].parts))
        for i in range(args.runs if "checkpoint" in phases else 0):
            await fleet.exec(sess.sandbox_id, "sed 's/^/{\"type\":\"user\",\"text\":\"/; s/$/\"}/' src/m2.py >> "
                                              f"~/.fake/sessions/{sess.harness_session}.jsonl; echo x >> README.md")
            calls.take()
            with rec.time("checkpoint_turn"):
                async with svc._locks[sess.id]:
                    ck = await svc._checkpoint(sess, "turn")
            assert ck is not None
            if i == 0:
                rec.note("calls_checkpoint_turn", calls.take())
            with rec.time("checkpoint_unchanged"):
                async with svc._locks[sess.id]:
                    assert await svc._checkpoint(sess, "interval") is None
            if i == 0:
                rec.note("calls_checkpoint_unchanged", calls.take())

        restore = svc._restore

        async def timed_restore(*a, **kw):
            calls.take()
            with rec.time("resume_restore"):
                out = await restore(*a, **kw)
            rec.note("calls_restore", calls.take())
            return out

        svc._restore = timed_restore
        for _ in range(args.runs if "resume" in phases else 0):
            with rec.time("suspend"):
                await svc.release(sess, "bench")
            with rec.time("resume_total"):
                await svc.ensure_sandbox(sess)
        await verify_restored(fleet, sess)

        # ---- turns: time to first output and to noticing the agent exited
        for i in range(args.runs if "turn" in phases else 0):
            t0 = time.time()
            before = len(svc.events(sess.id, tail=1000))
            calls.take()
            svc.send(sess.id, f"turn {i}", requester="bench")
            while len(svc.events(sess.id, tail=1000)) <= before:
                await asyncio.sleep(0.01)
            rec.add("turn_first_output", time.time() - t0)
            await settle(svc, sess.id)
            if i == 0:
                rec.note("calls_turn_with_checkpoint", calls.take())
            t = sess.turns[-1]
            sb = fleet.get(sess.sandbox_id)
            res = await fleet.exec(sb.id, f"stat -c %.9Y ~/.hangar/runs/{t.run_id}.exit", quiet=True)
            rec.add("turn_end_detect", max(0.0, t.finished_at - float(res.stdout.strip())))
        assert svc.get(sess.id).state == SessionState.IDLE
        await svc.release(sess, "bench")
    finally:
        await svc.stop()
        for sid in created:
            try:
                if fleet.get(sid).state.value != "stopped":
                    await fleet.destroy(sid)
            except Exception:  # noqa: BLE001
                pass
        for sb in fleet.list():
            await fleet.destroy(sb.id)
        await fleet.stop()
        daemon.terminate()
        shutil.rmtree(root, ignore_errors=True)
    return rec.result(provider="docker", runs=args.runs, session_poll_seconds=args.poll)


async def envd_phase(rec, prov, sb, runs: int) -> None:
    """--- envd: provider exec vs hangar-envd on one sandbox. Metrics end in `_provider` or `_envd`."""
    from hangar.envd import OFF

    router = prov.envd
    ep = await router.ready(sb)
    if ep is None:
        rec.note("envd", "unavailable (no binary, or not reachable): run envd/build.sh")
        return
    rec.note("envd", {"url": ep.url, "version": ep.version})
    small, big, huge = b"x" * 65536, bytes(range(256)) * 4096, os.urandom(16 << 20)
    for mode in ("provider", "envd"):
        prov.envd = OFF if mode == "provider" else router
        await prov.exec(sb, "true")  # warm both paths' connections
        for _ in range(runs * 5):
            with rec.time(f"exec_{mode}"):
                await prov.exec(sb, "true")
        for name, data in (("64k", small), ("1m", big), ("16m", huge)):
            for _ in range(runs * (1 if name == "16m" else 3)):
                with rec.time(f"write_{name}_{mode}"):
                    await prov.write_file(sb, f"/workspace/bench/{name}.bin", data)
                with rec.time(f"read_{name}_{mode}"):
                    got = await prov.read_file(sb, f"/workspace/bench/{name}.bin")
                assert got == data
        # A burst: 1000 commands, 50 at a time (what a fleet of agents polling their runs looks like).
        async def one(sem: asyncio.Semaphore) -> None:
            async with sem:
                await prov.exec(sb, "true")

        sem = asyncio.Semaphore(50)

        t = time.perf_counter()
        await asyncio.gather(*[one(sem) for _ in range(1000)])
        rec.add(f"burst_1000_{mode}", time.perf_counter() - t)
        # PTY echo: one keystroke into `cat` on a terminal until it comes back.
        term = await prov.open_terminal(sb, ["cat"], 80, 24)
        try:
            await asyncio.sleep(0.2)
            for i in range(runs * 3):
                key = chr(ord("a") + i % 26).encode()
                t = time.perf_counter()
                await term.write(key)
                seen = b""
                while key not in seen:
                    seen += await asyncio.wait_for(term.read(), 5)
                rec.add(f"pty_echo_{mode}", time.perf_counter() - t)
        finally:
            await term.close()
    prov.envd = router
    res = await prov.exec(sb, "for p in /proc/[0-9]*; do case \"$(cat $p/comm 2>/dev/null)\" in hangar-envd*) "
                              "grep -E 'VmRSS|VmHWM' $p/status;; esac; done")
    rec.note("envd_memory_after", " ".join(res.stdout.split()))


async def settle(svc, sid: str, timeout: float = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = svc.get(sid)
        if not svc.busy(sid) and s.state.value in ("idle", "suspended", "failed"):
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"session {sid} did not settle")


async def verify_restored(fleet, sess) -> None:
    """The resumed sandbox has the session's work: commits, the uncommitted edit, untracked files."""
    res = await fleet.exec(sess.sandbox_id, "git log --oneline | wc -l; grep -c edited README.md; ls notes | wc -l; "
                                            "stat -c %s blob.bin", quiet=True)
    commits, edited, notes, blob = res.stdout.split()
    assert int(commits) >= 4 and edited == "1" and notes == "100" and blob == "3000000", res.stdout


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--poll", type=float, default=5.0, help="session_poll_seconds (the production default is 5)")
    p.add_argument("--out", help="also write the JSON result here")
    p.add_argument("--phases", default="create,io,envd,checkpoint,resume,turn", help="comma-separated subset to run")
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING)
    emit(asyncio.run(main(a)), a.out)
