# 0010: Sessions, resumable streams of work

!!! note "Design record"
    Written 2026-10-12; **built**. The current behaviour is on [sessions.md](../sessions.md); the status line below says what shipped.

Status: built (2026-10-15): #37; M4 partly. · Date: 2026-10-12 · Builds on: 0001 (runs, harness resume), 0006 (environments, harnesses), 0009 (jobs)

> Part of a set. Ownership, cross-spec decisions and the combined build order are in [docs/README.md](README.md); where this spec conflicts with that page, the page wins.

## 1. Summary

A **session** is a stream of work you can come back to: "the CSV export fix", "Early Signal onboarding flow". It works the way Claude's cloud sessions do: **the conversation and the repo state are durable; the computer is not.**

- **While you work:** a session runs in a sandbox like any other. After every agent turn, and before the sandbox goes away for any reason, hangar **checkpoints** it to the control plane:
  - the harness's own conversation files (Claude Code, Codex and pi all keep resumable transcripts on disk);
  - unpushed commits, as a git bundle;
  - uncommitted changes;
  - untracked files.
- **When it goes idle:** after `idle_minutes` with no agent running and no one attached to a terminal, hangar checkpoints the session and **releases the sandbox**. A suspended session costs a few megabytes of storage, not a running pod.
- **When you come back:** `hangar session open <name>`, MCP `open_session`, or the Resume button in the dashboard does four things:
  1. boots a fresh sandbox of the session's environment (a warm one when the pool has it);
  2. checks out the session's branch;
  3. replays the commits and changes;
  4. puts the conversation files back where the harness expects them.

  Then `send(message)` runs the agent with `--resume <id>`, so it continues *the same conversation* with full context. Or an engineer opens the terminal and runs `claude --resume` interactively.

A session is the unit of *ongoing work*. A job (0009) is a *request* for work. In M2 every job runs in a session, so replying on a PR a week later resumes the job's session instead of failing with "the sandbox is gone".

## 2. Why

- Today the work survives only through git. A sandbox that expires takes with it:
  - uncommitted changes;
  - installed dependencies;
  - **the agent's conversation**, meaning everything it learned about the task.

  Coming back means a cold start and re-explaining.
- Keeping sandboxes alive overnight to avoid that is expensive. One warm 2 vCPU pod pins an n2-standard-8 node: about $284/month on demand, because the sandbox pool otherwise scales to zero.
- Jobs have the same problem: a follow-up after `keep_minutes` fails.
- The harnesses already persist resumable conversations to disk. Verified with the CLIs, 2026-10-12:

  | Harness | Conversation files | New session with a chosen id | Resume |
  | --- | --- | --- | --- |
  | Claude Code | `~/.claude/projects/<cwd-slug>/<uuid>.jsonl` | `--session-id <uuid>` | `-p … --resume <uuid>` / interactive `claude --resume <uuid>` |
  | Codex 0.162 | `~/.codex/sessions/YYYY/MM/DD/rollout-…-<id>.jsonl` | no (id printed as `thread_id` in `--json` output) | `codex exec resume <id> [prompt]` / interactive `codex resume <id>` |
  | pi 1.1.0 | `~/.pi/agent/sessions/<cwd-slug>/…` | `--session-id <id>` (creates it if missing) | same flag |
  | Antigravity 1.3.3 | `~/.gemini/antigravity-cli/conversations/<id>.db` | no (id is `conversation_id` in the stream-json `init` event) | `--conversation <id>`. An unknown id silently starts a new conversation; hangar's post-turn check catches that. |

  The cwd slug matters: hangar always checks the repo out at `/workspace/<repo>`, so the slug is the same in every sandbox of a session.

## 3. Goals / non-goals

Goals:

- Resume a session days later with the agent's conversation, commits, uncommitted changes and untracked files intact.
- Suspend idle sessions automatically. A suspended session uses no compute.
- No work lost when a sandbox dies: reaper expiry, manual destroy, or a node going away. The loss is bounded by the checkpoint interval.
- Same experience from the CLI, MCP and the dashboard, and (M2) from jobs and GitHub.

Non-goals (M1):

- Restoring running processes (dev servers, tmux windows). That is M4, with GKE Pod Snapshots.
- Moving a conversation between harnesses natively. Handoff (§5.5) covers it with a summary.
- Sharing one session between people at the same time. One active sandbox per session.

