"""The git gateway end to end: a real git client in a (fake) sandbox pushes through the gateway, served by uvicorn on
a free port, to a local upstream served by `git http-backend` that only accepts the upstream PAT."""

from __future__ import annotations

import base64
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import uvicorn

from hangar.models import CredentialKind, SandboxGrant
from tests.conftest import scratch_root

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

PAT = "ghp_upstreamSECRETvalue1234567890"


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd or "/nonexistent"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


class Upstream:
    """`git http-backend` behind a tiny HTTP server: the stand-in for github.com."""

    def __init__(self, root: Path):
        self.root = root
        self.requests: list[tuple[str, str]] = []
        up = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _body(self) -> bytes:
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    data = b""
                    while True:
                        n = int(self.rfile.readline().split(b";")[0], 16)
                        if n == 0:
                            self.rfile.readline()
                            return data
                        data += self.rfile.read(n)
                        self.rfile.readline()
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _serve(self) -> None:
                body = self._body() if self.command == "POST" else b""
                up.requests.append((self.command, self.path))
                want = "Basic " + base64.b64encode(f"x-access-token:{PAT}".encode()).decode()
                if self.headers.get("Authorization") != want:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                path, _, query = self.path.partition("?")
                env = {"PATH": "/usr/bin:/bin", "GIT_PROJECT_ROOT": str(up.root), "GIT_HTTP_EXPORT_ALL": "1",
                       "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(up.root), "PATH_INFO": path, "QUERY_STRING": query,
                       "REQUEST_METHOD": self.command, "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                       "CONTENT_LENGTH": str(len(body)), "REMOTE_USER": "agent", "REMOTE_ADDR": "127.0.0.1",
                       "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                       "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", "")}
                p = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
                sep = b"\r\n\r\n" if b"\r\n\r\n" in p.stdout else b"\n\n"
                head, _, out = p.stdout.partition(sep)
                status = 200
                headers = []
                for line in head.decode().splitlines():
                    k, _, v = line.partition(":")
                    if k.lower() == "status":
                        status = int(v.split()[0])
                    elif k:
                        headers.append((k, v.strip()))
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = _serve
            do_POST = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def refs(self) -> str:
        return _git("--git-dir", str(self.root / "acme/widgets.git"), "for-each-ref", "--format=%(refname)")

    def stop(self) -> None:
        self.httpd.shutdown()


class GatewayThread:
    """The gateway app on its own uvicorn server and event loop: the fake provider runs git synchronously."""

    def __init__(self, app):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def upstream():
    root = Path(tempfile.mkdtemp(prefix="upstream-", dir=scratch_root()))
    work = root / "seed"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "README").write_text("hi\n")
    _git("add", "README", cwd=work)
    _git("commit", "-q", "-m", "init", cwd=work)
    (root / "acme").mkdir()
    _git("clone", "-q", "--bare", str(work), str(root / "acme/widgets.git"))
    _git("--git-dir", str(root / "acme/widgets.git"), "branch", "keep", "main")
    up = Upstream(root)
    yield up
    up.stop()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def gw(fleet, settings, broker, upstream, tmp_path):
    server = GatewayThread(fleet.gateway.asgi_app())
    settings.gateway_url = f"http://127.0.0.1:{server.port}"
    settings.gateway_upstreams = {"github.com": f"http://127.0.0.1:{upstream.port}"}
    broker.put("gh-pat", "git", CredentialKind.GIT_TOKEN, PAT)
    fleet.catalog.add({"name": "gw", "harnesses": ["git"], "credentials": ["gh-pat"], "credential_mode": "gateway",
                       "repo": {"url": "https://github.com/acme/widgets.git", "branch": "main"}}, save_dir=tmp_path)
    fleet.catalog.add({"name": "auto", "harnesses": ["git"], "credentials": ["gh-pat"],
                       "repo": {"url": "https://github.com/acme/widgets.git", "branch": "main"}}, save_dir=tmp_path)
    yield server
    server.stop()


def _box(fleet, sb) -> Path:
    return fleet.providers["docker"].boxes[sb.provider_ref]


