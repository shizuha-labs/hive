#!/usr/bin/env bash
set -euo pipefail

# Explicit release-regression follow-up (not generic alert→task automation).
# Sends the owner alert and upserts exactly one deduped Pulse bug per bad SHA.
RING="${1:?ring required}"
BAD_SHA="${2:?bad SHA required}"
RESTORED_SHA="${3:?restored SHA required}"
EVIDENCE="${4:?gate evidence required}"
[[ "$BAD_SHA" =~ ^[0-9a-f]{40}$ && "$RESTORED_SHA" =~ ^[0-9a-f]{40}$ ]] ||
  { echo "::error::bad/restored SHA must be full SHAs"; exit 2; }

ALERTMANAGER_URL="${ALERTMANAGER_URL:-http://kube-prometheus-kube-prome-alertmanager.monitoring.svc.cluster.local:9093}"
PULSE_URL="${PULSE_URL:-http://shizuha-pulse.shizuha.svc.cluster.local:8002}"
ID_LOGIN_URL="${ID_LOGIN_URL:-http://shizuha-id.shizuha.svc.cluster.local:8001/api/auth/login/}"
PULSE_TOKEN="${PULSE_TOKEN:-}"
# Prefer a fresh ID login even when a historical token is still populated.
# Missing optional bootstrap keys retain the token-only compatibility path.
PULSE_USERNAME="${PULSE_USERNAME:-$(kubectl -n origin get secret pulse-api-token -o jsonpath='{.data.username}' 2>/dev/null | base64 -d || true)}"
PULSE_PASSWORD="${PULSE_PASSWORD:-$(kubectl -n origin get secret pulse-api-token -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)}"
if [[ -n "$PULSE_USERNAME" && -n "$PULSE_PASSWORD" ]]; then
  login_payload="$(PULSE_USERNAME="$PULSE_USERNAME" PULSE_PASSWORD="$PULSE_PASSWORD" python3 -c 'import json,os; print(json.dumps({"username":os.environ["PULSE_USERNAME"],"password":os.environ["PULSE_PASSWORD"]}))')"
  login_response="$(curl -fsS --max-time 15 -H 'Content-Type: application/json' --data-binary "$login_payload" "$ID_LOGIN_URL")" || {
    echo "::error::ID login failed while minting a fresh Pulse access token"
    exit 1
  }
  PULSE_TOKEN="$(printf '%s' "$login_response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tokens"]["access"])')" || {
    echo "::error::ID login response did not contain a Pulse access token"
    exit 1
  }
  unset PULSE_PASSWORD login_payload login_response
elif [[ -z "$PULSE_TOKEN" ]]; then
  token_b64="$(kubectl -n origin get secret pulse-api-token -o jsonpath='{.data.token}' || true)"
  if [[ -n "$token_b64" ]]; then
    PULSE_TOKEN="$(printf '%s' "$token_b64" | base64 -d)"
  fi
fi
[[ -n "$PULSE_TOKEN" ]] || { echo "::error::Pulse release-failure token unavailable"; exit 1; }

# Early Pulse auth validation (HIVE-623): verify the token is structurally valid
# AND can create HIVE rollback items before any canary/fleet mutation. A list or
# OPTIONS-only probe is insufficient: the historical failure was HTTP 400
# {"project":"You do not have permission to create items in this project"}
# after rollback. Probe create permission by creating a tightly-scoped
# preflight item and deleting it before rollout mutation; if either create or
# cleanup fails, fail closed before canary/fleet state can change.
echo "::group::Pulse auth validation"
projects_response="$(curl -fsS --retry 4 --retry-all-errors --retry-delay 2 \
  --retry-max-time 75 --connect-timeout 5 --max-time 15 \
  -H "Authorization: Bearer ${PULSE_TOKEN}" \
  "${PULSE_URL}/api/projects/?key=HIVE")" || {
  echo "::error::Pulse token rejected or projects endpoint unavailable"
  echo "::error::Fix: regenerate a write-capable Pulse service token and update secret origin/pulse-api-token"
  exit 1
}
project_id="$(printf '%s' "$projects_response" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin); rows=d.get("results",d if isinstance(d,list) else [])
    matches=[r for r in rows if r.get("key") == "HIVE"]
    assert len(matches) == 1, matches
    print(matches[0]["id"])
except Exception as exc:
    raise SystemExit(f"invalid HIVE project lookup: {exc}")
