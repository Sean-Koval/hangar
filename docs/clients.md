# Connecting MCP clients

Hangar serves MCP over streamable HTTP at `<public_url>/mcp`, authenticated with a bearer token. Create a token in the dashboard under **Connect**, or with `hangar token create <name>`.

## Codex (CLI, IDE extension, app)

```toml
# ~/.codex/config.toml
[mcp_servers.hangar]
url = "https://hangar.example.com/mcp"
bearer_token_env_var = "HANGAR_TOKEN"   # Codex rejects inline tokens for HTTP servers
tool_timeout_sec = 900                  # builds and forks can take minutes
startup_timeout_sec = 20
```

Or run `codex mcp add hangar --url https://hangar.example.com/mcp --bearer-token-env-var HANGAR_TOKEN`.

## Claude Code

```bash
claude mcp add --transport http hangar https://hangar.example.com/mcp \
  --header "Authorization: Bearer $HANGAR_TOKEN"
```

## ChatGPT, claude.ai and other OAuth clients

Hangar includes an OAuth 2.1 authorization server that follows the MCP authorization spec. Clients that can't send a static bearer token connect with it.

1. Make sure `HANGAR_PUBLIC_URL` is the https URL clients reach hangar at, for example `https://hangar.example.com`. OAuth turns itself off for plain-http hosts other than localhost.
2. In ChatGPT, go to **Settings → Connectors → Create** (developer mode). Set the MCP server URL to `https://hangar.example.com/mcp` and choose **OAuth** for authentication.
3. ChatGPT discovers hangar's metadata, registers itself, and opens hangar's approval page. Paste a hangar API token there and approve.

Only apps whose redirect URL is on `HANGAR_OAUTH_REDIRECT_HOSTS` can register. The default list covers ChatGPT, claude.ai and localhost, which stops look-alike apps from phishing approvals.

The app then gets an access token (valid for 1 hour) and a refresh token (valid for 30 days, rotated on every use). It acts as the token you approved with, so revoking that hangar token also cuts off every app it approved. Approvals expire after 90 days (`HANGAR_OAUTH_GRANT_DAYS`), and replaying an old refresh token revokes the whole grant. Connected apps are listed in the dashboard under **Connect**, where you can revoke each one immediately.

Under the hood:

- Discovery: `/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-authorization-server`
- Endpoints: `/register` (RFC 7591), `/authorize` (PKCE S256 only), `/token`, `/revoke`
- `/mcp` returns `401` with a `resource_metadata` pointer, so clients find all of the above on their own.

Never expose hangar without auth. Its tools can run arbitrary commands in your sandboxes.

## Prompts that work well

- "List environments. Start a `claude-python` sandbox on github.com/me/api with work branch `agent/fix-timeouts`, run the tests, and show me the failures."
- "Fork that sandbox 3 times with branch prefix `attempt`, run the agent in each on the same task, then compare the diffs with `git diff main` in each."
- "Open a terminal session called `server` in sb-1234, start `uv run uvicorn app:app`, and wait for 'Application startup complete'."
