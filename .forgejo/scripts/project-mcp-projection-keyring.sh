#!/usr/bin/env bash
# PLAT-5275: distribute only the capability signer's public keyring. The
# private-key-bearing source Secret stays confined to shizuha-hive.
set -euo pipefail

source_namespace="${1:?source namespace required}"
shift
source_secret="hive-capability-evidence-signer"
target_secret="hive-capability-evidence-signer-public"

keyring="$(kubectl -n "$source_namespace" get secret "$source_secret" \
  -o jsonpath='{.data.keyring\.json}')"
test -n "$keyring" || { echo "::error::$source_namespace/$source_secret missing keyring.json" >&2; exit 1; }

for namespace in "$@"; do
  test -n "$namespace" || continue
  kubectl get namespace "$namespace" >/dev/null
  kubectl -n "$namespace" create secret generic "$target_secret" \
    --from-literal=keyring.json="$(printf '%s' "$keyring" | base64 -d)" \
    --dry-run=client -o yaml | kubectl apply -f -
  # Prove no private material can cross this projection boundary.
  keys="$(kubectl -n "$namespace" get secret "$target_secret" -o json \
    | python3 -c 'import json,sys; print(",".join(sorted((json.load(sys.stdin).get("data") or {}).keys())))')"
  test "$keys" = keyring.json || { echo "::error::$namespace/$target_secret has unexpected keys: $keys" >&2; exit 1; }
done

# Isolated native-agent namespaces are dynamic. Project the same public-only
# Secret there; the Hive provisioner also repairs it on create/wake. A
# Forbidden on a fleet-* namespace is non-fatal (the provisioner repairs on
# create/wake); the static shizuha-fleet projection above is the hard gate.
for namespace in $(kubectl get namespaces -o json \
  | python3 -c 'import json,sys; print(" ".join(sorted(i["metadata"]["name"] for i in json.load(sys.stdin)["items"] if i["metadata"]["name"].startswith("fleet-"))))'); do
  if ! kubectl -n "$namespace" create secret generic "$target_secret" \
    --from-literal=keyring.json="$(printf '%s' "$keyring" | base64 -d)" \
    --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null; then
    echo "::warning::skipped public keyring projection to $namespace (Hive provisioner repairs on create/wake)"
    continue
  fi
done
