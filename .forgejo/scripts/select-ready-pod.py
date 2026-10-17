#!/usr/bin/env python3
"""Select one deterministic Ready pod that exposes the requested container."""

from __future__ import annotations

import json
import sys
from typing import Any


# kubectl exec through these nodes has produced apiserver→kubelet 502s
# that the HIVE-725 fixture treats as a release regression and rolls fleet
# back (HIVE-2202: 0acd129 rolled live then restored df5b536).
_UNRELIABLE_EXEC_NODES = frozenset({"i9-ws"})


def select_ready_pod(payload: dict[str, Any], container_name: str) -> str:
    preferred: list[str] = []
    fallback: list[str] = []
    for pod in payload.get("items", []):
        metadata = pod.get("metadata", {})
        status = pod.get("status", {})
        if metadata.get("deletionTimestamp") or status.get("phase") != "Running":
            continue
        conditions = {
            condition.get("type"): condition.get("status")
            for condition in status.get("conditions", [])
        }
        containers = {
            container.get("name"): container
            for container in status.get("containerStatuses", [])
        }
        name = metadata.get("name")
        if not (
            conditions.get("Ready") == "True"
            and containers.get(container_name, {}).get("ready") is True
            and name
        ):
            continue
        node = (pod.get("spec") or {}).get("nodeName") or ""
        if node in _UNRELIABLE_EXEC_NODES:
            fallback.append(name)
        else:
            preferred.append(name)
    candidates = preferred or fallback
    if not candidates:
        raise ValueError(
            f"no Running Ready pod exposes container {container_name!r}"
        )
    return sorted(candidates)[0]


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1]:
        print("usage: select-ready-pod.py CONTAINER", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
        print(select_ready_pod(payload, sys.argv[1]))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"select-ready-pod: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
