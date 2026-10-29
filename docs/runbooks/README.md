# Runbooks

Step-by-step procedures for the people who run hangar for a team. The guides explain how each feature works; a runbook
says what to do, in order, and what to check after.

| Runbook | When | Who |
| --- | --- | --- |
| [Onboarding a team](onboarding.md) | Putting 2 engineers on 1 repo, then 6 on 3: people, credentials, the repo, its policy and doors, until `hangar repo check` is green | An admin, with a GitHub admin for the App and branch protection |
| [The Friday harness review](harness-review.md) | Every Friday, 30 minutes: labelling where failed and abandoned jobs first went wrong, until 20 candidate tasks reach flightdeck | The team, with an admin as facilitator |
| [Run capture: the outbox](outbox.md) | Switching run capture on, checking that a run landed, retention and deletion requests, and what to do when uploads fail | An admin, with the GCP project's owner for the bucket |
| [Tower beside hangar on GKE](tower.md) | Pointing hangar at Tower in the cluster, in shadow mode, and what the power schedule does to it | An admin |
| [On call](on-call.md) | Who answers hangar's and Tower's alerts, how fast, and the escalation path when nobody does | Sean, the on-call owner |
| [A stuck job](stuck-job.md) | A job looks stuck, starts keep failing or the queue is slow (`jobs-stuck`, `job-starts-failing`, `queue-slow`) | The on-call owner |
| [A credential leak](credential-leak.md) | A secret may be somewhere it shouldn't be: revoke it, find where it went, close the path | Whoever finds it, then an admin |
| [Tower down](tower-down.md) | Tower isn't answering, so its hook and hangar fail closed (`tower-down`, `tower-fail-closed`) | The on-call owner |
| [A full disk or quota](disk-and-quota.md) | hangar's data disk, a node's disk or a GCP quota is full (`data-disk`, `node-disk`, `sandbox-creates-failing`) | The on-call owner |
| [A deploy waiting too long](deploy.md) | Production is behind main and a deploy waits for approval (`deploy-lag`): who approves, and what to do with a stale run | The on-call owner, or the second approver |
| [A bad deploy](bad-deploy.md) | The control plane crash-loops or stops answering after a deploy (`restarts`, `api-down`); links the rollback | The on-call owner |
| [Governance break-glass](break-glass.md) | Tower (the Govern plane) is unreachable or denies what it shouldn't: what hangar does today, and the planned switch back from `enforce` | An admin, and a second admin to confirm |

The incident runbooks follow from hangar's [SLOs](../slos.md) and [alerts](../alerting.md). Upgrading a running hangar has its own ordered procedures in [upgrading.md](../upgrading.md), and the nightly power
schedule and deploys are in [cicd.md](../cicd.md) and [gke.md](../gke.md). When something doesn't work, start with
[troubleshooting](../troubleshooting.md) and `hangar doctor` ([the Cloud Doctor](../doctor.md) is the nightly
version).
