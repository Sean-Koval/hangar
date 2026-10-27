# Runbook: a credential leak

When a secret hangar holds, or one an agent touched, may be outside where it belongs. That includes a token in a log, a commit, a PR, a Slack message or a sandbox it shouldn't reach. There is no alert for this: someone reports it, or the secret scanner stops a push. Speed matters more than order, but revoke first.

## 1. Revoke it at the source

Do this before anything else, even before telling anyone. A revoked secret is harmless wherever it went.

| What leaked | Revoke it |
| --- | --- |
| A provider API key or login (Anthropic, OpenAI, Google) | In the provider's console. Then `hangar cred add` the new one under the same name |
| A GitHub token or App key | In GitHub's settings. For the App, generate a new private key and delete the old one |
| A hangar API token | `hangar token revoke <name>`, then mint a new one for its owner |
| A webhook or Slack signing secret | In the sender's settings, then update the vault credential |
| hangar's OIDC signing key | `POST /api/cloud/rotate?drop_old=true` ([short-lived cloud credentials](../security/credentials.md#short-lived-cloud-credentials)) |
| The vault key | `hangar admin rotate-key` ([the vault key ring](../security/vault-key.md)) |
| Tower's service key, webhook secret or Slack tokens | Tower's [rotate a secret](https://sean-koval.github.io/tower/deploy-backup/#rotate-a-secret) |

`hangar cred ls` lists every credential, and marks the control-plane ones that never enter a sandbox.

## 2. Find where it went

```bash
hangar job events <id>                 # a job whose change held it: secret_found names file and line
kubectl -n hangar logs deploy/hangar --since=24h | grep -c '<first 6 characters of the secret>'
```

Search the console's audit (`GET /api/audit`) for `secret.found`, `cred.*` and `cloud.token` around the time. A secret the scanner caught was never pushed ([secret scanning](../security/secret-scanning.md)). A secret in a pushed commit stays in the remote's history. Rewrite the branch, and for a public repo ask GitHub support to purge cached views.

## 3. Close the path

Work out how it got out, then shut that path:

- **Into a sandbox it shouldn't reach.** Mark it control-plane: `hangar cred exposure <name> control_plane`.
- **Into a log or an event.** hangar redacts the values it knows. A value it didn't know is a bug: open a KAN ticket with where it appeared, never the value.
- **Through egress.** Check the sandbox's egress mode and the `egress.deny` audit ([egress](../security/egress.md)).

## 4. Tell people and record it

Tell the credential's owner and Sean. Open a KAN ticket with the timeline: when it leaked, when it was revoked, and where it went. Never put the secret, or any part of it, in the ticket, Slack or a commit.
