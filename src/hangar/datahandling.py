"""Data handling for a company install (KAN-296; docs/security/data-handling.md).

* **The audit chain.** store.py links each audit row to the one before it (`hash`), keyed with a secret in the vault.
  `verify_chain` walks the table and finds the first row that was edited, removed or reordered; `hangar doctor`
  runs it. The anchor that retention keeps starts the chain where the deleted rows ended, and an `audit.pruned` entry
  in the chain carries it.
* **Audit retention.** HANGAR_AUDIT_RETENTION_DAYS (400 by default, 0 keeps everything) deletes the oldest rows,
  separately from job and session retention (retention.py runs `prune_audit`).
* **Purge.** `hangar admin purge --person ID` deletes what hangar holds about one person: their sessions (with their
  checkpoints, artifacts, manifests and event logs), jobs, schedules, outbox records, personal credentials, tokens,
  sandbox records, snapshots, flight records, SSO binding and spend-alert marks, and the person. Their tokens go and
  their accounts are unlinked first, so nothing new starts as them. Their audit entries stay, with every name hangar
  knows them by replaced by one stable pseudonym, in the fields that name people only. It refuses while any of their
  work is live, and refuses the owner and admins.
* **Export.** `hangar admin export --job ID`: one tar.gz with the job's record, instructions, context, events,
  result, its sessions' event logs and artifacts, and every audit entry naming the job, its sessions or sandboxes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

from hangar import branchowners, people
from hangar.auditchain import audit_digest, audit_link, audit_redaction
from hangar.envrequests import EnvRequest
from hangar.flightrec import FlightRecord
from hangar.models import (
    JOB_TERMINAL,
    SESSION_LIVE,
    ApiToken,
    Credential,
    Job,
    ModelGrant,
    Person,
    Sandbox,
    SandboxGrant,
    SandboxState,
    ScheduledJob,
    Session,
    Snapshot,
    now,
)
from hangar.outbox import OutboxItem
from hangar.store import AUDIT_ANCHOR, AUDIT_PRUNED, CHAIN_STARTED, Store
from hangar.testresults import TestRun

log = logging.getLogger("hangar.datahandling")

PURGE_ACTION = "admin.purge"
SALT_NAME = "audit-pseudonym-salt"  # in the vault: the database alone can't confirm a guessed name
CHAIN_KEY_NAME = "audit-chain-key"  # in the vault: the database alone can't rebuild the chain after an edit
_BOUND = r"A-Za-z0-9._@/#-"  # a name is replaced only where it stands alone, never inside a longer word or a path
# Detail keys whose values name people: a name is replaced inside them, as in `actor` and `target`. Anywhere else in
# `detail`, only a value that is the name (or `<kind>:<name>`) as a whole is.
IDENTITY_KEYS = frozenset({"person", "people", "by", "requester", "requested_by", "owner", "user", "users", "actor",
                           "who", "member", "members", "login", "email", "author", "approver", "approved_by",
                           "decided_by", "sender", "linked", "link", "links", "account", "created_by", "leased_to"})
# Names too common to replace inside a longer string: they'd hit other people's entries ("admin", a branch "main").
COMMON = frozenset({"admin", "system", "owner", "hangar", "api", "main", "master", "root", "bot", "ci", "test", "user",
                    "team", "ui", "cli", "token", "person", "github", "slack", "jira", "web", "default"})
_LIVE_SANDBOX = {SandboxState.PENDING, SandboxState.STARTING, SandboxState.RUNNING, SandboxState.PAUSED,
                 SandboxState.STOPPING, SandboxState.ERROR}  # these still hold provider resources: destroy first


class PurgeBlocked(RuntimeError):
    """The person still has live work: stop it first. `message` is the plain answer the API gives."""

    def __init__(self, person_id: str, live: list[str]):
        self.message = f"{person_id} still has live work; stop it first: {', '.join(live[:20])}"
        super().__init__(self.message)


# ---------------------------------------------------------------------------- the audit chain
def audit_key(vault: Any) -> bytes:
    """The audit chain's HMAC key, made on first use and kept in the vault."""
    raw = vault.get_text(CHAIN_KEY_NAME)
    if not raw:
        raw = secrets.token_hex(32)
        vault.put(CHAIN_KEY_NAME, raw)
    return bytes.fromhex(raw)


