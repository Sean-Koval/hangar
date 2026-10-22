# Environments

An agent is only as good as the computer it gets. Say the dependencies aren't installed, the database isn't up or the
dev server died an hour ago. Then the output just gets weaker, and people blame the model.

hangar's environment specs (`envs/*.yaml`, [spec 0006](specs/0006-environments-and-harness-packs.md)) describe the
machine: the image, the harnesses, the credentials. This page answers how to write one. The **repo** can also say
things about itself, in a file committed next to its code:

| What | Page |
| --- | --- |
| `.hangar/environment.yaml`: how to install the repo's dependencies, what to start, which long-running processes to keep up, and which check proves a change works | [The repo's own file](environments/repo-file.md) |
| **Versioned builds**: hangar runs `install` ahead of time, in the background, and keeps the result as an image. Sandboxes for the repo start from it, with everything installed | [Builds](environments/builds.md) · [Build secrets and limits](environments/build-secrets.md) |
| **The service supervisor** (`hangar-agent svc`): one command for the agent to see, start, restart and read the logs of the environment's processes. A process that exits is restarted, so the agent doesn't have to look after it | [The service supervisor](environments/supervisor.md) |
| **Services** (Postgres, Redis...) declared as images, run beside the sandbox and reachable at their name | [Services](environments/services.md) |
| **Preview ports**: the sandbox's owner opens a declared port in their browser, through hangar, never publicly | [Preview ports](environments/previews.md) |
| **The heavy tier**: Docker inside a VM of the sandbox's own, Compose stacks under the service supervisor, and bigger resource profiles. It is for repos that test with `docker compose` or Testcontainers | [The heavy tier](environments/heavy-tier.md) · [Docker in the sandbox](environments/docker.md) · [Isolation](environments/heavy-isolation.md) |
| **Agent-led setup**: agents write a repo's file, hangar validates it, and it arrives as a PR | [Agent-led setup](environments/agent-led-setup.md) · [Re-runs, budget and safety](environments/agent-led-setup-safety.md) |
| Root in the sandbox, Claude Code plugins, and which MCP servers are trusted | [Root, plugins and MCP trust](environments/sudo-plugins-mcp.md) |

## Environment specs

