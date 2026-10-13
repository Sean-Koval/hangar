"""Minimal GitHub REST client used by the control plane (spec 0009): PRs, issue comments, webhooks.

The control plane holds its own token (a vault credential named by HANGAR_GITHUB_TOKEN_CREDENTIAL);
sandboxes never see it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

import httpx

MARKER = "<!-- hangar -->"  # on every comment hangar writes, so its own comments never trigger work

_REPO_RES = [
    re.compile(r"^https?://(?:[^@/]+@)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:([\w.-]+)/([\w.-]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?$"),
]


def parse_repo(url: str | None) -> str | None:
    """'owner/repo' for a GitHub clone URL, else None."""
    if not url:
        return None
    for rx in _REPO_RES:
        m = rx.match(url.strip())
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return None


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[7:])


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str, api_url: str = "https://api.github.com",
                 transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=api_url.rstrip("/"), transport=transport, timeout=30,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hangar"})

    async def close(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kw) -> Any:
        r = await self._client.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise GitHubError(f"GitHub {method} {path}: {r.status_code} {msg}"[:500])
        return r.json() if r.content else None

    async def find_pr(self, repo: str, branch: str) -> dict[str, Any] | None:
        owner = repo.split("/")[0]
        prs = await self._req("GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"})
        return prs[0] if prs else None

    async def open_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> dict[str, Any]:
        return await self._req("POST", f"/repos/{repo}/pulls",
                               json={"head": head, "base": base, "title": title, "body": body, "draft": draft})

    async def default_branch(self, repo: str) -> str:
        return (await self._req("GET", f"/repos/{repo}"))["default_branch"]

    async def comment(self, repo: str, number: int, body: str) -> None:
        await self._req("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": f"{MARKER}\n{body}"})

    async def team_membership(self, org: str, team: str, login: str) -> dict[str, Any] | None:
        """`{"state": "active" | "pending", "role": ...}`, or None when the user isn't on the team (GitHub's 404,
        which it also returns when the token can't see the team: it needs the org's Members read permission)."""
        r = await self._client.get(f"/orgs/{org}/teams/{team}/memberships/{login}")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise GitHubError(f"GitHub GET team membership {org}/{team}: {r.status_code}")
        return r.json()

    async def issue_comments(self, repo: str, number: int, limit: int = 20) -> list[dict[str, Any]]:
        out = await self._req("GET", f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100})
        return [c for c in out if MARKER not in (c.get("body") or "")][-limit:]
