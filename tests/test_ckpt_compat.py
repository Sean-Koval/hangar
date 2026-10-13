"""Checkpoints written before the one-exec capture (main at 2e3e0a3) must still restore: users have them in their
checkpoint stores. `_old_checkpoint` below is that version's `SessionService._checkpoint`, frozen verbatim (with
the per-file `session.patch` script it used), so this test writes real old-format checkpoints and then resumes
from them with the current code. Fake provider, real git and tmux."""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from hangar import exchange
from hangar.ckpt import CKPT_DIR, CKPT_REF, PARTS
from hangar.models import Checkpoint, SessionState, now
from hangar.sessions import NewSession, SessionService
from tests.test_session_streams import box, settle, svc, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


def _old_patch_script(base: str, dest: str, max_untracked: int) -> str:
    return (f"{{ git diff --binary {base}; git ls-files -z --others --exclude-standard | xargs -0 -r -n1 sh -c "
            f"'[ \"$(stat -c %s \"$1\")\" -le {int(max_untracked)} ] && git diff --no-index --binary -- /dev/null "
            f"\"$1\"; true' _; }} > {dest}")


async def _old_checkpoint(self, s, reason: str, skip_unchanged: bool = False):
    """SessionService._checkpoint as of 2e3e0a3 (one read per part, everything re-uploaded, no digests)."""
    if not s.sandbox_id or not self._alive(s.sandbox_id):
        return None
    sb = self.fleet.get(s.sandbox_id)
    repo = sb.info.get("repo_path")
    q = shlex.quote
    c, home = CKPT_DIR, f"/home/{self.settings.sandbox_user}"
    dirs = " ".join(q(x) for x in self._agent_dirs(s))
    last = s.checkpoints[-1] if s.checkpoints else None
    base = s.base_sha or "HEAD"
    max_untracked = self.settings.session_max_untracked_mb * 1024 * 1024
    script = [f"export GIT_OPTIONAL_LOCKS=0; rm -rf {c} && mkdir -p {c} && chmod 700 {c}"]
    if repo:
        script += [
            f"cd {q(repo)}",
            f"H=$(git rev-parse --verify -q HEAD) && git update-ref {CKPT_REF} $H && echo $H > {c}/head",
            f"if [ -n \"$H\" ]; then git bundle create -q {c}/repo.bundle {CKPT_REF} --not --remotes "
            f"2>/dev/null || git bundle create -q {c}/repo.bundle {CKPT_REF} --not {q(base)} 2>/dev/null "
            f"|| rm -f {c}/repo.bundle; fi",
            f"if [ -n \"$H\" ]; then git diff --binary $H > {c}/wip.patch; fi",
            f"git ls-files -z --others --exclude-standard > {c}/untracked.list",
            f"if [ -s {c}/untracked.list ]; then tar --null -T {c}/untracked.list -czf {c}/untracked.tgz; "
            f"xargs -0 stat -c '%n %s %Y' < {c}/untracked.list > {c}/untracked.stat 2>/dev/null; fi",
        ]
        diff = _old_patch_script(q(base), f"{c}/session.patch", max_untracked)
        script.append(f"if [ -n \"$H\" ]; then {diff}; fi")
    if dirs:
        script.append(f"cd {q(home)} && D=$(ls -d {dirs} 2>/dev/null); "
                      f"if [ -n \"$D\" ]; then tar czf {c}/agent.tgz --ignore-failed-read $D || true; fi")
    script.append(f"{{ cat {c}/head 2>/dev/null; sha1sum {c}/wip.patch 2>/dev/null | cut -c1-40; "
                  f"cat {c}/untracked.stat 2>/dev/null; "
                  f"cd {q(home)} && find {dirs or '/dev/null'} -type f -printf '%P %s %T@\\n' 2>/dev/null"
                  " | sort; }"
                  f" | sha1sum | cut -c1-40 > {c}/fingerprint")
    script.append(f"stat -c '%s %n' {c}/* 2>/dev/null || true")
    timeout = int(max(30, min(600, self.settings.pre_destroy_timeout_seconds - 30)))
    res = await self._sh(sb, " && ".join(script[:1]) + "; " + "; ".join(script[1:]), timeout=timeout)
    _, prov = self.fleet._running(sb.id)
    fp = (await prov.read_file(sb, f"{c}/fingerprint") or b"").decode().strip() or None
    head = (await prov.read_file(sb, f"{c}/head") or b"").decode().strip() or None
    if (reason == "interval" or skip_unchanged) and last and fp and fp == last.fingerprint:
        self._last_checked[s.id] = now()
        return None
    sizes = {}
    for ln in res.stdout.splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0].isdigit():
            sizes[Path(parts[1]).name] = int(parts[0])
    ck = Checkpoint(reason=reason, head=head if head and head != "HEAD" else None, base=s.base_sha,
                    branch=s.branch, fingerprint=fp)
    key = f"{s.id}/{ck.id}"
    max_part = self.settings.session_max_part_mb * 1024 * 1024
    try:
        for part in (*PARTS, *exchange.CHECKPOINT_EXTRA):
            if part not in sizes:
                continue
            cap = max_untracked if part == "untracked.tgz" else max_part
            if sizes[part] > cap:
                ck.warnings.append(f"{part} is {sizes[part] // 1024 // 1024} MB, over the "
                                   f"{cap // 1024 // 1024} MB limit; not checkpointed")
                continue
            data = await prov.read_file(sb, f"{c}/{part}")
            if data is None:
                ck.warnings.append(f"could not read {part}")
                continue
            blob = await asyncio.to_thread(self.vault.encrypt, data)
            await self.blobs.put(f"{key}/{part}.enc", blob)
            ck.parts[part] = len(data)
    except BaseException:
        self._spawn(self._drop(f"{key}/"))
        raise
    with contextlib.suppress(Exception):
        await self._sh(sb, f"rm -rf {c}")
    s.checkpoints.append(ck)
    keep = max(1, self.settings.session_keep_checkpoints)
    for old in s.checkpoints[:-keep]:
        self._spawn(self._drop(f"{s.id}/{old.id}/"))
    s.checkpoints = s.checkpoints[-keep:]
    self._last_checked[s.id] = now()
    self._save(s)
    return ck


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


