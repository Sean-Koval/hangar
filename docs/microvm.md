# The microVM tier: E2B and CubeSandbox as hangar providers

!!! warning "Experimental, outside 1.0"
    The `e2b` provider is experimental and not part of hangar 1.0 (KAN-84). It refuses `ask`, `deny-risky` and governed turns by default (#402). hangar's own E2B-compatible API is gone ([removed surfaces](upgrading.md#removed-surfaces)); this provider, which runs hangar's sandboxes on E2B or CubeSandbox, stays.

hangar can run sandboxes as microVMs that start by restoring a memory snapshot, in tens of milliseconds rather than the second or more a container or a GKE gVisor Pod takes. It doesn't run the microVMs itself. It drives a platform that does, through its `e2b` provider, and stays the control plane: environments, jobs, sessions, the git gateway, budgets and the MCP API all work the same on top.

| Platform | VMM | How it is fast | Flavour |
| --- | --- | --- | --- |
| [E2B](https://e2b.dev) cloud or BYOC | Firecracker | Restores pre-booted template snapshots: memory loaded lazily with `userfaultfd`, copy-on-write rootfs over NBD | `cloud` |
| E2B self-hosted ([infra](https://github.com/e2b-dev/infra) / Embed) | Firecracker | Same, on your machines | `selfhosted` |
| [CubeSandbox](https://github.com/TencentCloud/CubeSandbox) | Cloud Hypervisor fork | Restores a memory image mapped `MAP_PRIVATE`, so every VM from one template shares its page cache; XFS reflink rootfs; a pre-created TAP pool | `cube` |

This is spec 0013's phase P3 and the deployment design for its P4 (§6, §8.2, §9). Nothing here was run on KVM: this machine has none. The tests run the provider against a fake control plane, with real envd daemons behind it (see [Testing](#testing-without-kvm)).

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

The heavy tier ([environments.md](environments/heavy-tier.md): Docker inside the sandbox, and the large and
xlarge profiles) can run here as well as on GKE's Kata pool, and on a cluster with GKE's Agent Sandbox add-on it has
to, since the add-on refuses privileged Kata pods ([gke.md](gke/microvms.md#4a-the-heavy-tier-optional)):

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

## Security

- **CubeSandbox's envd has no token.** CubeAPI returns `envdAccessToken: null` (spec 0013 §5.4, `services/sandboxes.rs`), so the envd inside a Cube sandbox answers anyone who can reach it. hangar therefore **refuses such sandboxes and kills them**, unless `HANGAR_E2B_ALLOW_UNAUTHENTICATED_ENVD=true`. Set that only when all of these hold:
  - CubeProxy and the VMs' network are reachable only from hangar's control plane. On GKE that means a NetworkPolicy on the microVM pool that admits only the hangar namespace; on bare metal, a VPC firewall rule from the control plane's subnet alone.
  - Every sandbox is created with `allowPublicTraffic=false`, which hangar always does. CubeProxy then checks the traffic token on every port, envd's included (`CubeProxy/lua/sandbox_backend.lua`), and only hangar has that token. hangar checks the part it can see: a sandbox that comes back with neither an envd token nor a traffic token (CubeMaster made it public) is refused and killed, flag or not.
  - The VMs' envd port is reachable only through CubeProxy, not directly on the node network.
  - Nothing else on that network runs untrusted code. The sandboxes themselves are isolated by Cube's eBPF policy, which denies private ranges by default.
- **Upstream fix.** CubeSandbox should return an envd token and serve the v2 routes; spec 0013 §8.2 proposes the PR. Once it does, the `cube` flavour needs neither the flag nor the v1 routes.
- **API key.** The key is sent only to `HANGAR_E2B_API_URL`, never logged, and missing from every error message. For `cube`, CubeAPI can check keys against hangar through its `AUTH_CALLBACK_URL` (spec 0013 §8.3), which isn't built yet.
- **A sandbox's answers are untrusted.** Output is cut to a tail, reads stop at their limit, frames are capped and compression is refused, as for hangar-envd. Sandbox ids from the API are checked before they go into a URL path.
- **Not a hangar-envd provider.** The provider deliberately has no `envd` attribute, so the preview proxy never mistakes it for one.

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

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="the-e2b-provider"></span>The e2b provider | [The e2b provider and hot resume](microvm/e2b.md#the-e2b-provider) |
| <span id="flavours"></span>Flavours | [The e2b provider and hot resume](microvm/e2b.md#flavours) |
| <span id="what-changed-from-the-old-provider"></span>What changed from the old provider | [The e2b provider and hot resume](microvm/e2b.md#what-changed-from-the-old-provider) |
| <span id="templates"></span>Templates | [The e2b provider and hot resume](microvm/e2b.md#templates) |
| <span id="settings"></span>Settings | [The e2b provider and hot resume](microvm/e2b.md#settings) |
| <span id="hot-resume"></span>Hot resume | [The e2b provider and hot resume](microvm/e2b.md#hot-resume) |
| <span id="deployment"></span>Deployment | [Deploying the microVM tiers](microvm/deployment.md) |
| <span id="option-a-a-nested-virtualization-pool-on-gke-start-here"></span>Option A: a nested-virtualization pool on GKE (start here) | [Deploying the microVM tiers](microvm/deployment.md#option-a-a-nested-virtualization-pool-on-gke-start-here) |
| <span id="option-b-bare-metal-c3-outside-gke"></span>Option B: bare-metal C3 outside GKE | [Deploying the microVM tiers](microvm/deployment.md#option-b-bare-metal-c3-outside-gke) |
| <span id="option-c-gkes-managed-microvm-runtimeclass"></span>Option C: GKE's managed `microvm` RuntimeClass | [Deploying the microVM tiers](microvm/deployment.md#option-c-gkes-managed-microvm-runtimeclass) |
