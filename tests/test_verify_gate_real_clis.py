"""The verify gate against the real Claude Code and Codex CLIs, offline (--- overnight/autofix).

The scripted model APIs in fixtures/live/fake_models.py stand in for Anthropic and OpenAI: the model runs one shell
command (`echo x >> count.txt`) per turn, and again when the last message says AGAIN. The job's check passes once
count.txt has two lines and prints "RUN AGAIN" until then, so the check's output is what makes the model work on.
Skipped unless HANGAR_TEST_CLAUDE and/or HANGAR_TEST_CODEX name the CLIs (verified with claude 2.1.296 and codex
0.162.1):

    HANGAR_TEST_CLAUDE=$(command -v claude) HANGAR_TEST_CODEX=$(command -v codex) \
        uv run pytest tests/test_verify_gate_real_clis.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from hangar import driver, verifyloop
from hangar.models import now
from tests.fixtures.live import fake_models

CLAUDE, CODEX = os.environ.get("HANGAR_TEST_CLAUDE"), os.environ.get("HANGAR_TEST_CODEX")
CHECK = '[ "$(cat count.txt 2>/dev/null | wc -l)" -ge 2 ] || { echo "RUN AGAIN: count.txt is short"; exit 1; }'
CALL = "echo x >> count.txt"


def _home(tmp: Path) -> tuple[Path, Path, dict[str, str]]:
    home, work = tmp / "home", tmp / "home" / "work"
    work.mkdir(parents=True)
    (home / ".hangar" / "runs").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "CLAUDE", "OPENAI", "CODEX"))}
    env.update(HOME=str(home), HANGAR_RUN_ID="run-1")
    return home, work, env


def _rounds(home: Path) -> list[str]:
    return (home / ".hangar/runs/run-1.verify").read_text().splitlines()


@pytest.mark.skipif(not CLAUDE, reason="set HANGAR_TEST_CLAUDE to the real claude CLI")
@pytest.mark.parametrize("live", [False, True])
def test_claude_stop_hook_from_settings_flag(tmp_path, live):
    """`--settings '<json>'` carries the Stop hook; exit 2 sends Claude back with the check's output; and the
    agent's own settings files can't switch it off (`disableAllHooks: true` in user, project and local settings)."""
    srv, port = fake_models.serve(call=("Bash", {"command": CALL}))
    home, work, env = _home(tmp_path)
    (home / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
    for f in (home / ".claude/settings.json", work / ".claude/settings.json", work / ".claude/settings.local.json"):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"disableAllHooks": True}))
    env.update(ANTHROPIC_API_KEY="sk-ant-fake", ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1")
    settings = verifyloop.claude_settings(CHECK, str(work), 3, int(now()) + 600, 60)
    common = ["--output-format", "stream-json", "--verbose", "--session-id", str(uuid.uuid4()),
              "--model", "claude-sonnet-5-5", "--settings", settings]
    try:
        if live:  # under hangar's driver, the way live turns run
            (tmp_path / "ctl").mkdir()
            (tmp_path / "ctl" / "key").write_bytes(b"k")
            (tmp_path / "prompt").write_text("RUN please count")
            env["HANGAR_PROMPT_FILE"] = str(tmp_path / "prompt")
            argv = [sys.executable, driver.__file__, "--harness", "claude", "--ctl", str(tmp_path / "ctl"),
                    "--approvals", "auto", "--", CLAUDE, "-p", "--input-format", "stream-json", *common]
        else:
            argv = [CLAUDE, "-p", "RUN please count", "--dangerously-skip-permissions", *common]
        p = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True, timeout=180)
    finally:
        srv.shutdown()
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    assert (work / "count.txt").read_text().splitlines() == ["x", "x"]
    assert _rounds(home) == ["fail 1", "pass"]
    assert "RUN AGAIN" in p.stdout  # the hook's stderr went into the conversation


@pytest.mark.skipif(not CODEX, reason="set HANGAR_TEST_CODEX to the real codex CLI")
def test_codex_driver_gate(tmp_path):
    srv, port = fake_models.serve(shell=CALL)
    home, work, env = _home(tmp_path)
    (home / ".codex").mkdir()
    (tmp_path / "ctl").mkdir()
    (tmp_path / "ctl" / "key").write_bytes(b"k")
    (tmp_path / "prompt").write_text("please count")
    env.update(CODEX_HOME=str(home / ".codex"), HANGAR_MODEL="gpt-5.3-codex",
               HANGAR_PROMPT_FILE=str(tmp_path / "prompt"),
               HANGAR_VERIFY_GATE=json.dumps({"command": CHECK, "cwd": str(work), "rounds": 3,
                                              "deadline": int(now()) + 600, "timeout": 60}))
    try:
        p = subprocess.run([sys.executable, driver.__file__, "--harness", "codex", "--ctl", str(tmp_path / "ctl"),
                            "--approvals", "auto", "--", CODEX, "-c",
                            f'model_providers.fake={{name="fake",base_url="http://127.0.0.1:{port}/v1",'
                            'wire_api="responses"}', "-c", "model_provider=fake", "app-server"],
                           cwd=work, env=env, capture_output=True, text=True, timeout=180)
    finally:
        srv.shutdown()
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    assert (work / "count.txt").read_text().splitlines() == ["x", "x"]
    assert _rounds(home) == ["fail 1", "pass"]
    lines = [json.loads(x) for x in p.stdout.splitlines() if x.startswith("{")]
    assert len({x["thread_id"] for x in lines if x.get("type") == "thread.started"}) == 1  # same conversation
