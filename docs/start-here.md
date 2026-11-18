---
hide:
  - toc
---

# Start here: from the five words to a first task

Four steps take you from hangar's vocabulary to an agent working in a sandbox on your laptop. Take them in order,
then pick the track that matches your job.

<figure class="af-figure af-figure--wide">
--8<-- "start-path.svg"
<figcaption>The first steps: learn the words, run <code>hangar up</code>, give a first task, watch it in the console.</figcaption>
</figure>
<figure class="af-figure af-figure--stacked">
--8<-- "start-path-stacked.svg"
<figcaption>The first steps: learn the words, run <code>hangar up</code>, give a first task, watch it in the console.</figcaption>
</figure>

## The first four steps

<div class="af-tools">
  <a class="af-tool" href="concepts/"><small>Step 1</small><b>Concepts</b><span>Environment, harness, sandbox, session and job: what each is, why it exists and which page has the details.</span></a>
  <a class="af-tool" href="guide/#quickstart"><small>Step 2, five minutes</small><b>Quickstart</b><span>Docker and uv on a laptop. Five commands start the server, log the CLI in, build an environment and store a key.</span></a>
  <a class="af-tool" href="guide/#first-task"><small>Step 3</small><b>First task</b><span>Ask your MCP client, or the CLI, for a sandbox on your repo and have Claude Code explain its structure.</span></a>
  <a class="af-tool" href="console/"><small>Step 4</small><b>The web console</b><span>Needs you, Jobs, Sessions and the Fleet on <code>:8787</code>, with a live terminal for every sandbox.</span></a>
</div>

## Then, by what you do

<div class="af-tools hg-tools--3">
  <a class="af-tool" href="guide/after-the-first-task/"><small>You write code</small><b>After the first task</b><span>Delegate from your own agent, run attempts in parallel, take work from GitHub and write an environment for your repo.</span></a>
  <a class="af-tool" href="clients/"><small>You use an MCP client</small><b>Connecting clients</b><span><code>hangar connect</code> for Claude Code, Codex, Cursor and others; OAuth for clients that can't send a token.</span></a>
  <a class="af-tool" href="mcp/"><small>You build on MCP</small><b>Using MCP</b><span>The two servers, waiting without polling, answering an agent's question, tasks and typed results.</span></a>
  <a class="af-tool" href="team/"><small>You set up a team</small><b>People and credentials</b><span>Add people, link their GitHub, Slack and Jira accounts, add the team's credentials, then check it with <code>hangar doctor</code>.</span></a>
  <a class="af-tool" href="guide/production/"><small>You run the platform</small><b>From a laptop to GKE</b><span>Move the same control plane to GKE with <code>hangar up --gke</code>, then put a team on it.</span></a>
  <a class="af-tool" href="guide/where-it-runs/"><small>You choose the isolation</small><b>Where it runs</b><span>A laptop, your own Linux box, one VM, GKE or E2B: how each isolates a sandbox and what has been proven live.</span></a>
</div>

## Go deeper

| Section | What is in it |
| --- | --- |
| [What hangar gives you](concepts/control-plane.md) | What the control plane holds, and what it gives you that a laptop does not |
| [Guides](guides.md) | One page per task: jobs, sessions, landing, environments, front doors and usage |
| [Reference](reference.md) | The APIs and versioned contracts, the security model and CI |
| [Operations](operations.md) | Deploying on GKE, proving a deploy, runbooks, upgrades and team scale |
