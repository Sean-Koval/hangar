"""Core data models shared by the fleet manager, MCP tools, REST API and providers."""

from __future__ import annotations

import secrets
import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def now() -> float:
    return time.time()


class SandboxState(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class Resources(BaseModel):
    cpus: float = 2.0
    memory_gb: float = 4.0
    disk_gb: float | None = None


# --- heavy: named resource profiles (heavy.py). `standard` is the default above; the others run on the heavy tier.
PROFILES: dict[str, Resources] = {
    "standard": Resources(cpus=2, memory_gb=4),
    "large": Resources(cpus=8, memory_gb=32, disk_gb=100),
    "xlarge": Resources(cpus=16, memory_gb=64, disk_gb=200),
}


class Sandbox(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sb"))
    name: str
    environment: str
    provider: str
    state: SandboxState = SandboxState.PENDING
    provider_ref: str | None = None  # container id / claim name / e2b sandbox id
    image: str | None = None
    repo: str | None = None
    branch: str | None = None
    credentials: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    resources: Resources = Field(default_factory=Resources)
    parent: str | None = None  # sandbox or snapshot it was forked from
    created_at: float = Field(default_factory=now)
    expires_at: float | None = None
    last_activity: float = Field(default_factory=now)
    error: str | None = None
    created_by: str | None = None
    info: dict[str, Any] = Field(default_factory=dict)
    person: str | None = None  # --- identity: the owning person (people.py); None: made before people, admins only


class SnapshotKind(StrEnum):
    FULL = "full"  # provider-native: docker commit, GKE pod snapshot, E2B snapshot
    WORKSPACE = "workspace"  # portable tarball of the workspace dir


class Snapshot(BaseModel):
    id: str = Field(default_factory=lambda: new_id("snap"))
    sandbox_id: str
    environment: str
    provider: str
    kind: SnapshotKind
    label: str | None = None
    ref: str  # image tag / tarball path / provider snapshot id
    created_at: float = Field(default_factory=now)
    size_bytes: int | None = None
    person: str | None = None  # --- identity: its sandbox's person


class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str = ""
    duration_ms: int = 0
    truncated: bool = False
    # integrate (#60 x #55): "envd" when the in-sandbox daemon ran it (exec spans and metrics); never serialized
    transport: str | None = Field(default=None, exclude=True)


class CredentialKind(StrEnum):
    API_KEY = "api_key"
    OAUTH_TOKEN = "oauth_token"  # e.g. CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`
    AUTH_FILE = "auth_file"  # e.g. ~/.codex/auth.json (rotating refresh token -> leased)
    SSH_KEY = "ssh_key"
    GIT_TOKEN = "git_token"
    GENERIC = "generic"
    # --- workstream F: git gateway. JSON {app_id, installation_id?, private_key}; used by the gateway only.
    GITHUB_APP = "github_app"


class Credential(BaseModel):
    """Metadata only. Secret material lives encrypted in the vault, never in this model."""

    name: str
    harness: str  # claude-code | codex | antigravity | gemini | git | custom
    kind: CredentialKind
    description: str | None = None
    exclusive: bool = False  # only one sandbox may hold it at a time (rotating refresh tokens)
    leased_to: str | None = None
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    # How to materialize inside a sandbox; filled from the harness profile when omitted.
    env_var: str | None = None
    file_path: str | None = None
    # --- identity: where it may go (identity.exposure). Unset: derived (control-plane for signing keys, GitHub App
    # keys and the GitHub tokens hangar's settings name; sandbox for the rest). `sandbox` shares a control-plane
    # GitHub token with sandboxes; signing keys and App keys stay control-plane whatever this says.
    exposure: Literal["sandbox", "control_plane"] | None = None
    # --- identity: a person's own credential (their Claude or Codex login), used only in their sandboxes; None:
    # a team credential (--- teamcreds: those stored before people became the owner's, teamcreds.migrate)
    person: str | None = None
    # --- clouddoctor: credential health (credhealth.py), from a check that calls no model: when it stops working
    # (None: no known end), when a check last passed, and the last check {status, detail, checked_at, method, by}.
    # Storing a new value starts these over.
    expires_at: float | None = None
    last_ok: float | None = None
    health: dict[str, Any] | None = None


# ---------------------------------------------------------------------------- workstream C: events/usage/budgets
class Usage(BaseModel):
    """Tokens and money one agent turn (or a sum of turns) used. `input_tokens` excludes cache reads and writes."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None  # None: no harness-reported cost and no price for the model
    cost_source: Literal["harness", "prices", "mixed"] | None = None
    model: str | None = None
    # --- overnight/cache: the part of cache_write_tokens written with a 1-hour TTL (priced at 2x input, not
    # 1.25x), and what caching saved against sending the same tokens uncached, from the price table (negative
    # when writes cost more than reads saved; None without a price)
    cache_write_1h_tokens: int = 0
    saved_usd: float | None = None

    @property
    def tokens(self) -> int:
        """New tokens: input, output and cache writes. Cache reads are left out (every model call re-reads the
        cached context, so they would dwarf the rest); they are counted, and priced, separately."""
        return self.input_tokens + self.output_tokens + self.cache_write_tokens

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_hit_ratio(self) -> float | None:
        """--- overnight/cache: the share of prompt tokens read from the cache (None: no prompt tokens)."""
        prompt = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        return round(self.cache_read_tokens / prompt, 4) if prompt else None


class Budget(BaseModel):
    """Caps for one agent turn; the turn is stopped (outcome `over_budget`) when it crosses any of them."""

    max_minutes: int | None = Field(None, ge=1, le=1440)
    max_tokens: int | None = Field(None, ge=1)
    max_cost_usd: float | None = Field(None, gt=0)


class JobBudget(BaseModel):
    """--- frontdoor: caps for a whole job: all its agent turns together (the first, follow-ups, verify-fix and
    auto-fix turns, every best-of-N attempt and the judge). See jobbudget.py."""

    max_cost_usd: float | None = Field(None, gt=0)
    max_tokens: int | None = Field(None, ge=1)
    max_turns: int | None = Field(None, ge=1, le=1000)


class ApiToken(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tok"))
    name: str
    token_hash: str
    scopes: list[str] = Field(default_factory=lambda: ["admin"])
    # jobs-scoped tokens: the vault `webhook` credential that signs this token's job callbacks (set by an admin)
    callback_secret: str | None = None
    max_budget: Budget | None = None  # jobs-scoped tokens: caps the budget callers may ask for (workstream C)
    created_at: float = Field(default_factory=now)
    last_used: float | None = None
    person: str | None = None  # --- identity: who it belongs to; None: people.OWNER (tokens minted before people)


# ---------------------------------------------------------------------------- --- identity: people (people.py)
class Person(BaseModel):
    """Someone on the team. Admins see and act on everything; members on their own work."""

    id: str  # lowercase slug: the stable key records, quotas and tokens use
    name: str = ""
    role: Literal["admin", "member"] = "member"
    created_at: float = Field(default_factory=now)
    # --- teamcreds: their accounts elsewhere ("github:octocat", "slack:U012AB3CD", "jira:<account id>",
    # "email:a@b.co"): jobs from those senders run for this person (people.resolve_external)
    links: list[str] = Field(default_factory=list)


class AuditEvent(BaseModel):
    ts: float = Field(default_factory=now)
    actor: str
    action: str
    target: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------- jobs (spec 0009)
class JobState(StrEnum):
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    VERIFYING = "verifying"
    DELIVERING = "delivering"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # --- workstream A: triggers. Started by someone untrusted; waits (not queued) until a trusted person approves.
    AWAITING_APPROVAL = "awaiting_approval"
    NEEDS_INPUT = "needs_input"  # workstream E: the agent asked a question; a follow-up answers it (not terminal)


JOB_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


class ContextItem(BaseModel):
    """One piece of context handed to the agent as a file (or, for `url`, a link listed in the prompt)."""

    name: str
    kind: Literal["text", "file", "log", "issue", "diff", "url", "patch", "bundle"] = "text"
    content: str | None = None
    encoding: Literal["utf-8", "base64"] = "utf-8"
    url: str | None = None
    untrusted: bool = False  # written by someone outside the trusted list; the prompt says so (workstream A)
    # workstream E: `patch` is a binary diff applied before the turn; `bundle` a base64 bundle of unpushed
    # commits built on `base_commit` (a commit the remote has)
    base_commit: str | None = None

    def size(self) -> int:
        return len(self.content or "")


class Artifact(BaseModel):
    """A file the agent handed back (workstream E), stored encrypted next to the session's checkpoints."""

    path: str  # relative to out/artifacts
    turn: int  # the session turn that produced it
    size: int
    sha256: str
    media_type: str = "application/octet-stream"


class JobEvent(BaseModel):
    ts: float = Field(default_factory=now)
    state: str
    message: str = ""


# --- #166: what an outcome says about the agent. `agent`: the agent's own result (success or failure, counted);
# `infra_error`: infrastructure ended it (infra_reason says what), auto-retried for trials and left out of statistics;
# `cancelled`: someone or something stopped it; `human`: a person stepped in (an interrupt, a question). None: unknown
# (a record from before #166), never read as `agent`. outcomes.py classifies.
OutcomeClass = Literal["agent", "infra_error", "cancelled", "human"]
InfraReason = Literal["preempted", "power_down", "rate_limited", "provider_5xx", "sandbox_create", "checkpoint",
                      "control_plane_restart"]


class Turn(BaseModel):
    kind: Literal["initial", "follow_up"] = "initial"
    message: str | None = None  # follow-up text
    requester: str | None = None
    reply_to: str | None = None  # "github:owner/repo#N": where to post progress for this turn
    run_id: str | None = None
    harness_version: str | None = None  # --- images: the agent CLI version the turn ran (its session turn's)
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    summary: str | None = None
    usage: Usage | None = None  # workstream C
    outcome: str | None = None  # exited | timed_out | cancelled | over_budget | gone | error | needs_input
    outcome_reason: str | None = None
    outcome_class: OutcomeClass | None = None  # --- #166
    infra_reason: InfraReason | None = None
    # --- workstream E: context exchange
    context: list[ContextItem] = Field(default_factory=list)  # attachments (contents dropped once it starts)
    output_schema: dict[str, Any] | None = None
    result: Any = None
    result_error: str | None = None
    question: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    # --- overnight/autofix: why hangar started this turn itself ("verify": its check failed after the turn before;
    # "autofix:ci" / "autofix:review": the PR's CI or a reviewer), what is left of the budget for it, and how often
    # the check failed inside the turn (the Stop hook or the driver sent it back to the agent).
    trigger: str | None = None
    budget: Budget | None = None
    verify_rounds: int = 0


class TestSummary(BaseModel):
    """--- testresults: one run of a check, as structured results (testresults.py): counts, what failed, what is new
    on a red base, the flaky ones, coverage. The run itself (every case, keyed by test id) is a TestRun record."""

    __test__ = False  # not a pytest class
    run: str  # the TestRun's id: GET /api/jobs/{id}/tests
    kind: str  # base | verify | rerun | attempt | in_turn | autoinstall
    at: float = Field(default_factory=now)
    exit_code: int | None = None
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    complete: bool = True  # every case of every report was read
    failing: list[str] = Field(default_factory=list)  # the first few failing ids
    new_failures: list[str] | None = None  # failing here, not on the base (None: no structured base to compare to)
    flaky: list[str] = Field(default_factory=list)  # failing or retried here, and on the repo's flaky list
    coverage: float | None = None  # line coverage, percent
    coverage_delta: float | None = None  # against the base check's, in points
    reports: int = 0
    file: str | None = None  # where the agent reads the whole of it in its sandbox


class Baseline(BaseModel):
    """--- frontdoor: the job's standing `verify` run once on its base commit, before the agent starts (baseline.py).
    Shared by every job on the same repo, base commit and command."""

    sha: str
    exit_code: int | None = None
    failures: list[str] = Field(default_factory=list)  # test ids it could read from the output
    parsed: bool = False  # failing and the ids above are all of them; else nothing can be subtracted
    tail: str | None = None
    at: float = Field(default_factory=now)
    job_id: str | None = None  # the job whose sandbox ran it
    # --- testresults: its structured results (a TestRun), when the check wrote reports hangar could read all of;
    # then a red base's failures are subtracted by test id from those, not from the output's text
    tests: str | None = None
    test_failures: int | None = None

    @property
    def red(self) -> bool:
        return self.exit_code not in (0, None)


class JobResult(BaseModel):
    base_sha: str | None = None
    head_sha: str | None = None
    commits: list[str] = Field(default_factory=list)  # "abc1234 subject", newest first
    diffstat: str | None = None
    # --- review(#114): the delivered change's files, repo-relative (the trace's hangar.diff.*), at most 1,000
    files: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    pushed: bool = False
    pr_url: str | None = None
    pr_number: int | None = None
    verify_exit_code: int | None = None
    verify_tail: str | None = None
    summary: str | None = None
    # --- workstream E: the latest turn's structured result and open question; artifacts of every turn
    data: Any = None
    result_error: str | None = None
    question: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    # --- overnight/autofix: existing files the verify command relies on that the agent changed (strategies.py)
    touched_tests: list[str] = Field(default_factory=list)
    # --- frontdoor: the check on the base commit, and the failures the last check added to it (None: it passed,
    # the base was green, or they couldn't be told apart)
    baseline: Baseline | None = None
    new_failures: list[str] | None = None
    dropped_files: list[str] = Field(default_factory=list)  # agent scratch files kept out of the PR (scratch.py)
    tests: TestSummary | None = None  # --- testresults: the last check's structured results
    # --- #166: the class of the job's final outcome (set as it ends): infra_error jobs are left out of yield
    outcome_class: OutcomeClass | None = None
    infra_reason: InfraReason | None = None


# --- overnight/autofix: the PR auto-fix loop's memory, per job (autofix.py)
class AutofixRound(BaseModel):
    at: float = Field(default_factory=now)
    trigger: str  # "ci", "review" or "ci,review"
    head_sha: str | None = None
    items: list[str] = Field(default_factory=list)  # "check lint: failure", "review by alice: changes_requested"


class AutofixState(BaseModel):
    pending: list[dict[str, Any]] = Field(default_factory=list)  # triggers waiting for the coalescing window
    due_at: float | None = None
    rounds: list[AutofixRound] = Field(default_factory=list)
    seen: list[str] = Field(default_factory=list)  # deliveries already taken (redeliveries do nothing)
    fixed_shas: list[str] = Field(default_factory=list)  # heads a CI round already ran for: one per push
    stopped: str | None = None  # why the loop stopped (the round limit, someone else pushed)


# --- landing: where a job's PR is on its way to merged (landing.py)
class LandingState(BaseModel):
    pr_state: Literal["open", "approved", "merged", "closed"] | None = None
    draft: bool = False
    node_id: str | None = None  # the PR's GraphQL id: auto-merge is armed and disarmed through it
    head_sha: str | None = None  # the PR's head, as GitHub or hangar's last push reported it
    checks_sha: str | None = None  # the commit `checks` are about
    checks: dict[str, str] = Field(default_factory=dict)  # check run, suite or status -> conclusion, or "pending"
    ci_green_at: float | None = None  # when the required checks first all passed on `ci_green_sha`
    ci_green_sha: str | None = None
    approved_by: str | None = None  # the GitHub login whose approval counts (repo policy approvers, or CODEOWNERS)
    approved_sha: str | None = None  # the commit that approval was given on; it covers nothing pushed later
    approved_at: float | None = None
    review_id: int | None = None
    armed_sha: str | None = None  # auto-merge armed by hangar, for exactly this commit
    armed_at: float | None = None
    merged_at: float | None = None
    merged_by: str | None = None
    closed_at: float | None = None
    green_to_merge_seconds: float | None = None  # from the required checks passing on the merged head to the merge
    packet_comment_id: int | None = None  # the review packet: one comment, edited as the PR changes
    files: list[str] = Field(default_factory=list)  # paths the PR touches (capped)
    risk: str | None = None  # high | normal | low
    push_mode: str | None = None  # gateway | direct: whether the sandbox held a GitHub token
    note: str | None = None  # why it isn't landing yet
    ended: bool = False  # merged or closed: the session took its final checkpoint and let the sandbox go


class JobApproval(BaseModel):
    """Why a job waits for a trusted person, and who decided (workstream A)."""

    requested_by: str  # "github:<login>" or "webhook:<source>"
    reason: str = ""
    expires_at: float | None = None
    decided_by: str | None = None
    decided_at: float | None = None
    decision: Literal["approved", "rejected", "expired"] | None = None
    # --- approvals (#121): the deciding person, whether it was their own work (HANGAR_SELF_APPROVAL=allow_solo), and
    # the rule that let them decide: admin | repo_approver | front_door | solo | system (approvals.can_decide)
    decided_person: str | None = None
    self_approved: bool = False
    rule: str | None = None


# --- workstream D: forks/best-of-N
class Attempt(BaseModel):
    """One of a best-of-N job's attempts: a session forked from the job's prepared base session."""

    index: int  # 1-based; the agent sees it as $HANGAR_ATTEMPT
    session_id: str | None = None
    branch: str | None = None  # the attempt session's own branch (inside its sandbox)
    pushed_branch: str | None = None  # set when losers are pushed (HANGAR_BEST_OF_N_PUSH_ATTEMPTS)
    # --- trust: `waiting`: the power-down cut its turn off; it resumes from its checkpoint at power-up
    state: Literal["pending", "running", "verifying", "waiting", "done"] = "pending"
    restarts: int = 0  # --- trust: times it resumed from its checkpoint (restarts.resume_attempt)
    outcome: str | None = None  # exited | timed_out | cancelled | gone | error
    outcome_class: OutcomeClass | None = None  # --- #166
    infra_reason: InfraReason | None = None
    run_id: str | None = None
    exit_code: int | None = None
    verify_exit_code: int | None = None
    verify_tail: str | None = None
    head_sha: str | None = None
    diffstat: str | None = None
    diff_lines: int | None = None  # lines added + deleted against the base
    files: list[str] = Field(default_factory=list)  # "M path" lines from git diff --name-status (capped)
    touched_tests: list[str] = Field(default_factory=list)  # existing test/verify files it changed or removed
    new_failures: list[str] | None = None  # --- frontdoor: failures its check added to a red base (baseline.py)
    tests: TestSummary | None = None  # --- testresults: its check's structured results
    score: float | None = None
    rank: int | None = None  # 1 = delivered
    summary: str | None = None
    cost_usd: float | None = None  # from its usage: reported by the harness or priced (--- interactions)
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


class Job(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job"))
    title: str
    instructions: str
    source: Literal["api", "mcp", "cli", "github", "webhook", "schedule", "slack", "a2a",
                    "jira", "github-projects",
                    "env-setup",
                    "experiment"] = "api"  # --- intake; --- autoinstall: env setup's jobs; --- interfaces: trials
    source_ref: str | None = None  # e.g. "github:owner/repo#12"
    requester: str = "system"
    owner: str | None = None  # token id ("tok-…"), "admin", or "github:<login>"; jobs-scoped tokens see only theirs
    repo: str | None = None
    base_branch: str | None = None
    branch: str = ""
    environment: str
    harness: str | None = None
    model: str | None = None
    provider: str | None = None
    verify: str | None = None
    verify_source: Literal["job", "repo", "environment"] | None = None  # --- frontdoor: where `verify` came from
    deliver: Literal["draft_pr", "pr", "branch", "none"] = "draft_pr"
    context: list[ContextItem] = Field(default_factory=list)
    callback_url: str | None = None
    callback_secret: str | None = None  # vault credential name holding the HMAC key
    idempotency_key: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    timeout_minutes: int = 60
    keep_minutes: int = 120
    budget: Budget | None = None  # per agent turn (workstream C)
    job_budget: JobBudget | None = None  # --- frontdoor: all the job's turns together
    output_schema: dict[str, Any] | None = None  # workstream E: JSON Schema for the agent's out/result.json
    state: JobState = JobState.QUEUED
    reason: str | None = None
    cancel_requested: bool = False
    sandbox_id: str | None = None
    session_id: str | None = None  # the session that carries this job's sandbox, conversation and checkpoints
    # --- overnight/sweep: usage of the sessions that carried this job and were deleted since (session id -> total)
    deleted_session_usage: dict[str, Usage] = Field(default_factory=dict)
    turns: list[Turn] = Field(default_factory=list)
    result: JobResult = Field(default_factory=JobResult)
    events: list[JobEvent] = Field(default_factory=list)
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    # --- workstream D: forks/best-of-N
    strategy: Literal["single", "best_of_n"] = "single"
    n: int = 1
    judge: bool = False
    attempts: list[Attempt] = Field(default_factory=list)
    # --- workstream A: triggers
    approval: JobApproval | None = None
    untrusted_source: str | None = None  # who wrote untrusted task text ("github:<login>", "webhook:<source>")
    # --- overnight/autofix: the verify gate inside the turn, and the PR auto-fix loop (None: the defaults)
    verify_in_loop: bool | None = None
    max_verify_rounds: int | None = None
    autofix: str | None = None  # "ci,reviews" | "ci" | "reviews" | "off"
    autofix_state: AutofixState = Field(default_factory=AutofixState)
    person: str | None = None  # --- identity: the person it runs for (None: GitHub, Slack, webhooks, older jobs)
    # --- reconciler: times the job resumed on its own after a control-plane restart interrupted it (restarts.py)
    restarts: int = 0
    landing: LandingState = Field(default_factory=LandingState)  # --- landing
    taint: dict[str, Any] | None = None  # --- taint (#129): its untrusted input, and its sessions' taint (mirrored)
    harness_lock: dict[str, Any] | None = None  # --- harness locks (#134): the lock it runs from (harness_thaw.pin)


# ---------------------------------------------------------------------------- scheduled jobs (workstream A)
class ScheduledJob(BaseModel):
    """A job template submitted on a cron schedule. Each slot runs at most once, across restarts."""

    id: str = Field(default_factory=lambda: new_id("sch"))
    name: str
    cron: str  # 5 fields: minute hour day-of-month month day-of-week
    timezone: str = "UTC"  # IANA name
    template: dict[str, Any]  # SubmitJob fields
    enabled: bool = True
    allow_overlap: bool = False  # start a run while the previous one is still going
    owner: str = "admin"
    last_run_slot: float | None = None  # the slot (epoch seconds) last started or skipped
    next_run_at: float | None = None
    last_job_id: str | None = None
    last_error: str | None = None
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    person: str | None = None  # --- identity: its jobs run for this person


# ---------------------------------------------------------------------------- sessions (spec 0010)
class SessionState(StrEnum):
    STARTING = "starting"  # booting a sandbox and restoring the last checkpoint
    IDLE = "idle"  # sandbox attached, no agent turn running
    RUNNING = "running"  # an agent turn is running
    SUSPENDING = "suspending"
    SUSPENDED = "suspended"  # no sandbox; resumable from the last checkpoint
    ARCHIVED = "archived"  # checkpoints dropped; record kept
    FAILED = "failed"  # the last boot failed; retry with open


SESSION_LIVE = {SessionState.STARTING, SessionState.IDLE, SessionState.RUNNING}

# --- workstream steer: live turns. How tool permission prompts are answered: auto = no prompts (the harness runs
# with its permission checks off, as before), ask = a person answers (denied after HANGAR_APPROVAL_TIMEOUT_SECONDS),
# deny-risky = hangar answers at once, refusing what its rules call risky (driver.risky).
ApprovalPolicy = Literal["auto", "ask", "deny-risky"]
CachePolicy = Literal["auto", "5m", "1h", "off"]  # --- overnight/cache (cache.py)


class Approval(BaseModel):
    """A harness asking to use a tool during a live turn, and what became of it."""

    id: str
    tool: str = ""
    detail: str = ""  # the command, or the tool's input (clipped)
    requested_at: float = Field(default_factory=now)
    expires_at: float | None = None
    status: Literal["pending", "allowed", "allowed_always", "denied", "cancelled"] = "pending"
    by: str | None = None  # who answered: a hangar caller, "policy", "timeout" or "harness" (withdrawn)
    message: str | None = None
    decided_at: float | None = None
    taint: str | None = None  # --- taint (#130): why it needs a person: "git push, after reading untrusted content"


class Steer(BaseModel):
    """A message sent into a running turn."""

    id: str = Field(default_factory=lambda: new_id("str"))
    text: str
    requester: str | None = None
    ts: float = Field(default_factory=now)
    kind: Literal["steer", "interrupt"] = "steer"
    status: Literal["sending", "delivered", "queued", "failed"] = "sending"  # queued: it starts the next turn
    error: str | None = None


class SessionTurn(BaseModel):
    message: str
    requester: str | None = None
    run_id: str | None = None
    harness: str | None = None
    harness_version: str | None = None  # --- images: what the harness's pack installed (`--version`), when known
    mode: Literal["new", "resume", "handoff", "fork"] = "new"
    conversation: str | None = None  # the harness conversation id this turn used (preset ids are known up front)
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    summary: str | None = None
    usage: Usage | None = None  # workstream C: live while the turn runs, final after
    outcome: str | None = None  # exited | timed_out | cancelled | over_budget | gone | error | needs_input
    outcome_reason: str | None = None
    outcome_class: OutcomeClass | None = None  # --- #166
    infra_reason: InfraReason | None = None
    event_cursor: dict[str, Any] | None = None  # where event collection is in the run log (restart-safe)
    # --- workstream E: context exchange
    context: list[ContextItem] = Field(default_factory=list)  # attachments (contents dropped once it starts)
    output_schema: dict[str, Any] | None = None
    result: Any = None  # out/result.json (valid against output_schema when there is one)
    result_error: str | None = None
    question: str | None = None  # out/QUESTION.md
    progress: dict[str, Any] | None = None  # the last out/progress.jsonl line
    artifacts: list[Artifact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    repair_run_id: str | None = None  # the one extra run that asked for a missing or invalid result
    repair_usage: Usage | None = None  # --- interactions: what that run used (part of the turn's total)
    # --- workstream steer: a live turn runs under hangar's driver (steer, interrupt, approvals)
    live: bool = False
    approval_policy: ApprovalPolicy | None = None
    approvals: list[Approval] = Field(default_factory=list)
    steers: list[Steer] = Field(default_factory=list)
    # --- overnight/cache: the prompt-cache TTL the turn asked for (5m | 1h | off; None: the harness has no
    # setting), and how long it waited for a best-of-N or fork sibling to write the shared prefix first
    cache_ttl: str | None = None
    cache_wait_seconds: float | None = None
    # --- interfaces: the digest of the effective harness manifest captured as the turn started (manifest.py)
    manifest: str | None = None
    harness_digest: str | None = None  # and its two halves (#131): the harness's, the environment's
    environment_digest: str | None = None
    # --- taint (#129): {"tainted": true, "first_seq": n, "sources": [{"kind", "seq", "ref"}...]} once the turn has
    # read untrusted content (first_seq 0: it started tainted, from its context or an earlier turn); None: it hasn't
    taint: dict[str, Any] | None = None
    # --- harness locks (#134): turn 1 of a session thawed from a lock, compared with it: [] matched,
    # [{"component", "lock", "effective"}] drifted; None: not checked (not turn 1, no lock, or not yet)
    drift: list[dict[str, Any]] | None = None


class Checkpoint(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ckpt"))
    ts: float = Field(default_factory=now)
    reason: str = "turn"  # turn | suspend | destroy | interval | manual
    head: str | None = None
    base: str | None = None
    branch: str | None = None
    fingerprint: str | None = None  # HEAD + dirty-tree hash: skip identical checkpoints
    parts: dict[str, int] = Field(default_factory=dict)  # part name -> bytes (plaintext)
    warnings: list[str] = Field(default_factory=list)
    # --- perf: part name -> sha256 of the plaintext, as the sandbox computed it. A part whose digest matches the
    # previous checkpoint is copied inside the checkpoint store instead of being read and uploaded again.
    digests: dict[str, str] = Field(default_factory=dict)


class Session(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ses"))
    name: str
    title: str = ""
    owner: str = "admin"
    environment: str
    harness: str | None = None
    model: str | None = None
    repo: str | None = None
    base_branch: str | None = None
    base_sha: str | None = None
    branch: str = ""
    harness_session: str | None = None  # the harness's own conversation id
    harness_session_for: str | None = None  # which harness that id belongs to
    sandbox_id: str | None = None
    state: SessionState = SessionState.SUSPENDED
    reason: str | None = None
    turns: list[SessionTurn] = Field(default_factory=list)
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    idle_minutes: int = 30
    job_id: str | None = None  # set when the session carries a job (spec 0009/0010 M2)
    provider: str | None = None
    extra_dirs: list[str] = Field(default_factory=list)  # more $HOME-relative paths to checkpoint (job context)
    budget: Budget | None = None  # per agent turn (workstream C)
    labels: dict[str, str] = Field(default_factory=dict)
    needs_input: str | None = None  # workstream E: the agent's open question; the next send answers it
    created_at: float = Field(default_factory=now)
    last_active_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    # --- workstream D: forks/best-of-N
    parent_id: str | None = None  # the session this one was forked from
    forked_from_checkpoint: str | None = None
    fork_conversation: str | None = None  # the parent's conversation id; the next turn forks it, then clears it
    approvals: ApprovalPolicy | None = None  # --- workstream steer: unset = the environment's, else HANGAR_APPROVALS
    cache: CachePolicy | None = None  # --- overnight/cache: unset = the environment's, else HANGAR_CACHE
    person: str | None = None  # --- identity: the owning person; a job's session has its job's
    warnings: list[str] = Field(default_factory=list)  # --- #125: e.g. a subscription login in its sandbox
    # --- canary (pins.py): pinned at creation. The policy `model` was resolved from (`model` is then its concrete
    # id), and the locked version of the harness's CLI: a turn on another version hands off instead of resuming.
    model_policy: str | None = None
    harness_version: str | None = None
    # --- taint (#129): sticky; set by the first tainted turn (its number and seq), inherited by forks
    taint: dict[str, Any] | None = None
    # --- harness locks (#134): the lock it was thawed from, as claimed (harness_thaw.pin: ref, name, revision,
    # digest, harness, version, model, approvals, components)
    harness_lock: dict[str, Any] | None = None


# ---------------------------------------------------------------------------- workstream F: git gateway (spec 0003)
class SandboxGrant(BaseModel):
    """What one sandbox may do through the git gateway. The sandbox holds only the `hgs_` token, which is
    useless anywhere but the gateway; the real GitHub credential stays in the control plane."""

    id: str = Field(default_factory=lambda: new_id("gnt"))
    sandbox_id: str
    token_hash: str  # sha256 of "hgs_<random>"; the token itself is shown once, to the sandbox
    host: str = "github.com"
    repos: list[str] = Field(default_factory=list)  # "owner/name", lower-case
    push_refs: list[str] = Field(default_factory=list)  # exact refs, or patterns ending in "/**"; empty: fetch only
    credential: str  # vault credential (git_token or github_app) the gateway uses upstream
    # --- least-privilege: a delivery grant (delivery.py) pushes exactly these commits: {ref: commit id}
    pins: dict[str, str] = Field(default_factory=dict)
    created_at: float = Field(default_factory=now)
    expires_at: float | None = None
