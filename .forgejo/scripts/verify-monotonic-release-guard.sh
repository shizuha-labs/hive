#!/usr/bin/env bash
# Deterministic contract harness: mocks kubectl + git so no cluster/network is needed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUARD="${ROOT}/.forgejo/scripts/monotonic-release-guard.sh"
MOCK_BIN="$(mktemp -d)"
trap 'rm -rf "${MOCK_BIN}"' EXIT

cat >"${MOCK_BIN}/kubectl" <<'MOCK'
#!/usr/bin/env bash
if [ "${KUBECTL_MODE:-ok}" = error ]; then
  echo 'mock apiserver unavailable' >&2
  exit 42
fi
printf '%s' "${KUBECTL_OUTPUT:-}"
MOCK
cat >"${MOCK_BIN}/git" <<'MOCK'
#!/usr/bin/env bash
case "${1:-}" in
  cat-file) [ "${GIT_RESOLVE:-yes}" = yes ] ;;
  fetch) exit 0 ;;
  merge-base) [ "${GIT_RELATION:-forward}" = stale ] ;;
  *) echo "unexpected mocked git invocation: $*" >&2; exit 99 ;;
esac
MOCK
chmod +x "${MOCK_BIN}/kubectl" "${MOCK_BIN}/git"

RUN_SHA=1111111111111111111111111111111111111111
RECORDED=2222222222222222222222222222222222222222
if grep -q 'annotations.last-successful-sha' "${GUARD}"; then
  bootstrap_state='||'
  anchored_state="${RECORDED}||registry.local/app:release"
else
  bootstrap_state='|'
  anchored_state="${RECORDED}|registry.local/app:release"
fi

run_case() {
  local name="$1" expected="$2" mode="$3" state="$4" relation="$5" run_sha="$6" resolve="${7:-yes}" rc
  set +e
  PATH="${MOCK_BIN}:${PATH}" KUBECTL_MODE="$mode" KUBECTL_OUTPUT="$state" \
    GIT_RELATION="$relation" GIT_RESOLVE="$resolve" \
    bash "$GUARD" shizuha-test app "$run_sha" >"${MOCK_BIN}/${name}.log" 2>&1
  rc=$?
  set -e
  if [ "$rc" -ne "$expected" ]; then
    cat "${MOCK_BIN}/${name}.log" >&2
    echo "${name}: expected rc=${expected}, got rc=${rc}" >&2
    exit 1
  fi
  printf 'PASS %-14s rc=%s\n' "$name" "$rc"
}

run_case read-failure 2 error '' forward "$RUN_SHA"
run_case bootstrap 0 ok "$bootstrap_state" forward "$RUN_SHA"
run_case stale 10 ok "$anchored_state" stale "$RUN_SHA"
run_case equal 0 ok "$anchored_state" forward "$RECORDED"
run_case forward 0 ok "$anchored_state" forward "$RUN_SHA"
run_case unresolved 2 ok "$anchored_state" forward "$RUN_SHA" no

python3 - "${ROOT}/.forgejo/workflows/deploy.yml" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text()
jobs = {
    "deploy-staging": "kubectl create namespace staging",
    "deploy-canary": "render-ring-manifest.sh k8s/rollout/hive-canary",
    "deploy-fleet": "kubectl -n shizuha-hive patch deploy/hive",
}
starts = {name: text.index(f"  {name}:") for name in jobs}
ordered = sorted(starts.items(), key=lambda pair: pair[1])
for index, (name, start) in enumerate(ordered):
    end = ordered[index + 1][1] if index + 1 < len(ordered) else len(text)
    section = text[start:end]
    checkout = section.index("uses: actions/checkout@v4")
    guard = section.index("monotonic-release-guard.sh")
    mutation = section.index(jobs[name])
    assert checkout < guard < mutation, f"{name}: checkout/guard/mutation order invalid"
    print(f"PASS {name:14s} checkout < guard < persistent mutation")
PY
