"""A read-only GitHub API for sandboxes in gateway mode (--- egress2; docs/security.md#git-credential-gateway).

GitHub isn't an allowed host for proxied sandboxes any more (the `packages` preset dropped it): with anyone's token it
takes writes, so it is a way out. An agent still needs to read its PR, its review comments and its CI, and `gh` with
a token would be a write path too. So the git gateway serves `GET /gh/api/repos/<owner>/<repo>/...` with the same
grant that pushes the sandbox's branch:

* only the grant's own repository, only GET and HEAD, and only paths under `/repos/<owner>/<repo>`;
* upstream with the gateway's credential: a GitHub App installation token asking for read permissions only, or the
  PAT (whose writes the method rule keeps out);
* redirects (CI logs, release assets, archives) are followed here, only to GitHub's own content hosts and Azure blob
  storage where Actions keeps logs, without the token.

In the sandbox: `curl -sn "$HANGAR_GITHUB_API/repos/OWNER/REPO/pulls"` (~/.netrc holds the grant for the gateway's
host). `gh` can't use it: it talks to an Enterprise host over https only, so in gateway mode `gh` has no token and no
host; hangar opens and comments on PRs for jobs itself.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from hangar.models import CredentialKind, Sandbox

if TYPE_CHECKING:
    from hangar.gateway import Gateway

log = logging.getLogger("hangar.gateway")

READ = {"metadata": "read", "contents": "read", "pull_requests": "read", "issues": "read", "checks": "read",
        "actions": "read", "statuses": "read"}
_REPO_PATH = re.compile(r"^repos/(?P<owner>[A-Za-z0-9_.-]{1,100})/(?P<repo>[A-Za-z0-9_.-]{1,100})(?:/(?P<rest>.*))?$")
_SAFE = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
# Where GitHub's API redirects reads: archives, release assets, raw content, Actions logs and artifacts.
_REDIRECT_HOSTS = ("codeload.github.com", ".githubusercontent.com", ".blob.core.windows.net")
_PASS = ("content-type", "etag", "last-modified", "link", "x-ratelimit-limit", "x-ratelimit-remaining",
         "x-ratelimit-reset", "x-github-request-id")


def sandbox_env(settings: Any, sb: Sandbox) -> dict[str, str]:
    """HANGAR_GITHUB_API for a gateway-mode sandbox with a grant."""
    git = sb.info.get("git") or {}
    if git.get("mode") != "gateway" or not git.get("repo") or not settings.gateway_url:
        return {}
    return {"HANGAR_GITHUB_API": settings.gateway_url.rstrip("/") + "/gh/api"}


def netrc(current: str, host: str, token: str) -> str:
    """~/.netrc with one line for the gateway's host (the grant), the rest kept."""
    kept = [x for x in current.splitlines() if not x.strip().startswith(f"machine {host} ")]
    return "\n".join([*kept, f"machine {host} login x-access-token password {token}"]) + "\n"


class GitHubRead:
    def __init__(self, gw: Gateway):
        from hangar.gateway import AppTokens

        self.gw = gw
        self.tokens = AppTokens(gw.settings.github_api_url, gw._http, permissions=READ)

    def routes(self) -> list[Route]:
        return [Route("/gh/api/{rest:path}", self.handle, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])]

    def api_base(self, host: str) -> str:
        """The REST API of a git host the gateway serves: api.github.com, or /api/v3 on GitHub Enterprise Server."""
        if host == "github.com":
            return self.gw.settings.github_api_url.rstrip("/")
        return (self.gw.settings.gateway_upstreams.get(host) or f"https://{host}").rstrip("/") + "/api/v3"

    async def handle(self, request: Request) -> Response:
        gw = self.gw
        src = request.client.host if request.client else "unknown"
        token = gw._client_token(request)
        if token is not None and gw._failures.blocked(src):
            return PlainTextResponse("hangar: too many requests with an invalid grant from this address\n", 429)
        grant = gw.verify(token)
        if grant is None:
            if token is not None and gw._failures.add(src):
                gw.fleet.audit("gateway", "gw.auth.blocked", None, source=src)
            return PlainTextResponse("hangar: a valid sandbox grant is required (curl -n uses ~/.netrc)\n", 401,
                                     headers={"WWW-Authenticate": 'Basic realm="hangar github api"'})
        raw = (request.scope.get("raw_path") or b"").decode("latin-1").split("?", 1)[0]
        rest = raw.split("/gh/api/", 1)[1] if "/gh/api/" in raw else ""
        m = _REPO_PATH.match(rest)
        repo = f"{m['owner']}/{m['repo']}".lower() if m else ""
        if not m or repo not in grant.repos or not _SAFE.match(rest) or ".." in rest.split("/"):
            gw._deny(grant, rest[:200], "github api: not this sandbox's repository")
            return PlainTextResponse("hangar: only /repos/<this sandbox's repository>/... is readable here\n", 404)
        if request.method not in ("GET", "HEAD"):
            gw._deny(grant, repo, f"github api: {request.method} refused")
            return PlainTextResponse("hangar: the GitHub API through the gateway is read-only\n", 405,
                                     headers={"Allow": "GET, HEAD"})
        try:
            auth = await self._token(grant, repo)
        except Exception as e:  # noqa: BLE001 - never a secret in the message
            return PlainTextResponse(f"hangar: no GitHub credential for {repo} ({type(e).__name__})\n", 502)
        url = f"{self.api_base(grant.host)}/{rest}" + (f"?{request.url.query}" if request.url.query else "")
        headers = {"Authorization": f"Bearer {auth}", "User-Agent": "hangar-gateway",
                   "Accept": request.headers.get("accept", "application/vnd.github+json")[:200],
                   "X-GitHub-Api-Version": request.headers.get("x-github-api-version", "2022-11-28")[:20]}
        client = gw._http()
        try:
            resp = await client.send(client.build_request(request.method, url, headers=headers), stream=True)
            for _ in range(3):
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                await resp.aclose()
                nxt = str(resp.url.join(resp.headers.get("location", "")))
                u = urlsplit(nxt)
                same = u.hostname == urlsplit(url).hostname
                if u.scheme != "https" or not (same or (u.hostname or "").endswith(_REDIRECT_HOSTS)):
                    return PlainTextResponse("hangar: GitHub redirected off its own hosts\n", 502)
                h = headers if same else {"User-Agent": "hangar-gateway"}  # signed URLs: no token
                resp = await client.send(client.build_request(request.method, nxt, headers=h), stream=True)
        except Exception as e:  # noqa: BLE001
            log.warning("gateway: github api %s failed: %s", rest[:100], type(e).__name__)
            return PlainTextResponse(f"hangar: GitHub didn't answer ({type(e).__name__})\n", 502)
        public = f"{request.url.scheme}://{request.headers.get('host', '')}/gh/api"
        out = {k: v for k, v in resp.headers.items() if k.lower() in _PASS}
        if "link" in out:  # pagination links point back here
            out["link"] = out["link"].replace(self.api_base(grant.host), public)

        async def body() -> Any:
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(body(), status_code=resp.status_code, headers=out)

    async def _token(self, grant: Any, repo: str) -> str:
        """An installation token with read permissions only, or the PAT. An App without some of them (no Actions
        access, say) gets the push token instead: the gateway lets only reads through either way."""
        from hangar.gateway import GatewayError

        cred = self.gw.broker.get(grant.credential)
        if cred.kind == CredentialKind.GITHUB_APP:
            self.gw._http()
            try:
                return await self.tokens.token(self.gw.broker.secret_text(cred.name), repo)
            except GatewayError:
                pass
        return await self.gw.upstream_token(grant, repo)
