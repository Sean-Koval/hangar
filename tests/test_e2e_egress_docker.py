"""Egress allowlists end to end on a real Docker engine (--- egress).

A sandbox of an `allowlist` environment lands on hangar's internal `hangar-egress` network. It can't open a
connection to the internet itself, but through hangar's proxy (on the network's gateway address) it reaches an
allowed host and gets 403 for any other. Each outcome is in the audit log.

Opt-in (needs Docker on Linux, where hangar can listen on the network's gateway, and the internet):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_egress_docker.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")

ROOT = Path(__file__).parents[1]
FETCH = ("python3 -c \"import sys, urllib.request\n"
         "try:\n print(urllib.request.urlopen(sys.argv[1], timeout=20).status)\n"
         "except Exception as e:\n print(type(e).__name__, getattr(e, 'code', ''), str(e)[:200])\" ")


async def test_a_proxied_sandbox_reaches_only_its_allowlist_on_docker(tmp_path):
    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.models import AuditEvent
    from hangar.providers.docker_net import EGRESS_NAME

    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "locked.yaml").write_text(yaml.safe_dump({"name": "locked", "extends": "minimal",
                                                      "egress": {"mode": "allowlist", "presets": [],
                                                                 "allow": ["example.com"]}}))
    s = Settings(data_dir=tmp_path, env_dirs=[ROOT / "envs", envs], reaper_interval_seconds=3600,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    fleet = h.fleet
    await fleet.start()
    try:
        sb = await fleet.create("locked", name="e2e-egress", labels={"e2e-egress": "1"}, actor="test")
        sb = fleet.get(sb.id)
        prov = fleet.provider("docker")
        attrs = prov.client.api.inspect_container(sb.provider_ref)
        assert list(attrs["NetworkSettings"]["Networks"]) == [EGRESS_NAME]
        assert prov.client.api.inspect_network(EGRESS_NAME)["Internal"] is True
        assert sb.info["egress"]["mode"] == "allowlist" and sb.info["egress"]["enforced"]

        # no route out of the network: a direct connection fails
        r = await fleet.exec(sb.id, "timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' && echo open || echo shut")
        assert r.stdout.strip() == "shut", r.stdout + r.stderr
        # through the proxy (HTTPS_PROXY from ~/.hangar/env): the allowed host answers, another gets 403
        r = await fleet.exec(sb.id, FETCH + "https://example.com/", timeout=60)
        assert r.stdout.strip() == "200", r.stdout + r.stderr
        r = await fleet.exec(sb.id, FETCH + "https://www.iana.org/", timeout=60)
        assert "403" in r.stdout or "Forbidden" in r.stdout, r.stdout + r.stderr
        # and without the proxy's credential, nothing
        r = await fleet.exec(sb.id, "unset HTTPS_PROXY https_proxy; " + FETCH + "https://example.com/", timeout=60)
        assert r.stdout.strip() != "200", r.stdout

        fleet.egress.flush()
        events = [e for e in fleet.store.audit_tail(200) if isinstance(e, AuditEvent) and e.target == sb.id]
        assert any(e.action == "egress.allow" and e.detail["host"] == "example.com" for e in events)
        assert any(e.action == "egress.deny" and e.detail["host"] == "www.iana.org" for e in events)
    finally:
        await fleet.destroy_many(label="e2e-egress")
        await fleet.stop()
        await fleet.gateway.stop()


async def test_a_proxied_sandboxs_preview_port_is_reached_and_its_egress_stays_shut(tmp_path):
    """--- egress2 (#97 x #99): the preview proxy reaches a port of a sandbox on the internal egress network, at the
    address its hangar-envd proved, and nothing about that opens the sandbox's own way out."""
    import httpx

    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.preview import PreviewRouter

    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "locked-web.yaml").write_text(yaml.safe_dump({
        "name": "locked-web", "extends": "minimal", "ports": [{"port": 8000, "name": "web"}],
        "egress": {"mode": "allowlist", "presets": [], "allow": ["example.com"]}}))
    s = Settings(data_dir=tmp_path, env_dirs=[ROOT / "envs", envs], reaper_interval_seconds=3600,
                 admin_token="e2e-preview-admin-token-0123456789", preview_domain="localhost",
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    fleet = h.fleet
    await fleet.start()
    try:
        sb = await fleet.create("locked-web", name="e2e-egress-web", labels={"e2e-egress": "1"}, actor="test")
        r = await fleet.exec(sb.id, "mkdir -p /tmp/site && echo preview-ok > /tmp/site/index.html && "
                                    "(cd /tmp/site && nohup python3 -m http.server 8000 >/dev/null 2>&1 &) && sleep 1")
        assert r.exit_code == 0, r.stderr

        async def app(scope, receive, send):  # what the router passes on: nothing, here
            raise AssertionError(scope.get("path"))

        router = PreviewRouter(app, h)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=router), timeout=60,
                                     base_url=f"http://8000-{sb.id}.localhost",
                                     headers={"Authorization": f"Bearer {s.admin_token}"}) as c:
            resp = await c.get("/index.html")
        assert resp.status_code == 200 and resp.text.strip() == "preview-ok", resp.text
        # the sandbox's own way out is as shut as before
        r = await fleet.exec(sb.id, "timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' && echo open || echo shut")
        assert r.stdout.strip() == "shut", r.stdout + r.stderr
    finally:
        await fleet.destroy_many(label="e2e-egress")
        await fleet.stop()
        await fleet.gateway.stop()