## 4. User stories

1. Monday, Sean runs `hangar session new early-signal --env early-signal "build the onboarding flow"` and steers the agent for an hour. He closes the laptop. Thirty minutes later the session suspends. Wednesday, `hangar session open early-signal` gives him a sandbox in about 30 seconds on the same branch, with his uncommitted sketch and the agent remembering the whole Monday conversation. He sends "now add the email step".
2. From Claude Code: `list_sessions()`, then `send_to_session("csv-fix", "the reviewer wants a test for empty files")`. hangar resumes the session, runs the turn, checkpoints, and returns the summary.
3. (M2) A reviewer comments `/hangar add a changelog entry` on a hangar job's PR nine days later. The job's session resumes and pushes to the same PR.
4. The dashboard's Sessions page lists streams of work, newest first: title, repo and branch, last activity, state, and the last summary. **Resume** opens the terminal in the restored sandbox.

## 5. Design

### 5.1 Model (`hangar/sessions.py`, table `sessions`)

```python
class SessionState(StrEnum):
    STARTING; ACTIVE; IDLE; SUSPENDING; SUSPENDED; ARCHIVED; FAILED

class Session(BaseModel):
    id: str                      # "ses-" + 8 hex
    name: str                    # unique per owner, slug; what people type
    title: str
    owner: str                   # token id / user, as jobs
    environment: str; harness: str; model: str | None
    repo: str | None; base_branch: str | None; branch: str          # hangar/session/<name> by default
    harness_session: str | None  # the harness's conversation id (uuid for claude/pi; thread id for codex)
    sandbox_id: str | None       # current sandbox, None when suspended
    state: SessionState
    turns: list[SessionTurn]     # message, run_id, started/finished, exit_code, summary
    checkpoints: list[Checkpoint]  # newest last; at most `session_keep_checkpoints`
    idle_minutes: int = 30
    job_id: str | None           # (M2) the job this session serves
    created_at; last_active_at; updated_at
```

`Checkpoint` records `id`, `ts`, `reason` (turn, suspend, destroy, manual, interval), `head` (sha), `base` (sha it builds on), `sizes` and `parts`.

### 5.2 Checkpoint

These steps run in the sandbox. Each artifact is read back by the control plane and stored **encrypted with the vault key** under `<data_dir>/sessions/<id>/<checkpoint>/`.

| Part | How | Notes |
| --- | --- | --- |
| `agent.tgz` | `tar czf - -C ~ <harness session dirs that exist>` | Conversation files only. Credential files are never in these directories. |
| `repo.bundle` | HEAD is read once and pinned to `refs/hangar/checkpoint`, then `git bundle create … refs/hangar/checkpoint --not --remotes` (falling back to `--not <base>`) | Commits the remote doesn't have. Pinning HEAD means a commit landing mid-checkpoint can't make the recorded head and the bundle disagree. |
| `wip.patch` | `git diff --binary <pinned HEAD>` | Staged and unstaged changes |
| `untracked.tgz` | `git ls-files -z --others --exclude-standard \| tar --null -T - -czf -` | Respects `.gitignore`. Skipped with a warning above `session_max_untracked_mb` (default 50). |
| `meta.json` | branch, head, base, harness, harness_session, cwd | |

Checkpoints are taken:

- after every agent turn;
- before suspend;
- **before any destroy**, through a fleet pre-destroy hook, so reaper expiry and manual destroys are covered;
- every `session_checkpoint_minutes` (default 10) while a sandbox is attached and something changed (the HEAD or the dirty-tree hash differs).

The last `session_keep_checkpoints` (default 5) are kept.

**Limits:**

- Every part is capped: `session_max_part_mb` (default 100), and `session_max_untracked_mb` (default 20) for untracked files. An oversized part is skipped with a warning on the checkpoint.
- A partial checkpoint (timeout, error) is deleted, never recorded.
- Interval checkpoints compare a fingerprint first: HEAD, the diff hash, untracked file names/sizes/mtimes, and the conversation files' listing. They skip when nothing changed.

### 5.3 Resume

`open(session)`:

1. Create a sandbox from `environment` with the session's `harness`, on `repo` at `base_branch`, with labels `hangar.session=<id>`. The sandbox comes from the warm pool when there is one.
2. Reset the tree (`git reset --hard && git clean -fd`, in case environment setup touched tracked files). Fetch the bundle (or the head commit) and check out `branch` at `head`.
3. `git apply --binary wip.patch`, then untar `untracked.tgz`.
4. Untar `agent.tgz` into `~`.
5. State `IDLE`. The terminal hint is `claude --resume <harness_session>` (or the equivalent).

