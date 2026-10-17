#!/usr/bin/env sh
# Do not enable shell tracing around credential provisioning.
set -eu
exec python3 "$(dirname "$0")/ensure-pulse-fleet-read-client.py" "$@"
