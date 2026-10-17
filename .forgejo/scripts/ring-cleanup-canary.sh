#!/usr/bin/env bash
set -Eeuo pipefail

SHA="${RELEASE_SHA:?release SHA required}"
cleanup_failure() {
  local original_rc="$1" detail="$2"
  trap - ERR TERM INT
  set +e
  .forgejo/scripts/record-release-cleanup-event.sh firing fleet "$SHA" "$detail" || \
    echo "::error::failed to publish Hive cleanup alert"
  echo "::error::fleet release retained; post-commit canary cleanup failed" >&2
  exit "$original_rc"
}
trap 'cleanup_failure "$?" "command=${BASH_COMMAND}"' ERR
trap 'cleanup_failure 143 "signal=TERM"' TERM
trap 'cleanup_failure 130 "signal=INT"' INT

.forgejo/scripts/retire-canary-probe.sh
.forgejo/scripts/kubectl -n shizuha-hive delete deploy hive-canary --ignore-not-found --wait=true
.forgejo/scripts/kubectl -n shizuha-hive delete svc hive-canary --ignore-not-found
.forgejo/scripts/record-release-cleanup-event.sh resolved fleet "$SHA" "cleanup-converged" || \
  echo "::warning::failed to resolve prior Hive cleanup alert"
trap - ERR TERM INT
echo "Hive post-commit canary cleanup converged for ${SHA:0:7}"