def verify_chain(store: Store, batch: int = 5000) -> dict[str, Any]:
    """Walk the audit table oldest first. `ok` is False at the first row whose link doesn't follow from the row
    before it (a row removed, reordered or inserted), whose JSON doesn't match its digest (edited), that has no
    link, or that is marked redacted but doesn't match what the purge wrote. It is False too when the chain starts
    from an anchor the newest `audit.pruned` entry doesn't carry (rows cut from the front). `restarts` lists chain
    starts after the first. What this can't see: rows cut from the end, or a table reset to before the chain. The
    copy off the pod is the record for those, and bucket_check compares with it (KAN-541,
    docs/security/data-handling.md#what-doctor-proves)."""
    key = store.audit_key
    anchor = json.loads(store.kv_get(AUDIT_ANCHOR) or "null")
    prev = anchor["hash"] if anchor else ""
    out: dict[str, Any] = {"ok": key is not None, "rows": 0, "redacted": 0, "first_id": None, "last_id": None,
                           "broken_at": None, "reason": None if key else "the chain's key isn't loaded",
                           "anchor": None, "starts": 0, "restarts": []}
    if key is None:
        return out
    vouched = None  # the anchor the newest audit.pruned entry carries
    after = 0
    while True:
        rows = store.audit_rows(after, batch)
        if not rows:
            break
        for rid, _ts, data, digest, link, redacted, rdigest in rows:
            out["rows"] += 1
            out["first_id"] = out["first_id"] or rid
            out["last_id"] = rid
            data = data or ""
            if redacted:
                out["redacted"] += 1
            if out["ok"]:
                reason = None
                if not link or not digest:
                    reason = "a row without a link (written outside hangar, or its link was cleared)"
                elif redacted and (not rdigest
                                   or not hmac.compare_digest(audit_redaction(key, rid, digest, data), rdigest)):
                    reason = "a row marked redacted doesn't match what the purge wrote (edited since)"
                elif not redacted and audit_digest(key, data) != digest:
                    reason = "the row was edited after it was written"
                elif audit_link(key, prev, rid, digest) != link:
                    reason = "the row doesn't follow the one before it (a row was removed, inserted or reordered)"
                if reason:
                    out.update(ok=False, broken_at=rid, reason=reason)
            prev = link or ""
            try:
                row = json.loads(data)
                action, detail = row.get("action"), row.get("detail") or {}
            except (ValueError, AttributeError):
                continue
            if action == AUDIT_PRUNED and isinstance(detail, dict):
                vouched = detail.get("anchor")
            elif action == CHAIN_STARTED:
                out["starts"] += 1
                if out["starts"] > 1:  # the first links a table hangar never chained; any other relinked rows
                    out["restarts"].append(rid)
        after = rows[-1][0]
    if anchor != vouched and out["ok"]:
        out.update(ok=False, broken_at=out["first_id"],
                   reason="the chain starts from an anchor no prune entry records (rows were deleted from the "
                          "front, outside retention)" if anchor else
                          "a prune entry records an anchor the chain doesn't start from")
    out["anchor"] = vouched if out["ok"] else anchor
    return out


def prune_audit(store: Store, days: float, batch: int = 1000) -> int:
    """Delete the audit rows older than `days`, a batch at a time. Returns how many went."""
    if days <= 0:
        return 0
    before, gone = now() - days * 86400, 0
    while True:
        n = store.audit_prune(before, batch)
        gone += n
        if n < batch:
            return gone


async def doctor_checks(h: Any) -> list[dict[str, Any]]:
    from hangar.doctor import _check

    v = await asyncio.to_thread(verify_chain, h.store)
    days = h.settings.audit_retention_days
    out = []
    if v["ok"]:
        out.append(_check("audit chain", True, f"{v['rows']} entries, every link holds"
                          + (f"; {v['redacted']} redacted by a purge" if v["redacted"] else "")))
    else:
        out.append(_check("audit chain", False, f"entry {v['broken_at']}: {v['reason']}",
                          "compare the table with the copy off the pod (docs/security/audit.md#reading-it), then "
                          "find out who had the database: docs/security/data-handling.md#if-the-chain-breaks"))
    if v["restarts"]:
        out.append(_check("audit chain restarts", False,
                          f"the chain was started again at entry {', '.join(map(str, v['restarts'][:5]))}: rows "
                          "whose links were cleared were linked anew",
                          "compare the entries before it with the copy off the pod "
                          "(docs/security/data-handling.md#if-the-chain-breaks)"))
    anchor = v["anchor"]
    if days <= 0:
        out.append(_check("audit retention", None, "kept forever (HANGAR_AUDIT_RETENTION_DAYS=0)",
                          "set HANGAR_AUDIT_RETENTION_DAYS to what your policy asks for "
                          "(docs/security/data-handling.md)"))
    elif anchor and anchor.get("ts", 0) > now() - days * 86400:
        out.append(_check("audit retention", None,
                          f"entries up to {time.strftime('%Y-%m-%d', time.gmtime(anchor['ts']))} were deleted, sooner "
                          f"than the {days:g} days kept now",
                          "expected if the retention was raised since; otherwise rows were deleted early"))
    else:
        out.append(_check("audit retention", True, f"{days:g} days, then deleted oldest first"
                          + (f" ({anchor['pruned']} so far)" if anchor else "")))
    out.append(await bucket_check(h))  # --- KAN-541: the end of the table, against the copy off the pod
    return out


