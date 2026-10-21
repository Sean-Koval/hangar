# Heavy-tier isolation

Why a [heavy](heavy-tier.md) sandbox with Docker inside it stays contained, and why the tier uses Kata rather than
gVisor. Then what the agent is told about it.

## Isolation

The VM is the boundary. Under Kata the sandbox container is privileged *inside the guest*. kata-deploy configures
containerd with `privileged_without_host_devices = true`, so no host device is passed into the VM
([Kata: privileged containers][kata-privileged]).

Containers dockerd starts sit on a bridge inside the VM and leave it NATed to the pod's own address. So the pod's
NetworkPolicy applies to them as to the sandbox (`deploy/k8s/sandbox-network.yaml`). They reach no other pods, no
node addresses and no metadata server, and only the proxy in an allowlist environment.

dockerd listens on its Unix socket only, so containers can't reach it over the bridge. Sidecar services keep their own
unprivileged security context. On the `docker` provider there is no VM: privileged runc is root on the host.

## Why Kata and not gVisor

gVisor has a documented Docker-in-gVisor mode ([Docker in gVisor][gvisor-docker],
[Docker in GKE Sandbox][gvisor-gke]), and hangar evaluated it:

- the container needs added capabilities (SYS_ADMIN, NET_ADMIN...) and to run as root. GKE's Agent Sandbox policy
  refuses that for every sandbox ([Agent Sandbox requirements][gke-agent-sandbox]);
- `/var/lib/docker` must be tmpfs for overlay (gVisor only takes tmpfs as an overlay upper layer). So every image layer
  counts against the sandbox's memory, and a 100 GiB disk profile isn't possible;
- dockerd runs with `--iptables=false`, so published ports (`-p`, Compose's `ports:`, what Testcontainers maps) don't
  work and containers need `--network=host`;
- Docker 28 and later need runsc's `--allow-packet-socket-write`, which "allows sandboxed code to craft arbitrary
  network packets".

So Compose stacks and Testcontainers, the point of the tier, don't work there. Kata runs an unmodified guest kernel
with overlay, bridges, veth and netfilter built in. Its guest kernel config notes "Docker in Docker support requires
overlay". E2B and CubeSandbox are microVMs too. Docker-in-gVisor for builds alone could come later as a lighter tier.

## What the agent sees

- `hangar-agent env doctor` starts with a `tier` line: the tier, the profile and its size, and the runtime. It also
  says whether Docker answers, with the command that starts it when it doesn't.
- A "Docker (hangar heavy tier)" section in its instructions, and the `hangar-docker` skill. It covers Compose stacks
  under the supervisor, Testcontainers, and what to do when Docker or a pull fails.
- The `hangar-environment` skill says Docker exists only on the heavy tier.

[kata-privileged]: https://github.com/kata-containers/kata-containers/blob/main/docs/how-to/privileged.md
[gvisor-docker]: https://gvisor.dev/docs/tutorials/docker-in-gvisor/
[gvisor-gke]: https://gvisor.dev/docs/tutorials/docker-in-gke-sandbox/
[gke-agent-sandbox]: https://docs.cloud.google.com/kubernetes-engine/docs/how-to/how-install-agent-sandbox
