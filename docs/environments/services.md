# Services

A repo's services (Postgres, Redis...) are declared as images in its [file](repo-file.md) or its environment. They run
beside the sandbox and are reachable at their name. This page says where they run, which images and sizes are
allowed, and when they start.

Docker inside a gVisor sandbox is limited, so a service never runs inside the sandbox. It runs next to it, with the
same isolation, and goes away with it. The sandbox reaches it at its name, such as `postgres:5432`. On the heavy tier
a service can also be a Compose stack that runs inside the sandbox
([Compose stacks](supervisor.md#compose-stacks-under-the-service-supervisor)).

## Where a service runs

- **Kubernetes:** a sidecar container in the sandbox's pod. It shares the pod's network (127.0.0.1, and its name
  through the pod's `hostAliases`) and its runtime class.
- It also shares the pod's hardening: uid 1000, no capabilities, no privilege escalation. GKE Sandbox's admission
  policy requires that of every container.
- Its `cpus` and `memory_gb` (0.5 each by default) count toward the pod, memory requested in full. It takes the
  sandbox's own network policy: it reaches what the sandbox reaches (the internet, not other pods). Nothing reaches it
  from outside the pod.
- **Docker:** a container on a network of the sandbox's own (`hangar-svc-<sandbox>`), with an `/etc/hosts` entry per
  service. The sandbox is attached to it before it starts, since gVisor can't take a network added later.
- hangar's own network keeps sandboxes from reaching each other, so it can't carry this. The services' network is
  internal: no route out, no metadata server, and nobody else's sandbox or services on it.
- The services run under the same runtime, as uid 1000, without capabilities, with `no-new-privileges`. They are
  removed with the sandbox; the reaper removes any whose sandbox went away some other way.

Services run as uid 1000, so give a database a writable data directory. `volumes` lists empty directories (an
`emptyDir` on Kubernetes, a tmpfs on Docker). The official `postgres` image runs as any uid when `PGDATA` is a
directory it can create, as in the [example](repo-file.md#the-file). `redis` and most others need nothing.

## Which images

Images must match `HANGAR_ENV_SERVICE_IMAGES`. By default that is Docker's official images (`postgres:17`, `redis`),
directly or through `mirror.gcr.io/library/`, which GKE nodes pull without Docker Hub's rate limits. An admin adds a
registry with a pattern such as `us-docker.pkg.dev/acme/services/**` (`*` stays within one path segment, `**` crosses
them).

Other images are left out, with a setup failure that names them. hangar's own images are never a service, whatever
the patterns say. Those are the images under `HANGAR_REGISTRY` or `HANGAR_IMAGE_PREFIX`: environments, and repo
builds that hold their repo's checkout.

## How big

The repo writes each service's `cpus` and `memory_gb`. Together a sandbox's services must fit
`HANGAR_ENV_SERVICES_MAX_CPUS` and `HANGAR_ENV_SERVICES_MAX_MEMORY_GB` (2 and 2 GiB); the ones past that are left
out, with a setup failure.

That budget is what a node's packing must allow for on top of the sandbox. Using all of it, a 2 vCPU / 4 GiB sandbox
requests 2 vCPU and 6 GiB (`HANGAR_K8S_CPU_REQUEST_FRACTION=0.5`). So a node of [team scale](../team-scale.md) holds 7
such sandboxes (CPU-bound), not 14. Most repos declare none or one or two.

## When they start

The services are decided before the sandbox starts. They are those of the build it starts from, else the repo's file
as its default branch was last seen, else the environment's. A service added in a commit starts with the next sandbox, and
the agent is told so.

A sandbox with services of its own starts cold. On Kubernetes it gets a SandboxTemplate per set of services (or per
build), and on Docker a warm container can't join a network. Services that the environment itself declares are part
of its own template and warm pool.

The [service supervisor](supervisor.md) (`hangar-agent svc`) waits for the services to answer before it runs `start`,
and `hangar-agent svc status` shows them. Forks don't get services yet. E2B doesn't run them, and its sandboxes get a
setup failure saying so.
