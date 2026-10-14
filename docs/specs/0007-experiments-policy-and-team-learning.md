# 0007: Experiments, enforced policy, MCP gateway and team learning

Status: draft · Date: 2026-10-12 · Builds on: 0003 (gateway, egress), 0004 (performance, supervisor), 0005 (traces, decisions, evals), 0006 (environments, harness packs) · Evidence: the semver harness eval of 2026-10-12 (`evals/semver/`, PR #28)

> Part of a set. Ownership, cross-spec decisions and the combined build order are in [docs/specs/README.md](README.md); where this spec conflicts with that page, the page wins.

## 1. Summary

- **MCP servers in sandboxes, with credentials outside them.** Environments already declare MCP servers (`mcp:`, spec 0006), written into each harness's own config. Today the server's credential lands in a file the agent can read. A per-sandbox **MCP gateway** sidecar holds the credentials instead, enforces a tool allowlist, and turns every MCP call into a span. On E2B, hangar maps `mcp:` onto E2B's native Docker MCP gateway.
- **One policy object, enforced at four points.** Egress, MCP tools, filesystem (which packs are mounted), harness permission rules, model allowlist and budgets (time, steps, tokens, dollars) live in one `policy:` block. The network, the gateways, the rendered harness config and the run watcher each enforce their part. Every run records the policy it ran under.
- **Experiments over environment variants.** A variant is a base environment plus an overlay: a harness, model, skill set, MCP set, policy or instructions change. An experiment runs task × variant × trial in parallel under identical conditions, grades from outside the sandbox, and reports paired effect sizes. This generalizes 0005's two-config eval runner.
- **Traces, labels and lifecycle links.** All four harnesses' streams, MCP calls, git and CI events become one span schema. Each run is linked to a ticket and an objective. Labels come from a cascade: rules, then Decision-1, then an LLM judge, then human review. Each labeler's agreement with human labels is measured.
- **Team learning, as reviewed code.** Patterns mined from traces, PR reviews and outcomes become *proposed* changes to skills, instructions, environments and policy. Each proposal goes through an experiment before a human merges it into the team environment repo. "How we work" is versioned, evidenced and reviewable.
- **Performance work comes first.** It is what makes all of the above affordable: async creates, capacity, cached builds, warm variant pools and one-call experiments.

## 2. Problem and evidence

From the 2026-10-12 semver eval: two harnesses, two trials each, same model, 116 hidden tests.

| # | Observation | Why it matters here |
| --- | --- | --- |
| 1 | All four trials scored 116/116. pi cost $0.94 per trial and Antigravity about $1.17 (estimated). | Outcome metrics alone don't separate configurations; process metrics and harder tasks do (§5.4). |
| 2 | Within-harness spread (52 vs 82 calls; 54 vs 84) was as large as between-harness spread. | Single runs are anecdotes. Experiments need repeats and paired analysis (§5.4). |
| 3 | pi 1 spent 60 of 82 calls on node-semver, which ships inside pi's own pack. Every sandbox mounts all four packs. | Environment leakage. Variants must control exactly what is on disk (§5.2). |
| 4 | Antigravity read node-semver's docs on the open web. | Egress is an experimental variable and must be fixed per experiment (§5.2). |
| 5 | Decision-1 labeled 272 tool calls for $0.003. It agreed with keyword rules on 52–95% of each trial's calls, and its efficiency score barely moved between runs. | Cheap labels are viable, but need a taxonomy, a cascade, confidence routing and a human gold set (§5.5). |
| 6 | MCP credentials from `mcp:` are written into a 0600 file in the sandbox (`fleet._apply_session`). | The agent can read and exfiltrate them; 0003's threat model applies (§5.1). |
| 7 | The eval took about 40 manual steps and about 90 minutes. Creates hung at the 230 s tool timeout because a second node could not start (Google Cloud SSD quota, 320/500 GB). | Performance and capacity gate everything else (§5.3). |

## 3. Goals / non-goals

Goals:

- Compare configurations fairly: same task, same pinned image and packs, same policy; only the declared overlay differs.
- Enforce what an environment says. A declared limit is a limit, not a hint to the agent.
- Link every run to why it ran (ticket, objective) and to what happened (PR, review, CI, revert).
- Turn repeated evidence into reviewed changes to the team's environments, skills and practices.
- Run a 2 × 2 × 5 experiment (20 runs) in one call, at well under an hour of wall time.

Non-goals (v1):

- Automatic adoption of any learning. Humans merge.
- Monitoring individual people. Team-level learning only, from opt-in sources (§6).
- A general-purpose experimentation platform. Experiments are about agent work in hangar sandboxes.
- Training models. Labels feed classifiers, routing and reports, not fine-tuning.

## 4. User stories

1. "Run the same 10 hangar tickets on Claude Code with and without our `repo-conventions` skill, 5 times each, and tell me whether the skill helps." One call, a report with effect size and interval.
2. "Same environment, same model; Codex vs pi as the harness." The variant overlay changes only `harness`.
3. "No sandbox may reach the internet except package registries, and agents can use only the read tools of the GitHub MCP server." Declared once in the environment, enforced, visible in each run's record.
4. "Which of last month's agent runs got stuck re-reading the same files, and what did they have in common?" Labels plus a query.
5. "Our reviewers keep asking agents to add migration tests." That appears as a proposed instruction line with evidence, tested in an experiment, then merged into the team environment repo.

## 5. Design

### 5.1 MCP in sandboxes: gateway, catalog, spans

**Today (0006):** `mcp:` servers are rendered into each harness's config at claim. Remote servers get `${VAR}` headers resolved from the vault, and stdio servers must be installed in the image. This works now; it is how you add MCP servers to an environment.

**Target:** a per-sandbox **MCP gateway** that becomes the only MCP endpoint the agent sees.

```
agent (any harness) ──http──► localhost:7777/mcp  (gateway sidecar, same pod, separate container)
                                  │ holds credentials (from the vault, never in the agent's filesystem)
                                  │ enforces policy.mcp: servers, tools allow/deny, rate, arg patterns
                                  │ emits one span per tool call → run_events / traces.db (0005)
                                  ├──► remote MCP servers (HTTPS, through 0003 egress)
                                  └──► stdio servers run inside the sidecar (catalog images' commands)
```

- The rendered harness config points every server at the gateway with a per-sandbox bearer token. Agents never see upstream credentials. This extends the credential gateway in 0003 rather than replacing it: same grant model, new protocol.
- **Implementation choice:** start from Docker's open-source MCP gateway (`docker/mcp-gateway`). Its catalog, secret injection, tool filtering and call interceptors match the requirements, but this needs verifying (OQ1). Fallback: a small hangar proxy built on the MCP Python SDK.
- **Catalog:** `mcp:` entries may name a catalog server (`github`, `notion`, `exa`, `browserbase`, …) in place of a URL. The gateway resolves the image or command and the credential fields, the way E2B's `Sandbox.create({mcp: {...}})` does.
- **Kubernetes/gVisor:** sidecar container in the sandbox pod. It shares the network namespace, so the agent reaches it on localhost, and has its own filesystem. Admission-policy compliant: non-root, no capabilities, limits set.
- **Docker:** the same sidecar as a second container on a per-sandbox network.
- **E2B:** map `mcp:` onto E2B's native gateway, `Sandbox.create(mcp=...)` with the URL from `sandbox.get_mcp_url()` and the token from `get_mcp_token()`. Policy there is limited to what E2B exposes, and the report says so.
- **Spans:** `mcp.call {server, tool, args_digest, args_preview (scrubbed), ok, duration_ms, bytes_out}`. These are harness-neutral, so MCP behaviour can be compared across harnesses even where the harness's own stream is thin.

### 5.2 Policy: one block, four enforcement points

```yaml
policy:
  egress: pkg-only                 # 0003 presets: none | pkg-only | custom | open (+ allow list)
  mcp:
    github: {tools: [get_issue, list_pull_requests, get_file_contents]}   # allowlist; default deny
    exa: {deny: ["*"]}
  packs: selected                  # selected (only the run's harness is mounted) | all
  harness_permissions:             # rendered into each harness's own permission system
    deny_commands: ["git push --force*", "curl * | bash"]
    write_paths: ["/workspace/**"]
  models: ["opus", "sonnet", "openrouter/google/*"]
  budget: {wall_min: 30, tool_calls: 150, usd: 3.00, tokens: 2_000_000}
```

| Rule | Enforced by | How |
| --- | --- | --- |
| `egress` | Network | 0003 egress proxy and NetworkPolicy; deny is enforced outside the sandbox |
| `mcp` | MCP gateway (§5.1) | Tool calls outside the allowlist return an MCP error and are logged |
| `packs` | Provider | Pod spec mounts only the selected pack. Warm pools keep one pool per (environment, harness) when `selected`, an extra cost the scheduler accounts for (§5.3). |
| `harness_permissions` | Harness config, best effort | Claude Code `permissions.deny` in managed settings; Codex sandbox and approval config; pi `--tools`/`--exclude-tools`; agy `--mode` and its permission rules. Not a security boundary: the network and gateways are. Violations are visible in spans. |
| `models` | Run launcher + model-API proxy (0003 M9, if it ships) | `run_agent` rejects other models; the proxy blocks them if present |
| `budget` | Run watcher (0001 supervisor) | Counts from live events; at 100% sends interrupt, then cancel; at 80% warns. Partial results are kept and graded. |

The resolved policy, with defaults filled in and the hash of each part, is stored on every run as `Run.policy` and is part of the config fingerprint (0005 §5.8 rule 4). Two runs with different policies are never pooled in an experiment.

### 5.3 Performance: what made today slow, and the fix order

| Bottleneck (measured 2026-10-12) | Fix | Target |
| --- | --- | --- |
| Second node refused: SSD quota 320/500 GB, 200 GB boot disk per sandbox node | Sandbox pool disk 100 GB; request a 1 TB quota; alert on `FailedScaleUp` | Up to 4 nodes in quota |
| Each sandbox reserves 2 CPU while mostly waiting on the model | Requests 0.5 CPU / 2 GiB, limits 2 CPU / 4 GiB, if the Agent Sandbox add-on accepts it (OQ3) | 3–4× sandboxes per node |
| `create_sandbox` blocks and is cancelled by the client's timeout | Async create: return `pending` after about 20 s with the scheduler's reason; `wait_sandbox`; `asyncio.shield`; fail fast on quota or unschedulable | No lost creates; reason within 10 s |
| Image builds on a laptop: about 3–5 min, no cache | Cloud Build with registry cache in the cluster (apply PR #9 terraform); CI publishes default environments and packs on merge | Cached rebuild under 60 s; no laptop step |
| Cold claims: node scale-up, pulls | Warm pools per experiment variant during an experiment; pack pre-pull DaemonSet; GKE image streaming | Claim in 1–3 s warm, under 40 s cold |
| Stale warm pool held 2 CPU | Retire pools whose image or pack is outdated | 0 stale pools |
| Orchestration: about 40 manual calls, sleep-polling, base64 file shuttling | `run_experiment` (§5.4) does fan-out, budgets, grading and trace collection server-side; `wait_run` long-poll; completion notifications (0002 M8) | 1 call + 1 notification |
| Agent wander (138-step Antigravity run; 60-call reference lookup) | Budgets (§5.2), supervisor detectors (0004), harness effort settings per variant | Capped, flagged |

### 5.4 Experiments (A/B testing with environments)

**Variant.** A base environment plus an overlay, with everything pinned. The variant id is the hash of the resolved spec, image digest, pack digests, harness version, model id and thinking level, policy hash and instructions/skills hashes.

```yaml
experiment: skills-repo-conventions
base: {environment: hangar, harness: claude-code, model: sonnet}
variants:
  control: {}
  with-skill: {skills: [{path: skills/repo-conventions}]}
tasks: tasksets/hangar-tickets-2026-10.yaml   # each: prompt or ticket ref, base_sha, grader
trials: 5
policy: {egress: pkg-only, packs: selected, budget: {wall_min: 30, usd: 2.50}}
schedule: {parallel: 8, interleave: true}    # run variants of one task side by side
metrics: [verified_resolved, cost_usd, wall_s, tool_calls, labels.stuck_loop]
```

Rules, extending 0005 §5.8:

1. **Only the overlay differs.** Same base image digest, same pack digests, same policy and pod shape for every variant. The runner refuses an experiment whose variants differ in anything undeclared.
2. **Graded outside the agent's reach.** The grader (hidden tests, a verify command, or a 0002 verification) is copied in after the run ends, or runs in a separate clean sandbox from the agent's commit. The agent never has it on disk or reachable over egress.
3. **Interleaved, concurrent.** Trials of different variants on the same task run at the same time, so provider latency and rate limits hit all variants alike.
4. **Paired analysis per task** (0005 rule 2): difference in each metric across trials, a paired bootstrap over tasks, effect size with a 95% interval, n, and the power statement. Process metrics (calls, cost, time, labels) are reported the same way, because outcomes saturate (evidence #1).
5. **Task sets with headroom.** A task where every variant scores 100% is flagged "saturated" and reported, but excluded from the outcome comparison. Task sources:
   - graded synthetic tasks like `evals/semver`;
   - real tickets replayed at their `base_sha`, graded by the merged fix's tests;
   - SWE-style repo tasks.
6. **Cost gate** (0005 rule 7): projected runs × tokens × price and plan-quota use, confirmed before start.

`run_experiment` (MCP + REST) returns an id. `experiment_status` and `experiment_report` return the paired table, per-variant metrics, label distributions, saturated tasks, and links to every run's trace. Reports render in the dashboard and export as a doc.

### 5.5 Traces, labels and lifecycle links

**One span schema** across harnesses: 0005's `run_events` and `traces.db`, with OpenTelemetry GenAI-style attribute names (`gen_ai.request.model`, `gen_ai.usage.*`).

| Source | Parser | Status |
| --- | --- | --- |
| Claude Code stream-json + OTel | 0001 M3, 0005 M2 | planned |
| Codex `exec --json` | new | planned |
| pi `--mode json` | `evals/semver/spans.py` → `runevents.py` | prototype |
| Antigravity `stream-json` | `evals/semver/spans.py` → `runevents.py` | prototype |
| MCP gateway | §5.1 | new |
| git, CI, PR review, merge, revert | 0002 collector + GitHub webhooks | planned |

**Lifecycle links.** `Run.task_id → Task → ticket_ref` (a GitHub issue; 0004 tasks) `→ objective_ref`. Linking happens at creation (`run_agent(ticket=...)`), or after the fact from PR bodies and branch names. Outcomes flow back to the run: verified, merged, review rounds, CI result, reverted within 14 days, follow-up fix PR. Lifecycle metrics per objective and per environment:

- ticket → merged PR lead time;
- review rounds per PR;
- rework rate (revert or follow-up fix);
- cost per merged PR.

**Label taxonomy** (versioned file, `labels/taxonomy.yaml`):

| Level | Labels |
| --- | --- |
| Step (one span) | explore, reference, plan, edit, verify, test, vcs, wait, environment-fix, other |
| Episode (window of spans) | stuck-loop, oracle-fixation, scope-creep, test-gaming (editing tests to pass), premature-done (claims success without a passing run), unsafe-action, dependency-fight |
| Run | outcome (verified, failed, abandoned), strategy (reference-driven, implement-then-verify, explore-then-implement, trial-and-error), answer-key risk |

**Labeler cascade**, cheapest first, each with measured agreement:

1. Rules (free, deterministic). These cover most step labels.
2. Decision-1 (calibrated probabilities, about $0.00001 per span). Used where rules abstain or disagree. Confidence below 0.7 escalates.
3. LLM judge with rationale (a stronger model). Used for episode and run labels and escalations.
4. Humans. A sampled gold set of about 200 spans and 50 runs per taxonomy version, plus active learning that picks the most uncertain items.

Agreement per labeler versus the gold set (Cohen's κ, calibration curves) is shown in the dashboard. A labeler whose κ falls below 0.6 on a label is not used for that label. This builds on 0005 §8.3's offline evaluation.

**Algorithms on top of labels:**

- **Supervisor detectors** (0004): stuck-loop and oracle-fixation detectors on live spans, plus budget forecasts.
- **Process mining:** phase sequences (step labels in order) across runs, clustered. Which sequences predict verified outcomes per task type, and which predict waste.
- **Routing:** 0005's Beta-binomial recommendation, extended to a contextual bandit over variants inside explicit experiments only (exploration costs quota).
- **Environment-fix and instruction suggestions:** 0005 §5.8, now with labels as evidence ("dependency-fight in 6 of 9 runs on env X").

### 5.6 Team learning: capturing how we work

The idea: practices that work are observed in traces and outcomes, proposed as code, tested in experiments, and adopted by review.

```
evidence ─────────────► candidates ───────────► experiment ───► PR to team env repo ───► adopted
 traces + labels         "add 'run make            A/B on a         skills / AGENTS.md /     version-tagged,
 PR reviews/comments      migrate-test' to          held-out         env.yaml / policy        evidence linked
 CI + revert history      instructions"             task set
 (opt-in) human sessions  with evidence runs
```

- **Sources** (all opt-in at the repo or team level):
  - agent traces;
  - PR review comments and their resolutions;
  - CI failures;
  - reverts;
  - postmortems;
  - Claude Code OTel from human developers' own sessions, only with each person's consent and team-level aggregation.
- **Mining:** recurring review requests (clustered review comments with the PR diffs that satisfied them); recurring failure signatures; label patterns that predict rework; skills or instructions whose presence correlates with verified outcomes.
- **Learning records:** `Learning {id, claim, evidence_runs[], evidence_reviews[], proposed_change (diff to a skill / instructions / env / policy), status: candidate → testing → adopted | rejected, experiment_id}`.
- **Adoption** is a PR to the team environment repo (0006 M4). The change becomes part of every environment that inherits it. Its experiment result and evidence are linked in the PR body. Anything not adopted is kept with its result, so the same idea isn't re-tested blind.
- **Answering "is that possible with E2B's MCP support?"** Partly. E2B + Docker's MCP support gives sandboxes tools through a gateway, and a gateway sees every tool call, so MCP traffic becomes evidence. It doesn't capture practices, link work to tickets, test changes or adopt them. That is the hangar layer above (§5.4–5.6). E2B stays a provider, and its MCP gateway is one way to supply tools to a sandbox (§5.1).

## 6. Security, privacy and failure modes

- **Credentials:** MCP credentials move from agent-readable files (today) to the gateway sidecar. Until §5.1 ships, `mcp:` with secrets is documented as "the agent can read this credential".
- **Policy is not a sandbox:** harness permission rules are best effort (row 4 of §5.2's table). The enforced boundaries are the network, the gateways and the provider.
- **Experiments spend money and quota:** cost gate, per-experiment budget, and a kill switch (`cancel_experiment`).
- **Team-learning privacy:**
  - human sessions only with individual opt-in;
  - reports are team-level, never per-person rankings;
  - 0005's scrubbing runs on all sources;
  - review comments are quoted only inside the repo's own access boundary;
  - everyone can see which sources are on.
- **Label drift:** taxonomy and labeler versions are stored on every label. A version change recomputes κ before the new labels are used.

## 7. Milestones

Sizes: S ≤ 1 day, M ≈ 2–3 days, L ≈ 4–6 days.

| # | Milestone | Size | Depends on |
| --- | --- | --- | --- |
| M0 | Capacity and quota: smaller node disk, quota request, CPU requests below limits (or documented why not), stale pool retirement | S | — |
| M1 | Async create, `wait_sandbox`, fail-fast scheduling reasons | M | — |
| M2 | Builds without a laptop: in-cluster Cloud Build with cache; CI publishes packs and default envs on merge | M | PR #9 terraform applied |
| M3 | `policy:` block, schema and resolution, stored on runs; `packs: selected`; budgets in the run watcher | M | 0001 M2 |
| M4 | Harness permission rendering for four harnesses; model allowlist | M | M3 |
| M5 | MCP gateway sidecar (Docker MCP gateway or own), credentials out of the sandbox, tool allowlists, `mcp.call` spans; E2B native mapping | L | M3, 0003 M2 |
| M6 | Parsers for Codex, pi and Antigravity into `run_events` (from `evals/semver/spans.py`) | M | 0001 M3 |
| M7 | `run_experiment`: variants and overlays, pinning, interleaving, grader injection, paired report; absorbs 0005 M10 | L | M1, M3, M6 |
| M8 | Label taxonomy, rules and Decision-1 cascade, gold-set labeling UI, κ dashboard | L | 0005 M4–M5, M6 |
| M9 | Lifecycle links: ticket and objective refs, outcome back-propagation (merge, review rounds, revert) | M | 0002 M2 |
| M10 | Learning records, miners (review clusters, failure signatures, label patterns), proposal → experiment → PR | L | M7, M8, M9, 0006 M4 |

Suggested order: M0–M2 now (performance), then M3–M4 and M6, then M7 (the first real experiment: skills on/off on hangar tickets), then M5, M8, M9, M10.

## 8. Acceptance (live, GKE)

1. A 2 variant × 5 task × 3 trial experiment (30 runs) starts in one call and finishes in under 60 min, with no manual steps.
2. With `packs: selected`, `ls /opt/hangar/harness` in a sandbox shows one directory.
3. With `egress: pkg-only`, `curl https://example.com` fails in the sandbox, and a pip install succeeds.
4. A GitHub MCP tool outside the allowlist returns an error, and the call appears as a denied `mcp.call` span. The upstream token is not on the sandbox's filesystem.
5. A run that exceeds `budget.tool_calls` is interrupted, then cancelled, and its partial result is graded.
6. The report shows the paired effect for `verified_resolved` and `cost_usd`, with intervals, and flags saturated tasks.
7. One learning goes end to end: candidate (with evidence) → experiment → PR to the team env repo.

## 9. Open questions

1. Does Docker's MCP gateway run as a non-root sidecar under gVisor and accept per-sandbox tokens and tool filters? If not, how big is a minimal hangar gateway?
2. Can the GKE Agent Sandbox `SandboxTemplate` express a sidecar container? If not: use the OSS controller, or a gateway pod per sandbox reached over a NetworkPolicy-pinned service.
3. Does the add-on's admission policy accept CPU requests below limits?
4. ~~Which ticket system first?~~ **Jira and GitHub Projects**, the trackers the owner's teams use (owner, 2026-10-14), through one source-agnostic intake keyed by repo (repo-policy.md). GitHub Issues already work through the GitHub trigger.
5. Who curates the gold label set, and how much human labeling time per taxonomy version is acceptable?
6. Human-session capture: is anyone besides the owner in scope for v1?
