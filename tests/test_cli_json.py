import json

from typer.testing import CliRunner

from hangar import cli

runner = CliRunner()

SANDBOXES = [{"id": "sb1", "name": "alpha", "state": "running", "environment": "python", "provider": "docker"}]
ENVS = [{"name": "python", "harnesses": ["claude", "codex"], "images": {"docker": "reg/python:abc"}}]


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
