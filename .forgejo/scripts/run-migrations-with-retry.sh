#!/bin/bash
# Run Hive migrations with bounded retries for transient database startup only.
# Exit 42 is terminal under the fleet Job's podFailurePolicy; exit 75 asks the
# Job controller to retry after the inner network/primary convergence window.
set -uo pipefail

PYTHON="${HIVE_MIGRATION_PYTHON:-/app/.venv/bin/python}"
ATTEMPTS="${HIVE_MIGRATION_ATTEMPTS:-24}"
RETRY_SECONDS="${HIVE_MIGRATION_RETRY_SECONDS:-5}"

if ! "$PYTHON" -c 'import django'; then
  echo "Hive image does not provide Django through ${PYTHON}" >&2
  exit 42
fi

for attempt in $(seq 1 "$ATTEMPTS"); do
  output=$("$PYTHON" manage.py migrate --noinput 2>&1)
  status=$?
  printf '%s\n' "$output"
  [ "$status" -eq 0 ] && exit 0

  if ! grep -Eqi \
    'connection refused|could not connect|connection.*timed out|timeout expired|server closed the connection unexpectedly|database system is starting up|no route to host|name or service not known|temporary failure in name resolution' \
    <<<"$output"; then
    echo "non-retryable Django migration failure (exit ${status})" >&2
    exit 42
  fi

  echo "transient database connection failure on migration attempt ${attempt}/${ATTEMPTS}; waiting for policy or primary convergence"
  [ "$attempt" -eq "$ATTEMPTS" ] || sleep "$RETRY_SECONDS"
done

exit 75
