# Jobs: handing work to cloud agents

A job is a piece of development work handed to hangar by a service, an app, an MCP client, the CLI or a GitHub issue. Hangar runs a coding agent on it in a fresh sandbox, checks the result with the repo's own command, and delivers a branch and a (draft) PR. Engineers can follow along, reply, or take over. The design is in [spec 0009](specs/0009-jobs-and-the-factory-front-door.md).

```
caller ──POST /api/jobs──► queue ─► sandbox (env + harness) ─► agent turn ─► verify ─► commit, push hangar/<job>, PR
   ▲                                                      ▲                                  │
   └──── signed callback: job.started / job.finished ─────┼──────────────────────────────────┘
                     engineer: follow-up (API, MCP, CLI, `/hangar` on the PR) or attach to the terminal
```

## 0. Log in and check your setup

```bash
hangar login https://<your hangar> --token-file ~/.hangar/gke-admin-token   # once; no more HANGAR_URL/TOKEN exports
hangar doctor                                                               # what's broken, and the fix for each
```

`hangar doctor` checks:
- the providers;
- Cloud Build permissions (the `actAs` grant);
- recent failed sandbox starts (quota);
- the GitHub token's access to each mapped repo;
- the webhook secret;
- that each mapped environment exists, has credentials, and is built;
- that checkpoint storage is writable.

## 1. Give a service a token

A `jobs` token can submit and read its own jobs and nothing else. It gets 403 on every other route and 401 on `/mcp`, and it can't be exchanged for an MCP OAuth grant. Within jobs it is fenced in:

- it can only target repos listed in `HANGAR_GITHUB_REPOS`, in the environments mapped to them, and it can't pick a provider;
- its callbacks are signed with the key bound to the token by an admin, not one the caller names;
- it sees only the jobs it submitted (ownership is the token id, not its name).

```bash
hangar cred add support-hook-key --harness webhook --kind generic       # the shared signing key
HANGAR_GITHUB_REPOS='{"acme/app": "python", "https://git.acme.com/tools.git": "node"}'
hangar token create support-tool --scope jobs --callback-secret support-hook-key   # prints the token once
```

`HANGAR_GITHUB_REPOS` maps `owner/repo` (GitHub) or an exact git URL to one or more comma-separated environments; the first is the default. Admin tokens and MCP clients can submit against any repo and environment.

## 2. Submit a job

```bash
curl -sS https://hangar.example.com/api/jobs \
  -H "Authorization: Bearer $HANGAR_JOB_TOKEN" -H 'Content-Type: application/json' -d @- <<'EOF'
{
  "instructions": "CSV export drops non-ASCII characters. Fix it and add a regression test.",
  "repo": "https://github.com/acme/app",
  "environment": "python",
  "verify": "pytest -q",
  "context": [
    {"name": "trace.txt", "kind": "log", "content": "Traceback (most recent call last): ..."},
    {"name": "sample.csv", "kind": "file", "content": "bmFtZQrDqQo=", "encoding": "base64"},
    {"name": "ticket", "kind": "url", "url": "https://support.acme.com/t/4411"}
  ],
  "callback_url": "https://support.acme.com/hooks/hangar",
  "idempotency_key": "ticket-4411"
}
EOF
```

| Field | Meaning |
| --- | --- |
| `instructions` | What to do (required, up to 64 KB). |
| `repo`, `base_branch`, `branch` | The repo to work in; the base defaults to the repo's default branch, and the branch defaults to `hangar/<job>`. A custom branch must be under `hangar/` and must not exist yet: jobs never push to existing branches. |
| `environment`, `harness`, `model`, `provider` | Where and with what to run. The environment can be left out for repos mapped in `HANGAR_GITHUB_REPOS`. |
| `verify` | The repo's own check, run by hangar after each turn. A failing check still delivers, as a draft PR. |
| `deliver` | `draft_pr` (default), `pr`, `branch` or `none`. |
| `context` | Files the agent is given in `~/.hangar/jobs/<job>/` (outside the repo, mode 0600), plus links listed in the prompt. Up to 50 items and 2 MB. |
| `callback_url`, `callback_secret` | Where to POST `job.started` and `job.finished`. `jobs` tokens always sign with their bound key; admins may name any `webhook` credential. |
| `idempotency_key` | Resubmitting with the same key returns the existing job. |
| `timeout_minutes`, `keep_minutes` | Limit per agent turn, and how long the sandbox stays after a turn for follow-ups. |

