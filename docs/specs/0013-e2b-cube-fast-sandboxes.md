# 0013 E2B and CubeSandbox as references: API compatibility and a fast VM tier

!!! note "Design record"
    Written 2026-10-13; **partly built**. The current behaviour is on [e2b.md](../e2b.md), [envd.md](../envd.md) and [microvm.md](../microvm.md); the status line below says what shipped.

Status: partly built (2026-10-15): `hangar-envd`, the E2B-compatible API and the `e2b` provider flavours; the microVM tier on GKE isn't validated live. Was: proposal. · Date: 2026-10-13 · Builds on: 0012 (items 8 and 15, §4), 0004 (performance), 0003 (gateway, egress), 0010 (sessions), 0006 (environments)

The owner wants to use E2B and Tencent's CubeSandbox as references for hangar in three ways:

- their features, API specs, endpoints and design;
- how they make VMs start fast and run light;
- where Rust makes hangar faster and smaller.

This spec reads both projects from source and records the protocol hangar's in-sandbox daemon has to speak. It decides how hangar exposes an E2B-compatible API, how CubeSandbox and self-hosted E2B become hangar providers, and where a microVM tier fits next to the GKE gVisor pool.

Spec 0012 already compared the hosted platforms, measured gVisor checkpoint/restore, and made `hangard` conditional on GKE exec latency (its item 15). This spec does not repeat that work. It goes one level down, into wire protocols, snapshot mechanics and deployment constraints. It also changes one 0012 decision: the in-sandbox daemon is now worth building regardless of GKE exec latency, because it is also the E2B data plane (§1, decision 1).

External claims carry a source tag such as [E2], resolved in §11. A claim marked *measured* comes from runs on this machine (§2). One marked *unverified* is listed in §10.

## 1. Summary: the decisions

1. **Build `hangar-envd` (Rust) as a wire-compatible envd.** This was 0012 item 15; it is now unconditional, and §3 is its contract.
   - **The protocol is small and settled.** It is Connect RPC with the JSON codec over HTTP/1.1. It has 17 RPCs, of which the SDKs call 16, plus `/health` and `/files` [E2][E6].
   - **The SDKs work against it unchanged.** Both E2B SDKs (pip and npm `e2b` 2.56.0) run against any daemon that speaks it. I drove the real SDKs against upstream envd and recorded every call (*measured*, §3).
   - **It is much faster than `docker exec`.** An exec round trip through envd is 4.4 ms p50 against 74.75 ms for hangar's Docker exec (*measured*, [H1]).
   - **It makes hangar look like E2B.** With envd inside every sandbox, a hangar sandbox on Docker, GKE or a microVM answers to the E2B SDK, and decision 3 becomes cheap.
   - **The other agent is already building it.** It lives on branch `overnight/envd`.
2. **The fast part of both projects is restoring a snapshot, not booting, and not the choice of language.**
   - **E2B** (Firecracker, Go) restores pre-booted template snapshots. It loads memory lazily through `userfaultfd` and uses an NBD copy-on-write rootfs [E3].
   - **CubeSandbox** (a Cloud Hypervisor fork in Rust) restores a memory image mapped `MAP_PRIVATE`, so every VM from one template shares its page cache. It clones the rootfs with an XFS reflink and keeps a pre-created pool of TAP devices [C2][C10][C8].
   - **What each sandbox actually costs is dirty pages.** CubeSandbox's "<5 MB overhead" is a marketing figure. Its own benchmark measures 21–26 MB per idle 2 GiB sandbox on bare metal and 27–34 MB on a PVM cloud VM [C8][C9].
   - **Rust is the right language for a VMM and a node agent.** The speed, though, comes from those mechanisms.
3. **hangar gets an E2B-compatible API surface,** mounted at `/e2b` on its own host name, plus a sandbox-URL edge.
   - Template maps to environment, sandbox to sandbox. Pause with `memory=false` maps to session suspend; pause with `memory=true` maps to a provider snapshot. API keys map to hangar tokens with a new `sandboxes` scope.
   - Volumes, secrets, webhooks, MCP gateway, IAM and the template build DSL don't map in the first cut (§8.1).
4. **CubeSandbox and self-hosted E2B become flavours of hangar's `e2b` provider. Neither is a new provider.**
   - **Configuration.** The provider needs an explicit `api_url`, because CubeAPI listens on `:3000`, not `api.<domain>`. It also needs a `flavor`.
   - **Lifecycle client.** It needs its own thin REST client for the lifecycle. The pip SDK from 2.51 on creates sandboxes with `POST /v2/sandboxes`, which CubeAPI rejects with 405; Cube's own examples pin `e2b<2.51` [C7]. hangar requires `e2b>=2.50`.
   - **Security gap.** CubeSandbox returns no envd access token [C6]. hangar must keep its sandboxes private with traffic tokens and network policy.
5. **On GCP, a microVM tier runs in one of two places, and not on GKE bare metal.**
   - **Where it can't run.** GKE does not support `-metal` machine types [G4].
   - **Option A: a GKE node pool with nested virtualization.**
     - C3, C4 and N2 support it, and so do auto-created pools since 1.37.0-gke.4713000 [G2].
     - Its Pods must be privileged.
     - Google states a "10% or greater" CPU penalty, and more for I/O [G1].
   - **Option B: GCE bare-metal instances outside GKE.**
     - c3-standard-192-metal is about $9.68/h, roughly the same per vCPU as VMs (secondary source [G5]).
     - The smallest is 192 vCPUs. That only pays above roughly 100 concurrent sandboxes.
   - **Start with A.** hangar already has an N2 nested-virtualization Kata pool in Terraform [H2].
6. **MicroVM snapshots beat gVisor Pod Snapshots on restore by roughly 10×.**
   - **The numbers.** CubeSandbox creates from a snapshot in 61–68 ms, whatever the dirty size up to 600 MB (bare metal, [C8]). gVisor restore was 0.3–1.25 s locally (0012 §4.3), and GKE documents "a few seconds" [0012 S40].
   - **What to do.** Keep gVisor as the default tier. Add microVMs for forks, best-of-N and RL rollouts, where restore time dominates.
7. **Rust where it sits on a per-request hot path or inside the sandbox; Python everywhere else.**
   - **Rust:** `hangar-envd` first, then a Rust edge for sandbox URLs, then checkpoint capture as an envd RPC.
   - **The git gateway stays Python.** Its latency is GitHub's.
   - **A home-grown node agent comes last.** hangar builds it only if E2B and CubeSandbox both fail as providers.
   - **The boundary is separate binaries speaking HTTP, Connect or stdio, not PyO3.** The hot Python dependencies are already Rust underneath: `cryptography` and the E2B SDK's `pyqwest`.
8. **Phase gates with numbers** (§9).
   - Exec round trip: 75 ms → ≤ 10 ms on Docker.
   - Turn checkpoint: 524 ms → ≤ 300 ms.
   - MicroVM create: ≤ 100 ms p50 / ≤ 250 ms p95 on GCP nested virtualization.
   - Memory fork: N = 10 in ≤ 1 s.

## 2. Method

**Primary sources** were cloned on 2026-10-13. Commit hashes were read from `.git/shallow`.

- `e2b-dev/infra` @ `1c7f6c944858069e061c8aa92650365d496b37f5`. Its README now calls it "E2B Runtime" and links `e2b-dev/runtime`. It contains envd 0.10.0 (`packages/envd/pkg/version.go`) and Firecracker `v1.14-0.2.0` (`packages/shared/pkg/featureflags/flags.go`).
- `e2b-dev/E2B` @ `62a17dfc3f11439d1d06bfb9bb515eadc5adfcd3`. Its `python-sdk` and `js-sdk` are both 2.56.0, which is the latest on PyPI and npm today (`pip index versions e2b`, `npm view e2b version`).
- `TencentCloud/CubeSandbox` @ `8d3d9914dd9d09a9de37f93c4dac703d090fc0a5`, release line v0.7.x.

**Experiments** ran on a shared 2-vCPU, 8 GB VM, so treat the timings as relative. Scripts live in the overnight scratchpad `r13/`; the protocol notes are also in `scratchpad/overnight/envd-protocol.md`.

1. **Built upstream envd.** I compiled it from the commit above (`go build ./packages/envd`) and ran it with `-isnotfc -no-cgroups`.
2. **Recorded the wire traffic.** The real pip `e2b` 2.56.0 and npm `e2b` 2.56.0 SDKs, in `E2B_DEBUG=true` mode, drove every command, file, watch and PTY call through a TCP tap. I ran this over HTTP/1.1 and with the SDK's default HTTP/2 setting.
3. **Probed the encodings.** Raw Connect probes covered errors, enums, int64 values, unknown fields and `Connect-Timeout-Ms`.
4. **Measured round trips.** Exec and file round trips, 50 iterations each.
5. **Compared SDK versions.** I checked which create/connect endpoints `e2b` 2.29.5, 2.44.0, 2.52.1, 2.53.1, 2.55.1 and 2.56.0 call, by reading the wheels.

