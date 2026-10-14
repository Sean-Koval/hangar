"""A sandbox that comes back blank (a GKE pod recreated under its claim) must not be checkpointed over the session's
last good checkpoint (reconciler.py, B3). On main the capture took the "repo is gone" path and stored a checkpoint
with no conversation in it as the latest."""

from __future__ import annotations

import shutil

import pytest

from hangar.sessions import NewSession, SessionError
from tests.test_strategies import box, settle, svc, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")


async def test_a_blank_sandbox_is_not_checkpointed_over_the_last_good_one(svc, world, fleet):  # noqa: F811
    svc.new(NewSession(name="bl", environment="forkenv", repo=world["url"], message="start the work"),
            owner="admin", requester="sean")
    s = await settle(svc, "bl")
    good = s.checkpoints[-1]
    assert "agent.tgz" in good.parts
    b = box(fleet, s.sandbox_id)
    shutil.rmtree(b / "workspace/remote")  # what a pod recreated under the claim looks like
    for d in svc._agent_dirs(s):  # nor the agent's conversation and hangar's files for it
        shutil.rmtree(b / "home/agent" / d, ignore_errors=True)
    with pytest.raises(SessionError, match="neither the repo nor the agent's conversation"):
        await svc.checkpoint_now("bl", requester="sean")
    assert svc.get("bl").checkpoints[-1].id == good.id
