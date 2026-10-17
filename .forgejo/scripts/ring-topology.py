#!/usr/bin/env python3
"""Validate that a release ring has one stable, exact-build service topology.

The shell gate deliberately keeps Kubernetes reads separate from this parser so
the decision logic is deterministic and regression-testable. A snapshot is
STABLE only when the Deployment-owned exact-build Ready pods and ready
EndpointSlice targets describe the same desired set. Everything else is a
bounded convergence state; malformed/missing evidence is UNKNOWN.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def ready(pod: dict) -> bool:
    status = pod.get("status", {})
    conditions = status.get("conditions") or []
    return (
        status.get("phase") == "Running"
        and pod.get("metadata", {}).get("deletionTimestamp") is None
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in conditions
        )
    )


def labels_match(labels: dict, selector: dict) -> bool:
    return all(labels.get(key) == value for key, value in selector.items())


def result(outcome: str, reason: str, **extra: object) -> None:
    print(json.dumps({"outcome": outcome, "reason": reason, **extra}, sort_keys=True))


def main() -> int:
    if len(sys.argv) != 7:
        result(
            "UNKNOWN",
            "usage: deployment service replicasets pods endpointslices build_sha",
        )
        return 2

    try:
        deployment, service, replicasets, pods, slices = map(load, sys.argv[1:6])
        build_sha = sys.argv[6]
        desired = int(deployment.get("spec", {}).get("replicas", 0))
        if desired < 1:
            raise ValueError("Deployment desired replicas is not positive")

        metadata = deployment.get("metadata", {})
        deployment_uid = metadata.get("uid")
        if not deployment_uid:
            raise ValueError("Deployment UID is missing")
        status = deployment.get("status", {})
        generation = int(metadata.get("generation", 0))
        observed = int(status.get("observedGeneration", 0))
        template_labels = (
            deployment.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
        )
        if template_labels.get("build_sha") != build_sha:
            result("TOPOLOGY_CONVERGING", "Deployment template is not the requested build")
            return 0
        counts = {
            "available": int(status.get("availableReplicas") or 0),
            "ready": int(status.get("readyReplicas") or 0),
            "updated": int(status.get("updatedReplicas") or 0),
            "unavailable": int(status.get("unavailableReplicas") or 0),
        }
        if (
            observed != generation
            or any(counts[name] != desired for name in ("available", "ready", "updated"))
            or counts["unavailable"] != 0
        ):
            result(
                "TOPOLOGY_CONVERGING",
                f"Deployment status is not settled desired={desired} counts={counts} generation={generation}/{observed}",
            )
            return 0

        service_selector = service.get("spec", {}).get("selector") or {}
        if not service_selector:
            raise ValueError("Service selector is empty")

        owned_replicasets = {}
        for replicaset in replicasets.get("items", []):
            rs_meta = replicaset.get("metadata", {})
            rs_uid = rs_meta.get("uid")
            owners = rs_meta.get("ownerReferences") or []
            owned = any(
                owner.get("controller") is True
                and owner.get("kind") == "Deployment"
                and owner.get("uid") == deployment_uid
                for owner in owners
            )
            if owned:
                if not rs_uid:
                    raise ValueError("Deployment-owned ReplicaSet UID is missing")
                owned_replicasets[rs_uid] = {
                    "name": rs_meta.get("name"),
                    "uid": rs_uid,
                    "resourceVersion": rs_meta.get("resourceVersion"),
                }
        if not owned_replicasets:
            result("TOPOLOGY_CONVERGING", "Deployment has no owned ReplicaSet evidence")
            return 0

        selector_candidates = []
        ignored_collisions = []
        exact_ready = []
        for pod in pods.get("items", []):
            pod_meta = pod.get("metadata", {})
            pod_labels = pod.get("metadata", {}).get("labels") or {}
            if not labels_match(pod_labels, service_selector):
                continue
            selector_candidates.append(pod)
            owners = pod_meta.get("ownerReferences") or []
            controller = next(
                (owner for owner in owners if owner.get("controller") is True),
                None,
            )
            owned = bool(
                controller
                and controller.get("kind") == "ReplicaSet"
                and controller.get("uid") in owned_replicasets
            )
            exact_build = pod_labels.get("build_sha") == build_sha
            if owned and exact_build and ready(pod):
                exact_ready.append(pod)
            else:
                ignored_collisions.append(
                    {
                        "name": pod_meta.get("name"),
                        "uid": pod_meta.get("uid"),
                        "phase": pod.get("status", {}).get("phase"),
                        "controllerKind": (controller or {}).get("kind"),
                        "controllerUID": (controller or {}).get("uid"),
                        "owned": owned,
                        "exactBuild": exact_build,
                        "ready": ready(pod),
                        "deleting": pod_meta.get("deletionTimestamp") is not None,
                    }
                )

        if len(exact_ready) != desired:
            result(
                "TOPOLOGY_CONVERGING",
                f"Deployment-owned exact Ready pod set is not exact desired={desired} "
                f"selector_candidates={len(selector_candidates)} exact_ready={len(exact_ready)}",
                ignoredSelectorCollisions=ignored_collisions,
            )
            return 0

        pod_by_uid = {}
        pod_by_ip = {}
        for pod in exact_ready:
            pod_meta = pod.get("metadata", {})
            pod_status = pod.get("status", {})
            uid, ip = pod_meta.get("uid"), pod_status.get("podIP")
            if not uid or not ip:
                raise ValueError("Ready pod is missing uid or podIP")
            if uid in pod_by_uid or ip in pod_by_ip:
                raise ValueError("Ready pod set contains a duplicate uid or podIP")
            entry = {
                "name": pod_meta.get("name"),
                "uid": uid,
                "node": pod.get("spec", {}).get("nodeName"),
                "ip": ip,
                "resourceVersion": pod_meta.get("resourceVersion"),
            }
            pod_by_uid[uid] = entry
            pod_by_ip[ip] = entry

        slice_items = slices.get("items") or []
        if not slice_items:
            result("TOPOLOGY_CONVERGING", "No EndpointSlice exists for the Service")
            return 0

        ready_endpoints = []
        excluded_endpoints = []
        slice_versions = []
        for endpoint_slice in slice_items:
            slice_meta = endpoint_slice.get("metadata", {})
            if (slice_meta.get("labels") or {}).get("kubernetes.io/service-name") != service.get(
                "metadata", {}
            ).get("name"):
                raise ValueError("EndpointSlice does not belong to the exact Service")
            slice_versions.append(
                {"name": slice_meta.get("name"), "resourceVersion": slice_meta.get("resourceVersion")}
            )
            for endpoint in endpoint_slice.get("endpoints") or []:
                conditions = endpoint.get("conditions") or {}
                terminating = conditions.get("terminating") is True
                is_ready = conditions.get("ready") is True
                addresses = endpoint.get("addresses") or []
                target = endpoint.get("targetRef") or {}
                entry = {
                    "addresses": addresses,
                    "uid": target.get("uid"),
                    "kind": target.get("kind"),
                    "namespace": target.get("namespace"),
                    "ready": is_ready,
                    "terminating": terminating,
                }
                if is_ready and not terminating:
                    ready_endpoints.append(entry)
                else:
                    excluded_endpoints.append(entry)

        endpoint_pairs = []
        namespace = metadata.get("namespace")
        for endpoint in ready_endpoints:
            if endpoint["kind"] != "Pod" or endpoint["namespace"] != namespace:
                result(
                    "TOPOLOGY_CONVERGING",
                    "EndpointSlice has a foreign ready targetRef",
                    ignoredSelectorCollisions=ignored_collisions,
                )
                return 0
            if len(endpoint["addresses"]) != 1:
                result(
                    "TOPOLOGY_CONVERGING",
                    "EndpointSlice ready target does not have exactly one address",
                    ignoredSelectorCollisions=ignored_collisions,
                )
                return 0
            endpoint_pairs.append((endpoint["uid"], endpoint["addresses"][0]))

        pod_pairs = {(uid, pod["ip"]) for uid, pod in pod_by_uid.items()}
        if (
            len(endpoint_pairs) != len(set(endpoint_pairs))
            or set(endpoint_pairs) != pod_pairs
        ):
            result(
                "TOPOLOGY_CONVERGING",
                "EndpointSlice ready targets do not equal the exact-build Ready pod set",
                ignoredSelectorCollisions=ignored_collisions,
            )
            return 0

        topology = {
            "deployment": {
                "name": metadata.get("name"),
                "uid": deployment_uid,
                "desired": desired,
                "generation": generation,
                "observedGeneration": observed,
                "resourceVersion": metadata.get("resourceVersion"),
            },
            "replicaSets": sorted(owned_replicasets.values(), key=lambda item: item["uid"]),
            "service": {
                "name": service.get("metadata", {}).get("name"),
                "selector": service_selector,
                "resourceVersion": service.get("metadata", {}).get("resourceVersion"),
            },
            "pods": sorted(pod_by_uid.values(), key=lambda item: item["uid"]),
            "endpointSlices": sorted(slice_versions, key=lambda item: item["name"] or ""),
        }
        canonical = json.dumps(topology, sort_keys=True, separators=(",", ":"))
        signature = hashlib.sha256(canonical.encode()).hexdigest()
        first_pod_ip = sorted(pod_by_ip)[0]
        result(
            "STABLE",
            "exact-build Deployment/Service/EndpointSlice topology is consistent",
            signature=signature,
            pod_ip=first_pod_ip,
            topology=topology,
            ignoredSelectorCollisions=ignored_collisions,
            excludedEndpointSliceTargets=excluded_endpoints,
        )
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        result("UNKNOWN", f"topology evidence unavailable: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
