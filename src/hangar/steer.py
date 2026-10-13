"""Live, steerable agent turns (workstream steer; docs/steering.md).

A session turn of a harness with a structured mode (Claude Code, Codex) runs under hangar's turn driver
(`driver.py`), still in tmux and still writing the run log the event parser reads. While it runs, hangar can:

* **steer** it: a message folded into the running turn. A session `send` to a busy session does this; if the turn
  ends before the message lands, the message starts the next turn instead.
* **interrupt** it: the turn stops (outcome `interrupted`), or with a message, carries on with that message. A
  one-shot turn (other harnesses, or no python3 in the sandbox) is stopped by killing its run.
* **answer tool approvals** under the `ask` policy: the driver writes each request into the run log, the session's
  event watcher records it on the turn (`approvals`), and anyone with the session (REST, MCP, CLI) answers it.

Commands reach the driver as files moved into `<run>.ctl/in/`, each signed with a per-run key that hangar keeps
encrypted in its store (the driver deletes its copy at start and keeps the agent out of its memory), and the driver
signs its answers, so the agent can't answer for a person or for the driver by writing files. Where the sandbox
allows it the driver also runs as a user of its own (isolation.py, the uid split); elsewhere it shares a user with
the agent, and code a person allows to run can tamper with later turns (docs/steering.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import secrets
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hangar import isolation  # --- isolation
from hangar.driver import DECISIONS, ENDED, MAX_TEXT, sign
from hangar.events import RunEvent
from hangar.harnesses import Harness, get_harness
from hangar.models import Approval, Session, SessionState, SessionTurn, Steer, new_id, now

if TYPE_CHECKING:
    from hangar.models import Sandbox
    from hangar.sessions import SessionService

log = logging.getLogger("hangar.steer")

DRIVER = Path(__file__).with_name("driver.py")
KV = "steer-key:"
ACK_WAIT = 15.0  # seconds a command waits for the driver's answer
APPROVAL_ID = re.compile(r"^ap-[0-9a-f]{12}$")
MAX_APPROVALS = 200  # kept per turn (pending ones are never dropped)
MAX_MESSAGE = 2000  # an approval's note to the agent


class SteerError(ValueError):
    """A steer, interrupt or approval that cannot be done (shown to the caller as is)."""


class TurnEnded(SteerError):
    pass


class NotLive(SteerError):
    pass


class NotReady(SteerError):
    pass


# ====================================================================== starting a live turn
def policy(svc: SessionService, s: Session) -> str:
    """The session's approval policy, else its environment's, else HANGAR_APPROVALS."""
    if s.approvals:
        return s.approvals
    with contextlib.suppress(KeyError, ValueError):
        if env := svc.fleet.catalog.get(s.environment).approvals:
            return env
    return svc.settings.approvals


def ctl_dir(svc: SessionService, run_id: str, sb: Sandbox | None = None) -> str:
    """`sb`: where a split run (isolation.py) keeps it; without it, the agent's ~/.hangar/runs."""
    return isolation.ctl_dir(svc.settings, run_id, sb is not None and isolation.run_split(sb, run_id))


