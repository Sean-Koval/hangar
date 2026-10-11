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

## ChatGPT (developer mode connectors)

ChatGPT custom connectors support OAuth or no authentication. They don't accept a static bearer token. You have two options:

1. **Put an OAuth-capable proxy in front of `/mcp`.** For example, use Google IAP or Cloudflare Access with an OAuth app, or an MCP auth gateway. Have it forward requests to hangar with `Authorization: Bearer <hangar token>` injected.
2. **Use Codex or Claude Code as the orchestrator,** which work with bearer tokens today.

Never expose hangar without auth. Its tools can run arbitrary commands in your sandboxes.

## Prompts that work well

- "List environments. Start a `claude-python` sandbox on github.com/me/api with work branch `agent/fix-timeouts`, run the tests, and show me the failures."
- "Fork that sandbox 3 times with branch prefix `attempt`, run the agent in each on the same task, then compare the diffs with `git diff main` in each."
- "Open a terminal session called `server` in sb-1234, start `uv run uvicorn app:app`, and wait for 'Application startup complete'."
