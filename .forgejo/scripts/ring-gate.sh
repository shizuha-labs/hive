#!/usr/bin/env bash
set -Eeuo pipefail

# Fail-closed progressive-delivery gate.  HIVE-736 replaces the historical
# one-shot Service curl with a typed, bounded convergence primitive.  The same
# primitive runs before and after the bake so endpoint replacement or replica
# changes reset confidence instead of becoming an unexplained HTTP 000.

RING="${1:-}"
NAMESPACE="${2:-}"
SERVICE="${3:-}"
BUILD_SHA="${4:-${BUILD_SHA:-}}"
REGISTRY_URL="${REGISTRY_URL:-http://registry.registry.svc.cluster.local:5000}"
POLL_SECONDS="${POLL_SECONDS:-20}"
# The convergence budget must cover the Deployment's recovery contract, not
# merely its happy path. Production Hive requires 180 continuous Ready seconds,
# while its conservative liveness probe permits 90 seconds of sustained failure
# before repairing a wedged ASGI worker. A recovered replica can therefore need
# more than 270 seconds before topology is stable enough for the three
# independent confirmations below; the historical 300-second budget expired at
# 2/3 confirmations during run 1377 and rolled back a recovered release.
# This remains a health-SAMPLED, fail-closed gate: a longer budget tolerates one
# bounded repair, but recurring restarts keep changing topology and cannot pass.
CONVERGENCE_TIMEOUT_SECONDS="${CONVERGENCE_TIMEOUT_SECONDS:-600}"
CONVERGENCE_POLL_SECONDS="${CONVERGENCE_POLL_SECONDS:-2}"
CONVERGENCE_SUCCESS_SAMPLES="${CONVERGENCE_SUCCESS_SAMPLES:-3}"
CONVERGENCE_JITTER_MILLISECONDS="${CONVERGENCE_JITTER_MILLISECONDS:-400}"
INDEPENDENT_PROBE_TIMEOUT_SECONDS="${INDEPENDENT_PROBE_TIMEOUT_SECONDS:-30}"
TOPOLOGY_HELPER="${TOPOLOGY_HELPER:-$(dirname "$0")/ring-topology.py}"

case "$RING" in
  staging)
    DEFAULT_SOAK=300; MIN_BAKE=60; FAST_BAKE=30
    TARGET_DEPLOY="$SERVICE"; TARGET_SERVICE="$SERVICE"
    HEALTH_URL="http://${SERVICE}.${NAMESPACE}.svc.cluster.local:8030/api/health/"
    ;;
  canary)
    DEFAULT_SOAK=600; MIN_BAKE=120; FAST_BAKE=45
    TARGET_DEPLOY="${SERVICE}-canary"; TARGET_SERVICE="${SERVICE}-canary"
    HEALTH_URL="http://${SERVICE}-canary.${NAMESPACE}.svc.cluster.local:8030/api/health/"
    ;;
  fleet)
    DEFAULT_SOAK=900; MIN_BAKE=90; FAST_BAKE=45
    TARGET_DEPLOY="$SERVICE"; TARGET_SERVICE="$SERVICE"
    HEALTH_URL="http://${SERVICE}.${NAMESPACE}.svc.cluster.local:8030/api/health/"
    ;;
  *)
    echo "usage: ring-gate.sh staging|canary|fleet <namespace> <service> <40-char-build-sha>" >&2
    exit 2
    ;;
esac

