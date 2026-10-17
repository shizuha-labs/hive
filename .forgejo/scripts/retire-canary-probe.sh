#!/usr/bin/env bash
# HIVE-707: fence Prometheus before replacing/removing a Hive ring target.
#
# Deleting the Service/Deployment while the Probe is still active produces a
# deterministic probe_success=0 scrape and a false ShizuhaServiceDown page.
# Remove every Probe for the selected release ring first, then wait until every
# Prometheus HA replica no longer has the Probe's generated scrape pool; pool
# absence guarantees its target is no longer active. Fail closed: callers must
# leave the Service/Deployment intact when this fence cannot be proven.
set -Eeuo pipefail

PROMETHEUS_NAMESPACE="${PROMETHEUS_NAMESPACE:-monitoring}"
PROMETHEUS_SERVICE="${PROMETHEUS_SERVICE:-prometheus-operated}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
PROMETHEUS_CONTAINER="${PROMETHEUS_CONTAINER:-prometheus}"
RELEASE_RING="${1:-canary}"
case "$RELEASE_RING" in
  canary) DEFAULT_TARGET_NEEDLE="hive-canary.shizuha-hive" ;;
  staging) DEFAULT_TARGET_NEEDLE="hive.staging.svc.cluster.local" ;;
  *) echo "::error::unsupported Hive release ring: ${RELEASE_RING}" >&2; exit 2 ;;
