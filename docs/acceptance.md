# Acceptance: proving a deploy on the cluster

`scripts/acceptance/gke.sh` proves a running hangar does what the release says, on the real cluster. It talks to
hangar the way its users do, over REST and MCP, so it runs from your laptop, from `deploy.yml`, or from anywhere a
token reaches hangar, and it needs no kubectl (`--no-kubectl` leaves out the two checks that use it). This page is the
runbook for the **first live run after deploying Waves 1–3**: what to set up once, the order to run the checks in,
and what each failure most likely means and where to look.

```bash
hangar login https://hangar.example.com --token-file ~/.hangar/gke-admin-token
bash scripts/acceptance/gke.sh --no-kubectl                  # every check that costs nothing
bash scripts/acceptance/gke.sh --help                        # every option
```

Each check prints `PASS`, `FAIL` or `SKIP` with its time and one line, then the summary table, and writes a JSON
report (`--report`, default `./hangar-accept-report.json`) with every check's status, time and timings.

## Safe on a production install

The suite is built to run against the hangar your team uses:

- **Its own names.** Everything a run makes is named `accept-<run>`: sandboxes, sessions, tokens, test people,
  branches and PRs. Nothing else is touched.
- **Dedicated test environments.** Checks that need an environment save their own, `accept-computer`,
  `accept-egress`, `accept-services` and `accept-envbuild` (`--test-env-prefix` changes `accept`). They extend
  `--env` (default `minimal`) or `--session-env` (default `claude-python`) and are **kept** between runs, so their
  images are built once; a run saves one again only when what it needs changed. They show up in `hangar env list` and
  in `hangar images plan`, so the nightly prepull pulls them too. An environment of that name that isn't the suite's
  (its description says whose it is) is never saved over: the check fails and asks for another prefix.
- **Dedicated repos.** Jobs that push or open PRs use `--test-repo`; the build check commits to `--fixture-repo`'s
  default branch and puts the file back afterwards. Nothing pushes to any other repo, or to a default branch other
  than the fixture's.
- **Cleanup, even on failure.** What a check makes is recorded in the run's state file *before* it is made. At the
  end, after a failure or Ctrl-C too, cleanup brings powered-down pools back on, cancels jobs, closes PRs and deletes
  their branches, denies pending requests, removes sessions, sandboxes, tokens and test people, and puts back the
  landing check's repo policy and the fixture repo's files. It removes only what its own state file lists, and puts
  back only a policy field or a file that still holds what the run set: a change someone made in the meantime stays.
  Each check also releases the sessions and sandboxes it made as soon as it ends, pass, fail or skip, so the next
  check has the capacity; the run's shared sandbox and session (from `sandbox` and `session`, which later checks use)
  stay until the cleanup. `scale-smoke` deletes its sessions before it returns, whatever happened.
  `--keep` leaves everything for debugging; the run prints the command that cleans up later
  (`python3 scripts/acceptance/hangar_accept.py cleanup --state <file>`).
- **Room for the team.** `scale-smoke` skips when its sessions would leave fewer than a tenth of
  `HANGAR_MAX_SANDBOXES` (at least 10) free, so the team's own creates aren't refused.
- **Room in the pool.** The capacity-bound checks (`scale-smoke`, `prepull`, `warmstart`) skip, with the numbers,
  when the sandbox pool has no room and can't grow: once hangar has said for 90 s that a sandbox waits because "the
  cluster can't add a node" (a quota) or the pool "is at its maximum size", rather than waiting out
  `--boot-timeout`. `scale-smoke` says how many of its sessions fit (`the sandbox pool fit 7 of 20 sessions: …`);
  rerun it with that many `--scale-sessions` or give the pool room. A node being added is waited for as before.
  Pods that alternate "can't add a node" with "no node has room right now" keep counting towards the 90 s. One
  session's failure doesn't stop the others: when boots fail, `scale-smoke` reads each one's error, session reason
  and sandbox stage, and skips with the fit count if every failure was capacity (a no-room reason, a quota, or a
  start timeout while the pool said it was full or others of the environment still waited); any other error fails
  the check with its own text.
