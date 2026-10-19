# Troubleshooting

Start with `hangar doctor`: it checks the credentials, the front doors, the gateway, the power state and the loops,
and each failing line says how to fix it (`hangar doctor --fix` offers the fixes it can apply itself). The pages below
cover what it doesn't: what you see from the outside, and where to look.

## Labelling an issue or commenting `/hangar` does nothing

Open the repo's **Settings → Webhooks**, the hangar webhook, then **Recent Deliveries**. The response hangar sent says
what happened:

| Delivery | Why | Fix |
| --- | --- | --- |
| None at all | GitHub isn't sending: the webhook is inactive, points elsewhere, or lacks the events | It must be active, point at `https://<hangar>/api/hooks/github` (reachable from the internet), and send the events hangar handles (Issues, Issue comments, Pull requests, Pull request reviews and review comments, Check runs, Check suites, Statuses). `hangar setup github --repo owner/repo=ENV` registers it that way; `hangar doctor` checks each mapped repo's webhook, and `hangar doctor --fix` registers it again |
| `404` "GitHub webhook is not configured" | `HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL` isn't set | `hangar setup github` (it generates and stores the secret, and sets it on the webhook) |
| `503` "GitHub webhook secret is not stored" | The setting names a credential the vault doesn't have | The same; `hangar doctor` fails `github webhook secret` |
| `401` "bad signature" | The webhook's secret in GitHub isn't the stored one (`gh-webhook` by default) | Run `hangar setup github` again, which sets the stored secret on the webhook |
| `200` with `{"ignored": "…"}` | Hangar received the event and skipped it on purpose; the reason says why | "repo … is not in HANGAR_GITHUB_REPOS": add it (`hangar setup github --repo`). "not the trigger label": the label must be `HANGAR_GITHUB_TRIGGER_LABEL` (`hangar`). "not a /hangar comment": the comment must start with `/hangar`. "… is not trusted to continue jobs": the commenter isn't an owner, member or collaborator (or in `HANGAR_GITHUB_TRUSTED` when that is set). "duplicate delivery": a redelivery of one already handled. "job … is already working on …": one job per issue at a time |
| `202` with `{"ok": true, "job": …}` | A job was queued | The issue gets "Queued as hangar job …" at once; an untrusted labeller's job waits for an approver's `/hangar approve` instead, with a comment saying so |
| `429` | A front-door rate limit ([security.md](security.md#front-door-limits)) | Wait; a redelivery after the limit refills runs |

While the sandbox pools are powered down (01:00–10:00 ET) deliveries are still received and the jobs wait. With the
whole infra `off`, nothing receives them: GitHub's are redelivered on the next start if they are at most 3 days old
([cicd.md](cicd.md#taking-a-week-off)).

Jira and Slack have their own setup checks: [intake.md](intake.md) and [slack.md](slack.md#set-it-up).

## `hangar job list` says `No jobs.`

The CLI lists what its token may see, on the hangar it is talking to.

- **Which hangar.** `HANGAR_URL`, else the one `hangar login` saved; with neither, `http://localhost:8787`. Point it at
  the control plane that ran the job.
- **Whose jobs.** An admin token sees every job. A member's token sees all of that person's jobs, whichever door they
  came through. A `jobs`-scoped service token sees only the jobs it submitted, so jobs from GitHub, Jira, Slack or MCP
  don't show; use an admin token to see those.
- **Filters.** `--state` and `--reason` narrow the list (the message then says `No jobs in state '…'`), and `--limit`
  is 20. Shadow runs never appear in job lists; an admin reads them with `GET /api/jobs?source=shadow`.

## A job waits

**In `provisioning` for minutes, the first time.** When an environment has no image for the provider yet, its first
sandbox builds it, and any harness packs: the job's events say "building the `<env>` image (first use or the
environment changed; usually a few minutes)". Later jobs reuse it. To build ahead of time, run `hangar env build <env>`
(it streams the log), choose **Build** under **Environments** in the console, or `hangar images plan` and
`hangar images build` for every environment. At startup hangar also builds the environments in
`HANGAR_PREBUILD_ENVIRONMENTS` (by default the ones `HANGAR_GITHUB_REPOS` maps). A GitHub-triggered job comments
"Queued as hangar job …" right away, so the wait shows on the issue. The build doesn't use up the job's
`HANGAR_JOB_TIMEOUT_MINUTES` (60), which limits each agent turn; waiting for the sandbox to start has a limit of its own,
`HANGAR_SANDBOX_START_TIMEOUT_SECONDS` (900), sized for a node scale-up and a first image pull.

**In `queued`.** The job's `reason` says why ([jobs.md](jobs.md#2-submit-a-job)):

- `powered_down`: the sandbox pools are off for the night; it starts after the 09:50 power-up
  ([gke.md](gke.md#6-sandbox-nodes-off-at-night-optional)).
- `awaiting_capacity`: its person already has 5 agent PRs awaiting review; reviewing or closing one starts it, or an
  admin runs `hangar job admit <id>` ([team-scale.md](team-scale.md#review-capacity)).
- `eval_window`, `eval_slots`, `eval_tpm`: an eval-class job (an experiment trial) waiting for the 20:00–00:15 window,
  a free eval slot or token budget ([team-scale.md](team-scale.md#the-eval-queue-class)).

**In `awaiting_approval`.** An untrusted sender's job, or one over a daily spend cap, waits for an admin or a repo
approver: `hangar job approve <id>` ([landing.md](landing.md)).

## More

- [The Cloud Doctor](doctor.md): the nightly pass that groups failures and opens fixes.
- [hangar-envd](envd.md#troubleshooting): when sandboxes fall back to exec.
- [Acceptance](acceptance.md#when-a-check-fails): when a check on the cluster fails.
- [Run capture](runbooks/outbox.md#when-writes-fail): when runs don't reach the bucket.
