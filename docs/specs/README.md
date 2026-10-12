# hangar specs

Design specs for the next round of work. They came out of the first live GKE run on 2026-10-11: two Claude Code agents in gVisor sandboxes made real changes and opened PRs (#13, #14), and the run exposed the gaps these specs close. Each spec was drafted by a Claude Code agent in its own hangar sandbox, working in parallel. This page reconciles them.

| Spec | Owns | PR |
| --- | --- | --- |
| [0001 Agent runs](0001-agent-runs.md) | The `Run` record and its lifecycle states, the supervisor and liveness watcher, structured events and status, plan-usage tracking and the per-credential queue, `resume_agent` / `cancel_agent` / `send_input`, baked dependencies | #16 |
| [0002 Results and landing](0002-results-and-landing.md) | The results pipeline (`stage`, `RunResult`, collection from GitHub), verification, fan-out and grading, the land step, approvals, notifiers | #18 |
| [0003 Sandbox security](0003-sandbox-security.md) | Branch protection, the credential gateway (git smart-HTTP inspection, GitHub REST allowlist, grants, GitHub App tokens), the model-API proxy, egress allowlists, token scopes and limits, dashboard hardening | #17 |
| [0004 Supervisor, scheduling, performance](0004-supervisor-scheduling-performance.md) | Tasks and sub-task graphs, the deterministic supervisor and its intervention ladder, scheduler v2 (priority, fair share, plan-aware pacing, model tiers), warm-pool forecasting, image distribution, workspace checkpoints, right-sizing, benchmarks | #21 |
| [0005 Traces and decision model](0005-traces-and-decision-model.md) | OpenTelemetry collection from harnesses, trace storage and redaction, the `DecisionService` (Microsoft Decision-1 first, rules and local fallbacks), decision tasks, shadow/advisory/acting modes, the optimization and evaluation loop | #20 |
| [0006 Environments and harness packs](0006-environments-and-harness-packs.md) | Image/runtime/session split, harness packs, per-sandbox harness selection, `mcp:` and `secrets:`, default and team environments | #22 (M1–M3 built) |
| [0007 Experiments, policy and team learning](0007-experiments-policy-and-team-learning.md) | The MCP gateway, the `policy:` block and where each rule is enforced, experiments over environment variants, the label taxonomy and labeler cascade, links from runs to tickets and objectives, team learnings, and the performance fixes they depend on | this PR |

## Cross-spec decisions

Where a spec conflicts with this list, this list wins. Specs will be updated to match as their milestones start.

1. **One `Run` model with two axes.**
   - **Lifecycle state:** 0001's `RunState` (queued, starting, running, waiting_input, succeeded, failed, crashed, cancelled, timed_out), set by 0001's supervisor and watcher.
   - **Results pipeline:** 0002's `stage` (pending, publishing, collecting, verifying, done, degraded, error).
   - **Results:** 0002's `RunResult` replaces 0001's smaller `RunResult`.
   - **Name mapping:** 0002's `exited` becomes `succeeded` or `failed`, `lost` becomes `crashed`, `stop_agent` becomes `cancel_agent`, `max_minutes` becomes `timeout_minutes`, and `harness_session_id` becomes `session_id`.
   - **Crash detection:** a single watcher (0001 M2). It adopts 0002's probe rules: `kill -0` on the recorded pid, a zombie counts as gone, two misses before `crashed`. It polls every 10 s, not 60 s.
2. **Verification lives in 0002.** `repo.verify` in the environment spec (command, mode, retries) is the default. 0001's per-run `verify` argument overrides it for that run. The result is `RunResult.verification`, in 0002's shape. `succeeded` requires verification to pass when a verify command is set.
3. **One usage tracker and one queue.** These are 0001 M4; 0002's milestone 4 is dropped. Concurrency is counted per agent credential, default 2 for a subscription credential, configurable per credential. 0002's fan-out uses this queue rather than its own.
4. **One credential gateway, owned by 0003.** It runs as a separate listener on port 8788 with `SandboxGrant` records, smart-HTTP push inspection, a GitHub REST allowlist and per-grant GitHub App tokens. 0002's `/git` router on the main port and its `hgs_` fields in `Sandbox.info` are dropped, along with 0002 milestone 10. 0002's publish step and land worker push through the gateway like any sandbox.
5. **Merge rights never reach the gateway.** 0002's `Credential.purpose = "control-plane"` is adopted. The land credential is control-plane only. The gateway's REST allowlist must not include merge, review-approve, update-branch or ruleset endpoints. A test pins this.
6. **Pull requests.** Hangar opens the PR in the publish step (0002), using `open_pull_request` and `pull_request_status` (0003 M4). Agents may also run `gh pr create` through the allowlist. Either way hangar records the PR from GitHub, never from the agent's message.
7. **Scopes: 0003's model plus two of 0002's.** The base is 0003's: `read` ⊂ `operate` ⊂ `admin`, `TokenLimits`, `Principal`, and OAuth scopes `hangar:read` / `hangar:operate`. To that add 0002's two scopes:
   - **`land`:** request, inspect and cancel land requests.
   - **`approve`:** approve or reject a land request. It is human-only. It's never implied by `admin`, never grantable over OAuth, and has no MCP tool. Only the bootstrap or env admin token, explicitly scoped tokens, and IAP-authenticated users get it.

   Scopes are implemented once, in 0003 M1.
8. **Branch protection first.** 0003 M0 applies it to `main` before any agent work that pushes. It requires a PR, passing CI and one approval, blocks force-push and deletion, and has no bypass list. Until the gateway ships, the sandboxes' fine-grained token is the only thing an agent holds, and branch protection is what keeps it off `main`.
9. **Model-API proxy is gated.** 0003 M9 ships only after it is confirmed that routing a Claude subscription token through hangar is allowed and works (0003 open questions 1 and 5). Until then the subscription token stays in the sandbox, and egress limits (0003 M6–M7) are the mitigation.
10. **The decision interface is 0005's.** It uses fixed, versioned decision tasks: `classify_run(run_id, task)` returns `label`, `probabilities`, `actionable` and `evidence`. 0004's free-form `classify(question, options)` and its `rationale` field are dropped, since Decision-1 returns no rationale.
    - 0004 adds its supervisor decisions as new decision task files, not ad-hoc questions.
    - 0004 acts only when `actionable` is true. Otherwise it opens an escalation, as its spec describes.
    - 0004's `TraceStore` is 0005's run-event store, extended with task events.
11. **Scope names follow decision 7.** 0005's `runs:read` maps to `read` and `runs:write` to `operate`. 0004's proposal is accepted: `approve` also covers resolving `human_only` escalations. 0004's new `TokenLimits` fields are added to 0003 M1.
12. **One sandbox-facing listener.** Telemetry ingest (0005's `/otlp/v1/*`) lives on 0003's gateway listener (:8788) once it exists, behind a per-run ingest key. Until then it is on :8787 with the same key. Sandboxes never need the admin API.
13. **0004's changes to 0001 are accepted.**
    - The queue orders by class, priority, fair share and age, with adaptive concurrency under a hard cap.
    - `Run` gains `task_id`, `subtask_id`, `attempt_no`, `priority`, `queue_class`, `owner` and `model`.
    - The workspace half of 0001 M10 (checkpoints) moves to phase 2 as 0004 M4, because Spot preemption is the normal case on GKE.
14. **Harnesses come from packs (spec 0006, M1–M3 built and live-tested 2026-10-12).** Harness binaries ship as read-only OCI image volumes mounted at sandbox start, not baked into environment images. This was verified on GKE 1.36 with gVisor. Baking is the fallback for E2B and for Docker without the containerd image store. 0004's `model_flag` and 0005's per-harness parsers attach to the harness profile, not the environment.


15. **0007 extends 0003, 0005 and 0006; it does not fork them.**
    - **MCP gateway:** a second protocol on 0003's gateway grant model, not a separate trust system.
    - **Policy:** 0007's `policy:` block absorbs 0003's `network:` block as `policy.egress`.
    - **Experiments:** 0007's `run_experiment` replaces 0005 M10's two-config `POST /api/evals`, keeping all eight of 0005's evaluation rules.
    - **Parsers:** the Codex, pi and Antigravity parsers move from 0001 M7 and 0005 M3 into 0007 M6, starting from `evals/semver/spans.py`.

## Build order

Phases are in order. Within a phase, items can run in parallel. Sizes: S ≤ 1 day, M ≈ 2–3 days, L ≈ 4–6 days.

| Phase | Goal | Milestones |
| --- | --- | --- |
| 0. Now | Close the open hole, capture test data, unblock capacity | 0003 M0 (branch protection: **applied 2026-10-11**; `repo_guard` still to do) · 0001 M0 (fixtures, flag checks, baselines) · 0004 M0 (benchmarks) · 0005 M0 (OTLP fixtures) · 0007 M0 (quota, disk, CPU requests) · 0007 M1 (async create) · 0007 M2 (builds without a laptop) |
| 1. Runs you can trust | No more silent crashes, readable status, usage visible | 0001 M1 run model · 0001 M2 supervisor, liveness, cancel (with decision 1's probe rules) · 0001 M3 events and status · 0001 M4 usage and queue · 0004 M1 quick wins · 0005 M1 scrubbing |
| 2. Real results | Results from git and GitHub, verification by hangar, fast starts | 0002 M2 collector and publish · 0002 M3 verification · 0001 M5 resume · 0001 M9 baked dependencies (independent, can start in phase 1) · 0004 M2 warm-pool forecasting · 0004 M3 image distribution · 0004 M4 checkpoints · 0004 M5 scheduler v2 · 0005 M2 OTLP ingest · 0005 M4 decision core (shadow mode, rules backend) · 0007 M3–M4 policy and budgets · 0007 M6 harness parsers · 0007 M7 `run_experiment` |
| 3. Credentials out of sandboxes, tasks | Agents hold grants, not tokens; goals become supervised task graphs | 0003 M1 scopes (including `land`/`approve`) · 0003 M2 gateway skeleton · 0003 M3 git inspector · 0003 M4 App tokens, REST allowlist, PR tools · 0004 M6 task model · 0004 M7 detectors and ladder · 0004 M8 planner · 0005 M5 Decision-1 backend |
| 4. Landing | Human-approved merges, parallel PRs, best-of-N | 0002 M7 land core · 0002 M8 notifiers · 0002 M11 conflicts and rebase · 0002 M5 fan-out and ranking · 0004 M9 decisions and escalations · 0004 M10 routing · 0004 M11 right-sizing · 0005 M6 remaining decision tasks |
| 5. Network and front door | Default-deny egress, hardened dashboard | 0003 M5 dashboard hardening · 0003 M6 egress audit mode · 0003 M7 egress enforce · 0003 M8 IAP/SSO · 0003 M10 OAuth scopes and step-up · 0003 M11 E2B egress |
| 6. Polish | One dashboard, team learning, optional extras | One combined dashboard (0001 M8, 0002 M9, 0004 M12, 0005 M8) · 0007 M5 MCP gateway (with phase 5's egress) · 0007 M8 labels and gold set · 0007 M9 lifecycle links · 0007 M10 team learnings · 0001 M10 stdin and transcript archive · 0005 M9–M10 insights and evals · 0003 M9 model proxy (if decision 9 clears) |

Each phase ends with its specs' live acceptance checks on GKE (0001 §8.2, 0002 §8, 0003 §8.2), recorded in the PR that closes the phase. The docs milestones (0001 M11, 0002 M12, 0003 M12) are folded into those phase-closing PRs.

## Decisions needed from the owner

- ~~Apply branch protection to `main` now~~ Done 2026-10-11: PR required, `test`/`e2e-docker`/`terraform` must pass, no force-push or deletion, admins included. Required approvals stay at 0 until agents commit as a GitHub App identity; everything currently commits as the owner, and GitHub does not allow self-approval.
- **A Microsoft Foundry or OpenRouter account for Decision-1** (0005 M5). Until then, decisions run on the rules backend in shadow mode.
- **GitHub App for v1** (0002 OQ12, 0003 OQ11). Recommended. It gives short-lived, per-sandbox tokens, a bot identity (which lets the owner be the required reviewer), webhooks and separate rate limits. The fine-grained personal token stays as the dev fallback.
- **Is a Claude subscription token allowed to pass through a hangar-run proxy?** (0003 OQ5). This decides phase 6's model proxy.
- **Codex inside gVisor:** disable Codex's own sandbox, since hangar is the sandbox? (0001 OQ4)
- **Approver identity:** are named tokens enough for approvals, or does the dashboard need user accounts or IAP first? (0002 OQ11)
