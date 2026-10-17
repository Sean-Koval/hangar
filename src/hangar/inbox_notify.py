"""Inbox delivery (#157, #158): what waits on a person reaches them in Slack, linked to where they act on it.

Nobody runs `hangar inbox` to find out an agent is blocked on them. So:

* **At once.** A new `question` or `approval` item (inbox.py) goes to its person in a Slack DM, within a minute of
  the job or session change that raised it (the same changes `hangar://inbox` subscribers hear). One line of
  reason, "Answer here: <native link>" (the Slack thread, the Jira issue, the PR, else the job on the dashboard: the
  place where the answer is recorded with an identity; there are no buttons in the DM), and the CLI fallback.
  A burst is gathered into one DM (COALESCE), and each item goes once: `inbox:sent:<person>:<id>` in kv, kept a week.
* **Twice a day.** A digest at each of the person's `digest` times (10:15 and 16:00 in their `tz` by default,
  weekdays): questions oldest first, approvals with their expiry, jobs failed since the last digest, approved PRs
  that aren't landing; reviews only as a count and a link to GitHub's review queue (GitHub stays the review queue).
  It rides the ops loop, which runs while the sandbox pools are powered down (the control plane stays on), and
  `digest:last:<person>` keeps a restart from sending a slot twice; a slot missed while hangar was down goes out
  on recovery if it is under DIGEST_LATE old, else it is skipped.
* **Who.** A person's Slack account is their `slack:TEAM/USER` link (people.py); without one nothing is sent and
  `hangar doctor` lists them. `notify` (`hangar person set <id> --dm off|questions|all`): `off` sends nothing,
  `questions` (the default) their own agents' questions and approvals, `all` also the team's (the work nobody in
  particular owns, which the inbox gives admins), so not every admin is paged for every team question. `quiet`
  ("20:00-08:00" in their tz) holds DMs, except approvals that would expire before it ends.
* **Text.** Agent-written: escaped for Slack (`slack.escape`) and redacted (`broker.redact`), as thread posts are.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from hangar import inbox, people
from hangar.auth import Principal
from hangar.models import Job, Person, Session

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.slack import SlackAPI

log = logging.getLogger("hangar.inbox_notify")

DM_KINDS = ("question", "approval")  # sent at once; `failed` waits for the digest, `review` stays in GitHub
NOTIFY = ("off", "questions", "all")
SENT_PREFIX = "inbox:sent:"
SENT_TTL = 7 * 86400
COALESCE = 10.0  # seconds a burst of changes is gathered for, so five approvals in ten seconds are one DM
RECHECK = 60.0  # seconds between passes over everyone (quiet windows ending, a send that failed)
DIGEST_PREFIX = "digest:last:"
DIGEST_LATE = 2 * 3600  # a slot missed by more than this (hangar was down) is skipped
DIGEST_MAX = 20  # items listed in a digest; the rest are counted
LOGIN_PREFIX = "github:login:"  # kv: a GitHub user id's login, for the digest's review link
LOGIN_TTL = 7 * 86400
_HHMM = re.compile(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$")
_LINK_OK = re.compile(r"^https://[^\s<>|]+$")


# ====================================================================== people and their settings
def slack_user(p: Person) -> str | None:
    """The Slack user id a person's DMs go to (their `slack:TEAM/USER` link), or None."""
    return next((link.rsplit("/", 1)[-1] for link in p.links if link.startswith("slack:") and "/" in link), None)


def principal_for(p: Person) -> Principal:
    """Whose inbox to deliver: their own work, plus the team's for an admin who asked for it (`--dm all`)."""
    role = "admin" if p.role == "admin" and p.notify == "all" else "member"
    return Principal(f"inbox:{p.id}", f"inbox:{p.id}", [], person=p.id, role=role)


def parse_hhmm(v: str) -> dtime:
    m = _HHMM.match((v or "").strip())
    if not m:
        raise people.PeopleError(f"'{v}' isn't a time like 10:15")
    return dtime(int(m.group(1)), int(m.group(2)))


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise people.PeopleError(f"unknown time zone '{name}' (an IANA name like America/New_York)") from e


