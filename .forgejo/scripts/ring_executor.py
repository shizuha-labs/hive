#!/usr/bin/env python3
"""Submit and run the bounded, typed Hive release executor.

The submit path can only create an exact-generation request and two Jobs.  It
cannot supply a command, GVK, namespace, object name, or patch.  The execute
and recover paths run from the immutable release image under dedicated SAs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ring_fence import (
    Conflict, Fence, FenceError, FenceState, Holder, Kubectl, LeaseObserver,
    LEASE_DURATION_SECONDS, RENEW_INTERVAL_SECONDS,
)


REQUEST_NAMESPACE = "origin"
IMAGE_PREFIX = "localhost:30500/shizuha-hive:hive-"
RUN_RE = re.compile(r"^[0-9]{1,20}$")
REQUEST_KEYS = {
    "apiVersion", "operation", "holder", "image", "event_name", "hold",
    "hive725_phase", "prior_fence_token",
    "source_tree",
}
POST_COMMIT_FAILURE = 20
JOB_LOG_TAIL_LINES = 120
JOB_LOG_MAX_CHARS = 32_768
# PLAT-8251: pre-pull phase — warm the executor node's image cache before the
# executor Jobs are created so the observation budget is never burned on a
# first-pull (run 2686: cold s1 + 50-min deadline expired mid-pull).
PREPULL_SUFFIX = "prepull"
PREPULL_DEADLINE_SECONDS = 900


class ObservationDeadlineExpired(FenceError):
    """No fresh read established a terminal state before the existing deadline."""


def _observe_within_deadline(read: Any, deadline: float, description: str) -> Any:
    """Retry only a timed-out observation, never a mutation or authority failure."""
    while time.monotonic() < deadline:
        try:
            return read()
        except subprocess.TimeoutExpired:
            print(
                f"::warning::ring executor observation timed out: {description}; "
                "state remains unknown; retrying within the original deadline",
                file=sys.stderr,
            )
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise ObservationDeadlineExpired(
        f"observation deadline expired without a fresh state: {description}"
    )


def configure_incluster_kubeconfig() -> None:
    """Give kubectl an explicit ServiceAccount config, including exec streams.

    The k3s multicall client can infer the in-cluster API for ordinary REST
    calls, but its kubectl exec transport falls back to localhost without an
    explicit kubeconfig. Keep the projected token as a tokenFile so rotation
    remains effective for long fleet releases.
    """
    service_host = os.environ.get("KUBERNETES_SERVICE_HOST")
    service_port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ.get(
        "KUBERNETES_SERVICE_PORT"
    )
    service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    token_file = service_account / "token"
    ca_file = service_account / "ca.crt"
    if not service_host or not service_port or not token_file.is_file() or not ca_file.is_file():
        raise FenceError("in-cluster Kubernetes ServiceAccount projection is incomplete")

    path = Path("/tmp/hive-ring-kubeconfig")
    payload = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{
            "name": "incluster",
            "cluster": {
                "server": f"https://{service_host}:{service_port}",
                "certificate-authority": str(ca_file),
            },
        }],
        "users": [{
            "name": "incluster",
            "user": {"tokenFile": str(token_file)},
        }],
        "contexts": [{
            "name": "incluster",
            "context": {
                "cluster": "incluster",
                "user": "incluster",
                "namespace": REQUEST_NAMESPACE,
            },
        }],
        "current-context": "incluster",
    }
    path.write_text(json.dumps(payload, sort_keys=True))
    path.chmod(0o600)
    os.environ["KUBECONFIG"] = str(path)


def request_name(holder: Holder) -> str:
    run = re.sub(r"[^a-z0-9-]", "-", holder.run_id.lower()).strip("-")[-32:]
    return f"hive-ring-request-{holder.ring}-{run}"


def job_name(holder: Holder, suffix: str) -> str:
    return f"hive-ring-{suffix}-{holder.ring}-{holder.run_id}"[-63:].strip("-")


def validate_request(value: dict[str, Any]) -> tuple[Holder, str, dict[str, Any]]:
    if set(value) != REQUEST_KEYS or value.get("apiVersion") != "shizuha.io/hive-ring-request-v1":
        raise FenceError("unknown or incomplete executor request schema")
    if value.get("operation") not in {"release", "cleanup-canary"}:
        raise FenceError("executor request operation is not allowlisted")
    holder = Holder.from_dict(value["holder"])
    image = str(value["image"])
    if image != f"{IMAGE_PREFIX}{holder.release_sha[:7]}":
        raise FenceError("executor image is not the exact requested release")
    if value["event_name"] not in {"push", "workflow_dispatch"}:
        raise FenceError("event_name is not allowlisted")
    if not isinstance(value["hold"], bool):
        raise FenceError("hold must be boolean")
    if value["hive725_phase"] not in {"phase1", "finalize"}:
        raise FenceError("hive725_phase is not allowlisted")
    if not isinstance(value["prior_fence_token"], int) or value["prior_fence_token"] < 0:
        raise FenceError("prior_fence_token must be a non-negative integer")
    if not re.fullmatch(r"[0-9a-f]{40}", value["source_tree"]):
        raise FenceError("source_tree must be an exact Git tree SHA")
    if value["operation"] == "cleanup-canary" and holder.ring != "canary":
        raise FenceError("cleanup-canary is valid only under the canary fence")
    return holder, image, {
        "event_name": value["event_name"], "hold": value["hold"],
        "hive725_phase": value["hive725_phase"], "source_tree": value["source_tree"],
        "prior_fence_token": value["prior_fence_token"],
        "operation": value["operation"],
    }


def load_request(api: Kubectl, name: str) -> tuple[Holder, str, dict[str, Any]]:
    if not re.fullmatch(r"hive-ring-request-(canary|fleet)-[a-z0-9-]{1,40}", name):
        raise FenceError("invalid request name")
    result = api.run(["-n", REQUEST_NAMESPACE, "get", "configmap", name, "-o", "json"])
    body = json.loads(result.stdout)
    data = body.get("data", {})
    if set(data) != {"request.json"}:
        raise FenceError("request ConfigMap has unexpected data keys")
    return validate_request(json.loads(data["request.json"]))


def protected_targets(holder: Holder) -> list[tuple[str, str, str]]:
    if holder.ring == "canary":
        return [
            ("shizuha-hive", "deployment", "hive-canary"),
            ("shizuha-hive", "service", "hive-canary"),
            ("monitoring", "probe.monitoring.coreos.com", f"probe-shizuha-hive-canary-{holder.release_sha[:7]}"),
        ]
    return [
        ("shizuha-hive", "deployment", "hive"),
        ("shizuha-hive", "deployment", "hive-api"),
        ("shizuha-hive", "deployment", "hive-codex-refresh"),
        ("shizuha-hive", "configmap", "hive-tiers"),
        ("shizuha-hive", "service", "hive"),
        ("shizuha-hive", "service", "hive-direct"),
        ("shizuha-hive", "service", "hive-tail"),
        ("shizuha-hive", "service", "hive-connect-delivery"),
        ("shizuha-hive", "networkpolicy", "allow-intra-namespace"),
        ("shizuha-hive", "networkpolicy", "allow-node-healthchecks"),
        ("shizuha-hive", "networkpolicy", "allow-connect-delivery-ingress"),
        ("shizuha-hive", "networkpolicy", "allow-platform-egress"),
        ("shizuha-hive", "cronjob", "hive-usage-outbox-dispatcher"),
        ("shizuha-hive", "cronjob", "hive-identity-reconciler"),
        ("shizuha-hive", "cronjob", "fleet-agent-liveness-reconciler"),
        ("shizuha-hive", "cronjob", "hive-claw-isolation-reconcile"),
        ("shizuha-hive", "cronjob", "hive-harness-autoupgrade"),
        ("shizuha-hive", "cronjob", "fleet-harness-drift-roll"),
        ("shizuha-hive", "cronjob", "hive-on-demand-reconciler"),
        ("shizuha-hive", "cronjob", "hive-github-identity-reconciler"),
        # 2026-09-02: ring-release-fleet.sh applies hive-forgejo-token-reconciler
        # (ORIG-15 mint path). Missing from protected_targets → fenced kubectl
        # rejected the CronJob and fleet execute rolled hive-2b4c7e5 back to
        # hive-a85cceb (HIVE-580 class, same as hive-claw-idle / golden-path).
        ("shizuha-hive", "cronjob", "hive-forgejo-token-reconciler"),
        ("shizuha-hive", "cronjob", "hive-hosted-claw-wan-egress-smoke"),
        ("shizuha-hive", "cronjob", "hive-compute-billing"),
        ("shizuha-hive", "cronjob", "hive-golden-path-validation"),
        ("shizuha-hive", "job", f"ci-migrate-hive-{holder.release_sha[:7]}"),
        ("shizuha-hive", "job", f"hive725-non-connect-{holder.release_sha[:7]}"),
        ("shizuha-hive", "prometheusrule", "hive-codex-refresh"),
        ("shizuha-hive", "prometheusrule", "hive-runtime-lane"),
        ("shizuha-hive", "prometheusrule", "hive-runtime-contract"),
        ("shizuha-hive", "prometheusrule", "hive-usage-outbox"),
        ("shizuha-hive", "prometheusrule", "hive-golden-path"),
        ("monitoring", "prometheusrule", "hive-on-demand-reconcile"),
        ("monitoring", "prometheusrule", "hive-claw-isolation-reconcile"),
        ("monitoring", "prometheusrule", "hive-hosted-claw-wan-egress-smoke"),
        # HIVE-2011 / 2026-09-01: ring-release-fleet.sh applies these in
        # monitoring, but they were missing from protected_targets. Fenced
        # kubectl then rejected `servicemonitor/hive-claw-idle` and fleet
        # execute 35694 rolled 96df61f back to dab2088 (same HIVE-580 class).
        ("monitoring", "prometheusrule", "hive-claw-idle"),
        ("monitoring", "servicemonitor", "hive-claw-idle"),
        ("monitoring", "probe.monitoring.coreos.com", f"probe-shizuha-hive-fleet-{holder.release_sha[:7]}"),
    ]


def target_is_protected(holder: Holder, namespace: str, resource: str, name: str) -> bool:
    """Return whether a typed target belongs to this ring generation's surface."""
    if (namespace, resource, name) in set(protected_targets(holder)):
        return True
    sha7 = holder.release_sha[:7]
    patterns = {
        ("shizuha-hive", "job"): (
            rf"ci-migrate-hive-{sha7}",
            rf"hive725-non-connect-{sha7}",
            rf"hive-identity-reconciler-deploy-{sha7}-[0-9]+",
            # HIVE-2266 (recurrence of PLAT-7248): the fleet release creates a
            # golden-path validation Job via `kubectl create job
            # --from=cronjob/hive-golden-path-validation`, named
            # hive-golden-path-validation-deploy-<sha7>-<unix_ts>. PLAT-7248
            # fenced the CronJob + PrometheusRule but MISSED this Job, so the
            # fenced kubectl facade rejected the create (FenceError) and the
            # fleet release rolled back at phase2 stage=1. Fence it here.
            rf"hive-golden-path-validation-deploy-{sha7}-[0-9]+",
        ),
        ("shizuha-hive", "pod"): (
            # HIVE-2567/2579 (run 3071): the Connect cutover gate self-heals
            # orphaned old-image ReplicaSet pods. Such pods are created by the
            # ReplicaSet controller, never by a release, so they carry no
            # fence generation; the fenced kubectl adopts them at delete time
            # (bounded to this exact shape). hive-canary-*, hive-codex-refresh-*
            # and every other workload stay outside the allowlist.
            rf"hive-[0-9a-f]{{9,10}}-[0-9a-z]{{5}}",
        ),
        ("origin", "configmap"): (
            # capture-ring-evidence.sh: hive-ring-evidence-${RING}-${SHA7}-${GENERATION:0:12}
            rf"hive-ring-evidence-{holder.ring}-{sha7}-[0-9a-f]{{12}}",
        ),
        ("origin", "job"): (
            rf"hive-ring-evidence-{holder.ring}-{sha7}-[0-9a-f]{{12}}",
        ),
        ("monitoring", "probe.monitoring.coreos.com"): (
            (
                r"probe-shizuha-hive-canary-[0-9a-f]{7}",
                r"probe-shizuha-hive-reconcile-canary-[0-9a-f]{7}",
            )
            if holder.ring == "canary"
            else (r"probe-shizuha-hive-fleet-[0-9a-f]{7}",)
        ),
    }
    return any(
        re.fullmatch(pattern, name)
        for pattern in patterns.get((namespace, resource), ())
    )


