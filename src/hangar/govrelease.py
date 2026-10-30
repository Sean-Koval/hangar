"""Job release and landing ask Tower's decision point (KAN-33, hangar#172; spec 0015 §2, §4.4, §5-§7, §11).

hangar's two control-plane approvals: releasing a job that waits for a trusted person (`approvals.decide`, from REST,
MCP `approve_job`, GitHub and Jira `/hangar approve`), and arming a landing after an approving review
(`Landing._evaluate`, after `qualifies`, the self-approval rule and every other landing check). What Tower's answer
does depends on the kind's mode (`HANGAR_GOVERNANCE`, `HANGAR_GOVERNANCE_MODES`):

* `off`: Tower isn't asked; hangar decides as before.
* `shadow`: hangar decides. Tower is asked the same question as a dry run (with the person who acted as `signer`),
  in the background for a release, and both answers are kept on the decision (`JobApproval.governance`,
  `LandingState.governance`), audited `governance.shadow`, and a difference also `governance.disagree` (and counted
  in `hangar_governance_disagreements_total{kind}`). Shadow never changes an outcome.
* `enforce`: Tower decides, failing closed (§6). hangar's own rule still runs first (`approvals.check`: never your
  own work), and Tower never makes hangar more permissive: an allow lets hangar go on with its own checks.

Job release in `enforce`:

* A job entering `awaiting_approval` opens Tower's approval (`decide` with no signer, from `tick`); its id is kept
  on `JobApproval.workspace_approval_id`. A signature in Tower (Slack, Teams, the web) comes back as an
  `approval.decided` event (govevents.Receiver.hint) or the 60 s poll: `allow` queues the job with Tower's receipt,
  `deny` rejects it with Tower's reason (posted where the job was asked for), `expired` / `withdrawn` drops it as
  hangar's own expiry does.
* A person approving in hangar is sent as a `signer` (§4.4). `allow` releases the job; `wait` (more signatures, or
  a signer Tower can't map) keeps it waiting and raises ApprovalPending; a refused signature leaves it waiting and
  raises ApprovalRefused; a deny of the request itself rejects the job. A rejection in hangar always goes through
  and withdraws Tower's approval.
* Tower unreachable, refusing hangar's credential or answering nonsense: the job stays `awaiting_approval` and the
  issue or ticket is told "approval service unavailable" once per outage. hangar withdraws an approval nobody needs
  any more (the job expired, was cancelled or rejected in hangar).

Landing in `enforce`: once hangar would arm, it asks `landing.approve` for the approved head with the reviewer as
signer, and arms only on `allow` (the receipt goes on `LandingState.receipt` and the review packet). `wait` notes
who must sign and follows the approval; a new head withdraws it.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from hangar.governance import (
    CHECKS_MAX,
    DETAIL_MAX,
    FILES_MAX,
    Action,
    AiReview,
    Check,
    Context,
    Correlation,
    Decision,
    DecisionRequest,
    Party,
    Signer,
    Subject,
    fail_closed,
    idempotency_key,
    parse_modes,
)
from hangar.governed import record
from hangar.govevents import Unavailable
from hangar.jobs import JobError, JobNotFound
from hangar.models import JobState, now

if TYPE_CHECKING:
    from hangar import approvals
    from hangar.jobs import JobService
    from hangar.models import Job

log = logging.getLogger("hangar.govrelease")

REL = "gov-rel:"  # kv: Tower approval id -> the job whose release waits on it
LAND = "gov-land:"  # kv: Tower approval id -> the job whose landing waits on it
POLL = 60.0  # seconds between status reads of a waiting approval, and between asks Tower couldn't answer
UNAVAILABLE = "approval service unavailable"
_AI_VERDICT = {"approve": "ok", "comment": "comments", "blocking": "blocking"}


class ApprovalPending(JobError):
    """A person's approval reached Tower and the job still waits (more signatures, or Tower couldn't answer)."""


def mode_of(settings: Any, kind: str) -> str:
    """The kind's mode. A setting hangar can't read counts as `enforce`: Tower decides, failing closed."""
    try:
        return parse_modes(settings.governance, settings.governance_modes or None).get(kind, "off")
    except ValueError as e:
        log.error("governance: %s; treating %s as enforce", e, kind)
        return "enforce"


def _external(requester: str | None) -> str | None:
    """A front door's id for whose work it is, when no person is (`github:...`, `jira:...`, `webhook:...`)."""
    return requester[:200] if requester and requester != "system" and ":" in requester else None


