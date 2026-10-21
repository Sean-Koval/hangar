# Connecting MCP clients

hangar serves MCP over streamable HTTP as two servers ([the two servers](mcp.md#two-servers)):

- `hangar` at `<public_url>/mcp`: jobs, sessions, schedules, discovery;
- `hangar-sandbox` at `<public_url>/mcp/sandbox`: sandboxes, commands, files, terminals.

What clients get beyond tool calls (tasks, elicitation, typed results, subscriptions, prompts) is in
[using MCP](mcp.md). The quick way to connect a client:

```bash
hangar connect claude-code      # or codex, cursor, vscode, gemini, antigravity, claude-desktop
hangar connect --list           # each client, its config file, and whether hangar is in it
```

## What `hangar connect` writes

`hangar connect <client>` mints a token for that client on this machine and writes hangar into the client's own
config file. The token is named `mcp-<client>-<host>-<file id>` and has the `mcp` scope, so it reaches `/mcp` and
nothing else.

It can't call the REST API, so it can't read or change the vault, settings or tokens. It can still do anything an MCP
client can, including starting sandboxes that use stored credentials, so keep it as private as a password.

The other servers in the file, and every other key, are left as they were. Connecting the same file again replaces the
entry and revokes the token it held before. Tokens in the client's other config files are left alone: a project's
`.cursor/mcp.json`, or another `CLAUDE_CONFIG_DIR`. Options:

- `--print` shows the entry (with a new token) and writes nothing. Use it to configure another machine.
- `--auth oauth` writes the URL without a token. The client then signs in through hangar's OAuth server. This works
  for Claude Code, Codex, Cursor, Gemini CLI and Antigravity, and needs an https (or localhost) `HANGAR_PUBLIC_URL`.
- Cursor's sign-in redirects (`cursor://anysphere.cursor-mcp/oauth/callback`, and `http://localhost:8787/callback` in
  newer versions) are in the default `HANGAR_OAUTH_REDIRECT_HOSTS`. If you changed that list,
  `hangar connect cursor --auth oauth` prints the one command that adds the app scheme back
  ([OAuth connectors](security/people.md#oauth-connectors)).
- Antigravity's redirect (`https://antigravity.google/oauth-callback` in its docs) isn't in the default list. Add it
  with `hangar config set HANGAR_OAUTH_REDIRECT_HOSTS '[…, "antigravity.google"]'` (the current list plus that host)
  before `hangar connect antigravity --auth oauth`.
- `--with-sandbox` / `--no-sandbox`: also write `hangar-sandbox` (`<name>-sandbox`, `/mcp/sandbox`, the same token).
  It is on by default for Claude Code and Codex, which drive sandboxes themselves, and off for the others. In
  claude.ai, ChatGPT or Claude Desktop's connectors, add `<public_url>/mcp/sandbox` as a second connector if you want
  the sandbox tools.
- `--name` sets the server name (default `hangar`), and `--config PATH` writes another file, for example a VS Code
  Insiders profile.

Files are replaced atomically. A new file is created with mode 600. An existing one keeps its owner's permissions but
loses group and other access, since it now holds a token. A symlinked file (dotfiles) is written through the link.

Some files can't be edited safely: one that isn't plain JSON (VS Code and Gemini allow comments), or TOML hangar can't
edit. hangar leaves those alone and prints the entry for you to paste.

| Client | File | Entry | After connecting |
| --- | --- | --- | --- |
| `claude-code` | `~/.claude.json` (`$CLAUDE_CONFIG_DIR/.claude.json`), top-level `mcpServers` (user scope) | `{"type": "http", "url", "headers": {"Authorization": "Bearer …"}}` | new session; `claude mcp get hangar` |
| `codex` | `~/.codex/config.toml` (`$CODEX_HOME/config.toml`) | `[mcp_servers.hangar]` with `url`, `http_headers`, `startup_timeout_sec = 20`, `tool_timeout_sec = 900` | new session; `codex mcp get hangar` |
| `cursor` | `~/.cursor/mcp.json`, `mcpServers` | `{"url", "headers"}` | Settings → MCP |
| `vscode` | `mcp.json` in the VS Code user folder: `~/.config/Code/User` (Linux), `~/Library/Application Support/Code/User` (macOS), `%APPDATA%\Code\User` (Windows); key `servers` | `{"type": "http", "url", "headers"}` | MCP: List Servers |
| `gemini` | `~/.gemini/settings.json` (`$GEMINI_CLI_HOME/.gemini/settings.json`), `mcpServers` | `{"url", "type": "http", "headers"}` | new session; `/mcp` |
| `antigravity` | `~/.gemini/config/mcp_config.json`, `mcpServers` (the Antigravity CLI `agy`, app and IDE share it) | `{"serverUrl", "headers"}` | new `agy` session; `/mcp` |
| `claude-desktop` | `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), `%APPDATA%\Claude\claude_desktop_config.json` (Windows) | `npx -y mcp-remote@0.14.3 <url> --header-file <file>` | restart Claude Desktop |

Claude Desktop's config file only starts local (stdio) servers, so the entry runs the
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge, which needs Node.js. The token goes in a mode-600
header file under `~/.hangar/mcp-headers/`, not in the command line, where other users could read it from the process
list.

To use OAuth with Claude Desktop instead, add hangar as a custom connector under **Settings → Connectors**.

Where each format comes from, and how to refresh a client's tool list after an upgrade, is on
[config formats and tool refresh](clients/formats.md).

## Doing it by hand

Create a token with `hangar token create <name> --scope mcp`, or in the dashboard under **Connect**.

### Codex (CLI, IDE extension, app)

```toml
# ~/.codex/config.toml
[mcp_servers.hangar]
url = "https://hangar.example.com/mcp"
bearer_token_env_var = "HANGAR_TOKEN"   # or: http_headers = { Authorization = "Bearer hgr_..." }
tool_timeout_sec = 900                  # builds and forks can take minutes
startup_timeout_sec = 20
```

Or run `codex mcp add hangar --url https://hangar.example.com/mcp --bearer-token-env-var HANGAR_TOKEN`.

### Antigravity

In `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "hangar": {
      "serverUrl": "https://hangar.example.com/mcp",
      "headers": { "Authorization": "Bearer hgr_..." }
    }
  }
}
```

Leave out `headers` to have Antigravity sign in with OAuth instead (see the redirect note above).

### Claude Code

```bash
claude mcp add --scope user --transport http hangar https://hangar.example.com/mcp \
  --header "Authorization: Bearer $HANGAR_TOKEN"
```

## Prompts that work well

- "List environments. Start a `claude-python` sandbox on github.com/me/api with work branch `agent/fix-timeouts`, run the tests, and show me the failures."
- "Fork that sandbox 3 times with branch prefix `attempt` and run the agent in each on the same task. Then compare the diffs with `git diff main` in each."
- "Open a terminal session called `server` in sb-1234, start `uv run uvicorn app:app`, and wait for 'Application startup complete'."

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="when-hangars-tools-change"></span>When hangar's tools change | [Config formats and tool refresh](clients/formats.md#when-hangars-tools-change) |
| <span id="where-these-formats-come-from"></span>Where these formats come from | [Config formats and tool refresh](clients/formats.md#where-these-formats-come-from) |
| <span id="bots-that-act-for-people-relay"></span>Bots that act for people (relay) | [Bots that act for people](clients/relay.md) |
| <span id="chatgpt-claudeai-and-other-oauth-clients"></span>ChatGPT, claude.ai and other OAuth clients | [OAuth clients](clients/oauth.md) |
