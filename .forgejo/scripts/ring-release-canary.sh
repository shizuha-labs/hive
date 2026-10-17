#!/usr/bin/env bash
set -Eeuo pipefail
SHA="$RELEASE_SHA"
SHORT="${SHA:0:7}"
IMAGE="localhost:30500/shizuha-hive:hive-${SHORT}"
LAST_SHA="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o jsonpath='{.metadata.annotations.last-successful-sha}')"
[[ "$LAST_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::missing rollback anchor (INV-B)"; exit 1; }
ROLLBACK_TAG="hive-${LAST_SHA:0:7}"
# PLAT-7744: the single-shot INV-B probe 404'd twice (runs 2573/2575) while the
# manifest verifiably existed at the authoritative registry — the registry
# returns spurious failures under load (same transient class as the Origin
# API's empty bodies). Retry with backoff; a genuinely absent anchor image
# still fails loud after 6 attempts (~40s), now naming the probed tag,
# endpoint, and annotation value for diagnosis.
INV_B_OK=0
for _attempt in 1 2 3 4 5 6; do
  if curl -fsS --max-time 20 -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
    "http://registry.registry.svc.cluster.local:5000/v2/shizuha-hive/manifests/${ROLLBACK_TAG}" >/dev/null; then
    INV_B_OK=1
    break
  fi
  sleep $((_attempt * 2))
done
if [ "$INV_B_OK" != "1" ]; then
  echo "::error::rollback image absent (INV-B): probed http://registry.registry.svc.cluster.local:5000/v2/shizuha-hive/manifests/${ROLLBACK_TAG} 6x with backoff (last-successful-sha=${LAST_SHA})"
  exit 1
fi

# HIVE-1153: CI owns create-once signer provisioning.  Never generate
# or patch this key by hand, and never rotate an existing valid key.

r1_failure() {
  local original_rc="$1" failed_command="$2" pipeline_status="${3:-}" rollback_rc=0 evidence capture_output capture_rc
  # HIVE-1810: $BASH_COMMAND is the LAST pipeline element (e.g. `tail -n1`),
  # which destroys the causal command when an upstream stage fails. Derive the
  # actual failing stage + exit statuses from PIPESTATUS so the rollback
  # evidence names the real culprit, not the last stage.
  local stage_info="" i=0 rc
  for rc in $pipeline_status; do
    i=$((i + 1))
    if [ "$rc" != "0" ]; then
      stage_info="stage=${i} rc=${rc} pipeline_status=${pipeline_status}"
      break
    fi
  done
  [ -n "$stage_info" ] || stage_info="pipeline_status=${pipeline_status}"
  # A second TERM while bounded evidence capture is running must not
  # interrupt rollback. The original signal status is preserved.
  trap - ERR
  trap '' TERM INT
  set +e
  [[ -f /tmp/canary-gate.log ]] && cat /tmp/canary-gate.log
  capture_output="$(RING_EVIDENCE_NAMESPACE=origin timeout 30s \
    .forgejo/scripts/capture-ring-evidence.sh canary shizuha-hive hive-canary hive-canary \
      "$SHA" "command=${failed_command} ${stage_info}" /tmp/canary-gate.log 2>&1)"
  capture_rc=$?
  if [ "$capture_rc" -eq 0 ]; then
    evidence="$capture_output"
  else
    echo "::warning::bounded canary evidence capture failed rc=${capture_rc}: ${capture_output}"
    evidence="artifact=unavailable capture_rc=${capture_rc}"
  fi
  # Destructive cleanup happens only after the bounded snapshot.
  if .forgejo/scripts/retire-canary-probe.sh; then
    .forgejo/scripts/kubectl -n shizuha-hive delete deploy hive-canary --ignore-not-found --wait=true || rollback_rc=1
    .forgejo/scripts/kubectl -n shizuha-hive delete svc hive-canary --ignore-not-found || rollback_rc=1
  else
    # Fail closed: an observable canary target must retain its target
    # Service/Deployment rather than manufacture probe_success=0.
    rollback_rc=1
  fi
  # HIVE-2819 (D2): evidence/alert delivery is paperwork, not rollback
  # atomicity — a persistent delivery failure is downgraded to ::warning::
  # so it can never corrupt rollback_rc or mask the rollback result.
  if ! .forgejo/scripts/record-rollout-failure.sh canary "$SHA" "$LAST_SHA" \
    "rollback_rc=${rollback_rc} command=${failed_command} ${stage_info} ${evidence} $(tail -c 1500 /tmp/canary-gate.log 2>/dev/null | tr '\n' ' ')"; then
    echo "::warning::rollback evidence delivery failed (ring=canary bad_sha=${SHA} restored=${LAST_SHA}) — rollback atomicity result unchanged (rollback_rc=${rollback_rc}); file/close the Pulse rollback artifact manually"
  fi
  [ "$rollback_rc" -eq 0 ] || echo "::error::canary rollback/evidence contract had failures"
  exit "$original_rc"
}

.forgejo/scripts/render-ring-manifest.sh k8s/rollout/hive-canary.yaml.tpl "$IMAGE" "$SHA" /tmp/hive-canary.yaml
.forgejo/scripts/retire-canary-probe.sh
.forgejo/scripts/kubectl -n shizuha-hive delete deploy hive-canary --ignore-not-found --wait=true
# The cluster-resident watchdog survives uncatchable runner loss. The
# signal traps cover ordinary cancel/eviction TERM/INT immediately.
trap 'r1_failure "$?" "$BASH_COMMAND" "${PIPESTATUS[*]}"' ERR
trap 'r1_failure 143 "signal=TERM"' TERM
trap 'r1_failure 130 "signal=INT"' INT
.forgejo/scripts/kubectl apply -f /tmp/hive-canary.yaml
.forgejo/scripts/kubectl -n shizuha-hive rollout status deploy/hive-canary --timeout=300s
.forgejo/scripts/kubectl -n shizuha-hive get endpoints hive-canary -o jsonpath='{.subsets[0].addresses[0].ip}' | grep -q .
bash .forgejo/scripts/ring-gate.sh canary shizuha-hive hive "$SHA" > /tmp/canary-gate.log 2>&1
cat /tmp/canary-gate.log
trap - ERR TERM INT
printf 'R1 canary PASS; hold=%s\n' "$CANARY_HOLD"
