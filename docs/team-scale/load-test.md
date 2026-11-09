# The team load test

This page is part of [Team scale](../team-scale.md).

`scripts/bench/team_load.py` runs the real app on the fake provider (sandboxes are local directories; agents are
scripts that print an event every 0.5 s) and plays 6 engineers against it. Each has its own token and, at the
rates an MCP client and an open dashboard produce:

- starts 10 sessions over MCP, a second apart, and submits 2 jobs;
- polls each session with `get_session` every 5 s and sends the next message when a turn ends (2 turns each);
- calls `list_sessions`, `list_sandboxes` and `list_jobs` every 10 s, and `exec` (by sandbox name) every 4 s;
- refreshes `/api/sessions`, `/api/sandboxes` and `/api/jobs` every 5 s, and creates and destroys a sandbox over REST
  every 20–60 s.

Before it starts, the database gets 20,000 settled sandboxes and 5,000 finished jobs: about six months of a team's
history. The server reports event-loop lag (how late a 50 ms timer fires), RSS, CPU time, and the time spent in
SQLite per store method, on the event loop and in threads. `tests/test_team_scale.py::test_team_load_smoke` runs a
small version on every CI run and fails if any request scans the sandbox table on the event loop.

```
uv run python scripts/bench/team_load.py --out bench/results/<date>-team-load.json
uv run python scripts/bench/report.py <before.json> <after.json>
```

Results on a shared 2-vCPU VM (`bench/results/2026-10-14-team-load-*.json`). The fake agents (and hangar's turn
driver, which on a real provider runs in the sandbox) run on the same two vCPUs as the server, and other work shared
the machine, so the after numbers are upper bounds; a second after run, under a host load of ~9, shows how they
degrade. The request rates are the same in every column; the after runs finish the same work 7–9× sooner.

| 6 × 10 sessions, 20k records of history | Before (main, host load ~2) | After (host load ~1–4) | After (host load ~9) |
| --- | --- | --- | --- |
| Event-loop lag p50 / p95 / max | 1,296 / 5,221 / 8,326 ms | 1.0 / 70 / 258 ms | 4 / 263 / 765 ms |
| `get_session` (MCP) p50 / p95 | 3,364 / 5,914 ms | 42 / 305 ms | 214 / 807 ms |
| `list_sandboxes` (MCP) p50 / p95 | 13,363 / 17,034 ms | 60 / 553 ms | 434 / 1,755 ms |
| `exec` by name (MCP) p50 / p95 | 9,249 / 15,395 ms | 63 / 526 ms | 577 / 1,693 ms |
| `GET /api/sandboxes` p50 / p95 | 2,369 / 5,633 ms | 68 / 247 ms | 159 / 438 ms |
| Create a sandbox (REST) p50 | 21,213 ms (n=23) | 32 ms (n=1) | 1,847 ms (n=2) |
| An 8 s agent turn, send to done, p50 / p95 | 44.0 / 65.8 s | 8.6 / 10.8 s | 9.5 / 11.5 s |
| Job queue wait p50 / p95 (12 jobs) | 83 / 261 s (4 slots) | 0.01 / 0.06 s (24 slots, set with `--set job_concurrency=24`; the default is now 8) | 0.02 / 0.11 s |
| SQLite time on the event loop | 310 s of a 354 s run | 4.8 s of 38 s | 8.6 s of 49 s |
| Server CPU / peak RSS | 330 CPU-s / 1,031 MB | 7.7 CPU-s / 258 MB | 8.3 CPU-s / 264 MB |
| Wall time for the same work | 354 s | 38 s | 49 s |

Before, the server spent 87% of the run parsing sandbox records on the event loop: `fleet.list()` behind every
listing, `fleet.get(name)` behind every MCP call that names a sandbox, two scans per create. Every other request
waited behind them.

**Soak.** 6 × 10 sessions with 20 s turns (4 each, all 60 running at once) and 24 jobs, while retention moved the
20,000 seeded records to the archive (`2026-10-14-team-load-soak.json`): event-loop lag p50 0.6 / p95 47 / max
528 ms, `get_session` p50 14 / p95 545 ms, every turn and job finished, peak RSS 267 MB, 22.5 CPU-s in 117 s. The
first soak, with retention moving 500 rows a transaction, had a loop lag p95 of 811 ms: one batch held the store's
lock for up to 0.9 s. Hence 100.

## Months of job and session history

