---
id: 0007
kind: decision
status: proposed          # proposed | accepted | superseded; a person accepts it by merging
label: Use Parquet for run storage
sources:                  # where the decision was made: a Slack permalink, a PR, an issue; and the content's hash
  - https://acme.slack.com/archives/C0123/p1760000000000100
  - thread sha256:3f1c…
author: agent:claude-opus-5-5   # or a person's handle; `@hangar adr` writes agent:<model>
supersedes: []            # ids of the ADRs this one replaces
---

# 0007: Use Parquet for run storage

<!-- The ADR format `@hangar adr` writes (docs/slack.md#hangar-adr-a-thread-becomes-a-decision-record). Copy it to
     docs/decisions/0000-template.md (or keep your own) and create docs/decisions/ to opt a repo in. One decision per
     file, named NNNN-<slug>.md, numbered in order. -->

## Context

What forces the decision: the problem, the constraints and the facts, with who raised them.

## Decision

What was decided, in one or two paragraphs.

## Options considered

Each option that was raised, with its trade-offs.

## Consequences

What becomes easier or harder, the follow-up work, and what would make the team revisit this.

## Checks

Optional predicates over agent traces that should hold once this is accepted. Flightdeck runs them nightly
(Sean-Koval/flightdeck#15); hangar only checks that the block is valid YAML, and a later PR that removes or changes a
line here is risk `high` in hangar's review packet. Write "None." when nothing can be checked this way.

```yaml
checks:
  - id: 0007-1
    predicate: "no pr.merge unless actor == hangar-landing"
```
