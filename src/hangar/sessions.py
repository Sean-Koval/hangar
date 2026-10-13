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
import shutil
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hangar.fleet import Fleet, FleetError, _clip_tail
from hangar.harnesses import get_harness
from hangar.models import (
    SESSION_LIVE,
    Checkpoint,
    Sandbox,
    SandboxState,
    Session,
    SessionState,
    SessionTurn,
    now,
)
from hangar.vault import Vault

log = logging.getLogger("hangar.sessions")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_BRANCH_RE = re.compile(r"^(?!-)(?!/)(?!.*\.\.)(?!.*//)(?!.*@\{)[A-Za-z0-9._/-]{1,200}(?<!/)(?<!\.lock)(?<!\.)$")
MAX_MESSAGE = 64_000
PARTS = ("agent.tgz", "repo.bundle", "wip.patch", "untracked.tgz")
CKPT_REF = "refs/hangar/checkpoint"
CKPT_DIR = "/tmp/hangar-ckpt"
RESTORE_DIR = "/tmp/hangar-restore"


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


def extract_summary(harness: str | None, log_text: str) -> str | None:
    """The agent's last message from a run log (best effort per harness; falls back to the output tail)."""
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
    if last:
        return last.strip()[:8000]
    tail = _clip_tail(log_text)[-2000:].strip()
    return tail or None


