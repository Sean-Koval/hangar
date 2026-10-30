# Working with the agent

Following along, replying, taking over, the pull request, forks and best-of-N. Part of [jobs](README.md).

## 5. Work with the agent

Every job runs in a [session](../sessions.md). The agent keeps its conversation between turns, and the job's sandbox, context and work in progress are checkpointed. So a reply always works:

- a `/hangar` comment on the PR a week later;
- a reply in the job's Slack thread;
- `hangar job follow-up`;
- the dashboard's reply box.

If the sandbox was released, hangar starts a new one from the job's last checkpoint, and the agent continues where it left off.

The same happens on its own after a control-plane restart (a deploy) that took the job's sandbox with it, and when the nightly power-down takes the sandbox pools away under a running turn (hangar itself stays up; the job waits for power-up, [gke.md](../gke.md#6-sandbox-nodes-off-at-night-optional)). A job caught mid-turn whose session has a checkpoint goes back to the queue with a follow-up turn that resumes from it and tells the agent the restart happened; a job caught provisioning starts over, its check on the base commit included (also when its sandbox goes away under that check while hangar stays up). A best-of-N attempt the power-down cuts off resumes from its own session's checkpoint at power-up, the job waiting meanwhile. Each job (and each attempt) does this at most `HANGAR_JOB_RESTART_RESUMES` times, and such jobs re-enter the queue `HANGAR_JOB_RESTART_STAGGER_SECONDS` apart, so a power-up doesn't ask a cold cluster for every sandbox at once. A job with no checkpoint to resume from still fails as `interrupted`.