async def prepare(svc: SessionService, s: Session, turn: SessionTurn, sb: Sandbox, h: Harness
                  ) -> tuple[str, str | None]:
    """The turn's run id, and the command that runs it under the driver (None: one-shot `run`, as before).

    Installs the driver and the run's key in the sandbox. Without python3 there, an `auto` turn falls back to the
    one-shot command; an `ask` or `deny-risky` one fails rather than run with permission checks off."""
    run_id = new_id("run")
    pol = policy(svc, s)
    live = svc.settings.live_turns and h.live and h.live_run
    if not live:
        if pol != "auto":
            raise SteerError(f"approvals={pol} needs a live turn, and {h.name} turns are one-shot"
                             + ("" if svc.settings.live_turns else " (HANGAR_LIVE_TURNS is off)")
                             + "; use approvals=auto, or a harness with a structured mode (claude-code, codex)")
        return run_id, None
    key = secrets.token_hex(32)
    _, prov = svc.fleet._running(sb.id)
    # --- isolation: a driver of its own user where the sandbox allows it; `ask` says so, or refuses, where not
    split = await isolation.ensure(svc.fleet, prov, sb)
    if pol == "ask" and not split:
        why = isolation.why_not(svc.settings, sb)
        if svc.settings.uid_split == "required":
            raise SteerError(f"approvals=ask needs hangar's driver to run as a user of its own, and this sandbox can't "
                             f"({why}); HANGAR_UID_SPLIT=required (docs/steering.md)")
        turn.warnings.append(f"hangar's driver runs as the agent's own user here ({why}): code you allow to run can "
                             "tamper with later turns (docs/steering.md)")
    ctl = isolation.ctl_dir(svc.settings, run_id, split)
    wd = sb.info.get("repo_path") or svc.settings.workspace_dir  # where fleet.run_agent runs it
    if split:
        driver = isolation.DRIVER
        await asyncio.gather(
            isolation.put(prov, sb, isolation.HOME, {"driver.py": (DRIVER.read_bytes(), 0o644)}),
            isolation.put(prov, sb, isolation.RUNS, {f"{run_id}.ctl": (None, 0o700),
                                                     f"{run_id}.ctl/key": (key.encode(), 0o600)}))
    else:
        driver = f"/home/{svc.settings.sandbox_user}/.hangar/driver.py"
        await asyncio.gather(prov.write_file(sb, driver, DRIVER.read_bytes(), mode=0o644),
                             prov.write_file(sb, f"{ctl}/key", key.encode(), mode=0o600))
    svc.store.kv_set(KV + run_id, svc.vault.encrypt(key.encode()).decode(), durable=True)
    turn.live, turn.approval_policy = True, pol  # type: ignore[assignment]
    q = shlex.quote
    one_shot = isolation.agent_command(svc.settings, run_id, wd, h.run) if split else h.run
    fallback = (f"{one_shot}" if pol == "auto" else
                f"echo 'hangar: approvals={pol} needs python3 >= 3.8 in the sandbox' >&2; false")
    split_args = f" {isolation.driver_args(svc.settings, run_id, wd)}" if split else ""
    # -I: no PYTHON* variables, user site-packages or modules beside the driver, all of which the agent can write
    command = (f"if python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then "
               f"python3 -I {q(driver)} --harness {h.live} --ctl {q(ctl)} --approvals {pol} "
               f"--approval-timeout {int(svc.settings.approval_timeout_seconds)}{split_args} -- {h.live_run}; "
               f"else touch {q(ctl)}/one-shot; {fallback}; fi")
    return run_id, command


def forget(svc: SessionService, run_id: str | None) -> None:
    """The run is over (or never started): its key is of no more use."""
    if run_id:
        with contextlib.suppress(Exception):
            svc.store.kv_delete(KV + run_id)


# ====================================================================== the control channel
async def command(svc: SessionService, s: Session, turn: SessionTurn, op: str, **args: Any) -> dict[str, Any]:
    """Send a signed command to the turn's driver and return its answer (`ok`, `error`)."""
    raw = svc.store.kv_get(KV + (turn.run_id or ""))
    if not raw or not s.sandbox_id:
        raise NotLive("this turn takes no commands")
    key = svc.vault.decrypt(raw.encode())
    cid = "c-" + secrets.token_hex(8)
    cmd: dict[str, Any] = {"id": cid, "op": op, "args": args}
    cmd["mac"] = sign(key, cmd)
    sb = svc.fleet.get(s.sandbox_id)
    _, prov = svc.fleet._running(sb.id)
    split = isolation.run_split(sb, turn.run_id)  # --- isolation: the control files are hangar's user's
    ctl = isolation.ctl_dir(svc.settings, turn.run_id or "", split)
    if split:
        await isolation.put(prov, sb, ctl, {f".{cid}": (json.dumps(cmd).encode(), 0o600)})
    else:
        await prov.write_file(sb, f"{ctl}/.{cid}", json.dumps(cmd).encode(), mode=0o600)
    c, ended = shlex.quote(ctl), shlex.quote(f"{isolation.runs_dir(svc.settings, split)}/{turn.run_id}.exit")
    # Wait for the inbox (the driver may be starting, or between closing and reopening it), unless the turn is
    # over. A move into it is atomic: either the driver gets the command, or it fails to land.
    script = (f"c={c}; for i in $(seq 50); do [ -d \"$c/in\" ] && break; "
              f"{{ [ -e \"$c/done\" ] || [ -e \"$c/one-shot\" ] || [ -s {ended} ]; }} && break; sleep 0.1; done; "
              f"[ -e \"$c/one-shot\" ] && {{ rm -f \"$c/.{cid}\"; exit 4; }}; "
              f"mv \"$c/.{cid}\" \"$c/in/{cid}.json\" 2>/dev/null || {{ rm -f \"$c/.{cid}\"; "
              f"[ -e \"$c/done\" ] || [ -s {ended} ] && exit 3; exit 6; }}; "
              f"for i in $(seq {int(ACK_WAIT * 10)}); do grep -F '\"id\": \"{cid}\"' \"$c/acks\" 2>/dev/null "
              "&& exit 0; sleep 0.1; done; exit 5")
    res = (await isolation.sh(prov, sb, script, timeout=int(ACK_WAIT) + 30) if split
           else await svc._sh(sb, script, timeout=int(ACK_WAIT) + 30))
    if res.exit_code == 3:
        raise TurnEnded("the turn has ended")
    if res.exit_code == 4:
        raise NotLive("this turn runs one-shot (the sandbox has no python3 >= 3.8)")
    if res.exit_code == 5:
        raise SteerError(f"the agent's driver did not answer within {int(ACK_WAIT)} s")
    if res.exit_code == 6:
        raise NotReady("the agent is still starting; try again in a moment")
    if res.exit_code != 0:
        raise SteerError(f"could not reach the turn: {(res.stderr or res.stdout).strip()[-300:]}")
    # Only the driver's own answer counts: without the uid split the agent can write to `acks` as well.
    ack = next((a for a in map(_json, res.stdout.splitlines()) if a.get("id") == cid
                and isinstance(a.get("mac"), str) and hmac.compare_digest(a["mac"], sign(key, a))), None)
    if ack is None:
        raise SteerError("the answer to this command was not signed by the turn's driver: something in the sandbox "
                         "is tampering with hangar's control channel")
    if not ack.get("ok") and ack.get("error") == ENDED:
        raise TurnEnded("the turn has ended")
    return ack