**GCP facts** come from docs.cloud.google.com, fetched through the agent proxy. One price comes from a secondary source and is marked as such.

**Not done:** no CubeSandbox or E2B cluster ran here. There is no KVM, and the disk is nearly full. The Cube and E2B performance figures are theirs, with their hardware stated. hangar's GKE exec latency is still unmeasured; 0012 had the same gap.

## 3. The envd protocol: what `hangar-envd` must serve

This section is the contract for `overnight/envd`. Everything in it was observed on the wire (§2) unless marked otherwise.

### 3.1 Transport

| Item | Requirement |
| --- | --- |
| Port | `49983`. This is the SDK constant `ConnectionConfig.envd_port`; envd sets it with `-port`. Bind `0.0.0.0`. |
| HTTP version | **HTTP/1.1 is mandatory.** Over plain `http://`, both SDKs speak HTTP/1.1 even with their default `http_version="2"`, because there is no h2c prior knowledge (*measured*). HTTP/2 only appears through ALPN at a TLS edge, so the daemon may skip h2c. |
| Streaming | Server-stream responses are `Transfer-Encoding: chunked`, flushed per message. Python sends streaming requests chunked; JS sends them with `Content-Length`. |
| Timeouts | Set no server read or write timeout (envd uses 0) and a 640 s idle timeout [E2 `main.go`]. Streams live for hours. |
| Compression | RPCs send `Connect-Accept-Encoding: identity`, so never compress RPC responses. `/files` is the exception (§3.5). |
| CORS | `*` origins and headers, with the Connect headers exposed. Only browser use of the JS SDK needs it. |

### 3.2 Connect protocol

**Clients and routes.** Both SDKs use the **Connect protocol with the JSON codec**:

- Python: `connectrpc` 0.11 with a proto-JSON codec, `e2b/envd/client_shared.py`.
- JS: `@connectrpc/connect-web` with `useBinaryFormat: false`, `src/sandbox/index.ts` [E6].

Neither sends gRPC, gRPC-Web or binary proto. Routes are `POST /<package>.<Service>/<Method>`.

**Unary calls:**

- *Request:* `Content-Type: application/json` with the message as the body, plus `Connect-Protocol-Version: 1` and `Connect-Timeout-Ms: 60000`.
- *Success:* `200`, `application/json`, with the message as the body (`{}` when empty).
- *Error:* the HTTP status for the Connect code, with body `{"code":"not_found","message":"…"}`.
- *Codes the SDKs branch on:*
  - `not_found` → 404: `files.exists` returns False, `commands.kill` returns False.
  - `already_exists` → 409: `files.make_dir` returns False.
  - `invalid_argument` → 400, `unauthenticated` → 401, `unavailable` → 503, `deadline_exceeded` → 504, `resource_exhausted` → 429.

**Server streams** (`Start`, `Connect`, `WatchDir`):

- *Request:* `Content-Type: application/connect+json`. The body is **one envelope**: a `0x00` flag byte, a 4-byte big-endian length, then the JSON.
- *Response:* `200` and `application/connect+json`, carrying envelopes `0x00|len|json`.
- *End of stream:* the last envelope has flag `0x02`. Its body is `{}` on success, or `{"error":{"code":"deadline_exceeded","message":"…"}}` on failure.
- *Errors during a stream* travel in that last envelope, with HTTP 200. A stream that fails before its first message, such as `Connect` to an unknown pid, is still a 200 carrying only the error envelope (*measured*).

**Client streaming** (`StreamInput`) is never called. connect-web cannot do it, and the Python SDK doesn't use it.

**Proto3 JSON:**

- Emit lowerCamelCase names (`exitCode`, `watcherId`, `modifiedTime`). Accept those and the proto names on input.
- Omit default values. An exit code of 0 is absent: `{"end":{"exited":true,"status":"exit status 0"}}`.
- `bytes` → padded standard base64.
- `int64` → a JSON **string** (`"size":"5"`). `uint32` and `sint32` → numbers.
- Enums → names (`"SIGNAL_SIGKILL"`, `"FILE_TYPE_FILE"`, `"EVENT_TYPE_CREATE"`). Accept numbers too.
- `Timestamp` → RFC 3339 in UTC with nanoseconds.
- Both SDKs ignore unknown fields: Python through `ignore_unknown_fields`, connect-es by its `ignoreUnknownFields` default.

### 3.3 `process.Process` [E2 `spec/process/process.proto`]

**`Start(StartRequest) returns (stream StartResponse)`.** This is what the SDK sends:
```
commands.run("echo $FOO", envs={"FOO":"bar"}, cwd="/tmp")
  {"process":{"cmd":"/bin/bash","args":["-l","-c","echo $FOO"],"envs":{"FOO":"bar"},"cwd":"/tmp"},"stdin":false}
  headers: Keepalive-Ping-Interval: 50, Connect-Timeout-Ms: 60000
pty.create(PtySize(rows=24, cols=80))
  {"process":{"cmd":"/bin/bash","args":["-i","-l"],"envs":{"TERM":"xterm-256color","LANG":"C.UTF-8","LC_ALL":"C.UTF-8"}},
   "pty":{"size":{"cols":80,"rows":24}}}
```

Behaviour to match:

1. **Event order.** First `{"event":{"start":{"pid":N}}}` as soon as the child forks; the SDK's `request_timeout` covers only this. Then `data` events: `stdout` and `stderr` for pipes, `pty` for a PTY. Then exactly one `end`, then the `0x02` envelope. The `end` payload depends on how the process stopped:
   - **Exit:** `{"exitCode":3,"exited":true,"status":"exit status 3","error":"exit status 3"}`.
   - **Signal:** `{"exitCode":-1,"status":"signal: killed","error":"signal: killed"}`.
   - **Bad `cwd`:** a Connect `invalid_argument` error.
   - **Missing binary:** it still forks, because envd wraps the command in `/bin/sh -c`, and ends with 127.
2. **Keepalive.** Send `{"event":{"keepalive":{}}}` after `Keepalive-Ping-Interval` seconds with no data. The SDK sends 50; envd defaults to 90. Data resets the timer.
3. **The process outlives its stream.** A disconnect doesn't kill it; only `SendSignal` or the deadline does.
4. **`Connect-Timeout-Ms` on `Start` is the process's lifetime.** On expiry envd kills the process and ends the stream with `deadline_exceeded` (*measured*). The SDK sends 60000 for `commands.run` unless the caller passes `timeout=0`. hangar's long agent commands must pass 0.
5. **Environment.** Later entries win:
   1. `PATH` from the daemon;
   2. `HOME`, `USER` and `LOGNAME` of the run-as user;
   3. the sandbox default env;
   4. the request's `envs`.
   Nothing else is inherited.
6. **`cwd`.** Use the request's (with `~` expanded, and relative paths resolved against home), else the default workdir, else the user's home.
7. **User.** It comes from `Authorization: Basic base64("<user>:")`. When the header is absent, use the default user: envd's compiled default is `root`, and E2B templates use `user`.
8. **stdin.** An absent `stdin` means **true**, for backward compatibility. The SDKs send `false` explicitly, and the child then gets `/dev/null`.
9. **Chunk sizes.** envd reads 32 KiB from pipes and 16 KiB from a PTY. Any size works.
10. **`tag`.** It names a process for `Connect` and `SendSignal`. `_system` is reserved.
11. **OOM wrapper.** envd sets `oom_score_adj` 100 and `ionice`/`nice` on children through a `/bin/sh -c` wrapper. This is optional, but it keeps the daemon alive under OOM.

**`Connect`.** It takes `{"process":{"pid":N}}` or `{"process":{"tag":"t"}}`. It sends `start{pid}`, then live data and keepalives, then `end`. Earlier output is not replayed. If the process exited less than 30 s ago, `Connect` returns only its `end` event; otherwise it returns `not_found`.

**Unary RPCs:**

| RPC | SDK call | Request (from the tap) | Behaviour |
| --- | --- | --- | --- |
| `List` | `commands.list()` | `{}` | `{"processes":[{"config":{…},"pid":N,"tag":"…"}]}` |
| `SendInput` | `commands.send_stdin`, `pty.send_stdin` | `{"process":{"pid":N},"input":{"stdin":"<b64>"}}` or `{"input":{"pty":"<b64>"}}` | Writes the bytes. Sending stdin to a PTY process, or pty input to a pipe process, is an error. |
| `CloseStdin` | `commands.close_stdin` | `{"process":{"pid":N}}` | EOF. Fails on a PTY; send `0x04` instead. |
| `SendSignal` | `commands.kill`, `pty.kill` | `{"process":{"pid":N},"signal":"SIGNAL_SIGKILL"}` | Only SIGTERM (15) and SIGKILL (9) are defined. |
| `Update` | `pty.resize` | `{"process":{"pid":N},"pty":{"size":{"cols":100,"rows":30}}}` | `TIOCSWINSZ` |
| `StreamInput` | none | n/a | Skip it. |

