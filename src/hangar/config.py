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
    # --- scale: the share of an environment's `cpus` a sandbox pod requests; its limit stays `cpus`. 1 = Guaranteed
    # (each sandbox reserves every core it may use); 0.5 packs twice as many mostly-idle agents per node.
    k8s_cpu_request_fraction: float = Field(0.5, gt=0, le=1)

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
    # --- providers-e2b: one provider, three backends (docs/microvm.md). cloud: E2B's cloud or BYOC; selfhosted:
    # E2B's own stack (Embed) on your machines; cube: Tencent CubeSandbox's E2B-compatible CubeAPI.
    e2b_flavor: Literal["cloud", "selfhosted", "cube"] = "cloud"
    e2b_api_url: str | None = None  # the control API; default https://api.<domain>. Cube: http://<cubeapi>:3000
    # Where sandboxes' envd answers. Unset: the SDK's rule (https://sandbox.<domain> on E2B's own domains, else
    # https://49983-<id>.<domain>). A URL with {sandbox_id} (and {port}) is filled in per sandbox; without, every
    # sandbox is reached there and routed by the E2b-Sandbox-Id header (one host, or hangar's own /e2b/sandbox).
    e2b_sandbox_url: str | None = None
    # CubeSandbox returns no envd access token: its envd answers anyone who reaches it. Off: such sandboxes are
    # refused (and killed). On: use them, on a private network only (docs/microvm.md, "Security").
    e2b_allow_unauthenticated_envd: bool = False
    # hangar environment -> an E2B or Cube template built out of band (Cube's template pipeline, `e2b template
    # build`): the provider uses it instead of building the environment's image.
    e2b_environment_templates: dict[str, str] = Field(default_factory=dict)
    e2b_request_timeout_seconds: float = 60.0  # one control API call (create/connect restore in ~100 ms)
    e2b_template_build_timeout_seconds: int = 1800  # Cube template builds: boot, probe, snapshot
    # --- providers-e2b: sandboxes by tier. An environment's `tier:` (or a create's) picks the first of its
    # providers that is up; `fast` then falls back to HANGAR_DEFAULT_PROVIDER, `isolated` never falls back to
    # anything not listed (it is a security choice, not a speed one).
    tiers: dict[str, list[str]] = Field(default_factory=lambda: {"fast": ["e2b"], "isolated": ["kubernetes"]})
    tier_cooldown_seconds: int = 60  # a provider that was full or down is skipped for this long
    # --- heavy: the heavy tier (heavy.py, docs/environments.md#the-heavy-tier): Docker inside a VM-isolated sandbox,
    # and the large and xlarge profiles. Off until HANGAR_TIERS lists providers for it ({"heavy": ["kubernetes"]}).
    heavy_runtime_class: str = "kata-qemu-runtime-rs"  # Kubernetes: the VM runtime heavy pods run under
    heavy_node_selector: bool = True  # Kubernetes: pin them to deploy/terraform/gke's sandboxes-heavy pool
    heavy_warm_pools: bool = False  # a heavy environment's warm_pool is ignored unless this is on: heavy nodes cost
    # Live sandboxes one person may hold per profile at once (0: no cap); `large` counts xlarge ones too
    heavy_person_max: dict[str, int] = Field(default_factory=lambda: {"large": 3, "xlarge": 1})
    # Where an environment image gets dockerd, containerd, runc and the docker CLI (with compose and buildx) from
    heavy_docker_image: str = "docker:29.8.2-dind"
    # dockerd's registry-mirrors (Docker Hub images only), e.g. ["https://mirror.gcr.io"]. With HANGAR_HEAVY_MIRROR
    # on, hangar's own pull-through on the gateway comes first.
    heavy_registry_mirrors: list[str] = Field(default_factory=list)
    heavy_mirror: bool = False  # Docker Hub through hangar's read-only pull-through on the gateway (regmirror.py)
    # Its upstream: Docker Hub, or an Artifact Registry remote repository for Docker Hub (deploy/terraform/gke)
    heavy_mirror_upstream: str = "https://registry-1.docker.io"
    # Docker provider only: heavy sandboxes run privileged under runc, which is root on the host. For CI and
    # throwaway machines; a VM runtime is the boundary everywhere else.
    heavy_docker_privileged: bool = False

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
    max_sandboxes: int = 120  # --- scale: 6 people × 10 sessions, plus job slots, forks and best-of-N attempts
    reaper_interval_seconds: int = 60
    # --- reconciler: how often rows a restart left unfinished, and dead or replaced sandboxes, are looked for
    reconcile_interval_seconds: int = 30
    sandbox_user: str = "agent"
    workspace_dir: str = "/workspace"

    # --- jobs (spec 0009) ---------------------------------------------------
    # Jobs provisioning/running at once; the rest wait in the queue. --- scale: 0 = sized from the fleet: 4 on Docker,
    # else max_sandboxes / 5 between 4 and 8 (fairness.job_slots; --- review: 8, since job budgets are opt-in).
    job_concurrency: int = 0
    job_poll_seconds: float = 5.0
    job_timeout_minutes: int = 60  # per agent turn
    job_keep_minutes: int = 120  # sandbox kept after a turn for follow-ups and take-over
    job_verify_timeout_seconds: int = 1800
    job_max_context_bytes: int = 2_000_000
    job_max_context_items: int = 50
    # Hosts allowed for plain-http callbacks or private-address callbacks (https to public hosts always allowed).
    job_callback_hosts: list[str] = Field(default_factory=list)
    # --- reconciler: a job a restart interrupted (deploy, power-down) whose session has a checkpoint resumes on its
    # own this many times, instead of failing as `interrupted`; such jobs re-enter the queue this many seconds apart,
    # so a power-up doesn't start them all at once on a cold cluster
    job_restart_resumes: int = 2
    job_restart_stagger_seconds: float = 20.0

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
    # --- providers-e2b: on a provider with memory snapshots (hot_resume: E2B, CubeSandbox), a suspended session's
    # sandbox is paused, not destroyed, for this long, and resumes from its memory; 0 = always restore checkpoints.
    session_hot_resume_minutes: int = 1440
    session_hot_resume_max: int = 20  # sandboxes kept paused for hot resume at once; the longest-parked go first
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
    # --- landing: a `github_app` credential. Set, agent PRs are opened, commented on and landed as the App (tokens
    # minted per repo with contents and pull_requests write only), and it pushes for gateway sandboxes that have no
    # git credential of their own. The token above still comments on issues (docs/landing.md).
    github_app_credential: str | None = None
    # Vault credential holding the webhook secret; the webhook route is off until this is set.
    github_webhook_secret_credential: str | None = None
    # Repos that may trigger jobs, mapped to the environment to run them in: {"owner/repo": "env"}.
    github_repos: dict[str, str] = Field(default_factory=dict)
    github_trigger_label: str = "hangar"
    # --- reconciler: at startup, ask GitHub to redeliver the webhook deliveries that failed since the last start
    # (the cluster was down, or hangar died mid-delivery); GitHub keeps them for 3 days and never retries on its own
    github_redeliver_on_start: bool = True

    # --- workstream C: events/usage/budgets ----------------------------------
    # USD per million tokens, merged over the built-in table in events.py (dated list prices):
    # {"my-model": {"input": 1.0, "output": 4.0, "cache_read": 0.1, "cache_write": 1.25}}. Keys match model ids
    # exactly or as a prefix followed by a date/variant suffix.
    model_prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    # --- canary: model policies (`model: frontier`, modelpolicy.py). Vault credentials holding the API keys that list
    # each provider's models: {"anthropic": "<cred>", "openai": "<cred>", "google": "<cred>"}. OpenRouter's list is
    # public. Without one, that provider's policies resolve to the last id resolved, else a built-in one.
    model_list_credentials: dict[str, str] = Field(default_factory=dict)
    model_list_ttl_minutes: int = 360  # how long a fetched list is used before it is fetched again
    # Default budget for each agent turn of jobs and sessions that don't set one. Unset = no limit.
    budget_max_minutes: int | None = None
    budget_max_tokens: int | None = None
    budget_max_cost_usd: float | None = None
    # --- frontdoor: default caps for a whole job, all its turns together (jobbudget.py), when neither the job nor
    # its repo's policy sets one. Unset = no limit.
    job_budget_max_cost_usd: float | None = None
    job_budget_max_tokens: int | None = None
    job_budget_max_turns: int | None = None
    # --- frontdoor: where a day starts for daily spend caps (an IANA zone, e.g. America/New_York). UTC midnight is
    # 20:00 in New York, mid working day, so a team elsewhere sets its own.
    budget_day_timezone: str = "UTC"
    # --- integration: what one person's jobs may spend in a day, their interactive sessions included (identity's
    # person; jobs nobody in particular asked for, e.g. GitHub- and Slack-triggered, count only against their
    # repo's cap). A new job over it waits for approval; follow-ups are refused. Unset or 0 = no limit.
    person_max_usd_per_day: float | None = None
    events_max_mb: int = 20  # per-session event log size before it rotates (one rotated file is kept)
    # --- workstream A: triggers ----------------------------------------------
    # GitHub logins and "@org/team" entries whose /hangar comments and labels start jobs right away. Unset: the
    # OWNER/MEMBER/COLLABORATOR author associations count as trusted. Anyone else's trigger waits for approval.
    github_trusted: list[str] = Field(default_factory=list)
    github_team_cache_seconds: int = 600
    job_approval_ttl_hours: float = 72  # an unapproved job is cancelled after this long
    # --- approvals (#121): nobody approves their own job. `allow_solo` permits it while exactly one person exists
    # (a single-person install, legacy tokens only), audited `self_approved: true` (approvals.py)
    self_approval: Literal["deny", "allow_solo"] = "deny"
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
    # --- computer: agents' own computer and self-diagnosis (docs/computer-use.md) ----------
    env_doctor_mcp: bool = True  # the `hangar-env` MCP server (the doctor, requests) in every harness's config
    env_request_poll_seconds: float = 30.0  # how often a running turn is checked for new requests (0: at its end)
    # Screenshots and demos in PR bodies. "off" (default): listed by name, fetched through hangar by people who may see
    # the job. "signed": embedded through expiring links anyone holding them can open (public repos only, say).
    pr_media_links: Literal["signed", "off"] = "off"
    pr_media_link_days: int = 7
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
    # Sessions use the gateway when it is configured, unless their environment says credential_mode: direct.
    # --- least-privilege: jobs, repo builds and env-setup validation always do, fetch-only (delivery.py), whatever
    # this or credential_mode say, wherever the gateway serves the repo's host.
    gateway_for_jobs: bool = True
    # Upstream git hosts the gateway proxies to: {host: base URL}. Only these hosts get a grant.
    gateway_upstreams: dict[str, str] = Field(default_factory=lambda: {"github.com": "https://github.com"})
    # Refs every grant may push besides the sandbox's own branch. "/**" matches anything below.
    gateway_push_refs: list[str] = Field(default_factory=lambda: ["refs/heads/hangar/**"])
    gateway_allow_tags: bool = False
    gateway_credential: str | None = None  # upstream credential when the sandbox has no git credential of its own
    gateway_max_push_mb: int = 512
    gateway_timeout_seconds: float = 120.0  # upstream connect/read timeout (per read, not per request)
    # --- egress2: the gateway holds a push and scans it for secrets before forwarding it (pushscan.py, with
    # HANGAR_SECRET_SCAN). A bigger push is refused in block mode, let through unscanned (audited) in warn mode.
    gateway_scan_max_mb: int = Field(64, ge=1, le=1024)
    # --- egress: hostname allowlists (egress.py, docs/security.md#egress) ----
    # The mode of environments that don't set `egress.mode`: open (the public internet, as before), allowlist or none.
    egress_default: Literal["open", "allowlist", "none"] = "open"
    # The egress proxy's port. On GKE it listens on HANGAR_EGRESS_HOST (default HANGAR_GATEWAY_HOST, then HANGAR_HOST)
    # when HANGAR_EGRESS_URL is set; on Docker on the egress network's gateway address, when a sandbox needs it.
    egress_port: int = 3128
    egress_host: str | None = None
    # URL Kubernetes sandboxes reach the proxy at, e.g. http://hangar-gateway.hangar.svc:3128 (deploy/k8s/gateway.yaml).
    egress_url: str | None = None
    # Hosts the proxy may connect to at private addresses (an internal package mirror). Loopback and link-local
    # addresses (the metadata servers) are refused regardless.
    egress_private_hosts: list[str] = Field(default_factory=list)
    egress_max_connections_per_sandbox: int = 128  # one person's 10 sandboxes stay well under MAX_CONNECTIONS
    # --- egress2: all of one person's sandboxes together, so 6 people at their caps fit MAX_CONNECTIONS (4096)
    egress_max_connections_per_person: int = 640
    # --- egress2: on shutdown the proxy takes no new connection, and open tunnels get this long to finish
    egress_drain_seconds: int = Field(20, ge=0, le=300)
    # --- egress2: read-only package mirrors at /pkg/<pypi|npm|go>/ on the git gateway's port (pkgmirror.py). With
    # it on (and HANGAR_GATEWAY_URL set) the pypi, npm and go presets point proxied sandboxes there, and the public
    # registries' hosts, which take uploads too, stay closed. Upstreams default to the public registries; on GKE
    # point them at Artifact Registry remote repositories: {"pypi": "https://us-python.pkg.dev/PROJECT/REPO", ...}.
    package_mirror: bool = True
    package_mirrors: dict[str, str] = Field(default_factory=dict)
    package_mirror_cache_mb: int = Field(1024, ge=0)  # immutable files kept on the data volume; 0: no cache
    # --- caches: dependency and compile caches across sandboxes (buildcache.py, docs/caches.md). Served at /cache on
    # the git gateway's port, so they need HANGAR_GATEWAY_URL; a sandbox reads its own repo's with its grant.
    build_caches: bool = True
    # Where cached objects live (encrypted with the vault key): gs://bucket[/prefix] or a directory. Unset: the session
    # bucket's caches/ prefix when HANGAR_SESSION_STORE is a bucket, else <data_dir>/caches.
    build_cache_store: str | None = None
    # All repos together, least recently used entries first (private layers' before the shared ones). Unset: 100 GB
    # in a bucket, 10 GB on the data volume, which holds the database too.
    build_cache_max_gb: float | None = Field(None, ge=0)
    build_cache_repo_max_gb: float = Field(20.0, ge=0)  # per repo
    build_cache_retention_days: float = Field(14.0, gt=0)  # an entry unused this long goes (nightly)
    build_cache_private_days: float = Field(3.0, gt=0)  # ...and one in a session's private layer, this long
    # Sessions and jobs read the repo's shared caches (written only by its repo builds) and, with this on, write to a
    # private layer of their own session and branch that nothing else reads. Off: they only read.
    build_cache_private_writes: bool = True
    build_cache_object_max_mb: int = Field(4096, ge=1)  # one cached object (a dependency archive, a compile output)
    build_cache_transfer_mb: int = Field(256, ge=16)  # cache bytes held in the control plane's memory at once
    build_cache_wait_minutes: float = Field(10.0, ge=0, le=60)  # a repo build waits this long for its restore
    # --- egress: secret scanning before hangar pushes a job's branch (secretscan.py). block: a hit stops the push
    # and goes back to the agent (at most secret_scan_rounds fix turns); warn: job events only; off.
    secret_scan: Literal["block", "warn", "off"] = "block"
    secret_scan_rounds: int = 2
    # --- egress: the OIDC issuer URL in the tokens hangar mints for sandboxes' cloud credentials (cloudcreds.py);
    # default HANGAR_PUBLIC_URL. Must be https; AWS fetches <issuer>/.well-known/openid-configuration from it.
    oidc_issuer: str | None = None
    # --- egress2: a new signing key every this many days (0: only `POST /api/cloud/rotate`). It is published a week
    # (at most half the period) before it signs; where the JWKS is uploaded (GCP --jwk-json-path), upload it again
    # after the `cloud.key.next` audit entry.
    oidc_key_rotation_days: int = Field(0, ge=0, le=3650)
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
    # Long MCP tools (create/fork a sandbox, build an environment) answer within this many seconds. Past it they
    # return the sandbox or build as it stands ("starting"/"running") and keep going in the background: MCP clients
    # give up on a call after a few minutes, and a client that disconnects must not cancel the work.
    mcp_tool_wait_seconds: float = 150
    # --- workstream steer: live turns (docs/steering.md) ----------------------
    # Run Claude Code and Codex turns under hangar's driver, so they can be steered, interrupted and asked for tool
    # approvals. Off (or a sandbox without python3): one-shot turns, as before.
    live_turns: bool = True
    # How tool permission prompts are answered when neither the session nor its environment says: auto (no
    # prompts), ask (a person answers; denied after approval_timeout_seconds) or deny-risky (hangar's rules).
    approvals: Literal["auto", "ask", "deny-risky"] = "auto"
    approval_timeout_seconds: int = Field(600, ge=10, le=86400)
    # --- isolation: run each turn's driver as hangar's own user and the agent as the sandbox user, so code the agent
    # runs can't reach the driver, its key or its control files (docs/steering.md). auto: wherever the sandbox allows
    # it (Docker, an agent without sudo), else one user, with a warning; off: always one user; required: like auto,
    # and `ask` and `deny-risky` turns fail where the agent would share the driver's user, or where hangar can't own
    # the harness's settings (--- harden).
    uid_split: Literal["auto", "off", "required"] = "auto"
    # --- overnight/cache: prompt caching (cache.py, docs/sessions.md#prompt-caching). The TTL turns ask for when
    # neither the session nor its environment says: auto (1h when the session waits on people for more than
    # 5 min, else 5m), 5m, 1h or off. And how long best-of-N attempts and forks started together hold back for the
    # first one to answer, so they read the prompt prefix it cached instead of each writing it (0 = no wait).
    cache: Literal["auto", "5m", "1h", "off"] = "auto"
    cache_stagger_seconds: float = Field(15.0, ge=0, le=120)
    # --- overnight/autofix: an agent loop that finishes the job (docs/jobs.md §7) ---------------------------
    # A job's `verify` runs inside the agent's turn too (a Claude Code Stop hook; the driver for Codex; a follow-up
    # turn for the others), and a failure goes back to the agent, up to this many times per turn. Jobs can say
    # otherwise (`verify_in_loop`, `max_verify_rounds`); hangar's own check after the turn still decides delivery.
    verify_in_loop: bool = True
    max_verify_rounds: int = Field(3, ge=0, le=10)
    # --- testresults: structured results of every check (testresults.py, docs/jobs.md#test-results). inject: set the
    # reporter variables (PYTEST_ADDOPTS --junitxml, GOTESTSUM_JUNITFILE, JEST_JUNIT_OUTPUT_DIR) and read the reports
    # the check writes; collect: only read the reports (the repo's `verify_reports`, the usual paths); off: neither.
    test_reports: Literal["inject", "collect", "off"] = "inject"
    # The PR auto-fix loop: a failing CI check on the job's own head, or a trusted reviewer's changes requested or
    # inline comments, start a follow-up turn on the job's session. Per repo: {"owner/repo": "ci"} ("ci,reviews",
    # "ci", "reviews" or "off"); a job's own `autofix` can only narrow it. Default "ci,reviews".
    github_autofix: dict[str, str] = Field(default_factory=dict)
    autofix_max_rounds: int = Field(3, ge=0, le=20)  # per PR
    autofix_cooldown_seconds: float = 120  # at least this long between two rounds on one PR
    autofix_coalesce_seconds: float = 60  # failures and comments within this window become one turn
    autofix_log_bytes: int = Field(16_000, ge=1000, le=200_000)  # of each failing check's log, its end
    # --- perf: control-plane responsiveness -----------------------------------
    # Worker threads for blocking provider calls (docker-py, the kubernetes client, e2b) and file I/O. Python's
    # default is min(32, CPUs + 4): 6 on a 2-vCPU control plane, so a few long execs (a clone, a verify, a turn
    # waiting to end) stalled every other call. Also the Docker client's connection pool size.
    # review(perf): 0 = auto (io_pool). Half the pool is for turns long-polling in their sandbox.
    io_threads: int = 0
    # --- overnight/sweep: a workspace snapshot archive is read whole into memory, so it is capped
    snapshot_max_mb: int = 512
    # --- scale: B12. Workspace archives the control plane holds in memory at once, in MB (each counted at up to 3×
    # its size while in flight): snapshots, forks and restores past it wait their turn instead of exhausting memory.
    workspace_transfer_mb: int = Field(1024, ge=64)
    # --- overnight/sweep: database backups into the session store (backup.py, docs/gke.md). Unset = every 24 hours
    # when HANGAR_SESSION_STORE is a GCS bucket, else off; 0 = off. `hangar admin backup` works either way.
    backup_hours: float | None = None
    backup_keep: int = 14  # newest backups kept; older ones are deleted after each backup
    # --- durable: where backups go when not next to the checkpoints (gs://bucket[/prefix] or a path). Best a bucket
    # where hangar's service account may only create objects, with a lifecycle rule doing the keeping
    # (docs/security.md#backups-and-the-vault-key). Unset: the session store's `_backups/`.
    backup_store: str | None = None
    # How long `hangar serve` waits for open requests (streams, terminals) on shutdown before cutting them.
    # Long-polls answer at once (shutdown.py). Keep it under the pod's terminationGracePeriodSeconds (45).
    shutdown_grace_seconds: int = 15
    # --- scale: one control plane for a team (docs/team-scale.md). Caps per person: identity's person (people.py),
    # else a token's or an approved OAuth connector's name (fairness.person_of); hangar's own actors are exempt.
    # 0 = off.
    person_max_sessions: int = 15  # live sessions (each holds a sandbox); suspended ones don't count
    person_max_sandboxes: int = 20  # sandboxes started directly (MCP, REST, the E2B API); sessions' are above
    person_max_running_jobs: int = 0  # jobs running at once; 0 = half the job slots, at least 4. Others' jobs go first
    # --- teamcreds: how long a job's or session's sandbox waits for a free login when every login it could use is
    # leased (a Codex ChatGPT login goes into one sandbox at a time); then it fails, saying which are busy
    login_wait_seconds: float = Field(600, ge=0)
    # Stopped and failed sandbox records older than this move to an archive table (retention.py): out of listings
    # and of every scan, still found by id. Archived ones are deleted after sandbox_archive_days (0 = kept).
    sandbox_retention_days: float = 7
    sandbox_archive_days: float = 180
    # --- durable: jobs and sessions held in memory are the live ones, and those saved or read in the last
    # resident_minutes (resident.py); the rest are read from the store when asked for. Finished jobs and sessions
    # untouched for *_retention_days move to the archive (still found by id and in listings; never deleted), except
    # what is still referenced: open PRs, sessions with checkpoints, a schedule's last job. 0 = not archived.
    resident_minutes: float = Field(60, ge=1)
    job_retention_days: float = Field(30, ge=0)
    session_retention_days: float = Field(30, ge=0)
    # --- observe: traces, metrics and the cost view (docs/observability.md). Traces are configured with the
    # standard OTEL_* variables and are off unless they name an exporter.
    metrics_enabled: bool = True  # GET /metrics (admin or `metrics` tokens); off: no prometheus_client import
    # OTLP/HTTP base URL the agents *inside sandboxes* export to (an OpenTelemetry Collector they can reach, e.g.
    # http://otel-collector.observability.svc:4318). Set: Claude Code and Codex send their own traces, metrics and
    # logs there, under hangar's trace. Unset: sandboxes only get TRACEPARENT. No headers or secrets are passed.
    otel_sandbox_endpoint: str | None = None
    # --- stream: output, events and file changes streamed to MCP and REST clients (docs/streaming.md). Caps are per
    # token: open streams (exec, attach, event and watch streams), background processes and file watches; and per
    # stream: bytes per second (a faster command waits, like a slow terminal) and bytes in all (then output is only
    # kept for the result's tail).
    stream_max_per_token: int = Field(16, ge=1, le=1000)
    stream_max_total: int = Field(512, ge=1, le=100_000)
    stream_bytes_per_second: int = Field(1 << 20, ge=1024)
    stream_max_bytes: int = Field(64 << 20, ge=1 << 16)
    stream_max_hours: float = Field(12.0, gt=0)  # an event or watch stream left open longer is closed
    stream_recheck_seconds: float = Field(5.0, gt=0)  # how often open streams check their token is still valid
    background_max_per_token: int = Field(16, ge=0, le=1000)
    watch_max_per_token: int = Field(8, ge=0, le=64)
    # Follow a running turn's log through hangar-envd (`tail -F`), so its events arrive as the agent writes them
    # instead of on the next poll (session_poll_seconds). Off, or without hangar-envd: polling, as before.
    stream_turn_logs: bool = True
    # --- intake: tracker intake (docs/intake.md). Jira: {"url": "https://acme.atlassian.net", "email": ...,
    # "token_credential": ..., "webhook_secret_credential": ..., "projects": {"ABC": {"repo": "owner/repo"}},
    # "trigger": {"status": "Ready for agent"}, "statuses": {"started": "In Progress", ...}} (jira.JiraConfig).
    jira: dict[str, Any] = Field(default_factory=dict)
    # GitHub Projects boards, by "org/number": {"acme/7": {"trigger": "Ready for agent", "repos": ["acme/api"],
    # "statuses": {"started": "In progress", "delivered": "In review"}}} (ghprojects.Board).
    github_projects: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # --- envbuilds: environments that live with the repo (docs/environments.md). Off: a repo's
    # .hangar/environment.yaml is ignored and sandboxes start from the environment's image, as before.
    env_builds: bool = True
    env_builds_keep: int = Field(5, ge=1, le=50)  # builds kept per repo, environment and provider (the active one too)
    env_build_timeout_minutes: int = Field(30, ge=1, le=240)  # clone, install and capture, each build
    env_builds_concurrency: int = Field(2, ge=1, le=16)  # builds at once (each is a sandbox, then an image build)
    # Images a service may run: patterns where `*` matches within one path segment and `**` across them. Default:
    # Docker's official images (`postgres:17`, `redis`), directly or through mirror.gcr.io. Add your registry's, e.g.
    # "us-docker.pkg.dev/acme/services/**".
    env_service_images: list[str] = Field(default_factory=lambda: [
        "*", "docker.io/library/*", "mirror.gcr.io/library/*"])
    # --- envbuilds review: what a sandbox's services may add up to, on top of its own resources (each service's
    # cpus and memory_gb are the repo's to write; memory is requested in full on Kubernetes: docs/team-scale.md).
    env_services_max_cpus: float = Field(2.0, ge=0, le=32)
    env_services_max_memory_gb: float = Field(2.0, ge=0, le=64)
    # --- autoinstall: agent-led environment setup (`hangar env setup`, autoinstall.py, docs/environments.md).
    env_setup_retries: int = Field(5, ge=0, le=10)  # setup turns after a failed validation, each with its output
    env_setup_max_cost_usd: float = Field(30.0, gt=0)  # the job budget when the job and the repo's policy set none
    env_setup_max_turns: int = Field(16, ge=2, le=100)
    env_setup_validate_minutes: int = Field(45, ge=5, le=240)  # one validation: sandbox, install, every command
    env_setup_models: dict[str, str] = Field(default_factory=dict)  # harness -> model or policy, instead of balanced
    env_setup_label: str = "hangar-env-setup"  # an issue (GitHub, Jira) with this label starts an env setup
    # Preview ports (preview.py): `<port>-<sandbox>.<this domain>` reaches a sandbox's declared port, for its owner,
    # through hangar. Needs wildcard DNS pointing at hangar; `localhost` on a laptop. Unset: previews are off.
    preview_domain: str | None = None
    preview_session_hours: float = Field(12, gt=0, le=168)  # how long an opened preview stays signed in
    # --- ops: the sandbox node pools power down at night while hangar stays up (power.py, docs/gke.md). Providers
    # whose sandboxes run on those pools (add e2b when it runs on the microVM pool): while they are off, creates on
    # them answer at once with when they're back, and jobs wait queued.
    power_providers: list[str] = Field(default_factory=lambda: ["kubernetes"])
    power_up_job: str | None = None  # `hangar power up` runs this Cloud Run job (terraform output power_up_job)
    # Warm pools for the day at power-up: `auto` (from last week's creates in that hour), `off`, or `env=N,env2=M`
    power_warm_pools: str = "auto"
    power_warm_max: int = Field(3, ge=0, le=50)
    power_prepull_image: str = "registry.k8s.io/pause:3.10.1"  # the prepull DaemonSet's only container
    # --- clouddoctor: the nightly pass (opstatus.py): credential checks, then the Cloud Doctor (docs/doctor.md). A
    # cron in doctor_time_zone (unset: the power schedule's zone, else UTC); "off" turns it off. Half past midnight
    # in the power schedule's zone is before the sandbox pools power down at 01:00.
    doctor_schedule: str = "30 0 * * *"
    doctor_time_zone: str | None = None
    # The Cloud Doctor's jobs (clouddoctor.py): harness and model (unset: Claude Code where the environment has it,
    # else the environment's harness; the `balanced` policy for every harness, `frontier` to ask for more), its
    # budget per run (all its jobs together) and per job, how many root-cause jobs a run
    # starts, how many separate runs make a failure worth fixing, how far back it counts, and how often a transient
    # failure may recur before it is reported anyway.
    doctor_harness: str | None = None
    doctor_model: str | None = None
    doctor_max_usd: float = Field(10.0, ge=0)
    doctor_job_max_usd: float = Field(3.0, gt=0)
    doctor_max_jobs: int = Field(3, ge=0, le=20)
    doctor_min_runs: int = Field(2, ge=1)
    doctor_lookback_days: int = Field(7, ge=1, le=14)
    doctor_transient_alarm: int = Field(5, ge=1)
    # Where environment specs live as files, for fixes to them: a repo hangar's jobs can open PRs on, and the spec
    # files in it (fnmatch, `*` crosses directories). Unset: spec fixes are reported, not opened.
    doctor_specs_repo: str | None = None
    doctor_specs_paths: list[str] = Field(default_factory=lambda: ["envs/*.yaml", "envs/*.yml"])
    # --- interfaces: the sister tool's interfaces (docs/specs/0014-sister-tool-interfaces.md). With tracing on, each
    # turn's tool calls, subagents and approvals also go to the collector as children of its invoke_agent span.
    trace_live_tools: bool = False
    experiment_max_trials: int = Field(200, ge=1, le=2000)  # task x variant x trial in one experiment
    experiment_parallel: int = Field(8, ge=1, le=64)  # trials running at once, when the experiment doesn't say

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
