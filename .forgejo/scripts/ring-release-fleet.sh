#!/usr/bin/env bash
set -Eeuo pipefail
SHA="$RELEASE_SHA"
SHORT="${SHA:0:7}"
TAG="hive-${SHORT}"
if [ "$EVENT_NAME" = "workflow_dispatch" ]; then
  if [ "$REQUESTED_RING" != "fleet" ] || [ "$REQUESTED_SHA" != "$SHA" ]; then
    echo "::error::break-glass fleet requires ring=fleet and the exact full SHA"
    exit 1
  fi
fi
CUTOVER_SHA="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o jsonpath='{.metadata.annotations.hive725-cutover-complete-sha}' 2>/dev/null || true)"
PHASE1_SHA="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o jsonpath='{.metadata.annotations.hive725-phase1-sha}' 2>/dev/null || true)"
if [ "$REQUESTED_HIVE725_PHASE" = finalize ]; then
  [ "$EVENT_NAME" = workflow_dispatch ] && [ "$PHASE1_SHA" = "$SHA" ] || {
    echo "::error::HIVE-725 finalize requires an exact-SHA fleet dispatch after phase 1 is proven"
    exit 1
  }
  H725_MODE=finalize
elif [ -n "$CUTOVER_SHA" ]; then
  H725_MODE=steady
else
  H725_MODE=phase1
fi
export H725_MODE
H725_VERIFIER_SOURCE=0
if grep -Fq \
  "HIVE613_CONNECT_VERIFIER_CONTRACT = 'signed-owner-bound-v1'" \
  claws/views.py; then
  H725_VERIFIER_SOURCE=1
fi
export H725_VERIFIER_SOURCE
echo "HIVE-725 deploy mode: $H725_MODE verifier_source=$H725_VERIFIER_SOURCE (phase1=${PHASE1_SHA:-none} complete=${CUTOVER_SHA:-none})"

LAST_SHA="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o jsonpath='{.metadata.annotations.last-successful-sha}')"
[[ "$LAST_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::missing rollback anchor before R2"; exit 1; }
ROLLBACK_IMAGE="localhost:30500/shizuha-hive:hive-${LAST_SHA:0:7}"
curl -fsS --max-time 20 -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json'             "http://registry.registry.svc.cluster.local:5000/v2/shizuha-hive/manifests/hive-${LAST_SHA:0:7}" >/dev/null ||
  { echo "::error::rollback image absent before R2"; exit 1; }

# workflow_dispatch ring=fleet may skip canary; assert the same
# create-once key gate before any producer-capable rollout.

h725_snapshot_resource() {
  # `local` is a builtin: bash expands ALL of its assignment words
  # before performing any assignment, so a `file=...${kind}...` in the
  # same declaration reads `kind` while it is still unset and aborts
  # the whole R2 step under `set -u` ("kind: unbound variable").
  # Declare, then derive.
  local kind="$1" name="$2"
  local file="/tmp/h725-${kind}-${name}.yaml"
  rm -f "$file" "${file}.absent"
  if ! .forgejo/scripts/kubectl -n shizuha-hive get "$kind" "$name" -o json 2>/dev/null |
      python3 .forgejo/scripts/sanitize-rollback-resource.py >"$file"; then
    : >"${file}.absent"
    rm -f "$file"
  fi
}
h725_ready_pod() {
  local namespace="$1" selector="$2" container="$3"
  .forgejo/scripts/kubectl -n "$namespace" get pod -l "$selector" -o json |
    python3 .forgejo/scripts/select-ready-pod.py "$container"
}
h725_restore_boundary() {
  [ -e /tmp/h725-boundary-mutated ] || return 0
  local rc=0 kind name file
  for resource in \
    'service hive-connect-delivery' \
    'networkpolicy allow-intra-namespace' \
    'networkpolicy allow-node-healthchecks' \
    'networkpolicy allow-connect-delivery-ingress'; do
    kind="${resource%% *}"
    name="${resource#* }"
    file="/tmp/h725-${kind}-${name}.yaml"
    if [ -f "${file}.absent" ]; then
      .forgejo/scripts/kubectl -n shizuha-hive delete "$kind" "$name" --ignore-not-found || rc=1
    else
      # These policies can be reconciled between snapshot and rollback.
      # A plain replace then fails on the stale resourceVersion and
      # leaves the release ring unable to restore its own boundary.
      .forgejo/scripts/kubectl apply --server-side --force-conflicts \
        --field-manager=hive-release-ring-rollback \
        -f "$file" || rc=1
    fi
  done
  return "$rc"
}

r2_failure() {
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
  trap - ERR
  trap '' TERM INT
  set +e
  [[ -f /tmp/fleet-gate.log ]] && cat /tmp/fleet-gate.log
  capture_output="$(RING_EVIDENCE_NAMESPACE=origin timeout 30s \
    .forgejo/scripts/capture-ring-evidence.sh fleet shizuha-hive hive hive \
      "$SHA" "command=${failed_command} ${stage_info}" /tmp/fleet-gate.log 2>&1)"
  capture_rc=$?
  if [ "$capture_rc" -eq 0 ]; then
    evidence="$capture_output"
  else
    echo "::warning::bounded fleet evidence capture failed rc=${capture_rc}: ${capture_output}"
    evidence="artifact=unavailable capture_rc=${capture_rc}"
  fi
  # Preserve the failing topology before either phase-local or full
  # rollback restores the previously verified boundary.
  if [ -f /tmp/h725-phase1-proven ]; then
    # Phase-local rollback: once the exact verifier image, shared
    # secret, signed tenant-bound request, and L4 isolation have all
    # passed, never roll back to the prior unsigned-accepting image.
    # Re-open only the legacy 8030 listener while retaining the HMAC
    # verifier and dedicated NetworkPolicy, so a retry can safely
    # finish the final denial without creating an outage window.
    .forgejo/scripts/kubectl -n shizuha-hive set env deploy/hive --containers=hive \
      HIVE_CONNECT_ENFORCE_DEDICATED_LISTENER=false || rollback_rc=1
    # This is another two-replica serial rollout with
    # minReadySeconds=180. A 300s deadline cannot complete even when
    # both replicas become Ready immediately.
    .forgejo/scripts/kubectl -n shizuha-hive rollout status deploy/hive --timeout=1800s || rollback_rc=1
    .forgejo/scripts/kubectl -n shizuha-hive annotate deploy/hive \
      "hive725-phase1-sha=${SHA}" --overwrite || rollback_rc=1
    evidence="phase=hive725-phase2 command=${failed_command} ${stage_info} ${evidence} $(tail -c 1500 /tmp/fleet-gate.log 2>/dev/null | tr '\n' ' ') fixture_stderr=$(tail -c 1500 /tmp/h725-fixture.err 2>/dev/null | tr '\n' ' ')"
    # HIVE-2819 (D2): evidence/alert delivery is paperwork, not rollback
    # atomicity. record-rollout-failure.sh retries internally; a persistent
    # delivery failure is downgraded to ::warning:: so it can never corrupt
    # rollback_rc or mask the rollback result (the atomicity legs above own
    # rollback_rc). The job's redness stays governed by exit original_rc.
    if ! .forgejo/scripts/record-rollout-failure.sh fleet "$SHA" "$LAST_SHA" \
      "rollback_rc=${rollback_rc} ${evidence}"; then
      echo "::warning::rollback evidence delivery failed (ring=fleet bad_sha=${SHA} restored=${LAST_SHA}) — rollback atomicity result unchanged (rollback_rc=${rollback_rc}); file/close the Pulse rollback artifact manually"
    fi
    [ "$rollback_rc" -eq 0 ] || echo "::error::HIVE-725 phase-local rollback had failures"
    exit "$original_rc"
  fi
  h725_restore_boundary || rollback_rc=1
  .forgejo/scripts/kubectl -n shizuha-hive set image deploy/hive hive="$ROLLBACK_IMAGE" || rollback_rc=1
  # hive-api is rolled with hive; a rollback that restores only deploy/hive
  # leaves a gunicorn writer on the failed image and the next native release
  # publish refuses.
  if .forgejo/scripts/kubectl -n shizuha-hive get deploy hive-api >/dev/null 2>&1; then
    .forgejo/scripts/kubectl -n shizuha-hive set image deploy/hive-api hive-api="$ROLLBACK_IMAGE" || rollback_rc=1
  fi
  # codex-refresh is a separate Deploy (not an API sidecar) so arch
  # mismatch cannot take Hive readiness down during rollbacks either.
  if .forgejo/scripts/kubectl -n shizuha-hive get deploy hive-codex-refresh >/dev/null 2>&1; then
    .forgejo/scripts/kubectl -n shizuha-hive set image deploy/hive-codex-refresh codex-refresh="$ROLLBACK_IMAGE" || rollback_rc=1
  fi
  .forgejo/scripts/kubectl -n shizuha-hive label deploy/hive "build_sha=${LAST_SHA}" "release_ring=fleet" --overwrite || rollback_rc=1
  .forgejo/scripts/kubectl -n shizuha-hive patch deploy/hive --type=merge \
    -p "{\"spec\":{\"template\":{\"metadata\":{\"labels\":{\"build_sha\":\"${LAST_SHA}\",\"release_ring\":\"fleet\"}}}}}" || rollback_rc=1
  # INV-F may already have advanced the anchor before a later cleanup
  # command fails. Rollback is atomic only when image, build labels,
  # and both release-anchor spellings all return to LAST_SHA.
  .forgejo/scripts/kubectl -n shizuha-hive annotate deploy/hive \
    "last-successful-sha=${LAST_SHA}" \
    "shizuha.io/last-successful-sha=${LAST_SHA}" \
    --overwrite || rollback_rc=1
  restored_anchor="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o jsonpath='{.metadata.annotations.last-successful-sha}' 2>/dev/null || true)"
  restored_namespaced_anchor="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o jsonpath='{.metadata.annotations.shizuha\.io/last-successful-sha}' 2>/dev/null || true)"
  [ "$restored_anchor" = "$LAST_SHA" ] || rollback_rc=1
  [ "$restored_namespaced_anchor" = "$LAST_SHA" ] || rollback_rc=1
  .forgejo/scripts/kubectl -n shizuha-hive rollout status deploy/hive --timeout=1800s || rollback_rc=1
  # HIVE-2819 (D2): see the phase1 branch — evidence delivery is paperwork.
  if ! .forgejo/scripts/record-rollout-failure.sh fleet "$SHA" "$LAST_SHA" \
    "rollback_rc=${rollback_rc} command=${failed_command} ${stage_info} ${evidence} $(tail -c 1500 /tmp/fleet-gate.log 2>/dev/null | tr '\n' ' ') fixture_stderr=$(tail -c 1500 /tmp/h725-fixture.err 2>/dev/null | tr '\n' ' ')"; then
    echo "::warning::rollback evidence delivery failed (ring=fleet bad_sha=${SHA} restored=${LAST_SHA}) — rollback atomicity result unchanged (rollback_rc=${rollback_rc}); file/close the Pulse rollback artifact manually"
  fi
  [ "$rollback_rc" -eq 0 ] || echo "::error::fleet rollback/evidence contract had failures"
  exit "$original_rc"
}
PREV="$(.forgejo/scripts/kubectl get deploy -n shizuha-hive hive -o jsonpath='{.spec.template.spec.containers[?(@.name=="hive")].image}' 2>/dev/null || true)"
if [ -n "${PREV}" ]; then
  echo "Current image: ${PREV}"
  echo "Rollback command: kubectl set image -n shizuha-hive deploy/hive hive=${PREV} && kubectl rollout status -n shizuha-hive deploy/hive"
fi

# Phase 1 must be independently deployable before Connect points at 8031.
# HIVE-1334: gate ONLY on H725_MODE != phase1 — never OR H725_VERIFIER_SOURCE
# (that forced Connect-on-8031 preflight during phase1 and bricked fleet).
# Finalization/steady-state runs fail closed on the real signer contract.
if [ "$H725_MODE" != phase1 ]; then
# HIVE-725/HIVE-613 preflight: Connect must already be on the
# signer-first declarative release before Hive rolls the verifier.
# Secrets are namespace-scoped, so compare the decoded values without
# printing them; a merely present SecretKeyRef is not proof that both
# workloads share the same functional credential.
CONNECT_HMAC_B64="$(.forgejo/scripts/kubectl -n shizuha get secret hive-connect-hmac -o jsonpath='{.data.secret}')"
HIVE_HMAC_B64="$(.forgejo/scripts/kubectl -n shizuha-hive get secret hive-connect-hmac -o jsonpath='{.data.secret}')"
[ -n "$CONNECT_HMAC_B64" ] && [ -n "$HIVE_HMAC_B64" ] || {
  echo "::error::HIVE-725 shared HMAC SecretKeyRef is unresolved"
  exit 1
}
CONNECT_HMAC_SHA="$(printf '%s' "$CONNECT_HMAC_B64" | base64 -d | sha256sum | cut -d' ' -f1)"
HIVE_HMAC_SHA="$(printf '%s' "$HIVE_HMAC_B64" | base64 -d | sha256sum | cut -d' ' -f1)"
[ "$CONNECT_HMAC_SHA" = "$HIVE_HMAC_SHA" ] || {
  echo "::error::HIVE-725 Connect/Hive HMAC secrets differ"
  exit 1
}
.forgejo/scripts/kubectl -n shizuha get deploy \
  -l 'app.kubernetes.io/name=shizuha-connect,app.kubernetes.io/component=backend' \
  -o json > /tmp/h725-connect-deployments.json
python3 - <<'PY'
import json

expected_url = (
    "http://hive-connect-delivery.shizuha-hive.svc.cluster.local:8031/"
    "api/v1/internal/connect/messages/"
)
items = json.load(open("/tmp/h725-connect-deployments.json")).get("items", [])
if len(items) != 1:
    raise SystemExit(f"expected one Connect backend Deployment, found {len(items)}")
containers = items[0]["spec"]["template"]["spec"].get("containers", [])
if not containers:
    raise SystemExit("Connect backend Deployment has no containers")
env = {
    item.get("name"): item
    for container in containers
    for item in container.get("env", [])
}
secret = env.get("HIVE_CONNECT_HMAC_SECRET", {}).get("valueFrom", {}).get("secretKeyRef", {})
if secret != {"name": "hive-connect-hmac", "key": "secret"}:
    raise SystemExit("Connect signer SecretKeyRef is not the HIVE-725 canonical ref")
if env.get("HIVE_CONNECT_DELIVERY_URL", {}).get("value") != expected_url:
    raise SystemExit("Connect delivery URL is not the dedicated 8031 Service")
PY
CONNECT_POD="$(h725_ready_pod shizuha \
  'app.kubernetes.io/name=shizuha-connect,app.kubernetes.io/component=backend' \
  shizuha-connect)"
[ -n "$CONNECT_POD" ] || { echo "::error::no Ready Connect signer pod"; exit 1; }
.forgejo/scripts/kubectl -n shizuha exec "$CONNECT_POD" -- python manage.py shell -c '
from connect.services.hive_agents import _hmac_sign_payload
headers = _hmac_sign_payload({"hive725": "preflight"})
if not headers.get("X-Hive-Signature-256") or not headers.get("X-Hive-Timestamp"):
    raise SystemExit("Connect image cannot produce HIVE-613 signatures")
print("Connect signer image/ref preflight passed")
'
fi
# Scheduling guard must be applied on every CI deploy. This prevents a
# future apply/set-image path from sending Hive backend to s1 again.
# Arm the handler before the first production mutation; every later
# failure (migrate, rollout, config, probe, watch, anchor persistence)
# restores the verified anchor and records owner evidence.
trap 'r2_failure "$?" "$BASH_COMMAND" "${PIPESTATUS[*]}"' ERR
trap 'r2_failure 143 "signal=TERM"' TERM
trap 'r2_failure 130 "signal=INT"' INT
# Preserve installation-owned settings before the first config/image rollout.
.forgejo/scripts/preserve-installation-profile.sh
.forgejo/scripts/kubectl -n shizuha-hive patch deploy/hive --type=merge --patch-file=/dev/stdin <<'PATCH'
{
  "spec": {
    "minReadySeconds": 180,
    "progressDeadlineSeconds": 1800,
    "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
    "template": {
      "spec": {
        "nodeSelector": null,
        "affinity": {
          "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
              "nodeSelectorTerms": [
                {
                  "matchExpressions": [
                    {"key": "shizuha.io/platform", "operator": "In", "values": ["true"]},
                    {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["s1", "i9-ws"]}
                  ]
                }
              ]
            }
          }
        }
      }
    }
  }
}
PATCH

