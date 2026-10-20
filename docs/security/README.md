# Security model

hangar exists to run untrusted, model-generated code. The design assumes any sandbox can be fully compromised by the
agent inside it, and asks one question of every mechanism: what does a compromised sandbox get? This page is the map.
Each mechanism has a reference page of its own, with the settings, the limits and the tests that prove it.

## The boundaries

| Boundary | Mechanism |
| --- | --- |
| Sandbox ↔ host | runc (laptop, trusted use), gVisor user-space kernel, or Kata/Firecracker microVM with its own guest kernel |
| Sandbox ↔ sandbox | Separate containers/pods/VMs; no shared volumes; per-sandbox credentials |
| Sandbox ↔ network | GKE: `deploy/k8s/sandbox-network.yaml` (and Agent Sandbox's managed policy) deny RFC1918, link-local (metadata server) and all ingress. Docker: hangar's own network, where sandboxes can't reach each other ([sandbox networking](docker-network.md)); the single-VM install also drops what they send to link-local addresses. Environments in `allowlist` or `none` mode reach the internet only through hangar's [egress proxy](egress.md), by hostname |
| Client ↔ control plane | Bearer tokens (hashed at rest), single-use tickets for browser streams, TLS via ingress, optional IAP in front |
| Secrets | Fernet-encrypted vault, with a [rotatable key](vault-key.md#rotating-the-vault-key); never in images, API responses, MCP results, logs, or audit entries |

## What a compromised sandbox does not get

The attacker controls everything inside one sandbox: the agent's process, its files, `~/.hangar/env` (so every command
hangar runs there), its git binary and its network traffic. Six things stay out of reach; the
[threat model](threat-model.md) lists every goal, what stops it and the test that checks it.

1. **The GitHub token.** With the [git credential gateway](git-gateway.md) the token stays in the control plane. A
   sandbox gets a grant that works only at hangar's gateway, only for its repo, only while it runs. Every job's sandbox
   holds a fetch-only grant: hangar pushes the commit it verified, after the secret scan and the landing checks, with a
   grant pinned to that ref and commit for minutes. Builds and validations hold nothing else.
2. **A host of its choosing.** In `allowlist` and `none` environments the network admits only the gateway and the
   [egress proxy](egress.md), with no DNS. The proxy checks each connection's host by name, reads the whole ClientHello
   and refuses Encrypted ClientHello; `pypi`, `npm` and `go` are served by hangar's read-only package mirror, so the
   registries' upload hosts stay closed. Approvals come from the owner, never from the agent.
3. **A secret in a branch, a PR or a thread.** Before a push, the change and its commit messages are
   [scanned](secret-scanning.md) for stored values, their encodings and high-signal token formats; a hit blocks the push
   and goes back to the agent with file and line, never the value. Every push through the gateway, the agent's own
   included, is scanned again from the pack. Results, events, artifacts and audit entries are
   [redacted](secret-scanning.md#redaction).
4. **A credential it can keep.** [Credentials](credentials.md) are written into a running sandbox at claim time, never
   into an image. Control-plane credentials (webhook secrets, the Slack token, GitHub App keys) never enter a sandbox by
   any path. Build secrets reach only the build step. A `cloud:` environment gets a per-sandbox OIDC token that the
   cloud exchanges for short-lived credentials; no cloud key is stored. Subscription logins go only into sandboxes a
   person starts for themselves.
5. **Its own work landed.** Once an agent has read untrusted content (a ticket, a web page, an untrusted MCP server),
   the turn is [tainted](taint.md) for good: its PR opens as a draft labelled `hangar:tainted`, a second person must
   approve it to land, and egress requests need an admin or a repo approver. One approval rule holds on every path, and
   never the job's own person or requester. Intake jobs run under [posture](front-doors.md#intake-posture) `enforce`:
   no gateway, no job; at least `allowlist` egress; no sudo.
6. **The grade of its own trial.** An experiment's [grader](graders.md) runs sealed: egress `none` with no host at
   all, no credential, its git grant revoked before the patch's code runs, hidden tests from a pristine copy, and the
   grade written by hangar's own user where the sandbox has the uid split.

Shared infrastructure gets the same treatment: [build caches](build-caches.md) are written only by a repo's own builds
and sealed with the vault key; the [agent's screen](screen.md) is reached only through hangar; the
[vault key ring](vault-key.md) encrypts everything at rest and can be rotated.

## Who may do what

One hangar serves a team. Every token belongs to a [person](people.md), an admin or a member; members see and act only
on their own work, and OAuth connectors reach `/mcp` only, for at most 90 days. Work enters through
[seven front doors](front-doors.md) (REST, MCP, A2A, GitHub, Slack, signed webhooks, schedules); each decides who may
start work, and all of them end in the same job service with the same rate limits, caps and audit.

## What is not covered

Docker on a laptop or VM is a trusted-use setup: sandboxes can reach the host, so use GKE (gVisor, Kata) for untrusted
work. Inside the sandbox the agent is in charge of what hangar reads and writes there. Without the gateway a job's
sandbox holds the environment's git token, and landing never lands such a job. Redaction and the scan recognize stored
values and the encodings they know, not every transformation, and a direct-mode push isn't scanned at all. In `open`
environments the public internet is reachable; in the others, data can still go *to* an allowed host, and a CDN that
doesn't check `Host` against SNI reaches any other site there. The full list, with the gaps in graders, custom branch
names, E2B command output and idle connections, is on the [threat model](not-covered.md).

## Reference pages

| Page | What it covers |
| --- | --- |
| [A compromised sandbox: the threat model](threat-model.md) | Every goal an attacker inside a sandbox could have, what stops it and the test |
| [What is not covered](not-covered.md) | The gaps, stated plainly |
| [The git credential gateway](git-gateway.md) | Grants, turning it on, fetch-only jobs and hangar's delivery push, the GitHub App, what it doesn't cover |
| [Egress allowlists](egress.md) | Hostname allowlists, the proxy, what egress doesn't stop |
| [Package mirrors](package-mirrors.md) | Read-only PyPI, npm and Go through the gateway |
| [Sandbox networking on Docker](docker-network.md) | hangar's own network, where no sandbox reaches another |
| [Secret scanning and redaction](secret-scanning.md) | The pre-push scan, the gateway's pack scan, what redaction masks and what it can't |
| [Credentials in sandboxes](credentials.md) | Delivery, forks, control-plane credentials, the model gateway, build secrets, short-lived cloud credentials |
| [Taint tracking](taint.md) | What taints a turn, the gates that read it, injection canaries |
| [Front-door security](front-doors.md) | Who starts work through each door, intake posture, signed requests, SSRF, scopes, limits, audit |
| [People and OAuth connectors](people.md) | Admins and members, what each token can do, OAuth limits |
| [Sealed graders](graders.md) | The grader's sandbox, hidden tests, the grade, detectors |
| [Build caches as shared infrastructure](build-caches.md) | Who writes a cache, sealing, what a poisoned entry can and can't do |
| [Screens, previews and the agent's requests](screen.md) | VNC through hangar, tickets, env requests |
| [The vault key ring](vault-key.md) | Where the keys live, rotation, backups |

The design record is [spec 0003, sandbox security](../specs/0003-sandbox-security.md).

## Operational advice

- Keep the control plane private: localhost, an IAP tunnel, Tailscale, or IAP on the GKE ingress.
- Use a separate token per client so you can revoke one without affecting the rest. The audit log records which token did what. `hangar connect` does this for you: each client on each machine gets its own token with the `mcp` scope, which works at `/mcp` and nowhere else. A leaked client config can then drive sandboxes, but it can't call the REST API to read or change the vault, settings or tokens, or approve OAuth apps, and it can't map vault secrets into an environment. Sandboxes it starts can still use the stored credentials meant for sandboxes (never the control plane's own), so revoke a leaked token at once (`hangar token revoke`).
- Give E2B SDK clients tokens with the `sandboxes` scope, which work only on the [E2B-compatible API](../e2b.md) and its sandbox URLs, for the sandboxes the token created. Their sandboxes get no stored credentials unless the environment is in `HANGAR_E2B_TRUSTED_ENVIRONMENTS`.
- Settings stored with `hangar config` can't hold secrets: hangar refuses the admin token, the vault key and provider keys there. Settings that point at secrets name a vault credential instead.
- Set `HANGAR_SECRET_KEY` explicitly in clusters, from Secret Manager or a Kubernetes Secret, so the vault survives pod restarts. Keep the key in Secret Manager, never in a file on a laptop, and [rotate it](vault-key.md#rotating-the-vault-key).
