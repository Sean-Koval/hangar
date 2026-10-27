# The driver sidecar on GKE

How can an `ask` or `deny-risky` turn on GKE keep its driver out of the agent's reach? This page is the design, for whoever reviews or finishes it. Part of the [security model](README.md).

**Status: a prototype (KAN-83).** The pod's shape and the driver's side are built and tested. Turns don't use them yet ([KAN-186](https://seanmkoval.atlassian.net/browse/KAN-186)). Until then `HANGAR_UID_SPLIT=required` refuses those turns on GKE, and H-1 stays mitigated, not closed.

## Why Docker's split doesn't carry over

On Docker, [the uid split](../steering.md#the-uid-split) gives the driver a user of its own inside the sandbox. Docker's exec picks the uid, and root sets the user up. A GKE Sandbox pod has neither: its container runs as one non-root user with no capabilities. So the second user has to come from a second container.

## The pod

`HANGAR_K8S_DRIVER_SIDECAR=true` adds a `hangar-driver` container to each sandbox pod (`add_driver_sidecar` in `providers/kubernetes.py`). It runs the environment's own image, so nothing new is pulled.

| | `sandbox` (the agent) | `hangar-driver` |
| --- | --- | --- |
| uid | 1000 | 1001, no capabilities, `allowPrivilegeEscalation: false`, read-only root |
| `hangar-home` (`/var/lib/hangar`): the driver, run scripts, hangar's harness settings | read-only | read-write |
| `hangar-ctl` (`/var/lib/hangar-ctl`, in memory): run keys, commands, approvals, tmux | not mounted | read-write |
| PID namespace | its own (`shareProcessNamespace: false`) | its own |

Writes are stopped by the mounts, not by file modes. The pod's `fsGroup` makes both volumes group-writable for the agent's group. A pod whose agent container is privileged (a heavy pod with Docker) gets no sidecar. An agent that can be root gains nothing from it.

## Starting the harness from another container

The driver can't start a process in the agent's container, so hangar starts `relay.py` there instead, as the agent, through pods/exec. The run's relay token goes on its stdin, never on a command line. The relay makes itself non-dumpable before it reads the token.

The driver (`driver.py --relay`) listens on the pod's loopback and writes its port to `hangar-home`. The relay connects twice: `io` carries the harness's stdin and stdout, and `ctl` its stderr, exit status and signals. The driver takes the first two connections that say the token, then stops listening. Anything else on the loopback is dropped.

The relay and the harness are the agent's processes. The agent can stop them or lie through them, as it can through the harness under the Docker split. It can't reach the driver's key, control files, memory or approvals.

## What gVisor gives

gVisor runs the whole pod in one sandbox kernel, so isolation between its containers is gVisor's to enforce. These claims were checked in the gVisor source (`google/gvisor` at 4777ee7, 27 October 2026), not on a cluster.

| Claim | Where |
| --- | --- |
| A container without a shared PID namespace gets a new one: it can't see, signal or trace the driver | `runsc/boot/loader.go`, `startSubcontainer` |
| ptrace and `/proc/<pid>/mem` need the same uid, or `CAP_SYS_PTRACE`; non-dumpable is honoured; Yama defaults to scope 1 | `pkg/sentry/kernel/ptrace.go`, `kernel.go` |
| Each container has its own mount namespace; a shared volume is mounted with each container's own options, so `readOnly` holds per container | `runsc/boot/vfs.go`, `mountSharedSubmount` |
| GKE's admission controller and the shim make emptyDirs sandbox-internal tmpfs, shared at pod level | `pkg/shim/v1/utils/volumes.go` |

Not checked: that GKE's admission policy accepts the second container, and the loopback between containers. hangar's service sidecars already rely on that loopback.

## What it doesn't cover

- **Shared by the pod:** the network namespace, SysV IPC and `/dev/shm`. The driver uses none of them except its token-checked listener.
- **The sandbox kernel itself.** A gVisor escape reaches every container in the pod.
- **Per-edit trees and the verify gate.** The driver's container doesn't have the checkout. Trees turn off, and the gate is skipped with a note on stderr ([KAN-187](https://seanmkoval.atlassian.net/browse/KAN-187)).
- **E2B.** One microVM, one container: [KAN-189](https://seanmkoval.atlassian.net/browse/KAN-189).
- **Replays** don't depend on the uid split. Their sandbox has no credentials or egress, and the gateway compares outside it.

## Graders

The same shape fits [sealed graders](graders.md) ([KAN-188](https://seanmkoval.atlassian.net/browse/KAN-188)). The hidden tests go to `hangar-home`, read-only for the trial's code. The grade wrapper runs in `hangar-driver`. The grader's exit status and output come from the exec API, and its report reaches the wrapper on a loopback socket. That would make GKE grades `trust: isolated`.

## Cost

Each sandbox reserves 50m CPU and 64 MiB more, up to 64 MiB of it for the in-memory control volume. That is about 5% of a 2-CPU sandbox's CPU request. Each turn takes one more exec to start the relay. These are estimates, not measurements.

## Checking it on a cluster

With the setting on in the dev cluster's `hangar` ConfigMap, create a sandbox and find its pod. Each command's comment says what it must show.

```sh
NS=hangar-sandboxes; POD=<the sandbox's pod>
kubectl -n $NS get pod $POD -o jsonpath='{.spec.runtimeClassName} {.spec.shareProcessNamespace} {range .status.containerStatuses[*]}{.name}={.ready} {end}'
# gvisor false hangar-driver=true sandbox=true
kubectl -n $NS exec $POD -c hangar-driver -- bash -c 'id -u; echo s3cret > /var/lib/hangar-ctl/key; chmod 600 /var/lib/hangar-ctl/key; echo hi > /var/lib/hangar/probe; (exec -a hangar-probe-sleep sleep 600) >/dev/null 2>&1 &'
# 1001
kubectl -n $NS exec $POD -c sandbox -- sh -c 'id -u; ls /var/lib/hangar-ctl; cat /var/lib/hangar/probe; touch /var/lib/hangar/x; grep -l hangar-probe-sleep /proc/[0-9]*/cmdline; cat /proc/1/cmdline | tr "\0" " "'
# 1000 / No such file or directory / hi / Read-only file system / (nothing) / the agent's own sleep infinity
kubectl -n $NS exec $POD -c hangar-driver -- python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 7999)); s.listen(); c,_=s.accept(); print(c.recv(9))' &
kubectl -n $NS exec $POD -c sandbox -- python3 -c 'import socket; socket.create_connection(("127.0.0.1", 7999)).sendall(b"loopback\n")'
# b'loopback\n'
```

After KAN-186, also run an `ask` turn that tries to read the key and to write `.claude/settings.json` permission rules. The turn must still ask, and the key must stay unread.
