#!/usr/bin/env sh
# Origin-CI-only, create-once Ed25519 signer for HiveCapabilityEvidenceV2.
# Existing material is validated and retained; this script never rotates it.
set -eu

namespace="${1:?namespace required}"
secret="hive-capability-evidence-signer"
kid="hive-cap-ed25519-v1"

keyring_from_private() {
  key_path="$1"
  public_raw="$(openssl pkey -in "$key_path" -pubout -outform DER 2>/dev/null | tail -c 32 | base64 | tr '+/' '-_' | tr -d '=\n')"
  test "${#public_raw}" -eq 43 || { echo "::error::failed to derive Ed25519 public key"; exit 1; }
  printf '{"keys":[{"current":true,"kid":"%s","public_key":"%s"}],"schema_version":1}' \
    "$kid" "$public_raw"
}

if kubectl -n "$namespace" get secret "$secret" >/dev/null 2>&1; then
  private="$(kubectl -n "$namespace" get secret "$secret" -o jsonpath='{.data.private-key\.pem}')"
  existing_kid="$(kubectl -n "$namespace" get secret "$secret" -o jsonpath='{.data.kid}' | base64 -d)"
  test -n "$private" || { echo "::error::$namespace/$secret missing private-key.pem"; exit 1; }
  test "$existing_kid" = "$kid" || {
    echo "::error::$namespace/$secret kid mismatch; refusing implicit key rotation"
    exit 1
  }
  key_file="$(mktemp)"
  trap 'rm -f "$key_file"' EXIT HUP INT TERM
  printf '%s' "$private" | base64 -d >"$key_file" || {
    echo "::error::$namespace/$secret private-key.pem is not valid base64"
    exit 1
  }
  openssl pkey -in "$key_file" -noout -check >/dev/null 2>&1 || {
    echo "::error::$namespace/$secret private-key.pem is not a valid private key"
    exit 1
  }
  openssl pkey -in "$key_file" -text -noout 2>/dev/null | grep -qi 'ED25519' || {
    echo "::error::$namespace/$secret private-key.pem is not Ed25519"
    exit 1
  }
  keyring="$(keyring_from_private "$key_file")"
  # Add/repair public verification material without rotating or rewriting the
  # retained private key. Runtime pods receive this one key read-only.
  # POSIX sh treats '{\"data\"...}' as literal backslashes; kubectl then
  # rejects the merge patch (`invalid character '\\'`). That aborted every
  # Hive canary since 2026-08-25 (48-commit fleet lag).
  encoded_keyring="$(printf '%s' "$keyring" | base64 | tr -d '\n')"
  patch_json="$(python3 -c 'import json,sys; print(json.dumps({"data":{"keyring.json": sys.argv[1]}}))' "$encoded_keyring")"
  printf '%s' "$patch_json" | python3 -c 'import json,sys; json.loads(sys.stdin.read())'
  printf '%s' "$patch_json" | kubectl -n "$namespace" patch secret "$secret" --type=merge --patch-file=/dev/stdin >/dev/null
  echo "$namespace/$secret already provisioned (retained)"
  exit 0
fi

umask 077
key_file="$(mktemp)"
trap 'rm -f "$key_file"' EXIT HUP INT TERM
openssl genpkey -algorithm Ed25519 -out "$key_file" >/dev/null 2>&1
keyring="$(keyring_from_private "$key_file")"
kubectl -n "$namespace" create secret generic "$secret" \
  --from-file=private-key.pem="$key_file" \
  --from-literal=kid="$kid" \
  --from-literal=keyring.json="$keyring" >/dev/null
echo "$namespace/$secret provisioned by Origin CI"
