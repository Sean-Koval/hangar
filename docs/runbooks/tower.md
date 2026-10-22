# Tower beside hangar on GKE

Tower, the Govern plane, runs on hangar's GKE cluster in its own namespace, `tower`. Tower's [run Tower beside hangar](https://github.com/Sean-Koval/tower/blob/main/docs/deploy.md) page puts it there, and its [ADR 0018](https://github.com/Sean-Koval/tower/blob/main/docs/decisions/0018-deploy-beside-hangar.md) says why. This page is hangar's side.

## The settings

`deploy/k8s/config.yaml` points hangar at Tower:

| Setting | Value | Why |
| --- | --- | --- |
| `HANGAR_GOVERNANCE_URL` | `http://tower.tower.svc.cluster.local` | The Service in the cluster. No load balancer or certificate sits on the fail-closed path |
| `HANGAR_GOVERNANCE` | `shadow` | hangar's rules decide. Tower answers a dry run, and a different answer is audited `governance.disagree` |
| `HANGAR_GOVERNANCE_CREDENTIAL` | `tower` | The vault credential holding Tower's service key |
| `HANGAR_GOVERNANCE_WEBHOOK_SECRET` | `tower-webhook` | The vault credential holding the secret Tower signs its events with |

hangar reads these once [#172](https://github.com/Sean-Koval/hangar/issues/172) wires its client. Until then it ignores them, and its own rules decide as before.

## In order

```bash
# 1. In this checkout: the config map that points hangar at Tower.
bash deploy/gke/up.sh
# 2. In Tower's checkout, beside this one. Bootstrap stores both credentials in hangar's vault.
bash deploy/gke/tower.sh bootstrap
bash deploy/gke/tower.sh up        # ends with a dry-run decision from hangar's pod
```

Bootstrap stores `tower` and `tower-webhook` as team credentials only the control plane sees. To store one by hand, use the `webhook` harness: there is no `tower` harness, and hangar refuses one with no slot for `generic`.

```bash
hangar cred add tower --team --harness webhook --kind generic --exposure control_plane \
  --file <(gcloud secrets versions access latest --secret tower-hangar-key --project <project>)
```

Leave every kind in `shadow` for a week. Move a kind to `enforce` only when `hangar_governance_disagreements_total` stayed at zero for it (spec 0015 §14). [Break-glass](break-glass-tower.md) covers going back.

## The other paths

- **Sandboxes' hooks** call Tower's agents' name, `https://agents.<domain>`, with an agent key. Sandboxes can't reach pod addresses, and Tower's network policy admits only hangar's control plane. An environment with `allowlist` egress needs that host on its list.
- **Tower's events** come to hangar's public URL, `/api/governance/events`. Tower sends webhooks over https only.

## Power

`power.sh off` stops hangar, then Tower, then the system pool. `power.sh up` starts Tower before hangar, so hangar's first question has an answer. A cluster without Tower is unaffected.

`power.sh down` leaves Tower up by default: it is small, and it runs on the `system` pool. After `tower.sh schedule on`, Tower follows the schedule. `down` stops it after the sandbox pools, and `up` starts it first. The setting is the annotation `hangar.dev/power=follow` on Tower's Deployment.

A Tower stopped with `tower.sh down` carries `tower.dev/stopped`, and `power.sh up` leaves it stopped. `tower.sh up` starts it again. `power.sh status` shows which applies.