def _json(line: str) -> dict[str, Any]:
    try:
        d = json.loads(line)
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def running_turn(s: Session) -> SessionTurn:
    t = s.turns[-1] if s.turns else None
    if t is None or not t.run_id or t.finished_at or s.state != SessionState.RUNNING or not s.sandbox_id:
        raise SteerError(f"session {s.name} has no running turn")
    return t


def _check_text(text: str | None, required: bool) -> str | None:
    if text is None or not text.strip():
        if required:
            raise SteerError("the message is empty")
        return None
    if len(text) > MAX_TEXT:
        raise SteerError(f"the message is too long (at most {MAX_TEXT} characters)")
    return text


# ====================================================================== steer
async def steer(svc: SessionService, s: Session, text: str, requester: str) -> Steer:
    """Fold a message into the running turn and wait until the agent has it."""
    text = _check_text(text, True) or ""
    t = running_turn(s)
    if not t.live:
        raise NotLive(f"this {t.harness or s.harness} turn runs one-shot and can't take a message mid-turn; "
                      "wait for it, or interrupt it")
    st = Steer(text=text, requester=requester)
    t.steers.append(st)
    svc._save(s)
    try:
        ack = await command(svc, s, t, "steer", text=text)
    except SteerError as e:
        st.status, st.error = "failed", str(e)
        svc._save(s)
        raise
    st.status, st.error = ("delivered", None) if ack.get("ok") else ("failed", str(ack.get("error") or "refused"))
    svc._save(s)
    svc.fleet.audit(requester, "session.steer", s.id, run=t.run_id, ok=st.status == "delivered", message=text[:300])
    if st.status == "failed":
        raise SteerError(f"the agent did not take the message: {st.error}")
    return st


def can_steer(svc: SessionService, s: Session) -> bool:
    """A busy session takes a message now (a steer, or the next turn once this one is done) when its turns are
    live, or about to be (a turn starting for a live harness). Job sessions don't: follow-ups to a job are verified
    and delivered by the job."""
    t = s.turns[-1] if s.turns else None
    if t is None or s.job_id or s.state in (SessionState.SUSPENDING, SessionState.ARCHIVED, SessionState.FAILED):
        return False
    with contextlib.suppress(ValueError):
        starting = t.run_id is None and not t.finished_at and bool(svc.settings.live_turns
                                                                   and get_harness(s.harness or "").live)
        return t.live or starting
    return False


def steer_later(svc: SessionService, s: Session, text: str, requester: str) -> Steer:
    """A session `send` while it is busy with a live turn: deliver the message in the background, into the turn
    running (or starting), else as the next turn once this one is done. The session stays busy until then, so
    waiting for the turn waits for that one too."""
    t = s.turns[-1]
    st = Steer(text=text, requester=requester)
    t.steers.append(st)
    svc._save(s)
    svc.fleet.audit(requester, "session.steer", s.id, run=t.run_id, message=text[:300])
    _later(svc, s, st)
    return st


