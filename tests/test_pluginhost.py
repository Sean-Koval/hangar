"""The plugin host (pluginhost.py, docs/plugins.md): the built-in doors and trackers load as plugins, an outside
plugin found through its entry point loads only when HANGAR_PLUGINS enables it, mounts its door and hears events,
and a bad configuration refuses to start."""

from __future__ import annotations

import time
from importlib import metadata

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from hangar import intake, jobs, pluginhost
from hangar.app import Hangar, create_app
from hangar.models import Job, JobResult, JobState
from hangar.pluginhost import PluginConfigError

ADMIN = {"Authorization": "Bearer t0ken"}
RECEIVED: list = []


# ====================================================================== an outside plugin, as a package would ship it
class _Settings(BaseModel):
    model_config = {"extra": "forbid"}
    greeting: str
    events: list[str] = ["pr.*"]


class _Door:
    name = "testdoor"

    def __init__(self, greeting: str):
        self.greeting = greeting

    async def reply(self, svc, job, ref, body, kind):
        RECEIVED.append(("reply", ref, body))

    def mount(self, app: FastAPI) -> None:
        @app.post("/api/hooks/testdoor")
        async def hook():
            return JSONResponse({"greeting": self.greeting})

    def doctor(self):
        return [{"name": "testdoor", "status": "ok", "detail": "reachable", "fix": None}]


class _Notifier:
    name = "testdoor-events"

    def __init__(self, patterns):
        self.patterns = patterns

    def wants(self, person, event):
        import fnmatch

        return any(fnmatch.fnmatchcase(event.type, p) for p in self.patterns)

    async def send(self, event, person):
        RECEIVED.append(("event", event.type, event.id))
        return True


class _Tracker:
    async def __call__(self, svc, job, ticket, stage):
        RECEIVED.append(("tracker", ticket, stage))


class _Plugin:
    name = "testdoor"
    api = "hangar.plugin/v1"
    Settings = _Settings

    def register(self, host, settings: _Settings) -> None:
        host.add_door(_Door(settings.greeting))
        host.add_notifier(_Notifier(settings.events))
        host.add_tracker("testtix", _Tracker())


PLUGIN = _Plugin()


class _FuturePlugin(_Plugin):
    api = "hangar.plugin/v2"


FUTURE = _FuturePlugin()


class _Clash(_Plugin):
    def register(self, host, settings) -> None:
        host.add_tracker("jira", _Tracker())  # the built-in's scheme


CLASH = _Clash()


def _eps(*extra: tuple[str, str]) -> list[metadata.EntryPoint]:
    out = [ep for ep in metadata.entry_points(group="hangar.plugins")]
    return out + [metadata.EntryPoint(name=n, value=v, group="hangar.plugins") for n, v in extra]


@pytest.fixture
def installed(monkeypatch):
    """`testdoor` (and two broken ones) installed beside hangar, as `pip install` would."""
    RECEIVED.clear()
    monkeypatch.setattr(pluginhost, "entry_points", lambda: _eps(
        ("testdoor", "tests.test_pluginhost:PLUGIN"), ("future", "tests.test_pluginhost:FUTURE"),
        ("clash", "tests.test_pluginhost:CLASH")))
    for scheme in ("testdoor",):
        monkeypatch.delitem(jobs.NOTIFIERS, scheme, raising=False)
    monkeypatch.delitem(intake.TRACKERS, "testtix", raising=False)
    yield
    jobs.NOTIFIERS.pop("testdoor", None)
    intake.TRACKERS.pop("testtix", None)


# ====================================================================== built-ins
def test_the_built_ins_load_as_plugins(settings):
    settings.admin_token = "t0ken"
    from fastapi.testclient import TestClient

    app = create_app(settings)
    paths = {getattr(r, "path", "") for r in app.routes}
    assert {"/api/hooks/github", "/api/hooks/slack/events", "/api/hooks/slack/commands",
            "/api/hooks/slack/interactions", "/api/hooks/jira"} <= paths
    from hangar import jira, slack

    assert jobs.NOTIFIERS["slack"] is slack.notify and jobs.NOTIFIERS["jira"] is jira.notify
    assert intake.TRACKERS["jira"] is jira.update and "ghproject" not in intake.TRACKERS  # --- KAN-217
    with TestClient(app) as c:
        views = {p["name"]: p for p in c.get("/api/admin/plugins", headers=ADMIN).json()["plugins"]}
        assert views["slack"]["adds"] == ["door:slack"] and views["slack"]["builtin"]
        assert views["jira"]["adds"] == ["door:jira", "tracker:jira"]
        assert "ghprojects" not in views  # --- KAN-217: removed
        assert views["github"]["adds"] == ["door:github"]
        assert "webhook" not in views  # not configured: not loaded
        # off until configured, as before
        assert c.post("/api/hooks/github", content=b"{}").status_code == 404
        assert c.post("/api/hooks/jira", content=b"{}").status_code == 404
        checks = {x["name"]: x for x in c.get("/api/doctor", headers=ADMIN).json()}
        assert checks["plugin slack"]["status"] == "ok" and "door:slack" in checks["plugin slack"]["detail"]


def test_installed_is_not_enabled(settings, installed):
    app = create_app(settings)
    assert "/api/hooks/testdoor" not in {getattr(r, "path", "") for r in app.routes}
    assert "testdoor" not in jobs.NOTIFIERS and "testtix" not in intake.TRACKERS


