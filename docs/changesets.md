# Change sets: one ticket across several repos

Some work doesn't fit in one repository. A ticket that adds a table, the queue the service reads from and the endpoint that uses both touches the database repo, the infrastructure repo and the code repo, and the release only happens once the deploy repo points at the new build. A **change set** is that work as one unit:

- **One agent, one sandbox.** The agent works in every repo of the set at once, each checked out at `/workspace/<role>`, in one session (one conversation, one set of checkpoints).
- **One PR per changed repo.** At the end of a turn hangar checks each repo the agent changed with that repo's own `verify`, scans it for secrets and pushes it, then opens one PR in each, every PR linking its siblings.
- **Landing in order.** Each PR lands under its own repo's [policy](repo-policy.md) (its approvers, CODEOWNERS, [self-approval](landing.md#self-approval-who-may-be-the-only-approver), taint), with one more gate: the repos earlier in the profile's `land_order` have merged.
- **The deploy repo last** (next): once every changed repo has merged, a deterministic step (no agent) will change the trigger-deployment repo and open its PR. Until then a profile names the deploy repo as a `read` role, for the agent to read.

Single-repo jobs are unchanged: a job without `changeset` runs exactly as [jobs](jobs.md) describes.

## The profile

An admin describes the repos of a service once, as a **change-set profile**. Profiles are admin config, kept in hangar's store and set over the API or the CLI. Keep their YAML in a reviewed repo (your deploy repo's `policy/changesets/`, say) rather than in a target repo's `.hangar/`, which hangar treats as untrusted: whoever can push there must not be able to reorder landing.

```yaml
schema: hangar.changeset-profile/v1
name: payments-bot
environment: work-multirepo             # one image with every repo's tools (code toolchain, terraform, a DB client)
repos:                                  # cloned to /workspace/<role>
  infra:    {repo: https://github.com/acme/payments-infra.git}
  database: {repo: https://github.com/acme/payments-db.git}
  code:     {repo: https://github.com/acme/payments-bot.git}
  deploy:   {repo: https://github.com/acme/payments-deploy.git, agent: read}   # the agent may read it, never edit it
land_order: [database, infra, code]     # each lands only after the earlier changed ones merged
```

