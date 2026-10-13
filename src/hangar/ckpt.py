"""Checkpoint capture and restore in the sandbox, with as few round trips as possible (spec 0004 M1, 0010 M4).

Every provider call is a round trip (a Docker exec costs ~70 ms; a Kubernetes exec several times that), so a
capture is one exec and one read, and a restore is one write and one exec:

- capture: a script fingerprints the work first and stops there when a periodic check finds nothing changed.
  Otherwise it builds the parts, prints each part's size and SHA-256, and packs the parts that changed into one
  uncompressed tar that comes back in a single read. Parts whose digest matches the previous checkpoint are
  copied inside the checkpoint store instead of being read, encrypted and uploaded again.
- restore: the decrypted parts go in as one tar, and one script unpacks and applies them and prints the new HEAD.

Packs are capped at PACK_LIMIT so the control plane never holds more than that per round trip; bigger parts
travel alone, as before. Parts are gzipped at level 1: on conversation logs that is ~3x faster than the default
level 6 for ~20% more bytes.
"""

from __future__ import annotations

import hashlib
import io
import re
import shlex
import tarfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hangar import exchange

if TYPE_CHECKING:
    from hangar.vault import Vault

PARTS = ("agent.tgz", "repo.bundle", "wip.patch", "untracked.tgz")
ALL_PARTS = (*PARTS, *exchange.CHECKPOINT_EXTRA)
CKPT_REF = "refs/hangar/checkpoint"
CKPT_DIR = "/tmp/hangar-ckpt"
RESTORE_DIR = "/tmp/hangar-restore"
PACK = "pack.tar"
PACK_LIMIT = 64 * 1024 * 1024
_LINE = re.compile(r"^HANGAR-(FP|HEAD|PART|PACK|UNCHANGED)(?: (.*))?$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class Capture:
    fingerprint: str | None = None
    head: str | None = None
    unchanged: bool = False
    parts: dict[str, tuple[int, str]] = field(default_factory=dict)  # name -> (bytes, sha256)
    packed: set[str] = field(default_factory=set)  # parts inside CKPT_DIR/PACK


def capture_script(*, repo: str | None, home: str, dirs: list[str], base: str, max_untracked: int, max_part: int,
                   skip_if: str | None = None, previous: dict[str, str] | None = None) -> str:
    """The capture script. `skip_if`: stop after fingerprinting when the fingerprint equals it. `previous`: part ->
    sha256 in the last checkpoint; parts that still match are left out of the pack (the store copies them)."""
    q = shlex.quote
    c = CKPT_DIR
    d = " ".join(q(x) for x in dirs)
    # GIT_OPTIONAL_LOCKS=0: never take index.lock, so a checkpoint during a turn can't break the agent's git.
    s = [f"export GIT_OPTIONAL_LOCKS=0; rm -rf {c} && mkdir -p {c} && chmod 700 {c} || exit 1"]
    if repo:
        # Read HEAD once and pin it, so a commit landing mid-checkpoint can't make head and bundle disagree.
        s += [
            f"cd {q(repo)} || exit 1",
            f"H=$(git rev-parse --verify -q HEAD) && git update-ref {CKPT_REF} $H && echo $H > {c}/head",
            f"if [ -n \"$H\" ]; then git diff --binary $H > {c}/wip.patch; fi",
            f"git ls-files -z --others --exclude-standard > {c}/untracked.list",
            f"if [ -s {c}/untracked.list ]; then "
            f"xargs -0 stat -c '%n %s %Y' -- < {c}/untracked.list > {c}/untracked.stat 2>/dev/null; fi",
        ]
    # fingerprint: HEAD + tree diff + untracked names, sizes and times + conversation file listing
    s.append(f"FP=$({{ cat {c}/head 2>/dev/null; sha1sum {c}/wip.patch 2>/dev/null | cut -c1-40; "
             f"cat {c}/untracked.stat 2>/dev/null; cd {q(home)} && find {d or '/dev/null'} -type f "
             f"-printf '%P %s %T@\\n' 2>/dev/null | sort; }} | sha1sum | cut -c1-40)")
    s.append(f"echo \"HANGAR-FP $FP\"; echo \"HANGAR-HEAD $(cat {c}/head 2>/dev/null)\"")
    if skip_if:
        s.append(f"if [ \"$FP\" = {q(skip_if)} ]; then echo HANGAR-UNCHANGED; rm -rf {c}; exit 0; fi")
    if repo:
        s += [
            f"cd {q(repo)} || exit 1",
            f"if [ -n \"$H\" ]; then git bundle create -q {c}/repo.bundle {CKPT_REF} --not --remotes "
            f"2>/dev/null || git bundle create -q {c}/repo.bundle {CKPT_REF} --not {q(base)} 2>/dev/null "
            f"|| rm -f {c}/repo.bundle; fi",
            f"if [ -s {c}/untracked.list ]; then tar --null -T {c}/untracked.list -cf - 2>/dev/null "
            f"| gzip -1n > {c}/untracked.tgz; fi",
            # workstream E: the whole diff against base, so get_session_patch works while the session is suspended
            f"if [ -n \"$H\" ]; then {exchange.patch_script(q(base), f'{c}/session.patch', max_untracked)}; fi",
        ]
    if d:
        s.append(f"cd {q(home)} && D=$(ls -d {d} 2>/dev/null); if [ -n \"$D\" ]; then "
                 f"tar cf - --ignore-failed-read $D 2>/dev/null | gzip -1n > {c}/agent.tgz; fi")
    prev = previous or {}
    s.append("P=; T=0")
    for p in ALL_PARTS:
        cap = max_untracked if p == "untracked.tgz" else max_part
        changed = f" && [ \"$h\" != {q(prev[p])} ]" if _SHA.match(prev.get(p, "")) else ""
        s.append(f"if [ -f {c}/{p} ]; then n=$(stat -c %s {c}/{p}); h=$(sha256sum < {c}/{p} | cut -c1-64); "
                 f"echo \"HANGAR-PART {p} $n $h\"; "
                 f"if [ \"$n\" -le {int(cap)} ]{changed}; then P=\"$P {p}\"; T=$((T + n)); fi; fi")
    s.append(f"if [ -n \"$P\" ] && [ \"$T\" -le {PACK_LIMIT} ] && tar cf {c}/{PACK} -C {c} $P; "
             "then echo \"HANGAR-PACK$P\"; fi")
    return "\n".join(s) + "\n"


def parse_capture(stdout: str) -> Capture:
    out = Capture()
    for ln in stdout.splitlines():
        m = _LINE.match(ln.strip())
        if not m:
            continue
        kind, rest = m.group(1), (m.group(2) or "").strip()
        if kind == "FP":
            out.fingerprint = rest or None
        elif kind == "HEAD":
            out.head = rest if re.fullmatch(r"[0-9a-f]{40,64}", rest) else None
        elif kind == "UNCHANGED":
            out.unchanged = True
        elif kind == "PACK":
            out.packed = {p for p in rest.split() if p in ALL_PARTS}
        else:
            name, _, tail = rest.partition(" ")
            size, _, digest = tail.partition(" ")
            if name in ALL_PARTS and size.isdigit() and _SHA.match(digest):
                out.parts[name] = (int(size), digest)
    return out


def pack(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in parts.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def seal(vault: Vault, plain: bytes) -> tuple[str, bytes]:
    """A part's sha256 and its encrypted blob. The digest is taken here, from the bytes actually stored, so a part
    that changed (or was damaged) between the sandbox's report and the read is never mistaken for an old one."""
    return hashlib.sha256(plain).hexdigest(), vault.encrypt(plain)


def unpack(data: bytes, expected: set[str]) -> dict[str, bytes]:
    """Parts from a capture pack. The sandbox wrote it, so only regular files with an expected name are taken."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for m in tf:
            if m.isfile() and m.name in expected and m.name not in out:
                f = tf.extractfile(m)
                out[m.name] = f.read() if f else b""
    return out


def groups(sizes: dict[str, int], limit: int | None = None) -> list[list[str]]:
    """Split parts (name -> bytes) into packs of at most `limit` bytes (default PACK_LIMIT; a bigger part goes
    alone). Planned from the recorded sizes, so a restore fetches and holds one pack at a time."""
    limit = limit or PACK_LIMIT
    out: list[list[str]] = []
    total = 0
    for name, size in sizes.items():
        if not out or (total + size > limit and out[-1]):
            out.append([])
            total = 0
        out[-1].append(name)
        total += size
    return out


def restore_script(*, repo: str, home: str, branch: str, target: str, present: set[str], packs: str) -> str:
    """Unpack the parts written as `<packs>.<n>.tar` and apply them to the checkout; prints HANGAR-HEAD last."""
    q = shlex.quote
    r = RESTORE_DIR
    steps = [
        f"rm -rf {r} && mkdir -p {r} && chmod 700 {r}",
        f"{{ for f in {packs}.*.tar; do [ -e \"$f\" ] || continue; tar xf \"$f\" -C {r} && rm -f \"$f\" || exit 1; "
        "done; }",
        "{ git config user.email >/dev/null || git config user.email hangar@localhost; }",
        "{ git config user.name >/dev/null || git config user.name hangar; }",
        # Environment setup may have touched tracked files; start from a clean tree (ignored files stay).
        "git reset -q --hard && git clean -fdq",
        (f"git fetch -q {r}/repo.bundle {CKPT_REF} && git checkout -q -B {q(branch)} FETCH_HEAD"
         if "repo.bundle" in present else
         f"(git cat-file -e {q(target)}^{{commit}} 2>/dev/null || git fetch -q origin {q(target)})"
         f" && git checkout -q -B {q(branch)} {q(target)}"),
    ]
    if "wip.patch" in present:
        steps.append(f"if [ -s {r}/wip.patch ]; then git apply --binary --whitespace=nowarn {r}/wip.patch; fi")
    if "untracked.tgz" in present:
        steps.append(f"tar xzf {r}/untracked.tgz -C {q(repo)}")
    if "agent.tgz" in present:
        steps.append(f"tar xzf {r}/agent.tgz -C {q(home)}")
    steps.append(f"rm -rf {r} {r}-*.tar")  # and the packs of an earlier attempt that failed
    steps.append("echo \"HANGAR-HEAD $(git rev-parse HEAD)\"")
    return " && ".join(steps)


def restored_head(stdout: str) -> str | None:
    for ln in reversed(stdout.splitlines()):
        m = _LINE.match(ln.strip())
        if m and m.group(1) == "HEAD":
            return (m.group(2) or "").strip() or None
    return None
