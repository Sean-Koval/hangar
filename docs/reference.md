---
hide:
  - toc
---

# Reference: the contracts, the security model and CI

Pages you look things up in, not read through. Each API page is one versioned contract. The security model says what
a compromised sandbox gets, mechanism by mechanism, and names the test that proves it.

## Versioned contracts

Other tools read these without asking hangar anything. The schema name is in each payload.

<div class="hg-stack" markdown>

| Contract | Schema | Who reads it |
| --- | --- | --- |
| [Trace export](api/traces.md) | `hangar.trace/v1` | An OTLP collector, Jaeger, flightdeck's trace timelines |
| [Run capture, the outbox](api/outbox.md) | `hangar.run/v1` | flightdeck, from a bucket, while the sandbox pools are off |
| [Job events](api/outbox.md#job-events-hangarjob_eventv1) | `hangar.job_event/v1` | flightdeck: [what a merge kept](api/outcome-labels.md) of the agent's work |
| [Trials and pin sets](api/trials.md) | `hangar.pinset/v1` | flightdeck, which owns the sampling policy; hangar runs each trial as pinned |
| [Events out](plugins.md#events) | `hangar.event/v1` | Your own services: every job change, signed |

</div>

## APIs

<div class="af-tools hg-tools--3">
  <a class="af-tool" href="api/fleet/"><small>GET /api/fleet</small><b>The Fleet API</b><span>One joined view of every agent, what each is doing now and the caller's inbox, with a stream of the same.</span></a>
  <a class="af-tool" href="envd/"><small>In every sandbox</small><b>hangar-envd</b><span>The daemon that runs commands, file transfers and terminals in a sandbox.</span></a>
  <a class="af-tool" href="api/governance-events/"><small>POST /api/governance/events</small><b>Tower's events</b><span>The one route Tower pushes approval events and run stops to.</span></a>
  <a class="af-tool" href="api/trace-import/"><small>hangar trace import</small><b>Importing a session</b><span>Turn a Claude Code session hangar didn't run into hangar's own records.</span></a>
  <a class="af-tool" href="model-gateway/"><small>Record mode</small><b>Model gateway and tapes</b><span>A sandbox's harness calls its model provider through hangar, which keeps what the model answered.</span></a>
  <a class="af-tool" href="jobs/reference/"><small>Jobs</small><b>Labels, outcomes, settings</b><span>Failure labels, outcome classes and every job setting, one entry each.</span></a>
</div>

## The security model

hangar assumes the agent inside a sandbox can take it over completely. Start with the map, then the page for your
question.

<div class="af-tools hg-tools--3">
  <a class="af-tool" href="security/"><small>The map</small><b>Security model</b><span>The five boundaries, who may do what, and a row for every mechanism's own page.</span></a>
  <a class="af-tool" href="security/compromised-sandbox/"><small>The short answer</small><b>What a compromised sandbox does not get</b><span>The six things that stay out of an attacker's reach.</span></a>
  <a class="af-tool" href="security/threat-model/"><small>Goal by goal</small><b>The threat model</b><span>What one compromised sandbox should not get, what stops it and which test proves it.</span></a>
  <a class="af-tool" href="security/review/"><small>For a sign-off</small><b>Reviewing hangar for production</b><span>For the SRE or security engineer: what hangar stores, each control with its evidence, and what is open.</span></a>
  <a class="af-tool" href="security/not-covered/"><small>The gaps</small><b>What is not covered</b><span>Stated plainly, so nobody relies on a guarantee hangar doesn't make.</span></a>
  <a class="af-tool" href="security/operations/"><small>For operators</small><b>Running hangar safely</b><span>Where the control plane lives, how tokens are split and where the vault key is kept.</span></a>
</div>

## CI

| Page | What it answers |
| --- | --- |
| [CI](ci.md) | What a pull request's fast check runs, and what main runs after the merge |
| [Verify in a job](ci-verify.md) | The check a hangar job runs on this repo, in a few minutes on a 2-vCPU sandbox |

## Next

The reasoning behind these contracts is in the [design notes](specs/README.md). To use them from a client, start
with [using MCP](mcp.md).
