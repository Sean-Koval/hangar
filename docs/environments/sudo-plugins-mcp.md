# Root, plugins and MCP trust

Three admin decisions on an [environment](../environments.md): whether the agent gets root, and which Claude Code
plugins it runs. Then which of its MCP servers can carry text nobody you trust wrote.

## Root in the sandbox: `sudo`

An environment spec's `sudo` gives the sandbox user passwordless sudo, which makes the agent root whenever it likes.
It is admin-set; a repo's own file can't set it. It is **`false` by default** (#124). With sudo the agent can rewrite
files hangar owns (managed harness settings), and the [uid split](../steering.md#the-uid-split) protects nothing.

- **Install at build time.** `tools.apt`, mise and `setup` steps run as root in the image build. So the shipped
  environments (`base`, `bare`, `python` and the rest) need no runtime root. A repo's `install` runs as the sandbox
  user and should put what it installs inside the repo (`.venv`, `node_modules`).
- **`sudo: true`** is for an environment whose people need root at runtime (`apt-get install` while exploring). Keep
  it for people's own sessions.
- On Docker, a job's sandbox runs with `no-new-privileges` under `HANGAR_INTAKE_POSTURE=enforce`
  ([intake posture](../security/front-doors.md#intake-posture)). So sudo fails there even with `sudo: true`, as it
  always does on GKE (`allowPrivilegeEscalation: false`).
- **Heavy environments** run dockerd as root, so `heavy: true` (or `tier: heavy`) implies `sudo: true`. An explicit
  `sudo: false` beside it is refused.
- Under `enforce`, an intake job on a heavy environment is refused unless the repo policy sets
  `allow_heavy_for_intake: true` (`hangar repo set owner/repo --allow-heavy-for-intake`).
- `hangar doctor` lists the environments with `sudo: true` and whether intake jobs run in them. A sandbox's manifest
  records `permissions.sudo` and `permissions.no_new_privs`.

## Claude Code plugins

An environment's `plugins:` list names Claude Code plugins from local marketplace directories next to its spec. The
image build installs them with the locked Claude Code into a read-only seed at `/opt/hangar/claude-seed`. That seed is
root's, out of the agent's reach. hangar's own settings enable exactly those, in every turn, hardened ones included.

Only an admin's environment can declare them (not a repo's file), a prebuilt `image:` can't have them, and changing a
plugin rebuilds the image. The details, and what the manifest records, are on [plugins](../harnesses.md#plugins).
A plugin can also come from a git repo, pinned by commit and fetched through the git gateway ([git marketplaces](git-plugins.md)).

```yaml
harnesses: [claude-code]
plugins:
  - {path: plugins/team, name: workflow}   # .claude-plugin/marketplace.json in plugins/team
```

## MCP servers and trust

An environment's `mcp:` servers are written into each harness's own config at claim
([environment specs](../environments.md#environment-specs)). Each server can say whether its tool results can carry
text nobody you trust wrote:

```yaml
mcp:
  github:                         # issue comments, PR bodies: anyone can write them
    url: https://api.githubcopilot.com/mcp/
    headers: {Authorization: "Bearer ${GH_TOKEN}"}
    trust: untrusted              # the default for a url server
  docs-index:
    command: docs-mcp             # a local stdio server over your own repo
    trust: trusted                # the default for a command server
```

`mcp.<name>.trust` is `trusted` or `untrusted`. Unset, a remote (`url`) server is `untrusted` and a local stdio
(`command`) server is `trusted`. A call to an untrusted server's tool taints the turn ([taint](../security/taint.md)).

Mark a stdio server that relays third-party text (Jira, Slack, a mail or web reader) `untrusted`. Mark a remote one
you run over your own data `trusted`. hangar's own `hangar-env` server is trusted; the built-in browser
(`computer.browser_mcp`) is untrusted; a server the agent adds itself is untrusted. A repo's
`.hangar/environment.yaml` can't set `mcp:` at all.

`mcp.<name>.write_tools` lists the server's tools that change something, as fnmatch patterns of its tool names
(`["create_*", "update_*", "add_*_comment", "merge_pull_request"]`). Once a turn has read untrusted content, a call to
one needs a person under `ask` and `deny-risky` ([steering](../steering.md#taint)).
