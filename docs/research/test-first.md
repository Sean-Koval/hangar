# Test-first acceptance

How would hangar run a ticket whose acceptance tests are written and approved before its code? This page is the design, for whoever reviews or builds it. [Isolation and threats](test-first-isolation.md) says which guarantees hold on which provider.

**Status: a design spike ([KAN-80](https://seanmkoval.atlassian.net/browse/KAN-80), #188).** Nothing on this page is built. The build is split into the [follow-up drafts](#follow-up-drafts) at the end.

## The workflow it serves

About one ticket in five is in the risk tier and carries the label `agent:test-first`. For those, a test-writer agent turns the acceptance criteria into hidden tests. The requester approves them, in about three minutes. Then an implementer on a different harness writes the code. It never sees the tests: hangar runs them against its patch.

## One parent job, two phases

A test-first job is a parent of kind `test-first`, run like an [experiment](../jobs/experiments.md). A background runner with no job slot starts ordinary child jobs and records what they did.

| Step | What runs | The parent's state |
| --- | --- | --- |
| 1. Write | The writer job, `deliver: none`, on the pinned base commit | `running`, stage `writing` |
| 2. Seal | hangar moves the tests into the sealed store and runs them on the base | `running`, stage `sealing` |
| 3. Approve | An `approval` item in the requester's inbox | `awaiting_approval` |
| 4. Implement | The implementer job, on its own harness, with the repo's own `verify` | `running`, stage `implementing` |
| 5. Accept | A fresh sealed grader sandbox runs the approved tests on the base plus the patch | `verifying` |
| 6. Deliver | The implementer's branch and PR, as the parent's `deliver` says | `delivering` |

Phase 1 is steps 1 to 3. Phase 2 is steps 4 to 6.

## Phase 1: the writer

The writer gets the ticket's text and criteria and the repository at the base commit. Its prompt asks for two things in a new exchange directory, `out/sealed/`: the test files, and `criteria.json`, which maps each criterion to test ids. Unlike `out/artifacts/`, hangar never serves that directory. It moves it into the store and deletes it from the sandbox.

`test_first.writer_harness` picks the writer's harness, and the repo policy can set a default. The parent refuses to start when both phases would run the same harness. The writer's session is never forked or kept for anyone else, because its checkpoint holds the tests.

## The sealed store

The store is on the control plane, at `<data_dir>/sealed/<parent job id>/v<n>/`, files mode 0600, hangar's own. A version holds the tests, `criteria.json` and `manifest.json`: the content digest, base commit, writer harness and model, and base check. A version never changes once written. A rewrite adds the next one.

Two paths read the store: the sealed grader, which writes a version into its sandbox, and the approval view. The artifacts API, job exports, traces, the outbox, Slack, Jira, GitHub and MCP resources never get a test body. One canary per parent goes into every version's files, as `with_canary` does for graders.

**The base check.** Each new version runs on the pinned base in a sealed grader sandbox, as `_task_check` does. Every test should fail there. One that passes is marked a `guard` on the card. If all pass, the version is `invalid` and goes back to the writer before anyone is asked.

## The approval

Step 3 adds an `approval` item to the requester's inbox. Its Slack DM and Jira comment carry counts and a link, never a test body.

The DecisionDeck's ApprovalCard shows the criteria map with uncovered criteria flagged, the base check, the test files, and the writer's harness and model. Approving binds the version's digest. Rejecting needs a reason, as the console already asks. [Re-runs](test-first-isolation.md#re-runs) covers what follows.

**Who may decide.** `approvals.can_decide` gets a kind `tests`. Unlike `approve`, the requester may decide it: they confirm their own criteria, they don't release their own risky work. Repo approvers and admins may too. `mcp` and `jobs` tokens can't, as for every decision, so no agent approves.

**Tower.** [Governance](../governed-approvals.md) gets the kind `tests.approve`. Under `shadow` both answers are kept and compared. Under `enforce` Tower decides, and might ask for a second signer on a payments repo. Anything that goes wrong keeps phase 2 from starting.

## Phase 2: the implementer

The implementer is an ordinary job with the ticket's instructions and criteria, and nothing from the store. Its `verify` runs in the loop as today. After its turn, the parent runs the approved version through `hangar-grade`, the code behind `_grade_patch`, factored out of `experiments.py`. The canary scan covers the patch, RESULT.md, the trace and egress records.

On a pass, the parent delivers with `acceptance: passed` and the grade's `trust`. On a failure, a follow-up turn gets the counts and failing test ids, never the output. After `test_first.max_rounds` (default 2), the result is a draft PR marked `acceptance: failed` and an inbox `failed` item.

## Data flow

```text
Jira KAN-n (agent:test-first, Ready for agent)
  -> parent job (kind test-first, base pinned)
     -> writer job (harness A, deliver: none) -> out/sealed/ -> sealed store v1 (digest, canary)
     -> base check (sealed grader sandbox, base only) -> manifest
     -> inbox approval (console card; Slack and Jira get a link) -> Tower tests.approve (optional)
        reject + reason -> writer follow-up -> v2 ...
        approve (digest of vN)
     -> implementer job (harness B, the criteria, no tests) -> patch
     -> sealed grader sandbox (base + patch + vN, egress none) -> grade (trust) + canary scan
        fail -> implementer follow-up (counts, test ids) ...
        pass -> deliver (draft PR, acceptance: passed)
```

## Follow-up drafts

Drafts for Sean to file or drop. None are tickets yet. Each depends on the ones above it unless it says otherwise.

| Title | Scope | Acceptance | Verify | Lane |
| --- | --- | --- | --- | --- |
| Sealed store and the `out/sealed/` directory | `sealed.py` store: versions, digests, 0600 files. `exchange.py` collects `out/sealed/` into it and never into artifacts | A version can't be rewritten. No artifact, export, trace or outbox route returns a stored body | `uv run pytest -q tests/test_sealed_store.py tests/test_exchange.py` | `lane:agent` |
| Factor the sealed grade out of experiments | Move `_grade_patch`, `_task_check`, the canary scan and `hangar-grade` behind one function both callers use | Experiment grades are unchanged. A store version can be graded on a base and a patch | `uv run pytest -q tests/test_sealed_grader.py tests/test_grade_forgery.py tests/test_e2e_sealed_docker.py` | `lane:agent` |
| The test-first parent job | Kind `test-first`, its runner and states, the writer job, the base check, the harness rule, rewrites | A parent runs writer, seal and approval wait. Same harness is refused. Three rejections stop it | `uv run pytest -q tests/test_test_first.py` | `lane:pair` |
| The tests approval | `can_decide` kind `tests`, the inbox item, the DM and Jira text, the ApprovalCard view with the criteria map | The requester may approve; an `mcp` token may not. No DM or comment holds a test body | `uv run pytest -q tests/test_approvals.py tests/test_inbox.py tests/test_inbox_notify.py tests/test_ui_airframe.py` | `lane:pair` |
| Tower kind `tests.approve` | Add the kind to `governance.py` and route the tests approval through `GovernanceRouter` | `shadow` keeps both answers. `enforce` failing keeps phase 2 from starting | `uv run pytest -q tests/test_governance.py` | `lane:agent` |
| Phase 2: acceptance and its feedback | The implementer job, the sealed grade of its patch, the follow-up with counts and ids, `acceptance` on the result and PR | A pass delivers. A failure feeds back no output. The grade's `trust` is on the PR | `uv run pytest -q tests/test_test_first.py -k phase2` | `lane:pair` |
| Jira intake for `agent:test-first` | A `lane:agent` ticket with the label, moved to Ready for agent, starts a parent instead of a job | The label routes to `test-first`. Without it nothing changes | `uv run pytest -q tests/test_ticket_intake.py` | `lane:agent` |
| Pinned fetch grants for the writer | A gateway grant that fetches only the base commit's history, for writer sandboxes | A writer can't fetch `hangar/*` branches. Other grants are unchanged | `uv run pytest -q tests/test_gateway_git.py` | `lane:agent` |
| Rubber-stamp signals | Time to decide, files opened, and approvals under 30 seconds, in the audit log and the weekly report | Each decision records both. The weekly report counts fast approvals | `uv run pytest -q tests/test_weekly.py tests/test_approvals.py` | `lane:agent` |

GKE grades reach `trust: isolated` only with [KAN-188](https://seanmkoval.atlassian.net/browse/KAN-188), which is not on this list.