def _later(svc: SessionService, s: Session, st: Steer) -> None:
    svc._holds[s.id] += 1  # released by _deliver, right before the next turn takes the session
    svc._spawn(_deliver(svc, s, st))


async def _deliver(svc: SessionService, s: Session, st: Steer) -> None:
    held = True
    tried: set[str] = set()  # runs that turned out to be over (or one-shot)

    def release() -> None:
        nonlocal held
        if held:
            held = False
            svc._holds[s.id] -= 1
            if svc._holds[s.id] <= 0:
                del svc._holds[s.id]

    async def into(turn: SessionTurn) -> bool:
        """Steer into `turn`; False when it is over (or one-shot) and the message must wait for the next one."""
        try:
            ack = await command(svc, s, turn, "steer", text=st.text)
        except NotReady:
            return False  # try it again
        except (TurnEnded, NotLive):
            tried.add(turn.run_id or "")
            return False
        st.status, st.error = ("delivered", None) if ack.get("ok") else ("failed", str(ack.get("error")))
        return True

    try:
        deadline = now() + svc.settings.session_turn_timeout_minutes * 60 + 600
        while now() < deadline:
            cur = s.turns[-1]
            if (cur.live and cur.run_id and cur.run_id not in tried and not cur.finished_at
                    and s.state == SessionState.RUNNING):
                if await into(cur):  # the running turn took it
                    return
                continue
            if s.id not in svc._busy:  # no turn task holds the session (it finished: summary, checkpoint)
                if s.state in (SessionState.ARCHIVED, SessionState.SUSPENDING):
                    raise SteerError(f"session {s.name} is {s.state}")
                release()  # and at once, with no await in between, the next turn takes the session
                svc.start_turn(s, st.text, st.requester or "hangar")
                st.status = "queued"  # it began the next turn
                return
            await asyncio.sleep(0.2)
        raise SteerError("the session stayed busy; send the message again")
    except Exception as e:  # noqa: BLE001 - the message is not lost silently: the turn record says why
        log.warning("session %s: a steer was not delivered: %s", s.id, e)
        st.status, st.error = "failed", str(e)[:500]
    finally:
        release()
        svc._save(s)


# ====================================================================== interrupt
async def interrupt(svc: SessionService, s: Session, requester: str, text: str | None = None) -> Steer:
    """Stop the running turn (outcome `interrupted`); with `text`, the agent carries on with that message instead
    (in the same turn when it is live, else as the next turn)."""
    text = _check_text(text, False)
    t = running_turn(s)
    st = Steer(text=text or "", requester=requester, kind="interrupt")
    if t.live:
        try:
            ack = await command(svc, s, t, "interrupt", **({"text": text} if text else {}))
            if ack.get("ok"):
                st.status = "delivered"
            else:
                log.warning("session %s: the driver refused an interrupt (%s); stopping the run", s.id,
                            ack.get("error"))
        except NotLive:
            t.live = False  # the sandbox had no python3: stop the one-shot run below
        except TurnEnded:
            raise
        except SteerError as e:  # still starting, no answer, or an answer not signed by the driver
            log.warning("session %s: an interrupt was not confirmed (%s); stopping the run", s.id, e)
    if st.status != "delivered":  # one-shot, or the driver didn't confirm it: an interrupt always stops the run
        await kill(svc, s, t)
        st.status = "queued" if text else "delivered"
    t.steers.append(st)
    svc._save(s)
    svc.fleet.audit(requester, "session.interrupt", s.id, run=t.run_id, message=(text or "")[:300])
    if st.status == "queued" and not s.job_id:  # the run was stopped: its message starts the next turn
        _later(svc, s, st)  # (a job's turn ends `interrupted`, and the job carries on with it: jobs.py)
    return st


async def kill(svc: SessionService, s: Session, t: SessionTurn) -> None:
    """Stop a run outright; exit status 130 marks it interrupted (unless it had just finished)."""
    sb, prov = svc.fleet._running(s.sandbox_id or "")
    await isolation.kill(prov, sb, svc.settings, t.run_id or "", interrupted=True)  # --- isolation: where it runs