# ---------------------------------------------------------------------------- the bucket (KAN-541)
# What the chain can't show (rows cut from the end, a table reset or restored from an older backup) shows against the
# locked audit bucket: its newest `hangar.audit:` line carries a `seq` the table must reach and a `hash` the table's
# row must still have. hangar reads the bucket's view with roles/logging.viewAccessor on that view alone; it can't
# write to the bucket or change the sink.
_VIEW = re.compile(r"projects/[a-z][-a-z0-9]{4,28}[a-z0-9]/locations/[a-z0-9-]{1,40}/buckets/[A-Za-z0-9_-]{1,100}"
                   r"/views/[A-Za-z0-9_-]{1,100}")
_ENTRIES = "https://logging.googleapis.com/v2/entries:list"
_LINE = "hangar.audit: "
# the line as `hangar serve` logs it: "<date> <time> INFO hangar.audit: {...}" (cli.py), or bare. Text that merely
# contains it, inside another logger's message, isn't an audit line.
_LINE_RE = re.compile(r"^(?:\S+ \S+ [A-Z]+ )?hangar\.audit: (?=\{)")
_PAGE = 500
_WINDOW_DAYS = 30  # every line of the last 30 days, up to _MAX_LINES: a restore written past shows as two links
_MAX_LINES = 5000
_MAX_PAGES = 20  # per window: empty pages with a token can go on, and each call may take up to 20 s
_READ_SECONDS = 30.0  # the whole read; past it the line is not run, so doctor never hangs on Logging
_FIX_BEHIND = ("treat the bucket as the record: find out who had the database, and what was restored "
               "(docs/security/data-handling.md#if-the-chain-breaks)")


async def _token() -> str | None:
    from hangar.doctor import _metadata_token

    return await _metadata_token()


async def _list_entries(view: str, token: str, days: float, page_token: str | None = None,
                        page_size: int = _PAGE) -> tuple[int, dict[str, Any]]:
    """One page of the bucket's audit lines of the last `days`, newest first."""
    import httpx

    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now() - days * 86400))
    body: dict[str, Any] = {"resourceNames": [view], "filter": f'textPayload:"{_LINE}{{" AND timestamp>="{since}"',
                            "orderBy": "timestamp desc", "pageSize": page_size}
    if page_token:
        body["pageToken"] = page_token
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(_ENTRIES, json=body, headers={"Authorization": f"Bearer {token}"})
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data if isinstance(data, dict) else {}


async def _read_bucket(view: str, token: str) -> tuple[list[tuple[int, str | None]], str | None]:
    """The audit lines of the last 30 days (up to _MAX_LINES), else the newest one ever: (lines, why not read)."""
    gave_up = False
    for days, cap in ((_WINDOW_DAYS, _MAX_LINES), (3650, 1)):
        lines: list[tuple[int, str | None]] = []
        page: str | None = None
        for _ in range(_MAX_PAGES):  # a page may come back empty with a token when the search ran out of time
            code, data = await _list_entries(view, token, days, page)
            if code != 200:
                return [], f"HTTP {code}"
            lines += _bucket_lines(data)
            page = data.get("nextPageToken") or None
            if not page or len(lines) >= cap:
                break
        else:
            gave_up = bool(page)
        if lines:
            return lines, None
    return [], (f"no audit line in {_MAX_PAGES} pages" if gave_up else None)


def _bucket_lines(data: dict[str, Any]) -> list[tuple[int, str | None]]:
    """(seq, hash) of each audit line in an entries:list answer."""
    out = []
    for e in data.get("entries") or []:
        text = str(e.get("textPayload") or "").strip() if isinstance(e, dict) else ""
        m = _LINE_RE.match(text)
        if not m:
            continue
        try:
            row, end = json.JSONDecoder().raw_decode(text, m.end())
            seq = row["seq"]
        except (ValueError, KeyError, TypeError):
            continue
        if text[end:].strip() or not {"actor", "action"} <= row.keys():
            continue
        if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0:
            link = row.get("hash")
            out.append((seq, link if isinstance(link, str) else None))
    return out


def compare_with_bucket(store: Store, lines: list[tuple[int, str | None]]) -> tuple[bool, str]:
    """Whether the table reaches the bucket's newest entry and holds every entry the bucket read shows as the
    bucket has it. Entries retention deleted from the table since are skipped."""
    seq = max(s for s, _ in lines)
    newest = store.audit_max_id()
    if newest < seq:
        return False, (f"the table's newest entry is {newest}, the bucket's is {seq}: entries were cut from the end, "
                       "or the table was reset or restored from an older backup")
    links: dict[int, str] = {}
    for s, h in lines:
        if h is None:
            continue
        if links.setdefault(s, h) != h:
            return False, (f"the bucket holds entry {s} twice with different links: the table was restored or reset, "
                           "then written past")
    first = store.audit_rows(0, 1)
    kept = sorted(s for s in links if first and s >= first[0][0])
    table: dict[int, str | None] = {}
    after = kept[0] - 1 if kept else newest
    while kept and after < kept[-1]:
        rows = store.audit_rows(after, 5000)
        if not rows:
            break
        table.update((r[0], r[4]) for r in rows)
        after = rows[-1][0]
    for s in kept:
        if s not in table:
            return False, f"entry {s}, which the bucket holds, isn't in the table"
        if table[s] != links[s]:
            return False, (f"entry {s}'s link isn't the bucket's: the table was restored, reset or relinked, then "
                           "written past")
    return True, (f"the table reaches the bucket's newest entry, {seq}"
                  + (f", and the {len(kept)} entries read from the bucket match it" if kept else
                     " (no line read carries a link to compare)"))


