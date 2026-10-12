# hangar specs

Design specs for the next round of work. They came out of the first live GKE run on 2026-10-11: two Claude Code agents in gVisor sandboxes made real changes and opened PRs (#13, #14), and the run exposed the gaps these specs close. Each spec was drafted by a Claude Code agent in its own hangar sandbox, working in parallel. This page reconciles them.

| Spec | Owns | PR |
| --- | --- | --- |
| [0001 Agent runs](0001-agent-runs.md) | The `Run` record and its lifecycle states, the supervisor and liveness watcher, structured events and status, plan-usage tracking and the per-credential queue, `resume_agent` / `cancel_agent` / `send_input`, baked dependencies | #16 |
| [0002 Results and landing](0002-results-and-landing.md) | The results pipeline (`stage`, `RunResult`, collection from GitHub), verification, fan-out and grading, the land step, approvals, notifiers | #18 |
| [0003 Sandbox security](0003-sandbox-security.md) | Branch protection, the credential gateway (git smart-HTTP inspection, GitHub REST allowlist, grants, GitHub App tokens), the model-API proxy, egress allowlists, token scopes and limits, dashboard hardening | #17 |

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

## Build order

Phases are in order. Within a phase, items can run in parallel. Sizes: S ≤ 1 day, M ≈ 2–3 days, L ≈ 4–6 days.

| Phase | Goal | Milestones |
| --- | --- | --- |
| 0. Now | Close the open hole and capture test data | 0003 M0 (branch protection, `repo_guard`) · 0001 M0 (fixtures from the live run, flag checks, baseline timings) |
| 1. Runs you can trust | No more silent crashes, readable status, usage visible | 0001 M1 run model · 0001 M2 supervisor, liveness, cancel (with decision 1's probe rules) · 0001 M3 events and status · 0001 M4 usage and queue |
| 2. Real results | Results from git and GitHub, verification by hangar, fast starts | 0002 M2 collector and publish · 0002 M3 verification · 0001 M5 resume · 0001 M9 baked dependencies (independent, can start in phase 1) |
| 3. Credentials out of sandboxes | Agents hold grants, not tokens | 0003 M1 scopes (including `land`/`approve`) · 0003 M2 gateway skeleton · 0003 M3 git inspector · 0003 M4 App tokens, REST allowlist, PR tools |
| 4. Landing | Human-approved merges, parallel PRs, best-of-N | 0002 M7 land core · 0002 M8 notifiers · 0002 M11 conflicts and rebase · 0002 M5 fan-out and ranking |
| 5. Network and front door | Default-deny egress, hardened dashboard | 0003 M5 dashboard hardening · 0003 M6 egress audit mode · 0003 M7 egress enforce · 0003 M8 IAP/SSO · 0003 M10 OAuth scopes and step-up · 0003 M11 E2B egress |
| 6. Polish | One dashboard, more harnesses, optional extras | 0001 M8 and 0002 M9 dashboards (one combined design) · 0001 M7 Codex and Gemini · 0001 M10 stdin and checkpoints · 0003 M9 model proxy (if decision 9 clears) |

Each phase ends with its specs' live acceptance checks on GKE (0001 §8.2, 0002 §8, 0003 §8.2), recorded in the PR that closes the phase. The docs milestones (0001 M11, 0002 M12, 0003 M12) are folded into those phase-closing PRs.

## Decisions needed from the owner

- **Apply branch protection to `main` now** (0003 §5.2).
- **GitHub App or fine-grained PAT for v1** (0002 OQ12, 0003 OQ11). The App gives short-lived, per-sandbox tokens and its own attribution, at the cost of a one-time setup.
- **Is a Claude subscription token allowed to pass through a hangar-run proxy?** (0003 OQ5). This decides phase 6's model proxy.
- **Codex inside gVisor:** disable Codex's own sandbox, since hangar is the sandbox? (0001 OQ4)
- **Approver identity:** are named tokens enough for approvals, or does the dashboard need user accounts or IAP first? (0002 OQ11)
