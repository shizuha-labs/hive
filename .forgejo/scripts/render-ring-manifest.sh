#!/usr/bin/env bash
set -euo pipefail
template="${1:?template required}"
image="${2:?image required}"
sha="${3:?full build SHA required}"
out="${4:?output path required}"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::invalid full build SHA" >&2; exit 2; }
short="${sha:0:7}"
[[ "$image" =~ ^localhost:30500/shizuha-hive:hive-[0-9a-f]{7}$ ]] || {
  echo "::error::unexpected Hive image: $image" >&2
  exit 2
}
sed -e "s|__IMAGE__|${image}|g" \
    -e "s|__BUILD_SHA__|${sha}|g" \
    -e "s|__BUILD_SHORT__|${short}|g" "$template" > "$out"
if grep -q '__[A-Z_]*__' "$out"; then
  echo "::error::unrendered placeholder in $out" >&2
  exit 1
fi
