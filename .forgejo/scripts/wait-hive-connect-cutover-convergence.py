#!/usr/bin/env python3
"""Wait until the ordinary Hive Service contains only listener-enforcing pods."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any


ENFORCEMENT_ENV = "HIVE_CONNECT_ENFORCE_DEDICATED_LISTENER"


def _ready(pod: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in pod.get("status", {}).get("conditions", [])
    )


def _container(pod: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next(
        (
            container
            for container in pod.get("spec", {}).get("containers", [])
            if container.get("name") == name
        ),
        None,
    )


def current_template_hash(
    deployment: dict[str, Any],
    rs_items: dict[str, Any],
) -> str:
    """Pod-template-hash of the current-revision ReplicaSet, or "" if ambiguous.

    HIVE-2927: an old-revision ReplicaSet can retain a live surge pod with the
    same image as the current ring (break-glass hand-promote class). Image
    equality cannot distinguish such an orphan from a settled current pod, so
    the typed discriminator is the owner ReplicaSet's revision annotation
    versus the Deployment's revision annotation.
    """
    current_rev = str(
        (deployment.get("metadata", {}).get("annotations") or {}).get(
            "deployment.kubernetes.io/revision"
        )
        or ""
    ).strip()
    if not current_rev:
        return ""
    matches = [
        rs
        for rs in rs_items.get("items", [])
        if str(
            (rs.get("metadata", {}).get("annotations") or {}).get(
                "deployment.kubernetes.io/revision"
            )
            or ""
        ).strip()
        == current_rev
    ]
    if len(matches) != 1:
        return ""
    return str(
        matches[0].get("metadata", {}).get("labels", {}).get("pod-template-hash")
        or ""
    ).strip()


def orphaned_image_mismatch_pods(
    deployment: dict[str, Any],
    pods: dict[str, Any],
    *,
    container_name: str = "hive",
    template_hash: str = "",
) -> list[str]:
    """Names of live old-image ReplicaSet pods blocking a settled cutover.

    HIVE-2567: once ``kubectl rollout status`` has reported success, any
    ReplicaSet-owned pod still running the previous image without a
    deletionTimestamp is an orphan the ReplicaSet controller failed to reap
    (stale owner reference, deleted owner RS, ...).  Such a pod fails the
    strict image gate forever and auto-rolls back every subsequent healthy
    release.  The caller may safely delete it: the Deployment is already
    settled on the new image and the gate re-verifies the full end state
    afterwards.  Returns [] unless the Deployment itself is settled, so an
    active rollout is never disturbed.
    """
    desired = int(deployment.get("spec", {}).get("replicas") or 0)
    status = deployment.get("status", {})
    generation = int(deployment.get("metadata", {}).get("generation") or 0)
    observed = int(status.get("observedGeneration") or 0)
    counts = (
        int(status.get("updatedReplicas") or 0),
        int(status.get("readyReplicas") or 0),
        int(status.get("availableReplicas") or 0),
    )
    if desired < 1 or observed != generation or any(value != desired for value in counts):
        return []

    expected_container = _container(
        deployment.get("spec", {}).get("template", {}), container_name
    )
    if expected_container is None:
        return []
    expected_image = expected_container.get("image")

    orphans: list[str] = []
    for pod in pods.get("items", []):
        # Completed Pods may remain owned by old ReplicaSets for history. They
        # cannot serve or satisfy replicas; EndpointSlice equality below still
        # refuses any terminal Pod that remains a Service traffic target.
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        owners = pod.get("metadata", {}).get("ownerReferences") or []
        if not any(owner.get("kind") == "ReplicaSet" for owner in owners):
            continue
        if pod.get("metadata", {}).get("deletionTimestamp"):
            continue
        container = _container(pod, container_name)
        if container is None:
            continue
        image_mismatch = container.get("image") != expected_image
        revision_mismatch = bool(template_hash) and str(
            pod.get("metadata", {}).get("labels", {}).get("pod-template-hash") or ""
        ).strip() != template_hash
        if image_mismatch or revision_mismatch:
            name = str(pod.get("metadata", {}).get("name") or "")
            if name:
                orphans.append(name)
    return orphans


def evaluate_snapshot(
    deployment: dict[str, Any],
    pods: dict[str, Any],
    endpoint_slices: dict[str, Any],
    *,
    container_name: str = "hive",
    template_hash: str = "",
) -> tuple[bool, str]:
    desired = int(deployment.get("spec", {}).get("replicas") or 0)
    status = deployment.get("status", {})
    generation = int(deployment.get("metadata", {}).get("generation") or 0)
    observed = int(status.get("observedGeneration") or 0)
    counts = {
        "updated": int(status.get("updatedReplicas") or 0),
        "ready": int(status.get("readyReplicas") or 0),
        "available": int(status.get("availableReplicas") or 0),
    }
    if desired < 1:
        return False, f"invalid desired replicas={desired}"
    if observed != generation or any(value != desired for value in counts.values()):
        return (
            False,
            f"Deployment unsettled generation={observed}/{generation} "
            f"desired={desired} counts={counts}",
        )

    expected_container = _container(deployment.get("spec", {}).get("template", {}), container_name)
    if expected_container is None:
        return False, f"Deployment container {container_name!r} is absent"
    expected_image = expected_container.get("image")

    exact_pods: dict[str, dict[str, Any]] = {}
    for pod in pods.get("items", []):
        # Completed Pods may remain owned by old ReplicaSets for history. They
        # cannot serve or satisfy replicas; EndpointSlice equality below still
        # refuses any terminal Pod that remains a Service traffic target.
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        owners = pod.get("metadata", {}).get("ownerReferences") or []
        if not any(owner.get("kind") == "ReplicaSet" for owner in owners):
            continue
        name = str(pod.get("metadata", {}).get("name") or "")
        if pod.get("metadata", {}).get("deletionTimestamp"):
            return False, f"old ReplicaSet pod still terminating: {name}"
        container = _container(pod, container_name)
        if container is None:
            return False, f"pod {name} lacks container {container_name}"
        if template_hash and str(
            pod.get("metadata", {}).get("labels", {}).get("pod-template-hash") or ""
        ).strip() != template_hash:
            return False, f"old-revision ReplicaSet pod still live: {name}"
        env = {
            item.get("name"): item.get("value")
            for item in container.get("env", [])
            if "value" in item
        }
        if env.get(ENFORCEMENT_ENV) != "true":
            return False, f"pod {name} does not enforce the dedicated listener"
        if container.get("image") != expected_image:
            return False, f"pod {name} image does not match the Deployment"
        if not _ready(pod):
            return False, f"pod {name} is not Ready"
        exact_pods[name] = pod

    if len(exact_pods) != desired:
        return False, f"exact enforcing pods={len(exact_pods)} desired={desired}"

    endpoints: dict[str, dict[str, Any]] = {}
    for endpoint_slice in endpoint_slices.get("items", []):
        for endpoint in endpoint_slice.get("endpoints", []):
            target = endpoint.get("targetRef") or {}
            if target.get("kind") != "Pod" or not target.get("name"):
                return False, "Service EndpointSlice contains a non-Pod target"
            name = str(target["name"])
            conditions = endpoint.get("conditions") or {}
            if conditions.get("terminating") is True:
                return False, f"Service still contains terminating endpoint {name}"
            if conditions.get("ready") is not True:
                return False, f"Service endpoint {name} is not Ready"
            endpoints[name] = endpoint

    if set(endpoints) != set(exact_pods):
        return (
            False,
            "Service endpoints do not equal exact enforcing pods: "
            f"endpoints={sorted(endpoints)} pods={sorted(exact_pods)}",
        )
    return True, f"desired={desired} exact enforcing pods and endpoints converged"


def _kubectl_json(*args: str) -> dict[str, Any]:
    return json.loads(
        subprocess.check_output(
            ["kubectl", *args, "-o", "json"],
            text=True,
            timeout=20,
        )
    )


def _selector(deployment: dict[str, Any]) -> str:
    labels = deployment.get("spec", {}).get("selector", {}).get("matchLabels") or {}
    if not labels:
        raise ValueError("Deployment has no matchLabels selector")
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def wait_for_convergence(
    namespace: str,
    deployment_name: str,
    service_name: str,
    *,
    timeout_seconds: int,
    interval_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    last_reason = "no observation"
    reason_history: list[str] = []
    # HIVE-2567: bounded orphan reconciliation. Each pod name is delete-
    # attempted at most once per wait, and at most max_reconciles pods are
    # touched, so a pathological cluster state can never turn this gate into
    # a delete loop. Deletion failures keep the fail-closed hold unchanged.
    reconciled: set[str] = set()
    max_reconciles = 3
    while time.monotonic() < deadline:
        attempt += 1
        try:
            deployment = _kubectl_json(
                "-n", namespace, "get", "deployment", deployment_name
            )
            pods = _kubectl_json(
                "-n", namespace, "get", "pods", "-l", _selector(deployment)
            )
            rs_items = _kubectl_json(
                "-n", namespace, "get", "rs", "-l", _selector(deployment)
            )
            template_hash = current_template_hash(deployment, rs_items)
            endpoint_slices = _kubectl_json(
                "-n",
                namespace,
                "get",
                "endpointslices.discovery.k8s.io",
                "-l",
                f"kubernetes.io/service-name={service_name}",
            )
            for orphan in orphaned_image_mismatch_pods(
                deployment, pods, template_hash=template_hash
            ):
                if orphan in reconciled or len(reconciled) >= max_reconciles:
                    continue
                reconciled.add(orphan)
                print(
                    f"Hive Connect cutover convergence reconcile: deleting "
                    f"orphaned old-image pod {orphan} (Deployment settled "
                    f"without it; HIVE-2567)",
                    file=sys.stderr,
                )
                try:
                    subprocess.run(
                        [
                            "kubectl",
                            "-n",
                            namespace,
                            "delete",
                            "pod",
                            orphan,
                            "--wait=false",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    detail = ""
                    stderr = getattr(exc, "stderr", None)
                    if stderr and stderr.strip():
                        detail = ": " + " ".join(stderr.strip().splitlines()[-1].split())
                    print(
                        f"::warning::orphan pod {orphan} delete failed "
                        f"({type(exc).__name__}{detail}); holding fail-closed "
                        f"(needs pods/delete for this identity — HIVE-2567)",
                        file=sys.stderr,
                    )
            converged, last_reason = evaluate_snapshot(
                deployment, pods, endpoint_slices, template_hash=template_hash
            )
            if converged:
                print(
                    "Hive Connect cutover convergence PASS "
                    f"attempt={attempt} {last_reason}"
                )
                return
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            last_reason = f"observation failed: {type(exc).__name__}: {exc}"
        reason_history.append(last_reason)
        print(
            f"Hive Connect cutover convergence HOLD attempt={attempt}: {last_reason}",
            file=sys.stderr,
        )
        time.sleep(interval_seconds)
    headline, detail = _classify_failure(reason_history)
    raise TimeoutError(
        f"Hive Connect cutover did not converge in {timeout_seconds}s "
        f"after {len(reason_history)} attempts — {headline}: {detail}"
    )


def _classify_failure(reasons: list[str]) -> tuple[str, str]:
    """Turn the per-attempt reason history into a self-diagnosing outcome.

    HIVE-2579 fence-build follow-up: a bare timer death forced forensic
    reconstruction (window exhausted vs config violation vs unobservable
    state). The reason history already distinguishes the three shapes —
    surface the classification instead of the raw last line alone.
    """
    if not reasons:
        return "no observation was made", "the poll loop never completed one attempt"
    if all(reason.startswith("observation failed:") for reason in reasons):
        return (
            "pod states unobservable — every poll failed to observe",
            "kubectl observation failed on all attempts (node pressure / API timeouts?)",
        )
    listener = [r for r in reasons if "does not enforce the dedicated listener" in r]
    if listener:
        return (
            "pod Ready but dedicated listener not enforced (observed)",
            listener[-1],
        )
    not_ready = [
        r
        for r in reasons
        if (
            "is not Ready" in r
            or "unsettled" in r
            or "still terminating" in r
            or "still live" in r
            or "lacks container" in r
            or "image does not match" in r
        )
    ]
    if not_ready:
        return (
            "pod(s) not Ready/converged within the window "
            "(pod-start starvation under node pressure?)",
            not_ready[-1],
        )
    return "convergence not observed", reasons[-1]


class _StderrTee:
    """Write-through stderr proxy: real stderr first, fixture file second."""

    def __init__(self, real, fixture):
        self._real = real
        self._fixture = fixture

    def write(self, text):
        self._real.write(text)
        self._real.flush()
        try:
            self._fixture.write(text)
            self._fixture.flush()
        except OSError:
            pass

    def flush(self):
        self._real.flush()


def main() -> int:
    # HIVE-2927: mirror stderr into the rollback fixture file so the delivered
    # evidence names the first failing check instead of an empty fixture_stderr.
    # Best-effort: capture failure must never delay fail-closed rollback.
    fixture_path = "/tmp/h725-fixture.err"
    try:
        fixture = open(fixture_path, "a")
        sys.stderr = _StderrTee(sys.stderr, fixture)
    except OSError:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="shizuha-hive")
    parser.add_argument("--deployment", default="hive")
    parser.add_argument("--service", default="hive")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    try:
        wait_for_convergence(
            args.namespace,
            args.deployment,
            args.service,
            timeout_seconds=max(args.timeout, 1),
            interval_seconds=max(args.interval, 0.1),
        )
    except TimeoutError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