If the newest checkpoint doesn't restore (for example, its head is missing), the older ones are tried in turn, newest first. The session's status says which one was used, so one bad checkpoint can't brick a session.

`send(session, message)` resumes first if the session is suspended, then runs one agent turn with the resume flags:

- **Claude Code:** `--session-id <new uuid>` on the first turn, `--resume <uuid>` after that.
- **pi:** `--session-id <id>` always.
- **Codex:** a plain first turn, whose `thread_id` hangar captures from the JSON output; after that, `codex exec resume <thread_id>`.

When the turn finishes, hangar takes a checkpoint, records the summary (the last assistant message in the output), and sets the session to `IDLE`.

### 5.4 Suspend and idle

**A session never suspends while it is being used.** A pass every minute counts all of these as activity:

- an agent turn running (and, for jobs, the whole verify-and-deliver step);
- a dashboard or CLI terminal open on the sandbox (counted live, and touched on every keystroke);
- a tmux client attached (`hangar sb attach`);
- anything that used the sandbox through hangar (exec, files, terminals, MCP) in the last 90 seconds.

hangar's own housekeeping (checkpoints, these probes) runs "quiet": it neither counts as activity nor fills the audit log.

Only after `idle_minutes` with none of that is the session suspended:

1. checkpoint;
2. destroy the sandbox;
3. set the state to `SUSPENDED`.

- `idle_minutes` is per session. You can change it at any time (`hangar session keep NAME --idle 0`, MCP `keep_session`, or the dashboard). 0 means the session stays up until you suspend it.
- The sandbox's own TTL is extended for the whole of every turn and while the session is live, so the reaper never takes a session mid-work.
- Expired sandboxes are reaped concurrently, so one session's checkpoint doesn't delay the others.

`hangar session suspend` suspends on demand. `archive` drops the checkpoints and keeps the record and transcript summary.

### 5.5 Handoff (harness without resume, or switching harness)

The new turn's prompt starts with a compact account of the session:

- the original task;
- each turn's message and summary;
- the branch;
- the last check result.

This works for any harness. It is used when a harness has no resume support, or when a session is reopened with a different harness. It is lossy compared with native resume, so it is labelled as such.

### 5.6 Surfaces

