# Config formats and tool refresh

How a client sees hangar's tools after an upgrade, and where each config format [`hangar connect`](../clients.md)
writes comes from.

## When hangar's tools change

After an upgrade that adds or changes tools, a client has to fetch the tool list again to see them. Claude Code, Codex
and the other CLIs do that when a session starts. The claude.ai web, desktop and mobile apps don't: they keep the list
from when the connector was added ([claude-code#38324](https://github.com/anthropics/claude-code/issues/38324)).

Remove the hangar connector and add it again (**Settings → Connectors**) to refresh it. hangar notices calls from an
older list and says so in the tool's answer, at most once an hour ([deployment notes](../mcp/deployment.md)).
`serverInfo.version` (`<version>+tools.<hash>`) tells you which tool list a hangar serves.

## Where these formats come from

Each format was checked against the client itself or its official docs on 2026-10-13:

- **Claude Code 2.1.296:** `claude mcp add --scope user --transport http … --header …` writes exactly this entry to `.claude.json`, and `claude mcp get hangar` reports *Connected* for an entry written by `hangar connect`. See also [code.claude.com/docs/en/mcp](https://code.claude.com/docs/en/mcp).
- **Codex CLI 0.162.1:** `codex mcp add --url … --bearer-token-env-var …` writes `[mcp_servers.<name>]`, and `codex mcp get --json` parses the `http_headers`, `startup_timeout_sec` and `tool_timeout_sec` keys. `codex mcp login` handles OAuth.
- **Cursor:** [cursor.com/docs/mcp](https://cursor.com/docs/mcp) documents `~/.cursor/mcp.json`, `mcpServers` with `url` and `headers`, and OAuth for remote servers.
- **VS Code:** [the MCP configuration reference](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration) documents `servers` with `"type": "http"`, `url` and `headers`, in `mcp.json` in the user profile. The profile folder is the one [the settings docs](https://code.visualstudio.com/docs/configure/settings) give for the user `settings.json`. The docs don't say that VS Code registers itself with an OAuth server, so `--auth oauth` isn't offered for VS Code.
- **Gemini CLI 0.63.0:** `gemini mcp add --scope user --transport http -H …` writes exactly this entry. The CLI reads `GEMINI_CLI_HOME`, and `/mcp auth <name>` handles OAuth.
- **Antigravity** (checked 2026-10-19): [antigravity.google/docs/mcp](https://antigravity.google/docs/mcp) documents
  the global `~/.gemini/config/mcp_config.json`. A project's own is `.agents/mcp_config.json`: pass it with `--config`.
    - Its `mcpServers` entries for remote servers must use `serverUrl`, with `headers`. It doesn't support `url` or
      `httpUrl`, so Gemini CLI's entry doesn't work there.
    - OAuth is by dynamic client registration, with `https://antigravity.google/oauth-callback` as the redirect.
    - It documents no environment-variable expansion in that file, so the token is written into it, mode 600.
    - The [install page](https://antigravity.google/docs/install?tab=cli) names the CLI `agy`. It is not yet qualified
      against a live `agy`, including how it handles hangar's long tool calls
      ([deployment notes](../mcp/deployment.md)).
- **Claude Desktop:** the config file locations and the `--header-file` and `--allow-http` flags come from the
  `mcp-remote` 0.14.3 README and its code. `--allow-http` is for plain http to hosts other than localhost.