# ====================================================================== an outside plugin
def test_an_enabled_plugin_mounts_its_door_and_hears_events(settings, installed, monkeypatch):
    from fastapi.testclient import TestClient

    from hangar import events_out

    monkeypatch.setattr(events_out, "COALESCE", 0.0)
    settings.admin_token = "t0ken"
    settings.plugins = {"enable": ["testdoor"], "doors": {"testdoor": {"greeting": "hello from the plugin"}}}
    app = create_app(settings)
    with TestClient(app) as c:
        assert c.post("/api/hooks/testdoor").json() == {"greeting": "hello from the plugin"}
        assert intake.TRACKERS["testtix"] is not None and "testdoor" in jobs.NOTIFIERS
        views = {p["name"]: p for p in c.get("/api/admin/plugins", headers=ADMIN).json()["plugins"]}
        assert views["testdoor"]["adds"] == ["door:testdoor", "notifier:testdoor-events", "tracker:testtix"]
        assert not views["testdoor"]["builtin"]
        checks = {x["name"]: x for x in c.get("/api/doctor", headers=ADMIN).json()}
        assert checks["testdoor"]["detail"] == "reachable"  # the door's own doctor

        h = app.state.hangar
        job = Job(title="fix it", instructions="x", environment="e", repo="https://github.com/acme/api.git",
                  state=JobState.SUCCEEDED,
                  result=JobResult(pr_url="https://github.com/acme/api/pull/12", pr_number=12))
        h.store.put(job)
        deadline = time.time() + 10
        while time.time() < deadline and not any(r[0] == "event" for r in RECEIVED):
            time.sleep(0.05)
        assert ("event", "pr.opened", f"{job.id}:pr.opened:12") in RECEIVED
        assert all(r[1].startswith("pr.") for r in RECEIVED if r[0] == "event")  # its filter


async def test_a_doors_reply_carries_its_threads(settings, installed):
    settings.plugins = {"enable": ["testdoor"], "doors": {"testdoor": {"greeting": "hi"}}}
    h = Hangar(settings)
    pluginhost.of(h)
    job = Job(title="t", instructions="x", environment="e")
    await h.jobs._comment(job, "testdoor:thread-1", "the agent started")
    assert ("reply", "testdoor:thread-1", "the agent started") in RECEIVED


# ====================================================================== refusing to start
@pytest.mark.parametrize("config, why", [
    ({"enable": ["nope"]}, "'nope', which is not installed"),
    ({"doors": {"testdoor": {"greeting": "hi"}}}, "doesn't enable it"),
    ({"enable": ["testdoor"], "doors": {"testdoor": {}}}, "settings for 'testdoor' are invalid: greeting"),
    ({"enable": ["testdoor"], "doors": {"testdoor": {"greeting": "hi", "colour": "red"}}}, "colour"),
    ({"enable": ["testdoor"], "door": {}}, "unknown key(s) door"),
    ({"enable": "testdoor"}, "must be a list"),
    ({"enable": ["future"]}, "targets 'hangar.plugin/v2'"),
    ({"enable": ["clash"], "settings": {"clash": {"greeting": "x"}}}, "already a 'jira' tracker"),
    ({"notifiers": {"webhook": {"url": "https://bot.example.com/e"}}}, "secret_credential"),
    ({"notifiers": {"webhook": {"url": "http://bot.example.com/e", "secret_credential": "k"}}}, "must use https"),
    ({"notifiers": {"webhook": {"url": "https://10.0.0.5/e", "secret_credential": "k"}}}, "private address"),
    ({"notifiers": {"webhook": {"url": "https://x.example.com/e", "secret_credential": "k"}},
      "settings": {"webhook": {}}}, "configured in two sections"),
])
def test_a_bad_configuration_refuses_to_start(settings, installed, config, why):
    settings.plugins = config
    with pytest.raises(PluginConfigError) as e:
        create_app(settings)
    assert why in str(e.value)


def test_the_webhook_loads_when_configured(settings, installed):
    settings.job_callback_hosts = ["symphony-bot.internal"]
    settings.plugins = {"notifiers": {"webhook": {"url": "https://symphony-bot.internal/hangar/events",
                                                  "secret_credential": "bot-hook-key",
                                                  "events": ["pr.opened", "job.needs_input", "changeset.*"]}}}
    h = Hangar(settings)
    host = pluginhost.of(h)
    assert [n.name for n in host.notifiers] == ["webhook"]
    assert next(x for x in host.views() if x["name"] == "webhook")["adds"] == ["notifier:webhook"]


def test_a_missing_built_in_module_is_reported_not_fatal(settings, monkeypatch):
    monkeypatch.setattr(pluginhost, "entry_points", lambda: [])
    monkeypatch.setitem(pluginhost.BUILTINS, "later", "hangar.not_here_yet:PLUGIN")
    host = pluginhost.of(Hangar(settings))
    assert next(x for x in host.views() if x["name"] == "later")["error"].startswith("not available")


def test_two_plugins_with_one_name_refuse(settings, monkeypatch):
    monkeypatch.setattr(pluginhost, "entry_points", lambda: _eps(("slack", "tests.test_pluginhost:PLUGIN")))
    with pytest.raises(PluginConfigError, match="two installed plugins are named 'slack'"):
        pluginhost.of(Hangar(settings))
