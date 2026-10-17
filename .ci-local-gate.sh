#!/bin/bash
# Freeze this file before running: editing an executing Bash script invalidates
# its read offset. Wait for the process to exit before changing this gate.
# Local mirror of ci-test-hive (Origin deploy.yml `test` job): full backend
# suite in the SAME image lineage CI uses, against throwaway pg. Blocks
# primary-branch pushes via .githooks/pre-push.
#
# Speed (2026-07-14): test-db state persists on the named docker volume
# `hive-gate-pgdata` and the suite runs --keepdb, so repeat pushes skip
# CREATE DATABASE + migrations (measured: 1m55 cold -> 1m26 warm, 934 tests).
# A completion marker preserves the warm database only after a clean backend
# run; failed/interrupted runs rebuild only the disposable test database.
# If a rebased/edited migration ever wedges the cached db:
#   docker volume rm hive-gate-pgdata
# NOTE: --parallel is NOT enabled — 16 tests share redis/prometheus state and
# fail under parallel workers (isolation debt filed with engineering); the
# serial suite itself is only ~80s.
set -euo pipefail
# Concurrent gate runs share the hive-gate-pgdata volume: two postgres
# containers on one data dir corrupt it (then every later run wedges in crash
# recovery). flock queues concurrent pushes; 15min cap then fail loud.
exec 9>/tmp/hive-gate.volume.lock
flock -w 900 9 || { echo "gate: another gate run held the lock >15min — investigate (docker ps | grep hive-gate)" >&2; exit 1; }
cd "$(git rev-parse --show-toplevel)"
source .forgejo/scripts/local-gate-test-cache.sh
IMG="${SHIZUHA_CI_IMAGE:-}"
if [ -z "$IMG" ] && command -v kubectl >/dev/null 2>&1; then
  IMG=$(kubectl get deploy -n shizuha-hive hive -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
fi
IMG="${IMG:-localhost:30500/shizuha-hive:hive-1cc9de5}"  # pinned fallback
NET="hive-gate-$$"; PG="hive-gate-pg-$$"; RD="hive-gate-redis-$$"
cleanup() {
  # The persistent test database can take over a minute to fsync after the full
  # suite.  Killing it after 20s leaves an interrupted shutdown, so the next
  # coordinator spends its whole readiness budget in crash recovery and then
  # discards the otherwise-valid warm cache.  Redis is disposable; PostgreSQL
  # is not.
  docker stop -t 120 "$PG" >/dev/null 2>&1 || true
  docker stop -t 10 "$RD" >/dev/null 2>&1 || true
  docker rm -f "$PG" "$RD" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker volume create hive-gate-pgdata >/dev/null
docker network create "$NET" >/dev/null
docker run --cpus 20 --cpu-shares 512 -d --name "$PG" --network "$NET" -v hive-gate-pgdata:/var/lib/postgresql/data -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=shizuha_hive_ci postgres:16-alpine >/dev/null
docker run --cpus 20 --cpu-shares 512 -d --name "$RD" --network "$NET" redis:7-alpine >/dev/null
wait_pg() {
  for _i in $(seq 1 90); do
    docker exec "$PG" psql -U postgres -h 127.0.0.1 -c "SELECT 1" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
if ! wait_pg; then
  # Cached volume wedged (interrupted WAL recovery etc.) — it is only a cache:
  # recreate it fresh and retry once (next run repays migrations, then warm again).
  echo "gate: postgres not ready — recreating cache volume hive-gate-pgdata and retrying" >&2
  docker rm -f "$PG" >/dev/null 2>&1
  # A stopped container owned by another invocation is not ours to delete.
  # Docker refuses removal while any such consumer still references the cache.
  docker volume rm hive-gate-pgdata >/dev/null 2>&1
  docker volume create hive-gate-pgdata >/dev/null
  docker run --cpus 20 --cpu-shares 512 -d --name "$PG" --network "$NET" -v hive-gate-pgdata:/var/lib/postgresql/data -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=shizuha_hive_ci postgres:16-alpine >/dev/null
  wait_pg || {
    # SCLI-6xx self-diagnosis (operator 2026-09-22): a blind retry loop is not
    # a diagnosis. Read the dying container's own last lines and classify.
    echo "gate: postgres STILL not ready — diagnosing from container logs:" >&2
    docker logs "$PG" 2>&1 | grep -iE "error|fatal|no space" | tail -3 >&2 || true
    if docker logs "$PG" 2>&1 | grep -qi "no space left on device"; then
      echo "gate: ROOT CAUSE = docker root disk FULL (ENOSPC). Free space:" >&2
      df -h "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null)" 2>/dev/null | tail -1 >&2
      echo "gate: refusing unrelated builder/container/volume cleanup; invocation-owned resources are released by the EXIT trap." >&2
      echo "gate: investigate Docker storage ownership and retention before re-running." >&2
      exit 1
    fi
    echo "gate: postgres STILL not ready — aborting" >&2
    exit 1
  }
fi
echo "gate: image=$IMG (mounting working tree over /app code dirs)"
run_local_gate_backend "$PG" docker run --cpus 20 --cpu-shares 512 --rm --network "$NET" \
  -v "$PWD/fleet":/app/fleet -v "$PWD/claws":/app/claws -v "$PWD/analytics":/app/analytics \
  -v "$PWD/waitlist":/app/waitlist -v "$PWD/security":/app/security -v "$PWD/hive_project":/app/hive_project \
  -v "$PWD/hive_platform":/app/hive_platform -v "$PWD/credentials":/app/credentials \
  -v "$PWD/k8s":/app/k8s -v "$PWD/.forgejo":/app/.forgejo -v "$PWD/tests":/app/tests \
  -v "$PWD/mcp-server":/app/mcp-server:ro \
  -e SECRET_KEY=ci-secret -e JWT_SECRET_KEY=ci-secret -e SHIZUHA_JWT_SIGNING_KEY=ci-secret -e DEBUG=True \
  -e HIVE_TOKEN_COORDINATOR_KEK="$(python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')" \
  -e HIVE_TOKEN_COORDINATOR_KEK_ID=ci-test-kek \
  -e DB_NAME=shizuha_hive_ci -e DB_USER=postgres -e DB_PASSWORD=postgres -e DB_HOST="$PG" -e DB_PORT=5432 \
  -e REDIS_URL="redis://$RD:6379/5" -e CACHE_REDIS_URL="redis://$RD:6379/6" \
  "$IMG" /bin/bash -c \
  'set -euo pipefail
   # SCLI-376: token revocation is consulted for EVERY token and fails closed.
   # The gate container has no shizuha-id; without this stub every valid JWT
   # looks revoked (DNS fail-closed) and the suite reds on auth. Mirror of
   # Origin deploy.yml / premerge.yml.
   python3 - <<'"'"'PY'"'"' &
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = b"{\"active\": true}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
HTTPServer(("127.0.0.1", 19999), H).serve_forever()
PY
   REVOCATION_STUB_PID=$!
   trap "kill $REVOCATION_STUB_PID >/dev/null 2>&1 || true" EXIT
   for i in $(seq 1 10); do
     if python3 -c "import urllib.request; urllib.request.urlopen(urllib.request.Request(\"http://127.0.0.1:19999/api/oauth/revocation-status\", data=b\"{}\", method=\"POST\"), timeout=1)" >/dev/null 2>&1; then break; fi
     sleep 1
   done
   export SHIZUHA_EXPECTED_ISSUER=http://127.0.0.1:19999
   /app/.venv/bin/python manage.py makemigrations --check --dry-run &&
   /app/.venv/bin/python .forgejo/scripts/check-django-migration-graph.py &&
   /app/.venv/bin/python -m compileall -q claws fleet hive_project manage.py &&
   /app/.venv/bin/python manage.py check &&
   /app/.venv/bin/python tests/test_deployment_event_recorder.py &&
   /app/.venv/bin/python tests/test_hive623_pulse_preflight.py &&
   /app/.venv/bin/python tests/test_pulse_fleet_read_client_provisioning.py &&
   /app/.venv/bin/python tests/test_pulse_writer_auth.py &&
   /app/.venv/bin/python manage.py test --noinput --keepdb &&
   /app/.venv/bin/python tests/test_plat4086_rollout_rings.py RingFenceContractTests'

# Run frontend tests and build check if node/npm is available
if [ -d "$PWD/frontend" ] && command -v npm >/dev/null 2>&1; then
  echo "gate: running frontend test suite and vite build check"
  (cd "$PWD/frontend" && nice -n 15 npm test -- --run && VITE_BASE_PATH=/hive/ nice -n 15 npm run build >/dev/null)
fi
