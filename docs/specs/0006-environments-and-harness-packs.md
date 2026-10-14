# 0006: Environments and harness packs

Status: M1–M3 implemented in this PR; M4–M7 planned. Date: 2026-10-12.

> Part of a set. Ownership, cross-spec decisions and the build order are in [docs/specs/README.md](README.md) (decision 14).

## 1. Summary

- An environment has three parts. The **image** is what is on disk. The **runtime** is where it runs and with what isolation. The **session** is what is applied when a sandbox is claimed.
- `harnesses` lists the agents an environment *allows*. It no longer installs them into the image.
- Each harness ships once as a **harness pack**: a small OCI image holding `/opt/hangar/harness/<name>`. It is mounted read-only when a sandbox starts. Kubernetes uses image volumes (stable in 1.36, verified under gVisor on GKE). Docker uses `--mount type=image`.
- `create_sandbox(harness=...)` picks one allowed agent. Only that agent's credentials, env, config files and MCP servers are applied, and only its `bin/` goes on `PATH`.
- Providers that cannot mount images fall back to **baking**: E2B, and Docker without the containerd image store. Only the selected harness is baked, into a per-harness image variant built from the same pack stage.
- Five defaults ship: `bare`, `python`, `node`, `go`, `polyglot`. None carries harness binaries. Teams extend or clone them, for example the `hangar` environment in `envs/`.
- Out of the box: Claude Code, Codex, pi and Antigravity (`agy`). Gemini CLI stays usable but is marked deprecated, since Google is retiring it for AI Pro/Ultra accounts in favour of `agy`.

## 2. Problem

Before this change, `harnesses: [claude-code, codex, antigravity, gemini]` installed all four CLIs into the environment image. The image for `polyglot` grew by about 1.2 GB of agent binaries. Every environment that wanted "any agent" paid that cost, and rebuilt whenever any harness updated. Every sandbox also received every attached credential, including those of agents it would never run.

The owner's requirements (2026-10-11):

1. Control which harnesses an environment sets up, without bringing every harness into every environment.
2. Or choose a purpose-built environment, such as a prebuilt image or microVM image.
3. Teams build custom environments per project and per member, for example a "hangar env" that others clone and extend.
4. A bare default: a fresh agent and nothing else.
5. Codex, Claude Code, pi and Antigravity supported out of the box.

## 3. Design

### 3.1 The three parts

| Part | Fields | Applied |
| --- | --- | --- |
| Image | `base` or `image`, `tools`, `dependencies`, `skills`, `agent_config`, `files`, `env`, `repo.bake` | at build |
| Runtime | `resources`, `runtime_class`, `warm_pool`, provider | at start |
| Session | `harnesses`, `default_harness`, `model`, `credentials`, `secrets`, `mcp`, harness `config`/`env` | at claim, for one harness |

Skills and global instructions stay in the image, copied into every allowed harness's directory. They are small, and baking them keeps claims fast.

### 3.2 Harness packs

A harness profile (`src/hangar/harnesses.py`) has an `install` script. It runs as root in a builder image (`node:22-bookworm`) with `PREFIX=/opt/hangar/harness/<name>`, and must leave everything it needs under `$PREFIX` and its entrypoints in `$PREFIX/bin`.

| Harness | Version | What the pack holds | Size |
| --- | --- | --- | --- |
| claude-code | latest (installer) | the native `claude` binary | ~200 MB |
| codex | 0.162.1 | the static musl `codex` binary (`bin/codex` links to it), its helper binaries and bundled `rg`, which hangar appends to `PATH` at claim time (`path_extra`), so the pack holds no hangar code | 432 MB |
| pi | 1.1.0 (`@earendil-works/pi-coding-agent`) | the npm package plus its own node 22; a one-line launcher that names its prefix calls that node directly, so pi never uses or changes the project's node | 284 MB |
| antigravity | latest (installer) | the `agy` binary (glibc) | ~205 MB |

Codex and pi sizes were measured by running their install scripts. Claude Code and `agy` sizes are from the binaries inspected on 2026-10-11.