class Releases:
    """One per control plane: `jobs.release_gate` and `receiver.releases` point here. `receiver` gives Tower
    (`receiver.tower()`, govevents.Receiver)."""

    def __init__(self, jobs: JobService, receiver: Any, *, clock: Any = time.time):
        self.jobs, self.receiver = jobs, receiver
        self.fleet, self.settings, self.store = jobs.fleet, jobs.settings, jobs.store
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self._polled: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        jobs.release_gate = self  # type: ignore[attr-defined]
        receiver.releases = self

    def mode(self, kind: str) -> str:
        return mode_of(self.settings, kind)

    # ================================================================== asking
    async def tower_decide(self, req: DecisionRequest) -> Decision:
        try:
            tower = self.receiver.tower()
        except Unavailable as e:
            return fail_closed(req.kind, "unreachable", str(e), retryable=False)
        try:
            return await tower.decide(req)
        except Exception as e:  # noqa: BLE001 - a client bug is no allow either
            log.exception("governance: deciding %s failed", req.idempotency_key)
            return fail_closed(req.kind, "unreachable", f"{type(e).__name__}: {e}"[:300])

    async def tower_status(self, aid: str, kind: str) -> Decision:
        try:
            return await self.receiver.tower().status(aid, kind=kind)
        except Unavailable as e:
            return fail_closed(kind, "unreachable", str(e), approval_id=aid, retryable=False)

    def _shadowed(self, req: DecisionRequest, local: Decision, tower: Decision) -> dict[str, Any]:
        """Record a shadow comparison: the audit entries, the metric, and the record kept on the decision."""
        rec = record("shadow", local, local=local, tower=tower)
        detail = {"kind": req.kind, "key": req.idempotency_key, "hangar": local.outcome, "tower": tower.outcome,
                  "tower_rule": tower.rule, "tower_reason": tower.reason[:300], "failure": tower.failure}
        self.fleet.audit("hangar", "governance.shadow", req.subject.job, **detail)
        if tower.source == "workspace" and tower.outcome != local.outcome:
            self.fleet.audit("hangar", "governance.disagree", req.subject.job, **detail)
            with contextlib.suppress(Exception):
                from hangar import metrics

                metrics.M.governance_disagreements.labels(req.kind).inc()
        return rec

    @staticmethod
    def signer(approver: approvals.Approver, verdict: str = "approve") -> Signer:
        """The person who acted at a hangar surface, as Tower should know them (§4.4)."""
        return Signer(person=approver.person, external=None if approver.person else approver.actor[:200],
                      via=approver.via, verdict=verdict)  # type: ignore[arg-type]

    # ================================================================== job release
    def release_request(self, job: Job, signer: Signer | None = None) -> DecisionRequest:
        from hangar import taint
        from hangar.repopolicy import repo_key

        a = job.approval
        detail = str(self.fleet.broker.redact(job.instructions or ""))
        return DecisionRequest(
            kind="job.release", idempotency_key=idempotency_key("job.release", job=job.id),
            requester=Party(person=job.person, external=None if job.person else _external(job.requester)),
            signer=signer,
            subject=Subject(job=job.id, session=job.session_id, repo=repo_key(job.repo) if job.repo else None,
                            environment=job.environment),
            action=Action(detail=detail[:DETAIL_MAX]),
            context=Context(untrusted_source=job.untrusted_source, tainted=taint.tainted(job)),
            correlation=Correlation(job=job.id, session=job.session_id),
            deadline=a.expires_at if a else None)

    async def decide(self, job_id: str, approver: approvals.Approver, decision: str,
                     reason: str | None = None) -> Job:
        """approvals.decide, with `job.release` in `shadow` or `enforce`."""
        from hangar import approvals

        mode = self.mode("job.release")
        if mode != "enforce":
            job = approvals.decide_now(self.jobs, job_id, approver, decision, reason)  # type: ignore[arg-type]
            if mode == "shadow":
                a = job.approval
                local = Decision(outcome="allow" if decision == "approve" else "deny", source="local",
                                 status="local", rule=a.rule if a else None,
                                 reason=f"{decision}d by {approver.actor} (hangar's rule {a.rule if a else '?'})")
                req = self.release_request(job, signer=self.signer(approver, decision))
                self._spawn(self._shadow_release(job, req.model_copy(update={"dry_run": True}), local))
            return job
        async with self._locks[job_id]:
            job, d = approvals.check(self.jobs, job_id, approver, decision)  # type: ignore[arg-type]
            words = (reason or "").strip() or None
            if decision == "reject":  # a rejection always goes through, and withdraws Tower's approval
                aid = job.approval.workspace_approval_id if job.approval else None
                approvals._record(self.jobs, job, approver, "reject", d, words)
                self._keep(job, record("enforce", Decision(outcome="deny", source="local", status="local",
                                                           reason=f"rejected in hangar by {approver.actor}")))
                if aid:
                    self._unfollow(REL, aid)
                    self._spawn(self._withdraw(aid, f"rejected in hangar by {approver.actor}"))
                return job
            t = await self.tower_decide(self.release_request(job, signer=self.signer(approver)))
            return await self._signed(job, approver, d, words, t)

    async def _shadow_release(self, job: Job, req: DecisionRequest, local: Decision) -> None:
        tower = await self.tower_decide(req)
        rec = self._shadowed(req, local, tower)
        if job.approval is not None:
            job.approval = job.approval.model_copy(update={"governance": rec})
            self.jobs._save(job)

    async def _signed(self, job: Job, approver: approvals.Approver, d: approvals.Decision, words: str | None,
                      t: Decision) -> Job:
        """Act on Tower's answer to a person's approval in hangar (§4.4). Under the job's lock."""
        from hangar import approvals

        self._keep(job, record("enforce", t, tower=t))
        aid = t.approval_id
        if t.outcome == "allow":
            self._release(job, approver, d, words, t)
            return job
        if t.source == "fail_closed":
            self._unavailable(job, t)
            raise ApprovalPending(f"{t.reason}; hangar job {job.id} stays waiting for approval")
        if t.outcome == "wait":
            self._follow(job, aid)
            self._tell(job, f"Tower took {approver.actor}'s approval of hangar job `{job.id}` ({aid}); it still "
                            f"waits: {t.reason}")
            raise ApprovalPending(f"Tower took your approval ({aid}); hangar job {job.id} still waits: {t.reason}")
        if t.status in ("expired", "withdrawn"):  # nobody decided: the next ask opens a new approval
            self._set(job, workspace_approval_id=None)
            raise ApprovalPending(f"Tower's approval {aid} was {t.status}; hangar job {job.id} still waits, ask again")
        if aid:  # a deny naming an approval that still waits: Tower refused this signature, not the job
            now_ = await self.tower_status(aid, "job.release")
            if now_.outcome == "wait":
                self._follow(job, aid)
                why = f"Tower refused {approver.actor}'s signature: {t.reason}"
                self.jobs.admission.refused(f"owner:{approver.actor}", approver.actor, "job.approve.refused", job.id,
                                            why=why[:500], decided_person=approver.person, self_approved=False)
                raise approvals.ApprovalRefused(why)
        self._reject(job, t)
        return job

    def _release(self, job: Job, approver: approvals.Approver, d: approvals.Decision, words: str | None,
                 t: Decision) -> None:
        from hangar import approvals

        if job.approval and job.approval.workspace_approval_id:
            self._unfollow(REL, job.approval.workspace_approval_id)
        self._set(job, workspace_approval_id=t.approval_id, receipt=t.receipt)
        approvals._record(self.jobs, job, approver, "approve", d, words, tower=self._cite(t))
        self.fleet.audit("tower" if approver.via == "system" else approver.actor, "governance.release.allow", job.id,
                         tower_approval=t.approval_id, receipt=t.receipt, rule=t.rule)

    def _reject(self, job: Job, t: Decision) -> None:
        """Tower said no (its rules, or a person in Tower): the job is rejected with Tower's reason."""
        from hangar import approvals

        if job.approval and job.approval.workspace_approval_id:
            self._unfollow(REL, job.approval.workspace_approval_id)
        self._set(job, workspace_approval_id=t.approval_id or (job.approval.workspace_approval_id
                                                               if job.approval else None))
        why = str(self.fleet.broker.redact(t.reason or "denied in Tower"))[:500]
        approvals._record(self.jobs, job, approvals.Approver("tower", None, "system", frozenset({"tower"})),
                          "reject", approvals.Decision(True, "", "tower"), why,  # type: ignore[arg-type]
                          tower=self._cite(t))
        self.fleet.audit("tower", "governance.release.deny", job.id, tower_approval=t.approval_id, rule=t.rule,
                         status=t.status, reason=why[:300])

    async def _settle(self, job: Job, t: Decision, by: str | None = None) -> None:
        """Tower's answer with no hangar signer: the approval it opened, settled in Tower (or by its rules)."""
        from hangar import approvals
        from hangar.triggers import _drop

        self._keep(job, record("enforce", t, tower=t))
        if t.outcome == "allow":
            who = f"tower:{by}"[:200] if by else "tower"
            self._release(job, approvals.Approver(who, None, "system", frozenset({who})),
                          approvals.Decision(True, "", "tower"), None, t)  # type: ignore[arg-type]
        elif t.status in ("expired", "withdrawn"):
            if job.approval and job.approval.workspace_approval_id:
                self._unfollow(REL, job.approval.workspace_approval_id)
            job.approval = job.approval.model_copy(update={"decided_at": now(), "decision": "expired"}) \
                if job.approval else None
            self.fleet.audit("tower", "job.approval_expired", job.id, tower_approval=t.approval_id, status=t.status)
            _drop(self.jobs, job, f"approval {t.approval_id} {t.status} in Tower", "approval_expired",
                  f"Tower's approval {t.approval_id} for hangar job `{job.id}` was {t.status} before anyone "
                  "signed it, so the job was dropped. A trusted person can comment `/hangar` to start it again.")
        elif t.outcome == "deny":
            self._reject(job, t)

    async def open(self, job: Job) -> Decision:
        """A job waiting for a trusted person, in `enforce`: open Tower's approval (no signer). Under the job's lock."""
        t = await self.tower_decide(self.release_request(job))
        self._keep(job, record("enforce", t, tower=t))
        if t.source == "fail_closed":
            self._unavailable(job, t)
        elif t.outcome == "wait" and t.approval_id:
            self._follow(job, t.approval_id)
            self.jobs._event(job, f"waiting for approval {t.approval_id} in Tower: {t.reason}"[:500])
        else:
            await self._settle(job, t)
        return t

    # ================================================================== landing
    def landing_request(self, job: Job, sha: str) -> DecisionRequest:
        from hangar import taint
        from hangar.repopolicy import repo_key

        st, r, lg = job.landing, job.result, self.jobs.landing
        login = st.approved_by or ""
        signer = None
        if login:
            uid = lg._login_ids.get(login.lower())
            signer = Signer(person=lg._person_of_login(login), external=f"github:{uid}" if uid else f"github:{login}",
                            via="github")
        ai, review = st.ai_review or {}, None
        if ai.get("url") and ai.get("head_sha"):
            review = AiReview(url=str(ai["url"]), sha=str(ai["head_sha"]), harness=ai.get("harness"),
                              model=ai.get("model"), verdict=_AI_VERDICT.get(str(ai.get("verdict"))))  # type: ignore[arg-type]
        checks = [Check(name=n[:200], conclusion=str(c), sha=sha) for n, c in list(st.checks.items())[:CHECKS_MAX]
                  ] if st.checks_sha == sha else []
        first = (job.instructions or "").strip().splitlines()[:1]
        detail = f"PR #{r.pr_number}" + (f": {first[0]}" if first else "")
        return DecisionRequest(
            kind="landing.approve", idempotency_key=idempotency_key("landing.approve", job=job.id, head_sha=sha),
            requester=Party(person=job.person, external=None if job.person else _external(job.requester)),
            signer=signer,
            subject=Subject(job=job.id, session=job.session_id, repo=repo_key(job.repo) if job.repo else None,
                            environment=job.environment, pr=r.pr_number, base_sha=r.base_sha, head_sha=sha,
                            files=list(st.files)[:FILES_MAX], author=Party(person=job.person) if job.person else None),
            action=Action(detail=str(self.fleet.broker.redact(detail))[:DETAIL_MAX]),
            context=Context(untrusted_source=job.untrusted_source, tainted=taint.tainted(job),
                            risk=st.risk if st.risk in ("low", "normal", "high") else None,  # type: ignore[arg-type]
                            ai_review=review, checks=checks),
            correlation=Correlation(job=job.id, session=job.session_id))

    async def landing(self, job: Job) -> str | None:
        """Landing._evaluate, about to arm the approved head: None to go on, or why not (the landing's note).
        Under the landing's job lock."""
        mode = self.mode("landing.approve")
        st = job.landing
        sha = st.approved_sha
        if mode == "off" or not sha:
            return None
        if mode == "shadow":
            if (st.governance or {}).get("sha") != sha:  # once per approved head
                req = self.landing_request(job, sha)
                local = Decision(outcome="allow", source="local", status="local", rule="landing",
                                 reason=f"approved by @{st.approved_by} on {sha[:7]} (hangar's landing rule)")
                tower = await self.tower_decide(req.model_copy(update={"dry_run": True}))
                st.governance = {**self._shadowed(req, local, tower), "sha": sha}
                self.jobs._save(job)
            return None
        if st.receipt and st.receipt_sha == sha:
            return None
        if st.workspace_approval and (st.governance or {}).get("sha") != sha:  # an approval of an older head
            old = st.workspace_approval
            self._unfollow(LAND, old)
            st.workspace_approval = None
            self._spawn(self._withdraw(old, "a new head replaced the one it was asked about"))
        t = await self.tower_decide(self.landing_request(job, sha))
        return self._landing_answer(job, sha, t)

    def _landing_answer(self, job: Job, sha: str, t: Decision) -> str | None:
        st = job.landing
        told = (st.governance or {}).get("told") if (st.governance or {}).get("sha") == sha else None
        st.governance = {**record("enforce", t, tower=t), "sha": sha}
        if t.outcome == "allow":
            if st.workspace_approval:
                self._unfollow(LAND, st.workspace_approval)
            st.receipt, st.receipt_sha = t.receipt, sha
            st.workspace_approval = t.approval_id or st.workspace_approval
            self.jobs._save(job)
            self.fleet.audit("tower", "governance.landing.allow", job.id, pr=job.result.pr_number, sha=sha,
                             tower_approval=t.approval_id, receipt=t.receipt, rule=t.rule)
            if st.packet_comment_id:  # the packet shows the receipt (spec 0015 §11)
                self.jobs.landing._bg(job, self.jobs.landing.post_packet(job))
            return None
        if t.source == "fail_closed":
            st.governance["told"] = told or now()
            self.jobs._save(job)
            return f"{UNAVAILABLE} ({t.failure}); auto-merge isn't armed until Tower answers"
        if t.outcome == "wait" and t.approval_id:
            st.workspace_approval = t.approval_id
            self.store.kv_set(LAND + t.approval_id, job.id, durable=True)
            self.jobs._save(job)
            return f"waiting for approval {t.approval_id} in Tower: {t.reason}"
        if st.workspace_approval:
            self._unfollow(LAND, st.workspace_approval)
        if t.status in ("expired", "withdrawn"):
            st.workspace_approval = None
            self.jobs._save(job)
            return f"Tower's approval {t.approval_id} was {t.status}; it needs a new approval"
        self.jobs._save(job)
        self.fleet.audit("tower", "governance.landing.deny", job.id, pr=job.result.pr_number, sha=sha,
                         tower_approval=t.approval_id, rule=t.rule, reason=t.reason[:300])
        return f"Tower refused it ({t.approval_id or t.rule or 'policy'}): {self.fleet.broker.redact(t.reason)}"

    # ================================================================== events and the poll
    async def hint(self, aid: str, by: Any = None) -> dict[str, Any] | None:
        """Tower says approval `aid` changed (an event, or the poll). None: no release or landing waits on it."""
        if (jid := self.store.kv_get(REL + aid)) is not None:
            return await self._hint_release(aid, jid, by if isinstance(by, str) else None)
        if (jid := self.store.kv_get(LAND + aid)) is not None:
            return await self._hint_landing(aid, jid)
        return None

    def _job(self, jid: str) -> Job | None:
        try:
            return self.jobs.get(jid)
        except (JobNotFound, KeyError):
            return None

    async def _hint_release(self, aid: str, jid: str, by: str | None) -> dict[str, Any]:
        job = self._job(jid)
        if job is None or job.state != JobState.AWAITING_APPROVAL or job.approval is None \
                or job.approval.workspace_approval_id != aid:
            self._unfollow(REL, aid)
            return {"ignored": f"{aid} is settled"}
        t = await self.tower_status(aid, "job.release")
        if t.source == "fail_closed":
            raise Unavailable(t.reason)
        if t.outcome == "wait":
            return {"ok": True, "approval": aid, "status": t.status or "waiting"}
        async with self._locks[jid]:
            if job.state == JobState.AWAITING_APPROVAL:
                await self._settle(job, t, by)
        return {"ok": True, "approval": aid, "job": jid, "state": job.state.value}

    async def _hint_landing(self, aid: str, jid: str) -> dict[str, Any]:
        job = self._job(jid)
        st = job.landing if job is not None else None
        if job is None or st is None or st.workspace_approval != aid:
            self._unfollow(LAND, aid)
            return {"ignored": f"{aid} is settled"}
        t = await self.tower_status(aid, "landing.approve")
        if t.source == "fail_closed":
            raise Unavailable(t.reason)
        if t.outcome == "wait":
            return {"ok": True, "approval": aid, "status": t.status or "waiting"}
        sha = (st.governance or {}).get("sha") or st.approved_sha or ""
        note = self._landing_answer(job, sha, t)
        if note is None:
            self.jobs.landing._bg(job, self.jobs.landing.evaluate(job))  # arm it, if everything else still allows
        else:
            self.jobs.landing._note(job, note)
        return {"ok": True, "approval": aid, "job": jid, "landing": "allowed" if note is None else note}

    async def tick(self, t: float | None = None) -> None:
        """The receiver's loop: open approvals for waiting jobs (enforce), poll the ones being followed, withdraw
        the ones nobody needs any more, and ask again about landings Tower couldn't answer."""
        t = self.clock() if t is None else t
        if self.mode("job.release") == "enforce":
            for job in [j for j in self.jobs._jobs.values() if j.state == JobState.AWAITING_APPROVAL]:
                if job.approval is None or job.approval.workspace_approval_id \
                        or t - self._polled.get("open:" + job.id, float("-inf")) < POLL:
                    continue
                self._polled["open:" + job.id] = t
                async with self._locks[job.id]:
                    if job.state == JobState.AWAITING_APPROVAL and not job.approval.workspace_approval_id:
                        await self.open(job)
        for key in self.store.kv_keys(REL):
            aid, jid = key[len(REL):], self.store.kv_get(key)
            job = self._job(jid) if jid else None
            if job is None or job.state != JobState.AWAITING_APPROVAL or job.approval is None \
                    or job.approval.workspace_approval_id != aid:
                self._unfollow(REL, aid)
                self._spawn(self._withdraw(aid, "the job no longer waits for approval in hangar"))
                continue
            await self._poll(aid, t)
        for key in self.store.kv_keys(LAND):
            aid, jid = key[len(LAND):], self.store.kv_get(key)
            job = self._job(jid) if jid else None
            st = job.landing if job is not None else None
            if st is None or st.workspace_approval != aid or st.pr_state in ("merged", "closed") \
                    or st.approved_sha != (st.governance or {}).get("sha"):
                self._unfollow(LAND, aid)
                self._spawn(self._withdraw(aid, "the PR moved, closed or lost its approval"))
                continue
            await self._poll(aid, t)
        if self.mode("landing.approve") == "enforce" and getattr(self.jobs, "landing", None) is not None:
            for job in list(self.jobs._jobs.values()):
                st, g = job.landing, job.landing.governance or {}
                if g.get("decided_by") != "fail_closed" or st.pr_state != "approved" or not st.approved_sha \
                        or g.get("sha") != st.approved_sha or st.armed_sha == st.approved_sha \
                        or t - self._polled.get("land:" + job.id, float("-inf")) < POLL:
                    continue
                self._polled["land:" + job.id] = t
                self.jobs.landing._bg(job, self.jobs.landing.evaluate(job))

    async def _poll(self, aid: str, t: float) -> None:
        if t - self._polled.get(aid, float("-inf")) < POLL:
            return
        self._polled[aid] = t
        try:
            await self.hint(aid)
        except Unavailable as e:
            log.info("governance: %s: %s", aid, e)

    # ================================================================== helpers
    def _keep(self, job: Job, rec: dict[str, Any]) -> None:
        """The record of what was asked and answered, on the job's approval; `told` survives within an outage."""
        if job.approval is None:
            return
        told = (job.approval.governance or {}).get("told")
        if told and rec.get("decided_by") == "fail_closed":
            rec["told"] = told
        job.approval = job.approval.model_copy(update={"governance": rec})
        self.jobs._save(job)

    def _set(self, job: Job, **fields: Any) -> None:
        if job.approval is not None:
            job.approval = job.approval.model_copy(update=fields)
            self.jobs._save(job)

    def _follow(self, job: Job, aid: str | None) -> None:
        if not aid:
            return
        self._set(job, workspace_approval_id=aid)
        self.store.kv_set(REL + aid, job.id, durable=True)

    def _unfollow(self, prefix: str, aid: str) -> None:
        self.store.kv_delete(prefix + aid, durable=True)
        self._polled.pop(aid, None)

    def _unavailable(self, job: Job, t: Decision) -> None:
        """Tower couldn't answer: the job stays waiting; whoever asked is told once per outage."""
        g = (job.approval.governance or {}) if job.approval else {}
        if g.get("told"):
            return
        g["told"] = now()
        self._set(job, governance=g)
        self._tell(job, f"Hangar job `{job.id}` stays waiting for approval: {UNAVAILABLE} ({t.failure}). Nothing "
                        "was allowed; hangar asks again when Tower answers.")

    def _tell(self, job: Job, text: str) -> None:
        self.jobs._event(job, text[:500])
        if job.source_ref:
            self.jobs._spawn(self.jobs._comment(job, job.source_ref, str(self.fleet.broker.redact(text))))

    @staticmethod
    def _cite(t: Decision) -> str:
        parts = [f"Tower {t.approval_id}" if t.approval_id else "Tower"] + ([f"receipt {t.receipt}"] if t.receipt
                                                                            else [])
        return ", ".join(parts)

    async def _withdraw(self, aid: str, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self.receiver.tower().withdraw(aid, reason)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for shadow asks and withdrawals in flight (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()
