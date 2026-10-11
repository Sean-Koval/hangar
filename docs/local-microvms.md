# MicroVM sandboxes on your own machine

With Kata Containers registered in Docker, hangar's Docker provider runs every sandbox as a lightweight VM with its own kernel. Nothing else in hangar changes: the same environments, MCP tools, forks and dashboard all work.

## Requirements

- **`/dev/kvm`.** Bare-metal Linux, a cloud VM with nested virtualization (for example a GCE N2 instance with `enable_nested_virtualization`), or **WSL2 on Windows**.
- **Docker 26 or newer**, running under systemd.
- **Not macOS.** Docker Desktop on a Mac can't nest KVM, so a Mac gets containers. For microVMs from a Mac, run hangar on a Linux or WSL2 box and connect to it remotely (for example over Tailscale).

## Install

```bash
sudo bash deploy/local/kata-install.sh
```

This installs the official Kata 4.2.0 release into `/opt/kata` and registers two runtimes with Docker: `kata` (the Rust runtime, Kata's default) and `kata-go` (the Go fallback). It merges into your existing `/etc/docker/daemon.json` after writing a backup, then *reloads* Docker so running containers aren't restarted. It finishes by booting one test VM per runtime. Undo with the one-liner it prints.

## Run hangar on microVMs

```bash
HANGAR_DOCKER_RUNTIME=kata uv run hangar serve
```

The dashboard's provider badge and `fleet_overview` then report `microvm: true`. Each environment's `resources` (`cpus`, `memory_gb`) set the size of its VMs.

## Verified on

On WSL2 (Windows 11, 16 cores) with Kata 4.2.0 and Docker 26.1, `tests/test_e2e_kata.py` passed (run it with `HANGAR_E2E_KATA=1`):

| Operation | Time |
|---|---|
| Create a sandbox (new VM) | about 2 s |
| Fork into 2 VMs from a full snapshot | 2.3 s |
| 4 VMs in parallel | 2.2 s |

Every sandbox reported its own guest kernel and boot ID. Files, terminals, pause/resume and teardown all worked through the VM boundary.

Running Kata *inside* a kind or k3s node on WSL2 doesn't work: the VM boots, but the runtime can't reach the guest agent three virtualization layers deep. Use this Docker path locally, and Kata on a GKE nested-virtualization pool in the cloud (see [gke.md](gke.md)).
