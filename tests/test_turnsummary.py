"""A turn that ends without the agent's own summary gets one built from its events, never raw harness output
(KAN-137: an over-budget turn's summary and draft PR body were the tail of Claude Code's stream-json)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hangar import turnsummary
from hangar.sessions import extract_summary

FIX = Path(__file__).parent / "fixtures"
CUT = (FIX / "turnsummary" / "claude-code-over-budget.jsonl").read_text()
STREAMS = {"claude-code": "claude-code.jsonl", "codex": "codex.jsonl", "pi": "pi.jsonl",
           "antigravity": "antigravity.jsonl"}


def _no_stream(text: str) -> None:
    for ln in text.splitlines():
        assert not ln.lstrip().lstrip(">").lstrip().startswith(("{", "[")), ln
    for key in ('"type"', "tool_use", "session_id", "toolu_"):
        assert key not in text, key


def test_a_claude_code_turn_cut_off_mid_tool_is_summarised_from_its_events():
    t1 = time.time()
    out = extract_summary("claude-code", CUT, outcome="over_budget",
                          reason="ran 45 min; the budget is 45 min", started_at=t1 - 45 * 60, finished_at=t1)
    assert out is not None
    _no_stream(out)
    assert out.startswith("Stopped over budget after 45 min (ran 45 min; the budget is 45 min).")
    assert "> The staging step is in. Next: run the apply tests" in out  # the agent's last messages
    assert "> I'll make apply all or nothing" in out
    assert "Files changed" not in out  # a session turn doesn't know what will be delivered


def test_a_job_summary_has_the_stop_reason_diffstat_and_last_check():
    out = turnsummary.build(outcome="timed_out", started_at=1000.0, finished_at=1000.0 + 30 * 60,
                            said=["Halfway through the migration."],
                            diffstat=" src/a.py | 4 ++--\n 1 file changed, 2 insertions(+), 2 deletions(-)",
                            files_known=True, verify="bash scripts/ci/verify.sh", verify_exit_code=1,
                            verify_ran=True)
    _no_stream(out)
    assert out.splitlines()[0].startswith("Timed out after 30 min.")
    assert "    src/a.py | 4 ++--" in out
    assert "Last check `bash scripts/ci/verify.sh`: failed (exit 1)." in out
    assert "not run" in turnsummary.build(outcome="over_budget", verify="make test")
    assert "Files changed: none committed." in turnsummary.build(outcome="over_budget", files_known=True)


@pytest.mark.parametrize("harness", sorted(STREAMS))
def test_no_harness_stream_reaches_a_stopped_turns_summary(harness):
    """Each harness's real capture with its final event dropped and its last line cut, as a stopped turn leaves
    it."""
    lines = (FIX / "streams" / STREAMS[harness]).read_text().splitlines()
    body = [ln for ln in lines if not any(k in ln for k in ('"result"', "turn.completed", "agent_end"))]
    log = "\n".join(body[:-1]) + "\n" + body[-1][: len(body[-1]) // 2]
    out = extract_summary(harness, log, outcome="over_budget", reason="used 900,000 tokens")
    assert out is not None and out.startswith("Stopped over budget (used 900,000 tokens).")
    _no_stream(out)


def test_normal_turns_are_unchanged():
    log = (FIX / "streams" / "claude-code.jsonl").read_text()
    assert extract_summary("claude-code", log) == extract_summary("claude-code", log, outcome="exited")
    assert extract_summary("claude-code", log, outcome="exited") == json.loads(log.splitlines()[-1])["result"]
    assert extract_summary(None, "plain output\nlast line", outcome="exited").endswith("last line")
    assert extract_summary("claude-code", '{"type":"system","subtype":"init"}\n', outcome="exited") is None
