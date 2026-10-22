# Will my environments change? The `extends` merge rules

How `extends` merges an environment with its parent since the safe release (#132), and how to fix one that resolves
differently. The rest of the release is on [the safe release](safe-release.md).

## What changed

`extends` used to append every list except `harnesses`, `credentials`, `terminals` and `ports`. A child could never
drop a parent's skill. Now:

- component lists (`skills`, and every list not named below) replace the parent's;
- the additive build inputs (`tools.apt`, `dependencies.pip` and `dependencies.npm`) union;
- maps still merge by key, and `null` now deletes a key;
- `skills_remove` drops inherited skills, in environments and in experiment variants.

So a child's `skills: [x]` now means only `x`. The details are in
[how `extends` merges](../environments.md#how-extends-merges).

## Find the environments it changes

Before you upgrade, run the dry run against the checkout you'll deploy and your data volume. It reads the shipped
specs and the saved ones in `<data>/envs`, and needs no running hangar:

```bash
uv run hangar env migrate-merge --dry-run --data-dir /path/to/hangar-data   # or with HANGAR_DATA_DIR set
```

It names each environment that resolves differently: skills lost or gained, and other fields that change. No shipped
environment changes.

## Fix each one

For each environment it names, restate the parent's entries in the child's list, or switch to `skills_remove`. An
environment whose resolved spec changes gets a new image tag. Prebuild it with `hangar images plan` and
`hangar images build` before the rollout ([step 4](safe-release.md#4-prebuild-images)).
