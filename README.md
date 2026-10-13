# hangar

A control plane for fleets of cloneable coding-agent sandboxes, exposed over MCP.

Connect Codex, Claude Code, or any MCP client to hangar. Then spin up isolated sandboxes from environment templates that bundle your tools, agent harness, dependencies, skills, and repo. Hangar handles the logins, runs agents inside the sandboxes, forks a prepared sandbox into N parallel attempts, and tears everything down when you're done.

The same control plane runs on your laptop (Docker), on a single VM (Docker + gVisor or Kata microVMs), or on GKE (Agent Sandbox with gVisor or Kata). E2B is supported as a backend too.

```
 MCP client (Codex, Claude Code, …)        Browser
          │  streamable HTTP + bearer token   │  dashboard + live terminals
          ▼                                   ▼
 ┌──────────────────────── hangar control plane ────────────────────────┐
 │  MCP tools · REST API · websocket PTYs · audit log                    │
 │  Fleet manager ── environments (layered specs → images, warm pools)   │
 │                ── credential broker (encrypted vault, leases)         │
 │                ── snapshots / forks · TTL reaper                      │
 └───────┬─────────────────────────┬─────────────────────────┬──────────┘
         ▼                         ▼                         ▼
   Docker provider          Kubernetes provider          E2B provider
   laptop / single VM       Agent Sandbox CRDs           Firecracker VMs
   runc · gVisor · Kata     gVisor · Kata on GKE         cloud / BYOC / self-host
```

## What you get

- **Environments as layered specs.** Base image, then system packages and runtimes (via mise), then dependencies, then skills and agent config, then the repo. Layers are ordered for Docker cache reuse, and the image tag is content-addressed. Start from a default (`bare`, `python`, `node`, `go`, `polyglot`), or bring a prebuilt image.
- **Pick the agent per sandbox, not per image.** An environment lists the harnesses it allows (Claude Code, Codex, pi, Antigravity). Each harness ships once as a read-only *harness pack* that is mounted at start, so adding agents doesn't bloat images. `create_sandbox(harness=...)` applies only that agent's credentials, config and MCP servers. Providers that can't mount images get a per-harness image instead.
- **Fast, concurrent sandboxes.** Warm pools keep sandboxes pre-started. Claiming one, cloning the repo and checking out a fresh work branch took about 0.6s in testing, and four in parallel took about 1.2s.
- **Clone two ways.** Rebuild from the spec, or snapshot a live sandbox and fork it into up to 20 copies with the same files, installed deps and repo state.
- **Logins for Claude Code, Codex, pi and Antigravity.**
  - Both API keys and subscription logins are supported.
  - Subscription logins run inside a short-lived sandbox, and you finish them in the dashboard terminal.
  - Credentials are encrypted at rest and injected into `~/.hangar/env` or their usual files at claim time.
  - Codex's rotating refresh token is *leased* to one sandbox at a time and written back when that sandbox stops, so sandboxes never log each other out.
- **Terminals that agents and humans share.** MCP clients drive persistent tmux sessions (`terminal_send` / `terminal_read`). From the dashboard you can attach to the same session and watch, or type along.
- **Agents inside sandboxes.** `run_agent` starts Claude Code, Codex, pi or `agy` (Antigravity) headless on a task, with an optional `model`, so one orchestrator session can fan work out across the fleet.
- **Bearer tokens or OAuth.** `hangar connect <client>` writes the client's config with a token scoped to MCP. ChatGPT connectors and other OAuth-only clients use hangar's built-in OAuth 2.1 server: discovery, dynamic registration, PKCE, and a consent page where you approve with a hangar token. See [docs/clients.md](docs/clients.md).
- **A dashboard.** It covers the fleet, live terminals, the environment editor with streaming builds, credentials and sign-in, snapshots, MCP connect snippets with token management, and an audit log.

## Quickstart (laptop, 5 minutes)

