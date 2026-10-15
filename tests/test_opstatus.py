"""The operator console (--- clouddoctor): `hangar status`, `GET /api/status` and `hangar://status`."""

from __future__ import annotations

import json
import logging
import time

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from hangar import identity, metrics, opstatus, slo
from hangar.auth import Principal
from hangar.envrequests import EnvRequest
from hangar.flightrec import FlightRecord
from hangar.models import AuditEvent, CredentialKind, now
from tests.conftest import FakeProvider
from tests.test_jobs import sessions  # noqa: F401 - fixture

MEMBER = Principal("alice-laptop", "tok-a", ["mcp"], person="alice", role="member")
SECRET = "s3cr3t-in-a-log-line-987654321"


@pytest.fixture(autouse=True)
def _fresh_samples(monkeypatch):
    monkeypatch.setattr(slo, "SAMPLES", slo.Samples())
    opstatus.RING_HANDLER.lines.clear()


def test_percentiles_and_targets():
    s = slo.Samples()
    t = time.time()
    for x in (1.0, 1.1, 1.2, 1.3, 9.0):
        s.record("create", x, "warm", at=t)
    s.record("create", 20.0, "cold", at=t)
    s.record("create", 100.0, "cold", at=t - 2 * 86400)  # outside the day
    s.record("turn_start", 3.0)
    out = s.summary(t)
    assert out["create"]["warm"] == {"n": 5, "p50": 1.2, "p95": 9.0, "target_p50": 1.5, "target_p95": 2.5,
                                     "ok": False}
    assert out["create"]["cold"]["n"] == 1 and out["create"]["cold"]["ok"] is True
    assert out["turn_start"]["any"]["ok"] is True


def test_samples_survive_a_restart(store):
    a = slo.Samples()
    a.record("resume", 3.0, "cold")
    a.save(store)
    b = slo.Samples()
    b.record("resume", 5.0, "cold")
    b.load(store)
    assert [x[1] for x in b.by["resume"]] == [3.0, 5.0]
    assert not a.dirty


def test_the_ring_keeps_hangars_warnings_redacted_and_never_the_access_log(broker):
    broker.put("leak", "custom", CredentialKind.GENERIC, SECRET, env_var="LEAK")
    opstatus.install_ring()
    opstatus.install_ring()
    assert logging.getLogger("hangar").handlers.count(opstatus.RING_HANDLER) == 1
    logging.getLogger("hangar.jobs").warning("push failed with %s", SECRET)
    logging.getLogger("hangar.jobs").info("not a warning")
    logging.getLogger("uvicorn.access").warning('GET /api/x?token=%s 200', SECRET)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("hangar.fleet").exception("create failed")
    lines = opstatus.recent_log(broker.redact)
    assert [x["logger"] for x in lines] == ["hangar.fleet", "hangar.jobs"]
    assert SECRET not in json.dumps(lines) and lines[0]["error"] == "RuntimeError: boom"


async def test_status_brings_the_operators_view_together(fleet, store, sessions, monkeypatch):  # noqa: F811
    t = now()
    store.put(FlightRecord(kind="job", trigger="failed", cause="missing_tool", detail="pnpm", environment="node",
                           job_id="job-1", at=t - 60))
    store.put(FlightRecord(kind="turn", trigger="failed", cause="model_api", transient=True, environment="node",
                           at=t - 30))
    store.put(FlightRecord(kind="turn", trigger="failed", cause="model_api", transient=True, environment="py",
                           at=t - 2 * 86400))
    for _ in range(3):
        store.audit(AuditEvent(actor="sandbox:sb-1", action="egress.deny", target="sb-1",
                               detail={"host": "registry.npmjs.org", "port": 443, "environment": "node"}))
    store.audit(AuditEvent(actor="sandbox:sb-2", action="egress.deny", target="sb-2",
                           detail={"host": "registry.npmjs.org", "environment": "node", "suppressed_before": 4}))
    store.put(EnvRequest(kind="egress", environment="node", host="pypi.org", why="pip", requester="session:s",
                         session_id="ses-1", session="s1"))
    slo.record("create", 1.0, "warm")
    v = await opstatus.status(fleet, sessions)
    assert v["failures"]["total"] == 2 and v["failures"]["transient"] == 1
    assert list(v["failures"]["by_cause"]) in (["missing_tool", "model_api"], ["model_api", "missing_tool"])
    [host] = v["egress_denies"]["by_host"]
    assert host["host"] == "registry.npmjs.org" and host["count"] == 8 and host["sandboxes"] == 2
    assert v["requests"]["pending"] == 1 and v["requests"]["items"][0]["target"] == "pypi.org"
    assert v["slo"]["create"]["warm"]["n"] == 1
    assert v["harnesses"]["claude-code"]["locked"]
    assert v["builds"] == {"images": [], "packs": [], "repos": []}
    assert "reaper" in v["loops"] and v["power"]["state"] == "up"
    assert v["credentials"]["counts"] == {}
    assert v["models"]["uncapped"] and v["models"]["job_default"] == "frontier"  # --- integ3 (review #105)
    # the CLI prints the real thing
    from typer.testing import CliRunner

    from hangar import cli

    called = []
    monkeypatch.setattr(cli, "_call", lambda *a, **kw: called.append((a, kw)) or json.loads(json.dumps(v)))
    out = CliRunner().invoke(cli.app, ["status", "--section", "slo,failures"])
    assert out.exit_code == 0, out.output
    assert "missing_tool" in out.output and "registry.npmjs.org" in out.output and "create     warm" in out.output
    assert called[0][1]["params"] == {"check_credentials": "false", "section": "slo,failures"}
    out = CliRunner().invoke(cli.app, ["status"])  # --- integ3: the uncapped `frontier` default is shown
    assert "models: jobs default to frontier" in out.output and "NO job budget or daily cap" in out.output
    only = await opstatus.status(fleet, sessions, sections={"slo"})
    assert set(only) == {"at", "version", "slo"}
    ov = fleet.overview()
    assert ov["failures_24h"] == {"missing_tool": 1, "model_api": 1} or ov["failures_24h"] == {"model_api": 1,
                                                                                               "missing_tool": 1}


