# Tower's events (`POST /api/governance/events`)

Tower pushes approval events and run stops to hangar on one route. This page is the contract for whoever sends them, Tower's outbox today. The design is in [spec 0015 §5 and §12](../specs/0015-governance-decision-point.md#12-events-from-tower).

## Turning it on

The route answers 404 until governance is on. It needs both settings below; a secret named but not stored answers 503.

| Setting | Default | Meaning |
| --- | --- | --- |
| `HANGAR_GOVERNANCE` | `off` | Any kind in `shadow` or `enforce` (here or in `HANGAR_GOVERNANCE_MODES`) turns the route on |
| `HANGAR_GOVERNANCE_WEBHOOK_SECRET` | unset | The vault credential holding the secret Tower signs with: `tower-webhook` on GKE |
| `HANGAR_GOVERNANCE_URL`, `HANGAR_GOVERNANCE_CREDENTIAL` | unset | Where hangar re-reads a decision, and its key for that |
| `HANGAR_GOVERNANCE_SUSPEND_AFTER_SECONDS` | `60` | How long a tool call waits in its turn before the session is suspended |

## The request

Tower sends each event as JSON, signed as its `aw/webhooks.py` signs every delivery:

```
X-AW-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, t + "." + body)>
X-AW-Event-Id:  <the body's id>
```

The MAC covers the exact bytes of the body. Header names match in any case. No hangar token is sent or accepted.

| Check | Refused when | Answer |
| --- | --- | --- |
| Signature | missing, or not this secret's | 401 |
| Freshness | `t` more than 300 s from hangar's clock | 401 |
| Event id | the header names another id than the body | 400 |
| Body | not an event (below) | 400 |
| Replay | an id hangar accepted in the last 48 hours | 200, `{"ignored": "duplicate event"}` |

Each refusal is audited `governance.event_rejected` with its reason. A replay answers 200 so Tower's outbox stops retrying it. Accepted ids are kept in hangar's database, so a restart doesn't forget them.

## The events

| `kind` | Fields hangar reads | What hangar does |
| --- | --- | --- |
| `approval.created`, `approval.decided` | `approval.id`; `approval.decided_by` for display | Re-reads `GET /v1/decisions/{id}` and acts on that answer |
| `run.stop` | `run.session` or `corr.session`, else `run.job` or `corr.job`; `run.run`; `run.stopped_by`; `run.reason` | Interrupts the run and marks the session stopped |

An approval event is a hint. Its payload never decides anything, so events may arrive late, twice or out of order. A "decided" before its "created" is kept for an hour. hangar reads it once the call it decides starts waiting.

When Tower can't answer the re-read, hangar answers 503 and forgets the event id. Tower's retry then counts as new. A suspended session's approval is also re-read every minute, and at start.

A stop for a session hangar doesn't know answers 200 with `{"ignored": "hangar has no such run"}`, audited.

## Suspend and resume

A risky call in an `ask` turn asks Tower first. Once it has waited the suspend time, hangar does four things:

1. It denies the call with "Paused for approval A-9; this turn resumes when it's signed."
2. It interrupts the turn at that call.
3. It checkpoints the session.
4. It releases the sandbox, so the session is suspended and holds no slot.

When Tower allows the call, the session resumes from its checkpoint with a new turn. That turn's driver allows the matching call once. The match is the tool and the hash of its input (`args_hash`, spec 0015 §10). The call's approval records Tower's receipt; anything else asks as usual. A deny leaves the session suspended, with Tower's reason.

The pre-cleared call reaches the driver in a file signed with the run's key. The driver reads it once and deletes it. A file the agent wrote is ignored.

A job's turn isn't suspended: it waits in place up to the driver's own deadline. So does a turn when events aren't configured.

## Stopping a run

`run.stop` goes through hangar's interrupt path, like `hangar session interrupt`. The audit entry `governance.run_stop` names who stopped it. The session records the stop, and Tower's approval for that turn is withdrawn. A decision that arrives later doesn't resume it; hangar audits `governance.resume_refused`. A person may still send the session a new message, which starts a new run.

## A complete event

```json
{"id": "ev-0c1d6b0f1e2a4b7c9d8e7f6a5b4c3d2e", "kind": "run.stop", "at": "2026-10-27T15:02:11.000Z",
 "run": {"session": "ses-77", "run": "run-5d1", "stopped_by": "lee@acme.test", "reason": "wrong repo"},
 "corr": {"source": "hangar", "job": "job-9", "session": "ses-77", "turn": 3}}
```

`tests/fixtures/governance/tower_events.json` holds an event signed by Tower's own code; `tests/test_govevents.py` checks hangar verifies it.