### 3.4 `filesystem.Filesystem` [E2 `spec/filesystem/filesystem.proto`]

Paths expand `~` and resolve relative to the user's home. New entries are owned by that user.

| RPC | SDK call | Behaviour |
| --- | --- | --- |
| `Stat` | `files.get_info`, `files.exists` | Returns `{"entry":EntryInfo}` using `lstat`; `not_found` when missing. |
| `ListDir` | `files.list(path, depth=1)` | A depth of 0 means 1. Does not follow symlinks. Returns `not_found`, or `invalid_argument` when the path is not a directory. |
| `MakeDir` | `files.make_dir` | `mkdir -p`. Returns `already_exists` for an existing directory and `invalid_argument` for an existing file. |
| `Move` | `files.rename` | Creates the destination's parent directories, then calls `rename(2)`. Returns the new entry. |
| `Remove` | `files.remove` | `rm -rf` semantics; a missing path is not an error. |
| `WatchDir` (stream) | async Python `files.watch_dir(on_event=…)`, JS `files.watchDir` | `{"path","recursive","includeEntry","allowNetworkMounts"}`. Sends `{"start":{}}`, then `{"filesystem":{"name":"aw.txt","type":"EVENT_TYPE_CREATE"}}`, with keepalives between. `name` is relative to the watched directory. |
| `CreateWatcher`, `GetWatcherEvents`, `RemoveWatcher` | **sync** Python `watch_dir` / `get_new_events()` / `stop()` | Returns `{"watcherId":…}`; draining returns `{"events":[…]}`. |

`EntryInfo` as observed: `{"name":"a.txt","type":"FILE_TYPE_FILE","path":"/tmp/hx/a.txt","size":"5","mode":420,"permissions":"-rw-r--r--","owner":"root","group":"root","modifiedTime":"2026-10-13T16:13:31.282056477Z"}`. When they apply it also carries `symlinkTarget`, `isSymlink` and `metadata` (the `user.e2b.*` xattrs).

One file write produced CREATE, CHMOD and WRITE events. A recursive watch needs an inotify watch per directory.

### 3.5 Plain HTTP endpoints [E2 `spec/envd.yaml`]

| Route | Used by | Behaviour |
| --- | --- | --- |
| `GET /health` | `is_running` and error probes | `204`, no auth. A 502 *from the proxy* tells the SDK the sandbox is not running. |
| `GET /files?path=&username=` | `files.read` | Raw bytes with `Content-Disposition: inline; filename=…`. Honours `Accept-Encoding: gzip`; the SDKs send `gzip, deflate`, and envd answers with `Content-Encoding: gzip`. Errors are JSON `{"code":<int>,"message":…}`: 400 for a directory, 401 for an unknown user, 404, and 406 for a bad encoding. |
| `POST /files?path=&username=` | `files.write`, `files.write_files` | Two body types are accepted. **Multipart:** every part is named `file`, and its `filename` is the full target path in both SDKs; with a single part, `?path=` wins. **`application/octet-stream`:** the raw body, which needs `?path=` and allows `Content-Encoding: gzip`. Either way it creates parent directories, overwrites, and sets ownership to the user. `X-Metadata-<k>` headers become `user.e2b.<k>` xattrs. Returns `200 [{"path","name","type":"file"}]`, or 507 when the disk is full. |
| `GET /metrics`, `GET /envs`, `POST /files/compose` | not on the SDK's main path | Optional. |
| `POST /init` | E2B's orchestrator only | Sets the token, env, default user and workdir, and the clock. hangar can use its own boot path. The proxy must never forward `/init` from outside; E2B's proxy builds its deny list from the operations marked `x-internal`. |
| `/freeze`, `/unfreeze`, `/fsfreeze`, `/fsthaw`, `/collapse`, `/upgrade` | E2B's orchestrator around a snapshot | Skip them for the SDK. `/freeze` (freezing cgroups before a snapshot) is worth copying for the microVM tier (§6). |

### 3.6 Auth

- **Access token.** Clients send `X-Access-Token: <envdAccessToken>`. Once a token is set, every route except `GET /health`, `GET /files`, `POST /files` and `POST /init` must present it, or the request fails with `401 {"code":401,…}` [E2 `internal/api/auth.go`]. Compare in constant time. hangar sets the token at boot; envd stays open until `/init` sets one.
- **`/files` signatures.** These are for pre-signed URLs, and are accepted when the header token is absent: `"v1_" + base64.RawStd(sha256("<path>:<read|write>:<username>:<token>[:<expiration>]"))`, with an optional `signature_expiration` in unix seconds. A header token, if present, must match.
- **User selection is not authentication.** `Authorization: Basic base64("user:")` only picks the user to run as.
- **Routing headers.** Every request carries `E2b-Sandbox-Id` and `E2b-Sandbox-Port`, for the edge (§4.3). The daemon ignores them.

### 3.7 Version gating, the conformance harness, and the numbers

**Version gating.** The SDK takes `envdVersion` from the **control API** and gates features on it:

- `0.3.0`: stdin.
- `0.4.0`: default user. Below it the SDK forces `user`.
- `0.5.2`: `CloseStdin`.
- `0.5.7`: octet-stream upload.
- `0.6.2`: metadata.
- `0.6.3`: entry info in events.
- `0.6.4`: watching network mounts.

These are defined in `e2b/envd/versions.py` and `js-sdk/src/envd/versions.ts` [E6]. hangar's control API should report the highest version whose features `hangar-envd` fully implements; `0.6.4` covers everything 2.56 uses.

**Conformance harness.** `E2B_DEBUG=true` makes both SDKs skip the control plane and talk to `http://localhost:49983`. `Sandbox.create()` then returns `sandbox_id="debug_sandbox_id"` with `envd_version=99.99.99` and no token. So the real SDKs are the conformance suite, with no API server needed.

**Minimum surface.**

- *Must:* the transport and codec above; the process RPCs except `StreamInput`; all the filesystem RPCs; `/health`; `/files`, both directions, with gzip; `X-Access-Token`; Basic-auth user selection; keepalives; the error codes.
- *Should:* signatures, `X-Metadata-*`, `includeEntry`, the deadline semantics and the 30 s exit retention.
- *Skip:* `StreamInput`, `/files/compose`, `/init` and the freeze/upgrade routes.

**Reference numbers** (*measured*: upstream Go envd on loopback, no VM boundary, 50 runs each):

| Path | p50 | p95 |
| --- | --- | --- |
| Raw Connect `Start` `/bin/true` (HTTP/1.1 keep-alive) | 4.4 ms | 6.9 ms |
| Raw Connect `Start` `sh -c true` | 5.3 ms | 9.1 ms |
| SDK `commands.run("true")` (`bash -l -c`) | 18.0 ms | 25.0 ms |
| SDK `files.read` (small file) | 2.2 ms | 10.2 ms |
| hangar Docker `exec`, for comparison [H1] | 74.75 ms | 94.12 ms |
| hangar Docker `terminal_send` [H1] | 203.83 ms | 226.26 ms |

Upstream envd is a 16 MB binary and used 23 MB RSS after the probes (*measured*). Most of the SDK's 18 ms is `bash -l` start-up. Targets for `hangar-envd`: `/bin/true` round trip p50 ≤ 4 ms, idle RSS ≤ 8 MB, binary ≤ 8 MB.

## 4. The E2B control API

Source: `spec/openapi.yml`, version 0.1.0, server `https://api.e2b.app` [E1]. Request and response fields are listed for what hangar maps. Deprecated v1 routes are noted; the 2.56 SDK no longer calls them.

### 4.1 Auth

| Scheme | Header | Used for |
| --- | --- | --- |
| `ApiKeyAuth` | `X-API-Key: e2b_…` | Everything the SDKs call. The Python SDK sends `X-API-KEY` [E6 `api/__init__.py`]. |
| `AuthProviderBearerAuth` + `AuthProviderTeamAuth` | `Authorization: Bearer <access_token>` + `X-Team-ID` | The dashboard and CLI login. It is the only way to create API keys (`POST /api-keys`). |
| `AdminApiKeyAuth` / `AdminJWTAuth` + `AdminTeamAuth` | `X-Admin-Token`, or a Bearer JWT, plus `X-Team-ID` | `/nodes`, `/admin/*`, `/clusters/*` |

Per-sandbox tokens come back from create and connect:

- **`envdAccessToken`**, sent to envd as `X-Access-Token` (§3.6).
- **`trafficAccessToken`**, required by the edge when `network.allowPublicTraffic=false`, in the `e2b-traffic-access-token` header [E5 `grpc/proxy/metadata.go`]. The client proxy also forwards `X-Access-Token` when it auto-resumes a paused sandbox [E5 `client-proxy/internal/proxy/proxy.go`].

### 4.2 Endpoints

**Sandboxes.**

