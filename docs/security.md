# Security model

Hangar exists to run untrusted, model-generated code. The design assumes any sandbox can be fully compromised by the agent inside it.

| Boundary | Mechanism |
| --- | --- |
| Sandbox ↔ host | runc (laptop, trusted use), gVisor user-space kernel, or Kata/Firecracker microVM with its own guest kernel |
| Sandbox ↔ sandbox | Separate containers/pods/VMs; no shared volumes; per-sandbox credentials |
| Sandbox ↔ network | GKE: default-deny to RFC1918 + metadata server. Docker: use `HANGAR_DOCKER_NETWORK` with an egress-filtered network |
| Client ↔ control plane | Bearer tokens (hashed at rest), single-use tickets for browser streams, TLS via ingress, optional IAP in front |
| Secrets | Fernet-encrypted vault; never in images, API responses, MCP results, logs, or audit entries |

## Credential handling

- **Delivery.** Secrets are written into a running sandbox at claim time, to `~/.hangar/env` (mode 0600) or to the harness's own file. Sandbox images never contain them.
- **Forks.** A fork scrubs inherited secret files before re-injecting only the credentials it was granted.
- **Exclusive credentials** are ones with rotating refresh tokens, like a Codex ChatGPT login. Each is leased to one sandbox. When that sandbox stops, the possibly rotated file is copied back into the vault.
- **Snapshots** of a running sandbox (Docker `commit`) may contain secrets that were present at snapshot time. They stay in the local image store and are never pushed.
- **Exposure inside the sandbox.** Anything you inject is readable by the agent in that sandbox, and by any MCP client holding a hangar token (`exec`/`read_file` can read `~/.hangar/env`). Prefer scoped credentials: fine-grained GitHub tokens limited to the target repo, and API keys with spend limits.
- **Forks** run `tmux kill-server` before scrubbing secret files, so shells that exported the parent's secrets don't survive into the copy. A full memory snapshot (E2B) still carries any secrets held in other long-running processes' memory, so fork from a clean point when it matters.
- **Audit redaction.** Commands, prompts and terminal input are recorded in the audit log with stored credential values masked as `***`. Secrets that were never stored in the vault can't be recognized, so don't paste unrelated secrets into agent terminals.
- **Browser streams.** The dashboard never puts its token in a URL. Websocket terminals and build-log streams use single-use tickets that expire after 60 seconds.

## OAuth connectors

- **Scope:** OAuth access tokens reach `/mcp` only, never the REST API or the dashboard.
- **Lifetime:** every approval is tied to the hangar token that approved it and stops working when that token is revoked or the app is disconnected. Approvals last at most 90 days.
- **Registration:** limited to allowlisted redirect hosts and capped in number.
- **Consent page:** can't be framed, and isn't cached.

## Operational advice

- Keep the control plane private: localhost, an IAP tunnel, Tailscale, or IAP on the GKE ingress.
- Use a separate token per client so you can revoke one without affecting the rest. The audit log records which token did what.
- Set `HANGAR_SECRET_KEY` explicitly in clusters, from Secret Manager or a Kubernetes Secret, so the vault survives pod restarts.