def set_delivery(store: Any, person_id: str, *, dm: str | None = None, quiet: str | None = None,
                 tz: str | None = None, digest: list[str] | None = None,
                 digest_weekends: bool | None = None) -> Person:
    """Change how the inbox reaches someone. `quiet` "" clears the window; `digest` [] turns the digest off."""
    p = store.get(Person, person_id)
    if p is None:
        if person_id != people.OWNER:
            raise KeyError(f"no person '{person_id}'")
        p = people.get(store, person_id)
    assert p is not None
    if dm is not None:
        if dm not in NOTIFY:
            raise people.PeopleError(f"--dm must be one of {', '.join(NOTIFY)}")
        p.notify = dm  # type: ignore[assignment]
    if tz is not None:
        zone(tz)
        p.tz = tz
    if quiet is not None:
        if quiet.strip():
            a, sep, b = quiet.partition("-")
            if not sep:
                raise people.PeopleError(f"--quiet '{quiet}' isn't a window like 20:00-08:00")
            parse_hhmm(a)
            parse_hhmm(b)
            p.quiet = f"{a.strip()}-{b.strip()}"
        else:
            p.quiet = None
    if digest is not None:
        slots = sorted({parse_hhmm(x).strftime("%H:%M") for x in digest if x.strip()})
        if len(slots) > 6:
            raise people.PeopleError("at most 6 digest times a day")
        p.digest = slots
    if digest_weekends is not None:
        p.digest_weekends = digest_weekends
    store.put(p)
    return p


def quiet_until(p: Person, t: float) -> float | None:
    """When the person's quiet window ends, if `t` is inside it; else None."""
    if not p.quiet:
        return None
    try:
        a, b = (parse_hhmm(x) for x in p.quiet.split("-", 1))
        tz = zone(p.tz)
    except (people.PeopleError, ValueError):
        return None
    local = datetime.fromtimestamp(t, tz)
    start = datetime.combine(local.date(), a, tzinfo=tz)
    end = datetime.combine(local.date(), b, tzinfo=tz)
    if a <= b:  # same day: 12:00-14:00
        return end.timestamp() if start <= local < end else None
    if local >= start:  # over midnight: 20:00-08:00, evening part
        return (end + timedelta(days=1)).timestamp()
    return end.timestamp() if local < end else None


