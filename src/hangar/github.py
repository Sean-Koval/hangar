"""Minimal GitHub REST client used by the control plane (spec 0009): PRs, issue comments, webhooks.

The control plane holds its own token (a vault credential named by HANGAR_GITHUB_TOKEN_CREDENTIAL);
sandboxes never see it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Any
from urllib.parse import quote

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


_SHORT_RE = re.compile(r"^(?:(?:www\.)?github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.IGNORECASE)
_SCP_RE = re.compile(r"^[\w.-]+@[\w.-]+:\S+$")  # git@host:path
REPO_FORMS = ("owner/name, github.com/owner/name, a clone URL (https://github.com/owner/name.git) "
              "or an ssh address (git@github.com:owner/name.git)")


def clone_url(repo: str | None) -> str | None:
    """The clone URL a job's `repo` stands for (KAN-136): `owner/name` and `github.com/owner/name` become
    https://github.com/owner/name.git; a URL (any scheme), an ssh address or an absolute path is kept as given.
    None (or blank) stays None. Raises ValueError, naming the accepted forms, for anything else."""
    if repo is None or not repo.strip():
        return None
    r = repo.strip()
    if "://" in r or _SCP_RE.match(r) or r.startswith("/"):
        return r
    m = _SHORT_RE.match(r)
    if m and m.group(1) not in (".", "..") and m.group(2) not in (".", ".."):
        return f"https://github.com/{m.group(1)}/{m.group(2)}.git"
    raise ValueError(f"repo '{r[:200]}' is not a repo hangar can clone; use {REPO_FORMS}")


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Bytes, not str: compare_digest raises on non-ASCII text, which made a junk header a 500 (front-door-auth).
    return hmac.compare_digest(want.encode(), header[7:].encode("utf-8", "replace"))


class GitHubError(RuntimeError):
    pass


def _iso_ts(iso: str) -> float:
    """An ISO 8601 time GitHub gives, as Unix seconds (0 when it isn't one)."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


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

    async def add_labels(self, repo: str, number: int, labels: list[str]) -> Any:
        """--- taint (#130): label an issue or PR (Issues: write, or Pull requests: write for a PR)."""
        return await self._req("POST", f"/repos/{repo}/issues/{int(number)}/labels", json={"labels": labels})

    async def default_branch(self, repo: str) -> str:
        return (await self._req("GET", f"/repos/{repo}"))["default_branch"]

    # --- review(clouddoctor): the Cloud Doctor judges its pushed fix from GitHub (clouddoctor.after_push)
    async def branch_sha(self, repo: str, branch: str) -> str:
        return (await self._req("GET", f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}"))["object"]["sha"]

    async def compare(self, repo: str, base: str, head: str) -> dict[str, Any]:
        return await self._req("GET", f"/repos/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}")

    async def file_at(self, repo: str, path: str, ref: str) -> str | None:
        """A file's text at `ref`; None when it isn't there; `<symlink>` and the like for what isn't a file."""
        r = await self._client.get(f"/repos/{repo}/contents/{quote(path)}", params={"ref": ref})
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise GitHubError(f"GitHub GET contents of {path}: {r.status_code}"[:500])
        d = r.json()
        if not isinstance(d, dict) or d.get("type") != "file" or d.get("encoding") != "base64":
            return f"<{d.get('type') if isinstance(d, dict) else 'directory'}>"
        return base64.b64decode(d.get("content") or "").decode("utf-8", "replace")

    async def delete_branch(self, repo: str, branch: str) -> None:
        await self._req("DELETE", f"/repos/{repo}/git/refs/heads/{quote(branch, safe='/')}")

    async def comment(self, repo: str, number: int, body: str) -> dict[str, Any] | None:
        return await self._req("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": f"{MARKER}\n{body}"})

    async def edit_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any] | None:  # --- landing
        return await self._req("PATCH", f"/repos/{repo}/issues/comments/{int(comment_id)}",
                               json={"body": f"{MARKER}\n{body}"})

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """--- landing: a GraphQL call (auto-merge has no REST route). GitHub answers 200 with `errors` for a refused
        mutation; those raise like a REST error. Enterprise Server serves it at /api/graphql, next to /api/v3."""
        base = str(self._client.base_url).rstrip("/")
        url = base[: -len("/v3")] + "/graphql" if base.endswith("/api/v3") else base + "/graphql"
        out = await self._req("POST", url, json={"query": query, "variables": variables}) or {}
        if errs := out.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in errs if isinstance(e, dict)) or str(errs)
            raise GitHubError(f"GitHub GraphQL: {msg}"[:500])
        return out.get("data") or {}

    async def team_membership(self, org: str, team: str, login: str) -> dict[str, Any] | None:
        """`{"state": "active" | "pending", "role": ...}`, or None when the user isn't on the team (GitHub's 404,
        which it also returns when the token can't see the team: it needs the org's Members read permission)."""
        r = await self._client.get(f"/orgs/{org}/teams/{team}/memberships/{login}")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise GitHubError(f"GitHub GET team membership {org}/{team}: {r.status_code}")
        return r.json()

    async def hook_deliveries(self, repo: str, hook_id: int, cursor: str | None = None,
                              per_page: int = 100, org: bool = False) -> tuple[list[dict[str, Any]], str | None]:
        """--- reconciler: one page of a repo webhook's recent deliveries, newest first, and the cursor of the next
        page (GitHub pages these by a cursor in the Link header, not by page number). --- trust: `org`: `repo` is an
        organization and the hook is its own (GET /orgs/{org}/hooks/{hook_id}/deliveries, the same paging)."""
        params: dict[str, Any] = {"per_page": per_page}
        if cursor:
            params["cursor"] = cursor
        path = f"/{'orgs' if org else 'repos'}/{repo}/hooks/{int(hook_id)}/deliveries"
        r = await self._client.get(path, params=params)
        if r.status_code >= 400:
            raise GitHubError(f"GitHub GET {path}: {r.status_code}"[:500])
        nxt = (r.links.get("next") or {}).get("url")
        return r.json() or [], (httpx.URL(nxt).params.get("cursor") if nxt else None)

    async def redeliver(self, repo: str, hook_id: int, delivery_id: int, org: bool = False) -> None:  # --- reconciler
        await self._req("POST", f"/{'orgs' if org else 'repos'}/{repo}/hooks/{int(hook_id)}/deliveries/"
                                f"{int(delivery_id)}/attempts")

    async def merged_pulls(self, repo: str, since: float, pages: int = 10) -> list[dict[str, Any]]:
        """--- #168: PRs merged since `since` (Unix seconds), anyone's, newest updated first, paged (100 a page)."""
        out: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            got = await self._req("GET", f"/repos/{repo}/pulls", params={
                "state": "closed", "sort": "updated", "direction": "desc", "per_page": 100, "page": page})
            if not isinstance(got, list) or not got:
                break
            out += [p for p in got if isinstance(p, dict) and p.get("merged_at")]
            last = got[-1].get("updated_at") if isinstance(got[-1], dict) else None
            if len(got) < 100 or (isinstance(last, str) and _iso_ts(last) < since):
                break
        return [p for p in out if _iso_ts(p["merged_at"]) >= since]

    async def commits_since(self, repo: str, since: float, branch: str | None = None,
                            pages: int = 10) -> list[dict[str, Any]]:
        """--- #168: commits on `branch` (the default branch) since `since`, paged."""
        from datetime import UTC, datetime

        params: dict[str, Any] = {"since": datetime.fromtimestamp(since, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  "per_page": 100}
        if branch:
            params["sha"] = branch
        out: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            got = await self._req("GET", f"/repos/{repo}/commits", params={**params, "page": page})
            if not isinstance(got, list) or not got:
                break
            out += [c for c in got if isinstance(c, dict)]
            if len(got) < 100:
                break
        return out

    async def pull_reviews(self, repo: str, number: int, pages: int = 10) -> list[dict[str, Any]]:
        """--- KAN-70: a PR's submitted reviews, oldest first, paged."""
        return await self._listing(f"/repos/{repo}/pulls/{int(number)}/reviews", pages)

    async def pull_review_comments(self, repo: str, number: int, pages: int = 10) -> list[dict[str, Any]]:
        """--- KAN-70: a PR's review comments (on lines of the diff, replies included), oldest first, paged."""
        return await self._listing(f"/repos/{repo}/pulls/{int(number)}/comments", pages)

    async def pull(self, repo: str, number: int) -> dict[str, Any]:
        """--- KAN-70: one PR (its author is `user`)."""
        return await self._req("GET", f"/repos/{repo}/pulls/{int(number)}") or {}

    async def pull_commits(self, repo: str, number: int, pages: int = 3) -> list[dict[str, Any]]:
        """--- KAN-70: a PR's commits, oldest first (GitHub lists at most 250), paged."""
        return await self._listing(f"/repos/{repo}/pulls/{int(number)}/commits", pages)

    async def _listing(self, path: str, pages: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            got = await self._req("GET", path, params={"per_page": 100, "page": page})
            if not isinstance(got, list) or not got:
                break
            out += [x for x in got if isinstance(x, dict)]
            if len(got) < 100:
                break
        return out

    async def issue_comments(self, repo: str, number: int, limit: int = 20) -> list[dict[str, Any]]:
        out = await self._req("GET", f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100})
        return [c for c in out if MARKER not in (c.get("body") or "")][-limit:]