def _files_containing(root: Path, needle: str) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                if needle.encode() in p.read_bytes():
                    hits.append(str(p.relative_to(root)))
            except OSError:
                pass
    return hits


async def test_push_through_gateway(fleet, gw, upstream, store):
    sb = await fleet.create("gw", work_branch="hangar/job-1")
    assert sb.info["git"]["mode"] == "gateway"
    assert sb.info["git"]["push_refs"][0] == "refs/heads/hangar/job-1"
    box = _box(fleet, sb)

    # The clone went through the gateway; the sandbox holds a grant, never the PAT.
    assert (box / "workspace/widgets/README").read_text() == "hi\n"
    assert _files_containing(box, PAT) == []
    assert "GH_TOKEN" not in (box / "home/agent/.hangar/env").read_text()
    creds = (box / "home/agent/.git-credentials").read_text()
    assert "hgs_" in creds and "ghp_" not in creds
    token = creds.split("x-access-token:")[1].split("@")[0]

    # A push to the job branch succeeds (with a pack bigger than http.postBuffer: probe + chunked upload).
    res = await fleet.exec(sb.id, "head -c 300000 /dev/urandom > blob && git add blob && git commit -qm work && "
                                  "git -c http.postBuffer=65536 push origin HEAD:refs/heads/hangar/job-1")
    assert res.exit_code == 0, res.stderr
    assert "refs/heads/hangar/job-1" in upstream.refs()

    # New branches under hangar/ are allowed by the default pattern, and become the sandbox's own.
    res = await fleet.exec(sb.id, "git push -q origin HEAD:refs/heads/hangar/scratch")
    assert res.exit_code == 0, res.stderr
    res = await fleet.exec(sb.id, "git commit -q --allow-empty -m more && "
                                  "git push -q origin HEAD:refs/heads/hangar/scratch")
    assert res.exit_code == 0, res.stderr
    assert "refs/heads/hangar/scratch" in fleet.gateway.grants(sb.id)[0].push_refs

    # main is rejected with a message git shows, and nothing reaches the upstream's receive-pack.
    posts = sum(1 for m, p in upstream.requests if m == "POST" and p.endswith("git-receive-pack"))
    res = await fleet.exec(sb.id, "git push origin HEAD:main")
    assert res.exit_code != 0
    assert "[remote rejected]" in res.stderr and "hangar: this sandbox may push only to hangar/job-1" in res.stderr
    assert "main" in res.stderr
    # deletes and tags too
    res = await fleet.exec(sb.id, "git push origin :refs/heads/keep")
    assert res.exit_code != 0 and "hangar: deleting refs is not allowed" in res.stderr
    res = await fleet.exec(sb.id, "git tag v1 && git push origin refs/tags/v1")
    assert res.exit_code != 0 and "hangar: pushing tags is not allowed" in res.stderr
    assert posts == sum(1 for m, p in upstream.requests if m == "POST" and p.endswith("git-receive-pack"))
    refs = upstream.refs()
    assert "refs/heads/keep" in refs and "refs/tags/v1" not in refs
    main_sha = _git("--git-dir", str(upstream.root / "acme/widgets.git"), "rev-parse", "main").strip()
    first_sha = _git("--git-dir", str(upstream.root / "acme/widgets.git"), "rev-list", "--max-parents=0",
                     "main").strip()
    assert main_sha == first_sha

    # Fetching works for the granted repo, and not for another one.
    res = await fleet.exec(sb.id, "git fetch -q origin keep")
    assert res.exit_code == 0, res.stderr
    res = await fleet.exec(sb.id, "git ls-remote https://github.com/acme/other.git")
    assert res.exit_code != 0

    # Audit: pushes and denials with refs; no secrets anywhere in the log.
    events = store.audit_tail(200)
    actions = [e.action for e in events]
    assert "gw.git.push" in actions and "gw.git.deny" in actions and "gateway.grant" in actions
    push = next(e for e in events if e.action == "gw.git.push" and e.detail["refs"]
                and e.detail["refs"][0]["ref"] == "refs/heads/hangar/job-1")
    assert push.detail["result"] == "ok" and "ok refs/heads/hangar/job-1" in push.detail["report"]
    dump = " ".join(e.model_dump_json() for e in events)
    assert PAT not in dump and token not in dump

    # Destroy: the grant stops working at once.
    url = f"{fleet.settings.gateway_url}/git/github.com/acme/widgets.git/info/refs?service=git-upload-pack"
    auth = ("x-access-token", token)
    async with httpx.AsyncClient() as c:
        assert (await c.get(url, auth=auth)).status_code == 200
        await fleet.destroy(sb.id)
        assert store.list(SandboxGrant) == []
        r = await c.get(url, auth=auth)
        assert r.status_code == 401 and "hangar" in r.text


