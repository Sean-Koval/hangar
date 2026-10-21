# Builds

When a repo has an `install` step, hangar runs it ahead of time and keeps the result as an image. The repo's
sandboxes then start with everything installed. This page says where a build runs, what it holds and keeps, and when it
runs. The step itself is in [the repo's own file](repo-file.md).

A repo gets builds when it has an `install`, in its file or in its environment. A build is:

```
the environment's image  →  clone the default branch  →  run install  →  keep the checkout as an image
```

## Where it runs and what it holds

`install` is code from whoever can push, so it runs where agents run. That is an ordinary sandbox of the environment
(gVisor on GKE, hangar's isolated network on Docker), under the same network rules as its agents. It never runs on the
control plane or on an image builder.

The build sandbox holds three things:

- a fetch-only grant of the [git gateway](../security/git-gateway.md) for its clone;
- the [build secrets](../security/credentials.md#build-secrets) an admin mapped, in `install`'s environment only;
- egress per the environment and repo policy.

Nothing else: no credential of the environment (no model or person login, no `git_token`, no tool key), none of its
`secrets:`, no cloud tokens. Without the gateway (`HANGAR_GATEWAY_URL`) it holds no git credential at all, so a
private repo's build fails and says so.

## What is kept

Only the repo's checkout (`node_modules`, `.venv`, generated code and the rest) is kept. It is archived and `ADD`ed
onto the environment's image with no `RUN` step.

Nothing in `$HOME` is kept, so the sandbox's injected env and credentials can't end up in a build. Put what `install`
produces inside the repo; system tools belong in the environment spec. The checkout is capped at
`HANGAR_SNAPSHOT_MAX_MB` (512).

## Keys

A build is keyed by the repo, the environment, the provider and the environment's image. It is also keyed by a digest
of the file's build fields (`install`, `build_secrets`, `lockfiles`) and the lockfiles' git blob ids. A comment, a new
terminal or a changed `verify` doesn't rebuild.

Common lockfiles are watched anywhere in the repo: `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock(b)`,
`uv.lock`, `poetry.lock`, `Pipfile.lock`, `requirements*.txt`, `Cargo.lock`, `go.sum`, `Gemfile.lock`,
`composer.lock`, `.tool-versions`, `mise.toml` and more (`repoenv.LOCKFILES`). Add your own with `lockfiles:`.

## When

Never while anyone waits. After every clone, one exec reads the file and the lockfiles' ids. A rebuild starts in the
background, for the next sandbox, when the active build is behind. That is when the default branch's config, its
lockfiles or the environment's image moved on.

Startup's prebuild ([#83](../gke.md#3-environment-images)) rebuilds the builds an environment change made stale. A
build that failed isn't retried until something changes (the file, a lockfile, the image) or someone runs
`hangar env build-repo`. At most `HANGAR_ENV_BUILDS_CONCURRENCY` (2) builds run at once, one per repo, each within
`HANGAR_ENV_BUILD_TIMEOUT_MINUTES` (30).

## A failed build, and an up-to-date checkout

**A failed build never replaces the active one.** New sandboxes keep starting from the last good build. With none,
they start from the environment's own image, and the agent is told so, in plain words, at the top of its
instructions. It also gets the install command to run itself. The job's events say so too.

**The checkout is brought up to date.** A sandbox started from a build fetches and checks out the branch it asked
for. Untracked files left by `install` (not ignored ones like `node_modules`) are removed, so the agent's diff is its
own. When the branch's lockfiles differ from the build's, the agent is told to run the install.

## History and rollback

Each repo keeps its last `HANGAR_ENV_BUILDS_KEEP` (5) builds per environment and provider:

```console
$ hangar env builds acme/app
acme/app / python on kubernetes
*   v7  bld-1a2b3c4d  succeeded   3f2c1a9e0  2026-10-14 09:12  drift   us-docker.pkg.dev/.../python-repo@sha256:...
    v8  bld-5e6f7a8b  failed      91aa04c2d  2026-10-14 11:40  drift   install exited with 1
$ hangar env build-show bld-5e6f7a8b         # its config and the end of the install's output
$ hangar env build-repo acme/app --wait      # build now
$ hangar env rollback bld-0c0ffee0           # admin: make it active, pinned
$ hangar env unpin acme/app                  # admin: let new builds take over again
```

A rollback pins the build it picks: drift rebuilds still run and are kept, but don't replace it until an admin
unpins. Rollbacks and unpins are audited (`envbuild.rollback`, `envbuild.unpin`), as are builds (`envbuild.start`,
`envbuild.finish`).

## What the agent and you see

Every sandbox started for a repo with a config carries `env_build` in `get_sandbox`, `list_sandboxes` and the REST
sandbox views:

| Field | Meaning |
| --- | --- |
| `version`, `id` | The build it started from (`v7`, `bld-1a2b3c4d`); null on the environment's own image |
| `fallback` | true: no build in use; `reason` says why (none yet, the latest failed, the image is gone, ...) |
| `stale` | true: the repo or the environment moved on since this build; a rebuild is running or `reason` says why not |
| `commit`, `built_at`, `config_digest`, `image` | What the build is |

The agent gets the same in a "Your environment (hangar)" section of its harness's global instructions file
(`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, ...). It also gets a `hangar-environment` skill that documents the
supervisor.

Problems hangar ran into while setting up are in `sb.info["setup_failures"]` and in that section, never as a failed
sandbox. An invalid file and a supervisor that didn't start are two such problems.

Build secrets, the limits of builds and build caches are on [build secrets and limits](build-secrets.md).