SOAK_SECONDS="${SOAK_SECONDS:-$DEFAULT_SOAK}"
MIN_BAKE_SECONDS="${MIN_BAKE_SECONDS:-$MIN_BAKE}"
FAST_BAKE_SECONDS="${FAST_BAKE_SECONDS:-$FAST_BAKE}"
MIN_SAMPLES="${MIN_SAMPLES:-3}"
FAST_LATENCY_MS="${FAST_LATENCY_MS:-1500}"
[[ "$BUILD_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::BUILD_SHA must be a full 40-char lowercase SHA"; exit 2; }
[[ "$SOAK_SECONDS" =~ ^[0-9]+$ && "$POLL_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo "::error::invalid soak/poll duration"; exit 2; }
[[ "$CONVERGENCE_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ && "$CONVERGENCE_POLL_SECONDS" =~ ^[1-9][0-9]*$ && "$CONVERGENCE_SUCCESS_SAMPLES" =~ ^[1-9][0-9]*$ && "$CONVERGENCE_JITTER_MILLISECONDS" =~ ^[0-9]+$ ]] || { echo "::error::invalid convergence configuration"; exit 2; }
[[ -r "$TOPOLOGY_HELPER" ]] || { echo "::error::topology helper unavailable: $TOPOLOGY_HELPER"; exit 2; }

MARGINAL=0

remaining_budget() {
  local deadline="$1" remaining
  remaining=$((deadline - SECONDS))
  (( remaining > 0 )) || return 1
  printf '%s' "$remaining"
}

bounded_command() {
  local deadline="$1" remaining
  shift
  remaining="$(remaining_budget "$deadline")" || return 124
  timeout --signal=TERM "${remaining}s" "$@"
}

bounded_kubectl() {
  local deadline="$1" remaining
  shift
  remaining="$(remaining_budget "$deadline")" || return 124
  timeout --signal=TERM "${remaining}s" kubectl --request-timeout="${remaining}s" "$@"
}

topology_snapshot() {
  local deadline="$1" directory rc=0
  directory="$(mktemp -d)"
  set +e
  bounded_kubectl "$deadline" -n "$NAMESPACE" get deployment "$TARGET_DEPLOY" -o json >"$directory/deployment.json" 2>"$directory/error"
  rc=$?
  (( rc == 0 )) && { bounded_kubectl "$deadline" -n "$NAMESPACE" get service "$TARGET_SERVICE" -o json >"$directory/service.json" 2>>"$directory/error"; rc=$?; }
  (( rc == 0 )) && { bounded_kubectl "$deadline" -n "$NAMESPACE" get replicasets.apps -o json >"$directory/replicasets.json" 2>>"$directory/error"; rc=$?; }
  (( rc == 0 )) && { bounded_kubectl "$deadline" -n "$NAMESPACE" get pods -o json >"$directory/pods.json" 2>>"$directory/error"; rc=$?; }
  (( rc == 0 )) && { bounded_kubectl "$deadline" -n "$NAMESPACE" get endpointslices.discovery.k8s.io \
      -l "kubernetes.io/service-name=${TARGET_SERVICE}" -o json >"$directory/slices.json" 2>>"$directory/error"; rc=$?; }
  set -e
  if (( rc != 0 )); then
    printf '{"outcome":"UNKNOWN","reason":"Kubernetes topology read failed or exceeded the remaining convergence budget (rc=%s)"}\n' "$rc"
    rm -rf "$directory"
    return 0
  fi
  set +e
  bounded_command "$deadline" python3 "$TOPOLOGY_HELPER" \
    "$directory/deployment.json" "$directory/service.json" \
    "$directory/replicasets.json" "$directory/pods.json" \
    "$directory/slices.json" "$BUILD_SHA" >"$directory/snapshot.json"
  rc=$?
  set -e
  if (( rc == 0 )); then
    cat "$directory/snapshot.json"
  else
    printf '{"outcome":"UNKNOWN","reason":"Topology parser failed or exceeded the remaining convergence budget (rc=%s)"}\n' "$rc"
  fi
  rm -rf "$directory"
}

runner_probe() {
  local url="$1" deadline="$2" remaining connect_timeout max_time out rc
  remaining="$(remaining_budget "$deadline")" || { printf '000 unavailable'; return 0; }
  max_time="$remaining"; (( max_time > 8 )) && max_time=8
  connect_timeout="$max_time"; (( connect_timeout > 3 )) && connect_timeout=3
  set +e
  # Ring probes target cluster-internal Services and raw pod IPs; they must
  # never traverse the job container's egress proxy. The runner injects
  # http_proxy (apt-cache) with CIDR no_proxy entries the job image's curl
  # (7.81 < 7.86) cannot match, so a proxied pod-IP probe returns the
  # cache's 403 and wedges convergence as DATA_PLANE_UNREACHABLE.
  out="$(timeout --signal=TERM "${remaining}s" curl -sS --noproxy '*' --connect-timeout "$connect_timeout" --max-time "$max_time" -o /dev/null -w '%{http_code} %{time_total}' "$url" 2>/dev/null)"
  rc=$?
  set -e
  if (( rc != 0 )) || [[ ! "$out" =~ ^[0-9]{3}[[:space:]] ]]; then
    printf '000 unavailable'
  else
    printf '%s' "$out"
  fi
}

# Query from an existing blackbox-exporter pod through Kubernetes exec.  The
# HTTP request therefore originates from an identified, non-runner in-cluster
# network namespace, while the runner only uses the Kubernetes API transport.
# This is synchronous (no Prometheus discovery wait) and read-only.
independent_probe() {
  local phase="$1" url="$2" deadline="$3" pod_json selected name uid node ip container log rc remaining exec_timeout
  set +e
  pod_json="$(bounded_kubectl "$deadline" -n monitoring get pods -l app.kubernetes.io/name=blackbox-exporter -o json 2>/dev/null)"
  rc=$?
  set -e
  if (( rc != 0 )); then
    INDEPENDENT_CODE=000; INDEPENDENT_TIME=unavailable
    INDEPENDENT_IDENTITY="blackbox_exporter_list_failed phase=${phase}"
    return 1
  fi
  set +e
  selected="$(printf '%s' "$pod_json" | bounded_command "$deadline" python3 -c 'import json,sys
try:
 pods=json.load(sys.stdin).get("items",[])
 ready=[]
 for pod in pods:
  meta=pod.get("metadata",{}); status=pod.get("status",{}); spec=pod.get("spec",{})
  is_ready=status.get("phase")=="Running" and meta.get("deletionTimestamp") is None and any(c.get("type")=="Ready" and c.get("status")=="True" for c in status.get("conditions",[]))
  if is_ready and status.get("podIP") and spec.get("nodeName") and spec.get("containers"):
   ready.append((meta.get("name",""),meta.get("uid",""),spec.get("nodeName",""),status.get("podIP",""),spec["containers"][0].get("name","")))
 if not ready: raise ValueError("no Ready blackbox-exporter pod")
 print("\t".join(sorted(ready)[0]))
except Exception as exc:
 print("ERROR\t%s"%exc)' )"
  rc=$?
  set -e
  if (( rc != 0 )) || [[ "$selected" == ERROR$'\t'* || -z "$selected" ]]; then
    INDEPENDENT_CODE=000; INDEPENDENT_TIME=unavailable
    INDEPENDENT_IDENTITY="${selected:-ERROR no_blackbox_identity} phase=${phase} parser_rc=${rc}"
    return 1
  fi
  IFS=$'\t' read -r name uid node ip container <<<"$selected"
  INDEPENDENT_IDENTITY="pod=${name} uid=${uid} node=${node} ip=${ip} container=${container}"
  remaining="$(remaining_budget "$deadline")" || {
    INDEPENDENT_CODE=000; INDEPENDENT_TIME=unavailable
    INDEPENDENT_IDENTITY="${INDEPENDENT_IDENTITY} convergence_budget_exhausted=true"
    return 1
  }
  exec_timeout="$INDEPENDENT_PROBE_TIMEOUT_SECONDS"
  (( exec_timeout > remaining )) && exec_timeout="$remaining"
  set +e
  log="$(timeout --signal=TERM "${exec_timeout}s" kubectl --request-timeout="${exec_timeout}s" -n monitoring exec "$name" -c "$container" -- \
    /bin/sh -c 'set +e
output="$(wget -S -O /dev/null -T 8 "$1" 2>&1)"
rc=$?
code="$(printf "%s\n" "$output" | awk '\''/HTTP\// {code=$2} END {print code}'\'')"
[ -n "$code" ] || { [ "$rc" -eq 0 ] && code=200 || code=000; }
printf "%s\n" "$code"' probe "$url" 2>/dev/null)"
  rc=$?
  set -e
  if (( rc != 0 )) || [[ ! "$log" =~ ^[0-9]{3}$ ]]; then
    INDEPENDENT_CODE=000; INDEPENDENT_TIME=unavailable
    INDEPENDENT_IDENTITY="${INDEPENDENT_IDENTITY} exec_rc=${rc}"
    return 1
  fi
  INDEPENDENT_CODE="$log"
  INDEPENDENT_TIME=remote
  return 0
}

snapshot_fields() {
  local deadline="$1" snapshot="$2"
  printf '%s' "$snapshot" | bounded_command "$deadline" python3 -c 'import json,sys
value=json.load(sys.stdin)
print("\t".join(str(value.get(field, "")).replace("\t", " ").replace("\n", " ") for field in ("outcome", "reason", "signature", "pod_ip")))'
}

is_non_2xx_http() {
  [[ "$1" =~ ^[1-5][0-9][0-9]$ && "$1" != 200 ]]
}


converge_ring_target() {
  local phase="$1" deadline snapshot fields outcome reason signature pod_ip rc remaining
  local stable_signature="" successes=0 attempt=0
  local runner_service runner_direct runner_code direct_code last_outcome=UNKNOWN last_reason="no observation" jitter_ms sleep_seconds
  deadline=$((SECONDS + CONVERGENCE_TIMEOUT_SECONDS))
  echo "=== ${RING} ${phase} convergence build=${BUILD_SHA} deadline=${CONVERGENCE_TIMEOUT_SECONDS}s ==="
  while :; do
    attempt=$((attempt + 1))
    snapshot="$(topology_snapshot "$deadline")"
    set +e
    fields="$(snapshot_fields "$deadline" "$snapshot")"
    rc=$?
    set -e
    if (( rc == 0 )); then
      IFS=$'\t' read -r outcome reason signature pod_ip <<<"$fields"
    else
      outcome=UNKNOWN; reason="topology result parser exceeded the remaining convergence budget"; signature=""; pod_ip=""
    fi

    if [[ "$outcome" != STABLE ]]; then
      successes=0; stable_signature=""
      last_outcome="${outcome:-UNKNOWN}"; last_reason="$reason"
      echo "convergence phase=${phase} attempt=${attempt} outcome=${last_outcome} reason=${reason}"
    else
      if [[ "$signature" != "$stable_signature" ]]; then
        successes=0
        stable_signature="$signature"
        echo "convergence phase=${phase} attempt=${attempt} topology=${signature:0:12} changed=true success_streak_reset=true"
      fi
      runner_service="$(runner_probe "$HEALTH_URL" "$deadline")"
      runner_direct="$(runner_probe "http://${pod_ip}:8030/api/health/" "$deadline")"
      runner_code="${runner_service%% *}"
      direct_code="${runner_direct%% *}"
      independent_probe "$phase" "$HEALTH_URL" "$deadline" || true
      echo "convergence phase=${phase} attempt=${attempt} topology=${signature:0:12} runner_service=${runner_service} runner_pod=${runner_direct} independent=${INDEPENDENT_CODE} ${INDEPENDENT_TIME} identity='${INDEPENDENT_IDENTITY}'"

      if [[ "$runner_code" == 200 && "$direct_code" == 200 && "$INDEPENDENT_CODE" == 200 ]]; then
        successes=$((successes + 1))
        last_outcome="HEALTHY"; last_reason="all exact-target vantages returned HTTP 200"
        echo "convergence phase=${phase} stable_success=${successes}/${CONVERGENCE_SUCCESS_SAMPLES}"
        if (( successes >= CONVERGENCE_SUCCESS_SAMPLES )); then
          echo "=== ${RING} ${phase} convergence PASS topology=${signature} ==="
          return 0
        fi
      elif [[ "$INDEPENDENT_CODE" == 200 && ( "$runner_code" != 200 || "$direct_code" != 200 ) ]]; then
        successes=0; last_outcome="DATA_PLANE_UNREACHABLE"
        last_reason="independent exact-target probe is healthy while runner paths are Service=${runner_code} direct-pod=${direct_code}"
      elif is_non_2xx_http "$INDEPENDENT_CODE" && { [[ "$runner_code" == "$INDEPENDENT_CODE" ]] || [[ "$direct_code" == "$INDEPENDENT_CODE" ]]; }; then
        echo "::error::outcome=APP_UNHEALTHY phase=${phase} stable exact target returned HTTP ${INDEPENDENT_CODE} from independent and matching runner vantage"
        return 1
      elif [[ "$runner_code" == 200 || "$direct_code" == 200 ]]; then
        successes=0; last_outcome="UNKNOWN"
        last_reason="exact-target evidence disagrees: Service=${runner_code} direct-pod=${direct_code} independent=${INDEPENDENT_CODE}"
      elif [[ "$runner_code" == 000 && "$direct_code" == 000 && "$INDEPENDENT_CODE" == 000 ]]; then
        successes=0; last_outcome="UNKNOWN"
        last_reason="no vantage produced an HTTP response"
      else
        successes=0; last_outcome="UNKNOWN"
        last_reason="ambiguous exact-target evidence: Service=${runner_code} direct-pod=${direct_code} independent=${INDEPENDENT_CODE}"
      fi
      echo "convergence phase=${phase} attempt=${attempt} outcome=${last_outcome} reason=${last_reason}"
    fi

    remaining=$((deadline - SECONDS))
    if (( remaining <= 0 )); then
      echo "::error::outcome=${last_outcome} phase=${phase} deadline=${CONVERGENCE_TIMEOUT_SECONDS}s reason=${last_reason}"
      return 1
    fi
    jitter_ms=0
    (( CONVERGENCE_JITTER_MILLISECONDS > 0 )) && jitter_ms=$((RANDOM % (CONVERGENCE_JITTER_MILLISECONDS + 1)))
    sleep_seconds="$(awk -v base="$CONVERGENCE_POLL_SECONDS" -v jitter="$jitter_ms" -v remaining="$remaining" 'BEGIN{value=base + jitter/1000; if (value > remaining) value=remaining; printf "%.3f", value}')"
    sleep "$sleep_seconds"
  done
}

echo "=== ${RING} gate build=${BUILD_SHA} soak=${SOAK_SECONDS}s target=${TARGET_SERVICE} ==="
converge_ring_target pre_bake

# INV-B: fail closed and prove the referenced rollback image is present before R1.
if [[ "$RING" == canary ]]; then
  LAST_SHA="$(kubectl -n "$NAMESPACE" get deploy "$SERVICE" -o jsonpath='{.metadata.annotations.last-successful-sha}')"
  [[ "$LAST_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::missing/invalid last-successful-sha rollback anchor"; exit 1; }
  ROLLBACK_TAG="hive-${LAST_SHA:0:7}"
  curl -fsS --noproxy '*' --max-time 20 -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
    "${REGISTRY_URL}/v2/shizuha-hive/manifests/${ROLLBACK_TAG}" >/dev/null ||
    { echo "::error::rollback image ${ROLLBACK_TAG} is absent"; exit 1; }
fi

probe_health() {
  local out code latency latency_ms attempt attempt_deadline
  for attempt in 1 2 3; do
    attempt_deadline=$((SECONDS + 8))
    out="$(runner_probe "$HEALTH_URL" "$attempt_deadline")"
    code="${out%% *}"; latency="${out##* }"
    if [[ "$code" == 200 ]]; then
      latency_ms="$(awk -v x="$latency" 'BEGIN{printf "%d", x*1000}')"
      if (( attempt > 1 || latency_ms > FAST_LATENCY_MS )); then
        (( MARGINAL == 0 )) && echo "  (ring MARGINAL: attempt=${attempt} latency=${latency_ms}ms — conservative bake)"
        MARGINAL=1
      fi
      return 0
    fi
    (( attempt < 3 )) && sleep 3
  done
  echo "bake runner-only probe returned HTTP ${code} three times; entering typed exact-target convergence"
  MARGINAL=1
  converge_ring_target bake_failure
}

start=$SECONDS
samples=0
elapsed=0
while :; do
  probe_health
  samples=$((samples + 1))
  elapsed=$((SECONDS - start))
  echo "sample=${samples} elapsed=${elapsed}s health=200 target=${HEALTH_URL}"
  bake="$MIN_BAKE_SECONDS"; (( MARGINAL == 0 )) && bake="$FAST_BAKE_SECONDS"
  if (( elapsed >= bake && samples >= MIN_SAMPLES )); then
    echo "=== ${RING} health established: ${samples} samples over ${elapsed}s (bake ${bake}s marginal=${MARGINAL}) ==="
    break
  fi
  (( elapsed >= SOAK_SECONDS )) && break
  sleep_for="$POLL_SECONDS"
  (( elapsed + sleep_for > SOAK_SECONDS )) && sleep_for=$((SOAK_SECONDS - elapsed))
  (( sleep_for > 0 )) && sleep "$sleep_for"
done

(( samples >= MIN_SAMPLES )) || { echo "::error::${RING} gate saw only ${samples} healthy probes (< MIN_SAMPLES ${MIN_SAMPLES})"; exit 1; }
converge_ring_target post_bake
echo "=== ${RING} gate PASS build=${BUILD_SHA} samples=${samples} elapsed=${elapsed}s ==="