The same stage serves both delivery modes:

```dockerfile
FROM mirror.gcr.io/library/node:22-bookworm AS pack-codex
COPY packs/codex.sh /tmp/pack.sh
RUN bash /tmp/pack.sh
# pack image:
FROM scratch
COPY --from=pack-codex /opt/hangar/harness/codex/ /
# or, baked into an environment image:
COPY --from=pack-codex /opt/hangar/harness/codex /opt/hangar/harness/codex
```

The pack tag is `<registry>/harness-<name>:<version>-<digest>`. The digest covers the Dockerfile and install script, so changing either produces a new tag. "latest" versions do not change the tag; rebuild with `force` to pick up a new upstream release.

**Mounting.** The fleet resolves `spec.packs` (harness → image) before calling a provider. Every allowed agent harness is mounted, not just the selected one. A warm-pool sandbox is created before anyone picks a harness, and pod volumes cannot change after creation. Mounts are read-only and the images are cached per node, so mounting unused packs costs nothing in the sandbox. Only the selected harness is configured and on `PATH`.

- Kubernetes: `volumes[].image {reference, pullPolicy: IfNotPresent}` plus `volumeMounts[] {mountPath: /opt/hangar/harness/<name>, readOnly: true}`. Controlled by `HANGAR_K8S_IMAGE_VOLUMES` (default on). The GKE Agent Sandbox admission policies allow image volumes.
- Docker: `mounts: [{Type: image, Source, Target, ReadOnly}]`. Detected automatically: Engine API 1.48 or later and `driver-type` `io.containerd.snapshotter.v1`. Override with `HANGAR_DOCKER_IMAGE_VOLUMES`.
- E2B: no image mounts, so it always bakes.

**Building.** `fleet.build_pack` builds through the provider's normal builder (local docker, docker plus push, or Cloud Build), records `pack:<provider>:<harness>`, and is called on demand the first time an environment needs a pack. When the server has no builder, `hangar pack publish <harness>...` builds on your machine, pushes and registers. This mirrors `hangar env publish`.

**Bake fallback.** `harness_mode: auto` (the default) packs when the provider can mount images, otherwise it bakes. `bake` forces baking, for example for an image you will snapshot and move between providers. `pack` refuses to run on a provider that cannot mount. A baked image contains only the selected harness. Its tag differs per harness, and the default harness's variant backs the warm pool.

### 3.3 Selecting a harness

`create_sandbox(environment, harness=None)`:

1. `harness` must be in the environment's agent harnesses. If omitted, `default_harness` is used, else the first listed.
2. **Credentials.** Without an explicit `credentials` list, the environment's defaults are filtered. A credential is kept when its harness is the selected one, or is not an agent (git, or a free-form label). An explicit list is used as given.
3. **Env.** `~/.hangar/env` gets `PATH="/opt/hangar/harness/<h>/bin:$PATH"`, the harness's `env` (for example `DISABLE_AUTOUPDATER=1`), `HANGAR_HARNESS`, credential env vars, and `secrets:` entries (`ENV_VAR: credential`).
4. **Config.** JSON files in the harness's `config` are deep-merged. For example `.claude.json` gets `hasCompletedOnboarding`, so interactive `claude` skips its wizard.
5. **MCP.** Servers in `mcp:` (filtered by an optional per-server `harnesses`) are written in the harness's own format. A server whose `${VAR}` references cannot be resolved is skipped, with a warning in `sandbox.info.warnings`; creation does not fail.

