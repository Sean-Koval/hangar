# What a command wrote and ran

A tool call's event keeps a 200-character preview of its input. About half of real agent commands are longer. A
labeller that reads the preview can't see a write or a test run past the cut.

So hangar's parser reads three facts from the whole command before it clips it (KAN-287). This page says what they
are and where to find them.

## Where they are

| Where | Keys |
| --- | --- |
| A `command` event's data (any shell call) | `writes`, `runs`, `probe` |
| A `tool_call` event's data, for a file tool (`Write`, `Edit`, Codex `apply_patch` and the like) | `writes` |
| A shell call's `tool.call` span in the [trace export](traces.md) | `hangar.shell.writes`, `hangar.shell.runs`, `hangar.shell.probe` |

Event data keeps scalars only. So in an event, `writes` and `runs` are text with one item per line. In a span, they
are arrays of strings. A key with nothing in it is left out.

Events recorded before KAN-287 have none of these keys. Their span reads the facts from the clipped preview instead,
as it does for `hangar.shell.ops`. A [session imported](trace-import.md) from Claude Code gets them too, because the
same parser reads it.

## `writes`

The files the command writes, in order, each once:

- A redirect: `>`, `>>`, `&>`, `>|`. Not `/dev/null` or a descriptor like `2>&1`.
- `tee`, `sed -i` and `perl -i`: their files.
- `cp`, `mv`, `install` and `ln`: the destination. `touch`: each file.
- `patch`: its file or its `-o` output.
- A diff in a heredoc fed to `git apply`, `patch` or `apply_patch`: each file it changes or adds.
- Python in `python -c` or a heredoc: files opened for writing, and `Path(...).write_text` or `write_bytes`.

For a file tool, `writes` is the path the harness names. A Codex `apply_patch` has one item for each file.

## `runs`

The programs run at a command position, in order, each once. A tool with subcommands keeps its subcommand: `git
status`, `npm run build`, `uv sync`. A script keeps its name: `python -m pytest`, `python3 tools/gen.py`, `node -e`.

Wrappers are looked through, with their own flags: `uv run --project svc pytest` runs `pytest`. So are `sudo`,
`timeout`, `env`, `pnpm exec` and `VAR=value`. A tool's global flags are skipped too, so `kubectl -n ns get` is
`kubectl get`.

A program named by its full path keeps only its name, so `/usr/bin/git status` is `git status`. Text in quotes and
heredoc bodies is not a program. The commands of `bash -c '...'`, `$(...)` and `<(...)` are read too.

## `probe`

`true` when the command runs a test runner, and each one it runs was only asked for its version, its help or its
test list. Those are `--version`, `--help`, `--collect-only` and `--list`, among others. Nothing was tested then. A
command that probes once and then runs the tests has no `probe`.

The runners are those the [trace export](traces.md) lists for the `test` op, such as `pytest`, `go test` and `npm
test`.

## Limits

Only the first 65,536 characters of a command are read. Each list keeps at most 32 items of at most 256 characters,
and at most 2 KiB in all. Nothing else of the command is stored.

The facts are read from the command's words, not by running it. So they are incomplete. Variables, globs, `eval`,
`xargs`, `dd of=`, `curl -o` and a script's own writes are missed. Paths are as written: a `cd` before them isn't
followed.

The agent writes the command, so it controls the facts. Use them as hints for labelling and search, like `ops`. Never
let them gate an approval, a taint check or any other security decision. The command itself is in the event too.