')" || { echo "::error::Pulse token cannot see exactly one HIVE project"; exit 1; }
probe_payload="$(python3 - "$project_id" <<'PYJSON'
import json,sys
print(json.dumps({
    "project": int(sys.argv[1]),
    "title": "[PREFLIGHT][hive-rollback-create-permission]",
    "description": "permission probe; deleted before rollout mutation",
    "mode": "task",
    "priority": "low",
    "workflow_name": "autonomous-bug",
    "assignment_group": "engineering",
}))
PYJSON
)"
probe_response="$(curl -sS --max-time 10 -X POST -H "Authorization: Bearer ${PULSE_TOKEN}"   -H 'Content-Type: application/json' -d "$probe_payload"   "${PULSE_URL}/api/items/" -w $'
%{http_code}')" || {
  echo "::error::Pulse item-create permission probe transport failure"
  exit 1
}
probe_code="${probe_response##*$'
'}"
probe_body="${probe_response%$'
'*}"
if [[ "$probe_code" == "201" ]]; then
  probe_ref="$(printf '%s' "$probe_body" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("item_key") or d["id"])')" || {
    echo "::error::Pulse item-create permission probe did not return an item reference"
    exit 1
  }
  delete_response="$(curl -sS --max-time 10 -X DELETE -H "Authorization: Bearer ${PULSE_TOKEN}" \
    "${PULSE_URL}/api/items/${probe_ref}/" -w $'\n%{http_code}')" || {
    echo "::error::Pulse item-create permission probe cleanup transport failure"
    exit 1
  }
  delete_code="${delete_response##*$'\n'}"
  if [[ "$delete_code" != "204" ]]; then
    delete_body="${delete_response%$'\n'*}"
    echo "::error::Pulse item-create permission probe cleanup failed (HTTP ${delete_code}): ${delete_body:0:500}"
    exit 1
  fi
  echo "Pulse token: HIVE item-create permission valid (probe item created and deleted)"
elif [[ "$probe_code" == "401" || "$probe_code" == "403" || "$probe_code" == "400" ]] || grep -qi 'permission to create items\|not have permission\|forbidden' <<<"$probe_body"; then
  echo "::error::Pulse token lacks HIVE item-create permission (HTTP ${probe_code}): ${probe_body:0:500}"
  echo "::error::Fix: update origin/pulse-api-token to a token allowed to create HIVE autonomous-bug items"
  exit 1
else
  echo "::error::Unexpected Pulse item-create permission probe response HTTP ${probe_code}: ${probe_body:0:500}"
  exit 1
fi
echo "::endgroup::"

now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
bad_short="${BAD_SHA:0:7}"
restored_short="${RESTORED_SHA:0:7}"
summary="Hive ${RING} rollback ${bad_short} -> ${restored_short}"
alert_payload="$(python3 - "$RING" "$BAD_SHA" "$RESTORED_SHA" "$EVIDENCE" "$now" <<'PY'
import json,sys
ring,bad,restored,evidence,now=sys.argv[1:]
print(json.dumps([{
  "labels":{"alertname":"HiveReleaseRollback","severity":"critical","team":"engineering",
            "owner_group":"engineering","service":"shizuha-hive","release_ring":ring,
            "bad_sha":bad,"restored_sha":restored},
  "annotations":{"summary":f"Hive {ring} release rolled back",
                 "description":f"bad_sha={bad} restored_sha={restored} gate={evidence} Pulse=PLAT-4086",
                 "owner_group":"engineering",
                 "runbook_url":"https://wiki.shizuha.com/2ed20295-de16-4d3a-a095-6f77de666f09"},
  "startsAt":now
}]))
PY
)"
rc=0
# HIVE-2819 (D2): the owner alert is part of the same delivery contract —
# retry transient transport failures before declaring it lost.
if ! curl -fsS --retry 3 --retry-all-errors --retry-delay 5 --retry-max-time 90 \
    --connect-timeout 5 --max-time 30 -H 'Content-Type: application/json' -d "$alert_payload" \
    "${ALERTMANAGER_URL}/api/v2/alerts" >/dev/null; then
  echo "::warning::failed to send Hive rollback owner alert (alertmanager transport) — page the owning on-call lead manually if unacknowledged"
  rc=1
fi

