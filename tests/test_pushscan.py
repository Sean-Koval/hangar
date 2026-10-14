"""Secret scanning of agents' own pushes in the git gateway (--- egress2): the pack reader against git's own packs and
crafted ones, what is scanned and where hits are, the no-thin advertisement, and the speed of a large push. The real
git client end to end is in test_gateway_git.py."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import zlib

import pytest

from hangar import pushscan
from hangar.gateway import Command, Commands

COMMIT, TREE, BLOB = 1, 2, 3
TOKEN = "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF2hJ5"  # GitHub's classic PAT shape, made up
ZERO = "0" * 40


# ============================================================================ building packs
def _header(kind: int, size: int) -> bytes:
    out, c, size = bytearray(), (kind << 4) | (size & 0x0F), size >> 4
    while size:
        out.append(c | 0x80)
        c, size = size & 0x7F, size >> 7
    out.append(c)
    return bytes(out)


def _ofs(back: int) -> bytes:
    out = bytearray([back & 0x7F])
    back >>= 7
    while back:
        back -= 1
        out.insert(0, 0x80 | (back & 0x7F))
        back >>= 7
    return bytes(out)


def _size(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def make_delta(base: bytes, target: bytes) -> bytes:
    """A git delta: copy the common prefix from the base, insert the rest."""
    k = 0
    while k < min(len(base), len(target), 0xFFFFFF) and base[k] == target[k]:
        k += 1
    out = _size(len(base)) + _size(len(target))
    if k:
        out += bytes([0x80 | 0x10 | 0x20 | 0x40, k & 0xFF, (k >> 8) & 0xFF, (k >> 16) & 0xFF])
    rest = target[k:]
    for i in range(0, len(rest), 127):
        out += bytes([len(rest[i:i + 127])]) + rest[i:i + 127]
    return out


def make_pack(entries: list[tuple]) -> bytes:
    """entries: (kind, content), ("ofs", index of an earlier entry, delta) or ("ref", base id, delta)."""
    out, offsets = bytearray(b"PACK" + (2).to_bytes(4, "big") + len(entries).to_bytes(4, "big")), []
    for e in entries:
        offsets.append(len(out))
        if e[0] == "ofs":
            out += _header(6, len(e[2])) + _ofs(offsets[-1] - offsets[e[1]]) + zlib.compress(e[2])
        elif e[0] == "ref":
            out += _header(7, len(e[2])) + bytes.fromhex(e[1]) + zlib.compress(e[2])
        else:
            out += _header(e[0], len(e[1])) + zlib.compress(e[1])
    return bytes(out) + hashlib.sha1(out).digest()  # noqa: S324


def oid(kind: str, content: bytes) -> str:
    return pushscan.oid(kind, content, 40)


def tree(*entries: tuple[str, str, str]) -> bytes:
    return b"".join(f"{mode} {name}".encode() + b"\0" + bytes.fromhex(i) for mode, name, i in entries)


def commit(tree_id: str, message: str, *parents: str) -> bytes:
    head = f"tree {tree_id}\n" + "".join(f"parent {p}\n" for p in parents)
    return (head + "author a <a@a> 0 +0000\ncommitter a <a@a> 0 +0000\n\n" + message).encode()


def cmds(new: str, old: str = ZERO) -> Commands:
    return Commands([Command(old, new, "refs/heads/hangar/x")], ["report-status"])


# ============================================================================ the pack reader
def test_objects_deltas_and_ids_come_back_exactly():
    base = b"line one\nline two\n" * 50
    target = base + b"line three\n"
    b1, b2 = oid("blob", base), oid("blob", target)
    pack = make_pack([(BLOB, base), ("ofs", 0, make_delta(base, target)),
                      ("ref", b2, make_delta(target, b"x" + target[1:])), (BLOB, b"tail\n")])
    objs = pushscan.parse_pack(pack)
    assert objs[b1] == ("blob", base) and objs[b2] == ("blob", target)
    assert objs[oid("blob", b"x" + target[1:])][1] == b"x" + target[1:]
    assert len(objs) == 4


def test_a_ref_delta_whose_base_comes_later_is_resolved():
    base, target = b"a" * 300, b"a" * 290 + b"b" * 20
    pack = make_pack([("ref", oid("blob", base), make_delta(base, target)), (BLOB, base)])
    assert pushscan.parse_pack(pack)[oid("blob", target)] == ("blob", target)


@pytest.mark.parametrize("pack,why", [
    (make_pack([("ref", "ab" * 20, make_delta(b"x" * 10, b"y"))]), "thin"),
    (make_pack([(BLOB, b"hello")])[:-1] + b"\0", "checksum"),
    (b"NOPE" + b"\0" * 40, "not a git pack"),
], ids=["thin", "checksum", "garbage"])
def test_packs_the_gateway_cant_check_are_refused(pack, why):
    with pytest.raises(pushscan.PackError, match=why):
        pushscan.parse_pack(pack)


def test_an_object_that_inflates_past_its_size_or_the_budget_is_refused():
    body = bytearray(b"PACK" + (2).to_bytes(4, "big") + (1).to_bytes(4, "big"))
    body += _header(BLOB, 10) + zlib.compress(b"z" * 100_000)  # says 10 bytes, holds 100 KB
    with pytest.raises(pushscan.PackError, match="past its size"):
        pushscan.parse_pack(bytes(body) + hashlib.sha1(body).digest())  # noqa: S324
    big = make_pack([(BLOB, b"z" * 5_000_000)])  # 5 MB that compresses to almost nothing
    with pytest.raises(pushscan.PackError, match="inflates past"):
        pushscan.parse_pack(big, budget=4_000_000)
    assert pushscan.parse_pack(big)  # the default budget: 4x HANGAR_GATEWAY_SCAN_MAX_MB's default


def test_deltas_that_lie_are_refused():
    for d in (make_delta(b"abc", b"abcd")[:-1], _size(3) + _size(9) + b"\x01x", _size(3) + _size(1) + b"\x00",
              _size(4) + _size(4) + bytes([0x91, 0x00, 0x09])):
        with pytest.raises(pushscan.PackError):
            pushscan.apply_delta(b"abc" if d[0] == 3 else b"abcd", d)


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_packs_git_writes_parse_to_the_objects_git_has(tmp_path):
    """git pack-objects deltifies similar versions: every object comes back with git's own id and content."""
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*a: str, data: bytes | None = None) -> bytes:
        return subprocess.run(["git", *a], cwd=tmp_path, env=env, check=True, capture_output=True, input=data).stdout

    git("init", "-q")
    text = "".join(f"def f{i}(x):\n    return x * {i}\n\n" for i in range(400))
    for n in range(4):
        (tmp_path / "mod.py").write_text(text + f"# version {n}\n")
        (tmp_path / "sub").mkdir(exist_ok=True)
        (tmp_path / "sub" / "b.txt").write_text(f"{n}\n" * 50)
        git("add", "-A")
        git("commit", "-qm", f"c{n}")
    pack = git("pack-objects", "--stdout", "--revs", "-q", data=b"HEAD\n")
    objs = pushscan.parse_pack(pack)
    listed = git("rev-list", "--objects", "HEAD").decode().split()
    ids = {x for x in listed if len(x) == 40}
    assert set(objs) == ids
    for i, (kind, content) in objs.items():
        assert git("cat-file", kind, i) == content
    assert pushscan.paths(objs, 40)[git("rev-parse", "HEAD:sub/b.txt").decode().strip()] == "sub/b.txt"