def is_recovery_bootstrap_target(
    namespace: str,
    resource: str,
    name: str,
    target: dict[str, Any],
) -> bool:
    """Recognize exact legacy objects newly added to the fenced surface."""
    labels = target.get("metadata", {}).get("labels") or {}
    if (namespace, resource, name) == (
        "shizuha-hive", "cronjob", "fleet-harness-drift-roll",
    ):
        pod_spec = target.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
        containers = pod_spec.get("containers") or []
        return (
            labels.get("app.kubernetes.io/part-of") == "shizuha-platform"
            and pod_spec.get("serviceAccountName") == "hive-controller"
            and len(containers) == 1
            and containers[0].get("name") == "roll"
            and str(containers[0].get("image", "")).startswith("localhost:30500/shizuha-hive:")
        )
    if (namespace, resource, name) == (
        "shizuha-hive", "cronjob", "fleet-agent-liveness-reconciler",
    ):
        return (
            labels.get("app") == "hive"
            and labels.get("app.kubernetes.io/component") == "liveness-reconciler"
        )
    if (namespace, resource, name) == (
        "shizuha-hive", "configmap", "hive-tiers",
    ):
        return (
            labels.get("app.kubernetes.io/name") == "hive-api"
            and labels.get("app.kubernetes.io/component") == "tier-config"
            and labels.get("app.kubernetes.io/managed-by") == "shizuha-hive"
        )
    if (namespace, resource, name) == (
        "shizuha-hive", "networkpolicy", "allow-platform-egress",
    ):
        return (
            labels.get("app.kubernetes.io/managed-by") == "shizuha-hive"
            and labels.get("shizuha.io/policy") == "egress-hardening"
            and target.get("spec", {}).get("podSelector") == {}
            and target.get("spec", {}).get("policyTypes") == ["Egress"]
        )
    return False


