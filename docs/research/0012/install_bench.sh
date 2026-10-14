#!/usr/bin/env bash
# Dependency install time: empty cache vs warm cache vs an existing environment (what a baked layer gives).
# Spec 0012 §4.3. Run from anywhere; it works in a temporary directory and deletes it.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../../.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
t() { local s; s=$(date +%s.%N); "$@" >/dev/null 2>&1; echo "$(echo "$(date +%s.%N) - $s" | bc)"; }

# --- Python (uv): hangar's own lock, all extras, dev group
mkdir uvproj && cp "$REPO/pyproject.toml" "$REPO/uv.lock" uvproj/ && cd uvproj
export UV_CACHE_DIR=$PWD/cache
cold=$(t uv sync --frozen --no-install-project --all-extras)
pkgs=$(ls .venv/lib/python*/site-packages | wc -l)
rm -rf .venv
warm=$(t uv sync --frozen --no-install-project --all-extras)
noop=$(t uv sync --frozen --no-install-project --all-extras)
echo "uv: entries=$pkgs venv=$(du -sh .venv | cut -f1) cache=$(du -sh cache | cut -f1) cold=${cold}s warm_cache=${warm}s already_synced=${noop}s"
rm -rf .venv cache
cd ..

# --- Node (npm): a typical TS web toolchain
mkdir -p npmproj && cd npmproj
rm -rf node_modules package-lock.json cache
cat > package.json <<'EOF'
{"name":"bench","private":true,"devDependencies":{"typescript":"5.9.3","vite":"7.1.12","vitest":"3.2.4","eslint":"9.38.0","@types/node":"24.9.1","react":"19.2.0","react-dom":"19.2.0"}}
EOF
cold=$(t npm install --cache "$PWD/cache" --no-audit --no-fund)
rm -rf node_modules
warm=$(t npm ci --cache "$PWD/cache" --prefer-offline --no-audit --no-fund)
noop=$(t npm ci --cache "$PWD/cache" --prefer-offline --no-audit --no-fund)
echo "npm: node_modules=$(du -sh node_modules | cut -f1) cache=$(du -sh cache | cut -f1) cold=${cold}s warm_cache=${warm}s ci_again=${noop}s"
cd .. && rm -rf npmproj
