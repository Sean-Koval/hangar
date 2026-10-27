# Front-door security

Who may start work through each front door, and the fences every one of them ends in. Part of the [security model](README.md).

Work enters hangar through REST, MCP, A2A, GitHub webhooks, Slack, signed inbound webhooks and schedules. Each one decides who may start work, and every one of them ends in the same job service, which applies the limits below.

| Front door | Who can start or steer work | Fence |
| --- | --- | --- |
| REST, A2A | An admin token: anything. A `jobs` token: its own jobs only (another token's job id or A2A `contextId`/`taskId` is "not found") | `jobs` tokens: repos in `HANGAR_GITHUB_REPOS` and their environments, no provider choice, their `max_budget` |
| MCP | Admin tokens, and people's `user` and `mcp` tokens; an OAuth grant acts as the token that approved it | A member's tokens reach only their own work ([People](people.md#people)); `mcp` tokens can't set `secrets`, `image`, `base`, `runtime_class` or `credential_mode: direct` on an environment, call admin-only tools, or approve jobs |
| GitHub | Linked accounts and trusted people ([triggers.md](../triggers.md#1-who-can-start-work-from-github)); anyone else's trigger waits for approval, and their comments can't continue a job or answer its question. A person's job is continued only by that person's linked account | mapped repos, their environments |
| Slack | Linked accounts; members of your workspace in a mapped channel (or `HANGAR_SLACK_USERS`); Slack Connect guests from other organizations only when listed. A person's job is continued only by them | like a `jobs` token |
| Inbound webhooks | Whoever holds the source's HMAC key | the source's configured repo and environment |
| Schedules | Admin tokens create them | the template, validated at creation |

Text that hangar's caller didn't write is fenced in the prompt as untrusted: issue bodies and comments by untrusted authors, an untrusted person's `/hangar` request, Slack thread context, and everything from an inbound webhook ([triggers.md](../triggers.md#untrusted-text-in-the-prompt)).

**Agents in `credential_mode: direct` can reach the GitHub front door.** Without the [git gateway](git-gateway.md), a sandbox holds a GitHub token. The account behind it has write access to the repo, so by default (author association) its comments are trusted. An agent can then comment `/hangar approve` on an outsider's job, start new jobs, or continue another job's PR as that account. Use the gateway for GitHub-triggered work, or set `HANGAR_GITHUB_TRUSTED` and leave that account out.

## Intake posture

Text from a ticket, an issue, a Slack thread or a webhook is fenced in the prompt, but a prompt is not a boundary: with open egress and a GitHub token in reach, an injected prompt has a way out and the means to push. The intake posture (`posture.py`) makes two boundaries real for every **intake job**: one whose `source` is `github`, `github-projects`, `jira`, `slack` or `webhook`, one with an untrusted author (`untrusted_source`), or one with an untrusted context item. That covers [PR auto-fix](../jobs/agent-loop.md#pr-auto-fix) rounds (CI output and review comments are untrusted context; the job becomes an intake job from that turn on) and [agent-led environment setup](../environments.md) started from an issue (its child jobs and validation sandbox inherit the labels). A person's own submission over MCP, REST or the CLI is not an intake job.

| Boundary | What every intake job gets |
| --- | --- |
| Git | the [git gateway](git-gateway.md) must serve the repo's host; otherwise `submit` refuses the job with a message naming `HANGAR_GATEWAY_URL`, which the front door posts to the issue, ticket or thread |
| Egress | at least `allowlist`, even in an `open` environment |

No setting turns either off. `HANGAR_INTAKE_POSTURE=warn` and `off` were removed after v0.2.0; hangar refuses to start with either ([removed switches](../upgrading.md#removed-switches)).

**The egress floor.** An enforced intake job carries the label `hangar.egress.floor: allowlist`; its session and every sandbox it starts (best-of-N attempts, follow-ups, resumes) carry it too. `egress.resolve` raises the environment's mode to the floor, so the sandbox goes on the proxy-only network (the internal `hangar-egress` network on Docker, the proxied NetworkPolicy and a SandboxTemplate of its own on GKE) and gets the environment's presets and `allow`, the repo policy's hosts, and the harness's API hosts. A repo policy can still tighten it to `none`, never loosen it. A sandbox `info.egress` (and its [manifest](../api/manifest.schema.json)'s `egress`) records `floor: allowlist` and `floor_reason: intake`; the review packet says `Egress: allowlist (intake floor)`. A sandbox started before the job became an intake job (an auto-fix round on a person's job), or one parked for hot resume, is checkpointed and replaced before the turn runs.

**Warm pools.** A warm sandbox of an `open` environment is on the open network, so it is never claimed for an intake job: the job cold-starts, 30–60 s more. Where that matters, declare a proxied pool: an environment with `egress: {mode: allowlist}` and `warm_pool:` for intake work (map it in `HANGAR_GITHUB_REPOS`), whose warm sandboxes are proxied already. Hosts outside the allowlist go through [egress requests](egress.md), answered by a person.

**Where it can't run.** The floor needs hangar's egress proxy: `HANGAR_EGRESS_URL` on GKE, hangar on the Linux Docker host itself (not inside Docker Desktop's VM, and not in the Compose container) on Docker. Elsewhere an enforced intake job in an `open` environment fails to start with a message that says so, rather than run unfenced; run intake work on a Linux Docker host or GKE. `hangar doctor` reports the posture, the gateway, the egress proxy per provider, and each environment mapped for intake work with its effective mode. Under `enforce` without the gateway it fails once an intake front door is configured (a GitHub webhook secret, Slack's signing secret, `HANGAR_JIRA`, `HANGAR_GITHUB_PROJECTS` or `HANGAR_INBOUND_WEBHOOKS`), naming the doors whose jobs it refuses; before any is, as on a fresh `hangar up`, nothing is refused and it only warns. Either way its fix is the command that turns the gateway on where hangar runs ([turning it on](git-gateway.md#turning-it-on)): on a Linux laptop that is `HANGAR_GATEWAY_URL` and `HANGAR_GATEWAY_HOST` (the gateway binds loopback otherwise, out of the sandboxes' reach) and a restart. A job queued while the pools are powered down gets the floor when it claims its sandbox.

## Signed requests

- GitHub (`X-Hub-Signature-256`), Slack (`v0`) and inbound webhooks (per-source HMAC) are verified over the raw body, before anything parses it, and compared in constant time (as bytes, so a malformed header is a 401, not an error).
- **Replays.** A signed request is identified by what its signature covers, and remembered in the store, so a restart doesn't forget it. GitHub deliveries and inbound webhook deliveries are kept for `HANGAR_WEBHOOK_REPLAY_HOURS` (7 days); a replay with a fresh, unsigned delivery id or timestamp header is still a duplicate and does nothing. Slack events are deduplicated by `event_id`, and slash commands by signature within Slack's five-minute window.
- A missing secret is reported as "not stored" without naming the credential.
- **Jira** signs its admin webhook (`X-Hub-Signature`); use it for issue events. Its Automation rules can't sign, so hangar accepts a hidden `X-Hangar-Token` as a fallback. That token only proves who holds it, so an Automation request starts a job only when the issue's own history shows the trigger transition within the last 10 minutes, at most 3 times per issue a day, and only as a `hangar:automation` request. A stale transition, a wrong event or a wrong token alerts the admins (inbox, and a Slack DM), and `hangar doctor` warns on a token older than 90 days and on Automation in use without signed issue events. Rotate it with `hangar cred add <name> --rotate` ([intake.md](../intake.md#fallback-an-automation-rule)).

## Outbound webhooks (SSRF)

Job callbacks and A2A push notifications go only to public `https` hosts unless the host is in `HANGAR_JOB_CALLBACK_HOSTS`. Just before each send, the name is resolved, every address is checked, and the connection is made to the checked address itself (TLS still verifies the name). A name that re-resolves to `127.0.0.1` or a metadata address between the check and the connection (DNS rebinding) reaches nothing. Redirects aren't followed, and proxy environment variables are ignored for these requests.

## Scopes

Every MCP tool and resource and every REST route is listed, by verb, with the token scope it requires and its owner rule, in [`tests/fixtures/scope_matrix.yaml`](https://github.com/Sean-Koval/hangar/blob/main/tests/fixtures/scope_matrix.yaml). `tests/test_scope_parity.py` checks that list against the code on every change to a route or tool module: a tool's scope comes from how `mcp_server.tool()` registered it, and a route's from its FastAPI dependency. A new tool or route fails CI until it has an entry, and the failure prints one to paste. Where one verb's MCP and REST twins differ, the entry has to say why (`exception:`). Every privileged verb whose twins agree (approving and rejecting jobs, `hangar://status`) is also called on both surfaces, as a member, as an admin's `mcp`-only token and as an admin login, and must answer the same way. In short:

| Surface | Scope | Owner rule |
| --- | --- | --- |
| MCP tools and resources | any token `/mcp` accepts (admin, `mcp`, `user`, OAuth grants) | own records unless admin (`people.Guard`); `approve_job` / `reject_job` follow [who may approve](../triggers.md#who-may-approve); `hangar://status` is admin-only |
| REST `/api/jobs*`, experiments, manifests | admin, or a `jobs` service token | a `jobs` token's own jobs |
| REST approve / reject | [who may approve](../triggers.md#who-may-approve) | as MCP |
| REST `/api/me`, tokens, credentials, inbox, env requests, envbuilds, records | admin, or a person's `user` login | own records unless admin |
| The rest of REST | admin | everything |
| Webhooks, `/e2b`, the gateway's `/git`, `/pkg`, `/cache`, `/registry` | their own credentials (signatures, `sandboxes` tokens, sandbox grants) | the source's or the sandbox's own |

## Front-door limits

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

## Audit

Every approval decision (`job.approve`, `job.reject`, `job.approval_expired`, `job.approve.refused`, `job.reject.refused`) and every refused request is in the audit log (`GET /api/audit`, the dashboard's audit view). One rule decides approvals on every path, MCP, REST, GitHub and Jira: an admin or a repo policy approver, with a login token over MCP and REST, never on their own job ([who may approve](../triggers.md#who-may-approve)). Each decision records the deciding person, whether it was a self-approval (`HANGAR_SELF_APPROVAL=allow_solo`) and the rule that allowed it:

- `request.rejected`: any 401, 403, 413 or 429 answer, with the client address, method and path;
- `job.refused`: a submission or follow-up that was refused, with why (`rate_limited`, `too_many_active`, `invalid`, which covers a token's fence) and the caller;
- `hook.replay`: a duplicate signed delivery;
- `github.refused`: an untrusted person's attempt to continue a job or answer its question;
- `hook.automation.stale_transition`, `hook.automation.capped`, `hook.automation.wrong_event` and `jira.alert`: Jira Automation requests refused, and the alerts they raised ([intake.md](../intake.md#fallback-an-automation-rule)).

Refusals are themselves limited to `HANGAR_AUDIT_REJECTIONS_PER_MINUTE` (30) per client address or caller, so a flood can't fill the audit log; the next entry that gets through records how many were dropped (`suppressed_before`).