# ============================================================================ what is scanned
class NoBroker:
    def _secrets(self) -> list[str]:
        return []


def _push(files: dict[str, bytes], message: str = "work", parents: tuple[str, ...] = ()) -> tuple[bytes, str]:
    """A pack with one commit of `files` (top level, or under src/)."""
    blobs = {name: (oid("blob", c), c) for name, c in files.items()}
    sub = {n.removeprefix("src/"): b for n, b in blobs.items() if n.startswith("src/")}
    top = [("100644", n, b[0]) for n, b in sorted(blobs.items()) if not n.startswith("src/")]
    entries = [(BLOB, c) for _, c in blobs.values()]
    if sub:
        st = tree(*[("100644", n, b[0]) for n, b in sorted(sub.items())])
        entries.append((TREE, st))
        top.append(("40000", "src", oid("tree", st)))
    t = tree(*sorted(top, key=lambda e: e[1]))
    c = commit(oid("tree", t), message, *parents)
    entries += [(TREE, t), (COMMIT, c)]
    return make_pack(entries), oid("commit", c)


def _scan(pack: bytes) -> list[pushscan.Found]:
    return pushscan.scan(pushscan.parse_pack(pack), 40, NoBroker())[0]


def test_hits_name_the_file_and_line_or_the_commit_never_the_value():
    pack, _ = _push({".env": b"A=1\nB=2\nGITHUB_TOKEN=" + TOKEN.encode() + b"\n", "src/ok.py": b"print(1)\n",
                     "src/cfg.py": b"x = 1\n\nKEY = '" + TOKEN.encode() + b"'\n"})
    found = _scan(pack)
    assert sorted(f.where for f in found) == [".env:3", "src/cfg.py:3"]
    assert {f.kind for f in found} == {"GitHub token"}
    reason, lines = pushscan.message(found)
    assert TOKEN not in reason + "".join(lines) and ".env:3" in reason


