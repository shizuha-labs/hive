#!/usr/bin/env sh
# Origin-CI-only, create-once credential for Pulse -> Hive company-state reads.
# Preserve either existing side, fill a missing namespace, and refuse mismatch;
# a deploy must never rotate a working service credential implicitly.
set -eu

producer_namespace="${1:?producer namespace required}"
consumer_namespace="${2:?consumer namespace required}"
secret_name="hive-company-state-read"
secret_key="token"

read_secret() {
  namespace="$1"
  kubectl -n "$namespace" get secret "$secret_name" \
    -o "jsonpath={.data.${secret_key}}" 2>/dev/null | base64 -d
}

producer_token="$(read_secret "$producer_namespace" || true)"
consumer_token="$(read_secret "$consumer_namespace" || true)"

if [ -n "$producer_token" ] && [ -n "$consumer_token" ] && \
   [ "$producer_token" != "$consumer_token" ]; then
  echo "::error::$secret_name differs between $producer_namespace and $consumer_namespace; refusing implicit rotation"
  exit 1
fi

token="${producer_token:-$consumer_token}"
if [ -z "$token" ]; then
  token="$(openssl rand -hex 32)"
fi
[ "${#token}" -ge 32 ] || {
  echo "::error::$secret_name token is unexpectedly short"
  exit 1
}

for namespace in "$producer_namespace" "$consumer_namespace"; do
  kubectl -n "$namespace" create secret generic "$secret_name" \
    --from-literal="${secret_key}=${token}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

echo "$secret_name is synchronized for $producer_namespace -> $consumer_namespace"
