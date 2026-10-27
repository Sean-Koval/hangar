# Governed approvals

How does hangar ask Tower before a risky call, a blocked host or a missing secret? This page is for whoever turns governance on. The contract is [spec 0015](specs/0015-governance-decision-point.md), and [Tower beside hangar](runbooks/tower.md) covers the settings.

## What asks Tower

| What | When | Decision kind |
| --- | --- | --- |
| A tool call | The turn runs under `approvals: governed`, and the `deny-risky` rules or the taint gate flag the call | `tool.call` |
| An egress request | The agent asks for a blocked host (`hangar-agent env request egress`), under any policy | `egress.request` |
| A secret request | The agent asks for a missing secret (`hangar-agent env request secret`), under any policy | `secret.request` |

Under `governed`, a call the rules don't flag runs at once and never reaches Tower. That covers a plain `Read`, an edit in the repository and a test run. A flagged call waits in the driver, as under `ask`. hangar then asks Tower with the call's `args_hash`, the run's ids and the taint flag.

## The modes

`HANGAR_GOVERNANCE` sets the mode for every kind, and `HANGAR_GOVERNANCE_MODES` overrides it per kind, for example `tool.call=enforce,egress.request=shadow`.

| Mode | Who decides | What Tower sees |
| --- | --- | --- |
| `off` (default) | A person, as under `ask`. Requests wait for their owner | Nothing |
| `shadow` | A person, as under `off` | A dry run of the same question |
| `enforce` | Tower | The question; its answer is final |

In `shadow`, both answers are kept on the approval or request as `governance`. Each comparison is audited `governance.shadow`, and a different answer also `governance.disagree`. The metric `hangar_governance_disagreements_total` counts those, per kind.

In `enforce`, Tower's `allow` comes with a receipt, kept on the approval or request. A `deny` refuses the call with Tower's reason, and the agent carries on without it.

## When Tower makes you wait

A `wait` answer names a Tower approval. For a tool call, hangar follows it with [Tower's events](api/governance-events.md). After `HANGAR_GOVERNANCE_SUSPEND_AFTER_SECONDS` the session is suspended and its sandbox released. Once the approval is signed, the session resumes and the call runs once.

A request stays pending with the approval's id. An `approval.decided` event settles it, and so does a status read every 60 seconds.

## When Tower can't answer

Nothing that goes wrong is ever an allow. In `enforce`:

- A flagged call is denied at once, with "approval service unavailable" and the reason. The turn goes on.
- A request stays pending. Its session gets one event per outage, and hangar asks again every 60 seconds.
- Each refusal logs `hangar.slo: event=fail_closed` with the kind and the failure. [Tower down](runbooks/tower-down.md) is the runbook.

A missing `HANGAR_GOVERNANCE_URL` or credential counts as unreachable. In `shadow`, a failure changes nothing: a person decides, and the record says hangar fell back to its own policy.

## Answering in hangar

In `enforce`, a person who allows a flagged call or a request in hangar signs Tower's approval. Tower judges the signature: it may allow, ask for more signers, or refuse, for example your own work. hangar shows its reason. `allow_always` is refused, because Tower decides each call once. A deny always goes through, and hangar withdraws Tower's approval.

## On GKE

`HANGAR_UID_SPLIT=required` refuses a `governed` turn wherever the driver can't run as a user of its own, as it refuses `ask`. Tower deciding doesn't stop an agent that shares the driver's user from forging the driver's answer. Until [the driver sidecar](security/driver-sidecar.md) is wired, that means GKE.