# Run Django migrations with the exact image about to be rolled out.
# Fail closed: if migrations fail, do not update the Deployment to a
# schema-incompatible image. The Job copies the live backend pod env,
# secrets, volumes, and scheduling guard, then swaps only image/command.
MIGRATE_JOB="ci-migrate-hive-${SHORT}"
.forgejo/scripts/kubectl -n shizuha-hive delete job "${MIGRATE_JOB}" --ignore-not-found --wait=true
.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o json > /tmp/hive-deploy.json
TAG="${TAG}" MIGRATE_JOB="${MIGRATE_JOB}" python3 - <<'PY' > /tmp/hive-migrate-job.json
import copy, json, os

tag = os.environ["TAG"]
job_name = os.environ["MIGRATE_JOB"]
deploy = json.load(open("/tmp/hive-deploy.json"))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    c for c in pod_spec.get("containers", [])
    if c.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["image"] = f"localhost:30500/shizuha-hive:{tag}"
# A login shell rewrites the image's PATH and hides /app/.venv,
# turning every attempt into a deterministic "No module named
# django" failure. The image-owned runner uses the venv explicitly,
# classifies retryable connectivity failures, and returns exit 42
# for the podFailurePolicy to fail deterministic errors immediately.
container["command"] = [
    "/bin/bash",
    "/app/.forgejo/scripts/run-migrations-with-retry.sh",
]
container.pop("args", None)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe",
    "lifecycle",
):
    container.pop(field, None)

job_pod_spec = {"restartPolicy": "Never", "containers": [container]}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes",
    "imagePullSecrets", "serviceAccount", "serviceAccountName",
    "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

job = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {
        "name": job_name,
        "namespace": "shizuha-hive",
        "labels": {"app": "ci-migrate", "service": "hive"},
    },
    "spec": {
        # A healthy platform-db failover can briefly remove the
        # primary Service endpoint. Retry the idempotent migration
        # instead of failing an otherwise valid release on the first
        # refused connection.
        "backoffLimit": 6,
        "activeDeadlineSeconds": 900,
        "ttlSecondsAfterFinished": 900,
        "podFailurePolicy": {
            "rules": [{
                "action": "FailJob",
                "onExitCodes": {
                    "containerName": "hive",
                    "operator": "In",
                    "values": [42],
                },
            }],
        },
        "template": {
            "metadata": {
                "labels": {"app": "ci-migrate", "service": "hive"},
            },
            "spec": job_pod_spec,
        },
    },
}
print(json.dumps(job))
PY
.forgejo/scripts/kubectl apply -f /tmp/hive-migrate-job.json
if ! .forgejo/scripts/kubectl wait -n shizuha-hive --for=condition=complete "job/${MIGRATE_JOB}" --timeout=900s; then
  echo "::error::Django migrations failed; refusing to roll Hive backend"
  .forgejo/scripts/kubectl logs -n shizuha-hive "job/${MIGRATE_JOB}" --all-containers --tail=120 || true
  exit 1
fi
.forgejo/scripts/kubectl logs -n shizuha-hive "job/${MIGRATE_JOB}" --all-containers --tail=80 || true

# The tier ConfigMap and API default jointly define the model for claws whose
# database row has no explicit override. They had both remained pinned to the
# retired Qwen tier even after the DeepSeek-only cutover. Own the ConfigMap in
# the release ring so a source change cannot pass CI without reaching prod.
.forgejo/scripts/kubectl apply -f k8s/hive-tiers.yaml
.forgejo/scripts/kubectl -n shizuha-hive get configmap hive-tiers -o json > /tmp/hive-tiers-live.json
python3 - <<'PY'
import json
import yaml

config_map = json.load(open('/tmp/hive-tiers-live.json'))
tiers = yaml.safe_load(config_map['data']['tiers.yaml'])['tiers']
assert tiers['free']['llm']['default_model'] == 'DeepSeek-V4-Flash', tiers
PY

.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o json > /tmp/hive-deploy-pre-rollout.json
TAG="${TAG}" BUILD_SHA="${SHA}" H725_MODE="${H725_MODE}" python3 - <<'PY' > /tmp/hive-codex-refresh-deploy-patch.json
import copy
import json
import os

tag = os.environ["TAG"]
build_sha = os.environ["BUILD_SHA"]
h725_mode = os.environ["H725_MODE"]
deploy = json.load(open("/tmp/hive-deploy-pre-rollout.json"))
pod_spec = deploy["spec"]["template"]["spec"]
service_account = str(pod_spec.get("serviceAccountName") or "default")
containers = copy.deepcopy(pod_spec.get("containers", []))
hive = next(c for c in containers if c.get("name") == "hive")
image = f"localhost:30500/shizuha-hive:{tag}"
retired_env_names = {
    # Hive token pools are the exclusive fleet provider-auth source.
    # Do not preserve the former host/org credentials importer when
    # deriving a release patch from the live Deployment.
    "HIVE_ORG_CREDENTIALS_JSON",
}

def remove_retired_env(container):
    container["env"] = [
        item for item in (container.get("env") or [])
        if item.get("name") not in retired_env_names
    ]

def ensure_env(container, name, value):
    # Desired release settings replace stale live values. Deriving a
    # patch from the running Deployment must not preserve an old pin.
    env = [
        item for item in (container.get("env") or [])
        if item.get("name") != name
    ]
    env.append({"name": name, "value": value})
    container["env"] = env

def ensure_env_from_secret(container, name, secret_name, key, optional=False):
    env = [
        item for item in (container.get("env") or [])
        if item.get("name") != name
    ]
    env.append({
        "name": name,
        "valueFrom": {"secretKeyRef": {
            "name": secret_name, "key": key, "optional": optional,
        }},
    })
    container["env"] = env

# The refresh sidecar records counters through Django's shared cache,
# while the web container exposes them at /codex-refresh/metrics. Keep
# both containers on the same Redis DBs; localhost defaults would make
# sidecar metrics invisible to the scraped exporter.
for container in (hive,):
    remove_retired_env(container)
    ensure_env(container, "REDIS_URL", "redis://shizuha-redis-ha-master.shizuha.svc.cluster.local:6379/5")
    ensure_env(container, "CACHE_REDIS_URL", "redis://shizuha-redis-ha-master.shizuha.svc.cluster.local:6379/6")
    ensure_env(container, "FLEET_BROKER_IMAGE", "localhost:30500/mcp-auth-proxy:src-3a0a1b6a159c")
    ensure_env(container, "FLEET_COORDINATOR_OAUTH_SECRET_NAME", "hive-coordinator-broker-oauth")
    # Native27 source bootstrap precedes activation. Every later missing/error
    # read refuses actuation; never fall back to the legacy daemon release.
    ensure_env(container, "HIVE_NATIVE_RUNTIME_RELEASE_ENABLED", "true")
    ensure_env(container, "HIVE_NATIVE_RUNTIME_RELEASE_NAMESPACE", "shizuha-hive")
    ensure_env(container, "HIVE_CORTEX_DEFAULT_MODEL", "DeepSeek-V4-Flash")
    ensure_env_from_secret(
        container, "HIVE_CAPABILITY_EVIDENCE_PRIVATE_KEY_PEM",
        "hive-capability-evidence-signer", "private-key.pem",
    )
    ensure_env_from_secret(
        container, "HIVE_CAPABILITY_EVIDENCE_KID",
        "hive-capability-evidence-signer", "kid",
    )
    # HIVE-1153 (Security P1): the dedicated Pulse->Hive service credential
    # MUST reach the live producer. Without it the endpoint has an empty
    # expected token and every Pulse request 403s. CI provisions the
    # `hive-capability-evidence-client-auth` secret (key `token`) create-once
    # in shizuha-hive; inject it into the authoritative fleet patch too.
    ensure_env_from_secret(
        container, "HIVE_CAPABILITY_EVIDENCE_AUTH_TOKEN",
        "hive-capability-evidence-client-auth", "token",
    )
    # HIVE-1878: this read route fails closed when its dedicated credential is
    # absent. The fleet patch is derived from live state, so inject the reviewed
    # Secret ref here as well as in the source manifests.
    ensure_env_from_secret(
        container, "COMPANY_STATE_READ_TOKEN",
        "hive-company-state-read", "token",
    )
    # A renewable, read-only service identity replaces expiring user bearers
    # for Pulse fleet snapshots. Preserve these refs in every fleet rollout.
    for name, key in (
        ("HIVE_PULSE_OAUTH_CLIENT_ID", "client-id"),
        ("HIVE_PULSE_OAUTH_CLIENT_SECRET", "client-secret"),
        ("HIVE_PULSE_OAUTH_TOKEN_URL", "token-url"),
    ):
        ensure_env_from_secret(container, name, "hive-pulse-fleet-read", key)
    ensure_env(container, "HIVE_CAPABILITY_EVIDENCE_SERVICE_ACCOUNT", service_account)

    # Phase 1 creates and injects the shared key before the dual
    # listener rollout.  The pre-verifier image ignores the extra
    # env, while a later verifier merge remains available instead of
    # deterministically returning 401 until manual finalization.
    ensure_env_from_secret(
        container, "HIVE_CONNECT_HMAC_SECRET", "hive-connect-hmac", "secret",
    )
    ensure_env(
        container,
        "HIVE_CONNECT_ENFORCE_DEDICATED_LISTENER",
        "true" if h725_mode == "steady" else "false",
    )
    # HIVE-671: only the web producer may hold the dedicated Hive signing
    # key. Keep cutover disabled until the coordinated verifier/code PRs
    # are merged, deployed, and live-canary ready. The pre-verifier image
    # ignores the extra env; cutover=false keeps the shared HMAC fallback
    # authoritative until the reviewed cutover step. The secret is
    # provisioned create-once in shizuha-hive (hive-service-signing-key/key).
    ensure_env_from_secret(
        container, "HIVE_SERVICE_SIGNING_KEY",
        "hive-service-signing-key", "key",
        optional=True,
    )
    ensure_env(container, "HIVE_SIGNING_KEY_CUTOVER", "false")

# K3s NetworkPolicy observes kubelet HTTP probes through CNI source
# NAT on this fleet. Pin backend probes to loopback so reconciling the
# ingress boundary can never evict every Hive endpoint again.
probe_command = [
    "python",
    "-c",
    (
        "import urllib.request; "
        "response = urllib.request.urlopen("
        "'http://127.0.0.1:8030/api/health/', timeout=5); "
        "raise SystemExit(0 if response.status == 200 else 1)"
    ),
]
for probe_name in ("readinessProbe", "livenessProbe"):
    probe = copy.deepcopy(hive.get(probe_name) or {})
    for transport in ("httpGet", "tcpSocket", "grpc"):
        probe.pop(transport, None)
    probe["exec"] = {"command": probe_command}
    hive[probe_name] = probe

hive["image"] = image
# HA: never re-attach codex-refresh as a sidecar. Single-arch images
# make arm64 API pods NotReady and stall maxUnavailable:0 rolls.
# The loop runs as deploy/hive-codex-refresh (amd64 pin) below.
containers = [c for c in containers if c.get("name") != "codex-refresh"]
# Keep hive first/only for the API pod; other non-API containers stay.
if not any(c.get("name") == "hive" for c in containers):
    containers.insert(0, hive)
else:
    containers = [hive if c.get("name") == "hive" else c for c in containers]
backend_labels = {
    "app.kubernetes.io/name": "hive",
    "app.kubernetes.io/component": "backend",
}
patch = {
    "metadata": {"labels": {
        "build_sha": build_sha,
        "release_ring": "fleet",
        **backend_labels,
    }},
    "spec": {"template": {
        "metadata": {"labels": {
            "build_sha": build_sha,
            "release_ring": "fleet",
            **backend_labels,
        }},
        "spec": {"containers": containers},
    }},
}
print(json.dumps(patch))
PY
# HIVE-725 invariant: narrow every pre-existing broad policy before
# any new pod can expose 8031. Old 8030-only pods are unaffected.
# Snapshot first so an image rollout failure restores policy and image
# to the same verified release boundary.
rm -f /tmp/h725-boundary-mutated
h725_snapshot_resource service hive-connect-delivery
h725_snapshot_resource networkpolicy allow-intra-namespace
h725_snapshot_resource networkpolicy allow-node-healthchecks
h725_snapshot_resource networkpolicy allow-connect-delivery-ingress
: >/tmp/h725-boundary-mutated
# NetworkPolicy reconcilers may update these objects between our read
# and write. Own the release boundary explicitly so that a benign
# resourceVersion race cannot roll back an otherwise healthy image.
.forgejo/scripts/kubectl apply --server-side --force-conflicts \
  --field-manager=hive-release-ring \
  -f k8s/hive-connect-delivery-preflight-netpol.yaml

.forgejo/scripts/kubectl patch -n shizuha-hive deploy/hive --type=merge --patch-file=/tmp/hive-codex-refresh-deploy-patch.json
# A new backend must remain Ready through the delayed daemon reconnect
# and snapshot window before an old healthy replica is removed.
# Two replicas roll serially (maxUnavailable=0) and each must remain
# Ready for minReadySeconds=180. A cold local-registry pull has taken
# nearly nine minutes on a slow node; 12 minutes total falsely rolled
# back a healthy image seconds after that pull completed.
.forgejo/scripts/kubectl rollout status -n shizuha-hive deploy/hive --timeout=1800s

# hive-api serves the same Django code under gunicorn. The native runtime
# publisher treats every shizuha-hive image as a writer, so this Deployment
# has to land on the same tag or generation promotion stays refused.
# Reuse the rendered producer authority: an image update preserves old env
# overrides, which otherwise lets API lifecycle writes restore an old broker.
# Do not add another tag constant here; broker publication owns four pins.
API_BROKER_IMAGE="$(python3 - <<'PY'
import json

