"""Server side of `hangar setup github|slack` and of the webhook fix in `hangar doctor --fix`.

Each step of a manual setup failed for real at least once: a webhook with only push events, a secret on GitHub
that didn't match the vault, settings lost on redeploy because they lived in a hand-edited config map, a token that
could read the repo but not manage its webhooks. So the wizard checks the token before storing anything, stores the
credentials in the vault, writes the settings to the database-backed store (settings_store.py), registers or updates
the repo's webhook with the events hangar handles, and finishes with real deliveries: a signed request through
hangar's own route, then a ping from GitHub whose delivery result it reads back.

Secrets go in and never come out: no response, log line or audit event carries a token or the webhook secret.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from hangar.github import GitHub, GitHubError, parse_repo
from hangar.models import Credential, CredentialKind

# Events the webhook sends: labels and /hangar comments on issues and PRs (what jobs.handle_github acts on), and
# --- overnight/autofix: CI results and reviews on hangar's PRs, which start auto-fix rounds (autofix.py).
# `ensure_hook` adds any that an existing hook lacks, and `hook_problem` (doctor) names the missing ones.
HOOK_EVENTS = ("issues", "issue_comment", "pull_request_review_comment", "pull_request_review", "check_run",
               "check_suite", "status")
HOOK_PATH = "/api/hooks/github"
_WEBHOOK_URL_KEY = "setup:github:webhook_url"  # the URL the wizard registered (when it isn't HANGAR_PUBLIC_URL's)
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]{2,}$")

TOKEN_HELP = (
    "Create a fine-grained token at https://github.com/settings/personal-access-tokens/new for the repos you're "
    "connecting, with repository permissions Issues: Read and write, Pull requests: Read and write and Webhooks: "
    "Read and write (Metadata: Read is added automatically), and Actions: Read so auto-fix can read failing CI "
    "logs. A classic token needs the `repo` scope. Hangar's control plane uses it to comment and open PRs; it never "
    "goes into a sandbox."
)


def _step(steps: list[dict[str, Any]], step: str, ok: bool | None, detail: str) -> bool | None:
    steps.append({"step": step, "status": "ok" if ok else ("fail" if ok is False else "warn"), "detail": detail})
    return ok


def unreachable(url: str) -> str | None:
    """Why GitHub (or Slack) can't deliver to `url`, or None when it looks public."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        return f"{url} is not an http(s) URL"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".svc", ".cluster.local")):
        return f"{host} is only reachable from this network"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    return None if ip.is_global else f"{host} is not a public address"


def hook_url(h, override: str | None = None) -> str:
    base = (override or h.store.kv_get(_WEBHOOK_URL_KEY) or h.settings.public_url).rstrip("/")
    return base if base.endswith(HOOK_PATH) else base + HOOK_PATH


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ============================================================================ GitHub webhook
async def find_hook(gh: GitHub, repo: str, url: str) -> dict[str, Any] | None:
    hooks = await gh._req("GET", f"/repos/{repo}/hooks", params={"per_page": 100}) or []
    return next((x for x in hooks if (x.get("config") or {}).get("url", "").rstrip("/") == url.rstrip("/")), None)


def hook_problem(hook: dict[str, Any] | None) -> str | None:
    if hook is None:
        return "no webhook"
    missing = [e for e in HOOK_EVENTS if e not in (hook.get("events") or []) and "*" not in (hook.get("events") or [])]
    if missing:
        return f"the webhook doesn't send {', '.join(missing)}"
    if not hook.get("active", True):
        return "the webhook is inactive"
    if (hook.get("config") or {}).get("content_type") != "json":
        return "the webhook sends form data, not JSON"
    return None


