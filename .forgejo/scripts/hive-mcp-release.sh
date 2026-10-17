#!/usr/bin/env bash

# Release-safety helpers for the Hive MCP deployment workflow. This file is
# sourced by deploy-hive-mcp.yml so the exact failure path can be exercised by
# the regression harness without duplicating the production-order logic.

hive_mcp_load_release_state() {
  CURRENT_IMAGE="$(kubectl get deploy -n shizuha shizuha-hive-mcp -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
  LAST_SUCCESSFUL_IMAGE="$(kubectl get deploy -n shizuha shizuha-hive-mcp -o jsonpath='{.metadata.annotations.shizuha\.io/last-successful-image}' 2>/dev/null || true)"
  LAST_SUCCESSFUL_SHA="$(kubectl get deploy -n shizuha shizuha-hive-mcp -o jsonpath='{.metadata.annotations.shizuha\.io/last-successful-sha}' 2>/dev/null || true)"

  # Older successful releases recorded only the SHA. Derive the immutable
  # multi-arch image tag once, then persist the exact image on this success.
  if [ -z "${LAST_SUCCESSFUL_IMAGE}" ] && [[ "${LAST_SUCCESSFUL_SHA}" =~ ^[0-9a-f]{7,40}$ ]]; then
    LAST_SUCCESSFUL_IMAGE="localhost:30500/${IMG}:mcp-${LAST_SUCCESSFUL_SHA:0:7}"
  fi
  echo "Current desired image: ${CURRENT_IMAGE:-<none>}"
  echo "Last successful image: ${LAST_SUCCESSFUL_IMAGE:-<none>}"
}

hive_mcp_require_release_anchor() {
  local image_pattern="^localhost:30500/${IMG}:mcp-[0-9a-f]{7,40}$"
  if [ -z "${LAST_SUCCESSFUL_IMAGE}" ] || ! [[ "${LAST_SUCCESSFUL_IMAGE}" =~ ${image_pattern} ]]; then
    echo "::error::No valid last-successful Hive MCP image is recorded; refusing to mutate the Deployment"
    return 1
  fi
}

hive_mcp_capture_release_diagnostics_inner() {
  local request_timeout="${HIVE_MCP_KUBECTL_REQUEST_TIMEOUT:-8s}"
  kubectl --request-timeout="${request_timeout}" get deploy -n shizuha shizuha-hive-mcp -o wide || true
  kubectl --request-timeout="${request_timeout}" describe deploy -n shizuha shizuha-hive-mcp | tail -n 160 || true
  kubectl --request-timeout="${request_timeout}" get rs -n shizuha -l app=shizuha-hive-mcp -o wide || true
  kubectl --request-timeout="${request_timeout}" get pods -n shizuha -l app=shizuha-hive-mcp -o wide || true
  for pod in $(kubectl --request-timeout="${request_timeout}" get pods -n shizuha -l app=shizuha-hive-mcp -o name 2>/dev/null || true); do
    echo "--- diagnostics: ${pod} ---"
    kubectl --request-timeout="${request_timeout}" describe -n shizuha "${pod}" | tail -n 120 || true
    kubectl --request-timeout="${request_timeout}" logs -n shizuha "${pod}" --all-containers --tail=80 || true
    kubectl --request-timeout="${request_timeout}" logs -n shizuha "${pod}" --all-containers --previous --tail=80 || true
  done
  kubectl --request-timeout="${request_timeout}" get events -n shizuha --sort-by=.lastTimestamp | tail -n 80 || true
}

hive_mcp_capture_release_diagnostics() {
  local budget="${HIVE_MCP_DIAGNOSTIC_TIMEOUT_SECONDS:-45}"
  export -f hive_mcp_capture_release_diagnostics_inner
  if ! timeout --signal=TERM --kill-after=2s "${budget}s" bash -c hive_mcp_capture_release_diagnostics_inner; then
    echo "::warning::Hive MCP diagnostics exceeded ${budget}s or returned an error; continuing immediately to rollback"
  fi
}

