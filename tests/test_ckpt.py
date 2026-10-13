"""Faster checkpoints (spec 0004 M1, 0010 M4): one capture exec and one read, unchanged parts copied inside the
store, restore in one write and one exec, fallback on damaged parts. Fake provider, real git and tmux."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import tarfile
from types import SimpleNamespace

import httpx
import pytest
from docker.errors import NotFound

from hangar import ckpt
from hangar.checkpoints import GCSStore, LocalStore
from hangar.exchange import patch_script
from hangar.models import SessionState
from hangar.sessions import NewSession
from tests.test_session_streams import box, settle, svc, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


def _count(monkeypatch, prov) -> dict[str, int]:
    calls = {"exec": 0, "read_file": 0, "write_file": 0}
    for name in calls:
        orig = getattr(prov, name)

        async def wrapped(*a, _orig=orig, _name=name, **kw):
            calls[_name] += 1
            return await _orig(*a, **kw)

        monkeypatch.setattr(prov, name, wrapped)
    return calls


async def test_unchanged_parts_are_copied_and_survive_pruning(svc, world, fleet, settings, monkeypatch):  # noqa: F811
    settings.session_keep_checkpoints = 1
    svc.new(NewSession(name="inc", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "inc")
    first = s.checkpoints[-1]
    assert set(first.digests) == set(first.parts) >= {"agent.tgz", "repo.bundle", "wip.patch", "untracked.tgz"}
    # only the conversation changes
    conv = box(fleet, s.sandbox_id) / "home/agent/.fake/sessions" / f"{s.harness_session}.jsonl"
    conv.write_text(conv.read_text() + "a later thought\n")
    uploaded: list[str] = []
    put = svc.blobs.put

    async def spy(key, data):
        uploaded.append(key.rsplit("/", 1)[1])
        await put(key, data)

    monkeypatch.setattr(svc.blobs, "put", spy)
    calls = _count(monkeypatch, fleet.providers["docker"])
    ck = await svc.checkpoint_now("inc", requester="sean")
    assert uploaded == ["agent.tgz.enc"]
    assert calls == {"exec": 2, "read_file": 1, "write_file": 0}  # capture, read the pack, clean up
    assert ck.parts == {**first.parts, "agent.tgz": ck.parts["agent.tgz"]}
    assert ck.digests["untracked.tgz"] == first.digests["untracked.tgz"] != None  # noqa: E711
    await asyncio.sleep(0.2)  # pruning runs in the background
    assert [c.id for c in s.checkpoints] == [ck.id] and not (svc.root / s.id / first.id).exists()

    # suspend (everything unchanged: all parts copied) and resume from copies of copies
    svc.suspend("inc", requester="sean")
    s = await settle(svc, "inc")
    assert s.state == SessionState.SUSPENDED and set(s.checkpoints[-1].parts) == set(ck.parts)
    assert uploaded == ["agent.tgz.enc"]
    svc.send("inc", "continue with b", requester="sean")
    s = await settle(svc, "inc")
    assert s.state == SessionState.IDLE and s.turns[-1].exit_code == 0, s.reason  # the agent checks the restore
    conv = box(fleet, s.sandbox_id) / "home/agent/.fake/sessions" / f"{s.harness_session}.jsonl"
    assert "a later thought" in conv.read_text()


async def test_a_corrupt_part_falls_back_to_an_older_checkpoint(svc, world, fleet):  # noqa: F811
    svc.new(NewSession(name="cor", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "cor")
    good = s.checkpoints[-1]
    conv = box(fleet, s.sandbox_id) / "home/agent/.fake/sessions" / f"{s.harness_session}.jsonl"
    conv.write_text(conv.read_text() + "more\n")
    (box(fleet, s.sandbox_id) / "workspace/remote/later.txt").write_text("later\n")
    newer = await svc.checkpoint_now("cor", requester="sean")
    svc.suspend("cor", requester="sean")
    s = await settle(svc, "cor")
    newest = s.checkpoints[-1]
    assert newer.id != newest.id and newest.digests == newer.digests  # suspend copied every part of `newer`
    # damage the untracked files part both of them share (the local store hard-links copies)
    (svc.root / s.id / newest.id / "untracked.tgz.enc").write_bytes(b"not a fernet token")
    svc.open("cor", requester="sean")
    s = await settle(svc, "cor")
    assert s.state == SessionState.IDLE, s.reason
    assert f"older checkpoint ({good.id})" in s.reason and "corrupt" in s.reason, s.reason
    repo = box(fleet, s.sandbox_id) / "workspace/remote"
    assert (repo / "a.txt").exists() and (repo / "notes.txt").exists() and not (repo / "later.txt").exists()


async def test_periodic_check_of_an_unchanged_session_is_one_exec(svc, world, fleet, monkeypatch):  # noqa: F811
    svc.new(NewSession(name="calm", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "calm")
    calls = _count(monkeypatch, fleet.providers["docker"])
    async with svc._locks[s.id]:
        assert await svc._checkpoint(s, "interval") is None
    assert calls == {"exec": 1, "read_file": 0, "write_file": 0}
    assert not (box(fleet, s.sandbox_id) / "tmp/hangar-ckpt").exists()


async def test_big_parts_travel_alone(svc, world, fleet, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ckpt, "PACK_LIMIT", 64)  # every part is "too big to pack"
    svc.new(NewSession(name="big", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "big")
    assert {"agent.tgz", "untracked.tgz", "repo.bundle"} <= set(s.checkpoints[-1].parts)
    writes: list[str] = []
    prov = fleet.providers["docker"]
    write = prov.write_file

    async def spy(sb, path, data, mode=0o644):
        writes.append(path)
        await write(sb, path, data, mode)

    monkeypatch.setattr(prov, "write_file", spy)
    svc.suspend("big", requester="sean")
    await settle(svc, "big")
    svc.send("big", "continue with b", requester="sean")
    s = await settle(svc, "big")
    assert s.turns[-1].exit_code == 0, s.reason
    assert len([w for w in writes if w.startswith("/tmp/hangar-restore-")]) > 1


def test_groups_and_unpack_take_only_what_is_expected():
    assert ckpt.groups({"a": 10, "b": 10, "c": 30, "d": 1}, limit=25) == [["a", "b"], ["c"], ["d"]]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, kind in (("agent.tgz", tarfile.SYMTYPE), ("../wip.patch", tarfile.REGTYPE),
                           ("repo.bundle", tarfile.REGTYPE), ("evil", tarfile.REGTYPE)):
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = 0 if kind == tarfile.SYMTYPE else 3
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/passwd"
            tf.addfile(info, io.BytesIO(b"abc") if kind == tarfile.REGTYPE else None)
    assert ckpt.unpack(buf.getvalue(), dict.fromkeys(("agent.tgz", "wip.patch", "repo.bundle"), 3)) == \
        {"repo.bundle": b"abc"}
    assert ckpt.unpack(buf.getvalue(), {"repo.bundle": 2}) == {}  # integrate: over its cap, it is read alone
    out = ckpt.parse_capture("noise\nHANGAR-FP abc\nHANGAR-HEAD nothex\nHANGAR-PART agent.tgz 12 " + "f" * 64
                             + "\nHANGAR-PART ../x 1 " + "f" * 64 + "\nHANGAR-PACK agent.tgz evil\n")
    assert out.fingerprint == "abc" and out.head is None and list(out.parts) == ["agent.tgz"]
    assert out.packed == {"agent.tgz"}


def test_session_patch_script_covers_untracked_files_without_touching_the_index(tmp_path):
    repo = tmp_path / "r"
    env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": "a",
           "GIT_COMMITTER_EMAIL": "a@b"}

    def sh(cmd: str, cwd=repo) -> str:
        r = subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        return r.stdout

    repo.mkdir()
    sh("git init -q -b main . && echo a > t.txt && echo '*.log' > .gitignore && git add -A && git commit -qm i")
    base = sh("git rev-parse HEAD").strip()
    sh("echo b >> t.txt && echo new > 'with space.txt' && echo opt > ./-rf && echo log > x.log "
       "&& head -c 5000 /dev/zero > big.bin && ln -s t.txt link")
    index = sh("sha1sum .git/index")
    sh(patch_script(base, str(tmp_path / "p.patch"), 4000))
    assert sh("sha1sum .git/index") == index and not sh("git status --porcelain --untracked-files=no | grep -v '^ M' "
                                                        "|| true").strip()
    patch = (tmp_path / "p.patch").read_text()
    for name in ("t.txt", "with space.txt", "-rf", "link"):
        assert f"b/{name}" in patch, name
    assert "x.log" not in patch and "big.bin" not in patch
    clone = tmp_path / "c"
    sh(f"git clone -q {repo} {clone} && git -C {clone} apply --binary {tmp_path / 'p.patch'}", cwd=tmp_path)
    assert (clone / "-rf").read_text() == "opt\n" and os.readlink(clone / "link") == "t.txt"


async def test_local_store_copy(tmp_path):
    s = LocalStore(tmp_path)
    await s.put("ses/a/x.enc", b"data")
    assert await s.copy("ses/a/x.enc", "ses/b/x.enc") and await s.get("ses/b/x.enc") == b"data"
    await s.delete_prefix("ses/a/")
    assert await s.get("ses/b/x.enc") == b"data"
    assert not await s.copy("ses/a/x.enc", "ses/c/x.enc")
    with pytest.raises(ValueError):
        await s.copy("ses/b/x.enc", "../out")


async def test_gcs_copy_is_server_side_and_follows_rewrite_tokens():
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen.append((req.method, req.url.raw_path.decode()))
        if "missing" in req.url.raw_path.decode():
            return httpx.Response(404)
        if "rewriteToken" not in req.url.params:
            return httpx.Response(200, json={"done": False, "rewriteToken": "t1"})
        assert req.url.params["rewriteToken"] == "t1"
        return httpx.Response(200, json={"done": True})

    s = GCSStore("gs://bkt/pre", transport=httpx.MockTransport(handler))
    assert await s.copy("ses/a/x y.enc", "ses/b/x y.enc")
    assert seen[0][0] == "POST" and seen[0][1].startswith(
        "/storage/v1/b/bkt/o/pre%2Fses%2Fa%2Fx%20y.enc/rewriteTo/b/bkt/o/pre%2Fses%2Fb%2Fx%20y.enc")
    assert len(seen) == 2
    assert not await s.copy("ses/missing.enc", "ses/b/m.enc")
    await s.close()


async def test_docker_write_file_creates_the_directory_only_when_missing():
    from hangar.providers.docker import DockerProvider

    class Container:
        def __init__(self, missing: bool):
            self.missing, self.mkdirs, self.puts = missing, [], []

        def put_archive(self, path, data):
            if self.missing:
                raise NotFound("Could not find the file")
            self.puts.append(path)
            return True

        def exec_run(self, argv, user=None):
            self.mkdirs.append((argv, user))
            self.missing = False
            return SimpleNamespace(exit_code=0, output=b"")

    prov = DockerProvider.__new__(DockerProvider)
    prov.settings = SimpleNamespace(sandbox_user="agent")
    for missing in (False, True):
        c = Container(missing)

        async def container(_sb, _c=c):
            return _c

        prov._container = container
        await prov.write_file(None, "/home/agent/x/y.txt", b"hi")
        assert c.puts == ["/home/agent/x"]
        assert c.mkdirs == ([(["mkdir", "-p", "/home/agent/x"], "agent")] if missing else [])


async def _big_session(svc, fleet, name: str, mb: int = 6):  # noqa: F811
    """A session whose conversation, untracked files and commits each hold `mb` MB of incompressible data."""
    s = svc.get(name)
    b = box(fleet, s.sandbox_id)
    repo = b / "workspace/remote"
    (b / "home/agent/.fake/sessions/big.bin").write_bytes(os.urandom(mb << 20))
    (repo / "big-untracked.bin").write_bytes(os.urandom(mb << 20))
    (repo / "big-commit.bin").write_bytes(os.urandom(mb << 20))
    subprocess.run("git add big-commit.bin && git -c user.email=t@t -c user.name=t commit -qm big", shell=True,
                   cwd=repo, check=True)
    return s


async def _peak(coro) -> int:
    """Peak bytes allocated by Python (all threads) while `coro` runs."""
    import gc
    import tracemalloc

    gc.collect()
    tracemalloc.start()
    try:
        await coro
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


async def test_restore_holds_one_pack_at_a_time(svc, world, fleet, monkeypatch):  # noqa: F811
    """The control plane must not hold every part of a checkpoint at once: parts are fetched, decrypted and
    written one pack at a time (a burst of restores of big sessions would otherwise OOM it)."""
    svc.new(NewSession(name="heavy", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    await settle(svc, "heavy")
    s = await _big_session(svc, fleet, "heavy")
    ck = await svc.checkpoint_now("heavy", requester="sean")
    assert all(ck.parts[p] > 6 << 20 for p in ("agent.tgz", "untracked.tgz", "repo.bundle")), ck.parts
    monkeypatch.setattr(ckpt, "PACK_LIMIT", 8 << 20)  # one part per pack
    sb = fleet.get(s.sandbox_id)
    peak = await _peak(svc._restore_checkpoint(s, sb, sb.info["repo_path"], ck))
    # one 6 MB part: its ciphertext (~8 MB), the plaintext and the pack around it; all three parts would be ~3x
    assert peak < 34 << 20, f"restore peaked at {peak >> 20} MB"


async def test_capture_holds_one_unpacked_part_at_a_time(svc, world, fleet, monkeypatch):  # noqa: F811
    svc.new(NewSession(name="heavy2", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    await settle(svc, "heavy2")
    await _big_session(svc, fleet, "heavy2")
    monkeypatch.setattr(ckpt, "PACK_LIMIT", 8 << 20)  # too much to pack: every part is read on its own
    held = [0, 0]  # plaintext read from the sandbox and not yet encrypted; the most of it at any time
    prov, vault = fleet.providers["docker"], svc.vault
    read, encrypt = prov.read_file, vault.encrypt

    async def counting_read(sb, path, limit=None):
        data = await read(sb, path, limit)
        held[0] += len(data or b"")
        held[1] = max(held)
        return data

    def counting_encrypt(data):
        out = encrypt(data)
        held[0] -= len(data)
        return out

    monkeypatch.setattr(prov, "read_file", counting_read)
    monkeypatch.setattr(vault, "encrypt", counting_encrypt)
    ck = await svc.checkpoint_now("heavy2", requester="sean")
    assert all(ck.parts[p] > 6 << 20 for p in ("agent.tgz", "untracked.tgz", "repo.bundle")), ck.parts
    assert held[1] == max(ck.parts.values()), f"held {held[1] >> 20} MB at once; parts {ck.parts}"


async def test_recorded_digests_are_of_what_was_stored(svc, world, fleet, monkeypatch):  # noqa: F811
    """A part that changed between the sandbox's report and the read (an agent writing to /tmp, a torn read) must
    not be recorded under the reported digest: later checkpoints would copy the wrong blob for good."""
    import hashlib

    monkeypatch.setattr(ckpt, "PACK_LIMIT", 64)  # parts are read one by one, after the capture script
    svc.new(NewSession(name="torn", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "torn")
    (box(fleet, s.sandbox_id) / "workspace/remote/README.md").write_text("hello\nmore\n")
    prov = fleet.providers["docker"]
    exec_ = prov.exec

    async def capture_then_change(sb, cmd, **kw):
        res = await exec_(sb, cmd, **kw)
        if "HANGAR-PART" in cmd:
            (box(fleet, s.sandbox_id) / "tmp/hangar-ckpt/wip.patch").write_text("not what was reported\n")
        return res

    monkeypatch.setattr(prov, "exec", capture_then_change)
    ck = await svc.checkpoint_now("torn", requester="sean")
    stored = svc.vault.decrypt((svc.root / s.id / ck.id / "wip.patch.enc").read_bytes())
    assert ck.digests["wip.patch"] == hashlib.sha256(stored).hexdigest()


async def test_local_store_put_never_writes_through_a_shared_inode(tmp_path):
    """Copies are hard links. A copy interrupted between link and rename leaves `<key>.part` sharing the source's
    inode; a later put to that key must replace it, not write into the earlier checkpoint's blob."""
    s = LocalStore(tmp_path)
    await s.put("ses/a/x.enc", b"earlier checkpoint")
    (tmp_path / "ses/b").mkdir(parents=True)
    os.link(tmp_path / "ses/a/x.enc", tmp_path / "ses/b/x.enc.part")
    await s.put("ses/b/x.enc", b"new")
    assert await s.get("ses/a/x.enc") == b"earlier checkpoint" and await s.get("ses/b/x.enc") == b"new"