async def ensure_hook(gh: GitHub, repo: str, url: str, secret: str) -> tuple[dict[str, Any], str]:
    """Create the repo's webhook to `url`, or bring an existing one in line (events, JSON, active, our secret).

    Only adds: the hook's other events stay, and so does its TLS choice (a hangar behind a self-signed certificate
    needs insecure_ssl "1")."""
    config = {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}
    hook = await find_hook(gh, repo, url)
    if hook is None:
        made = await gh._req("POST", f"/repos/{repo}/hooks", json={
            "name": "web", "active": True, "events": list(HOOK_EVENTS), "config": config})
        return made, "created"
    config["insecure_ssl"] = str((hook.get("config") or {}).get("insecure_ssl") or "0")
    events = sorted(set(hook.get("events") or []) | set(HOOK_EVENTS))
    made = await gh._req("PATCH", f"/repos/{repo}/hooks/{hook['id']}",
                         json={"active": True, "events": events, "config": config})
    return made or hook, "updated"


async def ping_hook(gh: GitHub, repo: str, hook_id: int, fast: bool = False) -> tuple[bool | None, str]:
    """Ask GitHub to send a ping and read back how hangar answered it."""
    path = f"/repos/{repo}/hooks/{hook_id}/deliveries"
    before = {d.get("id") for d in (await gh._req("GET", path, params={"per_page": 30}) or [])}
    await gh._req("POST", f"/repos/{repo}/hooks/{hook_id}/pings")
    for attempt in range(8):
        await asyncio.sleep(0 if fast else min(1 + attempt, 4))
        new = [d for d in (await gh._req("GET", path, params={"per_page": 30}) or [])
               if d.get("id") not in before and d.get("event") == "ping"]
        if new:
            code = new[0].get("status_code")
            if code and 200 <= code < 300:
                return True, f"GitHub delivered a ping and hangar answered HTTP {code}"
            why = {401: "hangar rejected the signature: the secret on GitHub differs from the vault's",
                   404: "hangar's GitHub route is off (HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL isn't applied)",
                   503: "hangar couldn't read the webhook secret"}.get(code or 0,
                                                                      "GitHub couldn't reach hangar at that URL")
            return False, f"GitHub's ping got {code or 'no response'}: {why}"
    return None, "GitHub hasn't reported the ping yet; see the repo's Settings → Webhooks → Recent Deliveries"


async def self_test_github(app, secret: str) -> tuple[bool, str]:
    """A signed ping through hangar's own webhook route: proves the secret and settings are live in this process."""
    body = json.dumps({"zen": "hangar setup", "hook_id": 0}).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hangar.local") as c:
        r = await c.post(HOOK_PATH, content=body, headers={
            "content-type": "application/json", "x-github-event": "ping", "x-github-delivery": "hangar-setup",
            "x-hub-signature-256": sign(secret, body)})
    ok = r.status_code == 202 and r.json().get("pong") is True
    return ok, ("a signed test delivery to hangar's webhook route succeeded" if ok
                else f"hangar's webhook route answered HTTP {r.status_code}: {r.text[:200]}")


class GitHubSetup(BaseModel):
    token: str | None = Field(None, repr=False)  # omit to keep the stored token
    repos: dict[str, str] = Field(default_factory=dict)  # owner/repo -> environment(s), merged into the setting
    token_credential: str = "gh-control"
    secret_credential: str = "gh-webhook"
    webhook_url: str | None = None  # where GitHub delivers (a tunnel for a laptop); default HANGAR_PUBLIC_URL
    register_webhook: bool = True
    rotate_secret: bool = False
    # A secret the user chose, for a webhook they add by hand: one hangar generates is never shown.
    webhook_secret: str | None = Field(None, repr=False)


