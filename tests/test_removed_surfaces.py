"""KAN-217: the A2A endpoint, the E2B-compatible API, the GitHub Projects door and the gemini harness are gone.

Their routes answer 404, and a removed setting is refused wherever it is set, naming the upgrade note
(docs/upgrading.md#removed-surfaces), as KAN-74's removed switches are.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hangar.config import REMOVED_SETTINGS, Settings, removed_setting

NOTE = "docs/upgrading.md#removed-surfaces"


def test_every_removed_setting_is_gone_and_named():
    assert set(REMOVED_SETTINGS) >= {"a2a_enabled", "e2b_api_path", "e2b_api_domain", "github_projects",
                                     "e2b_max_streams_per_sandbox", "a2a_max_streams_per_owner"}
    for name in REMOVED_SETTINGS:
        assert name not in Settings.model_fields, name
        msg = removed_setting(name)
        assert f"HANGAR_{name.upper()}" in msg and NOTE in msg and "hangar config unset" in msg
    assert removed_setting("e2b_api_key") is None  # the provider's own settings stay
    assert "e2b_api_url" in Settings.model_fields and "preview_max_streams_per_sandbox" in Settings.model_fields


@pytest.mark.parametrize("env, value", [("HANGAR_A2A_ENABLED", "true"), ("HANGAR_E2B_API_PATH", "/e2b"),
                                        ("hangar_github_projects", "{}")])
def test_a_removed_setting_in_the_environment_stops_hangar(env, value, monkeypatch, tmp_path):
    monkeypatch.setenv(env, value)
    with pytest.raises(ValidationError) as e:
        Settings(data_dir=tmp_path)
    assert env.upper() in str(e.value) and NOTE in str(e.value)


def test_a_removed_setting_given_directly_is_refused(tmp_path):
    with pytest.raises(ValidationError, match="HANGAR_A2A_ENABLED"):
        Settings(data_dir=tmp_path, a2a_enabled=False)


def test_hangar_serve_refuses_to_start_naming_the_note(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from hangar.cli import app

    monkeypatch.setenv("HANGAR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HANGAR_E2B_TEMPLATES", '{"base": "base"}')
    r = CliRunner().invoke(app, ["serve"])
    assert r.exit_code == 2
    assert "hangar can't start: HANGAR_E2B_TEMPLATES" in r.output and NOTE in r.output


def test_hangar_config_refuses_it_and_a_stored_one_is_not_applied_until_unset(settings, store):
    from hangar.settings_store import SettingError, SettingsStore

    cfg = SettingsStore(store, settings)
    with pytest.raises(SettingError, match="removed-surfaces"):
        cfg.set("HANGAR_GITHUB_PROJECTS", '{"acme/7": {"trigger": "Ready"}}')
    store.kv_set("setting:github_projects", json.dumps({"acme/7": {"trigger": "Ready"}}))  # stored by v0.2.0
    store.kv_set("setting:a2a_enabled", "true")
    cfg.apply()  # not applied, and no error
    assert not hasattr(settings, "github_projects")
    assert [k for k, _ in cfg.removed_settings()] == ["HANGAR_A2A_ENABLED", "HANGAR_GITHUB_PROJECTS"]
    assert cfg.unset("HANGAR_GITHUB_PROJECTS")["source"] == "removed"
    assert [k for k, _ in cfg.removed_settings()] == ["HANGAR_A2A_ENABLED"]
    with pytest.raises(SettingError, match="unknown setting"):
        cfg.unset("HANGAR_NOT_A_SETTING")


def test_the_routes_answer_404_and_the_doctor_fails_on_a_stored_one(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    app = create_app(settings)
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not [p for p in paths if p.startswith(("/a2a", "/e2b", "/.well-known/agent-card"))]
    auth = {"Authorization": "Bearer t0ken"}
    with TestClient(app) as c:
        assert c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage"}, headers=auth).status_code \
            == 404
        assert c.get("/.well-known/agent-card.json").status_code == 404
        assert c.get("/e2b/health").status_code == 404
        assert c.get("/e2b/sandboxes", headers=auth).status_code == 404
        assert "a2a_agent_card" not in c.get("/api/connect", headers=auth).json()
        app.state.hangar.store.kv_set("setting:e2b_api_domain", json.dumps("e2b.example.com"))
        checks = {x["name"]: x for x in c.get("/api/doctor", headers=auth).json()}
    check = checks["removed setting HANGAR_E2B_API_DOMAIN"]
    assert check["status"] == "fail" and NOTE in check["detail"]
    assert check["fix"] == "hangar config unset HANGAR_E2B_API_DOMAIN"


def test_the_sandboxes_scope_and_the_gemini_harness_are_gone():
    from hangar.auth import SCOPES
    from hangar.canary import load_lock
    from hangar.harnesses import HARNESSES

    assert "sandboxes" not in SCOPES
    assert "gemini" not in HARNESSES and "gemini" not in load_lock()
    assert "antigravity" in HARNESSES


async def test_a_projects_item_starts_nothing():
    from types import SimpleNamespace

    from hangar.jobs import handle_github

    res = await handle_github(SimpleNamespace(), "projects_v2_item", "d-1", {"action": "edited"})
    assert "GitHub Projects intake was removed" in res["ignored"]
