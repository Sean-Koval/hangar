# Hook scripts in the harness digest

How does a harness's identity change when only a hook script changes?

A settings file names the hooks a runtime runs, but the scripts it calls are separate native files. The manifest
records each settings file's hooks with that file's digest. So a changed script under an unchanged settings file
would keep the same `harness_digest` (KAN-374).

## The pin

A settings file can pin the native files beside it. Its `env.HARNESS_NATIVE_DIGEST` holds one digest of every other
file under the harness's config directory (`~/.claude`). For each file in path order, the digest takes its
path under that directory, a NUL, the hex SHA-256 of its content and a newline.

The team harness's renderer writes the pin for every harness with native files.

## What hangar does with it

- **The turn manifest.** A settings file's hooks digest is the file's own digest while its pin matches the files
  beside it. When they don't match, the digest is one no lock can hold, and the entry carries `native` with the
  pinned and actual digests. The turn's `harness_digest` then differs from its lock's, so the turn reports drift.
- **Freeze.** A turn whose pin no longer matches its environment's files can't be frozen; freeze names the settings
  file. A hook script that a matching pin covers needs no warning.
- **Check.** `hangar harness check` fails on a stale pin in a settings file under `native/`. A pin is stale when
  it doesn't match the runtime's other native files and the lock pins the hooks to that file as it is.

The team harness renders a fresh pin and locks the rendered text, so an old pin on disk there is not an error.

An absolute path under `/home/<user>/` in the environment's `files` counts as its path under `$HOME`. It replaces an
`agent_config.files` entry for the same path, as the image build does.

## Limits

The manifest reads the environment spec, as for every harness section. A script the agent or a setup step rewrote
inside the sandbox is not seen. Only Claude Code's `settings.json` files are read; Codex hooks are not recorded yet.

## Without a pin

A settings file without the pin keeps its own digest, whatever sits beside it. Every lock written before the pin
keeps its identity, and no channel needs a re-key. Freeze still warns that the scripts beside such a file are read
from the environment as they are now.
