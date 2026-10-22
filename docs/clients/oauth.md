# ChatGPT, claude.ai and other OAuth clients

hangar includes an OAuth 2.1 authorization server that follows the MCP authorization spec. Clients that can't send a
static bearer token connect with it. Clients that can are on [connecting MCP clients](../clients.md).

## Connecting one

1. Make sure `HANGAR_PUBLIC_URL` is the https URL clients reach hangar at, for example `https://hangar.example.com`.
   OAuth turns itself off for plain-http hosts other than localhost.
2. In ChatGPT, go to **Settings → Connectors → Create** (developer mode). Set the MCP server URL to
   `https://hangar.example.com/mcp` and choose **OAuth** for authentication.
3. ChatGPT discovers hangar's metadata, registers itself, and opens hangar's approval page. Paste your hangar login
   token there (an admin's, or a teammate's own from `hangar person add`) and approve. The connector then acts as you.

Only apps whose redirect URL is on `HANGAR_OAUTH_REDIRECT_HOSTS` can register. The default list covers ChatGPT,
claude.ai and localhost, which stops look-alike apps from phishing approvals.

## What the app holds

The app then gets an access token (valid for 1 hour) and a refresh token (valid for 30 days, rotated on every use).
It acts as the token you approved with, so revoking that hangar token also cuts off every app it approved.

Approvals expire after 90 days (`HANGAR_OAUTH_GRANT_DAYS`), and replaying an old refresh token revokes the whole
grant. Connected apps are listed in the dashboard under **Connect**, where you can revoke each one immediately.

## Under the hood

- Discovery: `/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-authorization-server`. The sandbox
  server has `.../mcp/sandbox`, with the same authorization server: a grant for either works on both.
- Endpoints: `/register` (RFC 7591), `/authorize` (PKCE S256 only), `/token`, `/revoke`
- `/mcp` returns `401` with a `resource_metadata` pointer, so clients find all of the above on their own.

Never expose hangar without auth. Its tools can run arbitrary commands in your sandboxes.
