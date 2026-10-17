#!/usr/bin/env python3
"""Remove API-server-owned fields from a Kubernetes rollback snapshot."""

from __future__ import annotations

import json
import sys
from typing import Any


SERVER_OWNED_METADATA = {
    "creationTimestamp",
    "deletionGracePeriodSeconds",
    "deletionTimestamp",
    "generation",
    "managedFields",
    "resourceVersion",
    "selfLink",
    "uid",
}


def sanitize(resource: dict[str, Any]) -> dict[str, Any]:
    metadata = resource.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("resource lacks metadata")
    if not resource.get("apiVersion") or not resource.get("kind"):
        raise ValueError("resource lacks apiVersion or kind")
    for field in SERVER_OWNED_METADATA:
        metadata.pop(field, None)
    resource.pop("status", None)
    return resource


def main() -> int:
    try:
        resource = json.load(sys.stdin)
        if not isinstance(resource, dict):
            raise ValueError("snapshot must be a JSON object")
        json.dump(sanitize(resource), sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"sanitize-rollback-resource: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
