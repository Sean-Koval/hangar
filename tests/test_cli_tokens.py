from types import SimpleNamespace

from typer.testing import CliRunner

import hangar.app
import hangar.cli as cli

runner = CliRunner()


def _record(monkeypatch, response):
    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw))
        return response

    monkeypatch.setattr(cli, "_call", fake)
    return calls


def test_create_remote(monkeypatch):
    monkeypatch.setenv("HANGAR_URL", "http://hangar.test")
    calls = _record(monkeypatch, {"id": "t1", "name": "laptop", "token": "hg_secret"})
    r = runner.invoke(cli.app, ["token", "create", "laptop"])
    assert r.exit_code == 0, r.output
    assert calls == [("POST", "/api/tokens", {"json": {"name": "laptop"}})]
    assert "t1" in r.output and r.output.count("hg_secret") == 1


def test_list_remote(monkeypatch):
    monkeypatch.setenv("HANGAR_URL", "http://hangar.test")
    calls = _record(monkeypatch, [{"id": "t1", "name": "laptop", "last_used": None}])
    r = runner.invoke(cli.app, ["token", "list"])
    assert r.exit_code == 0, r.output
    assert calls == [("GET", "/api/tokens", {})]
    assert "t1" in r.output and "laptop" in r.output


def test_revoke_remote(monkeypatch):
    monkeypatch.setenv("HANGAR_URL", "http://hangar.test")
    calls = _record(monkeypatch, {})
    r = runner.invoke(cli.app, ["token", "revoke", "t1"])
    assert r.exit_code == 0, r.output
    assert calls == [("DELETE", "/api/tokens/t1", {})]
    assert "revoked t1" in r.output


def test_local_when_url_unset(monkeypatch):
    monkeypatch.delenv("HANGAR_URL", raising=False)
    calls = _record(monkeypatch, None)

    class FakeAuth:
        def create(self, name):
            return "raw-secret", SimpleNamespace(id="loc1")

    monkeypatch.setattr(hangar.app, "Hangar", lambda: SimpleNamespace(auth=FakeAuth()))
    r = runner.invoke(cli.app, ["token", "create", "x"])
    assert r.exit_code == 0, r.output
    assert "loc1" in r.output and "raw-secret" in r.output
    assert calls == []
