# Release notes

What each release of hangar changed for a running install, newest first. Each says which defaults change as soon as
it starts, what you configure, the ordered steps and how to roll back. The procedure that is the same for every release,
and when to deploy, is on [upgrading.md](../upgrading.md). What each PR added is in
[CHANGELOG.md](https://github.com/Sean-Koval/hangar/blob/main/CHANGELOG.md).

<div class="hg-stack" markdown>

| Release | Tag | Pages |
| --- | --- | --- |
| **Enforced intake and API keys**<br>KAN-74 (#223) | v1.0.0-rc.1, not tagged yet | [What breaks, the steps before you deploy and rollback](enforce.md) |
| **Adopt & capture and frontier**<br>#133–#189, with #140 and #177–#186 | v0.2.0 | [What it adds and what you configure](adopt-capture.md) · [the procedure and rollback](adopt-capture-procedure.md) |
| **The safe release**<br>#120–#132 | v0.2.0 | [What it adds, the procedure and rollback](safe-release.md) · [the `extends` merge rules](safe-release-merge.md) |
| **The team release**<br>#75–#117 | v0.2.0 | [What it adds](team-release.md) · [changed defaults](team-release-changes.md) · [steps 1–8](team-release-procedure.md) · [steps 9–11](team-release-sandboxes.md) · [steps 12–14 and rollback](team-release-operations.md) |
| **The overnight stack**<br>#48–#69 | none | [Back up and what happens to your data](overnight-stack.md) · [the defaults that change](overnight-stack-defaults.md) · [upgrade, prove it, roll back](overnight-stack-procedure.md) |

</div>

Releases stack. A hangar that runs the overnight stack does the team release, then the safe release, then adopt &
capture. It follows each page's steps in order, with `hangar doctor` at the end of each.

## Tags

v0.2.0 is the first tag. It carries the team release, the safe release and adopt & capture, so a hangar from before
it does all three procedures. Later tags each carry one release.

Pushing a `v*` tag runs two workflows. `release.yml` publishes the control-plane image and the harness packs to
GHCR, with a GitHub release that lists them by digest. `deploy.yml` builds the tagged commit and waits for the
owner's approval to deploy it ([deploying with deploy.yml](../upgrading.md#deploying-with-deployyml)).

## Making a tag

The suite's release checklist is the same for hangar, Tower and flightdeck. For hangar:

1. Move the Unreleased entries in `CHANGELOG.md` under `## vX.Y.Z`, and set `version` in `pyproject.toml` to match.
   For a pre-release, spell both the way the tag does: tag `v1.0.0-rc.1` needs version `1.0.0-rc.1`, not `1.0.0rc1`.
2. Merge that to main, and wait for `ci.yml` to pass on the merge commit. If it didn't run there, run it by hand
   on main.
3. Run `python3 scripts/release_check.py vX.Y.Z` there. It prints the notes, or what is wrong.
4. Tag the merge commit and push the tag: `git tag -a vX.Y.Z -m vX.Y.Z <sha> && git push origin vX.Y.Z`.

`release.yml` checks it all again before it builds anything. The tag must be on main, CI must have passed on its
commit, and the version and changelog must match. The [upgrade drill](../runbooks/drills.md) from the previous tag
must pass too, with the restore drill.

The release notes carry the changelog section above the image digests. After the deploy, check that the
running image's tag is the one you pushed.

### What makes this binding

The checks run only in `release.yml` as it is at the tag's commit. A tag on a commit from before this checklist
has no `check` job, and would still publish images. A manual run from another branch runs that branch's workflow.
So the gate needs one setting no file in the repo can make:

- **A tag ruleset on `v*`.** It blocks creating, updating and deleting `v*` tags. Its sole bypass is a deploy key
  kept on the owner's machine; no role, user or app may bypass it.

hangar's own sandboxes can't push tags anyway: the [git gateway](../security/git-gateway.md) refuses them unless
`HANGAR_GATEWAY_ALLOW_TAGS` is set. Local Claude Code sessions push as the owner's account, though. The ruleset is
what keeps an agent from cutting a release.

A manual run of `release.yml` must start from main. It may publish an old tag again only if that tag already has a
GitHub release.