# PLAT-8261: walk a freshly-created rollback record to its terminal state.
# The autonomous-bug workflow has no todo->done edge, so resolve the live
# transition graph and follow the shortest path to a terminal slug, posting
# the embedded evidence as the close comment. Every step is best-effort: any
# failure returns nonzero and the caller leaves the record todo (logged).
close_rollback_record() {
  local item_id="$1" evidence="$2"
  local current transitions target tid path_step
  current="$(pulse_call GET "${PULSE_URL}/api/items/${item_id}/" '' )" || return 1
  current="$(printf '%s' "$current" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status") or "")')" || return 1
  local guard=0
  while [[ "$guard" -lt 6 ]]; do
    guard=$((guard + 1))
    case "$current" in
      done|closed|resolved|completed) return 0 ;;
    esac
    transitions="$(pulse_call GET "${PULSE_URL}/api/items/${item_id}/available-transitions/" '')" || return 1
    # HIVE-2746: the fallback used to take ANY transition whose target slug
    # was not "todo". From `todo` the autonomous-bug graph has no direct
    # terminal edge, so the walk fired the first non-todo transition —
    # frequently "Escalate to Leads" (blocked) — which Pulse rejected with
    # missing_evidence_schema and auto-created an URGENT schema-repair
    # blocker child per rollback (HIVE-2744 -> HIVE-2745). Escalations are
    # CEO-only by that transition's own usage article and are never the
    # record-walk's business. The fallback is now a whitelist of the
    # linear implementation path (todo->in_progress->in_review), and
    # blocked/cancelled targets are never eligible.
    target="$(printf '%s' "$transitions" | python3 -c '
import json,sys
terminal = {"done", "closed", "resolved", "completed"}
FORBIDDEN = {"blocked", "cancelled"}
# Linear-path preferences per current status: the only legal moves.
ALLOWED = {
    "todo": {"in_progress"},
    "open": {"in_progress"},
    "in_progress": {"in_review"},
}
current = (sys.argv[1] if len(sys.argv) > 1 else "") or ""
d = json.load(sys.stdin)
rows = d.get("transitions") or d.get("results") or (d if isinstance(d, list) else [])
best = None
for row in rows:
    slug = str(row.get("target_status") or row.get("to_status") or "")
    if slug in terminal:
        best = row
        break
if best is None:
    allowed = ALLOWED.get(current, set())
    for row in rows:
        slug = str(row.get("target_status") or row.get("to_status") or "")
        if slug and slug in allowed:
            best = row
            break
    if best is None:
        # Nothing linear eligible: leave the record as-is (fail-closed to
        # the manual path) instead of firing a side-effect transition.
        sys.exit(3)
slug = str(best.get("target_status") or best.get("to_status") or "")
if slug in FORBIDDEN:
    sys.exit(3)
print(json.dumps({"id": best.get("id") or "", "slug": slug}))
' "$current")" || return 1
    tid="$(printf '%s' "$target" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
    path_step="$(printf '%s' "$target" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])')"
    [[ -n "$tid" ]] || return 1
    local comment_flag=""
    if [[ "$path_step" == "done" || "$path_step" == "closed" || "$path_step" == "resolved" || "$path_step" == "completed" ]]; then
      comment_flag=",\"comment\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "Auto-rollback record closed by the filer (PLAT-8261): the rollback this record describes already executed successfully at filing time. Evidence: ${evidence}")"
    fi
    pulse_call POST "${PULSE_URL}/api/items/${item_id}/execute-transition/" \
      "{\"transition_id\":\"${tid}\"${comment_flag}}" >/dev/null || return 1
    current="$path_step"
  done
  [[ "$current" == "done" || "$current" == "closed" || "$current" == "resolved" || "$current" == "completed" ]]
}

pulse_call() {
  local method="$1" url="$2" payload="${3:-}" response code body
  # HIVE-2819 (D2): run 3404 lost the rollback-artifact POST to a single
  # curl 28 at 20s/0 bytes AFTER auth validation had passed seconds earlier —
  # one transient hang on a converged-healthy rollback. Bound-retry every
  # Pulse call (3 attempts, 5s delay, 120s total window) and raise the
  # per-attempt budget so a slow-but-alive Pulse is not read as dead.
  local args=(-sS --retry 3 --retry-all-errors --retry-delay 5 --retry-max-time 120 \
    --connect-timeout 5 --max-time 45 -X "$method" -H "Authorization: Bearer ${PULSE_TOKEN}" -H 'Content-Type: application/json')
  [[ -z "$payload" ]] || args+=(-d "$payload")
  response="$(curl "${args[@]}" -w $'\n%{http_code}' "$url")" || {
    echo "::error::Pulse ${method} ${url} transport failure" >&2
    return 1
  }
  code="${response##*$'\n'}"
  body="${response%$'\n'*}"
  if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
    echo "::error::Pulse ${method} ${url} returned HTTP ${code}: ${body:0:1000}" >&2
    return 1
  fi
  printf '%s' "$body"
}

query="$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "[ROLLBACK][hive][${bad_short}]")"
if ! search="$(pulse_call GET "${PULSE_URL}/api/items/?search=${query}&status=needs_triage,open,in_progress,in_review,awaiting_merge,verification,blocked")"; then
  echo "::error::failed to search Pulse for an existing rollback artifact"
  exit 1
