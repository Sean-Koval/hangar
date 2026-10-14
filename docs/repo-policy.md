# Repo policy: the defaults every front door applies

Work reaches hangar through many doors: MCP, REST, the CLI, GitHub issues, Slack, A2A, inbound webhooks and schedules. A **repo policy** is one record per repository with the defaults its jobs get, whichever door they came through. A new kind of intake only has to name the repo.

```bash
hangar repo set acme/app --verify "uv run pytest -q" --env python
hangar repo set acme/app --model claude-code=opus --model codex=gpt-5.5
hangar repo list
hangar repo get acme/app            # the policy, and the environment it falls back to
hangar repo unset acme/app model    # clear one field
hangar repo unset acme/app          # remove the policy
```

| Field | What it sets |
| --- | --- |
| `verify` | The repo's check (see [jobs §7](jobs.md#7-an-agent-loop-that-finishes-the-job)). `""` means no check, even if the environment has one. |
| `environment` | The environment jobs run in when they don't name one. For a repo in `HANGAR_GITHUB_REPOS`, it must be one of the environments listed there. |
| `harness` | The default harness. It must be enabled in the environment; when a job runs in an environment without it, the job uses the environment's default and its events say why. |
| `model` | One model id, or one per harness (`{"claude-code": "opus", "codex": "gpt-5.5"}`), as in an environment's `model:`. A single id applies only to the policy's harness, or, without one, to jobs that didn't pick a harness: a model id means nothing to another harness. |

The repo is `owner/repo` for GitHub (any URL form, any case), or the git URL for another host.

## Precedence

For each field: **the job, then the repo's policy, then the environment, then hangar's settings.**

- A job's own `verify`, `environment`, `harness` or `model` always wins.
- An environment can carry a `verify:` too, the fallback for every repo that runs in it:

  ```yaml
  name: python-app
  extends: python
  verify: uv run pytest -q
  ```

- `verify: ""` at any level means "no check" and stops the fallback.
- The job's events say what came from the policy (`from the repo policy for acme/app: verify …`), and `verify_source` on the job says where its check came from: `job`, `repo` or `environment`.

`HANGAR_GITHUB_REPOS` stays the allowlist: which repos untrusted and service callers (GitHub, Slack, webhooks, `jobs` tokens) may start work on, and in which environments. A policy never widens it.

## REST

Admin tokens only. The repo is a query or body field, because git URLs carry slashes and colons.

| Route | |
| --- | --- |
| `GET /api/repos` | Every policy, each with `allowed_environments` (from `HANGAR_GITHUB_REPOS`), the environment's own `environment_verify`, and the `effective_verify`. |
| `GET /api/repos/policy?repo=acme/app` | One policy. |
| `PUT /api/repos/policy` | `{"repo": "acme/app", "verify": "pytest -q"}`: sets the fields sent and keeps the rest; a field sent as `null` is cleared. A policy no job could run with is refused: an unknown environment, an environment the allowlist doesn't have for the repo, or a harness the environment doesn't enable. |
| `DELETE /api/repos/policy?repo=acme/app[&field=verify]` | Clears the fields named, or removes the policy. |

Policies are stored in hangar's database, written durably like `hangar config` settings, and apply to the next job without a restart. Every change is audited (`repo.policy.set`, `.unset`, `.delete`).

## The check on the base commit

With a check on every front door, a red base branch would fail every job's check. The agent would spend its verify-fix rounds on failures it didn't cause, best-of-N would rank every attempt as failing, and every PR would go out as a draft. So hangar runs a job's **standing check**, the one from the repo's policy or the environment, once on the base commit before the agent starts:

- **Once per base commit.** The result is kept per (repo, base commit, command). Jobs that start on the same base while it runs wait for that run, and later jobs read the result, so a team running 60 sessions on one `main` pays for one check per merge. A check that times out or is killed isn't kept; the next job tries again.
- **Only for a standing check.** A `verify` the job brought itself is its acceptance test, written for the change, and expected to fail on the base.
- **A green base** changes nothing.
- **A red base** is reported in the job's events ("base was red: …"), the prompt (which failures already exist, with the output in `BASELINE.txt` next to the job's context) and the PR body (**Base was red**). The in-turn gate is off for the job, because it can't tell the base's failures from new ones. Hangar's check after the turn counts only the failures the change added:
  - it reads failing test ids from the output of pytest, `go test`, `cargo test`, jest and vitest, and subtracts the base's;
  - no new failures: the check counts as passed, and the PR isn't a draft for it;
  - new failures: they go back to the agent, named, as a verify-fix round;
  - when it can't read the ids on both sides, it can't tell what's new: no verify-fix round, the PR is a draft, and the job ends `succeeded` with reason `base_red` for a person to judge.

It counts test ids, so a check that fails for another reason in the same run can hide behind a red base: with `ruff check; pytest`, a new lint error leaves the test ids unchanged. Chain the steps with `&&`, so a failing first step stops the run and nothing can be compared.

## Agent scratch files

Delivery commits what the agent left, and agents leave notes. Hangar keeps these out of the PR, with a narrow rule so it never drops work someone asked for. A file is scratch when all of these hold:

- it is new on the job's branch (a file the base commit has is never touched);
- it is `HANDOFF.md`, `RESULT.md` or `NOTES.md` at the repo root (any case), or a `*.orig` or `*.rej` patch leftover anywhere;
- the task doesn't name it: neither the instructions nor any follow-up mention the file name.

Scratch the agent left uncommitted isn't staged. Scratch it committed itself is removed in a separate commit, `hangar: drop agent scratch files`, so the change is visible and easy to revert. Either way the file stays in the sandbox, the job's events and `result.dropped_files` list it, and the PR body says what was left out.
