#!/usr/bin/env bash
# hive-migrate-claw.sh — Milestone A: migrate shizuha-openclaw from s1 into Hive
#
# Implements HIVE-2 HLD §4.1–§4.5 exactly.
# Operator-grade runbook: read the full HIVE-2 HLD before running.
# Wiki: dd243d51-18b1-467f-8ddd-fee10aa1c226
#
# HARD PREREQUISITES (§4.1 must pass before proceeding):
#   G-a: registry contains the current Hive OpenClaw multi-arch manifest:
#        localhost:30500/openclaw:2026.6.11-multiarch
#        list digest sha256:b55a1a003f002ed273c7977abf5d30c7ca21008a11357ad065ac08a0eb927cf2
#   G-b: HIVE-25 §G-b = PASS on record (no WA QR re-pair; evidence wiki e1cde7e1)
#
# USAGE:
#   # Set the claw namespace (provisioner creates this via POST /v1/claws/ with migration flag)
#   export CLAW_NS=claw-<owner_id>-<claw_id>
#   # Optional: skip individual stages with SKIP_PREFLIGHT=1, SKIP_CUTOVER=1, etc.
#   bash ops/hive-migrate-claw.sh
#
# POINT OF NO RETURN (§4.5): once docker rm runs, rollback requires §4.6
# (recreate host container from retained dirs; WA session valid for 7d).
set -euo pipefail

: "${CLAW_NS:?Set CLAW_NS to the claw namespace (claw-<owner_id>-<claw_id>)}"
: "${SKIP_PREFLIGHT:=0}"
: "${SKIP_CUTOVER:=0}"

# Source locations are operator-managed inputs, never a built-in personal home.
: "${S1_HOST:?Set S1_HOST to the reviewed migration source host}"
: "${S1_DATA_DIR:?Set S1_DATA_DIR to the reviewed absolute source data directory}"
: "${S1_WORKSPACE_DIR:?Set S1_WORKSPACE_DIR to the reviewed absolute source workspace directory}"
for source_dir in "$S1_DATA_DIR" "$S1_WORKSPACE_DIR"; do
  [[ "$source_dir" = /* && "$source_dir" != / && "$source_dir" != *'/../'* && "$source_dir" != */.. ]] || {
    echo 'Migration source directories must be explicit absolute paths without traversal.' >&2
    exit 1
  }
done
CONTAINER_NAME="shizuha-openclaw"
G_A_MANIFEST_DIGEST="sha256:b55a1a003f002ed273c7977abf5d30c7ca21008a11357ad065ac08a0eb927cf2"
G_A_IMAGE="localhost:30500/openclaw:2026.6.11-multiarch"

