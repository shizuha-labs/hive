#!/usr/bin/env python3
"""Atomically prove the live release before advancing its rollback anchor."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any


SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _containers(deployment: dict[str, Any]) -> dict[str, tuple[int, dict[str, Any]]]:
    items = deployment["spec"]["template"]["spec"].get("containers", [])
    return {
        container.get("name", ""): (index, container)
        for index, container in enumerate(items)
    }


def assert_exact_release(
    deployment: dict[str, Any],
    sha: str,
    image: str,
    container_names: list[str],
    *,
    require_anchor: bool,
) -> None:
    metadata = deployment.get("metadata", {})
    spec = deployment.get("spec", {})
    status = deployment.get("status", {})
    template = spec.get("template", {})
    labels = template.get("metadata", {}).get("labels", {})
    replicas = int(spec.get("replicas", 0) or 0)

    failures: list[str] = []
    if labels.get("build_sha") != sha:
        failures.append(
            f"template build_sha={labels.get('build_sha')!r}, expected {sha!r}"
        )
    if metadata.get("labels", {}).get("build_sha") != sha:
        failures.append(
            "deployment build_sha="
            f"{metadata.get('labels', {}).get('build_sha')!r}, expected {sha!r}"
        )

    present = _containers(deployment)
    for name in container_names:
        if name not in present:
            failures.append(f"required container {name!r} is absent")
        elif present[name][1].get("image") != image:
            failures.append(
                f"container {name!r} image={present[name][1].get('image')!r}, "
                f"expected {image!r}"
            )

    if replicas < 1:
        failures.append(f"desired replicas={replicas}, expected at least 1")
    for field in ("updatedReplicas", "readyReplicas", "availableReplicas"):
        observed = int(status.get(field, 0) or 0)
        if observed != replicas:
            failures.append(f"status.{field}={observed}, expected {replicas}")
    if status.get("observedGeneration") != metadata.get("generation"):
        failures.append(
            "controller has not observed the current Deployment generation"
        )

    if require_anchor:
        annotations = metadata.get("annotations", {})
        for key in ("last-successful-sha", "shizuha.io/last-successful-sha"):
            if annotations.get(key) != sha:
                failures.append(
                    f"annotation {key}={annotations.get(key)!r}, expected {sha!r}"
                )

    if failures:
        raise ValueError("; ".join(failures))


def build_atomic_anchor_patch(
    deployment: dict[str, Any],
    sha: str,
    image: str,
    container_names: list[str],
) -> list[dict[str, Any]]:
    assert_exact_release(
        deployment, sha, image, container_names, require_anchor=False
    )
    metadata = deployment["metadata"]
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    present = _containers(deployment)
    patch: list[dict[str, Any]] = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": metadata["resourceVersion"],
        },
        {
            "op": "test",
            "path": "/metadata/labels/build_sha",
            "value": sha,
        },
        {
            "op": "test",
            "path": "/spec/template/metadata/labels/build_sha",
            "value": labels["build_sha"],
        },
    ]
    for name in container_names:
        index, _container = present[name]
        patch.append(
            {
                "op": "test",
                "path": f"/spec/template/spec/containers/{index}/image",
                "value": image,
            }
        )
    patch.extend(
        [
            {
                "op": "add",
                "path": "/metadata/annotations/last-successful-sha",
                "value": sha,
            },
            {
                "op": "add",
                "path": "/metadata/annotations/shizuha.io~1last-successful-sha",
                "value": sha,
            },
        ]
    )
    return patch


def kubectl_json(*args: str, input_text: str | None = None) -> dict[str, Any]:
    result = subprocess.run(
        ["kubectl", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"kubectl {' '.join(args)} failed with rc={result.returncode}: {detail}"
        )
    return json.loads(result.stdout)


def main(argv: list[str]) -> int:
    if len(argv) < 5:
        print(
            "usage: record-successful-release.py "
            "<namespace> <deployment> <sha> <image> <container> [<container> ...]",
            file=sys.stderr,
        )
        return 2
    namespace, deployment_name, sha, image, *container_names = argv
    if not SHA_RE.fullmatch(sha):
        print("release SHA must be exactly 40 lowercase hexadecimal characters", file=sys.stderr)
        return 2

    try:
        deployment = kubectl_json(
            "-n", namespace, "get", "deployment", deployment_name, "-o", "json"
        )
        patch = build_atomic_anchor_patch(
            deployment, sha, image, container_names
        )
        kubectl_json(
            "-n",
            namespace,
            "patch",
            "deployment",
            deployment_name,
            "--type=json",
            "--patch-file=/dev/stdin",
            "-o",
            "json",
            input_text=json.dumps(patch),
        )
        persisted = kubectl_json(
            "-n", namespace, "get", "deployment", deployment_name, "-o", "json"
        )
        assert_exact_release(
            persisted, sha, image, container_names, require_anchor=True
        )
    except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(
            f"::error::refusing to advance release anchor: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        f"release anchor advanced only after exact live-state proof: "
        f"{namespace}/{deployment_name} sha={sha}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