async def bucket_check(h: Any) -> dict[str, Any]:
    from hangar.doctor import _check

    view = (h.settings.audit_bucket or "").strip()
    if not view:
        return _check("audit bucket", None, "not run: no audit bucket is set (HANGAR_AUDIT_BUCKET), so rows cut from "
                      "the end of the table or a restored table go unseen",
                      "set enable_audit_export, then HANGAR_AUDIT_BUCKET to the Terraform output audit_view "
                      "(docs/security/audit.md#doctors-comparison)")
    fix = ("grant hangar's service account roles/logging.viewAccessor on that view (the Terraform audit export does), "
           "and check HANGAR_AUDIT_BUCKET")
    if not _VIEW.fullmatch(view):
        return _check("audit bucket", None, "not run: HANGAR_AUDIT_BUCKET isn't a log view "
                      "(projects/P/locations/global/buckets/B/views/V)", fix)
    token = await _token()
    if not token:
        return _check("audit bucket", None, "not run: hangar has no Google credentials to read the bucket with", fix)
    try:
        lines, why = await asyncio.wait_for(_read_bucket(view, token), _READ_SECONDS)
    except TimeoutError:
        return _check("audit bucket", None, "not run: the bucket read timed out", fix)
    except Exception as e:  # noqa: BLE001 - not read is not run, never a pass
        return _check("audit bucket", None, f"not run: the bucket wasn't read ({type(e).__name__})", fix)
    if why:
        return _check("audit bucket", None, f"not run: the bucket wasn't read ({why})", fix)
    if not lines:
        return _check("audit bucket", None, "not run: no audit line was found in the bucket", fix)
    try:
        ok, detail = await asyncio.to_thread(compare_with_bucket, h.store, lines)
    except Exception as e:  # noqa: BLE001 - a comparison that breaks says so; it never takes doctor down
        return _check("audit bucket", None, f"not run: the comparison failed ({type(e).__name__})", fix)
    return _check("audit bucket", ok, detail, _FIX_BEHIND)


# ---------------------------------------------------------------------------- purge
def pseudonym(h: Any, person_id: str) -> str:
    """The stable stand-in for a person in the audit table: the same for every entry and every purge, and unguessable
    from the database alone (its salt is in the vault)."""
    salt = h.vault.get_text(SALT_NAME)
    if not salt:
        salt = secrets.token_hex(32)
        h.vault.put(SALT_NAME, salt)
    return "person:" + hashlib.sha256(f"{salt}\n{person_id}".encode()).hexdigest()[:16]


# --- KAN-553: owner and requester values hangar itself writes (sessions.py, models.py defaults): nobody's names
FIXED_OWNERS = frozenset({"admin", "system", "jobs", "tower", "hangar"})


def aliases(store: Store, person_id: str) -> tuple[list[str], list[str]]:
    """The names the audit table may know the person by: their id, their tokens' ids, their linked accounts (and an
    email address bare), and `ui:<token name>` for a token name no other person's token has. Second, their display
    name, replaced only in an entry that names them otherwise, and only where a value is that name as a whole."""
    names = {person_id}
    p = store.get(Person, person_id)
    display: list[str] = []
    if p is not None:
        names.update(p.links)
        names.update(link.split(":", 1)[1] for link in p.links if link.startswith("email:"))
        others = {o.name for o in store.list(Person) if o.id != person_id}
        if p.name and p.name != person_id and p.name not in others:
            display.append(p.name)
    tokens = store.list(ApiToken)
    theirs = [t for t in tokens if t.person == person_id]
    names.update(t.id for t in theirs)
    others = {t.name for t in tokens if t.person != person_id}
    names.update(f"ui:{t.name}" for t in theirs if t.name not in others)
    return sorted((n for n in names if n), key=len, reverse=True), display


