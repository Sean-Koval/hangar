# Test-first: isolation and threats

What keeps a [test-first job](test-first.md)'s hidden tests hidden and its grade honest, and on which provider? This page is for whoever reviews the design's security. It builds on [sealed graders](../security/graders.md) and [the driver sidecar](../security/driver-sidecar.md).

**Status: a design spike (KAN-80).** Rows marked "today" describe code that exists, used as this design would use it.

## The guarantees, by provider

The implementer and the grade run in different sandboxes. The implementer's sandbox never holds the tests, so most of what follows is about the grader's sandbox, where the patch runs beside them.

| Guarantee | Docker | GKE | E2B |
| --- | --- | --- | --- |
| The implementer can't read the tests during its turn | Holds: they are never in its sandbox, context, ticket, PR or inbox text | Holds, for the same reason | Holds, for the same reason |
| The tests can't leave the grader's sandbox | Holds today: sealed, egress `none`, on the internal network | Holds today: sealed, proxied, no DNS | No grader: a sealed sandbox can't start on E2B. The grade runs on Docker or GKE |
| The patch can't change the tests while they run | Holds today: they are root-owned outside the repo | Doesn't hold: they sit under `/tmp`. Waits on KAN-188 (`hangar-home`, read-only for the trial) | Not applicable: no grader runs there |
| The patch can't forge the grade file | Holds with the uid split (`sudo: false`): `trust: isolated` | Doesn't hold: `trust: best_effort` until KAN-188 moves the wrapper into `hangar-driver` | Not applicable |
| The writer can't see an implementation | Holds before phase 2 delivers. After, it waits on pinned fetch grants | Same | Same |

The approval is decided on the control plane, not in a sandbox. So H-1, an agent answering its own tool approval on GKE, doesn't reach it. H-1 matters only if a phase runs under `ask` on GKE, which `HANGAR_UID_SPLIT=required` refuses until KAN-186.

**What none of this stops.** Code under test runs inside the test runner's process. It can patch the runner and lie on the pipe, on every provider. The defence is the detectors: the base check, the canary, `tamper_signals` and `touched_tests`.

## Threat cases

| Threat | What stops it | What is left |
| --- | --- | --- |
| The implementer reads the hidden tests | They are never in its sandbox. The store has no route that serves a body. The canary scan catches a copy in its patch, RESULT.md, trace or egress, and marks the run `contaminated` | A person pastes a test into the ticket. The card masks the canary, so the scan misses that copy |
| The implementer edits the hidden tests | The grader writes a fresh copy from the store for each grade. The patch can only edit the repo's own tests, listed as `touched_tests` | At grade time on GKE, the code under test can rewrite `/tmp` copies, until KAN-188 |
| The patch forges a pass | The uid split's wrapper owns the grade on Docker. The base check shows the tests can fail | `best_effort` on GKE. The PR shows the grade's `trust` so a reviewer knows |
| The writer sees the implementation | Phase 2 starts only after approval. The writer is pinned to the base and has a fetch-only grant | A fetch-only grant fetches any branch. A rewrite after phase 2 delivered could read it, until pinned grants |
| The tests leak the answer | The card shows every test file, so the requester sees one that carries an implementation. Feedback to the implementer is counts and test ids, never output | Test ids leak intent. The card shows them, so the requester sees what will be sent |
| The approval is rubber-stamped | The card leads with the criteria map and flags uncovered criteria and `guard` tests. Under `enforce`, Tower can ask for a second signer | A three-minute look stays a look. Fast approvals are counted, not blocked |
| Rejected tests are reused | An approval binds one version's digest. Rejected versions are kept but never graded | None known |
| A rewrite loops forever | Three rejections stop the parent and move the ticket to `lane:pair` | The cap is a guess, to tune from the first month |

## Re-runs

**A rejection** goes to the writer's session as a follow-up turn, with the requester's reason. Its base and harness stay pinned, and the new version needs a new approval. After three rejections the parent stops and the ticket moves to `lane:pair`. Rejected versions are kept for the audit log and never graded.

**New tests after phase 2 started.** The requester reopens the approval, which cancels a running implementer turn. The new version is approved, then the existing patch is graded again. The implementer's own follow-ups never change the tests.

## Why counts and ids, not output

A failing test's output quotes its assertions, and often its expected values. Sent to the implementer, a few rounds of it would rebuild the tests. Counts and ids say which criteria failed without saying what passing means. The requester sees the full output on the parent.

## Open questions

- Should hangar add the approved tests to the PR once the grade passes, as a commit of its own? They are no longer secret for that ticket, and the repo would keep them as regression tests.
- Should the writer also hand back a reference patch, to prove its tests can pass? It would be kept in the store and never shown to the implementer. It also makes the writer a second implementer.
- Is "a different harness" enough, or should it be a different vendor's model too?
