#!/bin/sh
# Refresh this repo's vendored brand files (README banner, favicon, header mark, social preview) from
# Sean-Koval/agent-factory, which generates them from Airframe (scripts/banners.py there). Take the files
# unmodified and never edit them here; change Airframe or the banner copy in agent-factory instead.
#
#   sh scripts/sync-brand.sh theme-v14            # a release tag
#   sh scripts/sync-brand.sh                      # main
#   sh scripts/sync-brand.sh --check              # are the files what docs/assets/brand/SOURCE says? (the docs CI)
#   sh scripts/sync-brand.sh --check main         # ... or what agent-factory main has now (drift)
#
# A sync records the agent-factory commit it took the files from, and each file's sha256, in
# docs/assets/brand/SOURCE; tests/test_brand_sync.py holds the files to those hashes, and --check fetches them
# again at that commit and compares. Re-run it whenever agent-factory regenerates the banners (a new theme-vN).
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
REPO="Sean-Koval/agent-factory"

root="$(cd "$(dirname "$0")/.." && pwd)"
source_file="$root/docs/assets/brand/SOURCE"
check=0
if [ "${1:-}" = "--check" ]; then check=1; shift; fi

if [ "$check" = 1 ] && [ -z "${1:-}" ]; then
  # the commit the last sync recorded
  ref="$(awk '$1 == "commit" {print $2}' "$source_file")"
  [ -n "$ref" ] || { echo "sync-brand: no commit in $source_file; run a sync first" >&2; exit 1; }
else
  ref="${1:-main}"
fi

# Resolve a tag or branch to the commit it names now, so SOURCE pins exactly what was taken.
case "$ref" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) commit="$ref" ;;
  *)
    auth=""
    [ -n "${GITHUB_TOKEN:-}" ] && auth="Authorization: Bearer $GITHUB_TOKEN"
    commit="$(curl -sS -L -H "Accept: application/vnd.github.sha" ${auth:+-H "$auth"} \
      "https://api.github.com/repos/$REPO/commits/$ref")" || { echo "sync-brand: could not resolve $ref" >&2; exit 1; }
    case "$commit" in
      *[!0-9a-f]* | "") echo "sync-brand: $REPO has no ref '$ref' ($commit)" >&2; exit 1 ;;
    esac ;;
esac

base="https://raw.githubusercontent.com/$REPO/${commit}/brand/${PRODUCT}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# fetch each distinct file once, and fail before touching anything if one is missing
for f in $(printf '%s\n' "$FILES" | awk 'NF {print $1}' | sort -u); do
  code="$(curl -sS -L -o "$tmp/$f" -w '%{http_code}' "$base/$f")" || {
    echo "sync-brand: could not fetch $base/$f" >&2; exit 1; }
  if [ "$code" != "200" ]; then
    echo "sync-brand: $base/$f returned HTTP $code (does '$ref' have brand/$PRODUCT/$f?)" >&2; exit 1
  fi
  [ -s "$tmp/$f" ] || { echo "sync-brand: $base/$f is empty" >&2; exit 1; }
done

if [ "$check" = 1 ]; then
  stale="$(printf '%s\n' "$FILES" | awk 'NF {print $1, $2}' | while read -r f dest; do
    cmp -s "$tmp/$f" "$root/$dest" || echo "  $dest differs from $REPO@$ref brand/$PRODUCT/$f"
  done)"
  if [ -n "$stale" ]; then
    printf 'sync-brand: the vendored brand files are not %s@%s:\n%s\nRun: sh scripts/sync-brand.sh <ref>\n' \
      "$REPO" "$ref" "$stale" >&2
    exit 1
  fi
  echo "sync-brand: every brand file matches $REPO@$ref"
  exit 0
fi

printf '%s\n' "$FILES" | awk 'NF {print $1, $2}' | while read -r f dest; do
  mkdir -p "$(dirname "$root/$dest")"
  if cmp -s "$tmp/$f" "$root/$dest"; then
    echo "unchanged  $dest"
  else
    cp "$tmp/$f" "$root/$dest"
    echo "updated    $dest"
  fi
done

{
  echo "# Written by scripts/sync-brand.sh: where the vendored brand files came from. Do not edit; re-run the sync."
  echo "ref $ref"
  echo "commit $commit"
  printf '%s\n' "$FILES" | awk 'NF {print $2}' | while read -r dest; do
    echo "sha256 $(sha256sum "$root/$dest" | cut -d' ' -f1) $dest"
  done
} > "$source_file"
echo "wrote      docs/assets/brand/SOURCE ($ref, $commit)"
