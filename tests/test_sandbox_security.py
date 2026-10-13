"""Attacks on the sandbox boundary (overnight/sandbox-security): an agent that controls its sandbox tries to push
outside its grant, hold the gateway, or plant files on the requester's machine through `session pull`.

Each test is a proof of concept against the code as it was, kept as the regression test for its fix. The gateway
ones push with real git (or raw pkt-lines) through uvicorn to a `git http-backend` upstream (tests/test_gateway_git).
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import subprocess
import tarfile
import time
from pathlib import Path

import httpx
import pytest

from hangar import exchange
from hangar.ckpt import PACK_READ_LIMIT
from hangar.gateway import FLUSH, pkt
from hangar.models import Session
from tests.test_exchange import settle, svc, world  # noqa: F401 - fixtures
from tests.test_gateway_git import _box, _git, gw, upstream  # noqa: F401 - fixtures

ZERO = "0" * 40


def _grant(fleet, sb) -> str:
    creds = (_box(fleet, sb) / "home/agent/.git-credentials").read_text()
    return creds.split("x-access-token:")[1].split("@")[0]


def _bare(up) -> Path:
    return up.root / "acme/widgets.git"


def _sha(up, ref: str) -> str:
    return _git("--git-dir", str(_bare(up)), "rev-parse", ref).strip()


async def _receive_pack(fleet, token: str, body: bytes) -> httpx.Response:
    url = f"{fleet.settings.gateway_url}/git/github.com/acme/widgets.git/git-receive-pack"
    async with httpx.AsyncClient(timeout=30) as c:
        return await c.post(url, content=body, auth=("x-access-token", token),
                            headers={"Content-Type": "application/x-git-receive-pack-request"})


def _empty_pack() -> bytes:
    return subprocess.run(["git", "pack-objects", "--stdout"], input=b"", capture_output=True, check=True).stdout


# ============================================================================ push options
async def test_push_options_cannot_be_smuggled_in_as_a_capability_value(fleet, gw, upstream):  # noqa: F811
    """git-receive-pack turns push options on for `push-options` *or* `push-options=<anything>`
    (parse_feature_request matches a value too); the gateway only refused the bare name."""
    bare = _bare(upstream)
    _git("--git-dir", str(bare), "config", "receive.advertisePushOptions", "true")
    seen = upstream.root / "push-options.txt"
    hook = bare / "hooks/pre-receive"
    hook.write_text(f'#!/bin/sh\necho "$GIT_PUSH_OPTION_COUNT $GIT_PUSH_OPTION_0" > {seen}\ncat >/dev/null\n')
    hook.chmod(0o755)
    sb = await fleet.create("gw", work_branch="hangar/job-1")
    main = _sha(upstream, "main")
    for caps in ("report-status push-options=1", "report-status\tpush-options", "push-options report-status"):
        body = (pkt(f"{ZERO} {main} refs/heads/hangar/opt\0{caps}\n".encode()) + FLUSH
                + pkt(b"merge_request.create\n") + FLUSH + _empty_pack())
        r = await _receive_pack(fleet, _grant(fleet, sb), body)
        assert r.status_code == 200
        assert not seen.exists(), f"{caps!r}: the upstream hook saw push options: {seen.read_text()}"
        assert b"hangar: push options are not allowed" in r.content, caps
    assert "refs/heads/hangar/opt" not in upstream.refs()


# ============================================================================ the hangar/** creation rule
async def test_pattern_cannot_create_or_block_session_branches(fleet, gw, upstream, store):  # noqa: F811
    """A sandbox may create branches under hangar/, but not one a session uses (or will use: hangar/session/<name>
    is predictable), and not one that blocks it by a directory/file conflict. A new session starts from its
    branch if it exists on the remote, so a planted branch would have become another session's starting point."""
    store.put(Session(name="later", environment="gw", branch="hangar/feature-x"))
    sb = await fleet.create("gw", work_branch="hangar/job-1")
    await fleet.exec(sb.id, "git commit -q --allow-empty -m planted")
    for ref, why in [("refs/heads/hangar/session/future", "reserved for sessions"),
                     ("refs/heads/hangar/session", "reserved for sessions"),
                     ("refs/heads/hangar/session/future/x", "reserved for sessions"),
                     ("refs/heads/hangar/feature-x", "belongs to session later"),
                     ("refs/heads/hangar/feature-x/sub", "belongs to session later")]:
        res = await fleet.exec(sb.id, f"git push origin HEAD:{ref}")
        assert res.exit_code != 0 and why in res.stderr, (ref, res.stderr)
    refs = upstream.refs()
    assert "hangar/session" not in refs and "feature-x" not in refs
    # its own branch, and unrelated new branches, still work
    for ref in ("refs/heads/hangar/job-1", "refs/heads/hangar/scratch"):
        res = await fleet.exec(sb.id, f"git push -q origin HEAD:{ref}")
        assert res.exit_code == 0, res.stderr