def test_commit_messages_file_names_are_scanned_and_binary_files_are_skipped():
    pack, c = _push({"a.bin": b"\0\x01" + TOKEN.encode(), TOKEN + ".txt": b"hi\n"}, message=f"oops {TOKEN}\n")
    found = {f.where for f in _scan(pack)}
    assert found == {f"commit {c[:12]} message", "a file name"}


def test_placeholders_and_stored_values():
    class Broker:
        def _secrets(self) -> list[str]:
            return ["s3cr3t-value-stored-in-the-vault"]

    pack, _ = _push({"x.py": b"AKIAIOSFODNN7EXAMPLE\nv = 's3cr3t-value-stored-in-the-vault'\n"})
    found = pushscan.scan(pushscan.parse_pack(pack), 40, Broker())[0]
    assert [(f.where, f.kind) for f in found] == [("x.py:2", "stored credential")]


def test_the_boundary_is_what_the_push_builds_on():
    pack, c = _push({"a": b"1\n"}, parents=("ab" * 20,))
    objs = pushscan.parse_pack(pack)
    assert pushscan.boundary(objs, cmds(c, "cd" * 20)) == ["ab" * 20, "cd" * 20]


def test_a_large_push_scans_in_well_under_a_second_or_two():
    """60 sessions share the control plane: a 16 MB push of source code is read and scanned in a worker thread fast
    enough not to hold a push up."""
    files = {f"src/m{i}.py": ("".join(f"def f{j}(x):\n    return x + {i * j}\n" for j in range(250))).encode()
             for i in range(2000)}
    files["src/m7.py"] += b"T = '" + TOKEN.encode() + b"'\n"
    pack, _ = _push(files)
    t = time.perf_counter()
    found = _scan(pack)
    took = time.perf_counter() - t
    assert [f.where for f in found] == ["src/m7.py:501"]
    assert sum(len(c) for c in files.values()) > 15_000_000 and took < 4.0, took


# ============================================================================ the advertisement
async def _collect(chunks) -> bytes:
    return b"".join([c async for c in chunks])


async def _gen(*parts: bytes):
    for p in parts:
        yield p


def _pkt(b: bytes) -> bytes:
    return b"%04x" % (len(b) + 4) + b


@pytest.mark.parametrize("first", [f"{'a' * 40} refs/heads/main", f"{ZERO} capabilities^{{}}"])
async def test_no_thin_is_added_to_receive_packs_capabilities(first):
    adv = (_pkt(b"# service=git-receive-pack\n") + b"0000" + _pkt(b"version 1\n")
           + _pkt(first.encode() + b"\0report-status side-band-64k ofs-delta\n")
           + _pkt(f"{'b' * 40} refs/heads/x\n".encode()) + b"0000")
    out = await _collect(pushscan.advertise_no_thin(_gen(adv[:7], adv[7:40], adv[40:])))
    from hangar.gateway import iter_pkts

    lines = iter_pkts(out)
    assert lines[2] == first.encode() + b"\0report-status side-band-64k ofs-delta no-thin\n"
    assert lines[3] == f"{'b' * 40} refs/heads/x\n".encode() and out.endswith(b"0000")


async def test_an_advertisement_it_doesnt_understand_passes_unchanged():
    for adv in (b"garbage that isn't pkt-lines", b""):
        assert await _collect(pushscan.advertise_no_thin(_gen(adv))) == adv


async def test_a_big_push_waits_on_disk_and_comes_back_whole(tmp_path):
    body = os.urandom(pushscan.SPOOL_BYTES + 5)
    held = await pushscan.read_body(_gen(body[100:]), body[:100], 1 << 30, 5, tmp_path)
    assert held.whole and held.size == len(body) and held.f._rolled and held.view() == body
    assert await _collect(held.chunks()) == body
    part = await pushscan.read_body(_gen(body[100:200], body[200:]), body[:100], 150, 5, tmp_path)
    assert not part.whole and part.size == 200
