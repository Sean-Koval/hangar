# Build secrets and limits

What a [build](builds.md) may hold while `install` runs, what builds can't do yet, and how they keep package caches.

## Build secrets

A private package registry needs a token while `install` runs, and only then. The repo names the ids it needs
(`build_secrets: [NPM_TOKEN]`). An admin maps each id to a hangar credential, in the environment's or the repo
policy's `build_secrets` (in the same release, with hangar.buildsecrets).

The values reach the install command's environment only, through a file that is read and removed before `install`
starts, never a command line. The output is redacted. An id nobody mapped fails the build with its name. The running
sandbox never sees any of them.

The checkout is scanned for the values before it is kept: in the sandbox, and again by hangar in the archive it
captured. That archive is what the image gets, whatever install left running. A build whose install wrote a token
into the repo (a repo-local `.npmrc`) fails and says where.

The scan catches a value written as it is, not one install encodes on purpose. `install` is the repo's code, so map a
build secret only for a repo whose default branch you'd trust with it.

## Limits of this version

- Docker and Kubernetes run builds. On E2B a repo's sandboxes start from the environment's image, with the warning.
- A sandbox started from a build starts cold: the environment's warm pool holds its own image. On Kubernetes each
  build has its own SandboxTemplate, with no warm pool.
- The reaper deletes a build's SandboxTemplate once the build is out of the history and no live sandbox runs on it.
  A session started from it before a rollback keeps it. The reaper does the same for a set of services' template that
  nothing uses any more (`k8sgc.py`).
- A build's `.git` keeps only what hangar and git need: its remotes, branches and core settings. Hooks (but their
  samples) and the rest of its config are dropped before the checkout is captured. That includes `core.hooksPath`,
  `core.fsmonitor`, filters, aliases and includes.
- Otherwise every sandbox from the build would run what `install` set up, hangar's own checkout first. It would run
  long after the commit that set it up was gone. A repo that wants git hooks in every sandbox sets them up in `start` (husky:
  `git config core.hooksPath .husky`).
- On providers that bake harnesses into images (no image volumes), builds are made for the environment's default
  harness. A sandbox for another harness starts from that harness's image variant, with the warning.
- Builds are made from the default branch, as Cursor's are. A feature branch's new dependencies are the agent's to
  install, and it is told so.

## Build caches

A build keeps the checkout, not `$HOME`. The package managers' caches, a branch whose lockfile moved on, and compile
output are covered by [build caches](../caches.md). A build restores them before `install` and saves them after it.

Builds are the only writers of a repo's shared caches. Every sandbox restores them in the background after the clone,
and sccache, Bazel and Turborepo come pointed at hangar's remote caches.
