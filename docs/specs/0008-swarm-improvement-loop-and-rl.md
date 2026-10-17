# 0008: Sandbox swarms for coding work, and the improvement loop, labeling and RL built on them

Status: deferred (2026-10-15): not scheduled, and not for 2026 (swarms, the improvement loop, RL); the Harbor adapter, `save_as_task` and ATIF moved to flightdeck (decisions 16 and 17). Was: draft. · Date: 2026-10-12 · Builds on: 0004 (scheduler, performance), 0005 (traces, Decision-1), 0006 (environments, packs), 0007 (policy, MCP gateway, experiments, labels, team learning) · Research: three briefs of 2026-10-12, sources in §10

> Part of a set. Ownership, cross-spec decisions and the combined build order are in [docs/specs/README.md](README.md); where this spec conflicts with that page, the page wins.

## 1. Summary

> **Product priority (owner, 2026-10-12).** Hangar is a control plane for sandboxes with agent harnesses, used to scale software development with coding agents. Swarms exist to get more real work done: issues fixed, migrations landed, PRs opened. Evals, labeling, the improvement loop and RL are features on top of that fleet, built from the traces real work already produces. When a choice trades development throughput or UX against eval or RL convenience, development wins.

- **Product shape:** hangar becomes the tool a developer, or the agent they are working with, reaches for to spread coding work across many isolated, fast, cheap sandboxes. It can split a migration across shards, try N fixes for an issue and keep the one that passes the repo's tests, or run untrusted code. Hangar handles placement, policy, credentials, traces and checks. A handful of MCP tools covers it, with `swarm` as the one-call default. Results land as branches and PRs, under 0002's human-approved landing.
- **Use open formats for the eval side; don't let them shape the core.** Development work is described by GitHub issues, 0004 tasks and `swarm` calls, and it is checked by the repo's own commands (its tests, linters, builds). For *graded* work (benchmarks, regression sets, experiments, RL), hangar uses **Harbor's task format** (`task.toml`, instruction, `tests/test.sh` → `reward.json`) as its only task format, implements **Harbor's sandbox interface** (`BaseEnvironment`) as an adapter, and exports trajectories as **ATIF v1.7** next to OpenTelemetry spans. A finished development run can be saved as a Harbor task, so real work becomes the regression set.
- **The improvement loop runs on a schedule and ends at a PR:**
  1. observe;
  2. detect (labels, regressions, per-tool difficulty);
  3. propose (reflective rewriting of instructions, skills and tool descriptions);
  4. evaluate (train, validation and held-out test splits; paired; repeated);
  5. open a PR with the evidence;
  6. a human merges;
  7. watch for regressions.

  Nothing reaches a team environment unmerged. Self-generated skills without eval gating made agents worse in SkillsBench (−1.3 pp), so gating is not optional.
- **Labeling is fast because it is routed and pre-labeled.**
  - Queues fill by rule: errors, retries, low Decision-1 confidence, disagreements, failed verification.
  - Decision-1 pre-labels each item.
  - People confirm with number keys, choose pairwise A/B/E, and mark the *first failure* in a run.
  - An assistant clusters free-text notes into a taxonomy.
  - Labels feed skill reports, tool and API difficulty reports, and labeler calibration.
- **RL comes in three stages, each useful on its own:**
  - **A (closed models, now):** best-of-N with a verifier at inference, plus prompt, skill and tool optimization.
  - **B:** export graded ATIF trajectories for supervised fine-tuning and preference pairs. *(Moved: ATIF is a flightdeck adapter over `hangar.trace/v1`; decision 16.)*
  - **C:** hangar as a rollout backend for open-model RL. It needs a model-interception gateway for token ids and logprobs, plus snapshot-fork for grouped rollouts.

  Reward-hacking defenses are built into the product at every stage.

## 2. Why now, and what the research says

