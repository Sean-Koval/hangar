# An agent loop that finishes the job

The verify gate inside the turn and the PR auto-fix loop. Part of [jobs](README.md).

Two loops keep the agent working until the job's own checks agree it is done, without someone pasting failures back to it. Both are on by default. The design is items 5 and 6 of [spec 0012](../specs/0012-cutting-edge.md).

## The verify gate inside the turn

With `verify` set, the agent sees a failing check *before* its turn ends, and fixes it in the same conversation:

- **Claude Code:** hangar passes a [Stop hook](https://code.claude.com/docs/en/hooks#stop) with `--settings '<json>'`. When Claude finishes, the hook runs the check in the repo. If the check fails, the hook exits 2 with the check's output on stderr, which sends Claude back to work with that output. The check has to pass, or the rounds have to run out, before the turn can end.
- **Codex** (in a [live turn](../steering.md)): hangar's driver runs the check when the turn completes. A failure goes back into the same conversation as the next message.
- **Other harnesses** (pi, Antigravity, or Codex without python3 in the image): hangar starts a follow-up turn with the failure. It does this when the agent finished cleanly and hangar's own check then failed.

Rounds and limits:

- **Per turn:** each failure sent back is a round. A turn and any follow-up turns it needs share `max_verify_rounds` (default 3, or `HANGAR_MAX_VERIFY_ROUNDS`).
- **Time:** the in-turn gate stops sending failures back when the turn's time limit is close. It leaves room for one more check and a fix.
- **Budget:** the rounds share the turn's [budget](../activity.md). Follow-up turns get what is left of it, not a fresh one.
- **Job view:** each turn shows `verify_rounds`, and hangar's follow-up turns show `trigger: "verify"`.

**hangar's own check after the turn still decides delivery.** The gate is advisory: it helps an agent that wants to pass the check, and it is not a control against one that doesn't. It runs inside the sandbox, as the agent's user, so the agent can get past it:

- **Settings files don't switch the hook off.** It lives on the command line, not in a file, and its `disableAllHooks: false` outranks the user, project and local settings files. This was verified with Claude Code 2.1.296: `disableAllHooks: true` in all three files leaves the hook running.
- **Other ways around it remain.** Managed settings (`/etc/claude-code/managed-settings.json`) outrank the command line, and the agent can write them in an environment that sets `sudo: true` (off by default since #124; a job's Docker sandbox under `HANGAR_INTAKE_POSTURE=enforce` runs with `no-new-privileges`, so sudo fails there anyway). The agent can also use up the rounds by writing to the rounds file, or put its own `claude`, `pytest` or other command first on `PATH` for this turn or, through `~/.hangar/env`, for later ones.
- **In a split sandbox with `approvals: ask` or `deny-risky`**, hangar starts Claude Code itself: by its path, with its own environment and settings ([hangar-owned harness settings](../steering.md#hangar-owned-harness-settings)). There the agent can't switch the Stop hook off for a later turn. It can still use up the rounds, and the check it runs is the repository's own code.

What decides is hangar's check after the turn. The hook's result never feeds delivery: hangar runs the job's `verify` itself, and only that exit code decides between a PR and a draft.

That check also runs in the sandbox, through the agent's `~/.hangar/env` (see the [threat model](../security/README.md)). So it tells you whether the work passes, and it can't prove that the agent didn't game it. Review the PR as you would any contributor's. Two things help:

- **The check stays the job's own.** The command is the job's `verify`, stored by hangar, and the agent can't change it.
- **Edited tests are flagged.** The agent can change files the check reads. So when the gate is on, hangar runs [best-of-N's test check](working-with-the-agent.md#6-forks-and-best-of-n) on every job: a diff that changes, renames or deletes the check's own test files is listed in `result.touched_tests`. It is also called out in the PR body, and the PR is delivered as a draft. The check covers the test files only. When `verify` names paths, it covers those paths; otherwise, any test file and `conftest.py`. It doesn't cover runner configs such as `pyproject.toml`, `pytest.ini`, `package.json` or a `Makefile`.

Turn the gate off per job with `"verify_in_loop": false` (`--no-verify-in-loop`), or for everything with `HANGAR_VERIFY_IN_LOOP=false`.

**A red base.** A standing check (from the repo's policy or the environment) runs once on the base commit before the agent starts, shared by every job on that base. When the base already fails it, the gate is off, and only the failures the change adds count against the job. See [repo-policy.md](../repo-policy.md#the-check-on-the-base-commit).

**Scratch files.** Notes the agent leaves in the repo (`HANDOFF.md`, `RESULT.md`, `NOTES.md` at the root, `*.orig` and `*.rej`) are kept out of the PR unless the task names them. See [repo-policy.md](../repo-policy.md#agent-scratch-files).

## PR auto-fix

After hangar opens a job's PR, it reacts to the PR's CI results and reviews. It does this by starting a follow-up turn on the job's session, as a `/hangar` comment would:

- **CI:** a `check_run` or `check_suite` that completes as `failure`, `timed_out` or `startup_failure`, or a commit `status` of `failure` or `error`. It counts only on the commit hangar last pushed to the PR (the job's `result.head_sha`).
  - The agent gets `ci-failures.md` with each failing check's output and annotations, and, for GitHub Actions, the end of the job's log (`HANGAR_AUTOFIX_LOG_BYTES`, plus the `##[error]` lines before it).
  - Logs are read with the control plane's token. The download link GitHub redirects to is fetched without the token, and only from GitHub's log storage (`*.actions.githubusercontent.com`, `*.blob.core.windows.net`) or the API's own host. For other hosts the agent gets the check's output without the log.
  - Only checks tied to the job's own PR count: by its number or the job's branch, and the job's head commit. A fork's PR can reuse the branch name and the commit, but its checks arrive without either.
  - Anyone who can post a check or a status on that commit can start a round: people with write access, and apps with the *Checks* or *Commit statuses* permission. What they post is fenced as untrusted text in the agent's prompt, and the round limit applies.
- **Reviews:** a review that requests changes, or that comments with inline comments, and inline review comments.
  - The round runs with the job's person's own login, so a review counts only from someone they or the repo policy trust ([who is trusted](../team/who-is-trusted.md), rule 6): the job's person (their linked GitHub account), an admin's linked account, a login in the repo policy's `approvers`, or a code owner of every path the PR touches. On a team job (no person), also anyone the GitHub front door trusts (a linked teammate, the repo's owners, members and collaborators, or `HANGAR_GITHUB_TRUSTED`). Never a bot, hangar's own App (`<slug>[bot]`) or hangar's own account and comments. A refused review is audited (`github.refused`, `untrusted review`).
  - The agent gets `review-comments.md` with each comment's author, file, line and diff hunk. Every comment is fenced and the file is an untrusted attachment, whoever wrote it: the instruction is hangar's ("address each comment that asks for a change to this task's code"), never the reviewer's. The agent is told to ask (`hangar-agent ask`, so the job goes to `needs_input`) when a comment is ambiguous, instead of guessing.

Loop control:

- **Coalescing:** everything that arrives within `HANGAR_AUTOFIX_COALESCE_SECONDS` (60) becomes one turn. Ten checks failing on one push make one round.
- **One CI round per push:** if the agent pushed nothing new, the same commit failing again doesn't start another round. Neither do hangar's own comments.
- **Round limit:** at most `HANGAR_AUTOFIX_MAX_ROUNDS` (3) rounds per PR, at least `HANGAR_AUTOFIX_COOLDOWN_SECONDS` (120) apart. After the last round, hangar says once on the PR that auto-fix stopped.
- **Someone else's push:** if the PR's head isn't the commit hangar pushed, hangar won't push over it. It says so once on the PR and pauses until it pushes again.
- **On the PR:** hangar comments at the start of each round with what failed. The turn's result is reported as usual.
- **Job state:** a round waits while a turn is running. Rounds don't start for a job that is cancelled or waiting for an answer.
- **Restarts:** triggers still waiting for their window survive a control-plane restart.

Settings:

- **Per repo:** `HANGAR_GITHUB_AUTOFIX='{"acme/app": "ci"}'`, with the values `ci,reviews`, `ci`, `reviews` or `off`.
- **Per job:** `"autofix"` in the request (`--autofix` on the CLI, `autofix` in MCP `submit_job`). A job's setting can only narrow its repo's.
- **Default:** `ci,reviews` for every PR hangar opens. PRs that hangar didn't open are never touched.
- **The job view:** `autofix_state` shows the rounds, the pending triggers and why the loop stopped.

The webhook needs the `pull_request_review`, `check_run`, `check_suite` and `status` events ([jobs from GitHub](from-github-slack-jira.md#4-trigger-from-github)): run `hangar setup github` again, or `hangar doctor --fix`.

