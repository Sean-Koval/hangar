# A deploy waiting too long

What to do when production falls behind main: the `deploy-lag` alert fired, or `hangar doctor` says production is days behind. A deploy only happens once someone approves its run on the `production` environment ([cicd.md](../cicd.md#2-start-the-deploy-then-approve-it)), so the fix is an approval.

## Who approves

| Approver | Role |
| --- | --- |
| Sean | The owner and first approver |
| `<second approver>` | Approves when Sean is away for a working day or more |

Both are required reviewers on the `production` environment. Add the second approver there: *Settings → Environments → production → Required reviewers*. Either one's approval releases a deploy. Write the name in the table above when it is chosen.

## How far behind is production

`hangar doctor` has a `production lag` line. It names the deployed commit and its tag, and how many commits and days it is behind `main`. The console shows the same in its top bar. The number turns orange past the threshold, two working days unless `HANGAR_DEPLOY_LAG_ALERT_WORKING_DAYS` says otherwise.

The days count from the oldest commit on main that production lacks. Working days are Monday to Friday, in the power schedule's time zone. hangar asks GitHub every 15 minutes with its own token. Without one, or offline, the lag reads "unknown".

## The alert

`deploy-lag` is a ticket: deal with it today. It fires only while the infra is up. The night's power-down never fires it, nor does a hold. deploy.yml skips merges then by design, and the next power-up starts a deploy for main ([cicd.md](../cicd.md)).

## When the waiting run is stale

deploy.yml keeps one run going and one waiting ([cicd.md](../cicd.md)). A run that waits for days was built from an old commit. Approving it ships nothing: it sees main has moved on and skips. Reject it, then approve the newest run.

Find the run waiting for approval, and the environment's id:

```bash
REPO=Sean-Koval/hangar
gh run list --repo $REPO --workflow deploy.yml --status waiting
ENV_ID=$(gh api repos/$REPO/environments/production --jq .id)
```

Reject the stale run (its id is the first column above):

```bash
RUN=39090659248
gh api -X POST repos/$REPO/actions/runs/$RUN/pending_deployments \
  -F "environment_ids[]=$ENV_ID" -f state=rejected -f comment="stale: main has moved on"
```

Start a run for main's head if none is queued, and wait for it to reach the approval:

```bash
gh workflow run deploy.yml --repo $REPO --ref main
gh run list --repo $REPO --workflow deploy.yml --limit 3
```

Approve the newest run once it waits on `production`:

```bash
NEW=<the newest run's id>
gh api -X POST repos/$REPO/actions/runs/$NEW/pending_deployments \
  -F "environment_ids[]=$ENV_ID" -f state=approved -f comment="deploying main's head"
gh run watch --repo $REPO $NEW
```

## Check after

- The run finishes green, and its summary shows the new server version.
- `hangar doctor` shows the new commit in `production lag`, with nothing to deploy. hangar looks again as soon as the deploy is recorded.
- The alert closes by itself 10 minutes after the last line.

If the deploy fails and rolls back, follow [a bad deploy](bad-deploy.md).