`run_agent(sandbox, prompt, model=None)` runs the sandbox's harness. Asking for a different harness is an error, because that harness's credentials and config were never applied. `model` (or the environment's `model`) is exported as `HANGAR_MODEL`, and each harness's run command passes it through (`--model` or `-m`). Forks inherit the parent's harness.

### 3.4 MCP rendering

One harness-neutral block:

```yaml
mcp:
  github:
    url: https://api.githubcopilot.com/mcp/
    headers: { Authorization: "Bearer ${GH_TOKEN}" }
  playwright:
    command: npx
    args: ["-y", "@playwright/mcp@latest"]
    harnesses: [claude-code, codex]
```

| Harness | File | Shape |
| --- | --- | --- |
| claude-code | `~/.claude.json` | `mcpServers.<n> = {type: http, url, headers}` or `{type: stdio, command, args, env}` |
| codex | `~/.codex/config.toml` | `[mcp_servers."<n>"]` with `url` + `http_headers`, or `command`/`args`/`env`. Written as a marked block that is replaced in place, so the user's own config survives |
| pi | `~/.pi/agent/mcp.json` | `mcpServers.<n> = {url, headers}` or `{command, args, env}` |
| antigravity | `~/.gemini/config/mcp_config.json` | `mcpServers.<n> = {serverUrl, headers}` or `{command, args, env}` |

`${VAR}` is resolved by the control plane when the file is written, and the file is mode 0600. Not every harness expands variables itself (Codex does not for headers), and the value is already in `~/.hangar/env`. So this adds no new exposure, but the secret does sit in a file the agent can read. Spec 0003's credential gateway replaces this for GitHub. Other MCP servers get the same treatment once the gateway can proxy them.

### 3.5 Credentials per harness

| Harness | Kind → where |
| --- | --- |
| claude-code | `api_key` → `ANTHROPIC_API_KEY`; `oauth_token` → `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) |
| codex | `api_key` → `CODEX_API_KEY` (+ `codex login --with-api-key`); `auth_file` → `~/.codex/auth.json`, exclusive lease (ChatGPT login rotates refresh tokens) |
| pi | `api_key` → `OPENROUTER_API_KEY` by default, or any provider's variable via `--env-var`; `auth_file` → `~/.pi/agent/auth.json`, exclusive; `generic` → GCP service-account key for `google-vertex/*` models |
| antigravity | `generic` → GCP service-account key, billed to the key's project: sets `AGY_ADC_AUTH=true`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_QUOTA_PROJECT` and `GOOGLE_CLOUD_LOCATION` (Antigravity's enterprise ADC mode). `api_key` → `GEMINI_API_KEY` (+ `modelProvider: gemini`), billed through the Gemini API (AI Studio) instead |

Live test on GKE (2026-10-12, `agy` 1.3.3). With a service-account key, `AGY_ADC_AUTH=true` and `GOOGLE_CLOUD_QUOTA_PROJECT`, headless `agy` bills the project. It answered on `gemini-3.8-flash-high`, and `agy models` listed Gemini 3.x models up to 3.8 Flash. That mode is documented on [antigravity.google/docs/enterprise](https://antigravity.google/docs/enterprise); the account needs `aiplatform.googleapis.com` enabled and Vertex access. The `GOOGLE_GENAI_USE_ENTERPRISE`/`USE_VERTEXAI` variables have no effect. With `modelProvider: gemini`, `agy` calls `generativelanguage.googleapis.com`, which bills through AI Studio's prepaid or postpaid plan, not the project's Cloud Billing. Interactive Google sign-in is stored in the OS keyring (Secret Service over D-Bus), which sandboxes lack, so `agy` has no login flow. pi's `google-vertex/*` models also work with the same service-account key.

### 3.6 Bring your own image

`image: us-docker.pkg.dev/acme/envs/ml-gpu:2026-10` runs a prebuilt image as-is, with no build step. The image must:

- have `bash`, `tmux`, `git`, `python3`, and the CA bundle the agents need;
- have a uid 1000 user named as `HANGAR_SANDBOX_USER` (default `agent`), with a writable home and a writable `/workspace`;
- source `~/.hangar/env` from login shells (`.profile`), or accept that only hangar's run scripts and exec see injected env;
- on providers that bake, already contain the harness under `/opt/hangar/harness/<name>`, or on `PATH`.

To customise a prebuilt image instead, set `base:` to it and hangar layers the rest on top. A Kata or microVM image is a runtime choice (`runtime_class`) on top of either.

### 3.7 Defaults and team environments

| Name | Extends | Adds |
| --- | --- | --- |
| `bare` | | Ubuntu 24.04, git, gh, tmux, ripgrep, fd, shellcheck; allows all four agents; default `claude-code`; 2 CPU / 4 GB |
| `python` | bare | Python 3.12, uv, ruff, pytest, pre-commit |
| `node` | bare | Node 22, pnpm, TypeScript, tsx |
| `go` | bare | Go 1.24 |
| `polyglot` | bare | Python + Node + Go; 4 CPU / 8 GB |

`base`, `claude-python`, `codex-node`, `minimal` and `example-repo` remain for compatibility.

Inheritance rules: maps merge, string lists union (`apt`, `pip`, ...), and `harnesses` and `credentials` **replace** the parent's. So `extends: python` plus `harnesses: [codex]` gives a Codex-only Python environment.

Tools for building environments (MCP and REST):

- `validate_environment(spec)` resolves `extends`, validates, warns about missing credentials, deprecated harnesses or no agent, and returns the Dockerfile. It saves nothing.
- `create_environment(spec)` saves it.
- `clone_environment(source, name, overrides)` saves `{extends: source, ...overrides}`. It is the "clone the hangar env for Sam with Codex" operation.

## 4. Milestones

| # | Milestone | Size | Status |
| --- | --- | --- | --- |
| M1 | Harness profiles: pi added; Codex `exec --json --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check`; `agy` skills and MCP paths; `HANGAR_MODEL`; Gemini deprecated | S | this PR |
| M2 | Session model: `harness=` selection, credential filtering, `secrets`, `mcp` rendering, harness `config`, BYO `image`, defaults, validate/create/clone tools | M | this PR |
| M3 | Packs: pack build/publish/register, Kubernetes image volumes, Docker image mounts with detection, per-harness bake fallback | M | this PR (code); live GKE acceptance below |
| M4 | Env sources: a team environment repo (`HANGAR_ENV_REPOS=github.com/acme/hangar-envs@main`) synced by the control plane, and `.hangar/env.yaml` in a project repo, picked up when `create_sandbox(repo=...)` names a repo with one | M | planned |
| M5 | Dashboard: environment editor with a harness picker, validate-as-you-type, "save as PR" to the team repo | M | planned |
| M6 | Personal profiles: per-user overlays (instructions, skills, model, MCP servers) applied at claim, after the environment's session | S | planned, low priority (owner, 2026-10-11) |
| M7 | Pack lifecycle: pre-pull packs on node start (DaemonSet), pinned versions per environment (`harnesses: [codex@0.163.0]`), GC of unused pack tags | S | planned |

## 5. Acceptance (live, GKE)

1. `hangar pack publish claude-code codex pi antigravity` pushes four packs to Artifact Registry.
2. `bare` builds an image with no `/opt/hangar` content; `docker history` shows no harness layer.
3. For each harness, `create_sandbox("bare", harness=h)` returns a sandbox where `<h> --version` works, and where `~/.hangar/env` contains only that harness's credentials.
4. Each harness completes a small task on its account and pushes a branch:
   - Claude Code on the Pro subscription token;
   - Codex on the ChatGPT login (auth.json, leased);
   - pi on an OpenRouter key, and on Vertex AI with a GCP service-account key;
   - Antigravity on the GCP project (service-account key, ADC mode). Done 2026-10-12.
5. A warm-pool claim for a non-default harness takes the same time as one for the default (packs are already mounted).
6. On Docker without the containerd store, `create_sandbox("python", harness="pi")` builds a variant that contains only pi.

## 6. Open questions

1. ~~Does `agy` accept a service-account key for project billing headless?~~ Yes, through `AGY_ADC_AUTH=true` (see 3.5). Claude models in `agy` need third-party models enabled under Gemini Enterprise > Settings > AI developer tools; not tested.
2. Should packs be signed and verified (cosign) before mounting? The mount is read-only and the images come from our own registry, so probably not before spec 0003 M5.
3. Claude Code's installer has no pinned-version URL documented for every release. If pinning matters, switch the pack to the npm package's native binary.