async def test_best_of_n_winner_still_creates_the_job_branch(fleet, gw, upstream, store):  # noqa: F811
    """The winner's session takes the job branch before delivery and creates it through the pattern; the job's own
    session has the same branch, which must not count against it. Another sandbox still can't."""
    store.put(Session(name="job-9", environment="gw", branch="hangar/job-9"))
    winner = Session(name="job-9-attempt-1", environment="gw", branch="hangar/job-9")
    store.put(winner)
    sb = await fleet.create("gw", work_branch="hangar/job-9-a-1", labels={"hangar.session": winner.id})
    other = await fleet.create("gw", work_branch="hangar/job-10")
    res = await fleet.exec(other.id, "git push origin HEAD:refs/heads/hangar/job-9")
    assert res.exit_code != 0 and "belongs to session" in res.stderr, res.stderr
    res = await fleet.exec(sb.id, "git commit -q --allow-empty -m win && "
                                  "git push -q origin HEAD:refs/heads/hangar/job-9")
    assert res.exit_code == 0, res.stderr


async def test_pattern_cannot_squat_below_another_sandboxes_branch(fleet, gw, upstream):  # noqa: F811
    """hangar/job-2 isn't pushed yet; creating hangar/job-2/x would make that push fail forever (deletes are
    refused, so nothing could clean it up)."""
    a = await fleet.create("gw", work_branch="hangar/job-1")
    await fleet.create("gw", work_branch="hangar/job-2")
    res = await fleet.exec(a.id, "git push origin HEAD:refs/heads/hangar/job-2/x")
    assert res.exit_code != 0 and "belongs to another sandbox" in res.stderr, res.stderr


# ============================================================================ holding the gateway
async def test_command_list_has_a_total_deadline(fleet, gw, upstream, settings):  # noqa: F811
    """The command list was read with a timeout per chunk, so a trickle of bytes held the request (and an
    upstream-bound slot in the control plane's event loop) forever."""
    settings.gateway_timeout_seconds = 0.6
    sb = await fleet.create("gw", work_branch="hangar/job-1")
    auth = base64.b64encode(f"x-access-token:{_grant(fleet, sb)}".encode()).decode()
    port = int(settings.gateway_url.rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"POST /git/github.com/acme/widgets.git/git-receive-pack HTTP/1.1\r\nHost: x\r\n"
                 f"Authorization: Basic {auth}\r\nTransfer-Encoding: chunked\r\n\r\n".encode())
    line = pkt(f"{ZERO} {'1' * 40} refs/heads/hangar/job-1\0report-status\n".encode())

    async def trickle():  # one byte every 0.2s: each arrives well inside the old per-chunk timeout
        for b in line:
            writer.write(b"1\r\n" + bytes([b]) + b"\r\n")
            await writer.drain()
            await asyncio.sleep(0.2)

    sender = asyncio.create_task(trickle())
    t0 = time.monotonic()
    try:
        status = await asyncio.wait_for(reader.readline(), timeout=10)
    finally:
        sender.cancel()
        writer.close()
    assert b" 408 " in status
    assert time.monotonic() - t0 < 5


