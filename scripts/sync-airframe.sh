#!/bin/sh
# Refresh the vendored Airframe theme (docs/stylesheets/airframe*.css) from the suite repo,
# Sean-Koval/agent-factory, which owns it. Take the files unmodified; the product hue lives in
# docs/stylesheets/product.css. Usage: sh scripts/sync-airframe.sh [ref]   (default: main)
set -eu

ref="${1:-main}"
base="https://raw.githubusercontent.com/Sean-Koval/agent-factory/${ref}/theme"
dest="$(cd "$(dirname "$0")/.." && pwd)/docs/stylesheets"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for f in airframe.css airframe-docs.css; do
  code="$(curl -sS -L -o "$tmp/$f" -w '%{http_code}' "$base/$f")" || {
    echo "sync-airframe: could not fetch $base/$f" >&2
    exit 1
  }
  if [ "$code" != "200" ]; then
    echo "sync-airframe: $base/$f returned HTTP $code (does the agent-factory repo exist and publish theme/$f on '$ref'?)" >&2
    exit 1
  fi
  if [ ! -s "$tmp/$f" ]; then
    echo "sync-airframe: $base/$f is empty" >&2
    exit 1
  fi
done

mkdir -p "$dest"
for f in airframe.css airframe-docs.css; do
  if cmp -s "$tmp/$f" "$dest/$f"; then
    echo "unchanged  docs/stylesheets/$f"
  else
    cp "$tmp/$f" "$dest/$f"
    echo "updated    docs/stylesheets/$f"
  fi
done