patch = json.load(open('/tmp/hive-codex-refresh-deploy-patch.json'))
containers = patch['spec']['template']['spec']['containers']
hive = next(container for container in containers if container['name'] == 'hive')
pins = [item for item in hive.get('env', []) if item.get('name') == 'FLEET_BROKER_IMAGE']
assert len(pins) == 1 and isinstance(pins[0].get('value'), str) and pins[0]['value'], \
    'rendered Hive producer must have exactly one literal broker image'
print(pins[0]['value'])
PY
)"
API_COORDINATOR_OAUTH_SECRET_NAME="$(python3 - <<'PY'
import json
patch = json.load(open('/tmp/hive-codex-refresh-deploy-patch.json'))
hive = next(c for c in patch['spec']['template']['spec']['containers'] if c['name'] == 'hive')
values = [e for e in hive.get('env', []) if e.get('name') == 'FLEET_COORDINATOR_OAUTH_SECRET_NAME']
assert len(values) == 1 and isinstance(values[0].get('value'), str) and values[0]['value'], \
    'rendered Hive producer must have exactly one literal coordinator OAuth Secret'
print(values[0]['value'])
PY
)"
.forgejo/scripts/kubectl -n shizuha-hive set image deploy/hive-api \
  "hive-api=localhost:30500/shizuha-hive:${TAG}"
.forgejo/scripts/kubectl -n shizuha-hive set env deploy/hive-api --containers=hive-api \
  HIVE_NATIVE_RUNTIME_RELEASE_ENABLED=true \
  HIVE_NATIVE_RUNTIME_RELEASE_NAMESPACE=shizuha-hive \
  "FLEET_BROKER_IMAGE=${API_BROKER_IMAGE}" \
  "FLEET_COORDINATOR_OAUTH_SECRET_NAME=${API_COORDINATOR_OAUTH_SECRET_NAME}"
.forgejo/scripts/kubectl rollout status -n shizuha-hive deploy/hive-api --timeout=1800s

# Separate codex-refresh Deploy: apply base + pin release image.
# Must never share a Pod with API readiness (operator HA 2026-07-28).
.forgejo/scripts/kubectl apply -f k8s/hive-codex-refresh-deploy.yaml
.forgejo/scripts/kubectl -n shizuha-hive set image deploy/hive-codex-refresh \
  "codex-refresh=localhost:30500/shizuha-hive:${TAG}"
.forgejo/scripts/kubectl rollout status -n shizuha-hive deploy/hive-codex-refresh --timeout=600s || \
  echo "::warning::hive-codex-refresh rollout slow/failed (API HA unaffected)"

# The pre-rollout policy temporarily admits the pod CIDR so the old
# HTTP-probed replicas survive the transition. Once every new replica
# is Ready on localhost exec probes, remove that transitional source.
.forgejo/scripts/kubectl apply --server-side --force-conflicts \
  --field-manager=hive-release-ring \
  -f k8s/hive-connect-delivery-postrollout-netpol.yaml

# API Services previously selected app=hive alone. Scheduled workers
# share that legacy label, have no readiness probe, and were therefore
# admitted as ready port-8030 endpoints while running. Apply the
# component-scoped selectors only after the new backend pods are Ready,
# preserving an interruption-free cutover.
.forgejo/scripts/kubectl apply -f k8s/hive-api-services.yaml
for SERVICE in hive hive-direct hive-tail; do
  COMPONENT="$(.forgejo/scripts/kubectl -n shizuha-hive get service "${SERVICE}" \
    -o jsonpath='{.spec.selector.app\.kubernetes\.io/component}')"
  [ "${COMPONENT}" = "backend" ] || {
    echo "::error::${SERVICE} selector is not backend-scoped"
    exit 1
  }
done

LIVE="$(.forgejo/scripts/kubectl get deploy -n shizuha-hive hive -o jsonpath='{.spec.template.spec.containers[?(@.name=="hive")].image}')"
REFRESH="$(.forgejo/scripts/kubectl get deploy -n shizuha-hive hive-codex-refresh -o jsonpath='{.spec.template.spec.containers[?(@.name=="codex-refresh")].image}' 2>/dev/null || true)"
echo "Live image: ${LIVE}"
echo "Codex refresh image: ${REFRESH}"
echo "${LIVE}" | grep -q "${TAG}" || { echo "::error::hive image mismatch"; exit 1; }
LIVE_CORTEX_DEFAULT="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="hive")].env[?(@.name=="HIVE_CORTEX_DEFAULT_MODEL")].value}')"
[ "$LIVE_CORTEX_DEFAULT" = "DeepSeek-V4-Flash" ] || {
  echo "::error::Hive Cortex default is not DeepSeek-only: ${LIVE_CORTEX_DEFAULT}"
  exit 1
}
# Fail API ring only if hive image is wrong. codex-refresh is best-effort
# for HA: missing/slow refresh must not outage /hive/api.
if [ -n "${REFRESH}" ]; then
  echo "${REFRESH}" | grep -q "${TAG}" || { echo "::error::codex-refresh image mismatch"; exit 1; }
  .forgejo/scripts/kubectl get deploy -n shizuha-hive hive-codex-refresh -o jsonpath='{.spec.template.spec.containers[?(@.name=="codex-refresh")].command}' | grep -q codex_refresh_loop || { echo "::error::codex-refresh worker missing"; exit 1; }
  # jsonpath prints the command as a JSON array ("--quota-stale-after","600"),
  # so a space-separated pattern can never match — that false negative failed
  # run 3200's fleet leg AFTER a fully healthy deploy and blocked the INV-F
  # anchor write (2026-07-11).
  .forgejo/scripts/kubectl get deploy -n shizuha-hive hive-codex-refresh -o jsonpath='{.spec.template.spec.containers[?(@.name=="codex-refresh")].command}' | grep -q -- '--quota-stale-after","600' || { echo "::error::codex-refresh quota freshness missing"; exit 1; }
else
  echo "::warning::hive-codex-refresh deploy missing after apply"
fi
# API pod must not re-grow a codex-refresh sidecar.
if .forgejo/scripts/kubectl get deploy -n shizuha-hive hive -o jsonpath='{.spec.template.spec.containers[*].name}' | tr ' ' '\n' | grep -qx codex-refresh; then
  echo "::error::codex-refresh reappeared as hive API sidecar (HA regression)"
  exit 1
fi
.forgejo/scripts/kubectl get deploy -n shizuha-hive hive -o json | python3 -c '
import json, sys
retired = {"HIVE_ORG_CREDENTIALS_JSON"}
deployment = json.load(sys.stdin)
present = sorted({
    item.get("name")
    for container in deployment["spec"]["template"]["spec"].get("containers", [])
    for item in container.get("env", [])
    if item.get("name") in retired
})
if present:
    raise SystemExit(f"retired credential environment survived deployment reconciliation: {present}")
'

# HIVE-671: the dedicated Hive signing key must reach the live producer
# via the CI-owned fleet patch, and the cutover flag must stay CI-owned.
# This gate proves the flag is never hand-set (no kubectl set env) and
# the producer-only credential never leaks to a non-producer container.
.forgejo/scripts/kubectl get deploy -n shizuha-hive hive -o json | python3 -c '
import json, sys
deployment = json.load(sys.stdin)
containers = {
    c["name"]: c
    for c in deployment["spec"]["template"]["spec"].get("containers", [])
}
hive_env = {
    item.get("name"): item
    for item in containers["hive"].get("env", [])
}
signing_ref = (
    hive_env.get("HIVE_SERVICE_SIGNING_KEY", {})
    .get("valueFrom", {})
    .get("secretKeyRef", {})
)
expected_ref = {
    "name": "hive-service-signing-key",
    "key": "key",
    "optional": True,
}
if signing_ref != expected_ref:
    raise SystemExit(f"Hive signing-key reference drifted: {signing_ref!r}")
cutover = hive_env.get("HIVE_SIGNING_KEY_CUTOVER", {}).get("value")
if cutover not in {"false", "true"}:
    raise SystemExit(f"Hive signing-key cutover is not CI-owned: {cutover!r}")
'

# HIVE-725 phase 1: only after the dual-port image is Ready, publish
# its Service and exact Connect allow. The broad policies were already
# narrowed before rollout, so no pod-IP exposure window exists.
.forgejo/scripts/kubectl apply --server-side --force-conflicts \
  --field-manager=hive-release-ring \
  -f k8s/hive-connect-delivery-boundary.yaml
H725_COMPONENT="$(.forgejo/scripts/kubectl -n shizuha-hive get service \
  hive-connect-delivery \
  -o jsonpath='{.spec.selector.app\.kubernetes\.io/component}')"
[ "$H725_COMPONENT" = backend ] || {
  echo "::error::HIVE-725 dedicated listener Service is not backend-scoped"
  exit 1
}
.forgejo/scripts/kubectl -n shizuha-hive get endpoints hive-connect-delivery \
  -o jsonpath='{.subsets[0].addresses[0].ip}' | grep -q . || {
    echo "::error::HIVE-725 dedicated listener Service has no ready endpoint"
    exit 1
  }

# Positive L4 control from the exact allowed source. A CNI may reject
# denied TCP with either a timeout or an immediate refusal; proving
# this Connect pod can open 8031 distinguishes policy denial from a
# missing listener before the negative probe accepts either result.
CONNECT_POD="$(h725_ready_pod shizuha \
  'app.kubernetes.io/name=shizuha-connect,app.kubernetes.io/component=backend' \
  shizuha-connect)"
[ -n "$CONNECT_POD" ] || { echo "::error::no Connect backend pod for HIVE-725 probe"; false; }
.forgejo/scripts/kubectl -n shizuha exec "$CONNECT_POD" -c shizuha-connect -- python -c '
import socket
with socket.create_connection(
    ("hive-connect-delivery.shizuha-hive.svc.cluster.local", 8031), 5
):
    print("authorized Connect 8031 TCP control: accepted")
'

# HIVE-1334: signed authority probes require Connect already cut over
# to 8031 + signer — phase1 only publishes the listener/Service.
if [ "$H725_MODE" != phase1 ]; then
# Application-control proof from an actual Connect backend pod. Use
# the exact Shizuha-owned qa-hive-fixture Claw established by CON-130
# and CON-156; never select an arbitrary tenant. An empty
# content request passes HMAC + owner/org/participant validation and
# then returns 200 ignored, without dispatching to the user's runtime.
HIVE_POD="$(h725_ready_pod shizuha-hive app=hive hive)"
[ -n "$HIVE_POD" ] || { echo "::error::no Hive backend pod for HIVE-725 fixture"; exit 1; }
H725_FIXTURE="$(.forgejo/scripts/kubectl -n shizuha-hive exec "$HIVE_POD" -c hive -- \
  python manage.py shell -c '
import json
from claws.models import Claw
from claws.views import _hive_agent_user_id, _owner_personal_organization_id
fixture_id = "a2b6cc91-ff1a-4b19-a7ec-20c41738dedc"
try:
    claw = Claw.objects.exclude(status=Claw.Status.DELETED).get(
        id=fixture_id,
        owner_id=252,
        owner_username="qa-hive-fixture",
        agent_type=Claw.AgentType.CLAW,
    )
except Claw.DoesNotExist:
    raise SystemExit("named internal QA Claw unavailable for HIVE-725 probe")
org_id = _owner_personal_organization_id(claw.owner_id)
if not org_id:
    raise SystemExit("named internal QA owner has no authoritative personal org")
stable_id = f"hive:{claw.agent_type}:{claw.id}"
fixture = {
    "stable_id": stable_id,
    "owner_id": str(claw.owner_id),
    "organization_id": int(org_id),
    "agent_participant_id": str(_hive_agent_user_id(stable_id)),
}
print("H725_FIXTURE=" + json.dumps(fixture, separators=(",", ":"), sort_keys=True))
' 2>/tmp/h725-fixture.err | sed -n 's/^H725_FIXTURE=//p' | tail -n1)"
[ -n "$H725_FIXTURE" ] || { echo "::error::failed to derive HIVE-725 live fixture"; exit 1; }
# Exercise Connect's production dispatcher, including persisted direct
# binding and Admin-backed personal-org selection. Retry the complete
# authority + delivery transaction: Admin membership reads and the
# internal request can both fail transiently during a rollout, while
# the production path remains deliberately fail closed. Each attempt
# is rolled back locally and empty content is ignored by Hive, so a
# retry has no durable user-visible side effect.
# HIVE-2502: the exec TRANSPORT (API server -> node kubelet) fails
# transiently with `error dialing backend ... 502` while a node's
# kubelet flaps — runs 2662/2663 both died at this exact stage with
# pipeline_status=0 1 0 (printf ok, exec rc=1, tee ok). The probe's
# internal retry only covers application-level rejections; a
# transport-level exec abort never reaches it. Retry the complete
# pipeline on the transport class only; semantic/fail-closed
# rejections still fail loud on the first attempt. The ERR trap is
# disarmed per attempt so a retried transient cannot mint a spurious
# rollback task; it re-arms verbatim (line-240 contract) after each
# attempt, and exhausted retries invoke r2_failure directly with
# truthful command/PIPESTATUS evidence.
h725_probe_rc=0
h725_probe_delay=2
for h725_probe_attempt in 1 2 3; do
  h725_probe_out="$(mktemp /tmp/h725-probe.XXXXXX)"
  trap - ERR
  set +e
  printf '%s' "$H725_FIXTURE" | .forgejo/scripts/kubectl -n shizuha exec -i "$CONNECT_POD" -- python manage.py shell -c '
import json, sys, time, uuid
from django.db import transaction
from connect.models import Conversation, ConversationParticipant, DirectMessage
from connect.services.hive_agents import dispatch_hive_agent_message, hive_agent_user_id
fixture = json.load(sys.stdin)
owner_id = int(fixture["owner_id"])
stable_id = fixture["stable_id"]
agent_user_id = hive_agent_user_id(stable_id)
max_attempts = 6
for attempt in range(1, max_attempts + 1):
    with transaction.atomic():
        conversation, _ = Conversation.objects.get_or_create(
            conversation_type="direct",
            direct_key=Conversation.build_direct_key(owner_id, agent_user_id),
            defaults={"created_by_id": owner_id},
        )
        ConversationParticipant.objects.get_or_create(
            conversation=conversation, user_id=owner_id,
            defaults={
                "participant_type": "human",
                "user_email": "hive725-probe@internal.invalid",
            },
        )
        agent, _ = ConversationParticipant.objects.get_or_create(
            conversation=conversation, user_id=agent_user_id,
            defaults={"participant_type": "agent", "nickname": stable_id},
        )
        message = DirectMessage.objects.create(
            conversation=conversation, sender_id=owner_id,
            sender_email="hive725-probe@internal.invalid", sender_name="HIVE-725 probe",
            content="", client_message_id=uuid.uuid4(),
        )
        accepted = dispatch_hive_agent_message(conversation, message, agent)
        failures = list(
            DirectMessage.objects.filter(
                conversation=conversation,
                reply_to=message,
                message_type="system",
            ).values_list("content", flat=True)
        )
        diagnostics = failures or ["no diagnostic"]
        transaction.set_rollback(True)
    if accepted:
        print(
            "production Connect dispatcher/authority probe: accepted "
            f"on attempt {attempt}/{max_attempts}"
        )
        break
    if attempt < max_attempts:
        delay = min(2 ** attempt, 8)
        print(
            "production Connect dispatcher/authority probe rejected "
            f"attempt {attempt}/{max_attempts}: "
            f"{diagnostics}; retrying in {delay}s",
            file=sys.stderr,
        )
        time.sleep(delay)
    else:
        print(
            "production Connect dispatcher/authority probe rejected "
            f"attempt {attempt}/{max_attempts}: "
            f"{diagnostics}",
            file=sys.stderr,
        )
