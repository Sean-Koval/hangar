"""The turn driver against the real Claude Code and Codex CLIs, offline (workstream steer).

The scripted model APIs in fixtures/live/fake_models.py stand in for Anthropic and OpenAI. Skipped unless
HANGAR_TEST_CLAUDE and/or HANGAR_TEST_CODEX name the CLIs (verified with claude 2.1.296 and codex 0.162.1):

    HANGAR_TEST_CLAUDE=$(command -v claude) HANGAR_TEST_CODEX=$(command -v codex) \
        uv run pytest tests/test_driver_real_clis.py
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from hangar import driver
from tests.fixtures.live import fake_models

CLIS = {k: v for k, v in (("claude", os.environ.get("HANGAR_TEST_CLAUDE")),
                          ("codex", os.environ.get("HANGAR_TEST_CODEX"))) if v}
pytestmark = pytest.mark.skipif(not CLIS, reason="set HANGAR_TEST_CLAUDE / HANGAR_TEST_CODEX to the real CLIs")
KEY = b"0123456789abcdef"


class Turn:
    """One driver run around a real CLI, with hangar's side of the control channel."""

    def __init__(self, which: str, tmp: Path, approvals: str = "ask", delay: float = 0.0,
                 call: tuple[str, dict] | None = None, shell: str | None = None, resume: str | None = None,
                 prompt: str = "RUN please make the probe file"):
        """`resume`: a later turn of a conversation (Claude Code), in the same home as the turn before."""
        self.srv, port = fake_models.serve(delay, call, shell)
        home, self.ctl = tmp / "home", tmp / f"ctl-{uuid.uuid4().hex[:6]}"
        self.work = home / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.ctl.mkdir()
        (self.ctl / "key").write_bytes(KEY)
        (self.ctl / "prompt").write_text(prompt)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "CLAUDE", "OPENAI", "CODEX"))}
        env.update(HOME=str(home), HANGAR_PROMPT_FILE=str(self.ctl / "prompt"))
        self.session = resume or str(uuid.uuid4())
        if which == "claude":
            (home / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
            env.update(ANTHROPIC_API_KEY="sk-ant-fake", ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}",
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1")
            cmd = [CLIS[which], "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                   "--verbose", "--resume" if resume else "--session-id", self.session, "--model", "claude-sonnet-5-5"]
        else:
            (home / ".codex").mkdir(exist_ok=True)
            env.update(CODEX_HOME=str(home / ".codex"), HANGAR_MODEL="gpt-5.3-codex")
            if resume:
                env["HANGAR_SESSION_RESUME"] = resume  # the driver opens it with thread/resume
            cmd = [CLIS[which], "-c", f'model_providers.fake={{name="fake",base_url="http://127.0.0.1:{port}/v1",'
                                      'wire_api="responses"}', "-c", "model_provider=fake", "app-server"]
        self.p = subprocess.Popen([sys.executable, driver.__file__, "--harness", which, "--ctl", str(self.ctl),
                                   "--approvals", approvals, "--approval-timeout", "60", "--", *cmd],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=self.work)
        self.lines: list[dict] = []
        self.q: queue.Queue[dict] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.p.stdout is not None
        for raw in self.p.stdout:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                self.lines.append(d)
                self.q.put(d)

    def wait_for(self, kind: str, timeout: float = 60) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            try:
                d = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if d.get("type") == kind:
                return d
        raise AssertionError(f"no {kind} in {self.lines[-5:]}")

    def command(self, op: str, **args) -> None:
        c = {"id": f"c-{uuid.uuid4().hex[:8]}", "op": op, "args": args}
        c["mac"] = driver.sign(KEY, c)
        (self.ctl / f".{c['id']}").write_text(json.dumps(c))
        os.rename(self.ctl / f".{c['id']}", self.ctl / "in" / f"{c['id']}.json")

    def finish(self, timeout: float = 90) -> int:
        try:
            return self.p.wait(timeout)
        finally:
            self.srv.shutdown()

    def said(self) -> str:
        """The agent's last words, from the log as hangar's summary extraction reads it."""
        from hangar.sessions import extract_summary

        return extract_summary(None, "\n".join(json.dumps(x) for x in self.lines)) or ""

    def acks(self) -> list[dict]:
        return [json.loads(x) for x in (self.ctl / "acks").read_text().splitlines()]


@pytest.fixture(params=sorted(CLIS) or ["none"])
def cli(request):
    return request.param


def test_approval_allow(cli, tmp_path):
    t = Turn(cli, tmp_path)
    req = t.wait_for("hangar.approval_request")
    assert "touch probe.txt" in req["detail"] and req["policy"] == "ask"
    assert not (t.work / "probe.txt").exists()
    t.command("approve", request=req["id"], decision="allow", by="test")
    assert t.finish() == 0 and (t.work / "probe.txt").exists()
    assert [a["ok"] for a in t.acks()] == [True] and (t.ctl / "done").exists() and not (t.ctl / "key").exists()


def test_steer_and_deny(cli, tmp_path):
    t = Turn(cli, tmp_path)
    req = t.wait_for("hangar.approval_request")
    t.command("steer", text="STEER-7731 also say bye")
    t.wait_for("hangar.steer")
    t.command("approve", request=req["id"], decision="deny", message="not this one", by="test")
    assert t.finish() == 0 and not (t.work / "probe.txt").exists()
    assert "STEER-7731" in json.dumps(t.lines[-12:]), t.lines[-12:]


@pytest.mark.parametrize("follow_up", [None, "INSTEAD-4410 do this"])
def test_interrupt(cli, tmp_path, follow_up):
    t = Turn(cli, tmp_path)
    t.wait_for("hangar.approval_request")
    t.command("interrupt", **({"text": follow_up} if follow_up else {}))
    resolved = t.wait_for("hangar.approval_resolved")
    assert resolved["decision"] == "cancelled" and resolved["by"] == "harness"
    code = t.finish()
    if follow_up:
        assert code == 0 and "INSTEAD-4410" in t.said(), t.said()
    else:
        assert code == 130
    assert not (t.work / "probe.txt").exists()


def test_auto_runs_without_prompts(cli, tmp_path):
    t = Turn(cli, tmp_path, approvals="auto")
    assert t.finish() == 0 and (t.work / "probe.txt").exists()
    assert not [x for x in t.lines if x.get("type") == "hangar.approval_request"]


@pytest.mark.skipif("claude" not in CLIS, reason="set HANGAR_TEST_CLAUDE")
def test_deny_risky_refuses_reading_credentials_claude(tmp_path):
    """Claude Code asks before reading outside the working tree; deny-risky must refuse credentials."""
    secret = tmp_path / "home" / ".ssh" / "id_ed25519"
    secret.parent.mkdir(parents=True)
    secret.write_text("PRIVATE-KEY-9911")
    t = Turn("claude", tmp_path, approvals="deny-risky", call=("Read", {"file_path": str(secret)}))
    assert t.finish() == 0
    resolved = [x for x in t.lines if x.get("type") == "hangar.approval_resolved"]
    assert [r["decision"] for r in resolved] == ["deny"] and "credentials" in resolved[0]["message"]
    assert "PRIVATE-KEY-9911" not in json.dumps(t.lines)


def test_allow_always_lasts_for_the_turn_only(cli, tmp_path):
    """`allow_always` is Claude Code's own suggestions with destination `session`, or codex's `acceptForSession`:
    nothing is written to the repo's or the user's settings, and the next turn (a new process resuming the
    conversation) asks again."""
    t = Turn(cli, tmp_path)
    req = t.wait_for("hangar.approval_request")
    t.command("approve", request=req["id"], decision="allow_always", by="test")
    assert t.finish() == 0 and (t.work / "probe.txt").exists()
    assert not list((tmp_path / "home").rglob("settings*.json"))
    assert not list(t.work.rglob("*.toml")) and not list(t.work.rglob(".claude*"))
    (t.work / "probe.txt").unlink()
    thread = next((x["thread_id"] for x in t.lines if x.get("type") == "thread.started"), None)
    again = Turn(cli, tmp_path, resume=t.session if cli == "claude" else thread, prompt="RUN AGAIN please")
    req = again.wait_for("hangar.approval_request")
    again.command("approve", request=req["id"], decision="deny", by="test")
    assert again.finish() == 0 and not (again.work / "probe.txt").exists()
