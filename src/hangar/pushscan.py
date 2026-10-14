"""Secret scanning of the pushes an agent makes itself, in the git gateway's receive path (--- egress2).

A job's delivery is scanned in the sandbox before hangar pushes it (secretscan.py). A session's agent pushes on its
own, through the gateway, and the sandbox can't be trusted to scan that. So the gateway reads the whole push, the
command list and the pack, scans it with the same scanner, and only then forwards it, or answers with git's own
"remote rejected" naming each file and line and the kind of secret, never the value.

How the pack is read, without a copy of the repository:

* **Self-contained packs.** The gateway adds `no-thin` to receive-pack's advertisement ("A client MUST NOT send a thin
  pack if the server advertises the no-thin capability", gitprotocol-capabilities), so every delta's base is in the
  pack and every object can be rebuilt. A pack that still refers to an object outside itself is refused.
* **Objects.** Each object is inflated (bounded by the size its header gives) and deltas are applied (pack-format:
  OFS_DELTA and REF_DELTA), within a budget of inflated bytes. The pack's checksum must match, so what was scanned is
  what GitHub gets.
* **What leaves.** The new commits and tags as written (messages, and every header, an author name included), file
  names, and the text of every new blob, at the path the new trees give it. Binary blobs are skipped, as git's diff
  skips them for the delivery scan.
* **Only what is new.** A new blob holds the whole file, so a hit in a file that already held the same text in the
  commit the push builds on is not counted: the gateway asks GitHub for that file (contents API) only when there is
  a hit, so a clean push costs no extra request. If it can't tell, the hit stands.

Fast at 60 sessions: a push is parsed and scanned in a worker thread (the event loop serves every sandbox's git and
proxy traffic), at most SCANS at a time, in one pass per pattern over all of the push's text. A push of a few files
takes milliseconds; HANGAR_GATEWAY_SCAN_MAX_MB (64) bounds the pack the gateway holds, and a bigger one is refused
in block mode, since hangar doesn't forward what it couldn't check.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import hashlib
import logging
import tempfile
import zlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import quote

from hangar import secretscan

if TYPE_CHECKING:
    from hangar.gateway import Commands, Gateway
    from hangar.models import SandboxGrant

log = logging.getLogger("hangar.gateway")

SCANS = 2  # pushes parsed and scanned at once; the rest wait their turn
INFLATE_FACTOR = 4  # inflated bytes held per byte of HANGAR_GATEWAY_SCAN_MAX_MB, whatever the pack's own size
SPOOL_BYTES = 8 * 1024 * 1024  # a push bigger than this waits on disk, not in memory
CHUNK = 64 * 1024
BINARY_PROBE = 8000  # git's buffer_is_binary: a NUL in the first 8000 bytes
LOOKUPS = 20  # GitHub file reads to tell an old secret from a new one, per push
_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
OFS_DELTA, REF_DELTA = 6, 7


class PackError(ValueError):
    """A pack the gateway can't read, so it can't be checked."""


# ============================================================================ the pack
def _varint_size(data: memoryview, p: int) -> tuple[int, int]:
    """A delta's size varint (pack-format: little-endian 7-bit groups)."""
    n = shift = 0
    while True:
        if p >= len(data):
            raise PackError("truncated delta header")
        c = data[p]
        p += 1
        n |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return n, p
        if shift > 63:
            raise PackError("bad delta header")


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Rebuild an object from its base and a git delta (copy and insert instructions)."""
    d = memoryview(delta)
    src, p = _varint_size(d, 0)
    dst, p = _varint_size(d, p)
    if src != len(base):
        raise PackError("a delta doesn't fit its base")
    out = bytearray()
    while p < len(d):
        op = d[p]
        p += 1
        if op & 0x80:
            off = size = 0
            for i in range(4):
                if op & (1 << i):
                    off |= d[p] << (8 * i)
                    p += 1
            for i in range(3):
                if op & (0x10 << i):
                    size |= d[p] << (8 * i)
                    p += 1
            size = size or 0x10000
            if off + size > len(base):
                raise PackError("a delta copies past its base")
            out += base[off:off + size]
        elif op:
            if p + op > len(d):
                raise PackError("a delta inserts past its end")
            out += d[p:p + op]
            p += op
        else:
            raise PackError("a delta has a reserved instruction")
        if len(out) > dst:
            raise PackError("a delta builds more than it says")
    if len(out) != dst:
        raise PackError("a delta builds less than it says")
    return bytes(out)


def _inflate(data: memoryview, pos: int, size: int) -> tuple[bytes, int]:
    """The zlib stream at `pos`, which must inflate to exactly `size` bytes; (bytes, where the stream ends). Fed in
    chunks, so finding an object's end doesn't copy the rest of the pack, and never inflated past `size`."""
    d = zlib.decompressobj()
    out = bytearray()
    fed = 0
    buf = data[pos:pos + CHUNK]
    while True:
        if not buf:
            raise PackError("the pack ends inside an object")
        fed += len(buf)
        try:
            out += d.decompress(buf, size + 1 - len(out))
        except zlib.error as e:
            raise PackError("an object doesn't inflate") from e
        if len(out) > size:
            raise PackError("an object inflates past its size")
        if d.eof:
            break
        buf = data[pos + fed:pos + fed + CHUNK]
    if len(out) != size:
        raise PackError("an object inflates short of its size")
    return bytes(out), pos + fed - len(d.unused_data)


def oid(kind: str, content: bytes, hexlen: int) -> str:
    h = hashlib.sha256() if hexlen == 64 else hashlib.sha1()  # noqa: S324 - git's object ids
    h.update(f"{kind} {len(content)}\0".encode())
    h.update(content)
    return h.hexdigest()


def parse_pack(pack: bytes | memoryview, hexlen: int = 40, budget: int | None = None) -> dict[str, tuple[str, bytes]]:
    """{object id: (type, content)} for every object in a self-contained pack. Raises PackError for a pack that
    doesn't parse, refers to an object outside itself (thin), fails its checksum, or inflates past `budget`."""
    data = memoryview(pack)
    raw = hexlen // 2
    if len(data) < 12 + raw or bytes(data[:4]) != b"PACK" or int.from_bytes(data[4:8], "big") not in (2, 3):
        raise PackError("not a git pack")
    h = hashlib.sha256() if hexlen == 64 else hashlib.sha1()  # noqa: S324
    h.update(data[:-raw])
    if h.digest() != bytes(data[-raw:]):
        raise PackError("the pack's checksum doesn't match")
    count = int.from_bytes(data[8:12], "big")
    end = len(data) - raw
    budget = budget if budget is not None else 64 * 1024 * 1024 * INFLATE_FACTOR
    used = 0
    full: dict[int, tuple[str, bytes]] = {}  # offset -> (type, content)
    deltas: list[tuple[int, int | str, bytes]] = []  # (offset, base offset or base id, delta)
    pos = 12
    for _ in range(count):
        if pos >= end:
            raise PackError("the pack has fewer objects than it says")
        at = pos
        c = data[pos]
        pos += 1
        kind, size, shift = (c >> 4) & 7, c & 0x0F, 4
        while c & 0x80:
            if pos >= end or shift > 60:
                raise PackError("bad object header")
            c = data[pos]
            pos += 1
            size |= (c & 0x7F) << shift
            shift += 7
        base: int | str | None = None
        if kind == OFS_DELTA:
            c = data[pos]
            pos += 1
            back = c & 0x7F
            while c & 0x80:
                if pos >= end:
                    raise PackError("bad delta offset")
                c = data[pos]
                pos += 1
                back = ((back + 1) << 7) | (c & 0x7F)
            base = at - back
            if base < 12 or back <= 0:
                raise PackError("a delta's base is outside the pack")
        elif kind == REF_DELTA:
            base = bytes(data[pos:pos + raw]).hex()
            pos += raw
        elif kind not in _TYPES:
            raise PackError("an object of an unknown type")
        used += size
        if used > budget:
            raise PackError("the push inflates past what the gateway scans")
        content, pos = _inflate(data, pos, size)
        if pos > end:
            raise PackError("an object runs into the pack's checksum")
        if base is None:
            full[at] = (_TYPES[kind], content)
        else:
            deltas.append((at, base, content))
    if pos != end:
        raise PackError("the pack has bytes after its last object")
    ids = {oid(k, c, hexlen): at for at, (k, c) in full.items()}
    while deltas:  # bases first: OFS bases come earlier in the pack, REF bases may come anywhere in it
        left = []
        for at, base, delta in deltas:
            b_at = ids.get(base) if isinstance(base, str) else base
            if b_at is None or b_at not in full:
                left.append((at, base, delta))
                continue
            kind, b = full[b_at]
            content = apply_delta(b, delta)
            used += len(content)
            if used > budget:
                raise PackError("the push inflates past what the gateway scans")
            full[at] = (kind, content)
            ids[oid(kind, content, hexlen)] = at
        if len(left) == len(deltas):
            raise PackError("the pack refers to objects outside itself (a thin pack)")
        deltas = left
    return {i: full[at] for i, at in ids.items()}


