# Changelog

## The overnight stack (#48–#67)

Nineteen feature PRs (#48–#66) on top of `2e3e0a3` (main after #43), and the upgrade guide and acceptance script (#67). Upgrading a running hangar: [docs/upgrading.md](docs/upgrading.md), which lists every default that changes. Proving it on GKE: `bash scripts/acceptance/gke.sh`.

### Agents and sessions
- **Live turns: steer, interrupt and tool approvals** for Claude Code and Codex. A small driver runs the turn inside the sandbox. Approvals (`auto`, `ask` or `deny-risky`) reach MCP clients as elicitations, and are also available over REST and the CLI. [docs/steering.md](docs/steering.md). #59
- **The uid split.** On Docker, a turn's driver runs as hangar's own user and the agent as the sandbox user, so code the agent runs can't reach the driver, its key or its control files. GKE and E2B keep one user (`HANGAR_UID_SPLIT`). #66
- **Verify inside the turn.** A job's `verify` runs before the agent stops: a Claude Code Stop hook, the driver for Codex, a follow-up turn for the others. Edited test files make the PR a draft. [docs/jobs.md §7](docs/jobs.md). #65
- **PR auto-fix.** Failing CI and trusted reviewers' comments on hangar's own PRs start follow-up turns, with round limits and a cooldown (`HANGAR_GITHUB_AUTOFIX`). The webhook needs new events. #65
- **Prompt caching.** A TTL policy per session or environment (`auto`, `5m`, `1h`, `off`), one model per session (a switch is warned about), best-of-N and fork siblings sharing the first one's cached prefix, and cache savings in usage. #63
- **Faster checkpoints and resume** (about 2x), with unchanged parts copied inside the store. A turn's end is noticed in about 0.2 s, and there are benchmarks under `scripts/bench/`. #53
- **Durable sessions.** Forks copy checkpoints inside the store instead of through memory. A checkpoint survives an agent that deleted its checkout. The usage of deleted sessions is kept. #57
- **The seams between features.** 14 bugs fixed where best-of-N, budgets, `needs_input`, forks, triggers, Slack, A2A and the gateway meet. Best-of-N attempts and the judge are budgeted, and each job has one event stream. #49

### Sandboxes and providers
- **hangar-envd**, a Rust daemon in each Docker and Kubernetes sandbox. It speaks E2B's envd protocol and carries exec, files and terminals without a `docker exec` or pods/exec per call, falling back to those when it can't be reached. The control-plane image builds it. [docs/envd.md](docs/envd.md). #60
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
- **Upgrade guide and live acceptance script**: [docs/upgrading.md](docs/upgrading.md) and `scripts/acceptance/gke.sh`, tested against Docker. `envd`, `envd_dir` and `metrics_enabled` are now flagged as needing a restart when stored with `hangar config set`. #67

### MCP
- **Tasks** (`io.modelcontextprotocol/tasks`) for long tools; **elicitation** for agents' questions and tool approvals; typed results; live resources with subscriptions; and prompts. Pre-2026 clients get stateful sessions (`HANGAR_MCP_STATELESS_HTTP` turns them off). [docs/mcp.md](docs/mcp.md). #52

### Specs
- **0012, Cutting edge: what to adopt next.** A comparison with cloud agents, sandbox platforms and protocols, ending in 15 prioritised items. #54
- **0013, E2B and CubeSandbox as references.** The envd wire protocol, the E2B control API, and where a microVM tier fits on GCP. #58
