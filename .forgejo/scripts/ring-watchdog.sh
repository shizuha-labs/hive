#!/usr/bin/env bash
set -euo pipefail
echo "::error::legacy runner watchdog is retired; use ring-executor.py submit" >&2
exit 64
