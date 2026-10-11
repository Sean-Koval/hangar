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

- **Environments as layered specs.** Base image, then system packages and runtimes (via mise), then agent harnesses, then dependencies, then skills and agent config, then the repo. Layers are ordered for Docker cache reuse, and the image tag is content-addressed, so changing a setup command doesn't reinstall your harnesses.
- **Fast, concurrent sandboxes.** Warm pools keep sandboxes pre-started. Claiming one, cloning the repo and checking out a fresh work branch took about 0.6s in testing, and four in parallel took about 1.2s.
- **Clone two ways.** Rebuild from the spec, or snapshot a live sandbox and fork it into up to 20 copies with the same files, installed deps and repo state.
- **Logins for Claude Code, Codex and Antigravity.**
  - Both API keys and subscription logins are supported.
  - Subscription logins run inside a short-lived sandbox, and you finish them in the dashboard terminal.
  - Credentials are encrypted at rest and injected into `~/.hangar/env` or their usual files at claim time.
  - Codex's rotating refresh token is *leased* to one sandbox at a time and written back when that sandbox stops, so sandboxes never log each other out.
- **Terminals that agents and humans share.** MCP clients drive persistent tmux sessions (`terminal_send` / `terminal_read`). From the dashboard you can attach to the same session and watch, or type along.
- **Agents inside sandboxes.** `run_agent` starts Claude Code, Codex, `agy` (Antigravity) or Gemini CLI headless on a task, so one orchestrator session can fan work out across the fleet.
- **A dashboard.** It covers the fleet, live terminals, the environment editor with streaming builds, credentials and sign-in, snapshots, MCP connect snippets with token management, and an audit log.

## Quickstart (macOS)