Requires Docker (Docker Desktop, OrbStack or Colima on macOS) and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Sean-Koval/hangar && cd hangar
uv sync --all-extras
uv run hangar up
```

`hangar up` does five things:
1. checks that Docker is running;
2. starts the control plane in the background (log: `~/.hangar/server.log`);
3. logs the CLI in;
4. builds the `claude-python` environment;
5. offers to connect each MCP client it finds.

Running it again is safe: it reuses the running server, the saved login and the built images. `--env NAME` builds other environments, `--no-build` skips building, and `--connect codex` connects a client without asking. `hangar down` stops the server.

Then:

1. **Add credentials.** Use the dashboard at http://localhost:8787 (under **Credentials**), or:
   ```bash
   claude setup-token | uv run hangar cred add claude-max --harness claude-code --kind oauth_token
   uv run hangar cred add codex-chatgpt --harness codex --kind auth_file --file ~/.codex/auth.json
   uv run hangar cred add gh --harness git --kind git_token --value "$(gh auth token)"
   ```
2. **Connect an MCP client**, if `hangar up` didn't. Run `uv run hangar connect claude-code` (or `codex`, `cursor`, `vscode`, `gemini`, `claude-desktop`). It adds hangar to the client's own config file, next to your other servers, with a fresh token that can reach `/mcp` and nothing else. `hangar connect --list` shows what's connected. Details are in [docs/clients.md](docs/clients.md).
3. **Ask your client to do something.** For example: *"Create 3 claude-python sandboxes on github.com/me/api, each on its own branch, and run the agent in each on 'make the flaky test in tests/test_sync.py deterministic'. Report which attempt passes."*
4. **Optional: take work from GitHub.** `uv run hangar setup github` asks for a repo and a token and stores them. It then registers the repo's webhook and sends a test delivery. GitHub can't reach `localhost`, so on a laptop pass `--webhook-url` with a tunnel URL. See [docs/jobs.md](docs/jobs.md#4-trigger-from-github).

`uv run hangar doctor` checks the setup, and `hangar doctor --fix` applies the fixes it can make on its own.

## Production (GKE)

```bash
cp deploy/terraform/gke/terraform.tfvars.example deploy/terraform/gke/terraform.tfvars   # set project_id, region
uv run hangar up --gke --dry-run          # the terraform plan; nothing changes
uv run hangar up --gke                    # asks, then applies that plan, runs deploy/gke/up.sh and logs in
hangar doctor --fix                       # what's missing, and the fixes it can apply
hangar setup github                       # token, webhook secret, repo webhook, settings, test delivery
hangar connect claude-code                # and any other MCP client
```

`hangar up --gke` never applies a plan that destroys or replaces anything unless you pass `--allow-destroy`, and without a terminal it applies nothing unless you pass `--yes`. The steps it runs are the ones in [docs/gke.md](docs/gke.md), which you can also run by hand. `up.sh` points `HANGAR_SESSION_STORE` at the checkpoint bucket terraform made, on a first install. If the certificate isn't Active yet, `hangar up --gke` prints the `hangar login` command to run once it is.

Settings you change with `hangar setup` or `hangar config set` are stored in hangar's database, on its volume. They survive redeploys and need no `kubectl`. The full walkthrough is in [docs/gke.md](docs/gke.md).

## MCP tools

| Area | Tools |
| --- | --- |
| Discover | `fleet_overview`, `list_environments`, `describe_environment`, `create_environment` (`dry_run` validates; `extends` clones), `build_environment`, `list_credentials` |
| Lifecycle | `create_sandbox`, `create_sandboxes` (N in parallel), `list_sandboxes`, `get_sandbox`, `destroy_sandbox` (one, several or by label), `pause_sandbox`, `resume_sandbox`, `extend_sandbox` |
| Commands and files | `exec`, `read_file`, `write_file`, `list_files` |
| Terminals | `terminal_open`, `terminal_send` (supports `wait_for` regex), `terminal_read`, `terminal_list`, `terminal_close` |
| Agents | `run_agent`, `agent_status` |
| Clone | `snapshot_sandbox`, `list_snapshots`, `fork_sandbox` |
| Jobs and sessions | `submit_job`, `get_job`, `list_jobs`, `follow_up_job`, `cancel_job`, `new_session`, `send_to_session`, `fork_session`, `wait_for_turn`, `get_events`, `get_artifact`, `get_session_patch`, ... |

Every tool accepts a sandbox id or name. Relative paths resolve against the sandbox's repo checkout, falling back to `/workspace`. Tools carry read-only/destructive hints and typed results; long work can run as MCP tasks, agents' questions reach you as elicitations, and jobs and sessions are subscribable resources. See [Using hangar from an MCP client](docs/mcp.md).

## Environment specs

Specs are YAML files in `envs/`. They can `extend` each other; lists and maps merge, except `harnesses` and `credentials`, which replace the parent's. The full design is [spec 0006](docs/specs/0006-environments-and-harness-packs.md).

```yaml
name: my-service
extends: python                    # bare + Python 3.12/uv/ruff/pytest; bare = Ubuntu + git/gh/tmux/rg
harnesses: [claude-code, codex]    # agents this env allows: claude-code, codex, pi, antigravity
default_harness: claude-code
model: { claude-code: opus, codex: gpt-5.5 }  # default per harness for run_agent; or one string
tools:
  apt: [postgresql-client]
  mise: { go: "1.23" }
