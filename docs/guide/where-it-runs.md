# Where it runs

Where hangar's sandboxes can run, how each place isolates them, and what has been proven live so far.

## Providers

| Where | How | Isolation |
| --- | --- | --- |
| Laptop | `hangar serve`, or `docker compose up` in `deploy/compose` | runc containers |
| Your Linux or WSL2 box | `sudo bash deploy/local/kata-install.sh`, then `HANGAR_DOCKER_RUNTIME=kata hangar serve` | Kata microVMs |
| Single Linux VM | `deploy/terraform/single-vm`: a GCE VM with nested virt, running Compose | gVisor (`runsc`) or Kata microVMs |
| GKE | `deploy/terraform/gke` + `deploy/k8s` | Agent Sandbox with gVisor; optional Kata pool |
| E2B | `HANGAR_ENABLED_PROVIDERS='["docker","e2b"]'` with `HANGAR_E2B_API_KEY` | Firecracker microVMs |

You can enable several providers on one control plane and pick one per sandbox. See [Running on GKE](../gke.md) for
the cluster walkthrough, [local microVMs](../local-microvms.md) for Kata on your own box and
[the E2B backend](../microvm.md).

hangar uses a single control-plane replica with SQLite state. Kubernetes forks copy workspace files; do not assume
they preserve process memory. E2B, Pod snapshots and Kata on GKE have not yet been validated live
([known limits](#known-limits)).

## The daemon in each sandbox

On Docker and Kubernetes, hangar starts a small daemon in each sandbox, [hangar-envd](../envd.md). Commands, file
transfers and terminals run through it instead of `docker exec` or the Kubernetes exec API. An `exec` takes about 7 ms
instead of 70 on Docker.

It speaks E2B's envd protocol. Build it with `envd/build.sh` (the control-plane image includes it); without it, or
with `HANGAR_ENVD=off`, hangar uses exec as before.

Because of that, hangar also answers E2B's API. The e2b SDKs, and the agent frameworks built on them, run in your
hangar sandboxes after you change `E2B_API_KEY` and a URL. See [the E2B-compatible API](../e2b.md).

## Known limits

- **Forks on Kubernetes** copy `/workspace` as a tarball; GKE can only restore a sandbox from its own snapshots. With
  Pod snapshots enabled, Kubernetes sandboxes can be paused and resumed with memory intact (see
  [Running on GKE](../gke.md)). On Docker and E2B, forks are full snapshots.
- **What has run for real:**
    - **Docker provider:** end to end in CI.
    - **Kubernetes provider:** end to end over MCP against the real Agent Sandbox v1.0.6 controller on kind, with the
      runc runtime. This covered builds pushed to a registry, claims, exec, 300 KB file writes, terminals, TTL
      extension, forks and teardown.
    - **Kata microVMs (Docker provider):** end to end on WSL2. Each sandbox is a VM with its own kernel; it starts in
      about 2 s, and 4 start in parallel in 2.2 s. See [local microVMs](../local-microvms.md).
    - **GKE with the Agent Sandbox add-on and gVisor:** end to end, with the control plane in the cluster behind a
      Google-managed certificate. A remote MCP client created two sandboxes from the warm pool over HTTPS in 3.1 s.
      Each had the repo cloned on its own branch, a Claude Code token and a fine-grained GitHub token.
    - **That GKE run, continued:** it predates
      [subscription logins being interactive-only](../team.md#subscription-logins); fleets use API keys. Claude Code
      ran headless in both sandboxes, made a change with tests, and opened a PR that passed CI
      ([#13](https://github.com/Sean-Koval/hangar/pull/13), [#14](https://github.com/Sean-Koval/hangar/pull/14)).
      `scripts/agent_fleet_demo.py` reproduces the run.
    - **Not yet run live:** Kata on GKE (the add-on only admits `gvisor` and `microvm`; see
      [Running on GKE](../gke.md)), Pod snapshots and E2B. They're covered by unit tests against fake APIs.
- **Antigravity (`agy`) subscription login** stores OAuth in the OS keyring, which headless containers may lack. Use a
  `GEMINI_API_KEY` credential for fleets.
- **Spend caps live in two places.** Set a workspace spend limit on each API key in the vendor's console (Anthropic
  Console, OpenAI project limits); hangar's job and daily budgets cap spend inside hangar.
- **The control plane is a single replica** with SQLite state, which is right for a personal or team instance.