else:
    raise SystemExit(
        "production Connect dispatcher/authority probe failed "
        f"after {max_attempts} attempts"
    )
' >"$h725_probe_out" 2>&1
  h725_probe_rc=$?
  set -e
  trap 'r2_failure "$?" "$BASH_COMMAND" "${PIPESTATUS[*]}"' ERR
  tee -a /tmp/fleet-gate.log <"$h725_probe_out"
  if [ "$h725_probe_rc" -eq 0 ]; then
    rm -f "$h725_probe_out"
    break
  fi
  # Only the transient exec-transport class is retryable. Anything
  # else (semantic rejection, fail-closed denial, probe bug) breaks
  # immediately so the failure surfaces on the first attempt.
  if ! grep -qE "error dialing backend|Unable to connect to the server|502 Bad Gateway|connection refused" \
    "$h725_probe_out"; then
    rm -f "$h725_probe_out"
    break
  fi
  if [ "$h725_probe_attempt" -lt 3 ]; then
    echo "::warning::HIVE-725 authority probe exec transport failure (attempt ${h725_probe_attempt}/3, rc=${h725_probe_rc}); retrying in ${h725_probe_delay}s"
    sleep "$h725_probe_delay"
    h725_probe_delay=$((h725_probe_delay * 2))
  fi
  rm -f "$h725_probe_out"
done
if [ "$h725_probe_rc" -ne 0 ]; then
  echo "::error::HIVE-725 production Connect dispatcher/authority probe failed after transport retries (rc=${h725_probe_rc})"
  r2_failure "$h725_probe_rc" \
    "printf '%s' \"\$H725_FIXTURE\" | .forgejo/scripts/kubectl -n shizuha exec -i \"$CONNECT_POD\" -- python manage.py shell -c <HIVE-725 authority probe> (exec transport retries exhausted)" \
    "0 ${h725_probe_rc} 0"
fi
printf '%s' "$H725_FIXTURE" | .forgejo/scripts/kubectl -n shizuha exec -i "$CONNECT_POD" -- python -c '
import hashlib, hmac, json, os, sys, time, urllib.error, urllib.request, uuid

fixture = json.load(sys.stdin)
secret = os.environ.get("HIVE_CONNECT_HMAC_SECRET", "")
if not secret:
    raise SystemExit("Connect signer secret is unavailable")
url = "http://hive-connect-delivery.shizuha-hive.svc.cluster.local:8031/api/v1/internal/connect/messages/"

def send(payload, *, signed):
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    deadline = time.monotonic() + 120
    attempt = 0
    while True:
        attempt += 1
        headers = {
            "Content-Type": "application/json",
            "Connection": "close",
        }
        if signed:
            # Remint on every attempt so a delayed retry never reuses
            # an ageing HMAC timestamp.
            timestamp = str(int(time.time()))
            headers["X-Hive-Timestamp"] = timestamp
            headers["X-Hive-Signature-256"] = hmac.new(
                secret.encode(),
                timestamp.encode() + b"||" + body,
                hashlib.sha256,
            ).hexdigest()
        request = urllib.request.Request(
            f"{url}?release_probe={uuid.uuid4().hex}",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                detail = response.read().decode()
        except urllib.error.HTTPError as exc:
            status = exc.code
            with exc:
                detail = exc.read().decode()
        except (TimeoutError, urllib.error.URLError) as exc:
            status = 0
            detail = f"{type(exc).__name__}: {exc}"
        transient = (
            status == 0
            or status in {408, 425, 429}
            or 500 <= status <= 599
        )
        if not transient:
            return status, detail
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return status, detail
        delay = min(2 ** min(attempt, 3), remaining)
        print(
            "Connect denial probe transient "
            f"attempt={attempt} HTTP {status} {detail[:200]}; "
            f"retrying in {delay:.1f}s",
            file=sys.stderr,
        )
        time.sleep(delay)

payload = {
    **fixture,
    "conversation_id": str(uuid.uuid4()),
    "message_id": str(uuid.uuid4()),
    "sender_id": int(fixture["owner_id"]),
    "client_message_id": str(uuid.uuid4()),
    "content": "",
}
status, body = send(payload, signed=False)
if status != 401:
    raise SystemExit(f"unsigned delivery did not fail closed: HTTP {status} {body[:300]}")
print(f"unsigned delivery denied: HTTP {status}")

cross_owner = {**payload, "owner_id": str(int(payload["owner_id"]) + 1000000000)}
status, body = send(cross_owner, signed=True)
if status != 403 or "owner_mismatch" not in body:
    raise SystemExit(f"cross-owner delivery did not fail closed: HTTP {status} {body[:300]}")
print(f"cross-owner delivery denied: HTTP {status}")
'
fi
# Negative L4 probe from a non-Connect pod. Prove DNS and the ordinary
# 8030 Hive route from this exact pod first. Then require 8031 to be
# denied by timeout (28) or immediate connection refusal (7), the two
# observed CNI rejection modes. The positive Connect TCP control above
# proves rc=7 cannot be a missing listener or empty Service.
NEG_JOB="hive725-non-connect-${SHORT}"
.forgejo/scripts/kubectl -n shizuha-hive delete job "$NEG_JOB" --ignore-not-found --wait=true
cat <<YAML | .forgejo/scripts/kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${NEG_JOB}
  namespace: shizuha-hive
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 300
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: negative-probe
          image: curlimages/curl:8.10.1
          command: ["/bin/sh", "-ec"]
          args:
            - |
              set +e
              control="\$(curl -sS --connect-timeout 3 --max-time 5 -o /tmp/control-body -w '%{http_code}' \
                http://hive.shizuha-hive.svc.cluster.local:8030/api/health/ 2>/tmp/control-error)"
              control_rc=\$?
              set -e
              [ "\$control_rc" -eq 0 ] || {
                echo "8030 DNS/reachability control failed: curl rc=\$control_rc"
                cat /tmp/control-error
                exit 1
              }
              case "\$control" in 2*) ;; *) echo "8030 control failed: HTTP \$control"; exit 1;; esac
              set +e
              curl -sS --connect-timeout 3 --max-time 5 -o /tmp/body -w '%{http_code}' \
                http://hive-connect-delivery.shizuha-hive.svc.cluster.local:8031/api/v1/internal/connect/messages/ \
                >/tmp/status 2>/tmp/curl-error
              rc=\$?
              set -e
              if [ "\$rc" -eq 0 ]; then
                echo "network boundary failed open: HTTP \$(cat /tmp/status)"
                cat /tmp/body
                exit 1
              fi
              case "\$rc" in
                7|28) ;;
                *)
                  echo "expected policy denial (curl 7 or 28), got rc=\$rc"
                  cat /tmp/curl-error
                  exit 1
                  ;;
              esac
              echo "dns=ok ordinary_8030=\$control dedicated_8031=policy-denied(rc=\$rc)"
YAML
for _ in $(seq 1 30); do
  COMPLETE="$(.forgejo/scripts/kubectl -n shizuha-hive get job "$NEG_JOB" -o jsonpath='{.status.succeeded}')"
  FAILED="$(.forgejo/scripts/kubectl -n shizuha-hive get job "$NEG_JOB" -o jsonpath='{.status.failed}')"
  [ "$COMPLETE" = "1" ] && break
  if [ -n "$FAILED" ] && [ "$FAILED" != "0" ]; then
    .forgejo/scripts/kubectl -n shizuha-hive logs "job/${NEG_JOB}" --all-containers --tail=80 || true
    echo "::error::non-Connect boundary probe failed"
    false
  fi
  sleep 2
done
[ "${COMPLETE:-}" = "1" ] || { echo "::error::non-Connect probe did not complete"; exit 1; }
if ! .forgejo/scripts/kubectl -n shizuha-hive logs "job/${NEG_JOB}" --all-containers --tail=20; then
  echo "::warning::non-Connect probe completed; log fetch failed (do not treat as release regression)"
fi

# Persist the independently deployable stage boundary. Connect/deploy
# may now switch to 8031; finalization is a separate exact-SHA dispatch.
.forgejo/scripts/kubectl -n shizuha-hive annotate deploy/hive \
  "hive725-phase1-sha=${SHA}" --overwrite
if [ "$H725_MODE" != phase1 ]; then
  : >/tmp/h725-phase1-proven
  .forgejo/scripts/kubectl -n shizuha-hive set env deploy/hive --containers=hive \
    HIVE_CONNECT_ENFORCE_DEDICATED_LISTENER=true
  # Two replicas roll serially and each must remain Ready for
  # minReadySeconds=180, so the final listener-only rollout needs the
  # same bounded slow-node allowance as the image rollout above.
  .forgejo/scripts/kubectl -n shizuha-hive rollout status deploy/hive --timeout=1800s
  # rollout status can turn green while the old phase-one pod is
  # terminating and still present in the Service EndpointSlice.  A
  # one-shot legacy denial probe can then hit that old, deliberately
  # non-enforcing pod and falsely roll back a healthy cutover.  Require
  # the Service target set to equal the exact Ready enforcing pods.
  # Ring-aware window (HIVE-2579 fence-build follow-up): the fleet leg rolls
  # 2 pods, and under node pressure their pod-start P99 does not fit the
  # canary-sized 180s — the fence build starved (39 polls x ~4.6s = 100% of
  # the window, generic timeout). 360s = canary 180 + 2-pod fleet headroom.
  python3 .forgejo/scripts/wait-hive-connect-cutover-convergence.py \
    --namespace shizuha-hive \
    --deployment hive \
    --service hive \
    --timeout 360 \
    --interval 2
  printf '%s' "$H725_FIXTURE" | .forgejo/scripts/kubectl -n shizuha exec -i "$CONNECT_POD" -- python -c 'if True:
  import json, sys, time, urllib.error, urllib.request, uuid
  fixture = json.load(sys.stdin)
  payload = {
      **fixture,
      "conversation_id": str(uuid.uuid4()),
      "message_id": str(uuid.uuid4()),
      "sender_id": int(fixture["owner_id"]),
      "client_message_id": str(uuid.uuid4()),
      "content": "",
  }
  body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
  base_url = (
      "http://hive.shizuha-hive.svc.cluster.local:8030"
      "/api/v1/internal/connect/messages/"
  )
  # EndpointSlice convergence is the control-plane boundary; kube-proxy
  # can retain a retiring backend in its data plane for longer.  Use
  # fresh, explicitly closed connections and require a sustained run
  # of denials before committing the cutover.  This remains bounded
  # and fail-closed if the stale route does not drain.
  deadline = time.monotonic() + 180
  required_consecutive = 6
  consecutive = 0
  attempt = 0
  while time.monotonic() < deadline:
      attempt += 1
      request = urllib.request.Request(
          f"{base_url}?cutover_probe={uuid.uuid4().hex}",
          data=body,
          headers={
              "Content-Type": "application/json",
              "Connection": "close",
          },
          method="POST",
      )
      try:
          with urllib.request.urlopen(request, timeout=10) as response:
              status = response.status
              detail = response.read(300).decode(errors="replace")
      except urllib.error.HTTPError as exc:
          status = exc.code
          with exc:
              detail = exc.read(300).decode(errors="replace")
      except (TimeoutError, urllib.error.URLError) as exc:
          status = 0
          detail = f"{type(exc).__name__}: {exc}"
      if status == 404:
          consecutive += 1
          print(
              "legacy 8030 data-plane denial observed: HTTP 404 "
              f"attempt={attempt} consecutive={consecutive}/"
              f"{required_consecutive}"
          )
          if consecutive >= required_consecutive:
              print(
                  "legacy 8030 delivery denied: HTTP 404 "
                  f"for {required_consecutive} consecutive fresh "
                  "connections"
              )
              break
      else:
          consecutive = 0
      remaining = deadline - time.monotonic()
      if remaining <= 0:
          raise SystemExit(
              "legacy 8030 delivery denial did not converge: "
              f"HTTP {status} {detail}; "
              f"consecutive={consecutive}/{required_consecutive}"
          )
      delay = min(2, remaining)
      if status != 404:
          print(
              "legacy 8030 delivery denial not yet converged "
              f"attempt={attempt}: HTTP {status} {detail}; "
              f"retrying in {delay:.1f}s",
              file=sys.stderr,
          )
      time.sleep(delay)
  else:
      raise SystemExit(
          "legacy 8030 delivery denial timed out after 180s: "
          f"consecutive={consecutive}/{required_consecutive}"
      )
  '
  .forgejo/scripts/kubectl -n shizuha-hive annotate deploy/hive \
    "hive725-cutover-complete-sha=${SHA}" --overwrite
else
  echo "HIVE-725 phase 1 complete; merge/deploy Connect signer+URL, then exact-SHA finalize"
fi
echo "Installing HIVE-583/HIVE-585 Codex refresh alerting"
cat <<'YAML' | .forgejo/scripts/kubectl apply -f -
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: hive-codex-refresh
  namespace: shizuha-hive
  labels:
    app.kubernetes.io/name: hive
    release: kube-prometheus-stack
spec:
  groups:
    - name: hive-codex-refresh
      rules:
        - alert: HiveCodexRefreshToken401
          expr: increase(hive_codex_refresh_401_total[5m]) > 0
          for: 0m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive Codex refresh token revoked ({{ $labels.account_email }})"
            description: >-
              Hive saw an OpenAI/Codex OAuth refresh HTTP 401 for account
              {{ $labels.account_email }} (entry {{ $labels.label }}) in
              the last 5m. Agents leasing this account will fail over or
              wedge until the account is re-authenticated. Re-auth:
              https://shizuha.com/hive/credentials/codex/re-auth?email={{ $labels.account_email_urlencoded }}
        - alert: HiveCodexRefreshLoopFailures
          expr: increase(hive_codex_refresh_total{outcome="failed"}[10m]) > 0
          for: 0m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive Codex refresh loop failed"
            description: >-
              The HIVE-583 codex-refresh-loop recorded one or more failed
              OpenAI/Codex OAuth refresh attempts in the last 10m. Check
              the codex-refresh sidecar logs on deploy/hive, verify token
              rows in Hive, and use the re-auth flow for revoked accounts.
              Pulse: https://pulse.shizuha?task=HIVE-583
        - alert: HiveCodexRefreshMetricsAbsent
          expr: absent(hive_codex_refresh_metrics_present)
          for: 30m
          labels:
            severity: info
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive Codex refresh metrics absent"
            description: >-
              hive_codex_refresh_metrics_present has not been scraped for
              30m — the HIVE-585 revoked-token fail-loud exporter is
              missing. Verify /hive/api/v1/internal/codex-refresh/metrics
              is reachable and scraped.
YAML
.forgejo/scripts/kubectl -n shizuha-hive get prometheusrule hive-codex-refresh

