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
from hangar.gateway import FLUSH, pkt
from hangar.models import Session
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
