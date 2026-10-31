# Turning the driver sidecar on

Which checks must pass before `HANGAR_K8S_DRIVER_SIDECAR` goes on in a cluster? This page is for whoever runs hangar on GKE. The design is [the driver sidecar](driver-sidecar.md).

## Before enabling

`HANGAR_K8S_DRIVER_SIDECAR` stays off until both checks pass on the dev cluster, with their output recorded on KAN-186:

1. The manual check in [Checking it on a cluster](#checking-it-on-a-cluster).
2. `scripts/k8s/check-sidecar-turn.sh`, run with the setting on. Its `ask` turn tries to read the run key and to write `.claude/settings.json` permission rules. The next turn must still ask, and the key must stay out of reach.

Then set it to `"true"` in the `hangar` ConfigMap and restart the control plane. Warm pools get the sidecar. A sandbox made before keeps refusing turns that ask until it is replaced.

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

## The end-to-end check

`scripts/k8s/check-sidecar-turn.sh` uses hangar's API, as a person would, and `kubectl` to look inside the pod. It needs `curl`, `jq`, a token and a scratch repository.

```sh
HANGAR_URL=https://hangar.dev.example HANGAR_TOKEN=... \
  bash scripts/k8s/check-sidecar-turn.sh --env claude-dev --repo https://github.com/you/scratch-repo
```

Its first turn asks the agent to read the run key, list the driver's files and write permission rules that allow everything. The check allows every approval, as a careless person would.

Then it checks the pod: the sandbox runs split through the sidecar, and the agent can't see `hangar-ctl`, write `hangar-home` or see the driver's processes. Its second turn asks for a command the rules would allow. That turn must still ask, and the check denies it. Each step prints PASS or FAIL; record the output on KAN-186.