dependencies:
  pip: [ruff, pytest]              # installed with `uv tool install`
  npm: [pnpm]
  script: |                        # runs as the sandbox user
    curl -fsSL https://example.com/install.sh | bash
skills:
  - path: skills/repo-conventions  # copied into ~/.claude/skills and ~/.codex/skills
  - git: https://github.com/org/skills#review@main
agent_config:
  instructions: |                  # written to each allowed harness's global instructions file
    Run `make test` before you finish.
files:
  .config/foo/config.toml: "key = 1"
repo:
  url: https://github.com/me/my-service.git
  branch: main
  setup: uv sync                   # after clone
  bake: false                      # true = clone + setup at build time, for instant claims
credentials: [claude-max, codex-chatgpt, gh]  # each sandbox gets its harness's ones, plus non-agent ones (git)
secrets: { SENTRY_TOKEN: sentry }  # env var <- credential, injected at claim
mcp:                               # written in each harness's own config format at claim
  sentry: { url: "https://mcp.sentry.dev/mcp", headers: { Authorization: "Bearer ${SENTRY_TOKEN}" } }
resources: { cpus: 4, memory_gb: 8 }
warm_pool: 2                       # keep two pre-started
```

`hangar env render my-service` prints the generated Dockerfile. `hangar pack render codex` prints a harness pack's build, and `hangar pack publish claude-code codex pi antigravity` builds and pushes packs from your machine when the server has no builder.

## Running elsewhere

| Where | How | Isolation |
| --- | --- | --- |
| Laptop | `hangar serve`, or `docker compose up` in `deploy/compose` | runc containers |
| Your Linux or WSL2 box | `sudo bash deploy/local/kata-install.sh`, then `HANGAR_DOCKER_RUNTIME=kata hangar serve` | Kata microVMs |
| Single Linux VM | `deploy/terraform/single-vm`: a GCE VM with nested virt, running Compose | gVisor (`runsc`) or Kata microVMs |
| GKE | `deploy/terraform/gke` + `deploy/k8s` | Agent Sandbox with gVisor; optional Kata pool |
| E2B | `HANGAR_ENABLED_PROVIDERS='["docker","e2b"]'` with `HANGAR_E2B_API_KEY` | Firecracker microVMs |

You can enable several providers on one control plane and pick one per sandbox. See [docs/gke.md](docs/gke.md) for the cluster walkthrough.

## Configuration

Everything is set through `HANGAR_*` settings (see `src/hangar/config.py`), which come from two places:

- **The environment.** This includes `.env` and, on GKE, the `hangar` and `hangar-site` config maps. It is read at startup, and it wins.
- **Hangar's database.** Edit it with `hangar config set KEY VALUE`, `get`, `unset` and `list` (admin tokens only). Values are checked against each setting's type and survive redeploys. Most apply at once; `hangar config set` tells you when one needs a restart. Secrets are refused: they go in the vault (`hangar cred add`), and settings name the credential.

`hangar config list` shows each value and where it came from. The common settings:

| Variable | Purpose |
| --- | --- |
| `HANGAR_ADMIN_TOKEN` | Admin bearer token. If unset, a bootstrap token is generated once. |
| `HANGAR_SECRET_KEY` | Fernet key for the credential vault. If unset, a key file is created in the data dir. |
| `HANGAR_DATA_DIR` | State: SQLite DB, key, workspace snapshots. Default `~/.hangar`. |
| `HANGAR_ENV_DIRS` | JSON list of spec directories. Default `["envs"]`. |
| `HANGAR_DEFAULT_PROVIDER` / `HANGAR_ENABLED_PROVIDERS` | `docker`, `kubernetes`, `e2b` |
| `HANGAR_DOCKER_RUNTIME` | `runsc` (gVisor) or `kata` on Linux hosts |
| `HANGAR_K8S_RUNTIME_CLASS` | `gvisor` (default), `kata-qemu-runtime-rs` (Kata microVMs), or empty for runc |
| `HANGAR_REGISTRY` | Where cluster/E2B images are pushed, e.g. Artifact Registry |
| `HANGAR_EXTRA_CA_BUNDLE` | PEM file trusted inside every image, for corporate TLS-inspecting proxies |
| `HANGAR_DEFAULT_TTL_MINUTES` / `HANGAR_MAX_SANDBOXES` | Fleet guardrails |
| `HANGAR_E2B_GCP_SERVICE_ACCOUNT_JSON` / `HANGAR_E2B_AWS_*` / `HANGAR_E2B_REGISTRY_USERNAME` | How E2B pulls images from a private `HANGAR_REGISTRY` |

`hangar gc` lists containers left behind by crashed creates or other state directories, and `hangar gc --yes` removes them.

## Known limits

- **Forks on Kubernetes** copy `/workspace` as a tarball; GKE can only restore a sandbox from its own snapshots. With Pod snapshots enabled, Kubernetes sandboxes can be paused and resumed with memory intact (see [docs/gke.md](docs/gke.md)). On Docker and E2B, forks are full snapshots.
- **What has run for real:**
  - **Docker provider:** end to end in CI.
  - **Kubernetes provider:** end to end over MCP against the real Agent Sandbox v1.0.6 controller on kind, with the runc runtime. This covered builds pushed to a registry, claims, exec, 300 KB file writes, terminals, TTL extension, forks and teardown.
  - **Kata microVMs (Docker provider):** end to end on WSL2. Each sandbox is a VM with its own kernel; it starts in about 2 s, and 4 start in parallel in 2.2 s. See [docs/local-microvms.md](docs/local-microvms.md).
  - **GKE with the Agent Sandbox add-on and gVisor:** end to end, with the control plane in the cluster behind a Google-managed certificate. A remote MCP client created two sandboxes from the warm pool over HTTPS in 3.1 s. Each had the repo cloned on its own branch, a Claude Code subscription token and a fine-grained GitHub token. Claude Code ran headless in both, made a change with tests, and opened a PR that passed CI ([#13](https://github.com/Sean-Koval/hangar/pull/13), [#14](https://github.com/Sean-Koval/hangar/pull/14)). `scripts/agent_fleet_demo.py` reproduces the run.
  - **Not yet run live:** Kata on GKE (the add-on only admits `gvisor` and `microvm`; see [docs/gke.md](docs/gke.md)), Pod snapshots and E2B. They're covered by unit tests against fake APIs.
- **Antigravity (`agy`) subscription login** stores OAuth in the OS keyring, which headless containers may lack. Use a `GEMINI_API_KEY` credential for fleets.
- **Concurrent sandboxes share one subscription's limits.** Parallel Claude Code agents on a single Pro login hit the plan's limits quickly. Mix in API-key credentials for wide fan-out.
- **The control plane is a single replica** with SQLite state, which is right for a personal or team instance.

## Development

```bash
uv sync --all-extras
uv run pytest                                  # unit + API tests (fake provider)
HANGAR_E2E=1 uv run pytest tests/test_e2e_docker.py   # real Docker, driven over MCP
uv run ruff check src tests
uv run hangar serve --reload
```

Code map: `fleet.py` (lifecycle), `envspec.py` (specs to Dockerfiles), `broker.py` + `vault.py` (credentials), `harnesses.py` (per-agent install, auth, run), `mcp_server.py`, `app.py` (REST, websockets, dashboard), `providers/` (docker, kubernetes, e2b), `ui/` (no-build dashboard).
