# Changelog

## Unreleased

- **Control-plane credentials never reach a sandbox** (hardening B4). An `mcp` token could map the GitHub webhook secret or the Slack bot token into a sandbox through an environment's `secrets:` and read it back, and then forge trusted webhooks. Credentials now have an exposure: webhook signing keys, GitHub App keys, the Slack secrets and the GitHub tokens hangar's settings name are control-plane only, refused by `secrets:`, attached env vars and files alike. `hangar cred exposure <name> sandbox` shares the control plane's GitHub token with direct-mode sandboxes on purpose. **Upgrade note:** a direct-mode environment that attaches the same GitHub token as `HANGAR_GITHUB_TOKEN_CREDENTIAL` or `HANGAR_GATEWAY_CREDENTIAL` now fails to start, with that command in the message. Run `hangar doctor` after upgrading: its `control-plane credentials` check names every such environment before a job fails on it.
- **Scopes on capabilities.** The REST and MCP auth paths record the caller's token and scopes (`identity.current`), and MCP tools can require a scope (`@tool(scope="admin")`). Saving an environment that sets `secrets`, `image`, `base`, `runtime_class` or `credential_mode: direct` needs an admin token; an `mcp` token gets a 403-style refusal. OAuth grants act as the token that approved them.
- **Long MCP tools no longer die with the client.** `create_sandbox`, `create_sandboxes`, `fork_sandbox` and `build_environment` answer within `HANGAR_MCP_TOOL_WAIT_SECONDS` (150 s). Past that they return the sandbox or build as it stands (`starting`, with a `stage` saying what it waits on) and keep going in the background. Before, an MCP client that gave up on the call (claude.ai stops after about 230 s) cancelled the create, and the sandbox ended in `error`.
- **No create waits on another harness's pack, on any path.** #75 covered creates whose image already existed. Now the image build a create triggers (first use, or an environment change) also builds just that harness's pack, side by side with the image. Scheduled warm-pool syncs never wait on a build. At startup, packs whose harness changed in the upgrade are rebuilt in the background. A create that does wait on its own pack says so in `stage`, with the time so far.
- **A create waits only on its own harness's pack.** Packs for the environment's other harnesses are mounted when they're already built. A stale one, after an upgrade changed that harness, is rebuilt in the background instead of holding up the sandbox. This is why the first `python` sandbox after the #69 upgrade took minutes: it rebuilt the Codex pack for a Claude Code sandbox.

## The overnight stack (#48–#69)

