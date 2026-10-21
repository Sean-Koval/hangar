# From a laptop to GKE and a team

hangar runs on a laptop first ([getting started](../guide.md)). This page moves it to GKE, then puts a team on it.

## Production (GKE)

```bash
cp deploy/terraform/gke/terraform.tfvars.example deploy/terraform/gke/terraform.tfvars   # set project_id, region
uv run hangar up --gke --dry-run          # the terraform plan; nothing changes
uv run hangar up --gke                    # asks, then applies that plan, runs deploy/gke/up.sh and logs in
hangar doctor --fix                       # what's missing, and the fixes it can apply
hangar setup github                       # token, webhook secret, repo webhook, settings, test delivery
hangar connect claude-code                # and any other MCP client
```

`hangar up --gke` never applies a plan that destroys or replaces anything unless you pass `--allow-destroy`. Without a
terminal it applies nothing unless you pass `--yes`. The steps it runs are the ones in [Running on GKE](../gke.md),
which you can also run by hand.

On a first install, `up.sh` points `HANGAR_SESSION_STORE` at the checkpoint bucket terraform made. If the certificate
isn't Active yet, `hangar up --gke` prints the `hangar login` command to run once it is.

Settings you change with `hangar setup` or `hangar config set` are stored in hangar's database, on its volume. They
survive redeploys and need no `kubectl`. The full walkthrough is in [Running on GKE](../gke.md).

## A team on one hangar

To put a team on hangar step by step (2 engineers on 1 repo, then 6 on 3), follow
[the onboarding runbook](../runbooks/onboarding.md); it ends with `hangar repo check` green for each repo. The
[runbooks](../runbooks/README.md) also cover the Friday harness review, run capture and governance break-glass.

An admin adds people; each gets a login token, connects their own clients and stores their own agent login:

```sh
hangar person add alice                       # a member; prints her login token once
hangar person add bob --role admin
hangar person ls                              # owner is the admin your existing tokens belong to
# alice, on her laptop:
hangar login https://hangar.example           # paste the token
hangar connect claude-code                    # an mcp token of her own
hangar cred add claude-alice --harness claude-code --kind api_key --file key.txt
```

Members see and act on their own sandboxes, sessions and jobs; admins on everyone's. Alice's sessions and jobs use her
own API key. Teammates without one of their own use a team API key (`hangar cred add NAME --team --kind api_key`).
Jobs and other automated work never use a subscription login ([subscription logins](../team/subscription-logins.md)).

A job that waits for approval is approved by an admin or a repo policy approver, from `hangar job approve` or a login
connection. Its own person and its requester never may ([the safe-release upgrade](../releases/safe-release.md)).

`hangar cred owner NAME PERSON|team` hands a credential to someone or back to the team, and `hangar person rm` revokes
someone's tokens. See [people and OAuth connectors](../security/people.md#people).
