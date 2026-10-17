# 0015: The governance decision point

Status: partly built (2026-10-17): the contract, the client skeleton and contract tests (spike #171, #208); the workspace side is proposed. · Date: 2026-10-16 · Builds on: 0002 (landing), 0011 (triggers and approvals), 0014 (manifest digests), #121 (one approval rule), #123 (intake posture), #129/#130 (taint and its gates) · Counterparts: [governed-agent-workspace](https://github.com/Sean-Koval/governed-agent-workspace) #13 (decision API), #14 (passports), #16 (correlation), #17 (webhooks), #18 (receipts), #19 (approver ≠ requester), #20 (reconciliation), #22 (per-run stop)

> Part of a set. Ownership, cross-spec decisions and the combined build order are in [docs/specs/README.md](README.md); where this spec conflicts with that page, the page wins.

## 1. The split

Hangar is the **Run plane**: it holds sandboxes, credentials, sessions, jobs and landing, and it *enforces*. The governed workspace is the **Govern plane**: one policy decision point (Cedar), approvals and their inbox, passports and hash-chained receipts, and it *decides*. Hangar's approvals ask the workspace instead of hangar keeping a second approval system.

The workspace decides only **whether a person or a policy allows something**. Everything about *how* it happens stays in hangar:

| Stays in hangar | Moves to the workspace (in `enforce`) |
| --- | --- |
| Landing mechanics: arming and disarming auto-merge, SHA pinning, the stale-gate, `push_mode`, branch protection (docs/landing.md §4) | Whether an approval of this head lands it (who may approve, how many, self-approval rules) |
| Budgets, fairness, the queue, the power schedule | Whether an untrusted job may run |
| Egress enforcement (proxy, NetworkPolicy), the git gateway, intake posture (#123) | Whether a blocked host or a missing secret may be granted |
| Taint tracking and which calls are risky (`driver.risky`, `taint_sink`) | Whether a risky tool call in an `ask` turn may run |
| Hangar's local self-approval check (#121), always run first | Approver ≠ requester across platforms (gaw#19), signatures, receipts |

Two rules follow. **The workspace never makes hangar more permissive than hangar's own enforcement**: an `allow` lets hangar proceed with its own checks; it never skips them. **Nothing that goes wrong on the way is ever an allow** (§6).

## 2. The five decisions

| Kind (hangar) | Wire kind | Today in hangar | Asked when | Hangar's deadline | Idempotency key | Workspace tool (gaw#13) |
| --- | --- | --- | --- | --- | --- | --- |
| `job.release` | `hangar.job.release` | `approvals.decide_now` (#121), reached from REST, MCP `approve_job`, GitHub and Jira `/hangar approve` | the job enters `awaiting_approval` (opens the approval), and each time a person acts on it in hangar (a signature) | `JobApproval.expires_at` (`HANGAR_JOB_APPROVAL_TTL_HOURS`) | `hangar:job:<job>:release` | `release_job`, resource `Job::"<repo>"` |
| `tool.call` | `hangar.tool.call` | the driver's `ask` approvals (`driver.Driver.request`, `steer.answer`), only calls `driver.risky` or `taint_sink` flag | the driver writes an approval request | the driver's `expires_at` (`approval_timeout_seconds`, default 600) | `hangar:session:<session>:tool:<driver approval id>` | `run_command`, `edit_file`, `fetch_url`, `use_tool` |
| `landing.approve` | `hangar.landing.approve` | `Landing._approved` → `qualifies` (policy approvers, code owners, the taint gate) | an approving review on the PR's head | none (the workspace's ttl, 3 days) | `hangar:job:<job>:landing:<head sha>` | `merge_pull_request` |
| `egress.request` | `hangar.egress.request` | `envrequests.decide` (egress), `egressapi.on_decided` | the agent files a request | the request's own expiry | `hangar:session:<session>:egress:<request id>` | `egress_request`, resource `Host::"<host>"` |
| `secret.request` | `hangar.secret.request` | `envrequests.decide` (secret) | the agent files a request | the request's own expiry | `hangar:session:<session>:secret:<request id>` | new: `secret_request`, resource `Secret::"<environment>/<name>"` |

Tool calls only reach the workspace under `approvals: ask` and only for the calls hangar's rules flag; `auto` turns and unflagged calls never leave hangar, which keeps the latency budget (§5) off the common path.

## 3. The client interface

`src/hangar/governance.py` (built in this spike, not yet wired):

```python
class DecisionPoint(Protocol):
    async def decide(self, req: DecisionRequest) -> Decision: ...
    async def status(self, approval_id: str, *, kind: Kind) -> Decision: ...
    async def withdraw(self, approval_id: str, reason: str) -> bool: ...

class Decision(BaseModel):
    outcome: Literal["allow", "deny", "wait"]
    reason: str; approval_id: str | None; receipt: str | None; expires_at: float | None
    status: str | None          # the workspace's approval state, or "unavailable" / "local"
    rule: str | None
    source: Literal["workspace", "local", "fail_closed"]
    failure: str | None         # why it failed closed: timeout, server_error, auth, malformed, ...
    retryable: bool
```

| Piece | What it does |
| --- | --- |
| `DecisionRequest` | kind, idempotency key, requester, optional signer, subject, action, context, correlation, deadline, `dry_run`; `to_wire()` / `from_wire()` for `hangar.decision/v1` |
| `LocalDecisionPoint(ctx)` | Today's rules: without a signer every kind waits for a person; with one, `approvals.can_decide` for release, landing and egress. No receipts. The source for `off`, `shadow` and break-glass |
| `HttpDecisionPoint(url, credential)` | `POST /v1/decisions`, `GET /v1/decisions/{id}`, `POST /v1/decisions/{id}/withdraw`; 2 s connect, 5 s total; one retry on 401 with a fresh token; a circuit breaker; the fail-closed table |
| `GovernanceRouter(local, remote, modes)` | What approval paths call: per kind `off`, `shadow` or `enforce`, plus break-glass |
| `wait_for(dp, decision, kind, deadline)` | Follows a `wait` with backoff (2 s doubling to 60 s, jittered) or until the event receiver wakes it; fails closed at the deadline |
| `verify_event(secret, headers, body, seen)` | Checks a pushed event's signature, freshness and id (§12) |
| `args_hash`, `idempotency_key` | The shared hash (identical to the workspace's `aw.store.args_hash`) and the key formats of §4.3 |

The protocol differs from the issue's sketch in two places: `status` takes the `kind` (so a failure falls back to that kind's row of §6), and `withdraw` exists (§4.5). The request's `requested_by` became `requester` (whose work it is) plus `signer` (who acted), because "the approver" and "the requester" are different people and the workspace must tell them apart (§4.4).

## 4. Wire contract: `hangar.decision/v1`

JSON over HTTPS, snake_case, times in RFC 3339 (`Z` or an offset). Within `v1` either side only adds optional fields; anything removed or renamed is `v2`.

### 4.1 Request

`POST {workspace}/v1/decisions`, headers `Authorization: Bearer <credential>` (§9), `Idempotency-Key: <key>` (same as the body's), `traceparent` (§10).

| Field | Required | Meaning |
| --- | --- | --- |
| `v` | yes | `"hangar.decision/v1"` |
| `kind` | yes | one of the five wire kinds |
| `idempotency_key` | yes | §4.3 |
| `requester` | no | whose work it is: `{person, email, external}`. `person` is hangar's id, `email` a verified address from the person's linked identity (the workspace maps it to its user), `external` a front door's stable id (`github:<numeric id>`, `jira:<account>`, `webhook:<source>`). Empty for a team job. Accepted only from an agent of kind `hangar` |
| `signer` | no | a person who acted at a hangar surface: the same fields plus `via` (`mcp`, `rest`, `github`, `jira`, `slack`) and `verdict` (`approve`, `reject`). §4.4 |
| `subject` | no | `job`, `session`, `turn`, `run`, `repo` (`owner/name`), `environment`, `pr`, `base_sha`, `head_sha`, `files` (≤ 300), `author` (landing: whose work the PR is), `harness_digest`, `environment_digest`, `model` (concrete id) |
| `action` | no | `tool`, `args_hash` (lowercase hex SHA-256 of the harness's input as canonical JSON), `detail` (what a person is shown: redacted, ≤ 2000 characters, both ends kept), `risky` (hangar's reason), `host`, `secret` (a name, never a value), `scope` (`session`, `environment`) |
| `context` | no | `untrusted_source`, `tainted`, `risk` (`low`, `normal`, `high`: landing.risk), `packet` (`docs`, `tests`, `code`), `ai_review` (`{url, sha, harness, model, verdict}`), `checks` (≤ 50 `{name, conclusion, sha, trusted}`) |
| `correlation` | no | `traceparent`, `job`, `session`, `turn`, `run`, `tool_call_id` (§10) |
| `deadline` | no | hangar's own timeout; the approval's `expires_at` is never later |
| `dry_run` | no | shadow: evaluate the rules only; no approval, no cards, no receipt (§7) |

<!-- contract: request -->
```json
{
  "v": "hangar.decision/v1",
  "kind": "hangar.job.release",
  "idempotency_key": "hangar:job:j-4143a542:release",
  "requester": {"external": "github:9912345"},
  "subject": {"job": "j-4143a542", "repo": "acme/api", "environment": "hangar-jobs"},
  "action": {"detail": "Fix the flaky retry test in tests/test_client.py"},
  "context": {"untrusted_source": "github:9912345", "tainted": true},
  "correlation": {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", "job": "j-4143a542"},
  "deadline": "2026-10-17T14:05:00Z"
}
```

A tool call relayed from an `ask` turn:

<!-- contract: request -->
```json
{
  "v": "hangar.decision/v1",
  "kind": "hangar.tool.call",
  "idempotency_key": "hangar:session:s-77:tool:ap-1a2b3c4d5e6f",
  "requester": {"person": "p-12", "email": "dev@acme.test"},
  "subject": {"job": "j-9", "session": "s-77", "turn": 3, "run": "r-5d1", "repo": "acme/api",
              "environment": "hangar-jobs", "harness_digest": "sha256:9f2c", "model": "claude-opus-4-5-20251101"},
  "action": {"tool": "Bash", "args_hash": "0f40a5cddf23051ff00817f5a7674212fa48257ff339c86f0fbedfc2503e88f7",
             "detail": "git push origin HEAD:main", "risky": "git push to a protected branch"},
  "context": {"tainted": true},
  "correlation": {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-a1b2c3d4e5f60718-01", "job": "j-9",
                  "session": "s-77", "turn": 3, "run": "r-5d1", "tool_call_id": "toolu_01ABC"},
  "deadline": "2026-10-16T15:10:00Z"
}
```

### 4.2 Response

`200` with:

| Field | Meaning |
| --- | --- |
| `decision` | `allow`, `deny` or `wait` |
| `approval` | the approval id; required with `wait` |
| `receipt` | the receipt id; **required with `allow`** (except dry runs). An allow without one is malformed and fails closed |
| `expires_at` | when the approval lapses |
| `status` | the approval's state: `allowed` (no person needed), `approved`, `blocked`, `waiting`, `denied`, `expired`, `withdrawn` |
| `rule` | the Cedar rule that decided |
| `reason` | one line a person can read; hangar posts it where the request came from (redacted) |
| `poll` | `/v1/decisions/<approval>` |

<!-- contract: response -->
```json
{"v": "hangar.decision/v1", "decision": "wait", "approval": "A-123", "expires_at": "2026-10-17T14:05:00Z",
 "status": "waiting", "rule": "release-untrusted-job",
 "reason": "Waiting for Dana or Lee (maintainers) to sign A-123: an untrusted job needs a maintainer.",
 "poll": "/v1/decisions/A-123"}
```

<!-- contract: response -->
```json
{"v": "hangar.decision/v1", "decision": "allow", "approval": "A-123", "receipt": "RC-104", "status": "approved",
 "rule": "release-untrusted-job", "reason": "Signed by Dana (A-123)."}
```

How hangar reads `deny`: `status` `blocked` or `denied` is the policy or a person saying no (a job is rejected with the reason, a tool call denied, a landing not armed, a request denied). `expired` and `withdrawn` mean nobody decided: hangar treats them like its own expiry (the job's `expire_approvals` path, a landing waiting for the next approval) rather than a rejection.

HTTP status codes and what hangar does:

| Code | Hangar |
| --- | --- |
| 200 | the decision, if well formed; otherwise fail closed (`malformed`) |
| 400, other 4xx | fail closed (`contract`), not retried, audited `governance.contract_error`: a bug on one side |
| 401 | one retry with a fresh token, then fail closed (`auth`), not retried; `hangar doctor` reports it |
| 403 | fail closed (`auth`) |
| 404 on `GET /v1/decisions/{id}` | the workspace lost or never had it: ask `decide` again with the same key |
| 409, 410 on withdraw | already closed: done |
| 429 | fail closed (`rate_limited`), retried with backoff |
| 5xx, **whatever the body** | fail closed (`server_error`). The workspace answers 503 `{"decision": "deny"}` on an internal error; hangar reads that as *unavailable*, not as a policy denial, so a workspace bug can't reject a job |

### 4.3 Idempotency

One approval per thing a person decides on, keyed as in §2. The rules:

1. The same key returns the same approval while it waits.
2. A settled `deny` (`blocked`, `denied`) keeps returning that deny for the approval's ttl: an agent retrying a declined call is told no again.
3. `job.release` and `landing.approve` keep returning their settled `allow` (with its receipt) for the ttl, so hangar can replay after a restart.
4. A `tool.call` allow is **single use** (the workspace's `claim_clearance`): the first answer that carries it consumes it, and the key is spent. A retry of the same HTTP request after a lost response gets the same answer (the workspace keys its response cache on the idempotency key for 10 minutes).
5. `expired` and `withdrawn` approvals don't bind the key: the next request opens a new approval.

The key never includes anything a caller controls beyond hangar's own ids. A new PR head is a new landing key, and hangar withdraws the old one.

### 4.4 Signatures from hangar surfaces

A person can act in hangar (`/hangar approve` on GitHub or Jira, `hangar job approve`, MCP `approve_job`, `answer_approval`, a PR review) or in the workspace (Slack, Teams, the web inbox). Both reach the same approval:

- **In the workspace**: the workspace records the signature itself and pushes `approval.decided` (§12).
- **In hangar**: hangar first runs its own local check (`approvals.can_decide`: never your own work, #121), then sends `decide` with the same idempotency key and a `signer`. The workspace counts it as a signature only if its own rules let that person sign: in the approver group, not the requester (gaw#19, across every platform id), the self-approval annotation for low-risk packets, multi-signature counts. It records `proof: "hangar:<via>"`. The answer is `allow` (rule satisfied, with receipt), `wait` (needs more signatures), or `deny` with `status: denied` and the reason when the signer may not sign or `verdict` was `reject`.

A signer the workspace can't map to a user (no verified email) is not a signature: the answer is `wait` with a reason saying so, and hangar tells the person to sign in the workspace.

### 4.5 Status and withdraw

`GET /v1/decisions/{approval}` returns the §4.2 shape, for approvals of the calling agent only (anything else is 404). It is the source of truth: hangar acts on it, never on an event's payload (§12).

`POST /v1/decisions/{approval}/withdraw` with `{"v": "hangar.decision/v1", "reason": "..."}` closes a waiting approval nobody needs any more: hangar expired the job (`expire_approvals`), the job was cancelled, a push replaced the PR's head, the turn ended before an answer. The workspace closes its cards and writes a `withdrawn` receipt (its existing `Governor.withdraw`).

### 4.6 Dry run

`dry_run: true` evaluates the rules for shadow mode (§7) and answers what it would decide: `decision`, `rule`, `reason`, and no approval, cards or receipt. It doesn't count toward the agent's hourly budget.

## 5. Timeouts and waiting

| Budget | Value | Why |
| --- | --- | --- |
| connect | 2 s | the workspace may run outside the cluster |
| decide (total, per request) | 5 s | under every caller's own deadline; a tool call's driver waits up to 600 s |
| status polling | 2 s, doubling to 60 s, ±20 % jitter, until the deadline | a fallback: events normally wake the waiter first |
| circuit breaker | opens after 5 unavailable answers in 30 s, for 30 s | an outage fails 60 sessions closed at once instead of each spending 7 s |

**No call blocks while a person decides.** `decide` answers `wait` at once; hangar keeps the thing pending in its own state and follows the approval:

- `job.release`: the job stays `awaiting_approval` with `job.approval.workspace_approval_id` (#172). An `approval.decided` event, or the 60 s poll of waiting jobs, re-reads the status; `allow` queues the job, `deny` rejects it.
- `landing.approve`: `landing.note` says who must sign; the landing is re-evaluated on the event, the next push, or the poll.
- `egress.request`, `secret.request`: the request stays pending with the approval id; the owner sees where to sign.
- `tool.call`: the turn's pending approval shows the workspace approval id and link. Then:
  1. With events on and a harness that can checkpoint, once the call has waited `suspend_after` (60 s; #190 owns the value) hangar denies it with "Paused for approval A-9; this turn resumes when it's signed", lets the turn end at that tool boundary, checkpoints, and releases the sandbox. The session is `awaiting_approval`.
  2. On `approval.decided` hangar re-reads the status. `allow` resumes the session from its checkpoint and **pre-clears** that one call in the driver: hangar passes `(tool, args_hash, receipt)`, and the driver allows the first matching call once, records the receipt on the approval, and asks as usual for anything else. The resumed turn doesn't ask the workspace again, so the workspace's run id changing with the turn number doesn't matter. `deny` leaves the session idle with the reason as an event (#190).
  3. Without events, or for a harness that can't checkpoint, the turn waits as `ask` turns do today, up to the driver's own deadline, then the driver denies. This replaces the hook's fixed 540 s poll for hangar sandboxes; it isn't another one.

## 6. Fail closed

What each kind does when the workspace can't answer. `retryable` failures are retried in the background (the 60 s poll for pending things; within the driver's deadline for a tool call); none of them ever becomes an allow.

| Condition | `job.release` | `tool.call` | `landing.approve` | `egress.request`, `secret.request` |
| --- | --- | --- | --- | --- |
| unreachable, connect timeout, no answer in 5 s | stays `awaiting_approval`; the issue or ticket gets "approval service unavailable" once per outage | denied with "approval service unavailable"; asked again while the driver's deadline allows, then the driver's timeout denial | not armed; `landing.note` = "approval service unavailable" | stays pending; the owner is told once |
| 5xx (including 503 `deny`), 429 | same | same | same | same |
| 401 after refresh, 403 | same, and `hangar doctor` fails the `governance` check; not retried until the credential changes | denied; not retried | not armed | stays pending |
| 400, other 4xx, malformed answer, allow without receipt | same; audited `governance.contract_error`; not retried | denied | not armed | stays pending |
| circuit open | same, without a network call | denied at once | not armed | stays pending |
| `wait` past hangar's deadline | `expire_approvals` drops the job as today, and hangar withdraws the approval | the driver denies as today; hangar withdraws | stays approved-but-unarmed until a new approval | expires as today; hangar withdraws |
| unknown approval (404 on status) | `decide` again with the same key | same | same | same |

Self-approval is refused locally before any of this (#121, #160): the workspace being down never turns a refused self-approval into a pending one.

## 7. Modes and break-glass

`HANGAR_GOVERNANCE=off|shadow|enforce` sets the default; `HANGAR_GOVERNANCE_MODES` overrides per kind (`tool.call=shadow,landing.approve=enforce` or JSON). The default is `off`.

| Mode | Who decides | The workspace |
| --- | --- | --- |
| `off` | hangar's local rules, as today | not asked |
| `shadow` | hangar's local rules | asked the same question as a dry run, in the background, never waited for; a different outcome is audited `governance.disagree` (with both rules and reasons) and counted in `hangar_governance_disagreements_total{kind}`. `HANGAR_GOVERNANCE_SHADOW_SAMPLE` samples tool calls (default 1.0 for the rest); at most 8 shadow requests in flight, extra ones are dropped rather than queued |
| `enforce` | the workspace, failing closed (§6) | asked; its allow (with receipt) is required |

A disagreement is any different `outcome`. Local `allow` against remote `wait` ("the workspace would have asked someone") is the common one while policies are tuned.

**Break-glass.** `hangar governance local --until <time> --reason <text>`, by an admin, confirmed by a second admin, at most 24 hours, audited `governance.break_glass` (start, confirm, end). While active, kinds in `enforce` are decided by the local rules; each such decision is queued and replayed to the workspace as a record once it answers again (an open item for the workspace, §16), so its receipts have no gap. Break-glass never applies to `off` or `shadow` kinds (they're local already) and ends on its own.

Hangar refuses to start with `shadow` or `enforce` and no `HANGAR_GOVERNANCE_URL`, and `hangar doctor` checks the URL, the credential and the webhook secret.

## 8. Offline and cluster down

| Situation | Behaviour |
| --- | --- |
| Overnight power-down (sandbox pools off 01:00–10:00 ET; control plane and workspace up) | Job release and landing decisions work as by day; released jobs queue for 10:00. No tool calls run. Events for suspended sessions are applied when they arrive; the resumed turns start at power-up |
| Workspace down | §6. Pending things stay pending; nothing is allowed; break-glass if it lasts |
| Hangar control plane down | The workspace retries webhook deliveries for 24 h (gaw#17's outbox). On start hangar re-reads the status of every pending item (jobs awaiting approval, unarmed landings, pending requests, suspended sessions) before taking new work |
| The workspace can't reach hangar (webhooks fail), hangar can reach it | Polling (§5) settles everything; slower, not wrong |
| Both down | Nothing moves; both catch up from their own state |
| Laptop or single-machine install with no workspace | `off` (the default): `LocalDecisionPoint` only |
| A workspace answer arrives after hangar gave up (deadline passed, job dropped) | Hangar withdraws the approval if it is still waiting; an `allow` for something already dropped is recorded and ignored |

## 9. Identity

**Control-plane calls** (every kind in §2 is asked by hangar's control plane, not by a sandbox):

1. **Phase 1, a service key.** `HANGAR_GOVERNANCE_CREDENTIAL` names a secret in hangar's vault; the workspace stores its hash for an agent of kind `hangar`, scoped to `/v1/decisions*` (gaw#13). It can assert any requester and signer, so it is as powerful as hangar's admin token and is rotated like one.
2. **Phase 2, a passport** (#174, gaw#14). Hangar mints itself a token (`iss` = hangar's OIDC issuer, `sub: hangar:control-plane`, `aud` = the workspace's `/oauth/token`, 5 minutes; a new `CloudCreds` method, since `mint` needs a sandbox today), exchanges it (RFC 8693) for a 15-minute passport, caches it until 60 s before expiry, and re-exchanges on a 401 (`Credential.token(fresh=True)`). The workspace trusts that issuer for agent `hangar` with `claims: {sub: "hangar:control-plane"}`. The service key goes.

The requester and signer still come from the body, signed by hangar's TLS-authenticated identity; they are accepted only from the `hangar` agent.

**Sandbox calls** are a different path: an environment with a `governance:` block installs a hangar-owned Claude Code hook that exchanges the sandbox's token (`sub: hangar:env:<env>`) for a passport naming the person, and calls `/hooks/claude-code` directly (#174, #175, gaw#14). **A turn uses exactly one path.** With the managed hook, the workspace sees every call through `/hooks`, and hangar's driver answers only from its local `deny-risky` rules as a guard rail, never relaying to the workspace as well; without it, the driver relays flagged `ask` calls as `tool.call` (§5). Otherwise one call would raise two approvals.

## 10. Correlation

Every request carries hangar's ids and the W3C trace context of the span that asked:

- the `traceparent` header and `correlation.traceparent`: for a tool call, the turn's `invoke_agent` span (the same `TRACEPARENT` the agent runs with, `fleet.py`); for a release or landing, the job's span;
- `correlation`: `job`, `session`, `turn`, `run` (hangar's run id), `tool_call_id` (the harness's id for the call: Claude Code's `tool_use_id`).

The workspace stores them on its run and receipts (`corr`, gaw#16; its run id becomes `R-h-<session>-<turn>`, or `R-h-job-<job>` for a release with no session) and echoes `corr` in events. Hangar records each decision as a `governance.decide` span with `hangar.governance.kind`, `.mode`, `.outcome`, `.approval`, `.receipt`, `.source` and `.failure`, and puts the receipt on the approval's span so reconciliation can match exactly (#176, gaw#20).

`args_hash` is the one value both sides compute: lowercase hex SHA-256 of `json.dumps(input, sort_keys=True, separators=(",", ":"), default=str)` over the harness's tool input as the harness sent it, before any clipping. Hangar sends it; the workspace records it on the receipt for `hangar.tool.call` (it doesn't recompute it from `detail`, which is clipped); hangar's exported `execute_tool` span carries the same value as `gen_ai.tool.call.arguments.hash`.

## 11. Receipts

In `enforce`, an `allow` without a receipt is refused (§4.2). Hangar keeps the id where the decision lives and shows it:

| Kind | Stored on (added by) | Shown |
| --- | --- | --- |
| `job.release` | `JobApproval.receipt`, `.workspace_approval_id` (#172) | job page, the issue comment, the review packet |
| `landing.approve` | `LandingState.receipt` (#172) | the review packet's landing rule line |
| `tool.call` | `Approval.receipt` on the turn (#173) | the turn's approvals, the trace span |
| `egress.request`, `secret.request` | `EnvRequest.receipt` | the request |

For `job.release` and `landing.approve` hangar also sends `subject.harness_digest`, `environment_digest` and the concrete `model` from the turn's manifest (0014), and `context.checks` with `trusted` set for hangar's own verification, so the workspace's receipt carries the attestation envelope (`aw.attest/v1`, gaw#18). Hangar links to `GET /v1/receipts/{id}/attestation`; it doesn't verify the chain itself. Local, shadow and break-glass decisions carry no receipt (break-glass decisions get one when replayed).

## 12. Events from the workspace

One inbound route, `POST /api/governance/events`, for everything the workspace pushes (it replaces the separate `/api/governance/decisions` callback #172 sketched; #190 builds it):

- **Signature** (gaw#17): `X-AW-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, t + "." + body)>`, `X-AW-Event-Id`. The secret is `HANGAR_GOVERNANCE_WEBHOOK_SECRET` in hangar's vault, one per subscription. Rejected and audited: no or bad signature, `t` more than 300 s off, an event id seen before (ids are kept 48 h, past the workspace's 24 h retry window), a malformed body.
- **Approval events are hints.** `approval.created` and `approval.decided` only make hangar re-read `GET /v1/decisions/{approval}` with its own credential and act on that answer. A forged, replayed, stale or reordered event can at most cause a status read, so out-of-order delivery ("decided" before "created") needs no special handling.
- **`run.stop`** (gaw#22) interrupts the run through hangar's existing interrupt path, is audited with who stopped it, and marks the session so that no later `approval.decided` resumes it. It only removes capability, so it is acted on once its signature checks out. The workspace never holds a hangar admin token for this.
- Events that arrive during the power-down are applied to hangar's state at once; anything that needs a sandbox waits for power-up.

<!-- contract: event -->
```json
{"id": "ev-01HF3", "kind": "approval.decided", "at": "2026-10-16T15:02:11Z",
 "approval": {"id": "A-123", "status": "approved", "idempotency_key": "hangar:job:j-4143a542:release",
              "decided_by": "dana@acme.test", "receipt": "RC-104", "expires_at": "2026-10-17T14:05:00Z"},
 "corr": {"source": "hangar", "job": "j-4143a542", "session": null, "turn": null}}
```

## 13. Security notes

- The `hangar` agent's credential can assert requesters and signers. Phase 1 scopes the key to `/v1/decisions*`; phase 2 replaces it with a 15-minute passport. Either way it never reaches a sandbox.
- A signer asserted by hangar is only as good as hangar's front door: GitHub numeric ids, linked accounts (people.py), never logins. The workspace records `proof: hangar:<via>` so an auditor can tell those signatures from ones made in the workspace.
- `action.detail` is content (a command, a file path). It is redacted (`broker.redact`), clipped to 2000 characters with both ends kept (`driver.shown`), shown on the approval card, and forgotten by the workspace once decided (its AD-6). Receipts keep only `args_hash`. Events carry ids and hashes only.
- Hangar posts the workspace's `reason` on public issues; it is redacted on the way out like any other text hangar writes there.

## 14. Rollout

| Phase | What turns on | Owner issues | Exit |
| --- | --- | --- | --- |
| 0. This spike | spec, `governance.py`, contract tests; mode `off` everywhere | #171 | merged; the gaw issues reference this schema |
| 1. Shadow, control plane | `job.release` and `landing.approve` in `shadow`, service key, dry runs | #172, gaw#13 | 7 days with every disagreement explained; p95 `decide` < 500 ms; unavailable < 1 % |
| 2. Identity and events | control-plane passport; `/api/governance/events` live; still shadow | #174, #190, gaw#14, gaw#17 | the service key deleted; events verified in production; restart replay tested |
| 3. Enforce, control plane | `job.release`, `landing.approve` in `enforce`; break-glass rehearsed once | #172 | a week with no disagreements, then hangar-local approval for these kinds is deprecated (kept for `off` and break-glass) |
| 4. Tool calls | `tool.call` shadow (sampled), then `enforce` under `ask`; suspend and resume; trace reconciliation | #173, #190, #176, gaw#20 | suspend/resume tested across a power-down; reconciliation shows no `unreceipted` hangar calls |
| 5. Requests and attestation | `egress.request`, `secret.request` in `enforce`; attestation on landing receipts | gaw#18 | |

Any phase rolls back by setting the kind back to `shadow` or `off`; nothing is migrated, and receipts already written stay.

## 15. Where the workspace's API differs from what hangar needs

Each item is proposed as a comment on the workspace issue that owns it.

| # | Today, or as gaw's issue drafts it | What hangar needs | Issue |
| --- | --- | --- | --- |
| 1 | Only `/hooks/claude-code` (a hook's tool call) and `/agentcore/decide`; nothing for a decision with no tool call | `POST /v1/decisions`, `GET /v1/decisions/{id}` | gaw#13 |
| 2 | Four kinds: `hangar.job.release`, `hangar.tool`, `hangar.landing`, `hangar.egress` | Five, named `hangar.<kind>`: `hangar.job.release`, `hangar.tool.call`, `hangar.landing.approve`, `hangar.egress.request`, `hangar.secret.request` (new tool `secret_request`) | gaw#13 |
| 3 | No way for a caller to bring a signature; people sign only in Slack, Teams or the web | `signer` (§4.4), from the `hangar` agent only, judged by `decide()`'s own rules, recorded as `proof: hangar:<via>` | gaw#13, gaw#19 |
| 4 | An exception answers 503 `{"decision":"deny"}` | Fine, but documented as advisory: hangar treats every 5xx as unavailable (§4.2) | gaw#13 |
| 5 | "Block, then ask again" when `deadline` is under 2 s: a deny with the approval id | For `/v1` always answer `wait`; hangar follows the approval itself (§5). Keep block-then-ask for hook callers | gaw#13 |
| 6 | "Same key → same final decision" | Single-use allows for `hangar.tool.call`; expired and withdrawn approvals don't bind the key; a 10-minute response cache for retried requests (§4.3) | gaw#13 |
| 7 | No dry run | `dry_run: true`: rules only, no approval, cards or receipt, not counted in the hourly budget (§4.6) | gaw#13 |
| 8 | `withdraw()` exists internally, no route | `POST /v1/decisions/{id}/withdraw` (§4.5) | gaw#13 |
| 9 | Hook answers use `expiresAt` (camelCase) and no state | snake_case `expires_at` (RFC 3339), plus `status` and `rule` (§4.2) | gaw#13 |
| 10 | A tool call's `action.input` (raw); receipts hash the workspace's mapped arguments (`from_hook`: `run_command` with the command clipped to 2000 characters and `project` added) | `action.args_hash` from hangar, recorded on the receipt for `hangar.tool.call`, and `action.detail` for the card (§10). Otherwise no receipt can match hangar's span | gaw#13, gaw#20 |
| 11 | `requester_known` is false for an outsider; rules on writes may block on it | `release_job` must ask maintainers even when the requester is an outsider (`requester.external` only): that is the case it exists for | gaw#13 |
| 12 | `ai_review` is a URL string | an object `{url, sha, harness, model, verdict}`: gaw#19 needs the harness and model to judge independence, and the sha to drop a review of an older head | gaw#13, gaw#19 |
| 13 | Passports are minted only with `aud = <origin>/mcp`, and `check()` refuses any other audience | `/v1/decisions` and `/hooks/*` must accept the passport hangar holds: one service audience for every route, or a list | gaw#14 |
| 14 | Federation example pins `sub: hangar:env:<env>` with `requester_claim` (sandboxes) | also a trust for agent `hangar` (kind `hangar`) with `sub: hangar:control-plane` and no requester claim; and hangar's tokens carry `person`, not a verified `email`, until #174 adds one (the `hangar_people` map is the fallback) | gaw#14 |
| 15 | Webhooks signed with HMAC (gaw#17); hangar's #172 and #190 drafts said "JWKS / EdDSA" | HMAC as gaw#17 specifies, because approval events are hints that hangar re-reads (§12). Hangar's drafts follow this spec | gaw#17 |
| 16 | Webhook payload: approval and `corr` | also the decision `kind` (`hangar.job.release`, ...); a `run.stop` event kind on the same subscription | gaw#17, gaw#22 |
| 17 | Per-run stop calls hangar's REST interrupt with a stored hangar token | a signed `run.stop` event to `/api/governance/events`; no hangar token in the workspace | gaw#22 |
| 18 | Reconciliation reads `gen_ai.tool.call.arguments` (raw) and matches the receipt by the span's tool name | match by `corr.tool_call_id` first, then `gen_ai.tool.call.arguments.hash` (bare lowercase hex, as receipts store it) and job/session; hangar doesn't export raw arguments by default | gaw#20 |
| 19 | `corr` is `{source, job, session, turn, trace_id, span_id, tool_call_id}` | add `run` (hangar's run id: one turn can have more than one run after a crash resume) | gaw#16 |
| 20 | Attestation is filled from `digests` | hangar sends the digests and model in `subject` (`subject.harness_digest`, `.environment_digest`, `.model`); `checks` in `context.checks` with `trusted`. Either name works if both sides agree; this spec uses `subject` | gaw#18 |
| 21 | Break-glass decisions taken while the workspace was down have no receipt | a "record" request (`POST /v1/decisions` with `recorded: {decided_by, rule, at}`) that writes a receipt with `proof: hangar-break-glass` and opens no approval | gaw#13 (open item) |

## 16. Open items

- **Break-glass replay** (§7, item 21): the record request's shape is agreed only in outline.
- **Approval ttl for landings**: the workspace's `merge_pull_request` ttl is 3 days; hangar has no landing deadline today. Hangar sends none for now.
- **Who signs in the workspace for a team job** (no person): `requester` is empty and only `context.untrusted_source` is set; maintainers sign. Fine for release; for landing, the policy approvers decide in hangar today, so their mapping to a workspace group needs agreeing per repo.
- **Multi-signature from hangar surfaces**: two `/hangar approve` comments by different people each add a signature; the second gets the `allow`. Tested on the workspace side once gaw#13 lands.

## 17. What the contract tests pin

`tests/test_governance.py`, against a fake workspace (`httpx.MockTransport`):

- each row of §6 for every kind: unreachable, timeouts, 500/502/503 (including 503 `{"decision":"deny"}`), 429, 401 after a refresh, 403, 400, a malformed body, an allow without a receipt, an open circuit: none is ever `allow`;
- the 401 retry with a fresh token; the circuit opening and closing;
- `wait` followed by status polling until `allow`, a webhook waking the waiter, and the deadline failing closed;
- shadow never changing the outcome and reporting disagreements; enforce using the workspace; break-glass needing two admins and 24 h at most;
- `args_hash` equal to the workspace's own function;
- the JSON examples in this document parse with the client's models, and an event signed as gaw#17 specifies verifies (stale, replayed and tampered ones don't).