async def test_job_and_session_sandboxes_default_to_gateway(fleet, gw, upstream):
    # A session's sandbox (jobs run in sessions) uses the gateway when it is configured...
    sb = await fleet.create("auto", labels={"hangar.session": "ses-1"}, push_branch="hangar/session/one")
    assert sb.info["git"]["mode"] == "gateway"
    assert sb.info["git"]["push_refs"][0] == "refs/heads/hangar/session/one"
    assert _files_containing(_box(fleet, sb), PAT) == []
    res = await fleet.exec(sb.id, "git checkout -qb hangar/session/one && git commit -q --allow-empty -m s && "
                                  "git push -q origin HEAD:refs/heads/hangar/session/one")
    assert res.exit_code == 0, res.stderr

    # ...and another sandbox can't push to that session's branch through the hangar/** pattern.
    other = await fleet.create("auto", labels={"hangar.session": "ses-2"}, push_branch="hangar/session/two")
    res = await fleet.exec(other.id, "git commit -q --allow-empty -m x && "
                                     "git push --force origin HEAD:refs/heads/hangar/session/one")
    assert res.exit_code != 0 and "belongs to another sandbox" in res.stderr
    # Once the first sandbox is gone its pushed branch is still safe: the pattern only creates branches.
    await fleet.destroy(sb.id)
    res = await fleet.exec(other.id, "git push --force origin HEAD:refs/heads/hangar/session/one")
    assert res.exit_code != 0 and "not update existing ones" in res.stderr

    # A plain sandbox of the same environment keeps the old direct mode. (No clone and no PAT here: direct mode
    # would talk to github.com; the direct path itself is covered by test_broker/test_fleet.)
    plain = await fleet.create("auto", credentials=[], _skip_repo=True)
    assert plain.info["git"]["mode"] == "direct"
    assert fleet.gateway.grants(plain.id) == []


async def test_fork_gets_its_own_grant(fleet, gw, upstream):
    sb = await fleet.create("gw", work_branch="hangar/job-2")
    parent_token = (_box(fleet, sb) / "home/agent/.git-credentials").read_text()
    kids = await fleet.fork(sb.id, count=1, work_branch_prefix="hangar/fork")
    kid = kids[0]
    assert kid.info["git"]["mode"] == "gateway"
    assert kid.info["git"]["push_refs"][0] == "refs/heads/hangar/fork-1"
    kid_token = (_box(fleet, kid) / "home/agent/.git-credentials").read_text()
    assert kid_token != parent_token and "hgs_" in kid_token
    res = await fleet.exec(kid.id, "git commit -q --allow-empty -m k && "
                                   "git push --force origin HEAD:refs/heads/hangar/job-2")
    assert res.exit_code != 0 and "belongs to another sandbox" in res.stderr
    res = await fleet.exec(kid.id, "git push -q origin HEAD:refs/heads/hangar/fork-1")
    assert res.exit_code == 0, res.stderr
    assert {g.sandbox_id for g in fleet.gateway.grants()} == {sb.id, kid.id}


async def test_gateway_mode_without_url_fails_closed(fleet, settings, broker, tmp_path):
    broker.put("gh-pat", "git", CredentialKind.GIT_TOKEN, PAT)
    fleet.catalog.add({"name": "gw2", "credentials": ["gh-pat"], "credential_mode": "gateway"}, save_dir=tmp_path)
    with pytest.raises(Exception, match="HANGAR_GATEWAY_URL"):
        await fleet.create("gw2")
    assert fleet.gateway.grants() == []