| Field | Meaning |
| --- | --- |
| `schema` | `hangar.changeset-profile/v1`. |
| `name` | The profile's name, what a submission names: lowercase letters, digits, `.`, `_` and `-`. |
| `environment` | The parent job's environment (unless the submission names one). One image must carry what every role needs to verify its repo. |
| `repos` | Roles (lowercase names) and their repos: `repo` (a clone URL), `agent` (`write`, the default, or `read`: cloned for the agent to read, never delivered) and an optional `base_branch` (default: the repo's default branch). Every role names a different repo. |
| `land_order` | Every role the agent writes, in the order their PRs land. A role that didn't change is skipped. |
| `primary` | The role whose repo is the parent job's `repo` (its checkout is the one in session checkpoints). Default: `code` when there is one, else the last role in `land_order`. |

`hangar changeset validate FILE` checks a profile offline. Setting it (`hangar changeset profile-set FILE`, `PUT /api/changesets/profiles`) also checks that the environment exists. Each change set keeps a copy of the profile it was made from and its `profile_digest` (sha256 over the profile's content), so editing a profile changes new sets only.

## Submitting a change set

Name the profile instead of a repo, and the ticket the work is for.

**MCP** (`/mcp`, from Claude Code, Codex or a bot):

```json
{"tool": "submit_job",
 "arguments": {"instructions": "PAY-412: add refunds (table, queue, endpoint)",
               "changeset": "payments-bot", "ticket": "jira:PAY-412", "deliver": "draft_pr"}}
```

**REST** (`POST /api/jobs`, a jobs-scoped or admin token): the same `SubmitJob` body every job uses, with two fields more.

```json
{"instructions": "PAY-412: add refunds (table, queue, endpoint)",
 "changeset": "payments-bot",
 "ticket": "jira:PAY-412",
 "deliver": "draft_pr"}
```

**CLI:** `hangar job submit "PAY-412: add refunds" --changeset payments-bot --ticket jira:PAY-412 --wait`.

| Field | Meaning |
| --- | --- |
| `changeset` | The profile's name. Leave `repo` out (or give the primary role's repo) and `branch` out: each repo's branch comes from its policy. |
| `ticket` | `scheme:key`, e.g. `jira:PAY-412` or `github:acme/payments-bot#12`. Optional unless a repo's policy has [`require_ticket`](#company-conventions-in-the-repo-policy). A Jira intake job uses its issue's key. |
| `deliver` | `draft_pr` (the default) or `pr`: one PR per changed repo. `none` delivers nothing. `branch` is refused. |
| `strategy` | `single` only: best-of-N over a set isn't supported. |

A jobs-scoped (service) token may submit a change set only when **every** repo of the profile is in `HANGAR_GITHUB_REPOS`; a profile never widens that allowlist. The answer is the parent job, as for any job, with its `changeset` (id, state, roles, parts and their PRs). Follow it with `wait_for_turn`, `get_job` or `hangar job get`.

## What happens

1. **Admit.** The profile fills in the primary repo and the environment; each repo's policy applies (a `require_ticket` anywhere in the set refuses a submission without a ticket). The parent job's events name the change set, its profile digest, its roles and its landing order.
2. **Work.** The sandbox clones every role into `/workspace/<role>`: the roles the agent writes on their branch, `read` roles on their base. The agent starts in the primary role's checkout; its instructions list every checkout, which ones are read-only, and the landing order. Its git grant may fetch every role's repo and push none (as for every job, hangar pushes).
3. **Check.** The primary role runs the job's `verify` as any job does (with the check inside the turn and its fix rounds). Each other role the agent changed runs its own repo policy's `verify` in its checkout. Any failure fails the job (`verify_failed`; the tail names the role, e.g. `[infra: \`terraform validate\` exited 1]`), and what the agent did is delivered as drafts.
4. **Deliver.** For each changed role, in `land_order`: what the agent left uncommitted is committed, the change is scanned for secrets, and hangar pushes it with a delivery grant pinned to that repo, branch and commit (a copy the agent lifts can push nothing else, to no other repo). Hangar then opens the role's PR as a **part job** (`source: changeset`, with `parent`, `role` and its own branch and landing state; a part never runs an agent). Changes in a `read` role are not delivered, and the parent's events say so. Once every PR has a number, each PR body gets the sibling table (role, repo, PR, state) and the landing order, and ends with the `Hangar-Job:` (the parent, the agent's job) and `Hangar-Changeset:` trailers. The set is now `landing`.
5. **Land.** Each PR needs its own repo's approvers: SRE through CODEOWNERS on infra, the database owners on the database repo (which may set `self_approval: never`). The parent's taint is copied to every part, so a tainted set opens all its PRs as drafts that need someone other than the requester. A PR approved before its turn waits, and its events say `not landing yet: waiting for database #12 to merge first`. When a part merges, the next approved one arms by itself. The parent's sandbox is checkpointed away before any part arms, and an agent turn on the parent (a follow-up, an answer, a fix round) disarms every part first: auto-merge is never armed while an agent can push.
6. **Done.** The set is `done` when every changed role merged.

Follow-ups go to the parent: a `/hangar` comment on any part's PR, `follow_up_job` on a part, or on the parent itself, continues the agent's conversation. The next delivery updates the open PRs (and opens one for a role the agent changes for the first time); a role whose PR already merged keeps its new commits in the sandbox and the parent's events say to submit a new job for them.

## States

| State | Meaning |
| --- | --- |
| `working` | The parent job is running; no PR yet. |
| `landing` | PRs are open; they land in order. |
| `done` | Every changed role's PR merged. |
| `broken` | A part's PR was closed without merging, or a merged part was reverted (as the nightly [revert pass](metrics.md) records it). Later parts are disarmed and don't land, and `changeset.broken` is announced with the reason. Nothing is reverted automatically: a person decides what to do with what merged. |

Each change of state is an event on the parent job (`changeset.landing: …`, `changeset.done: …`, `changeset.broken: …`), a callback to its `callback_url` with that event name, and an audit record.

## Company conventions in the repo policy

Three [repo policy](repo-policy.md) fields apply to every job, single-repo or not, and to each repo of a change set:

| Field | Meaning |
| --- | --- |
| `branch_template` | The job's branch in this repo, e.g. `feature/{ticket_key}-{job}`. It must contain `{job}` (a job only ever pushes a new branch of its own). Without a ticket, a template that uses one falls back to `hangar/<job>`, and the job's events say so. Unset: `hangar/<job>`. A branch a caller names itself must still be under `hangar/`. |
| `require_ticket` | `true` refuses a job with no `ticket`. |
| `title_template` | The title of the job's PRs, e.g. `{ticket_key}: {title}`. Unset, or without a ticket: the job's title. |

Placeholders: `{job}`, `{ticket}`, `{ticket_key}` (`PAY-412` for `jira:PAY-412`), `{title}` and `{role}`. Set them with `PUT /api/repos/policy` like any field.

## Limits in v1

- No atomic cross-repo merge and no rollback: the PRs land one after another, and a broken set stops, nothing more.
- No best-of-N over a set.
- Every role's repo must be on the same git host for the [git gateway](security.md#git-credential-gateway) to serve them; one that isn't gets no grant, and the sandbox's warnings say so.
- Session checkpoints hold the primary role's checkout. The other roles' work is safe once hangar has pushed it (at the end of every turn); a resumed sandbox clones them again and continues their branches.

## API and CLI

| Endpoint (admin tokens) | CLI | |
| --- | --- | --- |
| `GET /api/changesets/profiles` | `hangar changeset profiles` | Every profile, with its digest. |
| `PUT /api/changesets/profiles` | `hangar changeset profile-set FILE` | Create or replace a profile (the YAML above, as JSON). |
| `GET /api/changesets/profiles/{name}` | `hangar changeset profile NAME` | One profile. |
| `DELETE /api/changesets/profiles/{name}` | `hangar changeset profile-rm NAME` | Remove a profile; the sets made from it keep their copy. |
| `GET /api/changesets[?state=landing]` | `hangar changeset list [--state …]` | Change sets, newest first, each with its parts and their PRs. |
| `GET /api/changesets/{id}` | `hangar changeset get ID` | One change set. |
| | `hangar changeset validate FILE` | Check a profile offline. |
