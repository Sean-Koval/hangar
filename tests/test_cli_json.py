import json

from typer.testing import CliRunner

from hangar import cli

runner = CliRunner()

SANDBOXES = [{"id": "sb1", "name": "alpha", "state": "running", "environment": "python", "provider": "docker"}]
ENVS = [{"name": "python", "harnesses": ["claude", "codex"], "images": {"docker": "reg/python:abc"}}]
HARNESSES = [
    {"name": "claude-code", "version": "latest", "agent": True, "deprecated": None},
    {"name": "codex", "version": "0.162.1", "agent": True, "deprecated": None},
    {"name": "gemini", "version": "latest", "agent": True, "deprecated": "retiring"},
    {"name": "git", "version": "latest", "agent": False, "deprecated": None},
]


def _patch(monkeypatch, data):
    monkeypatch.setattr(cli, "_call", lambda *a, **kw: data)


def test_sb_ls_json(monkeypatch):
    _patch(monkeypatch, SANDBOXES)
    r = runner.invoke(cli.app, ["sb", "ls", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.output) == SANDBOXES


def test_sb_ls_default_is_table(monkeypatch):
    _patch(monkeypatch, SANDBOXES)
    r = runner.invoke(cli.app, ["sb", "ls"])
    assert r.exit_code == 0
    assert r.output == f"{'sb1':12} {'alpha':28} {'running':9} {'python':18} docker\n"


def test_env_list_json(monkeypatch):
    _patch(monkeypatch, ENVS)
    r = runner.invoke(cli.app, ["env", "list", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.output) == ENVS


def test_env_list_default_is_table(monkeypatch):
    _patch(monkeypatch, ENVS)
    r = runner.invoke(cli.app, ["env", "list"])
    assert r.exit_code == 0
    assert r.output == f"{'python':22} {'claude, codex':34} docker:abc\n"


def test_pack_list_json(monkeypatch):
    _patch(monkeypatch, HARNESSES)
    r = runner.invoke(cli.app, ["pack", "list", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.output) == HARNESSES


def test_pack_list_default_is_table(monkeypatch):
    _patch(monkeypatch, HARNESSES)
    r = runner.invoke(cli.app, ["pack", "list"])
    assert r.exit_code == 0
    expected = (
        f"{'claude-code':16} {'latest':12} agent\n"
        f"{'codex':16} {'0.162.1':12} agent\n"
        f"{'gemini':16} {'latest':12} agent (deprecated)\n"
        f"{'git':16} latest\n"
    )
    assert r.output == expected


def test_job_events_and_follow(monkeypatch):
    """`hangar job events -f` prints new events until the job has finished and nothing more arrives."""
    pages = [{"events": [{"seq": 1, "ts": 0, "turn": 1, "kind": "command", "text": "pytest -q"}], "next": 1},
             {"events": [{"seq": 2, "ts": 0, "turn": 1, "kind": "usage", "text": "1.2k tokens, $0.01"}], "next": 2},
             {"events": [], "next": 2}]
    calls = []

    def fake(method, path, **kw):
        calls.append((path, kw.get("params")))
        return pages.pop(0) if path.endswith("/events") else {"state": "succeeded"}

    monkeypatch.setattr(cli, "_call", fake)
    monkeypatch.setattr("time.sleep", lambda s: None)
    r = runner.invoke(cli.app, ["job", "events", "job-1", "--follow"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert len(lines) == 2 and "command" in lines[0] and "pytest -q" in lines[0] and "$0.01" in lines[1]
    assert [p["since"] for path, p in calls if path.endswith("/events")] == [0, 1, 2]
    monkeypatch.setattr(cli, "_call", lambda *a, **kw: {"events": [{"seq": 5, "ts": 0, "kind": "message",
                                                                     "text": "hi"}], "next": 5})
    r = runner.invoke(cli.app, ["session", "events", "s", "--json"])
    assert json.loads(r.output)["text"] == "hi"