# ====================================================================== the DM
def _age(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 86400}d" if s >= 86400 else f"{s // 3600}h" if s >= 3600 else f"{max(1, s // 60)}m"


def _link(url: str | None) -> str | None:
    return f"<{url}>" if url and _LINK_OK.match(url) else None


def _what(x: dict[str, Any]) -> str:
    from hangar.slack import escape

    ref = x.get("job") or x.get("session") or x.get("request")
    title = x.get("title")
    return escape(f"{title} ({ref})" if title and ref else str(ref or title or ""))


def dm_text(xs: list[dict[str, Any]]) -> str:
    """One item: its reason, where to answer, and the CLI fallback. A burst: one block per item."""
    from hangar.slack import escape

    def block(x: dict[str, Any]) -> list[str]:
        head = f"{escape(str(x['reason']))}" + (f" ({_what(x)})" if _what(x) else "")
        lines = [head]
        if link := _link(inbox.native(x)):
            lines.append(f"Answer here: {link}")
        lines.append(f"Or: `{escape(str(x['action']))}`")
        return lines

    if len(xs) == 1:
        return "\n".join(["*hangar* needs you: " + block(xs[0])[0], *block(xs[0])[1:]])
    out = [f"*hangar*: {len(xs)} things need you"]
    for x in xs:
        b = block(x)
        out += [f"• {b[0]}", *(f"   {ln}" for ln in b[1:])]
    return "\n".join(out)


async def _permalinks(svc: JobService, api: SlackAPI, xs: list[dict[str, Any]]) -> None:
    """Swap each item's thread link for Slack's own permalink (chat.getPermalink), cached in kv."""
    from hangar.slack import SlackError, parse_ref

    for x in xs:
        job = svc._jobs.get(x["job"]) if x.get("job") else None
        ref = inbox.thread_of(job) if job is not None else None
        where = parse_ref(ref or "")
        if where is None or ref is None:
            continue
        if not svc.store.kv_get(inbox.PERMALINK_PREFIX + ref):
            try:
                if url := await api.permalink(*where):
                    svc.store.kv_set(inbox.PERMALINK_PREFIX + ref, url)
            except SlackError as e:
                log.info("no permalink for %s: %s", ref, e)
        x.setdefault("links", {})["thread"] = inbox.thread_url(svc.store, ref)


async def send_dm(svc: JobService, user: str, text: str, api: SlackAPI | None = None) -> bool:
    """conversations.open, then chat.postMessage. False when Slack isn't configured or refused."""
    from hangar.slack import SlackError, client_for

    own = api is None
    api = api or client_for(svc)
    if api is None:
        return False
    try:
        channel = await api.open_dm(user)
        await api.post(channel, svc.broker.redact(text))
        return True
    except SlackError as e:
        log.warning("could not DM Slack user %s: %s", user, e)
        return False
    finally:
        if own:
            await api.close()


# ====================================================================== at once (#157)
class Notifier:
    """Watches job, session and request writes, and DMs each person their new questions and approvals."""

    def __init__(self, svc: JobService):
        self.svc = svc
        self.store = svc.store
        self.coalesce = COALESCE
        self.recheck = RECHECK
        self._dirty: set[str | None] = set()  # person ids; None: the team's (admins with --dm all)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        if self.on_put not in self.store.on_put:
            self.store.on_put.append(self.on_put)
        self._task = self._loop.create_task(self._run())

    async def stop(self) -> None:
        with contextlib.suppress(ValueError):
            self.store.on_put.remove(self.on_put)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    # --- producers (any thread)
    def on_put(self, obj: Any) -> None:
        if isinstance(obj, Job):
            self.mark(obj.person)
        elif isinstance(obj, Session):
            job = self.svc._jobs.get(obj.job_id) if obj.job_id else None
            self.mark(job.person if job is not None else obj.person)
        else:
            from hangar.envrequests import EnvRequest

            if isinstance(obj, EnvRequest):
                self.mark(obj.owner)

    def mark(self, person: str | None) -> None:
        with self._lock:
            self._dirty.add(person)
        loop, wake = self._loop, self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            wake.set()
        else:
            loop.call_soon_threadsafe(wake.set)

    # --- the loop
    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            woken = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.recheck)
                woken = True
            if woken:
                await asyncio.sleep(self.coalesce)  # gather the burst
            self._wake.clear()
            with self._lock:
                dirty, self._dirty = self._dirty, set()
            try:
                await self.deliver(None if not woken else dirty)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - next pass
                log.exception("inbox delivery pass failed")

    def targets(self, dirty: set[str | None] | None) -> list[Person]:
        """Who to look at: those whose records changed (None: everyone who gets DMs)."""
        out = []
        for p in people.list_people(self.store):
            if p.notify == "off" or slack_user(p) is None:
                continue
            if dirty is None or p.id in dirty or (None in dirty and p.role == "admin" and p.notify == "all"):
                out.append(p)
        return out

    async def deliver(self, dirty: set[str | None] | None = None, t: float | None = None) -> dict[str, int]:
        """One pass: DM each target their unsent questions and approvals. Returns {person: items sent}."""
        from hangar.slack import client_for

        targets = self.targets(dirty)
        if not targets:
            return {}
        api = client_for(self.svc)
        if api is None:
            return {}
        sent: dict[str, int] = {}
        try:
            for p in targets:
                if n := await self._deliver_one(p, api, t if t is not None else time.time()):
                    sent[p.id] = n
        finally:
            await api.close()
        return sent

    def _sent(self, person: str, item: str, t: float) -> bool:
        v = self.store.kv_get(f"{SENT_PREFIX}{person}:{item}")
        try:
            return v is not None and float(v) > t
        except ValueError:
            return False

    def _prune(self, person: str, t: float) -> None:
        for k in self.store.kv_keys(f"{SENT_PREFIX}{person}:"):
            with contextlib.suppress(ValueError, TypeError):
                if float(self.store.kv_get(k) or 0) <= t:
                    self.store.kv_delete(k)

    async def _deliver_one(self, p: Person, api: SlackAPI, t: float) -> int:
        user = slack_user(p)
        if p.notify == "off" or user is None:
            return 0
        xs = [x for x in inbox.items(self.svc, principal_for(p))
              if x["kind"] in DM_KINDS and not self._sent(p.id, x["id"], t)]
        if (end := quiet_until(p, t)) is not None:  # only what can't wait for the morning
            xs = [x for x in xs if x["kind"] == "approval" and x.get("expires_at") and x["expires_at"] < end]
        if not xs:
            return 0
        xs.sort(key=lambda x: x.get("since") or 0)
        await _permalinks(self.svc, api, xs)
        if not await send_dm(self.svc, user, dm_text(xs[:DIGEST_MAX]), api):
            return 0  # tried again on the next pass
        for x in xs[:DIGEST_MAX]:
            self.store.kv_set(f"{SENT_PREFIX}{p.id}:{x['id']}", f"{t + SENT_TTL:.0f}")
        self._prune(p.id, t)
        return min(len(xs), DIGEST_MAX)


def of(svc: JobService) -> Notifier:
    """The job service's notifier (made on first use)."""
    n = svc.__dict__.get("_inbox_notifier")
    if n is None:
        n = svc.__dict__["_inbox_notifier"] = Notifier(svc)
    return n


