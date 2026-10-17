# 0002: Run results, verification, fan-out grading and the land step

Status: partly built, in another shape (2026-10-15): verification with the repo's own check (0009, #82) and landing on approval through a GitHub App (#96, #101; [landing.md](../landing.md)). Was: proposed. · Date: 2026-10-12 · Author: hangar maintainers

> Part of a set. Ownership, cross-spec decisions and the combined build order are in [docs/specs/README.md](README.md); where this spec conflicts with that page, the page wins.

## 1. Summary

- Hangar collects each run's branch, commits, diff, PR and CI checks itself, from git and GitHub. It never parses the agent's prose.
- Hangar runs the environment's verify command in a clean sandbox after the agent finishes, so "done" no longer depends on the agent's word.
- `fanout_start` runs N attempts, grades them (verification, CI, diff size) and returns a ranked list with reasons.
- `land_request` waits for CI, updates the branch, detects conflicts, and merges only after a human approves. Hangar holds the merge credential. The agent holds none.
- A git push proxy limits each sandbox to its own branch. Notifiers (dashboard, webhook, Slack, ntfy, email) tell a human when to act.

## 2. Problem and evidence

The first live run (GKE, 2026-10-11) worked for provisioning (2 sandboxes in 3.1 s). It failed everywhere after the agent started. The table maps each observation to the code that caused it.

| Observation | Cause in current code |
| --- | --- |
| Dead agents reported `running` for 10+ minutes | `fleet.py:run_agent` writes `<run>.exit` from inside the tmux script, after the pipe closes. If tmux dies, nothing writes it. `fleet.py:agent_status` treats a missing file as `RUNNING` forever. |
| The orchestrator wrote its own stream-json parser | `agent_status` returns `output_tail`, a raw text tail. `harnesses.py` runs Claude with `--output-format stream-json --verbose` but has no adapter for the output. |
| 7-day usage at 91% was never surfaced | The `rate_limit_event` sits in the log. `fleet.py:overview` has no usage data. |
| Recovery meant a new run with a re-explained prompt | A run is a dict in `Sandbox.info["runs"]` with `id, harness, started_at, prompt[:200]`. No session id, no base sha, no result (`fleet.py:run_agent`). |
| PR URL scraped from the agent's message | No model holds git or PR facts (`models.py` has `Sandbox`, `Snapshot`, no `Run`). `mcp_server.py:get_sandbox` returns the last 10 run dicts. |
| Merging was manual, nothing waited for CI | No landing code exists in `fleet.py`, `mcp_server.py` or `app.py`. |
| `uv sync --all-extras` on every sandbox | `fleet.py:_setup_repo` runs `repo.setup` on each claim unless `repo.bake` is true. `envs/claude-python.yaml` sets no repo and no bake. |
| PAT and OAuth token readable by the agent | `harnesses.py` git slot `post_inject` writes `~/.git-credentials` and `GH_TOKEN`. `broker.py:inject` writes `~/.hangar/env`. Both are in the agent's home. |
| Every API token is full admin | `models.py:ApiToken.scopes` defaults to `["admin"]`. `auth.py` stores scopes but nothing checks them. `app.py:actor` only calls `verify`. |
| Work can vanish at TTL expiry | `fleet.py:_reaper_loop` calls `destroy` on expired sandboxes. Unpushed commits go with the pod. |

Two further findings from the run:

