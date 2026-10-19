# Getting started

Run hangar on a laptop in five minutes, then on GKE, then for a team. This is the one Quickstart; the [README](https://github.com/Sean-Koval/hangar/blob/main/README.md) repeats its five commands. [Concepts](concepts.md) explains the words
(environment, harness, sandbox, session, job) and what hangar gives you, and the [changelog](https://github.com/Sean-Koval/hangar/blob/main/CHANGELOG.md) what each release added.

## Quickstart

Five minutes on a laptop. You need [Docker](https://docs.docker.com/get-started/get-docker/) (Docker Desktop, OrbStack or Colima on macOS) and [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python 3.12+ is managed by uv).

```bash
git clone https://github.com/Sean-Koval/hangar.git && cd hangar
uv sync --all-extras
uv run hangar up        # server, CLI login, the claude-python environment, MCP clients
uv run hangar cred add claude-api --harness claude-code --kind api_key --file anthropic-key.txt
uv run hangar doctor    # every check passes, or names its fix
```

`hangar up` does five things: checks that Docker is running; starts the control plane in the background (log: `~/.hangar/server.log`); logs the CLI in; builds the `claude-python` environment (the first build downloads tools, so allow a few minutes); and offers to connect each MCP client it finds. Running it again is safe: it reuses the running server, the saved login and the built images. `--env NAME` builds other environments, `--no-build` skips building, and `--connect codex` connects a client without asking. `hangar down` stops the server.

Open the **console at [localhost:8787](http://localhost:8787)** to see your fleet, credentials, environments and live terminals ([the web console](console.md)). Local state is in `~/.hangar`. If a doctor check fails, follow its fix; `uv run hangar doctor --fix` applies the supported repairs.

To look around first without Docker or a key, run the demo hangar the front page's recording uses: `uv run python docs/assets/front/demo.py` (the console on :8787, its token printed), then `uv run python docs/assets/front/demo.py ticket` in another terminal to label issue #42.

### Credentials

Use **Credentials** in the console, or store a key from the CLI as above. Use an API key (an Anthropic Console key, an OpenAI project key, a Claude Team or Enterprise key) and set a spend limit on it in the vendor's console; hangar's own job and daily budgets cap spend inside hangar. For Codex, and for private repositories:

```bash
uv run hangar cred add codex-api --harness codex --kind api_key --file openai-key.txt
uv run hangar env build codex-node
uv run hangar cred add gh --harness git --kind git_token --value "$(gh auth token)"
```

Choose the credential when creating a sandbox, or list its name in your environment's `credentials` field. Storing a key alone does not attach it to every sandbox.

Jobs, schedules, webhooks and experiments run on API keys only. A personal subscription login (Claude Pro or Max, a ChatGPT sign-in) is for your own interactive sessions, with a warning: see [subscription logins](team.md#subscription-logins).

### Connect your client

If `hangar up` did not connect your client, run `uv run hangar connect claude-code` (or `codex`, `cursor`, `vscode`, `gemini`, `claude-desktop`). It adds hangar to the client's own config file, next to your other servers, with a fresh token that can reach the MCP endpoints and nothing else: `hangar` (`/mcp`: jobs, sessions, schedules) and, for Claude Code and Codex, `hangar-sandbox` (`/mcp/sandbox`: create_sandbox, exec, files, terminals; [the two servers](mcp.md#two-servers)). Start a new client session so it reads the updated configuration. `hangar connect --list` shows what's connected; [connecting clients](clients.md) covers all options, remote connections and OAuth.

### Use the hangar skill

The [hangar skill](https://github.com/Sean-Koval/hangar/blob/main/skills/hangar/SKILL.md) teaches your calling agent to turn a task or ticket into a cloud job or resumable session, follow progress, handle questions, and report changes with verification evidence.

Copy the `skills/hangar/` folder into your client's configured skills directory (for clients using the shared convention, `~/.agents/skills/hangar/`), and reload its skills. Connect that client to your hangar server as above; installing the skill alone does not connect MCP. For example:

> Use the hangar skill to work on ticket APP-42 in my repo. Use our configured Python environment, run the project's tests, and return a draft PR with the results.

This skill runs in the client that delegates work. The separate [repo-conventions skill](https://github.com/Sean-Koval/hangar/blob/main/envs/skills/repo-conventions/SKILL.md) guides coding agents working inside sandboxes.

### First task

In your connected MCP client, ask:

> Use hangar to create a claude-python sandbox for https://github.com/YOUR-USER/YOUR-REPO.git. Use the claude-api credential and a new work branch. Run Claude Code to explain the project structure and how to run its tests. Report the sandbox name and findings.

Replace the repository URL with your own. Start with a public repo, or add a Git credential for private repository access. The same from the CLI:

```bash
uv run hangar sb create claude-python \
  --name first-task \
  --repo https://github.com/YOUR-USER/YOUR-REPO.git \
  --work-branch hangar/first-task \
  --cred claude-api

uv run hangar sb agent first-task \
  "Explain the project structure and how to run its tests."
```

Open the sandbox in the console to follow the work in its live terminal. When you finish reviewing it, remove it with `uv run hangar sb rm first-task`. For Codex, use `codex-node` and `--cred codex-api` instead.

### Parallel attempts

Once a single task works, ask your MCP client:

> Create three claude-python sandboxes for my repo, each on its own work branch, using claude-api. Ask each agent to fix the same flaky test, run the project's tests, and report the changes and results. Let me review the attempts before removing the sandboxes.

To start from work you have already prepared, use `snapshot_sandbox` and `fork_sandbox`. Snapshot behavior varies by provider; see [where it runs](#where-it-runs). For a task that should produce a draft PR, use a [job](jobs.md): jobs support verification commands, follow-ups, budgets and GitHub triggers. [Sessions](sessions.md) preserve a conversation and repository state across turns.

### Take work from GitHub

`uv run hangar setup github` asks for a repo and a token and stores them. It then registers the repo's webhook and sends a test delivery. GitHub can't reach `localhost`, so on a laptop pass `--webhook-url` with a tunnel URL. See [jobs: trigger from GitHub](jobs.md#4-trigger-from-github).

### Your own environment

Save this as `envs/my-service.yaml`:

```yaml
name: my-service
extends: python
harnesses: [claude-code]
default_harness: claude-code
credentials: [claude-api]
repo:
  url: https://github.com/YOUR-USER/YOUR-REPO.git
  branch: main
  setup: uv sync
agent_config:
  instructions: |
    Run the project's tests before finishing.
resources:
  cpus: 2
  memory_gb: 4
warm_pool: 1
```

Replace the repo URL, branch and setup command with your project's values, then build it with `uv run hangar env build my-service`. Ask your client to create a sandbox from `my-service`; it uses the configured repo and credential. `warm_pool: 1` keeps one sandbox pre-started for faster claims. Built-in foundations include `bare`, `python`, `node`, `go` and `polyglot`; [environments](environments.md) covers tools, skills, secrets, MCP servers and multiple agent choices.

### Everyday commands

Run these from the hangar checkout:

| What you want | Command |
| --- | --- |
| List environments | `uv run hangar env list` |
| List sandboxes | `uv run hangar sb ls` |
| Run a command in a sandbox | `uv run hangar sb exec first-task "git status"` |
| What waits on you | `uv run hangar inbox` |
| Check client connections | `uv run hangar connect --list` |
| Inspect settings and their sources | `uv run hangar config list` |
| Freeze a good turn into a harness directory ([harness locks](harness-lock.md)) | `uv run hangar harness freeze job-1a2b3c4d --turn 1 --out ../harnesses/team-default` |
| Check a harness directory against its lock | `uv run hangar harness check ../harnesses/team-default` |
| Check setup | `uv run hangar doctor` |
| Stop the local server | `uv run hangar down` |

For remote instances, run `uv run hangar login https://YOUR-HANGAR-HOST` once; fleet commands then use that server.

## Production (GKE)

```bash
cp deploy/terraform/gke/terraform.tfvars.example deploy/terraform/gke/terraform.tfvars   # set project_id, region
uv run hangar up --gke --dry-run          # the terraform plan; nothing changes
uv run hangar up --gke                    # asks, then applies that plan, runs deploy/gke/up.sh and logs in
hangar doctor --fix                       # what's missing, and the fixes it can apply
hangar setup github                       # token, webhook secret, repo webhook, settings, test delivery
hangar connect claude-code                # and any other MCP client
```

`hangar up --gke` never applies a plan that destroys or replaces anything unless you pass `--allow-destroy`, and without a terminal it applies nothing unless you pass `--yes`. The steps it runs are the ones in [docs/gke.md](gke.md), which you can also run by hand. `up.sh` points `HANGAR_SESSION_STORE` at the checkpoint bucket terraform made, on a first install. If the certificate isn't Active yet, `hangar up --gke` prints the `hangar login` command to run once it is.

Settings you change with `hangar setup` or `hangar config set` are stored in hangar's database, on its volume. They survive redeploys and need no `kubectl`. The full walkthrough is in [docs/gke.md](gke.md).

## A team on one hangar

To put a team on hangar step by step (2 engineers on 1 repo, then 6 on 3), follow [the onboarding runbook](runbooks/onboarding.md); it ends with `hangar repo check` green for each repo. The [runbooks](runbooks/README.md) also cover the Friday harness review, run capture and governance break-glass.

An admin adds people; each gets a login token, connects their own clients and stores their own agent login:

```sh
hangar person add alice                       # a member; prints her login token once
hangar person add bob --role admin
hangar person ls                              # owner is the admin your existing tokens belong to
# alice, on her laptop:
hangar login https://hangar.example           # paste the token
hangar connect claude-code                    # an mcp token of her own
hangar cred add claude-alice --harness claude-code --kind api_key --file key.txt
```

Members see and act on their own sandboxes, sessions and jobs; admins on everyone's. Alice's sessions and jobs use her own API key; teammates without one of their own use a team API key (`hangar cred add NAME --team --kind api_key`). Jobs and other automated work never use a subscription login ([team.md](team.md#subscription-logins)). A job that waits for approval is approved by an admin or a repo policy approver, from `hangar job approve` or a login connection, never by its own person or requester ([upgrading.md](upgrading.md#the-safe-release-120132)). `hangar cred owner NAME PERSON|team` hands a credential to someone or back to the team, and `hangar person rm` revokes someone's tokens. See [security.md](security.md#people).

## Next steps

| To | Read |
| --- | --- |
| Connect Codex, Claude Code, Cursor or another client | [Connecting clients](clients.md) · [Using MCP](mcp.md) |
| Hand a task to a cloud agent and get a PR back | [Jobs](jobs.md) |
| Keep working with an agent across days | [Sessions](sessions.md) |
| Write an environment for your repo | [Environments](environments.md#environment-specs) |
| Take work from GitHub, Jira or Slack | [Jobs: trigger from GitHub](jobs.md#4-trigger-from-github) · [Intake](intake.md) · [Slack](slack.md) |
| Let an approval land the PR | [Landing](landing.md) |
| Put a team on it, repo by repo | [The onboarding runbook](runbooks/onboarding.md) |
| Upgrade a running hangar | [Upgrading](upgrading.md) |
| Run it day to day, and when something breaks | [Runbooks](runbooks/README.md) · [Troubleshooting](troubleshooting.md) |

## MCP tools

Moved to [Using MCP: the tools](mcp.md#the-tools).

## Environment specs

Moved to [Environments: environment specs](environments.md#environment-specs).

## Where it runs

| Where | How | Isolation |
| --- | --- | --- |
| Laptop | `hangar serve`, or `docker compose up` in `deploy/compose` | runc containers |
| Your Linux or WSL2 box | `sudo bash deploy/local/kata-install.sh`, then `HANGAR_DOCKER_RUNTIME=kata hangar serve` | Kata microVMs |
| Single Linux VM | `deploy/terraform/single-vm`: a GCE VM with nested virt, running Compose | gVisor (`runsc`) or Kata microVMs |
| GKE | `deploy/terraform/gke` + `deploy/k8s` | Agent Sandbox with gVisor; optional Kata pool |
| E2B | `HANGAR_ENABLED_PROVIDERS='["docker","e2b"]'` with `HANGAR_E2B_API_KEY` | Firecracker microVMs |

You can enable several providers on one control plane and pick one per sandbox. See [docs/gke.md](gke.md) for the cluster walkthrough, [local microVMs](local-microvms.md) for Kata on your own box and [the E2B backend](microvm.md). hangar uses a single control-plane replica with SQLite state. Kubernetes forks copy workspace files; do not assume they preserve process memory. E2B, Pod snapshots and Kata on GKE have not yet been validated live ([known limits](#known-limits)).

On Docker and Kubernetes, hangar starts a small daemon in each sandbox, [hangar-envd](envd.md), and runs commands, file transfers and terminals through it instead of `docker exec` or the Kubernetes exec API: an `exec` takes about 7 ms instead of 70 on Docker. It speaks E2B's envd protocol. Build it with `envd/build.sh` (the control-plane image includes it); without it, or with `HANGAR_ENVD=off`, hangar uses exec as before.

Because of that, hangar also answers E2B's API: the e2b SDKs, and the agent frameworks built on them, run in your hangar sandboxes after you change `E2B_API_KEY` and a URL. See [docs/e2b.md](e2b.md).

## Configuration

Everything is set through `HANGAR_*` settings (see `src/hangar/config.py`), which come from two places:

- **The environment.** This includes `.env` and, on GKE, the `hangar` and `hangar-site` config maps. It is read at startup, and it wins.
- **hangar's database.** Edit it with `hangar config set KEY VALUE`, `get`, `unset` and `list` (admin tokens only). Values are checked against each setting's type and survive redeploys. Most apply at once; `hangar config set` tells you when one needs a restart. Secrets are refused: they go in the vault (`hangar cred add`), and settings name the credential.

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

- **Forks on Kubernetes** copy `/workspace` as a tarball; GKE can only restore a sandbox from its own snapshots. With Pod snapshots enabled, Kubernetes sandboxes can be paused and resumed with memory intact (see [docs/gke.md](gke.md)). On Docker and E2B, forks are full snapshots.
- **What has run for real:**
  - **Docker provider:** end to end in CI.
  - **Kubernetes provider:** end to end over MCP against the real Agent Sandbox v1.0.6 controller on kind, with the runc runtime. This covered builds pushed to a registry, claims, exec, 300 KB file writes, terminals, TTL extension, forks and teardown.
  - **Kata microVMs (Docker provider):** end to end on WSL2. Each sandbox is a VM with its own kernel; it starts in about 2 s, and 4 start in parallel in 2.2 s. See [docs/local-microvms.md](local-microvms.md).
  - **GKE with the Agent Sandbox add-on and gVisor:** end to end, with the control plane in the cluster behind a Google-managed certificate. A remote MCP client created two sandboxes from the warm pool over HTTPS in 3.1 s. Each had the repo cloned on its own branch, a Claude Code token and a fine-grained GitHub token (that run predates [subscription logins being interactive-only](team.md#subscription-logins); fleets use API keys). Claude Code ran headless in both, made a change with tests, and opened a PR that passed CI ([#13](https://github.com/Sean-Koval/hangar/pull/13), [#14](https://github.com/Sean-Koval/hangar/pull/14)). `scripts/agent_fleet_demo.py` reproduces the run.
  - **Not yet run live:** Kata on GKE (the add-on only admits `gvisor` and `microvm`; see [docs/gke.md](gke.md)), Pod snapshots and E2B. They're covered by unit tests against fake APIs.
- **Antigravity (`agy`) subscription login** stores OAuth in the OS keyring, which headless containers may lack. Use a `GEMINI_API_KEY` credential for fleets.
- **Spend caps live in two places.** Set a workspace spend limit on each API key in the vendor's console (Anthropic Console, OpenAI project limits); hangar's job and daily budgets cap spend inside hangar.
- **The control plane is a single replica** with SQLite state, which is right for a personal or team instance.

## Development

```bash
uv sync --all-extras
uv run pytest -n 8                             # unit + API tests (fake provider), in parallel
scripts/ci/select_tests.py --base origin/main  # just the tests your branch can break (docs/ci.md)
HANGAR_E2E=1 uv run pytest tests/test_e2e_docker.py   # real Docker, driven over MCP
uv run ruff check src tests
uv run hangar serve --reload
```

The docs site builds the way `.github/workflows/docs.yml` builds it (Zensical, from `mkdocs.yml`):

```bash
pip install zensical==0.0.71
zensical build --strict    # into site/; a broken link or anchor fails it, as in CI
zensical serve             # http://localhost:8000, rebuilt on save
```

Code map: `fleet.py` (lifecycle), `envspec.py` (specs to Dockerfiles), `broker.py` + `vault.py` (credentials), `harnesses.py` (per-agent install, auth, run), `mcp_server.py`, `app.py` (REST, websockets, dashboard), `providers/` (docker, kubernetes, e2b), `ui/` (no-build dashboard).