| Method and path | Request | Response | Semantics |
| --- | --- | --- | --- |
| `POST /v2/sandboxes` | `NewSandboxV2`: `templateID`*, `timeout` (s, default 300), `autoPause`, `autoPauseMemory` (default true), `autoResume{enabled}`, `allow_internet_access`, `network`, `metadata`, `envVars`, `mcp`, `iam`, `volumeMounts`; header `Idempotency-Key` | 201 `Sandbox`: `templateID`, `sandboxID`, `alias`, `clientID`, `envdVersion`, `envdAccessToken`, `trafficAccessToken`, `domain` | Creates from a template. The SDK uses it from 2.51 on (§5.4). `POST /sandboxes` is deprecated (default timeout 15 s). |
| `GET /v2/sandboxes` | query `metadata`, `state` (`running`/`paused`), `order`, `startedAfter`, `template`, `nextToken`, `limit` | 200 `[ListedSandbox]`: ids, `startedAt`, `endAt`, `cpuCount`, `memoryMB`, `diskSizeMB`, `metadata`, `state`, `envdVersion`, `volumeMounts` | Paginated list. `GET /sandboxes` is deprecated. |
| `GET /sandboxes/{id}` | | 200 `SandboxDetail` = Listed + `envdAccessToken`, `domain`, `allowInternetAccess`, `network`, `lifecycle{autoResume,onTimeout: kill\|pause}`, `pauseMode: full\|filesystem` | |
| `DELETE /sandboxes/{id}` | | 204 | Kill. |
| `POST /sandboxes/{id}/timeout` | `{timeout}`* | 204 | Sets the expiry to *now + timeout*; it can shorten. |
| `POST /sandboxes/{id}/refreshes` | `{duration}` | 204 | Extends the TTL. |
| `POST /sandboxes/{id}/pause` | `{memory}` (default true) | 204; 409 if not running | `memory=false` keeps only the filesystem. Resume cold-boots, and auto-resume refuses such a sandbox. |
| `POST /v2/sandboxes/{id}/connect` | `{timeout}` (default 300), `memory` | 200 when running, 201 when resumed: `Sandbox` | Returns the tokens. Resumes if paused; the TTL is only ever extended. `memory=false` resumes from disk only and is "rejected with an error … never silently downgraded". `/resume` and the v1 `/connect` are deprecated. |
| `POST /sandboxes/{id}/fork` | `{timeout, count}` | 201 `[{sandbox}\|{error}]` | Checkpoints the source in place, which pauses it briefly. Then creates `count` sandboxes from one snapshot, each succeeding or failing on its own. The README says up to 100 per request [E3]. |
| `POST /sandboxes/{id}/snapshots` | `{name, memory}` | 201 `SnapshotInfo{snapshotID, names}` | A persistent snapshot, usable as a template. The source keeps running. |
| `GET /snapshots` | `sandboxID`, `name`, `limit`, `nextToken` | `[SnapshotInfo]` | |
| `GET /sandboxes/{id}/metrics`, `GET /sandboxes/metrics?sandbox_ids=` | `start`, `end` | `[SandboxMetric{timestamp, cpuCount, cpuUsedPct, memUsed, memTotal, memCache, diskUsed, diskTotal}]` | |
| `GET /v2/sandboxes/{id}/logs` | `cursor`, `limit`, `direction`, `level`, `search` | `{logs:[SandboxLogEntry]}` | v1 is deprecated. |
| `PUT /sandboxes/{id}/network` | `allowOut`, `denyOut`, `egressProxy` (SOCKS5), `rules`, `allow_internet_access` | 204 | Changes egress live. **`rules`** are per-domain header transforms on outbound HTTPS, i.e. credential injection: exact or `*.` suffix keys, exact match wins, and they "do not grant network access". This is 0012 item 7's design, already shipping at E2B. |

**Templates and builds.**

| Method and path | Request | Response | Semantics |
| --- | --- | --- | --- |
| `POST /v3/templates` | `name` (may carry `:tag`), `tags`, `teamID`, `cpuCount`, `memoryMB`, `minFreeDiskMb` | 202 `{templateID, buildID, public, names, tags, aliases}` | Registers a build. |
| `GET /templates/{id}/files/{hash}` | | 201 `{present, url}` | Upload URL for a `COPY` step's tarball, keyed by SHA-256. |
| `POST /v2/templates/{id}/builds/{buildID}` | `TemplateBuildStartV2`: `fromImage` \| `fromTemplate` \| `fromImageRegistry` (AWS/GCP/general), `steps[{type,args,filesHash,force}]`, `startCmd`, `readyCmd`, `force` | 202 | Starts the build. Layers are cached per step hash; the start/ready commands run before the template snapshot is taken. |
| `GET /templates/{id}/builds/{buildID}/status` | `logsOffset`, `limit`, `level` | `{status: building\|waiting\|ready\|error, logEntries, reason}` | |
| `GET /templates/{id}/builds/{buildID}/logs` | `cursor`, `limit`, `direction`, `level`, `source` | | |
| `GET /v2/templates`, `GET /templates/{id}` | | `[Template]`, or `TemplateWithBuilds` | `GET /templates` is deprecated. |
| `DELETE /templates/{id}`, `PATCH /v2/templates/{id}` | `{public}` | | |
| `POST`, `DELETE /templates/tags`; `GET /templates/{id}/tags`; `GET /templates/aliases/{alias}` | `{target:"name:tag", tags}` | | Tags are movable pointers to builds. |

**Everything else.**

- `GET /teams`; `GET /teams/{id}/metrics` and `/metrics/max`.
- `GET`, `POST /api-keys`, plus `PATCH` and `DELETE /api-keys/{id}`. Bearer only: an API key can't mint keys.
- `GET`, `POST`, `GET {id}` and `DELETE /volumes` (NFS-backed; envd mounts them).
- `/secrets` (metadata only on the control plane [E3]).
- `/events/sandboxes`, `/events/webhooks` and its deliveries and stats.
- Admin routes: `/nodes`, `/admin/*`, `/clusters/*/rigs`.
- `GET /health` → 204.

### 4.3 Sandbox URLs and the edge

- **URL format.** The SDK builds `https://{port}-{sandboxID}.{domain}` [E6 `connection_config.py: get_host`]. For E2B's own domains (`e2b.app`, `e2b.dev`, `e2b.pro`, `e2b-staging.dev`) it sends envd traffic to the shared host `https://sandbox.{domain}` and routes with the `E2b-Sandbox-Id` and `E2b-Sandbox-Port` headers. For any other domain, which is how hangar and CubeSandbox are reached, it uses the per-port host [E6 `sandbox_domains.py`].
- **How the edge parses it.** The proxy reads headers only on the shared host or on localhost and IP hosts. Otherwise it parses the first DNS label as `<port>-<id>` [E5 `proxy/host.go`].
  - E2B sandbox IDs match `^[a-z0-9]+$`.
  - hangar's IDs look like `sb-1a2b3c4d`, so hangar's edge must split the label at the *first* `-` only.
- **`domain` in the response.** The API's `Sandbox.domain` is set only for bring-your-own-cloud clusters. The SDK falls back to `E2B_DOMAIN`, then to `api.<domain>` for the API [E3].
- **SDK overrides.** `E2B_API_URL` overrides the API base and `E2B_SANDBOX_URL` the envd base [E6]. Both are global to the process.

## 5. CubeSandbox: what it actually is

### 5.1 Components and their real languages

Lines are counted per directory in the repo [C1]; vendored `vmlinux.h` is excluded from CubeNet.

| Component | Language | What it is |
| --- | --- | --- |
| CubeAPI | **Rust** (Axum), 14k lines | E2B-compatible REST gateway. It translates to gRPC for CubeMaster [C2]. |
| CubeMaster | **Go**, 91k | Cluster scheduler; publishes lifecycle events to Redis. |
| Cubelet | **Go**, 147k | Node agent: lifecycle, containerd integration, CubeCoW calls, a TAP pool. |
| CubeShim | **Rust**, 41k | containerd **Shim v2** runtime (`containerd-shim-cube-rs`): prepares the rootfs and memory file, then boots or restores the VM, over vsock. |
| CubeHypervisor | **Rust**, 136k | **A fork of Cloud Hypervisor**: crate `cube-hypervisor` 28.0.0, with the Cloud Hypervisor README, CREDITS and BSD-3/Apache licences kept [C3]. CH itself is built on rust-vmm, and credits Firecracker and crosvm. It is not Firecracker and not Dragonball. |
| cube-agent (`agent/`) | **Rust**, 30k | In-VM PID 1, **derived from the Kata Containers agent**, with Cube's own ttrpc protocol [C4]. |
| envd | **Go**, unmodified E2B | Built from `e2b-dev/infra` tag `2026.16` into the `cubesandbox-base` image, and started with `-isnotfc` by `cube-entrypoint.sh` [C5]. CubeSandbox's E2B data plane *is* E2B's envd, running inside the container that runs inside the VM. |
| CubeCoW | **Rust**, 10k | Volume engine: `FICLONE` reflinks on XFS. |
| CubeNet / CubeVS | **eBPF C** (~2k) + **Go** (11k) | Three TC programs plus their maps; the Go side is the control library. |
| CubeProxy, CubeEgress | **OpenResty** (nginx + Lua) | Sandbox URL routing; the L7 egress proxy with TLS MITM and header injection. |
| CubeOps, TemplateCenter, lifecycle-manager | **Go** | Operations, template distribution, auto-pause and auto-resume. |