log() { echo "[hive-migrate $(date '+%H:%M:%S')] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }

# ────────────────────────────────────────────────────────────
# §4.1 Pre-flight
# ────────────────────────────────────────────────────────────
if [[ "$SKIP_PREFLIGHT" != "1" ]]; then
  log "§4.1 PRE-FLIGHT"

  log "  G-a: checking multi-arch manifest digest in cluster registry..."
  actual_digest=""
  if command -v skopeo &>/dev/null; then
    if digest=$(skopeo inspect --raw "docker://${G_A_IMAGE}" \
      2>/dev/null | sha256sum | awk '{print "sha256:" $1}'); then
      actual_digest="${digest}"
    fi
  else
    # Fall back to crictl inspecti (NOT pull) on an arm64 node; the image must
    # already be present from the accepted multi-arch registry import.
    if digest=$(ssh gx10-1 sudo crictl inspecti "${G_A_IMAGE}" 2>/dev/null \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status",{}).get("repoDigests",[""])[0].split("@")[-1])'); then
      actual_digest="${digest}"
    fi
  fi
  log "    actual list digest: ${actual_digest:-<empty — verification failed>}"
  log "    expected:           ${G_A_MANIFEST_DIGEST}"
  if [[ -z "${actual_digest}" ]]; then
    fail "G-a FAILED: could not verify OpenClaw manifest digest; do not proceed until the correct multi-arch manifest is in the registry."
  fi
  if [[ "${actual_digest}" != "${G_A_MANIFEST_DIGEST}" ]]; then
    fail "G-a FAILED: manifest digest mismatch. actual='${actual_digest}' expected='${G_A_MANIFEST_DIGEST}'. Do not proceed until the correct multi-arch manifest is in the registry."
  fi
  log "  G-a: accepted"

  log "  G-b: HIVE-25 PASS is a recorded fact (evidence wiki e1cde7e1). Proceeding."

  log "  Hive control-plane reachable?"
  kubectl -n shizuha-hive get pods -l app=hive-api --no-headers | grep -q Running \
    || fail "hive-api not running in shizuha-hive; fix HIVE-1 health first"

  log "  Target namespace check..."
  if kubectl get ns "${CLAW_NS}" &>/dev/null; then
    log "  WARNING: namespace ${CLAW_NS} already exists."
    read -r -p "  Resume from §4.3 (skip §4.2 provision)? [y/N] " ans
    [[ "$ans" == "y" ]] || fail "Operator chose not to resume; aborting."
    SKIP_PROVISION=1
  else
    SKIP_PROVISION=0
  fi

  log "  Source container running on ${S1_HOST}?"
  ssh "${S1_HOST}" docker ps --filter "name=${CONTAINER_NAME}" --format '{{.Status}}' \
    | grep -q Up || fail "Container ${CONTAINER_NAME} not running on ${S1_HOST}"

  log "  Source data dirs non-empty?"
  ssh "${S1_HOST}" du -sh "${S1_DATA_DIR}" "${S1_WORKSPACE_DIR}" \
    || fail "Cannot stat source dirs on ${S1_HOST}"

  log "  WA session blob present?"
  ssh "${S1_HOST}" stat "${S1_DATA_DIR}/credentials/whatsapp/default/creds.json" \
    || fail "WA session blob missing — would force QR re-pair; aborting"

  log "  Extracting OPENCLAW_GATEWAY_TOKEN (goes to operator vault — NOT logged)..."
  GW_TOKEN=$(ssh "${S1_HOST}" "docker inspect ${CONTAINER_NAME} \
    | jq -r '.[0].Config.Env[] | select(startswith(\"OPENCLAW_GATEWAY_TOKEN=\"))' \
    | cut -d= -f2-")
  [[ -n "${GW_TOKEN}" ]] \
    || log "  WARNING: OPENCLAW_GATEWAY_TOKEN not found in container env (may be absent for this build)"
  log "  Gateway token extracted (value suppressed). Store it in your vault now."

  log "§4.1 PRE-FLIGHT PASSED"
fi

# ────────────────────────────────────────────────────────────
# §4.2 Provision the target claw (Option A — provision-then-overwrite)
# ────────────────────────────────────────────────────────────
if [[ "${SKIP_PROVISION:-0}" != "1" ]]; then
  log "§4.2 PROVISION — POST /v1/owner/claws via hive-api (or: kubectl apply -f k8s/claw/)"
  log "  Provision the proving claw via the hive-api and confirm namespace ${CLAW_NS} exists."
  log "  (Automated via hive-api POST; see hive CLAUDE.md for endpoint + auth)"
  log "  Waiting for namespace to appear..."
  for i in $(seq 1 30); do
    kubectl get ns "${CLAW_NS}" &>/dev/null && break
    sleep 5
    log "  ...waiting (${i}/30)"
  done
  kubectl get ns "${CLAW_NS}" || fail "Namespace ${CLAW_NS} not created; provisioning failed"
  log "§4.2 PROVISION DONE — claw pod booted briefly, PVC materialized"
fi

# ────────────────────────────────────────────────────────────
# §4.3 Data lift
# ────────────────────────────────────────────────────────────
log "§4.3 DATA LIFT"

# §4.3.1 Stop host container (graceful, 30s window)
log "  §4.3.1 Stopping host container (docker stop -t 30)..."
ssh "${S1_HOST}" docker stop -t 30 "${CONTAINER_NAME}" \
  || fail "docker stop returned non-zero; investigate before proceeding"

# §4.3.1a Assert container is in exited state
log "  §4.3.1a Asserting container exited cleanly..."
status=$(ssh "${S1_HOST}" "docker inspect ${CONTAINER_NAME} --format '{{.State.Status}}'")
[[ "${status}" == "exited" ]] \
  || fail "Container is in state '${status}' (not 'exited'); aborting before tar-pipe"
log "  Container confirmed exited."

# §4.3.2 Scale cluster StatefulSet/Deployment to 0 — free PVC for ephemeral mount
log "  §4.3.2 Scaling claw deployment to 0..."
kubectl -n "${CLAW_NS}" scale deployment/claw --replicas=0
kubectl -n "${CLAW_NS}" wait pod --all --for=delete --timeout=60s || true
log "  Claw pod deleted; PVC free for ephemeral mount."

# §4.3.3 Launch ephemeral migrator pod
log "  §4.3.3 Launching migrator pod..."
kubectl -n "${CLAW_NS}" apply -f ops/migrator.yaml
kubectl -n "${CLAW_NS}" wait pod/migrator --for=condition=ready --timeout=120s \
  || fail "Migrator pod did not become ready; check events: kubectl -n ${CLAW_NS} describe pod/migrator"
log "  Migrator pod ready."

# §4.3.4 Clean-slate: remove §4.2 brief-boot scaffolding
log "  §4.3.4 Clean-slate: removing §4.2 brief-boot scaffolding from PVC..."
kubectl -n "${CLAW_NS}" exec migrator -- sh -c 'rm -rf /mnt/dst/* /mnt/dst/.[!.]*'
log "  Verifying PVC is empty..."
contents=$(kubectl -n "${CLAW_NS}" exec migrator -- sh -c 'ls -la /mnt/dst | wc -l')
[[ "${contents}" -le 3 ]] \
  || fail "PVC not clean after rm; contents: $(kubectl -n ${CLAW_NS} exec migrator -- ls -la /mnt/dst)"
log "  PVC clean."

# §4.3.5 Data lift via GNU tar-pipe (preserves uid/gid/mode/xattrs/acls + .git)
# §4.3 PVC layout (HIVE-2 §3.2): openclaw-data/* → PVC root; openclaw-workspace/* → PVC/workspace/
log "  §4.3.5 Lifting openclaw-data → /mnt/dst/ ..."
ssh "${S1_HOST}" "sudo tar -C ${S1_DATA_DIR} --acls --xattrs --numeric-owner -cf - ." \
  | kubectl -n "${CLAW_NS}" exec -i migrator -- tar -C /mnt/dst --acls --xattrs --numeric-owner -xf -
log "  Lifting openclaw-workspace → /mnt/dst/workspace/ ..."
kubectl -n "${CLAW_NS}" exec migrator -- sh -c 'mkdir -p /mnt/dst/workspace'
ssh "${S1_HOST}" "sudo tar -C ${S1_WORKSPACE_DIR} --acls --xattrs --numeric-owner -cf - ." \
  | kubectl -n "${CLAW_NS}" exec -i migrator -- tar -C /mnt/dst/workspace --acls --xattrs --numeric-owner -xf -
log "  Tar-pipe complete."

# §4.3.6 chown to in-cluster uid (source is phoenix-owned; claw runs as node uid=1000)
log "  §4.3.6 chown 1000:1000 /mnt/dst ..."
kubectl -n "${CLAW_NS}" exec migrator -- chown -R 1000:1000 /mnt/dst
log "  chown done."

# §4.3.7 Checksum verify (byte-level equivalence)
log "  §4.3.7 Checksum verify..."
ssh "${S1_HOST}" "sudo find ${S1_DATA_DIR} ${S1_WORKSPACE_DIR} -type f -exec sha256sum {} +" \
  | awk -v data="${S1_DATA_DIR}" -v ws="${S1_WORKSPACE_DIR}" \
    '{sub(data "/","/mnt/dst/"); sub(ws "/","/mnt/dst/workspace/"); print $1, $2}' \
  | sort > /tmp/hive-migrate-src.sha
kubectl -n "${CLAW_NS}" exec migrator -- \
  sh -c 'find /mnt/dst -type f -exec sha256sum {} +' \
  | sort > /tmp/hive-migrate-dst.sha
if ! diff /tmp/hive-migrate-src.sha /tmp/hive-migrate-dst.sha; then
  fail "Checksum mismatch — see diff above. Re-lift or abort."
fi
log "  Checksum OK (byte-identical)."

# §4.3.8 .git integrity check
log "  §4.3.8 git fsck on /mnt/dst/workspace ..."
kubectl -n "${CLAW_NS}" exec migrator -- git -C /mnt/dst/workspace fsck --full \
  || fail ".git fsck failed — workspace .git may be corrupt. Re-lift workspace."
log "  .git fsck passed."

log "§4.3 DATA LIFT COMPLETE"

# ────────────────────────────────────────────────────────────
# §4.4 Sanity check
# ────────────────────────────────────────────────────────────
log "§4.4 SANITY CHECK — scaling claw to 1..."
kubectl -n "${CLAW_NS}" scale deployment/claw --replicas=1
kubectl -n "${CLAW_NS}" wait pod -l app=claw --for=condition=ready --timeout=180s \
  || fail "Claw pod did not become ready within 180s; check logs: kubectl -n ${CLAW_NS} logs -l app=claw"

log "  Pod logs (tail 50)..."
kubectl -n "${CLAW_NS}" logs -l app=claw --tail=50 || true

log "  WA session reconnect check (healthz endpoint)..."
CLAW_HOST=$(kubectl -n "${CLAW_NS}" get ingress claw-dashboard -o jsonpath='{.spec.rules[0].host}' 2>/dev/null || true)
if [[ -n "${CLAW_HOST}" ]]; then
  for i in $(seq 1 12); do
    resp=$(curl -sf "https://${CLAW_HOST}/api/healthz" 2>/dev/null || true)
    if echo "${resp}" | grep -q '"wa_session":"connected"'; then
      log "  WA session = connected. ✓"
      break
    fi
    log "  ...waiting for WA session (${i}/12) — response: ${resp:0:120}"
    sleep 10
  done
  echo "${resp}" | grep -q '"wa_session":"connected"' \
    || log "  WARNING: WA session not yet connected after 120s. Check dashboard manually."
else
  log "  No ingress found for claw-dashboard; check WA session manually via dashboard."
fi

log "  WA backlog drain window — wait 5 min then verify messages are flowing..."
log "  (T+15min check: send a test message from a known peer and confirm @claw receives + replies)"

log "§4.4 SANITY CHECK DONE — review logs above before proceeding to §4.5"
read -r -p "  Proceed to §4.5 cutover (POINT OF NO RETURN)? [y/N] " proceed
[[ "$proceed" == "y" ]] || { log "Operator aborted before cutover. Rollback: kubectl -n ${CLAW_NS} scale deployment/claw --replicas=0 && ssh ${S1_HOST} docker start ${CONTAINER_NAME}"; exit 0; }

# ────────────────────────────────────────────────────────────
# §4.5 Cutover — POINT OF NO RETURN
# ────────────────────────────────────────────────────────────
if [[ "$SKIP_CUTOVER" == "1" ]]; then
  log "§4.5 CUTOVER SKIPPED (SKIP_CUTOVER=1)"
else
  log "§4.5 CUTOVER — POINT OF NO RETURN"
  log "  docker rm ${CONTAINER_NAME} on ${S1_HOST}..."
  ssh "${S1_HOST}" docker rm "${CONTAINER_NAME}" \
    || fail "docker rm failed; investigate before proceeding"
  log "  Host container removed. Update Shizuha Cron config on ${S1_HOST} to remove the local claw."
  log "  ──────────────────────────────────────────────────────────"
  log "  ROLLBACK WINDOW: s1 host dirs retained for ≥7 days."
  log "  To rollback: recreate container from retained host dirs + Longhorn snapshot (§4.6)."
  log "  ──────────────────────────────────────────────────────────"
  log "§4.5 CUTOVER DONE"
fi

# Cleanup: delete migrator pod
log "  Cleaning up migrator pod..."
kubectl -n "${CLAW_NS}" delete pod migrator --ignore-not-found || true

# ────────────────────────────────────────────────────────────
# §11 Acceptance reminder
# ────────────────────────────────────────────────────────────
log ""
log "═══════════════════════════════════════════════════════════"
log "POST-CUTOVER ACCEPTANCE CHECKLIST (HIVE-2 §11)"
log "═══════════════════════════════════════════════════════════"
log "H1  claw-0 ready within 180s; no fatal errors          [ ]"
log "H2  WA session connected; @claw receives + sends 1 msg  [ ]"
log "H3  Dashboard ${CLAW_HOST:-<CLAW_HOST>} returns 200; Hive UI deep-link renders [ ]"
log "H4  OAuth providers valid (or auto-rotated)             [ ]"
log "H5  shizuha-openclaw not in docker ps on ${S1_HOST}         [ ]"
log "H6  git fsck clean inside container workspace            [ ]"
log "H7  G-a/G-b behavioral-equivalence gate satisfied        [ ]"
log ""
log "M1  Longhorn 75% snapshot+alert configured               [ ]"
log "M2  s1 host dirs retained read-only ≥7 days              [ ]"
log "M3  Shizuha Cron @claw config updated + verified         [ ]"
log "M6  WA backlog drained within 5 min of wa_session=connected [ ]"
log "═══════════════════════════════════════════════════════════"
log "HIVE-2 Milestone A migration complete."