def test_command_list_is_scanned_once(monkeypatch):
    """Re-parsing the whole buffer on every chunk is quadratic: one sandbox sending 1-byte segments could stall the
    control plane's event loop. The command list is now scanned incrementally and parsed once."""
    from hangar import gateway

    calls = []
    real = gateway.parse_commands
    monkeypatch.setattr(gateway, "parse_commands", lambda b: calls.append(1) or real(b))
    body = b"".join(pkt(f"shallow {'a' * 40}".encode()) for _ in range(500)) + \
        pkt(f"{ZERO} {'1' * 40} refs/heads/x\0report-status\n".encode()) + FLUSH + b"PACK"
    scan = gateway.CommandScanner()
    done = None
    for i in range(len(body)):
        done = scan.feed(body[i:i + 1])
        if done is not None:
            break
    assert done is not None and [c.ref for c in done[0].commands] == ["refs/heads/x"]
    assert len(calls) == 1


async def test_auth_failures_are_rate_limited_per_source(fleet, gw, upstream):  # noqa: F811
    url = f"{fleet.settings.gateway_url}/git/github.com/acme/widgets.git/info/refs?service=git-upload-pack"
    async with httpx.AsyncClient() as c:
        codes = [(await c.get(url, auth=("x", f"hgs_guess{i}"))).status_code for i in range(40)]
        assert codes[0] == 401 and codes[-1] == 429
        # git's first request carries no credentials; those never count
        assert (await c.get(url)).status_code == 401


async def test_concurrent_requests_per_grant_are_capped(fleet, gw, upstream):  # noqa: F811
    from hangar import gateway

    sb = await fleet.create("gw", work_branch="hangar/job-1")
    grant = fleet.gateway.grants(sb.id)[0]
    fleet.gateway._inflight[grant.token_hash] = gateway.MAX_INFLIGHT_PER_GRANT
    url = f"{fleet.settings.gateway_url}/git/github.com/acme/widgets.git/info/refs?service=git-upload-pack"
    async with httpx.AsyncClient() as c:
        r = await c.get(url, auth=("x-access-token", _grant(fleet, sb)))
    assert r.status_code == 429
    fleet.gateway._inflight[grant.token_hash] = 0
    async with httpx.AsyncClient() as c:
        assert (await c.get(url, auth=("x-access-token", _grant(fleet, sb)))).status_code == 200
    for _ in range(100):  # released when the ASGI call returns, just after the client has the response
        if fleet.gateway._inflight.get(grant.token_hash, 0) == 0:
            break
        await asyncio.sleep(0.02)
    assert fleet.gateway._inflight.get(grant.token_hash, 0) == 0