echo "Installing HIVE-803 RuntimeLane outbox fail-loud alerting"
RUNTIME_LANE_ALERTS=k8s/hive-runtime-lane-alerts.yaml
.forgejo/scripts/kubectl apply --dry-run=server -f "$RUNTIME_LANE_ALERTS" >/dev/null
.forgejo/scripts/kubectl apply -f "$RUNTIME_LANE_ALERTS"
.forgejo/scripts/kubectl -n shizuha-hive get prometheusrule hive-runtime-lane

echo "Installing PLAT-4683 native pre-wake fail-loud alerting"
RUNTIME_CONTRACT_ALERTS=k8s/hive-runtime-contract-alerts.yaml
.forgejo/scripts/kubectl apply --dry-run=server -f "$RUNTIME_CONTRACT_ALERTS" >/dev/null
.forgejo/scripts/kubectl apply -f "$RUNTIME_CONTRACT_ALERTS"
.forgejo/scripts/kubectl -n shizuha-hive get prometheusrule hive-runtime-contract

echo "Installing PLAT-4482 usage-outbox fail-loud alerting"
cat <<'YAML' | .forgejo/scripts/kubectl apply -f -
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: hive-usage-outbox
  namespace: shizuha-hive
  labels:
    app.kubernetes.io/name: hive
    release: kube-prometheus-stack
spec:
  groups:
    - name: hive-usage-outbox
      rules:
        - alert: HiveUsageOutboxIdempotencyConflict
          expr: sum(increase(hive_usage_outbox_delivery_total{outcome="conflict"}[15m])) by (source) > 0
          for: 0m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive usage outbox idempotency conflict (source={{ $labels.source }})"
            description: >-
              Metering Core rejected a producer outbox event because its
              immutable CloudEvents source/id was replayed with a different
              payload. Inspect the conflict row and notify the producer plus
              Metering owners. Pulse: https://pulse.shizuha?task=PLAT-4482
        - alert: HiveUsageOutboxDeliveryRetrying
          expr: sum(increase(hive_usage_outbox_delivery_total{outcome="retry"}[15m])) by (source) > 2
          for: 0m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive usage outbox delivery repeatedly failing (source={{ $labels.source }})"
            description: >-
              More than two Metering Core outbox deliveries failed for this
              source in 15m. Rows remain durable, but billing ingestion is
              delayed. Inspect the dispatcher Job and UsageEventOutbox.last_error.
        - alert: HiveUsageOutboxDispatcherStale
          expr: hive_usage_outbox_last_dispatch_timestamp_seconds == 0 or (time() - hive_usage_outbox_last_dispatch_timestamp_seconds > 300)
          for: 10m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive usage outbox dispatcher has stopped"
            description: >-
              No completed usage-outbox sweep has been observed for more
              than five minutes. Verify CronJob hive-usage-outbox-dispatcher
              and its recent Jobs; pending billable events are not reaching
              Metering Core until dispatch resumes.
YAML
.forgejo/scripts/kubectl -n shizuha-hive get prometheusrule hive-usage-outbox

.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o json > /tmp/hive-deploy-post-rollout.json

echo "Installing scheduled PLAT-4482 usage-outbox dispatcher CronJob"
python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > /tmp/hive-usage-outbox-dispatcher-cronjob.json
import copy
import json
import sys

deploy = json.load(open(sys.argv[1]))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    c for c in pod_spec.get("containers", [])
    if c.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["name"] = "dispatcher"
container["command"] = [
    "python", "manage.py", "dispatch_usage_outbox", "--limit", "1000",
]
container.pop("args", None)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe",
    "lifecycle",
):
    container.pop(field, None)

job_pod_spec = {
    "restartPolicy": "Never",
    "containers": [container],
}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes",
    "imagePullSecrets", "serviceAccount", "serviceAccountName",
    "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

labels = {
    "app": "hive",
    "app.kubernetes.io/name": "hive",
    "app.kubernetes.io/component": "usage-outbox-dispatcher",
    "shizuha.io/task": "PLAT-4482",
}
cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {
        "name": "hive-usage-outbox-dispatcher",
        "namespace": "shizuha-hive",
        "labels": labels,
    },
    "spec": {
        "schedule": "* * * * *",
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 3,
        # PLAT-6295: retain more failed jobs + finished pods so a KubeJobFailed
        # can be diagnosed from retained logs (previously cleaned up before the
        # failing step could be read).
        "failedJobsHistoryLimit": 10,
        "jobTemplate": {
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 120,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": job_pod_spec,
                },
            },
        },
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/hive-usage-outbox-dispatcher-cronjob.json
OUTBOX_DISPATCHER_IMAGE="$(.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-usage-outbox-dispatcher -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[?(@.name=="dispatcher")].image}')"
echo "Usage outbox dispatcher image: ${OUTBOX_DISPATCHER_IMAGE}"
echo "${OUTBOX_DISPATCHER_IMAGE}" | grep -q "${TAG}" || { echo "::error::usage outbox dispatcher image mismatch"; exit 1; }
.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-usage-outbox-dispatcher -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[?(@.name=="dispatcher")].command}' | grep -q dispatch_usage_outbox || { echo "::error::usage outbox dispatcher command missing"; exit 1; }

echo "Installing scheduled Hive identity reconciliation CronJob"
python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > /tmp/hive-identity-reconciler-cronjob.json
import copy
import json
import sys

deploy = json.load(open(sys.argv[1]))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    c for c in pod_spec.get("containers", [])
    if c.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["name"] = "reconciler"
# Retire archived and stale unprovisioned identity orphans through
# Hive's durable tombstone path; never physically delete desired state
# before the daemon actuator has converged.
container["command"] = [
    "python", "manage.py", "reconcile_agent_identities",
    "--reap-archived-tombstones", "--reap-stale-orphans",
]
container.pop("args", None)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe",
    "lifecycle",
):
    container.pop(field, None)

job_pod_spec = {
    "restartPolicy": "Never",
    "containers": [container],
}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes",
    "imagePullSecrets", "serviceAccount", "serviceAccountName",
    "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

labels = {
    "app": "hive",
    "app.kubernetes.io/name": "hive",
    "app.kubernetes.io/component": "identity-reconciler",
    "shizuha.io/task": "HIVE-599",
}
cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {
        "name": "hive-identity-reconciler",
        "namespace": "shizuha-hive",
        "labels": labels,
    },
    "spec": {
        "schedule": "17 * * * *",
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 3,
        "failedJobsHistoryLimit": 10,
        "jobTemplate": {
            "spec": {
                "backoffLimit": 1,
                "activeDeadlineSeconds": 300,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": job_pod_spec,
                },
            },
        },
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/hive-identity-reconciler-cronjob.json

echo "Installing Hive compute Hane billing CronJob"
python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > /tmp/hive-compute-billing-cronjob.json
import copy
import json
import sys

deploy = json.load(open(sys.argv[1]))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    c for c in pod_spec.get("containers", [])
    if c.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["name"] = "biller"
container["command"] = [
    "python", "manage.py", "bill_fleet_compute",
    "--hours", "1",
]
container.pop("args", None)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe",
    "lifecycle",
):
    container.pop(field, None)

job_pod_spec = {
    "restartPolicy": "Never",
    "containers": [container],
}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes",
    "imagePullSecrets", "serviceAccount", "serviceAccountName",
    "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

labels = {
    "app": "hive",
    "app.kubernetes.io/name": "hive",
    "app.kubernetes.io/component": "compute-billing",
}
cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {
        "name": "hive-compute-billing",
        "namespace": "shizuha-hive",
        "labels": labels,
    },
    "spec": {
        "schedule": "7 * * * *",
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 3,
        "failedJobsHistoryLimit": 3,
        "jobTemplate": {
            "spec": {
                "backoffLimit": 1,
                "activeDeadlineSeconds": 300,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": job_pod_spec,
                },
            },
        },
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/hive-compute-billing-cronjob.json
IDENTITY_RECONCILER_IMAGE="$(.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-identity-reconciler -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[?(@.name=="reconciler")].image}')"
echo "Identity reconciler image: ${IDENTITY_RECONCILER_IMAGE}"
echo "${IDENTITY_RECONCILER_IMAGE}" | grep -q "${TAG}" || { echo "::error::identity reconciler image mismatch"; exit 1; }
.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-identity-reconciler -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[?(@.name=="reconciler")].command}' | grep -q reconcile_agent_identities || { echo "::error::identity reconciler command missing"; exit 1; }
IDENTITY_RECONCILE_JOB="hive-identity-reconciler-deploy-${SHORT}-$(date +%s)"
.forgejo/scripts/kubectl -n shizuha-hive create job \
  --from=cronjob/hive-identity-reconciler \
  "${IDENTITY_RECONCILE_JOB}"
if ! .forgejo/scripts/kubectl -n shizuha-hive wait \
  --for=condition=complete \
  "job/${IDENTITY_RECONCILE_JOB}" \
  --timeout=360s; then
  .forgejo/scripts/kubectl -n shizuha-hive logs "job/${IDENTITY_RECONCILE_JOB}" --all-containers=true || true
  exit 1
fi
# Job complete is the gate. kubectl logs of the completed Job often lands on
# i9-ws (kubelet 502) and must not roll a healthy fleet (0fdb08f HIVE-725
# passed, then this log fetch red-lit R2).
if ! .forgejo/scripts/kubectl -n shizuha-hive logs "job/${IDENTITY_RECONCILE_JOB}" --all-containers=true; then
  echo "::warning::identity reconciler completed; log fetch failed (do not treat as release regression)"
fi

echo "Installing fleet-agent liveness reconciliation CronJob"
# Render the controller from the exact rolled Deployment on every release. The
# legacy static CronJob stayed pinned to an old image, so changes to its script
# could pass CI while never reaching production. Embed the reviewed ConfigMap
# script in the Job command: the ring executor does not need broad ConfigMap
# mutation authority, and the CronJob has one immutable release image/source.
python3 - /tmp/hive-deploy-post-rollout.json k8s/fleet-agent-liveness-reconciler.yaml <<'PY' > /tmp/fleet-agent-liveness-reconciler-cronjob.json
import copy
import json
import sys

import yaml

deploy = json.load(open(sys.argv[1]))
documents = list(yaml.safe_load_all(open(sys.argv[2])))
config_map = next(
    document for document in documents
    if document and document.get("kind") == "ConfigMap"
)
script = config_map["data"]["reconcile_liveness.py"]
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    container for container in pod_spec.get("containers", [])
    if container.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["name"] = "reconciler"
container["command"] = [
    "sh", "-c", "python manage.py shell <<'PY'\n" + script + "\nPY",
]
container.pop("args", None)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe", "lifecycle",
):
    container.pop(field, None)
env = [
    item for item in container.get("env", [])
    if item.get("name") not in {"WEDGE_SECONDS", "COOLDOWN_SECONDS", "LOG_LOOKBACK"}
]
env.extend([
    {"name": "WEDGE_SECONDS", "value": "2100"},
    {"name": "COOLDOWN_SECONDS", "value": "3600"},
    {"name": "LOG_LOOKBACK", "value": "3600"},
])
container["env"] = env

job_pod_spec = {"restartPolicy": "Never", "containers": [container]}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes", "imagePullSecrets",
    "serviceAccount", "serviceAccountName", "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

labels = {
    "app": "hive",
    "app.kubernetes.io/name": "hive",
    "app.kubernetes.io/component": "liveness-reconciler",
}
cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {
        "name": "fleet-agent-liveness-reconciler",
        "namespace": "shizuha-hive",
        "labels": labels,
    },
    "spec": {
        "schedule": "*/20 * * * *",
        # Ring recovery suspends mutating controllers while it holds the
        # generation fence.  `kubectl apply` preserves that externally-owned
        # scalar when the desired manifest omits it, leaving this reconciler
        # silently disabled after an otherwise successful release.
        "suspend": False,
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 3,
        "failedJobsHistoryLimit": 10,
        "jobTemplate": {
            "spec": {
                "activeDeadlineSeconds": 240,
                "backoffLimit": 0,
                # PLAT-6295: retain finished pods so a failed run can be diagnosed
                # from retained logs (matches the other hive reconciler CronJobs).
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": job_pod_spec,
                },
            },
        },
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/fleet-agent-liveness-reconciler-cronjob.json
LIVE_HIVE_IMAGE="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o jsonpath='{.spec.template.spec.containers[0].image}')"
LIVENESS_CRON_IMAGE="$(.forgejo/scripts/kubectl -n shizuha-hive get cronjob fleet-agent-liveness-reconciler -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}')"
[ "$LIVENESS_CRON_IMAGE" = "$LIVE_HIVE_IMAGE" ] || {
  echo "::error::fleet-agent liveness image drift: cron=${LIVENESS_CRON_IMAGE} live=${LIVE_HIVE_IMAGE}"
  exit 1
}
.forgejo/scripts/kubectl -n shizuha-hive get cronjob fleet-agent-liveness-reconciler \
  -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].command}' |
  grep -q ACTIVITY_UPDATE_ERROR || {
    echo "::error::fleet-agent liveness activity update guard missing"
    exit 1
  }
LIVENESS_CRON_SUSPENDED="$(.forgejo/scripts/kubectl -n shizuha-hive get cronjob fleet-agent-liveness-reconciler -o jsonpath='{.spec.suspend}')"
[ "$LIVENESS_CRON_SUSPENDED" = "false" ] || {
  echo "::error::fleet-agent liveness reconciler remained suspended after apply"
  exit 1
}

echo "Installing scheduled claw-isolation reconciliation CronJob"
# NB: the deploy JSON must come in via argv, NOT '< file' — combining a
# <<'PY' heredoc with a second stdin redirection makes the file win,
# so python executes the JSON (a valid dict literal, silent no-op)
# instead of this script and emits an empty manifest.
python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > /tmp/hive-claw-isolation-reconcile-cronjob.json
import copy
import json
import sys

deploy = json.load(open(sys.argv[1]))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    c for c in pod_spec.get("containers", [])
    if c.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["name"] = "reconcile"
# Finalize deletions FIRST (promote expired undo windows + destroy
# reclaim-ready claws) so the isolation audit doesn't re-apply policies
# to namespaces that are about to be torn down. Run every reconciler,
# but retain a failure exit code if any one fails so the Job controller
# retries the whole idempotent chain after a transient database/CNI
# outage. A 2026-07-26 transient database interruption otherwise left
# all three mechanisms pending until the next 30-minute schedule.
container["command"] = [
    "sh", "-c",
    "status=0; "
    "python manage.py finalize_claw_deletions || status=$?; "
    "python manage.py reconcile_claw_availability || status=$?; "
    "python manage.py reconcile_claw_isolation || status=$?; "
    "exit \"$status\"",
]
container.pop("args", None)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe",
    "lifecycle",
):
    container.pop(field, None)

