#!/usr/bin/env bash
set -euo pipefail

# HIVE-1385: post-commit cleanup is operational health, not release health.
# Publish/resolve a dedicated Alertmanager episode without creating a rollback
# task or attaching the release-regression label to a proven-healthy build.
STATE="${1:?firing|resolved required}"
RING="${2:?ring required}"
RELEASE_SHA="${3:?release SHA required}"
DETAIL="${4:?cleanup detail required}"
[[ "$STATE" =~ ^(firing|resolved)$ ]] || { echo "::error::invalid cleanup event state"; exit 2; }
[[ "$RING" =~ ^(canary|fleet)$ ]] || { echo "::error::invalid cleanup event ring"; exit 2; }
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::release SHA must be full lowercase SHA"; exit 2; }

ALERTMANAGER_URL="${ALERTMANAGER_URL:-http://kube-prometheus-kube-prome-alertmanager.monitoring.svc.cluster.local:9093}"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
payload="$(python3 - "$STATE" "$RING" "$RELEASE_SHA" "$DETAIL" "$now" <<'PY'
import json
import sys

state, ring, release_sha, detail, now = sys.argv[1:]
alert = {
    "labels": {
        "alertname": "HiveReleaseCleanupFailed",
        "severity": "warning",
        "team": "devops",
        "owner_group": "devops",
        "service": "shizuha-hive",
        "release_ring": ring,
    },
    "annotations": {
        "summary": f"Hive {ring} post-commit cleanup failed",
        "release_sha": release_sha,
        "description": (
            f"release_sha={release_sha} release_health=passed cleanup={detail}; "
            "the healthy release was intentionally not rolled back (HIVE-1385)"
        ),
        "owner_group": "devops",
        "runbook_url": "https://wiki.shizuha.com/5506dd83-961c-48d2-bb34-9c3dcf93a249",
    },
    "startsAt": now,
}
if state == "resolved":
    alert["endsAt"] = now
print(json.dumps([alert]))
PY
)"

curl -fsS --max-time 20 -H 'Content-Type: application/json' -d "$payload" \
  "${ALERTMANAGER_URL}/api/v2/alerts" >/dev/null
echo "Hive ${RING} release cleanup alert ${STATE} for ${RELEASE_SHA:0:7}"
