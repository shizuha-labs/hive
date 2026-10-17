#!/bin/bash
# Sourced by .ci-local-gate.sh while it owns hive-gate.volume.lock. PostgreSQL's
# clean shutdown protects WAL, not Django's test isolation: SIGTERM can prevent
# TransactionTestCase's flush and --keepdb will reuse those committed fixtures.
run_local_gate_backend() {
  local pg="$1"; shift
  local marker=/var/lib/postgresql/data/.hive-gate-backend-clean-v1
  local state result
  if [[ ! "$pg" =~ ^hive-gate-pg-[0-9]+$ ]] || [[ "$#" -eq 0 ]]; then
    echo 'gate: refusing cache operation outside the local test PostgreSQL caller' >&2
    return 1
  fi
  # A failed read is not evidence that the cache is clean (or absent).
  state=$(docker exec "$pg" sh -c 'if [ -f "$1" ]; then cat "$1"; else printf absent; fi' sh "$marker") || return $?
  # Invalidate BEFORE any test or DROP. An interrupted reset remains dirty too.
  docker exec "$pg" rm -f "$marker" || return $?
  if [[ "$state" != clean-v1 ]]; then
    echo 'gate: prior backend run absent/unclean — recreating only test_shizuha_hive_ci' >&2
    docker exec "$pg" dropdb -U postgres --if-exists --force test_shizuha_hive_ci || return $?
  else
    echo 'gate: prior backend completed cleanly — retaining warm test schema'
  fi
  if "$@"; then
    # Only the completed backend command may rearm reuse. EXIT/signal cleanup
    # deliberately cannot mark an interrupted run clean.
    docker exec "$pg" sh -c 'printf clean-v1 > "$1.tmp" && mv "$1.tmp" "$1"' sh "$marker" || return $?
  else
    result=$?
    echo "gate: backend failed/interrupted (exit=$result); test cache remains dirty" >&2
    return "$result"
  fi
}