async def _check_token(gh: GitHub, repos: list[str], need_hooks: bool, steps: list) -> bool:
    ok = True
    for repo in repos:
        try:
            info = await gh._req("GET", f"/repos/{repo}")
        except GitHubError as e:
            _step(steps, f"token: {repo}", False, f"the token can't read {repo} ({_status(e)}). {TOKEN_HELP}")
            ok = False
            continue
        perms = info.get("permissions") or {}
        if perms and not (perms.get("push") or perms.get("admin") or perms.get("maintain")):
            _step(steps, f"token: {repo}", False, f"the token's user can't write to {repo}, so hangar couldn't "
                                                  "open PRs or comment")
            ok = False
            continue
        if need_hooks:
            try:
                await gh._req("GET", f"/repos/{repo}/hooks", params={"per_page": 1})
            except GitHubError as e:
                _step(steps, f"token: {repo}", False, f"the token can't manage {repo}'s webhooks ({_status(e)}): "
                      "give it Webhooks: Read and write, or pass --no-webhook and add the webhook by hand")
                ok = False
                continue
        _step(steps, f"token: {repo}", True, "can read the repo" + (" and manage its webhooks" if need_hooks else ""))
    return ok


def _status(e: GitHubError) -> str:
    m = re.search(r": (\d{3}) ", str(e))
    return f"HTTP {m.group(1)}" if m else str(e)[:120]


def _pinned_note(h, names: list[str], steps: list) -> None:
    pinned = [n for n in names if n in h.config.pinned]
    if pinned:
        keys = ", ".join(f"HANGAR_{n.upper()}" for n in pinned)
        _step(steps, "settings", None, f"{keys} also set in the server's environment (on GKE: the hangar-site config "
              "map), which wins over hangar config. Remove them there (kubectl -n hangar edit configmap hangar-site) "
              "or keep them in sync by hand.")