# ====================================================================== approvals
def track(turn: SessionTurn, events: list[RunEvent]) -> bool:
    """Record approval requests and answers from the driver's events on the turn. True if anything changed. Only a
    live turn's log is the driver's: in a one-shot run's log such a line could come from anything."""
    if not turn.live:
        return False
    changed = False
    for ev in events:
        aid = ev.data.get("id")
        if ev.kind != "approval_request" or not isinstance(aid, str) or not APPROVAL_ID.match(aid):
            continue
        a = next((x for x in turn.approvals if x.id == aid), None)
        if ev.data.get("status") == "pending":
            if a is None:
                exp = ev.data.get("expires_at")
                detail = ev.data.get("detail")
                turn.approvals.append(Approval(id=aid, tool=str(ev.data.get("tool") or ""),
                                               detail=detail if isinstance(detail, str) else ev.text.split(": ", 1)[-1],
                                               requested_at=ev.ts,
                                               expires_at=float(exp) if isinstance(exp, int | float) else None))
                changed = True
        elif a is not None and a.status == "pending" and ev.data.get("status") in (
                "allowed", "allowed_always", "denied", "cancelled"):
            a.status, a.by = ev.data["status"], str(ev.data.get("by") or "hangar")[:100]
            a.message, a.decided_at = ev.data.get("message"), ev.ts
            changed = True
    if len(turn.approvals) > MAX_APPROVALS:
        decided = [a for a in turn.approvals if a.status != "pending"]
        drop = {id(a) for a in decided[: len(turn.approvals) - MAX_APPROVALS]}
        turn.approvals = [a for a in turn.approvals if id(a) not in drop]
    return changed


def pending(s: Session | None) -> list[Approval]:
    """The running turn's unanswered approvals (not those past their deadline: the driver has denied them)."""
    t = s.turns[-1] if s is not None and s.turns else None
    if t is None or t.finished_at or not t.live:
        return []
    return [a for a in t.approvals if a.status == "pending" and (a.expires_at is None or a.expires_at > now())]


def view(s: Session) -> dict[str, Any]:
    t = s.turns[-1] if s.turns else None
    return {"session": s.name, "turn": len(s.turns) or None, "running": bool(t and t.run_id and not t.finished_at),
            "policy": (t.approval_policy if t else None), "live": bool(t and t.live),
            "pending": [a.model_dump(mode="json") for a in pending(s)],
            "decided": [a.model_dump(mode="json") for a in (t.approvals if t else []) if a.status != "pending"][-20:]}


async def answer(svc: SessionService, s: Session, approval_id: str, decision: str, requester: str,
                 message: str | None = None) -> Approval:
    """Allow or deny a tool call the agent is waiting on. `allow_always` also allows calls like it for the
    rest of this turn (never written to any settings; the next turn asks again)."""
    if decision not in DECISIONS:
        raise SteerError(f"decision must be one of {', '.join(DECISIONS)}")
    if message is not None and len(message) > MAX_MESSAGE:
        raise SteerError(f"the message is too long (at most {MAX_MESSAGE} characters)")
    if not APPROVAL_ID.match(approval_id or ""):
        raise KeyError(f"no approval '{approval_id}'")
    t = running_turn(s)
    a = next((x for x in t.approvals if x.id == approval_id), None)
    if a is None:
        raise KeyError(f"no approval '{approval_id}' in the running turn of session {s.name}")
    if a.status != "pending":
        raise SteerError(f"approval {a.id} was already answered ({a.status} by {a.by})")
    ack = await command(svc, s, t, "approve", request=a.id, decision=decision, message=message or "", by=requester)
    if not ack.get("ok"):
        raise SteerError(f"approval {a.id} is no longer open: {ack.get('error')}")
    a.status = {"allow": "allowed", "allow_always": "allowed_always", "deny": "denied"}[decision]  # type: ignore[assignment]
    a.by, a.message, a.decided_at = requester[:100], message, now()
    svc._save(s)
    svc.fleet.audit(requester, "session.approval", s.id, run=t.run_id, approval=a.id, tool=a.tool, decision=decision)
    return a


def recover(svc: SessionService) -> None:
    """After a restart: messages that were on their way into a turn have no task delivering them any more."""
    for s in svc._sessions.values():
        stale = [st for t in s.turns[-1:] for st in t.steers if st.status in ("sending", "queued")]
        for st in stale:
            st.status, st.error = "failed", "the control plane restarted before it was delivered; send it again"
        if stale:
            svc._save(s)
