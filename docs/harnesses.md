# Harnesses: versions, the contract, the canary and models

hangar runs four agent CLIs (Claude Code, Codex, pi and Antigravity) and drives two of them over undocumented, fast-moving
protocols. This page covers how their versions are pinned and moved forward, what CI checks before a version reaches
anyone, and how each one gets "the latest strong model" without a stale default.

| | Where | When |
| --- | --- | --- |
| The pinned CLI versions | `src/hangar/harnesses.lock.yaml` | changed only by a reviewed PR |
| The contract tests | `.github/workflows/harness-contract.yml` | PRs that touch harness, driver or isolation code; every push to main |
| The canary | `.github/workflows/harness-canary.yml` | nightly, 03:17 ET (07:17 UTC), and by hand |
| Landing the canary's PR | `.github/workflows/harness-canary-land.yml` | on an approval of it |
| Model policies | an environment's or repo policy's `model:` (`balanced`, which jobs and `envs/bare.yaml` default to, or `frontier`) | when a sandbox or session is created |

None of these talk to the cluster, so they all work while it is powered down.

This page is about the agent CLIs themselves. A *harness* in the sense of what an agent runs with (its instructions, skills,
config fragments, MCP servers and hooks, pinned by digest) is a harness directory with a `harness.lock`: see
[Harness locks](harness-lock.md). Its `runtime.version` is the CLI version locked here.

## The lock

`src/hangar/harnesses.lock.yaml` pins every harness hangar installs: an exact version, plus the vendor's checksums where the
vendor publishes the binary directly (Claude Code's sha256 per platform from its release manifest; Antigravity's
build id and sha512 per archive). npm packages (Codex, pi) are checked by npm against the registry's sha512
integrity at install. A pack build fails unless the CLI reports the locked version ([gke.md](gke/images.md)).

A sandbox records the locked version its pack was built for (`harness_lock`) next to what the CLI reported
(`harness_version`).

## The contract tests

The real-CLI tests (`tests/test_*real_clis.py` and the real-CLI part of `tests/test_cache.py`) start each CLI under
hangar's driver against scripted model APIs (`tests/fixtures/live/fake_models.py`), offline:

- approvals, steering and interrupts (`test_driver_real_clis.py`);
- the hardened turn: what a turn asks for, rules and MCP servers (`test_harden_real_clis.py`);
- the verify gate (`test_verify_gate_real_clis.py`);
- prompt-cache TTLs (`test_cache.py`);
- **the protocols** (`test_protocol_real_clis.py`). For Codex, the app-server JSON Schema the CLI generates
  (`codex app-server generate-json-schema`), cut down to the methods `driver.Codex` sends and receives and the types they
  reach. For Claude Code, the stream-json messages a live turn writes (a permission prompt, an interrupt with a
  follow-up), as message type and field paths. What the driver relies on (`canary.CODEX_USES`, `canary.CLAUDE_DRIVER`)
  must be there for any version. The locked version must also produce exactly the record in `tests/fixtures/protocol/`.

`harness-contract.yml` installs the locked CLIs with the packs' own recipes (`python -m hangar.canary install`, no
root; checksums and the version check included), sets `HANGAR_TEST_CLAUDE`, `HANGAR_TEST_CODEX` and `HANGAR_TEST_PI`,
and runs those files. A skipped test fails the job: every CLI the lock pins must actually run. The CLIs are cached by
the lock and the recipes. A run that finds them takes about a minute, and a cold one about two (the four installs
take ~40 s).

Run the same thing locally:

```bash
uv run python -m hangar.canary install --dest ~/.cache/hangar-clis --env-file .env.clis
env $(cat .env.clis) PATH="$PATH:$HOME/.cache/hangar-clis/codex/libexec/codex-path" \
  uv run pytest tests/test_*real_clis.py tests/test_cache.py
```

Antigravity has no real-CLI test yet: its install (the checksum, then `agy --version` naming the locked version) is its
contract.

## Plugins

