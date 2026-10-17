#!/usr/bin/env sh
# Origin-CI-only, create-once directional credential for Connect -> Hive.
#
# The producer and verifier live in different namespaces.  Preserve any
# existing value, copy it to the missing side, and fail loud on divergence;
# never rotate a working credential implicitly during a rollout.
set -eu

producer_namespace="${1:?producer namespace required}"
consumer_namespace="${2:?consumer namespace required}"
secret_name="hive-connect-hmac"
secret_key="secret"

read_secret() {
  namespace="$1"
  kubectl -n "$namespace" get secret "$secret_name" \
    -o "jsonpath={.data.${secret_key}}" 2>/dev/null | base64 -d
}

producer_secret="$(read_secret "$producer_namespace" || true)"
consumer_secret="$(read_secret "$consumer_namespace" || true)"

if [ -n "$producer_secret" ] && [ -n "$consumer_secret" ] && \
   [ "$producer_secret" != "$consumer_secret" ]; then
  echo "::error::$secret_name differs between $producer_namespace and $consumer_namespace; refusing implicit rotation"
  exit 1
fi

shared_secret="${producer_secret:-$consumer_secret}"
if [ -z "$shared_secret" ]; then
  shared_secret="$(openssl rand -hex 32)"
fi
[ "${#shared_secret}" -ge 32 ] || {
  echo "::error::$secret_name is unexpectedly short"
  exit 1
}

for namespace in "$producer_namespace" "$consumer_namespace"; do
  kubectl -n "$namespace" create secret generic "$secret_name" \
    --from-literal="${secret_key}=${shared_secret}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

echo "$secret_name is synchronized for $producer_namespace -> $consumer_namespace"
