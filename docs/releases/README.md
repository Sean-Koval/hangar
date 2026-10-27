# Release notes

What each release of hangar changed for a running install, newest first. Each says which defaults change as soon as
it starts, what you configure, the ordered steps and how to roll back. The procedure that is the same for every release,
and when to deploy, is on [upgrading.md](../upgrading.md). What each PR added is in
[CHANGELOG.md](https://github.com/Sean-Koval/hangar/blob/main/CHANGELOG.md).

| Release | Tag | Pages |
| --- | --- | --- |
| **Enforced intake and API keys**<br>KAN-74 (#223) | next, unreleased | [What breaks, the steps before you deploy and rollback](enforce.md) |
| **Adopt & capture and frontier**<br>#133–#189, with #140 and #177–#186 | v0.2.0 | [What it adds and what you configure](adopt-capture.md) · [the procedure and rollback](adopt-capture-procedure.md) |
| **The safe release**<br>#120–#132 | v0.2.0 | [What it adds, the procedure and rollback](safe-release.md) · [the `extends` merge rules](safe-release-merge.md) |
| **The team release**<br>#75–#117 | v0.2.0 | [What it adds](team-release.md) · [changed defaults](team-release-changes.md) · [steps 1–8](team-release-procedure.md) · [steps 9–11](team-release-sandboxes.md) · [steps 12–14 and rollback](team-release-operations.md) |
| **The overnight stack**<br>#48–#69 | none | [Back up and what happens to your data](overnight-stack.md) · [the defaults that change](overnight-stack-defaults.md) · [upgrade, prove it, roll back](overnight-stack-procedure.md) |

Releases stack. A hangar that runs the overnight stack does the team release, then the safe release, then adopt &
capture. It follows each page's steps in order, with `hangar doctor` at the end of each.

## Tags

v0.2.0 is the first tag. It carries the team release, the safe release and adopt & capture, so a hangar from before
it does all three procedures. Later tags each carry one release.

Pushing a `v*` tag runs two workflows. `release.yml` publishes the control-plane image and the harness packs to
GHCR, with a GitHub release that lists them by digest. `deploy.yml` builds the tagged commit and waits for the
owner's approval to deploy it ([deploying with deploy.yml](../upgrading.md#deploying-with-deployyml)).
