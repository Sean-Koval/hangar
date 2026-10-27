# Runbook: a stuck job

For `jobs-stuck`, `job-starts-failing` and `queue-slow` ([alerting](../alerting.md)). A job counts as stuck when it is runnable and queued for an hour. It also counts when its sandbox isn't up in 20 minutes, or it runs 30 minutes past its own timeout.

## 1. Find the job

```bash
hangar status                                   # loops, power, the day's failures by cause
hangar job list --state queued                  # or provisioning, running
kubectl -n hangar logs deploy/hangar --since=1h | grep 'hangar.slo: event=job_start result=error'
```

The `jobs_stuck` line gives how many and their states, never their ids. The console's **Needs you** page and `hangar job list` give those.

## 2. Read what it last did

```bash
hangar job get <id>        # state, reason, sandbox
hangar job events <id>     # the timeline, newest last
```

Then match the state:

- **Queued, no reason.** Workers are busy or stopped. Check `hangar status` for a loop that stopped beating, and `HANGAR_JOB_CONCURRENCY`. A held job has a reason and isn't stuck ([troubleshooting](../troubleshooting.md#a-job-waits)).
- **Provisioning.** The sandbox didn't start. Look for "building the image" in the events: a first build takes minutes. Otherwise check the nodes: `kubectl get nodes` and `kubectl -n hangar-sandboxes get pods`. A pod stuck `Pending` is a node or quota problem ([disk or quota](disk-and-quota.md)).
- **Running, verifying or delivering.** The agent or a check hangs. `hangar job events <id>` shows the last output.

When many starts fail at once, the cause is shared: a node pool, a registry, a credential or a deploy. Check `hangar doctor`, then [bad deploy](bad-deploy.md) if a deploy just went out.

## 3. Unstick it

```bash
hangar job cancel <id>                  # stops the agent; the sandbox is kept for a while
```

Then retry it from the console's deck, which is the same job again or a fresh one (KAN-140). Or ask the requester to submit it again. A job hangar restarted under is resumed by the reconciler, so look before cancelling one that just came back.

## 4. Check it's over

The `jobs-stuck` alert closes ten minutes after the last stuck job. Watch the next job start on the **hangar SLOs** dashboard. If the cause was hangar's, open a KAN ticket with the job id and its last events.