So articles that call CubeSandbox "Rust" or "Go" are each half right. Rust covers the VMM, the shim, the guest agent, the API gateway and the storage engine. Go covers the scheduler, the node agent and the network control plane.

### 5.2 How it is fast and light

1. **Create means restore.** A template is an OCI image turned into a rootfs, cold-booted once and memory-snapshotted. A sandbox is a clone of that [C2].
2. **Memory is shared through the page cache.** On restore the VMM maps the memory image `mmap(MAP_PRIVATE, fd)`. That sets up the mapping without reading any data. Pages the guest only reads stay page-cache pages shared by every VM from the same image; a write copies one page into private anonymous memory [C10]. One VM's real cost is therefore its dirty pages plus the VMM process.
3. **Incremental snapshots.** A later snapshot writes only the pages marked soft-dirty since the last one (`/proc/PID/clear_refs`), into a file that is a reflink of the previous snapshot [C10]. The measured snapshot time is 46 ms at 7 MB dirty and 289 ms at 602 MB dirty. Create-from-snapshot stays at 61–68 ms at every dirty size [C8].
4. **Disk uses XFS reflink.** `FICLONE` copies only metadata, and blocks split on write. Snapshots are flat siblings, so deleting one never touches another [C10].
5. **The network is ready before the VM.** Cubelet pre-creates TAP devices (`tap_init_num`, default 500) [C8]. Every guest has the same internal IP, `169.254.68.6`, behind a per-TAP eBPF NAT [C2 `network.md`], so a restored or cloned VM needs no network reconfiguration.
6. **eBPF instead of iptables.** There are three programs:
   - `from_cube` on each TAP: policy, DNS snooping, SNAT and an ARP proxy;
   - `from_world` on the host NIC: reverse NAT;
   - `from_envoy` on `cube-dev`: transparent-proxy return traffic.

   They provide LPM-trie CIDR allow and deny lists, domain rules learned from DNS answers, and 5-tuple connection tracking. Private and link-local ranges are denied by default. HTTP(S) can be sent by TPROXY to CubeEgress, which filters by SNI, host, method and path, injects `Authorization`, and audits to JSONL. The template trusts a CubeEgress root CA [C2].
7. **What makes E2B fast, for comparison** [E3]:
   - Firecracker restore with `userfaultfd` lazy memory served from the template's memfile, plus a prefetch list recorded during the template build;
   - an NBD copy-on-write rootfs served in user space by the orchestrator;
   - pooled, reused network slots: a network namespace, veth and TAP per sandbox, with NAT and per-slot nftables (`docs/ARCHITECTURE.md`, "Networking");
   - 2 MiB hugepages, which are required (`HUGEPAGES`, 4 GiB in Embed [E4]).

   A pause ships memory and disk diffs to object storage, and resume prefers the node that still caches them.

   The trade-off between the two designs:
   - **E2B** fetches lazily from object storage, so a template or snapshot can resume on any node.
   - **CubeSandbox** relies on local page cache and reflink, which is faster on one node. Cross-node needs its S3 backend (v0.7, preview [C1]).

### 5.3 Deployment requirements

| | CubeSandbox [C9][C12] | E2B Embed / self-host [E4] |
| --- | --- | --- |
| CPU and virtualization | x86_64 with `/dev/kvm`, *or* any x86_64 cloud VM after installing Tencent's **PVM** host kernel (shadow-paging nested virtualization, no VT-x needed in the guest). arm64 needs bare metal. | x86_64 or arm64 with `/dev/kvm`: nested virtualization on a VM, or metal. arm64 needs kernel ≥ 6.10. |
| OS | glibc ≥ 2.31; XFS with reflink for `/data/cubelet`; OpenCloudOS 9 recommended for PVM | Ubuntu 24.04 or kernel ≥ 6.8, cgroup v2, **not Container-Optimized OS**; 2 MiB hugepages reserved before the kubelet starts |
| On Kubernetes | Helm: four DaemonSets (the Big Pod node agent, an installer, bootstrap, and PVM), `hostNetwork: true` | A one-node StatefulSet, `hostNetwork`, privileged init containers, NBD |
| GCP default | none | Terraform on `n4-standard-4` with `enable_nested_virtualization = true` |

Neither runs as an unprivileged Pod. Both need host-level setup: KVM, hugepages or XFS, NBD or TAP devices.

### 5.4 How faithful its E2B API is

CubeAPI [C6] implements:

