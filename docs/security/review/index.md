# Reviewing hangar for production

This section is for the SRE or security engineer who has to sign off on hangar before a team depends on it. It
gathers, in one place, what hangar is, what it stores, which controls protect it, and what is still open. Every claim
links to the page or the code that backs it.

## What you are reviewing

hangar is a control plane that runs coding agents in sandboxes. A person or a ticket asks for work and an agent does
it in a sandbox. hangar delivers the result as a pull request for review. The design assumes the agent can
compromise its own sandbox.

The [security model](../README.md) asks one question of every mechanism: what does a compromised sandbox get?

The production shape is GKE: one control-plane pod, sandboxes in gVisor or Kata pods, a git gateway and an egress
proxy. [Running hangar on GKE](../../gke.md) is the install, and `deploy/terraform/gke/` is the infrastructure.

## What hangar stores, and where

| Data | Where | Protection |
| --- | --- | --- |
| Jobs, sessions, schedules, settings, the audit log | One SQLite file on the `hangar-data` persistent disk | Disk encryption by Google; the pod's service account only |
| Tokens | The same database | SHA-256 hashes only; the token is shown once ([auth.py](https://github.com/Sean-Koval/hangar/blob/main/src/hangar/auth.py)) |
| Stored credentials (the vault) | The same database | Fernet with a rotatable key ring ([the vault key](../vault-key.md)) |
| Checkpoints, artifacts, database backups | The session-store bucket | Fernet-encrypted objects; 14 daily backups ([backups](../../gke.md#7-backups-and-restore)) |
| Finished runs, for flightdeck | The run-capture bucket, opt-in | Redacted before upload ([run capture](../../api/outbox.md)) |
| The vault key | Secret Manager, with a copy in the `hangar` Kubernetes Secret | Access-logged; operators who rotate keys only |
| Logs and SLO events | Cloud Logging, from the pod's stdout | No secrets in logs, by design and by redaction |

Beyond these, sandboxes hold clones of the repositories hangar works on. The database and buckets hold the prompts,
ticket text and agent output of each run.

## How to read this section

- [Controls and evidence](controls.md) maps the questions a review asks to the control and its proof.
- [Open findings and accepted risks](risks.md) lists what is not fixed, who owns it and why it is acceptable.
- [Gaps found in the 1.0 review](../review-1.0.md) is the independent review's own record, finding by finding.
- [What is not covered](../not-covered.md) states the limits of the design, which no fix will remove.

The threat model is [a compromised sandbox](../threat-model.md). Each goal there names its test, so you can run the
proof rather than read it. Operations are covered by the [SLOs](../../slos.md), the [alerts](../../alerting.md) and
the [runbooks](../../runbooks/README.md).

## The decisions still open

Three choices change what a reviewer will accept. They are Sean's to make before 1.0.

1. **Egress on GKE defaults to `open`.** Making `allowlist` the default stops data reaching arbitrary hosts, but jobs
   that install from the internet need their hosts listed (KAN-174, L-9).
2. **The warn switches.** The live deployment runs `HANGAR_SUBSCRIPTION_LOGINS=any`, so jobs use a person's ChatGPT
   login. The vendor's terms don't allow that for automated work. KAN-74 removes both switches; it needs API keys first.
3. **Custom session branches** can be created by another sandbox before the session exists. Closing it breaks
   continuing a branch you pushed yourself (known gap 8).
