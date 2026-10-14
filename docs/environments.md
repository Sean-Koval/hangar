# Environments that live with the repo

An agent is only as good as the computer it gets. When the dependencies aren't installed, the database isn't up or
the dev server died an hour ago, the output just gets weaker, and people blame the model. Hangar's environment specs
(`envs/*.yaml`, [spec 0006](specs/0006-environments-and-harness-packs.md)) describe the machine: the image, the
harnesses, the credentials. This page covers what the **repo** says about itself, in a file committed next to its
code:

- **`.hangar/environment.yaml`**: how to install the repo's dependencies, what to start, which long-running processes
  to keep up, and which check proves a change works.
- **Versioned builds**: hangar runs `install` ahead of time, in the background, and keeps the result as an image.
  Sandboxes for the repo start from it, with everything installed.
- **The supervisor** (`hangar-agent svc`): one command for the agent to see, start, restart and read the logs of the
  environment's processes. A process that exits is restarted, so the agent doesn't have to look after it.
- **Services** (Postgres, Redis...) declared as images, run beside the sandbox and reachable at their name.
- **Preview ports**: the sandbox's owner opens a declared port in their browser, through hangar, never publicly.

## The file

```yaml
# .hangar/environment.yaml
extends: python                  # a hangar environment the repo's HANGAR_GITHUB_REPOS entry allows
install: uv sync --frozen        # idempotent; runs on every build, in the repo's root, never when a sandbox starts
start: ./scripts/seed-db.sh      # once each time a sandbox starts, before the terminals
terminals:                       # long-running processes the supervisor keeps up
  - name: web
    command: npm run dev -- --host 0.0.0.0
    cwd: frontend                # relative to the repo
    port: 5173                   # ready once something listens here...
  - name: worker
    command: uv run worker
    ready: test -f /tmp/worker.ready   # ...or once this exits 0
    autostart: false             # declared, started when the agent asks: hangar-agent svc start worker
    restart: on-failure          # always (default) | on-failure | never
services:                        # images beside the sandbox, reachable at their name (postgres:5432)
  postgres:
    image: postgres:17
    port: 5432
    env: {POSTGRES_PASSWORD: dev, PGDATA: /var/lib/postgresql/data/pgdata}
    volumes: [/var/lib/postgresql/data]   # empty, writable directories
ports:                           # the owner can open these in a browser, through hangar
  - {port: 5173, name: web}
verify: uv run pytest -q         # the check jobs run, unless the job names one
build_secrets: [NPM_TOKEN]       # ids only; an admin maps each to a credential (see Build secrets)
lockfiles: [vendor/*.lock]       # more files whose change means a rebuild
mocks:                           # stand-ins for real dependencies, so nobody mistakes them (agent-led setup)
  - {what: S3, how: "MinIO service at minio:9000", why: no cloud credentials in sandboxes}
```

Every field is optional. `mocks` is documentation: hangar applies nothing from it, but agent-led setup (below)
requires every stand-in to be listed there and in its PR. Each field the file sets replaces the environment's own: an environment spec takes the same
`install`, `start`, `terminals`, `services`, `ports` and `verify` fields, so a team can share defaults there and a repo can override them.
`hangar env check` validates a file against your hangar without saving or building anything:

```console
$ hangar env check .hangar/environment.yaml --repo acme/app
.hangar/environment.yaml: valid (extends python)
```

The same check is `POST /api/envbuilds/validate {"content": ..., "repo": ...}`. In code, `hangar.repoenv` has
`parse` (validate), `dump` (write) and `check` (validate against a hangar); agent-led setup builds on these.

### What the repo can't set

The file is untrusted: anyone who can push to the repo writes it. It is parsed strictly: unknown fields are refused,
YAML aliases are refused, and it must be under 64 KiB. It can set only the fields above. Everything an admin decides
stays in hangar: the image and base (`image`, `base`), secrets and credentials (`secrets`, `credentials`), MCP
servers, `runtime_class`, resources, `sudo`, and the git credential mode. A file that names one of these is refused,
and the message says it's decided in hangar. Secrets stay in hangar, by name.