def redactor(names: list[str], stand_in: str, display: list[str] | None = None):
    """A rewrite of one audit row's JSON (store.audit_redact). `actor`, `target` and the detail values under
    IDENTITY_KEYS lose every name where it stands alone; any other detail value only when it is a name as a whole
    (or `<kind>:<name>`). A short or common name (COMMON) is replaced only as a whole value, never inside one."""
    names = sorted({n for n in names if n}, key=len, reverse=True)  # the longest name first: "github:x" over "x"
    if not names:
        return lambda data: data
    inner = [n for n in names if len(n) >= 4 and n.lower() not in COMMON]
    alt = "|".join(re.escape(n) for n in names)
    plain = "|".join(re.escape(n) for n in names if n.lower() not in COMMON) or "(?!)"
    common = "|".join(re.escape(n) for n in names if n.lower() in COMMON) or "(?!)"
    # a common name ("admin") counts only with a kind in front ("person:admin"): bare, it may be anyone's
    whole = re.compile(rf"(?:[a-z][a-z0-9_-]*:)*(?:{plain})|(?:[a-z][a-z0-9_-]*:)+(?:{common})")
    within = re.compile(rf"(?<![{_BOUND}])(?:{'|'.join(re.escape(n) for n in inner)})(?![{_BOUND}])") \
        if inner else None
    shown = set(display or [])
    hit = [False]

    def one(v: str, identity: bool) -> str:
        if (m := whole.fullmatch(v)) is not None:
            hit[0] = True
            prefix = v[:m.end() - len(next(n for n in names if v.endswith(n)))]
            return prefix + stand_in
        if identity and within is not None and within.search(v):
            hit[0] = True
            return within.sub(stand_in, v)
        return v

    def walk(v: Any, identity: bool) -> Any:
        if isinstance(v, str):
            return one(v, identity)
        if isinstance(v, list):
            return [walk(x, identity) for x in v]
        if isinstance(v, dict):
            return {k: walk(x, identity or k in IDENTITY_KEYS) for k, x in v.items()}
        return v

    def drop_display(v: Any) -> Any:
        if isinstance(v, str):
            return stand_in if v in shown else v
        if isinstance(v, list):
            return [drop_display(x) for x in v]
        if isinstance(v, dict):
            return {k: drop_display(x) for k, x in v.items()}
        return v

    quick = re.compile(alt)

    def rewrite(data: str) -> str:
        if not quick.search(data):
            return data
        try:
            row = json.loads(data)
        except ValueError:
            return data  # not an entry hangar wrote; left for verify_chain to report
        if not isinstance(row, dict):
            return data
        hit[0] = False
        for f in ("actor", "target"):
            if isinstance(row.get(f), str):
                row[f] = one(row[f], True)
        row["detail"] = walk(row.get("detail") or {}, False)
        if not hit[0]:
            return data
        if shown:
            row["detail"] = drop_display(row["detail"])
        return json.dumps(row, separators=(",", ":"), ensure_ascii=False)

    return rewrite


def _found(h: Any, person_id: str, names: list[str]) -> dict[str, list[Any]]:
    """What hangar holds about the person: by `person`, and by an `owner` or requester that is one of their names
    (a token id, a linked account), as GitHub, Slack and older jobs and sessions record it."""
    st = h.store
    # --- KAN-553: hangar writes fixed strings as owner and requester (a job's session is owned by "jobs", defaults are
    # "admin" and "system"), so a person by such an id must not match them; and a record whose `person` names someone
    # else is never theirs, whatever its owner says
    mine = set(names) - FIXED_OWNERS

    def unclaimed(rec: Any) -> bool:
        return getattr(rec, "person", None) in (None, person_id)

    def theirs(rec: Any, *fields: str) -> bool:
        return unclaimed(rec) and any(getattr(rec, f, None) in mine for f in fields)

    def by(cls: type, field: str) -> list[Any]:
        return [r for r in st.list_where(cls, archived=True, **{field: sorted(mine)}) if unclaimed(r)] if mine else []

    sessions = {s.id: s for s in st.list_where(Session, archived=True, person=person_id)}
    sessions.update({s.id: s for s in by(Session, "owner")})
    jobs = {j.id: j for j in st.list_where(Job, archived=True, person=person_id)}
    jobs.update({j.id: j for j in by(Job, "owner") + by(Job, "requester")})
    schedules = {sc.id: sc for sc in st.list(ScheduledJob) if sc.person == person_id or theirs(sc, "owner")}
    sids = set(sessions) | {a.session_id for j in jobs.values() for a in j.attempts if a.session_id} \
        | {j.session_id for j in jobs.values() if j.session_id}
    jids = set(jobs)
    sandboxes = {sb.id: sb for sb in st.list_where(Sandbox, archived=True, person=person_id)}
    sandboxes.update({sb.id: sb for sb in by(Sandbox, "created_by")})
    sbids = set(sandboxes)
    outbox = [i for i in st.list(OutboxItem) if i.session_id in sids or i.job_id in jids]
    return {
        "sessions": list(sessions.values()), "jobs": list(jobs.values()), "schedules": list(schedules.values()),
        "credentials": st.list_where(Credential, person=person_id),
        "tokens": st.list_where(ApiToken, person=person_id), "sandboxes": list(sandboxes.values()),
        "snapshots": st.list_where(Snapshot, person=person_id),
        "sandbox_grants": [g for g in st.list(SandboxGrant) if g.sandbox_id in sbids],
        "model_grants": [g for g in st.list(ModelGrant) if g.sandbox_id in sbids],
        "flight_records": [r for r in st.list(FlightRecord)
                           if r.person == person_id or r.job_id in jids or r.session_id in sids],
        "test_runs": [r for r in st.list(TestRun) if r.person == person_id or r.job_id in jids
                      or r.session_id in sids],
        "env_requests": [r for r in st.list(EnvRequest) if r.session_id in sids or theirs(r, "owner", "requester")],
        "outbox": outbox,
        "environment_claims": [k for k in st.kv_keys("identity:env-owner:") if st.kv_get(k) == person_id],
        "kv": _their_kv(st, person_id),
        "branch_creators": branchowners.recorded_for(st, person_id),
        "pending_deletes": _pending_deletes(h, sids, mine),
    }


