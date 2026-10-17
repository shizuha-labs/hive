#!/usr/bin/env bash
# Keep protected-branch pushes on Hive's CI-identical serial gate.  The generic
# Django hook lacks Hive's token-coordinator KEK and shared-state isolation.
set -euo pipefail
exec "$(dirname "$0")/.ci-local-gate.sh"