- `GET /health`; `GET`/`POST /sandboxes`; `GET /v2/sandboxes`;
- `GET`/`DELETE /sandboxes/{id}`; v1 and v2 logs;
- the sandbox actions `network`, `timeout`, `refreshes`, `pause`, `resume`, v1 `connect` and `snapshots`;
- `GET /snapshots`; templates (Cube's own `CreateTemplateRequest`, aliases, builds, status and logs); volumes.

It adds `rollback`, `templates/compat` and `adopt-baseline`.

Gaps, against the 2.56 SDK:

- **`POST /v2/sandboxes` and `POST /v2/sandboxes/{id}/connect` are missing.** From 2.51 on, pip `e2b` calls only these. I checked the wheels: 2.52.1, 2.53.1, 2.55.1 and 2.56.0 call v2, while 2.29.5 and 2.44.0 call v1 (§2, item 5). The 2.51 cut-off is from Cube's note. Cube's own example says create "gets 405 (that route is GET-only)" and pins `e2b>=2.7.0,<2.51` [C7]. So `Sandbox.create()` from a current SDK fails against CubeSandbox at this commit.
- **No fork, `/v3/templates`, step-based builds, file-hash uploads, tags, API keys, secrets, events or webhooks.** Template builds are image-based: `image`, CPU in millicores, `writableLayerSize`, `exposedPorts`, `probePort`. hangar's `Template.build` path (`from_image` → `/v3/templates` + `/v2/.../builds`) won't work, so it needs a Cube branch.
- **No envd access token.** `envdAccessToken` is always `None` [C6 `services/sandboxes.rs:146,663`]. envd inside CubeSandbox is unauthenticated, and CubeProxy enforces a traffic token only when `allowPublicTraffic=false` [C11 `sandbox_backend.lua`].
- **Auth is off by default.** With `AUTH_CALLBACK_URL` set, CubeAPI POSTs the caller's `Authorization: Bearer` or `X-API-Key`, with the path and method, to a callback, and allows the request only on a 200 [C11]. hangar can be that callback (§8.3).
- **The data plane is faithful by construction,** since it is E2B's envd.

### 5.5 Benchmarks: what was measured, and on what hardware

| Report | Hardware | Method | Result |
| --- | --- | --- | --- |
| Bare metal [C8] | Tencent BMI5: 2× Xeon Platinum 8255C, 96 threads, 375 GiB, NVMe XFS, kernel 6.6.119 | `cube-bench` (Go) calls CubeAPI `POST /sandboxes` until the sandbox is running. Sandbox 2 vCPU / 2 GiB; 3 warm-up rounds discarded. | **Concurrency 1:** avg 47.8, p95 57.4, max 60.4 ms. **10:** avg 88.7, p95 116.9 ms. **20:** avg 98.1, p95 175.8 ms. **50:** avg 276.1, p95 508.4, max 681.3 ms. 100% success. |
| Density [C8] | same host | `free -h` before and after N create-only sandboxes, idle | **21.5 MB per VM at 100, 25.7 MB at 1,000.** 1,000 VMs used about 25 GiB. |
| PVM cloud VM [C9] | Tencent SA9.4XLARGE32: AMD EPYC 9K65, 16 cores, 32 GB, PVM host kernel | same | **Concurrency 1:** avg 66.7, p95 78.2 ms. **10:** avg 171 ms. **20:** avg 365 ms, p99 674 ms. **Density:** 27–34 MB per VM. |
| Snapshots [C8] | BMI5 | Python scripts in `examples/snapshot-rollback-clone` | Snapshot 50 ms serial; 12.7 ms amortized at concurrency 10. Create from snapshot 61–68 ms. |

Where the claims and the data disagree:

- **Overhead.** The "<5 MB overhead" in the README and introduction has no method behind it. The repo's own density runs measure 21–34 MB per idle 2 GiB VM.
- **Concurrency.** The README says "under 50 concurrent creations, avg 67 ms, P95 90 ms" [C1]. The bare-metal report measures avg 276 ms and p95 508 ms at concurrency 50.
- **Hardware.** All the fast numbers are on a 96-thread bare-metal host or a PVM kernel.

Plan on about 50–70 ms per create at low concurrency and 25–35 MB per idle VM. None of this has been reproduced on GCP.

## 6. Where a microVM tier fits on GCP and GKE

### 6.1 Where KVM is available

- **GCE nested virtualization** [G1]. KVM is the only supported L1 hypervisor. It is not available on E2, memory-optimized, Arm, or AMD series except N4D. The stated cost is "10% or greater decrease … CPU-bound, possibly greater … I/O bound".
- **GKE nested virtualization** [G2].
  - *How to enable it:* `gcloud container node-pools create … --enable-nested-virtualization`, or `enableNestedVirtualization: true` in a ComputeClass (needs 1.37.0-gke.4713000+; works with auto-created pools and Autopilot). Google's example ComputeClass uses `c3` Spot, then `c4`, then `c4d` Spot, on `cos_containerd`.
  - *Limits:* it can't be turned on for an existing pool, the setting is immutable, and Pods need `privileged: true`. Autopilot allows privileged Pods only through a `WorkloadAllowlist` or for approved partners.
- **Bare metal** [G3]. `c3-{highcpu,standard,highmem}-192-metal` (192 vCPU with 512, 768 or 1,536 GB), `c4-*-288-metal`, `c4d-*-384-metal` and `c4a-*-96-metal`. Each needs a dedicated host, uses Hyperdisk only, and has no live migration (`TERMINATE` on maintenance). **GKE does not support `-metal` machine types** [G4].
- **Price.** A secondary source gives c3-standard-192-metal at $9.68–12.43/h on demand by region (≈ $7,064/month at the low end) [G5]. That is about $0.050 per vCPU-hour with 4 GB, the same as 0012's GKE estimate for N2. GCE bills metal by vCPU and memory, like VMs.
- **hangar today** [H2].
  - *Kata pool:* `deploy/terraform/gke` can add an Intel N2 Ubuntu nested-virtualization pool for Kata.
  - *GKE add-on runtime classes:* the Agent Sandbox add-on allows only the `gvisor` and `microvm` RuntimeClasses. `microvm` is GKE's managed Kata on Cloud Hypervisor, but node pools can't be created with it yet (gcloud 588).
  - *Measured start times:* local Kata creates in about 2 s, and GKE gVisor warm claims take 1.5 s, cold 17 s.

### 6.2 Deployment options for the tier

| Option | How | For | Against |
| --- | --- | --- | --- |
| **A. GKE nested-virtualization pool** (recommended first) | A new node pool (C3 or N2 Intel, Ubuntu image for E2B; COS works for Kata) tainted `hangar.dev/runtime=microvm`. It runs CubeSandbox's node DaemonSets or E2B's orchestrator as privileged `hostNetwork` Pods. hangar's control plane talks to that stack's API through the `e2b` provider (§8.2). | Same cluster, VPC, Workload Identity and autoscaler. Spot works. Terraform already has the pattern. | 10%+ nested penalty. Privileged Pods weaken the node boundary, so the pool must be dedicated. E2B needs a non-COS image and hugepages before the kubelet starts. CubeSandbox needs XFS for `/data/cubelet`. PVM is impossible because GKE doesn't allow a custom kernel. |
| **B. GCE bare-metal MIG outside GKE** | A managed instance group of `c3-standard-192-metal` running E2B Embed or CubeSandbox (or, later, a hangar node agent) directly. hangar reaches it over the VPC. | No nested penalty; the published numbers apply; very high density (CubeSandbox: 1,000 idle 2 GiB VMs in 25 GiB). | A 192-vCPU minimum (~$7k/month). No GKE scheduling, so it is a second fleet to run. TERMINATE on maintenance. |
| **C. GKE managed `microvm` RuntimeClass** | Wait for node pools with `--sandbox type=microvm`. | Google runs it. | Not available; restore speed and snapshot API unknown. |

**Comparison with the gVisor pool and Pod Snapshots.**

| | GKE gVisor (default) | GKE Pod Snapshots (gVisor) | MicroVM tier (A or B) |
| --- | --- | --- | --- |
| Isolation | user-space kernel | same | hardware VM, own kernel |
| Create | 1.5 s warm claim, 17 s cold (hangar, measured 2026-10-11) | n/a | CubeSandbox 48 ms bare metal / 67 ms PVM [C8][C9]; GCP nested is *unverified* |
| Restore or fork | n/a | 0.3–1.25 s for 256 MB locally (0012 §4.3); "a few seconds" on GKE; fan-out to N Pods *unverified* | 61–68 ms per clone, flat in dirty size [C8]. E2B fork: up to 100 per request from one checkpoint [E3]. |
| Memory per idle sandbox | the Pod's working set | each restore holds its own copy (0012 §4.3, 512 MB case) | dirty pages + VMM, 21–34 MB [C8][C9]; shared page cache |
| Operating cost | managed | managed | a privileged stack hangar must run |

**Decision.** Keep gVisor as the default tier: it is managed, needs no privilege, and is fast enough for single sessions. Add the microVM tier for forks, best-of-N and RL rollouts (0008), where per-clone restore time and memory sharing dominate. hangar selects it with the env's `runtime: microvm`.

## 7. Rust in hangar: where it pays

| Candidate | Today | Expected win | Footprint | Effort | Decision |
| --- | --- | --- | --- | --- | --- |
| **`hangar-envd`**: in-sandbox process, file and PTY daemon (§3) | Docker exec p50 74.75 ms; `terminal_send` 204 ms; GKE exec goes through the API server and kubelet and is unmeasured | Exec ≤ 10 ms on Docker (upstream envd: 4.4 ms raw). Terminal input in a few ms through `SendInput` instead of an exec. GKE exec stops loading the API server. **E2B SDK compatibility on every provider.** | ≤ 8 MB RSS and ≤ 8 MB static binary, against Go envd's 23 MB RSS and 16 MB binary | M (in progress) | **Build now.** |
| **Edge proxy** for `{port}-{id}.{domain}` and envd routing (§8.1) | none: hangar has no sandbox URLs | A new capability. Every SDK call passes through it, so per-request overhead and WebSocket and HTTP/2 fan-out matter. hyper plus rustls handles many long-lived streams with little memory. | one small binary | M | **Build after the E2B API**, as one binary: TLS (h2 via ALPN) → route → traffic-token check → upstream envd over HTTP/1.1. Auto-resume and the gateway listener (:8788) can join it later. |
| **Checkpoint capture and restore** (`ckpt.py`) | `checkpoint_turn` p50 524 ms: 2 execs = 452 ms, 3 encrypts = 40 ms, 3 puts = 19 ms [H1]; in-sandbox tar + `gzip -1` + `git bundle` | As an envd RPC (`hangar.Checkpoint/Capture`, streaming): no exec round trips, multithreaded zstd-1 instead of gzip-1, hashing while streaming. Estimate 524 → ≤ 250 ms. *Unverified* until built: zstd-1 is commonly 3–5× faster than gzip-1. | inside envd | M | **Build after envd.** Keep the control-plane side in Python: `cryptography` is already Rust/OpenSSL, and 40 ms for three encrypts is not the bottleneck. |
| **Git gateway** (`gateway.py`, smart-HTTP inspection) | Python | Latency is GitHub's. CPU only matters on very large pushes. It is security-critical code with tests. | | L | **Keep Python.** Revisit only if profiling shows its CPU on big monorepo pushes. |
| **Node agent for a snapshot-clone pool** (Firecracker or Cloud Hypervisor) | none | This is where Rust pays most: a VMM controller, reflink or NBD storage, UFFD or `MAP_PRIVATE` memory, an eBPF network. It is also what E2B (Go) and CubeSandbox (Go + Rust) already are. | | XL (weeks) | **Don't build yet.** Adopt E2B or CubeSandbox through the provider (§8.2). Build `hangar-node` only if both fail a hard requirement: gateway integration, harness packs as virtio-fs volumes, or GKE-native scheduling. If it is built, copy CubeSandbox's design (`MAP_PRIVATE` memory image, XFS reflink, fixed guest IP behind a per-TAP NAT, a pre-created TAP pool). It is simpler than E2B's UFFD plus NBD and doesn't need hugepages. |

**What stays in Python:** the control plane — `app.py`, jobs, sessions, MCP, A2A, the front door, scheduling, the store and the provider orchestration. It is I/O-bound and logic-heavy, it changes nightly, and it is where tests live.

**The boundary between them:**

- **Separate binaries, not PyO3.**
  - `hangar-envd` runs inside the sandbox.
  - The edge runs as a sidecar or Deployment.
  - hangar talks to both over HTTP and Connect.
  - The control plane's envd client is a ~150-line Connect-JSON client on `httpx` (`hangar/envd_client.py`), so hangar doesn't depend on the E2B SDK's internals and can talk to E2B, CubeSandbox and its own daemon the same way.
- **PyO3 only for in-process hot loops that profiling proves.** None exist today. The heavy Python dependencies are already Rust: `cryptography`, `pydantic-core`, and the E2B SDK's `pyqwest`.
- **Distribution.** The Rust binaries ship as static musl builds in the harness-pack image volume (0006), so every environment gets `hangar-envd` without rebuilding its image. Docker gets it through a bind mount; E2B and CubeSandbox templates through a baked layer.

## 8. Recommendations

### 8.1 An E2B-compatible API surface for hangar

**Where it lives.** Mount the API at `/e2b` on a separate host name, `api.<hangar-domain>`, so the SDK's default `https://api.<domain>` works with `E2B_DOMAIN=<hangar-domain>`. Serve sandbox URLs at `*.<hangar-domain>` through the edge (§7).

**What a client sets.** An E2B SDK user sets `E2B_DOMAIN` and `E2B_API_KEY=hgr_…` and changes no code.

**Endpoint mapping.**

| E2B | hangar | Notes |
| --- | --- | --- |
| `POST /v2/sandboxes` | `create_sandbox(env=templateID, ttl=timeout, labels=metadata, env=envVars)` | `templateID` resolves to an environment name, `name:tag`, or a snapshot ID. Returns `sandboxID` = hangar ID, `envdVersion` = what `hangar-envd` implements, a per-sandbox `envdAccessToken` and `trafficAccessToken`, and `domain`. Dedupe `Idempotency-Key` per caller, as jobs already do with `idempotency_key`. Warm pools serve these creates. |
| `GET /v2/sandboxes` | list, filtered by labels and state, with a cursor | `state=paused` covers sandboxes paused by the provider *and* suspended sessions. |
| `GET /sandboxes/{id}` | get | `cpuCount`, `memoryMB` and `diskSizeMB` come from `Resources`; `lifecycle.onTimeout` from the suspend policy. |
| `DELETE /sandboxes/{id}` | destroy | |
| `POST …/timeout`, `…/refreshes` | `extend` / set `expires_at` | E2B's `timeout` can shorten the expiry; refresh only extends it. |
| `POST …/pause {memory:false}` | **session suspend** (0010): checkpoint the conversation, commits and WIP, then destroy | Resume boots a fresh sandbox and restores. The response is 204 and the state `paused` with `pauseMode: filesystem`. |
| `POST …/pause {memory:true}` (default) | provider full snapshot: Docker or Kata snapshot, GKE Pod Snapshot, E2B or CubeSandbox pause | On a provider without memory snapshots, answer **400 with a clear message**, never a silent downgrade, as E2B itself does for `connect memory=false`. |
| `POST /v2/sandboxes/{id}/connect` | resume if paused (memory or filesystem, as recorded) and return the tokens | `memory=false` on a memory snapshot → restore from the filesystem checkpoint. |
| `POST …/fork {count}` | `fork_sandbox` ×N from one snapshot (0008, `fork_session`) | One result per fork, with an error per entry. Mint gateway grants and envd tokens **after** the fork (0012 §4.2). |
| `POST …/snapshots`, `GET /snapshots` | `snapshot` → `Snapshot` (full or workspace); usable as a `templateID` | |
| `GET …/metrics` | envd `/metrics`, sampled by hangar | |
| `GET /v2/…/logs` | the sandbox's events stream (0011) | |
| `PUT …/network` | egress policy (0012 item 7) | **501** until item 7 ships. Then `allowOut`/`denyOut` map to the policy and `rules` to credential injection; hangar's key never enters the sandbox. |
| `GET /v2/templates`, `GET /templates/{id}`, `/templates/aliases/{alias}`, `DELETE /templates/{id}`, tags | list, get and delete environments; tags map to env versions | |
| `POST /v3/templates` + `/v2/templates/{id}/builds/{bid}` + status, logs and file hashes | env build | Phase 3 at the earliest: translate `fromImage` + steps (`RUN`, `COPY`, `ENV`, `WORKDIR`, `USER`) into an `EnvSpec` and a build context. Until then, **501** with "build environments with `hangar env publish`". |
| `GET /teams` | one team per hangar | |
| `GET /health` | `/healthz` | |

**What does not map in the first cut:**

- *Not mapped:* volumes, secrets (hangar's vault and credentials are a different model), MCP gateway, IAM, the events webhooks (hangar has its own signed callbacks), the template build DSL, and `autoResume`-on-traffic (needs the edge and auto-resume first).
- *Answered `501` with a pointer:* `/api-keys`, `/admin`, `/nodes` and `/clusters`.
- *Accepted as stored fields:* `metadata` keys with E2B's own semantics, such as `autoPauseMemory`. They have no behaviour in hangar.

### 8.2 CubeSandbox and self-hosted E2B as hangar providers

These are changes to `providers/e2b.py` and `config.py`, in the provider's own module plus small marked additions to config.

1. **Endpoints.**
   - Today the provider only takes `e2b_domain`, and the SDK derives `https://api.<domain>` from it.
   - Add `HANGAR_E2B_API_URL`, passed as `api_url`. Use it for CubeSandbox (`http://<cubeapi>:3000`) and for E2B Embed when the API is not at `api.<domain>`.
   - Add `HANGAR_E2B_SANDBOX_URL` only for single-host debugging.
   - Never set the global `E2B_API_URL` environment variable from hangar, because it would leak into every SDK call in the process.
2. **A flavor setting.** Add `HANGAR_E2B_FLAVOR = e2b | cube`.
   - **e2b** (cloud, BYOC, Embed): current behaviour.
   - **cube** changes three things:
     - **Lifecycle through hangar's own REST client.** It sends `POST /sandboxes` and `POST /sandboxes/{id}/connect`, the v1 routes CubeAPI serves, because the pinned SDK (`e2b>=2.50`) posts to `/v2/sandboxes` and CubeAPI answers 405 (§5.4).
     - **Image-based templates.** Builds use Cube's `POST /templates` with `{image, cpu, memory, writableLayerSize, exposedPorts:[49983], probePort:49983}` and poll the build status.
     - **Private sandboxes.** Every sandbox is created with `network.allowPublicTraffic=false`, so CubeProxy demands the traffic token. Without that, CubeSandbox's unauthenticated envd is open to anyone who can reach CubeProxy.
   - Upstream: open a PR to CubeSandbox adding `POST /v2/sandboxes` and `POST /v2/sandboxes/{id}/connect` (a thin alias in `CubeAPI/src/routes.rs`) and returning an envd token. The `cube` branch can be removed once those land.
3. **Exec.** Run the command directly. Today `exec` sends `bash -lc '<cmd>'` through `commands.run`, which itself runs `/bin/bash -l -c`, so the user's login profile runs twice per exec. Pass `timeout=0` on harness starts so `Connect-Timeout-Ms` doesn't kill long agent turns (§3.3).
4. **Data plane.** Use hangar's own envd client (§7) on all three flavours. It is the same protocol, and it takes `X-Access-Token` when present.
5. **Capabilities.**
   - `cube`: `full_snapshots=True`, `pause=True`, `microvm=True`, plus rollback.
   - Both flavours: fork through hangar's snapshot-then-create path. E2B also has the native `/fork`.
6. **Testing.** A fake E2B API on `httpx.MockTransport`, covering both flavours' route sets, plus the debug-mode envd harness (§3.7). No live account is needed.

### 8.3 Auth mapping

| E2B credential | hangar |
| --- | --- |
| `X-API-Key` / `Authorization: Bearer` on `/e2b/*` | A hangar token (`hgr_…`) with a new scope, **`sandboxes`**: only `/e2b/*` and the edge. `admin` implies it; `jobs` and `mcp` don't. Compare in constant time. Budgets and `max_budget` apply as for MCP clients. |
| `envdAccessToken` | 32 random bytes per sandbox. hangar-envd gets it at boot through a 0600 file or env var read once and unset, never through a network `/init`. The control plane keeps it encrypted with the vault, so it can be returned again on connect. It is rotated on every fork and restore. It is never a gateway grant and grants nothing outside the sandbox. |
| `trafficAccessToken` | Per sandbox, checked by the edge on `e2b-traffic-access-token`. Default `allowPublicTraffic=false` (deny by default). An environment can opt into public ports. |
| `X-Team-ID`, admin tokens | Unsupported (501). |
| CubeSandbox `AUTH_CALLBACK_URL` | hangar can serve the callback: `POST /e2b/auth/callback` verifies the forwarded key as a `sandboxes`-scoped token. CubeSandbox then shares hangar's token store. That is optional; a single service key in hangar's provider config also works. |

## 9. Phased plan and success metrics

| Phase | Scope | Gate (all measured with `bench/`, 30+ runs, p50 / p95) |
| --- | --- | --- |
| **P0** (in progress) | `hangar-envd` passes §3 | **Conformance:** the probe suite from §2 (pip + npm SDK, `E2B_DEBUG`) at 100%. **Exec:** `/bin/true` round trip ≤ 4 / ≤ 8 ms on loopback. **Size:** idle RSS ≤ 8 MB, binary ≤ 8 MB. |
| **P1** | Docker and Kubernetes providers use envd for exec, files and terminals, falling back to `docker exec`/`kubectl exec`; `ckpt` capture as an envd RPC | **Docker:** `exec` 74.75 → ≤ 10 / ≤ 20 ms; `terminal_send` 204 → ≤ 20 ms; `checkpoint_turn` 524 → ≤ 300 ms. **GKE:** measure the `kubectl exec` baseline first (0012's open item), then exec through envd ≤ 25 / ≤ 50 ms. |
| **P2** | `/e2b` API (§8.1), `sandboxes` scope, Rust edge with traffic tokens | **SDK:** E2B SDK smoke tests (create, run, files, watch, PTY, pause/connect, fork, snapshot) pass against hangar on Docker and GKE with only `E2B_DOMAIN` and `E2B_API_KEY` set. **Create from a warm pool:** Docker ≤ 300 / ≤ 400 ms (today 318 / 354), GKE ≤ 1.5 / ≤ 2.5 s. **Edge:** adds ≤ 1 ms p50. |
| **P3** | `e2b` provider flavours (§8.2) against CubeSandbox and E2B Embed on one GCE nested-virtualization VM (`n2-standard-8` or `c3-standard-8`) | **Correctness:** hangar e2e (create → agent turn → checkpoint → fork → destroy) green on both. **Create:** ≤ 150 / ≤ 300 ms at concurrency 1. **Restore from snapshot:** ≤ 150 ms. Record the nested-virtualization penalty against the published bare-metal numbers. |
| **P4** | MicroVM tier option A on GKE (or B if A misses the gate); `runtime: microvm` envs | **Create:** ≤ 100 / ≤ 250 ms at concurrency 10. **Memory fork:** N = 10 in ≤ 1 s, 0008's target. **Memory:** idle overhead ≤ 40 MB per sandbox. **Cost:** $ per sandbox-hour reported next to the gVisor pool in the cost view. **Security:** no shared envd or gateway token between forks (test). |
| **P5** (conditional) | `hangar-node` (Rust) | Only if P3 and P4 show a blocking gap. Same gates as P4. |

Tier targets at a glance:

| Tier | Create p50 / p95 | Exec round trip p50 |
| --- | --- | --- |
| Docker (runc), warm | ≤ 300 / 400 ms | ≤ 10 ms (envd) |
| Docker + Kata, local | ≤ 1.5 s (today about 2 s) | ≤ 10 ms |
| GKE gVisor, warm claim | ≤ 1.5 / 2.5 s | ≤ 25 ms (envd) |
| GKE Pod Snapshot restore | ≤ 2 s | ≤ 25 ms |
| MicroVM (CubeSandbox or E2B on GCP nested virtualization) | ≤ 100 / 250 ms | ≤ 10 ms |
| MicroVM fork | N = 10 ≤ 1 s | n/a |

## 10. What I couldn't verify

- **CubeSandbox and E2B on GCP nested virtualization.** Nothing ran here (no KVM). All their latency and density numbers are their own, on their hardware.
- **E2B Cloud start, restore and fork latency.** The infra repo and its docs publish no number.
- **CubeSandbox's "<5 MB" overhead.** No method is published. Its own density data says 21–34 MB.
- **CubeSandbox with the current SDK at runtime.** The 405 on `POST /v2/sandboxes` comes from Cube's own pinning comment [C7] and its route table [C6]. I did not run it.
- **GKE `kubectl exec` latency.** Still unmeasured (0012 had the same gap). P1 measures it first.
- **The bare-metal price** comes from a secondary aggregator [G5]. GCP's own pricing page renders client-side and couldn't be read through the proxy.
- **Checkpoint speed-up from zstd in Rust** is an estimate until P1 builds it.
- **GKE managed `microvm` node pools.** Their availability after gcloud 588 was not re-checked.
- **JS SDK over HTTP/2 through a TLS edge.** Only HTTP/1.1 and plain HTTP were captured. An h2 edge in front of an HTTP/1.1 envd is E2B's own production shape [E3], so this is low risk.

## 11. Sources

Fetched or cloned 2026-10-13.

**E2B**

- E1 `e2b-dev/infra` @ `1c7f6c9`: [`spec/openapi.yml`](https://github.com/e2b-dev/infra/blob/1c7f6c944858069e061c8aa92650365d496b37f5/spec/openapi.yml)
- E2 `e2b-dev/infra` @ `1c7f6c9`, `packages/envd/`: [`spec/process/process.proto`](https://github.com/e2b-dev/infra/blob/1c7f6c944858069e061c8aa92650365d496b37f5/packages/envd/spec/process/process.proto), [`spec/filesystem/filesystem.proto`](https://github.com/e2b-dev/infra/blob/1c7f6c944858069e061c8aa92650365d496b37f5/packages/envd/spec/filesystem/filesystem.proto), [`spec/envd.yaml`](https://github.com/e2b-dev/infra/blob/1c7f6c944858069e061c8aa92650365d496b37f5/packages/envd/spec/envd.yaml), `main.go`, `internal/api/auth.go`, `internal/permissions/{authenticate,keepalive}.go`, `internal/services/process/{start,connect,service}.go`, `internal/services/process/handler/handler.go`, `internal/services/filesystem/{dir,move,remove}.go`, `internal/api/{upload,download}.go`; `packages/shared/pkg/keys/sha256.go`
- E3 `e2b-dev/infra` @ `1c7f6c9`: [`README.md`](https://github.com/e2b-dev/infra/blob/1c7f6c944858069e061c8aa92650365d496b37f5/README.md), [`docs/ARCHITECTURE.md`](https://github.com/e2b-dev/infra/blob/1c7f6c944858069e061c8aa92650365d496b37f5/docs/ARCHITECTURE.md)
- E4 `e2b-dev/infra` @ `1c7f6c9`: `embed/README.md`, `embed/kubernetes/README.md`, `embed/kubernetes/statefulset.yaml`, `embed/terraform/gcp/{main,variables}.tf`
- E5 `e2b-dev/infra` @ `1c7f6c9`: `packages/shared/pkg/proxy/host.go`, `packages/shared/pkg/grpc/proxy/metadata.go`, `packages/client-proxy/internal/proxy/proxy.go`, `packages/shared/pkg/id/id.go`
- E6 `e2b-dev/E2B` @ `62a17df` (= pip and npm `e2b` 2.56.0): `packages/python-sdk/e2b/{connection_config.py, sandbox_domains.py, api/__init__.py, envd/{client_shared.py, client_async/__init__.py, utils.py, versions.py}, sandbox_async/{main.py, commands/command.py, commands/pty.py, filesystem/filesystem.py}, sandbox/filesystem/filesystem.py}`; `packages/js-sdk/src/{sandbox/index.ts, connectionConfig.ts, envd/versions.ts}`; pip wheels 2.29.5, 2.44.0, 2.52.1, 2.53.1, 2.55.1, 2.56.0 (`e2b/sandbox_async/sandbox_api.py`)

**CubeSandbox** (`TencentCloud/CubeSandbox` @ `8d3d991`)

- C1 [`README.md`](https://github.com/TencentCloud/CubeSandbox/blob/8d3d9914dd9d09a9de37f93c4dac703d090fc0a5/README.md)
- C2 [`docs/architecture/overview.md`](https://github.com/TencentCloud/CubeSandbox/blob/8d3d9914dd9d09a9de37f93c4dac703d090fc0a5/docs/architecture/overview.md), `docs/architecture/network.md`
- C3 `hypervisor/Cargo.toml` (`name = "cube-hypervisor"`, `version = "28.0.0"`), `hypervisor/README.md`, `hypervisor/CREDITS.md`
- C4 `agent/README.md`
- C5 `docker/README.md`, `docker/cube-entrypoint.sh`, `.github/workflows/build-envd-base-image.yml`
- C6 `CubeAPI/src/routes.rs`, `CubeAPI/src/services/sandboxes.rs`, `openapi.yml`
- C7 `examples/code-sandbox-quickstart/requirements.txt`
- C8 `docs/blog/posts/2026-06-01-cubesandbox-perf-benchmark.md`
- C9 `docs/blog/posts/2026-06-03-cubesandbox-perf-benchmark-pvm.md`, `docs/guide/pvm-deploy.md`, `docs/guide/quickstart.md`
- C10 `docs/blog/posts/2026-06-25-cubesandbox-snapshot-clone-rollback-deep-dive.md`
- C11 `docs/guide/authentication.md`, `CubeProxy/lua/sandbox_backend.lua`
- C12 `docs/guide/kubernetes/architecture.md`

**Google Cloud**

- G1 [About nested virtualization (Compute Engine)](https://docs.cloud.google.com/compute/docs/instances/nested-virtualization/overview)
- G2 [Use nested VMs with GKE clusters](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/nested-virtualization)
- G3 [Bare metal instances](https://docs.cloud.google.com/compute/docs/instances/bare-metal-instances)
- G4 [About machine support with GKE clusters](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/machine-support)
- G5 [gcloud-compute.com: c3-standard-192-metal](https://gcloud-compute.com/c3-standard-192-metal.html) (secondary)

**hangar**

- H1 `bench/results/2026-10-13-perf-docker-ab-after.json`
- H2 `docs/gke.md` §4, `docs/local-microvms.md`, `deploy/terraform/gke/variables.tf`
- Spec 0012 (PR #54): §4.1, §4.3, items 8 and 15, sources S40–S43
