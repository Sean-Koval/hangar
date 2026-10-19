# Concepts

The words the rest of these docs use, in one page. Each section says what the thing is, why it exists, and which page
has the details.

```
 people and their tools           front doors                hangar (control plane)                 sandboxes
 ────────────────────────   ─────────────────────────   ─────────────────────────────────   ──────────────────────
 MCP clients, CLI, browser ─► MCP · REST · CLI          ─► jobs and sessions                 ─► sandbox: environment
 Jira, GitHub, Slack       ─► GitHub · Jira · Projects     repo policy · people · budgets        + harness (CLI, config)
 services, CI, agents      ─► Slack · webhooks ·           credentials (vault, gateway)         + repo checkout
                               schedules · A2A             verify → branch → PR → landing       on Docker, GKE or E2B
```

## Environment

A reusable recipe for a workspace: base image, system packages and runtimes, dependencies, skills and agent
instructions, MCP servers, credentials, resources, and optionally the repo. Specs are YAML files that `extend` each
other (`bare` → `python` → yours), and the image tag is content-addressed, so an unchanged spec never rebuilds. A repo
can also describe its own install, start and verify steps in a committed `.hangar/environment.yaml`, which hangar builds
ahead of time. See [environments](environments.md).

## Harness

Everything that shapes the agent apart from the model: the CLI at a pinned version, instructions, skills, plugins,
MCP bindings, hooks and approvals. A team freezes one into a harness directory (native files plus `harness.lock`);
its identity, `harness_digest`, is a SHA-256 over the lock's identity fields, equal to the manifest's. See
[harness locks](harness-lock.md).

The CLI is Claude Code, Codex, pi or Antigravity. An environment lists the CLIs it allows; you pick one per sandbox,
not per image. Each ships once as a read-only *harness pack* mounted at start, at the version the CLI pin file
(`src/hangar/harnesses.lock.yaml`) names, with a model resolved from a policy (`balanced`, `frontier`) or a pinned id.
See [harnesses](harnesses.md).

## Sandbox

