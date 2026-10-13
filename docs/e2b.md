# E2B-compatible API

Hangar answers [E2B](https://e2b.dev)'s API. Anything built on the e2b SDKs can then use your own sandboxes, with your environments, git gateway, limits and audit log, after changing an API key and a URL and nothing else. That covers the `e2b` and `e2b-code-interpreter` SDKs (Python and JS), the OpenAI Agents SDK's and LangChain's E2B integrations, and agent products that run code in E2B. It is spec 0013's phase 2.

- **The control API** (`src/hangar/e2b_api.py`) is mounted at `/e2b`. It creates, lists, times out, pauses, resumes and kills sandboxes, and lists environments as templates.
- **The sandbox router** (`src/hangar/sandbox_router.py`) serves E2B's sandbox URLs. It sends commands, files and terminals to each sandbox's [hangar-envd](envd.md), and an app's traffic to its own port.

Sandboxes run on Docker or Kubernetes, the providers that have hangar-envd.

## Quick start

1. Make a token for the client. The `sandboxes` scope works on `/e2b` and sandbox URLs and nowhere else:
   ```bash
   hangar token create my-agents --scope sandboxes
   ```
2. Point the client at hangar. The simplest setup needs no DNS: every sandbox call goes through hangar's own URL, routed by the `E2b-Sandbox-Id` header that the SDKs send anyway.
   ```bash
   export E2B_API_KEY=hgr_...                                  # the token
   export E2B_API_URL=https://hangar.example.com/e2b
   export E2B_SANDBOX_URL=https://hangar.example.com/e2b/sandbox
   ```
   With a domain whose `*.` and `api.` names point at hangar (wildcard DNS and a wildcard certificate; see [On GKE](#on-gke)), set `HANGAR_E2B_API_DOMAIN=e2b.example.com` on hangar, and a client needs only `E2B_DOMAIN=e2b.example.com` and `E2B_API_KEY`.
3. Use the SDK as usual. A template is a hangar environment:
   ```python
   from e2b import Sandbox

   sbx = Sandbox.create(template="python", timeout=600, envs={"MODE": "ci"}, metadata={"task": "42"})
   print(sbx.commands.run("python3 -V").stdout)
   sbx.files.write("/workspace/hello.py", "print('hi')")
   print(sbx.commands.run("python3 /workspace/hello.py").stdout)
   sbx.kill()
   ```
   E2B's own template names map to environments with `HANGAR_E2B_TEMPLATES='{"base": "python", "code-interpreter-v1": "jupyter"}'`. `Sandbox.create()` without a template asks for `base`.

## What works

Tested with the stock SDKs: pip `e2b` 2.55.1 (the locked version) and 2.56.0 (the latest, released today) in `tests/test_e2b_api.py` and `tests/test_e2e_e2b_docker.py`, and npm `e2b` 2.56.0 (a smoke script against Docker, below).

| SDK | API | Hangar |
| --- | --- | --- |
| `Sandbox.create(template, timeout, envs, metadata)` | `POST /v2/sandboxes` (2.51+), `POST /sandboxes` | `fleet.create` from the environment; returns the sandbox's tokens and `envdVersion`. `Idempotency-Key` is honoured per token. |
| `Sandbox.list(query)` | `GET /v2/sandboxes`, `GET /sandboxes` | metadata, state, template, `startedAfter`, order and `nextToken` paging (`x-next-token`, a position: killing a sandbox between pages doesn't break it) |
| `get_info()` | `GET /sandboxes/{id}` | |
| `kill()` | `DELETE /sandboxes/{id}` | `fleet.destroy` |
| `set_timeout()` | `POST /sandboxes/{id}/timeout` | sets the expiry to now + timeout (it may shorten it) |
| (refresh) | `POST /sandboxes/{id}/refreshes` | only extends |
| `pause()` | `POST /sandboxes/{id}/pause` | the provider's pause (see below); 409 if already paused |
| `Sandbox.connect(id)` | `POST /v2/sandboxes/{id}/connect`, `/connect`, `/resume` | 200 for a running sandbox (expiry only extended), 201 after resuming a paused one |
| `get_metrics()` | `GET /sandboxes/{id}/metrics`, `/sandboxes/metrics` | an empty list: not collected yet |
| (logs) | `GET /v2/sandboxes/{id}/logs`, `/logs` | the sandbox's entries in hangar's audit log (create, exec, pause...), not its processes' output |
| templates | `GET /templates`, `/v2/templates`, `/templates/{id}` | environments |
| `commands.run/list/kill/send_stdin/connect`, background commands | envd `process.Process` | through the router to hangar-envd |
| `files.read/write/write_files/list/exists/get_info/make_dir/rename/remove/watch_dir` | envd `filesystem.Filesystem`, `/files` | multipart uploads, gzip uploads and pre-signed `upload_url()`/`download_url()` too |
| `pty.create/send_stdin/resize/kill` | envd `process.Process` | |
| `get_host(port)` | `https://<port>-<id>.<domain>` | HTTP and WebSocket to an app's port, with the traffic token |
| `e2b-code-interpreter` `run_code` | port 49999 | routed and streamed; the environment must run E2B's code-interpreter server ([below](#code-interpreter)) |

The npm SDK 2.56.0 ran create, `commands.run` with env vars, file write/read/list, `watchDir`, a PTY, background commands, `setTimeout`, `getInfo`, `list`, `connect` and `kill` against a Docker sandbox through the router.

## What doesn't (and what happens instead)

Anything hangar can't do is refused in E2B's error shape with a plain message, never done halfway:

- **501:** fork, snapshots, network updates (`PUT /sandboxes/{id}/network`), template builds (`/v3/templates`, builds, files), volumes, secrets, API keys, teams, and admin routes. Build environments with `hangar env publish`.
- **400 at create:** `autoPause`/`autoResume` (a sandbox is killed when its timeout ends; pause it yourself), `allow_internet_access=false` and `network` egress rules (use an environment whose egress is locked down), `network.allowPublicTraffic=true` (ports are private; send the traffic token), `mcp`, `iam`, `volumeMounts`, metadata keys starting with `hangar.`, and `envs` or `metadata` over 24 KB each.
- **400 at connect:** `memory=false` on a paused sandbox ("resume from disk only"), as E2B does where that isn't available, since a hangar pause always keeps the memory.
- **The run-as user.** hangar-envd runs as the sandbox's user (`agent`) and can't switch users. E2B's default user, `user`, means that user. `root` is refused: `commands.run(..., user="root")` fails.
- **Processes inherit the sandbox's environment** (as `docker exec` would), plus the sandbox's `envs` and the request's own. E2B starts them with a minimal one. The default working directory is `/workspace`, not `/home/user`.
- **`envdVersion` is 0.5.7**, so the SDKs don't use file metadata (`X-Metadata-*`, 0.6.2) or entry info in watch events (0.6.3), which hangar-envd doesn't implement. A watch also reports the daemon's temporary upload file (`.name.envd-*`) before the rename to the final name, where E2B's envd writes in place.
- **A paused sandbox is kept for `HANGAR_E2B_MAX_TIMEOUT_SECONDS`** (24 h), then removed. E2B keeps them until you kill them, but a paused Docker container still holds its memory.
- **Metrics** come back empty, and **logs** are hangar's audit entries.
- **Browsers.** There is no CORS on sandbox URLs yet, so the JS SDK in a browser can't reach envd.

## How E2B maps onto hangar

- **Template → environment**, sandbox → sandbox, metadata → labels. Sandboxes start on `HANGAR_DEFAULT_PROVIDER` and use warm pools like any other.
- **`timeout` → the sandbox's expiry**, which hangar's reaper enforces. It is capped at `HANGAR_E2B_MAX_TIMEOUT_SECONDS`. A token's `max_budget.max_minutes`, when it has one, caps each timeout and also how long its sandboxes live from their start: refreshes, timeouts, connects and pauses can't keep one past it.
- **`envs` → the sandbox's default environment.** The router adds them to every process it starts (the request's own values win), as E2B's envd does with the ones the orchestrator sets. They're stored encrypted with the vault key.
- **Pause → the provider's pause, not a session suspend.** Docker freezes the container's processes, and GKE takes a Pod snapshot (with `HANGAR_K8S_POD_SNAPSHOTS`) and frees the node. Either way memory, processes and every file survive, and `connect` resumes exactly there, which is what an E2B client expects of a pause. A session suspend keeps only the repo, the uncommitted work and the agent's conversation, and resumes in a fresh sandbox, so files and processes an E2B client left behind would be gone. A Kubernetes provider without Pod snapshots answers 400.
- **Sandboxes from elsewhere.** An admin token sees every hangar sandbox through this API, including a job's or a session's, and can `connect` to one and drive it with the SDK.

## Security

- **Tokens.** `X-API-Key` (or `Authorization: Bearer`) is a hangar token with the `sandboxes` or `admin` scope. A `sandboxes` token can't reach `/api`, `/mcp` or anything else; `admin` implies it.
- **Ownership.** A `sandboxes` token sees, connects to and kills only the sandboxes it created. Anything else gets the same 404 as a sandbox that doesn't exist. Revoking the token revokes its sandboxes' tokens too: their sandbox URLs answer 401 (an admin can still kill them).
- **Credentials.** A sandbox a `sandboxes` token creates gets no stored credentials. An environment that maps secrets or uses the git gateway is refused (403) unless it is listed in `HANGAR_E2B_TRUSTED_ENVIRONMENTS`, because whoever runs code in the sandbox could read them. Admin tokens get environments as they are.
- **Per-sandbox tokens.** Each sandbox has E2B's `envdAccessToken` (for envd, `X-Access-Token`) and `trafficAccessToken` (for other ports, `e2b-traffic-access-token`). Both are HMACs of the vault key over the sandbox id and a random nonce, so nothing secret is stored and they survive a restart. Neither is the daemon's own token. The router checks them in constant time and signs its own request to the daemon (`X-Hangar-Sig` over the method, path, query and body), so the daemon's token never leaves the control plane, and E2B's token is useless anywhere but hangar.
- **Deny by default.** Every sandbox URL needs a token, `/health` included, and no port is public. The traffic token, or the access token (which e2b-code-interpreter sends), opens an app's port. Pre-signed `/files` URLs are checked against the access token, including their expiry; each signed parameter may appear once, and an upload through one takes a single file, written to the URL's path (a multipart part's filename can't send it elsewhere). A URL signed without an expiry lasts as long as the sandbox, as with E2B. E2B's and hangar's tokens, `X-Hangar-*` headers and hop-by-hop headers are stripped on the way to the sandbox, and `X-Hangar-*` on the way back.
- **A sandbox's other ports** are reached only at the address its daemon proved (`X-Hangar-Proof`) in the last 10 seconds, never loopback, link-local (the metadata server) or unspecified, and a request target must be a path, so no request can be steered to the control plane, the metadata server or another sandbox that inherited an old IP. Pausing or resuming through this API looks the daemon up again. WebSocket messages are capped at 16 MiB either way.
- **Limits.** Per `sandboxes` token: `HANGAR_E2B_RATE_PER_MINUTE` requests (a token bucket), `HANGAR_E2B_CREATE_PER_MINUTE` creations, and `HANGAR_E2B_MAX_SANDBOXES_PER_OWNER` running or paused sandboxes. Each answers 429 with `Retry-After`; admin tokens are exempt, as with jobs. Sandbox URLs allow `HANGAR_E2B_MAX_STREAMS_PER_SANDBOX` requests in flight per sandbox. Uploads stop at `HANGAR_MAX_REQUEST_MB`, after decompression, so a gzip bomb stops there too.
- **Audit.** Creates, kills, pauses, resumes and timeouts are audited with the token's name (`e2b:<name>`). Refused API calls and refused sandbox-URL requests are audited too, rate-limited like the front door's.

## The sandbox router

It runs in hangar's process, in front of the app, and routes by:

| Request | Goes to |
| --- | --- |
| `Host: <port>-<id>.<HANGAR_E2B_API_DOMAIN>` | that sandbox's port. The label is split at the first `-`, since hangar ids contain one. |
| `Host: sandbox.<domain>`, or any host with the path `/e2b/sandbox/...` | the `E2b-Sandbox-Id` and `E2b-Sandbox-Port` headers (port defaults to 49983) |
| `Host: api.<domain>` | the control API, as if under `/e2b` |

Port 49983 goes to hangar-envd, signed as [envd.md](envd.md) describes: RPC bodies (up to 8 MiB) are read whole and signed with their hash, and uploads stream through as frames that each carry their own HMAC, so the daemon writes nothing it hasn't checked and an upload the router stops (over the cap, a client gone) leaves the file as it was. A multipart upload becomes one signed upload per file, because the daemon takes signed uploads as octet-stream only. Every answer must carry the daemon's `X-Hangar-Proof`: anything else answering at the sandbox's address gets the client a 502, and the address is looked up again. The daemon's `/health` answers anyone, so the router answers it with a signed `/info`. Responses stream back as they arrive. Small ones go back in one piece, and for streams a client that disconnects closes the upstream request (the process keeps running, as with E2B). Other ports are plain HTTP and WebSocket proxying to the sandbox's address (see Security). Docker Desktop, where container addresses aren't routable, reaches only envd.

**What it costs.** `scripts/bench/e2b_router.py` drives the SDK and raw Connect calls against one Docker sandbox, through the router and straight to the daemon, interleaved. Hangar runs in a separate process. On a 2-vCPU VM (`bench/results/2026-10-13-e2b-router-docker.json`):

| | direct p50 / p95 | through the router p50 / p95 | added p50 |
| --- | --- | --- | --- |
| Connect unary (`filesystem.Filesystem/Stat`) | 1.20 / 1.76 ms | 2.71 / 3.64 ms | 1.51 ms |
| SDK `commands.run("true")` | 6.32 / 7.57 ms | 7.63 / 9.98 ms | 1.31 ms |
| SDK `files.read` (4 KiB) | 1.75 / 2.27 ms | 3.35 / 5.09 ms | 1.60 ms |
| SDK `Sandbox.create` (`minimal`, no warm pool) | | 414 / 549 ms | |

That is under the 2 ms budget for a Python router. Most of it is the extra HTTP hop through uvicorn and httpx. Building requests with httpx's `build_request` alone cost more than the rest of the router, so the router builds them directly. Spec 0013's Rust edge (hyper, TLS with h2, the traffic-token check) is the follow-up for when the hop matters, or when hangar should stop carrying sandbox traffic in its own process.

## Code interpreter

`e2b-code-interpreter` creates a sandbox from the `code-interpreter-v1` template and sends code to a Jupyter-backed server on port 49999 in it, through the same router (`tests/test_e2b_api.py` checks this against a stand-in server). To use it with hangar, build an environment that starts E2B's server ([e2b-dev/code-interpreter](https://github.com/e2b-dev/code-interpreter), `template/`) on port 49999 at boot, and map the template name to it with `HANGAR_E2B_TEMPLATES`.

## On GKE

Sandbox URLs on GKE need either nothing new (`E2B_SANDBOX_URL` through the existing Ingress) or a wildcard certificate, which GKE Ingress can't serve. [gke.md §8](gke.md#8-e2b-sdk-clients-optional) has both setups, the Certificate Manager steps and `deploy/k8s/e2b.yaml`.

## Migrating from E2B

1. Build each template you use as a hangar environment (`envs/*.yaml`, `hangar env publish`), or map E2B's names with `HANGAR_E2B_TEMPLATES`. Prebuild them (`HANGAR_PREBUILD_ENVIRONMENTS`): a create that has to build an image takes minutes, longer than the SDK's 60-second request timeout.
2. Create a `sandboxes` token per client or app, and set `E2B_API_KEY`, then `E2B_DOMAIN`, or `E2B_API_URL` and `E2B_SANDBOX_URL`.
3. Check your code for what differs: `user="root"`, paths under `/home/user`, `allow_internet_access`, lifecycle `autoPause`, fork and snapshots, metrics.
4. For anything that reads files the sandbox's environment provides (API keys, git credentials), list the environment in `HANGAR_E2B_TRUSTED_ENVIRONMENTS`, or use an admin token.

## Settings

| Setting | Default | |
| --- | --- | --- |
| `HANGAR_E2B_API_PATH` | `/e2b` | Where the API is mounted (sandbox URLs by header at `<path>/sandbox`). Empty turns the API and the router off. |
| `HANGAR_E2B_API_DOMAIN` | unset | The `E2B_DOMAIN` clients use. Turns on `api.<domain>`, `sandbox.<domain>` and `<port>-<id>.<domain>`, and is returned as each sandbox's `domain`. |
| `HANGAR_E2B_TEMPLATES` | `{}` | E2B template name → environment. |
| `HANGAR_E2B_TRUSTED_ENVIRONMENTS` | `[]` | Environments a `sandboxes` token gets with their credentials, secrets and gateway grant. |
| `HANGAR_E2B_MAX_TIMEOUT_SECONDS` | `86400` | Longest timeout; also how long a paused sandbox is kept. |
| `HANGAR_E2B_MAX_SANDBOXES_PER_OWNER` | `20` | Running and paused sandboxes per `sandboxes` token (0 = no cap). |
| `HANGAR_E2B_RATE_PER_MINUTE` / `_BURST` | `600` / `120` | API requests per `sandboxes` token. |
| `HANGAR_E2B_CREATE_PER_MINUTE` / `_BURST` | `30` / `10` | Sandbox creations per `sandboxes` token. |
| `HANGAR_E2B_MAX_STREAMS_PER_SANDBOX` | `256` | Requests in flight through the router, per sandbox. |

`HANGAR_E2B_API_KEY` and `HANGAR_E2B_DOMAIN` are different settings: they configure hangar's own `e2b` *provider*, which runs sandboxes on E2B's cloud.
