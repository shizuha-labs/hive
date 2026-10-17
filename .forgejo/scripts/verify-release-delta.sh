#!/usr/bin/env bash
set -euo pipefail

ANCHOR_SHA="${1:?verified live anchor SHA required}"
RELEASE_SHA="${2:?release SHA required}"
MODE="${3:-allow-migrations}"
[[ "$ANCHOR_SHA" =~ ^[0-9a-f]{40}$ && "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "::error::anchor and release must be full lowercase SHAs"
  exit 2
}

git cat-file -e "${ANCHOR_SHA}^{commit}" || { echo "::error::rollback anchor commit unavailable"; exit 1; }
git cat-file -e "${RELEASE_SHA}^{commit}" || { echo "::error::release commit unavailable"; exit 1; }
git merge-base --is-ancestor "$ANCHOR_SHA" "$RELEASE_SHA" || {
  echo "::error::rollback anchor is not an ancestor of release SHA; cannot prove INV-D"
  exit 1
}

mapfile -t migrations < <(git diff --name-only "$ANCHOR_SHA...$RELEASE_SHA" -- '*/migrations/*.py')
if [[ "$MODE" == reject-migrations && ${#migrations[@]} -gt 0 ]]; then
  printf '::error::break-glass fleet cannot bypass INV-D; anchor-to-release delta changes migrations:\n' >&2
  printf '  %s\n' "${migrations[@]}" >&2
  exit 1
fi
printf 'INV-D release delta PASS anchor=%s release=%s migrations=%d mode=%s\n' \
  "$ANCHOR_SHA" "$RELEASE_SHA" "${#migrations[@]}" "$MODE"