async def test_the_resource_is_for_admins(fleet, sessions):  # noqa: F811
    from hangar.mcp_server import build_mcp

    mcp = build_mcp(fleet, None, sessions)
    tok = identity.current.set(MEMBER)
    try:
        async with Client(mcp) as c:
            assert "hangar://status" in {str(r.uri) for r in (await c.list_resources()).resources}
            with pytest.raises(Exception, match="admin"):
                await c.read_resource("hangar://status")
    finally:
        identity.current.reset(tok)
    async with Client(mcp) as c:
        got = json.loads((await c.read_resource("hangar://status")).contents[0].text)
    assert "failures" in got and "slo" in got


def _app(settings):
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    return create_app(settings)


def test_rest_status_is_admin_only_and_the_ops_loop_keeps_time(settings):
    app = _app(settings)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer t0ken"
        h = app.state.hangar
        h.fleet.providers["docker"] = FakeProvider()
        r = client.get("/api/status", params={"section": "loops,slo"})
        assert r.status_code == 200 and set(r.json()) == {"at", "version", "loops", "slo"}, r.text
        assert "ops" in r.json()["loops"]
        tok = client.post("/api/tokens", json={"name": "laptop", "scopes": ["mcp"]}).json()["token"]
        assert client.get("/api/status", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
        assert client.get("/api/records").json()["records"] == []
        assert client.post("/api/credentials/check", json={}).json()["results"] == []
        assert "ops" in metrics.TICKS


async def test_the_nightly_pass_runs_once_per_slot(settings, store, fleet, monkeypatch):
    h = type("H", (), {"settings": settings, "store": store, "fleet": fleet, "sessions": None})()
    settings.doctor_schedule = "30 0 * * *"
    settings.doctor_time_zone = "America/New_York"
    ops = opstatus.Ops(h)
    t = now()
    assert ops.due(t) is None  # the first look only remembers the latest slot
    assert ops.due(t + 3600) is None
    assert ops.due(t + 86400 + 60) is not None  # a day later a new slot has passed
    assert ops.due(t + 86400 + 120) is None
    settings.doctor_schedule = "off"
    assert ops.due(t + 3 * 86400) is None

    ran = []

    async def boom(h, by):
        raise RuntimeError("step broke")

    async def fine(h, by):
        ran.append(by)
        return {"ok": True}

    monkeypatch.setattr(opstatus, "NIGHTLY", [("boom", boom), ("fine", fine)])
    rec = await ops.nightly("test")
    assert ran == ["test"] and rec["steps"]["fine"] == {"ok": True} and "step broke" in rec["steps"]["boom"]["error"]
    assert rec["steps"]["credentials"] == {"checked": 0}
    assert store.kv_get_json(opstatus.NIGHTLY_KEY)["by"] == "test"


def test_the_ring_masks_token_formats_hangar_never_stored(broker):
    """--- review: a token hangar minted or saw but never stored (the bootstrap admin token it logs on first boot, a
    GitHub App installation token in a git error) isn't in broker.redact's list; its format still gives it away."""
    opstatus.install_ring()
    admin = "hgr_" + "A1b2C3d4" * 6
    install = "ghs_" + "Z9y8X7w6" * 5
    logging.getLogger("hangar").warning("    %s", admin)
    logging.getLogger("hangar.gateway").warning("clone of https://x-access-token:%s@github.com/o/r failed", install)
    text = json.dumps(opstatus.recent_log(broker.redact))
    assert admin not in text and install not in text
