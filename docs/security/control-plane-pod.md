# How is the control-plane pod locked down?

This page is for whoever reviews or runs hangar on Kubernetes. It covers what the control-plane pod may do on its
node, and who may connect to it. Both `deploy/k8s` (the GKE install) and `deploy/kustomize/base` ship it. It is part
of the [security model](README.md).

## The pod

The container runs as the image's user, 1000, never root. It has no Linux capabilities and can't gain privileges. It
runs under the container runtime's default seccomp profile, and its root filesystem is read-only.

hangar writes to three places, each a volume:

| Path | Volume | What hangar writes there |
| --- | --- | --- |
| `/data` | the `hangar-data` PVC | The database, checkpoints, snapshots and the push scan's spool |
| `/tmp` | `emptyDir`, 4 GiB | Build contexts, and fork and snapshot archives in flight |
| `/home/hangar` | `emptyDir`, 256 MiB | gcloud's and docker's configuration for in-cluster image builds |

A local build of the image was started that way with Docker: read-only root, all capabilities dropped, `no-new-privileges`. It served
`/healthz` and the API, and `gcloud`, `docker buildx` and `git` ran. Without a writable home, `docker buildx` fails.

## Who may connect

Without a policy, any pod in the cluster reached all three of hangar's ports. `control-plane-network.yaml` now selects
hangar's pods, so only the connections below get through.

| From | Port | Policy |
| --- | --- | --- |
| Sandbox pods (`hangar-sandboxes`) | 8788 (git gateway), 3128 (egress proxy) | `hangar-ingress` |
| Tower's pods (namespace `tower`, `app: tower`) | 8787 | `hangar-ingress` |
| hangar's own pods | all three | `hangar-ingress` |
| Google's load balancer and health checks (35.191.0.0/16, 130.211.0.0/22) | 8787 | `hangar-ingress-gclb`, in `deploy/k8s/ingress.yaml` and the ingress-gce component |
| The ingress-nginx controller | 8787 | `hangar-ingress-nginx`, in the ingress-nginx component |
| Managed Prometheus, or Prometheus in `monitoring` | 8787 | in `observability/*.yaml` |

Sandboxes never reach 8787, the API. The owner's Tower calls hangar at its public URL, so its calls arrive through the
load balancer. The Tower rule is for a Tower that calls hangar inside the cluster.

`kubectl exec` and `port-forward` and the kubelet's probes don't pass through these policies. The optional microVM
pool is not covered: its VMs' traffic leaves from node addresses. Add an `ipBlock` for that pool's nodes if its
sandboxes use the gateway.

## Identity-Aware Proxy

IAP stays off by default. With it on, Google asks for a Google login before any request reaches hangar. That breaks
every caller that isn't a browser:

- GitHub, Slack and Jira webhooks, and signed webhooks;
- MCP clients, the CLI, the E2B SDK and A2A agents, which send hangar tokens;
- Tower's approval events, and the power jobs' calls.

hangar already checks a token on every request. Its tokens are hashed at rest, scoped and revocable. The policy above
means only the load balancer reaches the API port, so IAP couldn't be bypassed from inside the cluster either.

Turn IAP on when only browsers use this hangar, or put the callers above on a second backend without it. Uncomment
`iap` in the `hangar` BackendConfig and create the OAuth client secret `iap-oauth`. Grant
`roles/iap.httpsResourceAccessor` to the people who may sign in.

## What else

The cluster's own settings are on [Running hangar on GKE](../gke.md). The egress
default for GKE (`HANGAR_EGRESS_DEFAULT`) is unchanged here.
