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
- Settings stored with `hangar config` can't hold secrets: hangar refuses the admin token, the vault key and provider
  keys there. Settings that point at secrets name a vault credential instead.
- Set `HANGAR_SECRET_KEY` explicitly in clusters, from Secret Manager or a Kubernetes Secret, so the vault survives pod
  restarts. Keep the key in Secret Manager, never in a file on a laptop, and
  [rotate it](vault-key.md#rotating-the-vault-key).

## Vulnerability management

hangar's CI scans its own code, dependencies and image. Each scan runs on every pull request, and on main and nightly
in [`security.yml`](https://github.com/Sean-Koval/hangar/blob/main/.github/workflows/security.yml).

| Scan | Tool | What it does |
|---|---|---|
| Dependencies | osv-scanner on `uv.lock` | Fails on a High or Critical finding that has a fix |
| Image | Trivy on the control-plane image | Also scans main's builds and each release; reports to code scanning |
| Secrets | gitleaks | Scans a pull request's commits; the whole history nightly |
| Code | CodeQL | Analyses the Python and the web console's JavaScript |

Results go to GitHub code scanning. Dependabot proposes weekly updates for uv, Docker and Actions.

Sean Koval, the maintainer, triages every finding. Each is fixed within its severity's deadline:

| Severity | Fixed within |
|---|---|
| Critical | 7 days |
| High | 30 days |
| Medium | 90 days |
| Low | the next release |

A finding left in place is recorded with its reason, in `osv-scanner.toml`, `.trivyignore` or `.gitleaksignore`.