def _their_kv(st: Store, person_id: str) -> list[str]:
    """Other records keyed by the person: an SSO binding (`sso:binding:<id>`, with their email, provider subject and
    a sealed refresh token) and their sign-ins in flight (sso.py, KAN-294), and the marks of their daily cap's spend
    alerts (`spendalert:<day>:person:<id>:<usd>:<pct>`, spendalert.py, KAN-290)."""
    keys = [k for k in st.kv_keys("sso:binding:") if k == f"sso:binding:{person_id}"]
    for prefix in ("sso:pending:", "sso:confirm:", "sso:handoff:"):
        for k in st.kv_keys(prefix):
            try:
                v = json.loads(st.kv_get(k) or "null")
            except ValueError:
                continue
            if isinstance(v, dict) and v.get("person") == person_id:
                keys.append(k)
    for k in st.kv_keys("spendalert:"):
        parts = k.split(":")  # spendalert, day, scope, name (may hold ":"), usd, pct
        if len(parts) >= 6 and parts[2] == "person" and ":".join(parts[3:-2]) == person_id:
            keys.append(k)
    return keys


def _pending_deletes(h: Any, sids: set[str], names: set[str]) -> list[Path]:
    """Sessions deleted earlier whose event logs and manifests wait for the outbox (outbox.retire_session)."""
    root = h.settings.data_dir / "pending-delete"
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        try:
            m = json.loads((d / "marker.json").read_text())
        except (OSError, ValueError):
            continue
        if m.get("session_id") in sids or m.get("person") in names or m.get("requester") in names:
            out.append(d)
    return out


def _live(found: dict[str, list[Any]]) -> list[str]:
    live = [f"session {s.name or s.id}" for s in found["sessions"] if s.state in SESSION_LIVE or s.sandbox_id]
    live += [f"job {j.id}" for j in found["jobs"] if j.state not in JOB_TERMINAL]
    live += [f"sandbox {s.id}" for s in found["sandboxes"] if s.state in _LIVE_SANDBOX]
    live += [f"credential {c.name} (held by {c.leased_to})" for c in found["credentials"] if c.leased_to]
    return live


PURGE_NAMES = "purge-names:"  # in the vault while a purge runs: a rerun still knows the names it shut out


def _purge_names(h: Any, person_id: str) -> tuple[list[str], list[str]]:
    """The person's names (aliases), with any a purge of them that stopped partway had already shut out."""
    names, display = aliases(h.store, person_id)
    try:
        kept = json.loads(h.vault.get_text(PURGE_NAMES + person_id) or "{}")
    except ValueError:
        kept = {}
    names = sorted(set(names) | set(kept.get("names", [])), key=len, reverse=True)
    display = sorted(set(display) | set(kept.get("display", [])))
    return names, display


