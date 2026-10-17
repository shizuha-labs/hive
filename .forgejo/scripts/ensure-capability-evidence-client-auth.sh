#!/usr/bin/env sh
# Origin-CI-only, create-once service credential shared by Pulse and Hive.
# The same immutable value is materialized in each namespace; mismatches fail
# loud instead of silently selecting one side or rotating a working credential.
set -eu

producer_namespace="${1:?producer namespace required}"
consumer_namespace="${2:?consumer namespace required}"
secret="hive-capability-evidence-client-auth"

read_token() {
  namespace="$1"
  kubectl -n "$namespace" get secret "$secret" \
    -o jsonpath='{.data.token}' 2>/dev/null | base64 -d
}

producer_token="$(read_token "$producer_namespace" || true)"
consumer_token="$(read_token "$consumer_namespace" || true)"

if [ -n "$producer_token" ] && [ -n "$consumer_token" ] && \
   [ "$producer_token" != "$consumer_token" ]; then
  echo "::error::$secret differs between $producer_namespace and $consumer_namespace; refusing implicit rotation"
  exit 1
fi

token="${producer_token:-$consumer_token}"
if [ -z "$token" ]; then
  token="$(openssl rand -hex 32)"
fi
[ "${#token}" -ge 32 ] || {
  echo "::error::$secret token is unexpectedly short"
  exit 1
}

for namespace in "$producer_namespace" "$consumer_namespace"; do
  kubectl -n "$namespace" create secret generic "$secret" \
    --from-literal=token="$token" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

echo "$secret is synchronized for $producer_namespace -> $consumer_namespace"