def _discover_dynamic_targets(fence: Fence) -> list[tuple[str, str, str]]:
    """List bounded namespaces/classes so takeover cannot miss a run-created target."""
    inventories = (
        ("shizuha-hive", "job"),
        ("monitoring", "probe.monitoring.coreos.com"),
        ("origin", "configmap"),
        ("origin", "job"),
    )
    targets: list[tuple[str, str, str]] = []
    for namespace, resource in inventories:
        listed = fence.api.run(["-n", namespace, "get", resource, "-o", "json"])
        for item in json.loads(listed.stdout).get("items", []):
            name = str(item.get("metadata", {}).get("name", ""))
            if target_is_protected(fence.holder, namespace, resource, name):
                targets.append((namespace, resource, name))
    return targets


def claim_existing_targets(
    fence: Fence,
    *,
    allow_absent: bool,
    expected_generation: str | None = None,
    expected_token: int | None = None,
    expected_token_ceiling: int | None = None,
    discover_dynamic: bool = False,
) -> None:
    assert fence.token is not None
    targets = protected_targets(fence.holder)
    if discover_dynamic:
        for target in _discover_dynamic_targets(fence):
            if target not in targets:
                targets.append(target)
    else:
        for ring in (fence.holder.ring,):
            listed = fence.api.run([
                "-n", "monitoring", "get", "probe",
                "-l", f"service=hive,release_ring={ring}", "-o", "json",
            ])
            for item in json.loads(listed.stdout).get("items", []):
                name = str(item.get("metadata", {}).get("name", ""))
                if not re.fullmatch(rf"probe-shizuha-hive-{ring}-[0-9a-f]{{7}}", name):
                    raise FenceError(f"unexpected {ring} Probe name in protected selector")
                target = ("monitoring", "probe.monitoring.coreos.com", name)
                if target not in targets:
                    targets.append(target)
    for namespace, resource, name in targets:
        result = fence.api.run(["-n", namespace, "get", resource, name, "-o", "json"], check=False)
        if result.returncode:
            if allow_absent and "NotFound" in result.stderr:
                continue
            raise FenceError(f"cannot CAS-claim protected {resource}/{name}")
        target = json.loads(result.stdout)
        original_uid = str(target["metadata"].get("uid", ""))
        last_error: FenceError | None = None
        for attempt in range(8):
            metadata = target["metadata"]
            annotations = metadata.get("annotations") or {}
            ops = [{"op": "test", "path": "/metadata/resourceVersion", "value": str(metadata["resourceVersion"])}]
            if expected_token_ceiling is not None and (
                expected_generation is not None or expected_token is not None
            ):
                raise FenceError("takeover claim ownership modes are mutually exclusive")
            if expected_generation is not None or expected_token is not None:
                if expected_generation is None or expected_token is None:
                    raise FenceError("takeover claim requires both predecessor generation and token")
                if (
                    annotations.get("shizuha.io/ring-generation") == fence.holder.generation
                    and annotations.get("shizuha.io/fence-token") == str(fence.token)
                ):
                    # A recovery Job retry may inherit a partially completed claim.
                    # Exact recovery ownership is already the desired postcondition.
                    fence.validate({"recovering"})
                    break
                if annotations.get("shizuha.io/ring-generation") != expected_generation:
                    raise Conflict(f"protected {resource}/{name} belongs to another generation")
                if annotations.get("shizuha.io/fence-token") != str(expected_token):
                    raise Conflict(f"protected {resource}/{name} carries another fence token")
                ops.extend([
                    {
                        "op": "test",
                        "path": "/metadata/annotations/shizuha.io~1ring-generation",
                        "value": expected_generation,
                    },
                    {
                        "op": "test",
                        "path": "/metadata/annotations/shizuha.io~1fence-token",
                        "value": str(expected_token),
                    },
                ])
            elif expected_token_ceiling is not None:
                observed_generation = annotations.get("shizuha.io/ring-generation")
                observed_token = annotations.get("shizuha.io/fence-token")
                if (
                    observed_generation == fence.holder.generation
                    and observed_token == str(fence.token)
                ):
                    # A recovery retry resumes the same holder/token after a
                    # partial target claim. Its own token is necessarily one
                    # above the predecessor ceiling and is already the exact
                    # fail-closed postcondition, not a newer foreign owner.
                    fence.validate({"recovering"})
                    break
                if observed_generation is None and observed_token is None:
                    # This object predates ring fencing and was added to the
                    # protected set by the release that first tried to manage
                    # it. Recovery may bootstrap only the exact,
                    # strongly-labelled legacy object by resourceVersion CAS;
                    # every other unowned object remains fail-closed.
                    if not is_recovery_bootstrap_target(
                        namespace, resource, name, target,
                    ):
                        raise Conflict(
                            f"protected {resource}/{name} has invalid predecessor ownership"
                        )
                else:
                    try:
                        parsed_token = int(observed_token)
                    except (TypeError, ValueError) as exc:
                        raise Conflict(
                            f"protected {resource}/{name} has invalid predecessor ownership"
                        ) from exc
                    if not observed_generation or parsed_token < 0 or parsed_token > expected_token_ceiling:
                        raise Conflict(f"protected {resource}/{name} carries a newer fence token")
                    ops.extend([
                        {
                            "op": "test",
                            "path": "/metadata/annotations/shizuha.io~1ring-generation",
                            "value": observed_generation,
                        },
                        {
                            "op": "test",
                            "path": "/metadata/annotations/shizuha.io~1fence-token",
                            "value": str(parsed_token),
                        },
                    ])
            claims = {
                "shizuha.io/ring-generation": fence.holder.generation,
                "shizuha.io/fence-token": str(fence.token),
            }
            if "annotations" not in metadata:
                ops.append({"op": "add", "path": "/metadata/annotations", "value": claims})
            else:
                for key, value in claims.items():
                    path = "/metadata/annotations/" + key.replace("/", "~1")
                    ops.append({"op": "replace" if key in annotations else "add", "path": path, "value": value})
            fence.validate()
            try:
                fence.api.json_patch(namespace, resource, name, ops)
                last_error = None
                break
            except FenceError as exc:
                last_error = exc

            # CronJob controllers and Deployment controllers can update status
            # between the read and JSON Patch. k3s reports a failed JSON Patch
            # resourceVersion test as a generic 422 Invalid, not Conflict. Retry
            # only when a fresh read proves the same object advanced while its
            # ownership remained safe; unchanged invalid requests fail at once.
            refreshed_result = fence.api.run(
                ["-n", namespace, "get", resource, name, "-o", "json"],
                check=False,
            )
            if refreshed_result.returncode:
                break
            refreshed = json.loads(refreshed_result.stdout)
            refreshed_metadata = refreshed["metadata"]
            refreshed_annotations = refreshed_metadata.get("annotations") or {}
            if original_uid and str(refreshed_metadata.get("uid", "")) != original_uid:
                raise Conflict(f"protected {resource}/{name} was replaced during claim")
            if (
                refreshed_annotations.get("shizuha.io/ring-generation") == fence.holder.generation
                and refreshed_annotations.get("shizuha.io/fence-token") == str(fence.token)
            ):
                fence.validate()
                target = refreshed
                last_error = None
                break
            if expected_generation is not None and expected_token is not None:
                if refreshed_annotations.get("shizuha.io/ring-generation") != expected_generation:
                    raise Conflict(f"protected {resource}/{name} belongs to another generation")
                if refreshed_annotations.get("shizuha.io/fence-token") != str(expected_token):
                    raise Conflict(f"protected {resource}/{name} carries another fence token")
            elif expected_token_ceiling is not None:
                refreshed_generation = refreshed_annotations.get("shizuha.io/ring-generation")
                refreshed_token = refreshed_annotations.get("shizuha.io/fence-token")
                try:
                    parsed_refreshed_token = int(refreshed_token)
                except (TypeError, ValueError) as exc:
                    raise Conflict(
                        f"protected {resource}/{name} has invalid predecessor ownership"
                    ) from exc
                if (
                    not refreshed_generation
                    or parsed_refreshed_token < 0
                    or parsed_refreshed_token > expected_token_ceiling
                ):
                    raise Conflict(f"protected {resource}/{name} carries a newer fence token")
            if str(refreshed_metadata.get("resourceVersion")) == str(metadata["resourceVersion"]):
                break
            target = refreshed
            if attempt < 7:
                time.sleep(0.1)
        else:
            assert last_error is not None
        if last_error is not None:
            final_annotations = target["metadata"].get("annotations") or {}
            claimed = (
                final_annotations.get("shizuha.io/ring-generation") == fence.holder.generation
                and final_annotations.get("shizuha.io/fence-token") == str(fence.token)
            )
            if not claimed:
                raise FenceError(
                    f"CAS claim rejected for {namespace}/{resource}/{name}: {last_error}"
                ) from last_error


