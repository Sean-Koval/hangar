# Security model

hangar exists to run untrusted, model-generated code. The design assumes the agent inside any sandbox can fully
compromise it. It asks one question of every mechanism: what does a compromised sandbox get? This page is the map.
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

The attacker controls everything inside one sandbox. Six things stay out of its reach, each with
[its own section](compromised-sandbox.md):

1. [the GitHub token](compromised-sandbox.md#1-the-github-token);
2. [a host of its choosing](compromised-sandbox.md#2-a-host-of-its-choosing);
3. [a secret in a branch, a PR or a thread](compromised-sandbox.md#3-a-secret-in-a-branch-a-pr-or-a-thread);
4. [a credential it can keep](compromised-sandbox.md#4-a-credential-it-can-keep);
5. [its own work landed](compromised-sandbox.md#5-its-own-work-landed);
6. [the grade of its own trial](compromised-sandbox.md#6-the-grade-of-its-own-trial).

Build caches, the agent's screen and the vault key ring get the
[same treatment](compromised-sandbox.md#shared-infrastructure).

## Who may do what

One hangar serves a team. Every token belongs to a [person](people.md), an admin or a member. Members see and act only
on their own work, and OAuth connectors reach `/mcp` only, for at most 90 days.

Work enters through [seven front doors](front-doors.md): REST, MCP, A2A, GitHub, Slack, signed webhooks and schedules.
Each decides who may start work. All of them end in the same job service with the same rate limits, caps and audit.

## What is not covered

- Docker on a laptop or VM is a trusted-use setup: sandboxes can reach the host, so use GKE (gVisor, Kata) for
  untrusted work.
- Inside the sandbox the agent is in charge of what hangar reads and writes there.
- Without the gateway a job's sandbox holds the environment's git token, and landing never lands such a job.
- Redaction and the scan recognize stored values and the encodings they know, not every transformation. A
  direct-mode push isn't scanned at all.
- In `open` environments the public internet is reachable. In the others, data can still go *to* an allowed host. A
  CDN that doesn't check `Host` against SNI reaches any other site there.

The full list is on [what is not covered](not-covered.md). It adds the gaps in graders, custom branch names, E2B
command output and idle connections.

## Reference pages

| Page | What it covers |
| --- | --- |
| [What a compromised sandbox does not get](compromised-sandbox.md) | The six things out of an attacker's reach, one section each |
| [How the GKE cluster is hardened](gke-cluster.md) | Cluster settings, the CIS GKE mapping, Binary Authorization, changes to an existing cluster |
| [Running hangar safely](operations.md) | Operational advice: a private control plane, a token per client, the vault key |
| [A compromised sandbox: the threat model](threat-model.md) | Every goal an attacker inside a sandbox could have, what stops it and the test |
| [What is not covered](not-covered.md) | The gaps, stated plainly |
| [Gaps found in the 1.0 review](review-1.0.md) | What the independent review found, until each fix lands |
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

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="operational-advice"></span>Operational advice | [Running hangar safely](operations.md) |