# ============================================================================ what leaves, and where
class Found(NamedTuple):
    kind: str
    where: str  # "path:line", "commit 1a2b3c4d5e6f message", "a file name"
    path: str | None  # for a hit in a file, the path, to check the file's previous version
    value: str  # never logged, returned or shown: only compared with the previous version


def _tree_entries(content: bytes, rawlen: int) -> list[tuple[bool, str, str]]:
    """(is a directory, name, object id) for each entry of a tree object."""
    out, p = [], 0
    while p < len(content):
        sp = content.find(b" ", p)
        nul = content.find(b"\0", sp + 1)
        if sp < 0 or nul < 0 or nul + 1 + rawlen > len(content):
            raise PackError("a tree doesn't parse")
        mode = content[p:sp]
        out.append((mode in (b"40000", b"040000"), content[sp + 1:nul].decode("utf-8", "replace"),
                    content[nul + 1:nul + 1 + rawlen].hex()))
        p = nul + 1 + rawlen
    return out


def commit_links(content: bytes) -> tuple[str | None, list[str]]:
    """(tree, parents) of a commit object."""
    tree, parents = None, []
    for line in content.split(b"\n\n", 1)[0].split(b"\n"):
        if line.startswith(b"tree "):
            tree = line[5:].decode("ascii", "replace")
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode("ascii", "replace"))
    return tree, parents


