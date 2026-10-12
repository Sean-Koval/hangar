You are working in the hangar repository (Python 3.12, managed with uv). Your working directory is the repo checkout, on a fresh work branch.

Task: make `hangar token create`, `hangar token list` and `hangar token revoke` work against a remote hangar server.

Today these commands in src/hangar/cli.py open the local state store via `Hangar().auth`. Change them so that when the HANGAR_URL environment variable is set they use the server's REST API through the existing `_call` helper in the same file:
- create: POST /api/tokens with {"name": name}; the response contains "token" (the raw secret, print it once) plus "id" and "name".
- list: GET /api/tokens (a list of objects with id, name, last_used).
- revoke: DELETE /api/tokens/{token_id}.
When HANGAR_URL is not set, keep the current local behaviour exactly. Update the docstrings to say which mode is used.

Add tests in a new file tests/test_cli_tokens.py that use typer.testing.CliRunner on hangar.cli.app and monkeypatch hangar.cli._call to cover the remote path of all three commands, plus one test showing the local path is used when HANGAR_URL is unset (monkeypatch hangar.app.Hangar with a small fake).

Then run `uv sync --all-extras`, `uv run ruff check src tests` and `uv run pytest -q`, and fix anything that fails. Keep the change small and in the existing style (line length 120).

When everything passes: commit with a descriptive message, `git push -u origin HEAD`, and open a pull request against main with
`gh pr create --base main --title "CLI: hangar token commands work against a remote server" --body "<what changed and how it was tested>"`.
Do not merge the pull request. Finish by printing the pull request URL on its own line.
