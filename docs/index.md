<div class="af-hero" markdown>
<span class="af-eyebrow">Agent factory · Run plane</span>

# A control plane for isolated, cloneable coding-agent sandboxes

<p class="af-lede">Hangar owns the factory floor: environments, harnesses, sandboxes, credentials, jobs and sessions for Claude Code, Codex, pi and Antigravity. Engineers and their MCP clients, services and CI hand it work; it runs the agents in isolated sandboxes, verifies the result and delivers a branch and a pull request.</p>

[Get started](guide.md#quickstart-laptop-5-minutes){ .md-button .md-button--primary } [Suite overview](https://sean-koval.github.io/agent-factory/){ .md-button }
</div>

## What it does

- **Runs coding agents in sandboxes.** Each sandbox gets its own files, tools, repository checkout and agent configuration, on Docker, a single VM with gVisor or Kata, GKE, or E2B. See [environments](environments.md) and [harnesses](harnesses.md).
- **Takes work through many front doors.** MCP clients, the CLI, REST, GitHub issues, Jira, Slack, webhooks, schedules and A2A all become [jobs](jobs.md) or [sessions](sessions.md), with one [repo policy](repo-policy.md) applied to each.
- **Verifies and delivers.** Hangar runs the repo's own check, pushes a branch, opens a draft PR and, with [landing](landing.md) on, an approval lands it.
- **Keeps people in the loop.** [Steer](steering.md) a running agent, [stream](streaming.md) its output, and watch [activity, usage and budgets](activity.md) and [observability](observability.md).
- **Assumes the sandbox is hostile.** Credentials stay behind a gateway and merge rights never reach an agent. See the [security model](security.md).

## How it fits the suite

Hangar is the run plane of the agent factory: it executes agent work and emits the data about it. Sister tools read that data through versioned interfaces (the harness manifest, [trace export](api/traces.md) and experiment jobs), described in [spec 0014](specs/0014-sister-tool-interfaces.md). The suite's [architecture page](https://sean-koval.github.io/agent-factory/architecture/) shows how hangar, flightdeck, ai-dlc and the governed workspace connect.

## Where to go next

| If you want to | Read |
| --- | --- |
| Run hangar on a laptop in five minutes | [Overview and quick start](guide.md) |
| Connect Codex, Claude Code or another client | [Connecting clients](clients.md) · [Using MCP](mcp.md) |
| Hand work to cloud agents | [Jobs](jobs.md) · [Sessions](sessions.md) · [Context exchange](context-exchange.md) |
| Set hangar up for a team | [Team setup](team.md) · [Team scale](team-scale.md) |
| Deploy and operate it | [Running on GKE](gke.md) · [Deploying from CI/CD](cicd.md) · [Upgrading](upgrading.md) |
| Understand the design decisions | [Specs](specs/README.md) |
