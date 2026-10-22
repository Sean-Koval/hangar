# Sandbox networking on Docker

The network hangar creates for sandboxes on a Docker host, where no sandbox reaches another. Part of [networking and egress](egress.md) in the [security model](README.md).

On Docker's default bridge every container reaches every port of every other: hangar-envd, dev servers, anything an agent listens on. hangar creates its own network for sandboxes instead, `hangar-sandboxes` on the bridge `hangar0`, with inter-container connectivity off (`com.docker.network.bridge.enable_icc=false`), and starts every sandbox and warm-pool container on it. Checked on Docker 29.8 (`tests/test_e2e_isolation_docker.py`):

- one sandbox can't connect to another's ports, envd's included;
- the host still reaches each sandbox at its IP, and at envd's port published on `127.0.0.1` (only the host can use that address; a sandbox that dials the host's bridge address gets nothing);
- sandboxes still reach the internet, and the host at `host.docker.internal`, where the [git gateway](git-gateway.md) listens;
- a container attached to the network can't reach the sandboxes either, so a control plane that runs in a container (Docker Compose) reaches envd only through `docker exec`.

`HANGAR_DOCKER_NETWORK` names another network to use as it is (`bridge` for Docker's default). `hangar doctor` warns when that network lets sandboxes reach each other, and lists sandboxes started before this change, which stay where they are until they are recreated.

Docker has no setting that keeps containers off link-local addresses, so on a cloud VM a sandbox can ask the metadata server for the VM's credentials unless the host's firewall drops it. The bridge has a fixed name for that rule (the single-VM install adds it on every boot; DNS stays open because Docker's resolver may forward to the same address):

```sh
iptables -I DOCKER-USER -i hangar0 -d 169.254.0.0/16 -p tcp ! --dport 53 -j DROP
```

A sandbox with [services](../environments/services.md) (Postgres, Redis) is also attached to a network of its own, `hangar-svc-<sandbox>`, which holds only its services. The network is `internal`: it has no route out, so the services can't reach the internet, the host or the metadata server, and nobody else's sandbox is on it. The services run as uid 1000, without capabilities, with `no-new-privileges`, under the sandbox's runtime. [Preview ports](../environments/previews.md) go through hangar's authenticated proxy, owner-scoped, and are never published on the host.

