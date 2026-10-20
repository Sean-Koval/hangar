# Release notes

What each release of hangar changed for a running install, newest first: the defaults that change as soon as it
starts, what you configure, the ordered steps and how to roll back. The procedure that is the same for every release,
and when to deploy, is on [upgrading.md](../upgrading.md). What each PR added is in
[CHANGELOG.md](https://github.com/Sean-Koval/hangar/blob/main/CHANGELOG.md). Nothing is tagged yet (release tags are
issue #274), so a release is a range of pull requests.

| Release | Pages |
| --- | --- |
| **Adopt & capture and frontier**<br>#133–#189, with #140 and #177–#186 | [What it adds and what you configure](adopt-capture.md) · [the procedure and rollback](adopt-capture-procedure.md) |
| **The safe release**<br>#120–#132 | [What it adds, the procedure and the `extends` merge rules](safe-release.md) |
| **The team release**<br>#75–#117 | [What it adds and what you configure](team-release.md) · [what changes as soon as it starts](team-release-changes.md) · [steps 1–8](team-release-procedure.md) · [steps 9–11](team-release-sandboxes.md) · [steps 12–14 and rolling back](team-release-operations.md) |
| **The overnight stack**<br>#48–#69 | [Back up and what happens to your data](overnight-stack.md) · [the defaults that change](overnight-stack-defaults.md) · [upgrade, prove it, roll back](overnight-stack-procedure.md) |

Releases stack. A hangar that runs the overnight stack does the team release, then the safe release, then adopt &
capture, each page's steps in order, with `hangar doctor` at the end of each.
