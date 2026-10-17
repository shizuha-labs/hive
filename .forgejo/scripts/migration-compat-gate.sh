#!/usr/bin/env bash
set -euo pipefail

# INV-D. No migration change is a fast pass. Changed additive migrations are
# exercised both ways against the isolated R0 database; destructive operations
# fail closed for the separate PLAT-187 approval lane.
BASE_SHA="${1:?base SHA required}"
# HIVE-665: approval-only mode runs the PLAT-187 approval-doc check ALONE, with
# only BASE_SHA, so it can gate early (in the `test` job) and fail fast/loud/
# attributed to the offending commit instead of silently wedging the ring in the
# post-merge deploy-staging exercise. Images are not needed for this mode.
APPROVAL_ONLY="${MIGRATION_APPROVAL_ONLY:-0}"
if [[ "$APPROVAL_ONLY" == 1 ]]; then
  NEW_IMAGE="${2:-}"
  OLD_IMAGE="${3:-}"
else
  NEW_IMAGE="${2:?new image required}"
  OLD_IMAGE="${3:?old image required}"
fi
git cat-file -e "${BASE_SHA}^{commit}" || { echo "::error::base SHA unavailable"; exit 2; }
mapfile -t migrations < <(git diff --name-only "$BASE_SHA"...HEAD -- '*/migrations/*.py' | grep -v '/__init__.py$' || true)
if (( ${#migrations[@]} == 0 )); then
  echo "INV-D: no migration files changed"
  exit 0
fi
# PLAT-187 approval lane: a flagged migration passes only with a committed,
# reviewed approval doc at .forgejo/migration-approvals/<app>__<migration>.md
# (what it does, why it's safe, the reversible plan, who approved). Approved
# migrations still run the additive both-ways compat exercise below.
unapproved=()
for f in "${migrations[@]}"; do
  if grep -Eq 'migrations\.(RemoveField|DeleteModel|AlterField|RenameField|RunSQL|RunPython)' "$f"; then
    app="${f%%/*}"
    approval=".forgejo/migration-approvals/${app}__$(basename "$f" .py).md"
    if [[ -s "$approval" ]]; then
      echo "INV-D: flagged migration $f has PLAT-187 approval ($approval)"
    else
      unapproved+=("$f")
    fi
  fi
done
if (( ${#unapproved[@]} > 0 )); then
  printf '::error::destructive/semantic migration detected without PLAT-187 approval doc: %s — add .forgejo/migration-approvals/<app>__<migration>.md with a bespoke reversible plan\n' "${unapproved[*]}"
  exit 1
fi
# HIVE-665: in approval-only mode we are done once every flagged migration in the
# unreleased delta has its approval doc — the both-ways staging exercise (which
# needs the built images + the staging cluster) stays in the post-merge lane.
if [[ "$APPROVAL_ONLY" == 1 ]]; then
  printf 'INV-D (approval-only): PLAT-187 approval requirement satisfied for %d changed migration(s) in %s...HEAD\n' \
    "${#migrations[@]}" "$BASE_SHA"
  exit 0
fi
if [[ "${MIGRATION_COMPAT_PLAN_ONLY:-0}" == 1 ]]; then
  printf 'INV-D: additive compatibility exercise required for %d migration(s) from base=%s\n' \
    "${#migrations[@]}" "$BASE_SHA"
  exit 3
fi

NS="${MIGRATION_TEST_NAMESPACE:-staging}"
DB_DEPLOY=hive-staging-postgres
kubectl -n "$NS" rollout status deploy/"$DB_DEPLOY" --timeout=180s
kubectl -n "$NS" exec deploy/"$DB_DEPLOY" -- sh -lc \
  "PGPASSWORD=staging-only psql -U postgres -d shizuha_hive_staging -v ON_ERROR_STOP=1 -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'"

# Fail fast when an image cannot execute on this cluster (single-arch amd64 on
# arm64 nodes → exec format error). Mixed k3s fleets require multi-arch or an
# arch-matched tag for every schema-compat pod.
assert_image_runnable() {
  local image="$1" label="$2"
  local probe="compat-arch-probe-${label}-$$"
  kubectl -n "$NS" delete pod "$probe" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  # Prefer a platform node that matches the majority of the fleet (arm64 GB10).
  if ! kubectl -n "$NS" run "$probe" --restart=Never --image="$image" \
      --overrides='{"spec":{"nodeSelector":{"kubernetes.io/arch":"arm64"}}}' \
      --command -- /bin/sh -c 'python -c "import sys; print(sys.version)"' >/dev/null; then
    echo "::error::INV-D could not schedule arch probe for ${label} image=${image}"
    return 1
  fi
  if ! kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/"$probe" --timeout=120s; then
    echo "::error::INV-D ${label} image is not runnable on arm64 (exec format / pull failure): ${image}"
    kubectl -n "$NS" logs "$probe" --tail=50 || true
    kubectl -n "$NS" describe pod "$probe" | tail -30 || true
    kubectl -n "$NS" delete pod "$probe" --ignore-not-found --wait=false || true
    echo "::error::Use the live multi-arch Deployment image as OLD_IMAGE, not hive-\${last-successful-sha:0:7}."
    return 1
  fi
  kubectl -n "$NS" delete pod "$probe" --ignore-not-found --wait=true || true
}

assert_image_runnable "$OLD_IMAGE" old
assert_image_runnable "$NEW_IMAGE" new

run_image() {
  local name="$1" image="$2" command="$3"
  kubectl -n "$NS" delete pod "$name" --ignore-not-found --wait=true
  kubectl -n "$NS" run "$name" --restart=Never --image="$image" \
    --env=DB_NAME=shizuha_hive_staging --env=DB_USER=postgres --env=DB_PASSWORD=staging-only \
    --env=DB_HOST=hive-staging-postgres.staging.svc.cluster.local --env=DB_PORT=5432 \
    --env=REDIS_URL=redis://hive-staging-redis.staging.svc.cluster.local:6379/5 \
    --env=CACHE_REDIS_URL=redis://hive-staging-redis.staging.svc.cluster.local:6379/6 \
    --env=JWT_SECRET_KEY=staging-only-jwt --env=SHIZUHA_JWT_SIGNING_KEY=staging-only-jwt \
    --command -- /bin/sh -c "$command"
  if ! kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/"$name" --timeout=300s; then
    kubectl -n "$NS" logs "$name" --tail=200 || true
    # Surface the classic single-arch failure mode explicitly.
    if kubectl -n "$NS" logs "$name" 2>&1 | grep -q 'exec format error'; then
      echo "::error::INV-D pod ${name} hit exec format error for image=${image} (single-arch on wrong node)"
    fi
    return 1
  fi
  kubectl -n "$NS" logs "$name" --tail=100 || true
  kubectl -n "$NS" delete pod "$name" --wait=true
}
health_cmd='python manage.py check && python manage.py shell -c '"'"'from django.test import Client; r=Client().get("/api/health/"); assert r.status_code == 200, r.status_code'"'"
run_image compat-old-schema "$OLD_IMAGE" 'python manage.py migrate --noinput'
run_image compat-new-on-old "$NEW_IMAGE" "$health_cmd"
run_image compat-new-schema "$NEW_IMAGE" 'python manage.py migrate --noinput'
run_image compat-old-on-new "$OLD_IMAGE" "$health_cmd"
echo "INV-D: additive migration compatibility PASS old/new in both schema directions"
