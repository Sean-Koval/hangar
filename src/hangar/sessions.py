"""Sessions: resumable streams of work (spec 0010).

The conversation and the repo state are durable; the sandbox is not. After every agent turn, before any
destroy, and periodically, a session is checkpointed to the control plane: the harness's own conversation
files, unpushed commits (a git bundle), uncommitted changes and untracked files, encrypted with the vault key.
Idle sessions are suspended (checkpoint + release the sandbox). Opening or sending to a suspended session boots
a fresh sandbox of its environment, restores the checkpoint and resumes the harness's conversation with its
native resume flag (`claude --resume`, `codex exec resume`, `pi --session-id`), or with a summary handoff when
the harness has none.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import re
import shlex
import uuid
from collections.abc import Callable
from typing import Any

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, Field

from hangar import cache as caching  # --- overnight/cache: prompt-cache TTL, one model per session, siblings
from hangar import ckpt, exchange, isolation, observe  # --- observe, isolation
from hangar import computer as computer  # --- computer
from hangar import fairness as fairness  # --- scale
from hangar import flightrec as flightrec  # --- clouddoctor: failed and slow turns leave a record
from hangar import gittree as gittree  # --- #165: a one-shot turn's start and end trees
from hangar import harness_thaw as harness_thaw  # --- harness locks (#134): thawed sessions, turn 1's drift
from hangar import hot_resume as hot_resume  # --- providers-e2b
from hangar import keys as keys  # --- durable
from hangar import manifest as manifest  # --- interfaces: the effective harness manifest of each turn
from hangar import metrics as metrics  # --- reconciler
from hangar import outbox as outbox  # --- outbox (#163, #189): run capture; retention waits for it
from hangar import outcomes as outcomes  # --- #166: agent or infrastructure
from hangar import pins as pins  # --- canary: the model and the CLI version a session is pinned to
from hangar import resident as resident  # --- durable: live and recent sessions in memory, the rest in the store
from hangar import slo as slo  # --- clouddoctor: turn-start and resume times for `hangar status`
from hangar import steer as steering  # workstream steer: live turns
from hangar import taint as taint  # --- taint (#129): whether a turn has read untrusted content
from hangar import turnlogin as turnlogin  # --- #293: a turn's harness must have a login
from hangar import turnsummary as turnsummary  # --- KAN-137: a stopped turn's summary, from its events
from hangar.checkpoints import CheckpointStore, make_store
from hangar.ckpt import CKPT_DIR, CKPT_REF, PACK_READ_LIMIT, PARTS, RESTORE_DIR  # noqa: F401 - re-exported (strategies, exchange)
from hangar.events import Activity, RunEvent, resolve_budget, total_usage
from hangar.fleet import Fleet, FleetError
from hangar.harnesses import get_harness
from hangar.models import (
    SESSION_LIVE,
    ApprovalPolicy,
    Budget,
    CachePolicy,
    Checkpoint,
    ContextItem,
    Sandbox,
    SandboxState,
    Session,
    SessionState,
    SessionTurn,
    now,
)
from hangar.store import Ge, Not  # --- durable
from hangar.vault import Vault

log = logging.getLogger("hangar.sessions")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_BRANCH_RE = re.compile(r"^(?!-)(?!/)(?!.*\.\.)(?!.*//)(?!.*@\{)[A-Za-z0-9._/-]{1,200}(?<!/)(?<!\.lock)(?<!\.)$")
MAX_MESSAGE = 64_000


class SessionError(ValueError):
    pass


class SessionNotFound(KeyError):
    pass


class NewSession(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    title: str | None = Field(None, max_length=200)
    environment: str | None = None
    repo: str | None = None
    base_branch: str | None = None
    branch: str | None = None
    harness: str | None = None
    model: str | None = None
    message: str | None = Field(None, max_length=MAX_MESSAGE)
    idle_minutes: int | None = Field(None, ge=0, le=10080)  # 0 = never auto-suspend
    labels: dict[str, str] = Field(default_factory=dict)
    budget: Budget | None = None  # per agent turn; unset fields fall back to HANGAR_BUDGET_* (workstream C)
    # workstream E: attachments and local work for the first turn, and the structured result it must produce
    context: list[ContextItem] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    approvals: ApprovalPolicy | None = None  # workstream steer: auto | ask | deny-risky (default: the environment's)
    cache: CachePolicy | None = None  # --- overnight/cache: auto | 5m | 1h | off (default: the environment's)
    harness_lock: str | None = Field(None, max_length=200)  # --- harness locks (#134): name, name@rN, name@sha256:...
    # --- KAN-174 (gap 8): an admin continues a custom branch another person's sandbox created (audited)
    take_branch: bool = False


def extract_summary(harness: str | None, log_text: str, *, outcome: str | None = None, reason: str | None = None,
                    started_at: float | None = None, finished_at: float | None = None) -> str | None:
    """The agent's last message from a run log (best effort per harness). Without one (a turn stopped over budget,
    timed out, cancelled), a summary built from the log's events: why it stopped and what the agent said last,
    never the harness's raw JSON stream (KAN-137)."""
    last: str | None = None
    for ln in log_text.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") == "result" and isinstance(e.get("result"), str):  # claude stream-json
            last = e["result"]
        elif e.get("type") == "item.completed":  # codex --json
            item = e.get("item") or {}
            if item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
                last = item["text"]
        elif e.get("type") == "message_end":  # pi --mode json
            m = e.get("message") or {}
            if m.get("role") == "assistant":
                text = " ".join(c.get("text", "") for c in m.get("content") or [] if isinstance(c, dict)
                                and c.get("type") == "text").strip()
                last = text or last
        elif e.get("event") == "result" and isinstance(e.get("result"), dict):  # antigravity stream-json
            last = e["result"].get("response") or last
    if last and outcome not in turnsummary.STOPPED:
        return last.strip()[:8000]
    return turnsummary.from_log(harness, log_text, outcome=outcome, reason=reason, started_at=started_at,
                                finished_at=finished_at)


