# Runbook: a bad deploy

For `restarts` and `api-down`, or any alert that starts right after a deploy ([alerting](../alerting.md)). The rollback procedure itself isn't repeated here. It lives with the deploy, in [CI/CD: when it fails, it rolls back](../cicd.md#4-when-it-fails-it-rolls-back).

## 1. Was it the deploy?

```bash
kubectl -n hangar rollout history deploy/hangar
kubectl -n hangar get pods -l app=hangar               # CrashLoopBackOff, restarts
kubectl -n hangar logs deploy/hangar --previous --tail=50
```

If the alert began within an hour of a new image, treat it as the deploy until shown otherwise. The `deploy.yml` run's summary names the image it replaced.

## 2. Roll back the image

`deploy.yml` rolls back by itself when a check fails during the deploy. When the trouble shows up later, roll back by hand:

- **The image only.** Re-run the deploy on the tag of the last good release, as [cicd.md](../cicd.md#4-when-it-fails-it-rolls-back) says. Deploys run only from `main` and `v*` tags.
- **A whole release.** Some releases change settings or the database too. Follow that release's own rollback section, linked from [upgrading](../upgrading.md). The team release's is [rolling back this release](../releases/team-release-operations.md#rolling-back-this-release).

A release that changed the database may need the backup taken before it. [Upgrading](../upgrading.md) has you take one before every deploy.

## 3. Check it's back

```bash
hangar doctor
hangar status
```

Then submit one small job, or wait for the next real one, and watch it start on the **hangar SLOs** dashboard. `restarts` closes once 15 minutes pass without three restarts. `api-down` closes when the uptime check passes again.

## 4. Afterwards

Open a KAN ticket for the bad change: what broke, and the check that would have caught it before the deploy. Tower has its own [rollback](https://sean-koval.github.io/tower/deploy/#roll-back) for a bad Tower deploy.
