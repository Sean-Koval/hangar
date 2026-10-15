"""GitHub deliveries that hangar missed or half handled are run again (hardening B7/H7, product B1/P2b).

Two holes lost `/hangar` labels and comments:
- The replay guard remembered a delivery before handling it, so a pod that died mid-handler (or a power-down at that
  moment) left the delivery marked as seen: GitHub's redelivery of it was refused as a replay. Now it is marked
  `pending` with this process's boot id first and `done` only after the handler returns (`claim`/`done`). A pending
  mark from an earlier process means that process died with it, so the delivery runs; a pending mark from this one
  means the same delivery is being handled right now, so the duplicate is refused. A job's
  `idempotency_key=github-delivery:<id>` and the "already working on this issue" check stop a double submit.
- GitHub does not retry a failed delivery on its own, so everything sent while the cluster was down (01:00-10:00)
  was lost. At startup, `replay_missed` lists each connected repo's webhook deliveries since the last start and asks
  GitHub to redeliver every one with no successful attempt. They come back through the webhook route, signature and
  replay guard included.

Hangar's webhook is a repository webhook (`setup_api.ensure_hook` creates it with POST /repos/{repo}/hooks), so the
endpoints are the repository ones: GET /repos/{owner}/{repo}/hooks/{hook_id}/deliveries (cursor pagination, newest
first) and POST .../deliveries/{delivery_id}/attempts. The control plane's token already needs Webhooks read and write
for setup (`setup_api.TOKEN_HELP`). GitHub keeps deliveries for 3 days and keeps the delivery's GUID across
redeliveries, which is how a delivery that later succeeded is told apart.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

import httpx

from hangar.github import GitHub, GitHubError, parse_repo

if TYPE_CHECKING:
    from hangar.frontdoor import ReplayGuard

log = logging.getLogger("hangar.redeliver")

BOOT = secrets.token_hex(8)  # this process: a pending mark from any other one was left by a process that died
PENDING = "pending:"
DONE = "done"
SINCE_KEY = "github:redeliver:since:"  # + repo: when the last pass over its deliveries started
WINDOW = 3 * 86400  # GitHub keeps deliveries (and allows redelivery) for 3 days
FIRST_WINDOW = 86400  # no pass recorded yet: the last day, like GitHub's own example script
OVERLAP = 600.0  # seconds re-read before the last pass, for deliveries that finished failing while it ran
MAX_PAGES = 10  # of 100 deliveries per repo
START_DELAY = 5.0  # seconds after startup, so the webhook route is serving when GitHub redelivers


# ====================================================================== commit after handle
def claim(replays: ReplayGuard, digest: str, ttl: float) -> bool:
    """Mark a signed delivery as being handled by this process. False when it was handled already, or is being
    handled now; True when it is new or a process that died left it half handled."""
    seen = replays.get("github", digest)
    if seen is not None and not (seen.startswith(PENDING) and seen != PENDING + BOOT):
        return False
    replays.put("github", digest, PENDING + BOOT, ttl)
    return True


def done(replays: ReplayGuard, digest: str, ttl: float) -> None:
    replays.put("github", digest, DONE, ttl)


# ====================================================================== redelivery at startup
def _ts(value: Any) -> float:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _repos(mapping: dict[str, str]) -> list[str]:
    from hangar.setup_api import _REPO_RE

    out = []
    for key in mapping:
        repo = parse_repo(key) or (key if _REPO_RE.match(key) else None)
        if repo and repo not in out:
            out.append(repo)
    return out


async def _replay_repo(gh: GitHub, repo: str, hook_id: int, since: float, events: set[str]) -> int:
    newest: dict[str, dict[str, Any]] = {}  # guid -> its latest attempt
    succeeded: set[str] = set()
    cursor = None
    for _ in range(MAX_PAGES):
        items, cursor = await gh.hook_deliveries(repo, hook_id, cursor)
        for d in items:
            guid = str(d.get("guid") or d.get("id"))
            if 200 <= int(d.get("status_code") or 0) < 400:  # GitHub's own "success" range
                succeeded.add(guid)
            if guid not in newest or _ts(d.get("delivered_at")) > _ts(newest[guid].get("delivered_at")):
                newest[guid] = d
        if not cursor or not items or min(_ts(d.get("delivered_at")) for d in items) < since:
            break
    n = 0
    for guid, d in newest.items():
        if guid in succeeded or d.get("event") not in events or _ts(d.get("delivered_at")) < since:
            continue
        await gh.redeliver(repo, hook_id, int(d["id"]))
        n += 1
        log.info("asked GitHub to redeliver %s %s delivery %s (it got %s)", repo, d.get("event"), guid,
                 d.get("status_code") or d.get("status") or "no response")
    return n


async def replay_missed(h: Any) -> int:
    """Ask GitHub to redeliver the webhook deliveries that failed since the last pass. Returns how many."""
    from hangar.setup_api import HOOK_EVENTS, find_hook, hook_url

    s = h.settings
    if not (s.github_redeliver_on_start and s.github_webhook_secret_credential and s.github_repos):
        return 0
    gh = h.jobs._github()
    if gh is None:
        return 0
    url, total = hook_url(h), 0
    try:
        for repo in _repos(s.github_repos):
            started = time.time()
            last = float(h.store.kv_get(SINCE_KEY + repo) or 0)
            since = max((last - OVERLAP) if last else started - FIRST_WINDOW, started - WINDOW + OVERLAP)
            try:
                hook = await find_hook(gh, repo, url)
                if hook is None:
                    continue
                total += await _replay_repo(gh, repo, int(hook["id"]), since, set(HOOK_EVENTS))
                h.store.kv_set(SINCE_KEY + repo, str(started))
            except (GitHubError, httpx.HTTPError, KeyError, ValueError) as e:  # looked at again next time
                log.warning("could not replay missed GitHub deliveries for %s: %s", repo, e)
    finally:
        await gh.close()
    if total:
        h.fleet.audit("system", "github.redeliver", None, deliveries=total)
    return total


def start(h: Any) -> asyncio.Task:
    async def run() -> None:
        await asyncio.sleep(0 if h.jobs.transport else START_DELAY)
        try:
            if n := await replay_missed(h):
                log.info("asked GitHub to redeliver %d missed webhook deliveries", n)
        except Exception as e:  # noqa: BLE001 - the next start tries again from the same point
            log.warning("replaying missed GitHub deliveries failed: %s", e)

    return asyncio.create_task(run())
