#!/usr/bin/env sh
# Static, stripped, LTO-optimized hangar-envd binaries in envd/dist/hangar-envd-<arch>, where hangar looks for them
# (or set HANGAR_ENVD_DIR). Needs rustup; cross builds link with the rust-lld that ships with Rust, so no C
# toolchain or musl-gcc is needed. Cargo.lock pins every dependency (--locked).
#
#   envd/build.sh                     # x86_64 and aarch64
#   ARCHES=x86_64 envd/build.sh       # just one
set -eu
cd "$(dirname "$0")"
ARCHES="${ARCHES:-x86_64 aarch64}"
TARGET_DIR="${CARGO_TARGET_DIR:-target}"
HOST="$(rustc -vV | sed -n 's/^host: //p')"
for a in $ARCHES; do
  t="$a-unknown-linux-musl"
  rustup target add "$t" >/dev/null
  if [ "$t" = "$HOST" ]; then
    # A musl host (rust:alpine): build scripts and proc macros are linked for this same target, dynamically, which
    # rust-lld can't do on its own; the system linker can.
    cargo build --release --locked --target "$t"
  else
    env "CARGO_TARGET_$(echo "$t" | tr 'a-z-' 'A-Z_')_LINKER=rust-lld" \
      cargo build --release --locked --target "$t"
  fi
  mkdir -p dist
  install -m 0755 "$TARGET_DIR/$t/release/hangar-envd" "dist/hangar-envd-$a"
  echo "dist/hangar-envd-$a: $(wc -c < "dist/hangar-envd-$a") bytes"
done