Specs are YAML files in `envs/`. They can `extend` each other ([how `extends` merges](#how-extends-merges)):

- maps merge by key (`null` deletes one);
- lists replace the parent's (`skills`, `harnesses`, `credentials`, ...);
- the additive build inputs `tools.apt` and `dependencies.pip`/`npm` union;
- `skills_remove: [name]` drops an inherited skill.

The full design is [spec 0006](specs/0006-environments-and-harness-packs.md). A repo can describe its own steps in a
committed `.hangar/environment.yaml` (`install`, `start`, `terminals`, `verify`). hangar builds it ahead of time, and
agents start with dependencies installed and dev servers supervised ([the repo's own file](environments/repo-file.md)).

```yaml
name: my-service
extends: python                    # bare + Python 3.12/uv/ruff/pytest; bare = Ubuntu + git/gh/tmux/rg
harnesses: [claude-code, codex]    # agents this env allows: claude-code, codex, pi, antigravity
default_harness: claude-code
model: { claude-code: frontier, codex: balanced }  # per harness, or one: a policy or a pinned id (harnesses.md)
tools:
  apt: [postgresql-client]
  mise: { go: "1.23" }
dependencies:
  pip: [ruff, pytest]              # installed with `uv tool install`
  npm: [pnpm]
  script: |                        # runs as the sandbox user
    curl -fsSL https://example.com/install.sh | bash
skills:
  - path: skills/repo-conventions  # copied into ~/.claude/skills and ~/.codex/skills
  - git: https://github.com/org/skills#review@main
agent_config:
  instructions: |                  # written to each allowed harness's global instructions file
    Run `make test` before you finish.
files:
  .config/foo/config.toml: "key = 1"
repo:
  url: https://github.com/me/my-service.git
  branch: main
  setup: uv sync                   # after clone
  bake: false                      # true = clone + setup at build time, for instant claims
credentials: [claude-api, codex-api, gh]  # each sandbox gets its harness's ones, plus non-agent ones (git)
secrets: { SENTRY_TOKEN: sentry }  # env var <- credential, injected at claim
mcp:                               # written in each harness's own config format at claim
  sentry: { url: "https://mcp.sentry.dev/mcp", headers: { Authorization: "Bearer ${SENTRY_TOKEN}" } }
resources: { cpus: 4, memory_gb: 8 }
warm_pool: 2                       # keep two pre-started
model_gateway: record              # model calls through hangar: the API key stays out, calls become tapes
```

`model_gateway: record` sends Claude Code's and Codex's model calls through hangar's [model gateway](model-gateway.md).
The sandbox gets the gateway's URL and a per-sandbox bearer instead of the API key, and each run's calls are kept as a
tape. It needs `HANGAR_GATEWAY_URL` and an API key (subscription logins are refused).

`hangar env render my-service` prints the generated Dockerfile. `hangar pack render codex` prints a harness pack's
build. When the server has no builder, `hangar pack publish claude-code codex pi antigravity` builds and pushes packs
from your machine.

## How `extends` merges

An environment spec can `extends` another: the shipped `envs/*.yaml`, a saved one, or one `hangar env` /
`create_environment` cloned. The child is laid over the parent field by field:

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
is an error that lists the names it has. The table is `envspec.LIST_MERGE`.

A repo's `.hangar/environment.yaml` follows it too: each field it sets replaces the environment's. Every list it may
set replaces under `extends` as well. An experiment variant's `skills` and `skills_remove` have the same meaning
([experiments](jobs/experiments.md)).

Before #132 every list except `harnesses`, `credentials`, `terminals` and `ports` was appended. `hangar env
migrate-merge --dry-run` lists which environments resolve differently
([the safe-release upgrade](releases/safe-release.md#the-extends-merge-rules-132)).

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
| `HANGAR_HEAVY_MIRROR` | `false` | hangar's read-only Docker Hub pull-through on the gateway, first among the mirrors |
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

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="the-file"></span>The file | [The repo's own file](environments/repo-file.md#the-file) |
| <span id="what-the-repo-cant-set"></span>What the repo can't set | [The repo's own file](environments/repo-file.md#what-the-repo-cant-set) |
| <span id="resolution-order"></span>Resolution order | [The repo's own file](environments/repo-file.md#resolution-order) |
| <span id="builds"></span>Builds | [Builds](environments/builds.md) |
| <span id="what-the-agent-and-you-see"></span>What the agent and you see | [Builds](environments/builds.md#what-the-agent-and-you-see) |
| <span id="build-secrets"></span>Build secrets | [Build secrets and limits](environments/build-secrets.md#build-secrets) |
| <span id="limits-of-this-version"></span>Limits of this version | [Build secrets and limits](environments/build-secrets.md#limits-of-this-version) |
| <span id="build-caches"></span>Build caches | [Build secrets and limits](environments/build-secrets.md#build-caches) |
| <span id="services"></span>Services | [Services](environments/services.md) |
| <span id="preview-ports"></span>Preview ports | [Preview ports](environments/previews.md) |
| <span id="the-service-supervisor-hangar-agent-svc"></span>The service supervisor | [The service supervisor](environments/supervisor.md) |
| <span id="the-heavy-tier"></span>The heavy tier | [The heavy tier](environments/heavy-tier.md) |
| <span id="turning-it-on"></span>Turning it on | [The heavy tier](environments/heavy-tier.md#turning-it-on) |
| <span id="what-decides"></span>What decides | [The heavy tier](environments/heavy-tier.md#what-decides) |
| <span id="docker-in-the-sandbox"></span>Docker in the sandbox | [Docker in the sandbox](environments/docker.md) |
| <span id="docker-images"></span>Docker images | [Docker in the sandbox](environments/docker.md#docker-images) |
| <span id="compose-stacks-under-the-service-supervisor"></span>Compose stacks under the service supervisor | [The service supervisor](environments/supervisor.md#compose-stacks-under-the-service-supervisor) |
| <span id="isolation"></span>Isolation | [Heavy-tier isolation](environments/heavy-isolation.md) |
| <span id="why-kata-and-not-gvisor"></span>Why Kata and not gVisor | [Heavy-tier isolation](environments/heavy-isolation.md#why-kata-and-not-gvisor) |
| <span id="what-the-agent-sees"></span>What the agent sees | [Heavy-tier isolation](environments/heavy-isolation.md#what-the-agent-sees) |
| <span id="agent-led-setup"></span>Agent-led setup | [Agent-led setup](environments/agent-led-setup.md) |
| <span id="root-in-the-sandbox-sudo"></span>Root in the sandbox | [Root, plugins and MCP trust](environments/sudo-plugins-mcp.md) |
| <span id="claude-code-plugins"></span>Claude Code plugins | [Root, plugins and MCP trust](environments/sudo-plugins-mcp.md#claude-code-plugins) |
| <span id="mcp-servers-and-trust"></span>MCP servers and trust | [Root, plugins and MCP trust](environments/sudo-plugins-mcp.md#mcp-servers-and-trust) |
