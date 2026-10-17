#!/usr/bin/env bash
# Publish successful Hive rollouts to Pulse's idempotent deployment ledger.
# Pulse then creates the focused/recent-regression QA work for the live surfaces.
set -euo pipefail

usage() {
  echo "usage: $0 <service> <component> <image> [qa-surface,...]" >&2
  exit 64
}

[ "$#" -ge 3 ] && [ "$#" -le 4 ] || usage
SERVICE="$1"
COMPONENT="$2"
IMAGE="$3"
QA_SURFACES="${4:-/hive/agents,/hive/subscriptions}"
IMAGE_DIGEST="${DEPLOYMENT_EVENT_IMAGE_DIGEST:-}"
if [ -n "$IMAGE_DIGEST" ] && ! [[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "::error::DEPLOYMENT_EVENT_IMAGE_DIGEST must be a full sha256 digest" >&2
  exit 2
fi

REPO="${GITHUB_REPOSITORY:-shizuha-labs/hive}"
SHA="${GITHUB_SHA:-${RELEASE_SHA:-}}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "::error::GITHUB_SHA or RELEASE_SHA must be a full lowercase SHA" >&2
  exit 2
}
BRANCH="${GITHUB_REF_NAME:-main}"
RUN_ID="${GITHUB_RUN_ID:-${GITHUB_RUN_NUMBER:-unknown}}"
DEPLOYMENT_ENVIRONMENT="${DEPLOYMENT_ENVIRONMENT:-live}"
if [ "$DEPLOYMENT_ENVIRONMENT" != live ]; then
  echo "::error::backend DeploymentEvents are production-only; rejected environment=${DEPLOYMENT_ENVIRONMENT}" >&2
  exit 2
fi
PULSE_URL="${PULSE_URL:-http://shizuha-pulse.shizuha.svc.cluster.local:8002}"
ID_LOGIN_URL="${ID_LOGIN_URL:-http://shizuha-id.shizuha.svc.cluster.local:8001/api/auth/login/}"

if [ "${DEPLOYMENT_EVENT_DRY_RUN:-0}" != "1" ]; then
  # Refresh access on every run when bootstrap credentials exist. Optional
  # lookups preserve token-only installations; a failed configured login does
  # not fall back to a copied, possibly expired access token.
  PULSE_USERNAME="${PULSE_USERNAME:-$(kubectl -n origin get secret pulse-api-token -o jsonpath='{.data.username}' 2>/dev/null | base64 -d || true)}"
  PULSE_PASSWORD="${PULSE_PASSWORD:-$(kubectl -n origin get secret pulse-api-token -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)}"
  if [ -n "$PULSE_USERNAME" ] && [ -n "$PULSE_PASSWORD" ]; then
    login_payload="$(PULSE_USERNAME="$PULSE_USERNAME" PULSE_PASSWORD="$PULSE_PASSWORD" python3 -c 'import json,os; print(json.dumps({"username":os.environ["PULSE_USERNAME"],"password":os.environ["PULSE_PASSWORD"]}))')"
    login_response="$(curl -fsS \
      --retry 4 --retry-all-errors --retry-delay 2 --retry-max-time 75 \
      --connect-timeout 5 --max-time 15 \
      -H 'Content-Type: application/json' -d "$login_payload" "$ID_LOGIN_URL")" || {
      echo "::error::ID login failed while minting a fresh Pulse access token" >&2
      exit 1
    }
    PULSE_TOKEN="$(printf '%s' "$login_response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tokens"]["access"])')"
    unset PULSE_PASSWORD login_payload login_response
  elif [ -z "${PULSE_TOKEN:-}" ]; then
    token_b64="$(kubectl -n origin get secret pulse-api-token -o jsonpath='{.data.token}' || true)"
    if [ -n "$token_b64" ]; then
      PULSE_TOKEN="$(printf '%s' "$token_b64" | base64 -d)"
    fi
  fi
  [ -n "${PULSE_TOKEN:-}" ] || { echo "::error::Pulse login returned no access token" >&2; exit 1; }
fi

# PLAT-7421: publish observed convergence for this exact image, not just a
# healthy Deployment which may already belong to a successor release. This
# remains post-commit evidence; refusal cannot roll a healthy release back.
CONVERGENCE='{}'
if [ "${DEPLOYMENT_EVENT_DRY_RUN:-0}" != "1" ]; then
  deployment_json="$(kubectl -n shizuha-hive get "deployment/${SERVICE}" -o json)" || exit 1
  CONVERGENCE="$(printf '%s' "$deployment_json" | EXPECTED_IMAGE="$IMAGE" EXPECTED_SERVICE="$SERVICE" python3 -c '
import json, os, sys
row = json.load(sys.stdin)
spec, status, meta = row.get("spec", {}), row.get("status", {}), row.get("metadata", {})
containers = spec.get("template", {}).get("spec", {}).get("containers", [])
images = [c.get("image") for c in containers if c.get("name") == os.environ["EXPECTED_SERVICE"]]
desired, generation = spec.get("replicas"), meta.get("generation")
counts = [status.get(k) for k in ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas")]
if not (
    meta.get("name") == os.environ["EXPECTED_SERVICE"]
    and meta.get("namespace") == "shizuha-hive"
    and meta.get("uid") and meta.get("resourceVersion")
    and not meta.get("deletionTimestamp")
    and images == [os.environ["EXPECTED_IMAGE"]]
    and type(desired) is int and desired > 0
    and all(type(n) is int and n == desired for n in counts)
    and status.get("unavailableReplicas", 0) == 0
    and type(generation) is int and generation > 0
    and type(status.get("observedGeneration")) is int
    and status["observedGeneration"] == generation
):
    sys.exit("::error::Deployment is not converged at the expected image and generation; no event published")
print(json.dumps({
    "replicas_ready": True,
    "desired_replicas": desired,
    "ready_replicas": counts[1],
    "updated_replicas": counts[2],
    "available_replicas": counts[3],
    "generation": generation,
    "observed_generation": status["observedGeneration"],
    "deployment_uid": meta["uid"],
    "deployment_resource_version": meta["resourceVersion"],
    "observed_image": images[0],
}, separators=(",", ":")))
')" || exit 1
  unset deployment_json
fi

export REPO SHA BRANCH RUN_ID SERVICE COMPONENT IMAGE IMAGE_DIGEST QA_SURFACES
export CONVERGENCE
payload="$(python3 - <<'PY'
import json
import os

surfaces = [value.strip() for value in os.environ['QA_SURFACES'].split(',') if value.strip()]
print(json.dumps({
    'idempotency_key': ':'.join([
        'origin', os.environ['REPO'], os.environ['SERVICE'], os.environ['COMPONENT'], os.environ['SHA'],
    ]),
    'project_key': 'HIVE',
    'repo': os.environ['REPO'],
    'branch': os.environ['BRANCH'],
    'commit_sha': os.environ['SHA'],
    'service': os.environ['SERVICE'],
    'component': os.environ['COMPONENT'],
    'environment': 'live',
    'cluster': 'shizuha-k3s',
    'namespace': 'shizuha-hive',
    'deployment_target': os.environ['COMPONENT'],
    'image': os.environ['IMAGE'],
    'image_digest': os.environ['IMAGE_DIGEST'],
    'pipeline_system': 'origin-forgejo',
    'pipeline_run_id': os.environ['RUN_ID'],
    'state': 'smoke_passed',
    'smoke_result': 'passed',
    'metadata': {
        'risk_labels': ['requires-live-qa', 'user-facing', 'agent-runtime'],
        'qa_surfaces': surfaces,
        'truth_contract': 'hive-health-accounting-v1',
        # Dry-run deliberately has no observed readiness evidence.
        **json.loads(os.environ['CONVERGENCE']),
    },
}, separators=(',', ':')))
PY
)"

if [ "${DEPLOYMENT_EVENT_DRY_RUN:-0}" = "1" ]; then
  printf '%s\n' "$payload"
  exit 0
fi

IDEMPOTENCY_KEY="$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin)["idempotency_key"])')"
# A POST timeout is indeterminate: Pulse may commit the DeploymentEvent after
# the runner gives up waiting for its response (Origin run 1403 did exactly
# that). Replay the identical body and explicit key on bounded transport,
# 408/429/5xx, and other retryable failures. Pulse's unique idempotency key
# makes the replay a read-after-uncertain-write recovery, not a duplicate.
response="$(curl -fsS \
  --retry 4 --retry-all-errors --retry-delay 2 --retry-max-time 150 \
  --connect-timeout 5 --max-time 30 -X POST \
  -H "Authorization: Bearer ${PULSE_TOKEN}" \
  -H "Idempotency-Key: ${IDEMPOTENCY_KEY}" \
  -H 'Content-Type: application/json' \
  -d "$payload" \
  "${PULSE_URL}/api/deployment-events/ingest/")"
unset PULSE_TOKEN IDEMPOTENCY_KEY payload
printf '%s' "$response" | python3 -c '
import json, sys
row = json.load(sys.stdin)
print("Pulse DeploymentEvent {} state={} qa_task={}".format(
    row.get("id"), row.get("state"), row.get("qa_task_key")
))
'