async def purge(h: Any, person_id: str, requester: str, dry_run: bool = False) -> dict[str, Any]:
    """Delete everything hangar holds about `person_id` and pseudonymise their audit entries (module docstring).

    Their doors close first: their tokens go and their linked accounts are unlinked, so nothing new can start as
    them, and what they hold is found again after that. The audit rows are rewritten next, and the person's record
    goes last, so a purge that fails partway leaves no name behind and can be run again: the rerun finds the
    person, the names it shut out (kept sealed in the vault) and whatever is left."""
    people.check_id(person_id)
    if person_id == people.OWNER:
        raise people.PeopleError("the owner holds the server's admin token; it can't be purged")
    st = h.store
    person = st.get(Person, person_id)
    if person is not None and person.role == "admin":
        raise people.PeopleError(f"{person_id} is an admin: make them a member first "
                                 f"(PATCH /api/people/{person_id} with role member), then purge")
    names, display = await asyncio.to_thread(_purge_names, h, person_id)
    found = await asyncio.to_thread(_found, h, person_id, names)
    counts = {k: len(v) for k, v in found.items()}
    named = await asyncio.to_thread(st.audit_matching, names, 1)
    if person is None and not any(counts.values()) and not named:
        raise KeyError(f"hangar holds nothing about '{person_id}'")
    if live := _live(found):
        raise PurgeBlocked(person_id, live)
    if dry_run:
        return {"person": person_id, "dry_run": True, "deleted": counts, "person_record": person is not None,
                "exported_objects": sorted(i.key for i in found["outbox"] if i.state == "done"), "errors": [],
                "audit_names": len(names)}

    # close their doors: no token of theirs and no linked account can start anything while the rest goes
    await asyncio.to_thread(h.vault.put, PURGE_NAMES + person_id, json.dumps({"names": names, "display": display}))
    for t in found["tokens"]:
        st.delete(ApiToken, t.id)
    if person is not None and person.links:
        person.links = []
        st.put(person)
    found = await asyncio.to_thread(_found, h, person_id, names)  # what they started before the doors shut
    if live := _live(found):
        raise PurgeBlocked(person_id, live)
    counts = {k: len(v) for k, v in found.items()}
    exported = sorted(i.key for i in found["outbox"] if i.state == "done")
    report: dict[str, Any] = {"person": person_id, "dry_run": False, "deleted": counts,
                              "person_record": person is not None, "exported_objects": exported, "errors": []}

    stand_in = await asyncio.to_thread(pseudonym, h, person_id)
    rewrite = redactor(names, stand_in, display)
    start = await asyncio.to_thread(st.audit_max_id)
    changed = await asyncio.to_thread(st.audit_redact, rewrite)  # first: a failure below leaves no name behind
    errors = report["errors"]

    async def step(what: str, fn: Any, *a: Any, **kw: Any) -> None:
        try:
            r = fn(*a, **kw)
            if asyncio.iscoroutine(r):
                await r
        except Exception as e:  # noqa: BLE001 - report it, go on with the rest, keep the person for a rerun
            errors.append(f"{what}: {e}")

    for t in found["tokens"]:
        await step(f"token {t.id}", st.delete, ApiToken, t.id)
    for sc in found["schedules"]:
        await step(f"schedule {sc.id}", st.delete, ScheduledJob, sc.id)
    for j in found["jobs"]:
        h.jobs._jobs.pop(j.id, None)
        await step(f"job {j.id}", st.delete, Job, j.id)
    for s in found["sessions"]:
        try:
            h.sessions.delete(s.id, requester, force_now=True)
            await h.sessions._drop(f"{s.id}/")  # checkpoints and artifacts: gone before this returns
        except Exception as e:  # noqa: BLE001
            errors.append(f"session {s.id}: {e}")
            await step(f"session {s.id}", st.delete, Session, s.id)
    for d in found["pending_deletes"]:
        await step(f"deleted session files {d.name}", shutil.rmtree, d)
    ob = _outbox(h)
    for i in found["outbox"]:
        if ob is not None:
            await step(f"outbox file {i.key}", ob._blob(i.key).unlink, missing_ok=True)
        await step(f"outbox record {i.key}", st.delete, OutboxItem, i.key)
    for c in found["credentials"]:
        await step(f"credential {c.name}", h.broker.delete, c.name)
    for snap in found["snapshots"]:
        try:
            await h.fleet.delete_snapshot(snap.id, actor=requester)
        except Exception as e:  # noqa: BLE001
            errors.append(f"snapshot {snap.id}: {e}")
            await step(f"snapshot {snap.id}", st.delete, Snapshot, snap.id)
    for kind, cls in (("sandbox_grants", SandboxGrant), ("model_grants", ModelGrant), ("sandboxes", Sandbox),
                      ("flight_records", FlightRecord), ("test_runs", TestRun), ("env_requests", EnvRequest)):
        for rec in found[kind]:
            await step(f"{kind} {rec.id}", st.delete, cls, rec.id)
    for k in found["environment_claims"] + found["kv"]:
        await step(k, st.kv_delete, k)
    for k in found["branch_creators"]:  # --- KAN-553: kept, as nobody's, so the branch stays foreign to everyone
        await step(k, branchowners.forget_person, st, k)
    # anything of theirs that appeared while this ran (a job a door let in before it shut) keeps the person for a
    # rerun: the person goes only once nothing is left
    if not errors and (left := {k: len(v) for k, v in
                                (await asyncio.to_thread(_found, h, person_id, names)).items() if v}):
        errors.append(f"records appeared while the purge ran ({', '.join(f'{k}: {n}' for k, n in left.items())}); "
                      "run it again")
    if person is not None and not errors:
        st.delete(Person, person_id)
    # the steps above may have audited the person again: those rows too
    changed += await asyncio.to_thread(st.audit_redact, rewrite, 2000, start)
    if not errors:
        await asyncio.to_thread(h.vault.delete, PURGE_NAMES + person_id)
    report["person_record_deleted"] = person is not None and not errors
    report["audit_rows_redacted"] = len(set(changed))
    report["pseudonym"] = stand_in
    # the purge's own entry names the pseudonym, never the person
    h.fleet.audit(requester, PURGE_ACTION, stand_in, audit_rows_redacted=len(set(changed)),
                  deleted={k: v for k, v in counts.items() if v}, person_record=person is not None,
                  exported_objects=len(exported), errors=len(errors))
    log.info("purged %s: %s", stand_in, counts)
    return report


