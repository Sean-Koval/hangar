# Single sign-on with IAP

How to put Google's Identity-Aware Proxy in front of the console, and which paths stay outside it. For admins on
GKE. The OpenID Connect mode and what SSO changes are on [Single sign-on](sso.md).

## How hangar uses IAP

With `HANGAR_SSO_MODE=iap`, IAP signs every request it lets through with the `x-goog-iap-jwt-assertion` header.
hangar checks that header's ES256 signature against Google's IAP keys. It also checks the issuer and the backend
service's audience.

The console's sign-in reads the email from the header. It maps to a hangar person as in OIDC mode. hangar then sets
the same Strict session cookie.

Every later request with that cookie must carry a valid assertion for the same person. A cookie without IAP is
refused. So is a cookie for one person under another person's IAP sign-in.

```bash
HANGAR_SSO_MODE=iap
HANGAR_SSO_IAP_AUDIENCE=/projects/<project number>/global/backendServices/<backend service id>
```

## Paths that stay outside IAP

IAP asks for a Google login, which only a browser can give. Put these paths on a second backend without IAP:

| Path | Who calls it |
| --- | --- |
| `/api/hooks/*` | GitHub, Slack, Jira and signed webhooks |
| `/api/governance/events` | Tower's approval events |
| `/mcp`, `/mcp/sandbox` | MCP clients, with hangar tokens |
| `/oauth/*`, `/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke` | OAuth for MCP connectors |
| `/api/*` with a bearer token | The CLI and job callers, with hangar tokens |
| `/auth/sso/cli/token` | `hangar login --sso`, trading its one-use code |
| `/git/*` | The git gateway, when it shares the main port |

The console's paths go through IAP: `/`, `/ui/*` and `/auth/sso/login`. So does the console's own use of `/api/*`.

`/api/*` is on both sides, so paths alone can't split it. Route by header at the load balancer: a request with an
`Authorization` header goes to the backend without IAP. Everything else goes through IAP.

A request that skips IAP gets no session from a cookie. It still needs a hangar token, as without IAP.

## Deprovisioning with IAP

IAP stops a removed person at the console at once. IAP sends no refresh token, so hangar can't ask Google about
someone later.

Instead, each console visit through IAP confirms the person again. Their CLI and MCP tokens stop after
`HANGAR_SSO_MAX_STALE_MINUTES` without one. Raise it for people who mostly use the CLI. That trades a longer delay
after someone leaves.

For deprovisioning without console visits, use the OIDC mode.
