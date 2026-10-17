#!/usr/bin/env bash
# CD rollback ledger — the deny list of rolled-back release SHAs (PLAT-7708).
#
# Gap this closes (PLAT-7681 hive-mcp 59e9439 class): a rollback that only
# mutates live state leaves the CD annotations pointing at the bad build, so
# the next reconcile/re-run re-asserts a sha whose tag was pruned (ErrImagePull)
# — and the monotonic guard's "exact released SHA rerun allowed" path re-arms it.
#
# Contract:
#   - Every rollback event appends the failed sha to the ledger AND updates the
#     CD desired-state annotations in the SAME operation (cd_rollback).
#   - Every deploy path checks the ledger BEFORE building/mutating: a ledger sha
#     fails loudly (exit 1) and never retries (cd_ledger_check).
#   - The ledger is a ConfigMap `cd-rollback-ledger` in the workload namespace;
#     keys are `<deployment>/<short-sha>`, values are JSON provenance.
#
# Sourced by deploy-hive-mcp.yml (via hive-mcp-release.sh consumers) and safe to
# reuse by other CD workflows targeting the same contract.

# Resolve the ledger ConfigMap name for a namespace.
cd_ledger_ref() {
  echo "cm/cd-rollback-ledger"
}

# Read the ledger entry for a deployment+sha. Prints the JSON value if present.
cd_ledger_get() {
  local ns="$1" deployment="$2" sha="$3"
  local short="${sha:0:7}"
  kubectl get -n "$ns" "$(cd_ledger_ref)" \
    -o "jsonpath={.data.${deployment}__${short}}" 2>/dev/null || true
}

# cd_ledger_check <ns> <deployment> <sha>
# Exit 0  -> sha is NOT on the ledger (deploy may proceed)
# Exit 1  -> sha IS on the ledger (deploy forbidden — caller must fail loudly)
# Exit 2  -> ledger unreadable for a reason other than "absent" (fail closed)
cd_ledger_check() {
  local ns="$1" deployment="$2" sha="$3"
  local short="${sha:0:7}"
  local key="${deployment}__${short}"
  local out
  if ! out="$(kubectl get -n "$ns" "$(cd_ledger_ref)" -o json 2>/dev/null)"; then
    # NotFound -> empty ledger (bootstrap); any other error -> fail closed.
    if kubectl get -n "$ns" "$(cd_ledger_ref)" 2>&1 | grep -q "NotFound"; then
      return 0
    fi
    echo "::error::ROLLBACK LEDGER — cannot read $(cd_ledger_ref) in ns ${ns}; refusing to deploy (fail closed)"
    return 2
  fi
  if printf '%s' "$out" | python3 -c "
import json, sys
data = json.load(sys.stdin).get('data', {}) or {}
sys.exit(1 if '$key' in data else 0)
  "; then
    return 0
  fi
  echo "::error::ROLLBACK LEDGER — sha ${short} of ${deployment} was ROLLED BACK and is forbidden ($(cd_ledger_get "$ns" "$deployment" "$sha")). Roll forward with a new commit; never re-deploy or re-publish this build."
  return 1
}

# cd_ledger_append <ns> <deployment> <sha> <reason>
# Records a rollback event. Creates the ConfigMap if absent. Idempotent.
cd_ledger_append() {
  local ns="$1" deployment="$2" sha="$3" reason="$4"
  local short="${sha:0:7}"
  local key="${deployment}__${short}"
  local payload
  payload="$(python3 -c "
import json, datetime
print(json.dumps({
    'sha': '$sha',
    'rolled_back_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'reason': '''$reason''',
}))
")"
  if ! kubectl get -n "$ns" "$(cd_ledger_ref)" >/dev/null 2>&1; then
    kubectl create -n "$ns" configmap cd-rollback-ledger \
      --from-literal="${key}=${payload}" >/dev/null || return 2
    echo "ROLLBACK LEDGER — created $(cd_ledger_ref) in ns ${ns}; recorded ${deployment}/${short}"
    return 0
  fi
  # PLAT-8421: ConfigMap.data is map[string]string — the patch value must be
  # the payload JSON *encoded as a string*, not the raw object (the API server
  # rejects `Invalid value: ... must be a string`, which aborted every second+
  # rollback BEFORE `kubectl set image` and left the failed sha off the
  # ledger). Build the whole patch in python so both the key and the
  # string-encoded payload are injected safely.
  kubectl patch -n "$ns" "$(cd_ledger_ref)" --type merge -p "$(
    printf '%s' "$payload" | python3 -c 'import json, sys; print(json.dumps({"data": {sys.argv[1]: sys.stdin.read()}}))' "$key"
  )"
}

# cd_rollback <ns> <deployment> <container> <restore_image> <failed_sha> <reason>
# THE rollback operation: restores live state AND CD desired state AND records
# the deny-list entry in one sequence. Never call `kubectl set image` for a
# rollback without going through here (PLAT-7708 acceptance #1).
cd_rollback() {
  local ns="$1" deployment="$2" container="$3" restore_image="$4" failed_sha="$5" reason="$6"
  echo "ROLLBACK — ${deployment}: restoring ${restore_image} (failed sha ${failed_sha:0:7})"
  # Ledger FIRST: the moment a rollback is decided, the sha is forbidden — an
  # interruption after this point can never leave the sha re-armable (the
  # pre-build gate refuses any re-run even if the restore itself fails).
  cd_ledger_append "$ns" "$deployment" "$failed_sha" "$reason" || return 2
  kubectl set image -n "$ns" "deploy/${deployment}" "${container}=${restore_image}" || return 2
  if ! kubectl rollout status -n "$ns" "deploy/${deployment}" --timeout=300s; then
    echo "::error::ROLLBACK — ${deployment} did not converge on ${restore_image}"
    return 2
  fi
  # Desired state: the annotations must name the RESTORED build as last
  # successful, or the monotonic guard + release state re-arm the bad sha.
  kubectl annotate -n "$ns" "deploy/${deployment}" \
    "shizuha.io/last-successful-image=${restore_image}" \
    --overwrite >/dev/null || return 2
  local restore_sha
  restore_sha="$(printf '%s\n' "$restore_image" | sed -nE 's/.*[-:_/]([0-9a-fA-F]{7,40})([-.]|$)/\1/p' | head -1)"
  if [ -n "$restore_sha" ]; then
    kubectl annotate -n "$ns" "deploy/${deployment}" \
      "shizuha.io/last-successful-sha=${restore_sha}" \
      --overwrite >/dev/null || return 2
  fi
  echo "ROLLBACK — ${deployment} converged on ${restore_image}; failed sha ${failed_sha:0:7} is on the ledger"
}
