# Connecting MCP clients

Hangar serves MCP over streamable HTTP as two servers: `hangar` at `<public_url>/mcp` (jobs, sessions, schedules, discovery) and `hangar-sandbox` at `<public_url>/mcp/sandbox` (sandboxes, commands, files, terminals; [mcp.md](mcp.md#two-servers)). What clients get beyond tool calls (tasks, elicitation, typed results, subscriptions, prompts) is in [mcp.md](mcp.md). The quick way to connect a client:

```bash
hangar connect claude-code      # or codex, cursor, vscode, gemini, antigravity, claude-desktop
hangar connect --list           # each client, its config file, and whether hangar is in it
```

`hangar connect <client>` mints a token for that client on this machine and writes hangar into the client's own config file. The token is named `mcp-<client>-<host>-<file id>` and has the `mcp` scope, so it reaches `/mcp` and nothing else. It can't call the REST API, so it can't read or change the vault, settings or tokens. It can still do anything an MCP client can, including starting sandboxes that use stored credentials, so keep it as private as a password. The other servers in the file, and every other key, are left as they were. Connecting the same file again replaces the entry and revokes the token it held before; tokens in the client's other config files (a project's `.cursor/mcp.json`, another `CLAUDE_CONFIG_DIR`) are left alone. Options:

- `--print` shows the entry (with a new token) and writes nothing. Use it to configure another machine.
- `--auth oauth` writes the URL without a token. The client then signs in through hangar's OAuth server. This works for Claude Code, Codex, Cursor, Gemini CLI and Antigravity, and needs an https (or localhost) `HANGAR_PUBLIC_URL`. Cursor's sign-in redirects (`cursor://anysphere.cursor-mcp/oauth/callback`, and `http://localhost:8787/callback` in newer versions) are in the default `HANGAR_OAUTH_REDIRECT_HOSTS`; if you changed that list, `hangar connect cursor --auth oauth` prints the one command that adds the app scheme back (see [security.md](security.md#oauth-connectors)). Antigravity's redirect (`https://antigravity.google/oauth-callback` in its docs) isn't in the default list: add it with `hangar config set HANGAR_OAUTH_REDIRECT_HOSTS '[…, "antigravity.google"]'` (the current list plus that host) before `hangar connect antigravity --auth oauth`.
- `--with-sandbox` / `--no-sandbox`: also write `hangar-sandbox` (`<name>-sandbox`, `/mcp/sandbox`, the same token). On by default for Claude Code and Codex, which drive sandboxes themselves; off for the others. In claude.ai, ChatGPT or Claude Desktop's connectors, add `<public_url>/mcp/sandbox` as a second connector if you want the sandbox tools.
- `--name` sets the server name (default `hangar`), and `--config PATH` writes another file, for example a VS Code Insiders profile.

Files are replaced atomically. A new file is created with mode 600. An existing one keeps its owner's permissions but loses group and other access, since it now holds a token, and a symlinked file (dotfiles) is written through the link. If a file isn't plain JSON (VS Code and Gemini allow comments) or the TOML can't be edited safely, hangar leaves it alone and prints the entry for you to paste.

| Client | File | Entry | After connecting |
| --- | --- | --- | --- |
| `claude-code` | `~/.claude.json` (`$CLAUDE_CONFIG_DIR/.claude.json`), top-level `mcpServers` (user scope) | `{"type": "http", "url", "headers": {"Authorization": "Bearer …"}}` | new session; `claude mcp get hangar` |
| `codex` | `~/.codex/config.toml` (`$CODEX_HOME/config.toml`) | `[mcp_servers.hangar]` with `url`, `http_headers`, `startup_timeout_sec = 20`, `tool_timeout_sec = 900` | new session; `codex mcp get hangar` |
| `cursor` | `~/.cursor/mcp.json`, `mcpServers` | `{"url", "headers"}` | Settings → MCP |
| `vscode` | `mcp.json` in the VS Code user folder: `~/.config/Code/User` (Linux), `~/Library/Application Support/Code/User` (macOS), `%APPDATA%\Code\User` (Windows); key `servers` | `{"type": "http", "url", "headers"}` | MCP: List Servers |
| `gemini` | `~/.gemini/settings.json` (`$GEMINI_CLI_HOME/.gemini/settings.json`), `mcpServers` | `{"url", "type": "http", "headers"}` | new session; `/mcp` |
| `antigravity` | `~/.gemini/config/mcp_config.json`, `mcpServers` (the Antigravity CLI `agy`, app and IDE share it) | `{"serverUrl", "headers"}` | new `agy` session; `/mcp` |
| `claude-desktop` | `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), `%APPDATA%\Claude\claude_desktop_config.json` (Windows) | `npx -y mcp-remote@0.14.3 <url> --header-file <file>` | restart Claude Desktop |

Claude Desktop's config file only starts local (stdio) servers, so the entry runs the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge, which needs Node.js. The token goes in a mode-600 header file under `~/.hangar/mcp-headers/`, not in the command line, where other users could read it from the process list. To use OAuth with Claude Desktop instead, add hangar as a custom connector under **Settings → Connectors**.

### When hangar's tools change

After an upgrade that adds or changes tools, a client has to fetch the tool list again to see them. Claude Code, Codex and the other CLIs do that when a session starts. The claude.ai web, desktop and mobile apps don't: they keep the list from when the connector was added ([claude-code#38324](https://github.com/anthropics/claude-code/issues/38324)). Remove the hangar connector and add it again (**Settings → Connectors**) to refresh it. Hangar notices calls from an older list and says so in the tool's answer, at most once an hour ([mcp.md](mcp.md#deployment-notes)); `serverInfo.version` (`<version>+tools.<hash>`) tells you which tool list a hangar serves.

### Where these formats come from

Each format was checked against the client itself or its official docs on 2026-10-13:

- **Claude Code 2.1.296:** `claude mcp add --scope user --transport http … --header …` writes exactly this entry to `.claude.json`, and `claude mcp get hangar` reports *Connected* for an entry written by `hangar connect`. See also [code.claude.com/docs/en/mcp](https://code.claude.com/docs/en/mcp).
- **Codex CLI 0.162.1:** `codex mcp add --url … --bearer-token-env-var …` writes `[mcp_servers.<name>]`, and `codex mcp get --json` parses the `http_headers`, `startup_timeout_sec` and `tool_timeout_sec` keys. `codex mcp login` handles OAuth.
- **Cursor:** [cursor.com/docs/mcp](https://cursor.com/docs/mcp) documents `~/.cursor/mcp.json`, `mcpServers` with `url` and `headers`, and OAuth for remote servers.
- **VS Code:** [the MCP configuration reference](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration) documents `servers` with `"type": "http"`, `url` and `headers`, in `mcp.json` in the user profile. The profile folder is the one [the settings docs](https://code.visualstudio.com/docs/configure/settings) give for the user `settings.json`. The docs don't say that VS Code registers itself with an OAuth server, so `--auth oauth` isn't offered for VS Code.
- **Gemini CLI 0.63.0:** `gemini mcp add --scope user --transport http -H …` writes exactly this entry. The CLI reads `GEMINI_CLI_HOME`, and `/mcp auth <name>` handles OAuth.
- **Antigravity** (checked 2026-10-19): [antigravity.google/docs/mcp](https://antigravity.google/docs/mcp) documents the global `~/.gemini/config/mcp_config.json` (a project's own is `.agents/mcp_config.json`: pass it with `--config`), `mcpServers` entries whose remote servers must use `serverUrl` (it doesn't support `url` or `httpUrl`, so Gemini CLI's entry doesn't work there) with `headers`, and OAuth by dynamic client registration with `https://antigravity.google/oauth-callback` as the redirect. It documents no environment-variable expansion in that file, so the token is written into it, mode 600. The [install page](https://antigravity.google/docs/install?tab=cli) names the CLI `agy`. Not yet qualified against a live `agy`, including how it handles hangar's long tool calls ([mcp.md](mcp.md#deployment-notes)).
- **Claude Desktop:** the config file locations and the `--header-file` and `--allow-http` flags (the latter for plain http to hosts other than localhost) come from the `mcp-remote` 0.14.3 README and its code.

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

## Bots that act for people (relay)

A chat bot, such as the Symphony bot, can submit work for the person who asked in the room, so the job is theirs (their visibility, caps, credentials and audit trail), not the bot's. The bot holds a **relay** token and names that person on every MCP request:

```text
Authorization: Bearer hgr_<relay token>
X-Hangar-On-Behalf-Of: symphony:<Symphony user id>
# or: {"source": "symphony", "id": "<user id>", "reply_to": "symphony:<stream id>:<message id>"}
```

Set it up as an admin:

```bash
hangar person add symphony-bot --role admin --no-token   # the bot's person: only an admin's token can relay
hangar token create symphony-bot --scope relay --person symphony-bot   # or --scope relay_approve
hangar person link alice symphony <Symphony user id>                    # once per person
```

What hangar trusts, and what it refuses:

- **The bot vouches for the Symphony account, nothing more.** A claim names a `symphony` account only. GitHub, Slack, Jira and email accounts are verified by hangar's own doors, so a relay can't claim them.
- **Linked accounts only.** The request runs as the linked person with the `mcp` scope and nothing more: never admin, sandboxes, metrics or a login, whatever the bot's token or the person's other tokens hold. Records and audit entries show both identities, for example `@alice:relay:symphony-bot`.
- **Unlinked accounts are refused** (the default, `HANGAR_RELAY_UNLINKED=refuse`). With `approve`, an unlinked account may only submit work, which waits for a trusted person's approval as an untrusted door's does; until the submit path holds such jobs, hangar refuses them under either setting.
- **Approvals.** `approve_job` and `reject_job` through the bot need a token minted with `--scope relay_approve` instead of `relay`. They then decide as the named person under the usual rule (an admin, a repo policy approver, or a front door's trust on a team job), and nobody approves their own work through a bot, even with `HANGAR_SELF_APPROVAL=allow_solo`.
- **A relay token does nothing on its own.** Without the header it is refused, it can't hold other scopes, and REST and the OAuth consent page don't accept it.

**Status:** `/mcp` and `/mcp/sandbox` honour the header: every request runs through `auth.resolve_delegation`, and a refused claim answers 403 with the reason (audited as `relay.refused`). `HANGAR_RELAY_UNLINKED=approve` is accepted but still refuses unlinked accounts until `submit_job` holds relayed jobs for approval, which comes with the change-set work. Per-call `_meta.on_behalf_of` isn't read; only the header is.

## ChatGPT, claude.ai and other OAuth clients

Hangar includes an OAuth 2.1 authorization server that follows the MCP authorization spec. Clients that can't send a static bearer token connect with it.

1. Make sure `HANGAR_PUBLIC_URL` is the https URL clients reach hangar at, for example `https://hangar.example.com`. OAuth turns itself off for plain-http hosts other than localhost.
2. In ChatGPT, go to **Settings → Connectors → Create** (developer mode). Set the MCP server URL to `https://hangar.example.com/mcp` and choose **OAuth** for authentication.
3. ChatGPT discovers hangar's metadata, registers itself, and opens hangar's approval page. Paste your hangar login token there (an admin's, or a teammate's own from `hangar person add`) and approve; the connector then acts as you.

Only apps whose redirect URL is on `HANGAR_OAUTH_REDIRECT_HOSTS` can register. The default list covers ChatGPT, claude.ai and localhost, which stops look-alike apps from phishing approvals.

The app then gets an access token (valid for 1 hour) and a refresh token (valid for 30 days, rotated on every use). It acts as the token you approved with, so revoking that hangar token also cuts off every app it approved. Approvals expire after 90 days (`HANGAR_OAUTH_GRANT_DAYS`), and replaying an old refresh token revokes the whole grant. Connected apps are listed in the dashboard under **Connect**, where you can revoke each one immediately.

Under the hood:

- Discovery: `/.well-known/oauth-protected-resource/mcp` (and `.../mcp/sandbox` for the sandbox server, with the same authorization server: a grant for either works on both) and `/.well-known/oauth-authorization-server`
- Endpoints: `/register` (RFC 7591), `/authorize` (PKCE S256 only), `/token`, `/revoke`
- `/mcp` returns `401` with a `resource_metadata` pointer, so clients find all of the above on their own.

Never expose hangar without auth. Its tools can run arbitrary commands in your sandboxes.

## Prompts that work well

- "List environments. Start a `claude-python` sandbox on github.com/me/api with work branch `agent/fix-timeouts`, run the tests, and show me the failures."
- "Fork that sandbox 3 times with branch prefix `attempt`, run the agent in each on the same task, then compare the diffs with `git diff main` in each."
- "Open a terminal session called `server` in sb-1234, start `uv run uvicorn app:app`, and wait for 'Application startup complete'."
