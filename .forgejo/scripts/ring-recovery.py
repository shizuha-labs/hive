#!/usr/bin/env python3
"""Typed evidence-first rollback for an owned recovery fence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

from ring_fence import Conflict, Fence, FenceError, Holder, Kubectl


def get(api: Kubectl, namespace: str, resource: str, name: str) -> dict[str, Any]:
    result = api.run(["-n", namespace, "get", resource, name, "-o", "json"])
    return json.loads(result.stdout)


def _delete_preconditioned_once(fence: Fence, namespace: str, resource: str, name: str) -> None:
    result = fence.api.run(["-n", namespace, "get", resource, name, "-o", "json"], check=False)
    if result.returncode and "NotFound" in result.stderr:
        return
    if result.returncode:
        raise FenceError(f"ambiguous absence for {resource}/{name}")
    target = json.loads(result.stdout)
    tests = fence.target_tests(target)
    tests.append({
        "op": "add", "path": "/metadata/annotations/shizuha.io~1delete-intent",
        "value": fence.holder.generation,
    })
    marked = fence.api.json_patch(namespace, resource, name, tests)
    # DeleteOptions resourceVersion precondition makes a successor claim reject
    # this already-authorized deletion at storage time.
    uid = marked["metadata"]["uid"]
    rv = str(marked["metadata"]["resourceVersion"])
    plural = {
        "deployment": "deployments", "service": "services",
        "probe.monitoring.coreos.com": "probes",
    }[resource]
    if resource == "probe.monitoring.coreos.com":
        uri = f"/apis/monitoring.coreos.com/v1/namespaces/{namespace}/{plural}/{name}"
    elif resource == "deployment":
        uri = f"/apis/apps/v1/namespaces/{namespace}/{plural}/{name}"
    else:
        uri = f"/api/v1/namespaces/{namespace}/{plural}/{name}"
    body = json.dumps({
        "apiVersion": "v1", "kind": "DeleteOptions",
        "preconditions": {"uid": uid, "resourceVersion": rv},
        "propagationPolicy": "Foreground",
    })
    fence.validate()
    fence.api.run(["delete", f"--raw={uri}", "-f", "-"], stdin=body)


def delete_preconditioned(fence: Fence, namespace: str, resource: str, name: str) -> None:
    # Deployment status updates legitimately advance resourceVersion while a
    # failed canary is converging. Re-read and retry the complete mark+delete
    # CAS; ownership and UID/resourceVersion preconditions remain fail-closed.
    for attempt in range(8):
        try:
            _delete_preconditioned_once(fence, namespace, resource, name)
            return
        except Conflict:
            if attempt == 7:
                raise
            time.sleep(0.1)


def rollback_canary(fence: Fence) -> None:
    sha7 = fence.holder.release_sha[:7]
    # Capture runs before any deletion and is independently bounded.
    subprocess.run([
        "/usr/bin/timeout", "30s", "/bin/bash",
        "/app/.forgejo/scripts/capture-ring-evidence.sh", "canary",
        "shizuha-hive", "hive-canary", "hive-canary",
        fence.holder.release_sha, os.environ.get("RING_RECOVERY_REASON", "owner_lease_expired"),
    ], check=False, env=os.environ)
    retired = subprocess.run([
        "/bin/bash", "/app/.forgejo/scripts/retire-canary-probe.sh",
    ], check=False, env=os.environ)
    if retired.returncode:
        raise FenceError("canary Probe retirement was not proven during recovery")
    delete_preconditioned(fence, "monitoring", "probe.monitoring.coreos.com", f"probe-shizuha-hive-canary-{sha7}")
    delete_preconditioned(fence, "shizuha-hive", "service", "hive-canary")
    delete_preconditioned(fence, "shizuha-hive", "deployment", "hive-canary")


def rollback_fleet(fence: Fence) -> None:
    subprocess.run([
        "/usr/bin/timeout", "30s", "/bin/bash",
        "/app/.forgejo/scripts/capture-ring-evidence.sh", "fleet",
        "shizuha-hive", "hive", "hive", fence.holder.release_sha,
        os.environ.get("RING_RECOVERY_REASON", "owner_lease_expired"),
    ], check=False, env=os.environ)
    restored = fence.holder.restored_sha
    image = f"localhost:30500/shizuha-hive:hive-{restored[:7]}"
    deployment = get(fence.api, "shizuha-hive", "deployment", "hive")
    ops = fence.target_tests(deployment)
    annotations = deployment["metadata"].get("annotations", {})
    labels = deployment["metadata"].get("labels", {})
    template_labels = deployment["spec"]["template"]["metadata"].get("labels", {})
    for key, value, collection_path, present in (
        ("build_sha", restored, "/metadata/labels/", labels),
        ("release_ring", "fleet", "/metadata/labels/", labels),
        ("last-successful-sha", restored, "/metadata/annotations/", annotations),
        ("shizuha.io/last-successful-sha", restored, "/metadata/annotations/", annotations),
        ("build_sha", restored, "/spec/template/metadata/labels/", template_labels),
        ("release_ring", "fleet", "/spec/template/metadata/labels/", template_labels),
    ):
        path = collection_path + key.replace("/", "~1")
        ops.append({"op": "replace" if key in present else "add", "path": path, "value": value})
    for index, container in enumerate(deployment["spec"]["template"]["spec"]["containers"]):
        if container.get("name") == "hive":
            ops.append({"op": "replace", "path": f"/spec/template/spec/containers/{index}/image", "value": image})
    fence.api.json_patch("shizuha-hive", "deployment", "hive", ops)

    # Fleet rollout is a multi-target transaction: the backend may already be
    # on the new image when a later codex-refresh mutation/adapter fails. Restore
    # every image-bearing target under the same recovery fence before declaring
    # the predecessor boundary recovered.
    refresh = get(fence.api, "shizuha-hive", "deployment", "hive-codex-refresh")
    refresh_ops = fence.target_tests(refresh)
    for index, container in enumerate(refresh["spec"]["template"]["spec"]["containers"]):
        if container.get("name") == "codex-refresh":
            refresh_ops.append({
                "op": "replace",
                "path": f"/spec/template/spec/containers/{index}/image",
                "value": image,
            })
    fence.api.json_patch(
        "shizuha-hive", "deployment", "hive-codex-refresh", refresh_ops,
    )
    api_deploy = get(fence.api, "shizuha-hive", "deployment", "hive-api")
    api_ops = fence.target_tests(api_deploy)
    for index, container in enumerate(api_deploy["spec"]["template"]["spec"]["containers"]):
        if container.get("name") == "hive-api":
            api_ops.append({
                "op": "replace",
                "path": f"/spec/template/spec/containers/{index}/image",
                "value": image,
            })
    fence.api.json_patch("shizuha-hive", "deployment", "hive-api", api_ops)
    fence.validate()
    fence.api.run(["-n", "shizuha-hive", "rollout", "status", "deployment/hive", "--timeout=1800s"])
    fence.validate()
    fence.api.run([
        "-n", "shizuha-hive", "rollout", "status",
        "deployment/hive-codex-refresh", "--timeout=600s",
    ])
    restored_live = get(fence.api, "shizuha-hive", "deployment", "hive")
    restored_refresh = get(
        fence.api, "shizuha-hive", "deployment", "hive-codex-refresh",
    )
    meta = restored_live["metadata"]
    if meta.get("annotations", {}).get("last-successful-sha") != restored:
        raise FenceError("rollback anchor read-back mismatch")
    if restored_live["spec"]["template"]["metadata"].get("labels", {}).get("build_sha") != restored:
        raise FenceError("rollback build label read-back mismatch")
    images = {
        container.get("name"): container.get("image")
        for container in restored_refresh["spec"]["template"]["spec"]["containers"]
    }
    if images.get("codex-refresh") != image:
        raise FenceError("rollback codex-refresh image read-back mismatch")
    restored_api = get(fence.api, "shizuha-hive", "deployment", "hive-api")
    api_images = {
        container.get("name"): container.get("image")
        for container in restored_api["spec"]["template"]["spec"]["containers"]
    }
    if api_images.get("hive-api") != image:
        raise FenceError("rollback hive-api image read-back mismatch")
    fence.api.run([
        "-n", "shizuha-hive", "rollout", "status",
        "deployment/hive-api", "--timeout=1800s",
    ])


def main() -> int:
    try:
        holder = Holder.from_env()
        fence = Fence(holder, Kubectl())
        fence.token = int(os.environ["RING_FENCE_TOKEN"])
        fence.validate({"rollback-started"})
        if holder.ring == "canary":
            rollback_canary(fence)
        else:
            rollback_fleet(fence)
        return 0
    except (FenceError, KeyError, ValueError) as exc:
        print(f"::error::fenced recovery failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
