# The microVM tier: E2B and CubeSandbox as hangar providers

hangar can run sandboxes as microVMs that start by restoring a memory snapshot, in tens of milliseconds rather than the second or more a container or a GKE gVisor Pod takes. It doesn't run the microVMs itself. It drives a platform that does, through its `e2b` provider, and stays the control plane: environments, jobs, sessions, the git gateway, budgets and the MCP and E2B APIs all work the same on top.

| Platform | VMM | How it is fast | Flavour |
| --- | --- | --- | --- |
| [E2B](https://e2b.dev) cloud or BYOC | Firecracker | Restores pre-booted template snapshots: memory loaded lazily with `userfaultfd`, copy-on-write rootfs over NBD | `cloud` |
| E2B self-hosted ([infra](https://github.com/e2b-dev/infra) / Embed) | Firecracker | Same, on your machines | `selfhosted` |
| [CubeSandbox](https://github.com/TencentCloud/CubeSandbox) | Cloud Hypervisor fork | Restores a memory image mapped `MAP_PRIVATE`, so every VM from one template shares its page cache; XFS reflink rootfs; a pre-created TAP pool | `cube` |

This is spec 0013's phase P3 and the deployment design for its P4 (§6, §8.2, §9). Nothing here was run on KVM: this machine has none. The tests run the provider against a fake control plane and against hangar's own E2B API, with real envd daemons behind both (see [Testing](#testing-without-kvm)).

## Tiers

An environment (or a single create) asks for a tier, not a provider:

```yaml
# envs/rollouts.yaml
name: rollouts
tier: fast          # fast | isolated | default | heavy
```

| Tier | Meant for | Providers (`HANGAR_TIERS`) | When none is available |
| --- | --- | --- | --- |
| `fast` | forks, best-of-N, RL rollouts, short jobs: where start and restore time dominate | `["e2b"]` | falls back to `HANGAR_DEFAULT_PROVIDER`: it is a speed preference |
| `isolated` | untrusted code that needs a kernel boundary | `["kubernetes"]` (gVisor or Kata) | **fails**: it is a security choice, and falling back to runc would quietly weaken it |
| `default` | everything else | `HANGAR_DEFAULT_PROVIDER` | |
| `heavy` | Docker inside the sandbox, the large and xlarge profiles ([below](#the-heavy-tier-on-microvms)) | none until set, e.g. `["kubernetes"]` (Kata) or `["e2b"]` | **fails**: a privileged sandbox needs a VM around it |

- **How a provider is picked.** A create that names no provider takes the first provider in its tier's list that is loaded and not cooling down. A create that names a provider gets that provider, whatever the tier, with one exception: an `isolated` environment (or a create that asks for `isolated`) only ever gets a provider listed for that tier. Naming another provider, or asking for another tier, fails the create.
- **Fallback.** A provider that answers "full" or "unreachable" (HTTP 429, 502, 503, 504, or no connection) is skipped for `HANGAR_TIER_COOLDOWN_SECONDS` (60), and the create moves to the next provider. The audit log records `sandbox.tier_fallback`. Any other error, such as a missing template, fails the create as before.
- **Jobs and sessions** use their environment's tier, since they create sandboxes through the same `fleet.create`.
- **Change the lists** with `HANGAR_TIERS='{"fast": ["e2b"], "isolated": ["kubernetes"]}'`. An empty list is allowed, so `fast` can be just the default provider.

### The heavy tier on microVMs

The heavy tier ([environments.md](environments.md#the-heavy-tier): Docker inside the sandbox, and the large and
xlarge profiles) can run here as well as on GKE's Kata pool, and on a cluster with GKE's Agent Sandbox add-on it has
to, since the add-on refuses privileged Kata pods ([gke.md](gke.md#4a-the-heavy-tier-optional)):

```bash
HANGAR_TIERS='{"fast": ["e2b"], "heavy": ["e2b"]}'   # or ["kubernetes", "e2b"]: Kata first, microVMs when it's full
```

- **The template is the heavy environment's image**: hangar renders it with dockerd, containerd, runc and the docker
  CLI (with Compose and Buildx) copied from `HANGAR_HEAVY_DOCKER_IMAGE`, plus iptables. A template mapped out of band
  (`HANGAR_E2B_ENVIRONMENT_TEMPLATES`) must carry them itself.
- **dockerd runs on the guest kernel**, started at claim by `hangar-docker` as root through the template's sudo
  (heavy environments need `sudo: true`). The VM's root disk is a real block device, so `/var/lib/docker` is used as
  it is: no loop-mounted image as under Kata. The guest kernel needs overlayfs, bridges, veth and netfilter NAT. That
  wasn't verified on E2B's or CubeSandbox's kernels here (no KVM on this machine): `hangar-docker` reports what fails
  (`get_sandbox`'s `info.heavy.status`, `hangar-agent env doctor`), and the sandbox runs on without Docker.
- **Size**: a template is sized when it is built, so a sandbox has its environment's `profile` (or `resources`); a
  job's `hangar.profile` label that asks for another size is refused here. `disk_gb` becomes Cube's
  `writableLayerSize`.
- **Containers stay in the VM**: they leave it NATed to the VM's own address, under the platform's network policy
  (CubeSandbox's eBPF policy denies private ranges by default).
- **Hot resume** pauses the VM with its memory, so a resumed session finds dockerd, its images and running stacks as
  they were.

hangar's own E2B API never uses tiers. It always creates on `HANGAR_DEFAULT_PROVIDER`, the provider that has hangar-envd, so a `fast` environment requested through `/e2b` can't route back into the `e2b` provider. If that provider pointed at this same hangar, the request would loop. An `isolated` environment is created through `/e2b` only when the default provider is one of the isolated tier's; otherwise the create fails.

## The e2b provider

### Flavours

| | `cloud` | `selfhosted` | `cube` |
| --- | --- | --- | --- |
| Control API | `https://api.<domain>` (default `api.e2b.app`) | `HANGAR_E2B_API_URL`, or `https://api.<HANGAR_E2B_DOMAIN>` | `HANGAR_E2B_API_URL` (CubeAPI listens on `:3000`), required |
| API key | `HANGAR_E2B_API_KEY`, required | required | optional (CubeAPI has no auth unless its `AUTH_CALLBACK_URL` is set) |
| Create / connect | `POST /v2/sandboxes`, `POST /v2/sandboxes/{id}/connect` | same | `POST /sandboxes`, `POST /sandboxes/{id}/connect`: CubeAPI answers the v2 routes with 405 |
| envd URL | `https://sandbox.<domain>`, routed by the `E2b-Sandbox-Id` header | `https://49983-<id>.<domain>` | `https://49983-<id>.<domain>` through CubeProxy |
| envd access token | yes | yes | **none** ([Security](#security)) |
| Template builds | `Template.build` from your pushed image (e2b SDK) | same | CubeAPI's image builds (`POST /templates`) |
| Snapshots | `POST /sandboxes/{id}/snapshots` | same | same |

`HANGAR_E2B_SANDBOX_URL` overrides where envd is reached. With `{sandbox_id}` (and `{port}`) in it, it is filled in per sandbox. That is for a CubeProxy without TLS on a private network, for example `http://{port}-{sandbox_id}.cube.internal`. Without placeholders, every sandbox is reached at that one URL and routed by header. That fits a single-host test, or another hangar's `/e2b/sandbox`.

### What changed from the old provider

- **Lifecycle.** The provider uses its own thin httpx client for the control API, the same for every flavour. The e2b SDK from 2.51 on only posts to the v2 routes, which CubeAPI lacks (spec 0013 §5.4). The SDK is now needed only to build templates on E2B itself.
- **Commands, files and terminals** go to the sandbox's envd through hangar's own Connect client (`envd.py`), the one the Docker and Kubernetes providers use for hangar-envd. That means the same output tail (`MAX_OUTPUT`), the same read limits (#48), deadlines, and refusal of compressed or oversized answers. The difference is authentication: E2B's `X-Access-Token`, plus the traffic token when there is one, instead of hangar-envd's per-request signatures.
- **One login shell per command.** The old `exec` ran `bash -lc '<cmd>'` through `commands.run`, which itself runs `/bin/bash -l -c`, so the profile ran twice on every call. Now envd starts `timeout … bash -lc <cmd>` once, which also gives a timeout that kills the command.
- **Ports are private.** Every sandbox is created with `network.allowPublicTraffic=false`, so the edge wants the traffic token, which only hangar holds.
- **Tokens stay in memory.** The access token comes from create and connect, is never stored or logged, and is fetched again after a control-plane restart. The one exception is the traffic token: CubeAPI returns it only from create (its connect and resume read the sandbox back without it), so the provider keeps it with the sandbox record, encrypted with a key derived from hangar's secret key, and uses it whenever connect doesn't hand one out. On a running sandbox, `connect` only extends its timeout. A paused sandbox is never connected implicitly, because connecting would resume it.
- **Errors are honest.** `status` raises when the API can't be reached, rather than reporting the sandbox stopped. `list_refs` returns nothing on failure, so reconciliation doesn't mark every sandbox gone.

### Templates

A hangar environment becomes an E2B or Cube template in one of two ways:

1. **Built by hangar** (`hangar env publish`, or the first create). hangar builds the image, pushes it to `HANGAR_REGISTRY`, then:
   - on `cloud` or `selfhosted`, runs `Template.build` from that image, named `hangar-<env>:<digest>`. E2B adds its own envd;
   - on `cube`, posts `{image, name: hangar-<env>-<digest16>, cpu (millicores), memory (MiB), exposedPorts: [49983], probePort: 49983, probePath: /health, writableLayerSize}` to CubeAPI's `/templates`, then polls `/templates/{id}/builds/{job}/status` until it is `ready`. Creates look the alias up for its `tpl-…` id.
2. **Built out of band and mapped**: `HANGAR_E2B_ENVIRONMENT_TEMPLATES='{"python": "tpl-0a1b2c", "node": "my-team/node-22"}'`. The provider uses the mapped template and builds nothing. A mapped template has to contain what the environment would have (its tools, its harness, the `agent` user and `/workspace`), because hangar can't check it.

**What has to happen out of band on CubeSandbox.** Cube templates don't get an envd injected the way E2B's do. The image must start E2B's envd on 49983 itself, or Cube's build probe never succeeds:

- Set the environment's `base:` to `ghcr.io/tencentcloud/cubesandbox-base:2026.16`. That is Ubuntu 22.04 with envd, and its entrypoint (`cube-entrypoint.sh`) starts envd and then runs the image's command. hangar's rendered image keeps that entrypoint and adds `sleep infinity` as the command.
- Add `env: {ENVD_LOG_FILE: "-"}` to the environment. hangar's image ends as the unprivileged `agent` user, and the entrypoint's default log file under `/var/log` would make it exit.
- Make the image pullable by CubeMaster: a public registry, or `HANGAR_E2B_REGISTRY_USERNAME`/`_PASSWORD`, which go to Cube as `registryUsername`/`registryPassword`.
- Alternatively, build with Cube's own tooling (`cubemastercli tpl`, or its template guide) and map the result with `HANGAR_E2B_ENVIRONMENT_TEMPLATES`.

None of this ran here: no Cube cluster was available. The request body is taken from CubeAPI's `openapi.yml` and `services/templates.rs` at commit `8d3d991`.

### Settings

| Setting | Default | |
| --- | --- | --- |
| `HANGAR_E2B_FLAVOR` | `cloud` | `cloud`, `selfhosted` or `cube` |
| `HANGAR_E2B_API_URL` | `https://api.<domain>` | The control API (required for `cube`) |
| `HANGAR_E2B_DOMAIN` | `e2b.app` | Sandbox host names are `<port>-<id>.<domain>` |
| `HANGAR_E2B_SANDBOX_URL` | unset | Where envd is reached, overriding the domain rule (see above) |
| `HANGAR_E2B_API_KEY` | unset | Required for `cloud` and `selfhosted` |
| `HANGAR_E2B_ALLOW_UNAUTHENTICATED_ENVD` | `false` | Accept sandboxes whose envd has no token (CubeSandbox), on a private network only |
| `HANGAR_E2B_ENVIRONMENT_TEMPLATES` | `{}` | environment → template built out of band |
| `HANGAR_E2B_REQUEST_TIMEOUT_SECONDS` | `60` | One control API call |
| `HANGAR_E2B_TEMPLATE_BUILD_TIMEOUT_SECONDS` | `1800` | A Cube template build |
| `HANGAR_TIERS` | `{"fast": ["e2b"], "isolated": ["kubernetes"]}` | Tier → providers in order |
| `HANGAR_TIER_COOLDOWN_SECONDS` | `60` | How long a full or unreachable provider is skipped |
| `HANGAR_SESSION_HOT_RESUME_MINUTES` | `1440` | How long a suspended session's sandbox stays paused for hot resume; 0 turns it off |
| `HANGAR_SESSION_HOT_RESUME_MAX` | `20` | Most sandboxes kept paused for hot resume at once |

## Hot resume

On a provider whose pause keeps the VM's memory and whose resume restores it fast, the provider declares the `hot_resume` capability. The `e2b` provider does, on every flavour. A session suspend then works like this (`hot_resume.py`, spec 0010 M4):

1. **The checkpoint comes first, as always.** The conversation, commits and uncommitted work go to the checkpoint store. If that fails and there is no earlier checkpoint, the sandbox is kept, exactly as before.
2. **The sandbox is paused instead of destroyed.** It is kept for `HANGAR_SESSION_HOT_RESUME_MINUTES` (24 h), then the reaper removes it. The store records which sandbox is parked for the session and which checkpoint was the latest at that moment.
3. **The next turn resumes it.** E2B's connect, or CubeSandbox's, restores the memory snapshot, and hangar checks that a command runs. The session is back on the same machine: running processes, caches, build artifacts, ignored directories and anything else a checkpoint leaves out are all still there.

It falls back to restoring the checkpoint in a new sandbox whenever the parked sandbox can't be trusted to be the latest state:

- a newer checkpoint exists (the session resumed elsewhere and was suspended again);
- the sandbox is gone (reaped, or lost by the provider);
- resuming it fails, or it doesn't answer a command afterwards.

In every fallback the parked sandbox is destroyed. No checkpoint is ever deleted by any of this. A parked sandbox survives a control-plane restart; startup doesn't count it as an orphan. Archiving or deleting the session destroys it.

Parked sandboxes are a cache, so hangar bounds them. When a suspend would break one of these rules, the sandbox is destroyed as before, and the next resume restores the checkpoint:

- **A cap.** At most `HANGAR_SESSION_HOT_RESUME_MAX` (20) sandboxes are parked at once; past that, the longest-parked goes first.
- **Headroom.** Paused sandboxes count against `HANGAR_MAX_SANDBOXES`, so nothing is parked while the fleet is three-quarters full. If the fleet fills up later, a create evicts the longest-parked sandbox instead of being refused.
- **No rotating tokens.** A sandbox holding an exclusive credential, such as a Claude Code OAuth login with its rotating refresh token, isn't parked. The token goes back to the vault, so other sandboxes can use it, and a paused copy can't go stale.
- **Gateway grants.** A parked sandbox holds no live grant: it is revoked before the pause. Wake mints a new one for the session's branch as it is then (best-of-N's take-over moves the winner to the job's branch) and writes it where git reads it, as a cold boot would. If that fails, the session restores its checkpoint instead.

**Cost.** A paused sandbox isn't running, but it isn't free either: E2B keeps its snapshot (check your plan's terms for paused sandboxes), and CubeSandbox keeps the snapshot files on the node's disk. Turn hot resume off with `HANGAR_SESSION_HOT_RESUME_MINUTES=0` if that matters more than the restore time.

Pause and connect through hangar's own E2B API keep doing what they did before: the provider's pause, with memory.

## Security

- **CubeSandbox's envd has no token.** CubeAPI returns `envdAccessToken: null` (spec 0013 §5.4, `services/sandboxes.rs`), so the envd inside a Cube sandbox answers anyone who can reach it. hangar therefore **refuses such sandboxes and kills them**, unless `HANGAR_E2B_ALLOW_UNAUTHENTICATED_ENVD=true`. Set that only when all of these hold:
  - CubeProxy and the VMs' network are reachable only from hangar's control plane. On GKE that means a NetworkPolicy on the microVM pool that admits only the hangar namespace; on bare metal, a VPC firewall rule from the control plane's subnet alone.
  - Every sandbox is created with `allowPublicTraffic=false`, which hangar always does. CubeProxy then checks the traffic token on every port, envd's included (`CubeProxy/lua/sandbox_backend.lua`), and only hangar has that token. hangar checks the part it can see: a sandbox that comes back with neither an envd token nor a traffic token (CubeMaster made it public) is refused and killed, flag or not.
  - The VMs' envd port is reachable only through CubeProxy, not directly on the node network.
  - Nothing else on that network runs untrusted code. The sandboxes themselves are isolated by Cube's eBPF policy, which denies private ranges by default.
- **Upstream fix.** CubeSandbox should return an envd token and serve the v2 routes; spec 0013 §8.2 proposes the PR. Once it does, the `cube` flavour needs neither the flag nor the v1 routes.
- **API key.** The key is sent only to `HANGAR_E2B_API_URL`, never logged, and missing from every error message. For `cube`, CubeAPI can check keys against hangar through its `AUTH_CALLBACK_URL` (spec 0013 §8.3), which isn't built yet.
- **A sandbox's answers are untrusted.** Output is cut to a tail, reads stop at their limit, frames are capped and compression is refused, as for hangar-envd. Sandbox ids from the API are checked before they go into a URL path.
- **No loops.** The provider deliberately has no `envd` attribute, so hangar's E2B API and sandbox router never mistake it for a hangar-envd provider. hangar's own E2B API always creates on the default provider. A hangar whose default provider is its own `/e2b` gets a refusal ("has no hangar-envd"), not a loop. The tests check both.

## Deployment

Both platforms need `/dev/kvm` and host-level setup, and neither runs as an unprivileged Pod (spec 0013 §5.3).

### Option A: a nested-virtualization pool on GKE (start here)

`deploy/terraform/gke/microvm.tf` adds the pool, off by default:

```hcl
enable_microvm_pool  = true
microvm_machine_type = "c3-standard-8"   # c3, c4, c4d, n2, n4, n4d: series with nested virtualization
microvm_node_count   = 1                 # fixed: templates and snapshots are cached per node
microvm_hugepages_2m = 2048              # E2B: 4 GiB of 2 MiB hugepages before the kubelet starts; Cube: 0
microvm_local_ssds   = 0                 # Cube: raw local NVMe (a -lssd machine type), formatted XFS for /data/cubelet
```

The pool runs Ubuntu with nested virtualization (`advanced_machine_features.enable_nested_virtualization`), is labelled `hangar.dev/runtime=microvm` and `nested-virtualization=enabled`, and is tainted `hangar.dev/runtime=microvm:NoSchedule`. It was checked with `terraform fmt` and `terraform validate` (Terraform 1.13.4, google provider 8.7.0). Nothing was applied.

The constraints, from GKE's docs as spec 0013 §6.1 records them [G1][G2] (re-read on 2026-10-13):

- **Machines.** C3, C4, C4D, N2, N4 or N4D. Google's own ComputeClass example uses C3, C4 and C4D. E2, N2D, Arm and memory-optimized machines lack nested virtualization. GKE can't run `-metal` machine types [G4].
- **Immutable.** Nested virtualization can't be turned on for an existing pool, and the setting can't be changed later. An org policy that disables nested virtualization blocks it.
- **Privileged Pods.** Pods that use the nested VMs need `securityContext.privileged: true`. Both stacks also want `hostNetwork`. That is why the pool is tainted and dedicated: nothing else, including hangar's own sandboxes, may share these nodes.
- **ComputeClass instead of a pool.** A ComputeClass with `enableNestedVirtualization: true` needs GKE **1.37.0-gke.4713000** or later. It works with auto-created pools and Autopilot. On Autopilot, privileged Pods need a `WorkloadAllowlist`. Google's example:
  ```yaml
  apiVersion: cloud.google.com/v1
  kind: ComputeClass
  metadata: {name: microvm}
  spec:
    priorities:
    - {machineFamily: c3, spot: true, enableNestedVirtualization: true}
    - {machineFamily: c4, spot: false, enableNestedVirtualization: true}
    - {machineFamily: c4d, spot: true, enableNestedVirtualization: true}
    nodePoolConfig: {imageType: cos_containerd}   # use ubuntu_containerd for E2B (not COS)
    nodePoolAutoCreation: {enabled: true}
    whenUnsatisfiable: DoNotScaleUp
  ```
- **The cost.** Google states a CPU penalty of 10% or more for nested virtualization, and more for I/O-bound work [G1].
- **No PVM.** CubeSandbox's PVM kernel, its way to run without VT-x, needs a custom host kernel, which GKE doesn't allow. On GKE it is real nested KVM or nothing.

**CubeSandbox on the pool.** Cube's Helm chart installs four DaemonSets (the node agent "Big Pod", an installer, bootstrap, and PVM; leave PVM off), all `hostNetwork` [C12]. To fit this pool:

- add `nodeSelector: {hangar.dev/runtime: microvm}` and a toleration for the taint;
- give `/data/cubelet` an XFS filesystem with reflink, either formatted from local NVMe SSDs by the installer or, for a trial, as an XFS loop image on the boot disk;
- run CubeAPI, CubeMaster and CubeProxy as ordinary Deployments in the same namespace.

hangar then uses:

```bash
HANGAR_ENABLED_PROVIDERS='["kubernetes","e2b"]'
HANGAR_E2B_FLAVOR=cube
HANGAR_E2B_API_URL=http://cubeapi.cube-system.svc:3000
HANGAR_E2B_SANDBOX_URL='http://{port}-{sandbox_id}.cubeproxy.cube-system.svc.cluster.local'  # or your CubeProxy domain
HANGAR_E2B_ALLOW_UNAUTHENTICATED_ENVD=true   # only with the NetworkPolicy below
```

Pair it with a NetworkPolicy on the Cube namespace that admits ingress only from the hangar namespace.

**E2B self-hosted on the pool.** E2B's Embed is a one-node StatefulSet with `hostNetwork`, privileged init containers, NBD and 2 MiB hugepages [E4]. That is why the pool defaults to Ubuntu and 2048 hugepages. hangar then uses `HANGAR_E2B_FLAVOR=selfhosted`, `HANGAR_E2B_DOMAIN=<your E2B domain>`, `HANGAR_E2B_API_URL` if the API isn't at `api.<domain>`, and an E2B API key from that deployment.

### Option B: bare-metal C3 outside GKE

A managed instance group of `c3-standard-192-metal` runs CubeSandbox or E2B directly, and hangar reaches it over the VPC.

- **For:** no nested-virtualization penalty, so the published numbers apply, and very high density. Cube measured 1,000 idle 2 GiB VMs in about 25 GiB [C8].
- **Against:** the smallest metal machine has 192 vCPUs, about $9.68/h (≈ $7k/month; from a secondary source [G5]). It is billed per vCPU and memory like a VM, so it only pays above roughly 100 concurrent sandboxes. It needs a dedicated host and Hyperdisk, and it stops (`TERMINATE`) for maintenance, so plan for snapshots to be lost on maintenance. It is a second fleet to run, outside GKE's scheduling.

The provider settings are the same as on GKE, with addresses on the VPC. Restrict the firewall to the control plane's subnet. This is not in Terraform yet.

### Option C: GKE's managed `microvm` RuntimeClass

GKE's Agent Sandbox add-on allows a `microvm` RuntimeClass (managed Kata on Cloud Hypervisor), but node pools couldn't be created with it as of gcloud 588 [H2]. Its restore speed and snapshot API are unknown. Revisit it when it ships; it would plug in as the `kubernetes` provider with `runtime_class`, not as `e2b`.

## Expected latencies

These numbers come from the platforms' own published measurements, which spec 0013 §5.5 and §6.2 collected. Source tags ([C8], [G1] and so on) refer to spec 0013 §11. None of them were reproduced on GCP, and the GCP nested-virtualization numbers are not known yet: measuring them is P3's gate.

| | Create (p50 / p95) | Restore or fork | Memory per idle sandbox | Source and caveat |
| --- | --- | --- | --- | --- |
| CubeSandbox, bare metal (96 threads), concurrency 1 | avg 47.8 / p95 57.4 ms | 61–68 ms per clone from a snapshot, flat up to 600 MB dirty | 21.5 MB at 100 VMs, 25.7 MB at 1,000 | [C8]: Cube's own benchmark, `POST /sandboxes` until running, 2 vCPU / 2 GiB |
| CubeSandbox, bare metal, concurrency 50 | avg 276 / p95 508 ms | | | [C8]. The README's "P95 90 ms under 50 concurrent" doesn't match its own data |
| CubeSandbox, PVM cloud VM (16 cores), concurrency 1 | avg 66.7 / p95 78.2 ms | | 27–34 MB | [C9]: Tencent's PVM kernel, which GKE can't run |
| E2B | not published | fork: up to 100 per request from one checkpoint | | [E3]: no latency figure in the infra repo |
| GKE gVisor (hangar's default tier), for comparison | 1.5 s warm claim, 17 s cold | Pod Snapshot restore 0.3–1.25 s locally, "a few seconds" on GKE | the Pod's working set | hangar measurements, 2026-10-11; spec 0012 §4.3 |
| hangar Docker (runc), warm pool | 318 / 354 ms | | | spec 0013 §9 |

Plan on about 50–70 ms per create at low concurrency and 25–35 MB per idle VM on bare metal. Expect more on GCP nested virtualization: at least Google's 10% CPU penalty, and more for I/O, which is what restore is. Spec 0013's targets for the tier are create ≤ 100 / ≤ 250 ms at concurrency 10, and a memory fork of N = 10 in ≤ 1 s (§9, P4).

The provider adds little on top. One control-API round trip creates a sandbox, and the envd endpoint comes back in the same answer. Exec through E2B's envd measured 4.4 ms for a raw `Start` and 18 ms through the SDK's `bash -l` on loopback (spec 0013 §3.7). Dropping the second login shell removes one profile run from every call.

## Testing without KVM

`tests/test_e2b_provider.py` runs everything the provider does, in each flavour, with no microVM:

- **Against a fake control plane.** `FakeE2B` is a small FastAPI app with each flavour's routes and quirks:
  - CubeAPI's 405 on `POST /v2/sandboxes` and v2 connect, and its missing `envdAccessToken`;
  - a CubeProxy-like edge that checks the traffic token and authenticates to the daemon itself;
  - pagination, pause and connect with rotating tokens, snapshots, alias lookups and Cube's template build.

  Every fake sandbox runs a real hangar-envd daemon. hangar-envd accepts E2B's `X-Access-Token`, so commands, files and terminals cross the real envd protocol. The tests cover create (the v1 or v2 route per flavour), exec with exactly one login shell, the output and read caps, files and modes, a PTY, pause and resume keeping a process alive, a control-plane restart re-fetching tokens, timeouts, list, snapshots, kill, and the refusal of an unauthenticated envd.
- **Against hangar itself.** The `selfhosted` and `cube` flavours are pointed at another hangar's `/e2b` API and sandbox router, which runs local hangar-envd daemons. That is an end-to-end test of both sides. The same fixture checks that hangar can't loop through itself: a `fast` environment created through `/e2b` stays on the default provider, and a hangar whose default provider is its own `/e2b` refuses.
- **Tiers** (`fast` picks e2b, falls back when full and cools down; `isolated` never falls back; other errors don't fall back) on the fake fleet.
- **Hot resume** (`tests/test_hot_resume.py`) uses the session tests' scripted agent, real git and tmux:
  - suspend checkpoints, then pauses; the next turn resumes the same sandbox, with the ignored `.venv` still there;
  - a newer checkpoint, a reaped sandbox, or a failed resume each restore the checkpoint instead;
  - parked sandboxes survive a restart and go with an archive;
  - the cap evicts the longest-parked sandbox, and neither a rotating credential nor a nearly full fleet is parked.

## Follow-ups

1. **P3's live gate.** Run CubeSandbox and E2B Embed on one `c3-standard-8` with nested virtualization, then measure create, restore and the nested penalty against the table above.
2. **Upstream CubeSandbox.** Add the v2 create and connect routes and an envd token, then drop the `cube` special cases.
3. **hangar's edge as CubeAPI's auth callback** (spec 0013 §8.3).
4. **Native fork.** E2B's `POST /sandboxes/{id}/fork` and CubeSandbox's snapshot-and-clone for `fork_session` and best-of-N, instead of hangar's snapshot-then-create.
5. **hangar-envd as the template's envd.** A Cube or E2B template could carry hangar-envd, which is authenticated and smaller, so Cube templates wouldn't depend on `cubesandbox-base`.
6. **Option B in Terraform** (a bare-metal MIG), if A misses P4's gate.