esac
TARGET_NEEDLE="${2:-$DEFAULT_TARGET_NEEDLE}"
[[ -n "$TARGET_NEEDLE" ]] || { echo "::error::empty Prometheus target identity" >&2; exit 2; }
SCRAPE_POOL_PREFIX="${PROMETHEUS_SCRAPE_POOL_PREFIX:-probe/${PROMETHEUS_NAMESPACE}/probe-shizuha-hive-${RELEASE_RING}-}"
[[ -n "$SCRAPE_POOL_PREFIX" ]] || { echo "::error::empty Prometheus scrape-pool prefix" >&2; exit 2; }
TIMEOUT_SECONDS="${CANARY_PROBE_RETIRE_TIMEOUT_SECONDS:-300}"
POLL_SECONDS="${CANARY_PROBE_RETIRE_POLL_SECONDS:-2}"
PROMETHEUS_READ_TIMEOUT_SECONDS="${PROMETHEUS_READ_TIMEOUT_SECONDS:-15}"
[[ "$PROMETHEUS_READ_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
  echo "::error::PROMETHEUS_READ_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
}
RECONCILE_INTERVAL_SECONDS="${PROMETHEUS_RECONCILE_INTERVAL_SECONDS:-5}"
[[ "$RECONCILE_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || {
  echo "::error::invalid Prometheus reconcile interval: ${RECONCILE_INTERVAL_SECONDS}" >&2
  exit 2
}

kubectl -n "$PROMETHEUS_NAMESPACE" delete probe \
  -l "service=hive,release_ring=${RELEASE_RING}" \
  --ignore-not-found --wait=true
kubectl -n "$PROMETHEUS_NAMESPACE" delete probe \
  -l "service=hive-reconcile,release_ring=reconcile-${RELEASE_RING}" \
  --ignore-not-found --wait=true

# A previous failed run may already have deleted the Probe while Prometheus
# still serves the old generated configuration. In that state, deleting the
# absent object emits no informer event and convergence waits for the
# operator's periodic resync (currently longer than this safety fence).
# Metadata-only updates do not alter the generated Prometheus config. Create a
# short-lived, non-alerting Probe under THIS ring fence instead of borrowing the
# fleet Probe (which would turn canary cleanup into a reverse-order cross-ring
# mutation). Its generated scrape config wakes config-reloader while the
# retired pool disappears; the EXIT trap removes it afterward.
reconcile_short="$(printf '%s' "${RELEASE_RING}-$(date +%s%N)-$$" | sha256sum | cut -c1-7)"
reconcile_name="probe-shizuha-hive-reconcile-${RELEASE_RING}-${reconcile_short}"
cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: monitoring.coreos.com/v1
kind: Probe
metadata:
  name: ${reconcile_name}
  namespace: ${PROMETHEUS_NAMESPACE}
  labels:
    release: kube-prometheus
    app.kubernetes.io/component: synthetic-probes
    service: hive-reconcile
    release_ring: reconcile-${RELEASE_RING}
spec:
  jobName: shizuha-hive-reconcile-${RELEASE_RING}-${reconcile_short}
  prober:
    url: blackbox-exporter.monitoring.svc.cluster.local:9115
    scheme: http
    path: /probe
  module: http_2xx_insecure
  interval: 30s
  scrapeTimeout: 15s
  targets:
    staticConfig:
      static: [http://hive.shizuha-hive.svc.cluster.local:8030/api/health/]
      labels: {service: hive-reconcile, release_ring: reconcile-${RELEASE_RING}}
YAML
reconcile_token="${RELEASE_RING}-$(date +%s%N)-$$"
reconcile_probe="probe.monitoring.coreos.com/${reconcile_name}"
cleanup_reconcile_label() {
  kubectl -n "$PROMETHEUS_NAMESPACE" delete "$reconcile_probe" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup_reconcile_label EXIT
reconcile_attempt=0
refresh_reconcile_label() {
  reconcile_attempt=$((reconcile_attempt + 1))
  kubectl -n "$PROMETHEUS_NAMESPACE" patch "$reconcile_probe" --type=merge \
    -p "{\"spec\":{\"targets\":{\"staticConfig\":{\"labels\":{\"shizuha_probe_retire_reconcile\":\"${reconcile_token}-${reconcile_attempt}\",\"shizuha_io_probe_retire_reconcile\":\"${reconcile_token}-${reconcile_attempt}\"}}}}}"
}
refresh_reconcile_label
next_reconcile_at=$((SECONDS + RECONCILE_INTERVAL_SECONDS))

# Bash's SECONDS variable advances at one-second resolution. A short fence can
# therefore lose almost its entire budget when it starts just before a tick,
# skipping the first replica-discovery pass. Use EPOCHREALTIME's microsecond
# resolution for the fail-closed deadline while retaining integer-second
# timeout values for kubectl and coreutils timeout.
now_microseconds() {
  local now seconds microseconds
  now="$EPOCHREALTIME"
  seconds="${now%%.*}"
  microseconds="${now#*.}"
  printf '%s\n' "$((10#$seconds * 1000000 + 10#${microseconds:0:6}))"
}
deadline_microseconds=$(($(now_microseconds) + TIMEOUT_SECONDS * 1000000))
last_reason="Prometheus replica discovery has not completed"
while (( $(now_microseconds) < deadline_microseconds )); do
  # prometheus-operated is headless. Querying its Service DNS once can hit
  # only one HA replica while the other still evaluates the retiring target.
  # Resolve every ready EndpointSlice target to its Pod, then query inside the
  # Prometheus container. The CI Role already has EndpointSlice read + Pod exec;
  # it intentionally lacks legacy Endpoints read and pods/proxy. Forgejo jobs
  # run in nested Docker, so direct Pod-CIDR routing is not a valid fallback.
  mapfile -t prometheus_pods < <(
    kubectl -n "$PROMETHEUS_NAMESPACE" get endpointslice \
      -l "kubernetes.io/service-name=${PROMETHEUS_SERVICE}" -o json 2>/dev/null |
      python3 -c '
import json
import sys

payload = json.load(sys.stdin)
pods = {
    endpoint.get("targetRef", {}).get("name")
    for item in payload.get("items", [])
    for endpoint in (item.get("endpoints") or [])
        # `or []` not get(k, []): EndpointSlice items can carry explicit
        # "endpoints": null mid-retirement (HIVE-707 fence wait state), and
        # dict.get only substitutes the default when the key is ABSENT —
        # iterating None crashed every poll, so retirement was never proven
        # and the fence timed out (PLAT-6881 follow-up; evidence: PLAT-6881
        # cmt 1009949, run 3450 log body via mio 2026-10-04).
    # EndpointSlice v1 defines nil ready as true.
    if endpoint.get("conditions", {}).get("ready") is not False
       and endpoint.get("targetRef", {}).get("kind") == "Pod"
       and endpoint.get("targetRef", {}).get("name")
}
print("\n".join(sorted(pods)))
'
  )

  expected_replicas="${PROMETHEUS_EXPECTED_REPLICAS:-}"
  if [[ -z "$expected_replicas" ]]; then
    expected_replicas="$(
      kubectl -n "$PROMETHEUS_NAMESPACE" get statefulset \
        -l app.kubernetes.io/name=prometheus \
        -o jsonpath='{range .items[*]}{.spec.replicas}{"\n"}{end}' \
        2>/dev/null | awk 'NF { total += $1; seen = 1 } END { if (seen) print total }'
    )"
  fi

  all_absent=true
  if [[ ! "$expected_replicas" =~ ^[1-9][0-9]*$ ]] ||
      (( ${#prometheus_pods[@]} != expected_replicas )); then
    all_absent=false
    last_reason="expected ${expected_replicas:-unknown} Prometheus replicas but discovered ${#prometheus_pods[@]} ready Pods"
  else
    for pod in "${prometheus_pods[@]}"; do
      remaining_microseconds=$((deadline_microseconds - $(now_microseconds)))
      (( remaining_microseconds > 0 )) || break 2
      # Round up so the existing integer-second process/request timeouts never
      # become zero while a sub-second portion of the fence budget remains.
      remaining_seconds=$(((remaining_microseconds + 999999) / 1000000))
      read_timeout_seconds="$PROMETHEUS_READ_TIMEOUT_SECONDS"
      if (( read_timeout_seconds > remaining_seconds )); then
        read_timeout_seconds="$remaining_seconds"
      fi
      if ! scrape_pools="$(timeout --signal=KILL "${read_timeout_seconds}s" \
          kubectl --request-timeout="${read_timeout_seconds}s" \
          -n "$PROMETHEUS_NAMESPACE" exec "$pod" \
          -c "$PROMETHEUS_CONTAINER" -- wget -qO- \
          "http://127.0.0.1:${PROMETHEUS_PORT}/api/v1/scrape_pools" 2>/dev/null)"; then
        all_absent=false
        last_reason="Pod exec could not read Prometheus Pod ${pod}"
        break
      fi
      set +e
      python3 -c '
import json
import sys

prefix = sys.argv[1]
try:
    payload = json.load(sys.stdin)
    if payload.get("status") != "success":
        raise ValueError("Prometheus response status is not success")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("scrapePools"), list):
        raise ValueError("Prometheus response lacks data.scrapePools array")
    scrape_pools = data["scrapePools"]
    if not all(isinstance(pool, str) for pool in scrape_pools):
        raise ValueError("Prometheus scrapePools contains a non-string")
except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
    sys.exit(2)

# A removed Probe cannot still expose an active target after its generated
# scrape pool has disappeared. Checking the small, stable scrape-pools API is
# stricter and avoids downloading the fleet-wide targets payload on every
# replica and poll.
sys.exit(1 if any(pool.startswith(prefix) for pool in scrape_pools) else 0)
' "$SCRAPE_POOL_PREFIX" <<<"$scrape_pools"
      inspect_rc=$?
      set -e
      case "$inspect_rc" in
        0) ;;
        1)
          all_absent=false
          last_reason="Prometheus Pod ${pod} still lists retired scrape pool ${SCRAPE_POOL_PREFIX}*"
          break
          ;;
        *)
          all_absent=false
          last_reason="Prometheus Pod ${pod} returned an invalid scrape-pools payload"
          break
          ;;
      esac
    done
  fi

  if [[ "$all_absent" == true ]]; then
    echo "Hive ${RELEASE_RING} Probe retired; scrape pool and target absent on all ${expected_replicas} Prometheus replicas"
    exit 0
  fi
  # The operator can process the surviving-Probe update before its informer
  # cache processes the selected-Probe deletion, reproducing the stale config.
  # Keep emitting bounded, config-changing events until every HA replica proves
  # absence; each token is unique, so config-reloader cannot treat it as a no-op.
  if (( SECONDS >= next_reconcile_at )); then
    refresh_reconcile_label
    next_reconcile_at=$((SECONDS + RECONCILE_INTERVAL_SECONDS))
  fi
  sleep "$POLL_SECONDS"
done

echo "::error::Hive ${RELEASE_RING} Probe retirement was not proven after ${TIMEOUT_SECONDS}s: ${last_reason}" >&2
exit 1
