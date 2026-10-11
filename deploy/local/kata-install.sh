#!/bin/bash
# hangar: install Kata Containers for Docker on a Linux or WSL2 host, so every sandbox is a microVM.
# Run with:  sudo bash deploy/local/kata-install.sh      (KATA_VERSION=4.2.0 by default)
# Needs: /dev/kvm (bare metal, a VM with nested virtualization, or WSL2), Docker 26+, systemd.
#
# What it does (and nothing else):
#   1. apt-installs zstd (to unpack the release archives)
#   2. unpacks the official Kata release archives into /opt/kata
#   3. adds two runtimes to /etc/docker/daemon.json, keeping any runtimes you already have:
#        kata     -> Rust runtime (Kata 4.x default)
#        kata-go  -> Go runtime (fallback)
#      a backup is written to /etc/docker/daemon.json.bak-hangar first
#   4. reloads (not restarts) dockerd, so your running containers keep running
#   5. boots one throwaway microVM and prints its kernel next to the host's
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -e /dev/kvm ] || { echo "no /dev/kvm: enable virtualization / nested virtualization first"; exit 1; }
command -v jq >/dev/null || { apt-get update -qq && apt-get install -y -qq jq >/dev/null; }
V=${KATA_VERSION:-4.2.0}; TMP=$(mktemp -d)
case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64) ARCH=arm64 ;; *) echo "unsupported arch $(uname -m)"; exit 1 ;; esac
trap 'rm -rf "$TMP"' EXIT

echo "== 1/5 zstd"
command -v zstd >/dev/null || { apt-get update -qq && apt-get install -y -qq zstd >/dev/null; }

echo "== 2/5 Kata $V -> /opt/kata"
for a in kata-static kata-go-static; do
  curl -fsSL -o "$TMP/$a.tar.zst" "https://github.com/kata-containers/kata-containers/releases/download/$V/$a-$V-$ARCH.tar.zst"
  tar --zstd -xf "$TMP/$a.tar.zst" -C /
done
ls /opt/kata/runtime-rs/bin/containerd-shim-kata-v2 /opt/kata/bin/containerd-shim-kata-v2

echo "== 3/5 docker runtimes"
CFG=/etc/docker/daemon.json
[ -f "$CFG" ] || echo '{}' > "$CFG"
cp -n "$CFG" "$CFG.bak-hangar" || true
jq '.runtimes.kata = {"runtimeType": "/opt/kata/runtime-rs/bin/containerd-shim-kata-v2",
                      "options": {"ConfigPath": "/opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-runtime-rs.toml"}}
  | .runtimes["kata-go"] = {"runtimeType": "/opt/kata/bin/containerd-shim-kata-v2"}' "$CFG" > "$TMP/daemon.json"
jq -e '.runtimes.kata and .runtimes["kata-go"]' "$TMP/daemon.json" >/dev/null
install -m 644 "$TMP/daemon.json" "$CFG"

echo "== 4/5 reload dockerd (containers keep running)"
systemctl reload docker
sleep 2
docker info --format 'runtimes: {{range $k,$v := .Runtimes}}{{$k}} {{end}}'

echo "== 5/5 microVM check"
echo "host kernel:  $(uname -r)"
for rt in kata kata-go; do
  echo -n "$rt guest kernel: "
  timeout 120 docker run --rm --runtime "$rt" mirror.gcr.io/library/busybox:1.36 uname -r 2>&1 | tail -1 || echo "(failed)"
done
echo "done. Undo: sudo cp $CFG.bak-hangar $CFG && sudo systemctl reload docker && sudo rm -rf /opt/kata"
