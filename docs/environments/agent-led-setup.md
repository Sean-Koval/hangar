# Agent-led setup

A repo nobody has configured yet gets its [`.hangar/environment.yaml`](repo-file.md) from agents, validated by hangar,
as a PR. This follows Cursor's [autoinstall](https://cursor.com/blog/bootstrapping-composer-with-autoinstall) and its
[agent-driven setup](https://cursor.com/docs/cloud-agents/setup). Re-runs, the models and budget, and its safety are
on [re-runs, budget and safety](agent-led-setup-safety.md).

```console
$ hangar env setup acme/app --wait
env setup job-1a2b3c4d of acme/app (python): queued
  [running] both agents run on claude-opus-5-5 (the strongest current model in anthropic's model list)
  [running] goal: job-5e6f7a8b reads the repo and proposes the commands
  [running] goal: 9 commands proposed at 3f2c1a9e0; core: install, lint, unit-tests, serve
  [running] setup: job-9c0d1e2f makes the commands work on branch hangar/job-9c0d1e2f
  [running] validating attempt 1: a clean sandbox of hangar/job-9c0d1e2f at 81aa04c2d
  [running] attempt 1: failed: `uv run pytest -q` exit 1, expected 0
  [running] validating attempt 2: a clean sandbox of hangar/job-9c0d1e2f at c4d5e6f70
  [running] attempt 2: passed
  [succeeded] succeeded: https://github.com/acme/app/pull/42
$ hangar env setup acme/app --every "0 9 * * 1"     # admin: re-run weekly, only when the environment broke
```

## Where it starts

It is a job (`hangar.kind: env-setup` in its labels). So it also starts from
`POST /api/env/setup {repo, environment?, harness?, model?, notes?, mode?, job_budget?}`, from `POST /api/jobs` with
that label, and from a schedule's or an inbound webhook's job template.

It also starts from an issue carrying the `HANGAR_ENV_SETUP_LABEL` label (`hangar-env-setup`). That is a GitHub issue
given it, or a Jira issue that reaches the trigger status (or label) with it. The
job's events, its result, its tracker write-back and the inbox work like any job's.

## 1. Goal

An agent reads the repo at the commit its sandbox cloned: README, Makefile, manifests and lockfiles, devcontainer and
compose files. It reads `.github/workflows` too, the best evidence of how the repo really builds and tests.

It proposes about ten target commands (install, build, lint, unit tests, start the app, a smoke request). Each has the
expected outcome in words and is marked core or not. It also names the services and secrets the repo needs. It is a
job of its own (`deliver: none`) with a structured result.

## 2. Setup

A fresh agent, in another job (`deliver: branch`), makes at least the core commands work. It writes the file:

- services from the allowlisted images, such as `postgres`, `redis` or a MinIO image an admin allows, standing in for
  real dependencies;
- long-running processes as terminals under `hangar-agent svc`;
- `verify` set to the unit-test command.

It records every stand-in under `mocks:`, writes a "Development environment" section in `AGENTS.md`, and commits. A
real secret it lacks is filed with `hangar-agent env request secret NAME`, never invented; it reports which commands
that blocks.

## 3. Validation, by hangar

The file is checked like `POST /api/envbuilds/validate`, and must set `verify` and list every mock the agent reported.
Then a clean sandbox of the setup branch runs it. That sandbox has no repo build and the proposed services beside it.
Its environment is the one the file's `extends` picks, when the repo's allowlist permits it.

The sandbox runs the file's `install` through the build path's install step. It starts the supervisor and waits for
its services and terminals. Then it runs the core commands, the ones the agent says work, and `verify`.

Exit codes decide first. Only for commands the goal agent marked `check_output` does hangar judge the output against
its own description, in a follow-up turn of the goal job.

A failure goes back to the setup agent with the output, as files it can read, up to `HANGAR_ENV_SETUP_RETRIES` (5)
times. After that the job fails. Its summary says exactly what is missing: each failing command and why, file errors,
and the secrets nobody gave.

## 4. The PR

The setup job's branch becomes a PR through [the landing path](../landing.md): the App identity, the review packet,
and an approval that lands it. The PR body has the validated commands as a table, what is mocked, the secrets still
needed, and `verify`. The packet adds an "Environment setup" line.

A setup that passes except for commands blocked by secrets the agent requested opens as a draft
(`reason: secrets_needed`). Merged, the file gets [builds](builds.md) like any other.
