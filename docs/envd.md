# hangar-envd: the in-sandbox daemon

Every command, file read, file write and terminal hangar runs in a sandbox used to go through `docker exec` or the
Kubernetes exec API. A Docker exec costs about 70 ms; a Kubernetes exec is a websocket through the API server and
costs more. Agents and hangar make many of these calls (`exec`, `read_file`, `write_file`, terminal operations,
`agent_status` long-polls, checkpoint scripts), so hangar now starts a small daemon inside each Docker and Kubernetes
sandbox and sends those calls to it over HTTP. E2B sandboxes already have their own daemon (E2B's `envd`), which
hangar-envd is modeled on.

- **Code:** the Rust crate in [`envd/`](../envd) (the daemon) and `src/hangar/envd.py` (hangar's side).
- **Size:** a 2.7 MB static binary (x86_64, musl, LTO, stripped; aarch64 is 2.3 MB). It idles at about 2 MB RSS and
  peaked at 6 MB during the benchmark below.
- **Protocol:** E2B envd's: Connect RPC `process.Process` and `filesystem.Filesystem` (JSON or binary proto, over
  HTTP/1.1 or cleartext HTTP/2 on one port), plus `GET/POST /files` and `GET /health`. The stock e2b Python SDK works
  against it unchanged ([compatibility](#e2b-compatibility)).

## Numbers

Docker, the same sandbox and the same provider calls, before (`docker exec`) and after (hangar-envd). From
`scripts/bench/sandbox.py --phases envd --runs 10` on a 2-vCPU VM, sandbox limited to 1 CPU
(`bench/results/2026-10-13-envd-docker.json`):

| Call | docker exec p50 / p95 | hangar-envd p50 / p95 |
| --- | --- | --- |
| `exec("true")` (`timeout … bash -lc true`) | 73.6 / 87.8 ms | **7.4 / 8.3 ms** |
| write 64 KiB | 4.4 / 7.6 ms | 2.0 / 5.3 ms |
| read 64 KiB | 5.1 / 6.4 ms | 1.3 / 1.7 ms |
| write 1 MiB | 6.0 / 8.3 ms | 5.3 / 6.2 ms |
| read 1 MiB | 7.6 / 12.4 ms | 3.6 / 5.1 ms |
| write 16 MiB | 22.2 / 31.9 ms | 48.0 / 53.8 ms |
| read 16 MiB | 43.9 / 64.2 ms | 35.6 / 47.4 ms |
| 1000 execs, 50 at a time (wall time) | 32.7 s | **5.4 s** |
| PTY echo (a keystroke into `cat` and back) | 0.6 / 1.7 ms | 1.6 / 2.8 ms |

Most of the remaining 7 ms per `exec` is `bash -l` reading the login profile; starting `/bin/true` directly takes
3.0 ms p50 / 4.1 ms p95 (local loopback), against 4.4 / 6.9 ms for upstream E2B envd measured the same way. The
burst is CPU-bound inside the 1-CPU sandbox. Large writes are slower than `docker cp` because every byte is
authenticated: hangar signs an upload frame by frame as it sends it and the daemon checks each frame before writing
it, about 13 ms per 16 MiB on each side (SHA-256 with SHA-NI), and the daemon's share runs inside the sandbox's CPU
limit. Signing as it goes, rather than hashing the whole body before sending it, made 16 MiB writes about 10% faster
(54 vs 60 ms p50 side by side, 1-CPU sandbox; 1 MiB: 4.7 vs 5.9 ms). An unsigned upload would take about 20 ms; writes
that size are rare (workspace restores). Terminal keystrokes are a little slower than a raw Docker socket and still
well under human perception.

**Kubernetes: an estimate, not a measurement (there was no cluster to measure on).** Each provider call today opens a
new pods/exec websocket (TLS and authentication at the API server), which the API server proxies to the kubelet, which
asks the container runtime to exec into the sandbox; file transfers ride base64 through the same socket. That is the
"tens to hundreds of milliseconds per call" this work set out to remove, and it grows with API-server load. With
hangar-envd a call is one request on a kept-alive connection from the control-plane pod to the sandbox pod's IP (an
in-cluster hop, well under a millisecond) plus starting the process inside the sandbox. So expect a Kubernetes
`exec` to cost about what the Docker column above shows, more under gVisor (whose fork and exec are slower than
runc's), and `agent_status` long-polls to stop holding API-server connections and control-plane threads.

## How it works

1. **First use.** The first provider call for a sandbox finds no daemon, uses the provider's own exec path, and starts
   the daemon in the background. Later calls go to the daemon once it answers. `sb.info["envd"]` shows its address
   and version.
2. **Getting the binary in.** Hangar copies it in at start:
   - Docker: one `put_archive` into `/tmp` (root-owned, so the sandbox user can't swap it) and one exec to start it.
   - Kubernetes: one exec that starts it if it's already in the pod; the first time in a pod, a streamed upload
     (~3 MB, #53's single-exec upload) comes in between.

   The binary isn't baked into environment images, so no image is rebuilt, warm pools keep working, and a hangar
   upgrade ships a new daemon with nothing else to roll. An init container or a shared volume would need every
   SandboxTemplate (and so every warm pool) recreated and an image published per release; baking would need every
   environment rebuilt. The cost is one upload per pod, in the background.
3. **Starting it.** `hangar-envd --daemon` binds the port, then detaches (new session, stdio to `/dev/null` and the log
   at `/tmp/hangar-envd.log`), so the start exec returns as soon as it listens. It runs as the sandbox user.
4. **Reaching it.**
   - Docker: the container's IP (Linux hosts), else the port published on `127.0.0.1` (Docker Desktop, where
     container IPs aren't routable). Containers created before this change have no published port. Sandboxes run on
     hangar's own network, where containers can't reach each other ([security](security.md#sandbox-networking-on-docker)),
     and that includes a container attached to it: a control plane that runs in a container (Docker Compose) can't
     reach the daemon there, and uses `docker exec`. Pointing `HANGAR_DOCKER_NETWORK` at a network shared with the
     control plane makes the daemon reachable, and lets sandboxes reach each other and the control plane
     (`hangar doctor` warns).
   - Kubernetes: the pod IP. [`deploy/k8s/envd-network.yaml`](../deploy/k8s/envd-network.yaml) lets hangar's pods,
     and only them, reach port 49983; everything else stays denied by `sandbox-network.yaml`.
5. **Falling back.** The provider's own path is used whenever the daemon can't be: no binary for the sandbox's CPU,
   no reachable address, a start that failed (then not retried for 5 minutes), `exec` as another user (the daemon is
   unprivileged; Docker's root commands keep `docker exec`), writes under system directories on Docker (they stay
   root-owned), reads the sandbox user can't do, and any call the daemon refused (an error status, or an error before
   the start event) or never received (no connection). A command whose request may have arrived and whose answer then
   broke off, timed out or came from something other than the daemon is an error, not a retry: it may have run. A
   failed transport drops the cached address, so a resumed pod (new IP) is looked up again.

Exec results match the providers': the same `timeout --kill-after=5 N bash -lc` wrapper, exit code 124 and the
"timed out" note, the last 256 KiB of each stream with the truncation marker.

## Settings

| Setting | Default | |
| --- | --- | --- |
| `HANGAR_ENVD` | `auto` | `off` keeps every call on the provider's exec path. |
| `HANGAR_ENVD_DIR` | unset | Directory holding `hangar-envd-x86_64` / `hangar-envd-aarch64`. Unset: `/usr/local/lib/hangar` (the control-plane image), then `envd/dist` in a checkout. |

## Building

```bash
envd/build.sh                 # both CPUs into envd/dist/ (rustup; cross builds link with rust-lld, no C toolchain;
                              # on a musl host such as rust:alpine its own target links with the system linker)
ARCHES=x86_64 envd/build.sh   # one
cd envd && cargo test         # unit and in-process HTTP tests
```

`Cargo.lock` pins every dependency and builds use `--locked`. The control-plane `Dockerfile` builds both binaries in
a `rust:1.97-alpine` stage and copies them to `/usr/local/lib/hangar`, so `gcloud builds submit` (deploy/gke) and
`docker compose build` need nothing extra. Main crates: axum/hyper/tokio, `connectrpc` (the ConnectRPC project's Rust
runtime, which passes the Connect conformance suite) with `buffa` messages, protos compiled by `protox` (no `protoc`).

## Security

The daemon runs inside a sandbox that runs untrusted code, so the trust boundary is hangar's side:

- **Deny by default.** Everything but `GET /health` needs authentication.
- **Tokens.** Each sandbox's token is HMAC(vault key, sandbox id): unique per sandbox, never stored, the same after a
  control-plane restart (so a running daemon is found again, not started twice).
- **Signed requests.** Hangar never sends the token. Each request carries `X-Hangar-Sig` (`<ms>.<nonce>.<body>.<mac>`):
  an HMAC with the token over the time, a random nonce, the method, the path with its query and the body's SHA-256,
  or `chunked` for an upload whose body is frames each carrying an HMAC chained from the request's. The daemon checks
  the signature before it reads any of the body and spends the nonce only then, so a peer without the token gets
  nothing done, buffered or created, and can't burn nonces or fill the nonce table. It accepts a signature once,
  within five minutes of its time either way (the clocks of hangar and the sandbox may differ by that much). The body
  is then held to what was signed: in memory for RPCs, and frame by frame for uploads, each frame checked before it
  is written and the file replaced only after the last. A request that reaches the wrong daemon (say, a reused
  container IP) can't be replayed, altered, cut short or turned into a token. E2B clients use `X-Access-Token`,
  compared in constant time.
- **Answers prove the token.** Every answer to a signed request carries `X-Hangar-Proof`, an HMAC with the token over
  the request's signature. Hangar refuses answers without it (but a 401), so whatever else answers at a sandbox's
  address, such as another sandbox that got its IP, is never taken for its daemon: its exec results, file contents and
  "no such file" are all refused, and the address is looked up again. What it does get is that one request.
- **Untrusted answers.** Exec output keeps a bounded tail as it streams, reads stop at the caller's limit (64 MiB
  without one; past that the provider reads the file), Connect messages over 4 MiB end the call, small responses are
  capped, compressed answers are refused (`Accept-Encoding: identity`, raw reads), a malformed answer is an error, and
  every call has a deadline (an exec's is its timeout plus 45 s). `test_envd.py` runs hangar against a hostile fake
  daemon.
- **Unprivileged.** The daemon runs as the sandbox user and acts only as that user: processes, files and the
  `Authorization: Basic` user an E2B client names (any other user is refused). The token is not a secret from that
  user (it can read its own processes' memory); it protects against everything outside the sandbox.
- **Process hygiene.** The token is removed from the daemon's environment before anything starts, so commands don't
  inherit it. Commands get `oom_score_adj` 100, so the OOM killer takes them before the daemon.
- **Limits.** 512 processes at once, 8 clients attached to one process, 16 queued output chunks per client (a slow
  client slows the process, like a terminal, instead of growing memory; one that takes nothing for a minute is
  dropped), 64 directory watches, 16,384 watched
  directories per watch, 100,000 entries per listing, 8 MiB signed request bodies, 4 MiB upload frames, paths up to
  4 KiB with no NUL. Paths resolve like E2B's (`~` and relative paths from the user's home, then cleaned lexically).
- **Connections.** Anything that can reach the port can connect (on Docker's default bridge, or any network created
  without `enable_icc=false`, that includes other sandboxes), so the daemon holds at most 1,024 connections and 512 from one address, and closes a connection that
  sends no request, or doesn't finish its headers, for 75 s (longer than hangar keeps idle connections, 30 s). At the
  total cap it stops accepting until one closes. A panic unwinds the one request it happened in.

## E2B compatibility

Tested with the stock `e2b` 2.56.0 Python SDK pointed at a daemon (`tests/test_envd_e2b_sdk.py`, over HTTP/1.1 and
HTTP/2): `commands.run` (exit codes, stderr, env, cwd), background commands, `commands.list`/`kill`, `send_stdin`,
`files.write`/`read`/`list`/`exists`/`get_info`/`make_dir`/`rename`/`remove`, `files.watch_dir`, `pty.create`/
`send_stdin`/`resize`, and a wrong token surfacing as `AuthenticationException`. Upstream semantics it follows: the
start event first, `exitCode`/`exited`/`status`/`error` like Go's `ProcessState`, keepalive events, `Connect-Timeout-Ms`
on `Start` bounding the process, `Connect` answering for 30 s after an exit, `stdin` defaulting to on, `rm -rf`
semantics for `Remove`, 409 for an existing directory on `MakeDir`, inotify event types, `FileMode.Perm()` modes and
`FileMode.String()` permissions.

Not (yet) implemented: gzip on `/files` (uploads with `Content-Encoding: gzip` get 415; downloads are sent
uncompressed, which clients accept), pre-signed `/files` URLs, `X-Metadata-*` xattrs, `/files/compose`, `/init`,
`/metrics`, and switching users (upstream envd runs as root; this one doesn't, by design: what hangar runs as its own user goes through `docker exec`, [steering.md](steering.md#the-uid-split)). Processes inherit the daemon's
environment (the sandbox's, as `docker exec` gives) rather than upstream's minimal one. A hangar API that hands this
daemon to E2B clients should report envd version `0.5.7`, the newest whose SDK features are all here.

Hangar's additions: `X-Hangar-Sig`, `GET /info` (version, user, process count; authenticated), `mode=<octal>` on
uploads, and `Content-Length` on downloads.

## Troubleshooting

- Hangar logs `envd: not using a daemon in <sandbox> ... (<why>)` when it falls back, then tries again 5 minutes later.
- Inside the sandbox: `/tmp/hangar-envd.log`, and `ls /tmp/hangar-envd-*` for the binary.
- On GKE, a daemon that starts but never answers usually means the NetworkPolicy isn't applied or the CNI drops the
  traffic; hangar keeps using exec meanwhile.
