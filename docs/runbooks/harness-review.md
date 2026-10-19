# Runbook: the Friday harness review

Thirty minutes every Friday. The team labels where its own agents' jobs first went wrong. The first eval milestone is
**20 labelled real failures** that become flightdeck's first regression tasks (Sean-Koval/flightdeck#12). After that,
labelling goes on as a habit, but the hand-over has happened.

## Before the meeting (5 minutes, the facilitator)

```sh
hangar review status            # labelled: 13/20 candidate tasks, and labels by category
hangar review queue --since 7d  # what to look at, newest first
```

The queue lists [agent jobs](../metrics.md) from the window that need a look, newest first:

- jobs that failed;
- jobs whose PR was closed without merging (an abandoned PR, with its [abandon reason](../landing.md#what-hangar-records) when someone gave one);
- jobs that took two or more correction steers (once steers are classified, #162).

Jobs already labelled drop out. So do `infra_error` jobs ([jobs/reference.md](../jobs/reference.md#outcome-classes)): infrastructure isn't a
harness failure. `--include-infra` shows them anyway. A member sees their own jobs and an admin sees everyone's, so the
facilitator should use an admin token.

Each row links:

- the job (`/api/jobs/<id>`);
- its trace (`/api/jobs/<id>/trace`, or `hangar trace export <job>`);
- its session's event timeline (`/api/sessions/<session>/events?since=<seq-1>`), opened at the suggested first failure: the first `error` event or failed tool result.

The suggestion is only a starting point. Pick the step where the run actually went wrong.

## The agenda (30 minutes)

| Minutes | What |
| --- | --- |
| 0–2 | `hangar review status`: where the count stands, and which categories dominate |
| 2–25 | Each row, about 3 minutes. Open the timeline at the suggested seq and scroll back until you find the first step where the agent went wrong, then label it. |
| 25–30 | Look at the fixes: one AGENTS.md change, skill, environment fix or ticket-template change to make next week. File it. |

To label a row:

```sh
hangar review label <job> --seq <N> --category env --fix env --note "libpq missing in the image" [--task]
```

- `--seq`: the `hangar.seq` of the first failure (the timeline's `seq`, or `hangar.seq` in the trace).
- `--category`: what went wrong.
  - `env`: the sandbox or environment (a missing tool or service, a wrong version).
  - `tooling`: hangar, the harness or MCP (a tool that broke or lied).
  - `instructions`: AGENTS.md or the prompt led it astray.
  - `skill`: a skill was missing or wrong.
  - `model`: the model's own mistake with good inputs.
  - `ticket`: the ticket was unclear, wrong or too big.
  - `flaky`: a flaky test or service.
  - `other`.
- `--fix`: what would prevent it: `agents_md`, `skill`, `env`, `ticket_template`, or `none`.
- `--task`: mark it a **candidate task**, a failure flightdeck should turn into a regression task. Mark it only when it is reproducible from the base commit and the ticket, and a fix can be checked by tests.
- `--note`: a sentence of what happened. Put disagreements here; there is one label per job.

Relabelling a job replaces its label. The replacement is audited (`review.relabel`, with the old label), and the job's
timeline says who labelled it. `POST /api/jobs/{id}/failure-label` does the same as the CLI. There is no MCP tool.

## When to stop

At 20 candidate tasks, `hangar review status` and the queue print **"20 reached: hand over to flightdeck"**. Tell
flightdeck's owner. The labels are already in the bucket. Labelling keeps working after that.

## Where the labels go

- **The job.** `failure_label` on `GET /api/jobs/<id>`, and `hangar.failure.*` on the trace's `job` span ([traces.md](../api/traces.md#session-and-job)).
- **The outbox.** Each label (and each relabel) is a `label` job event, `v1/jobs/{date}/{job}/events/{ts}-label.json` ([outbox.md](../api/outbox.md#job-events-hangarjob_eventv1)).
  - It carries the label plus references: base and head SHA, repo, ticket ref, PR state, abandon reason, and the check command.
  - It also carries the ticket text, redacted by hangar's broker. Ticket text is untrusted and may hold customer data, so flightdeck's task builder redacts it again.
  - Flightdeck turns `candidate_task` labels into Harbor tasks.
- **Retention.** A labelled job's runs and test results stay until the outbox has exported them ([outbox.md](../api/outbox.md#retention-and-the-export-189)).
