# The review packet and the inbox

This page is part of [Landing: an approval lands the PR](../landing.md).

## 6. The review packet

hangar writes one comment on each agent PR when it delivers, and edits it on every later push. Built from the job record, it works for any repo without a CI job:

- **Verification**: the command and its result, "base was red" when the [check on the base](../repo-policy/readiness.md#the-check-on-the-base-commit) failed, and whether the agent changed the check's own tests;
- **Risk**: `high` when a path matches `.github/**`, `.hangar/**` (the repo's environment, which runs in every build and sandbox) or the policy's `risk_paths`, or when the PR removes or changes a line of an existing ADR's `checks:` block ([on how a PR lands](how-a-pr-lands.md#agent-drafted-adrs)), `low` for docs and tests only, `normal` otherwise;
- **Files** touched, with the diffstat;
- **Cost**: tokens and dollars so far, the job budget, and the agent turns;
- **Agent**: harness and the version its pack installed, model, environment, and the harness it ran with: the short
  `harness_digest` (`sha256:abcd1234…`, linking to `/api/jobs/{id}/manifest`) and the lock it was thawed from, if any
  (each distinct digest with its turns when they differed);
- **Session**: its name, and how to pull or continue it (`/hangar <message>`);
- **Recorded demos**: links to the video and GIF artifacts it handed back;
- **AI review**: the verdict, who reviewed (harness and model, or the bot's check), the head it reviewed, the confirmed findings, the unconfirmed ones dropped, and a link; "pending" until it is in (a PR delivered during a power-down is reviewed after power-up);
- **Landing rule**: who may land it, and why (a second reviewer, or the requester alone for a low-risk PR with an independent AI review; a tainted PR always needs someone other than its person);
- **Landing**: who can land it and how.

Ticket jobs also ask the agent for `evidence` and `start_here`:

| Line | What it shows |
| --- | --- |
| **Acceptance criteria** | One row per criterion of the ticket, with the evidence the agent cites: tests, a command or an artifact. A cited test counts only when it passed in hangar's own run of the check; otherwise the row says "unproven". A command or artifact alone is "claimed". All of it shows as inline code, never as Markdown. |
| **Start here** | At most 3 files the agent names, checked against the diff. The Files line then lists the rest, tests last. |
| **Size** | For any job: a warning when the changed lines pass the repo policy's `diff_budget_lines`. |

## 6a. AI first-pass review

Each non-draft agent PR on a repo with `ai_review` set gets an advisory first-pass review before a person reads it. A hangar job on another harness reviews it, or an org review bot's check decides. A second, cheaper pass keeps only the findings it can confirm. The App posts them as one COMMENT review with at most 5 inline comments, most severe first. [AI first-pass review](../ai-review.md) covers the reviewer, the two passes, the verdict and the spend.

## 7. The inbox: what needs you

One list per person, newest first, from the records hangar keeps:

| Kind | When | What to do |
| --- | --- | --- |
| `review` | your agent's PR is open and its job is done; or it is approved but can't land (`land: manual`, a draft, a failed check) | review and approve it, or merge it |
| `review` | a PR whose requester's own approval doesn't land it ([self-approval](how-a-pr-lands.md#self-approval-who-may-be-the-only-approver)): for the people linked to the repo policy's approvers (admins when none is linked) | review it as the second reviewer |
| `question` | a job in `needs_input`, or a session's open question | answer with a follow-up or `send_to_session` |
| `approval` | a job waiting for approval (an untrusted trigger, a spend cap); a turn waiting on a tool approval; requests other modules add (egress, secrets) | approve or deny |
| `failed` | a job that failed in the last 7 days and has no open PR | read why, then follow up or resubmit |
| `denied` | policy denied a step of a turn that is still running (the agent carried on without it) | steer the session, or let it be |
| `abandon` | your agent's PR was closed without merging in the last 7 days and has no [abandon reason](how-a-pr-lands.md#what-hangar-records) | comment `/hangar abandon <reason>` on it ([metrics.md](../metrics.md#what-hangar-records-for-them)) |
| `credential` | a credential failed its check or expires soon: yours, or for admins the team's ([observability.md](../observability.md)) | renew it, then `hangar cred check NAME` |
| `report` | admins only: a [Cloud Doctor](../doctor.md) finding it didn't fix | `hangar admin doctor show ID`, or `dismiss ID` |
| `alert` | admins only: a front door saw use that a real sender wouldn't make, such as a Jira Automation token used for an issue nobody just moved ([intake.md](../intake/jira.md#fallback-an-automation-rule)); kept 7 days | check the rule, rotate the token if it doesn't explain it |

It is per person, like everything since [people](../security/people.md#people): a member sees their own jobs and sessions. An admin sees their own, plus work nobody in particular owns (jobs from GitHub issues, Slack and inbound webhooks). Read it:

- MCP: the resource `hangar://inbox` (not a tool, so it adds nothing to the tool list). Subscribe to it to hear when it changes; it changes with any job or session.
- CLI: `hangar inbox` (`--json` for the raw list).
- REST: `GET /api/inbox` with an admin token or a person's `user` token: `{"person", "counts": {"review": 2, …}, "items": [{"id", "kind", "reason", "action", "since", "job", "pr", "links", …}]}`.

Each item carries a stable `id` (reading the inbox again gives the same id) and `links` to the native object where you act on it: `{"pr", "ticket", "thread", "job"}`. A `failed`, `denied`, `review`, `alert` or `abandon` item can be put away: `POST /api/inbox/{id}/ack` (per person; it leaves your list for 7 days, the failed window; `DELETE` brings it back). An `approval`, `question`, `credential` or `report` can't: it clears when decided, renewed or dismissed. `ticket` is the Jira issue (`<HANGAR_JIRA url>/browse/ABC-12`) or the GitHub issue the job came from; `thread` the Slack thread it talks in (Slack's own permalink once hangar has fetched it with `chat.getPermalink`); `job` the job on the dashboard (`<HANGAR_PUBLIC_URL>/#jobs?job=<id>`).

### Delivery: it comes to you

Nobody runs `hangar inbox` to find out an agent is blocked on them, so hangar sends it in Slack to each person with a linked Slack account ([team.md](../team/linked-accounts.md#inbox-delivery-dms-and-the-digest)):

- **At once: questions and approvals.** A new `question` or `approval` item goes to its person in a DM within a minute: the reason, "Answer here: <link>" (the Slack thread, the Jira issue, the PR, else the job), and the CLI fallback. There are no buttons: the answer is given where it's recorded with your identity. A burst (five approvals in ten seconds) is one DM, and an item is sent once (hangar remembers it for 7 days).
- **Twice a day: the digest**, at 10:15 and 16:00 in your time zone on weekdays: questions oldest first with their age, approvals with their expiry, jobs that failed since the last digest, approved PRs that aren't landing and why, PRs closed without a reason; then one line, "N agent PRs await your review", linking GitHub's review-request queue (reviews are never listed one by one: GitHub stays the review queue), and a footer with your open agent PRs (against the review-capacity cap, when set) and jobs held for it. At most 20 items, then "and N more: hangar inbox". An empty digest isn't sent. It runs on the control plane, which stays up while the sandbox pools are powered down (01:00-10:00), so the 10:15 digest goes out even when power-up is late; it then says "sandboxes not up yet". A restart never sends a slot twice; a slot missed while hangar was down goes out on recovery if it is under 2 hours late.
- **Not sent:** `failed` items wait for the digest, and `review` items stay in GitHub. `denied`, `alert`, `credential` and `report` items stay in the inbox. Work nobody in particular owns (GitHub, Slack and webhook jobs) goes only to admins who chose `--dm all`.

The digest's review link needs your GitHub login: hangar looks it up from your linked GitHub user id (`GET /user/{id}`, cached), and with `HANGAR_GITHUB_APP_SLUG` set narrows it to the App's PRs (`author:app/<slug>`). Without a GitHub token the line has no link.

Other modules add kinds of request through `inbox.SOURCES`: a function from the job service and the caller to items, which decides what that caller may see.
