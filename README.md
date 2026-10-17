![Hangar](docs/assets/banner.svg)

# Hangar

**A control plane for isolated, cloneable coding-agent sandboxes.**

Hangar manages sandboxes for Claude Code, Codex, pi, and Antigravity. Each sandbox gets its own files, tools, repository checkout, and agent configuration. Connect an MCP client to ask for work in plain language, or use the CLI and browser dashboard.

Start on a laptop with Docker. The same control plane also supports Kubernetes and E2B.

Documentation site: **[sean-koval.github.io/hangar](https://sean-koval.github.io/hangar/)**

[Quick start](#quick-start) · [First task](#run-your-first-task) · [Environments](#create-your-own-environment) · [Documentation](#documentation)

## How it works

**One shared MCP server. Multiple teammates. A fleet of isolated coding agents.**

Each teammate connects their own client to Hangar. From that client, they can create a workspace, start an agent, or fork a prepared sandbox into parallel attempts.

```mermaid
flowchart LR
    accTitle: A shared Hangar server connects a team to cloud coding-agent sandboxes
    accDescr: Two teammates using Codex and Claude Code connect over MCP to one Hangar server. Hangar manages three isolated cloud sandboxes, each with an agent, repository checkout, and work branch. Task requests, output, and results pass through the shared server.

    subgraph team["TEAM MEMBERS"]
        direction TB
        U1("Teammate A<br/><b>Codex client</b>")
        U2("Teammate B<br/><b>Claude Code client</b>")
    end

    H("<b>Hangar MCP server</b><br/>Environments + credentials<br/>Create · clone · run")

    subgraph cloud["CLOUD · ISOLATED SANDBOXES"]
        direction TB
        S1("<b>Sandbox A</b><br/>Claude Code<br/>Repo + work branch")
        S2("<b>Sandbox B</b><br/>Codex<br/>Repo + work branch")
        S3("<b>Sandbox C</b><br/>pi or Antigravity<br/>Repo + work branch")
    end

    U1 <-->|MCP / HTTPS| H
    U2 <--> H
    H <-->|Tasks / results| S1
    H <--> S2
    H <--> S3

    classDef client fill:#26323f,stroke:#7c8894,color:#e8ecef,stroke-width:1px
    classDef control fill:#26323f,stroke:#f2c230,color:#e8ecef,stroke-width:2px
    classDef sandbox fill:#26323f,stroke:#4fb39a,color:#e8ecef,stroke-width:1px
    class U1,U2 client
    class H control
    class S1,S2,S3 sandbox
    style team fill:transparent,stroke:#7c8894,stroke-dasharray:4 4
    style cloud fill:transparent,stroke:#7c8894,stroke-dasharray:4 4
```

The client on a teammate's machine and the agent inside a sandbox are separate choices: a Codex client can ask Hangar to run Claude Code, for example. All clients connect to the same `/mcp` endpoint; Hangar manages the sandboxes behind it.

| Feature | What it gives your team |
| --- | --- |
| **Connect from your own tools** | Use Codex, Claude Code, Cursor, or another [MCP client](docs/clients.md) with a client token or supported OAuth sign-in. |
| **Prepare once, run in parallel** | Define tools, dependencies, and repo setup in an [environment](#create-your-own-environment). Create separate sandboxes or fork a prepared workspace into multiple attempts. |
| **Keep workspaces separate** | Each sandbox has its own files, agent configuration, and repo checkout. Give parallel attempts separate work branches and choose which stored credentials each receives. |
| **Watch, steer, and review** | Follow live terminals in the dashboard, [steer supported agents](docs/steering.md), and review changes and test output. [Jobs](docs/jobs.md) support verification and draft PR delivery; [sessions](docs/sessions.md) preserve context across turns. |

Run the cloud fleet on a Docker VM, Kubernetes, or E2B. The same control plane also runs locally with Docker. See [deployment options and limits](#deployment-and-limits).

An **environment** is a reusable workspace recipe. A **sandbox** is a running copy. A **harness** is the agent CLI inside it.

## Quick start

You need a running [Docker](https://docs.docker.com/get-started/get-docker/) installation and [uv](https://docs.astral.sh/uv/getting-started/installation/). Hangar requires Python 3.12 or newer; uv manages the project environment.

Run these commands from the repository root:

```bash
git clone https://github.com/Sean-Koval/hangar.git
cd hangar
uv sync --all-extras
uv run hangar up
```

Hangar starts the server, saves your CLI login, builds the default `claude-python` environment, and offers to connect installed MCP clients. The first build downloads tools and dependencies, so allow time for it to finish.

Open the **dashboard at [localhost:8787](http://localhost:8787)** to see your fleet, credentials, environments, and live terminals. Local state is stored in `~/.hangar`; the server log is `~/.hangar/server.log`.

### 1. Add an agent API key

Use **Credentials** in the dashboard, or store a key from the CLI. Use an API key (an Anthropic Console key, an OpenAI project key, a Claude Team or Enterprise key) and set a spend limit on it in the vendor's console; hangar's own job and daily budgets cap spend inside hangar.

For Claude Code:

```bash
uv run hangar cred add claude-api --harness claude-code --kind api_key --file anthropic-key.txt
```

For Codex:

```bash
uv run hangar cred add codex-api --harness codex --kind api_key --file openai-key.txt
uv run hangar env build codex-node
```

Choose the credential when creating a sandbox, or list its name in your environment's `credentials` field. Storing a key alone does not attach it to every sandbox.

Jobs, schedules, webhooks and experiments run on API keys only. A personal subscription login (Claude Pro or Max, a ChatGPT sign-in) is for your own interactive sessions, with a warning: see [subscription logins](docs/team.md#subscription-logins).

### 2. Connect your client

If startup did not connect your client, choose one:

```bash
uv run hangar connect claude-code
# Or:
uv run hangar connect codex
```

Start a new client session so it reads the updated configuration. Cursor, VS Code, Gemini CLI, and Claude Desktop are also supported. See [client setup](docs/clients.md) for all options, remote connections, and OAuth.

### 3. Check your setup

```bash
uv run hangar doctor
```

If a check fails, follow the suggested fix. `uv run hangar doctor --fix` can apply supported repairs.

## Use the Hangar skill

The [Hangar skill](skills/hangar/SKILL.md) teaches your calling agent to turn a
task or ticket into a cloud job or resumable session, follow progress, handle
questions, and report changes with verification evidence.

Copy the `skills/hangar/` folder into your client's configured skills directory
(for clients using the shared convention, `~/.agents/skills/hangar/`), and reload
its skills. Connect that client to your Hangar server using the steps above;
installing the skill alone does not connect MCP.

For example, invoke the skill with:

> Use the Hangar skill to work on ticket APP-42 in my repo. Use our configured Python environment, run the project's tests, and return a draft PR with the results.

This skill runs in the client that delegates work. The separate
[repo-conventions skill](envs/skills/repo-conventions/SKILL.md) guides coding agents
working inside sandboxes.

## Run your first task

In your connected MCP client, ask:

> Use Hangar to create a claude-python sandbox for https://github.com/YOUR-USER/YOUR-REPO.git. Use the claude-api credential and a new work branch. Run Claude Code to explain the project structure and how to run its tests. Report the sandbox name and findings.

Replace the repository URL with your own. Start with a public repo, or add a Git credential for private repository access.

You can do the same from the CLI:

```bash
uv run hangar sb create claude-python \
  --name first-task \
  --repo https://github.com/YOUR-USER/YOUR-REPO.git \
  --work-branch hangar/first-task \
  --cred claude-api

uv run hangar sb agent first-task \
  "Explain the project structure and how to run its tests."
```

Open the sandbox in the dashboard to follow the work in its live terminal. When you finish reviewing it, remove it:

```bash
uv run hangar sb rm first-task
```

For Codex, use `codex-node` and `--cred codex-api` instead.

### Try parallel attempts

Once a single task works, ask your MCP client:

> Create three claude-python sandboxes for my repo, each on its own work branch, using claude-api. Ask each agent to fix the same flaky test, run the project's tests, and report the changes and results. Let me review the attempts before removing the sandboxes.

To start from work you have already prepared, use `snapshot_sandbox` and `fork_sandbox`. Snapshot behavior varies by provider; see [limitations](#deployment-and-limits).

For a task that should produce a draft PR, use a [job](docs/jobs.md). Jobs support verification commands, follow-ups, budgets, and GitHub triggers. [Sessions](docs/sessions.md) preserve a conversation and repository state across turns.

## Create your own environment

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

Replace the repo URL, branch, and setup command with your project's values. Then build it:

```bash
uv run hangar env build my-service
```

Ask your client to create a sandbox from `my-service`. It will use the configured repo and credential. `warm_pool: 1` keeps one sandbox pre-started for faster claims.

Built-in foundations include `bare`, `python`, `node`, `go`, and `polyglot`. Use [the environment reference](docs/guide.md#environment-specs) for additional tools, skills, secrets, MCP servers, and multiple agent choices.

## Everyday commands

Run these from the Hangar checkout:

| What you want | Command |
| --- | --- |
| List environments | `uv run hangar env list` |
| List sandboxes | `uv run hangar sb ls` |
| Run a command in a sandbox | `uv run hangar sb exec first-task "git status"` |
| Check client connections | `uv run hangar connect --list` |
| Inspect settings and their sources | `uv run hangar config list` |
| Freeze a good turn into a harness directory ([harness locks](docs/harness-lock.md)) | `uv run hangar harness freeze job-1a2b3c4d --turn 1 --out ../harnesses/team-default` |
| Check a harness directory against its lock | `uv run hangar harness check ../harnesses/team-default` |
| Check setup | `uv run hangar doctor` |
| Stop the local server | `uv run hangar down` |

For remote instances, run `uv run hangar login https://YOUR-HANGAR-HOST` once; fleet commands then use that server.

## Deployment and limits

| Where | Backend | Learn more |
| --- | --- | --- |
| Laptop | Docker | [Quick start](#quick-start) |
| Linux or WSL2 with microVMs | Docker + Kata | [Local microVMs](docs/local-microvms.md) |
| Single VM | Docker + gVisor or Kata | [Terraform setup](deploy/terraform/single-vm) |
| GKE | Kubernetes Agent Sandbox | [GKE deployment](docs/gke.md) |
| E2B | Firecracker microVMs | [E2B backend](docs/microvm.md) |

Hangar uses a single control-plane replica with SQLite state. Kubernetes forks copy workspace files; do not assume they preserve process memory. E2B, Pod snapshots, and Kata on GKE have not yet been validated live in the existing project reference. See [the full limitations](docs/guide.md#known-limits) and [security model](docs/security.md).

**Security posture.** Hangar treats every sandbox as hostile. A job from an intake door (a GitHub issue, Jira, GitHub Projects, Slack, an inbound webhook) can fetch its repo but never push: hangar pushes the commit it verified, through the git gateway. Its network is an egress allowlist (its environment's and repo policy's hosts and its model API), the agent has no sudo, and automated work never uses a subscription login. Once an agent has read untrusted content, its PR opens as a draft and a second person, not the requester, must approve it to land. See [the security model](docs/security.md) and [the safe-release upgrade](docs/upgrading.md#the-safe-release-120132).

For an existing deployment, read [the upgrade guide](docs/upgrading.md) before updating. To deploy from GitHub Actions with your approval instead of from a laptop, see [deploying from CI/CD](docs/cicd.md).

## Documentation

| Topic | Guide |
| --- | --- |
| Delegating tasks and tickets from an agent | [Hangar skill](skills/hangar/SKILL.md) |
| Onboarding a team, step by step | [Onboarding runbook](docs/runbooks/onboarding.md) |
| Client configuration and OAuth | [Connecting clients](docs/clients.md) |
| MCP tools, prompts, and waiting for work | [Using MCP](docs/mcp.md) |
| Jobs, verification, and draft PRs | [Jobs](docs/jobs.md) |
| Approval lands the PR, review packets, and the inbox | [Landing](docs/landing.md) |
| Resumable conversations and repo state | [Sessions](docs/sessions.md) |
| Messages, interrupts, and approvals | [Steering](docs/steering.md) |
| Live command and agent events | [Streaming](docs/streaming.md) |
| The agent's browser, screen, demos, and self-diagnosis | [Computer use](docs/computer-use.md) |
| Usage, cost, metrics, and traces | [Activity](docs/activity.md) · [Observability](docs/observability.md) |
| Environment specs, configuration, and operations | [Full reference](docs/guide.md) |
| Design decisions | [Specs](docs/specs/README.md) |
| Release history | [Changelog](CHANGELOG.md) |

## Development

```bash
uv sync --all-extras
uv run pytest -n 8
uv run ruff check src tests
uv run hangar serve --reload
```

Docker integration tests and the code map are in [the development reference](docs/guide.md#development). See [CI](docs/ci.md) for selecting relevant tests.

Licensed under [Apache 2.0](LICENSE).