- **Watch:** attach to the job's sandbox terminal from the dashboard, or with `hangar sb attach <sandbox_id>` (the run is tmux session `terminal_session`).
- **Steer:** follow up from the API, MCP, CLI or a PR comment. Each follow-up is a new agent turn with your message and the previous summary, on the same branch. The PR updates in place. While a Claude Code or Codex turn runs, you can also steer or interrupt it, and answer its tool approvals when its environment uses `approvals: ask` (`POST /api/jobs/{id}/steer`, `/interrupt`, `/approvals`; MCP `steer_turn`, `answer_approval`; see [steering](../steering.md)). An interrupt with a message carries on with it; without one, the job pauses in `needs_input` until you follow up or cancel, and nothing is delivered.
- **The summary:** the prompt asks the agent for a short summary in `RESULT.md` in the job's directory (`hangar-agent done "<summary>"` writes it there too), never in the repo; the [`repo-conventions` skill](https://github.com/Sean-Koval/hangar/blob/main/envs/skills/repo-conventions/SKILL.md) says the same. It becomes the turn's summary and the PR's. An agent that writes none still leaves a summary: the job's is then the end of the agent's output (its last 3,000 characters, redacted), which normally holds its final message. A `RESULT.md` left at the repo root instead is dropped from the PR ([scratch files](../repo-policy.md#agent-scratch-files)).
- **Retry** (`POST /api/jobs/{id}/retry`, the Fleet deck's button): a follow-up with the canned message "Re-run the failing check and fix what failed", in the same conversation, on the same branch, under the same rules as any follow-up. Its turn carries `origin: "retry"`, the audit log a `job.retry` entry (`mode: same`) and `/metrics` `hangar_job_retries_total{mode="same"}`, so retries are told apart from people's own follow-ups. **Start over** (`?fresh=1`) submits a new job with the same task and settings (instructions, title, repo, environment, harness, model, verify, delivery, budgets, labels) from a clean sandbox on the base branch, labelled `retry_of: <old id>`; the old job stays as it ended (`mode: fresh`, with `new_job`). Start over takes a finished job only (cancel a running one first), and not one from outside the team (start it again where it came from, so its approval applies), an experiment, an environment setup or a change set. The response is the job's view with `retry: {"mode", "of"}`.
- **Take over:** work in the sandbox yourself. Your commits go out with the next follow-up's delivery.
- After a turn the sandbox stays up while anyone is using it, and at least `keep_minutes` (default 120). Then it is checkpointed and released.

### The pull request

The PR body is the agent's summary, then what hangar knows: the verification, files left out, the check's own tests
the agent changed, best-of-N's attempts, screenshots, the job line (`hangar job … · requested by … · environment /
harness / model`), usage and `Closes #N` for an issue's job. It ends with **git trailers** (#137), so "which harness
wrote this PR" can be reviewed, read by a script and joined to merged-PR outcomes:

```
Hangar-Job: job-1a2b3c4d
Harness-Digest: sha256:…
Environment-Digest: sha256:…
Harness-Lock: team-default@r14
```

`Harness-Digest` and `Environment-Digest` are the latest turn's `harness_digest` and `environment_digest` from its
[manifest](../specs/0014-sister-tool-interfaces.md#2-the-effective-harness-manifest-hangarmanifestv1); `Harness-Lock`
appears only for a job [thawed from a lock](../harness-lock.md#thaw-and-drift). When the job's turns ran with different
harness digests, each one gets a line, in the order it first ran with the latest turn's last, followed by the turns
it covers (`Harness-Digest: sha256:… (turns 1-2)`). A turn recorded before digests existed adds nothing: no trailer
is printed without a value. `git interpret-trailers --parse` reads them from the body (hangar separates its part
from the summary with `___`, since git takes a `---` line for the end of a message; a summary that has its own `---`
line needs `--no-divider`). The body goes through the [secret scan](../security/secret-scanning.md#secret-scanning) like everything hangar posts. When
hangar [lands the PR](../landing.md#4-how-a-pr-lands), the merge commit's message ends with the same trailers.

## 6. Forks and best-of-N

For work where one try often misses (a flaky test, a tricky bug, an ambiguous refactor), ask for several attempts and keep the best:

```bash
hangar job submit "fix the flaky test_export" --repo https://github.com/acme/app --verify "pytest tests/test_export.py -q" \
  --best-of 3 --wait          # add --judge to have an agent compare the attempts that pass
hangar job attempts job-3fa2c1d0
```

Over REST or MCP: `"strategy": "best_of_n", "n": 3` (2–8), and optionally `"judge": true`.

How it runs:

1. hangar prepares the job once: a sandbox, the branch checks and the context files. Then it checkpoints that state.
2. It [forks](../sessions.md#forks-and-best-of-n) the job's session into N attempt sessions. Each attempt restores the checkpoint into its own sandbox, on branch `<job branch>-attempt-<i>`, and runs the agent on the same prompt. The agent sees `$HANGAR_ATTEMPT` (1…N). Each attempt is a full first turn: it gets the job's local work (`patch`/`bundle` context), its `output_schema` (with the repair run when the result is missing) and the [`HANGAR_IO`](../context-exchange.md) protocol, and the job's `budget` caps each attempt (and the judge) as it caps any agent turn.
3. Each attempt is verified in its own sandbox, and what it left uncommitted is committed there. An attempt stopped over budget or by its timeout is still ranked (below those that finished cleanly), and delivered as a draft if it wins.
4. Ranking:
   - attempts that pass `verify` come first;
   - among those, a short judge turn orders them when `judge` is set (it reads each attempt's diff and summary); otherwise the smallest diff wins, then the earliest finish;
   - attempts whose diff changes, renames or deletes the verify command's own test files are flagged (`touched_tests`) and ranked last. These are the paths the command names or, when it names none (`pytest -q`, `make test`), any test file. New test files are fine.
5. The winner is delivered as the job's branch and PR. Its summary, structured result and artifacts are the job's. The PR body has a table of all attempts. The other attempts are checkpointed and their sandboxes released. With `HANGAR_BEST_OF_N_PUSH_ATTEMPTS=true`, they are also pushed as `<job branch>-attempt-<i>`.

If no attempt finished but one asked a question (`hangar-agent ask`), the first to ask carries the job: it goes to `needs_input` with that question, and the answer (a follow-up, a `/hangar` comment, or a Slack thread reply) continues that attempt's session.

`get_job` and `GET /api/jobs/{id}` list `attempts`: session, branch, outcome, exit code, verify result, diffstat, lines changed, `touched_tests`, score, rank, summary and cost (from the attempt's [usage](../activity.md)). The job's `usage` is the sum over every attempt, its repair run and the judge. `GET /api/jobs/{id}/events` is one stream across all of them (each event from an attempt carries `data.attempt`), so one `since` cursor works before and after delivery.

- **Follow-ups** continue the winner's session: same conversation, sandbox and branch.
- **Cancel** stops every attempt. A control-plane restart re-attaches to each running attempt. One attempt's sandbox dying (or its agent failing) doesn't fail the job; it is recorded and left out of the ranking.
- To carry on with an attempt that wasn't delivered, fork its session: `hangar session fork job-3fa2c1d0-attempt-2 "..."`.

**Cost:** N attempts are N sandboxes running at the same time and N agent runs (the judge adds one more short run in a sandbox of its own). The preparation runs once. A job holds one worker slot (`HANGAR_JOB_CONCURRENCY`) however many attempts it runs, so the sandbox count, not the job count, is what to watch: `HANGAR_BEST_OF_N_MAX` caps N, and `HANGAR_MAX_SANDBOXES` caps the fleet.

