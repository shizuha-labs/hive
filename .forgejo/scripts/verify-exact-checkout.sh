#!/bin/sh
# POSIX sh (not bash): this runs in the alpine/git init container of the hive
# backend build job, which ships only busybox sh — a bash shebang here fails with
# `env: can't execute 'bash'` (exit 127) and silently blocks every backend build
# (regression introduced with PLAT-4086; hive backend was un-deployable until fixed
# 2026-07-10). Keep this dependency-free and POSIX.
set -eu

SHA="${1:?exact release SHA required}"
if ! printf '%s' "$SHA" | grep -qE '^[0-9a-f]{40}$'; then
  echo "::error::release SHA must be a full lowercase SHA"
  exit 2
fi

git fetch --no-tags --depth=1 origin "$SHA"
git checkout --detach "$SHA"
actual="$(git rev-parse HEAD)"
if [ "$actual" != "$SHA" ]; then
  echo "::error::exact release SHA checkout failed: wanted=${SHA} actual=${actual}"
  exit 1
fi
echo "exact release SHA checkout PASS sha=${SHA}"