Twenty-one feature PRs (#48–#66, #68, #69) on top of `2e3e0a3` (main after #43), and the upgrade guide and acceptance script (#67). Upgrading a running hangar: [docs/upgrading.md](docs/upgrading.md), which lists every default that changes. Proving it on GKE: `bash scripts/acceptance/gke.sh`.

### Agents and sessions
- **Live turns: steer, interrupt and tool approvals** for Claude Code and Codex. A small driver runs the turn inside the sandbox. Approvals (`auto`, `ask` or `deny-risky`) reach MCP clients as elicitations, and are also available over REST and the CLI. [docs/steering.md](docs/steering.md). #59
- **The uid split.** On Docker, a turn's driver runs as hangar's own user and the agent as the sandbox user, so code the agent runs can't reach the driver, its key or its control files. GKE and E2B keep one user (`HANGAR_UID_SPLIT`). #66
- **Hangar-owned harness settings.** In a split sandbox, an `ask` or `deny-risky` turn starts its harness from the pack or bake, with hangar's copy of the environment and settings hangar writes, so the agent can't set up a later turn that never asks. The repository's `.claude` settings, `.mcp.json` and skills don't apply there; the environment's settings, MCP servers and instructions and the repository's CLAUDE.md do. Every split harness runs with `no_new_privs`. Hangar reads a split run's status, log and approvals from its own files. `HANGAR_UID_SPLIT=required` also covers `deny-risky`. Codex's pack changes, so republish it. [docs/steering.md](docs/steering.md#hangar-owned-harness-settings). #69
- **Verify inside the turn.** A job's `verify` runs before the agent stops: a Claude Code Stop hook, the driver for Codex, a follow-up turn for the others. Edited test files make the PR a draft. [docs/jobs.md §7](docs/jobs.md). #65
- **PR auto-fix.** Failing CI and trusted reviewers' comments on hangar's own PRs start follow-up turns, with round limits and a cooldown (`HANGAR_GITHUB_AUTOFIX`). The webhook needs new events. #65
- **Prompt caching.** A TTL policy per session or environment (`auto`, `5m`, `1h`, `off`), one model per session (a switch is warned about), best-of-N and fork siblings sharing the first one's cached prefix, and cache savings in usage. #63
- **Faster checkpoints and resume** (about 2x), with unchanged parts copied inside the store. A turn's end is noticed in about 0.2 s, and there are benchmarks under `scripts/bench/`. #53
- **Durable sessions.** Forks copy checkpoints inside the store instead of through memory. A checkpoint survives an agent that deleted its checkout. The usage of deleted sessions is kept. #57
- **Live turn events.** A running turn's log is followed through hangar-envd, so events reach readers in tens of milliseconds on Docker instead of up to a 5 s poll later. Session and job events stream over SSE (`Last-Event-ID` resumes) and MCP subscriptions. [docs/streaming.md](docs/streaming.md). #68
- **The seams between features.** 14 bugs fixed where best-of-N, budgets, `needs_input`, forks, triggers, Slack, A2A and the gateway meet. Best-of-N attempts and the judge are budgeted, and each job has one event stream. #49

### Sandboxes and providers
- **hangar-envd**, a Rust daemon in each Docker and Kubernetes sandbox. It speaks E2B's envd protocol and carries exec, files and terminals without a `docker exec` or pods/exec per call, falling back to those when it can't be reached. The control-plane image builds it. [docs/envd.md](docs/envd.md). #60
- **Streaming to clients.** `exec(stream=true)` sends output as MCP progress notifications, or as SSE over REST. A `process` tool runs background processes you can attach to, write to and kill, and directory watches arrive as resource updates. Every stream has caps per token and per stream, and ends on revocation, destroy, pause and shutdown. [docs/streaming.md](docs/streaming.md). #68
- **E2B-compatible API** at `/e2b`, with a sandbox router and a `sandboxes` token scope, so the stock `e2b` SDKs run on hangar sandboxes. [docs/e2b.md](docs/e2b.md). #61
- **A fast microVM tier.** The `e2b` provider drives E2B's cloud, self-hosted E2B or CubeSandbox (`HANGAR_E2B_FLAVOR`). Sandbox tiers (`fast`, `isolated`), hot resume from paused memory, and an optional nested-virtualization node pool in terraform. [docs/microvm.md](docs/microvm.md). #64
- **Isolation.** On Docker, sandboxes run on hangar's own network, where they can't reach each other, and the single-VM install blocks the metadata server. On GKE, NetworkPolicies give sandboxes an egress baseline and block ingress to them (`deploy/k8s/sandbox-network.yaml`). #48, #62

### Security and front doors
- **Front-door review.** Authorization gaps closed across GitHub, Slack, A2A, REST scopes, OAuth and MCP. Rate limits and caps per caller and per front door, replay protection for signed webhooks and Slack, refusal audits, Slack Connect members gated, and a stricter OAuth redirect policy with an RFC 8707 audience check. #50
- **Git gateway and session pull** hardened against a compromised sandbox: safe extraction of `session pull` archives, the `push-options` capability, reserved session branches, a deadline on the command list, and per-grant and per-source limits. #48
- **Every read of a sandbox-written file is capped.** Body caps follow the token's scope, replay entries are synced to disk, and Cursor's OAuth sign-in works by default. #56

### Setup and operations
- **`hangar up`** (local or `--gke`, with a plan shown before anything is applied), **`hangar connect`** for MCP clients (with `mcp`-scoped tokens), the **`hangar setup github|slack`** wizards, **settings stored in hangar's database** (`hangar config`), and **`hangar doctor --fix`**. #51
- **Database backups**: `hangar admin backup|backups|restore`, every 24 h into the session bucket by default. **Graceful shutdown**: long-polls answer, streams are cut after a grace period, and the pod's grace period is 45 s. #57
- **Observability**: OpenTelemetry traces (off unless `OTEL_*` is set), Prometheus `/metrics` with a `metrics` token scope, a cost view (`/api/usage`, `hangar usage`), and optional manifests for Managed Prometheus, the Prometheus Operator and the OpenTelemetry Collector. [docs/observability.md](docs/observability.md). #55
- **Upgrade guide and live acceptance script**: [docs/upgrading.md](docs/upgrading.md) and `scripts/acceptance/gke.sh`, tested against Docker. `envd`, `envd_dir` and `metrics_enabled` are now flagged as needing a restart when stored with `hangar config set`. The script also checks that a streamed exec arrives unbuffered through the load balancer. #67

### MCP
- **Tasks** (`io.modelcontextprotocol/tasks`) for long tools; **elicitation** for agents' questions and tool approvals; typed results; live resources with subscriptions; and prompts. Pre-2026 clients get stateful sessions (`HANGAR_MCP_STATELESS_HTTP` turns them off). [docs/mcp.md](docs/mcp.md). #52

### Specs
- **0012, Cutting edge: what to adopt next.** A comparison with cloud agents, sandbox platforms and protocols, ending in 15 prioritised items. #54
- **0013, E2B and CubeSandbox as references.** The envd wire protocol, the E2B control API, and where a microVM tier fits on GCP. #58
