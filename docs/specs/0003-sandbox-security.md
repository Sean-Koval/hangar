# 0003: Sandbox security (git proxy, egress allowlists, token scopes)

Status: partly built (2026-10-15): branch protection, the git credential gateway (#44), egress allowlists (#97, #107) and token scopes in another shape (#122); the model-API proxy isn't scheduled. Was: draft. · Date: 2026-10-12.

> Part of a set. Ownership, cross-spec decisions and the combined build order are in [docs/specs/README.md](README.md); where this spec conflicts with that page, the page wins.

## 1. Summary

- Agents must never hold a credential that can write outside their own branch. hangar moves the GitHub token, and later the model credential, out of sandboxes and into a gateway in the control plane.
- Each sandbox gets a scoped, short-lived grant. The gateway checks every git push and every GitHub REST call against that grant before it attaches the real token.
- Egress goes through a per-policy allowlist proxy. NetworkPolicy lets sandboxes talk to that proxy and the gateway only.
- hangar's own tokens get real scopes (`read`, `operate`, `admin`) and per-token limits. The dashboard gets IAP/SSO, login rate limits and a CSP.
- Branch protection on `main` is step zero. It takes ten minutes and closes the worst hole today.

## 2. Problem and evidence

The first live run (GKE, 2026-10-11) showed that the sandbox boundary is good and the credential model is not.

What held: the metadata server, the Kubernetes API, the control plane and the kubelet were unreachable. gVisor, zero capabilities, no sudo, no service-account token.

What did not hold:

| Finding | Where it comes from in the code |
| --- | --- |
| The GitHub PAT is readable by the agent. | `harnesses.py:HARNESSES["git"]` (`post_inject`) writes `GH_TOKEN` and `~/.git-credentials`. `broker.py:Broker.inject` writes `~/.hangar/env`. |
| The PAT could push to `main` or merge PRs. `main` had no branch protection. | Nothing in hangar checks. `fleet.py:Fleet._setup_repo` only runs `git checkout -b <work_branch>` locally. Nothing enforces it. |
| The Claude OAuth token is readable by the agent. | `harnesses.py` `claude-code` slot `CLAUDE_CODE_OAUTH_TOKEN`, written by `Broker.inject`. |
| Egress to the public internet is open. | `docs/gke.md:114` (default SandboxTemplate policy allows the public internet). `providers/kubernetes.py:_pod_template` creates no policy. `providers/docker.py:_run_kwargs` only passes the global `docker_network`. `providers/e2b.py:E2BProvider.create` passes only `envs`. |
| Every hangar API token is full admin. | `auth.py:TokenAuth.create` defaults `scopes=["admin"]`. No code reads `ApiToken.scopes`. `app.py:actor` and `app.py:MCPAuthMiddleware` authenticate but never authorize. |
| OAuth has one scope. | `app.py:_setup_oauth` registers `valid_scopes=["hangar"]`. `hangar` grants all 31 MCP tools. |
| `read_file` can read secrets. | `docs/security.md` says `exec`/`read_file` can read `~/.hangar/env`. |
| The dashboard token sits in `localStorage` and the page loads scripts from CDNs. | `ui/app.js` (`store.token`), `ui/index.html:175-176` (jsdelivr), `:9-10` (Google Fonts). `app.py:index` sets no security headers. Only the OAuth consent page has them (`oauth.py:_HEADERS`). |
| There is no brute-force control. | `auth.py:TokenAuth.verify` and `oauth.py:consent_post` accept unlimited guesses. |

Two more facts shape the design:

- The agent runs with `--dangerously-skip-permissions` (`harnesses.py` `claude-code.run`). The sandbox is the only control. Anything the agent can reach, a prompt-injected agent can use.
- hangar runs as one process with one SQLite file (`deploy/k8s/deployment.yaml`: `replicas: 1`). A gateway that needs vault access has to live in that process, or share its store some other way.

## 3. Goals / non-goals

Goals:

1. An agent cannot merge, and cannot push anywhere but its own work branch, even with full control of its sandbox.
2. No long-lived GitHub secret in a sandbox. No model-provider secret in a sandbox, where the CLI allows it.
3. Egress is default-deny with a per-environment allowlist. Setup and build can differ from the agent phase.
4. hangar's own tokens are scoped and limited. OAuth grants never exceed the approving token.
5. A hardened dashboard.
6. Works on docker, kubernetes and e2b. Where a provider cannot do something, we say so.

Non-goals:

- Stopping prompt injection. We assume it succeeds and limit what it can reach.
- Commit signing. Open question 6.
- Multi-tenant isolation between mutually distrusting hangar users. One trust domain, many tokens.
- Replacing gVisor/Kata as the compute boundary.
- Auto-merge or CI waiting. A separate spec owns run results and the PR lifecycle. This one only provides the credentialed operations those need.

## 4. User stories

Orchestrator agent over MCP (token scope `operate`):

- O1. I create 3 sandboxes on `me/api`. Each can push only `agent/fix-flaky-N`. If a prompt-injected agent tries `git push origin HEAD:main`, the push is rejected and I see `gw.git.deny` in the audit log.
- O2. I call `open_pull_request(sandbox)` instead of scraping a URL from the agent's last message. The PR is a draft and I cannot merge it.
- O3. My token is limited to `repos: ["me/api"]` and 5 active sandboxes. A confused or injected orchestrator cannot spawn 50 sandboxes or point them at another repo.
- O4. I connect over OAuth with the baseline `hangar:read`. When I first call `create_sandbox`, the server challenges for `hangar:operate` and I re-consent.

Human on the dashboard:

- H1. I open the dashboard through IAP with my Google account. I never paste a token.
- H2. When I create a sandbox for a repo whose default branch is unprotected, I see a red banner with the exact settings to apply.
- H3. I see what each sandbox may reach: its branch, its egress policy, and the hosts it tried to reach and was denied.
- H4. I mint a read-only token for a teammate who only watches runs.
- H5. I revoke a sandbox's grant with one click. The agent loses git and model access at once.

## 5. Design

### 5.1 Overview

```
 sandbox (gVisor)                       control plane pod (one process, two ports)
 ┌──────────────────┐   :8788 gateway   ┌───────────────────────────────────────────┐
 │ git, gh, claude  │ ───────────────►  │ gateway app (no admin routes)             │
 │ holds: hgs_ grant│                   │  /git/…     smart-HTTP inspector          │
 │ (no real secrets)│                   │  /api/v3/…  GitHub REST allowlist         │
 │                  │                   │  /anthropic/…  model API proxy (phase 3)  │
 │                  │                   │ real tokens come from vault / App key     │
 │                  │   :3128 CONNECT   ├───────────────────────────────────────────┤
 │                  │ ───────────────►  │ :8787 main app (MCP, REST, dashboard)     │
 └──────────────────┘   egress proxy    └───────────────────────────────────────────┘
        │               (own pod, per policy)                       ▲ ingress, TLS, IAP
        └─ NetworkPolicy: only the proxy and the gateway
```

Decisions and why:

- **Gateway in the control plane process, on its own port (8788).** It needs the vault and the store. The store is SQLite on one PVC. A separate pod would need a second copy of the secrets or a new RPC. A second listener, with an app that mounts only gateway routes, keeps the sandbox-facing surface small. The ingress exposes 8787 only. A `hangar-gateway` Service exposes 8788 inside the cluster. If hangar ever goes multi-replica, the gateway can split out and call the control plane.
- **Egress proxy is a separate pod per policy.** It holds no secrets and does only hostname filtering. See 5.5.
- **Sandboxes get a grant, not a secret.** A grant is useless outside the gateway and dies with the sandbox.

### 5.2 Step zero: branch protection

Apply this to every repo agents push to, before anything else ships. Use a repository ruleset (preferred) or classic branch protection with the same settings.

Ruleset on the default branch (`~DEFAULT_BRANCH`):

| Setting | Value |
| --- | --- |
| Enforcement | Active |
| Bypass list | Empty. Do not add the repo admin role. |
| Restrict deletions | On |
| Block force pushes (non-fast-forward) | On |
| Require a pull request before merging | On |
| Required approvals | 1 |
| Dismiss stale approvals on new commits | On |
| Require approval of the most recent reviewable push | On |
| Require review from code owners | On if `CODEOWNERS` exists |
| Require status checks | On, with your test job as required (for hangar: the pytest job) |
| Require branches to be up to date | On |
| Require linear history | Optional. Recommended for a solo repo |
| Block creation/update of `.github/workflows/**` by non-admins | Use CODEOWNERS on `.github/` as the practical equivalent |

Also, in repo settings: Actions → require approval for workflows from outside collaborators; "Allow GitHub Actions to create and approve pull requests" Off; delete head branches on merge On.

Solo-owner trap. If the PAT belongs to the repo owner, agent PRs are authored by the owner. GitHub does not let authors approve their own PR, so "1 approval" deadlocks. Two fixes: (a) use a GitHub App identity for agent commits and PRs (5.3), which is the recommended one; (b) set approvals to 0 and rely on the required status check plus the gateway's refusal to merge. (b) is weaker. It is acceptable only while the PAT path is in use.

Check and warn. New module `hangar/repo_guard.py`:

```python
class Protection(BaseModel):
    status: Literal["protected", "partial", "unprotected", "unknown"]
    default_branch: str
    missing: list[str]          # e.g. ["pull_request", "required_status_checks", "non_fast_forward", "deletion"]
    checked_at: float
async def check_protection(repo: str, token: str) -> Protection
```

It calls `GET /repos/{owner}/{repo}` for `default_branch`, then `GET /repos/{owner}/{repo}/rules/branches/{default_branch}` (active rules that apply to the branch, ruleset-based) and, if that is empty, `GET /repos/{owner}/{repo}/branches/{default_branch}/protection` (classic; needs admin on the token, so a 403 or 404 means "unknown", not "unprotected"). It maps the rule types `pull_request`, `required_status_checks`, `non_fast_forward` and `deletion` onto `missing`. It caches for 10 minutes per repo.

Where it runs: `Fleet.create`, before the sandbox starts, when `repo` is a GitHub URL. Setting `HANGAR_BRANCH_PROTECTION=off|warn|enforce`, default `warn`.

- `warn`: `create_sandbox` still succeeds and returns `warnings`. The dashboard shows a banner. An audit event `repo.unprotected` is written.
- `enforce`: unprotected and partial repos raise `FleetError` with the settings table above. `unknown` is a warning, not a refusal.
- With the gateway on, `warn` stays the default, because the gateway is the primary control. Protection is the second layer for when the gateway or the token is wrong.

### 5.3 Git credential proxy

#### Grants

```python
# models.py
class SandboxGrant(BaseModel):
    id: str = Field(default_factory=lambda: new_id("gnt"))
    sandbox_id: str
    token_hash: str                  # sha256 of "hgs_<random>"; only the hash is stored
    repo: str                        # "owner/name", lower-cased
    host: str = "github.com"
    base_branch: str
    work_branch: str                 # the only ref this grant may write
    extra_read_repos: list[str] = [] # from env spec network.git_read, read-only
    git_credential: str              # name of a git_token / github_app credential
    model_credential: str | None = None
    author_name: str; author_email: str
    created_at: float; expires_at: float | None
    revoked_at: float | None = None
```

Lifecycle, inside `Fleet.create` (fills the gap between `broker.acquire` and `broker.inject`):

```
PENDING ─► STARTING ─► (grant minted) ─► inject ─► _setup_repo ─► RUNNING
                                                          └─ failure ─► ERROR ─► grant revoked
RUNNING ─► STOPPING ─► grant revoked ─► STOPPED         (destroy, reaper expiry, _mark_gone)
extend_sandbox ─► grant.expires_at moves with sandbox.expires_at
fork ─► new grant per fork, new work branch; parent grant untouched
```

- `Fleet.destroy`, `Fleet._mark_gone`, the reaper and the failure path in `create` all call `grants.revoke(sandbox_id)`. Revoke is idempotent.
- `create` requires `work_branch` when `credential_mode=proxied`. If the caller omitted it, hangar generates `agent/<sandbox-name>`. This is a small compatibility change: today a sandbox without `work_branch` stays on the base branch.
- Snapshots taken from a running sandbox contain the parent's `hgs_` token. It is bound to the parent's id and branch, and it dies with the parent. This is weaker leakage than a PAT in the same snapshot (`docs/security.md`, snapshot bullet).

#### What goes into the sandbox

`broker.inject` stops writing `GH_TOKEN` and `~/.git-credentials` for a proxied sandbox. The `git` harness post-inject becomes:

```sh
git config --global url."https://${GW}/git/github.com/".insteadOf "https://github.com/"
git config --global credential."https://${GW}".helper store
printf 'https://x-access-token:%s@%s\n' "$HGS" "$GW" > ~/.git-credentials; chmod 600 ~/.git-credentials
git config --global user.name "$NAME"; git config --global user.email "$EMAIL"   # from the grant, no `gh api user` call
export GH_HOST=$GW GH_ENTERPRISE_TOKEN=$HGS                                       # gh CLI, see below
```

`insteadOf` means `sb.repo` stays `https://github.com/org/repo.git`. `_setup_repo`'s `git clone` and all the agent's habits keep working. The identity lookup that `harnesses.py` does with `gh api user` moves into grant creation, which is done once per credential and cached.

SSH keys (`CredentialKind.SSH_KEY`) cannot be proxied. In `credential_mode=proxied` hangar refuses to attach an SSH key to a sandbox unless the token has `limits.allow_direct_credentials`. Egress policy also blocks port 22.

#### Smart-HTTP inspection

Routes on port 8788:

```
GET  /git/github.com/{owner}/{repo}.git/info/refs?service=git-upload-pack
POST /git/github.com/{owner}/{repo}.git/git-upload-pack
GET  /git/github.com/{owner}/{repo}.git/info/refs?service=git-receive-pack
POST /git/github.com/{owner}/{repo}.git/git-receive-pack
```

Auth is the Basic password, which carries the `hgs_` token (never a hangar token, never in a URL). Every request does the following:

1. Look up the grant by hash. Reject if revoked or expired (401).
2. Normalise `owner/repo` (lower-case, strip `.git`, reject `..`, `%2e`, extra path segments). It must equal `grant.repo`, or be in `extra_read_repos` and the service be `upload-pack`. Otherwise 404. Don't reveal whether the repo exists.
3. `upload-pack` and both `info/refs` calls: stream through with the real `Authorization` attached. Reads of any branch in the repo are allowed.
4. `receive-pack` POST: read the request body only up to the first flush-pkt (`0000`). That section is the command list: `<old> <new> <ref>\0<capabilities>` lines, optionally a `push-cert` and shallow lines. Parse it as pkt-lines. Rules:
   - Every command's ref must equal `refs/heads/<grant.work_branch>`. Anything else is rejected: other branches, `refs/tags/*`, `refs/pull/*`, `refs/heads/<base>`, and notes.
   - `new` must not be the all-zero id. Deleting the work branch is rejected.
   - Reject `push-options` capability, and any `Content-Encoding` other than identity.
   - Reject malformed pkt-lines, a command list over 64 KiB, or more than 8 commands.
   - If all commands pass, forward the buffered bytes plus the remaining stream (the packfile) to GitHub with the real token. The pack is never buffered in memory. Enforce a max body size (default 512 MiB).
5. On rejection, still drain the pack and answer with a valid `application/x-git-receive-pack-result` body: `unpack ok` and `ng <ref> hangar: <reason>` lines. Git then prints `! [remote rejected] main -> main (hangar: this sandbox may only push agent/fix-1)`, which the agent can read and act on. A bare 403 gives it only "returned error: 403". (To verify in the unit test; see 8.1.)
6. Audit: `gw.git.push` with ref, old, new, bytes. `gw.git.deny` with ref and reason. Reads are counted in metrics only.

Force pushes. The proxy cannot test ancestry, because the objects are not on GitHub yet. The work branch is the agent's own, so a force push there is allowed by default. Force pushes to any other ref are rejected, as all other refs are. Force push to protected refs is blocked twice: here, and by the ruleset's non-fast-forward rule. Open question 3 covers whether to deny force pushes on the work branch too.

What the proxy cannot see: file contents. A branch can contain a modified `.github/workflows/*.yml`. If CI runs on `push` and has secrets, the agent could exfiltrate them from the Actions runner. Mitigation: the real token (GitHub App or fine-grained PAT) must not have the `workflows` permission. GitHub then refuses pushes that touch workflow files. Also, keep secrets out of `push`-triggered workflows or behind environment approval. This is the same reason Copilot holds CI until a human approves.

#### Real token

Credential kind `github_app` (new `CredentialKind.GITHUB_APP`; vault value is JSON `{app_id, installation_id, private_key}`), or the existing `git_token`.

- **GitHub App (recommended).** The gateway mints an installation token per grant: `POST /app/installations/{id}/access_tokens` with `{"repositories": ["<repo>"], "permissions": {"contents": "write", "pull_requests": "write", "checks": "read", "metadata": "read"}}`. No `workflows`, no `administration`, no `actions`. Tokens last 1 hour. The gateway caches them in memory per grant, refreshes at 50 minutes, and never writes them to the store. Commits and PRs are authored by `<app>[bot]`, which also fixes the solo-owner approval trap in 5.2.
- **Fine-grained PAT (fallback).** One token for all grants. The proxy still enforces branch and endpoint rules. A leak of the control plane's vault would still expose the PAT. Use it for laptops.
- The App can't restrict writes to one branch. `contents: write` covers all branches. The proxy and the ruleset do that job, and that is why both exist.

Compatibility: `list_credentials` marks a `git_token` as `"proxied": true` when it is used through the gateway. `fork_sandbox` and `create_sandbox(credentials=[...])` behave as before; a credential in `credentials` with `harness=git` becomes the grant's `git_credential`.

#### REST API and the `gh` CLI

Two supported paths. Prefer the first.

1. **hangar opens the PR.** New MCP tool, REST endpoint and dashboard button call GitHub from the control plane with the grant's identity. The agent does not need `gh` at all.
2. **Allowlisted REST proxy.** For agents that run `gh pr create`, `gh run list`, `gh pr checks`. The sandbox gets `GH_HOST=<gateway host>` and `GH_ENTERPRISE_TOKEN=<hgs_>`. The `gh` CLI then talks to `https://<gateway>/api/v3/…`, which is its GitHub Enterprise Server layout. Verified: `gh` 2.45 is installed here. Not verified: that every `gh` subcommand works against a path-prefixed GHES host that is not really GHES (open question 2). The gateway TLS name must be a real hostname with a valid certificate (see 5.5 for how sandboxes reach it).

Allowlist (default deny; path matched after normalisation; `{r}` must equal `grant.repo`):

| Method and path | Condition |
| --- | --- |
| `GET /repos/{r}` | metadata only |
| `GET /repos/{r}/pulls`, `GET /repos/{r}/pulls/{n}` | any |
| `POST /repos/{r}/pulls` | `head == work_branch` (or `owner:work_branch`), `base == base_branch`, body forced `draft: true` unless the token's limits allow ready PRs. Records `n` in the grant. |
| `PATCH /repos/{r}/pulls/{n}` | `n` was created by this grant. Only `title`, `body`, `state: closed`. Reject `base`. |
| `GET\|POST /repos/{r}/issues/{n}/comments` | `n` created by this grant |
| `GET /repos/{r}/commits/{sha}/check-runs`, `…/status` | any |
| `GET /repos/{r}/actions/runs?branch=<work_branch>` | branch filter forced |
| `GET /repos/{r}/contents/{path}?ref=…`, `GET /repos/{r}/compare/…` | read only |

Everything else is denied with `403 {"message": "hangar: endpoint not allowed for sandbox grants"}` and audited as `gw.api.deny`. Specifically denied, because they are the ways to escape the branch: `PUT …/pulls/{n}/merge`, any `…/reviews`, `POST …/git/refs`, `PATCH|DELETE …/git/refs/*`, `PUT …/contents/*` (it writes commits to any branch), `…/dispatches`, `…/actions/workflows/*/dispatches`, `…/hooks`, `…/collaborators`, `…/branches/*/protection`, `…/secrets`, `/user`, `/graphql` (cannot be inspected cheaply), and `enablePullRequestAutoMerge` in any form.

#### New MCP tools (additive)

```python
@tool()
async def open_pull_request(sandbox: str, title: str, body: str = "", draft: bool = True,
                            base: str | None = None) -> dict[str, Any]:
    """Open a PR from the sandbox's work branch. Fails if the branch has not been pushed."""

@tool()
async def pull_request_status(sandbox: str) -> dict[str, Any]:
    """PR number/url, head sha, review state, mergeable flag, check runs, and how far behind base it is. Never merges."""
```

```json
// open_pull_request("api-fix-1", "Make test_sync deterministic", "…")
{"number": 41, "url": "https://github.com/me/api/pull/41", "draft": true,
 "head": "agent/api-fix-1", "base": "main", "author": "hangar-agents[bot]"}

// pull_request_status("api-fix-1")
{"number": 41, "url": "…", "head_sha": "9f2c1ab", "state": "open", "draft": true,
 "review": "none", "mergeable": "clean", "behind_by": 0,
 "checks": [{"name": "pytest", "status": "completed", "conclusion": "success"}]}
```

Changed outputs (additive keys only): `create_sandbox`/`fork_sandbox` and `get_sandbox` gain `git` and `network`.

```json
{"id": "sb-1f3a9c0e", "name": "api-fix-1", "state": "running", "repo": "https://github.com/me/api.git",
 "branch": "agent/api-fix-1",
 "git": {"mode": "proxied", "work_branch": "agent/api-fix-1", "base": "main",
         "protection": {"status": "protected", "missing": []}},
 "network": {"policy": "pkg-only", "phase": "agent"},
 "warnings": []}
```

REST: `POST /api/sandboxes/{ref}/pull-request`, `GET /api/sandboxes/{ref}/pull-request`, `GET /api/repos/protection?repo=owner/name`, `POST /api/sandboxes/{ref}/grant/revoke`.

### 5.4 Model-provider credential

Goal: the Claude subscription token never enters the sandbox. This is lower priority than git because the damage is smaller. `CLAUDE_CODE_OAUTH_TOKEN` is "model requests only" (comment in `harnesses.py`), so a leak burns the shared Pro quota but does not touch code. It still matters, since one leaked token drains the 7-day window of every sandbox at once (the live run showed 91% used).

What Claude Code supports. I checked the installed binary (`claude` 2.1.296) for strings, not behavior: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_CUSTOM_HEADERS`, `apiKeyHelper`, `CLAUDE_CODE_OAUTH_TOKEN`, `HTTPS_PROXY`, `NODE_EXTRA_CA_CERTS` and `CLAUDE_CODE_CLIENT_CERT` all appear. `--help` says `--bare` auth is strictly `ANTHROPIC_API_KEY` or `apiKeyHelper`. So pointing the CLI at a gateway is a documented pattern. Not verified: that subscription mode (`CLAUDE_CODE_OAUTH_TOKEN`) honors `ANTHROPIC_BASE_URL`, or that it works with a placeholder token. Open question 1.

Design:

- Sandbox env: `ANTHROPIC_BASE_URL=https://<gateway>/anthropic`, `ANTHROPIC_AUTH_TOKEN=<hgs_ grant>`. No `CLAUDE_CODE_OAUTH_TOKEN`, no `ANTHROPIC_API_KEY`.
- Gateway `/anthropic/{path:path}`: validate the grant, then forward to `https://api.anthropic.com/{path}` with the real credential: for an API key, `x-api-key`; for a subscription token, `Authorization: Bearer <sk-ant-oat…>` plus the OAuth beta header that the CLI normally sends (header name and value unverified; open question 1). Streaming (SSE) passes through without buffering. Request bodies are never logged.
- Allowed paths: `/v1/messages`, `/v1/messages/count_tokens`, `/v1/models`. Everything else 403.
- Usage and concurrency. The gateway is the only place that sees all sandboxes' model traffic, so it can protect the single Pro plan:
  - per-credential cap on concurrent streams (`HANGAR_MODEL_MAX_CONCURRENT`, default 4), queueing with a timeout, then `429` with `retry-after` so Claude Code backs off normally;
  - it copies the response's rate-limit headers (the source of the `rate_limit_event` in the stream-json log) into a `ModelUsage` row per credential (`utilization`, `resets_at`, `observed_at`) and exposes it in `fleet_overview` and `GET /api/gateway/usage`. The exact header names are unverified (open question 1);
  - `HANGAR_MODEL_STOP_AT` (default 0.95): `run_agent` refuses new runs above it with a clear error. It's the hook the run-result work needs to surface the "91%" case.
- Fallback and flag: `Credential.proxy: bool` (default `false` until proven). With `proxy=false` the credential is injected as today and `list_credentials` shows `"exposed": true`. Per credential, not global, so a misbehaving harness doesn't block the rollout.
- Other harnesses: Codex with a ChatGPT login stores a rotating `auth.json` that the CLI refreshes itself, so it cannot be proxied the same way. Codex with an API key and Gemini with an API key probably can (base-URL environment variables). I did not verify the variable names. Mark all as open question 4. Until then these credentials stay exposed and the dashboard says so.
- Terms of service. Routing a subscription token through a server you operate may be treated differently from the CLI talking to Anthropic directly. I could not verify the current terms. Open question 5. If the answer is no, the fallback is exposed injection plus per-sandbox API keys with spend limits, which `docs/security.md` already recommends.

### 5.5 Egress allowlists

Policy model (`envspec.py`, new optional block; absent means "use the server default"):

```yaml
network:
  policy: pkg-only            # none | pkg-only | custom | open
  allow:                      # agent phase, added to the preset
    - host: "docs.python.org"
    - host: "*.googleapis.com"
  methods: any                # any | read-only (GET/HEAD/OPTIONS); see tier 2
  setup_allow: ["github.com", "objects.githubusercontent.com"]   # extra hosts during repo.setup only
  git_read: ["me/shared-lib"]  # extra read-only repos through the git proxy
```

Presets, as data in `hangar/egress.py`:

| Preset | Allows |
| --- | --- |
| `none` | the gateway only |
| `pkg-only` (new default, once enforce ships) | gateway (git, GitHub REST, model API), `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `crates.io`, `static.crates.io`, `index.crates.io`, `proxy.golang.org`, `sum.golang.org`, `rubygems.org`, `index.rubygems.org` |
| `custom` | `pkg-only` plus `allow` |
| `open` | everything. Needs `limits.allow_open_egress` on the token. Logged. |

`github.com` is not in `pkg-only`: git goes through the gateway. `uv python install` and some build tools download from GitHub releases. They need `setup_allow` or a baked image. Telemetry hosts are not allowed. `harnesses.py` already sets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` for Claude Code, which keeps the list short.

**Two tiers of enforcement.**

- Tier 1, hostname allowlist, CONNECT only. The proxy sees `CONNECT host:443`, allows or denies by hostname and port, and tunnels bytes. No TLS interception, no CA injection. It cannot see methods or paths. Port 443 only (plus 80 for plain HTTP package mirrors).
- Tier 2, method restriction. To enforce `read-only` on HTTPS the proxy must terminate TLS for the listed hosts. That needs a hangar CA trusted in the image. The mechanism exists already: `Settings.extra_ca_bundle` is installed into every image (`fleet.py:Fleet._extra_ca`). Tier 2 is opt-in per policy and only for hosts that need it. Open question 8 asks whether it is worth the operational cost. Be clear about what it buys: `GET` can still carry data in a URL to any allowed host, so `read-only` lowers the bar but doesn't close the exfiltration leg. The Codex docs make the same point about methods.

**Proxy choice.** Smokescreen (Stripe) for tier 1: small, Go, hostname ACLs, refuses private and link-local destinations by default (a useful second wall against the metadata server). Squid or Envoy for tier 2 if we do it. Not verified here: how Smokescreen would receive a per-environment role; the design avoids needing that by running one proxy deployment per policy (below).

**Kubernetes (GKE Agent Sandbox).**

- One `Deployment` + `Service` per distinct policy, `hangar-egress-<policyhash>`, in the sandbox namespace, with a ConfigMap holding the rendered ACL. hangar renders and applies them when it builds an environment (`Fleet.build`), next to the SandboxTemplate.
- Sandbox pods get the label `dev.hangar/egress=<policyhash>` in `kubernetes.py:_pod_template`, and `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY` (gateway host) as container env.
- A `NetworkPolicy` per policy selects those pods and allows egress to exactly: the matching proxy pods (3128), the gateway Service (8788) and nothing else. No DNS: the proxy resolves names, so DNS tunnelling from the sandbox is closed too. Tools that ignore `HTTPS_PROXY` fail closed.
- **Problem:** NetworkPolicies are additive. The add-on's default policy already allows public internet. Another policy cannot take that away. The template therefore needs `networkPolicyManagement: Unmanaged` (the field `docs/gke.md` warns about) and hangar's own default-deny + allow policy, which must also block RFC1918 and the metadata IP itself. Allow-only-the-proxy covers both by construction. Update `docs/gke.md:105` accordingly, and re-run the same probes as the live run (metadata, kube API, control plane, kubelet) as an acceptance test. Whether the add-on's admission policy accepts `Unmanaged` templates: open question 7. GKE also has an FQDN NetworkPolicy resource on Dataplane V2. It is GKE-only, resolves names to IPs (shared CDN addresses make rules broad), and does not help docker or e2b, so it is not the primary design.
- RBAC: add `patch` on `pods` in `deploy/k8s/rbac.yaml` (for the phase switch below) and create/update for `networkpolicies`, `deployments`, `services`, `configmaps` in the sandbox namespace.
- Terraform: the sandbox namespace needs Dataplane V2 (already on; `deploy/terraform/gke/main.tf:49`).

**Setup phase vs agent phase** (the Codex model: setup has internet, the agent does not).

1. Build phase: image build (Docker build / Cloud Build) has the internet and no secrets. Nothing to change. This is where dependencies belong. The live run's `uv sync --all-extras` per sandbox should move here via `repo.bake: true` plus `repo.setup`, which already exists in `envspec.py`.
2. Setup phase: `Fleet._setup_repo` runs `repo.setup` after clone. During it the sandbox pod carries `dev.hangar/egress-phase=setup`, and a second NetworkPolicy additionally allows the proxy `setup_allow` list. When setup finishes, hangar patches the label to `agent` (this is the `pods patch` RBAC). The phase change is instant because NetworkPolicy is evaluated per packet.
3. Agent phase: `pkg-only`/`custom` allowlist as above.

Fallback if relabelling is blocked (the Sandbox controller may reconcile labels back; open question 7): union `setup_allow` into the agent list and log a warning.

**Docker (laptop / single VM).**

- Today there is one global network (`HANGAR_DOCKER_NETWORK`, `providers/docker.py:_run_kwargs`).
- New: hangar creates a per-policy network with `internal=True` (no route out) and a proxy container attached to it and to the default bridge. Sandboxes attach only to the internal network and get the `HTTPS_PROXY` env at `containers.run` time, via `_run_kwargs(environment=…)`. Warm-pool containers (`sync_warm_pool`) are created per policy, so they carry the right network.
- The control plane normally runs on the host and not in a container. The proxy container also publishes the alias `hangar-gateway`, forwarding TCP to the host's `:8788` (via `host.docker.internal` on Docker Desktop/OrbStack, the bridge gateway IP on Linux). Sandboxes can then reach the gateway and nothing else on the host.
- Setup phase: `docker network connect/disconnect` between a `setup` network and the agent network after setup. Costs about the same as the label patch.
- Laptop default stays `open` with a one-line warning until the user opts in. A single-VM install (`deploy/terraform/single-vm`) should default to `pkg-only`.
- `runc` laptop runs share the host kernel: egress rules do not protect against a container escape. That risk is the same as today.

**E2B.**

- Sandboxes are Firecracker VMs on E2B's network. No private network, so `HTTPS_PROXY` has to point at a public hostname. Gateway and proxy must be internet-reachable and authenticate the sandbox: the gateway already does (`hgs_` grant). Smokescreen does not authenticate callers, so for E2B the CONNECT endpoint is a gateway route (`CONNECT` with `Proxy-Authorization: Basic <grant>`, hostname ACL from the policy). It's the same ACL code, different front door.
- To make the proxy mandatory and not advisory, the VM's outbound traffic must be restricted at E2B's side. Newer E2B SDKs have sandbox network options (deny-all with allow lists). The SDK is not installed in this sandbox, so I could not check its parameters or whether it accepts hostnames or only CIDRs. Open question 9. Until verified, E2B egress is advisory (env-var only) and the dashboard says "egress: advisory".
- `providers/e2b.py:E2BProvider.create` passes only `envs` today; the fix is a small change there.

**Rollout mode.** `HANGAR_EGRESS=off|audit|enforce`. In `audit` the proxy allows everything and logs hostnames, so the allowlist is built from measured behavior: `GET /api/egress/denied?since=` returns would-be denials grouped by environment and host. This follows the "widen autonomy based on measurement" advice in Jarmak (2026).

### 5.6 Token scopes for hangar's API and MCP

Scopes, cumulative (`admin` ⊃ `operate` ⊃ `read`):

| Scope | Meaning |
| --- | --- |
| `read` | See fleet, environments, sandboxes, run output, files, terminals' scrollback, snapshots. Cannot change anything or run code. |
| `operate` | Create/destroy sandboxes, run commands and agents, terminals, files, snapshots/forks, build environments, open PRs. Everything the 31 MCP tools do today. |
| `admin` | Credentials (create, delete, release, login), tokens, environment editing, GC, OAuth client management, audit log. |

Data model (`models.py`):

```python
class TokenLimits(BaseModel):
    environments: list[str] | None = None   # globs; None = any
    credentials: list[str] | None = None    # which credentials a sandbox may attach
    repos: list[str] | None = None          # "owner/name" globs
    providers: list[str] | None = None
    max_active_sandboxes: int | None = None
    max_running_agents: int | None = None
    max_creates_per_hour: int | None = None
    max_ttl_minutes: int | None = None
    allow_direct_credentials: bool = False  # inject secrets instead of proxying
    allow_open_egress: bool = False
    allow_ready_prs: bool = False

class ApiToken(BaseModel):          # existing fields kept
    scopes: list[str] = ["admin"]   # existing default kept for old rows
    limits: TokenLimits = TokenLimits()
    expires_at: float | None = None
```

Existing rows deserialize unchanged (`scopes` already stored, `limits` defaults to unlimited). The old default `["admin"]` is kept for stored tokens. New tokens default to `operate`. The bootstrap token and `HANGAR_ADMIN_TOKEN` stay `admin`.

**Tool to scope map.** One dict in `mcp_server.py`, checked at import: a test fails if a registered tool is missing. Unknown tools fail closed (`admin`).

| Scope | MCP tools |
| --- | --- |
| `read` | `fleet_overview`, `list_environments`, `describe_environment`, `list_credentials`, `list_sandboxes`, `get_sandbox`, `list_files`, `read_file`\*, `terminal_list`, `terminal_read`, `agent_status`, `list_snapshots`, `pull_request_status` |
| `operate` | `build_environment`, `create_sandbox`, `create_sandboxes`, `destroy_sandbox`, `destroy_sandboxes`, `pause_sandbox`, `resume_sandbox`, `extend_sandbox`, `exec`, `write_file`, `terminal_open`, `terminal_send`, `terminal_close`, `run_agent`, `snapshot_sandbox`, `fork_sandbox`, `open_pull_request` |
| `admin` | none over MCP today. REST only: `/api/credentials*`, `/api/logins*`, `/api/tokens*`, `PUT /api/environments/{name}`, `/api/gc`, `/api/oauth/clients*`, `/api/audit` |

\* `read_file` needs `operate` while the target sandbox holds any directly injected secret (`credential_mode=direct`), because `~/.hangar/env` is readable by it. Once a sandbox is fully proxied it drops to `read`. `describe_environment` returns the Dockerfile; check that a spec holds no secrets (env specs are YAML in git, so they should not).

REST uses the same map via a `require(scope)` dependency replacing `A` in `app.py`; `GET /api/me` returns `{actor, scopes, limits}` so the dashboard can disable controls.

**Limits enforcement.** In `Fleet.create`/`fork`/`run_agent`, which receive an `actor`. Change `actor: str` to an `Principal(name, token_id, scopes, limits)` passed through, and keep `str(principal)` for audit so audit format is unchanged. `current_actor` in `mcp_server.py` becomes a `ContextVar[Principal]`. Counters (`max_creates_per_hour`) are in-memory sliding windows, rebuilt from audit events on start. Over-limit calls raise `FleetError("token 'ci' is limited to 5 active sandboxes")`, an expected error that the agent can read.

**Mapping the three entry points.**

- Bearer tokens: scopes from `ApiToken.scopes`. CLI: `hangar token create ci --scope operate --repo me/api --max-sandboxes 5 --expires 30d`. `TokenAuth.verify` returns a `Principal` and rejects expired tokens.
- OAuth (`app.py:_setup_oauth`, `oauth.py`): replace the single scope `hangar` with `hangar:read`, `hangar:operate`. `admin` is never grantable over OAuth, and OAuth tokens already reach `/mcp` only. The legacy scope string `hangar` is accepted and means `operate`, so existing connectors keep working. The consent page shows the requested scopes; the approving hangar token's scopes cap them (the grant is `min(requested, approver)`; no escalation). The grant also inherits the approver's `limits`.
- Step-up: `MCPAuthMiddleware` reads the JSON-RPC body of `tools/call` (stateless mode, one POST per call) and looks up the required scope. On a shortfall it returns `403` with `WWW-Authenticate: Bearer error="insufficient_scope", scope="hangar:operate", resource_metadata="…"`. This is the scope challenge from MCP spec 2025-11-25. Batches are checked per item. Dynamic registration's `default_scopes` becomes `hangar:read` (minimal baseline) only after we confirm the target clients handle step-up (open question 10). Until then the default stays `hangar` (= operate).
- No token passthrough: the gateway never forwards an `hgs_` or hangar token upstream, and it never accepts a hangar API token on port 8788. Grant tokens never work on 8787. Session IDs are not auth (the MCP server already runs `stateless_http=True`).
- Dashboard: the dashboard is a REST client with whatever token the user pastes; scopes apply. With IAP (5.7) the role comes from config.

### 5.7 Dashboard hardening

**IAP / SSO.** `HANGAR_AUTH_MODE=token|iap|both` (default `token`).

- Split hostnames. IAP protects a whole backend service, and MCP clients cannot do a Google login. So: `hangar.example.com` (IAP on; serves `/`, `/ui`, `/api`) and `mcp.example.com` (no IAP; serves `/mcp`, `/oauth`, `/.well-known`, bearer or OAuth). Two `Service`s and `BackendConfig`s over the same pods; `deploy/k8s/ingress.yaml` gets a second host and a second managed certificate. The app enforces the split with `HANGAR_DASHBOARD_HOST` and `HANGAR_MCP_HOST` (Host header check), so `/api` on the MCP host is 404 and `/mcp` on the dashboard host is 404.
- In `iap` mode `app.py:actor` accepts `X-Goog-IAP-JWT-Assertion`: verify the signature against Google's IAP keys, `iss=https://cloud.google.com/iap`, `aud=HANGAR_IAP_AUDIENCE` and `exp`. The principal is `iap:<email>`. Role comes from `HANGAR_IAP_ROLES` (`email → scope`, default `read`; an `*@domain` pattern is allowed). Without a verified JWT the request is 401: never trust a bare `X-Goog-Authenticated-User-Email`.
- CSRF: ambient browser auth (IAP cookie) means mutating REST calls need `Sec-Fetch-Site: same-origin` or a custom header `X-Hangar-CSRF: 1` (a header cross-site forms cannot set). Bearer calls are exempt.
- Websocket tickets (`TokenAuth.issue_ticket`) keep working; they're bound to the principal.

**Login rate limiting.** `auth.py`: `RateLimiter(window=60, max_failures=10)` keyed by client IP, applied to `TokenAuth.verify` failures, the OAuth consent token field (`oauth.py:consent_post`), and `/oauth/token`. Over the limit: `429` with `Retry-After`, escalating lockout (1, 5, 15 minutes), and a throttled `auth.fail` audit event. Client IP comes from `X-Forwarded-For` only if the peer is in `HANGAR_TRUSTED_PROXIES` (on GKE, the Google front-end range). In-memory is fine for one replica. Tokens are 256-bit, so this is about noise and cost, not guessing.

**CSP and headers.** A middleware sets, on every response from the main app:

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;
  font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Strict-Transport-Security: max-age=31536000   (when public_url is https)
Cache-Control: no-store                        (on /api and /oauth)
```

`connect-src 'self'` covers `wss:` to the same origin in current browsers. Verify on Safari. Work needed: `ui/index.html` loads xterm and fonts from jsdelivr and Google Fonts, so vendor them into `ui/vendor/` (pinned versions, hashes recorded in the repo) and drop the external origins. Check `ui/app.js` for inline `style=` in template strings; `style-src 'self'` blocks those and they must move to classes. Ship the CSP in `Report-Only` first.

**Token storage.** `ui/app.js` keeps the token in `localStorage`, which XSS can read. After the CSP lands, move it to `sessionStorage` (smaller window), and in `iap` mode there is no token. An `HttpOnly` session cookie (`__Host-` prefix, `SameSite=Strict`) is the proper fix; it is listed as a later task because it needs the CSRF header above.

**Dashboard UX additions.**

- Sandbox card: a "Access" row showing `git: agent/api-fix-1 only`, `egress: pkg-only (enforced)`, `model: proxied`, and a red chip `secrets in sandbox` when any credential is exposed. A "Revoke grant" button.
- Repo banner on create when `protection.status != protected`, with the settings table from 5.2 and a copy button.
- Credentials page: `proxied` / `exposed` column; a warning on any exposed credential.
- Network tab: denied hosts per environment with "add to allowlist" (writes the env spec; needs `admin`).
- Tokens page: scope picker, limits form (environments, repos, max sandboxes, expiry), per-token usage.
- Controls the current principal cannot use are disabled, using `/api/me`.

## 6. Security and failure modes

### 6.1 Threat model

Legs of the lethal trifecta (Willison 2025): **P** private data/capability, **U** untrusted content, **E** external communication. "Removes" means the mitigation takes that leg away for this attacker. "Reduces" means it narrows it.

| # | Attacker | Capability | Asset | Mitigation | Leg |
| --- | --- | --- | --- | --- | --- |
| T1 | Prompt injection in repo files, issues, dependency READMEs, web pages the agent reads | Makes the agent run commands in its sandbox | GitHub token in `~/.git-credentials` and `GH_TOKEN` | Git proxy + grant. The sandbox holds only `hgs_`, which is useless off the gateway | Removes **P** (token) |
| T2 | Same | Push to `main`, force-push, delete branches, tag | Default branch integrity | Gateway ref check (5.3), ruleset (5.2), App token without admin | Removes the write path; ruleset is a second wall |
| T3 | Same | Merge its own PR through the API | Review gate | REST allowlist denies merge, reviews, auto-merge; PR is draft; App-authored PRs can be approved by the owner | Removes the action, not a leg |
| T4 | Same | Open a PR containing a modified workflow, to read CI secrets | CI secrets | Token without `workflows` permission; CODEOWNERS on `.github/`; no secrets in `push` workflows | Reduces **P** |
| T5 | Same | `curl` repo contents or the model token to an attacker host | Source code, Claude token | Egress allowlist via per-policy proxy; DNS closed; model token not in sandbox | Removes **E** (outside the allowlist); model token removes **P** |
| T6 | Same | Send data through an allowed host (a URL query to `pypi.org`, a package upload) | Source code | Tier-2 `read-only` methods lowers it. The allowlist is small and audited (`gw.egress.deny`/audit mode) | Reduces **E**. Residual risk, see 6.3 |
| T7 | Same | Push stolen data to its own branch on a public repo | Source code of a private fork or secrets in the repo | Sandbox contains the repo only; no other secrets; draft PR needs human review. Public repo code is public anyway | Reduces **P** to repo contents |
| T8 | Same | Use the Claude subscription token for its own purposes | Shared Pro quota | Model proxy with grant; concurrency cap; usage stop | Removes **P** (when `proxy=true`) |
| T9 | A compromised sandbox | Reach metadata server, kube API, kubelet, other pods, control plane port 8787 | Cloud IAM, cluster | NetworkPolicy allows only proxy + gateway:8788; gVisor; no SA token (`_pod_template`); gateway app has no admin routes | Removes **E** to internal targets |
| T10 | A compromised sandbox | Use another sandbox's grant, or replay an old one | Other agents' branches | Grant bound to sandbox id, repo and branch; stored hashed; revoked on destroy; 128-bit random | Reduces **P** |
| T11 | A compromised sandbox | Attack the gateway: path traversal, huge bodies, slowloris, malformed pkt-lines | Control plane availability, real token | Strict path normalisation, size caps, timeouts, per-grant rate limits, fuzzed parser (8.1); gateway runs in the control plane process, so a gateway RCE bug is serious | Reduces **E** to the control plane |
| T12 | Injected orchestrator (reads `agent_status` output, PR text, file contents from agents: untrusted) | Calls hangar tools with its token | Fleet, repos, quota | Scopes (`operate` token cannot touch credentials/tokens), `limits.repos`, `max_active_sandboxes`, expiry, audit | Reduces **P**. Note the orchestrator is itself a trifecta victim: untrusted input, and spawn/exec capability |
| T13 | Stolen hangar token | Full admin today | Everything | Scopes, limits, expiry, rate limits, per-client tokens, revocation; IAP for humans | Reduces **P** |
| T14 | Browser XSS or a malicious page against the dashboard | Read the token from `localStorage`, call `/api` | Admin token | CSP, vendored scripts, `sessionStorage`/cookie, CSRF header, IAP | Removes **E** from the page; reduces **P** |
| T15 | Network attacker on the OAuth consent page | Guess a token | Admin token | Rate limit, 256-bit tokens, capped OAuth scopes | Reduces **P** |
| T16 | Malicious or typo'd `repo` URL passed to `create_sandbox` | Clone from an attacker host, get a credential sent there | Credentials | Only `github.com/<owner>/<repo>` (configurable host list) gets a grant; `limits.repos`; with `insteadOf`, other hosts get no credential | Removes **P** for other hosts |
| T17 | Fork/snapshot consumer | Read secrets left in a snapshot | Tokens | Proxied sandboxes hold only the dead-with-parent `hgs_`; `_scrub_inherited_secrets` also removes the grant file | Removes **P** |

### 6.2 Failure modes

| Failure | Behavior |
| --- | --- |
| Gateway is down | Sandboxes lose git and the model. Running processes keep working. Pushes fail with a clear git error. The reaper still works. |
| GitHub App key is missing or mint fails | `create` fails before the sandbox starts, if the repo needs a grant (fail closed). With `HANGAR_CREDENTIAL_MODE=direct` the old path is used. |
| Real installation token expires mid-push | The gateway refreshes on 401 once and retries only requests it has not started streaming. A pack upload is not retried; the git client retries. |
| Parser rejects a legitimate push (new git protocol feature) | Reject, log the capability string, and keep a `HANGAR_GATEWAY_ALLOW_CAPS` list to unblock without a release. |
| Egress proxy down | Sandboxes lose the internet and keep a working gateway path. NetworkPolicy keeps closed. |
| Label patch for phase switch fails | `_setup_repo` raises `FleetError`; the sandbox goes to `ERROR` and cleans up (existing path). |
| Branch protection check API fails or lacks permission | `unknown`, a warning, never a block. |
| Grant revoke races with an in-flight push | The push either completes (accepted before revoke) or is rejected. No partial state beyond GitHub's own atomicity. |
| Scope check bug | The tool map fails closed (unknown tool needs `admin`) and a unit test asserts every registered tool is mapped. |

Recovery paths must stay outside the failure domain (Jarmak 2026). Grant revoke, token revoke and `destroy_sandbox` run in the control plane and don't need the sandbox to cooperate. Revoking a grant does not exec into the sandbox.

### 6.3 Residual risk

- Exfiltration through allowed hosts and the model API itself is not closed. The model provider sees whatever the agent sends. This is accepted: the agent is the product.
- Content in a legitimate branch can still be harmful (backdoored code). Human review of a draft PR plus CI is the control, not hangar.
- A GitHub App token with `contents: write` can write any branch of its repo if it leaks. It does not leave the control plane.
- gVisor/Kata escapes are out of scope.

## 7. Rollout and migration

Three settings with explicit stages, so nothing breaks on upgrade:

| Setting | Values | Default now | Default after rollout |
| --- | --- | --- | --- |
| `HANGAR_CREDENTIAL_MODE` | `direct` / `proxied` | `direct` | `proxied` for git once M4 is proven live; model stays per credential |
| `HANGAR_EGRESS` | `off` / `audit` / `enforce` | `off` | `audit` (M6), `enforce` on k8s and single-VM (M8) |
| `HANGAR_BRANCH_PROTECTION` | `off` / `warn` / `enforce` | `warn` | `warn` |

Migration notes:

- `ApiToken` rows keep working: stored `scopes=["admin"]`, new `limits` default to none, no expiry. Admins are encouraged to mint scoped tokens and revoke the old ones. A dashboard banner lists tokens that are still `admin` and unused for 30 days.
- OAuth approvals already granted with scope `hangar` map to `operate` and keep working until they expire (90 days).
- MCP tools: all existing tools keep their names, parameters and return keys. Additions only: `open_pull_request`, `pull_request_status`, new keys `git`, `network`, `warnings` in sandbox output, `proxied`/`exposed` in `list_credentials`. **One behavioral change**, stated explicitly: with `credential_mode=proxied`, `create_sandbox` without `work_branch` on a repo gets a generated `agent/<name>` branch (before: stayed on the base branch). Pushing to the base branch from a sandbox stops being possible; there is no opt-out except `direct` mode.
- `read_file` on a sandbox with exposed secrets requires `operate` (before: any token). With the default of old tokens being `admin`, nobody notices.
- Docs: update `docs/security.md` (credential handling, boundaries), `docs/gke.md` (network section; the `Unmanaged` note), `README.md` (credentials, `hangar token create --scope`).
- Deployment changes: new Service for 8788, second ingress host (IAP is optional), new RBAC verbs, egress proxy image. `deploy/k8s/*` and the Terraform module get updates in the PRs that need them.
- Rollback: set the three settings back to `direct`/`off`/`off`. Sandboxes already running keep their grants until they are destroyed.

## 8. Test plan

### 8.1 Unit tests (`FakeProvider` in `tests/conftest.py`; no network, no cluster)

Gateway and grants (`tests/test_gateway_git.py`):

- Table-driven receive-pack commands against the pkt-line parser: push to work branch → forwarded; to `main`, to `refs/tags/v1`, to `refs/pull/1/head`, delete of work branch, two commands where one is bad, `push-options`, gzip body, oversized command list, truncated pkt-line, no flush-pkt → all rejected with the expected reason.
- Rejection response is a valid `receive-pack-result` (assert on bytes) and the body is drained.
- Path normalisation: `/git/github.com/Owner/Repo.git/…` matches, `..`, `%2e%2e`, double slashes, other repo, other host → 404.
- Grants: revoked, expired, unknown, token for another sandbox → 401. `Fleet.destroy`, `_mark_gone`, reaper expiry and a failing `create` all revoke.
- Fork: new grant, new branch; parent grant unchanged; `.git-credentials` scrubbed.
- Upstream stub (httpx `MockTransport`): the real token is attached upstream and never appears in responses, audit entries, or sandbox files (`FakeProvider` file store scanned for the token string).
- Streaming: a 100 MB fake pack is forwarded with bounded memory.
- Fuzz the pkt-line parser (Hypothesis): never raises anything but `ProtocolError`, never forwards bytes after a rejection.

REST allowlist (`tests/test_gateway_rest.py`): every row of the 5.3 table allowed/denied; `PUT …/merge`, `reviews`, `/graphql`, `PUT contents`, `POST git/refs` → 403 and audited; PR create forces `draft` and rejects a foreign `head`.

Repo guard (`tests/test_repo_guard.py`): stub GitHub responses for ruleset, classic protection, 403, 404 → `protected/partial/unprotected/unknown`; `enforce` refuses, `warn` returns `warnings`.

Scopes (`tests/test_scopes.py`):

- Every registered MCP tool has a scope (fails when a new tool is added without one).
- For each scope × tool: allowed or `403 insufficient_scope` with the right `WWW-Authenticate`; batched JSON-RPC; unknown tool → admin.
- REST: each route's required scope; `read` token cannot `POST /api/sandboxes`; `operate` cannot touch `/api/tokens`.
- Limits: `repos`, `environments`, `credentials`, `max_active_sandboxes`, `max_creates_per_hour` (clock injected), `max_ttl_minutes`, expiry.
- OAuth: requested ∪ approver cap; no escalation; legacy `hangar` → `operate`; admin never issued; revoked approver kills the grant (existing behaviour, keep test).
- Existing tests in `test_auth.py`, `test_oauth.py`, `test_app.py` pass unchanged.

Dashboard and auth: IAP JWT with wrong audience, expired, wrong issuer, unsigned → 401; spoofed email header alone → 401; CSRF header required on mutating IAP calls; rate limiter (11th failure → 429, window expiry, `X-Forwarded-For` honored only from trusted peers); CSP header present on `/`, `/api/*`; test that `index.html` references no external origin.

Egress rendering (`tests/test_egress.py`): preset + custom → proxy ACL and NetworkPolicy manifests (golden files); policy hash stable; `_pod_template` adds the label and proxy env; Docker `_run_kwargs` selects the internal network (mock docker client).

Model proxy: path allowlist; concurrency cap queueing and `429`; usage header parsing into `ModelUsage`; streaming passthrough; the real credential never reaches logs (broker `redact` covers it).

### 8.2 Live acceptance test on GKE

Setup: the same cluster as the 2026-10-11 run, Agent Sandbox add-on, gVisor, Dataplane V2. A scratch repo `Sean-Koval/hangar-acceptance` with the 5.2 ruleset on `main`. A GitHub App installed on that repo only (no `workflows` permission). `HANGAR_CREDENTIAL_MODE=proxied`, `HANGAR_EGRESS=enforce`. Two tokens: `operate` limited to the scratch repo, and `read`. Script: `scripts/acceptance_security.py` (to be written in M9) runs these from an MCP client and from inside the sandbox via `exec`.

Pass criteria (all must hold):

| # | Check | Pass |
| --- | --- | --- |
| A1 | `create_sandboxes(count=2)` from warm pool | Both `running`; median create time ≤ 4 s (live run: 3.1 s). Result contains `git.protection.status == "protected"`. |
| A2 | `grep -r -E "gh[pous]_|github_pat_|sk-ant-" ~ /proc/self/environ` as the agent user | No match. `~/.git-credentials` contains only an `hgs_` token. |
| A3 | `git push origin HEAD:main` | Rejected with `hangar:` message. Remote `main` sha unchanged. |
| A4 | `git push origin HEAD:refs/tags/x`, `git push origin :agent/other` | Both rejected. |
| A5 | `git push origin HEAD` on the work branch | Succeeds; branch appears on GitHub. Commit author is the App bot. |
| A6 | Direct `curl https://api.github.com/…` and `curl https://example.com` | Fail (connection refused or proxy 403). |
| A7 | `gh api -X PUT repos/…/pulls/N/merge`, `gh api graphql`, `gh api -X POST repos/…/git/refs` via the gateway | All 403; audit has three `gw.api.deny`. |
| A8 | `open_pull_request` | Returns draft PR; author is the bot; ruleset blocks merge until the check passes and a human approves. |
| A9 | Push a commit that edits `.github/workflows/x.yml` | Rejected by GitHub (no `workflows` permission). |
| A10 | The 2026-10-11 probes: metadata `169.254.169.254`, kube API, control plane `:8787`, kubelet `:10250`, DNS lookup of an arbitrary name, `pip install requests`, `uv sync` of a small project | The first five fail. The last two succeed through the proxy. |
| A11 | `claude -p "say hi"` in the sandbox with `proxy=true` (if open question 1 resolves yes) | Replies; `ANTHROPIC_*` secret not in env; gateway usage row updated. If it does not resolve, mark A11 "not applicable" and keep the credential exposed. |
| A12 | Revoke the grant while an agent is running | The next push and the next model call fail within 5 s; sandbox still `running`. |
| A13 | `read` token calls `exec` / `create_sandbox` | `403 insufficient_scope`. `operate` token with `repos` limit: `create_sandbox(repo=other)` fails. |
| A14 | Dashboard through IAP: user not in `HANGAR_IAP_ROLES` | `read` only; mutations 403. Response headers include the CSP; browser console shows no CSP violations; 11 bad tokens → 429. |
| A15 | Phase switch | During `repo.setup` a `setup_allow` host resolves; after it, the same host is blocked. |

Measure and record: create latency with and without proxies, gateway added latency on a clone of a 500 MB repo (target ≤ 10% over direct), and git push throughput. Repeat A1 to A5 three times and compare, since single runs vary by a few points (Jarmak 2026).

## 9. Milestones

Each is one PR unless noted. Order is by risk removed per effort.

| # | Task | Scope | Size |
| --- | --- | --- | --- |
| M0 | Branch protection runbook + `repo_guard` | Apply 5.2 settings to `hangar` repo (manual, documented). `repo_guard.py`, `HANGAR_BRANCH_PROTECTION`, `warnings` in `create_sandbox`/`fork_sandbox`, dashboard banner, `docs/security.md` section. | S |
| M1 | Scopes: model + enforcement | `Principal`, `TokenLimits`, tool→scope map, `require()` in REST, middleware 403 challenge, `hangar token create --scope/--limit`, `/api/me`, tests. Old tokens stay admin. | M |
| M2 | Grants + gateway skeleton | `SandboxGrant`, gateway app on :8788, `hgs_` auth, revoke hooks in `Fleet`, k8s Service and docker publish, `credential_mode` flag. No forwarding yet. | M |
| M3 | Git smart-HTTP inspector | Parser, forwarding with PAT, `insteadOf` + credential injection in `harnesses.py`/`broker.py`, reject-with-message, scrub on fork, audit. | L |
| M4 | GitHub App tokens + REST allowlist + PR tools | `github_app` credential kind, per-grant installation tokens, REST allowlist, `GH_HOST` path, `open_pull_request`, `pull_request_status`, dashboard buttons. Live checks A2 to A9. | L |
| M5 | Dashboard hardening | CSP (report-only then enforce), vendor xterm/fonts, security headers, login rate limiting, `sessionStorage`, CSRF header. | M |
| M6 | Egress: policy model + audit mode | `network` block in `envspec.py`, presets, Smokescreen deployment per policy, k8s labels/env/NetworkPolicy rendering, Docker internal network + proxy container, `HANGAR_EGRESS=audit`, denied-hosts API. | L |
| M7 | Egress enforce + phases | `Unmanaged` template and own policy, phase relabel (+RBAC), Docker network switch, probes A10/A15, `docs/gke.md` update, default `pkg-only` on k8s/single-VM. | L |
| M8 | IAP / SSO | Host split, JWT verification, role map, ingress and BackendConfig, CSRF for cookie auth. Optional install. | M |
| M9 | Model proxy | `/anthropic` route, usage tracking and stop threshold, concurrency cap, `Credential.proxy`. Gated by open questions 1 and 5. | M |
| M10 | OAuth scopes + step-up | `hangar:read`/`hangar:operate`, consent page, cap by approver, challenge flow, default baseline switch after client testing. | M |
| M11 | E2B egress | Gateway CONNECT route and SDK network options, once verified. | S/M |
| M12 | Acceptance script + docs | `scripts/acceptance_security.py`, doc updates, live run on GKE, results recorded in the PR. | M |

M0 and M1 can ship in parallel. M5 is independent of the gateway and can slip anywhere after M1.

## 10. Open questions

1. **Does Claude Code in subscription mode honor `ANTHROPIC_BASE_URL` with a placeholder `ANTHROPIC_AUTH_TOKEN`?** Strings for both exist in the 2.1.296 binary, but I did not run it against a gateway. Also unverified: the exact OAuth beta header the gateway must add, and the names of the rate-limit response headers behind `rate_limit_event`. Needs a spike of one day with a capture proxy.
2. **Does `gh` work against `GH_HOST=<gateway>` for `pr create`, `pr checks`, `run list`?** The GHES path layout is documented, but a gateway that is not GHES may lack endpoints `gh` probes at startup (for example `/api/v3/meta`). Fallback: `open_pull_request` only, and no `gh` in the sandbox.
3. **Should force pushes to the work branch be denied too?** Allowing them keeps `git rebase` working. Denying would need the gateway to track the last known tip per grant, which breaks if pushes bypass it. Default: allow.
4. **Can Codex (API key) and Gemini (API key) be proxied?** I did not verify their base-URL variables. Codex with a ChatGPT login (`auth.json`, rotating refresh token) probably cannot.
5. **Is routing a Claude subscription token through a hangar-operated gateway allowed under Anthropic's current terms?** I could not verify. A person with the plan should check before M9 ships; if not allowed, subscription credentials stay exposed and we recommend per-sandbox API keys with spend limits.
6. **Commit signing.** Anthropic's design keeps signing keys out of the sandbox. Signing needs either gateway-side re-signing (rewrites commits and the pack) or GitHub's web-flow signing via the API. Out of scope here; revisit after M4.
7. **Does the GKE Agent Sandbox add-on accept `networkPolicyManagement: Unmanaged` under its admission policies, and does its controller revert pod label patches?** This decides how M7 is implemented (own policy + relabel, or union fallback).
8. **Is tier-2 TLS interception for method restriction worth it?** It adds a CA to every image and a MITM proxy to operate, for a control that only lowers exfiltration bandwidth. Default plan: ship tier 1, decide after the audit-mode data.
9. **E2B network controls.** The SDK is not installed here. Need to check whether it can deny all outbound traffic and allow specific destinations, and whether destinations may be hostnames or only CIDRs.
10. **Which MCP clients handle `insufficient_scope` step-up?** Codex, Claude Code and the ChatGPT connector all need testing before the default registration scope drops to `read`.
11. **Per-sandbox GitHub Apps vs one App.** One App with per-grant installation tokens (this spec) means one private key in the vault. Is that key an acceptable single point, or should it live in Secret Manager and be fetched at mint time?
12. **Smokescreen vs Squid vs Envoy.** Smokescreen is the tier-1 choice here, but its ACL reload behaviour and its `CONNECT` logging format need a quick spike before M6.
13. **Reads through the git proxy are unrestricted within the repo.** Should a grant be able to fetch other users' branches (including other agents' work branches)? Default allows it; it could expose one agent's partial work to another.