async def setup_github(h, app, body: GitHubSetup, who: str) -> dict[str, Any]:
    from hangar.settings_store import SettingError

    steps: list[dict[str, Any]] = []
    s, transport = h.settings, h.jobs.transport
    repos: dict[str, str] = {}
    for repo, envs in body.repos.items():
        full = parse_repo(repo) or repo.strip()
        if not _REPO_RE.match(full):
            _step(steps, "repos", False, f"'{repo}' isn't owner/repo")
            return {"ok": False, "steps": steps}
        unknown = [e for e in (x.strip() for x in envs.split(",")) if e and e not in h.fleet.catalog.names()]
        if unknown or not envs.strip():
            _step(steps, "repos", False, f"{full}: unknown environment {', '.join(unknown) or '(none given)'}; "
                  f"choose from {', '.join(h.fleet.catalog.names())}")
            return {"ok": False, "steps": steps}
        repos[full] = envs.strip()
    all_repos = sorted({*repos, *(r for r in s.github_repos if _REPO_RE.match(r))})
    if not all_repos:
        _step(steps, "repos", False, "no repos: pass at least one owner/repo and its environment")
        return {"ok": False, "steps": steps}
    token = body.token.strip() if body.token else None
    if token is None:
        if h.store.get(Credential, body.token_credential) is None:
            _step(steps, "token", False, f"no token given and credential '{body.token_credential}' isn't stored. "
                  + TOKEN_HELP)
            return {"ok": False, "steps": steps}
        token = h.broker.secret_text(body.token_credential).strip()
    url = hook_url(h, body.webhook_url)
    far = unreachable(url) if body.register_webhook else None
    registering = body.register_webhook and not far
    old = (h.broker.secret_text(body.secret_credential).strip()
           if h.store.get(Credential, body.secret_credential) is not None else None)
    given = body.webhook_secret.strip() if body.webhook_secret else None
    if given is not None and len(given) < 16:
        _step(steps, "webhook secret", False, "the webhook secret is too short: use at least 16 characters "
              "(openssl rand -hex 32)")
        return {"ok": False, "steps": steps}
    own = ("generate one (openssl rand -hex 32 > ~/.hangar/gh-webhook-secret), pass it with --webhook-secret-file, "
           "and paste it into the webhook's Secret field on GitHub")
    if not body.register_webhook and not (given or old):
        _step(steps, "webhook secret", False, f"adding the webhook by hand needs a secret you know: {own}")
        return {"ok": False, "steps": steps}
    if body.rotate_secret and not given and not registering:
        _step(steps, "webhook secret", False, "a new secret has to reach GitHub too, and hangar isn't registering the "
              f"webhook here ({far or '--no-webhook'}): let it (a public URL, or --webhook-url), or {own}")
        return {"ok": False, "steps": steps}
    new = given or (secrets.token_hex(32) if body.rotate_secret or old is None else None)
    if new == old:
        new = None
    # A new secret replacing one GitHub signs with waits until every hook has it (_rotate). Otherwise it's stored
    # right away: nothing signs with it yet, or the user is giving it to GitHub by hand.
    rotating = new is not None and old is not None and registering
    gh = GitHub(token, s.github_api_url, transport=transport)
    try:
        if not await _check_token(gh, sorted(repos) or all_repos, registering, steps):
            return {"ok": False, "steps": steps}
        # Checks passed: store the credentials and the settings.
        if body.token:
            h.broker.put(body.token_credential, "git", CredentialKind.GIT_TOKEN, token,
                         "GitHub token the control plane uses for PRs and comments (hangar setup github)")
            h.fleet.audit(who, "credential.put", body.token_credential, harness="git", kind="git_token")
        if new is not None and not rotating:
            _put_secret(h, body.secret_credential, new, who)
            _step(steps, "webhook secret", True, ("stored your secret" if given else "generated and stored")
                  + f" as '{body.secret_credential}'")
        elif new is None:
            _step(steps, "webhook secret", True, f"kept the stored '{body.secret_credential}'")
        secret = old if rotating else h.broker.secret_text(body.secret_credential).strip()
        try:
            h.config.set("github_token_credential", body.token_credential)
            h.config.set("github_webhook_secret_credential", body.secret_credential)
            if repos:
                h.config.merge_map("github_repos", repos)
        except SettingError as e:
            _step(steps, "settings", False, str(e))
            return {"ok": False, "steps": steps}
        if body.webhook_url and not far:
            h.store.kv_set(_WEBHOOK_URL_KEY, body.webhook_url.rstrip("/"))
        h.fleet.audit(who, "setup.github", ",".join(sorted(repos)))
        _step(steps, "settings", True, "HANGAR_GITHUB_TOKEN_CREDENTIAL, HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL"
              + (", HANGAR_GITHUB_REPOS" if repos else "") + " saved in hangar's database (survive redeploys)")
        _pinned_note(h, ["github_token_credential", "github_webhook_secret_credential", "github_repos"], steps)
        moved: list[tuple[str, int]] = []
        if rotating and (moved := await _rotate(h, gh, sorted(repos) or all_repos, url, old, new,
                                                 body.secret_credential, who, steps)):
            secret = new
        ok, detail = await self_test_github(app, secret)
        _step(steps, "local delivery", ok, detail)
        if not body.register_webhook:
            _step(steps, "webhook", None, f"skipped: add a webhook to {url} with content type application/json, "
                  f"your secret (stored as '{body.secret_credential}'), and events {', '.join(HOOK_EVENTS)}")
        elif far:
            _step(steps, "webhook", None, f"skipped: GitHub can't deliver to {url} ({far}). Set HANGAR_PUBLIC_URL to "
                  "the public https URL, or pass --webhook-url (e.g. a tunnel to this machine), and run this again.")
        elif rotating:
            for repo, hook_id in moved:  # GitHub signs with the new secret now: prove hangar verifies it
                await _ping(gh, repo, hook_id, steps, fast=transport is not None)
        else:
            for repo in sorted(repos) or all_repos:
                await _register(gh, repo, url, secret, steps, fast=transport is not None)
    except (GitHubError, httpx.HTTPError) as e:
        _step(steps, "github", False, f"GitHub API error: {type(e).__name__}: {str(e)[:300]}")
    finally:
        await gh.close()
    return {"ok": all(x["status"] != "fail" for x in steps), "steps": h.broker.redact(steps), "webhook_url": url}


def _put_secret(h, name: str, secret: str, who: str) -> None:
    h.broker.put(name, "webhook", CredentialKind.GENERIC, secret, "GitHub webhook secret (hangar setup github)")
    h.fleet.audit(who, "credential.put", name, harness="webhook", kind="generic")


