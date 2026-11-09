# The first run: fixtures and the live run

This page is part of [Acceptance: proving a deploy on the cluster](../acceptance.md).

## Before the first run: fixtures

Each check that needs something skips, saying what, until it has it.

| For | Set up once | Tell the suite |
| --- | --- | --- |
| `secrets`, `landing` | A **test repo** (private is fine, nobody's real work in it) in `HANGAR_GITHUB_REPOS`, with the GitHub App installed and `HANGAR_GITHUB_APP_CREDENTIAL` set ([landing.md](../landing.md)), and **"Allow auto-merge"** on (Settings → General: GitHub refuses to arm without it). The App's webhook (or hangar's) must deliver pull request and review events; `hangar doctor` says when it doesn't | `--test-repo OWNER/REPO` or `HANGAR_ACCEPT_TEST_REPO` |
| `landing`'s approval | A **second GitHub account** added as a collaborator with write access to the test repo, and a **classic** token of its with the `repo` scope (GitHub's fine-grained tokens don't work on a repo where that account is only a collaborator). On the test repo's default branch, branch protection with one required approval, stale-approval dismissal, and **"Require review from Code Owners"** with a `CODEOWNERS` that names you, not that account: GitHub then waits for you after hangar arms auto-merge, so the check can see it armed and then disarmed by a push. Don't make a check required unless the test repo runs it: hangar arms only once required checks are green | `HANGAR_ACCEPT_APPROVER_TOKEN` |
| pushes to the test and fixture repos, closing PRs | A token that writes to both (or `gh auth login`) | `HANGAR_ACCEPT_GITHUB_TOKEN` |
| `envbuild` | A **fixture repo** whose default branch is unprotected (the check commits `.hangar/environment.yaml` and `.gitignore` there and puts them back). Public is simplest; a private one needs a credential hangar clones it with | `--fixture-repo OWNER/REPO` or `HANGAR_ACCEPT_FIXTURE_REPO` |
| `envbuild`'s preview | `HANGAR_PREVIEW_DOMAIN` with wildcard DNS (and a wildcard certificate on GKE) | nothing: it skips the preview part without it |
| `intake` | Jira intake configured (`HANGAR_JIRA`, [intake.md](../intake.md)) with a Jira project mapped to the test repo, such as `"ACC": {"repo": "you/accept-target"}` in its `projects` (a project nobody uses is fine: the issue is synthetic and never created in Jira). The synthetic issue's job is cancelled at once but may start first, so without a project on the test repo the check skips rather than start a job on a real repo; `--jira-project KEY` overrides that | `--test-repo`, `HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET` (the value of the vault credential `webhook_secret_credential` names) |
| `power --power-cycle` | `gcloud` logged in to the project, and `deploy/terraform/gke`'s outputs or `PROJECT`, `ZONE`, `CLUSTER` for `deploy/gke/power.sh`. With `HANGAR_POWER_UP_JOB` set, powering up goes through hangar instead | `--power-cycle`; `--power-down-cmd`, `--power-up-cmd` to override |
| `model`, `steer`, `secrets`, `landing`, egress's request, scale's job queue | A model credential in `--session-env` that reaches work no person owns (a team API key), or `--as PERSON` | `--with-model`; `--max-usd` caps each job or turn; `--as PERSON` |

In `deploy.yml`, the repos are variables and the tokens secrets of the `production` environment:
`HANGAR_ACCEPT_TEST_REPO`, `HANGAR_ACCEPT_FIXTURE_REPO` (variables), `HANGAR_ACCEPT_GITHUB_TOKEN`,
`HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET` (secrets). It never passes `--with-model` or `--power-cycle`. Leave them unset
until the checks pass by hand: each check without its fixture skips.

## The first live run after deploying Waves 1–3

Run these in order after [the upgrade](../releases/team-release.md) (its step 14). Each step
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
([harnesses.md](../harnesses/canary.md)) run in GitHub Actions only (`harness-contract.yml`, `harness-canary.yml`,
`harness-canary-land.yml`), never against the cluster: they need no sandboxes and run while the pools are off. Check
them in the repository's Actions tab: `contract` green on the deploy's commit, and the last nightly canary run green
or its `harness-canary` issue read. Their owner setup is in [releases/team-release-sandboxes.md](../releases/team-release-sandboxes.md#11b-the-harness-canary).
