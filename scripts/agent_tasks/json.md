You are working in the hangar repository (Python 3.12, managed with uv). Your working directory is the repo checkout, on a fresh work branch.

Task: add a `--json` flag to two CLI commands in src/hangar/cli.py so their output can be scripted:
- `hangar sb ls --json` prints the list returned by GET /api/sandboxes as indented JSON instead of the table.
- `hangar env list --json` prints the list returned by GET /api/environments as indented JSON instead of the table.
Without the flag, output must stay exactly as it is today. Both commands already fetch data through the `_call` helper; reuse it.

Add tests in a new file tests/test_cli_json.py that use typer.testing.CliRunner on hangar.cli.app and monkeypatch hangar.cli._call to return sample data, checking that `--json` output parses with json.loads and equals the sample, and that the default output is unchanged (still the table).

Then run `uv sync --all-extras`, `uv run ruff check src tests` and `uv run pytest -q`, and fix anything that fails. Keep the change small and in the existing style (line length 120).

When everything passes: commit with a descriptive message, `git push -u origin HEAD`, and open a pull request against main with
`gh pr create --base main --title "CLI: --json output for sb ls and env list" --body "<what changed and how it was tested>"`.
Do not merge the pull request. Finish by printing the pull request URL on its own line.
