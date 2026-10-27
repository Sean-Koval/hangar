# Controls and evidence

This page answers the questions an SRE or security review asks of hangar, one area at a time. Each row names the
control, its status and where to check it. "Open" rows link the Jira ticket that closes them. Start from
[Reviewing hangar for production](index.md) for what hangar is and stores.

Status reads **In place** (shipped and tested), **Partial** (shipped with a stated gap) or **Open** (tracked, not yet
shipped).

## Identity and access

| Control | Status | Evidence |
| --- | --- | --- |
| Every token belongs to a person, an admin or a member; members act only on their own work | In place | [People and OAuth connectors](../people.md) |
| Tokens are random 32-byte secrets, stored as SHA-256 hashes, shown once, revocable one by one | In place | `src/hangar/auth.py`; [Running hangar safely](../operations.md) |
| Scoped tokens: `mcp`, `jobs`, `metrics` and `sandboxes` each reach one surface | In place | [Scopes](../front-doors.md#scopes) |
| OAuth 2.1 for MCP clients, with consent, PKCE and a 90-day limit | In place | [OAuth connectors](../people.md#oauth-connectors) |
| Approvals: an admin or a policy approver, never on their own job | In place | [Who may approve](../../triggers.md#who-may-approve) |
| The console keeps the admin token in browser storage, with no CSP | Open | KAN-174, L-7 |
| IAP in front of the GKE ingress | Open | KAN-174, L-8; the API is public with token auth today |

## Secrets and encryption

| Control | Status | Evidence |
| --- | --- | --- |
| The vault, checkpoints, artifacts and backups are Fernet-encrypted with one rotatable key ring | In place | [The vault key ring](../vault-key.md) |
| The vault key lives in Secret Manager, outside the cluster | Partial | [Where the keys live](../vault-key.md#where-the-keys-live); `up.sh` keeps only the Kubernetes Secret, so copying it is an operator step |
| Secrets never reach images, API responses, MCP results, logs or audit entries | In place | [Boundaries](../README.md#the-boundaries); [redaction](../secret-scanning.md#redaction) |
| A sandbox never holds the GitHub token; the gateway grants per-sandbox, short-lived access | In place | [The git credential gateway](../git-gateway.md) |
| TLS at the edge with a Google-managed certificate | In place | `deploy/k8s/ingress.yaml` |
| TLS between pods (git grants cross nodes in plain HTTP) | Open | KAN-180 turns on inter-node encryption (L-10) |
| Kubernetes Secrets encrypted with a Cloud KMS key | Open | KAN-180 |

## Isolation and network

| Control | Status | Evidence |
| --- | --- | --- |
| Sandboxes run in gVisor pods on GKE (Kata where the cluster offers it) | In place | [The boundaries](../README.md#the-boundaries); [GKE](../../gke.md) |
| Sandboxes can't reach RFC1918, link-local or each other; all ingress denied | In place | `deploy/k8s/sandbox-network.yaml` |
| Egress by hostname allowlist through hangar's proxy, in `allowlist` and `none` environments | In place | [Egress allowlists](../egress.md) |
| Intake jobs (GitHub, Jira, Slack, webhooks) get at least `allowlist` and the gateway | In place | [Intake posture](../front-doors.md#intake-posture) |
| GKE's default egress is `open` | Open | A decision: KAN-174, L-9 |
| Approvals inside a turn bind the agent on GKE (no uid split) | Partial | `required` refuses such turns on GKE; the sidecar is KAN-83 |
| The control-plane pod: restricted security context and an ingress NetworkPolicy | Open | KAN-174, L-8 |

## Supply chain and change

| Control | Status | Evidence |
| --- | --- | --- |
| The image installs from `uv.lock` with hashes, every base image pinned by digest, non-root | In place | KAN-170; `tests/test_image_supply_chain.py` |
| Every GitHub Action pinned by commit SHA | In place | KAN-171; `tests/test_workflow_pins.py` |
| Tagged releases carry an SBOM and build provenance | In place | `.github/workflows/release.yml` |
| Dependency, image, secret and static-analysis scanning in CI | Open | KAN-179 |
| Deploys need the owner's approval of the `production` environment, and roll back on failure | In place | [Deploying from CI/CD](../../cicd.md); [rollback](../../cicd.md#4-when-it-fails-it-rolls-back) |
| hangar's Google identity has no project-wide storage or registry role | In place | KAN-172; [GKE](../../gke.md) |
| Agent work merges only through branch protection and a person's review | In place | [Landing](../../landing.md) |

## Audit, monitoring and response

| Control | Status | Evidence |
| --- | --- | --- |
| Every approval decision and every refused request is in the audit log | In place | [Audit](../front-doors.md#audit) |
| The audit log leaves the pod, to storage the pod can't rewrite | Open | KAN-182 |
| SLOs for job starts, queue time and sandbox creates, with alerts to a named channel | In place | [SLOs](../../slos.md); [alerts](../../alerting.md) |
| An on-call owner, an escalation path and incident runbooks | In place | [On call](../../runbooks/on-call.md); [runbooks](../../runbooks/README.md) |
| A credential leak has its own runbook: revoke, trace, close | In place | [A credential leak](../../runbooks/credential-leak.md) |

## Backup and recovery

| Control | Status | Evidence |
| --- | --- | --- |
| Daily encrypted database backups to the bucket, 14 kept; `hangar doctor` fails on a stale one | In place | [Backups and restore](../../gke.md#7-backups-and-restore) |
| A staged restore that keeps the replaced database beside it | In place | [Backups and restore](../../gke.md#7-backups-and-restore) |
| A restore drill and a rollback rehearsal, timed and recorded | Open | KAN-143 |
