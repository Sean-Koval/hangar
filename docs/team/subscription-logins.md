# Subscription logins

Why jobs refuse a Claude Pro or Max login, a ChatGPT sign-in and the other subscription logins. Then where a person
can still use one, and how one person's sandboxes share a sign-in file. Team credentials are on
[team setup](../team.md#3-team-credentials).

A subscription login is a credential stored with `--kind oauth_token` or `--kind auth_file`. That is
`claude setup-token` (Claude Pro or Max), a ChatGPT `~/.codex/auth.json`, a Google sign-in for Antigravity, and pi's
and Gemini's sign-in files.

The vendors' terms reserve these for one person's own use, and don't allow automated or headless use
([what the vendors allow](vendor-terms.md)). Anthropic has enforced this against third-party harnesses since April
2026. So hangar uses them only interactively.

## Where they are refused, and where they warn

- **Jobs, schedules, webhooks, experiments and replays refuse them.** An automated resolution leaves subscription
  logins out of the pool. A person with only `claude setup-token` gets this before any sandbox starts, on the ticket
  or thread that started the job:

  ```
  job needs an API-key credential for claude-code; subscription logins are interactive-only (docs/team.md#subscription-logins). add an Anthropic API key: hangar cred add claude-api --harness claude-code --kind api_key --file anthropic-key.txt (your own; an admin adds --team for work no person owns)
  ```

  A job that names a subscription login in `credentials` is refused the same way. Their person's API key, or the
  team's, is used instead when there is one.
- **Interactive sessions and sandboxes warn.** A person's own `new_session`, `create_sandbox` or `run_agent` still
  uses their subscription login, and says so. It adds a warning event, `warnings` on `get_session` and the sandbox,
  and an entry in each turn's manifest `warnings[]`:
  `subscription login in a hangar sandbox: personal, interactive use only`.
- **No setting relaxes this.** `HANGAR_SUBSCRIPTION_LOGINS=any` let jobs use them in v0.2.0 only. It was removed
  after it: hangar refuses to start with it ([removed switches](../upgrading.md#removed-switches)).
- **Spend caps.** Put a workspace spend limit on each API key at the vendor (Anthropic Console workspace limits,
  OpenAI project limits); hangar can't set those. hangar's job budgets and daily caps ([jobs](../jobs/README.md)) are
  the cap inside hangar. Use both.
- **Codex Enterprise access tokens** ("for trusted scripts, schedulers, and private CI runners") are for automation.
  Store one with `--kind api_key` and hangar treats it as an API key.
- **Antigravity**: a Google sign-in is an `auth_file`, so fleets and jobs use a `GEMINI_API_KEY` credential
  (`--kind api_key`).
- The one-at-a-time lease for sign-in files ([below](#codex-logins-one-sandbox-at-a-time)) still applies to
  interactive sessions.

Credential resolution happens when a job's sandbox is claimed. A submit also runs the same resolution as a dry run. So
an obviously missing API key fails the submit at once, even while the pools are down.

## Codex logins: one sandbox at a time

A Codex ChatGPT login (`~/.codex/auth.json`) rotates its refresh token as it's used. So hangar leases it to one sandbox
at a time and writes the new token back when the sandbox ends. The same holds for the other sign-in files (pi,
Gemini). One such login serves one sandbox at once, across everyone who uses it.

A sign-in file is always someone's own, and only interactive sessions and sandboxes use one. So this is about one
person's sessions. When a person has several logins of a kind,
they form a pool and a sandbox takes a free one.

When all are in use, a job or session **waits** and says so in its events:
`waiting for a free login (queued): alice's codex logins codex-1, codex-2 (all in use)`. It doesn't name the
sandboxes holding them, which may be other people's. It starts when one is released, or fails after
`HANGAR_LOGIN_WAIT_SECONDS` (600) with the same words. A sandbox made directly (`create_sandbox`) fails at once
instead.

A job's sandbox keeps its login while it is kept for follow-ups (`keep_minutes`, 120 by default). So a login stays busy
for up to that long after each job. `keep_minutes: 0` releases it when the turn ends; follow-ups still resume from the
checkpoint. To run many Codex jobs in parallel, use an OpenAI API key, which has no lease.

### When a Codex login stops working

Each refresh rotates the refresh token. So store a login nothing else uses: a copy of your laptop's
`~/.codex/auth.json` dies when Codex on the laptop refreshes it.

A turn whose refresh OpenAI refuses fails as `infra_error` / `login_rejected`. The job names the credential, and its
health stays failed until it is stored again. Sign in again from the console's Credentials page, under the same
name. Storing it ends any lease on the old login, so the old file is never written back.