# ====================================================================== twice a day (#158)
def due_slot(p: Person, t: float, last: float | None) -> tuple[float | None, bool]:
    """(the digest slot due at `t`, whether to send it): the latest of the person's slots at or before `t` that
    is newer than `last`. Sent only if under DIGEST_LATE old; an older one (hangar was down) is skipped, but still
    returned so it is recorded. Weekend days have no slots unless `digest_weekends`."""
    if not p.digest:
        return None, False
    try:
        tz = zone(p.tz)
        times = [parse_hhmm(x) for x in p.digest]
    except people.PeopleError:
        return None, False
    today = datetime.fromtimestamp(t, tz).date()
    slot = None
    for back in range(8):
        day = today - timedelta(days=back)
        if day.weekday() >= 5 and not p.digest_weekends:
            continue
        past = [s for x in times if (s := datetime.combine(day, x, tzinfo=tz).timestamp()) <= t]
        if past:
            slot = max(past)
            break
    if slot is None or (last is not None and slot <= last):
        return None, False
    return slot, t - slot <= DIGEST_LATE


async def github_login(svc: JobService, p: Person) -> str | None:
    """The person's GitHub login, from their linked user id (GET /user/{id}, cached a week), or None."""
    uid = next((link.split(":", 1)[1] for link in p.links if link.startswith("github:")), None)
    if not uid or not uid.isdigit():
        return None
    cached = svc.store.kv_get_json(LOGIN_PREFIX + uid, None)
    if isinstance(cached, dict) and time.time() - float(cached.get("at") or 0) < LOGIN_TTL:
        return cached.get("login")
    gh = svc._github()
    if gh is None:
        return None
    try:
        user = await gh._req("GET", f"/user/{uid}")
        login = str((user or {}).get("login") or "") or None
    except Exception as e:  # noqa: BLE001 - the digest leaves the link out
        log.info("could not look up GitHub user %s: %s", uid, e)
        return None
    finally:
        await gh.close()
    if login and re.match(r"^[A-Za-z0-9-]{1,39}(\[bot\])?$", login):
        svc.store.kv_set_json(LOGIN_PREFIX + uid, {"login": login, "at": time.time()})
        return login
    return None


def review_url(login: str, slug: str | None) -> str:
    q = f"is:open+is:pr+review-requested:{login}" + (f"+author:app/{slug}" if slug else "")
    return f"https://github.com/pulls?q={q}"


async def digest(svc: JobService, p: Person, since: float | None = None, t: float | None = None) -> str | None:
    """The digest's text for a person, or None when there is nothing in it (an empty digest isn't sent)."""
    from hangar.slack import escape

    t = t if t is not None else time.time()
    since = since if since is not None else t - 86400
    xs = inbox.items(svc, principal_for(p))
    questions = sorted((x for x in xs if x["kind"] == "question"), key=lambda x: x.get("since") or 0)
    approvals = sorted((x for x in xs if x["kind"] == "approval"), key=lambda x: x.get("since") or 0)
    failed = [x for x in xs if x["kind"] == "failed" and (x.get("since") or 0) > since]
    landing, reviews = [], 0
    for x in xs:
        if x["kind"] != "review":
            continue
        job = svc._jobs.get(x.get("job") or "")
        if job is not None and job.landing.pr_state == "approved":
            landing.append((x, job))
        else:
            reviews += 1
    if not (questions or approvals or failed or landing or reviews):
        return None

    def link(x: dict[str, Any]) -> str:
        return f" {u}" if (u := _link(inbox.native(x))) else ""

    rows: list[tuple[str, str]] = []  # (section, line)
    for x in questions:
        rows.append(("Questions", f"{escape(str(x['reason']))} ({_what(x)}, {_age(t - (x.get('since') or t))} ago)"
                                  f"{link(x)}"))
    for x in approvals:
        exp = x.get("expires_at")
        due = f", expires in {_age(exp - t)}" if exp and exp > t else ""
        rows.append(("Approvals", f"{escape(str(x['reason']))} ({_what(x)}{due}){link(x)}"))
    for x in failed:
        rows.append(("Failed since the last digest", f"{escape(str(x['reason']))} ({_what(x)}){link(x)}"))
    for x, job in landing:
        rows.append(("Approved but not landing", f"{_what(x)}: {escape(job.landing.note or 'not landing')}"
                                                 f"{link(x)}"))
    lines = ["*hangar digest*"]
    if svc.fleet.power.off:  # the control plane is up; the pools may not be yet
        lines.append(f"_Sandboxes not up yet: {escape(svc.fleet.power.status_line())}._")
    section = None
    for name, line in rows[:DIGEST_MAX]:
        if name != section:
            lines.append(f"*{name}*")
            section = name
        lines.append(f"• {line}")
    if len(rows) > DIGEST_MAX:
        lines.append(f"…and {len(rows) - DIGEST_MAX} more: `hangar inbox`")
    if reviews:
        login = await github_login(svc, p)
        where = f": <{review_url(login, svc.settings.github_app_slug)}>" if login else " on GitHub"
        lines.append(f"{reviews} agent PR{'s' if reviews != 1 else ''} await{'' if reviews != 1 else 's'} your "
                     f"review{where}")
    # footer: open agent PRs against the review-capacity cap (#159, when configured) and jobs held for capacity
    from hangar import fairness  # --- review capacity (#159): the same counts admission uses

    view = fairness.capacity_view(svc, p.id)
    open_prs, cap, held = view["open_agent_prs"], view["cap"], view["awaiting_capacity"]
    foot = f"Open agent PRs: {open_prs}" + (f" of {cap}" if isinstance(cap, int) and cap > 0 else "")
    if held:
        foot += f"; {held} job{'s' if held != 1 else ''} held for review capacity"
    lines.append(f"_{foot}._")
    return "\n".join(lines)