hive_mcp_rollback_candidate() {
  echo "Rolling back failed candidate to ${LAST_SUCCESSFUL_IMAGE}"
  # PLAT-7708: the rollback must go through cd_rollback so the CD desired-state
  # annotations are restored and the failed sha lands on the rollback ledger in
  # the same operation — otherwise a later re-run re-asserts the bad build.
  source "$(dirname "${BASH_SOURCE[0]}")/cd-rollback-ledger.sh"
  local failed_sha
  failed_sha="$(printf '%s\n' "${TARGET_IMAGE}" | sed -nE 's/.*[-:_/]([0-9a-fA-F]{7,40})([-.]|$)/\1/p' | head -1)"
  if ! cd_rollback shizuha shizuha-hive-mcp hive-mcp "${LAST_SUCCESSFUL_IMAGE}" "${failed_sha}" \
    "deploy-hive-mcp candidate failed rollout verification"; then
    echo "::error::Hive MCP rollback failed for ${LAST_SUCCESSFUL_IMAGE}"
    kubectl --request-timeout="${HIVE_MCP_KUBECTL_REQUEST_TIMEOUT:-8s}" get pods -n shizuha -l app=shizuha-hive-mcp -o wide || true
    return 1
  fi
  RESTORED_IMAGE="$(kubectl get deploy -n shizuha shizuha-hive-mcp -o jsonpath='{.spec.template.spec.containers[0].image}')"
  if [ "${RESTORED_IMAGE}" != "${LAST_SUCCESSFUL_IMAGE}" ]; then
    echo "::error::Hive MCP rollback image mismatch: expected ${LAST_SUCCESSFUL_IMAGE}, got ${RESTORED_IMAGE}"
    return 1
  fi
  echo "Rollback to ${RESTORED_IMAGE} converged"
}

hive_mcp_fail_candidate() {
  local reason="$1"
  echo "::error::Hive MCP candidate ${TARGET_IMAGE} failed: ${reason}; capturing bounded diagnostics"
  hive_mcp_capture_release_diagnostics
  if hive_mcp_rollback_candidate; then
    echo "::error::Candidate ${TARGET_IMAGE} failed; rollback to ${RESTORED_IMAGE} converged"
  else
    echo "::error::Candidate ${TARGET_IMAGE} failed and rollback did not converge"
  fi
  exit 1
}

hive_mcp_promote_candidate() {
  # INV-B: prove the rollback path exists before candidate mutation.
  hive_mcp_require_release_anchor
  # PLAT-7708: refuse a sha on the rollback ledger — fail closed, never retry.
  source "$(dirname "${BASH_SOURCE[0]}")/cd-rollback-ledger.sh"
  local candidate_sha
  candidate_sha="$(printf '%s\n' "${TARGET_IMAGE}" | sed -nE 's/.*[-:_/]([0-9a-fA-F]{7,40})([-.]|$)/\1/p' | head -1)"
  if ! cd_ledger_check shizuha shizuha-hive-mcp "${candidate_sha}"; then
    echo "::error::Hive MCP candidate ${TARGET_IMAGE} is on the rollback ledger; refusing to deploy"
    exit 1
  fi
  local contract="${HIVE_MCP_MANIFEST_JSON:?canonical deploy manifest JSON is required}"
  local temporary
  temporary="$(mktemp -d)"
  if ! (
    set -euo pipefail
    umask 077
    kubectl get deploy -n shizuha shizuha-hive-mcp -o json > "${temporary}/current.json" &&
    python3 "$(dirname "${BASH_SOURCE[0]}")/render-hive-mcp-deployment.py" \
      "${contract}" "${temporary}/current.json" "${TARGET_IMAGE}" > "${temporary}/patch.json" &&
    kubectl patch -n shizuha deploy/shizuha-hive-mcp --type=json --patch-file "${temporary}/patch.json"
  ); then
    rm -rf "${temporary}"
    return 1
  fi
  rm -rf "${temporary}"
  if ! kubectl rollout status -n shizuha deploy/shizuha-hive-mcp --timeout=300s; then
    hive_mcp_fail_candidate "rollout did not converge within 300s"
  fi
}
