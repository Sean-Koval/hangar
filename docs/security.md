# Security model

Hangar exists to run untrusted, model-generated code. The design assumes any sandbox can be fully compromised by the agent inside it.

| Boundary | Mechanism |
| --- | --- |
| Sandbox ↔ host | runc (laptop, trusted use), gVisor user-space kernel, or Kata/Firecracker microVM with its own guest kernel |
| Sandbox ↔ sandbox | Separate containers/pods/VMs; no shared volumes; per-sandbox credentials |
| Sandbox ↔ network | GKE: default-deny to RFC1918 + metadata server. Docker: use `HANGAR_DOCKER_NETWORK` with an egress-filtered network |
| Client ↔ control plane | Bearer tokens (hashed at rest), single-use tickets for browser streams, TLS via ingress, optional IAP in front |
| Secrets | Fernet-encrypted vault; never in images, API responses, MCP results, logs, or audit entries |

## Credential handling

- **Delivery.** Secrets are written into a running sandbox at claim time, to `~/.hangar/env` (mode 0600) or to the harness's own file. Sandbox images never contain them.
- **Forks.** A fork scrubs inherited secret files before re-injecting only the credentials it was granted.
- **Exclusive credentials** are ones with rotating refresh tokens, like a Codex ChatGPT login. Each is leased to one sandbox. When that sandbox stops, the possibly rotated file is copied back into the vault.
- **Snapshots** of a running sandbox (Docker `commit`) may contain secrets that were present at snapshot time. They stay in the local image store and are never pushed.
- **Exposure inside the sandbox.** Anything you inject is readable by the agent in that sandbox, and by any MCP client holding a hangar token (`exec`/`read_file` can read `~/.hangar/env`). Prefer scoped credentials: fine-grained GitHub tokens limited to the target repo, and API keys with spend limits. For git, use the [gateway](#git-credential-gateway): the token then never enters the sandbox.
- **Forks** run `tmux kill-server` before scrubbing secret files, so shells that exported the parent's secrets don't survive into the copy. A full memory snapshot (E2B) still carries any secrets held in other long-running processes' memory, so fork from a clean point when it matters.
- **Audit redaction.** Commands, prompts and terminal input are recorded in the audit log with stored credential values masked as `***`. Secrets that were never stored in the vault can't be recognized, so don't paste unrelated secrets into agent terminals.
- **Browser streams.** The dashboard never puts its token in a URL. Websocket terminals and build-log streams use single-use tickets that expire after 60 seconds.

## Git credential gateway

By default (`credential_mode: direct`) a sandbox gets the GitHub token itself, in `GH_TOKEN` and `~/.git-credentials`, and can push to any branch that token reaches. With the gateway, the token stays in the control plane:

- The sandbox gets a **grant**: an `hgs_…` token that works only at hangar's gateway, only for the sandbox's repo, and only while the sandbox runs. Hangar stores its hash; the token itself is written once, to the sandbox's `~/.git-credentials`.
- The sandbox's git is pointed at the gateway with `url.<gateway>/git/github.com/.insteadOf https://github.com/` (and the `git@github.com:` and `ssh://` forms). Clones, fetches and pushes keep working with the usual URLs.
- The gateway reads each push's ref updates before any of the pack is forwarded. It allows only the sandbox's own branch (the job or session branch, or `work_branch`), plus *creating* new branches under `refs/heads/hangar/` (`HANGAR_GATEWAY_PUSH_REFS`); a branch a sandbox created that way becomes its own, and nobody else can update it. It refuses pushes to other branches, deletes, tags (`HANGAR_GATEWAY_ALLOW_TAGS`), push options and signed pushes. Git shows the reason: `! [remote rejected] main -> main (hangar: this sandbox may push only to hangar/job-…)`.
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
- It doesn't cover: what is *in* the pushed branch (review the PR; keep secrets out of `push`-triggered workflows), the GitHub REST API from inside the sandbox (not proxied yet), egress filtering (a sandbox can still reach the internet unless your network policy says otherwise), or the model credential, which is still injected.
- Force pushes to the sandbox's own branch are allowed (agents rebase); force pushes anywhere else are refused like any other update. Keep branch protection on `main` as a second wall.
- Inside the cluster the gateway speaks plain HTTP, so the grant crosses the pod network unencrypted. It is worth little: it dies with the sandbox and reaches one branch of one repo.

## OAuth connectors

- **Scope:** OAuth access tokens reach `/mcp` only, never the REST API or the dashboard.
- **Lifetime:** every approval is tied to the hangar token that approved it and stops working when that token is revoked or the app is disconnected. Approvals last at most 90 days.
- **Registration:** limited to allowlisted redirect hosts (`HANGAR_OAUTH_REDIRECT_HOSTS`; an empty list allows none, `"*"` any) and capped in number. Redirect URIs must be `https`, `http` to `localhost`, `127.0.0.1` or `[::1]`, or a native app's own scheme (RFC 8252, e.g. Cursor's `cursor://anysphere.cursor-mcp/...`; list its host). Schemes a browser handles itself (`javascript:`, `data:`, `file:` and the like) are never allowed.
- **Audience:** a client may ask for a token only for hangar's own MCP endpoint (`resource` = `HANGAR_PUBLIC_URL` or `…/mcp`, RFC 8707); any other resource is refused at `/authorize`.
- **PKCE:** `S256` is required, and the code is bound to its redirect URI (the MCP SDK's token endpoint checks both).
- **Consent page:** needs an admin token, can't be framed, and isn't cached.

## Front doors

Work enters hangar through REST, MCP, A2A, GitHub webhooks, Slack, signed inbound webhooks and schedules. Each one decides who may start work, and every one of them ends in the same job service, which applies the limits below.

| Front door | Who can start or steer work | Fence |
| --- | --- | --- |
| REST, A2A | An admin token: anything. A `jobs` token: its own jobs only (another token's job id or A2A `contextId`/`taskId` is "not found") | `jobs` tokens: repos in `HANGAR_GITHUB_REPOS` and their environments, no provider choice, their `max_budget` |
| MCP | Admin tokens, and OAuth grants approved with an admin token | none |
| GitHub | Trusted people ([triggers.md](triggers.md#1-who-can-start-work-from-github)); anyone else's trigger waits for approval, and their comments can't continue a job or answer its question | mapped repos, their environments |
| Slack | Members of your workspace in a mapped channel (or `HANGAR_SLACK_USERS`); Slack Connect guests from other organizations only when listed | like a `jobs` token |
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
| `HANGAR_MAX_REQUEST_MB` | 100 | Request bodies from a valid token on `/api` and `/mcp` (jobs can carry git bundles); `/a2a` uses `HANGAR_A2A_MAX_REQUEST_BYTES` |
| | 64 KB | Request bodies without a valid token on `/api`, `/mcp` and `/a2a` (they get a 401 anyway), and the OAuth endpoints |
| | 25 MB, 1 MB | `/api/hooks/github` (GitHub's own cap), `/api/hooks/slack/*`; inbound webhooks keep their own setting |

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
- Use a separate token per client so you can revoke one without affecting the rest. The audit log records which token did what.
- Set `HANGAR_SECRET_KEY` explicitly in clusters, from Secret Manager or a Kubernetes Secret, so the vault survives pod restarts.
