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
  `--keep` leaves everything for debugging; the run prints the command that cleans up later
  (`python3 scripts/acceptance/hangar_accept.py cleanup --state <file>`).
- **Room for the team.** `scale-smoke` skips when its sessions would leave fewer than a tenth of
  `HANGAR_MAX_SANDBOXES` (at least 10) free, so the team's own creates aren't refused.
- **Nothing that costs money by default.** Checks that need a real agent turn run only with `--with-model`, and each
  of their jobs and turns has a budget of `--max-usd` (default $1), where hangar stops it. With everything on, that is
  at most about 13 of them: `model`, `steer`, egress's request, `secrets`, `landing`, and `scale-smoke`'s queue (its
  first person's job cap plus two, and one for each other test person: 8 with the defaults). The run prints what they
  actually spent (`model spend: $…`, also in the report), usually cents each. The power cycle runs only with
  `--power-cycle`, and skips while anyone else has a live session (idle ones too: their sandbox would go), an active
  job, or any other sandbox; it looks again just before powering down.
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
| **Core** (`CORE` in gke.sh) | `doctor`, `sandbox`, `exec`, `stream`, `files`, `mcp`, `isolation`, `session`, `e2b`, `backup`, `metrics`, `power` |
| Reported | `k8s`, `model`, `steer`, `gateway`, `computer`, `egress`, `services`, `envbuild`, `secrets`, `landing`, `intake`, `scale-smoke`, `prepull`, `warmstart`, `status`, `clouddoctor` |

Core checks exercise the control plane's own paths, run in minutes and depend on nothing outside hangar, so a core
failure after a deploy almost always means the new image broke something, which a rollback fixes. The reported checks
depend on fixtures (a test repo, Jira, the GitHub App), on images built the first time they run, or on the cluster
(power, prepull), where a rollback of the control plane fixes nothing. Run by hand, the default `--gate all` fails on
any check.

## Before the first run: fixtures

Each check that needs something skips, saying what, until it has it.

| For | Set up once | Tell the suite |
| --- | --- | --- |
| `secrets`, `landing` | A **test repo** (private is fine, nobody's real work in it) in `HANGAR_GITHUB_REPOS`, with the GitHub App installed and `HANGAR_GITHUB_APP_CREDENTIAL` set ([landing.md](landing.md)), and **"Allow auto-merge"** on (Settings → General: GitHub refuses to arm without it). The App's webhook (or hangar's) must deliver pull request and review events; `hangar doctor` says when it doesn't | `--test-repo OWNER/REPO` or `HANGAR_ACCEPT_TEST_REPO` |
| `landing`'s approval | A **second GitHub account** added as a collaborator with write access to the test repo, and a **classic** token of its with the `repo` scope (GitHub's fine-grained tokens don't work on a repo where that account is only a collaborator). On the test repo's default branch, branch protection with one required approval, stale-approval dismissal, and **"Require review from Code Owners"** with a `CODEOWNERS` that names you, not that account: GitHub then waits for you after hangar arms auto-merge, so the check can see it armed and then disarmed by a push. Don't make a check required unless the test repo runs it: hangar arms only once required checks are green | `HANGAR_ACCEPT_APPROVER_TOKEN` |
| pushes to the test and fixture repos, closing PRs | A token that writes to both (or `gh auth login`) | `HANGAR_ACCEPT_GITHUB_TOKEN` |
| `envbuild` | A **fixture repo** whose default branch is unprotected (the check commits `.hangar/environment.yaml` and `.gitignore` there and puts them back). Public is simplest; a private one needs a credential hangar clones it with | `--fixture-repo OWNER/REPO` or `HANGAR_ACCEPT_FIXTURE_REPO` |
| `envbuild`'s preview | `HANGAR_PREVIEW_DOMAIN` with wildcard DNS (and a wildcard certificate on GKE) | nothing: it skips the preview part without it |
| `intake` | Jira intake configured (`HANGAR_JIRA`, [intake.md](intake.md)) with a Jira project mapped to the test repo, such as `"ACC": {"repo": "you/accept-target"}` in its `projects` (a project nobody uses is fine: the issue is synthetic and never created in Jira). The synthetic issue's job is cancelled at once but may start first, so without a project on the test repo the check skips rather than start a job on a real repo; `--jira-project KEY` overrides that | `--test-repo`, `HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET` (the value of the vault credential `webhook_secret_credential` names) |
| `power --power-cycle` | `gcloud` logged in to the project, and `deploy/terraform/gke`'s outputs or `PROJECT`, `ZONE`, `CLUSTER` for `deploy/gke/power.sh`. With `HANGAR_POWER_UP_JOB` set, powering up goes through hangar instead | `--power-cycle`; `--power-down-cmd`, `--power-up-cmd` to override |
| `model`, `steer`, `secrets`, `landing`, egress's request, scale's job queue | A model credential in `--session-env` | `--with-model`; `--max-usd` caps each job or turn |

In `deploy.yml`, the repos are variables and the tokens secrets of the `production` environment:
`HANGAR_ACCEPT_TEST_REPO`, `HANGAR_ACCEPT_FIXTURE_REPO` (variables), `HANGAR_ACCEPT_GITHUB_TOKEN`,
`HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET` (secrets). It never passes `--with-model` or `--power-cycle`. Leave them unset
until the checks pass by hand: each check without its fixture skips.

## The first live run after deploying Waves 1–3

Run these in order after [the upgrade](upgrading.md#upgrading-to-the-team-release-75110) (its step 14). Each step
assumes the ones before it passed; when one fails, the next section says where to look. All of them work with
`--no-kubectl`.

**0. Before anything.** `hangar doctor` shows no failures (warnings are fine to read and move on), and
`hangar power status` says the sandbox pools are `up`. `hangar status` shows both at a glance, with the loops'
heartbeats, the day's failures, what is building, the SLOs, credential health and the `models` line (what jobs run on
by default, and **whether a budget caps it**: set one first if it says `NO job budget or daily cap`, since
`--with-model` runs paid turns). The suite needs sandboxes; while the pools are off it refuses
every create but `power`'s, which expects exactly that. Run it as an admin (`hangar login` with an admin token, or
`HANGAR_URL` and `HANGAR_TOKEN`).

**1. The control plane (core), ~10 min.**

```bash
bash scripts/acceptance/gke.sh --no-kubectl --only core
```

This is what `deploy.yml` gates on. If it fails, stop here: everything after builds on it.

**1b. The operator's views, a minute, free.**

```bash
bash scripts/acceptance/gke.sh --no-kubectl --only status,clouddoctor
```

`status` reads `hangar status` (as `GET /api/status`) and checks its sections; `clouddoctor` runs the Cloud Doctor
dry (`hangar admin doctor run --dry-run`, `GET /api/admin/doctor/plan`): what a run would do with the flight records
since the last one (count, watch, propose a request, or start a root-cause job, and what would stop that job, such as
no team credential), starting no job and writing nothing. On a fresh install it has nothing to do.

**2. The sandbox's own computer, egress and services, ~20 min the first time.**

```bash
bash scripts/acceptance/gke.sh --no-kubectl --only computer,egress,services
```

The first run saves `accept-computer`, `accept-egress` and `accept-services` and builds their images; the computer's
adds about 400 MB (Chromium and Playwright), so its first create can take ten minutes or more. Later runs start in
seconds. On GKE `computer` also asserts the sandbox runs under gVisor.

**3. An environment that lives with the repo.**

```bash
HANGAR_ACCEPT_GITHUB_TOKEN=... bash scripts/acceptance/gke.sh --no-kubectl --only envbuild --fixture-repo you/accept-fixture
```

Two builds (a good one, then a broken install) and a sandbox from the good one: allow 5–15 minutes.

**4. Front doors and landing (paid turns).**

```bash
export HANGAR_ACCEPT_GITHUB_TOKEN=... HANGAR_ACCEPT_APPROVER_TOKEN=... HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET=...
bash scripts/acceptance/gke.sh --no-kubectl --only intake
bash scripts/acceptance/gke.sh --no-kubectl --with-model --test-repo you/accept-target --only secrets,landing,egress
```

`landing` waits for GitHub's webhooks (the approval, the push) and, when the test repo requires checks, for CI.

**5. The team at scale.**

```bash
bash scripts/acceptance/gke.sh --no-kubectl --only scale-smoke                  # 20 sessions, 3 test people
bash scripts/acceptance/gke.sh --no-kubectl --only scale-smoke --with-model     # + the fair job queue (~8 short jobs)
```

Twenty sessions may add a node; `--scale-p95` (300 s) is the slowest p95 create it passes. It skips when the fleet
is too full to leave the team room (see above): run it in a quieter hour.

**6. Power, in a quiet window** (nobody else with a live session, an active job or a sandbox; it lists what it saw
and skips otherwise).

```bash
bash scripts/acceptance/gke.sh --no-kubectl --only power,prepull,warmstart --power-cycle
```

The pools go down (a create is then refused at once), come back up with the prepull, and the first create of each
prepulled environment is timed. Expect 10–20 minutes, most of it nodes starting.

**7. Everything, as deploy.yml runs it**, then a real deploy through `deploy.yml`:

```bash
bash scripts/acceptance/gke.sh --no-kubectl --only core --gate core                 # what rolls back
bash scripts/acceptance/gke.sh --no-kubectl --skip core,model,steer,gateway         # what is only reported
```

**What this suite doesn't cover: the harness contract.** The real-CLI contract tests and the nightly harness canary
([harnesses.md](harnesses.md#the-canary)) run in GitHub Actions only (`harness-contract.yml`, `harness-canary.yml`,
`harness-canary-land.yml`), never against the cluster: they need no sandboxes and run while the pools are off. Check
them in the repository's Actions tab: `contract` green on the deploy's commit, and the last nightly canary run green
or its `harness-canary` issue read. Their owner setup is in [upgrading.md](upgrading.md#11b-the-harness-canary).

## When a check fails

Each line is the most likely cause first. Where to look is always hangar itself: `hangar doctor`,
`hangar status` (where your hangar has it), `hangar power status`, `hangar job get|events`, `hangar env builds|build-show`, `hangar requests ls`, `hangar inbox`,
or the REST views named. `--keep` leaves what the run made, so you can look at the exact sandbox, session or job.

| Check | What it proves | A failure most likely means | Where to look |
| --- | --- | --- | --- |
| `doctor` | hangar's own checks pass | whatever the failing doctor check names; a deploy that broke it | `hangar doctor` (`--fix` repairs what it can) |
| `sandbox` | a create works | the pools are off or full, an image is missing, a quota | `hangar power status`; the create's error; `hangar images plan` |
| `exec`, `stream` | hangar-envd carries exec, unbuffered | envd not reachable (NetworkPolicy `allow-hangar-envd`), a proxy buffering SSE | `--no-expect-envd` to compare; `GET /metrics` `hangar_sandbox_exec_seconds` by transport |
| `files`, `mcp` | MCP works within claude.ai's 230 s; a dropped client's create still comes up | `HANGAR_MCP_TOOL_WAIT_SECONDS` too high; a regression of #86 | the sandbox's `stage` in `hangar sb ls`; the MCP version line |
| `isolation` | sandboxes can't reach each other, the metadata server or hangar | a NetworkPolicy not applied | `hangar doctor`; [security.md](security.md) |
| `session` | checkpoint, suspend and resume restore the work | the session store (bucket) unreachable | `hangar session get <name>` (its `reason`); `hangar doctor` |
| `e2b`, `backup`, `metrics` | the E2B API, backups, `/metrics` and its scope | `e2b` extra missing (it skips); the backup bucket; `HANGAR_METRICS_ENABLED` | the check's line; `hangar admin backups` |
| `computer` | Chromium runs (under gVisor on GKE), a page screenshot is a whole PNG of the display's size and not blank, the display is captured, 3 s of video record, the screen opens through a one-time ticket, a view-only viewer's pointer and keys never arrive and a control viewer's do | the image build failed (no Chromium), gVisor missing on the pool (`runtime runc`), `/dev/shm` or the display didn't start, the websocket blocked by the load balancer | `hangar env list` (`accept-computer`); `hangar-agent computer status` and `env doctor` in a `--keep` sandbox; `GET /api/audit` for `screen.*` |
| `egress` | an allowlist sandbox reaches the allowed host through the proxy, gets 403 for another, has no direct route and no DNS; with `--with-model`, the agent's `env request egress` reaches its owner as pending | `HANGAR_EGRESS_URL` unset (no proxy in the sandbox); the `sandbox-egress-proxied` NetworkPolicy missing (direct route); a resolver left in the pod (#97: `dnsPolicy: None`); the proxy refusing the allowed host (its address resolves privately) | `GET /api/egress/sandboxes/<id>` (mode, `enforced`, warning); `GET /api/audit` for `egress.deny`; `hangar requests ls` |
| `services` | a Postgres sidecar answers at `postgres:5432` | the image refused by `HANGAR_ENV_SERVICE_IMAGES`, over the services budget, or not pulled | the sandbox's `info.setup_failures` (`hangar sb ls --json`); `hangar-agent svc status` in it |
| `envbuild` | the fixture's config builds; a failing install leaves the good build active; a new sandbox starts from it with the dev server up; the preview link opens once | the build sandbox can't clone (a private fixture without credentials), install's network blocked by egress, `HANGAR_ENV_BUILDS=false`; a preview domain without DNS or certificate | `hangar env builds you/accept-fixture`, `hangar env build-show <id>` (the install's log) |
| `secrets` | a job whose change holds a synthetic GitHub token isn't pushed: `secret_found`, its events name the file and line, never the value | `HANGAR_SECRET_SCAN` set to `warn` or `off`; the scan couldn't read the change | `hangar job events <id>`; `GET /api/audit` for `secret.found` |
| `landing` | the job's PR comes from the GitHub App; the approver's approval arms auto-merge for that commit, a push disarms it | the App not installed on the test repo (the PR comes from the control token: the job's events say so); the webhook lacks `pull_request` or review events (`hangar doctor --fix`); a required check never ran; "merged at once" means nothing kept the PR open (add the code-owner rule above) | `hangar job get <id>` (`landing.note`); `hangar doctor` (`landing …` checks); `hangar inbox` |
| `intake` | a signed synthetic Jira delivery makes a job for the person its account is linked to; a bad signature gets 401 and a replay is dropped | the webhook secret differs from `webhook_secret_credential`'s value; the project isn't mapped, or its repo isn't in `HANGAR_GITHUB_REPOS` | `hangar doctor` (Jira checks); `hangar job get <id>` |
| `scale-smoke` | 20 sessions at once over MCP across 3 test people: the cap refuses one past `HANGAR_PERSON_MAX_SESSIONS`, a member can't read another's session, p95 create, nothing left after; with `--with-model`, nobody runs more than their job cap and nobody waits behind another's batch | nodes not scaling (p95 high: `sandbox_max_nodes`, quotas), a leak in teardown; a skip says the fleet was too full to leave the team room | `hangar power status`; `GET /api/overview`; `hangar sb ls` for `accept-*` |
| `power` | hangar reports the power state; with `--power-cycle`, the pools go down (a create refused at once with 503) and come back up, timed | power.sh couldn't reach the cluster (gcloud, terraform outputs); `HANGAR_POWER_UP_JOB` without `roles/run.invoker` | `hangar power status`; `bash deploy/gke/power.sh status` |
| `prepull` | after power-up, the first create of each prepulled environment pulls no image | an environment changed since the last power-up (its new image isn't on the floor node until the next prepull); the prepull DaemonSet timed out (`prepull.ready: false`) | `hangar power status` (`last_up.prepull`); `GET /api/admin/power/prepull` |
| `warmstart` | a warm pool claim against a cold start | `warm_pool` not set on `--warm-env` | `hangar power status` (`warm_pools`) |
| `status` | `hangar status` answers an admin with power, loops, harnesses, builds, failures, egress denies, requests, SLOs and the `models` line | a hangar from before the console (it skips), an admin token missing the `admin` scope | `hangar status`; its log lines at the end |
| `clouddoctor` | a Cloud Doctor dry run answers, and says what each new kind of failure would lead to | a hangar from before the doctor (it skips); `blocked` lines name what a real run would miss (a team credential, `HANGAR_DOCTOR_MAX_JOBS`) | `hangar admin doctor run --dry-run`; `hangar admin doctor ls`; [doctor.md](doctor.md) |

A check that **crashed** (`the check crashed (exit N)`) is a bug in the suite or a Python missing its packages: run
it again with `HANGAR_ACCEPT_PYTHON` pointing at the repo's environment (`uv run python`).

If the **cleanup** was incomplete, the run says so, keeps its state file and prints the command that retries it.
Leftovers are all named `accept-<run>`; test people are `accept-<run>-p1` and the like (`hangar person ls`).

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
