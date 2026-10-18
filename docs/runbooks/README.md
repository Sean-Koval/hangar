# Runbooks

Step-by-step procedures for the people who run hangar for a team. The guides explain how each feature works; a runbook
says what to do, in order, and what to check after.

| Runbook | When | Who |
| --- | --- | --- |
| [Onboarding a team](onboarding.md) | Putting 2 engineers on 1 repo, then 6 on 3: people, credentials, the repo, its policy and doors, until `hangar repo check` is green | An admin, with a GitHub admin for the App and branch protection |
| [The Friday harness review](harness-review.md) | Every Friday, 30 minutes: labelling where failed and abandoned jobs first went wrong, until 20 candidate tasks reach flightdeck | The team, with an admin as facilitator |
| [Run capture: the outbox](outbox.md) | Switching run capture on, checking that a run landed, retention and deletion requests, and what to do when uploads fail | An admin, with the GCP project's owner for the bucket |
| [Governance break-glass](break-glass.md) | Tower (the Govern plane) is unreachable or denies what it shouldn't: what hangar does today, and the planned switch back from `enforce` | An admin, and a second admin to confirm |

Upgrading a running hangar has its own ordered procedures in [upgrading.md](../upgrading.md), and the nightly power
schedule and deploys are in [cicd.md](../cicd.md) and [gke.md](../gke.md). When something doesn't work, start with
[troubleshooting](../troubleshooting.md) and `hangar doctor` ([the Cloud Doctor](../doctor.md) is the nightly
version).
