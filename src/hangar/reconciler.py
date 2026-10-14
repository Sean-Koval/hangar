"""One reconciler for every sandbox state a task can leave unfinished (hardening H1, B1-B3).

A deploy, a crash or the nightly power-down can stop hangar between any two steps of a create or a destroy, and the
rows those tasks leave behind were never looked at again: a sandbox left `starting` kept its exclusive credential (a
Codex ChatGPT login) leased until its TTL ran out, one left `stopping` kept its pod and lease for good, and a create
stopped before it stored the provider ref orphaned the cluster object. On GKE a pod replaced by node repair or a
scale-down came back blank under the same claim and passed for the running sandbox, so the next checkpoint could
drop the session's conversation.

Each pass (at startup, before sessions and jobs look at their sandboxes, then every HANGAR_RECONCILE_INTERVAL_SECONDS
on a task of its own, so an image build or a warm-pool sync in the reaper can't hold it up):
- a pending, starting or stopping row that no live task owns is finished: a create is torn down and marked `error`,
  a destroy is completed. A missing provider ref is looked up by the sandbox's label first.
- a running or paused sandbox whose backing container or pod died, vanished or (GKE) was replaced is marked stopped,
  its leases released, and a replaced pod's claim destroyed; its session resumes from its last checkpoint.
Whatever the pass acts on gets a flight record in `info`: `failed_stage`, `cause` and `elapsed_s`, with the create's
`stages` kept, rather than a bare error.

Only rows no task owns are touched: `create` and `destroy` register themselves here (`own`), and a destroy's
provider steps run on a task of their own, so a caller cancelled half-way (a client timeout, a shutdown) can't
strand the sandbox in `stopping` while hangar is up.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Iterable
from typing import TYPE_CHECKING, Any

from hangar import harden, metrics
from hangar.models import Credential, Sandbox, SandboxState, now
from hangar.providers.base import Provider

if TYPE_CHECKING:
    from hangar.fleet import Fleet
    from hangar.store import Store

log = logging.getLogger("hangar.reconciler")

UNFINISHED = frozenset({SandboxState.PENDING, SandboxState.STARTING, SandboxState.STOPPING})
SETTLED = frozenset({SandboxState.RUNNING, SandboxState.PAUSED})
WATCHED = UNFINISHED | SETTLED
# Causes in a flight record
ORPHANED = "orphaned_by_restart"  # the task doing a create or destroy is gone (restart, crash, power-down)
POD_REPLACED = "pod_replaced"  # GKE recreated the pod: node repair, scale-down, preemption
GONE = "gone"  # the container, pod or VM died or vanished
STAGES_KEPT = 30  # create stages kept per sandbox
ORPHAN_AGE = 120.0  # seconds an unowned unfinished row must be old before a pass after startup acts on it
CALL_TIMEOUT = 60.0  # per provider call in a pass: a hung API must not stall the loop or startup
STOP_GRACE = 10.0  # at shutdown, how long destroys in flight get to finish


def sandboxes_in(store: Store, states: Iterable[SandboxState]) -> list[Sandbox]:
    """Sandboxes in any of `states`. The one place the reconciler reads rows, so it can move to the store's indexed
    state query when that lands; until then one scan per pass (the scan the reaper's reconcile already made)."""
    want = frozenset(states)
    return [s for s in store.list(Sandbox) if s.state in want]


def note_stage(sb: Sandbox, msg: str) -> None:
    """Keep a create's stages (seconds since it began, what it was doing), so a failure says where it stopped."""
    stages = sb.info.setdefault("stages", [])
    stages.append([round(max(0.0, now() - sb.created_at), 1), msg[:200]])
    del stages[:-STAGES_KEPT]


def _stage_of(sb: Sandbox) -> str:
    if stage := sb.info.get("stage"):
        return str(stage)
    if sb.state == SandboxState.STOPPING:
        return "destroying the sandbox"
    if sb.state == SandboxState.PENDING:
        return "reserving credentials"
    if sb.state == SandboxState.STARTING:
        return str(sb.info["stages"][-1][1]) if sb.info.get("stages") else "starting the sandbox"
    return f"{sb.state.value}"


def record(sb: Sandbox, cause: str) -> str:
    """Write the flight record on `sb` and return the stage it was in."""
    stage = _stage_of(sb)
    sb.info["failed_stage"] = stage
    sb.info["cause"] = cause
    sb.info["elapsed_s"] = round(max(0.0, now() - sb.created_at), 1)
    return stage


def release_leases(store: Store, sandbox_id: str) -> list[str]:
    """Free the exclusive credentials leased to a sandbox without reading anything back (the vault copy stays). For
    sandboxes that never got as far as running an agent, or whose auth file went with them."""
    freed = []
    for c in store.list(Credential):
        if c.leased_to == sandbox_id:
            c.leased_to = None
            store.put(c)
            freed.append(c.name)
    return freed


class Reconciler:
    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self._owners: dict[str, tuple[str, asyncio.Task]] = {}  # sandbox id -> (create|destroy, the task doing it)
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ ownership
    def own(self, sandbox_id: str, kind: str) -> None:
        """The current task is creating or destroying this sandbox: no pass touches it meanwhile."""
        task = asyncio.current_task()
        if task is not None:
            self._owners[sandbox_id] = (kind, task)

    def disown(self, sandbox_id: str) -> None:
        cur = self._owners.get(sandbox_id)
        if cur is not None and cur[1] is asyncio.current_task():
            del self._owners[sandbox_id]

    def owned(self, sandbox_id: str) -> bool:
        cur = self._owners.get(sandbox_id)
        if cur is not None and cur[1].done():
            del self._owners[sandbox_id]
            return False
        return cur is not None

    def destroying(self, sandbox_id: str) -> asyncio.Task | None:
        cur = self._owners.get(sandbox_id)
        return cur[1] if cur is not None and cur[0] == "destroy" and not cur[1].done() else None

    def in_flight(self) -> dict[str, list[str]]:
        """Sandbox ids being created or destroyed right now (the power-down drain waits for them)."""
        out: dict[str, list[str]] = {"create": [], "destroy": []}
        for sid in list(self._owners):
            if self.owned(sid):
                out[self._owners[sid][0]].append(sid)
        return out

    def detach(self, sandbox_id: str, coro: Awaitable[Any]) -> asyncio.Task:
        """Run a destroy's provider steps on a task of their own that owns the sandbox until they finish: the
        caller may be cancelled (a client gone, a shutdown) without leaving the sandbox half destroyed."""
        async def run() -> Any:
            self.own(sandbox_id, "destroy")
            try:
                return await coro
            finally:
                self.disown(sandbox_id)

        task = asyncio.ensure_future(run())
        self._owners[sandbox_id] = ("destroy", task)  # before it first runs, so nothing else starts one meanwhile
        return task

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None
        # Destroys in flight get a moment: one left now is finished by the next start's pass, not lost.
        pending = [t for kind, t in list(self._owners.values()) if kind == "destroy" and not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=STOP_GRACE)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(max(1, self.fleet.settings.reconcile_interval_seconds))
            try:
                await self.run()
                metrics.loop_tick("reconciler")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next pass may work; the failure counter shows it
                log.exception("reconcile pass failed")
                metrics.loop_failed("reconciler")

    # ------------------------------------------------------------------ one pass
    async def run(self, boot: bool = False) -> list[asyncio.Task]:
        """One pass. `boot`: at startup nothing can own a row yet, so unfinished rows of any age are acted on.
        Returns the teardowns it started (they run on in the background)."""
        f = self.fleet
        t = now()
        started: list[asyncio.Task] = []
        rows = sandboxes_in(f.store, WATCHED)
        for pname, prov in list(f.providers.items()):
            mine = [sb for sb in rows if sb.provider == pname and not self.owned(sb.id)]
            for sb in mine:
                if sb.state not in UNFINISHED or not (boot or t - sb.created_at > ORPHAN_AGE):
                    continue
                cur = f.store.get(Sandbox, sb.id)  # re-read: an earlier provider's calls gave other tasks a turn
                if cur is not None and cur.state in UNFINISHED and not self.owned(cur.id):
                    started.append(self._finish(cur, prov))
            settled = [sb for sb in mine if sb.state in SETTLED]
            if settled:
                await self._check(prov, pname, settled)
        return started

    async def _check(self, prov: Provider, pname: str, settled: list[Sandbox]) -> None:
        """Running and paused sandboxes whose backing container, pod or VM died, vanished or was replaced."""
        try:
            refs = await asyncio.wait_for(prov.list_refs(), CALL_TIMEOUT)
        except Exception as e:  # noqa: BLE001 - can't compare: judge nothing gone
            log.warning("reconcile: can't list %s sandboxes: %s", pname, e)
            refs = None
        gone: dict[str, tuple[str, str]] = {}
        if refs is not None:  # None: the provider can't enumerate; nothing to compare against
            for sb in settled:
                if not sb.provider_ref:
                    continue
                reported = refs.get(sb.provider_ref)
                if reported is None:
                    gone[sb.id] = (GONE, "disappeared from provider")
                elif reported in (SandboxState.STOPPED, SandboxState.ERROR):
                    gone[sb.id] = (GONE, f"provider reports {reported.value}")
        running = [sb for sb in settled if sb.state == SandboxState.RUNNING and sb.id not in gone]
        if running:
            before = {sb.id: sb.info.get("pod_uid") for sb in running}
            try:
                replaced = await asyncio.wait_for(prov.replaced(running), CALL_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                log.warning("reconcile: can't check %s pods: %s", pname, e)
                replaced = {}
            for sb in running:  # a sandbox from before pod UIDs were recorded: its current pod is the one
                if (uid := sb.info.get("pod_uid")) and uid != before[sb.id] and sb.id not in replaced:
                    cur = self.fleet.store.get(Sandbox, sb.id)
                    if cur is not None and cur.state == SandboxState.RUNNING and not cur.info.get("pod_uid"):
                        cur.info["pod_uid"] = uid
                        self.fleet.store.put(cur)
            for sid, why in replaced.items():
                gone[sid] = (POD_REPLACED, why)
        for sid, (cause, why) in gone.items():
            self._gone(prov, sid, cause, why)

    def _gone(self, prov: Provider, sid: str, cause: str, why: str) -> None:
        f = self.fleet
        sb = f.store.get(Sandbox, sid)  # re-read: other tasks may have written it while the provider answered
        if sb is None or sb.state not in SETTLED or self.owned(sid):
            return
        record(sb, cause)
        log.info("reconcile: %s %s (%s)", sid, why, cause)
        f._mark_gone(sb, why)  # stopped, grant revoked, leases released (the auth file went with it)
        if (envd := getattr(prov, "envd", None)) is not None:
            envd.forget(sb)  # its daemon's address went with it
        if cause == POD_REPLACED:
            # The claim is still there with a blank pod in it: nothing will use it, and it costs a node.
            async def drop() -> None:
                try:
                    await asyncio.wait_for(prov.destroy(sb), CALL_TIMEOUT)
                except Exception as e:  # noqa: BLE001 - gc and the claim's own shutdownTime are the backstop
                    log.warning("reconcile: could not remove the replaced pod's claim for %s: %s", sid, e)

            self.detach(sid, drop())

    def _finish(self, sb: Sandbox, prov: Provider) -> asyncio.Task:
        """Finish what an orphaned create or destroy left: a create is torn down and ends in `error`, a destroy is
        completed and ends in `stopped`."""
        f = self.fleet
        first = sb.info.get("cause") != ORPHANED  # a later pass retrying a teardown that failed says nothing new
        stage = record(sb, ORPHANED)
        creating = sb.state != SandboxState.STOPPING
        if creating:
            # Free the lease now, whatever the provider does next: a held Codex login blocks every other Codex
            # create. The sandbox never ran an agent, so there is nothing to write back.
            if freed := release_leases(f.store, sb.id):
                log.info("reconcile: released %s from orphaned %s", ", ".join(freed), sb.id)
            sb.error = f"orphaned by a control-plane restart while: {stage} (after {sb.info['elapsed_s']:.0f}s)"
        f.store.put(sb)
        if first:
            f.audit("reconciler", "sandbox.reconcile", sb.id, cause=ORPHANED, state=sb.state.value, failed_stage=stage)

        async def finish() -> None:
            try:
                if not sb.provider_ref:
                    ref = await asyncio.wait_for(prov.find_ref(sb), CALL_TIMEOUT)
                    if ref:
                        cur = f.store.get(Sandbox, sb.id) or sb
                        sb.provider_ref = cur.provider_ref = ref
                        f.store.put(cur)
                if creating:
                    f.gateway.revoke(sb.id)
                    harden.forget(f, sb.id)
                    if sb.provider_ref:
                        await asyncio.wait_for(prov.destroy(sb), CALL_TIMEOUT)
                    cur = f.store.get(Sandbox, sb.id) or sb
                    cur.state = SandboxState.ERROR
                    cur.provider_ref = sb.provider_ref
                    f.store.put(cur)
                else:
                    await asyncio.wait_for(f._finish_destroy(sb, prov, "reconciler"), 2 * CALL_TIMEOUT)
                log.info("reconcile: finished orphaned %s (%s)", sb.id, stage)
            except Exception as e:  # noqa: BLE001 - left as it is; the next pass tries again
                log.warning("reconcile: could not finish orphaned %s: %s", sb.id, e)

        return self.detach(sb.id, finish())