Requires Docker Desktop, OrbStack or Colima, plus [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Sean-Koval/hangar && cd hangar
uv sync --all-extras
uv run hangar serve            # dashboard: http://localhost:8787
```

1. **Open the dashboard.** The first start prints a bootstrap admin token and saves it to `~/.hangar/bootstrap-token`. Paste it into the dashboard.
2. **Build an environment.** Under **Environments**, pick `claude-python` and choose **Build**. The first build takes a few minutes; after that, builds are cached.
3. **Add credentials.** Under **Credentials**, add an API key, or choose **Sign in to Claude Code / Codex**. From a terminal:
   ```bash
   claude setup-token | uv run hangar cred add claude-max --harness claude-code --kind oauth_token
   uv run hangar cred add codex-chatgpt --harness codex --kind auth_file --file ~/.codex/auth.json
   uv run hangar cred add gh --harness git --kind git_token --value "$(gh auth token)"
   ```
4. **Connect an MCP client.** Mint a token under **Connect** (or run `uv run hangar token create codex`), then:
   ```toml
   # ~/.codex/config.toml
   [mcp_servers.hangar]
   url = "http://localhost:8787/mcp"
   bearer_token_env_var = "HANGAR_TOKEN"
   tool_timeout_sec = 900
   ```
   ```bash
   export HANGAR_TOKEN=hgr_...
   # Claude Code:
   claude mcp add --transport http hangar http://localhost:8787/mcp --header "Authorization: Bearer $HANGAR_TOKEN"
   ```
5. **Ask your client to do something.** For example: *"Create 3 claude-python sandboxes on github.com/me/api, each on its own branch, and run the agent in each on 'make the flaky test in tests/test_sync.py deterministic'. Report which attempt passes."*

## MCP tools

| Area | Tools |
| --- | --- |
| Discover | `fleet_overview`, `list_environments`, `describe_environment`, `build_environment`, `list_credentials` |
| Lifecycle | `create_sandbox`, `create_sandboxes` (N in parallel), `list_sandboxes`, `get_sandbox`, `destroy_sandbox`, `destroy_sandboxes`, `pause_sandbox`, `resume_sandbox`, `extend_sandbox` |
| Commands and files | `exec`, `read_file`, `write_file`, `list_files` |
| Terminals | `terminal_open`, `terminal_send` (supports `wait_for` regex), `terminal_read`, `terminal_list`, `terminal_close` |
| Agents | `run_agent`, `agent_status` |
| Clone | `snapshot_sandbox`, `list_snapshots`, `fork_sandbox` |

Every tool accepts a sandbox id or name. Relative paths resolve against the sandbox's repo checkout, falling back to `/workspace`.

## Environment specs

Specs are YAML files in `envs/`. They can `extend` each other, and lists merge.

```yaml
name: my-service
extends: base                      # base = Ubuntu 24.04 + git/tmux/rg + mise python/uv/node
harnesses: [claude-code, codex]    # also: antigravity, gemini, git
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
  instructions: |                  # written to ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.gemini/GEMINI.md
    Run `make test` before you finish.
files:
  .config/foo/config.toml: "key = 1"
repo:
  url: https://github.com/me/my-service.git
  branch: main
  setup: uv sync                   # after clone
  bake: false                      # true = clone + setup at build time, for instant claims
credentials: [claude-max, gh]      # attached by default
resources: { cpus: 4, memory_gb: 8 }
warm_pool: 2                       # keep two pre-started
```

`hangar env render my-service` prints the generated Dockerfile.

## Running elsewhere

| Where | How | Isolation |
| --- | --- | --- |
| Laptop | `hangar serve`, or `docker compose up` in `deploy/compose` | runc containers |
| Single Linux VM | `deploy/terraform/single-vm`: a GCE VM with nested virt, running Compose | gVisor (`runsc`) or Kata microVMs |
| GKE | `deploy/terraform/gke` + `deploy/k8s` | Agent Sandbox with gVisor; optional Kata pool |
| E2B | `HANGAR_ENABLED_PROVIDERS='["docker","e2b"]'` with `HANGAR_E2B_API_KEY` | Firecracker microVMs |

You can enable several providers on one control plane and pick one per sandbox. See [docs/gke.md](docs/gke.md) for the cluster walkthrough.

## Configuration

Everything is set through `HANGAR_*` environment variables (see `src/hangar/config.py`). The common ones:

| Variable | Purpose |
| --- | --- |
| `HANGAR_ADMIN_TOKEN` | Admin bearer token. If unset, a bootstrap token is generated once. |
| `HANGAR_SECRET_KEY` | Fernet key for the credential vault. If unset, a key file is created in the data dir. |
| `HANGAR_DATA_DIR` | State: SQLite DB, key, workspace snapshots. Default `~/.hangar`. |
| `HANGAR_ENV_DIRS` | JSON list of spec directories. Default `["envs"]`. |
| `HANGAR_DEFAULT_PROVIDER` / `HANGAR_ENABLED_PROVIDERS` | `docker`, `kubernetes`, `e2b` |
| `HANGAR_DOCKER_RUNTIME` | `runsc` (gVisor) or `kata` on Linux hosts |
| `HANGAR_K8S_RUNTIME_CLASS` | `gvisor` (default), `kata-qemu`, or empty for runc |
| `HANGAR_REGISTRY` | Where cluster/E2B images are pushed, e.g. Artifact Registry |
| `HANGAR_EXTRA_CA_BUNDLE` | PEM file trusted inside every image, for corporate TLS-inspecting proxies |
| `HANGAR_DEFAULT_TTL_MINUTES` / `HANGAR_MAX_SANDBOXES` | Fleet guardrails |

## Known limits

- **ChatGPT developer-mode connectors** accept only OAuth or no auth, not bearer tokens. Codex and Claude Code work today. To use ChatGPT, front hangar with an OAuth proxy (see [docs/clients.md](docs/clients.md)).
- **Forks on Kubernetes** use portable workspace tarballs. GKE Pod snapshots, which also capture memory and installed packages outside `/workspace`, aren't wired in yet. On Docker and E2B, forks are full snapshots.
- **Test coverage of the Kubernetes and E2B providers** is unit tests against fake APIs only; they haven't run against a live cluster or account yet. The Docker path is exercised end to end.
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
