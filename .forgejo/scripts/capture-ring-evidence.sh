#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Capture bounded release-ring evidence before rollback destroys the failing
# topology.  The artifact is a ConfigMap owned by a small retention Job, so it
# survives the CI runner but is garbage-collected after the investigation
# window.  Callers must still wrap this helper in `timeout`: evidence failure
# must never delay fail-closed rollback.

RING="${1:?ring required}"
TARGET_NAMESPACE="${2:?target namespace required}"
DEPLOYMENT="${3:?deployment required}"
SERVICE="${4:?service required}"
BUILD_SHA="${5:?build SHA required}"
CAUSE="${6:?failure cause required}"
GATE_LOG="${7:-}"
EVIDENCE_NAMESPACE="${RING_EVIDENCE_NAMESPACE:-origin}"
HOLD_SECONDS="${RING_EVIDENCE_HOLD_SECONDS:-3600}"
[[ "$RING" =~ ^(staging|canary|fleet)$ ]] || { echo "invalid evidence ring" >&2; exit 2; }
[[ "$BUILD_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid evidence build SHA" >&2; exit 2; }
[[ "$HOLD_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo "invalid evidence retention" >&2; exit 2; }
: "${RING_FENCE_TOKEN:?ring fence token required}"
[[ "$RING_FENCE_TOKEN" =~ ^[1-9][0-9]*$ ]] || { echo "invalid ring fence token" >&2; exit 2; }

RING_GENERATION="$(PYTHONPATH="$SCRIPT_DIR" python3 - <<'PY'
from ring_fence import Holder
print(Holder.from_env().generation)
PY
)"
[[ "$RING_GENERATION" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid ring generation" >&2; exit 2; }

SHORT="${BUILD_SHA:0:7}"
NAME="hive-ring-evidence-${RING}-${SHORT}-${RING_GENERATION:0:12}"
NAME="${NAME:0:63}"
WORK="$(mktemp -d)"
RAW="$WORK/evidence.txt"
BOUNDED="$WORK/evidence-bounded.txt"
PODS_JSON="$WORK/exact-build-pods.json"
POD_TARGETS="$WORK/exact-build-targets.tsv"
PERSISTED=0
JOB_CREATED=0
CONFIGMAP_CREATED=0
cleanup() {
  local rc="${1:-$?}"
  trap - EXIT TERM INT
  rm -rf "$WORK"
  if (( PERSISTED == 0 )); then
    if (( CONFIGMAP_CREATED == 1 )); then
      kubectl -n "$EVIDENCE_NAMESPACE" delete configmap "$NAME" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    fi
    if (( JOB_CREATED == 1 )); then
      kubectl -n "$EVIDENCE_NAMESPACE" delete job "$NAME" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    fi
  fi
  exit "$rc"
}
trap 'cleanup $?' EXIT
trap 'cleanup 143' TERM
trap 'cleanup 130' INT

capture() {
  local title="$1"; shift
  {
    printf '\n### %s\n' "$title"
    set +e
    timeout 6s "$@" 2>&1
    rc=$?
    set -e
    (( rc == 0 )) || printf '[capture rc=%s]\n' "$rc"
  } >>"$RAW"
}

cat >"$RAW" <<EOF
artifact_version=2
task=HIVE-736
captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ring=${RING}
build_sha=${BUILD_SHA}
target_namespace=${TARGET_NAMESPACE}
deployment=${DEPLOYMENT}
service=${SERVICE}
cause=${CAUSE}
EOF

capture "deployment" kubectl -n "$TARGET_NAMESPACE" get deployment "$DEPLOYMENT" -o yaml
capture "service" kubectl -n "$TARGET_NAMESPACE" get service "$SERVICE" -o yaml
capture "exact-build pods" kubectl -n "$TARGET_NAMESPACE" get pods -l "build_sha=${BUILD_SHA}" -o wide --show-labels
capture "exact-build pod objects (UID, node, image, readiness, PodIP)" kubectl -n "$TARGET_NAMESPACE" get pods -l "build_sha=${BUILD_SHA}" -o json
capture "all service candidate pods" kubectl -n "$TARGET_NAMESPACE" get pods -o wide --show-labels
capture "EndpointSlices" kubectl -n "$TARGET_NAMESPACE" get endpointslices.discovery.k8s.io -l "kubernetes.io/service-name=${SERVICE}" -o yaml
capture "legacy Endpoints" kubectl -n "$TARGET_NAMESPACE" get endpoints "$SERVICE" -o yaml
capture "recent namespace events" kubectl -n "$TARGET_NAMESPACE" get events --sort-by=.lastTimestamp
capture "runner DNS" getent ahostsv4 "${SERVICE}.${TARGET_NAMESPACE}.svc.cluster.local"
capture "runner routes" ip route
capture "runner link and MTU" ip -details link show

# Preserve a bounded, exact-build target set before teardown.  Validation and
# truncation happen before the values are used in direct-pod URLs or log calls.
set +e
timeout 6s kubectl -n "$TARGET_NAMESPACE" get pods -l "build_sha=${BUILD_SHA}" -o json >"$PODS_JSON" 2>>"$RAW"
pods_rc=$?
if (( pods_rc == 0 )); then
  timeout 6s python3 - "$PODS_JSON" >"$POD_TARGETS" <<'PY'
import ipaddress
import json
import pathlib
import sys

items = json.loads(pathlib.Path(sys.argv[1]).read_text()).get("items", [])
targets = []
for pod in items:
    meta = pod.get("metadata", {})
    spec = pod.get("spec", {})
    status = pod.get("status", {})
    name = meta.get("name", "")
    ip = status.get("podIP", "")
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        continue
    if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-." for char in name):
        continue
    ready = next((c.get("status", "Unknown") for c in status.get("conditions", []) if c.get("type") == "Ready"), "Unknown")
    images = ",".join(c.get("image", "") for c in spec.get("containers", []))
    targets.append((name, meta.get("uid", ""), ip, spec.get("nodeName", ""), status.get("phase", ""), ready, images))
for row in sorted(targets)[:5]:
    print("\t".join(str(value).replace("\t", " ").replace("\n", " ") for value in row))
PY
  targets_rc=$?
else
  targets_rc="$pods_rc"
fi
set -e
if (( targets_rc != 0 )); then
  printf '\n### exact-build direct targets\n[target derivation rc=%s]\n' "$targets_rc" >>"$RAW"
  : >"$POD_TARGETS"
else
  {
    printf '\n### exact-build direct targets\n'
    printf 'pod_name\tpod_uid\tpod_ip\tnode\tphase\tready\timages\n'
    cat "$POD_TARGETS"
  } >>"$RAW"
fi

capture "timestamped verbose runner Service probe" /bin/sh -c '
  printf "started_at=%s target=%s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"
  curl -v -sS --connect-timeout 2 --max-time 5 -o /dev/null -w "http_code=%{http_code} time_total=%{time_total}\n" "$1"
  rc=$?
  printf "finished_at=%s rc=%s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc"
  exit "$rc"
' probe "http://${SERVICE}.${TARGET_NAMESPACE}.svc.cluster.local:8030/api/health/"

while IFS=$'\t' read -r pod uid pod_ip node phase ready images; do
  [[ -n "$pod" && -n "$pod_ip" ]] || continue
  authority="$pod_ip"; [[ "$pod_ip" == *:* ]] && authority="[${pod_ip}]"
  capture "timestamped verbose runner direct-pod probe pod=${pod} uid=${uid} node=${node} ip=${pod_ip}" /bin/sh -c '
    printf "started_at=%s target=%s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"
    curl -v -sS --connect-timeout 2 --max-time 5 -o /dev/null -w "http_code=%{http_code} time_total=%{time_total}\n" "$1"
    rc=$?
    printf "finished_at=%s rc=%s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc"
    exit "$rc"
  ' probe "http://${authority}:8030/api/health/"
  capture "logs ${pod}" kubectl -n "$TARGET_NAMESPACE" logs "$pod" --all-containers --tail=80
  capture "previous logs ${pod}" kubectl -n "$TARGET_NAMESPACE" logs "$pod" --all-containers --previous --tail=40
done <"$POD_TARGETS"

if [[ -n "$GATE_LOG" && -r "$GATE_LOG" ]]; then
  {
    printf '\n### gate log (tail)\n'
    tail -n 500 "$GATE_LOG"
  } >>"$RAW"
fi

# Stay below the ConfigMap object limit even if an event or log source is noisy.
head -c 700000 "$RAW" >"$BOUNDED"
EVIDENCE_BYTES="$(wc -c <"$BOUNDED" | tr -d ' ')"
EVIDENCE_SHA256="$(sha256sum "$BOUNDED" | awk '{print $1}')"

set +e
cat <<YAML | kubectl create -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${NAME}
  namespace: ${EVIDENCE_NAMESPACE}
  labels:
    app: hive-ring-evidence
    service: hive
    release_ring: ${RING}
    build_sha: ${BUILD_SHA}
    shizuha.io/task: HIVE-736
  annotations:
    shizuha.io/ring-generation: ${RING_GENERATION}
    shizuha.io/fence-token: "${RING_FENCE_TOKEN}"
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 900
  activeDeadlineSeconds: $((HOLD_SECONDS + 60))
  template:
    metadata:
      labels: {app: hive-ring-evidence, release_ring: ${RING}, build_sha: ${BUILD_SHA}}
    spec:
      restartPolicy: Never
      containers:
        - name: retention
          image: alpine/k8s:1.30.0
          command: ["/bin/sh", "-c", "sleep ${HOLD_SECONDS}"]
          resources:
            requests: {cpu: 1m, memory: 4Mi}
            limits: {cpu: 20m, memory: 16Mi}
YAML
JOB_CREATE_RC="${PIPESTATUS[1]}"
set -e
if (( JOB_CREATE_RC != 0 )); then
  printf 'evidence Job create was not confirmed; validating exact-generation collision\n' >&2
else
  JOB_CREATED=1
fi

JOB_JSON="$WORK/evidence-job.json"
kubectl -n "$EVIDENCE_NAMESPACE" get job "$NAME" -o json >"$JOB_JSON"
JOB_UID="$(python3 - "$JOB_JSON" "$NAME" "$EVIDENCE_NAMESPACE" "$RING" "$BUILD_SHA" \
  "$HOLD_SECONDS" "$RING_GENERATION" "$RING_FENCE_TOKEN" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text())
name, namespace, ring, build_sha = sys.argv[2:6]
hold_seconds, generation, token = sys.argv[6:9]
metadata = value.get("metadata", {})
labels = metadata.get("labels", {})
annotations = metadata.get("annotations", {})
expected_labels = {
    "app": "hive-ring-evidence",
    "service": "hive",
    "release_ring": ring,
    "build_sha": build_sha,
    "shizuha.io/task": "HIVE-736",
}
expected_annotations = {
    "shizuha.io/ring-generation": generation,
    "shizuha.io/fence-token": token,
}
if metadata.get("name") != name or metadata.get("namespace") != namespace:
    raise SystemExit("evidence Job identity read-back mismatch")
if any(labels.get(key) != item for key, item in expected_labels.items()):
    raise SystemExit("evidence Job labels read-back mismatch")
if any(annotations.get(key) != item for key, item in expected_annotations.items()):
    raise SystemExit("evidence Job generation read-back mismatch")
spec = value.get("spec", {})
pod = spec.get("template", {})
pod_labels = pod.get("metadata", {}).get("labels", {})
containers = pod.get("spec", {}).get("containers", [])
expected_container = {
    "name": "retention",
    "image": "alpine/k8s:1.30.0",
    "command": ["/bin/sh", "-c", f"sleep {hold_seconds}"],
}
if spec.get("backoffLimit") != 0 or spec.get("ttlSecondsAfterFinished") != 900:
    raise SystemExit("evidence Job retention policy read-back mismatch")
if spec.get("activeDeadlineSeconds") != int(hold_seconds) + 60:
    raise SystemExit("evidence Job deadline read-back mismatch")
if any(pod_labels.get(key) != item for key, item in {
    "app": "hive-ring-evidence", "release_ring": ring, "build_sha": build_sha,
}.items()):
    raise SystemExit("evidence Job pod identity read-back mismatch")
if len(containers) != 1 or any(
    containers[0].get(key) != item for key, item in expected_container.items()
):
    raise SystemExit("evidence Job container read-back mismatch")
uid = metadata.get("uid")
if not uid:
    raise SystemExit("evidence retention Job UID unavailable")
print(uid)
PY
)"
CONFIGMAP_JSON="$WORK/evidence-configmap.json"
kubectl -n "$EVIDENCE_NAMESPACE" create configmap "$NAME" \
  --from-file=evidence.txt="$BOUNDED" --dry-run=client -o json >"$CONFIGMAP_JSON"
python3 - "$CONFIGMAP_JSON" "$EVIDENCE_NAMESPACE" "$RING" "$BUILD_SHA" "$JOB_UID" \
  "$EVIDENCE_SHA256" "$EVIDENCE_BYTES" "$RING_GENERATION" "$RING_FENCE_TOKEN" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
namespace, ring, build_sha, job_uid = sys.argv[2:6]
digest, size, generation, token = sys.argv[6:10]
value = json.loads(path.read_text())
metadata = value.setdefault("metadata", {})
metadata["namespace"] = namespace
metadata["labels"] = {
    "app": "hive-ring-evidence",
    "service": "hive",
    "release_ring": ring,
    "build_sha": build_sha,
    "shizuha.io/task": "HIVE-736",
}
metadata["annotations"] = {
    "shizuha.io/ring-generation": generation,
    "shizuha.io/fence-token": token,
    "shizuha.io/evidence-sha256": digest,
    "shizuha.io/evidence-bytes": size,
}
metadata["ownerReferences"] = [{
    "apiVersion": "batch/v1",
    "kind": "Job",
    "name": metadata["name"],
    "uid": job_uid,
    "controller": True,
    "blockOwnerDeletion": True,
}]
path.write_text(json.dumps(value, separators=(",", ":")))
PY
set +e
kubectl create -f "$CONFIGMAP_JSON" >/dev/null
CONFIGMAP_CREATE_RC=$?
set -e
if (( CONFIGMAP_CREATE_RC != 0 )); then
  printf 'evidence ConfigMap create was not confirmed; validating exact-generation collision\n' >&2
else
  CONFIGMAP_CREATED=1
fi
kubectl -n "$EVIDENCE_NAMESPACE" get configmap "$NAME" -o json >"$WORK/persisted-configmap.json"
python3 - "$WORK/persisted-configmap.json" "$NAME" "$EVIDENCE_NAMESPACE" "$RING" \
  "$BUILD_SHA" "$JOB_UID" "$EVIDENCE_SHA256" "$EVIDENCE_BYTES" \
  "$RING_GENERATION" "$RING_FENCE_TOKEN" <<'PY'
import hashlib
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text())
name, namespace, ring, build_sha = sys.argv[2:6]
job_uid, digest, size, generation, token = sys.argv[6:11]
metadata = value["metadata"]
annotations = metadata.get("annotations", {})
owners = metadata.get("ownerReferences", [])
data = value.get("data", {}).get("evidence.txt", "").encode()
if not metadata.get("uid") or metadata.get("name") != name or metadata.get("namespace") != namespace:
    raise SystemExit("evidence ConfigMap UID unavailable")
if len(owners) != 1 or owners[0].get("uid") != job_uid:
    raise SystemExit("evidence ConfigMap owner read-back mismatch")
expected_labels = {
    "app": "hive-ring-evidence",
    "service": "hive",
    "release_ring": ring,
    "build_sha": build_sha,
    "shizuha.io/task": "HIVE-736",
}
if any(metadata.get("labels", {}).get(key) != item for key, item in expected_labels.items()):
    raise SystemExit("evidence ConfigMap labels read-back mismatch")
expected = {
    "shizuha.io/evidence-sha256": digest,
    "shizuha.io/evidence-bytes": size,
    "shizuha.io/ring-generation": generation,
    "shizuha.io/fence-token": token,
}
if any(annotations.get(key) != item for key, item in expected.items()):
    raise SystemExit("evidence ConfigMap identity read-back mismatch")
if len(data) != int(size) or hashlib.sha256(data).hexdigest() != digest:
    raise SystemExit("evidence ConfigMap payload read-back mismatch")
if "kubectl.kubernetes.io/last-applied-configuration" in annotations:
    raise SystemExit("evidence ConfigMap contains forbidden last-applied annotation")
PY

PERSISTED=1
printf 'artifact=configmap/%s namespace=%s retention=%ss\n' "$NAME" "$EVIDENCE_NAMESPACE" "$HOLD_SECONDS"