job_pod_spec = {
    "restartPolicy": "Never",
    "containers": [container],
}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes",
    "imagePullSecrets", "serviceAccount", "serviceAccountName",
    "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {
        "name": "hive-claw-isolation-reconcile",
        "namespace": "shizuha-hive",
        "labels": {
            "app": "hive-claw-isolation-reconcile",
            "app.kubernetes.io/name": "hive",
            "app.kubernetes.io/component": "claw-isolation-reconcile",
            "shizuha.io/task": "HIVE-429",
        },
    },
    "spec": {
        "schedule": "*/30 * * * *",
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 1,
        "failedJobsHistoryLimit": 10,
        "jobTemplate": {
            "spec": {
                # Initial attempt plus six exponential-backoff retries
                # covers a bounded dependency failover without waiting
                # for the next 30-minute CronJob tick.
                "backoffLimit": 6,
                "activeDeadlineSeconds": 1200,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "hive-claw-isolation-reconcile",
                            "app.kubernetes.io/name": "hive",
                            "app.kubernetes.io/component": "claw-isolation-reconcile",
                            "shizuha.io/task": "HIVE-429",
                        },
                    },
                    "spec": job_pod_spec,
                },
            },
        },
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/hive-claw-isolation-reconcile-cronjob.json

echo "Installing fleet harness-drift roll CronJob"
python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > /tmp/fleet-harness-drift-roll-cronjob.json
import copy
import json
import sys

deploy = json.load(open(sys.argv[1]))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(container for container in pod_spec["containers"] if container["name"] == "hive")
native = next((item for item in base_container.get("env", []) if item.get("name") == "HIVE_NATIVE_RUNTIME_RELEASE_ENABLED"), {})
if native.get("value") != "true":
    raise SystemExit("qualified Hive template does not enable native runtime authority")
container = copy.deepcopy(base_container)
container["name"] = "roll"
container["command"] = ["python", "manage.py", "roll_harness_drift", "--max-seats", "6"]
container["args"] = []
for field in ("ports", "livenessProbe", "readinessProbe", "startupProbe", "lifecycle"):
    container.pop(field, None)
job_pod_spec = {"restartPolicy": "Never", "containers": [container]}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes", "imagePullSecrets",
    "serviceAccount", "serviceAccountName", "securityContext", "runtimeClassName",
):
    if field in pod_spec:
        job_pod_spec[field] = copy.deepcopy(pod_spec[field])
labels = {
    "app": "hive",
    "app.kubernetes.io/name": "hive",
    "app.kubernetes.io/component": "harness-drift-roll",
    "app.kubernetes.io/part-of": "shizuha-platform",
}
cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {"name": "fleet-harness-drift-roll", "namespace": "shizuha-hive", "labels": labels},
    "spec": {
        "schedule": "*/15 * * * *",
        "startingDeadlineSeconds": 900,
        "suspend": False,
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 1,
        "failedJobsHistoryLimit": 3,
        "jobTemplate": {"spec": {
            "backoffLimit": 1,
            "ttlSecondsAfterFinished": 3600,
            "template": {"metadata": {"labels": labels}, "spec": job_pod_spec},
        }},
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/fleet-harness-drift-roll-cronjob.json
.forgejo/scripts/kubectl -n shizuha-hive get cronjob fleet-harness-drift-roll -o json > /tmp/fleet-harness-drift-roll-observed.json
python3 - /tmp/fleet-harness-drift-roll-cronjob.json /tmp/fleet-harness-drift-roll-observed.json <<'PY'
import json
import sys

expected, observed = (json.load(open(path)) for path in sys.argv[1:])
def require_owned_fields(desired, actual, path=""):
    if isinstance(desired, dict):
        if not isinstance(actual, dict):
            raise SystemExit("harness-drift roll shape mismatch at " + path)
        for key, value in desired.items():
            require_owned_fields(value, actual.get(key, [] if value == [] else None), path + "/" + key)
    elif isinstance(desired, list):
        if not isinstance(actual, list) or len(desired) != len(actual):
            raise SystemExit("harness-drift roll list mismatch at " + path)
        for position, (wanted, found) in enumerate(zip(desired, actual)):
            require_owned_fields(wanted, found, path + "/" + str(position))
    elif desired != actual:
        raise SystemExit("harness-drift roll value mismatch at " + path)
require_owned_fields(expected, observed)
container = observed["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
if any(field in container for field in ("ports", "livenessProbe", "readinessProbe", "startupProbe", "lifecycle")):
    raise SystemExit("harness-drift roll retained web-server-only fields")
print("harness-drift roll release template verified")
PY

echo "Installing harness auto-upgrade CronJob"
# The updater is part of the Hive release, not a hand-pinned workload.
# Render from the just-rolled Deployment on every fleet release so its
# command, dependencies, secrets, and bug fixes cannot lag for weeks.
python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > /tmp/hive-harness-autoupgrade-cronjob.json
import copy
import json
import sys

deploy = json.load(open(sys.argv[1]))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    c for c in pod_spec.get("containers", [])
    if c.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["name"] = "autoupgrade"
container["command"] = ["python", "manage.py", "harness_autoupgrade"]
container.pop("args", None)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe",
    "lifecycle",
):
    container.pop(field, None)

job_pod_spec = {
    "restartPolicy": "Never",
    "containers": [container],
}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes",
    "imagePullSecrets", "serviceAccount", "serviceAccountName",
    "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {
        "name": "hive-harness-autoupgrade",
        "namespace": "shizuha-hive",
        "labels": {
            "app": "hive",
            "app.kubernetes.io/name": "hive",
            "app.kubernetes.io/component": "harness-autoupgrade",
        },
    },
    "spec": {
        "suspend": False,
        # Native agents hold a target-fenced drain at their next persisted
        # boundary. Recheck that ready fence every minute; the management
        # command separately cadence-gates external version discovery to 15m.
        "schedule": "* * * * *",
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 1,
        "failedJobsHistoryLimit": 10,
        "jobTemplate": {
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "hive",
                            "app.kubernetes.io/name": "hive",
                            "app.kubernetes.io/component": "harness-autoupgrade",
                        },
                    },
                    "spec": job_pod_spec,
                },
            },
        },
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/hive-harness-autoupgrade-cronjob.json
LIVE_HIVE_IMAGE="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o jsonpath='{.spec.template.spec.containers[0].image}')"
HARNESS_CRON_IMAGE="$(.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-harness-autoupgrade -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}')"
[ "$HARNESS_CRON_IMAGE" = "$LIVE_HIVE_IMAGE" ] || {
  echo "::error::harness autoupgrade image drift: cron=${HARNESS_CRON_IMAGE} live=${LIVE_HIVE_IMAGE}"
  exit 1
}

# HIVE-628: the on-demand reconciler cron (10-min unread-wake +
# workload start/stop sweep) was installed from a static manifest and
# sat pinned to a stale image for weeks — align it to every fleet
# release like the other reconciler crons.
# Track the LIVE hive Deployment image (the actual current release),
# not IDENTITY_RECONCILER_IMAGE (a proxy that lags a ring). The
# on-demand sweep running a stale image kept idle agents alive because
# its old logic counted blocked/scheduled work as 'keep warm'
# (operator 2026-07-11, HIVE-651).
# Do not `kubectl set image cronjob/...`. The fenced set-image compiler
# only walks Deployment `spec.template.spec.containers`; CronJob
# containers live under `jobTemplate` (`set image named unknown
# container(s): ['reconciler']` — fleet 4c47765 2026-09-06 after the
# 422 retry unblocked the HIVE-725 cutover). The apply below re-renders
# the CronJob from the live hive Deployment, which is the image SoT.
# Resolve the live hive container by name (not [0]) so a sidecar cannot
# silently pin the reconciler to the wrong image.
LIVE_HIVE_IMAGE="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o jsonpath='{.spec.template.spec.containers[?(@.name=="hive")].image}')"
echo "hive-on-demand-reconciler apply will use live hive image ${LIVE_HIVE_IMAGE}"

# PLAT-7725: pin the rendered CronJob by IMMUTABLE DIGEST, never the mutable
# tag. The 09-03→09-04 rollback churn re-tagged the fleet image and the
# previously-rendered `hive-f12447b` tag was overwritten in the registry, so
# the CronJob sat dark in ImagePullBackOff — every 10m tick added another
# failed pod. A digest pin survives any re-tag/overwrite.
case "${LIVE_HIVE_IMAGE}" in
  *@sha256:*)
    # Already digest-pinned upstream; keep as-is.
    ON_DEMAND_IMAGE="${LIVE_HIVE_IMAGE}"
    ;;
  *:*)
    # Strip at the LAST colon: keeps the registry port in the repo ref.
    LIVE_HIVE_REPO="${LIVE_HIVE_IMAGE%:*}"
    LIVE_HIVE_TAG="${LIVE_HIVE_IMAGE##*:}"
    ON_DEMAND_DIGEST="$(curl -fsS -D - -o /dev/null --max-time 20 \
      -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
      "http://registry.registry.svc.cluster.local:5000/v2/shizuha-hive/manifests/${LIVE_HIVE_TAG}" \
      | awk 'tolower($1)=="docker-content-digest:"{gsub(/\r/,"",$2); print $2}' | head -n1)"
    [[ "${ON_DEMAND_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]] || {
      echo "::error::could not resolve digest for live hive image ${LIVE_HIVE_IMAGE} (got '${ON_DEMAND_DIGEST:-<empty>}'); refusing to render the on-demand reconciler against a mutable tag (PLAT-7725)"
      exit 1
    }
    ON_DEMAND_IMAGE="${LIVE_HIVE_REPO}@${ON_DEMAND_DIGEST}"
    echo "hive-on-demand-reconciler pinned to digest ${ON_DEMAND_DIGEST}"
    ;;
esac
export ON_DEMAND_IMAGE
.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-claw-isolation-reconcile

echo "Installing on-demand fleet reconciler CronJob"
# Activated 2026-07-11 after the k8s/hive-on-demand-reconciler.yaml
# gates were met (command in image incl. archived-agent exclusion,
# HIVE_PULSE_API_TOKEN present, dry-run reviewed). CI owns the live
# CronJob from here: rendered from the deployed pod spec each deploy
# so the image never drifts stale. Runs with --apply.
python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > /tmp/hive-on-demand-reconciler-cronjob.json
import copy
import json
import os
import sys

deploy = json.load(open(sys.argv[1]))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    c for c in pod_spec.get("containers", [])
    if c.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["name"] = "reconciler"
# PLAT-7725: pin by immutable digest resolved from the live image tag —
# a mutable tag pin orphans the CronJob when the tag is re-pushed.
container["image"] = os.environ["ON_DEMAND_IMAGE"]
container["command"] = [
    "python", "manage.py", "reconcile_on_demand_agents", "--apply",
    "--reconcile-org-memberships", "--reconcile-capabilities",
]
container.pop("args", None)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe",
    "lifecycle",
):
    container.pop(field, None)

job_pod_spec = {
    "restartPolicy": "Never",
    "containers": [container],
}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes",
    "imagePullSecrets", "serviceAccount", "serviceAccountName",
    "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {
        "name": "hive-on-demand-reconciler",
        "namespace": "shizuha-hive",
        "labels": {
            "app": "hive",
            "app.kubernetes.io/name": "hive",
            "app.kubernetes.io/component": "on-demand-reconciler",
        },
    },
    "spec": {
        "suspend": False,
        "schedule": "*/10 * * * *",
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 1,
        "failedJobsHistoryLimit": 10,
        "jobTemplate": {
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "hive",
                            "app.kubernetes.io/name": "hive",
                            "app.kubernetes.io/component": "on-demand-reconciler",
                        },
                    },
                    "spec": job_pod_spec,
                },
            },
        },
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/hive-on-demand-reconciler-cronjob.json
ON_DEMAND_SUSPENDED="$(.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-on-demand-reconciler -o jsonpath='{.spec.suspend}')"
[ "$ON_DEMAND_SUSPENDED" = "false" ] || {
  echo "::error::hive-on-demand-reconciler remained suspended after apply"
  exit 1
}
.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-on-demand-reconciler

# PLAT-4490 fail-loud path: the reconciler exits non-zero when the
# Connect lifecycle snapshot is unavailable, so no unproven stop is
# applied. These alerts DM the current Engineering on-call through
# the standard owner_group resolver instead of relying on pod logs.
cat <<'YAML' | .forgejo/scripts/kubectl apply -f -
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: hive-on-demand-reconcile
  namespace: monitoring
  labels:
    app.kubernetes.io/name: hive
    app.kubernetes.io/component: on-demand-reconciler
    release: kube-prometheus-stack
    shizuha.io/task: PLAT-4490
spec:
  groups:
    - name: hive-on-demand-reconcile
      rules:
        - alert: HiveOnDemandReconcileFailed
          expr: kube_job_status_failed{namespace="shizuha-hive", job_name=~"hive-on-demand-reconciler-.*"} > 0
          for: 0m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive on-demand lifecycle reconcile failed"
            description: >-
              The agent lifecycle sweep had a failed/partial outcome.
              Safe workload-driven starts may have been applied, but
              no unproven hibernation action was applied; inspect
              `.forgejo/scripts/kubectl logs -n shizuha-hive job/{{ $labels.job_name }}`
              and restore the Connect lifecycle snapshot contract.
            runbook_url: "https://wiki.shizuha.com/501362a3-4633-4218-8ccf-b0f4e15f37ce"
        - alert: HiveOnDemandReconcileCronJobMissing
          expr: absent(kube_cronjob_info{namespace="shizuha-hive", cronjob="hive-on-demand-reconciler"})
          for: 15m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive on-demand lifecycle CronJob is missing"
            description: >-
              The scheduled workload/DM wake and safe-hibernation
              sweep is absent. Restore it through the Hive deploy
              workflow; do not hand-apply the bootstrap manifest.
            runbook_url: "https://wiki.shizuha.com/501362a3-4633-4218-8ccf-b0f4e15f37ce"
        - alert: HiveOnDemandReconcileCronJobSuspended
          expr: kube_cronjob_spec_suspend{namespace="shizuha-hive", cronjob="hive-on-demand-reconciler"} == 1
          for: 5m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive on-demand lifecycle CronJob is suspended"
            description: >-
              The workload/DM wake controller exists but is disabled,
              so stopped team seats cannot wake for actionable work.
              Restore it through the Hive deploy workflow and inspect
              the actor that changed spec.suspend before closing.
            runbook_url: "https://wiki.shizuha.com/501362a3-4633-4218-8ccf-b0f4e15f37ce"
        - alert: HiveOnDemandReconcileStale
          expr: (time() - kube_cronjob_status_last_successful_time{namespace="shizuha-hive", cronjob="hive-on-demand-reconciler"}) > 1500 and on(namespace, cronjob) kube_cronjob_spec_suspend{namespace="shizuha-hive", cronjob="hive-on-demand-reconciler"} == 0
          for: 5m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive on-demand lifecycle reconcile is stale"
            description: >-
              The unsuspended 10-minute controller has not completed
              successfully for more than 25 minutes. Inspect CronJob,
              Job, and controller state; team wake guarantees are stale.
            runbook_url: "https://wiki.shizuha.com/501362a3-4633-4218-8ccf-b0f4e15f37ce"
YAML
echo "Installing github-identity reconciler CronJob (PLAT-4238, identity-pointer model)"
# Team-based GitHub identity: keeps each agent's NON-SECRET env
# GITHUB_IDENTITY pointer equal to its team identity (write teams ->
# kai2574, everyone else -> sara2574). The daemon render turns the
# pointer into a secretKeyRef to the shared <identity>-github-token
# Secret (cli 867add57) — durable, no token bytes in config, no race
# with the daemon (unlike the retired agent-creds patcher). CI-owned,
# rendered from the deployed pod spec so the image never drifts.
python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > /tmp/hive-github-identity-reconciler-cronjob.json
import copy
import json
import sys