Hangar reads the file from the commit (`git cat-file`), never from the working tree. A symlink committed in its
place is read as the link's text, so it can't make hangar read another file in the sandbox.

### Resolution order

Per field: **what the job asked for, then the repo's file, then the repo's policy** ([repo-policy.md](repo-policy.md)),
**then the environment spec**. Before a job's sandbox exists, hangar uses the file as the default branch had it at
the last clone. So a repo's `extends` and `verify` apply to every front door (GitHub issues, Slack, schedules, MCP)
from the second job on. `extends` picks the environment only when the repo's `HANGAR_GITHUB_REPOS` entry allows
that environment; a repo with no entry doesn't pick its environment (an environment decides credentials, secrets
and egress, so an admin says which ones a repo may name). Hangar uses the file only as a sandbox of the job's own
environment, or of one the allowlist names, last saw it: never as another environment's sandbox recorded it (its
`setup` and `install` run before the read, and members make environments).

## Builds

A repo gets builds when it has an `install`, in its file or in its environment. A build is:

```
the environment's image  →  clone the default branch  →  run install  →  keep the checkout as an image
```

- **Where it runs.** `install` is code from whoever can push, so it runs where agents run: in an ordinary sandbox of
  the environment (gVisor on GKE, hangar's isolated network on Docker), under the same network rules as its agents.
  It runs without the agent's model credentials. It never runs on the control plane or on an image builder.
- **What is kept.** Only the repo's checkout (`node_modules`, `.venv`, generated code and the rest), archived and
  `ADD`ed onto the environment's image with no `RUN` step. Nothing in `$HOME` is kept: the sandbox's injected env and
  credentials can't end up in a build. Put what `install` produces inside the repo; system tools belong in the
  environment spec. The checkout is capped at `HANGAR_SNAPSHOT_MAX_MB` (512).
- **Keys.** A build is keyed by the repo, the environment, the provider, a digest of the file's build fields
  (`install`, `build_secrets`, `lockfiles`) and the lockfiles' git blob ids, and the environment's image. A comment, a
  new terminal or a changed `verify` doesn't rebuild. Common lockfiles are watched anywhere in the repo:
  `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock(b)`, `uv.lock`, `poetry.lock`, `Pipfile.lock`,
  `requirements*.txt`, `Cargo.lock`, `go.sum`, `Gemfile.lock`, `composer.lock`, `.tool-versions`, `mise.toml` and more
  (`repoenv.LOCKFILES`). Add your own with `lockfiles:`.
- **When.** Never while anyone waits. After every clone, one exec reads the file and the lockfiles' ids. When the
  default branch's config, its lockfiles or the environment's image moved on from the active build, a rebuild starts
  in the background, for the next sandbox. Startup's prebuild ([#83](gke.md#3-environment-images)) rebuilds the builds
  an environment change made stale. A build that failed isn't retried until something changes (the file, a lockfile,
  the image) or someone runs `hangar env build-repo`. At most `HANGAR_ENV_BUILDS_CONCURRENCY` (2) builds run at once,
  one per repo, each within `HANGAR_ENV_BUILD_TIMEOUT_MINUTES` (30).
- **A failed build never replaces the active one.** New sandboxes keep starting from the last good build. With none,
  they start from the environment's own image, and the agent is told so, in plain words, at the top of its
  instructions, with the install command to run itself. The job's events say so too.
- **The checkout is brought up to date.** A sandbox started from a build fetches and checks out the branch it asked
  for. Untracked files left by `install` (not ignored ones like `node_modules`) are removed, so the agent's diff is its
  own. When the branch's lockfiles differ from the build's, the agent is told to run the install.
- **History and rollback.** Each repo keeps its last `HANGAR_ENV_BUILDS_KEEP` (5) builds per environment and provider:

