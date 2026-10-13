"""Runtime settings. Every field can be set with a HANGAR_* environment variable."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HANGAR_", env_file=".env", extra="ignore")

    # --- server -------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8787
    public_url: str = "http://localhost:8787"
    # Bootstrap admin token. If unset on first start, one is generated and printed once.
    admin_token: str | None = None
    # Built-in OAuth 2.1 server for MCP clients that need it (ChatGPT/claude.ai connectors).
    # public_url must be https (or localhost) for OAuth to be enabled.
    oauth_enabled: bool = True
    # Redirect hosts OAuth clients may register: a host (https, and http for loopback; subdomains too) or an exact
    # "scheme://host" for a native app's own scheme. "*" allows any (not recommended). --- overnight/sweep: Cursor's
    # app scheme is allowed by default, like the loopback ports native clients use (docs/security.md).
    oauth_redirect_hosts: list[str] = Field(default_factory=lambda: [
        "chatgpt.com", "chat.openai.com", "openai.com", "claude.ai", "claude.com", "localhost", "127.0.0.1",
        "cursor://anysphere.cursor-mcp"])
    oauth_max_clients: int = 200
    oauth_grant_days: int = 90  # absolute lifetime of an approval, refreshes included

    # --- state --------------------------------------------------------------
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".hangar")
    # Fernet key for the credential vault. If unset, a key file is created in data_dir.
    secret_key: str | None = None

    # --- environments -------------------------------------------------------
    env_dirs: list[Path] = Field(default_factory=lambda: [Path("envs")])
    image_prefix: str = "hangar-env"
    # Registry to push template images to (required for kubernetes provider), e.g.
    # us-central1-docker.pkg.dev/my-proj/hangar
    registry: str | None = None
    # PEM bundle trusted inside every image (corporate TLS-inspecting proxies, private CAs).
    extra_ca_bundle: Path | None = None

    # --- providers ----------------------------------------------------------
    default_provider: Literal["docker", "kubernetes", "e2b"] = "docker"
    enabled_providers: list[str] = Field(default_factory=lambda: ["docker"])

    # docker
    docker_runtime: str | None = None  # e.g. "runsc" for gVisor, "kata" if configured
    # --- isolation: unset = hangar's own network (hangar-sandboxes, bridge hangar0), where sandboxes can't reach each
    # other; a name = that network as it is (`bridge` is Docker's default, where they can). providers/docker_net.py
    docker_network: str | None = None
    # Mount harness packs with `--mount type=image` (Docker 28+ with the containerd image store). None = detect.
    docker_image_volumes: bool | None = None

    # kubernetes / agent-sandbox
    k8s_namespace: str = "hangar-sandboxes"
    k8s_in_cluster: bool = False
    k8s_runtime_class: str | None = "gvisor"  # gvisor | kata-qemu-runtime-rs | None (runc)
    k8s_gke_node_selector: bool = True  # add sandbox.gke.io/runtime selector+toleration for gvisor
    k8s_pod_snapshots: bool = False  # GKE Pod snapshots (podsnapshot.gke.io): full memory+fs forks
    k8s_snapshot_service_account: str = "hangar-sandbox"  # KSA with GCS access for the snapshot bucket
    k8s_snapshot_timeout_seconds: int = 600
    # Service account Cloud Build runs as for in-cluster builds (the control plane's own GSA). Without it
    # Cloud Build uses the project's default SA, which the control plane usually may not act as.
    k8s_build_service_account: str | None = None
    # Mount harness packs as OCI image volumes (Kubernetes >= 1.35, stable in 1.36). Off: bake into images.
    k8s_image_volumes: bool = True
    k8s_stream_uploads: bool = True  # one exec with stdin per file upload; falls back to 96 KB chunks

    # e2b
    e2b_api_key: str | None = None
    e2b_domain: str | None = None
    # How E2B pulls environment images from HANGAR_REGISTRY (private registries need one of these):
    e2b_gcp_service_account_json: str | None = None  # path or JSON; Artifact Registry / GCR reader key
    e2b_aws_access_key_id: str | None = None  # ECR
    e2b_aws_secret_access_key: str | None = None
    e2b_aws_region: str | None = None
    e2b_registry_username: str | None = None  # any other registry (basic auth)
    e2b_registry_password: str | None = None

    # --- envd: in-sandbox daemon for exec, files and terminals (docs/envd.md) ---
    envd: Literal["auto", "off"] = "auto"  # auto: Docker and Kubernetes sandboxes use it when it can be reached
    envd_dir: Path | None = None  # holds hangar-envd-<arch>; default /usr/local/lib/hangar, then envd/dist

    # --- e2b-api: E2B-compatible API and sandbox URLs for the e2b SDKs (docs/e2b.md) ---
    e2b_api_path: str = "/e2b"  # where the control API is mounted; "" turns the API and sandbox routing off
    # What E2B clients set as E2B_DOMAIN: the API also answers at api.<domain>, sandboxes at <port>-<id>.<domain>.
    e2b_api_domain: str | None = None
    e2b_templates: dict[str, str] = Field(default_factory=dict)  # E2B template name -> environment, e.g. base
    # Environments a `sandboxes` token gets with their credentials, secrets and gateway grant (admin tokens always
    # do). Any other environment is created for such tokens with no credentials, or refused if it needs secrets.
    e2b_trusted_environments: list[str] = Field(default_factory=list)
    e2b_max_timeout_seconds: int = 86400  # E2B's own limit (24 h on Pro)
    e2b_max_sandboxes_per_owner: int = 20  # running and paused, per `sandboxes` token; 0 = off
    e2b_rate_per_minute: float = 600  # control API requests per `sandboxes` token (a token bucket); 0 = off
    e2b_rate_burst: int = 120
    e2b_create_per_minute: float = 30  # sandbox creations per `sandboxes` token
    e2b_create_burst: int = 10
    e2b_max_streams_per_sandbox: int = 256  # requests in flight through the sandbox router, per sandbox

    # --- fleet defaults -----------------------------------------------------
    default_ttl_minutes: int = 240
    max_sandboxes: int = 50
    reaper_interval_seconds: int = 60
    sandbox_user: str = "agent"
    workspace_dir: str = "/workspace"

    # --- jobs (spec 0009) ---------------------------------------------------
    job_concurrency: int = 4  # jobs provisioning/running at once; the rest wait in the queue
    job_poll_seconds: float = 5.0
    job_timeout_minutes: int = 60  # per agent turn
    job_keep_minutes: int = 120  # sandbox kept after a turn for follow-ups and take-over
    job_verify_timeout_seconds: int = 1800
    job_max_context_bytes: int = 2_000_000
    job_max_context_items: int = 50
    # Hosts allowed for plain-http callbacks or private-address callbacks (https to public hosts always allowed).
    job_callback_hosts: list[str] = Field(default_factory=list)

    # --- sessions (spec 0010) -----------------------------------------------
    session_idle_minutes: int = 30  # suspend (checkpoint + release the sandbox) after this long idle
    session_checkpoint_minutes: int = 10  # periodic checkpoint while a sandbox is attached and changed
    session_keep_checkpoints: int = 5
    session_max_untracked_mb: int = 20  # untracked files above this are not checkpointed (warning)
    session_max_part_mb: int = 100  # any other checkpoint part above this is skipped (warning)
    session_handoff_turns: int = 10  # earlier turns summarised in a handoff prompt
    # Where checkpoints live: unset = <data_dir>/sessions; gs://bucket[/prefix] = GCS via Workload Identity
    # (durable, doesn't fill the control plane's disk; terraform output session_bucket).
    session_store: str | None = None
    session_poll_seconds: float = 5.0
    session_turn_timeout_minutes: int = 120
    pre_destroy_timeout_seconds: float = 300.0
    # Waiting for a sandbox to start covers node scale-up (2-4 min on GKE) plus a first image pull (1-3 min).
    sandbox_start_timeout_seconds: int = 900
    # Environments to build at startup so the first job/session doesn't wait on an image build. Unset = the
    # environments mapped in HANGAR_GITHUB_REPOS; [] = none.
    prebuild_environments: list[str] | None = None

    # --- workstream D: forks/best-of-N --------------------------------------
    fork_max: int = 8  # children per fork_session call
    best_of_n_max: int = 8  # attempts per best-of-N job (each is a sandbox running at the same time)
    best_of_n_push_attempts: bool = False  # also push the losing attempts as <job branch>-attempt-<i>
    best_of_n_judge_timeout_minutes: int = 15

    # --- GitHub (jobs delivery and trigger) ---------------------------------
    github_api_url: str = "https://api.github.com"
    # Vault credential holding a GitHub token the *control plane* uses to open PRs and comment.
    github_token_credential: str | None = None
    # Vault credential holding the webhook secret; the webhook route is off until this is set.
    github_webhook_secret_credential: str | None = None
    # Repos that may trigger jobs, mapped to the environment to run them in: {"owner/repo": "env"}.
    github_repos: dict[str, str] = Field(default_factory=dict)
    github_trigger_label: str = "hangar"

    # --- workstream C: events/usage/budgets ----------------------------------
    # USD per million tokens, merged over the built-in table in events.py (dated list prices):
    # {"my-model": {"input": 1.0, "output": 4.0, "cache_read": 0.1, "cache_write": 1.25}}. Keys match model ids
    # exactly or as a prefix followed by a date/variant suffix.
    model_prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    # Default budget for each agent turn of jobs and sessions that don't set one. Unset = no limit.
    budget_max_minutes: int | None = None
    budget_max_tokens: int | None = None
    budget_max_cost_usd: float | None = None
    events_max_mb: int = 20  # per-session event log size before it rotates (one rotated file is kept)
    # --- workstream A: triggers ----------------------------------------------
    # GitHub logins and "@org/team" entries whose /hangar comments and labels start jobs right away. Unset: the
    # OWNER/MEMBER/COLLABORATOR author associations count as trusted. Anyone else's trigger waits for approval.
    github_trusted: list[str] = Field(default_factory=list)
    github_team_cache_seconds: int = 600
    job_approval_ttl_hours: float = 72  # an unapproved job is cancelled after this long
    # Signed inbound webhooks at /api/hooks/inbound/<source>: {"sentry": {"secret": "...", ...}} (docs/triggers.md)
    inbound_webhooks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    inbound_max_body_bytes: int = 1_000_000
    schedule_poll_seconds: float = 10.0  # scheduler and approval-expiry loop
    # --- workstream E: context exchange (spec 0011 §2) ---------------------
    exchange_max_patch_mb: int = 10  # a `patch` context item, and get_session_patch's largest answer
    exchange_max_bundle_mb: int = 50  # a `bundle` context item (unpushed commits)
    exchange_max_result_kb: int = 1024  # out/result.json
    exchange_max_artifacts: int = 50  # new artifacts collected per turn; more are skipped with a warning
    exchange_max_artifact_mb: int = 25  # per file
    exchange_max_artifacts_mb: int = 100  # per turn
    exchange_inline_artifact_kb: int = 512  # MCP get_artifact returns at most this much (text or base64)
    exchange_repair_timeout_minutes: int = 10  # the one repair run for a missing or invalid result
    # --- workstream B: slack (docs/slack.md) ---------------------------------
    # Vault credentials: the app's signing secret (verifies requests; the routes are off until it's set) and its
    # bot token (xoxb-…, posts replies and reads threads).
    slack_signing_secret_credential: str | None = None
    slack_bot_token_credential: str | None = None
    # Channels that may start jobs, each with its default repo: {"C0123ABCD": "owner/repo"}. Repos must also be
    # in HANGAR_GITHUB_REPOS (an exact git URL works too, as there).
    slack_channels: dict[str, str] = Field(default_factory=dict)
    # Slack user ids allowed to start and steer jobs (empty: anyone in a mapped channel).
    slack_users: list[str] = Field(default_factory=list)
    slack_api_url: str = "https://slack.com/api"
    # --- workstream F: git gateway (spec 0003, docs/security.md) -------------
    # URL sandboxes use to reach the gateway, e.g. http://hangar-gateway.hangar.svc:8788 on GKE or
    # http://host.docker.internal:8788 on Docker. Unset = no gateway; environments asking for it fail to start.
    gateway_url: str | None = None
    # The gateway listens on its own port so a NetworkPolicy can let sandboxes reach it and nothing else of the
    # control plane. 0 = serve /git on the main port instead.
    gateway_port: int = 8788
    gateway_host: str | None = None  # bind address; default HANGAR_HOST
    # Jobs and sessions use the gateway when it is configured, unless their environment says credential_mode: direct.
    gateway_for_jobs: bool = True
    # Upstream git hosts the gateway proxies to: {host: base URL}. Only these hosts get a grant.
    gateway_upstreams: dict[str, str] = Field(default_factory=lambda: {"github.com": "https://github.com"})
    # Refs every grant may push besides the sandbox's own branch. "/**" matches anything below.
    gateway_push_refs: list[str] = Field(default_factory=lambda: ["refs/heads/hangar/**"])
    gateway_allow_tags: bool = False
    gateway_credential: str | None = None  # upstream credential when the sandbox has no git credential of its own
    gateway_max_push_mb: int = 512
    gateway_timeout_seconds: float = 120.0  # upstream connect/read timeout (per read, not per request)
    # --- workstream G: a2a (spec 0011, docs/a2a.md) -------------------------
    a2a_enabled: bool = True  # agent card at /.well-known/agent-card.json, JSON-RPC at /a2a
    a2a_default_repo: str | None = None  # repo for A2A tasks that don't name one in their metadata
    a2a_poll_seconds: float = 1.0  # how often streams and push notifications look for job changes
    a2a_max_wait_seconds: int = 1800  # a blocking SendMessage returns the task as it stands after this long
    a2a_max_request_bytes: int = 4_000_000  # base64 attachments are ~4/3 of HANGAR_JOB_MAX_CONTEXT_BYTES
    a2a_max_push_configs: int = 5  # push notification configs per task
    # --- front-door-auth: rate limits, caps and replay protection (docs/security.md) ------------
    # Job submissions and follow-ups per caller (a jobs token, a GitHub or Slack user, a webhook source): a token
    # bucket refilling at this rate. Admin tokens and schedules are exempt. 0 = off.
    job_rate_per_minute: float = 30
    job_rate_burst: int = 10
    # The same per front door (github, slack, webhook, a2a, api, mcp), across all its callers.
    job_source_rate_per_minute: float = 120
    job_source_rate_burst: int = 60
    job_max_active_per_owner: int = 10  # unfinished jobs (incl. waiting for approval or input) per caller; 0 = off
    max_request_mb: int = 100  # request bodies from a valid token (jobs carry bundles); anonymous ones get 64 KB
    webhook_replay_hours: float = 168  # how long a signed delivery is remembered so a replay is recognised
    audit_rejections_per_minute: int = 30  # refused requests audited per client address or caller; more are counted
    slack_teams: list[str] = Field(default_factory=list)  # other workspaces (Enterprise Grid) whose members count
    a2a_max_streams_per_owner: int = 20  # open A2A streams and blocking sends per caller
    # --- workstream mcp-modern (docs/mcp.md) --------------------------------
    # Serve pre-2026 MCP clients statelessly (no Mcp-Session-Id). Only for several replicas without sticky routing:
    # it costs those clients elicitation (hangar asking you an agent's question) and resource notifications.
    # 2026-07-28 clients are sessionless either way.
    mcp_stateless_http: bool = False
    # --- workstream steer: live turns (docs/steering.md) ----------------------
    # Run Claude Code and Codex turns under hangar's driver, so they can be steered, interrupted and asked for tool
    # approvals. Off (or a sandbox without python3): one-shot turns, as before.
    live_turns: bool = True
    # How tool permission prompts are answered when neither the session nor its environment says: auto (no
    # prompts), ask (a person answers; denied after approval_timeout_seconds) or deny-risky (hangar's rules).
    approvals: Literal["auto", "ask", "deny-risky"] = "auto"
    approval_timeout_seconds: int = Field(600, ge=10, le=86400)
    # --- perf: control-plane responsiveness -----------------------------------
    # Worker threads for blocking provider calls (docker-py, the kubernetes client, e2b) and file I/O. Python's
    # default is min(32, CPUs + 4): 6 on a 2-vCPU control plane, so a few long execs (a clone, a verify, a turn
    # waiting to end) stalled every other call. Also the Docker client's connection pool size.
    # review(perf): 0 = auto (io_pool). Half the pool is for turns long-polling in their sandbox.
    io_threads: int = 0
    # --- overnight/sweep: a workspace snapshot archive is read whole into memory, so it is capped
    snapshot_max_mb: int = 512
    # --- overnight/sweep: database backups into the session store (backup.py, docs/gke.md). Unset = every 24 hours
    # when HANGAR_SESSION_STORE is a GCS bucket, else off; 0 = off. `hangar admin backup` works either way.
    backup_hours: float | None = None
    backup_keep: int = 14  # newest backups kept; older ones are deleted after each backup
    # How long `hangar serve` waits for open requests (streams, terminals) on shutdown before cutting them.
    # Long-polls answer at once (shutdown.py). Keep it under the pod's terminationGracePeriodSeconds (45).
    shutdown_grace_seconds: int = 15
    # --- observe: traces, metrics and the cost view (docs/observability.md). Traces are configured with the
    # standard OTEL_* variables and are off unless they name an exporter.
    metrics_enabled: bool = True  # GET /metrics (admin or `metrics` tokens); off: no prometheus_client import
    # OTLP/HTTP base URL the agents *inside sandboxes* export to (an OpenTelemetry Collector they can reach, e.g.
    # http://otel-collector.observability.svc:4318). Set: Claude Code and Codex send their own traces, metrics and
    # logs there, under hangar's trace. Unset: sandboxes only get TRACEPARENT. No headers or secrets are passed.
    otel_sandbox_endpoint: str | None = None

    @property
    def backup_interval_hours(self) -> float:
        if self.backup_hours is not None:
            return max(0.0, self.backup_hours)
        return 24.0 if (self.session_store or "").startswith("gs://") else 0.0

    @property
    def io_pool(self) -> int:
        """Worker threads for blocking I/O: HANGAR_IO_THREADS, else 32 per CPU between 64 and 128. Threads waiting
        on a socket are cheap (~50 KB RSS each, the GIL released), but every busy one competes with the event loop
        for the GIL: on 2 vCPUs, 128 threads made exec p95 under load 287 ms and resume p50 4.4 s, against 215 ms
        and 3.7 s with 64, which still gives 32 turns at once their ~0.3 s turn-end notice (scripts/bench/control.py,
        50 sessions and 50 jobs; see the review of PR #53)."""
        if self.io_threads > 0:
            return self.io_threads
        try:
            cpus = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            cpus = os.cpu_count() or 2
        return max(64, min(128, 32 * cpus))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hangar.db"

    @property
    def saved_env_dir(self) -> Path:
        """Environments created through the API/MCP/dashboard (on the data volume, so they survive redeploys)."""
        return self.data_dir / "envs"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    return s
