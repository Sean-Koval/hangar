# Runbook: a full disk or quota

For `data-disk`, `node-disk` and `sandbox-creates-failing` ([alerting](../alerting.md)). Three things fill up: hangar's data disk, a node's ephemeral storage, and the GCP project's quotas. Each shows up differently.

## hangar's data disk (`hangar-data`)

hangar's SQLite store lives on the 20 GiB `hangar-data` volume, mounted at `/data`. On GKE its backups go to the session bucket instead. At 100% hangar can't write a job's event, and requests fail.

```bash
kubectl -n hangar exec deploy/hangar -- df -h /data
kubectl -n hangar exec deploy/hangar -- du -sh /data/* | sort -h | tail
```

- **Leftovers.** A restore leaves `hangar.db.before-restore-<time>` beside the store. Once the restored one has run for a day, it can go.
- **What grew.** `du` names the directory. The ops loop already prunes expired flight records, so a large one there is a bug worth a ticket.
- **Grow the disk.** Expand the PVC, which GKE's default storage class allows online: `kubectl -n hangar patch pvc hangar-data -p '{"spec":{"resources":{"requests":{"storage":"40Gi"}}}}'`.

Take a backup before deleting anything: `hangar admin backup`.

## A node's ephemeral storage

Sandboxes write to their node's disk. A full node evicts pods, so sandboxes vanish mid-turn and jobs fail with `sandbox_gone`.

```bash
kubectl get nodes
kubectl describe node <node> | grep -A5 Conditions     # DiskPressure
```

Images pile up on long-lived nodes. Cordon and drain the node, and the autoscaler replaces it. For lasting pressure raise `sandbox_disk_gb` in Terraform, then roll the pool in a powered-down window ([upgrading](../upgrading.md#when-to-deploy)).

## GCP quotas

When the sandbox pool can't scale up, pods stay `Pending` and creates fail after `HANGAR_SANDBOX_START_TIMEOUT_SECONDS` (900). That fires `sandbox-creates-failing`.

```bash
kubectl -n hangar-sandboxes get events --sort-by=.lastTimestamp | tail -20
gcloud compute regions describe <region> --project <project> --format='table(quotas.metric,quotas.usage,quotas.limit)'
```

Look for `FailedScaleUp` and `QUOTA_EXCEEDED`: CPUs, in-use addresses, SSD space or Spot capacity. Raise the quota in the console's **IAM & Admin > Quotas**, or lower `sandbox_max_nodes`. Spot capacity can't be raised: set `sandbox_spot = false` for a day.

A failed create that isn't a quota is usually an image pull or a node that won't join. The job's events and `hangar doctor` say which; the [stuck job runbook](stuck-job.md) continues from there.

## Afterwards

The disk alerts close when usage is back under the threshold for their window. Open a KAN ticket if the disk or quota needs a lasting change.
