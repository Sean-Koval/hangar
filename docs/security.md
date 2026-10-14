# Security model

Hangar exists to run untrusted, model-generated code. The design assumes any sandbox can be fully compromised by the agent inside it.

| Boundary | Mechanism |
| --- | --- |
| Sandbox ↔ host | runc (laptop, trusted use), gVisor user-space kernel, or Kata/Firecracker microVM with its own guest kernel |
| Sandbox ↔ sandbox | Separate containers/pods/VMs; no shared volumes; per-sandbox credentials |
| Sandbox ↔ network | GKE: `deploy/k8s/sandbox-network.yaml` (and Agent Sandbox's managed policy) deny RFC1918, link-local (metadata server) and all ingress. Docker: hangar's own network, where sandboxes can't reach each other ([below](#sandbox-networking-on-docker)); the single-VM install also drops what they send to link-local addresses. Environments in `allowlist` or `none` mode reach the internet only through hangar's [egress proxy](#egress-allowlists), by hostname |
| Client ↔ control plane | Bearer tokens (hashed at rest), single-use tickets for browser streams, TLS via ingress, optional IAP in front |
| Secrets | Fernet-encrypted vault; never in images, API responses, MCP results, logs, or audit entries |

## Credential handling

- **Delivery.** Secrets are written into a running sandbox at claim time, to `~/.hangar/env` (mode 0600) or to the harness's own file. Sandbox images never contain them.
- **Forks.** A fork scrubs inherited secret files before re-injecting only the credentials it was granted.
- **Exclusive credentials** are ones with rotating refresh tokens, like a Codex ChatGPT login. Each is leased to one sandbox. When that sandbox stops, the possibly rotated file is copied back into the vault.
- **Snapshots** of a running sandbox (Docker `commit`) may contain secrets that were present at snapshot time. They stay in the local image store and are never pushed.
- **Control-plane credentials never enter a sandbox.** Some credentials exist only for hangar itself: webhook signing keys (`webhook` credentials, the GitHub webhook secret, inbound webhook keys), the Slack bot token and signing secret, GitHub App keys, and the GitHub tokens hangar calls GitHub with (`HANGAR_GITHUB_TOKEN_CREDENTIAL`, `HANGAR_GATEWAY_CREDENTIAL`). Hangar refuses to put them in a sandbox by any path: an environment's `secrets:` (refused when the environment is saved, and again when a sandbox starts), an attached credential's env var or file, and so MCP config resolution, which only sees what those wrote. With the webhook secret anyone could forge trusted GitHub deliveries. `hangar cred ls` marks them `control-plane`. To share the control plane's GitHub token with direct-mode sandboxes on purpose, an admin runs `hangar cred exposure <name> sandbox`; signing keys, bot tokens and App keys can't be shared.
- **Exposure inside the sandbox.** Anything you inject is readable by the agent in that sandbox, and by any MCP client holding a hangar token (`exec`/`read_file` can read `~/.hangar/env`). Prefer scoped credentials: fine-grained GitHub tokens limited to the target repo, and API keys with spend limits. For git, use the [gateway](#git-credential-gateway): the token then never enters the sandbox.
- **Forks** run `tmux kill-server` before scrubbing secret files, so shells that exported the parent's secrets don't survive into the copy. A full memory snapshot (E2B) still carries any secrets held in other long-running processes' memory, so fork from a clean point when it matters.
- **Audit redaction.** Commands, prompts and terminal input are recorded in the audit log with stored credential values masked as `***`. Secrets that were never stored in the vault can't be recognized, so don't paste unrelated secrets into agent terminals.
- **Browser streams.** The dashboard never puts its token in a URL. Websocket terminals and build-log streams use single-use tickets that expire after 60 seconds.

## Git credential gateway

By default (`credential_mode: direct`) a sandbox gets the GitHub token itself, in `GH_TOKEN` and `~/.git-credentials`, and can push to any branch that token reaches. With the gateway, the token stays in the control plane:

- The sandbox gets a **grant**: an `hgs_…` token that works only at hangar's gateway, only for the sandbox's repo, and only while the sandbox runs. Hangar stores its hash; the token itself is written once, to the sandbox's `~/.git-credentials`.
- The sandbox's git is pointed at the gateway with `url.<gateway>/git/github.com/.insteadOf https://github.com/` (and the `git@github.com:` and `ssh://` forms). Clones, fetches and pushes keep working with the usual URLs.
- The gateway reads each push's ref updates before any of the pack is forwarded. It allows only the sandbox's own branch (the job or session branch, or `work_branch`), plus *creating* new branches under `refs/heads/hangar/` (`HANGAR_GATEWAY_PUSH_REFS`); a branch a sandbox created that way becomes its own, and nobody else can update it. That rule never creates a branch under `hangar/session/`, another session's branch, or one that would block either. It refuses pushes to other branches, deletes, tags (`HANGAR_GATEWAY_ALLOW_TAGS`), push options and signed pushes. Git shows the reason: `! [remote rejected] main -> main (hangar: this sandbox may push only to hangar/job-…)`.
- Allowed requests are streamed to GitHub with the real credential: a PAT from the vault, or a **GitHub App** installation token minted for that one repo (`contents: write`, `pull_requests: write`), cached in memory until shortly before it expires.
- Pushes and refusals are in the audit log (`gw.git.push`, `gw.git.deny`) with the refs and the result. The grant is revoked when the sandbox is destroyed, expires or disappears; a fork gets its own grant.

The control plane's own GitHub calls (opening PRs and commenting, `HANGAR_GITHUB_TOKEN_CREDENTIAL`) are unchanged.

### Turning it on

1. Store the credential the gateway uses upstream. Either a fine-grained PAT limited to the repos agents work on (Contents read & write):

   ```sh
   hangar cred add github-pat --harness git --kind git_token --file ~/.hangar/gh-token
   ```

   or a GitHub App (recommended; see below).
2. Tell hangar where sandboxes reach the gateway, and which credential to use when an environment doesn't attach one:

   | Where | Settings |
   | --- | --- |
   | GKE | `HANGAR_GATEWAY_URL=http://hangar-gateway.hangar.svc:8788` (`deploy/k8s/gateway.yaml` adds the Service and the NetworkPolicy) |
   | Docker on a laptop | `HANGAR_GATEWAY_URL=http://host.docker.internal:8788` and `HANGAR_GATEWAY_HOST=172.17.0.1` (the docker bridge; `0.0.0.0` also works but listens on every interface) |
   | Both | `HANGAR_GATEWAY_CREDENTIAL=github-app` (optional when environments list a `git_token`/`github_app` credential) |

   The gateway listens on its own port (`HANGAR_GATEWAY_PORT`, default 8788) and serves nothing but `/git` and `/healthz`, so a network policy can let sandboxes reach it without reaching the API, dashboard or MCP endpoint. `HANGAR_GATEWAY_PORT=0` serves `/git` on the main port instead; then `/git` is reachable wherever the main port is (through your ingress, too), so a grant copied out of a sandbox works from there for as long as the sandbox lives.
3. Choose which sandboxes use it. With the gateway configured, **jobs and sessions use it by default** (`HANGAR_GATEWAY_FOR_JOBS=true`). For other sandboxes, or to opt a job environment out, set it on the environment:

   ```yaml
   name: api
   credential_mode: gateway   # or: direct
   credentials: [claude-max, github-app]
   repo: {url: https://github.com/me/api.git}
   ```

4. Run `hangar doctor`: it checks that the gateway answers and that every GitHub App credential can mint a token.

In gateway mode no git secret is written into the sandbox, by any path: `git_token` and `github_app` credentials are not injected, SSH keys are left out (with a warning), and an environment whose `secrets:` maps a git credential into an env var fails to start. A `github_app` credential is never injected in direct mode either. If an environment asks for the gateway and `HANGAR_GATEWAY_URL` isn't set, its sandboxes fail to start rather than fall back to the token.

What changes for agents: `git push` works for their own branch and fails with a clear message elsewhere. `gh` has no token (PRs are opened by hangar for jobs). All github.com git traffic goes through the gateway, so fetching *other* repositories over git (a `git+https://github.com/...` dependency, a second clone) is refused; bake those into the image or vendor them.

### Creating a GitHub App

1. GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App** (under the organization if the repos are an org's).
2. Name it (e.g. `hangar-agents`), set any homepage URL, and turn **Webhook** off.
3. Repository permissions: **Contents: Read and write**, **Pull requests: Read and write** (Metadata: read is added automatically). Leave everything else, in particular **Workflows** and **Administration**, at *No access*: without Workflows, GitHub refuses pushes that change `.github/workflows/`.
4. Create it, note the **App ID**, then **Generate a private key** (a `.pem` file downloads).
5. **Install App** on only the repositories agents work on. The installation id is the number at the end of the installation's settings URL; it's optional (hangar looks it up per repo).
6. Store it without printing the key:

   ```sh
   jq -n --arg id 123456 --rawfile key ~/Downloads/hangar-agents.*.private-key.pem \
     '{app_id: $id, private_key: $key}' | hangar cred add github-app --harness git --kind github_app
   rm ~/Downloads/hangar-agents.*.private-key.pem
   ```

The push is made as the App; commits keep the agent's git identity. PRs for jobs are still opened with `HANGAR_GITHUB_TOKEN_CREDENTIAL`.

### What the gateway does and doesn't cover

- It covers: a prompt-injected agent reading the GitHub token, pushing to `main` or another agent's branch, deleting branches, pushing tags, and using a dead sandbox's grant. Grant tokens are bound to one sandbox, stored hashed, compared in constant time, and never accepted by the main API; hangar API tokens are never accepted by the gateway.
- It doesn't cover: what is *in* the pushed branch (review the PR; keep secrets out of `push`-triggered workflows), the GitHub REST API from inside the sandbox (not proxied yet), egress filtering (that is the environment's `egress` mode, [egress allowlists](#egress-allowlists)), or the model credential, which is still injected.
- Force pushes to the sandbox's own branch are allowed (agents rebase); force pushes anywhere else are refused like any other update. Keep branch protection on `main` as a second wall.
- Inside the cluster the gateway speaks plain HTTP, so the grant crosses the pod network unencrypted. It is worth little: it dies with the sandbox and reaches one branch of one repo.

## Sandbox networking on Docker

On Docker's default bridge every container reaches every port of every other: hangar-envd, dev servers, anything an agent listens on. Hangar creates its own network for sandboxes instead, `hangar-sandboxes` on the bridge `hangar0`, with inter-container connectivity off (`com.docker.network.bridge.enable_icc=false`), and starts every sandbox and warm-pool container on it. Checked on Docker 29.8 (`tests/test_e2e_isolation_docker.py`):

- one sandbox can't connect to another's ports, envd's included;
- the host still reaches each sandbox at its IP, and at envd's port published on `127.0.0.1` (only the host can use that address; a sandbox that dials the host's bridge address gets nothing);
- sandboxes still reach the internet, and the host at `host.docker.internal`, where the [git gateway](#git-credential-gateway) listens;
- a container attached to the network can't reach the sandboxes either, so a control plane that runs in a container (Docker Compose) reaches envd only through `docker exec`.

`HANGAR_DOCKER_NETWORK` names another network to use as it is (`bridge` for Docker's default). `hangar doctor` warns when that network lets sandboxes reach each other, and lists sandboxes started before this change, which stay where they are until they are recreated.

Docker has no setting that keeps containers off link-local addresses, so on a cloud VM a sandbox can ask the metadata server for the VM's credentials unless the host's firewall drops it. The bridge has a fixed name for that rule (the single-VM install adds it on every boot; DNS stays open because Docker's resolver may forward to the same address):

```sh
iptables -I DOCKER-USER -i hangar0 -d 169.254.0.0/16 -p tcp ! --dport 53 -j DROP
```

## Egress allowlists

An environment says what its sandboxes may reach on the internet, by hostname:

```yaml
name: api
egress:
  mode: allowlist            # open | allowlist | none
  presets: [pypi, github]    # default [packages]: pypi, npm, crates, go and github together
  allow: [docs.python.org, "*.googleapis.com", "registry.example.com:8443"]
```

| Mode | Network | What gets through |
| --- | --- | --- |
| `open` (the default, `HANGAR_EGRESS_DEFAULT`) | the public internet directly, as before | everything public |
| `allowlist` | only DNS, the git gateway and hangar's egress proxy | the harness's own hosts (its model API and login refresh), the environment's remote MCP servers, the presets, `allow`, and hosts an owner approved |
| `none` | the same | the harness's own hosts, the environment's MCP servers and approved hosts only |

A rule is a host (`pypi.org`, ports 443 and 80), a host and port, or `*.domain` (any name below it, not the domain itself). There is no `*` rule: that is mode `open`.

- **Presets** (`egress.PRESETS`): `pypi` (pypi.org, files.pythonhosted.org), `npm` (registry.npmjs.org), `crates` (crates.io, index.crates.io, static.crates.io), `go` (proxy.golang.org, sum.golang.org), `github` (github.com, api.github.com, codeload.github.com, objects/release-assets/raw.githubusercontent.com), and `packages`, all of them.
- **The harness's hosts** are always allowed in a proxied mode, since an agent can't work without its model: Claude Code's are `api.anthropic.com`, `claude.ai` and `platform.claude.com` ([Claude Code's network requirements](https://code.claude.com/docs/en/network-config#network-access-requirements)); Codex's `api.openai.com`, `chatgpt.com` and `auth.openai.com`; Gemini and Antigravity's `generativelanguage.googleapis.com`, `cloudcode-pa.googleapis.com` and `oauth2.googleapis.com`. A pi router not among them goes in `allow`.
- **Per repo.** A repo policy's `egress` (`PUT /api/repos/policy`, admin) adds presets and hosts for that repo's sandboxes and can tighten the mode (`open` < `allowlist` < `none`). It can't widen it. A repo policy that asks for `allowlist` in an `open` environment isn't enforced, because the network decides that; the sandbox carries a warning saying so.
- **Who sets it.** `egress` on an environment needs an admin, like `secrets:` and `image`.
- **Asking for a host.** The agent runs `hangar-agent env request egress <host> --why '…'`, and the proxy's refusal says exactly that. The session's or job's owner, or an admin, approves or denies the request. An approval adds the host to that session's allowlist; an admin can widen it to the whole environment. It applies to the next connection, and is audited as `egress.approve`. Nothing is granted automatically. Admins list approvals with `GET /api/egress/approvals?environment=…` (or `session=…`) and revoke one with `DELETE` and `rule=` (audited as `egress.revoke`). `GET /api/egress/sandboxes/<id>` shows what a sandbox may reach and why.

**How it is enforced.** An environment's mode decides the network its sandboxes start on:

- **GKE.** Pods of a proxied environment carry `dev.hangar/egress=proxied`. `sandbox-network.yaml`'s internet baseline leaves them out. `gateway.yaml`'s `sandbox-egress-proxied` gives them DNS and the proxy on hangar's pods (port 3128, `HANGAR_EGRESS_URL=http://hangar-gateway.hangar.svc:3128`), and `allow-hangar-gateway` gives them the git gateway. Their SandboxTemplate carries the same rules as its `networkPolicy`, so Agent Sandbox applies that policy instead of its default one, which allows the public internet. The template stays `Managed`.
- **Docker.** Their containers go on `hangar-egress` (bridge `hangar1`), an *internal* network: Docker gives it no route out, but its gateway address reaches the host. Hangar listens there with the proxy and relays the git gateway's port; `host.docker.internal` points at that address in those containers. DNS on an internal network answers container names only. This needs hangar on the Docker host (Linux). Docker Desktop's VM has that address and the Mac doesn't, so a proxied sandbox fails to start there with a message that says so; keep `open` for local work. A host firewall that drops input (ufw's default) must accept TCP 3128 and 8788 from `hangar1`.
- **E2B and other providers** can't keep a sandbox off the internet, so a proxied environment fails to start there rather than run unenforced.

**The proxy** is part of the control plane, next to the git gateway (`egress_proxy.py`). It is an HTTP CONNECT proxy that never terminates TLS. For each connection it:

1. authenticates the sandbox. HTTPS_PROXY carries `<sandbox id>:<HMAC(vault key, sandbox id)>`; the value is never stored and is refused once the sandbox isn't running. 30 bad credentials a minute from one address are refused without a check;
2. checks host and port against the sandbox's policy: its environment, its repo's policy and its session's approvals, cached for 5 seconds;
3. resolves the name itself and connects only to public addresses, to the address it checked. A name that resolves into the VPC, the cluster, loopback or the metadata server is refused. `HANGAR_EGRESS_PRIVATE_HOSTS` lists internal mirrors that may resolve to private addresses, but never to loopback or link-local ones;
4. reads the client's first bytes before relaying any of them. A TLS ClientHello must name the same host in SNI, and plain HTTP must carry it in `Host`; anything else is cut off. Absolute-form `http://` requests are forwarded one per connection, without `Proxy-Authorization`;
5. relays bytes both ways until a side closes or nothing moves for 15 minutes. A sandbox has at most `HANGAR_EGRESS_MAX_CONNECTIONS_PER_SANDBOX` (256) tunnels open.

Why there and not a separate proxy: the decision needs the sandbox, its person, its session's approvals and the audit log, all of which live in the control plane. A connection costs one SQLite read by primary key, and relaying is socket copying with 256 KB reads. An off-the-shelf proxy (Smokescreen, Squid) would need per-sandbox roles, a way to receive approvals live, and its logs mapped back to people. A Rust proxy in `envd/`'s workspace would need the same policy over an API. The relay is the part to move out if a profile ever shows it costing the control plane.

**What is recorded.** Every denied connection is an `egress.deny` audit entry with the sandbox, person, environment, session, job, host, port and reason. A sandbox gets at most 30 a minute; the next entry records how many were dropped (`suppressed_before`). It also appears in the events of the sandbox's session. Allowed connections are summed per sandbox and host into `egress.allow` entries every minute: connections, bytes out and in, and the rule that allowed them. Metrics: `hangar_egress_connections{decision,reason,mode}`, `hangar_egress_bytes{direction}`, `hangar_egress_open_connections`.

**What it doesn't stop.** Anything sent *to* an allowed host. GitHub and package registries accept uploads, and a URL to any allowed host can carry data. The allowlist narrows where data can go and records who sent how much where; it doesn't make an allowed host safe. Without TLS interception the proxy can't see a `Host` header inside TLS, so a CDN that routes by `Host` rather than SNI could still be fronted through an allowed name. Encrypted ClientHello (ECH) is refused unless its outer name is the requested host. DNS stays open on GKE, as the network policy needs it to find the proxy, so a sandbox can still leak small amounts of data in DNS queries. Tools that ignore `HTTPS_PROXY` (some JVM builds, raw sockets) simply fail in a proxied environment.

## Threat model: a compromised sandbox

The attacker controls everything inside one sandbox: the agent's process, its files, `~/.hangar/env` (so every command hangar runs there), its git binary and its network traffic. What it should not get:

| Goal | What stops it | Tests |
| --- | --- | --- |
| The upstream GitHub token | Never written into a gateway-mode sandbox (env, files, harness configs). Gateway errors carry exception types, never values; upstream redirects aren't followed or relayed; the host segment must equal the grant's host and a configured upstream, so it can't be pointed elsewhere. | `test_gateway_git.py` |
| A push outside its grant | The command list is parsed before any pack byte goes upstream: one deadline for the whole list, at most 8 commands, no NUL tricks, no special pkt-lines, strict ref names, no deletes, no tags, no signed pushes, and no push options in any spelling (git enables them for `push-options=<anything>` too). | `test_gateway.py`, `test_sandbox_security.py` |
| Another job's or session's branch | Exact grants for the sandbox's own branch. The `hangar/**` creation rule can't touch a branch another live sandbox, any session or any unfinished job (queued ones included) owns, can't create one that blocks it (`a/b` vs `a/b/c`), and can't create anything under `hangar/session/`. A new session starts from its branch if it exists, so a planted branch would otherwise become its starting point. | `test_sandbox_security.py` |
| A dead or foreign grant | Grants are 256-bit, stored hashed, compared in constant time, and stop working when their sandbox stops, pauses, expires or is destroyed. A fork gets its own. 30 invalid grants a minute from one address get 429 without a lookup. | `test_gateway_git.py`, `test_sandbox_security.py` |
| The control plane's attention | 8 requests in flight per grant (counted on the same Authorization header the gateway authenticates), deadlines on reading push and fetch requests, push size cap, a single pass over the command list. What hangar reads back is bounded where it is read, not by sizes the sandbox reports (its `~/.hangar/env` can redefine `stat` and `find`): command output keeps a bounded tail as it streams (Docker, Kubernetes), and every file hangar reads from a sandbox is read with a cap (Docker, Kubernetes, E2B): results, questions, artifacts, checkpoint parts and run logs; rotated auth files written back on release and login captures (1 MB; a bigger one is not stored); `~/.hangar/env` (1 MB; a bigger one is rewritten); harness configs hangar merges into (8 MB; a bigger one is left alone, with a warning); workspace snapshots (`HANGAR_SNAPSHOT_MAX_MB`, 512); and the MCP `read_file` tool (its first 10 MB). | `test_sandbox_security.py`, `test_e2e_sandbox_limits_docker.py`, `test_sweep_caps.py` |
| The requester's machine (`session pull --checkout`) | The untracked-files archive is the sandbox's: only regular files, directories and in-tree symlinks are restored, never under `.git` (any spelling), never through a symlink (including one the session's patch created), never over an existing file, at most 20,000 entries and 1 GB (the archive is capped compressed; gzip expands ~1000x). The checkpoint's head must be a commit id. The patch goes through `git apply`, which refuses `.git` paths. | `test_sandbox_security.py` |
| The dashboard's origin | Artifacts are served as attachments with `Content-Security-Policy: sandbox` and `nosniff`; active types are downgraded to `text/plain`. | `test_exchange.py` |
| Sending code or tokens to a host of its choosing (`allowlist`/`none` environments) | The network admits only DNS, the git gateway and the egress proxy. The proxy authenticates the sandbox and checks each connection's host against the sandbox's allowlist, then checks the TLS SNI or HTTP `Host` against it, so fronting by SNI fails. Approvals come from the owner, never from the agent. Every denial is audited and shown in the session, and allowed traffic is audited per host. | `test_egress.py`, `test_k8s_network.py` |
| The VPC, the cluster or the metadata server through the proxy | The proxy resolves names itself and connects only to the public address it checked (a name that re-resolves later doesn't matter). Loopback and link-local addresses are refused even for `HANGAR_EGRESS_PRIVATE_HOSTS`. | `test_egress.py` |
| Cloud credentials | GKE node pools use `GKE_METADATA` (Workload Identity), so pods never see the node's service account; sandbox pods mount no Kubernetes token. `sandbox-network.yaml` blocks the metadata server, which matters with Pod snapshots: their KSA can read every sandbox's snapshot. | manifests checked with a server-side dry run |
| Other sandboxes and the API | GKE: no ingress to sandbox pods (except from hangar's pods to the [envd](envd.md) port), no egress to private ranges; only the gateway port of hangar pods is open. Docker: no traffic between sandboxes on hangar's network. | manifests checked with a server-side dry run and `test_k8s_network.py` (every policy file read together); `test_e2e_isolation_docker.py` |
| A turn's driver and the approvals it relays | Docker sandboxes whose agent has no sudo run each turn split ([the uid split](steering.md#the-uid-split)): the driver, its key, its control files and the turn's script belong to a `hangar` user, the agent's harness runs as the sandbox user with no capabilities, so code a person allowed can't read the key, write a command or signal or trace the driver. The harness and what it starts run with `no_new_privs`; hangar reads a split run's status, log and approvals from its own files, never through the agent's links. In `ask` and `deny-risky` turns hangar also owns the harness's binary, environment and settings ([hangar-owned harness settings](steering.md#hangar-owned-harness-settings)). Elsewhere one user, with a warning on `ask` and `deny-risky` turns (`HANGAR_UID_SPLIT=required` refuses them). | `test_e2e_isolation_docker.py`, `test_e2e_harden_docker.py`, `test_isolation.py`, `test_harden.py`, `test_harden_real_clis.py` |
| Another sandbox's [envd](envd.md) daemon | A daemon accepts only requests signed with its own sandbox's token: HMAC(vault key, sandbox id), never stored and never sent. Each request carries an HMAC over its time, a nonce, method, path and body hash, usable once and for five minutes, so a request that reaches the wrong address (a reused IP) gives whoever got it nothing; a peer without the token is refused before its body is read and can't spend nonces, and connections are capped per address and closed when idle. Answers must carry an HMAC proof of the token, so nothing else at a sandbox's address is taken for its daemon. What a daemon returns is capped (message size, output tail, read limit), never decompressed, and waited for only so long. | `test_envd.py`, `cargo test` in `envd/` |

What is **not** covered:

- **Docker on a laptop or VM is a trusted-use setup.** Sandboxes can't reach each other's ports ([networking](#sandbox-networking-on-docker)), but they can reach the host (and the API port if hangar listens beyond loopback), and get `sudo` unless the environment sets `sudo: false`. On a cloud VM, block the metadata server as the single-VM install does (the rule above). For untrusted work use GKE (gVisor/Kata).
- **Inside the sandbox the agent is in charge.** Hangar's own reads and writes there (attachments in, results and artifacts out) follow whatever the agent left in place. On Docker with `sudo` that grants nothing new; with `sudo: false`, a symlink placed in `$HANGAR_IO` can make hangar write an attachment, or read a file, as root inside that container.
- **Secrets the sandbox already holds.** The model credential is still injected. Redaction masks stored values in results, artifacts and logs, not encoded copies. A grant the agent copies out works from anywhere that reaches the gateway until its sandbox stops.
- **What is in the pushed branch.** Review PRs, and keep secrets out of `push`-triggered workflows.
- **Egress.** Sandboxes in `open` environments (the default) can reach the public internet. In `allowlist` and `none` environments, data can still go *to* an allowed host: package registries and GitHub accept uploads, and URLs carry data. On GKE it can also go out in DNS queries ([what egress doesn't stop](#egress-allowlists)).
- **Predictable custom branch names.** A session with a custom branch outside `hangar/session/` can be pre-created by another sandbox before the session exists, and a session continues an existing branch. (A job's branch is reserved from the moment the job is submitted.)
- **E2B command output.** The E2B SDK buffers a command's whole output before hangar sees it, so on E2B a sandbox can still make one of hangar's commands return a very large output.
- **Idle connections.** The gateway listener has no connection cap (uvicorn's counts idle sockets and would let one sandbox lock out the rest), so many idle connections from one sandbox are bounded only by the process's file limit.

## People

One hangar serves a team: run one per team rather than splitting one between teams. Every token belongs to a person, and a person is an **admin** or a **member**.

| | Admin | Member |
| --- | --- | --- |
| Sandboxes, sessions, jobs, snapshots, schedules | everyone's | their own; anyone else's answers "not found", by id or by name |
| Environments | create and change any | create new ones and change their own; `secrets`, `image`, `base`, `runtime_class` and `credential_mode: direct` need an admin |
| Credentials | all; add team credentials (`hangar cred add --team`), assign one to a person (`hangar cred owner`) | the team credentials and their own (`hangar cred add` stores it as theirs) |
| Tokens | mint for anyone, within that person's role | their own: `user` (their login), `mcp` (what `hangar connect` mints), `jobs` |
| REST | everything | `/api/me`, `/api/connect`, their tokens and credentials; jobs through a `jobs` token |

- **A person's own credential** (their Claude or Codex login) goes only into their sandboxes. A sandbox for someone who has their own credential for its harness uses it in place of the team's; anyone else gets the team's. Naming another person's credential is refused. A repo's policy can require one or the other (`credentials: person | team | person_then_team`).
- **Team credentials** are added by an admin on purpose (`hangar cred add --team`); an admin's credential without `--team` is their own. Jobs no person owns use them: webhooks, schedules without a person, and GitHub and Slack senders linked to nobody. They are API keys, or other credentials the vendor's terms allow for automation: a subscription login (Claude Pro or Max, ChatGPT) is refused as a team credential, since the vendors' terms make it one person's and Claude Code's forbid routing requests through a Pro or Max login on behalf of others ([team.md](team.md#what-the-vendors-allow)).
- **Ownership is recorded** as `person` on sandboxes, snapshots, sessions, jobs and schedules. A job's session and sandbox, a fork and a best-of-N attempt are their source's person's. Work from a GitHub, Slack or Jira account an admin linked to a person (`hangar person link`, by ids that don't change hands: a GitHub user id, a Slack workspace and user id, a Jira account id) is that person's: their caps, their budget, their lists. A linked account acts as its person, so it is trusted at every front door, and only it continues that person's jobs there ([team.md](team.md#who-is-trusted)). Work from anyone linked to nobody, and from inbound webhooks, has no person, so only admins see it; the front door's own rule decides whether it waits for approval.
- **Existing installs keep working, and the owner's logins stay the owner's.** Tokens minted before people, the server's admin token and the bootstrap token belong to `owner`, an admin. Records made before have no person, so they are the admins'. Credentials stored before people become the owner's own on the first start, once (`hangar cred migration` shows which), so your Claude or Codex login never reaches a member's sandbox or a job no person owns; control-plane ones stay the control plane's. Add team credentials for everyone else ([team.md](team.md)).
- **Removing someone** revokes their tokens (OAuth grants they approved stop with them). Their work stays, visible to admins; their own credentials go into nobody's sandbox until an admin reassigns them. Demoting an admin drops the `admin` scope from their tokens at once.
- **Visibility is enforced at the front doors** (MCP tools and resources, REST), by the record each tool call names, so a new MCP tool with a `sandbox`, `session`, `job`, `ref`, `source` or `schedule` argument is covered without a change.

## OAuth connectors

- **Scope:** OAuth access tokens reach `/mcp` only, never the REST API or the dashboard.
- **Lifetime:** every approval is tied to the hangar token that approved it and stops working when that token is revoked or the app is disconnected. Approvals last at most 90 days.
- **Registration:** limited to allowlisted redirect hosts (`HANGAR_OAUTH_REDIRECT_HOSTS`; an empty list allows none, `"*"` any) and capped in number. Redirect URIs must be `https`, `http` to `localhost`, `127.0.0.1` or `[::1]`, or a native app's own scheme (RFC 8252). A host entry (`claude.ai`) covers `https` and its subdomains; a native app's scheme needs an exact `scheme://host` entry, so listing `claude.ai` doesn't let an app register `evil://claude.ai`. RFC 8252 §7.1's reverse-domain form, which has no host (`com.example.app:/oauth2redirect`), needs an exact `scheme:` entry (`com.example.app:`). The consent page names the scheme of a non-web redirect (`cursor://anysphere.cursor-mcp`). Schemes a browser handles itself (`javascript:`, `data:`, `file:` and the like) are never allowed. A client that asks for several redirect URIs is registered with the allowed ones (RFC 7591 §3.2.1); it is refused only when none is allowed.
- **Cursor:** `cursor://anysphere.cursor-mcp` is in the default list. Any app on the user's machine can claim a custom scheme, just as any app there can listen on the loopback ports native clients use (also allowed by default), so the scheme adds no new exposure: the code goes to an app on the machine of the person who approved, PKCE (`S256`) binds it to the client that started the flow, and approving needs a hangar login token. Cursor also asks for `https://www.cursor.com/agents/mcp/oauth/callback`; that sends the code to Cursor's servers, so it is left out unless you list `www.cursor.com`.
- **Audience:** a client may ask for a token only for hangar's own MCP endpoint (`resource` = `HANGAR_PUBLIC_URL` or `…/mcp`, RFC 8707); any other resource is refused at `/authorize`.
- **PKCE:** `S256` is required, and the code is bound to its redirect URI (the MCP SDK's token endpoint checks both).
- **Consent page:** needs an admin token or a person's login (`user`) token, can't be framed, and isn't cached. The grant acts as that token: a member's connector sees their own work.

## Front doors

Work enters hangar through REST, MCP, A2A, GitHub webhooks, Slack, signed inbound webhooks and schedules. Each one decides who may start work, and every one of them ends in the same job service, which applies the limits below.

| Front door | Who can start or steer work | Fence |
| --- | --- | --- |
| REST, A2A | An admin token: anything. A `jobs` token: its own jobs only (another token's job id or A2A `contextId`/`taskId` is "not found") | `jobs` tokens: repos in `HANGAR_GITHUB_REPOS` and their environments, no provider choice, their `max_budget` |
| MCP | Admin tokens, and people's `user` and `mcp` tokens; an OAuth grant acts as the token that approved it | A member's tokens reach only their own work ([People](#people)); `mcp` tokens can't set `secrets`, `image`, `base`, `runtime_class` or `credential_mode: direct` on an environment, or call admin-only tools |
| GitHub | Linked accounts and trusted people ([triggers.md](triggers.md#1-who-can-start-work-from-github)); anyone else's trigger waits for approval, and their comments can't continue a job or answer its question. A person's job is continued only by that person's linked account | mapped repos, their environments |
| Slack | Linked accounts; members of your workspace in a mapped channel (or `HANGAR_SLACK_USERS`); Slack Connect guests from other organizations only when listed. A person's job is continued only by them | like a `jobs` token |
| Inbound webhooks | Whoever holds the source's HMAC key | the source's configured repo and environment |
| Schedules | Admin tokens create them | the template, validated at creation |

Text that hangar's caller didn't write is fenced in the prompt as untrusted: issue bodies and comments by untrusted authors, an untrusted person's `/hangar` request, Slack thread context, and everything from an inbound webhook ([triggers.md](triggers.md#untrusted-text-in-the-prompt)).

**Agents in `credential_mode: direct` can reach the GitHub front door.** Without the [git gateway](#git-credential-gateway), a sandbox holds a GitHub token. The account behind it has write access to the repo, so by default (author association) its comments are trusted. An agent can then comment `/hangar approve` on an outsider's job, start new jobs, or continue another job's PR as that account. Use the gateway for GitHub-triggered work, or set `HANGAR_GITHUB_TRUSTED` and leave that account out.

### Signed requests

- GitHub (`X-Hub-Signature-256`), Slack (`v0`) and inbound webhooks (per-source HMAC) are verified over the raw body, before anything parses it, and compared in constant time (as bytes, so a malformed header is a 401, not an error).
- **Replays.** A signed request is identified by what its signature covers, and remembered in the store, so a restart doesn't forget it. GitHub deliveries and inbound webhook deliveries are kept for `HANGAR_WEBHOOK_REPLAY_HOURS` (7 days); a replay with a fresh, unsigned delivery id or timestamp header is still a duplicate and does nothing. Slack events are deduplicated by `event_id`, and slash commands by signature within Slack's five-minute window.
- A missing secret is reported as "not stored" without naming the credential.

### Outbound webhooks (SSRF)

Job callbacks and A2A push notifications go only to public `https` hosts unless the host is in `HANGAR_JOB_CALLBACK_HOSTS`. Just before each send, the name is resolved, every address is checked, and the connection is made to the checked address itself (TLS still verifies the name). A name that re-resolves to `127.0.0.1` or a metadata address between the check and the connection (DNS rebinding) reaches nothing. Redirects aren't followed, and proxy environment variables are ignored for these requests.

### Front-door limits

| Limit | Default | Applies to |
| --- | --- | --- |
| `HANGAR_JOB_RATE_PER_MINUTE`, `HANGAR_JOB_RATE_BURST` | 30, 10 | Job submissions and follow-ups per caller: a `jobs` token, a GitHub user, a Slack user, a webhook source. A token bucket: `BURST` at once, refilling at `PER_MINUTE`. |
| `HANGAR_JOB_SOURCE_RATE_PER_MINUTE`, `HANGAR_JOB_SOURCE_RATE_BURST` | 120, 60 | The same per front door (`github`, `slack`, `webhook`, `a2a`, `api`), across all its callers. Submissions that wait for approval (untrusted GitHub people) have a bucket of their own per front door, so outsiders can't use up the one trusted people need. |
| `HANGAR_JOB_MAX_ACTIVE_PER_OWNER` | 10 | Unfinished jobs per caller, counting those waiting for approval or input |
| `HANGAR_A2A_MAX_STREAMS_PER_OWNER` | 20 | Open A2A streams and blocking sends per token |
| `HANGAR_MAX_REQUEST_MB` | 100 | Request bodies from a token on a route it may use: an `admin` token anywhere on `/api` and `/mcp`, a `jobs` token on `/api/jobs*`, an `mcp` token (or an OAuth token) on `/mcp` (jobs can carry git bundles); `/a2a` uses `HANGAR_A2A_MAX_REQUEST_BYTES` for `admin` and `jobs` tokens |
| | 64 KB | Request bodies without a valid token on `/api`, `/mcp` and `/a2a` (they get a 401 anyway), from a token on a route its scope doesn't reach (it gets a 403 anyway), and the OAuth endpoints |
| | 2 MB, 1 MB | `/api/hooks/github`, `/api/hooks/slack/*`; inbound webhooks keep their own setting. GitHub sends up to 25 MB, but the events hangar acts on (`issues`, `issue_comment`) carry bodies of at most 65,536 characters; a bigger delivery of another event gets a 413, which changes nothing since hangar would ignore it |

Admin tokens and schedules aren't rate limited or capped: an admin token can run anything already. Set a rate to 0 to turn it off. REST answers a rate-limited request with `429` and `Retry-After`; other front doors say so in their own way (a JSON-RPC error, a message in the Slack thread, a 429 to the webhook sender). Buckets live in memory, so a restart refills them.

Bodies are capped before the route runs, because FastAPI parses a JSON body before it checks the token.

### Audit

Every approval decision (`job.approve`, `job.reject`, `job.approval_expired`, `job.approve.refused`) and every refused request is in the audit log (`GET /api/audit`, the dashboard's audit view):

- `request.rejected`: any 401, 403, 413 or 429 answer, with the client address, method and path;
- `job.refused`: a submission or follow-up that was refused, with why (`rate_limited`, `too_many_active`, `invalid`, which covers a token's fence) and the caller;
- `hook.replay`: a duplicate signed delivery;
- `github.refused`: an untrusted person's attempt to continue a job or answer its question.

Refusals are themselves limited to `HANGAR_AUDIT_REJECTIONS_PER_MINUTE` (30) per client address or caller, so a flood can't fill the audit log; the next entry that gets through records how many were dropped (`suppressed_before`).

## Operational advice

- Keep the control plane private: localhost, an IAP tunnel, Tailscale, or IAP on the GKE ingress.
- Use a separate token per client so you can revoke one without affecting the rest. The audit log records which token did what. `hangar connect` does this for you: each client on each machine gets its own token with the `mcp` scope, which works at `/mcp` and nowhere else. A leaked client config can then drive sandboxes, but it can't call the REST API to read or change the vault, settings or tokens, or approve OAuth apps, and it can't map vault secrets into an environment. Sandboxes it starts can still use the stored credentials meant for sandboxes (never the control plane's own), so revoke a leaked token at once (`hangar token revoke`).
- Give E2B SDK clients tokens with the `sandboxes` scope, which work only on the [E2B-compatible API](e2b.md) and its sandbox URLs, for the sandboxes the token created. Their sandboxes get no stored credentials unless the environment is in `HANGAR_E2B_TRUSTED_ENVIRONMENTS`.
- Settings stored with `hangar config` can't hold secrets: hangar refuses the admin token, the vault key and provider keys there. Settings that point at secrets name a vault credential instead.
- Set `HANGAR_SECRET_KEY` explicitly in clusters, from Secret Manager or a Kubernetes Secret, so the vault survives pod restarts.