- **Nothing that costs money by default.** Checks that need a real agent turn run only with `--with-model`, and each
  of their jobs and turns has a budget of `--max-usd` (default $1), where hangar stops it. With everything on, that is
  at most about 13 of them: `model`, `steer`, egress's request, `secrets`, `landing`, and `scale-smoke`'s queue (its
  first person's job cap plus two, and one for each other test person: 8 with the defaults). The run prints what they
  actually spent (`model spend: $…`, also in the report), usually cents each. The power cycle runs only with
  `--power-cycle`, and skips while anyone else has a live session (idle ones too: their sandbox would go), an active
  job, or any other sandbox; it looks again just before powering down.
- **A login for the turns, or a skip.** `model`, `steer` and `secrets` first ask hangar
  (`GET /api/credentials/login`) whether a login for the harness would reach their turn, and skip, naming the fix,
  when none would. The sessions and jobs a deploy's admin token makes belong to no person, so a personal subscription
  login never reaches them: add a team API key (`hangar cred add NAME --team --harness claude-code --kind api_key
  --file key.txt`), or pass `--as PERSON` (an admin) to create them as that person, whose own login then reaches the
  sessions (a job still needs an API key: subscription logins never reach automated work).
- **One run at a time.** Runs don't share names, so two that overlap don't clean up each other's things, but they
  do share the test environments, the fixture repo and the test repo's policy: the `envbuild` and `landing` checks
  of two overlapping runs can fail each other.
- **Secrets from the environment only**, never flags, since a command line is visible to every process.

## Core checks: what a deploy rolls back for

`deploy.yml` runs the suite in two steps. The first runs the **core** checks (`--only core --gate core`); a failure
there rolls the deploy back. The second runs the rest (`--skip core`) and can't fail the deploy: it has
`continue-on-error` and a time limit of its own (`timeout`, so its cleanup still runs), so neither a failing nor a slow
team check rolls back. Both land in the run summary and in hangar's deploy record. A forced deploy while the pools are
off runs only `doctor`, `power` and `metrics`, and no team checks. In `--only` and `--skip`, `core` stands for the
core checks.

`doctor` is core, but a doctor check can say it isn't a reason to roll back (`deploy_gate: false`). `credential
health` does: a person's login that expires, or stops working, during the deploy window is printed in the run
(`failing, but not a reason to roll back`) and fails `hangar doctor` as before, but a rollback can't fix it.

| | Checks |
| --- | --- |
| **Core** (`CORE` in gke.sh) | `doctor`, `sandbox`, `exec`, `stream`, `files`, `mcp`, `isolation`, `session`, `backup`, `metrics`, `power` |
| Reported | `k8s`, `model`, `steer`, `gateway`, `computer`, `egress`, `services`, `heavy`, `envbuild`, `secrets`, `landing`, `intake`, `scale-smoke`, `prepull`, `warmstart`, `status`, `clouddoctor` |

Core checks exercise the control plane's own paths, run in minutes and depend on nothing outside hangar, so a core
failure after a deploy almost always means the new image broke something, which a rollback fixes. The reported checks
depend on fixtures (a test repo, Jira, the GitHub App), on images built the first time they run, or on the cluster
(power, prepull), where a rollback of the control plane fixes nothing. Run by hand, the default `--gate all` fails on
any check.

## The report

```json
{"version": 1, "target": "https://hangar.example.com as ui:deploy", "gate": "core", "exit": 0, "cleanup": "ok",
 "core": ["backup", "doctor", ...], "failed": ["computer"], "core_failed": [],
 "spend": {"usd": 0.42, "jobs": 10, "sessions": 3, "unpriced": 0},
 "checks": [{"check": "computer", "status": "fail", "core": false, "seconds": 412.3, "summary": "...",
             "timings": {"create": 380.2, "chromium_screenshot": 6.1, "record_3s": 4.0, "screen_open": 1.2},
             "data": {...}}]}
```

`timings` holds each check's own measurements in seconds: create times, the scale test's p50 and p95, the power
cycle's down and up, the prepull's first creates. `spend` is what the run's agent turns cost (null when cleanup
didn't run). `deploy.yml` puts the table in the run summary and the statuses in
hangar's deploy record (`hangar power status` shows the last one). `--record FILE` also appends each check's line to a
JSON-lines file.

## Testing the suite

- `tests/test_acceptance_script.py` runs gke.sh against a hangar on Docker: the control-plane checks for real, with
  GitHub replaced by a local git server behind the gateway.
- `tests/test_e2e_acceptance_docker.py` runs the team checks that can run on Docker for real: `computer`, `egress`,
  `services`, `scale-smoke`, `power`, `status`, `clouddoctor`; and checks that `envbuild`, `landing`, `intake` and
  `prepull` skip cleanly.
- `tests/test_acceptance_team.py` runs the scale test, Jira intake, the power cycle, `status`, the doctor's dry run
  and the core gate against a real hangar on the fake provider, and the rest by their parts: the RFB client against hangar's own view-only filter, the
  screenshot and egress verdicts, the synthetic token against hangar's secret scanner, the fixture repo's commits on a
  local repo, and the secrets and landing flows against stand-in hangar and GitHub APIs.

The Docker suites are opt-in locally (`HANGAR_E2E=1`) and run in CI.

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="before-the-first-run-fixtures"></span>Before the first run: fixtures | [The first run: fixtures and the live run](acceptance/first-run.md#before-the-first-run-fixtures) |
| <span id="the-first-live-run-after-deploying-waves-13"></span>The first live run after deploying Waves 1–3 | [The first run: fixtures and the live run](acceptance/first-run.md#the-first-live-run-after-deploying-waves-13) |
| <span id="when-a-check-fails"></span>When a check fails | [When an acceptance check fails](acceptance/failures.md) |
