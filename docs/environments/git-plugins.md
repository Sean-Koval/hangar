# Plugins from a git marketplace

How do I give an environment's sandboxes a Claude Code plugin that lives in a git repo? Name the repo and a commit.
hangar fetches that commit through the git gateway before it builds the image. The plugin then goes into the
read-only seed like a local one ([plugins](../harnesses.md#plugins)). This page is for the admin who writes the spec.

```yaml
# envs/team.yaml
harnesses: [claude-code]
plugins:
  - git: https://github.com/acme/team-harness   # the repo, with no credential in the URL
    ref: 3f9c2e1d0b8a7c6e5f4d3c2b1a0f9e8d7c6b5a49 # a full commit id
    path: plugins/team      # its marketplace directory (.claude-plugin/marketplace.json); default: the repo root
    name: workflow          # a plugin in that marketplace
    # marketplace: team     # read from its marketplace.json when left out
```

## Pinned by commit

`ref` must be a full commit id. A branch or a tag can move between two builds, so the build refuses one. The
refusal names the commit the ref points at now, as the refusal of a floating model alias does
([concrete ids](../harnesses.md#concrete-ids-and-floating-aliases)):

```text
plugin workflow: ref `main` of https://github.com/acme/team-harness is a branch, which can move between two
builds: an environment pins a plugin to a commit. Pin the one it names now: ref: 3f9c2e1d…
```

An abbreviated commit is refused too. Pin the full id that `git rev-parse` prints.

## Fetched through the git gateway

The control plane fetches the commit, not the build and not a sandbox. The repo's host must be one of the git
gateway's upstreams (`HANGAR_GATEWAY_UPSTREAMS`), and the fetch goes to the URL the gateway forwards that host to.

A private repo is read with the gateway's own credential: `HANGAR_GATEWAY_CREDENTIAL`, else the GitHub App with
`contents: read`. That is how hangar reads the [team harness repo](../harness-lock.md#the-team-harness-repo).

The token reaches git in its environment, never on a command line. No sandbox token is ever used. The image build
gets the commit's marketplace directory as plain files in its context, so it runs no git and holds no credential.

Fetched commits are kept under `<data>/plugin-git/`, so the next build of the same commit fetches nothing. A
`file:///` URL names a repo on the control plane's own disk, read as it is.

## Before the first build

A dry run (`create_environment` with `dry_run`, or `POST /api/environments/validate`) doesn't fetch. It warns that
the plugin is fetched at build and shows the Dockerfile once the commit is fetched. A build fails, naming the
plugin, when the host isn't a gateway upstream or the commit isn't in the repo.

## What the manifest records

Each git plugin's entry in the manifest's `plugins` section has `git`, its repo URL, and `commit`, the pinned id.
They sit beside the `digest` of its marketplace entry and directory. A new commit with other content is a new
digest, so it changes `harness_digest` and rebuilds the image.
