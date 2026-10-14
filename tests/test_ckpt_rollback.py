"""Rolling back to the version before the stack (main at 2e3e0a3) must not strand suspended sessions: checkpoints the
current capture writes, including parts copied inside the store rather than uploaded again, must restore with that
version's restore. `_old_restore_checkpoint` is 2e3e0a3's `SessionService._restore_checkpoint`, frozen verbatim
(docs/upgrading.md, "Rolling back"). Fake provider, real git and tmux."""

from __future__ import annotations

import asyncio
import shlex
import shutil

import pytest

from hangar.models import SessionState
from hangar.sessions import NewSession, SessionError, SessionService
from tests.test_session_streams import box, settle, svc, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

_PARTS = ("agent.tgz", "repo.bundle", "wip.patch", "untracked.tgz")  # 2e3e0a3's sessions.PARTS
_CKPT_REF = "refs/hangar/checkpoint"
_RESTORE_DIR = "/tmp/hangar-restore"


async def _old_restore_checkpoint(self, s, sb, repo, ck) -> None:
    """SessionService._restore_checkpoint as of 2e3e0a3: one write per part, then one script. That version's
    `_restore` set the commit identity before every restore; the current one does it in its restore script."""
    await self._sh(sb, "git config user.email >/dev/null || git config user.email hangar@localhost; "
                       "git config user.name >/dev/null || git config user.name hangar", cwd=repo)
    q = shlex.quote
    _, prov = self.fleet._running(sb.id)
    present: set[str] = set()
    for part in _PARTS:
        if part not in ck.parts:
            continue
        blob = await self.blobs.get(f"{s.id}/{ck.id}/{part}.enc")
        if blob is None:
            raise SessionError(f"checkpoint part {part} is missing from {self.blobs.description}")
        data = await asyncio.to_thread(self.vault.decrypt, blob)
        await prov.write_file(sb, f"{_RESTORE_DIR}/{part}", data, mode=0o600)
        present.add(part)
    r, home = _RESTORE_DIR, f"/home/{self.settings.sandbox_user}"
    base = ck.base or s.base_sha or "HEAD"
    target = q(ck.head or base)
    steps = [
        "git reset -q --hard && git clean -fdq",
        (f"git fetch -q {r}/repo.bundle {_CKPT_REF} && git checkout -q -B {q(s.branch)} FETCH_HEAD"
         if "repo.bundle" in present else
         f"(git cat-file -e {target}^{{commit}} 2>/dev/null || git fetch -q origin {target})"
         f" && git checkout -q -B {q(s.branch)} {target}"),
    ]
    if "wip.patch" in present:
        steps.append(f"if [ -s {r}/wip.patch ]; then git apply --binary --whitespace=nowarn {r}/wip.patch; fi")
    if "untracked.tgz" in present:
        steps.append(f"tar xzf {r}/untracked.tgz -C {q(repo)}")
    if "agent.tgz" in present:
        steps.append(f"tar xzf {r}/agent.tgz -C {q(home)}")
    steps.append(f"rm -rf {r}")
    res = await self._sh(sb, " && ".join(steps), cwd=repo, timeout=600)
    if res.exit_code != 0:
        raise SessionError(f"restore failed: {(res.stderr or res.stdout)[-600:]}")
    head = (await self._sh(sb, "git rev-parse HEAD", cwd=repo)).stdout.strip()
    if ck.head and head != ck.head:
        raise SessionError(f"restore ended at {head[:12]}, expected {ck.head[:12]}")


async def test_checkpoints_written_now_restore_with_the_pre_stack_restore(svc, world, fleet):  # noqa: F811
    svc.new(NewSession(name="rollback", environment="sesenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "rollback")
    assert s.state == SessionState.IDLE, s.reason
    (box(fleet, s.sandbox_id) / "workspace/remote/later.txt").write_text("written after the turn\n")
    svc.suspend("rollback", requester="sean")
    s = await settle(svc, "rollback")
    assert s.state == SessionState.SUSPENDED, s.reason
    last, before = s.checkpoints[-1], s.checkpoints[-2]
    assert last.reason == "suspend" and set(last.digests) == set(last.parts)
    copied = [p for p in last.parts if before.digests.get(p) == last.digests[p]]
    assert "repo.bundle" in copied  # nothing new was committed: this part was copied inside the store

    restored = []

    async def old(self, s, sb, repo, ck) -> None:
        await _old_restore_checkpoint(self, s, sb, repo, ck)
        restored.append(ck.id)

    roll = pytest.MonkeyPatch()
    roll.setattr(SessionService, "_restore_checkpoint", old)  # the rollback
    try:
        svc.send("rollback", "continue with b", requester="sean")
        s = await settle(svc, "rollback")
    finally:
        roll.undo()
    # the agent checks the commit, the uncommitted change and the untracked file, and exits 7 without its conversation
    assert s.state == SessionState.IDLE and s.turns[-1].exit_code == 0, (s.reason, s.turns[-1].summary)
    assert restored == [last.id] and s.turns[-1].mode == "resume"
    repo = box(fleet, s.sandbox_id) / "workspace/remote"
    assert (repo / "later.txt").read_text() == "written after the turn\n"