def paths(objects: dict[str, tuple[str, bytes]], hexlen: int) -> dict[str, str]:
    """{blob id: path} for the blobs of the push, from its commits' trees (only trees the push carries: a tree the
    remote has already holds only files it has)."""
    out: dict[str, str] = {}
    seen: set[str] = set()
    stack = [(t, "") for k, c in objects.values() if k == "commit" and (t := commit_links(c)[0])]
    while stack:
        tid, prefix = stack.pop()
        if tid in seen or objects.get(tid, ("",))[0] != "tree":
            continue
        seen.add(tid)
        for is_dir, name, eid in _tree_entries(objects[tid][1], hexlen // 2):
            if is_dir:
                stack.append((eid, f"{prefix}{name}/"))
            elif eid not in out and objects.get(eid, ("",))[0] == "blob":
                out[eid] = prefix + name
    return out


def scan(objects: dict[str, tuple[str, bytes]], hexlen: int, broker: Any, limit: int = 50) -> tuple[list[Found], bool]:
    """The secrets in a push's new objects, and whether the text was cut at secretscan.MAX_SCAN_BYTES."""
    where = paths(objects, hexlen)
    parts: list[str] = []
    marks: list[tuple[int, str, str]] = []  # (start, what, label): "commit"/"blob"/"names"
    size, cut = 0, False
    names = sorted(set(where.values()))

    def add(text: str, what: str, label: str) -> None:
        nonlocal size, cut
        if size + len(text) > secretscan.MAX_SCAN_BYTES:
            cut = True
            return
        marks.append((size, what, label))
        parts.append(text)
        parts.append("\n\0\n")  # no pattern spans a NUL
        size += len(text) + 3

    for i, (k, c) in sorted(objects.items()):
        if k in ("commit", "tag"):
            add(c.decode("utf-8", "replace"), k, i)
    add("\n".join(names), "names", "")
    for i, path in sorted(where.items(), key=lambda x: x[1]):
        c = objects[i][1]
        if b"\0" not in c[:BINARY_PROBE]:
            add(c.decode("utf-8", "replace"), "blob", path)
    text = "".join(parts)
    starts = [m[0] for m in marks]
    out: list[Found] = []
    seen: set[tuple[str, str]] = set()
    for start, end, kind in sorted(secretscan.find(text, broker)):
        s, what, label = marks[bisect.bisect_right(starts, start) - 1]
        if what == "blob":
            loc, path = f"{label}:{text.count(chr(10), s, start) + 1}", label
        elif what == "names":
            loc, path = "a file name", None
        else:
            loc, path = f"{what} {label[:12]} message", None
        if (loc, kind) in seen:
            continue
        seen.add((loc, kind))
        out.append(Found(kind, loc, path, text[start:end]))
        if len(out) >= limit:
            break
    return out, cut


def boundary(objects: dict[str, tuple[str, bytes]], cmds: Commands) -> list[str]:
    """The commits the push builds on that the remote has: the parents of its commits that it doesn't carry, and the
    refs' old values."""
    out: list[str] = []
    for k, c in objects.values():
        if k == "commit":
            out += [p for p in commit_links(c)[1] if p not in objects]
    out += [c.old for c in cmds.commands if set(c.old) != {"0"}]
    return list(dict.fromkeys(out))[:4]


# ============================================================================ the gateway's side
def enabled(gw: Gateway) -> bool:
    return gw.settings.secret_scan != "off"


async def advertise_no_thin(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """receive-pack's ref advertisement with `no-thin` added to its capabilities (on the first ref line, after the
    smart-HTTP service line and its flush). Anything unexpected passes through as it is: a client that then sends a
    thin pack gets its push refused, never an unchecked one forwarded."""
    buf = bytearray()
    it = chunks.__aiter__()
    done = False
    while not done and len(buf) < 65536:
        try:
            buf += await it.__anext__()
        except StopAsyncIteration:
            break
        p = 0
        while p + 4 <= len(buf):
            try:
                n = int(buf[p:p + 4], 16)
            except ValueError:
                done = True
                break
            if n == 0:
                p += 4
                continue
            if n < 4 or p + n > len(buf):
                break
            line = bytes(buf[p + 4:p + n])
            if b"\0" in line:
                ref, _, caps = line.partition(b"\0")
                caps = caps.rstrip(b"\n")
                if b"no-thin" not in caps.split():
                    caps += b" no-thin"
                new = ref + b"\0" + caps + b"\n"
                buf[p:p + n] = b"%04x" % (len(new) + 4) + new
                done = True
                break
            p += n
    yield bytes(buf)
    async for c in it:
        yield c


class Spool:
    """The push body, held whole before any of it is forwarded: in memory up to SPOOL_BYTES, then on disk."""

    def __init__(self, data_dir: Any) -> None:
        self.f = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES, dir=data_dir)  # noqa: SIM115 - closed by close()
        self.size = 0
        self.whole = False  # the body ended here; else the rest is still to be read

    def write(self, data: bytes) -> None:
        self.f.write(data)
        self.size += len(data)

    def view(self) -> bytes:
        self.f.seek(0)
        return self.f.read()

    async def chunks(self) -> AsyncIterator[bytes]:
        self.f.seek(0)
        while data := self.f.read(CHUNK * 4):
            yield data
        self.close()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.f.close()


class Scanner:
    """One per gateway: the scan slots, and the GitHub reads that tell an old secret from a new one."""

    def __init__(self, gw: Gateway):
        self.gw = gw
        self._slots: asyncio.Semaphore | None = None

    @property
    def slots(self) -> asyncio.Semaphore:
        if self._slots is None:
            self._slots = asyncio.Semaphore(SCANS)
        return self._slots

    async def check(self, grant: SandboxGrant, repo: str, cmds: Commands, pack: bytes) -> tuple[list[Found], str]:
        """The new secrets in a push ([] when clean), and a note (the text was cut, or why it couldn't be read).
        Raises PackError when the pack can't be read."""
        hexlen = len(cmds.commands[0].new) if cmds.commands else 40
        broker = self.gw.broker
        broker._secrets()  # fill its cache here, not in the thread
        async with self.slots:
            budget = self.gw.settings.gateway_scan_max_mb * 1024 * 1024 * INFLATE_FACTOR
            objects = await asyncio.to_thread(parse_pack, pack, hexlen, budget)
            found, cut = await asyncio.to_thread(scan, objects, hexlen, broker)
        if found:
            found = await self._new_only(grant, repo, found, boundary(objects, cmds))
        return found, "only the first 64 MB of the push's text was scanned" if cut else ""

    async def _new_only(self, grant: SandboxGrant, repo: str, found: list[Found], bases: list[str]) -> list[Found]:
        """Drop hits in a file that held the same text in a commit the push builds on: pushing it again sends
        nothing new. One read per file and base, at most LOOKUPS; when a read fails the hit stays."""
        files: dict[tuple[str, str], bytes | None] = {}
        out = []
        for f in found:
            old = False
            for base in bases if f.path else []:
                key = (f.path, base)
                if key not in files:
                    files[key] = await self._file(grant, repo, f.path, base) if len(files) < LOOKUPS else None
                if (b := files[key]) is not None and f.value.encode() in b:
                    old = True
                    break
            if not old:
                out.append(f)
        return out

    async def _file(self, grant: SandboxGrant, repo: str, path: str, ref: str) -> bytes | None:
        """A file at a commit, from GitHub's contents API with the gateway's own credential; b"" when it isn't
        there, None when it can't be read."""
        gw = self.gw
        host = grant.host
        if host == "github.com":
            api = gw.settings.github_api_url.rstrip("/")
        else:  # GitHub Enterprise Server: the API lives under /api/v3 of the same host
            api = (gw.settings.gateway_upstreams.get(host) or f"https://{host}").rstrip("/") + "/api/v3"
        try:
            token = await gw.upstream_token(grant, repo)
            r = await gw._http().get(f"{api}/repos/{repo}/contents/{quote(path)}", params={"ref": ref},
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github.raw+json",
                                              "X-GitHub-Api-Version": "2022-11-28"}, timeout=15.0)
        except Exception as e:  # noqa: BLE001 - never fatal: the hit stands
            log.warning("gateway: couldn't read %s at %s to check a secret scan hit: %s", repo, ref[:12],
                        type(e).__name__)
            return None
        if r.status_code == 404:
            return b""
        return r.content if r.status_code == 200 else None