def _request_manifest(
    holder: Holder, image: str, args: argparse.Namespace, prior_fence_token: int,
) -> dict[str, Any]:
    request = {
        "apiVersion": "shizuha.io/hive-ring-request-v1", "operation": args.operation,
        "holder": {key: getattr(holder, key) for key in holder.__dataclass_fields__},
        "image": image, "event_name": args.event_name,
        "hold": args.hold == "true", "hive725_phase": args.hive725_phase,
        "source_tree": args.source_tree, "prior_fence_token": prior_fence_token,
    }
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {
            "name": request_name(holder), "namespace": REQUEST_NAMESPACE,
            "labels": {"app": "hive-ring-executor", "release_ring": holder.ring},
        },
        "data": {"request.json": json.dumps(request, sort_keys=True, separators=(",", ":"))},
    }


def _job_manifest(holder: Holder, image: str, mode: str) -> dict[str, Any]:
    deadline = 3000 if holder.ring == "canary" else 7500
    recovery_budget = 600 if holder.ring == "canary" else 2100
    active_deadline = (
        deadline if mode == "execute"
        else deadline + LEASE_DURATION_SECONDS + recovery_budget
    )
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {
            "name": job_name(holder, mode), "namespace": REQUEST_NAMESPACE,
            "labels": {"app": "hive-ring-executor", "release_ring": holder.ring},
        },
        "spec": {
            "backoffLimit": 0 if mode == "execute" else 2,
            "activeDeadlineSeconds": active_deadline,
            "ttlSecondsAfterFinished": 900,
            "template": {
                "metadata": {"labels": {"app": "hive-ring-executor", "release_ring": holder.ring}},
                "spec": {
                    "serviceAccountName": f"hive-ring-{mode}",
                    "automountServiceAccountToken": True,
                    "restartPolicy": "Never",
                    # HIVE-2406: the lease keeper must not share the runner
                    # plane's fate. The executor Jobs are cluster-resident, but
                    # without placement they schedule onto the runner nodes —
                    # so a runner-plane freeze (seat darkness) kills the pod,
                    # the 90s lease expires, and the recover Job rolls back a
                    # HEALTHY release (2 rollbacks in ~5h: 72db199, 5806714a).
                    # Pin to s1 — the stable control-plane node that stayed up
                    # through every runner-plane incident — so seat darkness
                    # no longer reaches the renewal loop. The fail-safe is
                    # preserved: a genuinely dead Job stops renewing, the
                    # lease expires, and rollback still fires. s1 carries the
                    # PLAT-5936 maintenance taint (operator=unschedulable:
                    # NoSchedule) — tolerated here per the PLAT-8187 form.
                    # The release image is multi-arch (amd64 child built by
                    # the release lane), so the amd64-only node schedules it.
                    "nodeSelector": {"kubernetes.io/hostname": "s1"},
                    "tolerations": [
                        {"key": "operator", "operator": "Equal", "value": "unschedulable", "effect": "NoSchedule"},
                        # s1 is also a workstation (live 2026-09-09): without
                        # this, operator=unschedulable alone still
                        # FailedScheduling and the fleet ring deadlocks
                        # concurrency (run 39729 pending 55m).
                        {"key": "node.shizuha/workstation", "operator": "Equal", "value": "true", "effect": "NoSchedule"},
                    ],
                    "containers": [{
                        "name": mode, "image": image, "imagePullPolicy": "IfNotPresent",
                        "command": ["/app/.venv/bin/python", "/app/.forgejo/scripts/ring-executor.py", mode],
                        "args": ["--request", request_name(holder)],
                        "env": [{"name": "RING_KUBECTL_REAL", "value": "/usr/local/bin/kubectl"}],
                        "resources": {"requests": {"cpu": "25m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
                    }],
                },
            },
        },
    }


def _prepull_job_manifest(holder: Holder, image: str) -> dict[str, Any]:
    """PLAT-8251: pre-pull the release image onto the executor node.

    The canary observation budget does not account for first-pull on a cold
    executor node: post-GC s1 held ZERO shizuha-hive images, so the pinned
    executor Job spent its window pulling while legitimately Active and the
    50-min deadline expired before first-pull + the 45m soak could complete
    (run 2686, ni's 3-probe node read). Pre-pulling as a first-class observable
    step — pinned IDENTICALLY to the executor (same nodeSelector + both s1
    tolerations) — keeps the fail-loud budget tight (a genuinely hung Job still
    trips the deadline fast) instead of widening it. No ServiceAccount token:
    the pull needs no API access.
    """
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {
            "name": job_name(holder, PREPULL_SUFFIX), "namespace": REQUEST_NAMESPACE,
            "labels": {"app": "hive-ring-executor", "release_ring": holder.ring},
        },
        "spec": {
            "backoffLimit": 1,
            "activeDeadlineSeconds": PREPULL_DEADLINE_SECONDS,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {"app": "hive-ring-executor", "release_ring": holder.ring}},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "nodeSelector": {"kubernetes.io/hostname": "s1"},
                    "tolerations": [
                        {"key": "operator", "operator": "Equal", "value": "unschedulable", "effect": "NoSchedule"},
                        {"key": "node.shizuha/workstation", "operator": "Equal", "value": "true", "effect": "NoSchedule"},
                    ],
                    "containers": [{
                        "name": "prepull", "image": image, "imagePullPolicy": "IfNotPresent",
                        # Layered pull happens before container start; the
                        # command is a no-op that proves the image runs.
                        "command": ["/app/.venv/bin/python", "-c", "pass"],
                        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "256Mi"}},
                    }],
                },
            },
        },
    }