```console
$ hangar env builds acme/app
acme/app / python on kubernetes
*   v7  bld-1a2b3c4d  succeeded   3f2c1a9e0  2026-10-14 09:12  drift   us-docker.pkg.dev/.../python-repo@sha256:...
    v8  bld-5e6f7a8b  failed      91aa04c2d  2026-10-14 11:40  drift   install exited with 1
$ hangar env build-show bld-5e6f7a8b         # its config and the end of the install's output
$ hangar env build-repo acme/app --wait      # build now
$ hangar env rollback bld-0c0ffee0           # admin: make it active, pinned
$ hangar env unpin acme/app                  # admin: let new builds take over again
```

  A rollback pins the build it picks: drift rebuilds still run and are kept, but don't replace it until an admin
  unpins. Rollbacks and unpins are audited (`envbuild.rollback`, `envbuild.unpin`), as are builds
  (`envbuild.start`, `envbuild.finish`).

### What the agent and you see

Every sandbox started for a repo with a config carries `env_build` in `get_sandbox`, `list_sandboxes` and the REST
sandbox views:

| Field | Meaning |
| --- | --- |
| `version`, `id` | The build it started from (`v7`, `bld-1a2b3c4d`); null on the environment's own image |
| `fallback` | true: no build in use; `reason` says why (none yet, the latest failed, the image is gone, ...) |
| `stale` | true: the repo or the environment moved on since this build; a rebuild is running or `reason` says why not |
| `commit`, `built_at`, `config_digest`, `image` | What the build is |

