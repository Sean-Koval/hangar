# Bots that act for people (relay)

A chat bot, such as the Symphony bot, can submit work for the person who asked in the room. The job is then theirs
(their visibility, caps, credentials and audit trail), not the bot's. The bot holds a **relay** token and names that
person on every MCP request:

```text
Authorization: Bearer hgr_<relay token>
X-Hangar-On-Behalf-Of: symphony:<Symphony user id>
# or: {"source": "symphony", "id": "<user id>", "reply_to": "symphony:<stream id>:<message id>"}
```

Set it up as an admin:

```bash
hangar person add symphony-bot --role admin --no-token   # the bot's person: only an admin's token can relay
hangar token create symphony-bot --scope relay --person symphony-bot   # or --scope relay_approve
hangar person link alice symphony <Symphony user id>                    # once per person
```

## What hangar trusts, and what it refuses

- **The bot vouches for the Symphony account, nothing more.** A claim names a `symphony` account only. GitHub, Slack,
  Jira and email accounts are verified by hangar's own doors, so a relay can't claim them.
- **Linked accounts only.** The request runs as the linked person with the `mcp` scope and nothing more. That is
  never admin, sandboxes, metrics or a login, whatever the bot's token or the person's other tokens hold. Records and
  audit entries show both identities, for example `@alice:relay:symphony-bot`.
- **Unlinked accounts are refused** (the default, `HANGAR_RELAY_UNLINKED=refuse`). With `approve`, an unlinked account
  may only submit work: a job or a [change set](../changesets.md).
- That work waits in `awaiting_approval` for a trusted person's approval, as an untrusted door's does
  (`hangar job approve`, `approve_job`). Its text counts as untrusted, so the work opens as draft PRs. The bot can't
  approve it: the held account may call nothing but `submit_job`.
- **Approvals.** `approve_job` and `reject_job` through the bot need a token minted with `--scope relay_approve`
  instead of `relay`. They then decide as the named person under the usual rule. That is an admin, a repo policy
  approver, or a front door's trust on a team job.
- Nobody approves their own work through a bot, even with `HANGAR_SELF_APPROVAL=allow_solo`.
- **A relay token does nothing on its own.** Without the header it is refused, it can't hold other scopes, and REST
  and the OAuth consent page don't accept it.

## Status

`/mcp` and `/mcp/sandbox` honour the header: every request runs through `auth.resolve_delegation`. A refused claim
answers 403 with the reason (audited as `relay.refused`). With `HANGAR_RELAY_UNLINKED=approve`, `submit_job` holds an
unlinked account's job for approval.

A claim's `reply_to` is kept on the job as the `relay.reply_to` label, where [events out](../plugins.md) find the
conversation. Per-call `_meta.on_behalf_of` isn't read; only the header is.