async def _register(gh: GitHub, repo: str, url: str, secret: str, steps: list, fast: bool) -> None:
    try:
        hook, how = await ensure_hook(gh, repo, url, secret)
    except GitHubError as e:
        hint = (" (the token needs Webhooks: Read and write)" if _status(e) in ("HTTP 403", "HTTP 404") else "")
        _step(steps, f"webhook: {repo}", False, f"couldn't register the webhook: {str(e)[:300]}{hint}")
        return
    _step(steps, f"webhook: {repo}", True, f"{how} {url} for {', '.join(HOOK_EVENTS)}")
    await _ping(gh, repo, hook["id"], steps, fast)


async def _ping(gh: GitHub, repo: str, hook_id: int, steps: list, fast: bool) -> None:
    try:
        ok, detail = await ping_hook(gh, repo, hook_id, fast=fast)
    except GitHubError as e:
        ok, detail = None, f"couldn't ask GitHub for a test delivery: {str(e)[:300]}"
    _step(steps, f"test delivery: {repo}", ok, detail)


async def _rotate(h, gh: GitHub, repos: list[str], url: str, old: str, new: str, name: str, who: str,
                  steps: list) -> list[tuple[str, int]]:
    """Move every repo's hook to the new secret, then the vault; returns the hooks moved, or [] when it kept the old
    secret. A token with Webhooks: Read passes the token check but can't change a hook: if GitHub refuses one, the
    hooks already moved go back to the old secret and the vault keeps it, so deliveries verify either way."""
    moved: list[tuple[str, int]] = []
    for repo in repos:
        try:
            hook, how = await ensure_hook(gh, repo, url, new)
        except (GitHubError, httpx.HTTPError) as e:
            _step(steps, f"webhook: {repo}", False, f"couldn't set the new secret: {str(e)[:300]}")
            break
        moved.append((repo, hook["id"]))
        _step(steps, f"webhook: {repo}", True, f"{how} {url} for {', '.join(HOOK_EVENTS)}, with the new secret")
    else:
        _put_secret(h, name, new, who)
        _step(steps, "webhook secret", True, f"rotated '{name}' on GitHub and in the vault")
        return moved
    stuck = []
    for repo, _ in moved:
        try:
            await ensure_hook(gh, repo, url, old)
        except (GitHubError, httpx.HTTPError):
            stuck.append(repo)
    _step(steps, "webhook secret", False, f"kept the old '{name}', since GitHub refused the new one" + (
        f"; {', '.join(stuck)} still has the new secret and fails deliveries: run `hangar setup github` again"
        if stuck else "; every hook still has it"))
    return []


async def fix_webhook(h, repo: str, who: str) -> dict[str, Any]:
    """`hangar doctor --fix`: re-register a repo's webhook with the stored token and secret."""
    s, steps = h.settings, []
    full = parse_repo(repo) or repo
    if not _REPO_RE.match(full) or full not in s.github_repos:
        return {"ok": False, "steps": [{"step": "webhook", "status": "fail",
                                        "detail": f"{repo} isn't in HANGAR_GITHUB_REPOS"}]}
    try:
        token = h.broker.secret_text(s.github_token_credential or "").strip()
        secret = h.broker.secret_text(s.github_webhook_secret_credential or "").strip()
    except Exception:  # noqa: BLE001
        return {"ok": False, "steps": [{"step": "webhook", "status": "fail", "detail": "the GitHub token or webhook "
                                        "secret isn't stored: run `hangar setup github`"}]}
    url = hook_url(h)
    if far := unreachable(url):
        return {"ok": False, "steps": [{"step": "webhook", "status": "fail",
                                        "detail": f"GitHub can't deliver to {url} ({far})"}]}
    gh = GitHub(token, s.github_api_url, transport=h.jobs.transport)
    try:
        await _register(gh, full, url, secret, steps, fast=h.jobs.transport is not None)
    except (GitHubError, httpx.HTTPError) as e:
        _step(steps, "github", False, f"GitHub API error: {type(e).__name__}")
    finally:
        await gh.close()
    h.fleet.audit(who, "doctor.fix", full, fix="github_webhook")
    return {"ok": all(x["status"] != "fail" for x in steps), "steps": h.broker.redact(steps)}