| Finding | Source (§10) | Consequence for hangar |
| --- | --- | --- |
| Harbor defines the sandbox interface (`start/stop/exec/upload/download`), the task and verifier layout, separate verifier sandboxes, and ATIF. Its docs list about 30 integrated providers, including `gke`, `e2b`, `daytona` and `modal`. | R1 (interface, verifier, ATIF verified at source); provider list per the research brief | *(Moved to flightdeck, 2026-10-17: the task format and the ATIF adapter are flightdeck's; decision 16.)* Implement `BaseEnvironment`; adopt the task format and ATIF; convert `evals/semver` into a Harbor task |
| `verifiers` v1: taskset × harness × trace. A model-interception proxy captures tokens and logprobs. GEPA is built in and optimizes prompts over any harness, including Claude Code and Codex. | R1 | Hangar's model-API proxy (0003 M9) doubles as the interception point; GEPA is a candidate proposer |
| Memory snapshot plus fork of a running sandbox exists at E2B, Morph, Freestyle, CodeSandbox, Modal (alpha) and GKE Pod Snapshots (GA May 2026). Others fork only the filesystem. | R2 | `fork(n)` records whether each backend forks memory or only the filesystem. Coding state is mostly on disk, so disk-level forks on GKE are the default and memory fork is opt-in (decision in §7) |
| Sub-second starts come from warm pools, lazy snapshot restore, and copy-on-write disk and memory shared across clones. Lazy image loading cuts pulls about 7× (SOCI). | R2 | Pools per variant, snapshot-backed pools, image streaming |
| The OpenAI Agents SDK sandbox abstraction (Apr 2026) ships with 7 providers. No MCP-level sandbox spec exists. | R2 | Hangar's MCP tools are the MCP-side answer; add an Agents SDK provider later |
| Rollout scale in practice: 512 containers per step (DeepSWE; Docker daemon limits pushed them to Kubernetes); 1k+ concurrent rollouts (GLM-5); one Firecracker VM per rollout with snapshot-fork (Cursor). | R1 | Kubernetes plus pools is the right base; set a target of 512 concurrent agent sandboxes |
| Reward hacking is common: edited tests, `sys.exit(0)`, reading answer keys from the grader, special-cased inputs. What works: separate or late-mounted tests, test-file diff checks, no network for graded runs, an LLM-judge detector, an "abort" option, patching exploits rather than penalizing them. | R1 | Defenses are product features (§5.6). Our own semver run found an answer key on disk (0007 evidence #3). |
| GEPA beats GRPO with up to 35× fewer rollouts. ACE keeps an evolving playbook through small deltas, avoiding "context collapse". SkillsBench: curated skills +16.2 pp, self-generated −1.3 pp, and 2–3 focused skills beat comprehensive ones. | R1, R3 | Propose small deltas, select on validation, confirm on held-out, then a human merges |
| Fast labeling comes from rule-routed queues, pre-labels, number-key hotkeys with auto-advance, pairwise A/B/E, content rendered natively, and blind overlap for agreement. Error analysis means marking the *first failure* in each of 30–100 traces, then clustering (open → axial coding). | R3 | §5.4 UX |

## 3. Goals / non-goals

Goals:

- One MCP call spreads a piece of coding work across sandboxes and returns checked, ranked branches. A swarm of 20 small sandboxes is claimed in under 5 s warm.
- The swarm UX is built for development first: repo checkout, the repo's own checks, branches and PRs, budgets, and cancellation.
- Run Harbor tasks and `verifiers` environments on hangar unchanged, with hangar's policy and traces added. This is a feature, not the core.
- An improvement loop that runs unattended and produces reviewed PRs with evidence, never silent changes.
- Labeling a queue item takes about 3 seconds for a binary or categorical call.
- A data path from runs to training sets (stage B) and a rollout path for open-model RL (stage C), without redesigning anything.

Non-goals:

- Training infrastructure, GPUs or trainers. Hangar is the environment and rollout side; trainers (prime-rl, slime, rLLM) are external.
- A general labeling product. Labeling is scoped to agent traces and runs.
- Auto-merge of learned changes to team environments.

## 4. User stories

1. An agent in Claude Code calls `swarm(task="fix flaky test X", n=6, strategy="best_of_n", verify="pytest tests/test_x.py -q")`. It gets back 6 attempts ranked by verifier result, cost and diff size, and opens a PR from the best one.
2. An agent calls `swarm(items=[...200 files], strategy="map", prompt="migrate to the new logging API", n=20)`. Twenty sandboxes split the 200 files and produce one combined branch, with conflicts flagged.
3. Sean opens the Review tab and finds 40 items queued, each pre-labeled. He confirms most with `1`/`2`, marks the first failure in 6 failed runs, and accepts the suggested taxonomy cluster "misreads test output".
4. Monday's report: "The `github` MCP server's `create_pull_request` errors in 18% of calls; agents retry 2.1× on average. Proposed fix: tool-description patch in PR #NN. Validation: errors 18% → 4%, held-out confirmed."
5. "Does our `repo-conventions` skill help?" A paired skill report: with vs without, trigger accuracy, cost delta, intervals.
6. Later: point prime-rl at hangar as the environment backend. It runs 512 concurrent rollouts of an open model on Harbor tasks, with interception logprobs, separate verifier sandboxes and hack detection.

## 5. Design

### 5.1 Swarm primitives (the MCP surface)

There are few tools, each covering a lot, with sensible defaults. The existing sandbox tools stay; these sit above them.

| Tool | What it does |
| --- | --- |
| `swarm(task \| items, n, strategy, env?, harness?, model?, verify?, budget?)` | Creates the sandboxes, runs, verifies, ranks and returns results. Strategies: `best_of_n` (same task N ways, forked from a prepared snapshot), `map` (shard `items` across N), `tournament` (best-of-N, then fork the top k from their state and continue), `matrix` (variants, i.e. 0007 experiments). Progress streams as MCP progress notifications; results are MCP resources. |
| `sandbox_pool(env, size, ttl)` | Reserves a pool for a burst (warm, snapshot-backed where the backend allows) and returns a handle. `swarm` uses one automatically. |
| `fork_sandbox(source, n, kind="auto"\|"memory"\|"filesystem")` | Existing tool, extended. The result says which kind it got. |
| `exec_many(sandboxes \| pool, command)` | Fan-out command execution with collected outputs. For agents using sandboxes as tools, with no harness inside. |
| `swarm_status / swarm_results / cancel_swarm` | Inspect, page through and stop. |

**Two sandbox kinds:**

- **Tool sandboxes** (`profile: nano`, 0.25–0.5 vCPU, 512 MB–1 GB, no harness): the calling agent runs its own loop and only executes code here. These are the cheapest, the densest, and the sub-second ones.
- **Agent sandboxes** (`profile: agent`, 1–2 vCPU, harness pack mounted): a harness runs inside under policy.

**Placement** chooses a backend by need:
- **Default:** GKE pools on hangar's own nodes, forking at the disk level (repo, dependencies and build caches carried over). Code and credentials stay in the owner's project.
- **Process state needed** (a running dev server, warmed language server, mid-session fork): GKE Pod Snapshots, then E2B if the M2 benchmark shows Pod Snapshots too slow or too fragile.
- **Local:** Docker.

The cost model is per backend, and placement is visible in the result.

**Merging `map` output:** each shard commits on `swarm/<id>/<k>`. Hangar merges the shards in order onto `swarm/<id>`, reports conflicts per shard, and can run one agent sandbox to resolve them.

### 5.2 Performance architecture (targets)

| Path | Mechanism | Target |
| --- | --- | --- |
| Warm claim | Pools per (env, harness, profile) and per experiment variant; forecasting (0004 M2) | p50 < 1 s, p95 < 3 s |
| Fork N | Disk-level fork from a prepared sandbox on GKE (default); memory snapshot restore where process state is needed (GKE Pod Snapshots pinned to machine series and gVisor version; E2B) | Disk fork N=10 < 15 s; memory fork N=10 < 5 s where the backend supports it |
| Cold start | GKE image streaming or lazy pulls; pack pre-pull DaemonSet; images built in CI | < 30 s p95 |
| Density | CPU requests below limits; `nano` profile; bin-packing | ≥ 30 nano or ≥ 12 agent sandboxes per 8-vCPU node |
| Scale | Async create plus queue; node pools sized from forecasts; quota headroom | 512 concurrent agent sandboxes and 2,000 nano per cluster |
| Orchestration | Server-side swarm loop; long-poll and notifications | 1 call per swarm; 0 client polling |

Each number gets a benchmark in `evals/perf/`, run on every release (0004 M0).

### 5.3 The automated improvement loop

```
 nightly (scheduled task)                                                 human
 ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐   ┌──────────┐   ┌───────┐   ┌──────────┐
 │ observe  │──►│ detect   │──►│ propose  │──►│ evaluate     │──►│ PR with  │──►│ merge │──►│ monitor  │
 │ ATIF +   │   │ labels,  │   │ deltas to│   │ train → val  │   │ evidence │   │       │   │ canary,  │
 │ spans,   │   │ tool     │   │ skills / │   │ → held-out,  │   │ + report │   │       │   │ regress- │
 │ outcomes │   │ difficulty│  │ instr. / │   │ paired, ≥3   │   │          │   │       │   │ ion alert│
 └──────────┘   │ regress. │   │ tool desc│   │ repeats      │   └──────────┘   └───────┘   └──────────┘
                └──────────┘   │ / env    │   └──────────────┘
                               └──────────┘
```

*(2026-10-17: "observe" now means `hangar.trace/v1` and the outbox; ATIF is a flightdeck adapter, decision 16.)*

- **Detect** (no model calls where rules suffice):
  - label rates by environment, skill and tool;
  - verified-outcome regressions after model or harness version changes;
  - per-tool difficulty: error rate, retries per call, recovery rate, steps until the next successful call;
  - environment friction (0005's environment-fix signatures).
- **Propose:**
  - ACE-style *small deltas* to a playbook, kept per repo in the team environment repo: instructions, skills, MCP tool descriptions, environment setup;
  - GEPA-style reflective rewriting for a single prompt or skill description.

  Every proposal carries its evidence runs.
- **Evaluate:** the 0007 experiment runner, with the splits GEPA and skill-creator use:
  - propose on train;
  - select on validation (a Pareto set over pass rate, cost and time);
  - confirm once on held-out test.

  Repeats are at least 3, with paired analysis. A proposal that only wins on train is discarded. Skill trigger accuracy is evaluated separately (should-trigger vs near-miss queries, 60/40 split).
- **Output:** a PR to the team environment repo or the skill repo. The body holds the claim, the evidence, the experiment report (effect, interval, n, cost), and the diff. Default automation level: open the PR. Levels are configurable:
  - L0: report only;
  - L1: open a PR (default);
  - L2: auto-merge to a `staging` environment channel that only experiments use.

  Never auto-merge to the default channel.
- **Monitor:** after merge, the next runs on that environment are a canary. A regression on the tracked metric opens a revert PR.
- **Re-validate on model change:** a new model or harness version re-runs adopted skills' with/without evals. Skills that no longer help are flagged for retirement, following skill-creator's capability-uplift vs encoded-preference distinction.

### 5.4 Labeling UX

**Review tab (dashboard)**

- **Queues by rule:**
  - tool error;
  - retry storm;
  - Decision-1 confidence below 0.7;
  - rules and Decision-1 disagree;
  - verification failed but the agent claimed success;
  - flagged as a possible hack;
  - random sample, about 5%, for unbiased rates.
- **Item views render the content natively:** diffs as diffs, terminal output as a terminal, MCP calls as request and response, the run as a timeline with step labels in colour. Tokens are hidden unless asked for.
- **Keys:** `1–9` pick a category, `A/B/E` choose pairwise, `F` marks the first failure here, `N` adds a note, `Enter` completes and advances, `?` shows the cheatsheet. Pre-labels from Decision-1 (or rules) show with their confidence; confirming one is a single key.
- **Error analysis mode:**
  - read 30–100 failed runs;
  - mark the first failure in each, with a free note (open coding);
  - an assistant clusters the notes into candidate categories with counts;
  - accept a cluster, which becomes a taxonomy entry (axial coding);
  - stop at saturation, when new runs add no categories.
- **Natural-language search over runs:** "runs where the agent re-ran the same failing command more than 3 times." This is a judge plus citations into the trajectory, as in Docent, and is used to bulk-queue or bulk-label.
- **Agreement:** optional blind overlap of 2 reviewers on 10% of items; per-label κ between labelers and humans (0007 §5.5).

**From MCP clients:** `review_next(queue)`, `submit_label(item, label)` and `search_runs(query)` let a person label inside Claude Code or any MCP client, and let an agent help triage. Agent labels are stored as `annotator: llm` and never count as gold.

**Reports built from labels:**

- **Skill quality:** paired with/without, trigger accuracy, label deltas ("stuck-loop ↓ 40% with skill"), cost delta.
- **Tool and API difficulty:** a table per MCP server and tool, and per external API (seen in shell spans):
  - error rate;
  - retries;
  - recovery;
  - error classes;
  - example runs.

  The fix suggestions it produces go into the loop (§5.3).
- **Environment friction:** dependency fights, setup failures, slow steps.

### 5.5 Interop: Harbor, verifiers, ATIF, Agents SDK

> **Moved to flightdeck (2026-10-17, decisions 16 and 17).** Harbor-compatible graded tasks, `save_as_task` (a flightdeck function over hangar's outbox data, `hangar.run/v1`) and the ATIF import and export adapter are flightdeck's. Hangar stores and exports `hangar.trace/v1` (OTLP/JSON on the GenAI conventions) and emits no ATIF; its experiment job takes tasks as `instructions` or an `issue` plus a grader. The text below is kept for history.

Scope: this section is about graded work. Development runs do not need a Harbor task; they use the repo's own checks (`verify=`) and land as branches and PRs. Harbor enters when a run is meant to be scored and compared.

- **Save as task:** `save_as_task(run)` turns a finished development run into a Harbor task. It takes the starting commit as the environment, the issue text as the instruction, and the tests that changed or were added in the merged fix as `tests/`. This is how the team's real work becomes its regression set.
- **One task format:** Harbor's layout is hangar's only task format. Hangar-specific hints (sandbox size, preferred harness) go in a `[metadata.hangar]` table in `task.toml`. Variants, policy, budgets and ticket links belong to the 0007 experiment definition, which points at task directories. Pin a Harbor version and run its task validator in CI.
- **`hangar.harbor:HangarEnvironment`** implements Harbor's `BaseEnvironment` against hangar's REST API, so `harbor run -e hangar.harbor:HangarEnvironment` runs any Harbor task on hangar under its policy. Credentials stay in the Harbor process or hangar's vault, per Harbor's guidance. Capabilities are declared honestly.
- **Tasks:** `evals/semver` is converted to a Harbor task (`task.toml`, `instruction.md`, `tests/test.sh` writing `reward.json` per category). The 0007 experiment runner accepts Harbor task directories as its task-set format.
- **Separate verifier sandbox** by default for graded runs (Harbor's `environment_mode = "separate"`): fresh sandbox, only declared artifacts copied, no network.
- **Trajectories (moved: a flightdeck adapter, see the note above):** each run exports ATIF v1.7 (`trajectory.json`) from the normalized spans (0007 M6 parsers), with `final_metrics` for tokens and cost. OTel stays the live channel. Claims that harnesses export ATIF natively are unverified; hangar converts.
- **`verifiers` v1:** run its environments via Harbor, or directly by implementing its runtime hooks (OQ2).
- **OpenAI Agents SDK:** a hangar sandbox provider, later (M12), so SDK agents get hangar sandboxes.

### 5.6 RL path and reward-hacking defenses

| Stage | What hangar provides | Needs |
| --- | --- | --- |
| **A. Inference-time and context optimization** (closed models, now) | `swarm(best_of_n)` + verifier = test-time scaling; GEPA/ACE proposals (§5.3); routing bandits within experiments | §5.1, 0007 M7 |
| **B. Data** | `export_dataset(filter)` (moved to flightdeck: the ATIF adapter): ATIF trajectories, rewards, labels and config fingerprints; successful-trajectory sets for SFT; preference pairs from best-of-N (verified winner vs loser); erroneous segments masked using labels | M7 (§8) |
| **C. Online RL rollouts** (open models) | Hangar as the environment backend for prime-rl / slime / rLLM: | 0003 M9 proxy, fork, scale targets |
| | • pools and fork for grouped rollouts (same start state); | |
| | • interception through the model gateway (token ids, logprobs, per-rollout id); | |
| | • 512+ concurrent sandboxes; | |
| | • separate verifier sandboxes; | |
| | • reward plus labels returned per rollout | |

**Defenses** (all stages; on by default for graded runs):

1. Tests and grader arrive after the run, or live only in the verifier sandbox (0007 rule 2, Harbor separate mode).
2. Egress off for graded runs, or a fixed allowlist recorded in the run.
3. **Answer-key scan:** before an experiment, scan the sandbox image and mounted packs for the task's reference artifacts. This is the semver lesson: node-semver was in pi's pack.
4. Test-file diff check: a run that changed tests, grader files, `conftest.py` or CI config is labeled `test-tampering` and its reward voided.
5. Hack detector: a Decision-1 or LLM-judge label `reward-hack` (hard-coded outputs, `sys.exit(0)`, monkey-patched evaluators) over the trajectory and diff.
6. An explicit `abort` / "task is impossible" action counts as a valid outcome, not a failure. This reduced cheating from 54% to 9% in the cited study.
7. Patch exploits in the environment rather than penalizing them, since penalties teach stealthier hacks (METR). Every detected exploit opens an issue against the task.

### 5.7 Security and observability at swarm scale

- **No secrets in sandboxes:** credentials are injected at the egress and MCP gateways (0003, 0007 §5.1), the same pattern as Cloudflare's outbound workers and OpenAI's guidance.
- **Per-swarm token scopes and budgets:** a swarm token can create at most N sandboxes, spend at most $X, and use only the declared environments. Swarms inherit the caller's policy and can only narrow it.
- **Isolation:** gVisor by default on GKE; Kata or microVM for untrusted multi-tenant work; Firecracker backends for memory fork.
- **Observability:** hangar emits OTel spans per sandbox lifecycle step (claim, fork, exec, verify) and per MCP and tool call, with GenAI-style attributes. ATIF per run (moved: a flightdeck adapter over `hangar.trace/v1`). A swarm view shows a tree of sandboxes with status, cost and reward.

## 6. Security, privacy and failure modes

- **Runaway fan-out:** hard caps per token and per swarm, a cost gate before large swarms, and `cancel_swarm` kills the whole tree.
- **Cross-sandbox leakage:** sandboxes in a swarm share nothing except declared artifacts. Forks inherit the parent's disk, so 0006's secret scrubbing applies to every fork.
- **Training-data privacy:** dataset export is opt-in per repo and scrubbed (0005 §5.4). Exports carry provenance and the policy each run ran under.
- **Labeler bias:** agent labels are never gold. Random-sample queues give unbiased rates next to the rule-routed queues.

## 7. Open questions

1. ~~Harbor's task format as the only task format?~~ **Decided (owner, 2026-10-12): yes, for graded work only.** *Restated 2026-10-17 (decision 16): graded-task formats are flightdeck's, Harbor-compatible.* Development work keeps GitHub issues, 0004 tasks and the repo's own checks. Hangar hints go in `[metadata.hangar]`; experiment settings stay in 0007's experiment definition (§5.5).
2. `verifiers` integration through Harbor, or natively? Decide after M6.
3. ~~Memory-fork backend?~~ **Decided (owner, 2026-10-12):** disk-level forks on GKE are the default. Memory fork is opt-in for workloads that need process state: GKE Pod Snapshots first, E2B as the fallback, Morph only if M2 shows a gap neither closes. M2 benchmarks time to N ready forks (N = 1, 10, 50), cost per fork-hour, whether a running process survives, failure rate, and whether it works under gVisor with policy enforced. Run it after 0007 M0 so SSD quota doesn't skew the GKE numbers.
4. Which open model and trainer for stage C, and on what GPU budget? Out of scope until stage B data exists.
5. Should the improvement loop's L2 `staging` channel exist at all, or is L1 (PR only) enough?

## 8. Milestones

Sizes: S ≤ 1 day, M ≈ 2–3 days, L ≈ 4–6 days. These come after 0007 M0–M2 (capacity, async create, builds).

| # | Milestone | Size | Depends on |
| --- | --- | --- | --- |
| M1 | `nano` profile, `exec_many`, and pools per (env, harness, profile) | M | 0007 M0–M1 |
| M2 | `fork_sandbox(kind=…)`: disk fork on GKE (default) and Docker, memory fork via GKE Pod Snapshots and E2B; benchmark per §7 OQ3 | M | 0007 M0 |
| M3 | `swarm` with `best_of_n` and `map`, server-side loop, progress notifications, ranking, merge of map shards | L | M1, M2, 0007 M3 (budgets) |
| M4 | Harbor `BaseEnvironment` adapter; `evals/semver` as a Harbor task; `save_as_task`; separate verifier sandboxes. **Moved to flightdeck (2026-10-17); hangar's sealed grader sandboxes (#126) cover the separate verifier.** | M | — |
| M5 | ATIF export from normalized spans; `export_dataset`. **Moved to flightdeck (2026-10-17): an ATIF adapter over `hangar.trace/v1`.** | M | 0007 M6 |
| M6 | Review tab: rule-routed queues, pre-labels, hotkeys, first-failure marking, MCP review tools | L | 0007 M8 |
| M7 | Error-analysis assistant (clustering notes → taxonomy), natural-language run search | M | M6 |
| M8 | Reports: skill quality, tool and API difficulty, environment friction | M | M6, 0007 M7 |
| M9 | Improvement loop: nightly detect → propose (ACE deltas, GEPA) → evaluate (splits) → PR → monitor; L0–L1 | L | M8, 0007 M7, M10 |
| M10 | Reward-hacking defenses: answer-key scan, test diff check, hack-detector label, abort action | M | M4 |
| M11 | Stage C readiness: model-interception gateway (tokens, logprobs), rollout ids, 512-concurrent load test | L | 0003 M9, M2, M3 |
| M12 | OpenAI Agents SDK provider | M | M3 |

## 9. Acceptance

1. `swarm(best_of_n, n=10)` on a real hangar GitHub issue returns 10 attempts checked by the repo's own tests, and the best one opens a PR. Claim plus disk fork completes in under 15 s on a warm GKE pool.
2. `swarm(map, n=20)` over a real multi-file change in hangar produces one combined branch with per-shard conflicts reported.
3. `harbor run -e hangar.harbor:HangarEnvironment` runs the semver Harbor task and writes `reward.json`.
4. A reviewer labels 50 queued items in under 5 minutes with keys alone.
5. The nightly loop opens one PR with evidence and a held-out result; a deliberately bad proposal is rejected on validation.
6. A planted answer key in an image is caught by the scan before the experiment starts.

## 10. Sources

Research briefs of 2026-10-12. Harbor interface, task, verifier and ATIF details were checked at the source; the rest is from the briefs' cited pages.

- **R1: RL and self-improvement:**
  - [Harbor custom sandboxes](https://docs.harborframework.com/sandboxes/custom-sandboxes.md)
  - [Harbor verifier](https://docs.harborframework.com/tasks/verifier.md)
  - [ATIF](https://docs.harborframework.com/agents/atif) (now the flightdeck adapter's reference)
  - [verifiers v1](https://docs.primeintellect.ai/verifiers/v1/overview)
  - [verifiers GEPA](https://docs.primeintellect.ai/verifiers/v1/gepa.md)
  - [DeepSWE](https://together.ai/blog/deepswe)
  - [GLM-5](https://arxiv.org/pdf/2602.15763)
  - [Cursor real-time RL](https://cursor.com/blog/real-time-rl-for-composer)
  - [ImpossibleBench](https://www.greaterwrong.com/posts/qJYMbrabcQqCZ7iqm/impossiblebench-measuring-reward-hacking-in-llm-coding-1)
  - [METR reward hacking](https://metr.org/blog/2025-06-05-recent-reward-hacking)
  - [GEPA](https://www.alphaxiv.org/abs/2507.19457v2)
  - [ACE](https://huggingface.co/papers/2510.04618)
  - [SkillsBench](https://arxiv.org/html/2602.12670v1)
  - [Nebius OpenHands trajectories](https://nebius.com/blog/posts/openhands-trajectories-with-qwen3-coder-480b)
- **R2: sandboxes:**
  - [GKE Agent Sandbox and Agent Substrate](https://cloud.google.com/blog/products/containers-kubernetes/bringing-you-agent-sandbox-on-gke-and-agent-substrate)
  - [GKE Pod Snapshots benchmarks](https://infoq.com/news/2026/09/gke-pod-snapshots-benchmarks)
  - [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
  - [E2B snapshots](https://docs.e2b.dev/sandbox/snapshots)
  - [Morph branch](https://cloud.morph.so/docs/documentation/instances/branch)
  - [Modal sandbox snapshots](https://modal.com/docs/guide/sandbox-snapshots)
  - [Vercel Sandbox snapshots](https://vercel.com/docs/vercel-sandbox/concepts/snapshots)
  - [OpenAI Agents SDK sandboxes](https://openai.com/index/the-next-evolution-of-the-agents-sdk/)
  - [BuildBuddy fast runners](https://www.buildbuddy.io/blog/fast-runners-at-scale)
  - [SOCI lazy loading](https://arxiv.org/abs/2607.06868)
  - [Firecracker page-fault handling](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/handling-page-faults-on-snapshot-resume.md)
- **R3: labeling and loops:**
  - [Langfuse annotation queue shortcuts](https://langfuse.com/changelog/2026-06-26-annotation-queue-keyboard-shortcuts.md)
  - [LangSmith annotation queues](https://docs.langchain.com/langsmith/annotation-queues)
  - [Braintrust human review](https://www.braintrust.dev/docs/core/human-review)
  - [Phoenix annotations](https://arize.com/docs/phoenix/tracing/concepts-tracing/annotations-concepts.md)
  - [Docent search and clustering](https://docs.transluce.org/analysis/search-and-clustering.md)
  - [Error analysis (Husain)](https://hamel.dev/blog/posts/evals-faq/why-is-error-analysis-so-important-in-llm-evals-and-how-is-it-performed.html)
  - [EvalGen](https://arxiv.org/pdf/2404.12272)
  - [MAST taxonomy](https://arxiv.org/html/2503.13657v3)
  - [Issue-solving failure taxonomy](https://arxiv.org/html/2509.13941v1)
  - [skill-creator evals](https://claude.com/blog/improving-skill-creator-test-measure-and-refine-agent-skills)
  - [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