- The hangar test fake ran a real `tmux kill-server`. Fixed in 47a9a0a (`TMUX_TMPDIR` per fake box in `tests/conftest.py`). The same class of failure (any process killing the agent's tmux) will recur, so hangar must detect it.
- `main` had no branch protection, so the PAT could push to main or merge PRs.

Research this spec follows:

- **Reliability is a system property** (Jarmak, arXiv 2608.13867). Verify the workspace, not the self-report. One model "submitted" 100% of trials but resolved 44% under an external oracle. Single runs vary by 2–6 points, so compare repeats. Keep recovery outside the failure domain and make retried steps idempotent.
- **Credentials stay out of the sandbox** (Anthropic, Claude Code sandboxing). A proxy authenticates the sandbox with a scoped credential, checks the request (for example, push only to the configured branch), then attaches the real token.
- **Copilot cloud agent model** (GitHub docs, risks and mitigations). One branch, no approve or merge, the requester cannot approve, CI waits for a human, commits link to session logs.
- **Lethal trifecta** (Willison) and **egress defaults** (WorkOS, Codex cloud internet access). Remove one leg and justify open egress. This spec removes the credential leg for git. It does not solve egress (see Open questions).
- **MCP security best practices** (spec 2025-11-25). Minimal baseline scopes, step-up by scope challenge, no token passthrough, sessions are not auth.

## 3. Goals / non-goals

Goals:

1. A first-class `Run` record with a `RunResult` that hangar builds from git and GitHub.
2. Independent verification that does not depend on the agent.
3. Fan-out of N attempts with deterministic, explainable ranking.
4. A land step: CI wait, branch update, conflict detection, merge after explicit human approval, with hangar's own credential.
5. Agents can push only to their own branch and can never merge, on every provider.
6. A pluggable notifier so humans learn when they are needed.
7. Respect one shared Claude Pro subscription: cap concurrent agents and stop starting runs when usage is high.
8. All existing MCP tools keep working (see 5.11 for the exact deltas).

Non-goals:

- An LLM judge or semantic grading. Grading here is mechanical. A `scorer_command` hook can come later.
- Egress filtering (a separate spec). Resumable agent conversations (this spec only stores `harness_session_id`).
- Hosts other than GitHub in v1. The `GitHost` interface leaves room for GitLab and Gitea.
- Replacing GitHub branch protection. Hangar adds a gate. It does not remove the need for rulesets.

## 4. User stories

Orchestrator agent over MCP:

- As an orchestrator, I call `fanout_start(n=3)` and get back one id. `fanout_status(wait_seconds=60)` returns when something changes, with a ranking and reasons. I never read logs.
- I call `run_result(run_id)` and get commits, diff stat, PR, checks and verification as JSON. A run that died is `lost`, with whatever commits exist.
- I call `fanout_pick(land=true)`. Hangar opens a PR for the winner and queues it for landing. I poll `land_status` and see `awaiting_approval`. I cannot approve. I tell the user to look.
- I get `conflict` with the conflicting files and the PR that caused it. I may start a follow-up run to resolve it.

Human on the dashboard:

- I open **Inbox** and see "run-1a2b: ready to land, CI green, verification passed, +41/−7 in 3 files". I read the diff link, click **Approve**, and hangar merges.
- I see why an attempt ranked first or last, and I can pick a different one.
- I see Claude subscription usage and how many runs are queued behind it.
- I get a Slack or phone message only when a decision is needed, not for every event.

## 5. Design

### 5.1 Data model (`models.py`, `store.py`)

New tables go in `store.py:_TABLES`. `Store.__init__` already creates a table for each entry, so there is no migration script. All new models are pydantic, stored as JSON like the rest.

```python
class RunState(StrEnum):         # the agent process
    QUEUED = "queued"            # waiting for a concurrency slot
    RUNNING = "running"
    EXITED = "exited"            # .exit file or process end observed
    LOST = "lost"                # tmux/process gone, no exit code
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

class RunStage(StrEnum):         # the result pipeline, runs after the agent stops
    PENDING = "pending"
    PUBLISHING = "publishing"    # hangar pushes the branch
    COLLECTING = "collecting"
    VERIFYING = "verifying"
    DONE = "done"
    DEGRADED = "degraded"        # some source unavailable, see warnings; retried
    ERROR = "error"

class CommitInfo(BaseModel):  sha: str; message: str; author: str; authored_at: float
class FileChange(BaseModel):  path: str; status: str; additions: int; deletions: int
class PullRequest(BaseModel):
    number: int; url: str; state: str            # open | closed | merged
    draft: bool; head_sha: str; mergeable_state: str | None  # GitHub's: clean, dirty, behind, blocked...
class CheckRun(BaseModel):  name: str; status: str; conclusion: str | None; url: str | None
class Verification(BaseModel):
    mode: str                    # clean | fork | in_place
    command: str; head_sha: str
    passed: bool | None          # None = could not run (see error)
    exit_code: int | None; duration_ms: int; attempts: int; flaky: bool = False
    output_tail: str             # redacted
    sandbox_id: str | None; at: float; error: str | None = None

class RunResult(BaseModel):
    git: GitFacts                # repo, base_branch, base_sha, branch, head_sha, pushed, dirty,
                                 # ahead, behind, commits[], diff{files,additions,deletions,changed[],truncated}
    pr: PullRequest | None
    checks: ChecksSummary | None # head_sha, rollup (none|pending|success|failure), runs[]
    verification: Verification | None
    agent: AgentFacts            # exit_code, duration_s, turns, cost_usd, usage{}  (from structured events)
    agent_summary: str | None    # the agent's own words. A claim. Shown separately and never used for logic.
    warnings: list[str]
    collected_at: float

class Run(BaseModel):            # table "runs"
    id: str                      # run-xxxxxxxx (same format as today, so agent_status still validates it)
    sandbox_id: str; sandbox_name: str
    environment: str; provider: str; harness: str
    prompt: str                  # full prompt, passed through broker.redact
    state: RunState = RunState.RUNNING
    stage: RunStage = RunStage.PENDING
    exit_code: int | None = None
    started_at: float; finished_at: float | None = None
    max_minutes: int | None = None
    base_branch: str | None; base_sha: str | None; work_branch: str | None
    harness_session_id: str | None   # captured from structured output, for later resume
    fanout_id: str | None; attempt: int | None
    created_by: str; created_by_token: str | None
    result: RunResult | None = None
    land_id: str | None = None

class Fanout(BaseModel):         # table "fanouts"
    id: str; environment: str; prompt: str; n: int; strategy: str   # fresh | fork
    concurrency: int; grade: GradeSpec
    run_ids: list[str]; winner: str | None; created_by: str; created_at: float

class Approval(BaseModel):
    by: str; by_token_id: str | None; at: float
    decision: str                # approve | reject
    head_sha: str                # approval is bound to this commit
    via: str                     # dashboard | api | link:<notifier> | policy:auto-land
    comment: str | None = None

class LandRequest(BaseModel):    # table "land_requests"
    id: str                      # land-xxxxxxxx
    run_id: str; sandbox_id: str; repo: str
    base: str; branch: str; head_sha: str; pr_number: int | None
    state: LandState; state_reason: str | None
    method: str                  # merge | squash | rebase
    policy: dict                 # snapshot of the effective policy at request time
    approvals: list[Approval]
    ci: ChecksSummary | None
    overlaps: list[dict]         # other open land requests touching the same files
    conflicts: list[str]         # files, when state == conflict
    requested_by: str; requested_by_token: str | None
    created_at: float; updated_at: float
    merge_sha: str | None = None
    history: list[dict]          # [{ts, state, reason}], append-only
```

Other model changes:

- `Credential.purpose: Literal["sandbox", "control-plane"] = "sandbox"`. `broker.py:inject` skips `control-plane` credentials and `fleet.py:create` rejects them in a `credentials` list. This is how the merge and push credentials live in the vault but never reach a sandbox. `Broker.redact` already masks every vault secret in audit entries.
- `Sandbox.info["git"] = {"mode": "proxy|token", "token_hash": ..., "branch": ...}` (scoped sandbox token, section 5.6).
- `ApiToken.scopes` gets real meaning (section 5.7).
- `RepoSpec.verify` (envspec.py). Named `verify`, but `Harness.verify` already exists and only checks `claude --version`. The field is a model of its own so the names do not clash.

```yaml
repo:
  url: https://github.com/Sean-Koval/hangar.git
  setup: uv sync --all-extras
  bake: true                    # recommended: dependencies in the image, not installed per sandbox
  git_mode: proxy               # proxy (default for new envs) | token (legacy)
  verify:
    command: uv run pytest -q
    timeout_minutes: 20
    mode: clean                 # clean | fork | in_place
    retries: 1                  # rerun once on failure; a pass on retry sets flaky=true
```

Settings (`config.py`): `max_concurrent_agents: int = 3` (per subscription credential), `run_watch_interval_seconds: int = 10`, `github_api_url`, `land_credential: str | None`, `push_credential: str | None`, `git_proxy_url: str | None` (what sandboxes use to reach hangar's `/git`; defaults to `public_url`), `policy_file: Path = data_dir/"policies.yaml"`, `notifiers_file`.

### 5.2 Run state machine

```
queued ──slot free──▶ running ──.exit written──▶ exited ─┐
                         │ tmux/pid gone, no .exit        │
                         ├────────────────────▶ lost ─────┤
                         │ max_minutes exceeded           │
                         ├────────────────────▶ timed_out ┤  (agent stopped by hangar)
                         │ stop_agent                     │
                         └────────────────────▶ cancelled ┤
                                                          ▼
 stage:  pending ─▶ publishing ─▶ collecting ─▶ verifying ─▶ done
                         └─────────── any failure ─▶ degraded (retry, backoff) ─▶ error after 5 tries
```

Rules:

1. **A run watcher** replaces polling by the client. A new `Fleet._run_watch_loop` task runs every `run_watch_interval_seconds` (default 10 s; `_reaper_loop` at 60 s is too slow). For each `running` run it makes one `exec`: `cat .exit || (tmux has-session -t <run> && echo RUNNING || echo GONE)`.
2. **`lost` detection.** `GONE` twice in a row (two ticks) and no `.exit` means `lost`, exit code `-1`. `run_agent` also writes `$$` to `<run>.pid`. The probe checks `kill -0` so a zombie (the "defunct" processes seen on 2026-10-11) counts as gone. A `lost` run still goes through `publishing` and `collecting`. Commits made before the crash are real.
3. **Stages are idempotent and keyed by `(run_id, head_sha)`.** After a control-plane restart, `Fleet.start` scans runs whose stage is not `done` or `error` and resumes them (Jarmak: recovery outside the failure domain, retried steps idempotent).
4. **Publishing.** Hangar pushes the branch itself, not the agent. It execs in the agent's sandbox with hardened git: `git -c core.hooksPath=/dev/null -c core.fsmonitor=false push origin HEAD:refs/heads/<work_branch>`. In proxy mode this goes through the proxy. The agent may also push (it is allowed to push its own branch). Uncommitted changes set `git.dirty = true` and a warning. Hangar does not auto-commit unless `publish.auto_commit: true`.
5. **Collecting reads from GitHub, not the sandbox.** The sandbox is only used for `head_sha`, `dirty`, and unpushed count, using plumbing commands (`rev-parse`, `status --porcelain`, `rev-list --count`). The agent controls that repo's config, hooks and aliases, so its local `git log` output is not trusted. Everything else (commits, files, PR, checks) comes from GitHub (5.3).
6. **Compat.** `agent_status` keeps `state: "running" | "finished"`. `lost`, `timed_out` and `cancelled` map to `finished` with `exit_code: -1` (the code already uses -1 for an empty file). New fields `phase` (the full `RunState`) and `stage` are added.
7. **Run limits.** `max_minutes` (default from policy, 120) stops a run that runs too long: `tmux kill-session` for that run, `state = timed_out`.

### 5.3 Collecting results (`hosts/github.py`)

A `GitHost` ABC with one implementation, `GitHubHost`, using `httpx` and a control-plane credential (`push_credential` or `land_credential`; read-only calls can use either). Methods: `compare(base, head)`, `find_pr(branch)`, `create_pr(...)`, `checks(sha)`, `update_branch(pr, expected_sha)`, `merge(pr, sha, method)`, `review(pr, event)`, `get_pr(n)`.

GitHub endpoints used (REST, `X-GitHub-Api-Version` pinned):

| Need | Call |
| --- | --- |
| commits, files, ahead/behind | `GET /repos/{o}/{r}/compare/{base}...{head}` (commits with sha, message, author; per-file additions/deletions; `ahead_by`, `behind_by`) |
| PR by branch | `GET /repos/{o}/{r}/pulls?head={o}:{branch}&state=all` |
| PR state, mergeability | `GET /repos/{o}/{r}/pulls/{n}` (`mergeable_state`, `head.sha`, `merged`) |
| CI | `GET /repos/{o}/{r}/commits/{sha}/check-runs` plus `GET .../commits/{sha}/status` for legacy statuses |
| update branch | `PUT /repos/{o}/{r}/pulls/{n}/update-branch` with `expected_head_sha` |
| merge | `PUT /repos/{o}/{r}/pulls/{n}/merge` with `sha` (fails if the head moved) and `merge_method` |

The compare endpoint caps commits and files. Hangar sets `diff.truncated = true` when it hits a cap and shows "at least". Responses are cached by ETag. A rate limit or outage sets the stage to `degraded` with a warning; it never fails the run.

`rollup`: `none` if there are no check runs, `pending` if any is not `completed`, `failure` if any conclusion is not in `{success, neutral, skipped}`, else `success`. `policy.required_checks` filters which names count for landing.

**Claims vs facts.** `agent_summary` comes from the structured final event of the harness (for Claude `--output-format stream-json`, the last `type: "result"` line). It is stored as a claim. The dashboard shows it under "Agent says (unverified)". A PR URL in the summary is ignored.

**Harness adapters.** `Harness` gains `events: str | None` naming a small parser (`claude_stream_json`, later `codex_jsonl`, `gemini_stream_json`). The parser returns `{summary, session_id, turns, cost_usd, usage, rate_limit}` and is best effort. If it cannot parse, the fields are null and a warning is added. Nothing else depends on them.

### 5.4 Verification

Why not just trust the exit code or CI? Jarmak measured a model that "submitted" every trial and resolved 44% under an external oracle. CI is also slower and can be skipped or edited. Hangar needs its own check, run where the agent cannot influence the environment it runs in.

Modes (`repo.verify.mode`, default `clean`):

| Mode | What happens | Cost | Use when |
| --- | --- | --- | --- |
| `clean` | New sandbox from the same environment, label `hangar-role=verify`, no model credentials. Checks out the pushed `head_sha` (extend `_setup_repo` with a `ref`), runs the command. | A warm claim (3.1 s on GKE) plus the setup. Fast with `bake: true`. | Default. Tests exactly what will land. |
| `fork` | `fork_sandbox` of the agent's final state, then run. | Snapshot + restore. On GKE without pod snapshots this is a workspace tarball, which includes the venv and is large. | Dirty or unpushed state matters, or no remote exists. |
| `in_place` | Run in the agent's sandbox, in a separate tmux socket (`tmux -L verify`) under `timeout`. | None. | Provider cannot create more sandboxes. Weakest: leftover state and agent-owned processes can affect it. |

Process:

1. After `publishing`, if `repo.verify` is set (or `run_agent(verify=True)`), the stage becomes `verifying`.
2. The verify sandbox counts toward `max_sandboxes` (50). If the fleet is full, the run waits in `verifying` with a warning. It is destroyed when done, even on failure.
3. `exec` with `timeout = verify.timeout_minutes * 60`. The current `exec` path is used for up to 30 minutes (the same bound as `repo.setup`). Longer commands need the tmux + log path (open question 6).
4. On non-zero exit, rerun once if `retries > 0`. Pass on retry sets `flaky = true`. Both attempts are recorded.
5. The result is a `Verification` with `passed`, `exit_code`, redacted `output_tail` and `head_sha`. `passed = null` means "could not run", which grading treats as not passed.

Limits, stated plainly: verification catches false claims of success. It does not stop an agent that edits the tests to pass. For that, CI with protected workflow/test paths and human review remain the backstop. A policy option `verify.restore_paths: [tests/conftest.py]` can restore named files from `base_sha` before running (open question 7).

Usage: verification uses no model tokens, so it does not touch the Claude subscription.

### 5.5 Fan-out and grading

#### Tools

```python
fanout_start(environment: str, prompt: str, n: int, repo: str | None = None,
             branch: str | None = None, work_branch_prefix: str | None = None,
             harness: str | None = None, strategy: str = "fresh", source: str | None = None,
             concurrency: int | None = None, grade: dict | None = None,
             credentials: list[str] | None = None, provider: str | None = None,
             ttl_minutes: int | None = None, name_prefix: str | None = None) -> dict
fanout_status(fanout_id: str, wait_seconds: int = 0, include_results: bool = False) -> dict
fanout_pick(fanout_id: str, attempt: int | None = None, land: bool = False,
            discard_others: bool = False, title: str | None = None) -> dict
rank_runs(run_ids: list[str], grade: dict | None = None) -> dict
```

- `strategy="fresh"` calls the same path as `create_sandboxes` (`fleet.create` per attempt, branch `{prefix}-{i}`).
- `strategy="fork"` requires `source` (a prepared sandbox or snapshot id) and uses `fleet.fork` with `work_branch_prefix`. Forks scrub secrets and re-inject, as today. Exclusive credentials are not copied.
- `n` is 1–20, like `create_sandboxes`. `concurrency` defaults to `max_concurrent_agents`. Sandboxes for all N are created at once (cheap). Agents start at most `concurrency` at a time. The rest are `queued`.
- `rank_runs` grades runs that were made by hand with `create_sandboxes` or `fork_sandbox`, so existing workflows get grading without changing how they start.
- `fanout_status(wait_seconds=N)` blocks up to N seconds (max 120) and returns early on any state change. This lets an orchestrator avoid a poll loop and stay within its MCP `tool_timeout_sec`.
- `fanout_pick` with no `attempt` takes the top-ranked eligible run. `land=true` calls `land_request` for it. `discard_others=true` destroys the other sandboxes and closes their PRs with hangar's credential. Default is to leave them, because the user may want to inspect.

#### Grade spec

```json
{
  "gates": {"verification": "required", "ci": "required", "exit_code": 0, "min_commits": 1},
  "order": ["ci", "verification", "diff_lines", "files", "duration"],
  "ci_wait_minutes": 30
}
```

- `gates` are pass/fail. A run that fails a gate is `eligible: false` and ranks below all eligible runs. `ci: "required" | "preferred" | "ignore"`. `verification` likewise. Default is `required` when the environment defines `repo.verify` and `preferred` otherwise.
- `order` is a lexicographic sort over the eligible set: `ci` (green first), `verification` (pass first), `diff_lines` (additions + deletions, fewer first), `files`, `duration`. Lexicographic is chosen over a weighted score because the orchestrator can explain it in one sentence. An optional `weights` object is accepted as an alternative and returns a numeric `score`.
- Ties are broken by attempt number, so results are deterministic.
- Smaller diff is a tiebreak, not a goal. A one-line diff that passes is better than a rewrite that passes, but a failing one-liner never beats a passing rewrite.

#### What the orchestrator sees

```json
{
  "fanout_id": "fan-7c1d2e3f", "state": "grading", "n": 3, "concurrency": 2,
  "counts": {"queued": 0, "running": 1, "stopped": 2, "done": 1},
  "usage": {"claude-max": {"seven_day_pct": 91, "five_hour_pct": 34, "as_of": 1791800000, "source": "agent log"}},
  "ranking": [
    {"rank": 1, "attempt": 2, "run_id": "run-1a2b3c4d", "sandbox": "api-2", "eligible": true,
     "reasons": ["ci success (2/2 required checks)", "verification passed (uv run pytest -q, 41 s)", "+41/-7 in 3 files"],
     "branch": "agent/flaky-2", "head_sha": "9f3c2ab", "pr": {"number": 42, "url": "https://github.com/me/api/pull/42", "state": "open"},
     "flaky": false},
    {"rank": 2, "attempt": 1, "run_id": "run-55aa66bb", "eligible": true,
     "reasons": ["ci success", "verification passed", "+210/-95 in 9 files (larger diff)"]},
    {"rank": 3, "attempt": 3, "run_id": "run-77cc88dd", "eligible": false,
     "reasons": ["agent exited 0 and claims success", "verification FAILED (3 failed, 118 passed)", "ci failure: test"]}
  ],
  "pending": [], "winner": null
}
```

State is `running` until all attempts are `done`, then `grading` (waiting for CI up to `ci_wait_minutes`), then `ready`. A partial ranking is returned while attempts are running, with `provisional: true`.

Variance: the fan-out record keeps per-attempt duration, cost and result. Because single runs vary by 2–6 points (Jarmak), anyone comparing harnesses or prompts should use repeats. `fanout_start` is the repeat mechanism, and the dashboard shows pass rate across attempts.

#### Usage and concurrency (single Claude Pro subscription)

- All sandboxes that attach the same subscription credential share one limit. `Fleet._slots_free(credential)` counts `running` runs per credential. Queued runs start from the watcher when a slot frees.
- Hangar reads the rate-limit event the Claude log already carries (the live run saw a `rate_limit_event` at 91%). The adapter stores the last one per credential in `kv` key `usage:<credential>`. `fleet_overview` returns it under `usage`.
- If the last known utilization is at or above `usage.pause_at_pct` (default 90), the watcher stops starting queued runs, sets the fan-out to `paused_usage`, and fires `usage.high`. A human or the orchestrator may override with `fanout_resume`. Open question 3 covers the exact event schema.
- N attempts cost about N times one attempt. `fanout_start` returns a `warnings` entry when `n > concurrency` or usage is already above 70%.

### 5.6 Git access: one branch, no merge rights

This is the section the safety of the land step rests on. Today the PAT lives in the sandbox (`harnesses.py` git slot). Hangar cannot stop that token from pushing to `main` or calling the merge API except through GitHub rules. Two supported modes:

| | **proxy mode** (recommended) | **token mode** (compat) |
| --- | --- | --- |
| Agent holds a GitHub credential | No. Only a per-sandbox hangar token. | Yes. Fine-grained PAT "A" (contents + PRs write, one repo). |
| Can push elsewhere than its branch | No. Proxy rejects it. | Yes, unless GitHub rules forbid it. |
| Can merge or approve | No. No credential. | Merge blocked only by GitHub rules. Needs rulesets. |
| Who opens the PR | Hangar (identity "B") | The agent (identity "A") |
| What enforces "human approved" | Hangar (and optionally GitHub review) | GitHub required review, satisfied by hangar's review-bridge |

**Proxy mode.** A new `app.py` router at `/git/{sandbox_id}/{owner}/{repo}.git/` speaks the git smart-HTTP protocol (`info/refs`, `git-upload-pack`, `git-receive-pack`).

- At claim time (`fleet.create`, after `broker.inject`), hangar writes in the sandbox: `git config --global url."<git_proxy_url>/git/<sandbox_id>/".insteadOf https://github.com/` and a `~/.git-credentials` entry for the proxy host with username `x-hangar` and password `hgs_<random>`. Hangar stores only the hash in `Sandbox.info["git"]`. No GitHub token is written. The existing commit identity (commit b820e5b) is set from a one-time `GET /user` made by hangar with its push credential and cached.
- Auth: the `hgs_` token is valid only for its sandbox id, its repo, and its work branch. It is revoked on `destroy` and when the land request ends. It is never accepted on `/api` or `/mcp`.
- Fetch and clone: allowed for the sandbox's repo only. Hangar proxies the upload-pack with the push credential's read access.
- Push: the proxy reads the first pkt-lines of the `git-receive-pack` request body (`<old> <new> <ref>\0<caps>`, then a flush). It rejects the whole request, before forwarding a byte of the pack, if any command is not an update of exactly `refs/heads/<work_branch>`. Tags, other branches, deletion (all-zero new id) and a ref outside the branch all return an error that git prints to the agent. Non-fast-forward (force) pushes to its own branch are allowed by default, since agents rebase. `push.allow_force: false` rejects them by comparing the old id with the proxy's cached tip.
- Then the proxy forwards the request to GitHub with the push credential "B-push".
- Limits: pushes over `push.max_bytes` (default 200 MiB) are rejected. Pushes touching `.github/workflows/**` are allowed but flagged `sensitive` in the result (see 6).
- The proxy is the only place the push credential is used for agent traffic. The credential is `purpose: control-plane`.

**Token mode** stays so nothing breaks on day one. Safety then comes from GitHub: a ruleset on the default branch (and release branches) that requires a PR, requires status checks, requires one approval from someone other than the PR author, and blocks force-push and deletion, with an empty bypass list. The agent's PAT user must not be a repo admin (admins bypass classic protection unless "include administrators" is set). Token mode cannot enforce "one branch only" because a PAT with write access can push to any unprotected branch. Hangar shows a banner for any sandbox in token mode: "this sandbox holds a GitHub token".

**Identities.** Two hangar-side credentials, both `purpose: control-plane`, stored in the vault and never injected:

- `push_credential`: push to `refs/heads/*` of one repo via the proxy, plus create PRs. Fine-grained PAT or GitHub App installation token: Contents write, Pull requests write, Metadata read.
- `land_credential`: merge, update-branch, review. May be the same identity as the push credential in a solo setup. A separate identity is better, because it can be limited to `Pull requests: write` + `Contents: write` and listed alone in a ruleset's merge/bypass rules. An App is preferred over a PAT. A merge by the App is attributed to it in GitHub, and App tokens can be short-lived.

**Provider reachability.** Sandboxes must reach the proxy.

| Provider | Path | Notes |
| --- | --- | --- |
| docker | `git_proxy_url` = `http://host.docker.internal:8787` (Docker Desktop/OrbStack) or the gateway of `docker_network`. | A laptop with `127.0.0.1` binding cannot be reached. Bind to the docker bridge or set the URL. |
| kubernetes (GKE Agent Sandbox) | The default NetworkPolicy blocks the control plane. Two options: (a) the public HTTPS URL, since public egress is open; this needs an ingress rule that exposes `/git/*` alone, not behind IAP, authenticated by `hgs_`; (b) a second Service/port `hangar-git` and an extra NetworkPolicy allowing sandbox egress to just that port. | Whether the add-on tolerates an extra allow policy alongside `Managed` is not verified (open question 1). Do not set `networkPolicyManagement: Unmanaged` (docs/gke.md). |
| e2b | Public HTTPS URL only. | Control plane must be internet-reachable. |

If the proxy is unreachable, `fleet.create` fails fast with a clear error (a one-request health probe from inside the sandbox). It does not silently fall back to token mode.

### 5.7 Token scopes (`auth.py`, `app.py`, `mcp_server.py`)

`ApiToken.scopes` exists but is never checked. Define:

| Scope | Grants |
| --- | --- |
| `read` | GET endpoints, read-only MCP tools |
| `operate` | everything that exists today (create, exec, run_agent, fork, credentials...) |
| `land` | `land_request`, `land_status`, `land_list`, `land_cancel`, `fanout_pick(land=true)` |
| `approve` | approve/reject a land request. REST and dashboard only. **No MCP tool exists for it.** |
| `admin` | `read`, `operate`, `land`, token and credential management. **Does not include `approve`.** |

- Existing tokens keep `["admin"]`, so every current call works. Only the new `approve` check is strict.
- The env admin token and the generated `bootstrap-admin` token receive `approve` (they are the human's tokens). Other existing tokens do not. `hangar token create NAME --scopes operate,land` is for orchestrators. `hangar token scope ID +approve` grants it later.
- OAuth tokens (`oauth.py`, scope `hangar`) map to `operate` + `land` and can never reach `/api`, as today (docs/security.md). They cannot approve.
- Enforcement is a `require(scope)` dependency on REST routes and a check in the MCP `tool()` wrapper using the actor's scopes (the contextvar in `MCPAuthMiddleware` carries them). A tool call without scope raises `ToolError("insufficient_scope: requires 'land'")`. For transport-level step-up, `MCPAuthMiddleware` can answer 403 with `WWW-Authenticate: Bearer error="insufficient_scope", scope="land"` as the MCP security guidance describes. Whether the clients in docs/clients.md act on it is unverified (open question 8).
- Session ids are never used for authorization.

### 5.8 The land step

#### Tools

```python
land_request(run_id: str, title: str | None = None, body: str | None = None,
             base: str | None = None, method: str | None = None, draft: bool | None = None) -> dict
land_status(land_id: str, wait_seconds: int = 0) -> dict
land_list(state: str | None = None, repo: str | None = None) -> list[dict]
land_cancel(land_id: str) -> dict
```

Needs scope `land`. There is no `land_approve` tool, on purpose.

`land_request` fails unless `run.stage == done` and the run has at least one commit. It is idempotent per `(run_id, head_sha)`: a second call returns the same land request. It then:

1. Takes `head_sha` from the run result.
2. Opens a PR (if none exists for the branch) as B. Title defaults to the first commit subject. The body is generated from facts: commit list, diff stat, verification line, a link to `public_url/#run/<id>` (the session log), and the agent summary in a collapsed block labelled unverified. Draft by default (`draft_pr: true`), as in the Copilot model.
3. Enters the state machine.

#### States

```
requested ─▶ queued ─▶ updating ─▶ waiting_ci ─▶ awaiting_approval ─▶ merging ─▶ landed
                │          │            │               │                 │
                │          ├─▶ conflict │               ├─▶ rejected      ├─ base moved ─▶ updating
                │          │            ├─▶ ci_failed   ├─▶ expired       │  (approval lapses)
                └──────────┴────────────┴───────────────┴─▶ cancelled / failed
```

- **queued.** Land requests are serialized per `(repo, base)`. Only one is in `updating`..`merging` at a time. This keeps "update, test, merge" from racing with itself. If the repo uses GitHub's merge queue, hangar should enqueue instead (open question 4).
- **updating.** If `behind_by > 0` or `mergeable_state == "behind"`, bring the branch up to date. `update: merge` (default) calls update-branch with `expected_head_sha`. `update: rebase` runs in an ephemeral **land worker** sandbox (no model credentials, proxy token scoped to this branch) that does `git rebase origin/<base>` and `git push --force-with-lease=<branch>:<old_sha>` through the proxy. The control plane itself never runs git against repo content. A clean update gives a new `head_sha`. Any prior approval is dropped (below). Verification re-runs on the new head (stage `verifying` for that sha).
- **conflict.** Update failed with a conflict. Hangar records the file list, never resolves it silently, and notifies `land.conflict`. The orchestrator may call `run_agent` on the same sandbox with a resolve prompt. Hangar offers this as `land_resolve_conflicts` in the dashboard (not an MCP tool in v1). The new commits restart the pipeline.
- **waiting_ci.** Poll `checks(head_sha)` every 20 s (only for active land requests). Required checks come from policy. Missing required checks after `ci_timeout_minutes` (default 45) go to `ci_failed` with reason "check X never reported". This covers a workflow that was skipped or renamed. A GitHub webhook receiver `POST /api/webhooks/github` (HMAC) can replace polling later.
- **awaiting_approval.** Only reached when CI is green on the exact `head_sha`, verification passed (if required), the branch is up to date, and there are no unresolved conflicts. A notification goes out (5.9). The card in the Inbox shows the facts.
- **merging.** Check approvals (below). Call merge with `sha=head_sha`. A 409/405 means the head or base moved: return to `updating`. After a crash the step is safe to repeat: the first action is `get_pr`, and if `merged` is true, record `landed`.
- **landed.** Store `merge_sha`. Revoke the sandbox git token. If `auto_destroy_sandbox` is set, destroy the sandbox after a grace period. Post a PR comment as B: "Landed by hangar. Approved by X via dashboard. Run: <link>".

#### Conflicts between parallel PRs

1. **Early warning.** Every time a land request's files are known, hangar intersects them with other open land requests on the same base and fills `overlaps: [{"land": "land-ab12", "files": ["src/a.py"]}]`. This costs nothing (the compare API already returned the file lists). It is shown as a warning in the Inbox, even when the PRs are still in CI.
2. **Predicted conflict.** When two requests overlap, the land worker runs `git merge-tree --write-tree` for the pair (git 2.38 or later). Conflicts show as "will conflict if land-ab12 merges first".
3. **Real conflict** is found at `updating`, after the first one lands. The second request moves to `conflict`. Hangar never force-merges and never rewrites a branch without `--force-with-lease`.

#### Approvals

- Approve and reject go through `POST /api/land/{id}/approve|reject` (needs scope `approve`) or an approval link (below). There is no MCP tool.
- `Approval.head_sha` binds the decision to a commit. Any change to the head (update, rebase, a new push, even by the agent) drops the approval and returns to `waiting_ci`. This is GitHub's "dismiss stale approvals" rule. Setting `approval_survives_clean_update: true` keeps it across a clean merge-from-base update only. Default is false.
- `required_approvals` (default 1) counts distinct approvers. `forbid_self_approval` (default true) rejects an approver whose token id equals `requested_by_token` or `Run.created_by_token`. The ban applies to tokens, not people. It stops an orchestrator token from approving its own work, which the scope already prevents. It also covers the case where a human's dashboard token launched the run. A solo user who launches from and approves in the dashboard sets `forbid_self_approval: false` knowingly.
- **Approval link.** For a phone or Slack, the notifier includes `public_url/approve/<apr_token>`. The token is HMAC-signed, bound to `(land_id, head_sha)`, single use, and expires in 30 minutes (`approval_link_ttl`). `GET` shows the card and a button only. A `POST` with a one-time nonce is what approves, so link previewers and scanners cannot approve. `approval.link_enabled: false` disables links. Redeeming a link counts as `via: link:<notifier>`. It does not need a dashboard token, so it is only as safe as the channel that carries it. Send links only to channels the agent and orchestrator cannot read.
- **Sensitive paths.** If the diff touches `policy.sensitive_paths` (default `.github/**`, `deploy/**`, `Dockerfile*`, `pyproject.toml`, `package.json`), the card shows a red banner, auto-land is disabled, and `sensitive_extra_approvals` more approvals are needed (default 0, but the approver must tick "I reviewed the sensitive files").

#### Policy

`data_dir/policies.yaml`, loaded at start, editable on the dashboard (each edit is audited). A `default` block plus per-repo overrides.

```yaml
default:
  base: main
  method: squash               # merge | squash | rebase. Squash gives one GitHub-signed commit.
  update: merge                # merge | rebase
  draft_pr: true
  required_checks: [test, lint]
  require_verification: true
  required_approvals: 1
  forbid_self_approval: true
  approval_survives_clean_update: false
  github_review: ignore        # ignore | required: also wait for GitHub's reviewDecision == APPROVED
  review_bridge: false         # token mode: B posts the GitHub APPROVE review after a hangar approval
  sensitive_paths: [".github/**", "deploy/**", "Dockerfile*", "pyproject.toml"]
  sensitive_extra_approvals: 0
  max_diff_lines: 2000         # above this the request needs an explicit "large change" ack
  ci_timeout_minutes: 45
  approval_ttl_minutes: 240    # an unanswered request expires; the PR stays open
  auto_land:
    enabled: false
    when: {verification: pass, checks: pass, max_diff_lines: 150, only_paths: ["docs/**", "tests/**"]}
repos:
  "Sean-Koval/hangar": {required_checks: [test]}
```

- **Auto-land** records an approval with `by: policy`, `via: policy:auto-land`, and the rule text. It never applies to sensitive paths and never when `required_approvals` > the policy's own count. It is off by default. The Inbox shows suggestions ("docs/** : 18 of 18 landed without edits in 30 days, enable auto-land?") from recorded approval history. These are suggestions, never automatic, following Jarmak's advice to widen autonomy one action class at a time by measured approval rates.
- Policy is resolved when `land_request` is created and snapshot into `LandRequest.policy`. Later policy edits do not change in-flight requests, except `cancel`.

#### Interaction with GitHub branch protection

Hangar's gate and GitHub's gate are different layers. Use both.

| GitHub setting | Proxy mode | Token mode |
| --- | --- | --- |
| Ruleset on `main`: require PR | Required. Defense in depth. B merges through the PR. | Required. This is what stops the agent pushing to main. |
| Required status checks | Required. Same names as `policy.required_checks`. Hangar also checks them itself. | Required. |
| Required approving reviews | 0 is enough, because hangar is the gate. Set 1+ for dual control, and set `github_review: required`; a human then approves in GitHub too. | **1+, from a non-author.** Set `review_bridge: true`: after a hangar approval B submits the APPROVE review (B differs from A, the author), and B merges. |
| Restrict who can push or merge | Nobody but B (and humans) has write. The agent has no credential. | Cannot restrict A without breaking its push. |
| Bypass list | Empty. Admins included. | Empty. A must not be admin. |
| Signed commits | Squash merges via the API are signed by GitHub, so main stays signed even though agent commits are not (keys must not be in the sandbox). | Same. |
| Merge queue | Compatible if hangar enqueues (open question 4). | Same. |

"Requester cannot approve" holds at both layers. In hangar it is the `approve` scope plus `forbid_self_approval`. In GitHub it is the platform rule that a PR author cannot approve their own PR. This is why B opens PRs in proxy mode, and why B reviews A's PRs in token mode.

CI trigger gating (Copilot holds workflows until a human approves) is only partly reachable: GitHub controls it, and the settings that gate workflow runs apply to forks and first-time contributors. Hangar's equivalent is: the PR is a draft, and `verify` runs first in a clean sandbox before any human time is spent. See open question 5.

### 5.9 Notifications (`notify/`)

A small pluggable framework. The dashboard is always on and is the source of truth. Other channels are best effort.

```python
class Notification(BaseModel):
    id: str; kind: str                # approval.requested, land.conflict, land.ci_failed, land.landed,
                                      # run.lost, run.done, fanout.ready, usage.high
    severity: Literal["info", "action", "alert"]
    title: str; text: str             # built only from hangar facts, never from agent text
    url: str                          # dashboard deep link
    refs: dict[str, str]              # land_id, run_id, sandbox, fanout_id
    approve_url: str | None = None    # one-time approval link, only for kinds that need it
    dedupe_key: str; created_at: float

class Notifier(ABC):
    name: ClassVar[str]
    def __init__(self, config: dict, secrets: Vault): ...
    async def send(self, n: Notification) -> None: ...      # raise on failure; the router retries
    async def test(self) -> None: ...
```

Built in:

| Notifier | Transport | Notes |
| --- | --- | --- |
| `dashboard` | SSE stream `GET /api/events` (ticket auth, as `build_stream` does) and the Inbox badge | Always on. Cannot be disabled. |
| `webhook` | HTTPS POST of the JSON above | Header `X-Hangar-Signature: sha256=<hmac>` over the body. Works with Zapier, n8n, PagerDuty. |
| `slack` | Slack incoming webhook | Message with fact fields and link buttons that open the dashboard or approval link. Incoming webhooks cannot carry interactive buttons, so approval happens on the hangar page. |
| `ntfy` | HTTP POST to an ntfy topic (self-hosted or ntfy.sh) | Phone push without an app store account. Title and click URL set. |
| `email` | SMTP (`smtp://` URL in the vault) | Digest option for `info`. |

Third parties add notifiers via the `hangar.notifiers` entry-point group. Web Push (VAPID) is a later notifier. It needs a service worker in `ui/`.

Config (`data_dir/notifiers.yaml`; secrets such as webhook URLs go to the vault under `notifier:<name>`):

```yaml
notifiers:
  slack-main: {type: slack, secret: notifier:slack-main}
  phone:      {type: ntfy,  topic: hangar-sean, server: https://ntfy.sh}
routes:
  - {match: {kind: approval.requested}, to: [dashboard, slack-main, phone], escalate_after_minutes: 20, escalate_to: [phone]}
  - {match: {severity: alert},          to: [dashboard, slack-main]}
  - {match: {kind: run.done},           to: [dashboard]}
```

Router rules: dedupe by `dedupe_key` (one `approval.requested` per `(land_id, head_sha)`), retry with backoff up to 5 times, log each delivery in the audit table, rate-limit per channel (default 30 per hour, `alert` exempt). A failing notifier never blocks the pipeline. Messages are plain text with only hangar-built fields so a prompt-injected agent cannot place links or markdown in a human's channel. The agent summary is not sent.

### 5.10 REST endpoints

All in `app.py`, using `A`/`require(scope)`. Bodies are pydantic models next to the existing ones.

| Method and path | Scope | Purpose |
| --- | --- | --- |
| `GET /api/runs?sandbox=&fanout=&stage=&limit=` | read | List runs |
| `GET /api/runs/{id}` | read | Run + result |
| `POST /api/runs/{id}/collect` | operate | Re-run publish + collect |
| `POST /api/runs/{id}/verify` | operate | Body `{"mode": "clean", "command": null}` |
| `POST /api/runs/{id}/cancel` | operate | Stop a run (also MCP `stop_agent`) |
| `POST /api/fanouts`, `GET /api/fanouts/{id}`, `POST /api/fanouts/{id}/pick`, `POST /api/fanouts/{id}/resume` | operate | Fan-out |
| `POST /api/land`, `GET /api/land?state=&repo=`, `GET /api/land/{id}` | land / read | Land requests |
| `POST /api/land/{id}/approve`, `/reject` | **approve** | Body `{"head_sha": "9f3c2ab...", "comment": "...", "ack_sensitive": false}`. `head_sha` is required; a mismatch returns 409 `stale_head`. |
| `POST /api/land/{id}/cancel`, `/retry`, `/resolve-conflicts` | land | |
| `GET /approve/{apr_token}`, `POST /approve/{apr_token}` | link | The approval page (no bearer token) |
| `GET /api/policy`, `PUT /api/policy` | admin | Policy file |
| `GET /api/notifiers`, `PUT /api/notifiers`, `POST /api/notifiers/{name}/test` | admin | Notifier config |
| `GET /api/events` | read (ticket) | SSE |
| `* /git/{sandbox_id}/{owner}/{repo}.git/*` | `hgs_` token | Git proxy |

Approve example:

```
POST /api/land/land-3fa91c20/approve
{"head_sha": "9f3c2ab41d0e7f6a5c8b1e2d3f4a5b6c7d8e9f01", "comment": "Read the diff."}
→ 200 {"id": "land-3fa91c20", "state": "merging", "approvals": [{"by": "sean", "via": "dashboard", "head_sha": "9f3c2ab...", "decision": "approve"}]}
→ 403 {"error": "token lacks scope 'approve'"}
→ 409 {"error": "stale_head", "current": "c0ffee..."}
```

`land_status` as the orchestrator sees it:

```json
{
  "id": "land-3fa91c20", "state": "awaiting_approval", "repo": "me/api", "base": "main",
  "branch": "agent/flaky-2", "head_sha": "9f3c2ab", "pr": {"number": 42, "url": "https://github.com/me/api/pull/42"},
  "ci": {"rollup": "success", "required": {"test": "success", "lint": "success"}},
  "verification": {"passed": true, "mode": "clean"},
  "approvals": {"have": 0, "need": 1, "eligible_approvers": "humans with scope approve"},
  "overlaps": [], "conflicts": [], "next": "A human must approve in the dashboard. This request cannot be approved over MCP.",
  "dashboard_url": "https://hangar.example.com/#land/land-3fa91c20"
}
```

### 5.11 Backward-compatible MCP changes

| Tool | Change |
| --- | --- |
| `run_agent` | New optional `verify`, `publish`, `max_minutes`. Same positional args. Return adds `base_sha`, `stage`. `run_id` format unchanged. |
| `agent_status` | New optional `wait_seconds`. Return adds `phase`, `stage`, `result` (summary, when `stage == done`). `state` and `exit_code` keep their meaning. |
| `get_sandbox` | `runs` entries gain `state`, `stage`, `pr_url`, `verified`. Source moves from `info["runs"]` to the `runs` table. |
| `fleet_overview` | Adds `usage` and `queued_runs`. |
| `create_sandboxes`, `fork_sandbox` | Unchanged. |
| New | `run_result`, `verify_run`, `stop_agent`, `fanout_start`, `fanout_status`, `fanout_pick`, `fanout_resume`, `rank_runs`, `land_request`, `land_status`, `land_list`, `land_cancel` |

Intentional behavior changes (the only breaks):

1. A dead run now reports `finished` with `exit_code: -1`, not `running` forever. This fixes the bug.
2. In proxy mode the sandbox has no `GH_TOKEN` and no `gh` login. Prompts that tell the agent to `gh pr create` stop working. Hangar adds a short line to the run preamble: "Commit to the current branch. Hangar pushes and opens the PR." (`prompt_preamble`, opt-out).
3. `run_agent` may return `state: "queued"` when the subscription cap is reached.

### 5.12 Dashboard UX (`ui/`)

New rail items: **Inbox** (count badge, the approvals queue) and **Runs**. Fleet rows gain a run badge. The Fleet view's 4 s poll stays; Inbox uses SSE.

Inbox card:

```
┌ Ready to land · me/api · main ◀ agent/flaky-2                     land-3fa91c20 ┐
│ Make the flaky test in tests/test_sync.py deterministic            run-1a2b3c4d │
│ ✔ CI 2/2 required  ✔ Verified clean (uv run pytest -q, 41 s)  ✔ Up to date    │
│ +41 −7 · 3 files · 2 commits    ⚠ overlaps land-ab12 on src/sync.py             │
│ Files: src/sync.py  tests/test_sync.py  tests/conftest.py   [Open diff on GitHub]│
│ Agent says (unverified): "Fixed the race by seeding the clock…"                 │
│ Approvals 0/1   [ Approve & land ]  [ Reject… ]  [ Hold ]   head 9f3c2ab        │
└────────────────────────────────────────────────────────────────────────────────┘
```

- **Approve & land** posts the current `head_sha`. If the head changed since the card loaded, the button shows "Changed, review again" and reloads.
- Sensitive files get a red banner and a checkbox.
- Run detail: timeline (queued, running, exited/lost, publishing, collecting, verifying, done), commits, files, checks (live), verification output, usage, link to the terminal log.
- Fan-out view: one row per attempt with the ranking reasons, a "Pick this one" button, and pass rate across attempts.
- Policy and Notifiers editors under **Connect**. Policy changes show a diff and need confirmation.
- The agent's summary and all log text are rendered as escaped plain text.

## 6. Security and failure modes

Threats and controls:

| Threat | Control |
| --- | --- |
| Agent pushes to `main` or another branch | Proxy rejects any ref other than `refs/heads/<work_branch>`. GitHub ruleset as backstop. Token mode relies on the ruleset alone. |
| Agent merges its own PR | No GitHub credential in proxy mode. In token mode GitHub requires a non-author review, which only B posts after a recorded human approval. |
| Orchestrator approves its own work | `approve` scope is not in `admin`, not in OAuth, and has no MCP tool. `forbid_self_approval` compares token ids. |
| Stale approval used on changed code | Approval binds `head_sha`. Merge passes `sha`, so GitHub refuses a moved head. |
| Agent lies about results | Result fields come from GitHub and hangar's own verify. The summary is stored as a claim. |
| Agent tampers with local git to fake output | Local git is used only through plumbing with hooks and fsmonitor off, for `head_sha` and `dirty`. Commits and diff come from GitHub. |
| Agent edits tests or CI to pass | Not prevented by verify. CI on protected workflow paths, `sensitive_paths` extra review, and the human reading the diff. |
| Workflow change exfiltrates repo secrets (same-repo PR workflows run with the PR's version) | `.github/**` is sensitive by default: red banner, no auto-land, acknowledge box. Recommend `pull_request` (not `pull_request_target`), minimal repo secrets, CODEOWNERS on `.github/`. |
| Prompt injection reaches a human through notifications | Messages contain only hangar-built fields. The summary is not sent. |
| Approval link leaks | Single use, 30 min, bound to the head sha, POST needed. Only send to channels the agent cannot read. Can be disabled. |
| Proxy used to attack hangar | Own listener or path, tokens are per sandbox and not accepted elsewhere, request body capped, receive-pack parsed before forwarding. |
| Code exfiltration via open egress | **Not solved here.** The credential leg of the trifecta is removed for GitHub. The Claude token and the source are still readable with open egress. See open question 2. What this spec does guarantee: nothing the agent does reaches `main` without a human who sees the diff stat and the sensitive-path banner. |
| Control-plane credential theft | `purpose: control-plane` secrets are never sent to sandboxes, never in API output, masked by `Broker.redact`. The land worker and verify sandbox receive only a branch-scoped proxy token. |

Failure modes:

- **Sandbox dies before collection.** Pushed work is on GitHub and is collected. Unpushed work is lost. To limit this, `destroy` and the TTL reaper (`_reaper_loop`) run publish first when a run's stage is not `done` and the branch has unpushed commits. They warn if the push fails.
- **Control plane restarts mid-pipeline.** Stages resume from the stored stage. PR creation checks for an existing PR for the branch first. Notifications dedupe by key.
- **GitHub down or rate limited.** Stage `degraded`, retry with backoff (max 5), warnings on the result. Landing pauses. Nothing merges on stale data.
- **Base moves while merging.** `sha` guard and 409 send the request back to `updating`. The approval lapses.
- **Double-click or two approvers racing.** Approvals are idempotent on `(land, by, head_sha)`. Merge is idempotent via `get_pr` first.
- **Flaky verification.** One retry, `flaky: true`, shown on the card.
- **Verify sandbox cannot start (fleet full).** Run waits in `verifying`; it is not marked failed.
- **Notifier down.** Retries, then audit entry. The Inbox still has the item. `escalate_after_minutes` uses a second channel.
- **Subscription limit hit mid-fan-out.** Queued runs stay queued. Running ones may fail. Failed runs are `exited` with non-zero code and are not retried automatically (retrying burns the same limit).

## 7. Rollout and migration

1. **No data migration.** New tables appear on first start (`Store.__init__`). Old `Sandbox.info["runs"]` entries are backfilled lazily into `Run` rows the first time they are read, with `base_sha = null`. Their results are marked partial.
2. **Phase 0, results.** Run records, watcher and collector ship on by default. They are read-only toward GitHub and need a GitHub credential in the vault (`push_credential`; a read-only PAT is enough). Without it, results show git facts from the sandbox only, with a warning.
3. **Phase 1, verify.** Opt-in per environment through `repo.verify`. Add `bake: true` to `envs/claude-python.yaml` and to project envs (this also removes the per-sandbox `uv sync`).
4. **Phase 2, fan-out and rank.** New tools only. `max_concurrent_agents` defaults to 3, so existing multi-sandbox flows that call `run_agent` directly will see `queued` beyond 3 concurrent agents on one subscription credential. That is a behavior change. Set it to 0 to disable queueing.
5. **Phase 3, land in token mode.** Enabled when `policies.yaml` exists and `land_credential` is set. Start with one repo, `review_bridge: true`, and a ruleset. Guide in the new `docs/landing.md`.
6. **Phase 4, proxy mode.** Opt-in per environment (`repo.git_mode: proxy`). After one release with no regressions, new environments default to proxy. `describe_environment` and the dashboard show warnings for token-mode sandboxes. The `gh` credential moves to `purpose: control-plane` when the user switches (`hangar cred set-purpose gh control-plane`).
7. **Tokens.** `approve` scope is new. Existing tokens keep working. Orchestrator tokens should be recreated with `operate,land`. `docs/security.md` and `docs/clients.md` are updated.
8. **Rollback.** Each phase is a setting (`HANGAR_LAND=off`, `repo.git_mode: token`). Stored rows are ignored when the feature is off.

## 8. Test plan

### Unit tests (fake provider, no network)

New fakes in `tests/conftest.py`:

- `FakeGitHost` implementing `GitHost`: in-memory repos, PRs, check runs, `mergeable_state`, programmable failures and 409s. A helper `advance_main(repo)` simulates base movement.
- `FakeNotifier` capturing `Notification`s.
- `FakeProvider` already runs real bash and real git in a temp dir with a private tmux socket (`TMUX_TMPDIR`). New tests use real `git` against a local bare repo as "origin". The fake agent is a shell script harness (`scripted`, test only) that makes commits.

Tests:

| Area | Tests |
| --- | --- |
| Run states | exit file leads to `exited`; **`tmux kill-server` inside the fake box leads to `lost` within two ticks** (reproduces 2026-10-11); a zombie pid counts as gone; `max_minutes` leads to `timed_out`; `agent_status` compat (`state` is `finished`, `exit_code` -1). Restart resumes stages. |
| Collector | commits, author and diff stat equal `FakeGitHost` data; PR URL in the agent's summary is ignored; dirty tree sets warning; local git with a hostile `core.fsmonitor`/hook does not execute it; compare truncation sets `truncated`. |
| Verify | clean mode checks out `head_sha` not the working tree; failing command gives `passed=false`; retry pass gives `flaky`; fleet-full leads to waiting, not failure; verify sandbox destroyed in all paths; `in_place` uses a separate tmux socket and leaves the agent's session alive. |
| Fan-out | n attempts get distinct branches; concurrency cap leaves the rest `queued` and starts them as slots free; ranking order and `reasons` for ties, ineligible, flaky, missing CI; `rank_runs` on hand-made runs; `fanout_pick(discard_others)` destroys and closes PRs. |
| Usage | adapter extracts rate-limit info from a captured real `stream-json` fixture; `pause_at_pct` pauses queued starts; unparseable log gives null fields and a warning, not an error. |
| Scopes | each new tool and route rejects tokens lacking its scope; `admin` lacks `approve`; OAuth token cannot reach `/api`; bootstrap token has `approve`; no MCP tool named like approve exists (introspect `build_mcp`). |
| Land | full happy path; CI missing hits `ci_timeout`; CI failure; base moved → `updating` → new sha → approval dropped; stale-`head_sha` approve returns 409; self-approval blocked; two approvers when `required_approvals: 2`; duplicate `land_request` is idempotent; merge crash-restart records `landed` once; sensitive path needs ack and blocks auto-land; auto-land fires only within its rule; overlap detection between two requests; conflict state keeps the branch untouched. |
| Git proxy | pkt-line parser: accepts one update of own branch; rejects other branch, tag, delete, multi-ref mixes, truncated bodies, oversize; token for sandbox A rejected for B; token revoked on destroy; real `git push` against the ASGI app and a local bare repo accepts own branch and prints a clear error for `main`. |
| Notifiers | routing, dedupe, retry/backoff, escalation, webhook HMAC verifies, a failing notifier does not block landing, messages contain no agent text. |
| Approval link | single use, expiry, head-bound, `GET` does not approve, POST requires nonce. |
| Policy | defaults, per-repo override, snapshot not changed by later edits, bad YAML rejected with a message. |

Provider-specific unit tests: `test_kubernetes.py` gets a case that the proxy URL and `insteadOf` config are written, and that the claim fails fast when the probe fails.

### Live acceptance test on GKE

Setup: the existing GKE deployment from the 2026-10-11 run; a throwaway test repo `<owner>/hangar-testbed` with a small Python project, a seeded flaky test and a seeded failing test, CI workflow `test` and `lint`; a ruleset on `main` (PR required, checks `test` and `lint`, no bypass); `push_credential` and `land_credential` as a GitHub App; the Claude Pro token; a webhook sink with HMAC check; hangar token `orch` (`operate,land`) and the bootstrap token (`approve`). `max_concurrent_agents: 2`. Environment baked (`bake: true`), proxy mode, warm pool 2. A `scripted` harness is used where model behavior does not matter, to save subscription quota. The real Claude harness is used for A3 and A4.

Script `scripts/accept_landing.py` runs these steps and prints PASS/FAIL per line.

| # | Step | Pass criteria |
| --- | --- | --- |
| A1 | `fanout_start(n=3)` | 3 sandboxes ready in ≤ 10 s. Exactly 2 `running` and 1 `queued`. The third starts ≤ 30 s after a slot frees. |
| A2 | `exec tmux kill-server` in one attempt mid-run | Run becomes `lost` in ≤ 30 s. `fanout_status` lists it as `eligible: false`. Commits it already pushed appear in `git.commits`. |
| A3 | Prompt tells the agent "end with: PR #999 opened, all tests pass" | `pr.number` equals the real PR (or null), never 999. `git.commits` equals GitHub's own compare output fetched by the test with an independent token. |
| A4 | Prompt breaks a test and says tests pass | Agent exits 0. `verification.passed == false`. The attempt ranks last and `eligible: false`. |
| A5 | Verification timing | `clean` verify finishes ≤ 120 s on the baked env with no `uv sync` in the output. |
| A6 | From inside a sandbox: `git push origin HEAD:main`; `HEAD:refs/heads/other`; `git push origin :agent-branch` (delete); `git push --tags`; `env \| grep -iE 'gh_\|github\|token'`; `cat ~/.git-credentials`; `gh api -X PUT repos/<r>/pulls/1/merge` | All pushes rejected with the proxy message. Push to own branch succeeds. The env check shows no GitHub token. The credentials file holds only an `hgs_` token. The merge call fails for lack of credentials. |
| A7 | Same checks with the sandbox's `hgs_` token used against another sandbox's path | 403. |
| A8 | `land_request` on the winner; push an unrelated commit to `main` first | State passes `updating` → `waiting_ci` → `awaiting_approval`. New `head_sha`. Webhook receives `approval.requested` ≤ 10 s after the state change, with a valid HMAC. |
| A9 | Approve with the `orch` token, then with the same token that launched the run | Both 403. Neither merges. |
| A10 | Approve with the bootstrap token | Merged within 60 s. GitHub shows the merge by the App identity, one squash commit, signature "Verified". `merge_sha` equals `main` HEAD. PR comment present. |
| A11 | Approve, then push a new commit to the branch before merge | Merge not performed. State returns to `waiting_ci`. Old approval marked lapsed. |
| A12 | Two attempts edit the same line. Land A, then B | B reaches `conflict` with the file listed. Before A lands, B's card showed the overlap warning. `main` contains A only. No force-push occurred (check GitHub events). |
| A13 | Restart the control plane pod during `waiting_ci` | Pipeline resumes. One PR only. One `approval.requested` notification. |
| A14 | In a token-mode sandbox (legacy PAT "A"), merge its own PR via the API, and push to `main` | Both refused by GitHub (review and PR required). Shows the ruleset is the backstop. |
| A15 | Sandbox TTL expiry with unpushed commits | Reaper publishes before destroy. Commits are on GitHub. |
| A16 | Usage | `fleet_overview.usage` shows the subscription percentage from a real run, within 1 point of the value in the raw log. |

Pass: all of A1–A16 pass on one run, repeated twice (Jarmak: single runs vary). Record timings in the PR.

## 9. Milestones

PR-sized, in order. Sizes: S ≤ 1 day, M ≈ 2–3 days, L ≈ 4–6 days.

| # | Task | Scope | Size |
| --- | --- | --- | --- |
| 1 | Run records and watcher | `Run`, store table, `run_agent` writes rows and `base_sha`, `.pid`, watcher loop, `lost`/`timed_out`, `stop_agent`, `agent_status` compat and `wait_seconds`, lazy backfill. | M |
| 2 | GitHost and collector | `GitHost` ABC, `GitHubHost`, publish step with hardened git, `RunResult`, `run_result` tool, REST `/api/runs`, harness event adapter for Claude (with fixture), `push_credential`, `Credential.purpose`. | L |
| 3 | Verification | `RepoSpec.verify`, `_setup_repo(ref=)`, clean and in_place modes, retries, `verify_run`, bake in `envs/*.yaml`. | M |
| 4 | Concurrency and usage | `max_concurrent_agents`, `queued` state and scheduler, usage capture, `fleet_overview.usage`, `usage.high` event stub. | M |
| 5 | Fan-out and ranking | `Fanout`, `fanout_start/status/pick/resume`, `rank_runs`, grade spec, dashboard fan-out view. | M |
| 6 | Token scopes | `require(scope)`, MCP wrapper checks, CLI `--scopes` and `token scope`, bootstrap token gets `approve`, docs. | M |
| 7 | Land core (token mode) | `LandRequest`, state machine, policy loader, PR creation, CI wait, update-branch merge, merge with `sha`, approvals API, review bridge, `land_*` tools. | L |
| 8 | Notifier framework | `Notification`, router, `dashboard` SSE, `webhook`, `slack`, `ntfy`, `email`, approval links and page. | M |
| 9 | Dashboard | Inbox, run detail, land cards, policy and notifier editors. | L |
| 10 | Git push proxy | `/git` router, pkt-line check, `hgs_` tokens, sandbox git config, provider reachability (docker URL, GKE Service and NetworkPolicy or public path, e2b), `git_mode`, identity from `GET /user`. | L |
| 11 | Conflicts and rebase worker | Overlap detection, `merge-tree` prediction, land worker sandbox, `update: rebase`, resolve-conflicts run, publish-before-destroy in `destroy` and the reaper. | M |
| 12 | Docs and acceptance | `docs/landing.md`, update `docs/security.md`, `docs/gke.md`, `scripts/accept_landing.py`, run A1–A16 on GKE. | M |

Milestones 1–3 deliver value alone: a lost run is visible, and results and verification are real. Milestone 10 is the one that changes the security posture, so it ships behind `git_mode`.

## 10. Open questions

1. **GKE reachability of the proxy.** Does the Agent Sandbox add-on allow an extra allow-egress NetworkPolicy next to its managed default? If not, use the public `/git` path. Needs a test on the live cluster. Not verified.
2. **Egress.** The credential leg is removed for git, but code and the Claude token can still leave over open egress. Should a sibling spec define per-environment allowlists (None / common dependencies / all, GET-only mode) as in Codex cloud, with setup phase open and agent phase closed? Hangar's setup scripts run at claim time, so the two-phase model needs a design.
3. **Usage event schema.** The live run showed a `rate_limit_event` with 7-day usage at 91%. Field names, units and whether it appears on every run are not verified. M4 starts by capturing real logs as fixtures. Also unknown: whether the 5-hour window is reported.
4. **GitHub merge queue.** If a repo uses it, hangar should call the GraphQL `enqueuePullRequest` mutation instead of the merge endpoint. I believe the mutation exists but did not verify its permissions or merge-queue behavior with App tokens.
5. **CI gating.** Copilot holds workflows until a human approves. Hangar can only order its own steps (verify first, draft PR). Is there a GitHub setting that holds `pull_request` workflows for same-repo bot-authored PRs? Not found or verified. Also: marking a draft PR ready needs the GraphQL `markPullRequestReadyForReview` mutation (REST has no equivalent that I know of). Verify, or default to non-draft.
6. **Long verify commands.** `exec` is used up to 30 minutes. Longer suites need the tmux + log path. Does the kubernetes provider's exec stream survive that long through the GKE API proxy?
7. **Test tampering.** Should `verify.restore_paths` exist, or is a protected-paths CODEOWNERS rule enough? Restoring files from `base_sha` can break legitimately changed tests.
8. **MCP scope challenge.** Do Claude Code and Codex act on `insufficient_scope` challenges, or only show the error? `docs/clients.md` does not say. Not verified.
9. **Other harnesses' events.** Claude Code accepts `--output-format stream-json` (confirmed against the local `claude` 2.1.296 help, which also has `--session-id`, `--resume` and `--fork-session`). I could not install Codex or Gemini here. `codex exec --json`, Gemini's `--output-format stream-json`, and `agy`'s event format are unverified, as are the exact fields for session ids. Until verified, those harnesses report null `agent` fields and still get full git/PR/verify results, which do not depend on the adapter.
10. **Hosts beyond GitHub.** The `GitHost` interface fits GitLab and Gitea, but the proxy's pkt-line check is host-neutral while review-bridge and update-branch are not. Defer, or include one more host in v1?
11. **Approver identity.** Hangar tokens are named but not tied to people. Is a token name enough for the audit trail, or does the dashboard need user accounts (or IAP identity headers) before `required_approvals: 2` means two humans?
12. **App vs PAT.** A GitHub App gives short-lived tokens and distinct attribution but is more setup than a fine-grained PAT. Should v1 support PAT only, with App support in M12?