class SessionService:
    def __init__(self, fleet: Fleet, vault: Vault):
        self.fleet = fleet
        self.settings = fleet.settings
        self.store = fleet.store
        self.vault = vault
        self.root = self.settings.data_dir / "sessions"
        # --- durable: copies re-encrypt what an old vault key sealed while a key rotation runs (keys.Resealing)
        self.blobs: CheckpointStore = keys.Resealing(make_store(self.settings.session_store, self.settings.data_dir),
                                                     vault.ring)
        # --- durable: live sessions and those used in the last HANGAR_RESIDENT_MINUTES; the store has the rest
        self._sessions: resident.Resident[Session] = resident.Resident(
            self.store, Session, self._pinned, lambda: self.settings.resident_minutes * 60)
        self._locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self._busy: set[str] = set()  # sessions with a turn, boot or suspend of their own in flight
        self._holds: collections.Counter[str] = collections.Counter()  # outside holders (jobs), refcounted
        self._restart_holds: set[str] = set()  # job sessions held from startup until their job re-attaches
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.Task | None = None
        self._last_checked: dict[str, float] = {}  # last time a checkpoint ran (taken or found unchanged)
        self.activity = Activity(self.settings, fleet.broker.redact)  # workstream C: events, usage, budgets
        # --- overnight/sweep: called with a session just before it is deleted (jobs keep its usage); must not raise
        self.on_delete: list[Callable[[Session], None]] = []
        self.env_requests: Any = None  # --- computer: envrequests.EnvRequests, wired by the app
        self.governed: Any = None  # --- KAN-79: governed.Governed, wired by the app (approvals=governed asks Tower)
        # --- taint (#129): the job a session carries (wired by JobService), for what the job's input taints; and
        # listeners called with (session, turn, ref) when a session first becomes tainted (must not raise)
        self.job_of: Callable[[str], Any] | None = None
        self.on_taint: list[Callable[[Session, SessionTurn, str], None]] = []

    # ================================================================== lifecycle
    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._sessions.adopt(self._load())  # --- durable: not every session ever
        for s in self._sessions.values():
            if s.state in SESSION_LIVE or s.state == SessionState.SUSPENDING:
                if s.sandbox_id and self._alive(s.sandbox_id):
                    if s.job_id:
                        # The job re-attaches itself; until then, nothing may suspend it under a running agent.
                        self._holds[s.id] += 1
                        self._restart_holds.add(s.id)
                        s.last_active_at = now()
                        self._set(s, SessionState.IDLE, "control plane restarted")
                    elif s.state == SessionState.RUNNING and s.turns and s.turns[-1].run_id:
                        self._busy.add(s.id)  # steer: busy from now, not from when the task first runs
                        self._spawn(self._resume_wait(s.id))  # re-attach to the running turn
                    else:
                        self._set(s, SessionState.IDLE, "control plane restarted")
                else:
                    s.sandbox_id = None
                    self._set(s, SessionState.SUSPENDED, "control plane restarted; resuming from the last checkpoint")
        steering.recover(self)  # workstream steer: messages that were on their way into a turn
        if self._hook not in self.fleet.pre_destroy:
            self.fleet.pre_destroy.append(self._hook)
        parked = hot_resume.parked_ids(self)  # --- providers-e2b: paused for their sessions, not orphaned
        orphans = [sb.id for sb in self.fleet.list(label="hangar.session") if sb.id not in parked and
                   ((o := self._sessions.get(sb.labels.get("hangar.session", ""))) is None or o.sandbox_id != sb.id)]
        if orphans:
            self._spawn(self._destroy_orphans(orphans))
        self._loop = asyncio.create_task(self._idle_loop())

    async def _destroy_orphans(self, ids: list[str]) -> None:
        """Sandboxes a session started but never attached (the control plane restarted mid-boot): nothing will
        ever use them, and they cost money until their TTL."""
        for sid in ids:
            log.info("destroying orphaned session sandbox %s", sid)
            with contextlib.suppress(Exception):
                await self.fleet.destroy(sid, actor="session")

    async def stop(self) -> None:
        with contextlib.suppress(ValueError):
            self.fleet.pre_destroy.remove(self._hook)
        for t in [*self._tasks, *([self._loop] if self._loop else [])]:
            t.cancel()
        for t in [*self._tasks, *([self._loop] if self._loop else [])]:
            with contextlib.suppress(BaseException):
                await t
        self._tasks.clear()
        if close := getattr(self.blobs, "close", None):  # perf: the GCS store keeps a connection pool
            with contextlib.suppress(Exception):
                await close()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ================================================================== public API
    def get(self, ref: str) -> Session:
        # --- durable: by id, then by name, in memory first, then in the store (archive included)
        s = self._sessions.get(ref) or next(
            (x for x in self._sessions.values() if x.name == ref and x.state != SessionState.ARCHIVED), None) \
            or next(iter(self.where(name=ref, state=Not(SessionState.ARCHIVED))), None)
        if s is None:
            raise SessionNotFound(f"no session '{ref}'")
        return self._sessions.get(s.id)

    def list(self, include_archived: bool = False, limit: int = 100, person: str | None = None,
             cursor: str | None = None) -> list[Session]:
        return [s for s, _ in self.page(include_archived, limit, person, cursor)[0]]

    def page(self, include_archived: bool = False, limit: int = 100, person: str | None = None,
             cursor: str | None = None) -> tuple[list[tuple[Session, str]], str | None]:
        """--- durable: sessions most recently active first, from the store's indexes, filtered there (a person's
        are all of theirs). Each comes with its cursor, where it stood in the store (the copy in memory may have
        moved on since), then the next page's cursor, None at the end. See JobService.page."""
        limit = max(1, min(limit, 500))
        fields: dict[str, Any] = {} if include_archived else {"state": Not(SessionState.ARCHIVED)}
        if person is not None:
            fields["person"] = person
        rows = self.store.page(Session, limit + 1, resident.after(cursor), archived=True, **fields)
        marks = [resident.cursor(at, r.id) for r, at in rows[:limit]]  # --- review: SQLite's reading of the time
        return (list(zip(self._sessions.adopt([r for r, _ in rows[:limit]]), marks, strict=True)),
                marks[-1] if len(rows) > limit else None)

    # --- durable: lookups that see every session, not just those in memory
    def where(self, **fields: Any) -> list[Session]:
        """Sessions whose fields match (Resident.where): archived ones too, as the copies in memory."""
        return self._sessions.where(**fields)

    def of_job(self, job_id: str) -> list[Session]:
        """Every session that carried the job: its own, best-of-N attempts and the judge. A session's job is saved
        with it when it's made (for_job, the forks), so no pass over the sessions in memory: views call this."""
        return self._sessions.where(memory=False, job_id=job_id)

    def touched_since(self, t: float, **fields: Any) -> list[Session]:
        """Sessions saved since `t` (and matching `fields`), to read only: the copy in memory where there is one,
        else the store's, which stays out of memory."""
        return self._sessions.read(updated_at=Ge(t), **fields)

    def exists(self, sid: str) -> bool:
        return self._sessions.peek(sid) is not None or self.store.get(Session, sid) is not None

    def taken(self, name: str | None = None, branch: str | None = None) -> bool:
        """Whether a session that isn't archived has this name (or branch)."""
        fields = {k: v for k, v in (("name", name), ("branch", branch)) if v is not None}
        return bool(self._sessions.read(state=Not(SessionState.ARCHIVED), **fields))

    def _pinned(self, s: Session) -> bool:
        """Kept in memory whatever its age: live, suspending, or something drives it (a turn, a job, a lock)."""
        return (s.state in SESSION_LIVE or s.state == SessionState.SUSPENDING or self.busy(s.id)
                or s.id in self._restart_holds or (s.id in self._locks and self._locks[s.id].locked()))

    def _load(self) -> list[Session]:
        """At start: live and suspending sessions, and those changed in the last HANGAR_RESIDENT_MINUTES."""
        found = [*self.store.list_where(Session, state=(*SESSION_LIVE, SessionState.SUSPENDING)),
                 *self.store.list_where(Session, updated_at=Ge(now() - self.settings.resident_minutes * 60))]
        return list({s.id: s for s in found}.values())

    def evict(self) -> int:
        """Let go of the sessions nothing needs in memory any more (resident.py); returns how many."""
        gone = self._sessions.evict()
        for sid in gone:
            if sid in self._locks and not self._locks[sid].locked():
                del self._locks[sid]
            self._last_checked.pop(sid, None)
        return len(gone)

    def new(self, req: NewSession, owner: str, requester: str, person: str | None = None) -> Session:
        if not _NAME_RE.match(req.name):
            raise SessionError("name: lowercase letters, digits, '.', '_' and '-' (start with a letter or digit)")
        if self.taken(name=req.name):  # --- durable: in the store too
            raise SessionError(f"a session named '{req.name}' already exists; open it, or archive it first")
        env = req.environment or self._env_for_repo(req.repo)
        if not env:
            raise SessionError("environment is required (or a repo listed in HANGAR_GITHUB_REPOS)")
        try:
            spec = self.fleet.catalog.get(env)
            harness = spec.pick_harness(req.harness)
        except (KeyError, ValueError) as e:
            raise SessionError(str(e).strip("'\"")) from e
        if not harness:
            raise SessionError(f"environment '{env}' has no agent harness")
        thawed = None
        if req.harness_lock:  # --- harness locks (#134): the lock fixes the harness, model and approvals
            try:
                thawed = harness_thaw.pin(self.fleet, req.harness_lock, env, req.harness, req.model, req.approvals)
            except harness_thaw.ThawError as e:
                raise SessionError(str(e)) from e
        branch = req.branch or f"hangar/session/{req.name}"
        if not _BRANCH_RE.match(branch) or (req.base_branch and req.base_branch == branch):
            raise SessionError(f"invalid branch '{branch}'")
        repo = req.repo or (spec.repo.url if spec.repo else None)
        taken_from = self._check_branch_creator(req, branch, repo, requester, person)  # --- KAN-174 (gap 8)
        context = exchange.check_context(req.context, self.settings)  # workstream E
        schema = exchange.check_schema(req.output_schema)
        if (context or schema) and not req.message:
            raise SessionError("context and output_schema go with a message for the agent")
        fairness.check_sessions(self, owner, person=person)  # --- scale: live sessions per person
        self.fleet.power.check(None)  # --- ops: no session that can't start until morning
        s = Session(name=req.name, title=(req.title or (req.message or req.name).strip().splitlines()[0])[:200],
                    owner=owner, environment=env, harness=harness, model=req.model,
                    repo=repo, base_branch=req.base_branch,
                    branch=branch, labels=req.labels, approvals=req.approvals, cache=req.cache,
                    idle_minutes=(req.idle_minutes if req.idle_minutes is not None
                                  else self.settings.session_idle_minutes),
                    person=person)  # --- identity
        s.budget = resolve_budget(req.budget, self.settings)
        if thawed is not None:  # --- harness locks (#134)
            harness_thaw.apply(s, thawed)
        caching.pin_model(self, s)  # --- overnight/cache
        pins.pin(self, s)  # --- canary: a policy becomes a concrete id; the CLI version
        self._sessions[s.id] = s
        self._set(s, SessionState.STARTING, f"created by {requester}")
        self.fleet.audit(requester, "session.new", s.id, name=s.name, environment=env, repo=s.repo)
        if taken_from is not None:  # --- KAN-174 (gap 8): an admin took another person's branch
            self.fleet.audit(requester, "session.branch_override", s.id, name=s.name, repo=s.repo, branch=branch,
                             created_by=taken_from.get("person"), sandbox=taken_from.get("sandbox"))
        if req.message:
            s.turns.append(SessionTurn(message=req.message, requester=requester, context=context,
                                       output_schema=schema))
            self._save(s)
            self._busy.add(s.id)
            self._spawn(self._turn(s.id))
        else:
            self._busy.add(s.id)
            self._spawn(self._boot_task(s.id))
        return s

    def _check_branch_creator(self, req: NewSession, branch: str, repo: str | None, requester: str,
                              person: str | None) -> dict[str, Any] | None:
        """--- KAN-174 (known gap 8): a custom branch outside hangar/session/ that another person's sandbox created
        through the gateway (branchowners.py) would hand this session that sandbox's commits. Refused, audited as
        `session.branch_refused`, unless an admin passes `take_branch`; returns the record then, for the override's
        audit entry. A branch hangar has no record of (pushed by a person, or before the record existed) and one the
        same person's sandbox created are continued as before. The session's own record (it is in the store from
        here on) reserves the branch against every other sandbox at the gateway."""
        from hangar import branchowners, identity
        from hangar.gateway import SESSION_REFS, parse_remote

        if not req.branch or f"refs/heads/{branch}".startswith(SESSION_REFS):
            return None
        remote = parse_remote(repo, self.fleet.gateway.hosts) if repo else None
        rec = branchowners.creator(self.store, remote[1], branch) if remote else None
        who = person if person is not None else identity.person()
        if rec is None or remote is None or not branchowners.foreign(rec, who):
            return None
        by = rec.get("person")
        owner = f"{by}'s" if by else "a"
        if req.take_branch and identity.sees_all():
            return rec
        self.fleet.audit(requester, "session.branch_refused", None, name=req.name, repo=remote[1], branch=branch,
                         created_by=by, sandbox=rec.get("sandbox"), take_branch=req.take_branch)
        if req.take_branch:
            raise identity.Forbidden("only an admin may take a branch another person's sandbox created")
        raise SessionError(f"branch '{branch}' was created by {owner} hangar sandbox"
                           f"{' (job ' + rec['job'] + ')' if rec.get('job') else ''}, not one of yours; "
                           "a session would continue its commits. Pick another branch, or ask an admin "
                           "to start the session with take_branch")

    def open(self, ref: str, requester: str, harness: str | None = None) -> Session:
        s = self.get(ref)
        if s.state == SessionState.ARCHIVED:
            raise SessionError(f"session {s.name} is archived")
        live = bool(s.sandbox_id and self._alive(s.sandbox_id))
        if harness and harness != s.harness and s.harness_lock:  # --- harness locks (#134)
            raise SessionError(f"session {s.name} runs harness lock {s.harness_lock['ref']} ({s.harness}); "
                               "start another session to run another harness")
        if harness and harness != s.harness and (live or self.busy(s.id)):
            raise SessionError(f"session {s.name} is running {s.harness}; suspend it before switching to {harness}")
        if harness and harness != s.harness:
            spec = self.fleet.catalog.get(s.environment)
            try:
                s.harness = spec.pick_harness(harness)
            except ValueError as e:
                raise SessionError(str(e)) from e
            s.model = spec.model_for(s.harness)  # --- overnight/cache: model ids are per harness; a switch hands off
            pins.pin(self, s)  # --- canary
            self._save(s)
        if self.busy(s.id) or (live and s.state in SESSION_LIVE):
            return s
        fairness.check_sessions(self, s.owner, s)  # --- scale: resuming takes a sandbox
        self.fleet.power.check(s.provider)  # --- ops
        self._busy.add(s.id)
        self._set(s, SessionState.STARTING, f"opened by {requester}")
        self.fleet.audit(requester, "session.open", s.id)
        self._spawn(self._boot_task(s.id))
        return s

    def send(self, ref: str, message: str, requester: str, context: list[ContextItem] | None = None,
             output_schema: dict[str, Any] | None = None, model: str | None = None) -> Session:
        """`model` (overnight/cache): another model from this turn on, with a warning on the turn (the switch costs
        an uncached read of the whole conversation)."""
        s = self.get(ref)
        if not message.strip():
            raise SessionError("message is empty")
        if len(message) > MAX_MESSAGE:
            raise SessionError("message is too long")
        context = exchange.check_context(context or [], self.settings)  # workstream E
        output_schema = exchange.check_schema(output_schema)
        try:
            model = caching.check_model(model)  # --- overnight/cache
        except ValueError as e:
            raise SessionError(str(e)) from e
        model = pins.concrete(self, s, model)  # --- canary: `frontier` names a model now, not each turn
        if s.state == SessionState.ARCHIVED:
            raise SessionError(f"session {s.name} is archived")
        if s.job_id:
            raise SessionError(f"session {s.name} carries job {s.job_id}; follow up on the job instead "
                               "(its turns are verified and delivered)")
        if self.busy(s.id) or s.state in (SessionState.RUNNING, SessionState.SUSPENDING):
            # --- workstream steer: a live turn takes the message as it runs
            if not context and not output_schema and steering.can_steer(self, s):
                steering.steer_later(self, s, message, requester)
                caching.note_switch(self, s, model, requester)  # --- overnight/cache: from the next turn
                return s
            raise SessionError(f"session {s.name} is busy ({s.state}); wait for it to finish, or interrupt it")
        if not (s.sandbox_id and self._alive(s.sandbox_id)):
            fairness.check_sessions(self, s.owner, s)  # --- scale: resuming takes a sandbox
            self.fleet.power.check(s.provider)  # --- ops: the message stays unsent, not a failed turn
        warning = caching.switch_model(self, s, model, requester)  # --- overnight/cache
        self.start_turn(s, message, requester, context, output_schema)
        if warning:
            s.turns[-1].warnings.append(warning)
        return s

    def start_turn(self, s: Session, message: str, requester: str, context: list[ContextItem] | None = None,
                   output_schema: dict[str, Any] | None = None, preclear: list[dict[str, str]] | None = None) -> None:
        """Begin a turn now; the caller has checked that the session can take one. `preclear` (KAN-77): calls
        Tower signed, which the turn's driver allows once each (govevents.py)."""
        s.turns.append(SessionTurn(message=message, requester=requester, context=context or [],
                                   output_schema=output_schema, preclear=preclear or []))
        s.needs_input = None  # this message answers the agent's question, if it had one
        s.question_options = []
        s.last_active_at = now()
        self._busy.add(s.id)
        self._set(s, SessionState.RUNNING if s.sandbox_id and self._alive(s.sandbox_id) else SessionState.STARTING,
                  f"message from {requester}")
        self.fleet.audit(requester, "session.send", s.id, message=message[:300])
        self._spawn(self._turn(s.id))

    def suspend(self, ref: str, requester: str) -> Session:
        s = self.get(ref)
        if self.busy(s.id):
            raise SessionError(f"session {s.name} is busy ({s.state}); wait for it to finish")
        if not s.sandbox_id:
            return s
        self._busy.add(s.id)
        self.fleet.audit(requester, "session.suspend", s.id)
        self._spawn(self._suspend_task(s.id, f"suspended by {requester}"))
        return s

    async def checkpoint_now(self, ref: str, requester: str) -> Checkpoint | None:
        s = self.get(ref)
        async with self._locks[s.id]:
            return await self._checkpoint(s, "manual")

    def archive(self, ref: str, requester: str) -> Session:
        s = self.get(ref)
        if s.sandbox_id or self.busy(s.id):
            raise SessionError(f"suspend session {s.name} before archiving it")
        self._spawn(self._drop(f"{s.id}/"))
        self._spawn(hot_resume.discard(self, s.id))  # --- providers-e2b
        s.checkpoints = []
        self._set(s, SessionState.ARCHIVED, f"archived by {requester}")
        self.fleet.audit(requester, "session.archive", s.id)
        return s

    def delete(self, ref: str, requester: str, force_now: bool = False) -> None:
        """--- #189: its manifests and event log go once the outbox exported its runs (pending-delete until then);
        `force_now` (admins: personal-data deletion) drops them now, audited."""
        s = self.archive(ref, requester)
        for fn in self.on_delete:  # --- overnight/sweep
            try:
                fn(s)
            except Exception:  # noqa: BLE001 - a listener must never block a delete
                log.exception("session delete listener failed")
        try:
            outbox.of(self.fleet).retire_session(s, self, requester, force_now=force_now)
        except Exception:  # noqa: BLE001 - never keep a deleted session's record over its files
            log.exception("session %s: could not hand its files to retention; deleting them", s.id)
            manifest.store(self.settings).drop(s.id)
            self.activity.log.drop(s.id)
        if force_now:
            self.fleet.audit(requester, "session.delete_now", s.id)
        self.store.delete(Session, s.id)
        self._sessions.pop(s.id, None)
        self.activity.live.drop(s.id)  # KAN-140

    def events(self, ref: str, since: int = 0, limit: int = 200, tail: int | None = None) -> list[dict[str, Any]]:
        """The session's activity events (every turn), oldest first: after `since`, or the last `tail`."""
        s = self.get(ref)
        return [e.model_dump(mode="json", exclude={"off"}) for e in self.activity.read(s.id, since, limit, tail)]

    def view(self, s: Session) -> dict[str, Any]:
        d = s.model_dump(mode="json", exclude={"checkpoints"})
        h = get_harness(s.harness) if s.harness else None
        cps = s.checkpoints
        d["checkpoints"] = [c.model_dump(mode="json") for c in cps[-3:]]
        d["checkpoint_count"] = len(cps)
        d["last_checkpoint_at"] = cps[-1].ts if cps else None
        d["busy"] = self.busy(s.id)
        last = s.turns[-1] if s.turns else None
        d["terminal_session"] = last.run_id if last else None
        d["last_summary"] = next((t.summary for t in reversed(s.turns) if t.summary), None)
        d["resume_command"] = self.resume_command(s, h)
        for t in d["turns"]:  # workstream C: the collector's read position is internal
            t.pop("event_cursor", None)
        d["usage"] = u.model_dump(mode="json") if (u := total_usage(s.turns)) else None
        d["pending_approvals"] = [a.model_dump(mode="json") for a in steering.pending(s)]  # workstream steer
        if self.env_requests is not None:  # --- computer: the agent's requests, answered like approvals
            d["pending_approvals"] += self.env_requests.approvals_view(s)
        # KAN-155: the step policy stopped in the live turn, so the Sessions page shows Denied as the Fleet does
        d["policy_denial"] = den.model_dump(mode="json") if (den := steering.policy_denial(s)) else None
        return exchange.scrub_turns(d)  # workstream E: attachment contents never leave in a view

    @staticmethod
    def resume_command(s: Session, h: Any = None) -> str | None:
        """The harness command that resumes this session's conversation locally, when there is one."""
        h = h or (get_harness(s.harness) if s.harness else None)
        if h and h.resume_hint and s.harness_session and s.harness_session_for == s.harness:
            return h.resume_hint.format(id=s.harness_session)
        return None

    # ================================================================== internals: state
    def _env_for_repo(self, repo: str | None) -> str | None:
        from hangar.github import parse_repo

        if not repo:
            return None
        m = self.settings.github_repos
        raw = m.get(parse_repo(repo) or "\0") or m.get(repo) or m.get(repo.removesuffix(".git")) or ""
        envs = [e.strip() for e in raw.split(",") if e.strip()]
        return envs[0] if envs else None

    def _alive(self, sandbox_id: str) -> bool:
        try:
            return self.fleet.get(sandbox_id).state == SandboxState.RUNNING
        except FleetError:
            return False

    def _save(self, s: Session) -> None:
        s.updated_at = now()
        self.store.put(s)

    # ------------------------------------------------------------------ --- taint (#129)
    def _job(self, s: Session) -> Any:
        if not s.job_id or self.job_of is None:
            return None
        try:
            return self.job_of(s.job_id)
        except Exception:  # noqa: BLE001 - a job no longer in memory: its sessions carry their own taint
            return None

    def _untrusted_files(self, s: Session, turn: SessionTurn) -> set[str]:
        job = self._job(s)
        return {c.name for c in [*turn.context, *(job.context if job is not None else [])] if c.untrusted}

    def _taint_begin(self, s: Session, turn: SessionTurn) -> None:
        n = next((i for i, t in enumerate(s.turns, 1) if t is turn), len(s.turns))
        first = taint.begin(s, turn, n, taint.job_sources(self._job(s)))
        self._save(s)
        if first is not None:
            ev = RunEvent(kind="progress", text=f"taint: read untrusted content from {first}", turn=n,
                          run_id=turn.run_id or "", data={"source": "hangar", "action": "taint", "ref": first[:200]})
            with contextlib.suppress(OSError):
                self.activity.append(s.id, [ev])
                if s.job_id:
                    self.activity.append(s.job_id, [ev.model_copy()])
            self._tainted(s, turn, first)

    def _tainted(self, s: Session, turn: SessionTurn, ref: str) -> None:
        """A session just became tainted (its first source): tell the turn's driver (#130) and the listeners (its
        job mirrors it)."""
        if turn.live and turn.approval_policy not in (None, "auto") and turn.run_id and not turn.finished_at:
            self._spawn(self._taint_driver(s, turn, ref))
        for fn in self.on_taint:
            try:
                fn(s, turn, ref)
            except Exception:  # noqa: BLE001 - recording taint never breaks a turn
                log.exception("session %s: taint listener failed", s.id)

    async def _taint_driver(self, s: Session, turn: SessionTurn, ref: str) -> None:
        """--- taint (#130): the signed `taint` command, so the driver gates pushes, PRs and fetches from now on.
        Signed like a steer: the agent can't send it, or one that clears it (there is none)."""
        try:
            await steering.command(self, s, turn, "taint", ref=ref[:200])
        except Exception as e:  # noqa: BLE001 - the turn may have ended; the next one starts --tainted
            log.info("session %s: could not tell the driver it is tainted: %s", s.id, e)

    def _set(self, s: Session, state: SessionState, reason: str | None = None) -> None:
        s.state = state
        s.reason = self.fleet.broker.redact(reason)[:500] if reason else None
        self._save(s)

    # ================================================================== internals: boot / restore
    async def _boot_task(self, sid: str) -> None:
        s = self._sessions[sid]
        try:
            async with self._locks[sid]:
                await self._boot(s)
        except Exception as e:  # noqa: BLE001
            log.exception("session %s failed to boot", sid)
            self._set(s, SessionState.FAILED, f"could not start: {e}")
        finally:
            self._busy.discard(sid)

    @observe.traced("session.boot")  # --- observe
    async def _boot(self, s: Session, progress: Callable[[str], None] | None = None) -> Sandbox:
        """Attach a running sandbox to the session, restoring its last checkpoint. Caller holds the lock."""
        if s.sandbox_id and self._alive(s.sandbox_id):
            return self.fleet.get(s.sandbox_id)
        s.sandbox_id = None
        t0 = now()  # --- clouddoctor
        self._set(s, SessionState.STARTING, "starting a sandbox" + (" and restoring the last checkpoint"
                                                                     if s.checkpoints else ""))
        ttl = max(s.idle_minutes * 3, 120) + self.settings.session_turn_timeout_minutes

        def stage(msg: str) -> None:  # where the time goes, for people waiting on the session (and its job)
            self._set(s, SessionState.STARTING, msg)
            if progress:
                progress(msg)

        if (hot := await hot_resume.wake(self, s, stage)) is not None:  # --- providers-e2b: its memory, if parked
            slo.record("resume", now() - t0, "hot")  # --- clouddoctor
            return hot
        sb = await self.fleet.create(
            s.environment, name=f"ses-{s.name[:30]}-{uuid.uuid4().hex[:4]}", repo=s.repo, branch=s.base_branch,
            harness=s.harness, ttl_minutes=ttl, labels={**s.labels, "hangar.session": s.id}, provider=s.provider,
            actor=f"session:{s.id}", progress=stage,
            push_branch=s.branch,  # workstream F: the one branch a gateway-mode sandbox may push
            person=s.person,  # --- identity: the session's person's sandbox, with their credentials
            purpose="automated" if s.job_id else "interactive",  # --- #125: a job's session is automated
            harness_lock=s.harness_lock)  # --- #221: its prefix layer mounted from the lock it was thawed from
        self._credential_warnings(s, sb)
        if s.checkpoints and sb.info.get("repo_path"):
            stage("restoring the last checkpoint")
        note = None
        try:
            if sb.info.get("repo_path"):
                note = await self._restore(s, sb)
        except Exception:
            s.state = SessionState.FAILED  # so the pre-destroy hook does not checkpoint a half-restored tree
            with contextlib.suppress(Exception):
                await self.fleet.destroy(sb.id, actor=f"session:{s.id}")
            raise
        s.sandbox_id = sb.id
        s.harness = s.harness or sb.info.get("harness")
        s.last_active_at = now()
        if s.checkpoints:  # --- clouddoctor
            slo.record("resume", now() - t0, "cold")
        self._set(s, SessionState.IDLE, note or ("ready" + (" (restored)" if s.checkpoints else "")))
        return sb

    def _credential_warnings(self, s: Session, sb: Sandbox) -> None:
        """--- #125: a subscription login went into the session's sandbox: say so once per boot, as an event, and
        keep it on the session (get_session) and so in its turns' manifests."""
        warns = list(sb.info.get("credential_warnings") or [])
        s.warnings = warns
        if not warns:
            return
        from hangar.events import RunEvent

        with contextlib.suppress(Exception):
            self.activity.append(s.id, [RunEvent(kind="progress", turn=len(s.turns), text=w,
                                                 data={"source": "hangar", "warning": "subscription_login"})
                                        for w in warns])

    async def _sh(self, sb: Sandbox, cmd: str, timeout: int = 300, cwd: str | None = None):
        # Housekeeping: must not count as activity (or the idle check would keep every session awake forever).
        return await self.fleet.exec(sb.id, cmd, cwd=cwd, timeout=timeout, actor="session", quiet=True)

    async def _restore(self, s: Session, sb: Sandbox) -> str | None:
        q = shlex.quote
        repo = sb.info["repo_path"]
        if not s.base_sha:
            res = await self._sh(sb, "git rev-parse --verify -q HEAD", cwd=repo)
            s.base_sha = res.stdout.strip() if res.exit_code == 0 and res.stdout.strip() else None
            if not s.base_branch:
                res = await self._sh(sb, "git rev-parse --abbrev-ref HEAD", cwd=repo)
                s.base_branch = res.stdout.strip() or None
        if not s.checkpoints:
            await self._sh(sb, "git config user.email >/dev/null || git config user.email hangar@localhost; "
                               "git config user.name >/dev/null || git config user.name hangar", cwd=repo)
            # First boot: continue the session branch if it was pushed before, else start it from the base.
            b = q(s.branch)
            res = await self._sh(sb, f"(git fetch -q origin {b} 2>/dev/null && git checkout -q -B {b} FETCH_HEAD) "
                                     f"|| git checkout -q -B {b}", cwd=repo)
            if res.exit_code != 0:
                raise SessionError(f"could not check out {s.branch}: {(res.stderr or res.stdout)[-400:]}")
            return None
        errors: list[str] = []
        for ck in reversed(s.checkpoints):  # newest first; an unrestorable checkpoint must not brick the session
            try:
                await self._restore_checkpoint(s, sb, repo, ck)
                if ck is not s.checkpoints[-1]:
                    return f"restored an older checkpoint ({ck.id}); newer ones failed: {'; '.join(errors)}"
                return None
            except SessionError as e:
                errors.append(f"{ck.id}: {e}")
                log.warning("session %s: checkpoint %s did not restore: %s", s.id, ck.id, e)
        raise SessionError("no checkpoint could be restored: " + "; ".join(errors)[-600:])

    @observe.traced("checkpoint.restore")  # --- observe
    async def _restore_checkpoint(self, s: Session, sb: Sandbox, repo: str, ck: Checkpoint) -> None:
        """Parts are fetched and decrypted in parallel, written as one archive and applied by one script (perf: two
        round trips, where it was one write per part and three execs). Packs are planned from the recorded sizes
        and sent one at a time, so the control plane holds at most one pack (or one big part) per restore."""
        _, prov = self.fleet._running(sb.id)

        async def fetch(part: str) -> tuple[str, bytes]:
            blob = await self.blobs.get(f"{s.id}/{ck.id}/{part}.enc")
            if blob is None:
                raise SessionError(f"checkpoint part {part} is missing from {self.blobs.description}")
            try:
                return part, await asyncio.to_thread(self.vault.decrypt, blob)
            except InvalidToken as e:  # a damaged blob must fall back to an older checkpoint, not brick the session
                raise SessionError(f"checkpoint part {part} is corrupt (or was written with another key)") from e

        async def send(i: int, names: list[str]) -> None:
            group = dict(await asyncio.gather(*(fetch(p) for p in names)))
            await prov.write_file(sb, f"{packs}.{i}.tar", await asyncio.to_thread(ckpt.pack, group), mode=0o600)

        present = {p: ck.parts[p] for p in PARTS if p in ck.parts}
        packs = f"{RESTORE_DIR}-{ck.id}"
        for i, names in enumerate(ckpt.groups(present)):
            await send(i, names)  # one pack in memory at a time
        script = ckpt.restore_script(repo=repo, home=f"/home/{self.settings.sandbox_user}", branch=s.branch,
                                     target=ck.head or ck.base or s.base_sha or "HEAD", present=set(present),
                                     packs=packs)
        res = await self._sh(sb, script, cwd=repo, timeout=600)
        if res.exit_code != 0:
            raise SessionError(f"restore failed: {(res.stderr or res.stdout)[-600:]}")
        head = ckpt.restored_head(res.stdout) or ""
        if ck.head and head != ck.head:
            raise SessionError(f"restore ended at {head[:12]}, expected {ck.head[:12]}")

    # ================================================================== internals: checkpoint
    def _agent_dirs(self, s: Session) -> list[str]:
        dirs: list[str] = []
        for t in {s.harness, s.harness_session_for} - {None}:
            with contextlib.suppress(KeyError):
                dirs += get_harness(t).session_dirs
        return sorted(set(dirs + s.extra_dirs))

    @observe.traced("checkpoint.capture")  # --- observe
    async def _checkpoint(self, s: Session, reason: str, skip_unchanged: bool = False) -> Checkpoint | None:
        """Snapshot conversation + repo state into the control plane. Caller holds the lock.

        One exec builds the parts (a periodic check stops after the fingerprint when nothing changed), one read
        brings back the parts that changed, and parts identical to the last checkpoint are copied in the store."""
        if not s.sandbox_id or not self._alive(s.sandbox_id):
            return None
        sb = self.fleet.get(s.sandbox_id)
        _, prov = self.fleet._running(sb.id)
        last = s.checkpoints[-1] if s.checkpoints else None
        max_untracked = self.settings.session_max_untracked_mb * 1024 * 1024
        max_part = self.settings.session_max_part_mb * 1024 * 1024
        skip = last.fingerprint if (reason == "interval" or skip_unchanged) and last and last.fingerprint else None
        previous = {p: d for p, d in last.digests.items() if p in last.parts} if last else {}
        script = ckpt.capture_script(repo=sb.info.get("repo_path"), home=f"/home/{self.settings.sandbox_user}",
                                     dirs=self._agent_dirs(s), base=s.base_sha or "HEAD", max_untracked=max_untracked,
                                     max_part=max_part, skip_if=skip, previous=previous)
        timeout = int(max(30, min(600, self.settings.pre_destroy_timeout_seconds - 30)))
        # --- overnight/sweep: run from $HOME, so a repo directory that is gone can't fail the exec itself
        res = await self._sh(sb, script, timeout=timeout, cwd=f"/home/{self.settings.sandbox_user}")
        cap = ckpt.parse_capture(res.stdout)
        if cap.fingerprint is None:
            raise SessionError(f"checkpoint failed: {(res.stderr or res.stdout)[-400:]}")
        if cap.unchanged or (skip and cap.fingerprint == skip):
            self._last_checked[s.id] = now()  # nothing changed; don't re-run the whole thing every minute
            return None
        if cap.norepo and last and "agent.tgz" in last.parts and "agent.tgz" not in cap.parts:
            # --- reconciler: no checkout and no conversation where the last checkpoint had both is a blank
            # sandbox (a pod recreated under its claim), not work to save: keep the last checkpoint the latest
            raise SessionError(f"sandbox {sb.id} has neither the repo nor the agent's conversation any more (its pod "
                               "was probably replaced); not checkpointing it over the last good checkpoint")
        ck = Checkpoint(reason=reason, head=cap.head, base=s.base_sha, branch=s.branch, fingerprint=cap.fingerprint)
        key = f"{s.id}/{ck.id}"
        data: dict[str, bytes] = {}  # parts to encrypt and upload
        sizes: dict[str, int] = {}  # part -> plaintext bytes stored
        same: list[str] = []  # parts unchanged since `last`: copied inside the store
        limits = {p: max_untracked if p == "untracked.tgz" else max_part for p in ckpt.ALL_PARTS}
        carried: dict[str, int] = {}  # --- overnight/sweep: repo parts kept from `last` when the checkout is gone
        if cap.norepo:
            if last:
                carried = {p: last.parts[p] for p in ckpt.REPO_PARTS if p in last.parts and p in last.digests}
                ck.digests |= {p: last.digests[p] for p in carried}
                same += list(carried)
                ck.head = ck.head or last.head
            kept = f"its code is kept as of checkpoint {last.id}" if last and carried else \
                "only the conversation was checkpointed"
            ck.warnings.append(f"the repo directory {sb.info.get('repo_path')} is gone; {kept}")
        try:
            for part in ckpt.ALL_PARTS:
                if part not in cap.parts:
                    continue
                size, digest = cap.parts[part]
                limit = limits[part]
                if size > limit:
                    ck.warnings.append(f"{part} is {size // 1024 // 1024} MB, over the "
                                       f"{limit // 1024 // 1024} MB limit; not checkpointed")
                    continue
                ck.digests[part] = digest
                if last and previous.get(part) == digest:
                    same.append(part)
            fresh = [p for p in ck.digests if p not in same]
            if packed := set(fresh) & cap.packed:
                # integrate (#48): every size above came from the sandbox (its `stat` and `tar` are the agent's to
                # redefine), so the read itself is capped: a pack is PACK_LIMIT plus tar headers.
                raw = await prov.read_file(sb, f"{CKPT_DIR}/{ckpt.PACK}", limit=PACK_READ_LIMIT) or b""
                if len(raw) <= PACK_READ_LIMIT:
                    data = await asyncio.to_thread(ckpt.unpack, raw, {p: limits[p] for p in packed})  # off the loop
                del raw

            copied: list[str] = []

            async def copy(part: str) -> None:
                ok = await self.blobs.copy(f"{s.id}/{last.id}/{part}.enc", f"{key}/{part}.enc")
                (copied if ok else fresh).append(part)  # the earlier copy is gone: send this one

            await asyncio.gather(*(copy(p) for p in same))
            singles = [p for p in fresh if p not in data]  # not packed (too big), or a copy that failed
            # Once nothing is left to read, drop the sandbox's copies (its /tmp may be RAM) while the uploads run.
            cleanup = None if singles else asyncio.ensure_future(self._sh(sb, f"rm -rf {CKPT_DIR}"))

            async def upload(part: str, plain: bytes) -> None:
                # review: the digest of what is stored, not the one the sandbox reported, decides later copies
                ck.digests[part], blob = await asyncio.to_thread(ckpt.seal, self.vault, plain)
                await self.blobs.put(f"{key}/{part}.enc", blob)
                sizes[part] = len(plain)

            async def single(part: str) -> None:
                plain = await prov.read_file(sb, f"{CKPT_DIR}/{part}", limit=limits[part])  # integrate (#48)
                if plain is None or len(plain) > limits[part]:
                    ck.warnings.append(f"could not read {part}" if plain is None else
                                       f"{part} is over the {limits[part] // 1024 // 1024} MB limit; not checkpointed")
                    ck.digests.pop(part, None)
                    return
                await upload(part, plain)

            try:
                await asyncio.gather(*(upload(p, data.pop(p)) for p in list(data)))
                for part in singles:  # review: one at a time, as each may be up to session_max_part_mb
                    await single(part)
            finally:
                cleanup = cleanup or asyncio.ensure_future(self._sh(sb, f"rm -rf {CKPT_DIR}"))
                with contextlib.suppress(Exception):
                    await cleanup
        except BaseException:
            self._spawn(self._drop(f"{key}/"))  # never leave an unrecorded partial checkpoint
            raise
        sizes |= {p: carried[p] if p in carried else cap.parts[p][0] for p in copied}
        ck.parts = {p: sizes[p] for p in ckpt.ALL_PARTS if p in sizes}
        s.checkpoints.append(ck)
        keep = max(1, self.settings.session_keep_checkpoints)
        for old in s.checkpoints[:-keep]:
            self._spawn(self._drop(f"{s.id}/{old.id}/"))
        s.checkpoints = s.checkpoints[-keep:]
        self._last_checked[s.id] = now()
        self._save(s)
        return ck

    # ================================================================== internals: turns
    def _handoff(self, s: Session) -> str:
        lines = [f"# Earlier in this session ({s.name}: {s.title})",
                 "You are continuing work that an earlier agent conversation started. What happened so far:"]
        earlier = s.turns[:-1]
        keep = max(1, self.settings.session_handoff_turns)
        if len(earlier) > keep:
            lines.append(f"({len(earlier) - keep} earlier turns omitted)")
        for i, t in list(enumerate(earlier, 1))[-keep:]:
            lines.append(f"{i}. Asked: {t.message[:800]}")
            if t.summary:
                lines.append(f"   Result: {t.summary[:800]}")
        lines.append(f"The work is on branch `{s.branch}`; `git log` and `git diff {s.base_sha or ''}` show it.")
        return "\n".join(lines)

    def _prompt(self, s: Session, turn: SessionTurn, mode: str) -> str:
        parts = []
        if mode == "handoff":
            parts.append(self._handoff(s))
            parts.append("# Now")
        parts.append(turn.message)
        if len(s.turns) == 1 or mode == "handoff":
            parts.append(f"(You are in a hangar session on branch `{s.branch}`. Commit your work with clear messages "
                         "as you finish steps; don't push unless asked. The session is saved between turns, so it is "
                         "fine to stop and ask a question.)")
        # workstream E: the I/O protocol (first turn and handoffs), this turn's attachments and result schema
        return "\n\n".join(parts) + exchange.prompt_notes(self.settings, s, turn,
                                                         protocol=len(s.turns) == 1 or mode == "handoff")

    async def _turn(self, sid: str) -> None:
        s = self._sessions[sid]
        turn = s.turns[-1]
        try:
            await self.ensure_sandbox(s)
            await self.begin_turn(s, turn)
            await self.end_turn(s, turn)
        except Exception as e:  # noqa: BLE001
            log.exception("session %s turn failed", sid)
            turn.finished_at = turn.finished_at or now()
            alive = bool(s.sandbox_id and self._alive(s.sandbox_id))
            self._set(s, SessionState.IDLE if alive else (SessionState.SUSPENDED if s.checkpoints
                                                          else SessionState.FAILED), f"turn failed: {e}")
        finally:
            self._busy.discard(sid)

    async def _resume_wait(self, sid: str) -> None:
        """After a control-plane restart: wait for a turn that was already running."""
        s = self._sessions[sid]
        self._busy.add(sid)
        try:
            await self.end_turn(s, s.turns[-1])
        except Exception as e:  # noqa: BLE001
            self._set(s, SessionState.IDLE if self._alive(s.sandbox_id or "") else SessionState.SUSPENDED,
                      f"turn interrupted: {e}")
        finally:
            self._busy.discard(sid)

    # ================================================================== jobs (spec 0010 M2)
    def for_job(self, *, job_id: str, environment: str, harness: str | None, model: str | None, repo: str | None,
                base_branch: str | None, branch: str, provider: str | None, labels: dict[str, str],
                extra_dirs: list[str], idle_minutes: int, person: str | None = None) -> Session:
        """The session that carries a job: same sandbox lifecycle, checkpoints and conversation resume, but turns
        are driven by the job service (which verifies and delivers them)."""
        existing = next((x for x in self.of_job(job_id) if not x.parent_id), None)  # --- durable: in the store too
        if existing:
            return existing
        s = Session(name=f"job-{job_id.removeprefix('job-')}", title=f"job {job_id}", owner="jobs",
                    environment=environment, harness=harness, model=model, repo=repo, base_branch=base_branch,
                    branch=branch, provider=provider, labels={**labels, "hangar.job": job_id}, job_id=job_id,
                    extra_dirs=extra_dirs, idle_minutes=max(1, idle_minutes), person=person)  # --- identity
        pins.pin(self, s)  # --- canary: jobs default to `balanced` (pins.JOB_DEFAULT)
        self._sessions[s.id] = s
        self._set(s, SessionState.SUSPENDED, f"carries job {job_id}")
        return s

    def adopt(self, s: Session, sandbox_id: str, base_sha: str | None, turns: list[SessionTurn]) -> None:
        """Attach an already-running sandbox to a session (jobs started before sessions existed), so it gets
        checkpoints, suspend and resume from here on."""
        sb = self.fleet.get(sandbox_id)
        sb.labels["hangar.session"] = s.id
        self.store.put(sb)
        s.sandbox_id, s.base_sha = sb.id, s.base_sha or base_sha
        s.harness = s.harness or sb.info.get("harness")
        s.turns = turns
        s.last_active_at = now()
        self._set(s, SessionState.IDLE, "adopted a running sandbox")

    def busy(self, sid: str) -> bool:
        return sid in self._busy or self._holds[sid] > 0

    @contextlib.asynccontextmanager
    async def hold(self, s: Session):
        """Keep the idle loop away (no suspend, no interval checkpoint) while a caller drives the session.
        Refcounted, and taken under the session lock, so it waits for an in-flight suspend to finish first:
        afterwards the caller sees the truth about the sandbox (gone -> resume it)."""
        async with self._locks[s.id]:
            self._holds[s.id] += 1
        self.release_restart_hold(s)
        try:
            yield s
        finally:
            self._holds[s.id] -= 1
            if self._holds[s.id] <= 0:
                del self._holds[s.id]
            s.last_active_at = now()
            self._save(s)

    def release_restart_hold(self, s: Session) -> None:
        if s.id in self._restart_holds:
            self._restart_holds.discard(s.id)
            self._holds[s.id] -= 1
            if self._holds[s.id] <= 0:
                del self._holds[s.id]

    def will_resume(self, s: Session) -> bool:
        """Will the next turn continue the harness's own conversation (vs. start fresh or hand off)?"""
        h = get_harness(s.harness) if s.harness else None
        return bool(h and h.resume and s.harness_session and s.harness_session_for == s.harness)

    def resumable(self, s: Session) -> bool:
        return bool(s.sandbox_id and self._alive(s.sandbox_id)) or bool(s.checkpoints)

    async def release(self, s: Session, why: str, checkpoint: bool = True) -> None:
        """Suspend now (checkpoint and release the sandbox); with checkpoint=False, just drop the sandbox
        (used when a job fails before doing any work)."""
        async with self._locks[s.id]:
            if checkpoint:
                await self._suspend(s, why)
                return
            sid, s.sandbox_id = s.sandbox_id, None
            self._set(s, SessionState.SUSPENDED, why)
            if sid:
                with contextlib.suppress(Exception):
                    await self.fleet.destroy(sid, actor=f"session:{s.id}")

    def touch(self, s: Session, idle_minutes: int | None = None) -> None:
        if idle_minutes is not None:
            s.idle_minutes = max(1, idle_minutes)
        s.last_active_at = now()
        self._save(s)

    # ================================================================== turns (also used by jobs)
    async def ensure_sandbox(self, s: Session, progress: Callable[[str], None] | None = None) -> Sandbox:
        """A running sandbox for the session: the current one, or a new one restored from the last checkpoint."""
        async with self._locks[s.id]:
            try:
                return await self._boot(s, progress=progress)
            except Exception as e:
                self._set(s, SessionState.SUSPENDED if s.checkpoints else SessionState.FAILED,
                          f"could not start: {e}")
                raise

    async def _check_login(self, s: Session, turn: SessionTurn, sb: Sandbox) -> None:
        """--- #293: refuse a turn whose harness would start with no login (turnlogin.py), recording it on the turn
        as `infra_error` / `no_login`, so it reads as configuration, not the agent's failure."""
        harness = s.harness or sb.info.get("harness")
        try:
            await turnlogin.check(self.fleet, sb, harness, s.person)
        except turnlogin.NoLogin as e:
            turn.harness = harness
            turn.outcome, turn.outcome_reason = "error", str(e)
            turn.finished_at = now()
            outcomes.mark(turn, "infra_error", turnlogin.INFRA_REASON)
            self.fleet.audit(f"session:{s.id}", "session.turn_refused", s.id, reason=turnlogin.INFRA_REASON,
                             harness=harness)
            self._save(s)
            raise

    @observe.turn_begin  # --- observe: the turn's invoke_agent span
    async def begin_turn(self, s: Session, turn: SessionTurn, prompt: str | None = None,
                         extra_env: dict[str, str] | None = None, timeout_minutes: int | None = None,
                         handoff: bool = True) -> None:
        """Start one agent turn in the session's (running) sandbox, resuming the harness's own conversation when
        it can. `prompt` overrides the session's prompt (jobs write their own); handoff=False when that prompt
        already carries the history."""
        sb = self.fleet.get(s.sandbox_id or "")
        t0 = now()  # --- clouddoctor: turn start, for `hangar status`
        await self._check_login(s, turn, sb)  # --- #293: no login for the harness: refused, not an agent failure
        # The sandbox must outlive this turn (the idle loop skips busy sessions, so extend now).
        tmo = timeout_minutes if timeout_minutes is not None else self.settings.session_turn_timeout_minutes
        want = now() + (tmo + max(s.idle_minutes, 30) + 30) * 60
        if sb.expires_at is not None and sb.expires_at < want:
            await self.fleet.extend(sb.id, int((want - sb.expires_at) / 60) + 1, actor=f"session:{s.id}")
        from hangar.strategies import claim_conversation, fork_turn_env  # workstream D (import cycle)

        h = get_harness(s.harness)
        env: dict[str, str] = {}
        pins.before_turn(self, s, sb, turn)  # --- canary: another CLI version than the conversation's hands off
        claim_conversation(self, s)
        if self.will_resume(s):
            turn.mode = "resume"
            env["HANGAR_SESSION_RESUME"] = s.harness_session
        else:
            turn.mode = "handoff" if len(s.turns) > 1 else "new"
            if h.resume == "preset":
                env["HANGAR_SESSION_NEW"] = str(uuid.uuid4())
            if forked := fork_turn_env(s, h):
                turn.mode = "fork"
                env.update(forked)
        turn.harness = s.harness
        turn.conversation = env.get("HANGAR_SESSION_RESUME") or env.get("HANGAR_SESSION_NEW")
        self._save(s)
        if prompt is not None and turn.mode == "handoff" and handoff:
            prompt = self._handoff(s) + "\n\n# Now\n\n" + prompt
        # --- overnight/cache: the turn's prompt-cache TTL; a best-of-N or fork sibling waits for the first to answer
        cache_env, turn.cache_ttl, why = caching.turn_env(self, s, h)
        env.update(cache_env)
        log.debug("session %s turn %d: prompt cache %s (%s)", s.id, len(s.turns), turn.cache_ttl, why)
        try:
            if waited := await caching.STAGGER.wait(s.id, self.settings.cache_stagger_seconds):
                turn.cache_wait_seconds = round(waited, 1)
            run_id, command = await steering.prepare(self, s, turn, sb, h,  # workstream steer: live or one-shot
                                                     env={**(extra_env or {}), **env})  # --- harden: the gate
        except BaseException:
            caching.STAGGER.responded(s.id)
            raise
        if not turn.live:  # --- #165: a one-shot turn has no driver to hash its trees: its start tree, here
            await gittree.oneshot(self, s, turn, sb, run_id, "start")
        probe = manifest.start_probe(self, s, sb)  # --- interfaces: the instructions and skills the turn will see
        # The probe must read the sandbox before the agent can change it, or a skill the agent edits in this turn
        # shows up as what the turn started with (and freezing that turn is refused). Wait briefly; a slow probe
        # goes on in the background and the manifest says what it could read.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(probe), manifest.PROBE_WAIT_SECONDS)
        try:
            io_env = await exchange.prepare_turn(self, s, turn, sb)  # workstream E: HANGAR_IO, attachments, local work
            text = prompt if prompt is not None else self._prompt(s, turn, turn.mode)  # --- interfaces: kept
            res = await self.fleet.run_agent(sb.id, text,
                                             harness=s.harness, model=s.model,
                                             env={**(extra_env or {}), **io_env, **env}, actor=f"session:{s.id}",
                                             run_id=run_id, command=command)
        except BaseException:
            probe.cancel()  # --- interfaces
            steering.forget(self, run_id)  # workstream steer: the run never started
            caching.STAGGER.responded(s.id)  # --- overnight/cache: nor will it answer; siblings go ahead
            raise
        turn.run_id, turn.started_at = res["run_id"], now()
        self._taint_begin(s, turn)  # --- taint (#129): inherited, the turn's context, the job's input
        slo.record("turn_start", turn.started_at - t0 - (turn.cache_wait_seconds or 0))  # --- clouddoctor
        turn.harness_version = res.get("harness_version")  # --- images
        self._spawn(manifest.record(self, s, turn, sb, probe, text, {**(extra_env or {}), **env}))  # --- interfaces
        turn.context = exchange.strip(turn.context)
        s.model = s.model or res.get("model")
        s.last_active_at = now()
        self._set(s, SessionState.RUNNING, f"agent turn {len(s.turns)} running ({turn.mode})")

    def _login_rejected(self, s: Session, turn: SessionTurn) -> None:
        """--- KAN-178: a turn whose login the vendor refused marks the sandbox's login for its harness failed
        (credhealth.login_rejected), so health, `hangar doctor` and its owner's inbox stop saying it works."""
        from hangar import credhealth

        try:
            sb = self.fleet.store.get(Sandbox, s.sandbox_id) if s.sandbox_id else None
            line = credhealth.login_rejected(self.fleet.store, list(sb.credentials) if sb else [], turn.harness,
                                             turn.run_id)
            log.warning("session %s: turn %s: %s", s.id, turn.run_id, line)
        except Exception:  # noqa: BLE001 - the turn's outcome stands either way
            log.warning("session %s: recording a refused login failed", s.id, exc_info=True)

    @observe.turn_end  # --- observe: closes it; turn duration, tokens, cost and budget stops in /metrics
    async def end_turn(self, s: Session, turn: SessionTurn, timeout_minutes: int | None = None,
                       should_cancel: Callable[[], bool] | None = None) -> tuple[str, str]:
        """Wait for the turn, then record its summary and conversation id and checkpoint.
        Returns (outcome, raw log): outcome is exited | timed_out | cancelled | gone (| needs_input, workstream E)."""
        tmo = timeout_minutes if timeout_minutes is not None else self.settings.session_turn_timeout_minutes
        deadline = (turn.started_at or now()) + tmo * 60
        errors = 0
        outcome = "exited"
        # --- workstream C: collect events and usage from the run log as it grows; stop the turn over budget.
        watch = self.activity.watch(self.fleet, s, turn, lambda: self._save(s),
                                    taint_files=self._untrusted_files(s, turn),
                                    on_taint=lambda ref: self._tainted(s, turn, ref))  # --- taint (#129)
        over: str | None = None
        wait = 0.0  # the first look is immediate; later ones wait in the sandbox for the turn to end (perf)
        while True:
            if not s.sandbox_id or not self._alive(s.sandbox_id):
                outcome = "gone"
                break
            if should_cancel and should_cancel():
                outcome = "cancelled"
                break
            try:
                st = await self._turn_status(s, turn, wait, watch)
                errors = 0
            except Exception:  # noqa: BLE001 - transient provider errors
                errors += 1
                if errors >= 5:
                    outcome = "gone" if not self._alive(s.sandbox_id or "") else "error"
                    break
                st = None
                await asyncio.sleep(self.settings.session_poll_seconds)
            wait = self.settings.session_poll_seconds
            if st and st["state"] == "finished":
                turn.exit_code = st["exit_code"]
                break
            if now() > deadline:
                outcome = "timed_out"
                break
            if over := await watch.poll():  # workstream C
                outcome = "over_budget"
                break
            if harness_thaw.stops(self, s, turn):  # --- harness locks (#134): HANGAR_HARNESS_DRIFT=fail
                outcome = "failed"
                break
            if self.env_requests is not None:  # --- computer: what the agent asks its owner for, while it works
                await self.env_requests.tick(s, turn)
            if self.governed is not None:  # --- KAN-79: the calls a governed turn's driver holds go to Tower
                self.governed.tick(s, turn)
        if outcome == "exited" and await harness_thaw.stops_after(self, s, turn):  # --- harness locks (#134)
            outcome = "failed"
        turn.finished_at = now()
        if outcome == "exited" and turn.exit_code == 130:  # --- workstream steer: stopped by an interrupt
            outcome = "interrupted"
        steering.forget(self, turn.run_id if turn.live else None)
        turn.outcome, turn.outcome_reason = outcome, over  # workstream C
        if outcome == "failed":  # --- harness locks (#134): until part B's infra_error outcome
            turn.outcome_reason = harness_thaw.DRIFT_REASON
        if over:
            watch.note("error", f"stopped: over budget ({over})", reason="over_budget")
        if outcome == "gone":
            await watch.finish(alive=False)
            gone_sb = self.fleet.store.get(Sandbox, s.sandbox_id) if s.sandbox_id else None  # --- #166: why it went
            outcomes.mark(turn, *outcomes.classify_turn(outcome, turn.exit_code, harness=turn.harness,
                                                         **outcomes.gone_context(self.fleet, gone_sb)))
            outbox.of(self.fleet).turn_ended(self, s, turn, outcome)  # --- outbox (#163): after the class is set
            s.sandbox_id = None if not self._alive(s.sandbox_id or "") else s.sandbox_id
            self._set(s, SessionState.SUSPENDED if s.checkpoints else SessionState.FAILED,
                      "the sandbox went away during the turn")
            turn.outcome = outcome  # workstream E
            flightrec.of(self.fleet).turn_ended(self, s, turn, outcome, "")  # --- clouddoctor
            self._eval_turn(s, turn)  # --- #181
            await self._close_tape(s, turn)  # --- #177
            outbox.of(self.fleet).session_turn(self, s, turn)  # --- outbox (#163): off the turn's path
            return outcome, ""
        if outcome in ("cancelled", "timed_out", "error", "over_budget", "failed"):
            # Never leave an agent running that nobody is watching (it would keep working, and spending).
            with contextlib.suppress(Exception):
                ksb, kprov = self.fleet._running(s.sandbox_id or "")
                await isolation.kill(kprov, ksb, self.settings, turn.run_id or "")  # --- isolation: where it runs
            turn.exit_code = turn.exit_code if turn.exit_code is not None else -1
        outbox.of(self.fleet).turn_ended(self, s, turn, outcome)  # --- outbox (#163): reads its patch meanwhile
        raw = ""
        try:
            sb = self.fleet.get(s.sandbox_id)
            _, prov = self.fleet._running(sb.id)
            split = isolation.run_split(sb, turn.run_id)  # --- harden: a split run's own log, not the agent's link
            rlog = f"{isolation.runs_dir(self.settings, split)}/{turn.run_id}.log"
            # The log's end, never all of it: the agent decides how big it gets.
            tail = f"tail -c 2000000 {shlex.quote(rlog)} > {shlex.quote(rlog)}.tail"
            await (isolation.sh(prov, sb, tail) if split else self._sh(sb, tail))
            raw = (await prov.read_file(sb, f"{rlog}.tail", limit=2_000_000) or b"")[-2_000_000:].decode(
                errors="replace")
            turn.summary = self.fleet.broker.redact(extract_summary(
                turn.harness, raw, outcome=outcome, reason=turn.outcome_reason, started_at=turn.started_at,
                finished_at=turn.finished_at) or "") or None  # KAN-137: a stopped turn's is built from its events
            await self._record_conversation(s, sb, turn, raw)
        except Exception as e:  # noqa: BLE001 - the work is in the sandbox; don't lose the turn over its log
            log.warning("session %s: could not read turn %s output: %s", s.id, turn.run_id, e)
        await watch.finish()  # workstream C: whatever the last polls missed
        # workstream E: result, question (-> needs_input), artifacts; before the checkpoint so it has them all
        outcome = await exchange.after_turn(self, s, turn, outcome)
        outcomes.mark(turn, *outcomes.classify_turn(outcome, turn.exit_code, raw=raw, harness=turn.harness))  # #166
        if turn.infra_reason == "login_rejected":  # --- KAN-178: the vendor refused the login; mark it, say what to do
            self._login_rejected(s, turn)
        if not turn.live and s.sandbox_id and self._alive(s.sandbox_id):  # --- #165: and its end tree
            await gittree.oneshot(self, s, turn, self.fleet.get(s.sandbox_id), turn.run_id, "end")
            self._save(s)
        if self.env_requests is not None:  # --- computer: the last requests, and screenshots/demos as events
            await self.env_requests.scan(s, turn)
        computer.note_media(self, s, turn)
        try:
            async with self._locks[s.id]:
                await self._checkpoint(s, "turn")
        except Exception as e:  # noqa: BLE001 - the next interval/suspend checkpoint retries
            log.warning("session %s: checkpoint after turn failed: %s", s.id, e)
        s.last_active_at = now()
        if s.sandbox_id and self._alive(s.sandbox_id):
            self._set(s, SessionState.IDLE, f"turn {len(s.turns)} {outcome} (exit {turn.exit_code})")
        else:  # destroyed while we were finishing up (the pre-destroy hook checkpointed it)
            s.sandbox_id = None
            self._set(s, SessionState.SUSPENDED, f"turn {len(s.turns)} finished; sandbox released")
        # --- clouddoctor: the session is idle: a failed or slow turn's record (its log is read and sealed off-turn)
        flightrec.of(self.fleet).turn_ended(self, s, turn, outcome, raw)
        self._eval_turn(s, turn)  # --- #181: the provider's token rate, for the eval queue
        await self._close_tape(s, turn)  # --- #177: the model gateway's tape of this turn, to the outbox
        outbox.of(self.fleet).session_turn(self, s, turn)  # --- outbox (#163): off the turn's path
        return outcome, raw

    def _eval_turn(self, s: Session, turn: SessionTurn) -> None:
        """--- #181: a turn's usage counts towards its provider's token rate (evalqueue.Meter)."""
        from hangar import evalqueue

        job = self.job_of(s.job_id) if s.job_id and self.job_of is not None else None
        evalqueue.turn_ended(self.fleet, s, turn, job)

    async def _close_tape(self, s: Session, turn: SessionTurn) -> None:
        """--- #177: a turn of a `model_gateway: record` sandbox ends: its tape gets its final index (modelgw.py)."""
        from hangar import modelgw

        gw = modelgw.of(self.fleet)
        if not gw.tapes and not gw._pending:
            return
        try:
            job = self.job_of(s.job_id) if s.job_id and self.job_of is not None else None
            await gw.turn_ended(s, turn, job)
        except Exception:  # noqa: BLE001 - a tape never fails the turn
            log.exception("session %s: could not close the model gateway tape", s.id)

    async def _turn_status(self, s: Session, turn: SessionTurn, wait: float, watch: Any) -> dict[str, Any]:
        """The run's status after waiting up to `wait` seconds in the sandbox for it to finish (or, before its first
        output, to print something): the end of a turn is noticed within ~0.2 s, not after up to a whole poll
        interval, for the same number of round trips (perf). A wait holds a worker thread, so when half the pool
        is already waiting the fleet sleeps instead (review: one bound shared with MCP clients' long-polls)."""
        grow = 0 if watch.parser.next == 0 else None
        if wait and self.fleet.closing.is_set():
            # review(sweep): shutting down, agent_status no longer waits in the sandbox; wait here instead of
            # polling back to back until the app's shutdown cancels this
            await asyncio.sleep(wait)
        return await self.fleet.agent_status(s.sandbox_id or "", turn.run_id or "", tail_lines=5, quiet=True,
                                             wait_seconds=wait, grow_from=grow)

    async def _record_conversation(self, s: Session, sb: Sandbox, turn: SessionTurn, raw_log: str) -> None:
        """Remember the harness's conversation id so the next turn resumes it."""
        h = get_harness(turn.harness or s.harness)
        cid: str | None = turn.conversation
        if h.resume == "capture" and h.session_id_regex:
            m = re.search(h.session_id_regex, raw_log)
            cid = m.group(1) if m else cid
        if cid and h.session_dirs:
            # Only keep the id if the harness actually saved that conversation (a crash may not have; agy starts a
            # fresh one when asked to resume an id it doesn't know).
            dirs = " ".join(shlex.quote(f"/home/{self.settings.sandbox_user}/{d}") for d in h.session_dirs)
            res = await self._sh(sb, f"find {dirs} -type f -name {shlex.quote('*' + cid + '*')} 2>/dev/null | head -1")
            if not res.stdout.strip():
                cid = None
        turn.conversation = cid
        s.harness_session = cid
        s.harness_session_for = (turn.harness or s.harness) if cid else None
        if cid:
            s.fork_conversation = None  # a forked session has its own conversation now
        elif turn.mode == "fork":
            s.harness_session_for = turn.harness or s.harness  # the fork didn't take: fork again next turn
        self._save(s)

    # ================================================================== internals: suspend / idle
    async def _suspend_task(self, sid: str, why: str, idle: bool = False) -> None:
        s = self._sessions[sid]
        try:
            async with self._locks[sid]:
                if idle and self._holds[sid] > 0:
                    return  # someone took hold while we were deciding; it's not idle any more
                await self._suspend(s, why)
        except Exception as e:  # noqa: BLE001
            log.exception("session %s failed to suspend", sid)
            self._set(s, SessionState.IDLE if self._alive(s.sandbox_id or "") else SessionState.SUSPENDED,
                      f"suspend failed: {e}")
        finally:
            self._busy.discard(sid)

    async def _suspend(self, s: Session, why: str) -> None:
        if not s.sandbox_id:
            return
        self._set(s, SessionState.SUSPENDING, why)
        ck = await self._checkpoint(s, "suspend")
        if ck is None and not s.checkpoints and self._alive(s.sandbox_id):
            raise SessionError("could not checkpoint; keeping the sandbox")
        sid = s.sandbox_id
        s.sandbox_id = None
        self._set(s, SessionState.SUSPENDED, why)
        if await hot_resume.park(self, s, sid):  # --- providers-e2b: paused, not destroyed, after the checkpoint
            return
        with contextlib.suppress(Exception):
            await self.fleet.destroy(sid, actor=f"session:{s.id}")

    async def _drop(self, prefix: str) -> None:
        try:
            await self.blobs.delete_prefix(prefix)
        except Exception as e:  # noqa: BLE001 - leftovers are harmless; log and move on
            log.warning("could not delete checkpoint data %s: %s", prefix, e)

    async def _hook(self, sb: Sandbox) -> None:
        """Fleet pre-destroy hook: checkpoint a session whose sandbox is being destroyed by someone else."""
        sid = sb.labels.get("hangar.session")
        s = self._sessions.get(sid or "")
        if s is None or s.sandbox_id != sb.id or s.state not in (SessionState.IDLE, SessionState.RUNNING):
            return  # not ours, or we are the ones destroying it (suspend / failed boot)
        ck = None
        try:
            async with asyncio.timeout(self.settings.pre_destroy_timeout_seconds - 5):
                async with self._locks[s.id]:
                    ck = await self._checkpoint(s, "destroy")
        finally:
            s.sandbox_id = None
            if ck is not None:
                why = "sandbox was destroyed; checkpointed first"
            elif s.checkpoints:
                why = "sandbox was destroyed before it could be checkpointed; resumes from the previous checkpoint"
            else:
                why = "sandbox was destroyed before anything was checkpointed; resumes from the base branch"
            self._set(s, SessionState.SUSPENDED, why)

    async def _in_use(self, sb: Sandbox) -> bool:
        """Someone is working in the sandbox: a dashboard/CLI terminal is open, a tmux client is attached (e.g.
        `hangar sb attach`), or anything used it through hangar (exec, files, terminals) since the last check."""
        if self.fleet.attached.get(sb.id):
            return True
        if sb.last_activity and now() - sb.last_activity < 90:
            return True
        rdir = f"/home/{self.settings.sandbox_user}/.hangar/runs"
        res = await self._sh(sb, f"n=0; for f in {rdir}/run-*.sh; do [ -e \"$f\" ] || continue; "
                                 "r=$(basename \"$f\" .sh); "
                                 # --- isolation: a split run's tmux is hangar's user's; its pid file says
                                 f"if [ ! -e {rdir}/$r.exit ] && {{ tmux has-session -t \"$r\" 2>/dev/null || "
                                 f"{{ p=$(cat {rdir}/$r.pid 2>/dev/null); [ -n \"$p\" ] && [ -d /proc/$p ]; }}; }}; "
                                 "then n=$((n+1)); fi; done; "
                                 "echo $(( n + $(tmux list-clients 2>/dev/null | wc -l) ))", timeout=20)
        if res.stdout.strip() not in ("", "0"):
            return True
        if not isolation.split(sb):
            return False
        # --- harden: split runs, from hangar's user's own files (their links above are the agent's to remove)
        _, prov = self.fleet._running(sb.id)
        r = isolation.RUNS
        res = await isolation.sh(prov, sb, f"n=0; for f in {r}/run-*.sh; do [ -e \"$f\" ] || continue; "
                                           "x=$(basename \"$f\" .sh); "
                                           f"if [ ! -e {r}/$x.exit ] && {{ {isolation.TMUX} has-session -t \"$x\" "
                                           f"2>/dev/null || {{ p=$(cat {r}/$x.pid 2>/dev/null); [ -n \"$p\" ] && "
                                           "[ -d /proc/$p ]; }; }; then n=$((n+1)); fi; done; "
                                           f"echo $(( n + $({isolation.TMUX} list-clients 2>/dev/null | wc -l) ))",
                                 timeout=20)
        return res.stdout.strip() not in ("", "0")

    def set_idle(self, ref: str, idle_minutes: int, requester: str) -> Session:
        """Change how long a session may sit idle before suspending (0 = stay up until suspended by hand)."""
        if idle_minutes < 0 or idle_minutes > 10080:
            raise SessionError("idle_minutes must be between 0 and 10080")
        s = self.get(ref)
        s.idle_minutes = idle_minutes
        s.last_active_at = now()
        self._save(s)
        self.fleet.audit(requester, "session.idle", s.id, idle_minutes=idle_minutes)
        return s

    async def _idle_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await self.tick()
                metrics.loop_tick("sessions")  # --- reconciler: heartbeat
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("session idle loop failed")
                metrics.loop_failed("sessions")  # --- reconciler

    async def tick(self) -> None:
        """One pass: suspend idle sessions, checkpoint changed ones, keep active sandboxes from expiring. Sessions
        are checked a few at a time (perf: one after another, each costing round trips, a pass over 50 live
        sessions took tens of seconds and delayed every suspend and periodic checkpoint behind it)."""
        gate = asyncio.Semaphore(8)

        async def one(s: Session) -> None:
            async with gate:
                try:
                    await self._tick_one(s)
                except Exception:  # noqa: BLE001 - one bad session must not stall the rest
                    log.exception("session %s: idle check failed", s.id)

        await asyncio.gather(*(one(s) for s in list(self._sessions.values())))

    async def _tick_one(self, s: Session) -> None:
        t = now()
        if s.state not in (SessionState.IDLE, SessionState.RUNNING) or not s.sandbox_id:
            return
        if not self._alive(s.sandbox_id):
            if not self.busy(s.id):
                s.sandbox_id = None
                self._set(s, SessionState.SUSPENDED, "sandbox is gone; resumes from the last checkpoint")
            return
        sb = self.fleet.get(s.sandbox_id)
        if sb.expires_at and sb.expires_at - t < 30 * 60:  # busy or not: never let the reaper take it mid-work
            await self.fleet.extend(sb.id, 60, actor=f"session:{s.id}")
        if self.busy(s.id):
            # Mid-turn: no suspend, but keep checkpointing, so a long turn isn't lost with its sandbox.
            if s.sandbox_id and not self._locks[s.id].locked() and self._checkpoint_due(s):
                async with self._locks[s.id]:
                    await self._checkpoint(s, "interval")
            return
        if await self._in_use(sb):
            s.last_active_at = now()
            self._save(s)
        if self.busy(s.id):  # a send landed while we were checking
            return
        if s.idle_minutes and now() - s.last_active_at > s.idle_minutes * 60:
            self._busy.add(s.id)
            await self._suspend_task(s.id, f"idle for {s.idle_minutes} min", idle=True)
            return
        if self._checkpoint_due(s):
            async with self._locks[s.id]:
                await self._checkpoint(s, "interval")

    def _checkpoint_due(self, s: Session) -> bool:
        last = max(s.checkpoints[-1].ts if s.checkpoints else s.created_at, self._last_checked.get(s.id, 0))
        return now() - last > self.settings.session_checkpoint_minutes * 60

