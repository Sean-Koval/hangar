"""Settings stored in the database (`hangar config`): validation, precedence under the environment, restarts."""

import pytest
from fastapi.testclient import TestClient

from hangar.app import Hangar, create_app
from hangar.config import Settings
from hangar.settings_store import SettingError, SettingsStore, field_name
from hangar.store import Store
from tests.conftest import ENVS, FakeProvider


def _settings(tmp_path, **kw) -> Settings:
    return Settings(data_dir=tmp_path, env_dirs=[ENVS], enabled_providers=[], reaper_interval_seconds=3600, **kw)


def test_field_names():
    assert field_name("HANGAR_GITHUB_REPOS") == field_name("github-repos") == field_name("github_repos") == \
        "github_repos"


def test_set_validates_and_applies_live(tmp_path):
    s = _settings(tmp_path)
    cfg = SettingsStore(Store(s.db_path), s)
    d = cfg.set("HANGAR_GITHUB_REPOS", '{"acme/app": "python"}')
    assert s.github_repos == {"acme/app": "python"} and d["source"] == "hangar config" and not d["restart"]
    assert cfg.set("job_keep_minutes", "30")["value"] == 30 and s.job_keep_minutes == 30
    assert cfg.set("github_trigger_label", "agent")["value"] == "agent"
    assert cfg.set("best_of_n_push_attempts", "true")["value"] is True
    assert cfg.set("github_trusted", '["alice", "@acme/devs"]')["value"] == ["alice", "@acme/devs"]
    assert cfg.set("budget_max_cost_usd", 2.5)["value"] == 2.5
    assert cfg.set("job_concurrency", "2")["restart"] is True
    with pytest.raises(SettingError, match="invalid value for HANGAR_JOB_KEEP_MINUTES"):
        cfg.set("job_keep_minutes", "soon")
    with pytest.raises(SettingError, match="invalid value"):
        cfg.set("github_repos", "[1, 2]")
    with pytest.raises(SettingError, match="unknown setting"):
        cfg.set("no_such_thing", "1")
    with pytest.raises(SettingError, match="unknown setting"):
        cfg.set("db_path", "/x")  # a property, not a field


@pytest.mark.parametrize("key", ["HANGAR_ADMIN_TOKEN", "secret_key", "e2b_api_key", "e2b_registry_password",
                                 "data_dir", "port"])
def test_secrets_and_bootstrap_fields_refused(tmp_path, key):
    s = _settings(tmp_path)
    with pytest.raises(SettingError, match="can't be stored"):
        SettingsStore(Store(s.db_path), s).set(key, "x")


def test_environment_wins_and_stored_values_survive_restart(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    cfg = SettingsStore(Store(s.db_path), s)
    cfg.set("github_trigger_label", "agent")
    cfg.set("job_keep_minutes", "30")
    # restart: a fresh process applies what was stored...
    h = Hangar(_settings(tmp_path))
    assert h.settings.github_trigger_label == "agent" and h.settings.job_keep_minutes == 30
    # ...but a value from the environment (a config map on GKE) wins, and setting it doesn't change it
    monkeypatch.setenv("HANGAR_JOB_KEEP_MINUTES", "5")
    h = Hangar(_settings(tmp_path))
    assert h.settings.job_keep_minutes == 5
    d = h.config.set("job_keep_minutes", "45")
    assert h.settings.job_keep_minutes == 5 and d["source"] == "environment" and d["stored"] == 45
    # unset falls back to the default (when the environment doesn't pin it)
    assert h.config.unset("github_trigger_label")["value"] == "hangar" and h.settings.github_trigger_label == "hangar"
    assert "job_keep_minutes" in {x["key"].removeprefix("HANGAR_").lower() for x in h.config.list()}


def test_bad_stored_value_is_skipped_not_fatal(tmp_path):
    s = _settings(tmp_path)
    Store(s.db_path).kv_set("setting:job_keep_minutes", '"not a number"')
    assert Hangar(_settings(tmp_path)).settings.job_keep_minutes == 120


@pytest.fixture
def client(settings):
    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.hangar.fleet.providers["docker"] = FakeProvider()
        c.headers["Authorization"] = "Bearer t0ken"
        yield c


def test_settings_routes_admin_only(client):
    h = client.app.state.hangar
    r = client.put("/api/settings/HANGAR_GITHUB_REPOS", json={"value": '{"acme/app": "plain"}'})
    assert r.status_code == 200 and r.json()["value"] == {"acme/app": "plain"}
    assert h.settings.github_repos == {"acme/app": "plain"}
    assert client.get("/api/settings/github_repos").json()["source"] == "hangar config"
    assert any(x["key"] == "HANGAR_GITHUB_REPOS" for x in client.get("/api/settings").json())
    assert len(client.get("/api/settings", params={"all": True}).json()) > 50
    assert not any(x["key"] in ("HANGAR_ADMIN_TOKEN", "HANGAR_SECRET_KEY")
                   for x in client.get("/api/settings", params={"all": True}).json())
    r = client.put("/api/settings/HANGAR_ADMIN_TOKEN", json={"value": "hgr_x"})
    assert r.status_code == 400 and "secret" in r.json()["detail"]
    assert client.delete("/api/settings/github_repos").json()["value"] == {}
    for scope in ("jobs", "mcp"):
        raw, _ = h.auth.create(f"svc-{scope}", [scope])
        hdr = {"Authorization": f"Bearer {raw}"}
        assert client.get("/api/settings", headers=hdr).status_code == 403
        assert client.put("/api/settings/github_repos", json={"value": "{}"}, headers=hdr).status_code == 403
    assert any(e["action"] == "settings.set" for e in client.get("/api/audit").json())


def test_settings_read_at_startup_say_so():
    """The broker copies sandbox_user and the event log copies events_max_mb when hangar starts."""
    from hangar.settings_store import needs_restart

    assert needs_restart("sandbox_user") and needs_restart("events_max_mb")
    # integrate: #52's transport mode is fixed when the app is built; #50's limits are read on every request
    assert needs_restart("mcp_stateless_http") and not needs_restart("job_rate_per_minute")
    assert not needs_restart("job_keep_minutes") and not needs_restart("github_repos")
