# Linked accounts and inbox delivery

Work reaches hangar from GitHub (issue labels, `/hangar` comments), Slack, and Jira intake. Each
sender is an account there, not a hangar person, until an admin links them. This page links them, says what a link
changes, and how the inbox reaches a person. The rest of the setup is on [team setup](../team.md).

```bash
hangar person link alice github alice-gh           # a GitHub login, stored as its numeric user id
hangar person link alice slack T0123ABCD/U012AB3CD # workspace/user id (profile → ⋯ → Copy member ID)
hangar person link alice jira 5b10ac8d82e05b22cc7d4ef5   # an Atlassian account id (Data Center: the user key)
hangar person link alice email alice@acme.com      # a Jira user known only by email
hangar person link owner github sean               # you, too: the owner exists without being added
hangar person unlink alice github alice-gh
```

## Ids that don't change hands

Links use ids that don't change hands. A GitHub login is looked up once, when you link it, with the control plane's
GitHub token (`https://api.github.com/users/<login>`). It is stored as the account's numeric id: a login can be
renamed, and whoever registers the old one is not your teammate. Without a GitHub token, link by the id itself.

A Slack user id is unique only within its workspace, so a link names both (`T…/U…`). In a Slack Connect channel the
poster's own workspace counts.

Only admins link, and hangar doesn't check that the account is the person's: you vouch for it. Link an account you've
seen the person use, for example by asking them to comment from it.

## What a link changes

A job from a linked account **runs for that person**:

- it uses their own login when they have one, else the team's
  ([whose login a job uses](../team.md#whose-login-a-job-uses));
- it counts against their caps (running jobs, sessions, sandboxes) and their daily budget
  (`HANGAR_PERSON_MAX_USD_PER_DAY`), not a separate bucket per account;
- it is theirs to see and act on over MCP (`list_jobs`, `get_job`, `follow_up_job`, `cancel_job`), as their own work
  is.

A job from an account linked to nobody **runs for the team**: team credentials, no person's caps, visible to admins
only. `hangar doctor` names the senders who start jobs often but are linked to nobody. Who may start and continue work
is on [who is trusted](who-is-trusted.md).

## Inbox delivery: DMs and the digest

What needs someone ([the inbox](../landing.md#7-the-inbox-what-needs-you)) reaches them in Slack through their linked
Slack account. They get a DM for each new agent question or approval, and a digest at 10:15 and 16:00 on weekdays.

Someone without a linked Slack account gets nothing, and `hangar doctor` lists them under "inbox not delivered". The
Slack app needs the `im:write` scope ([inbox DMs](../slack.md#inbox-dms)).

```bash
hangar person set alice --dm questions          # the default: her own agents' questions and approvals, at once
hangar person set alice --dm off                # no DMs and no digest (the inbox itself is unchanged)
hangar person set sean --dm all                 # an admin who also wants the team's (GitHub, Slack, webhook jobs)
hangar person set alice --tz Europe/Berlin --digest 09:30,16:00   # local times; --digest "" turns it off
hangar person set alice --digest-weekends       # the digest on Saturdays and Sundays too
hangar person set alice --quiet 20:00-08:00     # hold DMs in this window of her day; --quiet "" clears it
```

| Setting | Default | |
| --- | --- | --- |
| `--dm` | `questions` | `off`, `questions` (their own work), or `all` (admins: also the work nobody in particular owns, so not every admin is paged for every team question) |
| `--tz` | `America/New_York` | IANA time zone for the digest and the quiet window |
| `--digest` | `10:15,16:00` | Local digest times (up to 6); empty for none |
| `--digest-weekends` | off | Weekends are skipped unless set |
| `--quiet` | none | `HH:MM-HH:MM` in their time zone. DMs wait until it ends, except an approval that would expire first |

Anyone may change their own settings; admins anyone's (`PUT /api/people/{id}/delivery`).
