# Single sign-on

How people sign in to hangar through the team's identity provider, and how leaving the provider ends their access.
For admins setting up hangar at a company. IAP is the other mode: [Single sign-on with IAP](sso-iap.md).

SSO is off by default. With it off, people paste a token, as before.

## What changes with SSO on

- **The console** sends people to the provider, with PKCE. hangar checks the id token's signature, issuer, audience,
  expiry and nonce.
- **The session** is a cookie: `__Host-`, HttpOnly, Secure, SameSite=Strict. It lasts `HANGAR_SSO_SESSION_MINUTES`
  (60). No token is kept in the browser's storage.
- **The cookie's value is no token.** hangar derives the session's bearer from it with a key only the server holds.
  Copied out of the browser and sent as `Authorization: Bearer`, it is refused.
- **The CLI** gets its token with `hangar login <url> --sso`. A browser signs in and asks the person to confirm
  that they ran the command. A one-use code goes back to the CLI on `127.0.0.1`, which prints whom it signed in.
- **Without a browser**, add `--no-browser` and paste the code the page shows. Never confirm a sign-in you didn't
  start: whoever sent the link would get your token.
- **Changes from the console** must carry its own header and come from the same origin, on top of SameSite=Strict.

## Who gets in

A person's first sign-in needs an email the provider vouches for: `email_verified`, or Entra's `xms_edov`. That
email maps to a hangar person. Link it with `hangar person link <id> email <address>`.

The first sign-in binds the person to the provider's account id. Later sign-ins find them by that id. They end once
their email is unlinked or linked to someone else. Another account with the same email is refused. An admin clears a binding with
`DELETE /api/sso/bindings/<person>`.

A refused or failed sign-in shows the person only a reference. The reason is in the audit log under it, as
`sso.refused` or `sso.unavailable`.

An email linked to nobody is refused. `HANGAR_SSO_DEFAULT_ROLE=member` makes it a new member instead. That needs
`HANGAR_SSO_ALLOWED_DOMAINS`, so not every account the provider knows gets in.

`HANGAR_SSO_REQUIRED_GROUP` admits only that group's members, from the id token's `HANGAR_SSO_GROUP_CLAIM`
(`groups`).

With Google, the account's Workspace domain (`hd`) must be an allowed domain too. A personal Google account made
with a work address has none.

## Leaving the provider

hangar asks the provider about each signed-in person every `HANGAR_SSO_RECHECK_MINUTES` (10). It uses their
refresh token, then checks the email, domain and group again.

When the provider refuses the refresh (`invalid_grant`), or the person left the group, hangar disables them. Every token of theirs is
revoked, the CLI's included. No admin needs to run `hangar person remove`. Their work stays, visible to admins.

A provider hangar can't reach disables nobody. But a person not confirmed for `HANGAR_SSO_MAX_STALE_MINUTES` (60)
can't use their own tokens until it answers again. Their `jobs` and `metrics` tokens ride out an outage for
`HANGAR_SSO_SERVICE_STALE_HOURS` (24), then stop too.

A pass that would disable more than a fifth of the team, and more than one person, looks like a broken client
registration. It revokes nothing, suspends those people instead, and `hangar doctor` fails. A clean pass or a
sign-in lifts it.

Past that grace, hangar suspends them too. A disabled or suspended person gets nothing through the relay, Tower
passports, Slack, GitHub or Jira. Nor does anyone who never signed in.

The tokens of people who never signed in through SSO stop too. `HANGAR_SSO_REQUIRED=false` opts out during a
rollout. List service people, such as a CI bot, in `HANGAR_SSO_EXEMPT_PEOPLE`.

## Break-glass

The server's admin token and the bootstrap token, matched by its id, are never gated. An admin can still get in while the provider is
down. The console keeps a pasted token in the tab's memory only.

Each use is audited as `sso.break_glass`, at most once an hour. `hangar doctor` warns for a week after one. Rotate a
break-glass token once it was used.

## Turn it on

Register hangar as a web application at the provider. Its redirect URI is `<HANGAR_PUBLIC_URL>/auth/sso/callback`,
which must be https. Store the client secret in the vault, then set:

```bash
HANGAR_SSO_MODE=oidc
HANGAR_SSO_ISSUER=https://accounts.google.com
HANGAR_SSO_CLIENT_ID=<client id>
HANGAR_SSO_CLIENT_SECRET_CREDENTIAL=sso-client-secret
HANGAR_SSO_ALLOWED_DOMAINS=example.com
```

hangar refuses to start with a setting that would let in too much. `hangar doctor` checks the provider answers.

| Provider | Issuer | Also set |
| --- | --- | --- |
| Google Workspace | `https://accounts.google.com` | `HANGAR_SSO_AUTH_PARAMS=access_type=offline&prompt=consent`, for refresh tokens. List every Workspace domain. Google sends no group claim: limit the app to a group in the Admin console instead. |
| Okta | `https://<org>.okta.com/oauth2/default` | `HANGAR_SSO_SCOPES=openid email profile offline_access groups` |
| Entra ID | `https://login.microsoftonline.com/<tenant>/v2.0` | `offline_access` in the scopes. Turn on the `xms_edov` optional claim: a first sign-in needs it. Entra sends no `email_verified`, so set `HANGAR_SSO_REQUIRE_EMAIL_VERIFIED=false` with allowed domains and a single-tenant issuer. |

`hangar doctor` fails when someone has no refresh token. hangar then can't learn that they left, and their tokens
stop after the stale limits.