The job and session services used to load every job and session ever made at startup and keep them: ~30 KB for a
finished job with its timeline, so 18,000 jobs (six months at 100 a day) cost over half a GB, and every listing sorted
all of them. Now each holds what is live (unfinished jobs, jobs whose PR is still on its way to landed or with
auto-fix triggers waiting, live, suspending or busy sessions) and whatever was saved or asked for in the last
`HANGAR_RESIDENT_MINUTES` (60). Everything else is read back from the store when asked for, by id, by name, or
through the store's indexes (`resident.py`): the GitHub and Jira lookups (by PR number, branch, head commit,
ticket), idempotent retries, the inbox, the usage report and the daily caps. Every 5 minutes the retention loop lets
go of what nothing needs any more. A record read back stays one object while it's in use, so two callers never hold
copies that overwrite each other's changes.

Listings (`list_jobs`, `list_sessions`, `GET /api/jobs`, `GET /api/sessions`, the recent-work resources) are pages
from the store's indexes, newest first, filtered there by person: a member's listing is all of their own work, not
what of it was left after the team's newest 500. Each item carries a `cursor`; pass the last one's back for the next
page (REST also answers `X-Next-Cursor` when there is one). Finished jobs and settled sessions untouched for 30 days
move to `jobs_archive` and `sessions_archive` (below), which listings and lookups read too.

The team load test with that history seeded (6 people × 5 sessions, 12 jobs; 20,000 sandbox records, **18,000
finished jobs and 3,000 sessions over six months**; `--job-history 18000 --session-history 3000`), two runs each on
the shared 2-vCPU VM, `bench/results/2026-10-14-team-load-history-*.json`:

| | Before (build/ops) | After |
| --- | --- | --- |
| RSS at idle, history loaded | 735 / 736 MB | 213 / 213 MB |
| Peak RSS | 744 / 746 MB | 222 / 222 MB |
| Server CPU for the same work | 25.6 / 25.7 CPU-s | 14.5 / 16.5 CPU-s |
| Event-loop lag p95 / max | 69 / 881, 67 / 859 ms | 60 / 127, 61 / 132 ms |
| `list_jobs` (MCP) p50 / p95 | 115 / 225, 115 / 265 ms | 87 / 191, 77 / 168 ms |
| `list_sessions` (MCP) p50 / p95 | 139 / 245, 112 / 722 ms | 73 / 152, 63 / 134 ms |
| `GET /api/jobs` p50 / p95 | 76 / 178, 65 / 223 ms | 65 / 133, 42 / 124 ms |
| `GET /api/sessions` p50 / p95 | 74 / 126, 87 / 400 ms | 65 / 176, 65 / 134 ms |
| SQLite time on the event loop | 1.2 s | 6.5 s |

The loop now spends more time in SQLite (a page of jobs is read and decoded per listing, ~6 ms for 50) and less in
Python over every job in memory. Reading the pages in a worker thread was tried: decoding holds the GIL, so it saved
the loop nothing and raised the peak RSS by ~35 MB. The `hangar_jobs` and `hangar_sessions` gauges are counted by
SQLite in a worker thread on a connection of their own, at most a minute old.

What remains, by SQLite time on the loop: `get` of one sandbox by id (sessions check that theirs is alive, ~0.4 ms
each, 5k calls a run), `put` of sessions (every state change writes the whole record), and the token lookup in
`auth.principal`, which decodes every API token on every request (~1 ms; see follow-ups).

**Retention and the export.** Archiving keeps everything; what deletes data is deleting a session (its event log and
manifests went with it) and testresults' windows (every case after 3 days, runs after 14). With run capture on
(`HANGAR_OUTBOX_URL`, [outbox.md](../api/outbox.md#retention-and-the-export-189)) neither deletes what the outbox hasn't
exported yet: a deleted session's files wait in `<data>/pending-delete/` until its runs are uploaded (or
`HANGAR_EXPORT_GRACE_DAYS`, 30, audited `retention.unexported_deleted`), and the test results of failed, abandoned
or failure-labelled jobs outlive the windows until exported. Months of history then live in the bucket, not on the
control plane's disk: hangar keeps what it needs to run, the sister tool what it needs to look back. Pending-delete
data counts toward the data volume; `hangar doctor` reports how much. With the outbox off, retention is as before.

**Provider calls.** At 60 sessions and 12–24 jobs the fake saw ~30–60 `exec`s a second, plus 5–15 file reads and as many writes:
turn-status long-polls, the activity watch, checkpoints, setup. On GKE each of those is a pods/exec through the API
server unless hangar-envd answers it, so **envd must be up at team scale** (`HANGAR_ENVD=auto`, the default).