def _outbox(h: Any) -> Any:
    try:
        from hangar import outbox

        return outbox.of(h.fleet)
    except Exception:  # noqa: BLE001 - no outbox: nothing staged to delete
        return None


# ---------------------------------------------------------------------------- export
_SAFE = re.compile(r"[^A-Za-z0-9._/-]")


def _arc(path: str) -> str:
    parts = [p for p in _SAFE.sub("_", path).split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or "_"


async def export_job(h: Any, job_id: str, requester: str) -> Path:
    """Write the job's export to a temporary file and return its path (the caller deletes it)."""
    from hangar import exchange

    job = h.store.get(Job, job_id)
    if job is None:
        raise KeyError(f"no job '{job_id}'")
    files: dict[str, bytes] = {}
    missing: list[str] = []

    def put(name: str, value: Any) -> None:
        files[name] = value if isinstance(value, bytes) else (
            json.dumps(value, indent=2, default=str) + "\n").encode()

    record = job.model_dump(mode="json")
    for f in ("callback_url", "callback_secret"):  # a webhook URL or its secret is a credential, not evidence
        if record.get(f):
            record[f] = "(withheld)"
    put("job.json", record)
    files["instructions.md"] = (job.instructions or "").encode()
    put("context.json", [c.model_dump(mode="json") for c in job.context])
    put("events.json", [e.model_dump(mode="json") for e in job.events])
    put("result.json", job.result.model_dump(mode="json"))
    sids = [x for x in dict.fromkeys([job.session_id, *(a.session_id for a in job.attempts)]) if x]
    sandboxes = [x for x in dict.fromkeys([job.sandbox_id, *(getattr(a, "sandbox_id", None) for a in job.attempts)])
                 if x]
    for sid in sids:
        s = h.store.get(Session, sid)
        if s is None:
            missing.append(f"session {sid}: deleted")
            continue
        sandboxes += [t.sandbox_id for t in s.turns if getattr(t, "sandbox_id", None)]
        put(f"sessions/{_arc(sid)}/session.json", s.model_dump(mode="json"))
        for p in h.sessions.activity.log._paths(sid):
            if p.exists():
                files[f"sessions/{_arc(sid)}/events/{p.name}"] = await asyncio.to_thread(p.read_bytes)
        for a in (a for t in s.turns for a in t.artifacts):
            try:
                _, data = await exchange.read_artifact(h.sessions, s, a.turn, a.path)
                name = f"sessions/{_arc(sid)}/artifacts/{a.turn}/{_arc(a.path)}"
                while name in files:  # two paths that read the same once made safe ("a b", "a_b") both stay
                    name += "~"
                files[name] = data
            except Exception as e:  # noqa: BLE001 - say what's missing, export the rest
                missing.append(f"artifact {sid} turn {a.turn} {a.path}: {e}")
    entries = await asyncio.to_thread(h.store.audit_matching, [job.id, *sids, *dict.fromkeys(sandboxes)])
    files["audit.jsonl"] = "".join(e.model_dump_json() + "\n" for e in entries).encode()
    manifest = {"job": job.id, "exported_at": now(), "exported_by": requester, "missing": missing,
                "files": {n: {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()} for n, b in files.items()}}
    put("manifest.json", manifest)

    def write() -> Path:
        tmp = tempfile.NamedTemporaryFile(prefix=f"hangar-export-{_arc(job.id)}-", suffix=".tar.gz", delete=False)
        with tmp, tarfile.open(fileobj=tmp, mode="w:gz") as tar:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo(f"{_arc(job.id)}/{name}")
                info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
                tar.addfile(info, io.BytesIO(data))
        return Path(tmp.name)

    path = await asyncio.to_thread(write)
    h.fleet.audit(requester, "admin.export", job.id, files=len(files), bytes=path.stat().st_size,
                  missing=len(missing))
    return path


# ---------------------------------------------------------------------------- REST
def mount(app: Any, h: Any, admin: Any) -> None:
    from fastapi import Body
    from fastapi.responses import FileResponse, JSONResponse
    from starlette.background import BackgroundTask

    @app.post("/api/admin/purge")
    async def admin_purge(body: dict[str, Any] = Body(...), who: str = admin):
        try:
            return await purge(h, str(body.get("person") or ""), who, dry_run=bool(body.get("dry_run")))
        except PurgeBlocked as e:
            return JSONResponse({"error": e.message}, status_code=409)  # built from record names, no trace

    @app.get("/api/admin/export/jobs/{job_id}")
    async def admin_export_job(job_id: str, who: str = admin):
        path = await export_job(h, job_id, who)
        return FileResponse(path, media_type="application/gzip", filename=f"{_arc(job_id)}-export.tar.gz",
                            background=BackgroundTask(path.unlink, missing_ok=True))
