"""Team credentials, people's own, and which ones a sandbox gets (the owner's decision 5; docs/team.md).

* **Whose a credential is.** A person's own (`person` set: their Claude or Codex login) goes only into their
  sandboxes. A **team credential** (`person` unset) is one an admin added for everyone: jobs no person owns use it,
  and a person without a login of their own falls back to it. Control-plane credentials (identity.exposure) never
  reach a sandbox, whoever owns them.
* **The migration.** Before people every credential was unowned, and #88 read unowned as the team's, so the owner's
  own Claude and Codex logins went into every member's sandbox. `migrate` makes each of them the owner's, once, at
  startup, and records what it moved (`hangar cred migration`). After it, an unowned credential is a team credential
  an admin added on purpose (`hangar cred add --team`); an admin's `hangar cred add` without `--team` is their own.
* **Which one a sandbox gets.** For its agent harness, and for git tokens when its environment lists one: its
  person's own, else the team's, as the repo's policy says (`credentials: person | team | person_then_team`, the
  default). Another person's own is dropped from an environment's list and refused when named.
* **Logins one sandbox holds at a time.** A Codex ChatGPT login rotates its refresh token, so it is leased to one
  sandbox (broker.acquire). Several logins of one kind are a pool: a sandbox takes a free one. When all are in use,
  a job or session waits and says which are busy (HANGAR_LOGIN_WAIT_SECONDS) instead of failing on the first.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import httpx

import hangar.identity as identity
from hangar.broker import CredentialError
from hangar.harnesses import HARNESSES
from hangar.models import Credential, CredentialKind, ScheduledJob, now
from hangar.people import OWNER

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.fleet import Fleet
    from hangar.store import Store

Mode = Literal["person", "team", "person_then_team"]
MODES: tuple[str, ...] = ("person", "team", "person_then_team")
DEFAULT_MODE: Mode = "person_then_team"
MIGRATION_KEY = "teamcreds:migration"
# Consumer sign-ins (`claude setup-token`, a ChatGPT or Google login): one person's, by the vendors' terms.
SUBSCRIPTION_KINDS = frozenset({CredentialKind.OAUTH_TOKEN, CredentialKind.AUTH_FILE})
POLL_SECONDS = 5.0  # how often a create waiting for a leased login looks again
FREQUENT_SENDER_JOBS = 3  # unlinked GitHub or Slack senders with this many jobs in UNLINKED_DAYS: doctor names them
UNLINKED_DAYS = 14


def _subscription(harness: str, kind: CredentialKind) -> bool:
    h = HARNESSES.get(harness)
    return kind in SUBSCRIPTION_KINDS and h is not None and h.agent


def is_subscription(c: Credential) -> bool:
    return _subscription(c.harness, c.kind)


def owner(c: Credential, settings: Settings | None) -> str:
    """What listings show: `control-plane`, `team`, or the person whose own it is."""
    if identity.exposure(c, settings) == "control_plane":
        return "control-plane"
    return c.person or "team"


def check_team(name: str, harness: str, kind: CredentialKind) -> None:
    """Refuse a subscription login (a Claude Pro or Max sign-in, a ChatGPT `auth.json`, a Google sign-in) as a team
    credential, new or moved (the owner's decision): the vendors' terms make a login one person's, and Claude Code's
    forbid routing requests through a Pro or Max login on behalf of others. Team credentials are API keys, or
    credentials the vendor's terms allow for automation."""
    if _subscription(harness, kind):
        raise identity.Forbidden(
            f"'{name}' is a subscription login, and a subscription login can't be a team credential: the vendors' "
            "terms make it one person's (docs/team.md#what-the-vendors-allow). Keep it someone's own (hangar cred "
            f"owner {name} PERSON), and add an API key for the team: hangar cred add NAME --team --harness {harness} "
            "--kind api_key --file key.txt")


# ---------------------------------------------------------------------------- the migration
def migration(store: Store) -> dict[str, Any] | None:
    """The migration's record: when it ran, and which credentials it made the owner's (`hangar cred migration`)."""
    return store.kv_get_json(MIGRATION_KEY)


def migrate(store: Store, settings: Settings | None,
            audit: Callable[..., None] | None = None) -> dict[str, Any]:
    """Make every credential stored before people the owner's, once. Control-plane ones stay the control plane's.

    The list is recorded before anything moves, so a crash part way finishes the same list on the next start; a
    credential an admin reassigned in between is left as they set it."""
    rec = migration(store)
    if rec is not None and rec.get("state") == "done":
        return rec
    if rec is None:
        unowned = [c for c in store.list(Credential) if c.person is None]
        rec = {"state": "running", "started_at": now(), "to": OWNER,
               "moved": sorted(c.name for c in unowned if identity.exposure(c, settings) != "control_plane"),
               "control_plane": sorted(c.name for c in unowned if identity.exposure(c, settings) == "control_plane")}
        store.kv_set(MIGRATION_KEY, json.dumps(rec), durable=True)
    for name in rec["moved"]:
        c = store.get(Credential, name)
        if c is not None and c.person is None:
            c.person = OWNER
            store.put(c)
    rec.update(state="done", finished_at=now())
    store.kv_set(MIGRATION_KEY, json.dumps(rec), durable=True)
    if audit is not None and rec["moved"]:
        audit("hangar", "credential.migrate", None, to=OWNER, moved=rec["moved"])
    return rec


# ---------------------------------------------------------------------------- which credentials a sandbox gets
def mode_for(store: Store, repo: str | None) -> str:
    """The repo policy's `credentials`, else person_then_team."""
    from hangar.repopolicy import RepoPolicies

    p = RepoPolicies(store).get(repo) if repo else None
    return p.credentials if p is not None and p.credentials else DEFAULT_MODE


class Resolved(NamedTuple):
    names: list[str]
    busy: list[str]  # pools whose every login is leased to another sandbox, said plainly


def _pick(chosen: list[Credential]) -> tuple[list[Credential], str | None]:
    """One login from each pool of leased-one-at-a-time logins (same kind): a free one, else the first (whose
    acquire then fails, or the caller waits). Everything else as it is."""
    pools: dict[CredentialKind, list[Credential]] = {}
    for c in chosen:
        if c.exclusive:
            pools.setdefault(c.kind, []).append(c)
    picked: set[str] = set()
    busy: list[str] = []
    for pool in pools.values():
        free = [c for c in pool if not c.leased_to]
        picked.add((free or pool)[0].name)
        if not free:
            whose = pool[0].person or "the team"
            # --- review: no sandbox ids: a team login is busy in other people's sandboxes, and this text reaches
            # the job's thread (a Jira or GitHub comment) when the wait runs out
            busy.append(f"{whose}'s {pool[0].harness} login{'s' if len(pool) > 1 else ''} "
                        + ", ".join(c.name for c in pool) + (" (all in use)" if len(pool) > 1 else " (in use)"))
    return [c for c in chosen if not c.exclusive or c.name in picked], "; ".join(busy) or None


def resolve(store: Store, names: list[str], person: str | None, harness: str | None, explicit: bool,
            is_agent: Callable[[str | None], bool], mode: str = DEFAULT_MODE,
            settings: Settings | None = None) -> Resolved:
    """The credentials a sandbox owned by `person` (None: a job no person owns) gets.

    `names` is what the caller named (`explicit`: used as named, except that another person's own is refused, or
    left out of a sandbox no one owns) or the environment's list for the selected harness. For the agent harness,
    and for git when the environment lists a git credential, the person's own replace the team's (`mode`); with no
    own and no team one listed, every team credential for it is the pool."""
    creds = {c.name: c for c in store.list(Credential)}
    out: list[str] = []
    for n in names:
        c = creds.get(n)
        if c is not None and c.person is not None and c.person != person:
            if explicit and person is not None:
                raise CredentialError(f"credential '{n}' is {c.person}'s own; attach your own or a team one")
            continue
        out.append(n)
    if explicit:
        return Resolved(out, [])
    groups = [harness] if harness and is_agent(harness) else []
    if any(creds[n].harness == "git" for n in names if n in creds):
        groups.append("git")
    busy: list[str] = []
    for g in groups:
        own = sorted((c for c in creds.values() if person and c.person == person and c.harness == g),
                     key=lambda c: c.name)
        team = [creds[n] for n in out if n in creds and creds[n].harness == g and creds[n].person is None] or \
            sorted((c for c in creds.values() if c.person is None and c.harness == g
                    and identity.exposure(c, settings) == "sandbox"), key=lambda c: c.name)
        chosen = own if mode == "person" else team if mode == "team" else (own or team)
        if not chosen and mode == "person" and g == harness:
            raise CredentialError(
                "this repo's policy says credentials: person, and "
                + (f"{person} has no {g} credential of their own (they add one with hangar cred add)" if person
                   else "this job runs for no one: link its sender to a person (hangar person link), or set "
                        "credentials: person_then_team"))
        chosen, why = _pick(chosen)
        if why:
            busy.append(why)
        keep = {c.name for c in chosen}
        out = [n for n in out if n not in creds or creds[n].harness != g or n in keep] + \
            [c.name for c in chosen if c.name not in out]
    return Resolved(out, busy)


async def for_sandbox(fleet: Fleet, names: list[str], person: str | None, harness: str | None, explicit: bool,
                      repo: str | None, progress: Callable[[str], None] | None) -> list[str]:
    """resolve(), waiting while every login of a pool is leased. Jobs and sessions (they pass `progress`) wait up to
    HANGAR_LOGIN_WAIT_SECONDS and say which logins are busy; a direct create fails at once with the same words.

    Nothing awaits between the last resolve and the create's broker.acquire, so two creates can't take the same
    free login."""
    mode = mode_for(fleet.store, repo)
    deadline = time.monotonic() + fleet.settings.login_wait_seconds
    said = None
    while True:
        r = resolve(fleet.store, names, person, harness, explicit, fleet._is_agent, mode, fleet.settings)
        if not r.busy:
            return r.names
        why = "; ".join(r.busy)
        if progress is None or time.monotonic() >= deadline or fleet.closing.is_set():
            raise CredentialError(
                f"every login this sandbox could use is in use: {why}. A ChatGPT login (like the other sign-in "
                "files) goes into one sandbox at a time; add another login to the pool, or use an API key, which "
                "isn't leased (docs/team.md#codex-logins-one-sandbox-at-a-time)")
        if why != said:
            with contextlib.suppress(Exception):
                progress(f"waiting for a free login (queued): {why}")
            said = why
        await asyncio.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------- linked accounts (--- review)
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


async def link_id(h: Any, source: str, value: str) -> tuple[str, str | None]:
    """(the id to link, the login it was looked up from). A GitHub account links by its numeric user id, which
    survives a rename; an admin may give the login, which is looked up here with the control plane's GitHub token."""
    from hangar.github import GitHubError
    from hangar.people import PeopleError

    v = (value or "").strip().removeprefix("@")
    if (source or "").strip().lower() not in ("github", "github_projects", "github-projects") or v.isdigit():
        return value, None
    if not _LOGIN.match(v):
        raise PeopleError(f"'{value}' isn't a GitHub login or user id")
    gh = h.jobs._github()
    if gh is None:
        raise PeopleError(f"can't look up the GitHub login '{v}': no GitHub token is stored "
                          f"(HANGAR_GITHUB_TOKEN_CREDENTIAL). Link by user id instead: it's the `id` at "
                          f"https://api.github.com/users/{v}")
    try:
        user = await gh._req("GET", f"/users/{v}")
    except (GitHubError, httpx.HTTPError) as e:
        raise PeopleError(f"can't look up the GitHub login '{v}': {e}") from e
    finally:
        await gh.close()
    uid = (user or {}).get("id")
    if not isinstance(uid, int) or isinstance(uid, bool) or str((user or {}).get("type")) not in ("User", "Bot"):
        raise PeopleError(f"GitHub's answer for '{v}' has no user id")
    return str(uid), str(user.get("login") or v)


def may_continue(job_person: str | None, sender: str | None) -> str | None:
    """Why a sender at a front door (GitHub, Slack, Jira) may not continue or answer a job, or None. A person's job
    runs with their own login and budget: only they continue it there. Admins too go through hangar itself
    (`hangar job follow-up`), since in direct mode an agent can comment as the account behind its git token. A team
    job (no person) is anyone's the front door trusts."""
    if job_person is None or sender == job_person:
        return None
    return (f"this is {job_person}'s job, and it runs with their own login: only {job_person} continues it here "
            "(an admin can with hangar job follow-up). Start your own with /hangar")


# ---------------------------------------------------------------------------- hangar doctor
def _no_person_sources(h: Any) -> list[tuple[str, str, str | None, str | None]]:
    """(what, environment, harness asked for, repo) for the work no person owns: mapped repos (GitHub issues and
    Slack), inbound webhooks and schedules without a person."""
    out: list[tuple[str, str, str | None, str | None]] = []
    s = h.settings
    for repo, envs in s.github_repos.items():
        policy = h.jobs.policies.get(repo)
        env = policy.environment if policy and policy.environment else next(
            (e.strip() for e in envs.split(",") if e.strip()), None)
        if env:
            out.append((f"jobs on {repo} from GitHub and Slack", env, policy.harness if policy else None, repo))
    for name, cfg in (s.inbound_webhooks or {}).items():
        if isinstance(cfg, dict) and isinstance(cfg.get("environment"), str):
            out.append((f"webhook {name}", cfg["environment"], cfg.get("harness"), cfg.get("repo")))
    for sch in h.store.list(ScheduledJob):
        t = sch.template or {}
        if sch.person is None and sch.enabled and isinstance(t.get("environment"), str):
            out.append((f"schedule {sch.name}", t["environment"], t.get("harness"), t.get("repo")))
    return out


def _check(name: str, status: str, detail: str, fix: str | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "fix": None if status == "ok" else fix}


def doctor_checks(h: Any) -> list[dict[str, Any]]:
    """`hangar doctor`: personal logins still shared, work no person owns that has no team credential, and the
    GitHub and Slack senders who start jobs often but are linked to nobody."""
    out: list[dict[str, Any]] = []
    s, store = h.settings, h.store
    creds = store.list(Credential)

    # 1. personal credentials still shared
    rec = migration(store)
    shared = sorted(c.name for c in creds if c.person is None and is_subscription(c)
                    and identity.exposure(c, s) == "sandbox")
    moved = f"the migration made {len(rec.get('moved') or [])} earlier credential(s) the owner's" if rec else \
        "the credential migration hasn't run (it runs when hangar starts)"
    if shared:
        out.append(_check("personal credentials shared", "fail",
                          f"subscription logins stored as team credentials, so they reach every member's sandbox and "
                          f"jobs no person owns: {', '.join(shared)}; {moved}",
                          "a subscription login can't be a team credential (the vendors' terms make it one person's): "
                          "make each someone's own (hangar cred owner NAME PERSON) or remove it, and add team API keys "
                          "(hangar cred add NAME --team --harness claude-code --kind api_key --file key.txt); "
                          "docs/team.md#what-the-vendors-allow"))
    else:
        out.append(_check("personal credentials shared", "ok" if rec else "warn",
                          f"no subscription login is a team credential; {moved}", "restart hangar"))

    # 2. work no person owns, and the team credential it runs with
    missing: list[str] = []
    person_only: list[str] = []
    sources = _no_person_sources(h)
    for what, env, wanted, repo in sources:
        try:
            spec = h.fleet.catalog.get(env)
            harness = wanted if wanted in spec.agent_harnesses else spec.pick_harness(None)
        except (KeyError, ValueError):
            continue  # the environment checks report it
        if not harness or not h.fleet._is_agent(harness):
            continue
        mode = mode_for(store, repo)
        if mode == "person":
            person_only.append(f"{what} ({repo})")
            continue
        names = [c for c in spec.credentials if h.fleet._cred_harness(c) in (harness, None)
                 or not h.fleet._is_agent(h.fleet._cred_harness(c))]
        try:
            got = resolve(store, names, None, harness, False, h.fleet._is_agent, mode, s).names
        except CredentialError:
            got = []
        if not any(c.name in got and c.harness == harness for c in creds):
            missing.append(f"{what}: {env} ({harness})")
    if missing:
        out.append(_check("team credentials for jobs", "warn",
                          "work no person owns would run with no login for its harness: " + "; ".join(missing),
                          "add a team credential, preferably an API key (hangar cred add NAME --team --harness "
                          "HARNESS --kind api_key --file key.txt), or link the senders to people (hangar person "
                          "link PERSON github LOGIN); docs/team.md"))
    elif person_only:
        out.append(_check("team credentials for jobs", "warn",
                          "these repos' policies say credentials: person, so jobs from senders linked to nobody "
                          "fail: " + "; ".join(person_only),
                          "link the senders (hangar person link), or hangar repo set REPO --credentials "
                          "person_then_team"))
    else:
        out.append(_check("team credentials for jobs", "ok", "work no person owns has a team credential"
                          if sources else "nothing runs for no one (no mapped repos, webhooks or "
                          "unowned schedules)"))

    # 3. frequent senders linked to nobody
    since = now() - UNLINKED_DAYS * 86400
    seen: Counter[str] = Counter(j.requester for j in h.jobs.list(limit=500)
                                 if j.source in ("github", "slack") and j.person is None and j.created_at >= since)
    frequent = [(who, n) for who, n in seen.most_common() if n >= FREQUENT_SENDER_JOBS]
    if frequent:
        cmds = []
        for who, _ in frequent[:3]:
            src, _, ext = who.partition(":")  # github:<login> (looked up when linked), slack:<team>/<user>
            cmds.append(f"hangar person link PERSON {src} {ext}")
        out.append(_check("unlinked senders", "warn",
                          f"started jobs in the last {UNLINKED_DAYS} days but are linked to nobody, so their jobs run "
                          "for the team, outside anyone's caps and budget: "
                          + ", ".join(f"{who} ({n} jobs)" for who, n in frequent[:10]),
                          "; ".join(cmds)))
    else:
        out.append(_check("unlinked senders", "ok",
                          f"no GitHub or Slack sender linked to nobody started {FREQUENT_SENDER_JOBS}+ jobs in the "
                          f"last {UNLINKED_DAYS} days"))
    return out