async def webhook_checks(h, transport: httpx.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
    """`hangar doctor`: each mapped GitHub repo has an active JSON webhook to hangar with the events it handles."""
    from hangar.doctor import _check

    s = h.settings
    names = {c.name for c in h.store.list(Credential)}
    if not (s.github_token_credential in names and s.github_webhook_secret_credential and s.github_repos):
        return []
    url = hook_url(h)
    if far := unreachable(url):
        return [_check("github webhook", None, f"GitHub can't deliver to {url} ({far})",
                       "set HANGAR_PUBLIC_URL to hangar's public https URL, or `hangar setup github --webhook-url "
                       "<tunnel URL>` on a laptop")]
    out: list[dict[str, Any]] = []
    try:
        token = h.broker.secret_text(s.github_token_credential).strip()
    except Exception:  # noqa: BLE001 - the github token check reports this
        return []
    gh = GitHub(token, s.github_api_url, transport=transport)
    try:
        for repo in sorted(r for r in s.github_repos if _REPO_RE.match(r)):
            try:
                problem = hook_problem(await find_hook(gh, repo, url))
            except (GitHubError, httpx.HTTPError) as e:
                out.append(_check(f"github webhook {repo}", None, f"can't list the repo's webhooks: {str(e)[:200]}",
                                  f"check by hand that {repo} has a webhook to {url} (JSON) with events "
                                  f"{', '.join(HOOK_EVENTS)}, or give the token Webhooks: Read and write"))
                continue
            out.append(_check(f"github webhook {repo}", problem is None,
                              f"{url} receives {', '.join(HOOK_EVENTS)}" if problem is None else f"{problem} to {url}",
                              f"hangar doctor --fix   (or: hangar setup github --repo {repo}=<env>)",
                              action={"id": "github_webhook", "repo": repo}))
    finally:
        await gh.close()
    return out


# ============================================================================ Slack
class SlackSetup(BaseModel):
    signing_secret: str | None = Field(None, repr=False)
    bot_token: str | None = Field(None, repr=False)
    channels: dict[str, str] = Field(default_factory=dict)  # channel id -> owner/repo, merged into the setting
    users: list[str] | None = None  # replaces HANGAR_SLACK_USERS when given
    signing_credential: str = "slack-signing"
    bot_credential: str = "slack-bot"


async def self_test_slack(app, secret: str) -> tuple[bool, str]:
    """A signed url_verification through hangar's own Slack route, like the one Slack sends when you save the URL."""
    challenge = secrets.token_urlsafe(12)
    body = json.dumps({"type": "url_verification", "challenge": challenge}).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hangar.local") as c:
        r = await c.post("/api/hooks/slack/events", content=body, headers={
            "content-type": "application/json", "x-slack-request-timestamp": ts, "x-slack-signature": sig})
    ok = r.status_code == 200 and r.json().get("challenge") == challenge
    return ok, ("a signed url_verification to hangar's Slack route succeeded" if ok
                else f"hangar's Slack route answered HTTP {r.status_code}: {r.text[:200]}")


async def setup_slack(h, app, body: SlackSetup, who: str) -> dict[str, Any]:
    from hangar.settings_store import SettingError
    from hangar.slack import SlackAPI, SlackError, _repo_url

    steps: list[dict[str, Any]] = []
    s = h.settings
    for ch, repo in body.channels.items():
        if not _CHANNEL_RE.match(ch):
            _step(steps, "channels", False, f"'{ch}' isn't a channel id (like C0123ABCD: channel name → About)")
            return {"ok": False, "steps": steps}
        if not h.jobs.repo_envs(_repo_url(repo)):
            _step(steps, "channels", False, f"{repo} isn't in HANGAR_GITHUB_REPOS: run `hangar setup github --repo "
                  f"{repo}=<env>` first")
            return {"ok": False, "steps": steps}
    for label, given, name in (("signing secret", body.signing_secret, body.signing_credential),
                               ("bot token", body.bot_token, body.bot_credential)):
        if not given and h.store.get(Credential, name) is None:
            _step(steps, label, False, f"no {label} given and credential '{name}' isn't stored")
            return {"ok": False, "steps": steps}
    bot = body.bot_token.strip() if body.bot_token else h.broker.secret_text(body.bot_credential).strip()
    api = SlackAPI(bot, s.slack_api_url, transport=h.jobs.transport)
    try:
        me = await api.auth_test()
    except SlackError as e:
        _step(steps, "bot token", False, f"Slack rejected the bot token: {str(e)[:200]}. Use the Bot User OAuth "
              "Token (xoxb-…) from OAuth & Permissions, after installing the app")
        return {"ok": False, "steps": steps}
    finally:
        await api.close()
    _step(steps, "bot token", True, f"works: {me.get('user', '?')} in {me.get('team', me.get('team_id', '?'))}")
    if body.bot_token:
        h.broker.put(body.bot_credential, "webhook", CredentialKind.GENERIC, bot, "Slack bot token (hangar setup)")
        h.fleet.audit(who, "credential.put", body.bot_credential, harness="webhook", kind="generic")
    if body.signing_secret:
        h.broker.put(body.signing_credential, "webhook", CredentialKind.GENERIC, body.signing_secret.strip(),
                     "Slack signing secret (hangar setup)")
        h.fleet.audit(who, "credential.put", body.signing_credential, harness="webhook", kind="generic")
    try:
        h.config.set("slack_signing_secret_credential", body.signing_credential)
        h.config.set("slack_bot_token_credential", body.bot_credential)
        if body.channels:
            h.config.merge_map("slack_channels", body.channels)
        if body.users is not None:
            h.config.set("slack_users", body.users)
    except SettingError as e:
        _step(steps, "settings", False, str(e))
        return {"ok": False, "steps": steps}
    h.fleet.audit(who, "setup.slack", ",".join(sorted(body.channels)))
    _step(steps, "settings", True, "Slack credentials and channels saved in hangar's database (survive redeploys)")
    _pinned_note(h, ["slack_signing_secret_credential", "slack_bot_token_credential", "slack_channels",
                     "slack_users"], steps)
    if not s.slack_channels:
        _step(steps, "channels", None, "no channel is mapped yet: pass --channel C0123ABCD=owner/repo")
    ok, detail = await self_test_slack(app, h.broker.secret_text(body.signing_credential).strip())
    _step(steps, "local delivery", ok, detail)
    base = s.public_url.rstrip("/")
    if far := unreachable(base):
        _step(steps, "public url", None,
              f"Slack can't reach {base} ({far}); set HANGAR_PUBLIC_URL to a public https URL")
    return {"ok": all(x["status"] != "fail" for x in steps), "steps": h.broker.redact(steps),
            "urls": {"commands": f"{base}/api/hooks/slack/commands", "events": f"{base}/api/hooks/slack/events"}}


# ============================================================================ routes
class WebhookFix(BaseModel):
    repo: str


def mount(app, h, admin) -> None:
    # One setup at a time: two wizards racing could each store a new webhook secret and leave GitHub with the other's.
    lock = asyncio.Lock()

    @app.post("/api/setup/github")
    async def setup_github_route(body: GitHubSetup, who: str = admin):
        async with lock:
            return await setup_github(h, app, body, who)

    @app.post("/api/setup/github/webhook")
    async def setup_github_webhook(body: WebhookFix, who: str = admin):
        async with lock:
            return await fix_webhook(h, body.repo, who)

    @app.post("/api/setup/slack")
    async def setup_slack_route(body: SlackSetup, who: str = admin):
        async with lock:
            return await setup_slack(h, app, body, who)
