#!/usr/bin/env bash
# Prevent a stale queued/rerun workflow from moving a live release backwards.
# Exit 0 = release allowed; 10 = stale ancestor, skip release; other = fail closed.
set -euo pipefail
NAMESPACE="${1:?usage: monotonic-release-guard.sh <namespace> <deployment> <run-sha>}"
DEPLOYMENT="${2:?usage: monotonic-release-guard.sh <namespace> <deployment> <run-sha>}"
RUN_SHA="${3:?usage: monotonic-release-guard.sh <namespace> <deployment> <run-sha>}"
if ! RELEASE_STATE="$(kubectl get deploy -n "${NAMESPACE}" "${DEPLOYMENT}" \
  -o jsonpath='{.metadata.annotations.shizuha\.io/last-successful-sha}{"|"}{.metadata.annotations.last-successful-sha}{"|"}{.spec.template.spec.containers[0].image}')"; then
  echo "::error::MONOTONIC GUARD — cannot read live Deployment ${NAMESPACE}/${DEPLOYMENT}; refusing to mutate live state"
  exit 2
fi
IFS='|' read -r RECORDED LEGACY_RECORDED LIVE_IMAGE <<< "${RELEASE_STATE}"
RECORDED="${RECORDED:-${LEGACY_RECORDED}}"
if [ -z "${RECORDED}" ]; then
  RECORDED="$(printf '%s\n' "${LIVE_IMAGE}" | sed -nE 's/.*[-:_/]([0-9a-fA-F]{7,40})([-.]|$)/\1/p' | head -1)"
fi
if [ -z "${RECORDED}" ]; then
  echo "::warning::MONOTONIC GUARD — ${NAMESPACE}/${DEPLOYMENT} has no readable release SHA; allowing one bootstrap release"
  exit 0
fi
if [ "${RECORDED}" = "${RUN_SHA}" ] || [ "${RECORDED}" = "${RUN_SHA:0:${#RECORDED}}" ]; then
  echo "MONOTONIC GUARD — exact released SHA rerun allowed (${RUN_SHA})"
  exit 0
fi
git cat-file -e "${RUN_SHA}^{commit}" 2>/dev/null || {
  echo "::error::MONOTONIC GUARD — triggering SHA ${RUN_SHA} is unavailable in the checked-out repository"
  exit 2
}
if ! git cat-file -e "${RECORDED}^{commit}" 2>/dev/null; then
  git fetch --no-tags --depth=200 origin "${RECORDED}" >/dev/null 2>&1 || true
fi
git cat-file -e "${RECORDED}^{commit}" 2>/dev/null || {
  echo "::error::MONOTONIC GUARD — recorded release SHA ${RECORDED} cannot be resolved; refusing to mutate live state"
  exit 2
}
if git merge-base --is-ancestor "${RUN_SHA}" "${RECORDED}"; then
  echo "::warning::MONOTONIC GUARD — ${RUN_SHA} is an ancestor of already-released ${RECORDED}; skipping stale release mutation"
  exit 10
fi
echo "MONOTONIC GUARD — ${RUN_SHA} is not an ancestor of ${RECORDED}; release allowed"
