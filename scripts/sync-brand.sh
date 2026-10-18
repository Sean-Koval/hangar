#!/bin/sh
# Refresh this repo's vendored brand files (README banner, favicon, header mark, social preview) from
# Sean-Koval/agent-factory, which generates them from Airframe (scripts/banners.py there). Take the files
# unmodified and never edit them here; change Airframe or the banner copy in agent-factory instead.
#
#   sh scripts/sync-brand.sh theme-v4     # a release tag
#   sh scripts/sync-brand.sh              # main
#
# Template: agent-factory brand/sync-brand.sh. Each tool repo keeps a copy as scripts/sync-brand.sh with
# PRODUCT set to its folder under brand/ and FILES listing "<brand file> <destination>" pairs.
set -eu

PRODUCT="hangar"
FILES="
banner-dark.svg    docs/assets/brand/banner-dark.svg
banner-light.svg   docs/assets/brand/banner-light.svg
social.png         docs/assets/brand/social.png
favicon.svg        docs/assets/brand/favicon.svg
favicon.svg        src/hangar/ui/favicon.svg
mark.svg           docs/assets/brand/mark.svg
"

ref="${1:-main}"
base="https://raw.githubusercontent.com/Sean-Koval/agent-factory/${ref}/brand/${PRODUCT}"
root="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# fetch each distinct file once, and fail before touching anything if one is missing
for f in $(printf '%s\n' "$FILES" | awk 'NF {print $1}' | sort -u); do
  code="$(curl -sS -L -o "$tmp/$f" -w '%{http_code}' "$base/$f")" || {
    echo "sync-brand: could not fetch $base/$f" >&2; exit 1; }
  if [ "$code" != "200" ]; then
    echo "sync-brand: $base/$f returned HTTP $code (is '$ref' a published ref that has brand/$PRODUCT/$f?)" >&2; exit 1
  fi
  [ -s "$tmp/$f" ] || { echo "sync-brand: $base/$f is empty" >&2; exit 1; }
done

printf '%s\n' "$FILES" | awk 'NF {print $1, $2}' | while read -r f dest; do
  mkdir -p "$(dirname "$root/$dest")"
  if cmp -s "$tmp/$f" "$root/$dest"; then
    echo "unchanged  $dest"
  else
    cp "$tmp/$f" "$root/$dest"
    echo "updated    $dest"
  fi
done
