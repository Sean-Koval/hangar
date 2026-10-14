"""The protocols hangar's driver speaks with the real CLIs, recorded and checked (--- canary, docs/harnesses.md).

Codex: the app-server JSON Schema the CLI generates (`codex app-server generate-json-schema`), cut down to the methods
driver.Codex uses and the types they reach. Claude Code: the stream-json messages a live turn writes (an approval, an
interrupt with a follow-up), as message type -> field paths. The driver's own needs (canary.CODEX_USES,
canary.CLAUDE_DRIVER) must hold for whichever CLI runs; the locked CLI must also give the checked-in record in
fixtures/protocol (Codex's exactly; Claude Code's messages, whose fields include timing telemetry that varies by
machine), so the nightly canary's diff of a candidate against it is a diff against the lock.

Skipped unless HANGAR_TEST_CLAUDE / HANGAR_TEST_CODEX name the CLIs. HANGAR_PROTOCOL_OUT=<dir> writes what each CLI
gave there (the canary diffs that; after a bump, copy it over fixtures/protocol):

    uv run python -m hangar.canary install --dest ~/.cache/hangar-clis --env-file .env.clis
    env $(cat .env.clis) HANGAR_PROTOCOL_OUT=/tmp/proto uv run pytest tests/test_protocol_real_clis.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from hangar import canary
from hangar.harnesses import HARNESSES

CLAUDE, CODEX = os.environ.get("HANGAR_TEST_CLAUDE"), os.environ.get("HANGAR_TEST_CODEX")
FIXTURES = Path(__file__).parent / "fixtures" / "protocol"


def _record(name: str, surface: object) -> None:
    if out := os.environ.get("HANGAR_PROTOCOL_OUT"):
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / name).write_text(canary.dump_surface(surface))


def _is_locked(binary: str, harness: str, home: Path) -> bool:
    """Whether this CLI is the version the lock pins (only then must it match the checked-in record)."""
    out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "HOME": str(home)}).stdout
    v = re.escape(HARNESSES[harness].version)
    return re.search(rf"(^|[^0-9A-Za-z.])v?{v}([^0-9A-Za-z.]|$)", out, re.M) is not None


@pytest.mark.skipif(not CODEX, reason="set HANGAR_TEST_CODEX to the real codex CLI")
def test_codex_app_server_protocol(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex")}
    subprocess.run([CODEX, "app-server", "generate-json-schema", "--out", str(tmp_path / "schema")], env=env,
                   check=True, capture_output=True, timeout=120)
    surface = canary.codex_surface(tmp_path / "schema")
    _record("codex-app-server.json", surface)
    assert canary.codex_missing(surface) == [], "the app-server no longer has what driver.Codex uses"
    if _is_locked(CODEX, "codex", home):
        fixture = json.loads((FIXTURES / "codex-app-server.json").read_text())
        assert surface == fixture, "the locked codex disagrees with fixtures/protocol (see the module docstring)"


@pytest.mark.skipif(not CLAUDE, reason="set HANGAR_TEST_CLAUDE to the real claude CLI")
def test_claude_stream_json_protocol(tmp_path):
    from tests.test_driver_real_clis import Turn

    messages: list[dict] = []
    t = Turn("claude", tmp_path / "a")  # a permission prompt, allowed
    req = t.wait_for("hangar.approval_request")
    t.command("approve", request=req["id"], decision="allow", by="test")
    assert t.finish() == 0
    messages += t.lines
    t = Turn("claude", tmp_path / "b")  # an interrupt while a prompt is open, with a follow-up message
    t.wait_for("hangar.approval_request")
    t.command("interrupt", text="INSTEAD do this")
    assert t.finish() == 0
    messages += t.lines
    shape = canary.claude_shape([m for m in messages if not str(m.get("type", "")).startswith("hangar.")])
    _record("claude-stream-json.json", shape)
    assert canary.claude_missing(shape) == [], "Claude Code no longer writes what driver.Claude reads"
    if _is_locked(CLAUDE, "claude-code", tmp_path / "a" / "home"):
        # The messages, exactly; not every field: a result's timing telemetry (time_origin_ms, warm_spare_claimed,
        # ...) comes and goes with the machine. The canary lists such differences without failing on them.
        fixture = json.loads((FIXTURES / "claude-stream-json.json").read_text())
        assert sorted(shape) == sorted(fixture), "the locked claude writes other messages than fixtures/protocol"