A running copy of an environment: its own files, tools, repo checkout and agent configuration, isolated from every
other sandbox. Sandboxes start from warm pools in about a second, can be snapshotted and forked into parallel attempts,
and run on Docker (a laptop or one VM, optionally with gVisor or Kata), GKE (Agent Sandbox with gVisor or Kata) or E2B.
hangar runs commands in them through a small daemon, [hangar-envd](envd.md). See the
[overview](guide.md#where-it-runs).

## Session

A resumable stream of work: an agent's conversation plus the repo's state, checkpointed after every turn. A follow-up
days later boots a fresh sandbox from the checkpoint and the agent continues the same conversation. You can
[steer](steering.md) a running turn, [stream](streaming.md) its output, and pass files and results in and out through
[context exchange](context-exchange.md). See [sessions](sessions.md).

## Job

A piece of work handed to hangar, which runs it to a result: an agent turn in a session, the repo's own check
(`verify`), then a branch and a draft pull request. Jobs take follow-ups, fix their own failing CI and review comments,
run best-of-N attempts, and carry budgets. A job's outcome class says whether the agent, a person or the
infrastructure ended it. See [jobs](jobs.md).

## Front doors

Every way work reaches hangar: MCP clients, the CLI and REST from people; GitHub issues and `/hangar` comments, Jira
and GitHub Projects, Slack and signed webhooks from where the team already works; schedules; and A2A from other agents.
Each becomes a job or a session, and work from outside (an issue, a ticket, a thread) is an *intake job* with stricter
defaults. See [intake](intake.md), [Slack](slack.md), [triggers](triggers.md) and [A2A](a2a.md).

## Repo policy

One record per repository with the defaults every front door applies: the check to run, the environment, harness and
model, budgets and a daily cap, egress additions, and how its PRs land. See [repo policy](repo-policy.md); `hangar repo
check` reports whether a repo is ready for agent work.

## Landing

With landing on, an approval lands the PR: agent PRs come from a GitHub App, hangar arms auto-merge for exactly the
approved commit once the required checks pass, and GitHub's branch protection stays the gate. Each PR gets a review
packet, and one inbox lists what waits on each person. See [landing](landing.md).

## People and credentials

Every token belongs to a person, an admin or a member; members see and act on their own work. Agent logins are stored
encrypted and injected at claim time: a person's own API key, else the team's. Subscription logins (Claude Pro or Max,
ChatGPT) are for a person's own interactive sessions only; automated work runs on API keys. Approvals follow one rule:
an admin or a repo approver, never your own job. See [team setup](team.md) and [team scale](team-scale.md).

## The trust boundary

hangar assumes the sandbox is hostile. Git credentials stay behind the [git gateway](security.md#git-credential-gateway):
job sandboxes only fetch, and hangar pushes the commit it verified. [Egress allowlists](security.md#egress-allowlists)
limit where a sandbox can connect. Intake jobs get both by default ([intake posture](security.md#intake-posture)). A turn
that read untrusted content is [tainted](security.md#taint), and its push, PR, landing and egress then need a second
person. See the [security model](security.md).

## What hangar emits

hangar is the run plane of the agent factory: it runs the work and emits data about it, and sister tools interpret it.
Every turn records a [manifest](specs/0014-sister-tool-interfaces.md#2-the-effective-harness-manifest-hangarmanifestv1)
of the configuration it ran with; [trace export](api/traces.md) gives each run as OTLP on the OpenTelemetry GenAI
conventions; [run capture](api/outbox.md) writes every finished run to a bucket once; and
[experiment jobs](jobs.md#9-experiments) run variants against graders. Flightdeck reads these to evaluate harnesses.
Tower, the Govern plane, is to decide approvals once hangar asks it: its decision API is built, and hangar's wiring is
open (#172, [spec 0015](specs/0015-governance-decision-point.md)). Live metrics, traces and cost are in
[activity](activity.md) and [observability](observability.md).

## Inside the control plane

Connect Codex, Claude Code, or any MCP client to hangar. Then spin up isolated sandboxes from environment templates that bundle your tools, agent harness, dependencies, skills, and repo. hangar handles the logins, runs agents inside the sandboxes, forks a prepared sandbox into N parallel attempts, and tears everything down when you're done. The same control plane runs on your laptop (Docker), on a single VM (Docker + gVisor or Kata microVMs), or on GKE (Agent Sandbox with gVisor or Kata). E2B is supported as a backend too.

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
  - API keys (Anthropic Console, OpenAI project, Claude Team or Enterprise keys) work everywhere: sessions, sandboxes, jobs, schedules, webhooks and experiments.
  - Subscription logins (Claude Pro or Max, a ChatGPT or Google sign-in) are for a person's own interactive sessions and sandboxes only, with a warning; jobs and other automated work refuse them ([team.md](team.md#subscription-logins)). A sign-in runs inside a short-lived sandbox, and you finish it in the dashboard terminal.
  - Credentials are encrypted at rest and injected into `~/.hangar/env` or their usual files at claim time.
  - Codex's rotating refresh token is *leased* to one sandbox at a time and written back when that sandbox stops, so sandboxes never log each other out.
- **Terminals that agents and humans share.** MCP clients drive persistent tmux sessions (`terminal_send` / `terminal_read`). From the dashboard you can attach to the same session and watch, or type along.
- **Agents inside sandboxes.** `run_agent` starts Claude Code, Codex, pi or `agy` (Antigravity) headless on a task, with an optional `model`, so one orchestrator session can fan work out across the fleet.
- **Bearer tokens or OAuth.** `hangar connect <client>` writes the client's config with a token scoped to MCP. ChatGPT connectors and other OAuth-only clients use hangar's built-in OAuth 2.1 server: discovery, dynamic registration, PKCE, and a consent page where you approve with a hangar token. See [docs/clients.md](clients.md).
- **A web console.** It covers jobs, sessions, the fleet four ways ([the web console](console.md)), live terminals, the environment editor with streaming builds, credentials and sign-in, snapshots, MCP connect snippets with token management, and an audit log.
- **Traces, metrics and cost.** OpenTelemetry traces from the control plane when `OTEL_*` is set; Claude Code's and Codex's own spans join them. Prometheus `/metrics` for a scrape-only token. A usage view of spend by day, repo and requester. See [docs/observability.md](observability.md).

