"""--- envbuilds: preview ports (preview.py): only a sandbox's declared ports, only for its owner (or an admin), a
two-minute link that signs the browser in to that one origin, and nothing of hangar's reaching the app."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request

from hangar import identity, people, preview
from hangar.auth import Principal
from hangar.models import Sandbox, SandboxState

from .conftest import free_port

DOMAIN = "preview.test"


class Echo(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def app_port():
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()


class Router(preview.PreviewRouter):
    async def _host(self, sb):  # the test's app listens on loopback; the real one only takes envd's proven address
        return "127.0.0.1"


TOKENS = {"tok-alice": Principal("alice", "t1", ["user"], person="alice", role="member"),
          "tok-bob": Principal("bob", "t2", ["user"], person="bob", role="member"),
          "tok-alice-jobs": Principal("alice-ci", "t4", ["jobs"], person="alice", role="member"),
          "tok-admin": Principal("root", "t3", ["admin"], person="root", role="admin")}


@pytest.fixture
def world(settings, store, fleet, app_port):
    settings.preview_domain = DOMAIN
    settings.public_url = "https://hangar.test"
    people.add(store, "alice")
    people.add(store, "bob")
    people.add(store, "root", role="admin")
    sb = Sandbox(name="s", environment="e", provider="docker", person="alice", state=SandboxState.RUNNING,
                 info={"services": [{"name": "web", "kind": "port", "port": app_port, "check": None}]})
    store.put(sb)
    auth = SimpleNamespace(principal=lambda raw: TOKENS.get(raw or ""))
    h = SimpleNamespace(settings=settings, fleet=fleet, store=store, auth=auth)

    async def member(request: Request) -> str:
        p = TOKENS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if p is None or not (p.admin or "user" in p.scopes):
            raise HTTPException(401)
        identity.current.set(p)
        return f"ui:{p.name}"

    api = FastAPI()
    preview.mount(api, h, Depends(member))
    app = Router(api, h)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hangar.test")
    return SimpleNamespace(sb=sb, client=client, port=app_port, h=h, store=store)


def host(w, port=None) -> str:
    return f"{port or w.port}-{w.sb.id}.{DOMAIN}"


async def links(w, token: str):
    return await w.client.get(f"/api/sandboxes/{w.sb.id}/previews", headers={"authorization": f"Bearer {token}"})


async def test_the_owner_opens_a_link_and_the_app_sees_nothing_of_hangar(world):
    w = world
    r = await links(w, "tok-alice")
    assert r.status_code == 200, r.text
    link = r.json()["previews"][0]
    assert link["origin"] == f"https://{host(w)}" and link["url"].startswith(f"https://{host(w)}/__hangar/preview?t=")
    path = link["url"].split(host(w), 1)[1]
    r = await w.client.get(path, headers={"host": host(w)})
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-hangar_preview=") and "HttpOnly" in cookie and "Secure" in cookie
    session = cookie.split(";")[0]
    r = await w.client.get("/app?x=1", headers={"host": host(w), "cookie": f"theme=dark; {session}",
                                               "authorization": "Bearer tok-alice"})
    assert r.status_code == 200, r.text
    seen = r.json()
    assert seen["path"] == "/app?x=1"
    assert "authorization" not in seen["headers"] and seen["headers"]["cookie"] == "theme=dark"
    assert any(e.action == "preview.link" for e in w.store.audit_tail(10))


async def test_a_link_opens_once(world):
    """Review: the ticket is in the link's URL (browser history, a proxy's or load balancer's access log). Opened a
    second time within its two minutes, it signs nobody in."""
    w = world
    link = (await links(w, "tok-alice")).json()["previews"][0]
    path = link["url"].split(host(w), 1)[1]
    assert (await w.client.get(path, headers={"host": host(w)})).status_code == 303
    r = await w.client.get(path, headers={"host": host(w)})
    assert r.status_code == 401 and "set-cookie" not in r.headers
    again = (await links(w, "tok-alice")).json()["previews"][0]  # a new link, even within the same second
    assert again["url"] != link["url"]
    assert (await w.client.get(again["url"].split(host(w), 1)[1], headers={"host": host(w)})).status_code == 303


async def test_without_a_session_or_with_someone_elses_it_is_refused(world):
    w = world
    assert (await w.client.get("/", headers={"host": host(w)})).status_code == 401
    assert (await links(w, "tok-bob")).status_code == 404  # not hers to see: like a missing sandbox
    r = await w.client.get("/", headers={"host": host(w), "authorization": "Bearer tok-bob"})
    assert r.status_code == 401
    r = await w.client.get("/", headers={"host": host(w), "authorization": "Bearer tok-alice-jobs"})
    assert r.status_code == 401  # a service token of hers isn't her browser
    forged = preview.Previews(w.h).token("s", w.sb.id, w.port, "alice", 3600).rsplit(".", 1)[0] + ".00"
    r = await w.client.get("/", headers={"host": host(w), "cookie": f"__Host-hangar_preview={forged}"})
    assert r.status_code == 401
    assert any(e.action == "preview.denied" for e in w.store.audit_tail(10))


async def test_a_session_is_for_one_port_of_one_sandbox_and_tickets_dont_open_it(world):
    w = world
    p = preview.Previews(w.h)
    other = p.token("s", "sb-other", w.port, "alice", 3600)
    r = await w.client.get("/", headers={"host": host(w), "cookie": f"__Host-hangar_preview={other}"})
    assert r.status_code == 401
    ticket = p.token("t", w.sb.id, w.port, "alice", 60)  # a ticket is only good for the handshake
    r = await w.client.get("/", headers={"host": host(w), "cookie": f"__Host-hangar_preview={ticket}"})
    assert r.status_code == 401
    expired = p.token("s", w.sb.id, w.port, "alice", -1)
    r = await w.client.get("/", headers={"host": host(w), "cookie": f"__Host-hangar_preview={expired}"})
    assert r.status_code == 401
    r = await w.client.get("/__hangar/preview?t=" + p.token("t", w.sb.id, w.port, "alice", -1),
                           headers={"host": host(w)})
    assert r.status_code == 401


async def test_only_declared_ports_and_never_envd(world):
    w = world
    for port in (w.port + 1, 49983):
        r = await w.client.get("/", headers={"host": host(w, port), "authorization": "Bearer tok-alice"})
        assert r.status_code == 404, port
        r = await w.client.get("/", headers={"host": host(w, port)})
        assert r.status_code == 401, port  # nobody else learns which ports (or sandboxes) exist
    r = await w.client.get("/", headers={"host": f"{w.port}-sb-00000000.{DOMAIN}"})
    assert r.status_code == 401


async def test_an_admin_may_and_a_change_of_owner_ends_the_session(world):
    w = world
    r = await w.client.get("/", headers={"host": host(w), "authorization": "Bearer tok-admin"})
    assert r.status_code == 200
    session = preview.Previews(w.h).token("s", w.sb.id, w.port, "alice", 3600)
    ok = await w.client.get("/", headers={"host": host(w), "cookie": f"__Host-hangar_preview={session}"})
    assert ok.status_code == 200
    w.sb.person = "bob"
    w.store.put(w.sb)
    r = await w.client.get("/", headers={"host": host(w), "cookie": f"__Host-hangar_preview={session}"})
    assert r.status_code == 401


async def test_a_stopped_sandbox_and_other_hosts(world):
    w = world
    r = await w.client.get("/api/sandboxes/nope/previews", headers={"authorization": "Bearer tok-alice"})
    assert r.status_code == 404
    w.sb.state = SandboxState.STOPPED
    w.store.put(w.sb)
    r = await w.client.get("/", headers={"host": host(w), "authorization": "Bearer tok-alice"})
    assert r.status_code == 502 and "no such preview" in r.text  # as E2B's sandbox URLs answer for a gone sandbox
    r = await w.client.get("/", headers={"host": f"x.y.{DOMAIN}"})
    assert r.status_code == 404
    r = await w.client.get(f"/api/sandboxes/{w.sb.id}/previews", headers={"host": "hangar.test",
                                                                         "authorization": "Bearer tok-alice"})
    assert r.status_code == 200  # hangar's own host goes on to the app


async def test_previews_off_without_a_domain(world):
    world.h.settings.preview_domain = None
    r = await links(world, "tok-alice")
    assert r.status_code == 400 and "HANGAR_PREVIEW_DOMAIN" in r.json()["detail"]


def test_http_previews_use_a_plain_cookie(settings, store, fleet):
    settings.preview_domain, settings.public_url = "localhost", "http://localhost:8787"
    h = SimpleNamespace(settings=settings, fleet=fleet, store=store, auth=None)
    r = preview.PreviewRouter(FastAPI(), h)
    assert r.cookie == "hangar_preview" and not r.secure
    sb = Sandbox(name="s", environment="e", provider="docker")
    assert preview.Previews(h).origin(sb, 3000) == f"http://3000-{sb.id}.localhost:8787"