| Surface | Calls |
| --- | --- |
| REST | `POST /api/sessions`, `GET /api/sessions`, `GET /api/sessions/{id}`, `POST …/open`, `POST …/send`, `POST …/suspend`, `POST …/archive`, `DELETE …` |
| MCP | `new_session`, `list_sessions`, `get_session`, `open_session`, `send_to_session`, `keep_session`, `suspend_session` |
| CLI | `hangar session new/list/get/open/send/keep/suspend/archive` (`--wait`) |
| Dashboard | Sessions page: resume, message the agent, **Talk to the agent here** (a terminal running the harness's interactive resume), shell, idle control, turns and summaries |

### 5.7 Cost and speed

- **Suspended:** storage only. A conversation is typically under 5 MB and a WIP checkpoint under 50 MB.
- **Storage:** checkpoints go through a store:
  - the control plane's volume by default;
  - or **GCS** with `HANGAR_SESSION_STORE=gs://bucket[/prefix]`. GCS is durable, doesn't fill or die with the control plane's zonal disk, and uses the pod's Workload Identity token, with no client library.

  Terraform creates the bucket (`session_bucket` output) with a retention rule (90 days by default).
- **Resume time:** sandbox start, plus the restore.
  - On Kubernetes, uploads go in **one exec per file**, streamed on stdin (`head -c N`, since the exec websocket can't signal EOF), instead of one exec per 96 KB. The chunked path stays as a fallback.
  - While a sandbox starts, sessions and jobs show where the time goes: "building the image", "adding a node to the cluster", "a Google Cloud quota is exhausted (SSD_TOTAL_GB)", "pulling the image", "cloning", "running setup".
  - The start timeout is 15 minutes (it was 5), long enough for a node scale-up plus a first image pull.
- **Warm pools on a schedule:** `warm_pool_hours: "Mon-Fri 08:00-20:00 America/New_York"` keeps the pool only in working hours. Outside them it scales to 0, so the node pool can scale down. A warm sandbox makes resume about 10 seconds. Without one, it is a cold start: image already built, node already present, so about 30–60 seconds.
- **Prebuilt images:** at startup hangar builds the environments mapped for jobs (`HANGAR_GITHUB_REPOS`, or `HANGAR_PREBUILD_ENVIRONMENTS`), so the first job or session doesn't wait on a build.
- **Cluster settings (opt-in):** terraform variables `image_streaming = true` (GKE image streaming on the sandbox pool) and `sandbox_disk_gb = 100` (pd-balanced counts against the 500 GB SSD quota that blocked scale-up). Both default to the old values because changing them recreates the sandbox nodes: suspend sessions first, they resume from their checkpoints.
- **Checkpoint bucket:** no lifecycle deletion by default (`session_retention_days = 0`); hangar prunes old checkpoints itself, and a suspended session's newest checkpoint is its only copy of the work. Note that the hangar service account (which Cloud Build also runs as) can read and write the bucket: environment builds run with the same identity. Checkpoints are encrypted with the vault key, so a build step can't read them, but it could delete them; a separate build service account closes that.

## 6. Security

- Checkpoints are encrypted at rest with the vault key, and can only be read through the owner's session API.
- **Credential files are not checkpointed.** Only the harnesses' conversation directories are archived (`~/.claude/projects`, `~/.codex/sessions`, `~/.pi/agent/sessions`). Auth files and `~/.hangar/env` live elsewhere, and secrets are injected fresh on resume.
- **Transcripts can still contain secrets** if the agent printed one (for example `env`, or a key in a file it read). Transcripts are not redacted, because that would break resume. They are treated as sensitive, like the audit log. Summaries shown in lists and comments go through `broker.redact`.
- **Access:** M1 sessions are admin-only (REST `admin` scope and MCP), and every admin can see and resume every session. Per-owner restriction comes with user accounts (0002 OQ11).
- Restore runs inside the new sandbox; the control plane does not run git on untrusted bundles.

## 7. Milestones

| # | Milestone | Size | Depends on |
| --- | --- | --- | --- |
| M1 | Session model and store; checkpoint/restore (agent state, bundle, WIP, untracked); resume flags for Claude Code, Codex and pi; `send` with resume; idle suspend; pre-destroy checkpoint hook; REST, MCP, CLI; handoff fallback | L | — |
| M2 ✓ | Jobs run in sessions: every job's turns resume the agent's conversation; job context is checkpointed with it; `keep_minutes` releases via suspend; follow-ups after the sandbox is gone resume the session instead of failing | M | M1 |
| M3 ✓ | Dashboard Sessions page; activity-based keep-alive; idle control; Antigravity resume (verified, agy 1.3.3) | M | M1 |
| M4 (partly ✓) | Speed and durability. Done: GCS checkpoint store; streamed uploads; startup progress and pending reasons; scheduled warm pools; prebuild; image streaming. Not yet: sandbox-side fetch of checkpoints (signed URL); hot resume with GKE Pod Snapshots (opt-in). | M | M1, 0004 M2 |
| M5 ✓ | `fork_session(n)`: restore one checkpoint into N sandboxes and fork the conversation (Claude `--fork-session`, Codex `exec fork`, pi `--fork`; Antigravity hands off). Best-of-N jobs on top of sessions (0009 M3). Swarms (0008) later. | M | M1 |

## 8. Acceptance

1. Create a session, run a turn that commits and leaves an uncommitted change and an untracked file, then suspend. The sandbox is gone.
2. `send` a follow-up: a new sandbox has the commit, the change and the file, and the harness was invoked with its resume flag and the same conversation id.
3. A sandbox destroyed by the reaper leaves a checkpoint taken before the destroy.
4. Idle sessions suspend after `idle_minutes`; attached terminals keep them alive.
5. Live on GKE with Claude Code: the resumed agent answers a question about something said only in the first turn.

## 9. Open questions

1. ~~Where checkpoints live past M1?~~ Both are supported. GCS (`HANGAR_SESSION_STORE`) is recommended on GKE, and `hangar doctor` warns when it's not set.
2. Should suspend also push the branch? It is safer, but it publishes work in progress. Recommended: no; the bundle covers it, and a push stays opt-in.
3. Retention for archived sessions' transcripts: keep forever, or 90 days?
