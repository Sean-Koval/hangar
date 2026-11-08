# 0014 Work deployment and the team harness

Status: proposal · Date: 2026-11-08 · Builds on: 0003 (gateway, egress, IAP), 0006 (environments, M4–M7), 0007 (MCP gateway, budgets), 0009 (jobs), 0011 (triggers, context exchange), 0012 (items 4, 7, 12)

Hangar runs well for one owner: the overnight stack (#48–#69) gave it isolation, live turns, verify in the loop, PR auto-fix, the git gateway and caching. This spec covers what a team at work needs before it can rely on it. That means three things. First, the security review a company runs before agents touch its code. Second, a **team harness**: the skills, settings, instructions and connectors (GitHub, Jira, Confluence) every sandbox starts with, kept as code the team reviews. Third, a cost story: what a verified PR costs, and how to lower that.

It doesn't add a new provider or protocol. Most of it finishes milestones other specs left `planned` (0003 M6–M8, 0006 M4–M7, 0007 M5). The new parts are identity, credential ownership, ticket context and the Jira front door.

## 1. Summary: the decisions

1. **People sign in; tokens belong to people.** The dashboard and API accept OIDC (Google Workspace, Okta, Entra), with IAP on GKE as the simplest path. Every token, job, session, sandbox and audit entry carries a user principal. Roles are `viewer` ⊂ `operator` ⊂ `admin`, plus `approve`, which stays human-only (specs README decision 7). The bootstrap admin token remains for break-glass use only.
2. **Credentials have owners.** A credential is `personal` (one user), `team` (a group) or `service` (the control plane only). A sandbox gets a credential only when its requester may use it. Today any token that can create a sandbox can attach any stored credential.
3. **Model access goes through the company's account, not a person's subscription.** The defaults for work are org API keys with spend limits, or the company's LLM gateway, Vertex or Bedrock via `ANTHROPIC_BASE_URL` and the harness's provider settings. Subscription logins stay for personal instances.
4. **Gateway mode is the only mode at work.** `HANGAR_REQUIRE_GATEWAY=true` refuses `credential_mode: direct`, GitHub tokens in `secrets:`, and PAT upstream credentials when a GitHub App is configured. Without the gateway, an agent can comment `/hangar approve` as its token's account (security.md, "Front doors").
5. **Egress: audit first, then enforce** (0003 M6–M7, 0012 item 7). It is a blocker for most security reviews, so it moves up from phase 5.
6. **Tickets: Jira joins GitHub Issues.** This amends specs README decision 16 (owner request, 2026-11-08). Linear is still not planned. Jira can start a job, supply its context and receive the result, the way GitHub issues already do.
7. **The control plane fetches ticket context; the agent doesn't.** A job started from `ABC-123` gets the ticket, its comments, linked Confluence pages and linked GitHub PRs as context files, fetched and fenced by hangar before the turn. The common path needs **no Atlassian credential in the sandbox**. Fetching once, up front, is also cheaper than an agent running MCP lookups turn after turn.
8. **The team harness is a repository.** A team env repo (0006 M4) holds environments, skills, harness settings and instructions. A change is a PR, and the control plane syncs the default branch. Each project repo may add a `.hangar/env.yaml` overlay.
9. **Hardened turns keep the team harness.** Today `ask` and `deny-risky` turns drop skills and plugins, because Claude Code finds them in places the agent can write (`harden.py`). Hangar will instead install the environment's skills and plugins in a hangar-owned, read-only directory and load them from there. A team harness that vanishes exactly when approvals are on can't be a work default.
10. **One headline metric: cost per verified PR.** It counts tokens and compute per PR that passed verify and was merged, per team and per repo. Budgets, routing and caching are judged against it.

## 2. Where we are

| Works today | Gap for a work deployment |
| --- | --- |
| Bearer tokens (hashed) with scopes `admin`, `jobs`, `mcp`, `metrics`, `sandboxes`; OAuth 2.1 for MCP clients, approved with an admin token | No user identity. Tokens have names, not owners. An `mcp` token's front door has no fence (security.md). OAuth consent needs an admin token, so every connected client acts as admin. |
| Fernet vault; credentials injected at claim; exclusive credentials leased | Credentials have no owner or access list. A personal Claude subscription is shared by the fleet (README, "Known limits"). |
| Git gateway with GitHub App tokens; branch protection on `main` | It is optional: `direct` mode is the default outside jobs and sessions. |
| NetworkPolicies block RFC1918, metadata and ingress; `HANGAR_EXTRA_CA_BUNDLE` for TLS-inspecting proxies | No egress allowlist; sandboxes reach the whole internet. |
| Checkpoints, artifacts and backups in the session bucket, encrypted | No retention period, no per-user deletion, no export of everything hangar holds about a job. |
| `skills:`, `agent_config`, `files:`, `mcp:`, `secrets:`, `extends`, `clone_environment` | Environments live in the control plane's `envs/` or its database. There is no team repo, no repo overlay and no personal profile (0006 M4, M6 planned). |
| Hardened turns apply the environment's `~/.claude/settings.json` (from `files:`) and its MCP servers, with hangar's verify gate merged in | Skills and plugins are off in those turns. Settings are one opaque JSON string per file, so a child environment replaces its parent's file instead of merging with it. |
| GitHub issues and comments, Slack, inbound webhooks and schedules start jobs; GitHub and Slack get replies (`NOTIFIERS` in `jobs.py`) | No Jira source and no `jira:` notifier. A generic inbound webhook can start a job, but can't fetch linked context or report back. |
| Per-job and per-session `Budget`; a `jobs` token's `max_budget`; usage by day, repo and requester | No per-team or per-user caps over a period, no chargeback export, no cost-per-verified-PR view. |

## 3. Identity and access

### 3.1 Sign-in

- **OIDC login** (`HANGAR_OIDC_ISSUER`, client id, and the client secret as a vault credential). The dashboard uses the authorization-code flow with PKCE. Sessions are a signed, `HttpOnly`, `SameSite=Strict` cookie that lasts at most 12 hours.
- **IAP on GKE** as an alternative to OIDC: a `BackendConfig` on the ingress (`deploy/k8s/ingress.yaml`). Hangar checks the `x-goog-iap-jwt-assertion` header (its audience, and Google's keys) and takes the user from it. Requests that skip the load balancer have no valid assertion and are refused. `/api/hooks/*`, `/a2a`, `/e2b` and `/mcp` stay token-authenticated, because webhooks and MCP clients can't follow an IAP login. IAP's own path rules exempt them.
- **Groups** come from the ID token's `groups` claim, or for Google Workspace from the Directory API with a service account. They map to roles and to credential access lists (§4) via `HANGAR_ROLE_GROUPS`, e.g. `{"admin": ["eng-platform@acme.com"], "operator": ["eng@acme.com"]}`.

### 3.2 Principals and roles

`Principal` gains `user` (the OIDC subject and email) and `groups`. Every `ApiToken` gets an `owner` user. A token's effective rights are the intersection of its scopes and its owner's current role. Removing someone from the IdP group therefore disarms their tokens at the next request, without waiting for anyone to revoke them.

| Role | Can |
| --- | --- |
| `viewer` | Read the fleet, jobs, sessions, events and usage of teams they belong to |
| `operator` | Everything `viewer` can, plus create sandboxes, jobs and sessions with environments and credentials their groups may use; steer, cancel and approve tool calls in their own turns; mint `mcp` and `jobs` tokens for themselves |
| `admin` | Settings, environments, team credentials, other people's tokens, the audit log |
| `approve` | Approve job starts from untrusted sources (`awaiting_approval`) and `human_only` escalations. Human-only: never implied by `admin`, never granted over OAuth, no MCP tool (README decision 7) |

- **`hangar connect`** mints a token owned by whoever is signed in to the CLI (`hangar login --sso` opens a browser using a device-code flow).
- **OAuth consent** is approved by the signed-in user, not with an admin token. The grant is bounded by that user's role.
- **MCP gets a fence** like `jobs` tokens have: the owner's environments, credentials and repos.
- **The audit log** records `actor` as `user (token name)`. A sandbox, job or session shows who started it, and on whose behalf when it came from a front door (`github:<login>`, `slack:<user>`, `jira:<accountId>`, mapped to a user where the email matches).

### 3.3 Trust from the front doors

GitHub, Slack and Jira identities map to users by verified email, or by an explicit map (`HANGAR_IDENTITY_MAP`). A mapped person's trigger runs with that user's role and credential access. An unmapped one waits in `awaiting_approval`, as an untrusted GitHub author does today. `HANGAR_GITHUB_TRUSTED` and `HANGAR_SLACK_USERS` keep working, and the identity map is read first.

## 4. Credentials

### 4.1 Ownership

`Credential` gains `owner: "user:<email>" | "group:<group>" | "service"` and `uses: list[Literal["agent", "git", "connector", "secret"]]`.

- **Attach rule.** At create, an environment's `credentials:` and `secrets:` resolve only to credentials the requester may use. A `personal` credential means its owner. A `team` credential means anyone in that group. A `service` credential is never injected into a sandbox: it is for the control plane's own calls, such as the gateway's upstream, the GitHub PR opener, and the Jira and Confluence resolvers in §7.3.
- **When the requester can't use one,** the create fails. The error names the credential, not its value, and tells the requester to ask an admin of the owning group.
- **Personal fallbacks.** An environment may name a credential role instead of a fixed name, e.g. `credentials: [{role: model}, github-app]`. At claim, `{role: model}` resolves to the requester's own model credential if they have one, else to the team's. This is how one environment serves an engineer who brings their own Claude key and one who uses the team's.
- **Migration.** Existing credentials become `owner: group:<HANGAR_DEFAULT_GROUP>` (default: everyone with `operator`), so nothing breaks on upgrade. `hangar doctor` lists credentials with no owner.

### 4.2 Model access at work

| Option | How | When |
| --- | --- | --- |
| Org API key per team | `kind: api_key` credential with `owner: group:…`; a spend limit set on the provider's side | Default |
| Company LLM gateway | Claude Code: `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` (a per-team gateway key); Codex: its provider config. Set through `env:` and `secrets:` on the team environment | The company already meters and logs model traffic centrally |
| Vertex AI or Bedrock | Claude Code's `CLAUDE_CODE_USE_VERTEX` / `CLAUDE_CODE_USE_BEDROCK` and the provider's credentials. Sandbox pods have no Kubernetes token and can't reach the metadata server, so Workload Identity doesn't reach them: use a key, or the model proxy | Billing must stay on the cloud account |
| Model proxy (0003 M9) | Hangar injects the key outside the sandbox | After egress enforcement (§5). It is still gated on README decision 9 for subscription tokens; API keys don't need that decision |

`hangar doctor` warns when a work deployment (`HANGAR_PROFILE=work`, §9) attaches a subscription login to an environment used by more than one person.

## 5. Network

This finishes 0003 M6–M7, as 0012 item 7 describes, in this order:

1. **Audit mode (M6).** An egress proxy on the gateway listener (:8788). Sandboxes get `HTTPS_PROXY`/`HTTP_PROXY`, and the NetworkPolicy allows only DNS, the gateway and hangar's pods. Every CONNECT host is logged as `gw.egress` with the sandbox, job and environment. Nothing is blocked. After a week of normal work, `hangar egress report` lists the hosts each environment used.
2. **Levels per environment.** `network: none | trusted | custom` with `allow: [hosts]`. `trusted` is a list hangar maintains: package registries for the bundled environments' languages, plus the model API hosts for the harnesses it allows, plus `HANGAR_EGRESS_TRUSTED_EXTRA` for the company's own hosts (internal pip/npm mirrors, Artifactory, a GHES host, the LLM gateway).
3. **Enforce (M7).** Hosts outside the list get a 403 from the proxy, which tells the agent why. The default for `HANGAR_PROFILE=work` is `trusted`.

Credential injection for allowlisted hosts (0012 item 7) comes after enforcement and isn't part of this spec. §7.3 already removes the Atlassian credential from the common path.

## 6. Data handling

| Setting | Default | Covers |
| --- | --- | --- |
| `HANGAR_RETENTION_DAYS` | 90 | Checkpoints, artifacts, turn logs and event streams of finished jobs and of sessions idle that long. A bucket lifecycle rule is also set by terraform as a backstop. |
| `HANGAR_AUDIT_RETENTION_DAYS` | 365 | The audit log. It is kept apart from the above, because security teams want it longer than the work itself. |
| `hangar admin purge --user EMAIL` | n/a | Deletes a user's sessions, jobs, artifacts and personal credentials. It keeps audit entries, with the user replaced by a hash. |
| `hangar admin export --job ID` | n/a | One archive with the job's instructions, context, events, result, artifacts and audit entries, for an incident review. |

Turn logs can contain secrets the agent printed. Redaction (security.md) masks stored credential values only. That limit stays, and it is documented on the retention page.

## 7. The team harness

### 7.1 Team env repo and repo overlays (0006 M4, M5)

- **`HANGAR_ENV_REPOS`**, e.g. `["github.com/acme/hangar-envs@main"]`, is read through the gateway's GitHub App with `contents: read`. It syncs on a push webhook and every 10 minutes.
- **Layout:**

  ```
  hangar-envs/
    envs/*.yaml                 environments (team bases, one per service or stack)
    skills/<name>/SKILL.md      team skills, referenced as `path: ../skills/<name>`
    harness/settings.yaml       team harness settings (§7.2), inherited by every env that `extends: team`
    harness/instructions.md     team instructions, written to CLAUDE.md and AGENTS.md
    profiles/<email>.yaml       personal profiles (§7.4), editable by that user and admins
  ```

- **Validation.** A sync validates every spec (`validate_environment`). A broken commit is refused as a whole: the previous revision stays live, and the sync's error is posted to the commit as a check run. Environments from a repo are read-only in the dashboard. The dashboard's "save" opens a PR instead (0006 M5).
- **Repo overlay.** `.hangar/env.yaml` in a project repo (for example `extends: team-python` plus `repo.setup` and `repo.verify`) applies when `create_sandbox(repo=…)` or a job names that repo. `repo.verify` is new here: the default verify command for jobs on that repo, which a job's own `verify` overrides. Specs README decision 2 describes it, but `RepoSpec` doesn't have it yet, so today every job (or inbound-webhook source) sets its own. Its `harness_settings`, `mcp`, `secrets` and `credentials` are ignored unless the repo is listed in `HANGAR_TRUSTED_OVERLAY_REPOS`. Anyone who can open a PR on the repo can edit the overlay, and a job runs from the branch it names.
- **Self-maintenance** (0012 item 4(c–e)) belongs here: a rebuild when a lockfile digest changes on the default branch, and a `maintenance:` command run on claim. `hangar env init --from-repo` (item 4(b)) drafts a repo's overlay in a sandbox and proves it by running it.

### 7.2 Harness settings hangar owns

A structured `harness_settings:` block replaces hand-written `files: {.claude/settings.json: …}`:

```yaml
harness_settings:
  claude-code:
    permissions: { deny: ["Bash(git push --force:*)", "Read(./.env*)"] }
    hooks:
      PostToolUse: [{ matcher: "Edit|Write", hooks: [{ type: command, command: "hangar-team fmt" }] }]
      PreToolUse:  [{ matcher: "Bash", hooks: [{ type: command, command: "hangar-team guard" }] }]
    plugins: [{ marketplace: "github.com/acme/claude-plugins@v3", name: "acme-review" }]
  codex:
    config: { model_reasoning_effort: "medium" }
```

- **Merging.** Lists and maps merge down the `extends` chain like the rest of the spec. A child adds a hook; it doesn't replace the team's file.
- **Rendering.** The block is rendered into the harness's settings at claim. In a hardened turn it is the source of `harden.claude_settings`, instead of parsing `files:`. `files: .claude/settings.json` keeps working and is merged under `harness_settings`, with a deprecation warning.
- **Plugins** are fetched by the control plane at build time, pinned to the ref, and placed in the image's skills/plugins layer under `/opt/hangar/team/`. They are root-owned and read-only.
- **Skills in hardened turns (decision 9).** The environment's skills (not the repository's `.claude/skills`) are copied to `/opt/hangar/team/skills` at build time. A hardened turn loads them from there. The concern in `harden.py`, that "their `allowed-tools` are permission rules the agent could write", doesn't apply to a directory the agent can't write. **Verify first, with the real CLI** (as `tests/test_harden_real_clis.py` does): which mechanism loads skills and plugins under `--setting-sources ''`. Candidates are `--add-dir` on a directory holding `.claude/skills`, and `--plugin-dir`. Also check that the agent can't shadow a hangar-owned skill with one of the same name in a directory it does own. If no mechanism works without loading user settings, ship skills as a plugin served from `/opt/hangar/team/` and say so in steering.md.
- **Codex** has no plugin system. Its skills are copied as today; the gap described in `CODEX_RULES` remains.

### 7.3 Connectors: GitHub, Jira, Confluence

Ticket and document systems are reached in three ways, cheapest and safest first.

**1. Context resolvers (control plane, before the turn).** `ContextItem.kind` gains `jira`, `confluence` and `github` (an issue or PR by URL or number), each with a `ref`. When a job or turn starts, hangar resolves them with a `service` credential and writes the results to `$HANGAR_IO/in/<turn>/`:

| Kind | Fetched | Written |
| --- | --- | --- |
| `jira` (`ABC-123`) | Summary, description, acceptance criteria, status, type, priority, components, comments (newest 50), subtasks, linked issues (one level), attachments ≤ 1 MB of text types, remote links | `jira-ABC-123.md`; linked Confluence pages and GitHub PRs are resolved one level deep as their own items |
| `confluence` (page id or URL) | Page body as Markdown, title, ancestors, last editor and date; child pages listed, not fetched | `confluence-<id>-<slug>.md` |
| `github` (`owner/repo#12`) | Issue or PR body, comments, review comments, changed files (list), CI status | `github-<repo>-12.md` |

- **Limits.** Each resolver has a per-job cap: `HANGAR_CONTEXT_RESOLVE_MAX_ITEMS`, 20, and the existing context size caps.
- **Errors.** One that fails writes a short note in place of the item ("ABC-124: no access") instead of failing the job.
- **Fencing.** Text written by people outside the identity map is fenced as untrusted (0011, workstream A). Ticket bodies are untrusted by default unless the reporter maps to a user.
- **Search.** `HANGAR_CONTEXT_CONFLUENCE_SEARCH`, off by default, adds the top 3 Confluence results for the ticket's summary in the configured spaces, marked as search results.

**2. Write-back (control plane, after the turn).** A `jira:` notifier registered in `jobs.NOTIFIERS` (beside `slack:`) posts the job's outcome to the ticket. On start it posts a link. On `needs_input` it posts the question, and a reply on the ticket from a mapped user is the follow-up. On finish it posts the summary, the PR and verification. The PR's title and branch carry the ticket key (`hangar/job-…-abc-123`), so Jira's GitHub integration links them. Optional transitions per source: `{"on_start": "In Progress", "on_pr": "In Review"}`. Hangar never moves a ticket to Done.

**3. In-turn access (the agent, when it really needs it).** Searching docs or drafting a decision record in the middle of a task needs the agent to reach Atlassian itself. This is opt-in per environment:
- **Skills that wrap a CLI** (`acli`, or `hangar-team jira` / `hangar-team confluence` scripts in the team repo) with a `team` credential limited to the needed scopes. Prefer this to MCP: a skill costs context only when it is used, while an MCP server's tool list is in every turn.
- **An MCP server** (Atlassian's remote server, or a stdio one) through `mcp:` when the team wants it. Both mean an Atlassian token in the sandbox. A read-only service account is the recommended credential.
- **The MCP gateway (0007 M5)** removes that token. Hangar proxies the sandbox's MCP calls, injects the credential, and checks each tool against an allowlist, e.g. `jira.read*`, `jira.addComment` allowed, `*delete*` and `*transition*` denied. It depends on §5's egress enforcement, since otherwise the sandbox could call Atlassian directly.

GitHub needs nothing new: the gateway handles git, hangar opens PRs, and `mcp: github` in `envs/hangar.yaml` is the example for in-turn reads.

### 7.4 Personal profiles (0006 M6, raised from low priority)

A `profiles/<email>.yaml` file in the team repo, or one stored by the user in the dashboard, applies at claim after the environment and the repo overlay. It may set `model`, `default_harness`, extra `instructions`, extra `skills`, extra `mcp` servers (with the user's own `personal` credentials only) and `harness_settings.*.hooks` that *add*.

It may not remove the team's hooks or permission denies, change `approvals`, `network` or `credential_mode`, or attach `team` credentials the user couldn't attach anyway. A profile is the requester's. For a job from a front door, it is the mapped user's, or none.

### 7.5 A starter team harness

This ships as `examples/hangar-envs/` in this repo, as a template for a team repo.

| Piece | Contents |
| --- | --- |
| `envs/team.yaml` | `extends: bare`; `harnesses: [claude-code, codex]`; `credential_mode: gateway`; `approvals: deny-risky`; `network: trusted`; `cache: auto`; the team instructions and harness settings |
| `envs/team-python.yaml`, `team-node.yaml` | Stack bases with `lsp:` once 0012 item 9 lands |
| `skills/ticket-to-plan` | Read `in/jira-*.md` and the linked pages; write a plan with acceptance checks to `out/progress.jsonl`; ask through `hangar-agent ask` when the acceptance criteria are missing |
| `skills/confluence-context` | Where to find design docs, runbooks and decision records; how to cite them in `RESULT.md` |
| `skills/pr-conventions` | Title with the ticket key, the PR template, CODEOWNERS, labels, small commits |
| `skills/verify-before-done` | Run the job's verify command (`repo.verify` by default); put evidence (commands and their output tail) in `RESULT.md` |
| `skills/decision-record` | Draft a decision record as an artifact (`out/artifacts/adr-*.md`) for a human to publish. The agent doesn't write to Confluence. |
| `harness/settings.yaml` | Deny force-push and reads of `.env*`; a `PreToolUse` guard that blocks `curl … | sh` and writes outside the checkout; a formatter on edit; a secret scan before `git commit` |
| `bin/hangar-team` | The small scripts the hooks and skills call (`fmt`, `guard`, `secret-scan`, `jira`, `confluence`) |

## 8. Jira as a front door

Jira is a named source rather than a raw inbound webhook, because it needs identity mapping, write-back and context resolution:

```json
HANGAR_JIRA = {
  "site": "https://acme.atlassian.net",
  "credential": "jira-service",
  "webhook_secret": "jira-hook",
  "projects": {
    "API": { "repo": "acme/api", "environment": "team-python",
             "trigger": { "label": "hangar" },
             "transitions": { "on_start": "In Progress", "on_pr": "In Review" } }
  }
}
```

- **The endpoint** is `POST /api/hooks/jira`. Jira Cloud webhooks registered with a secret are verified with the same HMAC code as inbound webhooks. A Jira Automation "send web request" rule can only send a fixed header. That is accepted only with `"auth": "static_header"` on the project, which `hangar doctor` flags as weaker (whoever sees the header can start jobs, though only within that project's fence), and replays are still refused.
- **Triggers.** A label added (`trigger.label`), a transition into a status (`trigger.status`), or a comment from a mapped user that starts with `/hangar`. The job's `source` is `jira`, `source_ref` is `jira:ABC-123`, and its `context` starts with `{kind: jira, ref: ABC-123}`.
- **Trust.** The person who added the label or moved the ticket must map to an `operator` (§3.3). Otherwise the job waits in `awaiting_approval`, and a comment on the ticket says so. A trusted user's `/hangar approve` on the ticket starts it.
- **Follow-ups.** A mapped user's comment on the ticket while the job is `needs_input` or finished becomes `follow_up_job`, as on GitHub.
- **Limits.** The existing front-door rate limits and active-job cap apply, with `jira` as a source.

## 9. Cost and value

- **Cost per verified PR**, per team, repo, environment and model, at `/api/usage?group=team` and on the usage page. A PR counts once it is merged with verify passing. Abandoned jobs count toward cost but not toward PRs, so failed work shows up in the number.
- **Budgets per period.** A `Budget` on a group or user per calendar month (`max_cost_usd`, `max_tokens`). When it is reached, new jobs and turns are refused with `over_budget`, and turns already running finish. Alerts at 50%, 80% and 100% go to the team's Slack channel, if Slack is set up.
- **Chargeback.** `hangar usage export --month 2026-11 --by team` writes a CSV of tokens, cost, sandbox-hours and PRs.
- **Routing** (0012 item 12). `model:` may be a rule list keyed on the ticket's type, labels or story points, e.g. Haiku or Sonnet for `docs` and `chore`, Opus for `bug` with priority ≥ High. It is measured against cost per verified PR with a 0007 experiment before it becomes a team default.
- **Best-of-N** stays opt-in per job or label. The usage page shows its share of spend.
- **`HANGAR_PROFILE=work`** sets the defaults this spec recommends in one place: gateway required, egress `trusted`, approvals `deny-risky`, retention on, subscription-login warnings, OIDC or IAP required for the dashboard. Each can still be overridden by setting it explicitly.

Compute is the smaller part of spend. The GKE defaults already scale sandbox nodes to zero, use Spot and power off at night (`deploy/terraform/gke`). Tokens are the larger part, which is why verify-in-loop, caching and routing come before anything else in this section.

## 10. Milestones

Sizes as in the specs README: S ≤ 1 day, M ≈ 2–3 days, L ≈ 4–6 days.

| # | Milestone | Size | Depends on | Phase |
| --- | --- | --- | --- | --- |
| M0 | Prove the current stack: `scripts/acceptance/gke.sh` on the work cluster; `HANGAR_PROFILE=work` with the settings that exist today (gateway required, retention via a bucket lifecycle rule) | S | none | A |
| M1 | OIDC login and IAP assertion check; `Principal.user/groups`; token owners; roles; audit actors | L | none | A |
| M2 | Credential owners, the attach rule, credential roles (`{role: model}`), migration and doctor checks | M | M1 | A |
| M3 | `HANGAR_REQUIRE_GATEWAY` and the refusals in decision 4 | S | none | A |
| M4 | Egress audit mode and `hangar egress report` (0003 M6) | M | none | A |
| M5 | Retention, `purge`, `export` | M | M1 | A |
| M6 | Team env repo sync, read-only repo environments, PR-on-save (0006 M4–M5) | M | none | B |
| M7 | `.hangar/env.yaml` overlays with the trusted-repo rule | S | M6 | B |
| M8 | `harness_settings:` with merging, rendering, plugins pinned into `/opt/hangar/team/` | M | none | B |
| M9 | Skills and plugins in hardened turns: CLI verification first, then loading from `/opt/hangar/team/` | M | M8 | B |
| M10 | Context resolvers for `jira`, `confluence` and `github` | M | M2 (service credentials) | B |
| M11 | Jira front door, identity map and `jira:` notifier with transitions | M | M1, M10 | B |
| M12 | Starter team harness in `examples/hangar-envs/` | S | M8, M10 | B |
| M13 | Personal profiles (0006 M6) | S | M1, M6 | B |
| M14 | Egress levels and enforcement (0003 M7) | M | M4 | C |
| M15 | Cost per verified PR, monthly budgets, alerts, chargeback export | M | M1 | C |
| M16 | Self-maintaining environments (0012 item 4(b–e)) | M | M6 | C |
| M17 | Model routing rules and their experiment (0012 item 12) | M | M15 | C |
| M18 | MCP gateway with tool allowlists (0007 M5) | L | M14 | C |

Phase A is what a security review needs before anyone else uses hangar. Phase B is what makes a team want to use it. Phase C makes it cheaper and closes the remaining review questions. Within a phase, milestones without a dependency between them can run in parallel, one PR each, like 0011 §3.

## 11. Acceptance (live, on the work cluster)

1. **Sign-in and tokens.** An engineer signs in through the company's IdP, runs `hangar connect claude-code`, and their token's jobs show their email in the audit log. Removing them from the IdP group makes that token fail on its next request.
2. **Credential access.** A user outside `group:payments` can't create a sandbox that attaches the payments team's credential. The error names the credential and the group.
3. **Gateway.** With `HANGAR_REQUIRE_GATEWAY=true`, an environment with `credential_mode: direct` fails validation, and no GitHub token is readable in any sandbox (env, files, `/proc`).
4. **Egress audit.** A week of audit mode produces a host list per environment. With enforcement on, `curl https://example.org` from a `trusted` sandbox gets the proxy's 403, and `uv sync` still works.
5. **Jira round trip.** Adding the `hangar` label to `API-1` by a mapped operator starts a job whose `in/` holds the ticket and its linked Confluence page, with no Atlassian credential in the sandbox. The ticket gets a start comment and moves to In Progress. The PR carries `API-1`, and the ticket gets the summary and moves to In Review. The same label added by an unmapped user leaves the job in `awaiting_approval`, with a comment on the ticket saying so.
6. **Team repo and hardening.** A PR to the team repo that adds a skill and a hook reaches new sandboxes within 10 minutes of merge. In a `deny-risky` turn that skill is listed and used, and the hook runs. An agent that writes `~/.claude/skills/<same name>` or edits `~/.claude/plugins` changes neither.
7. **Budgets.** A team at 100% of its monthly budget gets `over_budget` on new jobs, and its Slack channel got the 80% alert first.
8. **Cost per verified PR** is shown per team for the first full month and exported with `hangar usage export`.

## 12. Decisions needed from the owner

- **IdP and route:** OIDC directly, or IAP in front of GKE. IAP is less code in hangar, but it ties the dashboard to Google Cloud.
- **Model access at work:** org API keys per team, the company's LLM gateway, or Vertex/Bedrock. This decides §4.2's default and whether 0003 M9 moves into phase C.
- **Atlassian credential:** a read-only service account for resolvers and write-back (recommended), or per-user OAuth tokens so ticket comments show the real person. Per-user tokens need a consent flow per user and leases like Codex's.
- **Is in-turn Atlassian access needed in phase B,** or do resolvers and write-back cover the first month? Recommended: resolvers only, and measure what agents still ask for.
- **GitHub Enterprise Server or github.com at work.** The gateway's upstream checks are written against github.com and need a GHES test if it's the latter.
- **Retention defaults** (90 days for work, 365 days for audit) against the company's own policy.

## 13. What I couldn't verify

- **Skills and plugins under `--setting-sources ''`:** whether `--add-dir` or `--plugin-dir` loads them, and whether a same-named skill in a directory the agent owns would shadow one in a hangar-owned directory. M9 starts by testing this against the real CLI.
- **Atlassian's remote MCP server:** which authentication it accepts from a headless client (OAuth only, or also API tokens or service accounts), and its current host name.
- **Jira Cloud webhooks:** the exact header and format of the HMAC signature for webhooks registered with a secret, and whether Automation's web-request action can sign at all. §8 assumes it can't.
- **Codex's provider settings** for a company LLM gateway in the pinned Codex version.
- **IAP:** whether its path exemptions cover streamable-HTTP MCP and SSE without buffering. The acceptance script's unbuffered-stream check would catch it.