def _bounded_job_log(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= JOB_LOG_MAX_CHARS:
        return value
    omitted = len(encoded) - JOB_LOG_MAX_CHARS
    tail = encoded[-JOB_LOG_MAX_CHARS:].decode("utf-8", errors="replace")
    return f"[... {omitted} earlier bytes omitted ...]\n{tail}"


def _emit_job_logs(api: Kubectl, name: str) -> None:
    """Emit captured Job diagnostics without letting them grow without bound."""
    try:
        result = api.run([
            "-n", REQUEST_NAMESPACE, "logs", f"job/{name}",
            f"--tail={JOB_LOG_TAIL_LINES}",
        ], check=False)
    except subprocess.TimeoutExpired:
        print(
            f"::warning::ring executor diagnostics job/{name} timed out; logs unavailable",
            file=sys.stderr,
        )
        return
    print(f"::group::ring executor diagnostics job/{name}", file=sys.stderr)
    if result.stdout:
        bounded = _bounded_job_log(result.stdout)
        print(bounded, end="" if bounded.endswith("\n") else "\n")
    if result.stderr:
        bounded = _bounded_job_log(result.stderr)
        print(
            bounded, end="" if bounded.endswith("\n") else "\n",
            file=sys.stderr,
        )
    if result.returncode:
        print(
            f"::warning::kubectl logs job/{name} failed rc={result.returncode}",
            file=sys.stderr,
        )
    print("::endgroup::", file=sys.stderr)


def submit(args: argparse.Namespace) -> int:
    holder = Holder(
        repo="shizuha-labs/hive", workflow="deploy.yml", run_id=args.run_id,
        run_number=args.run_number, job=args.job, ring=args.ring,
        release_sha=args.release_sha, restored_sha=args.restored_sha,
    )
    image = f"{IMAGE_PREFIX}{holder.release_sha[:7]}"
    api = Kubectl(args.kubectl)
    prior = FenceState.from_lease(api.get_lease(holder.lease_name))
    # Delete is exact-name cleanup of this workflow run only. Stable Leases are
    # never deleted; the fresh request records the Lease token it must advance.
    for suffix in (PREPULL_SUFFIX, "execute", "recover"):
        api.run(["-n", REQUEST_NAMESPACE, "delete", "job", job_name(holder, suffix), "--ignore-not-found", "--wait=true"])
    api.run(["-n", REQUEST_NAMESPACE, "delete", "configmap", request_name(holder), "--ignore-not-found"])
    # PLAT-8251: gate executor creation on the pre-pull. A cold executor node
    # burns the observation budget on first-pull while the Job is legitimately
    # Active (run 2686); warming the cache first keeps the fail-loud budget
    # tight and makes the pull a first-class observable step.
    prepull_name = job_name(holder, PREPULL_SUFFIX)
    api.run(["create", "-f", "-"], stdin=json.dumps(_prepull_job_manifest(holder, image)))
    prepull_deadline = time.monotonic() + PREPULL_DEADLINE_SECONDS + 120
    while True:
        try:
            observed = _observe_within_deadline(
                lambda: api.run(["-n", REQUEST_NAMESPACE, "get", "job", prepull_name, "-o", "json"]),
                prepull_deadline, f"job/{prepull_name}",
            )
        except ObservationDeadlineExpired as exc:
            print(f"::error::{exc}", file=sys.stderr)
            _emit_job_logs(api, prepull_name)
            return 1
        status = json.loads(observed.stdout).get("status", {})
        if int(status.get("succeeded", 0) or 0) > 0:
            break
        if int(status.get("failed", 0) or 0) > 0:
            _emit_job_logs(api, prepull_name)
            print(
                "::error::pre-pull failed on the executor node; refusing to create the "
                "executor Jobs and burn the observation budget on a cold pull (PLAT-8251)",
                file=sys.stderr,
            )
            return 1
        time.sleep(5)
    api.run(["create", "-f", "-"], stdin=json.dumps(
        _request_manifest(holder, image, args, prior.token)
    ))
    api.run(["create", "-f", "-"], stdin=json.dumps(_job_manifest(holder, image, "execute")))
    api.run(["create", "-f", "-"], stdin=json.dumps(_job_manifest(holder, image, "recover")))
    owner_references = []
    for suffix in ("execute", "recover"):
        created = api.run([
            "-n", REQUEST_NAMESPACE, "get", "job", job_name(holder, suffix),
            "-o", "json",
        ])
        job = json.loads(created.stdout)
        owner_references.append({
            "apiVersion": "batch/v1", "kind": "Job",
            "name": job["metadata"]["name"], "uid": job["metadata"]["uid"],
            "controller": False, "blockOwnerDeletion": False,
        })
    api.run([
        "-n", REQUEST_NAMESPACE, "patch", "configmap", request_name(holder),
        "--type=json", "-p", json.dumps([{
            "op": "add", "path": "/metadata/ownerReferences",
            "value": owner_references,
        }], separators=(",", ":")),
    ])
    deadline = time.monotonic() + (3000 if holder.ring == "canary" else 7500)
    execute_name = job_name(holder, "execute")
    while time.monotonic() < deadline:
        try:
            observed = _observe_within_deadline(
                lambda: api.run(["-n", REQUEST_NAMESPACE, "get", "job", execute_name, "-o", "json"]),
                deadline, f"job/{execute_name}",
            )
        except ObservationDeadlineExpired:
            break
        status = json.loads(observed.stdout).get("status", {})
        if int(status.get("succeeded", 0) or 0) > 0:
            return 0
        if int(status.get("failed", 0) or 0) > 0:
            _emit_job_logs(api, execute_name)
            try:
                state = _observe_within_deadline(
                    lambda: FenceState.from_lease(api.get_lease(holder.lease_name)),
                    deadline, f"lease/{holder.lease_name} after failed job/{execute_name}",
                )
            except ObservationDeadlineExpired as exc:
                print(f"::error::{exc}", file=sys.stderr)
                return 1
            if state.holder == holder.serialized() and state.phase == "committed":
                return POST_COMMIT_FAILURE
            return 1
        time.sleep(5)
    print(
        f"::error::ring executor observation deadline expired: job/{execute_name}; "
        "no terminal Job status established",
        file=sys.stderr,
    )
    _emit_job_logs(api, execute_name)
    return 1


def _renew_loop(fence: Fence, stop: threading.Event, failure: list[BaseException], child: subprocess.Popen[str]) -> None:
    while not stop.wait(RENEW_INTERVAL_SECONDS):
        try:
            fence.renew()
        except Conflict as exc:
            # INV-F is committed by the child process with its own CAS. Once
            # that exact holder/token reaches committed, renewal must stop
            # without killing post-commit work. Any other conflict is genuine
            # ownership loss and remains cancellation-coupled.
            try:
                state = fence.read()
                if (
                    state.holder == fence.holder.serialized()
                    and state.token == fence.token
                    and state.phase == "committed"
                ):
                    return
            except BaseException:
                pass
            failure.append(exc)
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            return
        except BaseException as exc:
            failure.append(exc)
            # Renewal loss is cancellation-coupled to the whole executor group.
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            return


def _renew_only_loop(
    fence: Fence, stop: threading.Event, failure: list[BaseException],
) -> None:
    while not stop.wait(RENEW_INTERVAL_SECONDS):
        try:
            fence.renew()
        except Conflict as exc:
            # A same-owner phase CAS may legitimately win this resourceVersion
            # race. Re-read the exact holder/token now; never mask a successor
            # takeover as that benign race.
            try:
                fence.validate()
            except BaseException:
                failure.append(exc)
                return
            continue
        except BaseException as exc:
            failure.append(exc)
            return


def execute(args: argparse.Namespace) -> int:
    started = time.monotonic()
    api = Kubectl()
    holder, _, options = load_request(api, args.request)
    deadline = started + (3000 if holder.ring == "canary" else 7500)
    fence = Fence(holder, api)
    fence.acquire(expected_prior_token=options["prior_fence_token"])
    claim_stop = threading.Event()
    claim_failures: list[BaseException] = []
    claim_renewer = threading.Thread(
        target=_renew_only_loop,
        args=(fence, claim_stop, claim_failures), daemon=True,
    )
    claim_renewer.start()
    try:
        claim_existing_targets(fence, allow_absent=True, discover_dynamic=True)
    finally:
        claim_stop.set()
        claim_renewer.join(timeout=2)
    if claim_failures:
        raise FenceError(f"lease renewal lost during target claim: {type(claim_failures[0]).__name__}")
    fence.phase("active", {"acquiring"})
    env = {**os.environ, **{f"RING_{key.upper()}": getattr(holder, key) for key in holder.__dataclass_fields__}}
    env["RING_FENCE_TOKEN"] = str(fence.token)
    env["RING_KUBECTL_REAL"] = "/usr/local/bin/kubectl"
    env["PATH"] = f"/app/.forgejo/scripts:{env.get('PATH', '')}"
    env.update({
        "RELEASE_SHA": holder.release_sha,
        "CANARY_HOLD": "true" if options["hold"] else "false",
        "EVENT_NAME": options["event_name"],
        "REQUESTED_RING": holder.ring,
        "REQUESTED_SHA": holder.release_sha,
        "REQUESTED_HIVE725_PHASE": options["hive725_phase"],
        "SOURCE_TREE": options["source_tree"],
    })
    script = (
        "/app/.forgejo/scripts/ring-cleanup-canary.sh"
        if options["operation"] == "cleanup-canary"
        else f"/app/.forgejo/scripts/ring-release-{holder.ring}.sh"
    )
    child = subprocess.Popen(["/bin/bash", script], env=env, text=True, start_new_session=True)
    stop = threading.Event()
    failures: list[BaseException] = []
    renewer = threading.Thread(target=_renew_loop, args=(fence, stop, failures, child), daemon=True)
    renewer.start()
    rc = child.wait()
    stop.set()
    renewer.join(timeout=2)
    if failures:
        raise FenceError(f"lease renewal lost; executor killed: {type(failures[0]).__name__}")
    if rc:
        return rc
    final = _observe_within_deadline(
        fence.read, deadline, f"lease/{holder.lease_name} after release completion",
    )
    if holder.ring == "fleet" and options["operation"] == "release":
        if final.holder != holder.serialized() or final.token != fence.token:
            raise Conflict("fleet commit owner/token changed")
        if final.phase != "committed":
            fence.phase("committed", {"active", "evidence-pending"})
    else:
        fence.phase("completed", {"active", "evidence-pending"})
    return 0


def recover(args: argparse.Namespace) -> int:
    api = Kubectl()
    predecessor, _, options = load_request(api, args.request)
    requested_recovery = Holder(
        repo=predecessor.repo, workflow=predecessor.workflow,
        run_id=f"{predecessor.run_id}-recovery", run_number=predecessor.run_number,
        job="recover", ring=predecessor.ring, release_sha=predecessor.release_sha,
        restored_sha=predecessor.restored_sha,
    )
    recovery = requested_recovery
    failed_holder = predecessor
    predecessor_fence = Fence(predecessor, api)
    observer = LeaseObserver(predecessor_fence.read)
    executor_deadline = 3000 if predecessor.ring == "canary" else 7500
    hard_deadline = executor_deadline + LEASE_DURATION_SECONDS + 60
    started = time.monotonic()
    owner_seen = False
    while True:
        state, expired = observer.observe()
        try:
            observed_holder = Holder.from_dict(json.loads(state.holder))
        except (json.JSONDecodeError, TypeError) as exc:
            raise FenceError("ring Lease holderIdentity is malformed") from exc
        is_this_recovery = (
            observed_holder.repo == requested_recovery.repo
            and observed_holder.workflow == requested_recovery.workflow
            and observed_holder.run_id == requested_recovery.run_id
            and observed_holder.run_number == requested_recovery.run_number
            and observed_holder.job == "recover"
            and observed_holder.ring == requested_recovery.ring
        )
        if is_this_recovery:
            # An orphan takeover can preserve the failed release SHA rather
            # than the new request SHA. Reconstruct the exact persisted holder
            # so Job retry remains idempotent after takeover.
            recovery = observed_holder
            failed_holder = Holder(
                repo=observed_holder.repo, workflow=observed_holder.workflow,
                run_id=observed_holder.run_id.removesuffix("-recovery"),
                run_number=observed_holder.run_number, job="deploy-fleet" if observed_holder.ring == "fleet" else "deploy-canary",
                ring=observed_holder.ring, release_sha=observed_holder.release_sha,
                restored_sha=observed_holder.restored_sha,
            )
            if state.phase == "completed":
                return 0
            if state.phase not in {"recovering", "rollback-started", "evidence-pending"}:
                raise FenceError(f"recovery owner has invalid phase={state.phase}")
            owned = Fence(recovery, api)
            owned.token = state.token
            break
        if state.holder == predecessor.serialized():
            if (
                state.token <= options["prior_fence_token"]
                and state.phase in Fence.TERMINAL_PHASES
            ):
                if time.monotonic() - started >= executor_deadline:
                    raise FenceError("executor did not acquire ring within its scheduling budget")
                time.sleep(1)
                continue
            owner_seen = True
            if state.phase in Fence.TERMINAL_PHASES:
                return 0
        elif not owner_seen and state.phase in Fence.TERMINAL_PHASES:
            # The recovery pod may schedule before the executor has acquired
            # the fence. Wait a bounded start window; do not reinterpret the
            # previous completed generation as this request's completion.
            if time.monotonic() - started >= executor_deadline:
                raise FenceError("executor did not acquire ring within its scheduling budget")
            time.sleep(1)
            continue
        else:
            same_lane = (
                observed_holder.repo == predecessor.repo
                and observed_holder.workflow == predecessor.workflow
                and observed_holder.ring == predecessor.ring
            )
            # A new request may arrive while an older executor/recovery owner
            # is orphaned. The executor cannot acquire a non-terminal Lease;
            # after a full unchanged Lease duration, let this independently
            # scheduled recovery Job adopt and roll back that exact failed SHA.
            # A token newer than the request snapshot is a real successor and
            # remains untouchable.
            if (
                not same_lane
                or state.phase in Fence.TERMINAL_PHASES
                or state.token != options["prior_fence_token"]
            ):
                return 0
            if state.phase not in Fence.ACTIVE_PHASES:
                raise FenceError(f"orphan ring owner has invalid phase={state.phase}")
            owner_seen = True
            if expired:
                failed_holder = observed_holder
                recovery = Holder(
                    repo=observed_holder.repo, workflow=observed_holder.workflow,
                    run_id=requested_recovery.run_id,
                    run_number=requested_recovery.run_number, job="recover",
                    ring=observed_holder.ring, release_sha=observed_holder.release_sha,
                    restored_sha=predecessor.restored_sha,
                )
                owned = Fence(observed_holder, api).takeover(state, recovery)
                break
        if expired:
            break
        if time.monotonic() - started >= hard_deadline:
            raise FenceError("owner remained live beyond the recovery watch budget")
        time.sleep(RENEW_INTERVAL_SECONDS)
    if state.holder == predecessor.serialized():
        owned = predecessor_fence.takeover(state, recovery)
    renew_stop = threading.Event()
    renew_failures: list[BaseException] = []
    renewer = threading.Thread(
        target=_renew_only_loop,
        args=(owned, renew_stop, renew_failures), daemon=True,
    )
    renewer.start()
    try:
        current = owned.validate({"recovering", "rollback-started", "evidence-pending"})
        if current.phase == "recovering":
            # Takeover is incomplete until every extant target carries the new token.
            claim_existing_targets(
                owned,
                allow_absent=True,
                expected_token_ceiling=owned.token - 1,
                discover_dynamic=True,
            )
            current = owned.phase("rollback-started", {"recovering"})
        env = {**os.environ, **{f"RING_{key.upper()}": getattr(recovery, key) for key in recovery.__dataclass_fields__}}
        env["RING_FENCE_TOKEN"] = str(owned.token)
        env["RING_RECOVERY_REASON"] = "owner_lease_expired"
        env["RING_KUBECTL_REAL"] = "/usr/local/bin/kubectl"
        env["PATH"] = f"/app/.forgejo/scripts:{env.get('PATH', '')}"
        if current.phase == "rollback-started":
            command = (
                ["/bin/bash", "/app/.forgejo/scripts/ring-cleanup-canary.sh"]
                if options["operation"] == "cleanup-canary"
                else ["/app/.venv/bin/python", "/app/.forgejo/scripts/ring-recovery.py"]
            )
            result = subprocess.run(command, env=env, text=True)
            if result.returncode:
                return result.returncode
            if renew_failures:
                raise FenceError("lease renewal lost during rollback")
            owned.phase("evidence-pending", {"rollback-started"})
        if options["operation"] == "cleanup-canary":
            owned.phase("completed", {"evidence-pending"})
            return 0
        # record-rollout-failure remints Pulse auth and redacts credentials.
        # HIVE-2819 (D2): evidence recording is paperwork AFTER the rollback
        # has converged — a delivery failure here must not mask the completed
        # rollback (run 3404: a 20s Pulse POST timeout reded a healthy
        # rollback). The lease is already parked at evidence-pending (written
        # when the rollback branch completed); print a ::warning:: and return
        # 0 — a later recover re-runs ONLY the evidence. Do NOT rewrite the
        # phase here: evidence-pending is not writable from evidence-pending.
        record = subprocess.run([
            "/bin/bash", "/app/.forgejo/scripts/record-rollout-failure.sh",
            failed_holder.ring, failed_holder.release_sha, failed_holder.restored_sha,
            "owner_lease_expired rollback_rc=0",
        ], env=env, text=True)
        if record.returncode:
            print(
                f"::warning::rollback evidence recording failed (rc={record.returncode}) "
                f"ring={failed_holder.ring} bad_sha={failed_holder.release_sha} "
                f"restored_sha={failed_holder.restored_sha} — the rollback itself already "
                f"converged; a later recover retries only the evidence, or file/close the "
                f"Pulse rollback artifact manually",
                file=sys.stderr,
            )
            return 0
        if renew_failures:
            raise FenceError("lease renewal lost during evidence recording")
        owned.phase("completed", {"evidence-pending"})
        return 0
    finally:
        renew_stop.set()
        renewer.join(timeout=2)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    sub = result.add_subparsers(dest="mode", required=True)
    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("--ring", choices=("canary", "fleet"), required=True)
    submit_parser.add_argument("--operation", choices=("release", "cleanup-canary"), default="release")
    submit_parser.add_argument("--release-sha", required=True)
    submit_parser.add_argument("--restored-sha", required=True)
    submit_parser.add_argument("--run-id", required=True)
    submit_parser.add_argument("--run-number", required=True)
    submit_parser.add_argument("--job", required=True)
    submit_parser.add_argument("--event-name", choices=("push", "workflow_dispatch"), default="push")
    submit_parser.add_argument("--hold", choices=("true", "false"), default="false")
    submit_parser.add_argument("--hive725-phase", choices=("phase1", "finalize"), default="phase1")
    submit_parser.add_argument("--source-tree", required=True)
    submit_parser.add_argument("--kubectl", default="kubectl")
    for mode in ("execute", "recover"):
        item = sub.add_parser(mode)
        item.add_argument("--request", required=True)
    sub.add_parser("commit")
    return result


def commit(_args: argparse.Namespace) -> int:
    holder = Holder.from_env()
    if holder.ring != "fleet":
        raise FenceError("only the fleet executor may commit INV-F")
    fence = Fence(holder, Kubectl())
    fence.token = int(os.environ["RING_FENCE_TOKEN"])
    for attempt in range(5):
        try:
            fence.phase("committed", {"active"})
            return 0
        except Conflict:
            # The renewer can win the resourceVersion race while preserving
            # the same holder/token/active phase. Retry only that exact state.
            state = fence.read()
            if (
                state.holder != holder.serialized()
                or state.token != fence.token
                or state.phase not in {"active", "committed"}
            ):
                raise
            if state.phase == "committed":
                return 0
            if attempt == 4:
                raise
            time.sleep(0.1)
    raise FenceError("unreachable commit retry state")


def main() -> int:
    args = parser().parse_args()
    try:
        if args.mode in {"execute", "recover"}:
            configure_incluster_kubeconfig()
        return {"submit": submit, "execute": execute, "recover": recover, "commit": commit}[args.mode](args)
    except (FenceError, Conflict, KeyError, json.JSONDecodeError) as exc:
        # PLAT-11234: the fail-closed error line must live INSIDE a diagnostics
        # group — run 3451 recorded an empty group (job existed but had no
        # logs) while the real evidence (the API error body) sat on the
        # fail-closed line. A non-empty group on every fail-closed failure
        # makes the log classifier's job deterministic.
        print("::group::ring executor failure diagnostics", file=sys.stderr)
        print(f"::error::ring executor fail-closed: {exc}", file=sys.stderr)
        print("::endgroup::", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