class SessionService:
    def __init__(self, fleet: Fleet, vault: Vault):
        self.fleet = fleet
        self.settings = fleet.settings
        self.store = fleet.store
        self.vault = vault
        self.root = self.settings.data_dir / "sessions"
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self._busy: set[str] = set()  # sessions with a turn or boot in flight
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.Task | None = None
        self._last_checked: dict[str, float] = {}  # last time a checkpoint ran (taken or found unchanged)

    # ================================================================== lifecycle
    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._sessions = {s.id: s for s in self.store.list(Session)}
        for s in self._sessions.values():
            if s.state in SESSION_LIVE or s.state == SessionState.SUSPENDING:
                if s.sandbox_id and self._alive(s.sandbox_id):
                    if s.state == SessionState.RUNNING and s.turns and s.turns[-1].run_id:
                        self._spawn(self._resume_wait(s.id))  # re-attach to the running turn
                    else:
                        self._set(s, SessionState.IDLE, "control plane restarted")
                else:
                    s.sandbox_id = None
                    self._set(s, SessionState.SUSPENDED, "control plane restarted; resuming from the last checkpoint")
        if self._hook not in self.fleet.pre_destroy:
            self.fleet.pre_destroy.append(self._hook)
        self._loop = asyncio.create_task(self._idle_loop())

    async def stop(self) -> None:
        with contextlib.suppress(ValueError):
            self.fleet.pre_destroy.remove(self._hook)
        for t in [*self._tasks, *([self._loop] if self._loop else [])]:
            t.cancel()
        for t in [*self._tasks, *([self._loop] if self._loop else [])]:
            with contextlib.suppress(BaseException):
                await t
        self._tasks.clear()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ================================================================== public API
    def get(self, ref: str) -> Session:
        s = self._sessions.get(ref) or next(
            (x for x in self._sessions.values() if x.name == ref and x.state != SessionState.ARCHIVED), None)
        if s is None:
            raise SessionNotFound(f"no session '{ref}'")
        return s

    def list(self, include_archived: bool = False, limit: int = 100) -> list[Session]:
        out = [s for s in self._sessions.values() if include_archived or s.state != SessionState.ARCHIVED]
        return sorted(out, key=lambda s: s.last_active_at, reverse=True)[: max(1, min(limit, 500))]

    def new(self, req: NewSession, owner: str, requester: str) -> Session:
        if not _NAME_RE.match(req.name):
            raise SessionError("name: lowercase letters, digits, '.', '_' and '-' (start with a letter or digit)")
        if any(s.name == req.name and s.state != SessionState.ARCHIVED for s in self._sessions.values()):
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
        branch = req.branch or f"hangar/session/{req.name}"
        if not _BRANCH_RE.match(branch) or (req.base_branch and req.base_branch == branch):
            raise SessionError(f"invalid branch '{branch}'")
        s = Session(name=req.name, title=(req.title or (req.message or req.name).strip().splitlines()[0])[:200],
                    owner=owner, environment=env, harness=harness, model=req.model,
                    repo=req.repo or (spec.repo.url if spec.repo else None), base_branch=req.base_branch,
                    branch=branch, labels=req.labels,
                    idle_minutes=(req.idle_minutes if req.idle_minutes is not None
                                  else self.settings.session_idle_minutes))
        self._sessions[s.id] = s
        self._set(s, SessionState.STARTING, f"created by {requester}")
        self.fleet.audit(requester, "session.new", s.id, name=s.name, environment=env, repo=s.repo)
        if req.message:
            s.turns.append(SessionTurn(message=req.message, requester=requester))
            self._save(s)
            self._busy.add(s.id)
            self._spawn(self._turn(s.id))
        else:
            self._busy.add(s.id)
            self._spawn(self._boot_task(s.id))
        return s

    def open(self, ref: str, requester: str, harness: str | None = None) -> Session:
        s = self.get(ref)
        if s.state == SessionState.ARCHIVED:
            raise SessionError(f"session {s.name} is archived")
        live = bool(s.sandbox_id and self._alive(s.sandbox_id))
        if harness and harness != s.harness and (live or s.id in self._busy):
            raise SessionError(f"session {s.name} is running {s.harness}; suspend it before switching to {harness}")
        if harness and harness != s.harness:
            spec = self.fleet.catalog.get(s.environment)
            try:
                s.harness = spec.pick_harness(harness)
            except ValueError as e:
                raise SessionError(str(e)) from e
            self._save(s)
        if s.id in self._busy or (live and s.state in SESSION_LIVE):
            return s
        self._busy.add(s.id)
        self._set(s, SessionState.STARTING, f"opened by {requester}")
        self.fleet.audit(requester, "session.open", s.id)
        self._spawn(self._boot_task(s.id))
        return s

    def send(self, ref: str, message: str, requester: str) -> Session:
        s = self.get(ref)
        if not message.strip():
            raise SessionError("message is empty")
        if len(message) > MAX_MESSAGE:
            raise SessionError("message is too long")
        if s.state == SessionState.ARCHIVED:
            raise SessionError(f"session {s.name} is archived")
        if s.id in self._busy or s.state in (SessionState.RUNNING, SessionState.SUSPENDING):
            raise SessionError(f"session {s.name} is busy ({s.state}); wait for it to finish")
        s.turns.append(SessionTurn(message=message, requester=requester))
        s.last_active_at = now()
        self._busy.add(s.id)
        self._set(s, SessionState.RUNNING if s.sandbox_id and self._alive(s.sandbox_id) else SessionState.STARTING,
                  f"message from {requester}")
        self.fleet.audit(requester, "session.send", s.id, message=message[:300])
        self._spawn(self._turn(s.id))
        return s

    def suspend(self, ref: str, requester: str) -> Session:
        s = self.get(ref)
        if s.id in self._busy:
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
        if s.sandbox_id or s.id in self._busy:
            raise SessionError(f"suspend session {s.name} before archiving it")
        shutil.rmtree(self.root / s.id, ignore_errors=True)
        s.checkpoints = []
        self._set(s, SessionState.ARCHIVED, f"archived by {requester}")
        self.fleet.audit(requester, "session.archive", s.id)
        return s

    def delete(self, ref: str, requester: str) -> None:
        s = self.archive(ref, requester)
        self.store.delete(Session, s.id)
        self._sessions.pop(s.id, None)

    def view(self, s: Session) -> dict[str, Any]:
        d = s.model_dump(mode="json", exclude={"checkpoints"})
        h = get_harness(s.harness) if s.harness else None
        cps = s.checkpoints
        d["checkpoints"] = [c.model_dump(mode="json") for c in cps[-3:]]
        d["checkpoint_count"] = len(cps)
        d["last_checkpoint_at"] = cps[-1].ts if cps else None
        d["busy"] = s.id in self._busy
        last = s.turns[-1] if s.turns else None
        d["terminal_session"] = last.run_id if last else None
        d["last_summary"] = next((t.summary for t in reversed(s.turns) if t.summary), None)
        d["resume_command"] = (h.resume_hint.format(id=s.harness_session)
                               if h and h.resume_hint and s.harness_session and s.harness_session_for == s.harness
                               else None)
        return d

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

    async def _boot(self, s: Session) -> Sandbox:
        """Attach a running sandbox to the session, restoring its last checkpoint. Caller holds the lock."""
        if s.sandbox_id and self._alive(s.sandbox_id):
            return self.fleet.get(s.sandbox_id)
        s.sandbox_id = None
        self._set(s, SessionState.STARTING, "starting a sandbox" + (" and restoring the last checkpoint"
                                                                     if s.checkpoints else ""))
        ttl = max(s.idle_minutes * 3, 120) + self.settings.session_turn_timeout_minutes
        sb = await self.fleet.create(
            s.environment, name=f"ses-{s.name[:30]}-{uuid.uuid4().hex[:4]}", repo=s.repo, branch=s.base_branch,
            harness=s.harness, ttl_minutes=ttl, labels={**s.labels, "hangar.session": s.id}, actor=f"session:{s.id}")
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
        self._set(s, SessionState.IDLE, note or ("ready" + (" (restored)" if s.checkpoints else "")))
        return sb

    async def _sh(self, sb: Sandbox, cmd: str, timeout: int = 300, cwd: str | None = None):
        return await self.fleet.exec(sb.id, cmd, cwd=cwd, timeout=timeout, actor="session")

    async def _restore(self, s: Session, sb: Sandbox) -> str | None:
        q = shlex.quote
        repo = sb.info["repo_path"]
        if not s.base_sha:
            res = await self._sh(sb, "git rev-parse --verify -q HEAD", cwd=repo)
            s.base_sha = res.stdout.strip() if res.exit_code == 0 and res.stdout.strip() else None
            if not s.base_branch:
                res = await self._sh(sb, "git rev-parse --abbrev-ref HEAD", cwd=repo)
                s.base_branch = res.stdout.strip() or None
        await self._sh(sb, "git config user.email >/dev/null || git config user.email hangar@localhost; "
                           "git config user.name >/dev/null || git config user.name hangar", cwd=repo)
        if not s.checkpoints:
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

    async def _restore_checkpoint(self, s: Session, sb: Sandbox, repo: str, ck: Checkpoint) -> None:
        q = shlex.quote
        _, prov = self.fleet._running(sb.id)
        d = self.root / s.id / ck.id
        present: set[str] = set()
        for part in PARTS:
            f = d / f"{part}.enc"
            if f.exists():
                data = await asyncio.to_thread(lambda f=f: self.vault.decrypt(f.read_bytes()))
                await prov.write_file(sb, f"{RESTORE_DIR}/{part}", data, mode=0o600)
                present.add(part)
        r, home = RESTORE_DIR, f"/home/{self.settings.sandbox_user}"
        base = ck.base or s.base_sha or "HEAD"
        target = q(ck.head or base)
        steps = [
            # Environment setup may have touched tracked files; start from a clean tree (ignored files stay).
            "git reset -q --hard && git clean -fdq",
            (f"git fetch -q {r}/repo.bundle {CKPT_REF} && git checkout -q -B {q(s.branch)} FETCH_HEAD"
             if "repo.bundle" in present else
             f"(git cat-file -e {target}^{{commit}} 2>/dev/null || git fetch -q origin {target})"
             f" && git checkout -q -B {q(s.branch)} {target}"),
        ]
        if "wip.patch" in present:
            steps.append(f"if [ -s {r}/wip.patch ]; then git apply --binary --whitespace=nowarn {r}/wip.patch; fi")
        if "untracked.tgz" in present:
            steps.append(f"tar xzf {r}/untracked.tgz -C {q(repo)}")
        if "agent.tgz" in present:
            steps.append(f"tar xzf {r}/agent.tgz -C {q(home)}")
        steps.append(f"rm -rf {r}")
        res = await self._sh(sb, " && ".join(steps), cwd=repo, timeout=600)
        if res.exit_code != 0:
            raise SessionError(f"restore failed: {(res.stderr or res.stdout)[-600:]}")
        head = (await self._sh(sb, "git rev-parse HEAD", cwd=repo)).stdout.strip()
        if ck.head and head != ck.head:
            raise SessionError(f"restore ended at {head[:12]}, expected {ck.head[:12]}")

    # ================================================================== internals: checkpoint
    def _agent_dirs(self, s: Session) -> list[str]:
        dirs: list[str] = []
        for t in {s.harness, s.harness_session_for} - {None}:
            with contextlib.suppress(KeyError):
                dirs += get_harness(t).session_dirs
        return sorted(set(dirs))

    async def _checkpoint(self, s: Session, reason: str) -> Checkpoint | None:
        """Snapshot conversation + repo state into the control plane. Caller holds the lock."""
        if not s.sandbox_id or not self._alive(s.sandbox_id):
            return None
        sb = self.fleet.get(s.sandbox_id)
        repo = sb.info.get("repo_path")
        q = shlex.quote
        c, home = CKPT_DIR, f"/home/{self.settings.sandbox_user}"
        dirs = " ".join(q(x) for x in self._agent_dirs(s))
        last = s.checkpoints[-1] if s.checkpoints else None
        base = s.base_sha or "HEAD"
        max_untracked = self.settings.session_max_untracked_mb * 1024 * 1024
        script = [f"rm -rf {c} && mkdir -p {c} && chmod 700 {c}"]
        if repo:
            # Read HEAD once and pin it, so a commit landing mid-checkpoint can't make head and bundle disagree.
            script += [
                f"cd {q(repo)}",
                f"H=$(git rev-parse --verify -q HEAD) && git update-ref {CKPT_REF} $H && echo $H > {c}/head",
                f"if [ -n \"$H\" ]; then git bundle create -q {c}/repo.bundle {CKPT_REF} --not --remotes "
                f"2>/dev/null || git bundle create -q {c}/repo.bundle {CKPT_REF} --not {q(base)} 2>/dev/null "
                f"|| rm -f {c}/repo.bundle; fi",
                f"if [ -n \"$H\" ]; then git diff --binary $H > {c}/wip.patch; fi",
                f"git ls-files -z --others --exclude-standard > {c}/untracked.list",
                f"if [ -s {c}/untracked.list ]; then tar --null -T {c}/untracked.list -czf {c}/untracked.tgz; "
                f"xargs -0 stat -c '%n %s %Y' < {c}/untracked.list > {c}/untracked.stat 2>/dev/null; fi",
            ]
        if dirs:
            script.append(f"cd {q(home)} && ls -d {dirs} >/dev/null 2>&1 && "
                          f"tar czf {c}/agent.tgz --ignore-failed-read $(ls -d {dirs} 2>/dev/null) || true")
        # fingerprint: HEAD + tree diff + untracked names + conversation file listing
        script.append(f"{{ cat {c}/head 2>/dev/null; sha1sum {c}/wip.patch 2>/dev/null | cut -c1-40; "
                      f"cat {c}/untracked.stat 2>/dev/null; "
                      f"cd {q(home)} && find {dirs or '/dev/null'} -type f -printf '%P %s %T@\\n' 2>/dev/null"
                      " | sort; }"
                      f" | sha1sum | cut -c1-40 > {c}/fingerprint")
        script.append(f"stat -c '%s %n' {c}/* 2>/dev/null || true")
        timeout = int(max(30, min(600, self.settings.pre_destroy_timeout_seconds - 30)))
        res = await self._sh(sb, " && ".join(script[:1]) + "; " + "; ".join(script[1:]), timeout=timeout)
        _, prov = self.fleet._running(sb.id)
        fp = (await prov.read_file(sb, f"{c}/fingerprint") or b"").decode().strip() or None
        head = (await prov.read_file(sb, f"{c}/head") or b"").decode().strip() or None
        if reason == "interval" and last and fp and fp == last.fingerprint:
            self._last_checked[s.id] = now()  # nothing changed; don't re-run the whole thing every minute
            return None
        sizes = {}
        for ln in res.stdout.splitlines():
            parts = ln.split()
            if len(parts) == 2 and parts[0].isdigit():
                sizes[Path(parts[1]).name] = int(parts[0])
        ck = Checkpoint(reason=reason, head=head if head and head != "HEAD" else None, base=s.base_sha,
                        branch=s.branch, fingerprint=fp)
        d = self.root / s.id / ck.id
        d.mkdir(parents=True, exist_ok=True)
        max_part = self.settings.session_max_part_mb * 1024 * 1024
        try:
            for part in PARTS:
                if part not in sizes:
                    continue
                cap = max_untracked if part == "untracked.tgz" else max_part
                if sizes[part] > cap:
                    ck.warnings.append(f"{part} is {sizes[part] // 1024 // 1024} MB, over the "
                                       f"{cap // 1024 // 1024} MB limit; not checkpointed")
                    continue
                data = await prov.read_file(sb, f"{c}/{part}")
                if data is None:
                    ck.warnings.append(f"could not read {part}")
                    continue
                blob = await asyncio.to_thread(self.vault.encrypt, data)
                await asyncio.to_thread((d / f"{part}.enc").write_bytes, blob)
                ck.parts[part] = len(data)
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, d, True)  # never leave an unrecorded partial checkpoint
            raise
        with contextlib.suppress(Exception):
            await self._sh(sb, f"rm -rf {c}")
        s.checkpoints.append(ck)
        keep = max(1, self.settings.session_keep_checkpoints)
        for old in s.checkpoints[:-keep]:
            await asyncio.to_thread(shutil.rmtree, self.root / s.id / old.id, True)
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
        return "\n\n".join(parts)

    async def _turn(self, sid: str) -> None:
        s = self._sessions[sid]
        turn = s.turns[-1]
        try:
            async with self._locks[sid]:
                sb = await self._boot(s)
            # The sandbox must outlive this turn (the idle loop skips busy sessions, so extend now).
            want = now() + (self.settings.session_turn_timeout_minutes + max(s.idle_minutes, 30) + 30) * 60
            if sb.expires_at is not None and sb.expires_at < want:
                await self.fleet.extend(sb.id, int((want - sb.expires_at) / 60) + 1, actor=f"session:{s.id}")
            h = get_harness(s.harness)
            env: dict[str, str] = {}
            same = s.harness_session and s.harness_session_for == s.harness
            if h.resume and same:
                turn.mode = "resume"
                env["HANGAR_SESSION_RESUME"] = s.harness_session
            else:
                turn.mode = "handoff" if len(s.turns) > 1 else "new"
                if h.resume == "preset":
                    env["HANGAR_SESSION_NEW"] = str(uuid.uuid4())
            turn.harness = s.harness
            turn.conversation = env.get("HANGAR_SESSION_RESUME") or env.get("HANGAR_SESSION_NEW")
            self._save(s)
            res = await self.fleet.run_agent(sb.id, self._prompt(s, turn, turn.mode), harness=s.harness,
                                             model=s.model, env=env, actor=f"session:{s.id}")
            turn.run_id, turn.started_at = res["run_id"], now()
            s.model = s.model or res.get("model")
            self._set(s, SessionState.RUNNING, f"agent turn {len(s.turns)} running ({turn.mode})")
            await self._wait_turn(s, turn, env)
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
            await self._wait_turn(s, s.turns[-1], {})
        except Exception as e:  # noqa: BLE001
            self._set(s, SessionState.IDLE if self._alive(s.sandbox_id or "") else SessionState.SUSPENDED,
                      f"turn interrupted: {e}")
        finally:
            self._busy.discard(sid)

    async def _wait_turn(self, s: Session, turn: SessionTurn, env: dict[str, str]) -> None:
        deadline = (turn.started_at or now()) + self.settings.session_turn_timeout_minutes * 60
        errors = 0
        while True:
            if not s.sandbox_id or not self._alive(s.sandbox_id):
                raise SessionError("the sandbox went away during the turn")
            try:
                st = await self.fleet.agent_status(s.sandbox_id, turn.run_id or "", tail_lines=5)
                errors = 0
            except Exception:  # noqa: BLE001
                errors += 1
                if errors >= 5:
                    raise
                st = None
            if st and st["state"] == "finished":
                turn.exit_code = st["exit_code"]
                break
            if now() > deadline:
                with contextlib.suppress(Exception):
                    await self.fleet.exec(s.sandbox_id, f"tmux kill-session -t {shlex.quote(turn.run_id or '')}")
                turn.exit_code = -1
                break
            await asyncio.sleep(self.settings.session_poll_seconds)
        turn.finished_at = now()
        sb = self.fleet.get(s.sandbox_id)
        _, prov = self.fleet._running(sb.id)
        rlog = f"/home/{self.settings.sandbox_user}/.hangar/runs/{turn.run_id}.log"
        raw = (await prov.read_file(sb, rlog) or b"")[-2_000_000:].decode(errors="replace")
        turn.summary = self.fleet.broker.redact(extract_summary(turn.harness, raw) or "") or None
        await self._record_conversation(s, sb, turn, env, raw)
        async with self._locks[s.id]:
            await self._checkpoint(s, "turn")
        s.last_active_at = now()
        if s.sandbox_id and self._alive(s.sandbox_id):
            self._set(s, SessionState.IDLE, f"turn {len(s.turns)} finished (exit {turn.exit_code})")
        else:  # destroyed while we were finishing up (the pre-destroy hook checkpointed it)
            s.sandbox_id = None
            self._set(s, SessionState.SUSPENDED, f"turn {len(s.turns)} finished; sandbox released")

    async def _record_conversation(self, s: Session, sb: Sandbox, turn: SessionTurn, env: dict[str, str],
                                   raw_log: str) -> None:
        """Remember the harness's conversation id so the next turn resumes it."""
        h = get_harness(turn.harness or s.harness)
        # No env means we re-attached after a restart: use what the turn recorded before it started.
        cid: str | None = (env.get("HANGAR_SESSION_RESUME") or env.get("HANGAR_SESSION_NEW")) if env \
            else (turn.conversation or s.harness_session)
        if h.resume == "capture" and h.session_id_regex:
            m = re.search(h.session_id_regex, raw_log)
            cid = m.group(1) if m else cid
        if cid and h.session_dirs:
            # Only keep the id if the harness actually saved that conversation (a crash may not have).
            dirs = " ".join(shlex.quote(f"/home/{self.settings.sandbox_user}/{d}") for d in h.session_dirs)
            res = await self._sh(sb, f"find {dirs} -type f -name {shlex.quote('*' + cid + '*')} 2>/dev/null | head -1")
            if not res.stdout.strip():
                cid = None
        s.harness_session = cid
        s.harness_session_for = (turn.harness or s.harness) if cid else None
        self._save(s)

    # ================================================================== internals: suspend / idle
    async def _suspend_task(self, sid: str, why: str) -> None:
        s = self._sessions[sid]
        try:
            async with self._locks[sid]:
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
        with contextlib.suppress(Exception):
            await self.fleet.destroy(sid, actor=f"session:{s.id}")

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

    async def _idle_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await self.tick()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("session idle loop failed")

    async def tick(self) -> None:
        """One pass: suspend idle sessions, checkpoint changed ones, keep active sandboxes from expiring."""
        for s in list(self._sessions.values()):
            try:
                await self._tick_one(s)
            except Exception:  # noqa: BLE001 - one bad session must not stall the rest
                log.exception("session %s: idle check failed", s.id)

    async def _tick_one(self, s: Session) -> None:
        t = now()
        if s.state not in (SessionState.IDLE, SessionState.RUNNING) or not s.sandbox_id:
            return
        if not self._alive(s.sandbox_id):
            if s.id not in self._busy:
                s.sandbox_id = None
                self._set(s, SessionState.SUSPENDED, "sandbox is gone; resumes from the last checkpoint")
            return
        sb = self.fleet.get(s.sandbox_id)
        if sb.expires_at and sb.expires_at - t < 30 * 60:  # busy or not: never let the reaper take it mid-work
            await self.fleet.extend(sb.id, 60, actor=f"session:{s.id}")
        if s.id in self._busy:
            return
        attached = await self._sh(sb, "tmux list-clients 2>/dev/null | wc -l", timeout=20)
        if attached.stdout.strip() not in ("", "0"):
            s.last_active_at = now()
            self._save(s)
        if s.id in self._busy:  # a send landed while we were checking
            return
        if s.idle_minutes and now() - s.last_active_at > s.idle_minutes * 60:
            self._busy.add(s.id)
            await self._suspend_task(s.id, f"idle for {s.idle_minutes} min")
            return
        last = max(s.checkpoints[-1].ts if s.checkpoints else s.created_at, self._last_checked.get(s.id, 0))
        if now() - last > self.settings.session_checkpoint_minutes * 60:
            async with self._locks[s.id]:
                await self._checkpoint(s, "interval")