deploy = json.load(open(sys.argv[1]))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    c for c in pod_spec.get("containers", [])
    if c.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["name"] = "reconciler"
container["command"] = [
    "python", "manage.py", "reconcile_agent_github_identity", "--apply",
]
container.pop("args", None)
# ORIG-18 cutover: Origin (Forgejo) is primary source control; every
# agent's GITHUB_IDENTITY resolves to the read-only PAT regardless of
# write-team membership. Remove only if GitHub ever becomes writable
# for agents again.
container.setdefault("env", []).append(
    {"name": "HIVE_GITHUB_READONLY_CUTOVER", "value": "1"}
)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe",
    "lifecycle",
):
    container.pop(field, None)

job_pod_spec = {
    "restartPolicy": "Never",
    "containers": [container],
}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes",
    "imagePullSecrets", "serviceAccount", "serviceAccountName",
    "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {
        "name": "hive-github-identity-reconciler",
        "namespace": "shizuha-hive",
        "labels": {
            "app": "hive",
            "app.kubernetes.io/name": "hive",
            "app.kubernetes.io/component": "github-identity-reconciler",
        },
    },
    "spec": {
        "suspend": False,
        "schedule": "*/30 * * * *",
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 1,
        "failedJobsHistoryLimit": 10,
        "jobTemplate": {
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "hive",
                            "app.kubernetes.io/name": "hive",
                            "app.kubernetes.io/component": "github-identity-reconciler",
                        },
                    },
                    "spec": job_pod_spec,
                },
            },
        },
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/hive-github-identity-reconciler-cronjob.json
.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-github-identity-reconciler

echo "Installing Origin/Forgejo token reconciler CronJob (PLAT-7406)"
# ensure_agent_cortex_key create-if-missing persists CORTEX_API_KEY only.
# FORGEJO_TOKEN refs are optional, so a wipe leaves Ready pods with no Origin
# identity. Rendered from the rolled hive image so the command cannot drift.
python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > /tmp/hive-forgejo-token-reconciler-cronjob.json
import copy
import json
import sys

deploy = json.load(open(sys.argv[1]))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    c for c in pod_spec.get("containers", [])
    if c.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["name"] = "reconciler"
container["command"] = [
    "python", "manage.py", "reconcile_agent_forgejo_tokens", "--apply",
]
container.pop("args", None)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe",
    "lifecycle",
):
    container.pop(field, None)

job_pod_spec = {
    "restartPolicy": "Never",
    "containers": [container],
}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes",
    "imagePullSecrets", "serviceAccount", "serviceAccountName",
    "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {
        "name": "hive-forgejo-token-reconciler",
        "namespace": "shizuha-hive",
        "labels": {
            "app": "hive",
            "app.kubernetes.io/name": "hive",
            "app.kubernetes.io/component": "forgejo-token-reconciler",
            "shizuha.io/task": "PLAT-7406",
        },
    },
    "spec": {
        "suspend": False,
        "schedule": "27 * * * *",
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 3,
        "failedJobsHistoryLimit": 10,
        "jobTemplate": {
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 600,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "hive",
                            "app.kubernetes.io/name": "hive",
                            "app.kubernetes.io/component": "forgejo-token-reconciler",
                        },
                    },
                    "spec": job_pod_spec,
                },
            },
        },
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/hive-forgejo-token-reconciler-cronjob.json
.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-forgejo-token-reconciler
.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-forgejo-token-reconciler -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[?(@.name=="reconciler")].command}' | grep -q reconcile_agent_forgejo_tokens || { echo "::error::forgejo token reconciler command missing"; exit 1; }

cat <<'YAML' | .forgejo/scripts/kubectl apply -f -
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: hive-claw-isolation-reconcile
  namespace: monitoring
  labels:
    app.kubernetes.io/name: hive
    app.kubernetes.io/component: claw-isolation-reconcile
    release: kube-prometheus-stack
    shizuha.io/task: HIVE-429
spec:
  groups:
    - name: hive-claw-isolation-reconcile
      rules:
        - alert: HiveClawIsolationReconcileFailed
          expr: kube_job_status_failed{namespace="shizuha-hive", job_name=~"hive-claw-isolation-reconcile-.*"} > 0
          for: 0m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive claw-isolation reconcile job failed"
            description: >-
              The scheduled HIVE-429 claw-isolation reconciliation
              CronJob failed. Existing claws may be missing required
              NetworkPolicies such as wan-egress. Inspect
              `.forgejo/scripts/kubectl logs -n shizuha-hive job/{{ $labels.job_name }}`
              and rerun
              `.forgejo/scripts/kubectl -n shizuha-hive exec deploy/hive -- python manage.py reconcile_claw_isolation --check`
              after fixing the cause.
        - alert: HiveClawIsolationReconcileCronJobMissing
          expr: absent(kube_cronjob_info{namespace="shizuha-hive", cronjob="hive-claw-isolation-reconcile"})
          for: 15m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive claw-isolation reconcile CronJob is missing"
            description: >-
              The HIVE-429 scheduled reconcile trigger is absent, so
              existing claw namespace drift will not self-heal.
              Restore the `hive-claw-isolation-reconcile` CronJob from
              the Hive deploy workflow.
YAML

# HIVE-2011 / ADR-0080: scrape the Claw idle-recycle metrics and load the
# fail-loud stale-Running rules. The rule MUST carry release=kube-prometheus
# (the label the live Prometheus ruleSelector matches in the monitoring
# namespace) — the hive-claw-isolation-reconcile rule above uses
# kube-prometheus-stack and is NOT selected by the live Prometheus.
cat <<'YAML' | .forgejo/scripts/kubectl apply -f -
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: hive-claw-idle
  namespace: monitoring
  labels:
    release: kube-prometheus
    app.kubernetes.io/part-of: shizuha-platform
    app.kubernetes.io/component: hive-claw-idle
    hive-task: "2011"
spec:
  namespaceSelector:
    matchNames:
      - shizuha-hive
  selector:
    matchLabels:
      app: hive
  endpoints:
    - port: http
      path: /hive/api/v1/internal/claw-idle/metrics
      interval: 30s
      scrapeTimeout: 10s
---
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: hive-claw-idle
  namespace: monitoring
  labels:
    release: kube-prometheus
    app.kubernetes.io/part-of: shizuha-platform
    app.kubernetes.io/component: hive-claw-idle
    hive-task: "2011"
spec:
  groups:
    - name: hive-claw-idle
      interval: 30s
      rules:
        - alert: HiveClawIdleRunning
          expr: hive_claw_idle_running == 1
          for: 30m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Running Claw {{ $labels.namespace }} is idle past its TTL and not stopped"
            description: >-
              Claw {{ $labels.namespace }} (id {{ $labels.claw_id }}) is still
              Running past its idle TTL after the scheduled idle-recycle
              reconcile — scale-to-zero did not complete. PVC is retained; the
              next sweep retries, but this must not rot silently (ADR-0080).
              Inspect:
              `.forgejo/scripts/kubectl -n shizuha-hive exec deploy/hive -- python manage.py finalize_claw_deletions`
              and check the claw Deployment replica count in namespace
              {{ $labels.namespace }}.
        - alert: HiveClawIdleStopFailed
          expr: sum(increase(hive_claw_idle_stop_failure_total[30m])) > 0
          for: 0m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Idle Claw scale-to-zero failed"
            description: >-
              The idle-recycle worker failed to scale at least one idle Running
              Claw to zero in the last 30 minutes. finalize_claw_deletions
              should have failed loudly; check its logs and the
              HiveClawIdleRunning alert for the affected tenant.
        - alert: HiveClawIdleMetricsAbsent
          expr: absent(hive_claw_idle_running)
          for: 2h
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hive claw-idle metric absent"
            description: >-
              The hive_claw_idle_running metric has not been scraped for 2
              hours — the metrics endpoint or the idle-recycle reconcile may be
              down. Without it the stale-Running guard (HIVE-2011) cannot fire.
              Verify /hive/api/v1/internal/claw-idle/metrics is reachable and
              scraped.
YAML

echo "Installing scheduled hosted-claw WAN-egress regression smoke"
python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > /tmp/hive-wan-egress-smoke-cronjob.json
import copy
import json
import os
import sys

deploy = json.load(open(sys.argv[1]))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    c for c in pod_spec.get("containers", [])
    if c.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["name"] = "wan-egress-smoke"
container["command"] = ["python", "manage.py", "check_hosted_claw_wan_egress"]
container.pop("args", None)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe",
    "lifecycle",
):
    container.pop(field, None)
# HIVE-450: default to the internal test claw DevOps used for HIVE-402
# AC4/AC5 live verification. Deploy-time env can override this, or the
# command can target a namespace labelled shizuha.io/wan-egress-smoke=true.
env = list(container.get("env") or [])
if not any(item.get("name") == "HIVE_WAN_EGRESS_PROBE_NAMESPACE" for item in env):
    env.append({
        "name": "HIVE_WAN_EGRESS_PROBE_NAMESPACE",
        "value": os.environ.get(
            "HIVE_WAN_EGRESS_PROBE_NAMESPACE",
            "claw-275-246f8f894b8d4cb294b9364b7dd61f27",
        ),
    })
container["env"] = env

job_pod_spec = {
    "restartPolicy": "Never",
    "containers": [container],
}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes",
    "imagePullSecrets", "serviceAccount", "serviceAccountName",
    "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {
        "name": "hive-hosted-claw-wan-egress-smoke",
        "namespace": "shizuha-hive",
        "labels": {
            "app": "hive-hosted-claw-wan-egress-smoke",
            "app.kubernetes.io/name": "hive",
            "app.kubernetes.io/component": "hosted-claw-wan-egress-smoke",
            "shizuha.io/task": "HIVE-450",
        },
    },
    "spec": {
        "schedule": "*/15 * * * *",
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 1,
        "failedJobsHistoryLimit": 10,
        "jobTemplate": {
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "hive-hosted-claw-wan-egress-smoke",
                            "app.kubernetes.io/name": "hive",
                            "app.kubernetes.io/component": "hosted-claw-wan-egress-smoke",
                            "shizuha.io/task": "HIVE-450",
                        },
                    },
                    "spec": job_pod_spec,
                },
            },
        },
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/hive-wan-egress-smoke-cronjob.json
.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-hosted-claw-wan-egress-smoke

cat <<'YAML' | .forgejo/scripts/kubectl apply -f -
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: hive-hosted-claw-wan-egress-smoke
  namespace: monitoring
  labels:
    app.kubernetes.io/name: hive
    app.kubernetes.io/component: hosted-claw-wan-egress-smoke
    release: kube-prometheus-stack
    shizuha.io/task: HIVE-450
spec:
  groups:
    - name: hive-hosted-claw-wan-egress-smoke
      rules:
        - alert: HiveHostedClawWanEgressSmokeFailed
          expr: kube_job_status_failed{namespace="shizuha-hive", job_name=~"hive-hosted-claw-wan-egress-smoke-.*"} > 0
          for: 0m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hosted-claw WAN-egress regression smoke failed"
            description: >-
              The HIVE-450 hosted-claw WAN-egress smoke failed. Public
              GitHub HTTPS must be reachable from the internal test
              claw, while 10.42/10.43, RFC1918, 169.254.169.254, and
              100.64.0.0/10 targets must remain unreachable. Inspect
              `.forgejo/scripts/kubectl logs -n shizuha-hive job/{{ $labels.job_name }}`
              and run
              `.forgejo/scripts/kubectl -n shizuha-hive create job --from=cronjob/hive-hosted-claw-wan-egress-smoke hive-hosted-claw-wan-egress-smoke-manual`.
        - alert: HiveHostedClawWanEgressSmokeMissing
          expr: absent(kube_cronjob_info{namespace="shizuha-hive", cronjob="hive-hosted-claw-wan-egress-smoke"})
          for: 15m
          labels:
            severity: warning
            team: engineering
            owner_group: engineering
            service: shizuha-hive
          annotations:
            summary: "Hosted-claw WAN-egress smoke CronJob is missing"
            description: >-
              The HIVE-450 scheduled WAN-egress regression smoke is
              absent, so public-WAN/private-network isolation can
              silently regress.
YAML

# The declared public ID/Hive journey uses the private TLS edge even for the
# previous application image. Reconcile this independent connectivity invariant
# through the ring fence; do not apply unrelated namespace policies here.
python3 .forgejo/scripts/render-hive-public-tls-egress.py > /tmp/hive-public-tls-egress.json
.forgejo/scripts/kubectl apply --dry-run=server -f /tmp/hive-public-tls-egress.json >/dev/null
.forgejo/scripts/kubectl apply -f /tmp/hive-public-tls-egress.json

echo "Installing HIVE-580 golden-path validation fail-loud alerting"
GOLDEN_PATH_ALERTS=k8s/hive-golden-path-alerts.yaml
.forgejo/scripts/kubectl apply --dry-run=server -f "$GOLDEN_PATH_ALERTS" >/dev/null
.forgejo/scripts/kubectl apply -f "$GOLDEN_PATH_ALERTS"
.forgejo/scripts/kubectl -n shizuha-hive get prometheusrule hive-golden-path

echo "Installing HIVE-580 weekly golden-path validation CronJob"
# Generated from the post-rollout Deployment so the image and DB/JWT
# env are the exact CI-promoted ones (contract clause 6 — no
# hand-built tag, no bootstrap tag, no manual set-image). The
# declarative reference lives at k8s/hive-golden-path-validation.yaml
# and claws/test_hive580_golden_path_cronjob.py asserts the contract
# clauses against it.
# NB: the deploy JSON must come in via argv, NOT '< file' — combining a
# <<'PY' heredoc with a second stdin redirection makes the file win.
python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > /tmp/hive-golden-path-cronjob.json
import copy
import json
import sys

deploy = json.load(open(sys.argv[1]))
pod_spec = deploy["spec"]["template"]["spec"]
base_container = next(
    c for c in pod_spec.get("containers", [])
    if c.get("name") == "hive"
)
container = copy.deepcopy(base_container)
container["name"] = "validator"
# All cycle logic (brief rotation, fail-closed credential check,
# publish-then-exit, lowercase RED detection) lives in the management
# command so this workflow and the declarative manifest invoke one
# implementation instead of duplicating shell that would drift.
container["command"] = ["python", "manage.py", "run_golden_path_cycle"]
container.pop("args", None)
for field in (
    "ports", "livenessProbe", "readinessProbe", "startupProbe",
    "lifecycle",
):
    container.pop(field, None)

