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
- **The heavy tier**: Docker inside a VM of the sandbox's own, Compose stacks under the supervisor, and bigger
  resource profiles, for repos that test with `docker compose` or Testcontainers ([below](#the-heavy-tier)).

## How `extends` merges

An environment spec can `extends` another (the shipped `envs/*.yaml`, a saved one, or `hangar env` /
`create_environment` cloning one). The child is laid over the parent field by field:

| Field kind | Rule | Fields |
| --- | --- | --- |
| Scalars | the child's value replaces | `image`, `model`, `verify`, `install`, `resources.cpus`, ... |
| Maps | merge by key; the child's key wins; `KEY: null` deletes the parent's key | `mcp`, `secrets`, `env`, `files`, `agent_config.files`, `services`, `tools.mise`, `build_secrets` |
| Component lists | the child's list **replaces** the parent's (`[]` empties it) | `skills`, `harnesses`, `credentials`, `terminals`, `ports`, and every list not named below |
| Additive build inputs | **union** with the parent's, in order, without duplicates | `tools.apt`, `dependencies.pip`, `dependencies.npm`; and `egress.allow` (hosts add up along a chain, as before) |

Installing one more package never changes what the agent is told, so build inputs add up. What shapes the agent's
behaviour (skills, harnesses, credentials) is the child's to state whole. To drop one inherited skill without
restating the rest, name it in `skills_remove`:

```yaml
name: python-no-conventions
extends: claude-python
skills_remove: [repo-conventions]   # by the skill's name (its `name`, else its directory's)
```

`skills_remove` applies once, to the merged list, and is not inherited. Naming a skill the environment doesn't have
is an error that lists the names it has. The table is `envspec.LIST_MERGE`. A repo's `.hangar/environment.yaml`
follows it too: each field it sets replaces the environment's (every list it may set replaces under `extends` as
well). An experiment variant's `skills` and `skills_remove` have the same meaning ([jobs.md](jobs.md#9-experiments)).
Before #132 every list except `harnesses`, `credentials`, `terminals` and `ports` was appended. `hangar env
migrate-merge --dry-run` lists which environments resolve differently ([upgrading.md](upgrading.md#the-extends-merge-rules-132)).

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
verify_reports: [reports/*.xml]  # JUnit, Cobertura or LCOV files it writes (most runners need none: docs/jobs.md §8)
build_secrets: [NPM_TOKEN]       # ids only; an admin maps each to a credential (see Build secrets)
lockfiles: [vendor/*.lock]       # more files whose change means a rebuild
mocks:                           # stand-ins for real dependencies, so nobody mistakes them (agent-led setup)
  - {what: S3, how: "MinIO service at minio:9000", why: no cloud credentials in sandboxes}
heavy: true                      # Docker inside, on the heavy tier, where an admin allows it (see The heavy tier)
profile: large                   # standard | large | xlarge, up to what the repo's policy allows
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
and the message says it's decided in hangar. Secrets stay in hangar, by name. `heavy`, `tier: heavy` and `profile`
are requests that the repo's policy bounds ([the heavy tier](#what-decides)); Docker is off unless an admin allows it
for the repo (`hangar repo set owner/repo --heavy allow`), and no other tier can be asked for.

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
  It never runs on the control plane or on an image builder.
- **What the build sandbox holds:** a fetch-only grant of the [git gateway](security.md#git-credential-gateway) for
  its clone, the [build secrets](security.md#build-secrets) an admin mapped (in `install`'s environment only), and
  egress per the environment and repo policy. Nothing else: no credential of the environment (no model or person
  login, no `git_token`, no tool key), none of its `secrets:`, no cloud tokens. Without the gateway
  (`HANGAR_GATEWAY_URL`) it holds no git credential at all, so a private repo's build fails and says so.
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

### Build caches

A build keeps the checkout, not `$HOME`. The package managers' caches, a branch whose lockfile moved on, and compile
output are covered by [build caches](caches.md). A build restores them before `install` and saves them after it.
Builds are the only writers of a repo's shared caches. Every sandbox restores them in the background after the clone,
and sccache, Bazel and Turborepo come pointed at hangar's remote caches.

## Services

Docker inside a gVisor sandbox is limited, so a service never runs inside the sandbox. It runs next to it, with the
same isolation, and goes away with it. The sandbox reaches it at its name: `postgres:5432` above. (On the heavy tier a
service can also be a Compose stack that runs inside the sandbox: [Compose stacks](#compose-stacks-under-the-supervisor).)

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

1. starts the Compose stacks (heavy tier), and waits for them and the environment's services to be ready (up to
   180 s);
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

## The heavy tier

Most real repos run their integration tests with `docker compose` or Testcontainers, and build with more than the
default 2 CPUs and 4 GiB. A **heavy** sandbox is a VM of its own with Docker running natively inside it, so `docker
build`, `docker compose up` and Testcontainers work as on a laptop, and the containers it starts stay inside that VM.
The heavy tier also holds the bigger **resource profiles**. gVisor stays the default tier for everything else.

```yaml
# envs/python-docker.yaml (an admin's environment)
name: python-docker
extends: python
heavy: true            # or `tier: heavy`: Docker in the image and in the sandbox
profile: large         # standard (2 vCPU / 4 GiB) | large (8 / 32 / 100 GiB disk) | xlarge (16 / 64 / 200)
```

```yaml
# .hangar/environment.yaml (the repo's)
heavy: true            # a request, off unless an admin allows it; see "What decides" below
services:
  deps: {compose: docker-compose.yml}              # a Compose stack the supervisor runs (heavy only)
  db: {compose: ops/compose.test.yml, port: 5432}  # ready once 5432 answers
  cache: docker-compose.cache.yml                  # short for {compose: docker-compose.cache.yml}
```

### Turning it on

The tier is off until an admin lists providers for it, as for `fast` and `isolated`:

```bash
HANGAR_TIERS='{"fast": ["e2b"], "isolated": ["kubernetes"], "heavy": ["kubernetes"]}'
```

A heavy sandbox only ever runs on a provider listed there, in order, falling back past one that is full or down, and
never anywhere else: like `isolated`, it is a security choice (a privileged sandbox needs a VM around it).

| Provider | What a heavy sandbox is | Setup |
| --- | --- | --- |
| `kubernetes` | a Kata pod (`HANGAR_HEAVY_RUNTIME_CLASS`, `kata-qemu-runtime-rs`) on the `sandboxes-heavy` pool | [gke.md](gke.md#4a-the-heavy-tier-optional): the pool, kata-deploy, and the Agent Sandbox controller caveat |
| `e2b` | an E2B or CubeSandbox microVM | [microvm.md](microvm.md#the-heavy-tier-on-microvms) |
| `docker` | a privileged runc container: **root on the host** | `HANGAR_HEAVY_DOCKER_PRIVILEGED=true`, for CI and throwaway machines only |

### What decides

Before a sandbox starts (`heavy.decide`):

- **Docker is deny by default.** It comes from the environment (`heavy: true`, `tier: heavy`; admin fields); else the
  repo's policy (`hangar repo set acme/app --heavy on`); else the repo's `.hangar/environment.yaml` asking for it (as
  its default branch was last seen), which is only a **request**: it is honored when an admin has allowed it for that
  repo (`hangar repo set acme/app --heavy allow`) and this hangar has a heavy tier. Unset, or `--heavy deny`, it isn't:
  anyone who can push the default branch could otherwise give the repo's sandboxes root in a VM on costlier nodes. A
  heavy repo on a plain environment gets that environment's heavy variant: the same spec, its image built with Docker
  in it. The repo's builds then start from that variant too.
- **A request that isn't allowed** runs on the default tier, without Docker, and says so plainly: a warning on the
  sandbox (`get_sandbox`'s `info.warnings`, the job's events), a "Docker isn't enabled for this repo" section in the
  agent's instructions that gives the admin command, and a `WARN` on `hangar-agent env doctor`'s `tier` line:

  ```text
  WARN tier      tier standard (2 CPUs, 4 GiB, gvisor); Docker isn't enabled for acme/app: its .hangar/environment.yaml
                 asks for the heavy tier, and a repo's own file only requests it, and an admin hasn't allowed that for
                 this repo, so this sandbox runs on the default tier, without Docker. An admin enables it: hangar repo
                 set acme/app --heavy allow
  ```
- **Profile:** a job's label `hangar.profile` (`hangar job submit --profile large`, or `labels` on `POST /api/jobs`
  and `create_sandbox`), then the repo's file, each up to the policy's `max_profile` (else no more than the policy's or
  the environment's own profile); then the policy's `profile`; then the environment's. `large` and `xlarge` run on the
  heavy tier's nodes, with Docker or not: the gVisor pool's nodes can't hold them. A profile replaces `resources`.
  Asking past what's allowed fails the create and says which `hangar repo set` raises it.
- **Caps:** one person holds at most `HANGAR_HEAVY_PERSON_MAX` live sandboxes per profile (3 `large` or larger, 1
  `xlarge`), with the same per-person accounting as the other caps ([team-scale.md](team-scale.md)). Work no person
  owns (jobs from GitHub, Slack or Jira senders linked to nobody, webhooks, schedules without a person) shares one
  team bucket with the same caps, so it can't take the heavy pool either. Repo builds are hangar's own, one per repo
  and environment at a time, and aren't counted. Docker at the standard size isn't capped beyond the usual sandbox
  caps.
- **Warm pools** of heavy environments stay at 0 unless `HANGAR_HEAVY_WARM_POOLS=true`: a heavy node held warm costs
  (see [team-scale.md](team-scale.md#heavy-tier)).

`heavy`, `tier: heavy` and a `profile` other than standard are admin fields on an environment, like `runtime_class`.

### Docker in the sandbox

At claim, before the repo's services, hangar installs `~/.hangar/bin/hangar-docker` (a standard-library script, like
the supervisor) and runs it as root with sudo (heavy environments need `sudo: true`):

1. **Storage.** dockerd's overlay storage can't use virtio-fs as its upper layer, and under Kata the sandbox's volume
   for `/var/lib/docker` is virtio-fs. So there it makes a sparse ext4 image of the profile's disk on that volume and
   loop-mounts it on `/var/lib/docker`, the approach Kata's own guide recommends over a memory-backed volume
   ([How to run Docker in Docker with Kata Containers][kata-dind]). On ext4, xfs, btrfs or tmpfs (a microVM's root disk,
   a Docker volume) it uses the directory as it is. The volume is an `emptyDir` sized to the disk (`sizeLimit`), with an
   `ephemeral-storage` request of the same, so the scheduler places the pod where the node's disk has room.
2. **cgroups.** Kata's guest uses cgroup v1 unless its kernel is told otherwise ([kata-agent][kata-agent]), which
   dockerd 29 still supports, though deprecated ([Docker deprecations][docker-deprecated]). On cgroup v2 it moves the
   container's processes into a child cgroup first, so controllers can be delegated (what moby's `hack/dind` does).
3. **dockerd** (from `HANGAR_HEAVY_DOCKER_IMAGE`, `docker:29.8.2-dind`, copied into the image as static binaries with
   the CLI, Compose and Buildx), under a loop that restarts it, with iptables for its NAT and published ports, the
   bridge MTU set to the sandbox's, and its socket owned by the sandbox user's group. Nothing listens on TCP.

Its result is recorded (`get_sandbox`'s `info.heavy.status`) and shown to the agent. If Docker doesn't start, the
sandbox still starts, with a setup failure that says why and how to start it again. Docker's images and volumes
live in the sandbox: a resumed session starts with an empty Docker.

### Docker images

dockerd's pulls go through the sandbox's egress policy like everything else:

- **Open** egress: straight to the registries. `HANGAR_HEAVY_REGISTRY_MIRRORS` (dockerd's `registry-mirrors`, Docker
  Hub only) can name `https://mirror.gcr.io`, Google's cache of popular Docker Hub images, whose pulls don't count
  against Docker Hub's rate limits ([Pull cached Docker Hub images][ar-mirror]).
- **Allowlist**: dockerd uses the sandbox's egress proxy, with its own credential. The `docker` preset opens Docker
  Hub (`registry-1.docker.io`, `auth.docker.io` and its blob CDN, from [Docker's allowlist][docker-allowlist]); other
  registries go in `allow`. Docker Hub also takes pushes with anyone's token, so prefer hangar's mirror:
- **Hangar's mirror** (`HANGAR_HEAVY_MIRROR=true`): a read-only pull-through of Docker Hub on the git gateway's port,
  in the same pattern as the [package mirrors](security.md#package-mirrors): GET and HEAD of manifests, blobs and
  tags only. Its upstream is Docker Hub, or an Artifact Registry remote repository for Docker Hub
  (`HANGAR_HEAVY_MIRROR_UPSTREAM`, terraform's `dockerhub_mirror`) holding Docker Hub credentials of your own, so a
  team's pulls don't hit the anonymous limit. Hangar reads it with its own Google identity; sandboxes never get
  `*.pkg.dev`, which serves every project's writable repositories too. dockerd can't send credentials to a mirror (it
  refuses a mirror URL with a user in it), so the sandbox's egress credential is in the mirror's path. Use is summed
  into the `egress.allow` audit entries as `docker.mirror.hangar`.

In an allowlist sandbox, containers and builds get the egress proxy too (`~/.docker/config.json`'s `proxies`, by
address: they can't resolve hangar's names).

### Compose stacks under the supervisor

A `services:` entry with `compose: FILE` (relative to the checkout) is a Compose stack: the supervisor runs `docker
compose -p NAME -f FILE up` in the foreground, so the containers' output is its log (`hangar-agent svc logs NAME`) and
a stop or restart stops the containers (SIGTERM). Stacks start with the services, before `start`, and `start` and the
terminals wait for them: ready when `port` answers, or `ready` exits 0, or by default when every service in the file
has a container running (and healthy, if it has a healthcheck) or exited 0. `env` is passed to Compose (for
variable substitution). A stack takes only `compose`, `port`, `ready` and `env`: images, commands and volumes are the
file's. Stacks come from the checkout itself, so one added in a commit starts right away, unlike image services.
`hangar-agent svc add NAME --compose FILE` supervises one for this run. In a sandbox without Docker, a stack is a
setup failure that says the heavy tier is needed.

### Isolation

The VM is the boundary. Under Kata the sandbox container is privileged *inside the guest*: kata-deploy configures
containerd with `privileged_without_host_devices = true`, so no host device is passed into the VM
([Kata: privileged containers][kata-privileged]). Containers dockerd starts sit on a bridge inside the VM and leave it
NATed to the pod's own address, so the pod's NetworkPolicy applies to them as to the sandbox: no other pods, no
node addresses, no metadata server (`deploy/k8s/sandbox-network.yaml`), and only the proxy in an allowlist
environment. dockerd listens on its Unix socket only, so containers can't reach it over the bridge. Sidecar services
keep their own unprivileged security context. On the `docker` provider there is no VM: privileged runc is root on the
host.

### Why Kata and not gVisor

gVisor has a documented Docker-in-gVisor mode ([Docker in gVisor][gvisor-docker], [Docker in GKE Sandbox][gvisor-gke])
and hangar evaluated it:

- the container needs added capabilities (SYS_ADMIN, NET_ADMIN...) and to run as root, which GKE's Agent Sandbox
  policy refuses for every sandbox ([Agent Sandbox requirements][gke-agent-sandbox]);
- `/var/lib/docker` must be tmpfs for overlay (gVisor only takes tmpfs as an overlay upper layer), so every image
  layer counts against the sandbox's memory, and a 100 GiB disk profile isn't possible;
- dockerd runs with `--iptables=false`, so published ports (`-p`, Compose's `ports:`, what Testcontainers maps) don't
  work and containers need `--network=host`;
- Docker 28 and later need runsc's `--allow-packet-socket-write`, which "allows sandboxed code to craft arbitrary
  network packets".

So Compose stacks and Testcontainers, the point of the tier, don't work there. Kata runs an unmodified guest kernel
with overlay, bridges, veth and netfilter built in (its guest kernel config notes "Docker in Docker support requires
overlay"), and E2B and CubeSandbox are microVMs too. Docker-in-gVisor for builds alone could come later as a lighter
tier.

### What the agent sees

- `hangar-agent env doctor` starts with a `tier` line: the tier, the profile and its size, the runtime, and whether
  Docker answers (with the command that starts it when it doesn't).
- A "Docker (hangar heavy tier)" section in its instructions, and the `hangar-docker` skill: Compose stacks under the
  supervisor, Testcontainers, and what to do when Docker or a pull fails. The `hangar-environment` skill says Docker
  exists only on the heavy tier.

[kata-dind]: https://github.com/kata-containers/kata-containers/blob/main/docs/how-to/how-to-run-docker-with-kata.md
[kata-privileged]: https://github.com/kata-containers/kata-containers/blob/main/docs/how-to/privileged.md
[kata-agent]: https://github.com/kata-containers/kata-containers/blob/main/src/agent/README.md
[docker-deprecated]: https://docs.docker.com/engine/misc/deprecated/
[ar-mirror]: https://docs.cloud.google.com/artifact-registry/docs/pull-cached-dockerhub-images
[docker-allowlist]: https://docs.docker.com/desktop/setup/allow-list/
[gvisor-docker]: https://gvisor.dev/docs/tutorials/docker-in-gvisor/
[gvisor-gke]: https://gvisor.dev/docs/tutorials/docker-in-gke-sandbox/
[gke-agent-sandbox]: https://docs.cloud.google.com/kubernetes-engine/docs/how-to/how-install-agent-sandbox

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

**Models and budget.** Both agents run on the `balanced` model policy, the same one jobs default to
([harnesses.md](harnesses.md#models-the-latest-strong-model-per-provider)): it is resolved from the provider's
model list (read with `HANGAR_MODEL_LIST_CREDENTIALS`, or OpenRouter's public list for pi) when each agent's
session is created, and pinned there. A job's own `model` wins, and `HANGAR_ENV_SETUP_MODELS` pins a model or a
policy per harness. The job's events, its report and the PR's packet name the id, the policy, its list price and
where the id came from (a live list, the last id resolved, or hangar's built-in one). The job budget is the job's, else the repo
policy's, else `HANGAR_ENV_SETUP_MAX_COST_USD` and `HANGAR_ENV_SETUP_MAX_TURNS`. The goal job gets a quarter of it
(the proposal and the judging; at least one turn), and the setup job gets the rest (every attempt), so together
they can't exceed it. A budget of fewer than 2 turns is refused. When the goal job's turns run out, later outputs are
judged by their exit codes, and the job's events say so.

**The PR holds what was validated.** The packet names the commit hangar validated. If the setup branch's head has
moved since, the PR isn't opened and the job fails `validation_stale`. Neither agent's sandbox nor the validation
sandbox can push (fetch-only grants), so that takes someone else with write access. `.hangar/**` is high risk in every
PR's packet.

**Safety.** The validation sandbox runs code an agent wrote on an unmerged branch, so it gets less than a build:
a fetch-only grant of the git gateway for its clone (without the gateway, no git credential at all), and nothing
else: no credential of the environment, none of its `secrets:`, no cloud tokens, no build secrets. Its
egress is the environment's and the repo policy's, as for any sandbox. The file it validates can't name what an
admin decides (refused like any `.hangar/environment.yaml`), its services must be on `HANGAR_ENV_SERVICE_IMAGES`, and
`mocks` are documentation only, shown as inline code in the PR and its packet. The PR lands through the landing
path like any agent PR: a person approves it. A repo whose install needs a private registry token fails
validation and names the token, and an admin maps it as a build secret for the merged file. The env-setup job
holds no job slot while it waits: a background task drives its stages and keeps its state in the job's
`result.data`, so a restart resumes it. A follow-up to it answers whichever of its agents asked a question.
Cancelling it cancels both agents and removes the validation sandbox.

## Root in the sandbox: `sudo`

An environment spec's `sudo` (admin-set; a repo's own file can't set it) gives the sandbox user passwordless sudo, which
makes the agent root whenever it likes. It is **`false` by default** (#124). With sudo the agent can rewrite files
hangar owns (managed harness settings) and the [uid split](steering.md#the-uid-split) protects nothing.

- **Install at build time.** `tools.apt`, mise and `setup` steps run as root in the image build, so the shipped
  environments (`base`, `bare`, `python` and the rest) need no runtime root. A repo's `install` runs as the sandbox user
  and should put what it installs inside the repo (`.venv`, `node_modules`).
- **`sudo: true`** for an environment whose people need root at runtime (`apt-get install` while exploring). Keep
  it for people's own sessions. On Docker, a job's sandbox runs with `no-new-privileges` under
  `HANGAR_INTAKE_POSTURE=enforce` ([security.md](security.md#intake-posture)), so sudo fails there even with
  `sudo: true`, as it always does on GKE (`allowPrivilegeEscalation: false`).
- **Heavy environments** run dockerd as root, so `heavy: true` (or `tier: heavy`) implies `sudo: true`, and an explicit
  `sudo: false` beside it is refused. Under `enforce`, an intake job on a heavy environment is refused unless the repo
  policy sets `allow_heavy_for_intake: true` (`hangar repo set owner/repo --allow-heavy-for-intake`).
- `hangar doctor` lists the environments with `sudo: true` and whether intake jobs run in them. A sandbox's manifest
  records `permissions.sudo` and `permissions.no_new_privs`.


## MCP servers and trust

An environment's `mcp:` servers are written into each harness's own config at claim ([guide.md](guide.md)). Each
server can say whether its tool results can carry text nobody you trust wrote:

```yaml
mcp:
  github:                         # issue comments, PR bodies: anyone can write them
    url: https://api.githubcopilot.com/mcp/
    headers: {Authorization: "Bearer ${GH_TOKEN}"}
    trust: untrusted              # the default for a url server
  docs-index:
    command: docs-mcp             # a local stdio server over your own repo
    trust: trusted                # the default for a command server
```

`mcp.<name>.trust` is `trusted` or `untrusted`. Unset, a remote (`url`) server is `untrusted` and a local stdio
(`command`) server is `trusted`. A call to an untrusted server's tool taints the turn ([security.md](security.md#taint)).
Mark a stdio server that relays third-party text (Jira, Slack, a mail or web reader) `untrusted`, and a remote one you
run over your own data `trusted`. Hangar's own `hangar-env` server is trusted; the built-in browser
(`computer.browser_mcp`) is untrusted; a server the agent adds itself is untrusted. A repo's
`.hangar/environment.yaml` can't set `mcp:` at all.

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
| `HANGAR_ENV_SETUP_MODELS` | unset | `{"claude-code": "frontier", "codex": "gpt-6-astra"}`: a model or a policy per harness instead of `balanced` |
| `HANGAR_ENV_SETUP_LABEL` | `hangar-env-setup` | An issue (GitHub, Jira, GitHub Projects) with this label starts an env setup |
| `HANGAR_TIERS` `heavy` | unset (off) | The heavy tier's providers, in order (`kubernetes`, `e2b`, `docker`) |
| `HANGAR_HEAVY_RUNTIME_CLASS` | `kata-qemu-runtime-rs` | Kubernetes: the VM runtime heavy pods run under |
| `HANGAR_HEAVY_NODE_SELECTOR` | `true` | Kubernetes: pin heavy pods to the `sandboxes-heavy` pool (`hangar.dev/runtime=heavy`) |
| `HANGAR_HEAVY_PERSON_MAX` | `{"large": 3, "xlarge": 1}` | Live sandboxes per person per profile (`large` counts xlarge too); 0: no cap |
| `HANGAR_HEAVY_WARM_POOLS` | `false` | Keep heavy environments' warm pools |
| `HANGAR_HEAVY_DOCKER_IMAGE` | `docker:29.8.2-dind` | Where images get dockerd, containerd, runc and the CLI (Compose, Buildx) |
| `HANGAR_HEAVY_REGISTRY_MIRRORS` | `[]` | dockerd's `registry-mirrors` (Docker Hub only), e.g. `["https://mirror.gcr.io"]` |
| `HANGAR_HEAVY_MIRROR` | `false` | Hangar's read-only Docker Hub pull-through on the gateway, first among the mirrors |
| `HANGAR_HEAVY_MIRROR_UPSTREAM` | `https://registry-1.docker.io` | Its upstream: Docker Hub, or an Artifact Registry remote repository for it |
| `HANGAR_HEAVY_DOCKER_PRIVILEGED` | `false` | Docker provider: heavy sandboxes as privileged runc containers (root on the host) |

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