The agent gets the same in a "Your environment (hangar)" section of its harness's global instructions file
(`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, ...), and a `hangar-environment` skill that documents the supervisor.
Problems hangar ran into while setting up (an invalid file, a supervisor that didn't start) are in
`sb.info["setup_failures"]` and in that section, never as a failed sandbox.

### Build secrets

A private package registry needs a token while `install` runs, and only then. The repo names the ids it needs
(`build_secrets: [NPM_TOKEN]`). An admin maps each id to a hangar credential, in the environment's or the repo
policy's `build_secrets` (in the same release, with hangar.buildsecrets). The values reach the install command's
environment only, through a file that is read and removed before `install` starts, never a command line. The output
is redacted. The checkout is scanned for the values before it is kept, in the sandbox and again by hangar in the
archive it captured (what the image gets, whatever install left running): a build whose install wrote a token into
the repo (a repo-local `.npmrc`) fails and says where. An id nobody mapped fails the build with its name. The running
sandbox never sees any of them. The scan catches a value written as it is, not one install encodes on purpose:
`install` is the repo's code, so map a build secret only for a repo whose default branch you'd trust with it.

### Limits of this version

- Docker and Kubernetes run builds. On E2B a repo's sandboxes start from the environment's image, with the warning.
- A sandbox started from a build starts cold: the environment's warm pool holds its own image. On Kubernetes each
  build has its own SandboxTemplate, with no warm pool. The reaper deletes it once the build is out of the history
  and no live sandbox runs on it (a session started from it before a rollback keeps it), as it does a set of
  services' template that nothing uses any more (`k8sgc.py`).
- A build's `.git` keeps only what hangar and git need: its remotes, branches and core settings. Hooks (but their
  samples) and the rest of its config, such as `core.hooksPath`, `core.fsmonitor`, filters, aliases and includes, are
  dropped before the checkout is captured. Otherwise every sandbox from the build would run what `install` set up,
  hangar's own checkout first, long after the commit that set it up was gone. A repo that wants git hooks in every
  sandbox sets them up in `start` (husky: `git config core.hooksPath .husky`).
- On providers that bake harnesses into images (no image volumes), builds are made for the environment's default
  harness. A sandbox for another harness starts from that harness's image variant, with the warning.
- Builds are made from the default branch, as Cursor's are. A feature branch's new dependencies are the agent's to
  install, and it is told so.

## Services

Docker inside a gVisor sandbox is limited, so a service never runs inside the sandbox. It runs next to it, with the
same isolation, and goes away with it. The sandbox reaches it at its name: `postgres:5432` above.

- **Kubernetes:** a sidecar container in the sandbox's pod. It shares the pod's network (127.0.0.1, and its name
  through the pod's `hostAliases`), its runtime class, and its hardening: uid 1000, no capabilities, no privilege
  escalation, which GKE Sandbox's admission policy requires of every container. Its `cpus` and `memory_gb` (0.5 each
  by default) count toward the pod, memory requested in full. It takes the sandbox's own network policy: it reaches
  what the sandbox reaches (the internet, not other pods), and nothing reaches it from outside the pod.
- **Docker:** a container on a network of the sandbox's own (`hangar-svc-<sandbox>`), with the sandbox attached to it
  before it starts (gVisor can't take a network added later) and an `/etc/hosts` entry per service. hangar's own
  network keeps sandboxes from reaching each other, so it can't carry this. The services' network is internal: no route
  out, no metadata server, and nobody else's sandbox or services on it. The services run under the same runtime, as uid
  1000, without capabilities, with `no-new-privileges`. They are removed with the sandbox; the reaper removes any whose
  sandbox went away some other way.

Services run as uid 1000, so give a database a writable data directory: `volumes` lists empty directories (an
`emptyDir` on Kubernetes, a tmpfs on Docker). The official `postgres` image runs as any uid when `PGDATA` is a
directory it can create, as in the example. `redis` and most others need nothing.

Images must match `HANGAR_ENV_SERVICE_IMAGES`: by default Docker's official images (`postgres:17`, `redis`), directly
or through `mirror.gcr.io/library/`, which GKE nodes pull without Docker Hub's rate limits. An admin adds a registry
with a pattern such as `us-docker.pkg.dev/acme/services/**` (`*` stays within one path segment, `**` crosses them).
Other images are left out, with a setup failure that names them. Hangar's own images (under `HANGAR_REGISTRY` or
`HANGAR_IMAGE_PREFIX`: environments, and repo builds that hold their repo's checkout) are never a service, whatever
the patterns say.

The repo writes each service's `cpus` and `memory_gb`, so together a sandbox's services must fit
`HANGAR_ENV_SERVICES_MAX_CPUS` and `HANGAR_ENV_SERVICES_MAX_MEMORY_GB` (2 and 2 GiB); the ones past that are left
out, with a setup failure. That budget is what a node's packing must allow for on top of the sandbox: using all of
it, a 2 vCPU / 4 GiB sandbox requests 2 vCPU and 6 GiB (`HANGAR_K8S_CPU_REQUEST_FRACTION=0.5`), so a node of
[team-scale.md](team-scale.md) holds 7 such sandboxes (CPU-bound), not 14. Most repos declare none or one or two.

The services are decided before the sandbox starts: those of the build it starts from, else the repo's file as its
default branch was last seen, else the environment's. A service added in a commit starts with the next sandbox, and
the agent is told so. A sandbox with services of its own starts cold: on Kubernetes it gets a SandboxTemplate per set of
services (or per build), and on Docker a warm container can't join a network. Services that the environment itself
declares are part of its own template and warm pool. The supervisor waits for the services to answer before it runs
`start`, and `hangar-agent svc status` shows them. Forks don't get services yet. E2B doesn't run them, and its
sandboxes get a setup failure saying so.

## Preview ports

A declared port (`ports:`) can be opened by the sandbox's owner in a browser. Nothing is public, and it never runs on
hangar's own origin:

```console
$ hangar sb preview my-sandbox
 5173  web        https://5173-sb-1a2b3c4d.hangar-preview.example.net/__hangar/preview?t=...
```

- **One origin per port per sandbox:** `<port>-<sandbox>.<HANGAR_PREVIEW_DOMAIN>`. What a dev server serves (the
  agent's HTML and scripts) can't reach the dashboard's token or another sandbox's preview.
- **Owner-scoped.** `GET /api/sandboxes/{ref}/previews` (the owner's `user` token, or an admin's) gives a link per
  port that works for two minutes, once. Opening it signs that browser in to that origin only: an HttpOnly cookie (`__Host-`
  on https), signed and bound to the sandbox, the port and the person, valid `HANGAR_PREVIEW_SESSION_HOURS` (12).
  Every request checks it again against the sandbox's owner as it is then. `curl -H "Authorization: Bearer $TOKEN"`
  works too, with a `user` or admin token.
- **Deny by default.** Only declared ports, never hangar-envd's, only while the sandbox runs. Links and refusals are
  audited (`preview.link`, `preview.denied`, `preview.port.denied`).
- **Nothing of hangar's reaches the app:** the proxy drops the `Authorization` header and its own cookie. It reaches
  the port at the address the sandbox's hangar-envd proved, never loopback, link-local or another sandbox's
  (the same proxy as [E2B's sandbox URLs](e2b.md)). HTTP and WebSockets both work.

Set it up with wildcard DNS for the domain pointing at hangar (on GKE, a wildcard certificate for `*.<domain>` on
hangar's ingress too: a preview's host is one label under the domain), then for example
`HANGAR_PREVIEW_DOMAIN=hangar-preview.example.net`. On a laptop, `HANGAR_PREVIEW_DOMAIN=localhost` is enough: browsers
resolve `*.localhost` to 127.0.0.1. Dev servers must listen on 0.0.0.0, and the agent is told so. Previews need
hangar-envd (Docker and Kubernetes sandboxes); on Kubernetes, also the `allow-hangar-sandbox-ports` NetworkPolicy
(`deploy/k8s/e2b.yaml`), since sandbox pods take no other ingress.

**Give previews a registrable domain of their own**, not a subdomain of your company's or of hangar's host. The
proxy drops hangar's credentials, but it can't tell the app's own cookies from others the browser sends: under
`preview.example.com`, cookies your SSO or intranet set for `example.com` would reach the agent's app. A domain of its
own (as `githubusercontent.com` is GitHub's) also keeps the agent's pages from being same-site with your other apps.

## The supervisor: `hangar-agent svc`

```console
$ hangar-agent svc status
NAME    KIND      STATE    READY  PID   RESTARTS  UP   PORT  LOG
web     terminal  running  yes    4121  0         12m  5173  ~/.hangar/svc/logs/web.log
worker  terminal  stopped  no     -     0         -    -     ~/.hangar/svc/logs/worker.log
start   start     done     -      -     0         -    -     ~/.hangar/svc/logs/start.log
$ hangar-agent svc wait web --timeout 120     # exit 1 says which failed or isn't ready
$ hangar-agent svc logs web -n 200 | rg -i error
$ hangar-agent svc restart web                # also: start, stop, --all
$ hangar-agent svc add docs --port 8000 -- python3 -m http.server 8000   # supervise a process of your own
```

When a sandbox starts, hangar writes `~/.hangar/svc/manifest.json` from the effective config and runs
`hangar-svc up`, which returns at once. The supervisor then:

1. waits for the environment's services to answer (up to 180 s);
2. runs `start` once, in the repo;
3. starts every terminal with `autostart` (default true), each in its own process group, through `bash -l` (so
   `~/.hangar/env` applies);
4. checks readiness: `port` (something listens on it) or `ready` (the command exits 0), every second until ready,
   then every 15 s;
5. restarts a terminal that exits, with backoff from 0.5 s to 60 s. One that exits 10 times in a row within 30 s of
   starting is `failed` until someone starts it again. `restart: on-failure` leaves a clean exit alone; `never` runs
   it once.

Output goes to `~/.hangar/svc/logs/<name>.log` (rotated at 10 MB to `.log.1`): files the agent reads and searches with
the tools it already has, rather than output hangar fetches for it. `hangar-agent svc status --json` is the same view
for tools. The supervisor's own log is `~/.hangar/svc/supervisor.log`.

**Why a script and not hangar-envd.** The supervisor runs the agent's own processes, as the agent's user, and the
agent drives it. hangar-envd is hangar's authenticated transport into a sandbox. It isn't on every provider (E2B
brings its own envd), and it's replaced on upgrades, which would take every dev server down with it. Process control
the agent drives would also widen what talks to it. The supervisor is a standard-library python3 script (python3 is
in the [image contract](specs/0006-environments-and-harness-packs.md)), installed in each sandbox at claim like
`hangar-agent`, with no image rebuild. It holds nothing secret. Its socket and state live in `~/.hangar/svc` (0700).
Under the [uid split](steering.md#the-uid-split) it stays the agent's: hangar's own user never runs it, and it never
reaches hangar's runs.

## Agent-led setup

A repo nobody has configured yet gets its `.hangar/environment.yaml` from agents, validated by hangar, as a PR.
This follows Cursor's [autoinstall](https://cursor.com/blog/bootstrapping-composer-with-autoinstall) and its
[agent-driven setup](https://cursor.com/docs/cloud-agents/setup).

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

It is a job (`hangar.kind: env-setup` in its labels), so it also starts from `POST /api/env/setup {repo,
environment?, harness?, model?, notes?, mode?, job_budget?}`, from `POST /api/jobs` with that label, from a
schedule's or an inbound webhook's job template, and from an issue carrying the `HANGAR_ENV_SETUP_LABEL` label
(`hangar-env-setup`): a GitHub issue given it, a Jira issue that reaches the trigger status (or label) with it, a
GitHub Projects item whose issue has it. The job's events, its result, its tracker write-back and the inbox work
like any job's.

1. **Goal.** An agent reads the repo at the commit its sandbox cloned: README, Makefile, manifests and lockfiles,
   `.github/workflows` (the best evidence of how the repo really builds and tests), devcontainer and compose files.
   It proposes about ten target commands (install, build, lint, unit tests, start the app, a smoke request), each
   with the expected outcome in words and marked core or not, plus the services and secrets the repo needs. It is a
   job of its own (`deliver: none`) with a structured result.
2. **Setup.** A fresh agent, in another job (`deliver: branch`), makes at least the core commands work. It writes
   the file (services from the allowlisted images, such as `postgres`, `redis` or a MinIO image an admin allows,
   standing in for real dependencies; long-running processes as terminals under `hangar-agent svc`; `verify` set to
   the unit-test command), records every stand-in under `mocks:`, writes a "Development environment" section in
   `AGENTS.md`, and commits. A real secret it lacks is filed with `hangar-agent env request secret NAME`, never
   invented; it reports which commands that blocks.
3. **Validation, by hangar.** The file is checked like `POST /api/envbuilds/validate`, and must set `verify` and
   list every mock the agent reported. Then a clean sandbox of the setup branch (no repo build; the proposed
   services beside it; the environment the file's `extends` picks when the repo's allowlist permits it) runs the
   file's `install` through the build path's install step, starts the supervisor and waits for its services and
   terminals, and runs the core commands, the ones the agent says work, and `verify`. Exit codes decide first. Only
   for commands the goal agent marked `check_output` does it judge the output against its own description, in a
   follow-up turn of the goal job. A failure goes back to the setup agent with the output, as files it can read, up to
   `HANGAR_ENV_SETUP_RETRIES` (5) times. After that the job fails, and its summary says exactly what is missing:
   each failing command and why, file errors, and the secrets nobody gave.
4. **The PR.** The setup job's branch becomes a PR through [the landing path](landing.md): the App identity, the
   review packet, and an approval that lands it. The PR body has the validated commands as a table, what is mocked,
   the secrets still needed, and `verify`. The packet adds an "Environment setup" line. A setup that passes except
   for commands blocked by secrets the agent requested opens as a draft (`reason: secrets_needed`). Merged, the file
   gets builds like any other (above).

**Re-running on drift.** `mode: drift` (`--drift`; the schedules `--every` creates) runs setup only when the
repo's environment is broken: the default branch has no config or an invalid one, or the repo's latest environment
build failed (after a lockfile or CI change). It doesn't run while an earlier env-setup PR for the repo is still
open. Otherwise it ends at once (`no_changes`), with no agent started. The agents get the failing build's output
and update the existing file.

**Models and budget.** Both agents run on the strongest current model of their harness's provider, picked from
the provider's own model list (`hangar.modelpick`): Anthropic's, OpenAI's or Google's, read with the team's API
key for the harness, or OpenRouter's public list without one. Only general models of the newest generation count,
ranked by list price (`HANGAR_MODEL_PRICES` and the built-in table; a release not priced yet takes its line's
price). A job's own `model` wins, and `HANGAR_ENV_SETUP_MODELS` pins one per harness. When no list can be read,
the harness's own default applies, and the job's events say so. The job budget is the job's, else the repo
policy's, else `HANGAR_ENV_SETUP_MAX_COST_USD` and `HANGAR_ENV_SETUP_MAX_TURNS`. The goal job gets a quarter of it
(the proposal and the judging), and the setup job gets the rest (every attempt), so together they can't exceed it.

**Safety.** The validation sandbox runs code an agent wrote on an unmerged branch, so it gets less than a build:
no agent or person credentials, and no build secrets. A repo whose install needs a private registry token fails
validation and names the token, and an admin maps it as a build secret for the merged file. The env-setup job
holds no job slot while it waits: a background task drives its stages and keeps its state in the job's
`result.data`, so a restart resumes it. A follow-up to it answers whichever of its agents asked a question.
Cancelling it cancels both agents and removes the validation sandbox.

## Settings

| Setting | Default | |
| --- | --- | --- |
| `HANGAR_ENV_BUILDS` | `true` | Off: repo files are ignored and sandboxes start from the environment's image, as before |
| `HANGAR_ENV_BUILDS_KEEP` | 5 | Builds kept per repo, environment and provider (the active one is always kept) |
| `HANGAR_ENV_BUILD_TIMEOUT_MINUTES` | 30 | Clone, install and capture, per build |
| `HANGAR_ENV_BUILDS_CONCURRENCY` | 2 | Builds at once |
| `HANGAR_ENV_SERVICE_IMAGES` | `*`, `docker.io/library/*`, `mirror.gcr.io/library/*` | Images services may use |
| `HANGAR_ENV_SERVICES_MAX_CPUS`, `HANGAR_ENV_SERVICES_MAX_MEMORY_GB` | 2, 2 | What a sandbox's services may add up to |
| `HANGAR_PREVIEW_DOMAIN` | unset (off) | `<port>-<sandbox>.<domain>` serves preview ports (a registrable domain of its own) |
| `HANGAR_PREVIEW_SESSION_HOURS` | 12 | How long an opened preview stays signed in |
| `HANGAR_ENV_SETUP_RETRIES` | 5 | Setup turns after a failed validation, each with the failure's output |
| `HANGAR_ENV_SETUP_MAX_COST_USD`, `HANGAR_ENV_SETUP_MAX_TURNS` | 30, 16 | An env setup's job budget when neither the job nor the repo's policy sets one |
| `HANGAR_ENV_SETUP_VALIDATE_MINUTES` | 45 | One validation: the sandbox, install, the services and every command |
| `HANGAR_ENV_SETUP_MODELS` | unset | `{"claude-code": "...", "codex": "..."}`: pin the agents' model per harness instead of the pick |
| `HANGAR_ENV_SETUP_MODEL_CACHE_HOURS` | 6 | How long providers' model lists are cached |
| `HANGAR_ENV_SETUP_LABEL` | `hangar-env-setup` | An issue (GitHub, Jira, GitHub Projects) with this label starts an env setup |

## REST

| Route | Who | |
| --- | --- | --- |
| `GET /api/envbuilds` | member | Every repo with builds: its active and latest build |
| `GET /api/envbuilds?repo=&environment=&provider=` | member | One repo's history (newest first) |
| `GET /api/envbuilds/{id}` | member | One build, with its config and the end of the install's output |
| `POST /api/envbuilds {repo, environment?, provider?}` | member | Build now, in the background |
| `POST /api/envbuilds/{id}/rollback` | admin | Make it active, pinned |
| `POST /api/envbuilds/unpin {repo, environment?, provider?}` | admin | Let new builds replace the active one again |
| `POST /api/envbuilds/validate {content, repo?, environment?}` | member | Check a file; nothing is saved |
| `POST /api/env/setup {repo, environment?, harness?, model?, notes?, mode?, job_budget?}` | admin, `jobs` token | Agent-led setup: an env-setup job (as `POST /api/jobs` returns it) |
| `GET /api/sandboxes/{ref}/previews` | owner, admin | Two-minute links to its preview ports |

The environment, when not named, is the one a job for the repo would get.