def message(found: list[Found], note: str = "") -> tuple[str, list[str]]:
    """(the reason git shows per ref, the lines shown as `remote:`)."""
    first = "; ".join(f"{f.where} ({f.kind})" for f in found[:3]) + (f"; and {len(found) - 3} more"
                                                                       if len(found) > 3 else "")
    lines = ["hangar: what you pushed looks like it contains a secret; nothing was pushed:"]
    lines += [f"hangar:   {f.where}: {f.kind}" for f in found[:30]]
    lines += ["hangar: remove each one and rewrite the commits that carry it (a later commit that deletes it still "
              "pushes the old one), then push again. A test fixture or placeholder: make it obviously fake."]
    if note:
        lines.append(f"hangar: {note}")
    return f"hangar: possible secret: {first}"[:900], lines


async def read_body(it: AsyncIterator[bytes], head: bytes, cap: int, timeout: float, data_dir: Any) -> Spool:
    """The push body (`head` already read): whole, or (`whole` False) its first `cap` bytes or so, the rest unread."""
    spool = Spool(data_dir)
    try:
        spool.write(head)
        while spool.size <= cap:
            try:
                chunk = await asyncio.wait_for(it.__anext__(), timeout=timeout)
            except StopAsyncIteration:
                spool.whole = True
                break
            spool.write(chunk)
    except BaseException:
        spool.close()
        raise
    return spool