# ============================================================================ session pull on the requester's machine
def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "laptop"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README").write_text("hi\n")
    _git("add", "README", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def _tgz(members: list[tuple[str, str, bytes | str]]) -> bytes:
    """(kind, name, data-or-linkname) with kind file | dir | sym | link."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for kind, name, data in members:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size, ti.mode = len(data), 0o755
                tf.addfile(ti, io.BytesIO(data))  # type: ignore[arg-type]
            elif kind == "dir":
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(ti)
            else:
                ti.type, ti.linkname = (tarfile.SYMTYPE if kind == "sym" else tarfile.LNKTYPE), str(data)
                tf.addfile(ti)
    return buf.getvalue()


@pytest.mark.parametrize("members,planted", [
    # straight into the git dir
    ([("file", ".git/hooks/pre-commit", b"#!/bin/sh\necho pwned\n")], ".git/hooks/pre-commit"),
    ([("file", ".GIT/hooks/post-checkout", b"#!/bin/sh\n")], ".git/hooks/post-checkout"),
    ([("file", ".git/config", b"[core]\n\tfsmonitor = touch /tmp/pwned\n")], ".git/config"),
    # through a symlink the archive creates first (tarfile's "data" filter allows a link that stays inside)
    ([("sym", "x", ".git"), ("file", "x/hooks/pre-commit", b"#!/bin/sh\n")], ".git/hooks/pre-commit"),
    ([("sym", "y", ".git/hooks/pre-push"), ("file", "y", b"#!/bin/sh\n")], ".git/hooks/pre-push"),
    # a hard link onto git's own config, then content through it
    ([("link", "z", ".git/description")], None),
])
def test_pull_checkout_never_writes_into_the_git_dir(tmp_path, members, planted):
    repo = _repo(tmp_path)
    head = _git("rev-parse", "HEAD", cwd=repo).strip()
    before = {p: p.read_bytes() for p in (repo / ".git").rglob("*") if p.is_file()}
    manifest = {"head": head, "bundle_ref": None}
    exchange.pull_into(repo, "s1", manifest, {"untracked.tgz": _tgz(members)}, checkout=True)
    if planted:
        assert not (repo / planted).exists() or (repo / planted).read_bytes() == before.get(repo / planted)
    after = {p: p.read_bytes() for p in (repo / ".git").rglob("*") if p.is_file() and "logs" not in p.parts
             and p.name not in ("ORIG_HEAD", "index", "HEAD") and "refs" not in p.parts}
    assert after == {p: v for p, v in before.items() if p in after}
    assert not any(os.path.islink(repo / m[1]) and m[0] == "file" for m in members)
    assert not (repo / "z").exists() and not (repo / ".GIT").exists()  # .GIT is .git on macOS


def test_pull_checkout_does_not_write_through_a_symlink_from_the_patch(tmp_path):
    """The wip.patch can create a symlink (git apply allows any target); the untracked files must not then be
    written through it."""
    repo = _repo(tmp_path)
    head = _git("rev-parse", "HEAD", cwd=repo).strip()
    (repo / "hooklink").symlink_to(".git/hooks/pre-commit")
    wip = subprocess.run(["git", "-C", str(repo), "diff", "--no-index", "--binary", "--", "/dev/null", "hooklink"],
                         capture_output=True).stdout
    os.remove(repo / "hooklink")
    assert b"new file mode 120000" in wip
    data = _tgz([("file", "hooklink", b"#!/bin/sh\necho pwned\n")])
    exchange.pull_into(repo, "s1", {"head": head}, {"wip.patch": wip, "untracked.tgz": data}, checkout=True)
    assert os.path.islink(repo / "hooklink")
    assert not (repo / ".git/hooks/pre-commit").exists()


def test_pull_checkout_still_restores_ordinary_untracked_files(tmp_path):
    repo = _repo(tmp_path)
    head = _git("rev-parse", "HEAD", cwd=repo).strip()
    data = _tgz([("dir", "notes", b""), ("file", "notes/todo.md", b"- a\n"), ("file", "run.sh", b"#!/bin/sh\n"),
                 ("sym", "latest", "notes/todo.md"), ("file", "README", b"theirs\n")])
    notes = exchange.pull_into(repo, "s1", {"head": head}, {"untracked.tgz": data}, checkout=True)
    assert (repo / "notes/todo.md").read_text() == "- a\n"
    assert os.access(repo / "run.sh", os.X_OK)
    assert os.readlink(repo / "latest") == "notes/todo.md"
    assert (repo / "README").read_text() == "hi\n" and any("kept your README" in n for n in notes)


@pytest.mark.parametrize("member", [
    ("file", "../outside.txt", b"x"), ("file", "/tmp/abs-hangar-test.txt", b"x"),
    ("sym", "esc", "../../outside"), ("sym", "abs", "/etc/passwd"),
])
def test_pull_checkout_stays_inside_the_checkout(tmp_path, member):
    repo = _repo(tmp_path)
    head = _git("rev-parse", "HEAD", cwd=repo).strip()
    notes = exchange.pull_into(repo, "s1", {"head": head}, {"untracked.tgz": _tgz([member])}, checkout=True)
    assert not (tmp_path / "outside.txt").exists() and not Path("/tmp/abs-hangar-test.txt").exists()
    assert not os.path.lexists(repo / member[1].lstrip("/")) or member[0] == "file"
    assert any("skipped" in n for n in notes)


def test_pull_refuses_a_head_that_is_not_a_commit_id(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(exchange.ExchangeError, match="not a commit id"):
        exchange.pull_into(repo, "s1", {"head": "--orphan=x"}, {}, checkout=True)


def test_untracked_archive_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(exchange, "MAX_UNTRACKED_FILES", 3)
    repo = _repo(tmp_path)
    head = _git("rev-parse", "HEAD", cwd=repo).strip()
    data = _tgz([("file", f"f{i}", b"x") for i in range(5)])
    notes = exchange.pull_into(repo, "s1", {"head": head}, {"untracked.tgz": data}, checkout=True)
    assert sum((repo / f"f{i}").exists() for i in range(5)) == 3 and any("more than 3" in n for n in notes)


def test_untracked_archive_cannot_fill_the_requesters_disk(tmp_path, monkeypatch):
    """The archive is capped compressed, not expanded: a few MB of gzip can expand to many GB on the requester's
    machine. What is restored is capped in bytes too."""
    monkeypatch.setattr(exchange, "MAX_UNTRACKED_BYTES", 1 << 20, raising=False)
    repo = _repo(tmp_path)
    head = _git("rev-parse", "HEAD", cwd=repo).strip()
    data = _tgz([("file", "small", b"ok\n"), ("file", "bomb", bytes(2 << 20)), ("file", "after", b"ok\n")])
    assert len(data) < 64 * 1024
    notes = exchange.pull_into(repo, "s1", {"head": head}, {"untracked.tgz": data}, checkout=True)
    assert not (repo / "bomb").exists()
    assert (repo / "small").read_bytes() == b"ok\n" and (repo / "after").read_bytes() == b"ok\n"
    assert any("bomb" in n and "MB" in n for n in notes), notes


# ============================================================================ review of #48 (second pass)
# An agent whose ~/.hangar/env (sourced by every command hangar runs) makes `stat` and `find` report 1 byte, then
# leaves files over every cap. Hangar checked sizes with those commands, then read the files whole into memory.
LIAR = r"""#!/usr/bin/env bash
cat >> "$HOME/.hangar/env" <<'EOF'
stat() { local f=""; [ "$1" = -c ] && { f="$2"; shift 2; }; for a in "$@"; do
  if [ "$f" = "%s %n" ]; then echo "1 $a"; else echo 1; fi; done; }
find() { command find "$@" | tr '\0' '\n' | sed -E 's/^[0-9]+ /1 /' | tr '\n' '\0'; }
EOF
head -c 3000000 /dev/zero > "$HANGAR_IO/out/artifacts/big.bin"
printf '"%s"' "$(head -c 3000000 /dev/zero | tr '\0' a)" > "$HANGAR_IO/out/result.json"
head -c 3000000 /dev/urandom > untracked-big.bin
echo '{"type":"result","result":"done"}'
"""


@pytest.mark.parametrize("packed", [True, False])  # integrate (#53): parts come back in a pack, or one by one
async def test_sizes_reported_by_the_sandbox_do_not_bound_what_hangar_reads(svc, world, fleet, monkeypatch,  # noqa: F811
                                                                            packed):
    from hangar import ckpt
    from hangar.harnesses import HARNESSES, Harness
    from hangar.sessions import NewSession

    if not packed:
        monkeypatch.setattr(ckpt, "PACK_LIMIT", 0)  # nothing fits a pack: every part is read on its own

    agent = world["root"] / "liar.sh"
    agent.write_text(LIAR)
    monkeypatch.setitem(HARNESSES, "liar", Harness(name="liar", title="Liar", run=f"bash {agent}"))
    (world["envs"] / "liar.yaml").write_text("name: liar\nharnesses: [liar]\n")
    fleet.catalog.reload()
    svc.settings.exchange_max_artifact_mb = 1
    svc.settings.exchange_max_result_kb = 1024
    svc.settings.session_max_untracked_mb = 1
    reads: list[tuple[str, int | None, int]] = []
    prov = fleet.providers["docker"]
    real = prov.read_file

    async def spy(sb, path, limit=None):
        data = await real(sb, path, limit)
        reads.append((path, limit, len(data or b"")))
        return data

    monkeypatch.setattr(prov, "read_file", spy)
    svc.new(NewSession(name="liar", environment="liar", repo=world["url"], message="go"), owner="admin",
            requester="sean")
    s = await settle(svc, "liar")
    t = s.turns[0]
    assert not t.artifacts and any("big.bin" in w and "limit" in w for w in t.warnings), t.warnings
    assert t.result is None and "limit" in (t.result_error or ""), t.result_error
    ck = s.checkpoints[-1]
    assert "untracked.tgz" not in ck.parts and any("untracked.tgz" in w for w in ck.warnings), ck.warnings
    # integrate (#53): the checkpoint's parts come back as one pack, a single read capped at PACK_READ_LIMIT; the
    # untracked.tgz inside it was over its own cap, so it was dropped (above) rather than kept
    packs = [(lim, n) for p, lim, n in reads if p.endswith("/pack.tar")]
    assert bool(packs) == packed and all(lim is not None and lim <= PACK_READ_LIMIT for lim, _ in packs), packs
    big = [(p, n) for p, _, n in reads if n > 1_100_000 and not p.endswith("/pack.tar")]
    assert not big, f"read whole from the sandbox: {big}"
async def test_idle_connections_cannot_lock_other_sandboxes_out(fleet):
    """uvicorn's limit_concurrency counts open connections, including ones that never send a request (there is no
    header timeout), and answers everyone 503 past it. So one sandbox holding a few hundred idle sockets locked
    every other sandbox out of the gateway."""
    import resource

    from hangar.gateway import _Server

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < 2048:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 4096), hard))
        except (ValueError, OSError):
            pytest.skip("needs ~1300 file descriptors")
    srv = _Server(fleet.gateway.asgi_app(), "127.0.0.1", 0)
    srv.start()
    held = []
    try:
        for _ in range(200):
            if srv.server.started:
                break
            await asyncio.sleep(0.02)
        port = srv.server.servers[0].sockets[0].getsockname()[1]
        for _ in range(600):
            held.append(await asyncio.open_connection("127.0.0.1", port))
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"http://127.0.0.1:{port}/healthz")
        assert r.status_code == 200, r.text
    finally:
        for _, w in held:
            w.close()
        await srv.stop()


async def test_inflight_cap_reads_the_same_authorization_header_as_the_gateway(fleet, gw, upstream):  # noqa: F811
    """The cap keyed on the last Authorization header and the gateway authenticated with the first, so a second,
    made-up grant on every request sidestepped the per-grant cap."""
    from hangar import gateway

    sb = await fleet.create("gw", work_branch="hangar/job-1")
    grant = fleet.gateway.grants(sb.id)[0]
    fleet.gateway._inflight[grant.token_hash] = gateway.MAX_INFLIGHT_PER_GRANT
    port = int(fleet.settings.gateway_url.rsplit(":", 1)[1])
    good = base64.b64encode(f"x-access-token:{_grant(fleet, sb)}".encode()).decode()
    decoy = base64.b64encode(b"x-access-token:hgs_decoy").decode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET /git/github.com/acme/widgets.git/info/refs?service=git-upload-pack HTTP/1.1\r\n"
                     f"Host: x\r\nAuthorization: Basic {good}\r\nAuthorization: Basic {decoy}\r\n"
                     f"Connection: close\r\n\r\n".encode())
        status = await asyncio.wait_for(reader.readline(), timeout=10)
    finally:
        writer.close()
        fleet.gateway._inflight.pop(grant.token_hash, None)
    assert b" 429 " in status, status


async def test_pattern_cannot_squat_a_queued_jobs_branch(fleet, gw, upstream, store):  # noqa: F811
    """A job that is still queued has no session or sandbox yet, but its branch is already decided (and shown on
    its issue). Creating it, or a branch below it, made the job fail with bad_branch when it started."""
    from hangar.models import Job, JobState

    url = "https://github.com/acme/widgets.git"
    store.put(Job(title="t", instructions="x", environment="gw", repo=url, branch="hangar/job-abcd1234"))
    store.put(Job(title="t", instructions="x", environment="gw", repo=url, branch="hangar/job-done0000",
                  state=JobState.SUCCEEDED))
    sb = await fleet.create("gw", work_branch="hangar/job-1")
    await fleet.exec(sb.id, "git commit -q --allow-empty -m planted")
    for ref in ("refs/heads/hangar/job-abcd1234", "refs/heads/hangar/job-abcd1234/x"):
        res = await fleet.exec(sb.id, f"git push origin HEAD:{ref}")
        assert res.exit_code != 0 and "belongs to job job-" in res.stderr, (ref, res.stderr)
    assert "job-abcd1234" not in upstream.refs()
    res = await fleet.exec(sb.id, "git push -q origin HEAD:refs/heads/hangar/job-done0000-followup")
    assert res.exit_code == 0, res.stderr


class _Flood:
    """An exec websocket whose command prints `frames` chunks of stdout (Kubernetes provider)."""

    def __init__(self, chunks: list[str]):
        self.chunks, self.returncode = chunks, None

    def update(self, timeout=0):
        pass

    def is_open(self):
        return bool(self.chunks)

    def peek_stdout(self):
        return bool(self.chunks)

    def read_stdout(self):
        chunk = self.chunks.pop()
        if not self.chunks:
            self.returncode = 0
        return chunk

    def peek_stderr(self):
        return False

    def close(self):
        pass


async def test_kubernetes_exec_and_reads_keep_bounded_output(monkeypatch):
    """The Kubernetes provider kept every byte a command printed, then cut it to the last 256 KB."""
    import tracemalloc

    import hangar.providers.kubernetes as k
    from hangar.models import Sandbox
    from tests.test_kubernetes import provider

    p = provider()
    p.core = type("Core", (), {"connect_get_namespaced_pod_exec": None})()
    sb = Sandbox(name="a", environment="py", provider="kubernetes", info={"pod": "pod-1"})
    chunk = "a" * (1 << 20)
    monkeypatch.setattr(k, "k8s_stream", lambda fn, pod, ns, **kw: _Flood([chunk] * 200))
    tracemalloc.start()
    try:
        res = await p.exec(sb, "yes")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert res.exit_code == 0 and res.truncated and len(res.stdout) == k.MAX_OUTPUT
    assert peak < 32 * 1024 * 1024, f"exec held {peak // 1024 // 1024} MB"

    def head(fn, pod, ns, **kw):  # a real `head -c N | base64`
        n = int(kw["command"][-1].split("head -c ")[1].split()[0])
        return _Flood([base64.b64encode(b"x" * min(n, 5000)).decode()])

    monkeypatch.setattr(k, "k8s_stream", head)
    assert await p.read_file(sb, "/home/agent/f", limit=100) == b"x" * 101
    assert await p.read_file(sb, "/home/agent/f", limit=10_000) == b"x" * 5000
    # something in the sandbox that isn't head: more than head prints is refused, not decoded
    monkeypatch.setattr(k, "k8s_stream", lambda fn, pod, ns, **kw: _Flood([chunk] * 3))
    assert await p.read_file(sb, "/home/agent/f", limit=100) is None
