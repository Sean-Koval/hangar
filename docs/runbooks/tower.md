# Tower beside hangar on GKE

Tower, the Govern plane, runs on hangar's GKE cluster in its own namespace, `tower`. Tower's [deploy runbook](https://github.com/Sean-Koval/tower/blob/main/docs/deploy.md) puts it there, and its [ADR 0018](https://github.com/Sean-Koval/tower/blob/main/docs/decisions/0018-deploy-beside-hangar.md) says why. This page is hangar's side.

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
# 1. Deploy Tower (Tower's docs/deploy.md). It writes the two values below to ~/.tower.
# 2. Both into hangar's vault, as team credentials only the control plane sees.
hangar cred add tower --team --harness tower --kind generic --exposure control_plane --value "$(cat ~/.tower/hangar-key)"
hangar cred add tower-webhook --team --harness tower --kind generic --exposure control_plane \
  --value "$(cat ~/.tower/webhook-secret)"
# 3. The new config map. hangar reads it when it next starts: a new image does that, or a restart.
bash deploy/gke/up.sh
kubectl -n hangar rollout restart deploy/hangar && kubectl -n hangar rollout status deploy/hangar
# 4. From hangar's pod, Tower answers.
kubectl -n hangar exec deploy/hangar -- python3 -c \
  "import urllib.request as u; print(u.urlopen('http://tower.tower.svc.cluster.local/api/health').read())"
```

Leave every kind in `shadow` for a week. Move a kind to `enforce` only when `hangar_governance_disagreements_total` stayed at zero for it (spec 0015 §14). [Break-glass](break-glass-tower.md) covers going back.

## The other paths

- **Sandboxes' hooks** call Tower's agents' name, `https://agents.tower.<domain>`. Sandboxes can't reach pod addresses. An environment with `allowlist` egress needs that host on its list.
- **Tower's events** come to hangar's public URL, `/api/governance/events`. Tower sends webhooks over https only.

## Power

`power.sh down` leaves Tower up: it runs on the `system` pool. `power.sh off` stops hangar, then Tower, then the system pool. `power.sh up` starts Tower before hangar, so hangar's first question has an answer. A cluster without Tower is unaffected.
