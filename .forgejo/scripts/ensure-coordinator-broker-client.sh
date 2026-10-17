#!/bin/bash
set -euo pipefail
exec python3 "$(dirname "$0")/ensure-coordinator-broker-client.py" "$@"
