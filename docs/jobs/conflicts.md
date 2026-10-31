# When a job's PR conflicts with main

What hangar does when other work lands on a job's base branch and its PR no longer merges. Part of [the agent loop](agent-loop.md#pr-auto-fix).

## Why it happens

Jobs run side by side, each from the base branch as it was when the job started. Their PRs often touch the same shared files: the changelog, the docs nav, an editorial ceiling or the code owners. The first one to merge leaves the others conflicted.

GitHub then marks them `dirty`, and nobody can merge them until the branch takes the new base in.

## What hangar does

When a PR merges into a branch, GitHub sends a `pull_request` event. hangar then checks every other open PR it opened on that base:

1. It waits for GitHub to work out whether each PR still merges, and asks again a few times while the answer is unknown.
2. A PR that still merges is left alone.
3. A `dirty` PR gets an auto-fix round, like a failing check. The agent merges the base into its branch and resolves the conflicts.
4. The agent then runs the job's check, because what landed can break the change without a conflict. hangar pushes, and the PR updates in place.

The agent merges rather than rebases. hangar pushes without force, so the PR keeps its history, its reviews and its approvals' context. When a conflict needs a decision, such as two changes to the same behaviour, the agent asks (`hangar-agent ask`) instead of guessing.

## Limits

The round counts toward `HANGAR_AUTOFIX_MAX_ROUNDS`, like any other. hangar won't push over someone else's commit on the branch. Only merges through a PR start a check: a direct push to the base doesn't.

Turn it off with `conflicts` left out of `HANGAR_GITHUB_AUTOFIX` for a repo, or of a job's `autofix`.
