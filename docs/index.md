---
hide:
  - navigation
  - toc
---

<div class="af-hero af-hero--visual" markdown>
<div markdown>
<span class="af-eyebrow">Agent factory · Run plane</span>

# Hand tickets to coding agents in isolated cloud sandboxes

<p class="af-lede">Your team wants to hand tickets to coding agents, but not to a laptop with everyone's credentials and no record of what ran. Hangar runs Claude Code, Codex, pi and Antigravity in isolated sandboxes, checks their work with your repo's own tests and hands back a draft pull request.</p>

[Quickstart](guide.md#quickstart){ .md-button .md-button--primary } [The web console](console.md){ .md-button }
</div>
<figure class="af-hero__visual">
<video src="assets/front/hangar-demo.mp4" poster="assets/front/ui-jobs.png" autoplay muted playsinline controls width="1280" height="800" aria-label="A GitHub issue labelled hangar becomes a job: a sandbox starts, the agent edits and tests the code, hangar verifies and opens draft PR #43, and the PR waits in Needs you with a question another job asked"></video>
<figcaption>18 s: a ticket becoming a sandbox run and a draft PR, recorded by docs/assets/front/make.py against the demo hangar</figcaption>
</figure>
</div>

## Who it is for

<div class="af-tools">
  <a class="af-tool" href="intake/"><small>Ticket to draft PR</small><b>Product engineers</b><span>Label a Jira or GitHub issue <code>hangar</code>. A sandbox starts from the repo's environment, the agent fixes it, hangar runs the repo's check and opens a draft PR that closes the issue. You review a PR, not a chat log.</span></a>
  <a class="af-tool" href="context-exchange/"><small>Delegate from your agent</small><b>Developers in Claude Code or Codex</b><span>Say "hand this to the cloud". Your uncommitted changes go along, three attempts run in parallel, you pick the best and pull its patch back while your laptop stays free.</span></a>
  <a class="af-tool" href="team/"><small>One fleet for the team</small><b>Platform engineers</b><span>Run one hangar on GKE. Everyone connects their own client with their own token; credentials stay behind a gateway, budgets cap spend, and the inbox brings each person their reviews, approvals and agents' questions.</span></a>
</div>

## How it works

Many doors bring work in; hangar runs it in a sandbox and verifies it; the pull request is the one gate out. Around it, the team harness repo defines what the agents run, flightdeck judges the runs and [Tower](https://sean-koval.github.io/agent-factory/tools/workspace/) is to decide risky actions ([#172](https://github.com/Sean-Koval/hangar/issues/172)). The suite's [architecture page](https://sean-koval.github.io/agent-factory/architecture/) shows how they connect.

![Trackers, chat and automation send work to hangar, which runs it in sandboxes, verifies it and opens a draft PR for people to review; the team harness repo supplies the harness lock, flightdeck reads the outbox, and hangar is to ask Tower about risky actions](assets/front/hangar-suite-dark.png#only-dark)
![Trackers, chat and automation send work to hangar, which runs it in sandboxes, verifies it and opens a draft PR for people to review; the team harness repo supplies the harness lock, flightdeck reads the outbox, and hangar is to ask Tower about risky actions](assets/front/hangar-suite-light.png#only-light)

## What it looks like

The console on `:8787` opens on **Needs you**: held tool calls, jobs waiting to start and the agents' questions, decided in place. [The web console](console.md) walks through it, the Jobs and Sessions pages and the Fleet's four views.

![Needs you: a job from outside the team waiting for approval, a held git push with its policy and expiry and Deny, Allow for this turn and Allow, and an agent's question answered in place](assets/front/ui-needs.png)

## Features

| Feature | What it gives your team | Status |
| --- | --- | --- |
| **Many front doors** | MCP clients, the CLI, REST, GitHub issues and Projects, Jira, Slack, webhooks, schedules and A2A all become [jobs](jobs.md) or [sessions](sessions.md), with one [repo policy](repo-policy.md) for each repo. | Shipped |
| **Isolated sandboxes** | Each has its own files, tools, checkout and agent configuration, from [environments](environments.md) you prepare once; fork a prepared one into parallel attempts. Docker, gVisor or Kata, GKE or E2B. | Shipped |
| **Any agent** | Claude Code, Codex, pi and Antigravity as [harnesses](harnesses.md), pinned by version; a Codex client can ask for Claude Code and the other way round. A team shares one harness (instructions, skills, Claude Code plugins) frozen from a good run into a [harness repo](harness-lock.md#the-team-harness-repo). | Shipped |
| **Verified delivery** | The repo's own check runs before anything is pushed; hangar pushes through the git gateway and opens a draft PR with a review packet; with [landing](landing.md), an approval lands it. | Shipped |
| **People in the loop** | [Steer](steering.md) a running agent, answer its questions where it asked (the ticket, the Slack thread, MCP), watch its terminal; the inbox and the [web console](console.md) show what needs you. | Shipped |
| **Resumable sessions** | The conversation and repo state survive the sandbox; come back days later or fork a session. | Shipped |
| **Hostile-sandbox security** | Credentials stay behind a gateway; intake jobs get an egress allowlist and no push rights; tainted work opens as a draft and needs a second person. See the [security model](security.md). | Shipped |
| **Usage and insight** | Tokens, cost and budgets per job and person ([activity](activity.md)), traces as OTLP ([observability](observability.md)), and run capture for [flightdeck](specs/0014-sister-tool-interfaces.md). | Shipped |
| **Shadow runs** | A repo policy samples real tickets and runs a candidate harness on each at the same base commit; it delivers nothing, and flightdeck grades it against what people merged ([shadows](repo-policy.md#shadows)). | Shadow |
| **Governed approvals** | Job release, landing and risky tool calls ask [Tower](https://sean-koval.github.io/agent-factory/tools/workspace/), the Govern plane, which decides and signs a receipt; hangar enforces and fails closed. Today hangar's own approval rule decides; the client and contract are built ([spec 0015](specs/0015-governance-decision-point.md)). | Planned ([#172](https://github.com/Sean-Koval/hangar/issues/172)) |
| **Overnight lane** | One Spot node kept up while the sandbox pools are powered down (01:00–10:00) for the intake jobs of repos that opt in and for evals, so evening tickets become PRs by morning; off until an admin turns it on ([the overnight lane](gke.md#the-overnight-lane)). | Shipped |
| **Plugins and events out** | Doors, trackers, notifiers and deliverers are plugins: a package of your own adds a chat bot's door or a company tracker without changing hangar, and every job change goes out as a signed `hangar.event/v1` ([plugins](plugins.md)). | Shipped |
| **Deploy on your company's cluster** | Kustomize base and components, a Terraform module that never creates a cluster, released images, secrets from your secret manager and admin configuration as reviewed files (`hangar apply`) ([company deploy](deploy-company.md)). | Shipped |
| **Bots that act for people** | A chat bot's relay token submits work as the linked person who asked, with their rights and audit trail, never more ([relay](clients.md#bots-that-act-for-people-relay)). | Partial |

*Shipped*: on main, on by default or behind a setting. *Shadow*: built, and runs beside real work without changing what people get. *Partial*: built and tested; its wiring is in progress (the relay: `/mcp` doesn't accept relay tokens yet). *Planned*: an open issue.

**Security posture.** Hangar treats every sandbox as hostile. A job from an intake door (a GitHub issue, Jira, GitHub Projects, Slack, an inbound webhook) can fetch its repo but never push: hangar pushes the commit it verified, through the git gateway. Its network is an egress allowlist, the agent has no sudo, and automated work never uses a subscription login. Once an agent has read untrusted content, its PR opens as a draft and a second person, not the requester, must approve it to land. See [the security model](security.md) and [the safe-release upgrade](upgrading.md#the-safe-release-120132).

## Where to go next

<div class="af-tools">
  <a class="af-tool" href="guide/#quickstart"><small>Five minutes</small><b>Quickstart</b><span>Docker and uv on a laptop: <code>hangar up</code>, a credential, a first task, your own environment, then GKE and a team.</span></a>
  <a class="af-tool" href="concepts/"><small>The words</small><b>Concepts</b><span>Environment, harness, sandbox, session and job, and what hangar gives you that a laptop does not.</span></a>
  <a class="af-tool" href="console/"><small>The screens</small><b>The web console</b><span>Needs you, Jobs, Sessions, the Fleet four ways, terminals, and what each action does.</span></a>
</div>
