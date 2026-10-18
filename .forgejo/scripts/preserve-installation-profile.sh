#!/usr/bin/env bash
# Preserve only the reviewed installation-owned fields, never Secret values.
set -euo pipefail
# HIVE-2927: mirror stderr into the rollback fixture file (append; the file is
# tailed bounded at evidence time). Best-effort — never block the gate.
if { true 2>>/tmp/h725-fixture.err; } 2>/dev/null; then
  exec 2> >(tee -a /tmp/h725-fixture.err >&2)
fi
KUBECTL="${KUBECTL:-.forgejo/scripts/kubectl}"
NAMESPACE=shizuha-hive
PROFILE=hive-installation-config

PROFILE_UID="$("$KUBECTL" -n "$NAMESPACE" get secret "$PROFILE" \
  --ignore-not-found -o 'jsonpath={.metadata.uid}')"
if [ -z "$PROFILE_UID" ]; then
  echo 'Hive installation profile is absent; existing environment is unchanged.' >&2
  exit 0
fi

# Render key names only, never values; a partial profile must fail before
# changing either Deployment into an unstartable required-Secret reference.
PROFILE_KEYS="$("$KUBECTL" -n "$NAMESPACE" get secret "$PROFILE" \
  -o 'go-template={{range $key, $value := .data}}{{printf "%s\n" $key}}{{end}}' | tr '\n' ' ')"
for key in HIVE_RUNTIME_HOST_CLI HIVE_RUNTIME_BASE_IMAGE HIVE_RUNTIME_BROKER_IMAGE \
  HIVE_RUNTIME_AGENT_NODE_SELECTOR HIVE_RUNTIME_DAEMON_NODE_SELECTOR \
  HIVE_HOSTPLANE_SSH_TAILNET_CIDRS HIVE_HOSTPLANE_SSH_BOOTSTRAP_CIDRS \
  SHIZUHA_PULSE_HUMAN_EMAILS; do
  case " $PROFILE_KEYS " in
    *" $key "*) ;;
    *) echo "::error::installation profile is missing required key $key" >&2; exit 1 ;;
  esac
done

# Check both known owners before changing either. A renamed/missing owner is
# an error, not permission to synthesize a container in an unrelated workload.
for owner in hive hive-api; do
  names="$("$KUBECTL" -n "$NAMESPACE" get deploy "$owner" \
    -o 'jsonpath={.spec.template.spec.containers[*].name}')"
  found=0
  for container in $names; do
    [ "$container" != "$owner" ] || found=1
  done
  [ "$found" -eq 1 ] || {
    echo "::error::installation profile owner deploy/$owner has no expected container" >&2
    exit 1
  }
done

TEMPORARY="$(mktemp -d)"
trap 'rm -rf "$TEMPORARY"' EXIT
for owner in hive hive-api; do
  python3 - "$owner" "$PROFILE" >"$TEMPORARY/patch.json" <<'PY'
import json
import sys
owner, profile = sys.argv[1:]
names = (
    'HIVE_RUNTIME_HOST_CLI', 'HIVE_RUNTIME_BASE_IMAGE', 'HIVE_RUNTIME_BROKER_IMAGE',
    'HIVE_RUNTIME_AGENT_NODE_SELECTOR', 'HIVE_RUNTIME_DAEMON_NODE_SELECTOR',
    'HIVE_HOSTPLANE_SSH_TAILNET_CIDRS', 'HIVE_HOSTPLANE_SSH_BOOTSTRAP_CIDRS',
    'SHIZUHA_PULSE_HUMAN_EMAILS',
)
entries = [{
    'name': name,
    'value': None,
    'valueFrom': {'configMapKeyRef': None, 'fieldRef': None,
                  'resourceFieldRef': None, 'secretKeyRef': {
        'name': profile, 'key': name, 'optional': False,
    }},
} for name in names]
print(json.dumps({'spec': {'template': {'spec': {'containers': [{
    'name': owner, 'env': entries,
}]}}}}))
PY
  "$KUBECTL" -n "$NAMESPACE" patch "deploy/$owner" --type=strategic \
    --patch-file="$TEMPORARY/patch.json" >&2
done