async def test_checkpoints_written_by_the_old_capture_still_restore(svc, world, fleet):  # noqa: F811
    old_capture = pytest.MonkeyPatch()
    old_capture.setattr(SessionService, "_checkpoint", _old_checkpoint)
    svc.new(NewSession(name="legacy", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "legacy")
    assert s.state == SessionState.IDLE, s.reason
    # something the agent handed back through HANGAR_IO, and an untracked file with an awkward name
    io = box(fleet, s.sandbox_id) / "home/agent" / exchange.io_rel(s)
    (io / "in/1").mkdir(parents=True, exist_ok=True)  # out/ is emptied each turn; in/ and artifacts stay
    (io / "in/1/brief.md").write_text("kept across the upgrade\n")
    (box(fleet, s.sandbox_id) / "workspace/remote/-rf odd name.txt").write_text("odd\n")
    svc.suspend("legacy", requester="sean")
    s = await settle(svc, "legacy")
    assert s.state == SessionState.SUSPENDED, s.reason
    old = s.checkpoints[-1]
    assert old.reason == "suspend" and not old.digests  # really the old format
    assert {"agent.tgz", "repo.bundle", "wip.patch", "untracked.tgz", "session.patch"} <= set(old.parts)

    old_capture.undo()  # upgrade: the current capture and restore from here on
    svc.send("legacy", "continue with b", requester="sean")
    s = await settle(svc, "legacy")
    # the agent checks the commit, the uncommitted change, the untracked file and that ignored files stayed out;
    # it exits 7 when its conversation was not restored
    assert s.state == SessionState.IDLE and s.turns[-1].exit_code == 0, (s.reason, s.turns[-1].summary)
    assert s.turns[-1].mode == "resume"
    sbox = box(fleet, s.sandbox_id)
    repo = sbox / "workspace/remote"
    assert _git(repo, "log", "--format=%s").split("\n")[:3] == ["feat: b", "feat: a", "init"]
    assert (repo / "-rf odd name.txt").read_text() == "odd\n"
    assert (sbox / "home/agent" / exchange.io_rel(s) / "in/1/brief.md").read_text() == "kept across the upgrade\n"
    conv = sbox / "home/agent/.fake/sessions" / f"{s.harness_session}.jsonl"
    assert conv.read_text().splitlines() == ["start the work", "continue with b"]

    # the first checkpoint after the upgrade has nothing to copy from, uploads everything, and restores too
    new = s.checkpoints[-1]
    assert new.reason == "turn" and set(new.digests) == set(new.parts)
    svc.suspend("legacy", requester="sean")
    s = await settle(svc, "legacy")
    svc.open("legacy", requester="sean")
    s = await settle(svc, "legacy")
    assert s.state == SessionState.IDLE and "older checkpoint" not in (s.reason or ""), s.reason
    assert (box(fleet, s.sandbox_id) / "workspace/remote/b.txt").exists()


async def test_a_new_capture_skips_an_unchanged_session_checkpointed_by_the_old_one(svc, world):  # noqa: F811
    """The fingerprint formula is unchanged, so the first periodic check after an upgrade finds nothing to do."""
    old_capture = pytest.MonkeyPatch()
    old_capture.setattr(SessionService, "_checkpoint", _old_checkpoint)
    svc.new(NewSession(name="steady", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "steady")
    assert s.state == SessionState.IDLE, s.reason
    old_capture.undo()
    async with svc._locks[s.id]:
        assert await svc._checkpoint(s, "interval") is None
    assert len(s.checkpoints) == 1 and not s.checkpoints[0].digests