async def digest_tick(h: Any, t: float | None = None) -> dict[str, str]:
    """The ops loop's minute: send each person's digest slot when it is due. Returns {person: what happened}."""
    svc = h.jobs
    t = t if t is not None else time.time()
    out: dict[str, str] = {}
    for p in people.list_people(svc.store):
        if p.notify == "off" or not p.digest or (user := slack_user(p)) is None:
            continue
        key = DIGEST_PREFIX + p.id
        last = svc.store.kv_get_json(key, None)
        slot, send = due_slot(p, t, float(last) if isinstance(last, int | float) else None)
        if slot is None:
            continue
        if send:
            text = await digest(svc, p, since=float(last) if isinstance(last, int | float) else None, t=t)
            if text is not None and not await send_dm(svc, user, text):
                out[p.id] = "failed"  # tried again next minute, until the slot is too late
                continue
            out[p.id] = "sent" if text is not None else "empty"
        else:
            out[p.id] = "skipped"
        svc.store.kv_set_json(key, slot)
    return out


# ====================================================================== wiring, REST, doctor
class DeliveryBody(BaseModel):
    dm: str | None = Field(None, max_length=20)
    quiet: str | None = Field(None, max_length=20)
    tz: str | None = Field(None, max_length=64)
    digest: list[str] | None = Field(None, max_length=6)
    digest_weekends: bool | None = None


def register(h: Any) -> None:
    """The digest rides the ops loop (opstatus.MINUTELY), once."""
    from hangar import opstatus

    if digest_tick not in opstatus.MINUTELY:
        opstatus.MINUTELY.append(digest_tick)


def mount(app: Any, h: Any, person_actor: Any) -> None:
    """PUT /api/people/{id}/delivery: how the inbox reaches someone (themselves, or an admin)."""
    from fastapi import HTTPException

    from hangar import identity

    @app.put("/api/people/{person_id}/delivery")
    async def set_person_delivery(person_id: str, body: DeliveryBody, who: str = person_actor):
        me = identity.principal()
        if me is not None and not me.admin and me.person != person_id:
            raise HTTPException(403, "you can change only your own inbox delivery")
        p = set_delivery(h.store, person_id, dm=body.dm, quiet=body.quiet, tz=body.tz, digest=body.digest,
                         digest_weekends=body.digest_weekends)
        h.fleet.audit(who, "person.delivery", person_id, **body.model_dump(exclude_none=True))
        return p.model_dump(mode="json")


def doctor_checks(h: Any) -> list[dict[str, Any]]:
    """With the Slack app configured: who the inbox can't reach (no linked Slack account)."""
    from hangar.doctor import _check

    if not h.settings.slack_bot_token_credential:
        return []
    missing = [p.id for p in people.list_people(h.store)
               if p.notify != "off" and slack_user(p) is None and (p.id != people.OWNER or h.store.get(Person, p.id))]
    if not missing:
        return [_check("inbox delivery", True, "everyone who gets DMs has a linked Slack account")]
    return [_check("inbox delivery", None, f"inbox not delivered (no linked Slack account): {', '.join(missing)}",
                   "hangar person link <id> slack TEAM/USER, or hangar person set <id> --dm off")]