env = [e for e in container.get("env", []) if e.get("name") not in {
    "ID_BASE_URL", "SHIZUHA_ID_INTERNAL_URL", "HIVE_BASE_URL", "VALIDATION_RUNTIME_TOKEN",
}]
env.extend([
    {"name": "ID_BASE_URL", "value": "https://shizuha.com/id"},
    {"name": "SHIZUHA_ID_INTERNAL_URL", "value": "http://shizuha-id.shizuha.svc.cluster.local:8001"},
    {"name": "HIVE_BASE_URL", "value": "https://shizuha.com"},
    # Clause 5: provisioned OUTSIDE this workload and consumed
    # fail-closed. Required (not optional) so a missing Secret stops
    # the pod rather than silently running an anonymous journey.
    {
        "name": "VALIDATION_RUNTIME_TOKEN",
        "valueFrom": {
            "secretKeyRef": {
                "name": "hive-golden-path-validation",
                "key": "validation-runtime-token",
            },
        },
    },
])
container["env"] = env

job_pod_spec = {
    "restartPolicy": "Never",
    "containers": [container],
}
for field in (
    "affinity", "nodeSelector", "tolerations", "volumes",
    "imagePullSecrets", "serviceAccount", "serviceAccountName",
    "securityContext", "runtimeClassName",
):
    value = pod_spec.get(field)
    if value is not None:
        job_pod_spec[field] = copy.deepcopy(value)

labels = {
    "app": "hive",
    "app.kubernetes.io/name": "hive",
    "app.kubernetes.io/component": "golden-path-validation",
    "shizuha.io/task": "HIVE-580",
}
cronjob = {
    "apiVersion": "batch/v1",
    "kind": "CronJob",
    "metadata": {
        "name": "hive-golden-path-validation",
        "namespace": "shizuha-hive",
        "labels": labels,
    },
    "spec": {
        "schedule": "23 4 * * 1",
        "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 3,
        "failedJobsHistoryLimit": 3,
        "jobTemplate": {
            "spec": {
                # No retry: a RED scorecard is a real result to
                # publish, not a flake to re-run until it looks green.
                "backoffLimit": 0,
                "activeDeadlineSeconds": 1800,
                "ttlSecondsAfterFinished": 86400,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": job_pod_spec,
                },
            },
        },
    },
}
print(json.dumps(cronjob))
PY
.forgejo/scripts/kubectl apply -f /tmp/hive-golden-path-cronjob.json
GOLDEN_PATH_IMAGE="$(.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-golden-path-validation -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[?(@.name=="validator")].image}')"
echo "Golden-path validator image: ${GOLDEN_PATH_IMAGE}"
echo "${GOLDEN_PATH_IMAGE}" | grep -q "${TAG}" || { echo "::error::golden-path validator image mismatch"; exit 1; }
.forgejo/scripts/kubectl -n shizuha-hive get cronjob hive-golden-path-validation -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[?(@.name=="validator")].command}' | grep -q run_golden_path_cycle || { echo "::error::golden-path validator command missing"; exit 1; }

# Contract clause 1: "An alert is not a trigger; the merge workflow
# must create/dispatch the run." Weekly scheduling alone is not enough.
GOLDEN_PATH_JOB="hive-golden-path-validation-deploy-${SHORT}-$(date +%s)"
.forgejo/scripts/kubectl -n shizuha-hive create job \
  --from=cronjob/hive-golden-path-validation \
  "${GOLDEN_PATH_JOB}"
# Deliberately NOT gating the deploy on the outcome. A RED scorecard is
# the DESIGNED steady state today: HIVE-566 is deferred by Architecture
# decision, so provisioning stops fail-closed at manual_review and
# stages 3-5 are unreachable. Per the HIVE-580 disposition that RED is
# the forcing signal, not a deploy defect — gating here would wedge
# every Hive deploy on a known-red cycle. Fail-loud is carried by the
# Job's own non-zero exit plus the hive-golden-path PrometheusRule.
# `kubectl wait --for=condition=complete` ignores Failed and sits the
# full timeout (Origin 38401: Job exit 2 in 4s, then 1800s hang). Treat
# succeeded OR failed as terminal.
GOLDEN_WAIT_DEADLINE=$((SECONDS + 180))
GOLDEN_SUCC=""
GOLDEN_FAIL=""
while [ "$SECONDS" -lt "$GOLDEN_WAIT_DEADLINE" ]; do
  GOLDEN_SUCC="$(.forgejo/scripts/kubectl -n shizuha-hive get job "${GOLDEN_PATH_JOB}" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
  GOLDEN_FAIL="$(.forgejo/scripts/kubectl -n shizuha-hive get job "${GOLDEN_PATH_JOB}" -o jsonpath='{.status.failed}' 2>/dev/null || true)"
  if [ "${GOLDEN_SUCC:-0}" = "1" ]; then
    break
  fi
  if [ -n "${GOLDEN_FAIL:-}" ] && [ "${GOLDEN_FAIL}" != "0" ]; then
    echo "::warning::golden-path cycle Job failed (designed RED while HIVE-566 is deferred)"
    break
  fi
  sleep 5
done
if [ "${GOLDEN_SUCC:-0}" != "1" ]; then
  echo "::warning::golden-path cycle did not complete green — scorecard follows (expected while HIVE-566 is deferred)"
fi
.forgejo/scripts/kubectl -n shizuha-hive logs "job/${GOLDEN_PATH_JOB}" --all-containers=true || true


NODE="$(.forgejo/scripts/kubectl get pods -n shizuha-hive -l app=hive -o jsonpath='{.items[?(@.status.phase=="Running")].spec.nodeName}')"
echo "Hive node(s): ${NODE}"
! echo "${NODE}" | grep -qw s1 || { echo "::error::Hive backend landed on excluded node s1"; exit 1; }
.forgejo/scripts/kubectl get pods -n shizuha-hive -l app=hive

echo "Verifying leftover Hive-managed claw namespaces still have dashboard ingress policies"
MANAGED_CLAW_NS="$(.forgejo/scripts/kubectl get ns -l app.kubernetes.io/managed-by=shizuha-hive -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep '^claw-' || true)"
if [ -z "${MANAGED_CLAW_NS}" ]; then
  echo "No Hive-managed claw namespaces remain (Claw retired HIVE-2007); skipping dashboard NetworkPolicy verification"
else
  for NS in ${MANAGED_CLAW_NS}; do
    echo "Checking ${NS}: owner-isolation + dashboard-ingress"
    .forgejo/scripts/kubectl get networkpolicy -n "${NS}" owner-isolation dashboard-ingress >/dev/null
    PORTS="$(.forgejo/scripts/kubectl get networkpolicy -n "${NS}" dashboard-ingress -o jsonpath='{range .spec.ingress[*].ports[*]}{.port}{"\n"}{end}')"
    echo "${PORTS}" | grep -qx '18789' || { echo "::error::${NS}/dashboard-ingress does not allow port 18789"; exit 1; }
  done
fi

echo "Verifying public Hive dashboard proxy routes for any remaining running claws"
# HIVE-2007: Claw is retired; FleetAgent is the only agent. Requiring a RUNNING
# Hritik/Harshita Claw (HIVE-2029) rolled every post-retirement fleet release
# back to e6a894d. Probe remaining running claws if any exist; do not fail
# closed when the product is gone.
DASHBOARD_TARGETS="$(.forgejo/scripts/kubectl exec -n shizuha-hive deploy/hive -- python manage.py shell -c 'exec("from claws.models import Claw\nowners=(\"hritik\", \"harshita\")\nseen={}\nfor claw in Claw.objects.filter(owner_username__in=owners, status=Claw.Status.RUNNING).order_by(\"owner_username\", \"-created_at\"):\n    seen.setdefault(claw.owner_username, claw)\nif not seen:\n    print(\"SKIP\")\nelse:\n    for owner, claw in seen.items():\n        print(f\"{owner} {claw.owner_id} {claw.id}\")")')"
if printf '%s\n' "${DASHBOARD_TARGETS}" | grep -qx SKIP; then
  echo "No running hritik/harshita claws (Claw retired HIVE-2007); skipping openclaw dashboard proxy smoke"
else
  PUBLIC_BASE="${HIVE_PUBLIC_BASE_URL:-https://s1.tail.shizuha.com}"
  printf '%s\n' "${DASHBOARD_TARGETS}" | while read -r OWNER OWNER_ID CLAW_ID; do
    [ -n "${OWNER:-}" ] || continue
    URL="${PUBLIC_BASE}/hive/openclaw/${OWNER_ID}/${CLAW_ID}/"
    echo "Checking ${OWNER} dashboard route: ${URL}"
    # HIVE: this is a PUBLIC end-to-end smoke of the claw dashboard proxy —
    # it depends on live claw-runtime + ingress/proxy readiness, which can
    # transiently 502 during/right after a rollout even when the Hive BACKEND
    # deploy is healthy (image verified, rollout complete, migrations OK).
    # A single un-retried curl here spuriously failed an otherwise-successful
    # deploy and paged the repo lead (run 222 / origin run 1300 — the backend
    # was 1/1 on the new image, the route returned 200 minutes later). Retry
    # with backoff so a transient blip clears; fail ONLY if the route is
    # PERSISTENTLY non-200 after ~60s (a real dashboard regression).
    CODE=000
    for attempt in 1 2 3 4 5 6; do
      CODE="$(curl -k -sS -o "/tmp/hive-dashboard-${OWNER}.html" -w '%{http_code}' --max-time 30 "${URL}" || echo 000)"
      echo "${OWNER} dashboard HTTP ${CODE} (attempt ${attempt}/6)"
      [ "${CODE}" = "200" ] && break
      [ "${attempt}" = "6" ] || sleep 10
    done
    if [ "${CODE}" != "200" ]; then
      echo "::error::${OWNER} dashboard route persistently non-200 (got ${CODE}) after 6 attempts (~60s)"
      head -c 500 "/tmp/hive-dashboard-${OWNER}.html" || true
      exit 1
    fi
  done
fi

# ── R2 post-deploy: build-attributed 60m SLO gate ───────────────────
echo "=== R2: Fleet post-deploy ==="
.forgejo/scripts/render-ring-manifest.sh k8s/rollout/hive-fleet-probe.yaml.tpl \
  "localhost:30500/shizuha-hive:${TAG}" "$SHA" /tmp/hive-fleet-probe.yaml
.forgejo/scripts/kubectl -n monitoring delete probe -l service=hive,release_ring=fleet --ignore-not-found
.forgejo/scripts/kubectl apply -f /tmp/hive-fleet-probe.yaml
bash .forgejo/scripts/ring-gate.sh fleet shizuha-hive hive "$SHA" > /tmp/fleet-gate.log 2>&1
cat /tmp/fleet-gate.log

# Every long-lived Hive workload is regenerated above from the now
# sanitized Deployment. Fail closed before recording success if a
# legacy host/org provider credential reference remains anywhere.
.forgejo/scripts/kubectl get deploy,cronjob -n shizuha-hive -o json | python3 -c '
import json, sys
retired_env = {"HIVE_ORG_CREDENTIALS_JSON"}
retired_secrets = {"hive-org-credentials"}
resources = json.load(sys.stdin)
violations = []
for resource in resources.get("items", []):
    kind = resource.get("kind", "?")
    name = resource.get("metadata", {}).get("name", "?")
    spec = resource.get("spec", {})
    if kind == "Deployment":
        pod_spec = spec.get("template", {}).get("spec", {})
    elif kind == "CronJob":
        pod_spec = (
            spec.get("jobTemplate", {}).get("spec", {})
            .get("template", {}).get("spec", {})
        )
    else:
        continue
    for container in pod_spec.get("containers", []):
        for item in container.get("env", []):
            secret_name = (
                item.get("valueFrom", {})
                .get("secretKeyRef", {})
                .get("name")
            )
            if item.get("name") in retired_env or secret_name in retired_secrets:
                container_name = container.get("name", "?")
                item_name = item.get("name", "?")
                violations.append(f"{kind}/{name}:{container_name}:{item_name}")
if violations:
    raise SystemExit("retired credential references remain: " + ", ".join(sorted(violations)))
'

# INV-F: persistence is permitted only after atomically proving the
# exact image, build labels, observed generation, and fully available
# replicas. A concurrent/manual rollback must fail this run.
python3 .forgejo/scripts/record-successful-release.py \
  shizuha-hive hive "$SHA" \
  "localhost:30500/shizuha-hive:${TAG}" \
  hive
STORED="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o jsonpath='{.metadata.annotations.last-successful-sha}')"
STORED_NAMESPACED="$(.forgejo/scripts/kubectl -n shizuha-hive get deploy hive -o jsonpath='{.metadata.annotations.shizuha\.io/last-successful-sha}')"
[ "$STORED" = "$SHA" ] || { echo "::error::INV-F anchor persistence mismatch"; exit 1; }
[ "$STORED_NAMESPACED" = "$SHA" ] || { echo "::error::INV-F namespaced anchor persistence mismatch"; exit 1; }

# Commit the exact holder/token immediately at INV-F. Any later operational
# failure is reported but cannot be reinterpreted by recovery as a bad release.
/app/.venv/bin/python /app/.forgejo/scripts/ring-executor.py commit

# HIVE-1385: INV-F is the release-health commit point. From here on,
# teardown/finalization failures are operational failures and must not
# enter r2_failure or roll a proven-healthy fleet back. Disarm the
# in-process health rollback first; the cluster watchdog independently
# checks the committed anchor before any stale-run rollback.
trap - ERR TERM INT
echo "=== R2: Fleet ring gate PASS; last-successful-sha=${SHA} ==="
# DeploymentEvent is post-INV-F evidence, not part of the rollout
# transaction (frontend/wiki precedent). Origin 38477 already committed
# last-successful-sha=f1f0647, then this call failed on a token-only
# secret and marked the Actions run failed — which queued a duplicate
# f1f0647 dispatch (38478) ahead of the next SHA.
# PLAT-7421: Hive owns a four-argument recorder (<service> <component>
# <image> [qa-surfaces]). The deploy repository has a different seven-argument
# recorder; importing that call shape here exits64 before any event is sent.
# Post-INV-F evidence, not part of the rollout transaction (HIVE-1385).
IMAGE_INDEX_DIGEST="$(
  curl -fsS -D - -o /dev/null --max-time 20 \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    "http://registry.registry.svc.cluster.local:5000/v2/shizuha-hive/manifests/${TAG}" |
    tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest" {print $2}' | tail -1
)"
[[ "$IMAGE_INDEX_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "::error::registry omitted a valid multi-arch index digest"
  exit 1
}
if ! DEPLOYMENT_EVENT_IMAGE_DIGEST="$IMAGE_INDEX_DIGEST" \
    DEPLOYMENT_EVENT_HELM_MANAGED=0 \
    .forgejo/scripts/record-deployment-event.sh \
    hive hive-backend "localhost:30500/shizuha-hive:${TAG}" \
    /hive/agents,/hive/subscriptions; then
  echo "::warning::rollout succeeded but DeploymentEvent recording failed"
fi

# Final dark-producer activation: bind the immutable record only after
# every fleet gate and release-event write has passed.  Failure leaves
# the endpoint fail-closed without disturbing the healthy Hive rollout.
CAPABILITY_HIVE_POD="$(h725_ready_pod shizuha-hive app=hive hive)"
.forgejo/scripts/kubectl -n shizuha-hive exec "$CAPABILITY_HIVE_POD" -c hive -- \
  python manage.py activate_capability_evidence_generation \
    --generation-id "hive-${SHA}" \
    --source-sha "$SHA" \
    --source-tree "$SOURCE_TREE" \
    --image-index-digest "$IMAGE_INDEX_DIGEST"