Claude Code plugins (skills, hooks, agents and MCP servers bundled with a manifest) are part of a harness's **prefix
layer**: they shape what the model is told, so they must not change under an experiment or come from anything the
agent can write. An environment declares them, and each image carries them in a read-only **seed directory**
(`src/hangar/plugins.py`, #136). An environment's plugins load in Claude Code only; Codex's skills stay in
`~/.codex/skills` (#139). A Codex sandbox run from a harness lock gets the lock's plugins from their Codex forms, with
exactly their hooks trusted ([harness-lock.md](harness-lock/thaw.md#codex-plugins), #244).

```yaml
# envs/team.yaml
harnesses: [claude-code]
plugins:
  - path: plugins/team          # a marketplace directory (.claude-plugin/marketplace.json), next to this file
    name: workflow              # a plugin in it
    # marketplace: team         # read from its marketplace.json when left out
    # enabled: false            # installed in the seed, not loaded
  - {git: https://github.com/acme/team-harness, ref: <full commit id>, path: plugins/team, name: review}
```

- **The build.** The image build installs them with the locked Claude Code, in the pack's own build stage
  (`claude plugin marketplace add` and `claude plugin install` with `CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/hangar/claude-seed`),
  and copies the seed into the image, root's and not writable by the sandbox user. Each marketplace sits at
  `/opt/hangar/claude-seed/marketplaces/<name>`: Claude Code skips a seed marketplace it can't find there (a
  `directory` marketplace is otherwise recorded where it was added from), and loads its plugins in place. A plugin's
  content is a build input. A prebuilt `image:` can't have plugins. Git ones: [git marketplaces](environments/git-plugins.md).
- **The run.** At claim, `CLAUDE_CODE_PLUGIN_SEED_DIR=/opt/hangar/claude-seed` and `CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1`
  (so turn 1 doesn't race the install) join the harness's environment. Every run adds hangar's own settings to
  `--settings` (`HANGAR_CLAUDE_SETTINGS`), never to `~/.claude/settings.json`: `enabledPlugins` for exactly the declared
  plugins, and `syncClaudeAiPlugins: false`, so none come from a claude.ai account.
- **Hardened turns** (`ask`, `deny-risky` in a split sandbox) load them too ([steering.md](steering/harness-settings.md)).
  The seed's plugins are the only ones: `enabledPlugins` comes from hangar's settings file alone, and
  `CLAUDE_CODE_PLUGIN_CACHE_DIR` points at a new, unguessable directory each turn (Claude Code creates it as it
  starts, and must be able to write it), so Claude Code never reads `~/.claude/plugins` and nothing the agent left
  behind is in it. A plugin the environment's own settings enable from anywhere else still doesn't load,
  and the turn says so.
- **No updates mid-experiment**, three ways: `DISABLE_AUTOUPDATER=1` is in Claude Code's environment, which also stops
  plugin auto-update; Claude Code forces `autoUpdate` off for seed marketplaces and never writes to the seed
  (`claude plugin marketplace update` or `remove` on one fails, naming the seed); and any `extraKnownMarketplaces`
  entry in an environment's `~/.claude/settings.json` gets `"autoUpdate": false` in the image.
- **Identity.** The manifest's `plugins` section lists each declared plugin: `name`, `marketplace`, `version`, the
  `path` it loads from under the seed, a `digest` of its marketplace entry and directory as declared, `enabled`,
  `effective` (whether hangar's settings load it for that harness, hardened turns included) and `source: seed`. It is
  part of `harness_digest` (by `{name, marketplace, digest, enabled}`: not its path or version), so a plugin's change
  is a harness change; Claude Code's writable caches in `~/.claude` are not inputs. A turn's probe also records
  whether each plugin's directory is in the sandbox (`probe.plugins`), which tells an image built before the plugin
  was added.
- **Locks** (#221). A harness lock pins each plugin as `plugin:<name>@<marketplace>` with its files, and a session
  or job thawed from the lock gets them as the seed at claim instead of the image's, so an environment without the
  plugin runs it ([harness-lock.md](harness-lock/thaw.md)).

What Claude Code does with the seed was verified with 2.1.296 (`tests/test_harden_real_clis.py`: the seed plugin's
skill and hook load in `auto` and `deny-risky` turns with their path under the seed, an agent-written plugin with a
permissive `PreToolUse` hook doesn't load in the hardened turn, and the seed marketplace can't be updated or removed).
Two plugin policy keys don't help here: `strictKnownMarketplaces` and `disableSideloadFlags` count only in managed
settings, not in `--settings` (and `disableSideloadFlags` would refuse the hardened turn's own `--mcp-config`); hangar
writes no managed settings, which would replace the image's. Seed directories and `syncClaudeAiPlugins` need Claude
Code 2.1.273 or later: the canary refuses a lock below that floor (`canary.FLOORS`).

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="the-canary"></span>The canary | [The harness canary](harnesses/canary.md) |
| <span id="landing-the-bump-one-approval-never-without-it"></span>Landing the bump: one approval, never without it | [The harness canary](harnesses/canary.md#landing-the-bump-one-approval-never-without-it) |
| <span id="setup"></span>Setup | [The harness canary](harnesses/canary.md#setup) |
| <span id="after-a-bump"></span>After a bump | [The harness canary](harnesses/canary.md#after-a-bump) |
| <span id="models-the-latest-strong-model-per-provider"></span>Models: the latest strong model per provider | [Models: the latest strong model per provider](harnesses/models.md) |
| <span id="where-the-ids-come-from"></span>Where the ids come from | [Models: the latest strong model per provider](harnesses/models.md#where-the-ids-come-from) |
| <span id="concrete-ids-and-floating-aliases"></span>Concrete ids and floating aliases | [Models: the latest strong model per provider](harnesses/models.md#concrete-ids-and-floating-aliases) |
| <span id="pinned-cached-and-what-happens-when-a-list-cant-be-had"></span>Pinned, cached, and what happens when a list can't be had | [Models: the latest strong model per provider](harnesses/models.md#pinned-cached-and-what-happens-when-a-list-cant-be-had) |