fi
# Only dedupe against a correctly routed artifact. A task accidentally created by
# an older fail-open payload must not suppress the canonical HIVE bug.
existing_fields="$(printf '%s' "$search" | python3 -c '
import json,sys
d=json.load(sys.stdin); rows=d.get("results",d if isinstance(d,list) else [])
for row in rows:
    if row.get("project_key") == "HIVE" and row.get("workflow_name") == "autonomous-bug" and row.get("assignment_group") == "engineering":
        ref=row.get("item_key") or row.get("id", "")
        row_id=row.get("id") or ref
        print(f"{ref}\t{row_id}"); break
')"
IFS=$'\t' read -r existing existing_id <<<"$existing_fields"
body="bad_sha=${BAD_SHA}\nrestored_sha=${RESTORED_SHA}\nring=${RING}\ngate_evidence=${EVIDENCE}\nrecorded_at=${now}\nsource_task=PLAT-4086"
if [[ -n "$existing" ]]; then
  payload="$(python3 -c 'import json,sys; print(json.dumps({"item":sys.argv[1],"content":sys.argv[2]}))' "$existing" "$body")"
  if ! response="$(pulse_call POST "${PULSE_URL}/api/comments/" "$payload")"; then
    echo "::error::failed to update the deduped Pulse rollback artifact"
    rc=1
  elif ! printf '%s' "$response" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert str(d.get("item")) in {sys.argv[1],sys.argv[2]} and d.get("content"), d' "$existing" "$existing_id"; then
    echo "::error::Pulse comment response did not confirm the target item/content"
    rc=1
  fi
  echo "updated rollback task id=${existing}"
else
  if ! projects="$(pulse_call GET "${PULSE_URL}/api/projects/?key=HIVE")"; then
    echo "::error::failed to resolve the HIVE project"
    exit 1
  fi
  project_id="$(printf '%s' "$projects" | python3 -c '
import json,sys
d=json.load(sys.stdin); rows=d.get("results",d if isinstance(d,list) else [])
matches=[r for r in rows if r.get("key") == "HIVE"]
assert len(matches) == 1, matches
print(matches[0]["id"])
')" || { echo "::error::Pulse did not return exactly one HIVE project"; exit 1; }
  payload="$(python3 - "$bad_short" "$summary" "$body" "$project_id" "$RING" <<'PY'
import json,sys
short,summary,body,project_id,ring=sys.argv[1:]
print(json.dumps({"title":f"[ROLLBACK][hive][{short}] {summary}","description":body,
                  "mode":"task","priority":"urgent","project":int(project_id),
                  "workflow_name":"autonomous-bug","assignment_group":"engineering",
                  "metadata":{"emitter":"record-rollout-failure.sh","emitter_ring":ring,
                              "principal_note":"filed by the hive CI automation via origin/pulse-api-token; reporter identity on this record is the token principal, not a human filer"},
                  "labels":{"release-regression":"true","auto-rollback":"true","hive":"true"}}))
PY
)"
  if response="$(pulse_call POST "${PULSE_URL}/api/items/" "$payload")"; then
    created_id="$(printf '%s' "$response" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d.get("id"), d
assert d.get("project_key") == "HIVE", d
assert d.get("workflow_name") == "autonomous-bug", d
assert d.get("assignment_group") == "engineering", d
print(d["id"])
')" || rc=1
    echo "created rollback task id=${created_id}"
    # PLAT-8261: the record describes an ALREADY-EXECUTED rollback (rc=0 is in
    # the evidence) — filing it as urgent/todo pollutes the engineering queue
    # with unactionable work (8 stale instances accumulated by 2026-09-09).
    # Walk the fresh record to its terminal state in the same run. Fail-open:
    # a failed walk leaves the item todo and logs loudly (the old behavior) —
    # the rollback itself already ran and this bookkeeping must never flip the
    # script's exit code.
    if [[ -n "${created_id:-}" ]]; then
      close_rollback_record "$created_id" "$body" || \
        echo "::warning::rollback record ${created_id} left in todo (terminal-state walk failed) — close it manually"
    fi
  else
    # HIVE-2819 (D2): post-rollback paperwork must not read as a rollback
    # failure. The record can be filed manually from the evidence in this
    # log; callers (ring-release-*.sh, ring_executor recover) downgrade
    # this rc to a warning.
    echo "::warning::failed to create the Pulse rollback artifact (transport/HTTP) — file it manually: ring=${RING} bad_sha=${BAD_SHA} restored_sha=${RESTORED_SHA} evidence=${EVIDENCE:0:800}"
    rc=1
  fi
fi
exit "$rc"
