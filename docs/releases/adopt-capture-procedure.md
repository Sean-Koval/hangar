# Adopt & capture and frontier: the procedure

The ordered steps for [adopt & capture and frontier](adopt-capture.md) (#133–#189), ending with `hangar doctor`, and how to roll back. The general procedure is on [upgrading.md](../upgrading.md).

## 1. Back up and record the image

```bash
kubectl -n hangar get deploy hangar -o jsonpath='{.spec.template.spec.containers[0].image}' | tee ~/.hangar/pre-upgrade-image
hangar admin backup && hangar admin backups
```

The image is what [rolling back](#rolling-back-adopt-capture-and-frontier) puts back.

## 2. Terraform: the run bucket

`deploy/terraform/gke/outbox.tf` is new: the `<project>-hangar-runs` bucket (`enable_run_bucket`, on by default),
`roles/storage.objectCreator` on it for hangar's service account, a `hangar-write-once=runs` tag, and an IAM deny
policy that takes `objects.delete` and `objects.update` away from that account ([gke.md](../gke.md#run-capture-bucket)).
Applying the deny policy needs `roles/iam.denyAdmin` on the project. The bucket does nothing until step 9 points hangar
at it; set `enable_run_bucket = false` to leave it out.

```bash
cd deploy/terraform/gke
terraform plan    # expect additions only: the bucket, its IAM member, the tag key, value and binding, the deny policy
terraform apply
```

Leave `enable_overnight_lane` at `false` for now (step 9). If the plan changes a node pool, something else in your
tfvars moved: apply that in the power-down window.

## 3. Prebuild the repo-conventions images

The `repo-conventions` skill changed (#143), so the images that bake it in get new tags: `claude-python`,
`claude-python-kata`, `codex-node`, `example-repo`, `hangar`, `hangar-jobs` and `polyglot` among the shipped ones, and
any environment of yours that lists the skill or extends one of them. Nothing else in this release changes an image
tag. Build them before anyone waits on a first sandbox:

```bash
uv run hangar images plan                           # what will build
uv run hangar images build --registry "$REG"        # or let images.yml build them on the merge
```

## 4. The Slack app: `im:write` and interactivity

DMs need the bot scope `im:write`, and the steer buttons need *Interactivity* with the request URL
`<public_url>/api/hooks/slack/interactions`. Both are in [the manifest](../slack-app-manifest.yaml). Write it with your URL
filled in, paste it into the app's *App Manifest* page at <https://api.slack.com/apps>, and reinstall the app to the
workspace so the token gets the new scope:

```bash
hangar setup slack --manifest slack-app.yaml        # Enter at each secret prompt keeps the stored one
```

Without the scope `hangar doctor` warns (`slack dm scope`), and DMs fail while threads keep working.

## 5. Deploy with deploy.yml, by day

Between 10:00 and 01:00 ET, while the pools are on ([when to deploy](../upgrading.md#when-to-deploy)):

```bash
gh workflow run deploy.yml --ref main
```

Approve it on `production`. It builds the images it needs, rolls out, and runs the core acceptance checks, `mcp`
among them, then the team checks ([cicd.md](../cicd.md)). On Docker: `uv run hangar down`, update the checkout,
`uv run hangar up`.

On its first start hangar counts each person's open agent PRs, so the review cap (step 7) applies from then, and
creates the tables it adds (the outbox's queue, pin sets, the trial cache, model grants). Inbox delivery starts with
it; run capture, the standup line and the harness repo stay idle until step 9 sets them.

## 6. Re-add MCP clients

```bash
hangar connect claude-code      # writes hangar (/mcp) and hangar-sandbox (/mcp/sandbox); --no-sandbox leaves it out
hangar connect codex            # the same; other clients get hangar only unless --with-sandbox
```

In claude.ai, ChatGPT or Claude Desktop, which never refresh a tool list, remove the hangar connector and add it again
(`<public_url>/mcp`), then add `<public_url>/mcp/sandbox` as a second connector if you drive sandboxes yourself
([clients.md](../clients.md)). Until then a call of a drive tool on `/mcp` still works, with a note an hour saying where it
moved. The Ingress sends every path to hangar, so `/mcp/sandbox` needs no change there.

## 7. The review rules, per repo

Decide for each landing repo, right after the rollout (the previous version's CLI doesn't know these flags):

```bash
hangar repo set acme/app --ai-review hangar                 # an independent AI review of every agent PR
hangar repo set acme/app --self-approval any                # or: the requester's approval lands any PR, as before
hangar repo set acme/app --max-open-agent-prs 8             # this repo's cap (0 turns it off)
hangar job list --reason awaiting_capacity                  # who is waiting on review now
```

With the default `self_approval` and `ai_review` off, a person's own approval never lands their PR: a second
reviewer does. A team of one keeps `HANGAR_SELF_APPROVAL=allow_solo`. `--ai-review hangar` needs a credential for
the reviewing harness (an API key: [team.md](../team.md#3-team-credentials)).

## 8. People and the inbox

DMs go to a person's linked Slack account only ([team.md](../team/linked-accounts.md#inbox-delivery-dms-and-the-digest)):

```bash
hangar person link alice slack T0123ABCD/U012AB3CD
hangar person set alice --tz Europe/Berlin --digest 09:30,16:00 --quiet 19:00-08:00
hangar person set bob --dm off                              # no DMs, no digest
```

`hangar doctor` lists everyone the inbox can't reach (`inbox delivery`).

## 9. Turn on what you want

Each is off until set; turn them on one at a time.

- **Run capture** (#163, #189): on GKE, `deploy/gke/up.sh` sets `HANGAR_OUTBOX_URL` from terraform's `run_bucket`
  and restarts hangar when it changes (#273); add flightdeck's compactor to `run_bucket_readers`. Step 5's `deploy.yml`
  only changes the image, so run `up.sh` once for it, or set the URL by hand and restart. Operating it:
  [the outbox runbook](../runbooks/outbox.md).
- **The Jira door** (#156): `hangar setup jira` ([intake.md](../intake.md#3-the-configuration)); then
  `hangar repo set acme/app --verify-allowed 'make test*'` if tickets may name their check.
- **The team harness repo** (#138): `hangar config set HANGAR_HARNESS_REPO https://github.com/acme/harnesses.git`, add
  the `push` event to that repo's webhook, and `HANGAR_DEFAULT_HARNESS=team-default@stable` once a lock is promoted
  ([harness-lock.md](../harness-lock.md#the-team-harness-repo)).
- **The standup line** (#168): `hangar config set HANGAR_STANDUP_CHANNEL C0123ABCD` ([metrics.md](../metrics.md#the-standup-line)).
- **Shadows and canaries** (#183, #184): repo policy `shadow`, refused while the review-wait p50 is over 4 h
  ([repo-policy.md](../repo-policy.md#shadows)).
- **Record and replay** (#177, #179): `model_gateway: record` on an environment ([model-gateway.md](../model-gateway.md));
  the nightly determinism report then replays its recorded runs.
- **The overnight lane** (#186): `enable_overnight_lane = true` and `terraform apply`, then `hangar power overnight on`,
  refused below 60% agent PR yield ([gke.md](../gke.md#the-overnight-lane)).

## 10. `hangar doctor`

```bash
hangar doctor
```

All green means: both MCP servers are served and list their tools (`mcp hangar`, `mcp hangar-sandbox`), the Slack
token has `im:write`, everyone who gets DMs has a linked Slack account, and, where you turned them on, the outbox is
draining (`run capture (outbox)`) and the harness repo is readable (`harness repo`). Then check one job end to end:
a ticket moved to *Ready for agent*, its PR body ending in trailers, and a DM for its first question.

## Rolling back adopt & capture and frontier

Put the image from step 1 back:

```bash
kubectl -n hangar set image deploy/hangar hangar="$(cat ~/.hangar/pre-upgrade-image)"
kubectl -n hangar rollout status deploy/hangar
```

- **Records an earlier version can't read.** Jobs with `source: review` (AI reviews, #161) or `source: shadow` (#183),
  and trial turns with `infra_reason: drift` (#178), fail validation in the safe release's models, so any read that
  reaches them fails there. Roll back before turning on AI review, shadows or pin-set trials, or plan to roll forward.
- **MCP**: the earlier version serves no `/mcp/sandbox`, so `hangar-sandbox` connectors stop answering; `/mcp` lists
  every tool again.
- **What stays**: the bucket and its objects (write-once), the new tables (the earlier version doesn't read them),
  repo policy fields and people's delivery settings, which it ignores. Digests it records for a turn won't match the
  ones this version recorded for the same configuration.

