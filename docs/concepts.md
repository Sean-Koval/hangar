# Concepts

The words these docs use: what each thing is, why it exists, and which page has the details. [What hangar gives you](concepts/control-plane.md) shows what is inside the control plane.

<figure class="af-figure af-figure--wide">
--8<-- "concepts-map.svg"
<figcaption>People and their tools, the front doors, the control plane and the sandboxes it runs</figcaption>
</figure>
<figure class="af-figure af-figure--stacked">
--8<-- "concepts-map-stacked.svg"
<figcaption>People and their tools, the front doors, the control plane and the sandboxes it runs</figcaption>
</figure>

## Environment

A reusable recipe for a workspace. It names a base image, system packages and runtimes, dependencies, skills and agent
instructions, MCP servers, credentials, resources, and optionally the repo. Specs are YAML files that `extend` each
other (`bare` → `python` → yours). The image tag is content-addressed, so an unchanged spec never rebuilds.

A repo can also describe its own install, start and verify steps in a committed `.hangar/environment.yaml`, which
hangar builds ahead of time. See [environments](environments.md).

## Harness

Everything that shapes the agent but the model: the pinned CLI, instructions, skills, plugins, MCP bindings, hooks and
approvals. A team freezes one into a harness directory (native files plus `harness.lock`).
Its identity, `harness_digest`, is a SHA-256 over the lock's identity fields, and matches the manifest's. See
[harness locks](harness-lock.md).

The CLI is Claude Code, Codex, pi or Antigravity. An environment lists the CLIs it allows; you pick one per sandbox,
not per image. Each ships once as a read-only *harness pack* mounted at start. Its version is the one the CLI pin file
(`src/hangar/harnesses.lock.yaml`) names. Its model comes from a policy (`balanced`, `frontier`) or a pinned id.
See [harnesses](harnesses.md).

## Sandbox

A running copy of an environment, with its own files, tools, repo checkout and agent configuration. Sandboxes start
from warm pools in about a second, and can be snapshotted and forked into parallel attempts.

They run on Docker (a laptop or one VM, optionally with gVisor or Kata), GKE (Agent Sandbox with gVisor or Kata) or
E2B. hangar runs commands in them through a small daemon, [hangar-envd](envd.md). See
[where it runs](guide/where-it-runs.md).

## Session

A resumable stream of work: an agent's conversation plus the repo's state, checkpointed after every turn. Days later,
a follow-up boots a fresh sandbox from the checkpoint, and the agent continues the conversation. You can
[steer](steering.md) a running turn, [stream](streaming.md) its output, and pass files and results in and out through
[context exchange](context-exchange.md). See [sessions](sessions.md).

## Job

Work handed to hangar, which runs it to a result. An agent turn runs in a session, then the repo's own check
(`verify`), then hangar opens a branch and a draft PR.

Jobs take follow-ups, fix their own failing CI and review comments, run best-of-N attempts, and carry budgets. A job's
outcome class says whether the agent, a person or the infrastructure ended it. See [jobs](jobs/README.md).

## Front doors

Every way work reaches hangar: MCP clients, the CLI and REST; GitHub issues and `/hangar` comments; Jira; Slack,
signed webhooks and schedules.

Each becomes a job or a session. Work from outside (an issue, a ticket, a thread) is an *intake job* with stricter
defaults. See [intake](intake.md), [Slack](slack.md) and [triggers](triggers.md).

## Repo policy

One record per repository with the defaults every front door applies. It names the check, the environment, harness
and model, budgets and a daily cap, egress additions, and how PRs land. See [repo policy](repo-policy.md);
`hangar repo check` reports whether a repo is ready for agent work.

## Landing

With landing on, an approval lands the PR. Agent PRs come from a GitHub App. hangar arms auto-merge for exactly the
approved commit once the required checks pass, and GitHub's branch protection stays the gate. Each PR gets a review
packet, and one inbox lists what waits on each person. See [landing](landing.md).

## People and credentials

Every token belongs to a person, an admin or a member; members see and act on their own work. Agent logins are stored
encrypted and injected at claim time: the person's own API key, else the team's.

Subscription logins (Claude Pro or Max, ChatGPT) are for a person's own interactive sessions only; automated work runs
on API keys. Approvals follow one rule: an admin or a repo approver, never your own job. See [team setup](team.md) and
[team scale](team-scale.md).

## The trust boundary

hangar assumes the sandbox is hostile. Git credentials stay behind the [git gateway](security/git-gateway.md): job
sandboxes only fetch, and hangar pushes what it verified. [Egress allowlists](security/egress.md) limit where a
sandbox can connect. Intake jobs get both by default ([intake posture](security/front-doors.md#intake-posture)).

A turn that read untrusted content is [tainted](security/taint.md). Its push, PR, landing and egress then need a
second person. See the [security model](security/README.md).

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="what-hangar-emits"></span>What hangar emits | [What hangar gives you: what it emits](concepts/control-plane.md#what-hangar-emits) |
| <span id="inside-the-control-plane"></span>Inside the control plane | [What hangar gives you](concepts/control-plane.md#inside-the-control-plane) |
| <span id="what-you-get"></span>What you get | [What hangar gives you: what you get](concepts/control-plane.md#what-you-get) |
