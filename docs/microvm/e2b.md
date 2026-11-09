# The e2b provider and hot resume

This page is part of [The microVM tier: E2B and CubeSandbox as hangar providers](../microvm.md).

## The e2b provider

### Flavours

| | `cloud` | `selfhosted` | `cube` |
| --- | --- | --- | --- |
| Control API | `https://api.<domain>` (default `api.e2b.app`) | `HANGAR_E2B_API_URL`, or `https://api.<HANGAR_E2B_DOMAIN>` | `HANGAR_E2B_API_URL` (CubeAPI listens on `:3000`), required |
| API key | `HANGAR_E2B_API_KEY`, required | required | optional (CubeAPI has no auth unless its `AUTH_CALLBACK_URL` is set) |
| Create / connect | `POST /v2/sandboxes`, `POST /v2/sandboxes/{id}/connect` | same | `POST /sandboxes`, `POST /sandboxes/{id}/connect`: CubeAPI answers the v2 routes with 405 |
| envd URL | `https://sandbox.<domain>`, routed by the `E2b-Sandbox-Id` header | `https://49983-<id>.<domain>` | `https://49983-<id>.<domain>` through CubeProxy |
| envd access token | yes | yes | **none** ([Security](../microvm.md#security)) |
| Template builds | `Template.build` from your pushed image (e2b SDK) | same | CubeAPI's image builds (`POST /templates`) |
| Snapshots | `POST /sandboxes/{id}/snapshots` | same | same |

`HANGAR_E2B_SANDBOX_URL` overrides where envd is reached. With `{sandbox_id}` (and `{port}`) in it, it is filled in per sandbox. That is for a CubeProxy without TLS on a private network, for example `http://{port}-{sandbox_id}.cube.internal`. Without placeholders, every sandbox is reached at that one URL and routed by header. That fits a single-host test.

### What changed from the old provider

- **Lifecycle.** The provider uses its own thin httpx client for the control API, the same for every flavour. The e2b SDK from 2.51 on only posts to the v2 routes, which CubeAPI lacks (spec 0013 §5.4). The SDK is now needed only to build templates on E2B itself.
- **Commands, files and terminals** go to the sandbox's envd through hangar's own Connect client (`envd.py`), the one the Docker and Kubernetes providers use for hangar-envd. That means the same output tail (`MAX_OUTPUT`), the same read limits (#48), deadlines, and refusal of compressed or oversized answers. The difference is authentication: E2B's `X-Access-Token`, plus the traffic token when there is one, instead of hangar-envd's per-request signatures.
- **One login shell per command.** The old `exec` ran `bash -lc '<cmd>'` through `commands.run`, which itself runs `/bin/bash -l -c`, so the profile ran twice on every call. Now envd starts `timeout … bash -lc <cmd>` once, which also gives a timeout that kills the command.
- **Ports are private.** Every sandbox is created with `network.allowPublicTraffic=false`, so the edge wants the traffic token, which only hangar holds.
- **Tokens stay in memory.** The access token comes from create and connect, is never stored or logged, and is fetched again after a control-plane restart. The one exception is the traffic token: CubeAPI returns it only from create (its connect and resume read the sandbox back without it), so the provider keeps it with the sandbox record, encrypted with a key derived from hangar's secret key, and uses it whenever connect doesn't hand one out. On a running sandbox, `connect` only extends its timeout. A paused sandbox is never connected implicitly, because connecting would resume it.
- **Errors are honest.** `status` raises when the API can't be reached, rather than reporting the sandbox stopped. `list_refs` returns nothing on failure, so reconciliation doesn't mark every sandbox gone.

### Templates

A hangar environment becomes an E2B or Cube template in one of two ways:

1. **Built by hangar** (`hangar env publish`, or the first create). hangar builds the image, pushes it to `HANGAR_REGISTRY`, then:
   - on `cloud` or `selfhosted`, runs `Template.build` from that image, named `hangar-<env>:<digest>`. E2B adds its own envd;
   - on `cube`, posts `{image, name: hangar-<env>-<digest16>, cpu (millicores), memory (MiB), exposedPorts: [49983], probePort: 49983, probePath: /health, writableLayerSize}` to CubeAPI's `/templates`, then polls `/templates/{id}/builds/{job}/status` until it is `ready`. Creates look the alias up for its `tpl-…` id.
2. **Built out of band and mapped**: `HANGAR_E2B_ENVIRONMENT_TEMPLATES='{"python": "tpl-0a1b2c", "node": "my-team/node-22"}'`. The provider uses the mapped template and builds nothing. A mapped template has to contain what the environment would have (its tools, its harness, the `agent` user and `/workspace`), because hangar can't check it.

**What has to happen out of band on CubeSandbox.** Cube templates don't get an envd injected the way E2B's do. The image must start E2B's envd on 49983 itself, or Cube's build probe never succeeds:

- Set the environment's `base:` to `ghcr.io/tencentcloud/cubesandbox-base:2026.16`. That is Ubuntu 22.04 with envd, and its entrypoint (`cube-entrypoint.sh`) starts envd and then runs the image's command. hangar's rendered image keeps that entrypoint and adds `sleep infinity` as the command.
- Add `env: {ENVD_LOG_FILE: "-"}` to the environment. hangar's image ends as the unprivileged `agent` user, and the entrypoint's default log file under `/var/log` would make it exit.
- Make the image pullable by CubeMaster: a public registry, or `HANGAR_E2B_REGISTRY_USERNAME`/`_PASSWORD`, which go to Cube as `registryUsername`/`registryPassword`.
- Alternatively, build with Cube's own tooling (`cubemastercli tpl`, or its template guide) and map the result with `HANGAR_E2B_ENVIRONMENT_TEMPLATES`.

None of this ran here: no Cube cluster was available. The request body is taken from CubeAPI's `openapi.yml` and `services/templates.rs` at commit `8d3d991`.

### Settings

| Setting | Default | |
| --- | --- | --- |
| `HANGAR_E2B_FLAVOR` | `cloud` | `cloud`, `selfhosted` or `cube` |
| `HANGAR_E2B_API_URL` | `https://api.<domain>` | The control API (required for `cube`) |
| `HANGAR_E2B_DOMAIN` | `e2b.app` | Sandbox host names are `<port>-<id>.<domain>` |
| `HANGAR_E2B_SANDBOX_URL` | unset | Where envd is reached, overriding the domain rule (see above) |
| `HANGAR_E2B_API_KEY` | unset | Required for `cloud` and `selfhosted` |
| `HANGAR_E2B_ALLOW_UNAUTHENTICATED_ENVD` | `false` | Accept sandboxes whose envd has no token (CubeSandbox), on a private network only |
| `HANGAR_E2B_ENVIRONMENT_TEMPLATES` | `{}` | environment → template built out of band |
| `HANGAR_E2B_REQUEST_TIMEOUT_SECONDS` | `60` | One control API call |
| `HANGAR_E2B_TEMPLATE_BUILD_TIMEOUT_SECONDS` | `1800` | A Cube template build |
| `HANGAR_TIERS` | `{"fast": ["e2b"], "isolated": ["kubernetes"]}` | Tier → providers in order |
| `HANGAR_TIER_COOLDOWN_SECONDS` | `60` | How long a full or unreachable provider is skipped |
| `HANGAR_SESSION_HOT_RESUME_MINUTES` | `1440` | How long a suspended session's sandbox stays paused for hot resume; 0 turns it off |
| `HANGAR_SESSION_HOT_RESUME_MAX` | `20` | Most sandboxes kept paused for hot resume at once |

## Hot resume

On a provider whose pause keeps the VM's memory and whose resume restores it fast, the provider declares the `hot_resume` capability. The `e2b` provider does, on every flavour. A session suspend then works like this (`hot_resume.py`, spec 0010 M4):

1. **The checkpoint comes first, as always.** The conversation, commits and uncommitted work go to the checkpoint store. If that fails and there is no earlier checkpoint, the sandbox is kept, exactly as before.
2. **The sandbox is paused instead of destroyed.** It is kept for `HANGAR_SESSION_HOT_RESUME_MINUTES` (24 h), then the reaper removes it. The store records which sandbox is parked for the session and which checkpoint was the latest at that moment.
3. **The next turn resumes it.** E2B's connect, or CubeSandbox's, restores the memory snapshot, and hangar checks that a command runs. The session is back on the same machine: running processes, caches, build artifacts, ignored directories and anything else a checkpoint leaves out are all still there.

It falls back to restoring the checkpoint in a new sandbox whenever the parked sandbox can't be trusted to be the latest state:

- a newer checkpoint exists (the session resumed elsewhere and was suspended again);
- the sandbox is gone (reaped, or lost by the provider);
- resuming it fails, or it doesn't answer a command afterwards.

In every fallback the parked sandbox is destroyed. No checkpoint is ever deleted by any of this. A parked sandbox survives a control-plane restart; startup doesn't count it as an orphan. Archiving or deleting the session destroys it.

Parked sandboxes are a cache, so hangar bounds them. When a suspend would break one of these rules, the sandbox is destroyed as before, and the next resume restores the checkpoint:

- **A cap.** At most `HANGAR_SESSION_HOT_RESUME_MAX` (20) sandboxes are parked at once; past that, the longest-parked goes first.
- **Headroom.** Paused sandboxes count against `HANGAR_MAX_SANDBOXES`, so nothing is parked while the fleet is three-quarters full. If the fleet fills up later, a create evicts the longest-parked sandbox instead of being refused.
- **No rotating tokens.** A sandbox holding an exclusive credential, such as a Claude Code OAuth login with its rotating refresh token, isn't parked. The token goes back to the vault, so other sandboxes can use it, and a paused copy can't go stale.
- **Gateway grants.** A parked sandbox holds no live grant: it is revoked before the pause. Wake mints a new one for the session's branch as it is then (best-of-N's take-over moves the winner to the job's branch) and writes it where git reads it, as a cold boot would. If that fails, the session restores its checkpoint instead.

**Cost.** A paused sandbox isn't running, but it isn't free either: E2B keeps its snapshot (check your plan's terms for paused sandboxes), and CubeSandbox keeps the snapshot files on the node's disk. Turn hot resume off with `HANGAR_SESSION_HOT_RESUME_MINUTES=0` if that matters more than the restore time.