Then poll `GET /api/jobs/{id}`, or wait for the callback. The job view carries `state`, the `events` timeline, `result` (commits, diffstat, `pr_url`, verification exit code and output tail, the agent's summary), `sandbox_id` and `terminal_session`.

From the CLI:

```bash
hangar job submit "fix the unicode bug in CSV export" --repo https://github.com/acme/app \
  --env python -c trace.txt --verify "pytest -q" --wait
hangar job list
hangar job follow-up job-3fa2c1d0 "also handle the empty-file case" --wait
hangar job cancel job-3fa2c1d0
```

From an MCP client (Claude Code, Codex, …): `submit_job`, `get_job`, `list_jobs`, `follow_up_job`, `cancel_job`.

## 3. Verify callbacks

Each callback is `POST` with `{"event": "job.started" | "job.finished", "job": {...}}` and the header `X-Hangar-Signature: sha256=<hex HMAC-SHA256 of the raw body>`. A receiver checks it like this:

```python
import hashlib, hmac
ok = hmac.compare_digest(request.headers["X-Hangar-Signature"],
                         "sha256=" + hmac.new(KEY, request.body, hashlib.sha256).hexdigest())
```

Callbacks go only to public `https` hosts. Plain `http`, non-public addresses and internal names (including names that *resolve* to non-public addresses just before sending) need the host listed in `HANGAR_JOB_CALLBACK_HOSTS`. Context contents are never echoed in callbacks, and the agent's summary and verification output are redacted for known secrets before they leave hangar.

## 4. Trigger from GitHub

1. Store a GitHub token for the control plane (it opens PRs and comments; it does not go into sandboxes) and a webhook secret:
   ```bash
   hangar cred add gh-control --harness git --kind git_token
   hangar cred add gh-webhook --harness webhook --kind generic
   ```
2. Configure the control plane:
   ```bash
   HANGAR_GITHUB_TOKEN_CREDENTIAL=gh-control
   HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL=gh-webhook
   HANGAR_GITHUB_REPOS='{"acme/app": "python"}'      # repo -> environment(s), comma-separated
   ```
   On GKE, put these in the optional `hangar-site` config map, which `deploy/gke/up.sh` never overwrites, then restart:
   ```bash
   kubectl -n hangar create configmap hangar-site \
     --from-literal=HANGAR_GITHUB_TOKEN_CREDENTIAL=gh-control \
     --from-literal=HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL=gh-webhook \
     --from-literal=HANGAR_GITHUB_REPOS='{"acme/app": "python"}' \
     --from-literal=HANGAR_SESSION_STORE=gs://<project>-hangar-sessions \
     --dry-run=client -o yaml | kubectl apply -f -
   kubectl -n hangar rollout restart deploy/hangar
   ```
3. In the repo settings, add a webhook to `https://<hangar>/api/hooks/github` with content type `application/json`, the same secret, and the **Issues** and **Issue comments** events.

Then:

- Label an issue `hangar` (`HANGAR_GITHUB_TRIGGER_LABEL`), or comment `/hangar` on it, to start a job. The issue body and recent comments by owners, members and collaborators become context; other people's comments are left out. `/hangar harness=codex model=gpt-5.5 also add a test` picks the harness and model and adds instructions.
- Hangar comments when it starts and when it finishes, with the summary, verification and PR link. The PR says `Closes #N`.
- Comment `/hangar <message>` on the PR to continue in the same sandbox and branch.

Only owners, members and collaborators can trigger with comments. Only repos in `HANGAR_GITHUB_REPOS` are accepted, and GitHub's redelivery of the same event does not start a second job.

## 5. Work with the agent

Every job runs in a [session](sessions.md). The agent keeps its conversation between turns, and the job's sandbox, context and work in progress are checkpointed. So a reply always works:
- a `/hangar` comment on the PR a week later;
- `hangar job follow-up`;
- the dashboard's reply box.

If the sandbox was released, hangar starts a new one from the job's last checkpoint, and the agent continues where it left off.

- **Watch:** attach to the job's sandbox terminal from the dashboard, or with `hangar sb attach <sandbox_id>` (the run is tmux session `terminal_session`).
- **Steer:** follow up from the API, MCP, CLI or a PR comment. Each follow-up is a new agent turn with your message and the previous summary, on the same branch. The PR updates in place.
- **Take over:** work in the sandbox yourself. Your commits go out with the next follow-up's delivery.
- After a turn the sandbox stays up while anyone is using it, and at least `keep_minutes` (default 120). Then it is checkpointed and released.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_JOB_CONCURRENCY` | 4 | Jobs provisioning or running at once |
| `HANGAR_JOB_TIMEOUT_MINUTES` | 60 | Per agent turn |
| `HANGAR_JOB_KEEP_MINUTES` | 120 | Sandbox kept after a turn |
| `HANGAR_JOB_VERIFY_TIMEOUT_SECONDS` | 1800 | |
| `HANGAR_JOB_MAX_CONTEXT_BYTES` / `_ITEMS` | 2 MB / 50 | |
| `HANGAR_JOB_CALLBACK_HOSTS` | `[]` | Hosts allowed for http or internal callbacks |
| `HANGAR_GITHUB_TOKEN_CREDENTIAL`, `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL`, `HANGAR_GITHUB_REPOS`, `HANGAR_GITHUB_TRIGGER_LABEL` | | See §4 |

## Security notes

Issue text, comments and service context are untrusted input to the agent. Today's mitigations:

- the repo and environment allowlist for service tokens and GitHub triggers;
- the collaborator check for `/hangar` comments, and only maintainers' comments as context (a label already needs triage rights, but the issue *author* can be anyone);
- `jobs`-only service tokens that can't reach MCP or OAuth;
- secret redaction of everything the agent writes that leaves hangar (summaries, verification output, PR bodies, comments, callbacks);
- pushes only to new `hangar/` branches, never forced;
- a prompt that marks context as data.

Known gap: the callback check resolves the host before sending but doesn't pin the address, so DNS rebinding is not fully blocked. Without the [git gateway](security.md#git-credential-gateway) the agent's sandbox holds a git credential, so restrict `HANGAR_GITHUB_REPOS` to repos where a mistaken push to a `hangar/*` branch is acceptable, and keep PR merges human-approved. With `HANGAR_GATEWAY_URL` set, jobs push through the gateway: the sandbox has no GitHub token and can push only its own branch and `hangar/**`.
