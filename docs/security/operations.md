# Running hangar safely

Operational advice for whoever runs a hangar: where the control plane lives, how tokens are split, and where the
vault key is kept. It is part of the [security model](README.md).

- Keep the control plane private: localhost, an IAP tunnel, Tailscale, or IAP on the GKE ingress.
- Use a separate token per client so you can revoke one without affecting the rest. The audit log records which token
  did what. `hangar connect` does this for you: each client on each machine gets its own token with the `mcp` scope,
  which works at `/mcp` and nowhere else.
- A leaked client config can then drive sandboxes, but it can't call the REST API to read or change the vault,
  settings or tokens. It can't approve OAuth apps, and it can't map vault secrets into an environment.
- Sandboxes a leaked token starts can still use the stored credentials meant for sandboxes (never the control plane's
  own). So revoke a leaked token at once (`hangar token revoke`).
- Give E2B SDK clients tokens with the `sandboxes` scope. They work only on the [E2B-compatible API](../e2b.md) and
  its sandbox URLs, for the sandboxes the token created. Their sandboxes get no stored credentials unless the
  environment is in `HANGAR_E2B_TRUSTED_ENVIRONMENTS`.
- Settings stored with `hangar config` can't hold secrets: hangar refuses the admin token, the vault key and provider
  keys there. Settings that point at secrets name a vault credential instead.
- Set `HANGAR_SECRET_KEY` explicitly in clusters, from Secret Manager or a Kubernetes Secret, so the vault survives pod
  restarts. Keep the key in Secret Manager, never in a file on a laptop, and
  [rotate it](vault-key.md#rotating-the-vault-key).
