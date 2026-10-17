import dataclasses
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from pathlib import Path

import yaml
from installation_profile_test_cases import InstallationProfileCases


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".forgejo" / "scripts"
SHA = "a" * 40
OLD_SHA = "b" * 40


def run(cmd, *, env=None, cwd=ROOT):
    return subprocess.run(
        cmd,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
    )


class RolloutManifestTests(unittest.TestCase):
    def test_ci_reconciles_only_exact_recovery_rbac_before_submit(self):
        narrow = [item for item in yaml.safe_load_all(
            (ROOT / "k8s" / "hive-ring-recover-rbac.yaml").read_text()
        ) if item]
        self.assertGreaterEqual(len(narrow), 14)
        self.assertEqual(
            [item["kind"] for item in narrow[:8]],
            ["Role", "RoleBinding", "Role", "RoleBinding", "Role", "RoleBinding",
             "Role", "RoleBinding"],
        )
        self.assertIn(
            "hive-ring-recover-forgejo-token",
            [item["metadata"]["name"] for item in narrow],
        )
        (role, binding, drift_role, drift_binding,
         billing_role, billing_binding, execute_role, execute_binding) = narrow[:8]
        self.assertEqual(role["metadata"], {
            "name": "hive-ring-recover-fleet-liveness",
            "namespace": "shizuha-hive",
        })
        self.assertEqual(binding, {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {
                "name": "hive-ring-recover-fleet-liveness",
                "namespace": "shizuha-hive",
            },
            "subjects": [{
                "kind": "ServiceAccount", "name": "hive-ring-recover",
                "namespace": "origin",
            }],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                "name": "hive-ring-recover-fleet-liveness",
            },
        })
        normalized_rules = [(
            tuple(rule["apiGroups"]), tuple(rule["resources"]),
            tuple(rule.get("resourceNames", [])), tuple(rule["verbs"]),
        ) for rule in role["rules"]]
        self.assertEqual(normalized_rules, [
            (("batch",), ("cronjobs",),
             ("fleet-agent-liveness-reconciler",), ("get", "patch")),
            (("",), ("configmaps",), ("hive-tiers",), ("get", "patch")),
        ])
        self.assertEqual(drift_role, {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {
                "name": "hive-ring-recover-harness-drift",
                "namespace": "shizuha-hive",
            },
            "rules": [{
                "apiGroups": ["batch"], "resources": ["cronjobs"],
                "resourceNames": ["fleet-harness-drift-roll"],
                "verbs": ["get", "patch"],
            }],
        })
        self.assertEqual(drift_binding, {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {
                "name": "hive-ring-recover-harness-drift",
                "namespace": "shizuha-hive",
            },
            "subjects": [{
                "kind": "ServiceAccount", "name": "hive-ring-recover",
                "namespace": "origin",
            }],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                "name": "hive-ring-recover-harness-drift",
            },
        })
        self.assertEqual(billing_role, {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {
                "name": "hive-ring-recover-compute-billing",
                "namespace": "shizuha-hive",
            },
            "rules": [{
                "apiGroups": ["batch"], "resources": ["cronjobs"],
                "resourceNames": ["hive-compute-billing"],
                "verbs": ["get", "patch"],
            }],
        })
        self.assertEqual(billing_binding, {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {
                "name": "hive-ring-recover-compute-billing",
                "namespace": "shizuha-hive",
            },
            "subjects": [{
                "kind": "ServiceAccount", "name": "hive-ring-recover",
                "namespace": "origin",
            }],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                "name": "hive-ring-recover-compute-billing",
            },
        })
        self.assertEqual(execute_role, {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {
                "name": "hive-ring-execute-tier-config",
                "namespace": "shizuha-hive",
            },
            "rules": [{
                "apiGroups": [""], "resources": ["configmaps"],
                "resourceNames": ["hive-tiers"], "verbs": ["get", "patch"],
            }],
        })
        self.assertEqual(execute_binding, {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {
                "name": "hive-ring-execute-tier-config",
                "namespace": "shizuha-hive",
            },
            "subjects": [{
                "kind": "ServiceAccount", "name": "hive-ring-execute",
                "namespace": "origin",
            }],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                "name": "hive-ring-execute-tier-config",
            },
        })

        bootstrap = list(yaml.safe_load_all(
            (ROOT / "k8s" / "hive-ring-executor-rbac.yaml").read_text()
        ))
        full_role = next(
            item for item in bootstrap
            if item.get("kind") == "Role"
            and item.get("metadata", {}).get("name") == "hive-ring-recover"
            and item.get("metadata", {}).get("namespace") == "shizuha-hive"
        )
        cron_rule = next(
            rule for rule in full_role["rules"]
            if rule.get("apiGroups") == ["batch"]
            and rule.get("resources") == ["cronjobs"]
        )
        self.assertIn("fleet-agent-liveness-reconciler", cron_rule["resourceNames"])
        self.assertIn("hive-compute-billing", cron_rule["resourceNames"])
        self.assertEqual(cron_rule["verbs"], ["get", "patch"])

        execute_role = next(
            item for item in bootstrap
            if item.get("kind") == "Role"
            and item.get("metadata", {}).get("name") == "hive-ring-execute"
            and item.get("metadata", {}).get("namespace") == "shizuha-hive"
        )
        tier_rule = next(
            rule for rule in execute_role["rules"]
            if rule.get("apiGroups") == [""]
            and rule.get("resources") == ["configmaps"]
        )
        self.assertEqual(tier_rule.get("resourceNames"), ["hive-tiers"])
        self.assertEqual(tier_rule["verbs"], ["get", "patch"])

        workflow = (ROOT / ".forgejo" / "workflows" / "deploy.yml").read_text()
        self.assertEqual(
            workflow.count("kubectl apply -f k8s/hive-ring-recover-rbac.yaml"), 2,
        )
        self.assertEqual(
            workflow.count("kubectl diff -f k8s/hive-ring-recover-rbac.yaml"), 2,
        )
        self.assertNotIn('RECOVERY_IDENTITY=', workflow)
        self.assertNotIn('--as="$RECOVERY_IDENTITY"', workflow)
        self.assertNotIn(
            "kubectl apply -f k8s/hive-ring-executor-rbac.yaml", workflow,
        )

    def test_all_workflow_kubectl_bootstraps_use_canonical_anonymous_artifact(self):
        workflows = "\n".join(
            path.read_text()
            for path in sorted((ROOT / ".forgejo" / "workflows").glob("*.yml"))
        )
        canonical = "api/packages/actions/generic/kubectl/v1.34.1/kubectl-"
        fetches = workflows.count(canonical)
        self.assertGreater(fetches, 0)
        self.assertEqual(workflows.count("anonymous-fetch: sanctioned"), fetches)
        self.assertNotIn("shizuha-labs/generic/ci-kubectl", workflows)
        lines = workflows.splitlines()
        for index, line in enumerate(lines):
            if canonical in line:
                request = "\n".join(lines[max(0, index - 4):index + 1])
                self.assertNotRegex(request, r"(?:-H|--header=)[^\n]*Authorization")

    def test_k3s_multicall_client_keeps_kubectl_argv0(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("COPY kubectl /usr/local/bin/kubectl\n", dockerfile)
        self.assertNotIn("/usr/local/bin/kubectl-real", dockerfile)

        for path in (
            SCRIPTS / "kubectl",
            SCRIPTS / "ring_executor.py",
            SCRIPTS / "ring_fence.py",
        ):
            content = path.read_text()
            self.assertNotIn("/usr/local/bin/kubectl-real", content, path)

    def test_incluster_kubeconfig_preserves_projected_token_rotation(self):
        loader = importlib.machinery.SourceFileLoader(
            "ring_executor_kubeconfig_test", str(SCRIPTS / "ring_executor.py")
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"ring_fence": mock.Mock(FenceError=RuntimeError)}):
            loader.exec_module(module)

        with tempfile.TemporaryDirectory() as td:
            service_account = Path(td) / "serviceaccount"
            service_account.mkdir()
            (service_account / "token").write_text("rotating-token")
            (service_account / "ca.crt").write_text("ca")
            output = Path(td) / "kubeconfig"
            with (
                mock.patch.object(module, "Path", side_effect=lambda value: (
                    service_account
                    if value == "/var/run/secrets/kubernetes.io/serviceaccount"
                    else output
                    if value == "/tmp/hive-ring-kubeconfig"
                    else Path(value)
                )),
                mock.patch.dict(os.environ, {
                    "KUBERNETES_SERVICE_HOST": "10.43.0.1",
                    "KUBERNETES_SERVICE_PORT_HTTPS": "443",
                }, clear=False),
            ):
                module.configure_incluster_kubeconfig()

            value = json.loads(output.read_text())
            self.assertEqual(value["clusters"][0]["cluster"]["server"], "https://10.43.0.1:443")
            self.assertEqual(value["users"][0]["user"], {"tokenFile": str(service_account / "token")})
            self.assertNotIn("rotating-token", output.read_text())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_ring_templates_are_clean_and_addressable(self):
        with tempfile.TemporaryDirectory() as td:
            rendered = []
            for name in (
                "hive-staging-deps.yaml.tpl",
                "hive-staging-app.yaml.tpl",
                "hive-canary.yaml.tpl",
                "hive-fleet-probe.yaml.tpl",
            ):
                out = Path(td) / name.removesuffix(".tpl")
                result = run(
                    [
                        SCRIPTS / "render-ring-manifest.sh",
                        ROOT / "k8s" / "rollout" / name,
                        f"localhost:30500/shizuha-hive:hive-{SHA[:7]}",
                        SHA,
                        out,
                    ]
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                rendered.extend(yaml.safe_load_all(out.read_text()))

        objects = [x for x in rendered if x]
        for obj in objects:
            metadata = obj.get("metadata", {})
            self.assertNotIn("uid", metadata)
            self.assertNotIn("resourceVersion", metadata)
            self.assertNotIn("managedFields", metadata)
            self.assertNotIn("creationTimestamp", metadata)
            self.assertNotIn("status", obj)

        staging = next(
            x for x in objects
            if x["kind"] == "Deployment"
            and x["metadata"]["name"] == "hive"
            and x["metadata"]["namespace"] == "staging"
        )
        staging_text = yaml.safe_dump(staging)
        self.assertNotIn("secretRef", staging_text)
        self.assertNotIn("configMapRef", staging_text)
        self.assertIn("hive-staging-postgres", staging_text)
        self.assertIn("hive-staging-redis", staging_text)

        canary_svc = next(
            x for x in objects
            if x["kind"] == "Service" and x["metadata"]["name"] == "hive-canary"
        )
        self.assertEqual(canary_svc["spec"]["selector"]["app"], "hive-canary")
        self.assertEqual(canary_svc["spec"]["selector"]["build_sha"], SHA)
        canary_probe = next(
            x for x in objects
            if x["kind"] == "Probe" and x["spec"]["jobName"].startswith("shizuha-hive-canary-")
        )
        target = canary_probe["spec"]["targets"]["staticConfig"]["static"][0]
        self.assertIn("hive-canary.shizuha-hive", target)
        self.assertIn(":8030/api/health/", target)

    def test_renderer_rejects_non_sha_and_unexpected_image(self):
        template = ROOT / "k8s" / "rollout" / "hive-fleet-probe.yaml.tpl"
        with tempfile.NamedTemporaryFile() as out:
            bad_sha = run([SCRIPTS / "render-ring-manifest.sh", template, "x", "short", out.name])
            self.assertNotEqual(bad_sha.returncode, 0)


class SuccessfulReleaseAnchorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = SCRIPTS / "record-successful-release.py"
        spec = importlib.util.spec_from_file_location("record_successful_release", path)
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    @staticmethod
    def deployment(*, live_sha=SHA, live_image=None, ready=2):
        image = live_image or f"localhost:30500/shizuha-hive:hive-{live_sha[:7]}"
        return {
            "metadata": {
                "resourceVersion": "1234",
                "generation": 9,
                "labels": {"build_sha": live_sha},
                "annotations": {
                    "last-successful-sha": OLD_SHA,
                    "shizuha.io/last-successful-sha": OLD_SHA,
                },
            },
            "spec": {
                "replicas": 2,
                "template": {
                    "metadata": {"labels": {"build_sha": live_sha}},
                    "spec": {
                        "containers": [
                            {"name": "hive", "image": image},
                        ]
                    },
                },
            },
            "status": {
                "observedGeneration": 9,
                "updatedReplicas": ready,
                "readyReplicas": ready,
                "availableReplicas": ready,
            },
        }

    def test_atomic_patch_tests_exact_release_before_advancing_both_anchors(self):
        image = f"localhost:30500/shizuha-hive:hive-{SHA[:7]}"
        patch = self.module.build_atomic_anchor_patch(
            self.deployment(), SHA, image, ["hive"]
        )
        operations = {(item["op"], item["path"]): item["value"] for item in patch}
        self.assertEqual(
            operations[("test", "/metadata/resourceVersion")], "1234"
        )
        self.assertEqual(
            operations[("test", "/spec/template/spec/containers/0/image")], image
        )
        self.assertEqual(
            operations[("add", "/metadata/annotations/last-successful-sha")], SHA
        )
        self.assertEqual(
            operations[
                ("add", "/metadata/annotations/shizuha.io~1last-successful-sha")
            ],
            SHA,
        )

    def test_rolled_back_image_cannot_be_recorded_as_new_release(self):
        expected = f"localhost:30500/shizuha-hive:hive-{SHA[:7]}"
        rolled_back = f"localhost:30500/shizuha-hive:hive-{OLD_SHA[:7]}"
        with self.assertRaisesRegex(ValueError, "template build_sha|image="):
            self.module.build_atomic_anchor_patch(
                self.deployment(live_sha=OLD_SHA, live_image=rolled_back),
                SHA,
                expected,
                ["hive"],
            )

    def test_unavailable_release_cannot_advance_anchor(self):
        expected = f"localhost:30500/shizuha-hive:hive-{SHA[:7]}"
        with self.assertRaisesRegex(ValueError, "readyReplicas=1"):
            self.module.build_atomic_anchor_patch(
                self.deployment(ready=1),
                SHA,
                expected,
                ["hive"],
            )


class TypedConvergenceTests(unittest.TestCase):
    def test_default_budget_covers_liveness_repair_and_min_ready_window(self):
        gate = (SCRIPTS / "ring-gate.sh").read_text()
        workflow = "\n".join(
            (SCRIPTS / name).read_text()
            for name in ("ring-release-canary.sh", "ring-release-fleet.sh")
        )
        self.assertIn(
            'CONVERGENCE_TIMEOUT_SECONDS="${CONVERGENCE_TIMEOUT_SECONDS:-600}"',
            gate,
        )
        self.assertIn('"minReadySeconds": 180', workflow)

    @staticmethod
    def topology(*, replicas=1, generation=1, suffix="1"):
        pods = []
        endpoints = []
        for index in range(replicas):
            number = index + 1
            uid = f"uid-{suffix}-{number}"
            ip = f"10.0.{suffix}.{number}"
            pods.append(
                {
                    "metadata": {
                        "name": f"hive-{suffix}-{number}",
                        "uid": uid,
                        "resourceVersion": f"pod-rv-{suffix}-{number}",
                        "labels": {
                            "app": "hive",
                            "release_ring": "staging",
                            "build_sha": SHA,
                        },
                        "ownerReferences": [
                            {
                                "apiVersion": "apps/v1",
                                "kind": "ReplicaSet",
                                "name": f"hive-rs-{suffix}",
                                "uid": f"rs-uid-{suffix}",
                                "controller": True,
                            }
                        ],
                    },
                    "spec": {"nodeName": f"node-{number}"},
                    "status": {
                        "phase": "Running",
                        "podIP": ip,
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                }
            )
            endpoints.append(
                {
                    "addresses": [ip],
                    "conditions": {"ready": True, "terminating": False},
                    "targetRef": {
                        "kind": "Pod",
                        "namespace": "staging",
                        "uid": uid,
                    },
                }
            )
        deployment = {
            "metadata": {
                "name": "hive",
                "namespace": "staging",
                "uid": f"deployment-uid-{suffix}",
                "generation": generation,
                "resourceVersion": f"deploy-rv-{suffix}",
            },
            "spec": {
                "replicas": replicas,
                "template": {"metadata": {"labels": {"build_sha": SHA}}},
            },
            "status": {
                "observedGeneration": generation,
                "availableReplicas": replicas,
                "readyReplicas": replicas,
                "updatedReplicas": replicas,
            },
        }
        service = {
            "metadata": {"name": "hive", "resourceVersion": "svc-rv-1"},
            "spec": {
                "selector": {
                    "app": "hive",
                    "release_ring": "staging",
                    "build_sha": SHA,
                }
            },
        }
        replicasets = {
            "items": [
                {
                    "metadata": {
                        "name": f"hive-rs-{suffix}",
                        "namespace": "staging",
                        "uid": f"rs-uid-{suffix}",
                        "resourceVersion": f"rs-rv-{suffix}",
                        "ownerReferences": [
                            {
                                "apiVersion": "apps/v1",
                                "kind": "Deployment",
                                "name": "hive",
                                "uid": f"deployment-uid-{suffix}",
                                "controller": True,
                            }
                        ],
                    }
                }
            ]
        }
        slices = {
            "items": [
                {
                    "metadata": {
                        "name": "hive-slice",
                        "resourceVersion": f"slice-rv-{suffix}",
                        "labels": {"kubernetes.io/service-name": "hive"},
                    },
                    "endpoints": endpoints,
                }
            ]
        }
        return deployment, service, replicasets, {"items": pods}, slices

    def fake_environment(self, directory, *, first=None, settled=None):
        directory = Path(directory)
        fixtures = directory / "fixtures"
        fixtures.mkdir()
        first = first or self.topology()
        settled = settled or first
        for variant, snapshot in (("1", first), ("2", settled)):
            for name, value in zip(
                ("deployment", "service", "replicasets", "pods", "slices"), snapshot
            ):
                (fixtures / f"{name}-{variant}.json").write_text(json.dumps(value))

        (directory / "kubectl").write_text(
            textwrap.dedent(
                """\
                #!/bin/bash
                args="$*"
                if [[ "${TOPOLOGY_MODE:-green}" == hang && "$args" == *"get deployment"* ]]; then
                  /bin/sleep 30
                fi
                case "$args" in
                  *"blackbox-exporter"*"-o json")
                    printf '%s\\n' '{"items":[{"metadata":{"name":"blackbox-independent","uid":"probe-uid"},"spec":{"nodeName":"node-independent","containers":[{"name":"blackbox-exporter"}]},"status":{"phase":"Running","podIP":"10.9.0.1","conditions":[{"type":"Ready","status":"True"}]}}]}' ;;
                  *"exec blackbox-independent"*)
                    case "${INDEPENDENT_MODE:-green}" in unhealthy) printf '503\\n';; missing) exit 1;; *) printf '200\\n';; esac ;;
                  *"get deployment"*"-o json")
                    n=$(cat "$SNAP_COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$SNAP_COUNT"
                    variant=2; [ "$n" -eq 1 ] && variant=1
                    cat "$FIXTURES/deployment-$variant.json" ;;
                  *"get service"*"-o json")
                    n=$(cat "$SNAP_COUNT"); variant=2; [ "$n" -eq 1 ] && variant=1
                    cat "$FIXTURES/service-$variant.json" ;;
                  *"get replicasets.apps"*"-o json")
                    n=$(cat "$SNAP_COUNT"); variant=2; [ "$n" -eq 1 ] && variant=1
                    cat "$FIXTURES/replicasets-$variant.json" ;;
                  *"get pods -o json")
                    n=$(cat "$SNAP_COUNT"); variant=2; [ "$n" -eq 1 ] && variant=1
                    cat "$FIXTURES/pods-$variant.json" ;;
                  *"get endpointslices.discovery.k8s.io"*)
                    [ "${TOPOLOGY_MODE:-green}" != missing ] || exit 1
                    n=$(cat "$SNAP_COUNT"); variant=2; [ "$n" -eq 1 ] && variant=1
                    cat "$FIXTURES/slices-$variant.json" ;;
                  *) exit 0 ;;
                esac
                """
            )
        )
        (directory / "curl").write_text(
            textwrap.dedent(
                """\
                #!/bin/bash
                args="$*"
                if [[ "$args" == *"hive.staging.svc"* ]]; then
                  n=$(cat "$SERVICE_COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$SERVICE_COUNT"
                  if [[ "${RUNNER_MODE:-green}" == transient && "$n" -le "${RUNNER_FAIL_COUNT:-2}" ]]; then exit 7; fi
                  if [[ "${RUNNER_MODE:-green}" == after_pre_bake && "$n" -gt "${RUNNER_PASS_COUNT:-3}" ]]; then exit 7; fi
                  [[ "${RUNNER_MODE:-green}" != unhealthy ]] || { printf '503 0.010'; exit 0; }
                elif [[ "$args" == *"10.0."* ]]; then
                  [[ "${RUNNER_DIRECT_MODE:-green}" != unreachable ]] || exit 7
                  [[ "${RUNNER_MODE:-green}" != unhealthy ]] || { printf '503 0.010'; exit 0; }
                fi
                printf '200 0.010'
                """
            )
        )
        (directory / "sleep").write_text(
            "#!/bin/sh\n"
            "[ \"${REAL_SLEEP:-0}\" = 1 ] && exec /bin/sleep \"$@\"\n"
            "case \"${REAL_SLEEP:-0}:$1\" in bounded:0.*|bounded:1|bounded:1.0*) exec /bin/sleep \"$@\";; esac\n"
            "exit 0\n"
        )
        for name in ("kubectl", "curl", "sleep"):
            (directory / name).chmod(0o755)
        return {
            "PATH": f"{directory}:{os.environ['PATH']}",
            "FIXTURES": str(fixtures),
            "SNAP_COUNT": str(directory / "snap-count"),
            "SERVICE_COUNT": str(directory / "service-count"),
            "SOAK_SECONDS": "0",
            "MIN_SAMPLES": "1",
            "CONVERGENCE_TIMEOUT_SECONDS": "10",
            "CONVERGENCE_POLL_SECONDS": "1",
            "CONVERGENCE_JITTER_MILLISECONDS": "0",
            "CONVERGENCE_SUCCESS_SAMPLES": "3",
        }

    def run_topology(self, snapshot):
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for name, value in zip(
                ("deployment", "service", "replicasets", "pods", "slices"), snapshot
            ):
                path = Path(td) / f"{name}.json"
                path.write_text(json.dumps(value))
                paths.append(path)
            result = run([SCRIPTS / "ring-topology.py", *paths, SHA])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def selector_collision(index, *, phase="Succeeded", ready=False, deleting=False):
        pod = {
            "metadata": {
                "name": f"hive-job-{index}",
                "uid": f"job-pod-uid-{index}",
                "resourceVersion": f"job-pod-rv-{index}",
                "labels": {
                    "app": "hive",
                    "release_ring": "staging",
                    "build_sha": SHA,
                },
                "ownerReferences": [
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "name": f"hive-job-{index}",
                        "uid": f"job-uid-{index}",
                        "controller": True,
                    }
                ],
            },
            "spec": {"nodeName": "node-job"},
            "status": {
                "phase": phase,
                "podIP": f"10.9.0.{index + 1}",
                "conditions": [
                    {"type": "Ready", "status": "True" if ready else "False"}
                ],
            },
        }
        if deleting:
            pod["metadata"]["deletionTimestamp"] = "2026-07-13T00:00:00Z"
        return pod

    def test_terminal_selector_collisions_are_diagnostic_not_promotion_members(self):
        snapshot = list(self.topology(replicas=2))
        for index in range(15):
            snapshot[3]["items"].append(
                self.selector_collision(
                    index,
                    phase="Failed" if index % 2 else "Succeeded",
                    deleting=index == 14,
                )
            )
        parsed = self.run_topology(snapshot)
        self.assertEqual(parsed["outcome"], "STABLE", parsed)
        self.assertEqual(len(parsed["ignoredSelectorCollisions"]), 15)

    def test_foreign_ready_endpoint_from_selector_collision_fails_closed(self):
        snapshot = list(self.topology(replicas=2))
        collision = self.selector_collision(20, phase="Running", ready=True)
        snapshot[3]["items"].append(collision)
        snapshot[4]["items"][0]["endpoints"].append(
            {
                "addresses": [collision["status"]["podIP"]],
                "conditions": {"ready": True, "terminating": False},
                "targetRef": {
                    "kind": "Pod",
                    "namespace": "staging",
                    "uid": collision["metadata"]["uid"],
                },
            }
        )
        parsed = self.run_topology(snapshot)
        self.assertEqual(parsed["outcome"], "TOPOLOGY_CONVERGING", parsed)

    def test_old_replicaset_ready_endpoint_fails_closed(self):
        snapshot = list(self.topology(replicas=2))
        old_rs_uid = "rs-uid-old"
        snapshot[2]["items"].append(
            {
                "metadata": {
                    "name": "hive-rs-old",
                    "namespace": "staging",
                    "uid": old_rs_uid,
                    "resourceVersion": "rs-rv-old",
                    "ownerReferences": [
                        {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "name": "hive",
                            "uid": snapshot[0]["metadata"]["uid"],
                            "controller": True,
                        }
                    ],
                }
            }
        )
        old_pod = self.selector_collision(21, phase="Running", ready=True)
        old_pod["metadata"]["labels"]["build_sha"] = "old-build"
        old_pod["metadata"]["ownerReferences"][0].update(
            {"kind": "ReplicaSet", "uid": old_rs_uid}
        )
        snapshot[3]["items"].append(old_pod)
        snapshot[4]["items"][0]["endpoints"].append(
            {
                "addresses": [old_pod["status"]["podIP"]],
                "conditions": {"ready": True, "terminating": False},
                "targetRef": {
                    "kind": "Pod",
                    "namespace": "staging",
                    "uid": old_pod["metadata"]["uid"],
                },
            }
        )
        parsed = self.run_topology(snapshot)
        self.assertEqual(parsed["outcome"], "TOPOLOGY_CONVERGING", parsed)

    def test_inactive_endpoint_targets_are_excluded_not_promotion_members(self):
        snapshot = list(self.topology())
        snapshot[4]["items"][0]["endpoints"].append(
            {
                "addresses": ["10.9.0.99"],
                "conditions": {"ready": False, "terminating": True},
                "targetRef": {
                    "kind": "Pod",
                    "namespace": "staging",
                    "uid": "old-terminating-pod",
                },
            }
        )
        parsed = self.run_topology(snapshot)
        self.assertEqual(parsed["outcome"], "STABLE", parsed)
        self.assertEqual(len(parsed["excludedEndpointSliceTargets"]), 1)

    def test_forged_build_labels_without_controller_ownership_do_not_pass(self):
        snapshot = list(self.topology())
        snapshot[3]["items"][0]["metadata"]["ownerReferences"][0]["uid"] = "foreign-rs"
        parsed = self.run_topology(snapshot)
        self.assertEqual(parsed["outcome"], "TOPOLOGY_CONVERGING", parsed)

    def test_endpoint_uid_address_and_identity_mismatches_fail_closed(self):
        mutations = {
            "missing_uid": lambda endpoint: endpoint["targetRef"].pop("uid"),
            "address_mismatch": lambda endpoint: endpoint.update({"addresses": ["10.99.0.1"]}),
            "foreign_namespace": lambda endpoint: endpoint["targetRef"].update(
                {"namespace": "other"}
            ),
            "foreign_kind": lambda endpoint: endpoint["targetRef"].update({"kind": "Service"}),
            "terminating": lambda endpoint: endpoint["conditions"].update(
                {"terminating": True}
            ),
            "not_ready": lambda endpoint: endpoint["conditions"].update({"ready": False}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                snapshot = list(self.topology())
                mutate(snapshot[4]["items"][0]["endpoints"][0])
                parsed = self.run_topology(snapshot)
                self.assertEqual(parsed["outcome"], "TOPOLOGY_CONVERGING", parsed)

    def test_duplicate_endpoint_pair_fails_closed(self):
        snapshot = list(self.topology())
        snapshot[4]["items"][0]["endpoints"].append(
            json.loads(json.dumps(snapshot[4]["items"][0]["endpoints"][0]))
        )
        parsed = self.run_topology(snapshot)
        self.assertEqual(parsed["outcome"], "TOPOLOGY_CONVERGING", parsed)

    def test_endpoint_replacement_resets_streak_then_passes(self):
        with tempfile.TemporaryDirectory() as td:
            env = self.fake_environment(td, first=self.topology(suffix="1"), settled=self.topology(suffix="2"))
            result = run([SCRIPTS / "ring-gate.sh", "staging", "staging", "hive", SHA], env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertGreaterEqual(result.stdout.count("success_streak_reset=true"), 3, result.stdout)
            self.assertIn("gate PASS", result.stdout)

    def test_desired_replica_change_resets_streak_then_passes(self):
        with tempfile.TemporaryDirectory() as td:
            env = self.fake_environment(
                td,
                first=self.topology(replicas=1, generation=1, suffix="1"),
                settled=self.topology(replicas=2, generation=2, suffix="2"),
            )
            result = run([SCRIPTS / "ring-gate.sh", "staging", "staging", "hive", SHA], env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("success_streak_reset=true", result.stdout)

    def test_runner_only_timeout_is_typed_and_recovers_from_independent_success(self):
        with tempfile.TemporaryDirectory() as td:
            env = self.fake_environment(td)
            env.update({"RUNNER_MODE": "transient", "RUNNER_FAIL_COUNT": "2"})
            result = run([SCRIPTS / "ring-gate.sh", "staging", "staging", "hive", SHA], env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("outcome=DATA_PLANE_UNREACHABLE", result.stdout)
            self.assertIn("node=node-independent", result.stdout)

    def test_stable_application_failure_is_typed_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            env = self.fake_environment(td)
            env.update({"RUNNER_MODE": "unhealthy", "INDEPENDENT_MODE": "unhealthy"})
            result = run([SCRIPTS / "ring-gate.sh", "staging", "staging", "hive", SHA], env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("outcome=APP_UNHEALTHY", result.stdout + result.stderr)

    def test_runner_direct_only_failure_is_data_plane_not_application(self):
        with tempfile.TemporaryDirectory() as td:
            env = self.fake_environment(td)
            env.update({
                "RUNNER_DIRECT_MODE": "unreachable",
                # Ownership closure adds one bounded ReplicaSet read before the
                # three probe vantages; leave enough wall-clock budget to reach
                # the typed data-plane classification under REAL_SLEEP.
                "CONVERGENCE_TIMEOUT_SECONDS": "2",
                "REAL_SLEEP": "1",
            })
            result = run([SCRIPTS / "ring-gate.sh", "staging", "staging", "hive", SHA], env=env)
            self.assertNotEqual(result.returncode, 0)
            output = result.stdout + result.stderr
            self.assertIn("runner_service=200", output)
            self.assertIn("runner_pod=000", output)
            self.assertIn("independent=200", output)
            self.assertIn("outcome=DATA_PLANE_UNREACHABLE", output)
            self.assertNotIn("outcome=APP_UNHEALTHY", output)

    def test_runner_only_failure_after_pre_bake_uses_typed_convergence(self):
        with tempfile.TemporaryDirectory() as td:
            env = self.fake_environment(td)
            env.update({
                "RUNNER_MODE": "after_pre_bake",
                "RUNNER_PASS_COUNT": "3",
                "CONVERGENCE_TIMEOUT_SECONDS": "5",
                "REAL_SLEEP": "bounded",
            })
            result = run([SCRIPTS / "ring-gate.sh", "staging", "staging", "hive", SHA], env=env)
            self.assertNotEqual(result.returncode, 0)
            output = result.stdout + result.stderr
            self.assertIn("pre_bake convergence PASS", output)
            self.assertIn("entering typed exact-target convergence", output)
            self.assertIn("phase=bake_failure", output)
            self.assertIn("outcome=DATA_PLANE_UNREACHABLE", output)
            self.assertNotIn("outcome=APP_UNHEALTHY", output)

    def test_missing_topology_evidence_is_unknown_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            env = self.fake_environment(td)
            env.update({
                "TOPOLOGY_MODE": "missing",
                "CONVERGENCE_TIMEOUT_SECONDS": "1",
                "REAL_SLEEP": "1",
            })
            result = run([SCRIPTS / "ring-gate.sh", "staging", "staging", "hive", SHA], env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("outcome=UNKNOWN", result.stdout + result.stderr)

    def test_hanging_kubectl_is_bounded_by_total_deadline_and_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            env = self.fake_environment(td)
            env.update({"TOPOLOGY_MODE": "hang", "CONVERGENCE_TIMEOUT_SECONDS": "1"})
            started = time.monotonic()
            result = run([SCRIPTS / "ring-gate.sh", "staging", "staging", "hive", SHA], env=env)
            elapsed = time.monotonic() - started
            self.assertNotEqual(result.returncode, 0)
            self.assertLess(elapsed, 4.0, result.stdout + result.stderr)
            output = result.stdout + result.stderr
            self.assertIn("outcome=UNKNOWN", output)
            self.assertIn("convergence budget", output)

    def test_term_during_capture_cannot_preempt_canary_cleanup(self):
        release = (SCRIPTS / "ring-release-canary.sh").read_text()
        function = "r1_failure()" + release.split("r1_failure()", 1)[1].split(
            "\n.forgejo/scripts/render-ring-manifest.sh", 1
        )[0]
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            started, calls = td / "capture-started", td / "kubectl-calls"
            capture = td / "capture"
            capture.write_text(f"#!/bin/sh\ntouch {started}\nsleep 1\necho artifact=configmap/test\n")
            record = td / "record"
            record.write_text("#!/bin/sh\nexit 0\n")
            watchdog = td / "watchdog"
            watchdog.write_text("#!/bin/sh\nexit 0\n")
            retire = td / "retire"
            retire.write_text("#!/bin/sh\nexit 0\n")
            kubectl = td / "kubectl"
            kubectl.write_text(f"#!/bin/sh\necho \"$*\" >> {calls}\nexit 0\n")
            for path in (capture, record, watchdog, retire, kubectl):
                path.chmod(0o755)
            function = function.replace(".forgejo/scripts/capture-ring-evidence.sh", str(capture))
            function = function.replace(".forgejo/scripts/record-rollout-failure.sh", str(record))
            function = function.replace(".forgejo/scripts/ring-watchdog.sh", str(watchdog))
            function = function.replace(".forgejo/scripts/retire-canary-probe.sh", str(retire))
            function = function.replace(".forgejo/scripts/kubectl", str(kubectl))
            script = f"""
            set -Eeuo pipefail
            {function}
            SHA={SHA}; LAST_SHA={OLD_SHA}; SHORT={SHA[:7]}
            r1_failure 143 signal=TERM
            """
            process = subprocess.Popen(
                ["bash", "-c", textwrap.dedent(script)],
                cwd=ROOT,
                env={**os.environ, "PATH": f"{td}:{os.environ['PATH']}"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(100):
                if started.exists():
                    break
                import time
                time.sleep(0.01)
            self.assertTrue(started.exists(), "capture did not start")
            process.send_signal(15)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143, stdout + stderr)
            self.assertIn("delete deploy hive-canary", calls.read_text())

    def test_evidence_capture_creates_owned_ttl_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            calls, manifests = td / "calls", td / "manifests"
            persisted_evidence = td / "persisted-evidence"
            persisted_job = td / "persisted-job.json"
            persisted_configmap = td / "persisted-configmap.json"
            gate_log = td / "gate.log"
            gate_log.write_text("x" * 720_000)
            curl_calls, ip_calls = td / "curl-calls", td / "ip-calls"
            kubectl = td / "kubectl"
            kubectl.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/bash
                    echo "$*" >> {calls}
                    case "$*" in
                      "create -f -")
                        source=$(mktemp)
                        cat > "$source"
                        cat "$source" >> {manifests}
                        python3 -c 'import json,sys,yaml; d=yaml.safe_load(open(sys.argv[1])); d["metadata"]["uid"]="evidence-job-uid"; json.dump(d,open(sys.argv[2],"w"))' "$source" {persisted_job}
                        [[ "${{FAKE_CREATE_COLLISION:-0}}" == 1 ]] && exit 1
                        ;;
                      "create -f "*)
                        source="${{@: -1}}"
                        cat "$source" >> {manifests}
                        python3 -c 'import json,sys; p=sys.argv[1]; o=sys.argv[2]; d=json.load(open(p)); d["metadata"]["uid"]="evidence-configmap-uid"; json.dump(d,open(o,"w"))' "$source" {persisted_configmap}
                        case "${{FAKE_CONFIGMAP_MISMATCH:-}}" in
                          digest) python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["metadata"]["annotations"]["shizuha.io/evidence-sha256"]="0"*64; json.dump(d,open(p,"w"))' {persisted_configmap} ;;
                          generation) python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["metadata"]["annotations"]["shizuha.io/ring-generation"]="0"*64; json.dump(d,open(p,"w"))' {persisted_configmap} ;;
                          owner) python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["metadata"]["ownerReferences"][0]["uid"]="other-job"; json.dump(d,open(p,"w"))' {persisted_configmap} ;;
                          size) python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["metadata"]["annotations"]["shizuha.io/evidence-bytes"]="699999"; json.dump(d,open(p,"w"))' {persisted_configmap} ;;
                        esac
                        [[ "${{FAKE_CREATE_COLLISION:-0}}" == 1 ]] && exit 1
                        ;;
                      *"get job"*"-o json"*) cat {persisted_job} ;;
                      *"create configmap"*)
                        for arg in "$@"; do
                          case "$arg" in --from-file=evidence.txt=*) cat "${{arg#--from-file=evidence.txt=}}" > {persisted_evidence} ;; esac
                        done
                        python3 -c 'import json,sys; print(json.dumps({{"apiVersion":"v1","kind":"ConfigMap","metadata":{{"name":sys.argv[1]}},"data":{{"evidence.txt":open(sys.argv[2]).read()}}}}))' "$5" {persisted_evidence}
                        ;;
                      *"get configmap"*"-o json"*) cat {persisted_configmap} ;;
                      *"get pods"*"build_sha="*"-o json"*) printf '%s\n' '{{"items":[{{"metadata":{{"name":"hive-canary-test","uid":"pod-uid"}},"spec":{{"nodeName":"node-a","containers":[{{"image":"registry/hive:{SHA[:7]}"}}]}},"status":{{"phase":"Running","podIP":"10.23.4.5","conditions":[{{"type":"Ready","status":"True"}}]}}}}]}}' ;;
                      *"get pods"*"-o name"*) exit 0 ;;
                      *) printf '{{"items":[]}}\n' ;;
                    esac
                    """
                )
            )
            curl = td / "curl"
            curl.write_text(f"#!/bin/sh\necho \"$*\" >> {curl_calls}\nprintf 'HTTP/1.1 200 OK\\n'\n")
            ip = td / "ip"
            ip.write_text(f"#!/bin/sh\necho \"$*\" >> {ip_calls}\nprintf '1: lo: mtu 65536\\n'\n")
            for path in (kubectl, curl, ip):
                path.chmod(0o755)
            result = run(
                [
                    SCRIPTS / "capture-ring-evidence.sh",
                    "canary",
                    "shizuha-hive",
                    "hive-canary",
                    "hive-canary",
                    SHA,
                    "test failure",
                    gate_log,
                ],
                env={
                    "PATH": f"{td}:{os.environ['PATH']}",
                    "RING_EVIDENCE_HOLD_SECONDS": "1",
                    "RING_REPO": "shizuha-labs/hive",
                    "RING_WORKFLOW": "deploy.yml",
                    "RING_RUN_ID": "1582",
                    "RING_RUN_NUMBER": "1582",
                    "RING_JOB": "deploy-canary",
                    "RING_RING": "canary",
                    "RING_RELEASE_SHA": SHA,
                    "RING_RESTORED_SHA": OLD_SHA,
                    "RING_FENCE_TOKEN": "7",
                    "FAKE_CREATE_COLLISION": "1",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("artifact=configmap/hive-ring-evidence-canary", result.stdout)
            rendered = manifests.read_text()
            self.assertIn("ttlSecondsAfterFinished: 900", rendered)
            self.assertIn("activeDeadlineSeconds: 61", rendered)
            call_log = calls.read_text()
            self.assertNotIn("label configmap", call_log)
            self.assertNotIn("patch configmap", call_log)
            self.assertIn("-details link show", ip_calls.read_text())
            probe_log = curl_calls.read_text()
            self.assertIn("-v", probe_log)
            self.assertIn("hive-canary.shizuha-hive.svc.cluster.local", probe_log)
            self.assertIn("http://10.23.4.5:8030/api/health/", probe_log)
            evidence = persisted_evidence.read_text()
            self.assertEqual(len(evidence.encode()), 700_000)
            self.assertIn("artifact_version=2", evidence)
            self.assertIn("pod_name\tpod_uid\tpod_ip\tnode\tphase\tready\timages", evidence)
            self.assertIn("started_at=", evidence)
            self.assertIn("timestamped verbose runner direct-pod probe", evidence)
            configmap = json.loads(persisted_configmap.read_text())
            metadata = configmap["metadata"]
            self.assertEqual(metadata["ownerReferences"][0]["uid"], "evidence-job-uid")
            self.assertEqual(metadata["labels"]["build_sha"], SHA)
            self.assertEqual(metadata["annotations"]["shizuha.io/evidence-bytes"], "700000")
            self.assertEqual(metadata["annotations"]["shizuha.io/fence-token"], "7")
            self.assertNotIn(
                "kubectl.kubernetes.io/last-applied-configuration",
                metadata["annotations"],
            )

            for mismatch_type, error in (
                ("digest", "identity read-back mismatch"),
                ("generation", "identity read-back mismatch"),
                ("owner", "owner read-back mismatch"),
                ("size", "identity read-back mismatch"),
            ):
                with self.subTest(collision_mismatch=mismatch_type):
                    calls_before_mismatch = calls.read_text().splitlines()
                    mismatch = run(
                        [
                            SCRIPTS / "capture-ring-evidence.sh",
                            "canary", "shizuha-hive", "hive-canary", "hive-canary",
                            SHA, "test failure", gate_log,
                        ],
                        env={
                            "PATH": f"{td}:{os.environ['PATH']}",
                            "RING_EVIDENCE_HOLD_SECONDS": "1",
                            "RING_REPO": "shizuha-labs/hive",
                            "RING_WORKFLOW": "deploy.yml",
                            "RING_RUN_ID": "1582",
                            "RING_RUN_NUMBER": "1582",
                            "RING_JOB": "deploy-canary",
                            "RING_RING": "canary",
                            "RING_RELEASE_SHA": SHA,
                            "RING_RESTORED_SHA": OLD_SHA,
                            "RING_FENCE_TOKEN": "7",
                            "FAKE_CREATE_COLLISION": "1",
                            "FAKE_CONFIGMAP_MISMATCH": mismatch_type,
                        },
                    )
                    self.assertNotEqual(mismatch.returncode, 0)
                    self.assertIn(f"evidence ConfigMap {error}", mismatch.stderr)
                    mismatch_calls = calls.read_text().splitlines()[len(calls_before_mismatch):]
                    self.assertFalse(any("delete configmap" in call for call in mismatch_calls))
                    self.assertFalse(any("delete job" in call for call in mismatch_calls))


class PulsePreflightTests(unittest.TestCase):
    @staticmethod
    def preflight_manifest_and_script():
        workflow = (ROOT / ".forgejo" / "workflows" / "deploy.yml").read_text()
        marker = "cat <<YAML | kubectl apply -f -\n"
        preflight = workflow.split("Pre-deploy Pulse auth validation", 1)[1]
        manifest = preflight.split(marker, 1)[1].split("\n          YAML", 1)[0]
        manifest = textwrap.dedent(manifest)
        # Model the outer shell's unquoted-heredoc expansion without requiring
        # a live cluster: JOB is expanded and escaped dollars reach the pod.
        manifest = manifest.replace("${JOB}", "test-job").replace("\\$", "$")
        parsed = yaml.safe_load(manifest)
        container = parsed["spec"]["template"]["spec"]["containers"][0]
        script = (
            container.get("args", [None])[0]
            or (SCRIPTS / "pulse-auth-preflight.py").read_text()
        )
        return workflow, parsed, script

    def test_preflight_keeps_bootstrap_credentials_in_kubelet_and_precedes_r0_mutation(self):
        workflow, manifest, _ = self.preflight_manifest_and_script()
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        secret_refs = {
            item["name"]: item["valueFrom"]["secretKeyRef"]
            for item in container["env"]
            if "valueFrom" in item
        }
        self.assertEqual(secret_refs, {
            "PULSE_TOKEN": {"name": "pulse-api-token", "key": "token"},
            "PULSE_USERNAME": {"name": "pulse-api-token", "key": "username", "optional": True},
            "PULSE_PASSWORD": {"name": "pulse-api-token", "key": "password", "optional": True},
        })
        preflight = workflow.index("Pre-deploy Pulse auth validation")
        r0 = workflow.index("R0 — isolated dependencies", preflight)
        self.assertLess(preflight, r0)
        runner_segment = workflow[preflight:r0]
        self.assertNotRegex(runner_segment, r"kubectl\s+[^\n]*get\s+secret")
        self.assertNotIn("secrets.PULSE", runner_segment)



class MigrationAndEvidenceTests(unittest.TestCase):
    def test_destructive_migration_fails_before_cluster_action(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
            path = repo / "claws" / "migrations"
            path.mkdir(parents=True)
            mig = path / "0001_initial.py"
            mig.write_text("from django.db import migrations\n")
            subprocess.run(["git", "add", str(mig)], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            mig.write_text("from django.db import migrations\nx=migrations.DeleteModel(name='X')\n")
            subprocess.run(["git", "add", str(mig)], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "danger"], cwd=repo, check=True)
            result = run(
                [SCRIPTS / "migration-compat-gate.sh", base, "new", "old"],
                cwd=repo,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("destructive", result.stdout + result.stderr)

    def test_failure_recorder_dedupes_existing_task_and_sends_alert(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            log = td / "calls"
            (td / "kubectl").write_text(
                "#!/bin/sh\ncase \"$*\" in *.data.token*) printf dG9rZW4= ;; esac\n"
            )
            (td / "curl").write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/bash
                    echo "$*" >> {log}
                    args="$*"
                    case "$args" in
                      *'/api/projects/?key=HIVE'*) body='{{"results":[{{"id":9,"key":"HIVE"}}]}}'; code=200 ;;
                      *hive-rollback-create-permission*'/api/items/'*) body='{{"id":"9a3e6a53-a33e-4f44-b78a-47292d599683","item_key":"HIVE-123","priority":{{"id":1}}}}'; code=201 ;;
                      *'/api/items/HIVE-123/'*) body=''; code=204 ;;
                      *'/api/items/?search='*) body='{{"results":[{{"id":"b48bced8-41d0-4c41-9cf7-9b68e8ff12ea","item_key":"HIVE-77","project_key":"HIVE","workflow_name":"autonomous-bug","assignment_group":"engineering"}}]}}' ;;
                      *'/api/comments/'*) body='{{"item":"b48bced8-41d0-4c41-9cf7-9b68e8ff12ea","content":"recorded"}}' ;;
                      *) body='{{}}' ;;
                    esac
                    printf '%s' "$body"
                    [[ "$args" == *'%{{http_code}}'* ]] && printf '\n%s' "${{code:-200}}"
                    exit 0
                    """
                )
            )
            (td / "kubectl").chmod(0o755)
            (td / "curl").chmod(0o755)
            result = run(
                [SCRIPTS / "record-rollout-failure.sh", "canary", SHA, OLD_SHA, "probe absent"],
                env={"PATH": f"{td}:{os.environ['PATH']}", "PULSE_TOKEN": "test-only"},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = log.read_text()
            self.assertIn("/api/v2/alerts", calls)
            self.assertIn("/api/comments/", calls)
            self.assertEqual(calls.count("/api/comments/"), 1)
            self.assertIn('"item": "HIVE-77"', calls)

    def test_failure_recorder_creates_in_hive_autonomous_bug_and_engineering(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            log = td / "calls"
            (td / "kubectl").write_text("#!/bin/sh\ncase \"$*\" in *.data.token*) printf dG9rZW4= ;; esac\n")
            (td / "curl").write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/bash
                    echo "$*" >> {log}
                    args="$*"
                    case "$args" in
                      *'/api/items/?search='*) body='{{"results":[]}}' ;;
                      *'/api/projects/?key=HIVE'*) body='{{"results":[{{"id":9,"key":"HIVE"}}]}}' ;;
                      *hive-rollback-create-permission*'/api/items/'*) body='{{"id":"9a3e6a53-a33e-4f44-b78a-47292d599683","item_key":"HIVE-123","priority":{{"id":1}}}}'; code=201 ;;
                      *'/api/items/HIVE-123/'*) body=''; code=204 ;;
                      *'-X POST'*'/api/items/'*) body='{{"id":88,"project_key":"HIVE","workflow_name":"autonomous-bug","assignment_group":"engineering"}}' ;;
                      *) body='{{}}' ;;
                    esac
                    printf '%s' "$body"
                    [[ "$args" == *'%{{http_code}}'* ]] && printf '\n%s' "${{code:-201}}"
                    exit 0
                    """
                )
            )
            (td / "kubectl").chmod(0o755)
            (td / "curl").chmod(0o755)
            result = run(
                [SCRIPTS / "record-rollout-failure.sh", "fleet", SHA, OLD_SHA, "post-roll failure"],
                env={"PATH": f"{td}:{os.environ['PATH']}", "PULSE_TOKEN": "test-only"},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = log.read_text()
            self.assertIn('"project": 9', calls)
            self.assertIn('"workflow_name": "autonomous-bug"', calls)
            self.assertIn('"assignment_group": "engineering"', calls)

    def test_failure_recorder_fails_closed_without_hive_create_permission(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            log = td / "calls"
            (td / "kubectl").write_text("#!/bin/sh\ncase \"$*\" in *.data.token*) printf dG9rZW4= ;; esac\n")
            (td / "curl").write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/bash
                    echo "$*" >> {log}
                    args="$*"
                    case "$args" in
                      *'/api/projects/?key=HIVE'*) body='{{"results":[{{"id":9,"key":"HIVE"}}]}}'; code=200 ;;
                      *hive-rollback-create-permission*'/api/items/'*) body='{{"project":"You do not have permission to create items in this project"}}'; code=400 ;;
                      *) body='{{}}'; code=200 ;;
                    esac
                    printf '%s' "$body"
                    [[ "$args" == *'%{{http_code}}'* ]] && printf '\n%s' "${{code}}"
                    exit 0
                    """
                )
            )
            (td / "kubectl").chmod(0o755)
            (td / "curl").chmod(0o755)
            result = run(
                [SCRIPTS / "record-rollout-failure.sh", "canary", SHA, OLD_SHA, "probe absent"],
                env={"PATH": f"{td}:{os.environ['PATH']}", "PULSE_TOKEN": "test-only"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lacks HIVE item-create permission", result.stdout + result.stderr)
            calls = log.read_text()
            self.assertNotIn("/api/v2/alerts", calls)
            self.assertNotIn("/api/items/?search=", calls)

    def test_failure_recorder_mints_fresh_token_and_fails_closed_without_leaking(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            calls = td / "calls"
            (td / "kubectl").write_text(textwrap.dedent("""\
                #!/bin/sh
                case "$*" in
                  *'.data.username'*) printf b3JpZ2luLWNp ;;
                  *'.data.password'*) printf Ym9vdHN0cmFwLXNlY3JldA== ;;
                  *) exit 2 ;;
                esac
            """))
            (td / "curl").write_text(textwrap.dedent(f"""\
                #!/bin/bash
                echo "$*" >> {calls}
                args="$*"
                case "$args" in
                  *'/api/auth/login/'*)
                    if [[ "${{MINT_MODE:-ok}}" == bad ]]; then
                      printf '{{"tokens":{{}}}}'
                    else
                      printf '{{"tokens":{{"access":"fresh-access-token"}}}}'
                    fi
                    exit 0
                    ;;
                  *'/api/projects/?key=HIVE'*) body='{{"results":[{{"id":9,"key":"HIVE"}}]}}'; code=200 ;;
                  *hive-rollback-create-permission*'/api/items/'*) body='{{"id":"9a3e6a53-a33e-4f44-b78a-47292d599683","item_key":"HIVE-123","priority":{{"id":1}}}}'; code=201 ;;
                  *'/api/items/HIVE-123/'*) body=''; code=204 ;;
                  *'/api/items/?search='*) body='{{"results":[{{"id":"b48bced8-41d0-4c41-9cf7-9b68e8ff12ea","item_key":"HIVE-77","project_key":"HIVE","workflow_name":"autonomous-bug","assignment_group":"engineering"}}]}}'; code=200 ;;
                  *'/api/comments/'*) body='{{"item":"b48bced8-41d0-4c41-9cf7-9b68e8ff12ea","content":"recorded"}}'; code=201 ;;
                  *) body='{{}}'; code=200 ;;
                esac
                printf '%s' "$body"
                [[ "$args" == *'%{{http_code}}'* ]] && printf '\n%s' "$code"
                exit 0
            """))
            (td / "kubectl").chmod(0o755)
            (td / "curl").chmod(0o755)
            env = {"PATH": f"{td}:{os.environ['PATH']}", "PULSE_TOKEN": "expired-snapshot"}
            ok = run(
                [SCRIPTS / "record-rollout-failure.sh", "canary", SHA, OLD_SHA, "mint regression"],
                env=env,
            )
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertEqual(calls.read_text().count("/api/auth/login/"), 1)
            for secret in ("origin-ci", "bootstrap-secret", "fresh-access-token"):
                self.assertNotIn(secret, ok.stdout + ok.stderr)

            calls.write_text("")
            bad = run(
                [SCRIPTS / "record-rollout-failure.sh", "canary", SHA, OLD_SHA, "mint regression"],
                env={**env, "MINT_MODE": "bad"},
            )
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("did not contain a Pulse access token", bad.stdout + bad.stderr)
            for secret in ("origin-ci", "bootstrap-secret", "fresh-access-token"):
                self.assertNotIn(secret, bad.stdout + bad.stderr)
            self.assertNotIn("/api/projects/", calls.read_text())

    def test_release_safety_handlers_cover_all_post_mutation_failures(self):
        r1 = (SCRIPTS / "ring-release-canary.sh").read_text()
        r2 = (SCRIPTS / "ring-release-fleet.sh").read_text()
        self.assertLess(r1.index("trap 'r1_failure"), r1.index("kubectl apply -f /tmp/hive-canary.yaml"))
        self.assertIn("trap 'r1_failure 143", r1)
        self.assertIn("record-rollout-failure.sh canary", r1)
        self.assertLess(r1.index("capture-ring-evidence.sh canary"), r1.index("delete deploy hive-canary"))
        self.assertIn("trap '' TERM INT", r1)
        self.assertNotIn("if ! bash .forgejo/scripts/ring-gate.sh", r1)

        armed = r2.index("trap 'r2_failure")
        self.assertLess(armed, r2.index("kubectl -n shizuha-hive patch deploy/hive", armed))
        self.assertIn("trap 'r2_failure 143", r2)
        self.assertIn("record-rollout-failure.sh fleet", r2)
        self.assertLess(r2.index("capture-ring-evidence.sh fleet"), r2.index("set image deploy/hive"))
        self.assertIn("trap '' TERM INT", r2)
        self.assertNotIn("if ! bash .forgejo/scripts/ring-gate.sh", r2)

    def test_pulse_permission_probe_is_versioned_recoverable_and_fail_fast(self):
        workflow = (ROOT / ".forgejo" / "workflows" / "deploy.yml").read_text()
        preflight = workflow.split("JOB=\"hive-pulse-auth-preflight-", 1)[1].split(
            "Pulse auth preflight failed", 1
        )[0]
        self.assertIn(
            "image: ${IMAGE}\n"
            "                  command:\n"
            "                  - /app/.venv/bin/python\n"
            "                  - /app/.forgejo/scripts/pulse-auth-preflight.py",
            preflight,
        )
        self.assertIn("value: ${GITHUB_SHA}", preflight)
        self.assertIn('while kill -0 "$WP"', workflow)
        helper = (SCRIPTS / "pulse-auth-preflight.py").read_text()
        self.assertIn("hive-rollback-create-permission-{release_sha}", helper)
        self.assertIn("find_probe_refs(", helper)
        self.assertIn("expected=(204, 404)", helper)

    def test_term_trap_runs_cleanup_and_preserves_signal_failure(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "cleanup"
            script = textwrap.dedent(
                f"""\
                set -Eeuo pipefail
                cleanup() {{ trap - ERR TERM INT; echo restored > {marker}; exit "$1"; }}
                trap 'cleanup 143' TERM
                touch {Path(td) / "mutated"}
                kill -TERM $$
                """
            )
            result = run(["bash", "-c", script])
            self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
            self.assertEqual(marker.read_text().strip(), "restored")





    def test_post_inv_f_cleanup_failure_retains_healthy_release_and_alerts_cleanup_lane(self):
        cleanup = (SCRIPTS / "ring-cleanup-canary.sh").read_text()
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            fake_bin = td / "bin"
            scripts = td / ".forgejo" / "scripts"
            fake_bin.mkdir()
            scripts.mkdir(parents=True)
            image, labels, anchor = td / "image", td / "labels", td / "anchor"
            calls = td / "calls"
            image.write_text(SHA)
            labels.write_text(SHA)
            anchor.write_text(SHA)
            (fake_bin / "kubectl").write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/bash
                    echo "kubectl $*" >> {calls}
                    case "$*" in
                      *"set image deploy/hive"*) printf '%s' "{OLD_SHA}" > {image} ;;
                      *"label deploy/hive"*|*"patch deploy/hive"*) printf '%s' "{OLD_SHA}" > {labels} ;;
                      *"annotate deploy/hive"*) printf '%s' "{OLD_SHA}" > {anchor} ;;
                    esac
                    exit 0
                    """
                )
            )
            (fake_bin / "kubectl").chmod(0o755)
            (scripts / "ring-watchdog.sh").write_text(
                f"#!/bin/sh\necho \"watchdog $*\" >> {calls}\nexit 0\n"
            )
            (scripts / "retire-canary-probe.sh").write_text(
                f"#!/bin/sh\necho retire >> {calls}\nexit 1\n"
            )
            (scripts / "record-release-cleanup-event.sh").write_text(
                f"#!/bin/sh\necho \"cleanup-event $*\" >> {calls}\nexit 0\n"
            )
            for helper in scripts.iterdir():
                helper.chmod(0o755)
            injected = td / "injected.sh"
            injected.write_text(
                "set -Eeuo pipefail\n"
                f"export RELEASE_SHA={SHA}\n"
                + cleanup.replace(".forgejo/scripts/kubectl", str(fake_bin / "kubectl"))
            )
            result = run(
                ["bash", injected],
                cwd=td,
                env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(image.read_text(), SHA)
            self.assertEqual(labels.read_text(), SHA)
            self.assertEqual(anchor.read_text(), SHA)
            call_log = calls.read_text()
            self.assertIn("cleanup-event firing fleet", call_log)
            self.assertIn("command=.forgejo/scripts/retire-canary-probe.sh", call_log)
            self.assertNotIn("set image deploy/hive", call_log)
            self.assertNotIn("record-rollout-failure", call_log)

    def test_post_inv_f_term_retains_healthy_release_and_alerts_cleanup_lane(self):
        cleanup = (SCRIPTS / "ring-cleanup-canary.sh").read_text()
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            scripts = td / ".forgejo" / "scripts"
            scripts.mkdir(parents=True)
            calls = td / "calls"
            (scripts / "ring-watchdog.sh").write_text("#!/bin/sh\nexit 0\n")
            (scripts / "retire-canary-probe.sh").write_text(
                f'#!/bin/bash\necho retire >> {calls}\nkill -TERM "$PPID"\nsleep 1\n'
            )
            (scripts / "record-release-cleanup-event.sh").write_text(
                f'#!/bin/sh\necho "cleanup-event $*" >> {calls}\nexit 0\n'
            )
            for helper in scripts.iterdir():
                helper.chmod(0o755)
            injected = td / "injected.sh"
            injected.write_text(
                "set -Eeuo pipefail\n"
                f"export RELEASE_SHA={SHA}\n"
                + cleanup
            )
            result = run(["bash", injected], cwd=td)
            self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
            call_log = calls.read_text()
            self.assertIn("cleanup-event firing fleet", call_log)
            self.assertIn("signal=TERM", call_log)
            self.assertNotIn("record-rollout-failure", call_log)

    def test_cleanup_event_uses_devops_alert_without_pulse_rollback_task(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            calls = td / "calls"
            payload = td / "payload"
            curl = td / "curl"
            curl.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/bash
                    echo "$*" >> {calls}
                    while (($#)); do
                      if [[ "$1" == -d ]]; then shift; printf '%s' "$1" > {payload}; fi
                      shift
                    done
                    """
                )
            )
            curl.chmod(0o755)
            result = run(
                [SCRIPTS / "record-release-cleanup-event.sh", "firing", "fleet", SHA, "step=retire-canary-probe"],
                env={"PATH": f"{td}:{os.environ['PATH']}"},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            alert = json.loads(payload.read_text())[0]
            self.assertEqual(alert["labels"]["alertname"], "HiveReleaseCleanupFailed")
            self.assertEqual(alert["labels"]["severity"], "warning")
            self.assertEqual(alert["labels"]["owner_group"], "devops")
            self.assertNotIn("release_sha", alert["labels"])
            self.assertEqual(alert["annotations"]["release_sha"], SHA)
            self.assertNotIn("bad_sha", alert["labels"])
            self.assertNotIn("release-regression", payload.read_text())
            self.assertNotIn("/api/items/", calls.read_text())

            resolved = run(
                [SCRIPTS / "record-release-cleanup-event.sh", "resolved", "fleet", SHA, "cleanup-converged"],
                env={"PATH": f"{td}:{os.environ['PATH']}"},
            )
            self.assertEqual(resolved.returncode, 0, resolved.stdout + resolved.stderr)
            resolved_alert = json.loads(payload.read_text())[0]
            self.assertIn("endsAt", resolved_alert)
            self.assertEqual(resolved_alert["labels"], alert["labels"])

    def test_exact_sha_anchor_delta_and_functional_smoke_are_fail_closed(self):
        workflow = (ROOT / ".forgejo" / "workflows" / "deploy.yml").read_text()
        self.assertIn('verify-exact-checkout.sh "$GITHUB_SHA"', workflow)
        self.assertNotIn("building default HEAD", workflow)
        self.assertIn('verify-release-delta.sh "$LAST_SHA" "$SHA" "$DELTA_MODE"', workflow)
        self.assertIn('migration-compat-gate.sh "$LAST_SHA" "$IMAGE" "$OLD_IMAGE"', workflow)
        # Break-glass left last-successful-sha pointing at a single-arch tag while
        # live ran multi-arch; reconstruct-from-sha blocked R0 forever.
        self.assertIn('LIVE_IMAGE="$(kubectl -n shizuha-hive get deploy hive', workflow)
        self.assertIn('OLD_IMAGE="$LIVE_IMAGE"', workflow)
        self.assertNotIn('migration-compat-gate.sh "$BASE_SHA"', workflow)
        self.assertIn("hive-channel-token-smoke.py", workflow)

    def test_full_anchor_delta_catches_migration_in_intermediate_commit(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
            (repo / "README").write_text("anchor\n")
            subprocess.run(["git", "add", "README"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "anchor"], cwd=repo, check=True)
            anchor = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            migrations = repo / "claws" / "migrations"
            migrations.mkdir(parents=True)
            (migrations / "0002_add.py").write_text("from django.db import migrations\n")
            subprocess.run(["git", "add", "claws/migrations/0002_add.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "intermediate migration"], cwd=repo, check=True)
            (repo / "README").write_text("release\n")
            subprocess.run(["git", "add", "README"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "release without direct migration"], cwd=repo, check=True)
            release = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            result = run(
                [SCRIPTS / "verify-release-delta.sh", anchor, release, "reject-migrations"],
                cwd=repo,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("anchor-to-release delta changes migrations", result.stdout + result.stderr)
            normal = run(
                [SCRIPTS / "migration-compat-gate.sh", anchor, "new", "old"],
                cwd=repo,
                env={"MIGRATION_COMPAT_PLAN_ONLY": "1"},
            )
            self.assertEqual(normal.returncode, 3, normal.stdout + normal.stderr)
            self.assertIn("additive compatibility exercise required", normal.stdout)

    def test_exact_checkout_fails_when_requested_sha_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
            (repo / "README").write_text("head\n")
            subprocess.run(["git", "add", "README"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "head"], cwd=repo, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=repo, check=True)
            missing = "f" * 40
            result = run([SCRIPTS / "verify-exact-checkout.sh", missing], cwd=repo)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("PASS", result.stdout + result.stderr)

    def test_workflow_gates_hold_anchor_evidence_and_manual_sha(self):
        workflow = (ROOT / ".forgejo" / "workflows" / "deploy.yml").read_text()
        release = "\n".join(
            (SCRIPTS / name).read_text()
            for name in ("ring-release-canary.sh", "ring-release-fleet.sh")
        )
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("github.event.inputs.sha == github.sha", workflow)
        self.assertIn("github.event.inputs.hold != 'true'", workflow)
        self.assertIn("needs.deploy-canary.result == 'success'", workflow)
        self.assertIn("record-rollout-failure.sh canary", release)
        self.assertIn("record-rollout-failure.sh fleet", release)
        self.assertIn('set image deploy/hive hive="$ROLLBACK_IMAGE"', release)
        self.assertIn('set image deploy/hive-api hive-api="$ROLLBACK_IMAGE"', release)
        self.assertIn('set image deploy/hive-codex-refresh codex-refresh="$ROLLBACK_IMAGE"', release)
        self.assertIn('hive-api=localhost:30500/shizuha-hive:${TAG}', release)
        self.assertIn('"minReadySeconds": 180', release)
        self.assertIn('"progressDeadlineSeconds": 1800', release)
        self.assertGreaterEqual(release.count("--timeout=1800s"), 2)
        self.assertIn("record-successful-release.py", release)
        self.assertIn("rollback_rc=", release)
        self.assertIn('STORED="$(.forgejo/scripts/kubectl', release)
        self.assertIn('STORED_NAMESPACED="$(.forgejo/scripts/kubectl', release)
        anchor_section = release.split("INV-F: persistence", 1)[1]
        self.assertNotIn("|| true", anchor_section.split("canary deployment", 1)[0])
        recorder = (SCRIPTS / "record-rollout-failure.sh").read_text()
        self.assertIn("api/auth/login/", recorder)
        self.assertIn(".data.token", recorder)
        self.assertIn(".data.username", recorder)
        self.assertIn(".data.password", recorder)
        self.assertLess(recorder.index(".data.username"), recorder.index(".data.token"))
        watchdog = (SCRIPTS / "ring-watchdog.sh").read_text()
        self.assertNotIn(".data.token", watchdog)
        self.assertIn("legacy runner watchdog is retired", watchdog)
        self.assertIn('"owner_group":"engineering"', recorder)
        self.assertIn('"runbook_url":', recorder)


class RingFenceContractTests(InstallationProfileCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPTS))
        import ring_executor
        import ring_fence

        cls.executor_module = ring_executor
        cls.fence_module = ring_fence
        recovery_loader = importlib.machinery.SourceFileLoader(
            "ring_recovery_test", str(SCRIPTS / "ring-recovery.py")
        )
        recovery_spec = importlib.util.spec_from_loader(
            recovery_loader.name, recovery_loader,
        )
        assert recovery_spec is not None
        recovery = importlib.util.module_from_spec(recovery_spec)
        sys.modules[recovery_loader.name] = recovery
        recovery_loader.exec_module(recovery)
        cls.recovery_module = recovery
        loader = importlib.machinery.SourceFileLoader(
            "ring_kubectl_facade", str(SCRIPTS / "kubectl")
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        facade = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = facade
        loader.exec_module(facade)
        cls.facade_module = facade

    def test_fleet_claw_idle_surface_is_fenced_and_recoverable(self):
        # Run-2459 regression (8d8e847f, 2026-08-31): the HIVE-2011 claw-idle
        # scrape surface (ServiceMonitor + PrometheusRule, both ns monitoring)
        # is installed by ring-release-fleet.sh but was missing from
        # protected_targets, so the fenced kubectl rejected the ServiceMonitor
        # mutation ("protected target is not allowlisted") and the fleet
        # release rolled back mid-flight. Both objects must be fenced.
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m)

        self.assertTrue(e.target_is_protected(
            holder, "monitoring", "servicemonitor", "hive-claw-idle",
        ))
        self.assertTrue(e.target_is_protected(
            holder, "monitoring", "prometheusrule", "hive-claw-idle",
        ))

        release = (ROOT / ".forgejo/scripts/ring-release-fleet.sh").read_text()
        self.assertIn('name: hive-claw-idle', release)

        # The recovery must be able to re-claim the FULL fleet surface after a
        # mid-flight rollback: run 2459's recovery pod failed fail-closed
        # ("cannot CAS-claim protected prometheusrule/hive-golden-path")
        # because the hive-ring-recover Role resourceNames allowlists were
        # never extended with the HIVE-580 golden-path targets, and the
        # monitoring-recover Role lacked the claw-idle surface entirely.
        rbac = list(yaml.safe_load_all(
            (ROOT / "k8s" / "hive-ring-executor-rbac.yaml").read_text()
        ))

        def role(name, namespace):
            return next(
                item for item in rbac
                if item.get("kind") == "Role"
                and item.get("metadata", {}).get("name") == name
                and item.get("metadata", {}).get("namespace") == namespace
            )

        hive_recover = role("hive-ring-recover", "shizuha-hive")
        cron_rule = next(
            rule for rule in hive_recover["rules"]
            if rule.get("resources") == ["cronjobs"]
        )
        self.assertIn("hive-golden-path-validation", cron_rule["resourceNames"])
        prom_rule = next(
            rule for rule in hive_recover["rules"]
            if rule.get("resources") == ["prometheusrules"]
        )
        self.assertIn("hive-golden-path", prom_rule["resourceNames"])

        monitoring_recover = role("hive-ring-monitoring-recover", "monitoring")
        sm_rule = next(
            rule for rule in monitoring_recover["rules"]
            if "servicemonitors" in (rule.get("resources") or [])
        )
        self.assertIn("hive-claw-idle", sm_rule["resourceNames"])
        mon_prom_rule = next(
            rule for rule in monitoring_recover["rules"]
            if rule.get("resources") == ["prometheusrules"]
        )
        self.assertIn("hive-claw-idle", mon_prom_rule["resourceNames"])

        # The executor's monitoring Role must grant servicemonitors at all —
        # without it the fenced apply would fail on RBAC right after the
        # allowlist passes.
        monitoring_execute = role("hive-ring-monitoring", "monitoring")
        exec_sm = next(
            rule for rule in monitoring_execute["rules"]
            if "servicemonitors" in (rule.get("resources") or [])
        )
        self.assertIn("create", exec_sm["verbs"])
        self.assertIn("patch", exec_sm["verbs"])

    @staticmethod
    def holder(module, *, run="100", sha=SHA, ring="fleet", job="deploy-fleet"):
        return module.Holder(
            repo="shizuha-labs/hive", workflow="deploy.yml", run_id=run,
            run_number="100", job=job, ring=ring, release_sha=sha,
            restored_sha=OLD_SHA,
        )

    class FakeApi:
        def __init__(self, lease):
            self.lease = json.loads(json.dumps(lease))
            self.patches = []

        def get_lease(self, _name):
            return json.loads(json.dumps(self.lease))

        def json_patch(self, _namespace, _resource, _name, operations):
            self.patches.append(json.loads(json.dumps(operations)))
            for operation in operations:
                parts = [
                    part.replace("~1", "/").replace("~0", "~")
                    for part in operation["path"].split("/")[1:]
                ]
                cursor = self.lease
                for part in parts[:-1]:
                    cursor = cursor[part]
                if operation["op"] == "test":
                    if cursor[parts[-1]] != operation["value"]:
                        raise RuntimeError("test failed")
                else:
                    cursor[parts[-1]] = operation["value"]
            self.lease["metadata"]["resourceVersion"] = str(
                int(self.lease["metadata"]["resourceVersion"]) + 1
            )
            return json.loads(json.dumps(self.lease))

    def lease(self, holder, *, token=7, phase="active", rv="12", renew_time="2999-01-01T00:00:00Z"):
        return {
            "metadata": {
                "resourceVersion": rv,
                "annotations": {
                    "shizuha.io/fence-token": str(token),
                    "shizuha.io/phase": phase,
                    "shizuha.io/ring-generation": holder.generation,
                },
            },
            "spec": {
                "holderIdentity": holder.serialized(),
                "leaseDurationSeconds": 90,
                # Default is deliberately far future: LeaseObserver expiry
                # must ignore writer wall clock while the owner is alive.
                "renewTime": renew_time,
            },
        }

    def test_same_and_different_sha_successor_fence_resumed_predecessor(self):
        m = self.fence_module
        for successor_sha in (SHA, "c" * 40):
            predecessor = self.holder(m, run="100")
            successor = self.holder(m, run="101", sha=successor_sha)
            stale = m.Fence(predecessor, self.FakeApi(self.lease(successor, token=9)))
            stale.token = 7
            with self.assertRaisesRegex(m.Conflict, "holder/token changed"):
                stale.validate()

    def test_delayed_request_cannot_acquire_after_ring_token_advanced(self):
        m = self.fence_module
        holder = self.holder(m, run="100")
        successor = self.holder(m, run="101")
        api = self.FakeApi(self.lease(successor, token=9, phase="completed"))
        with self.assertRaisesRegex(m.Conflict, "advanced after request submission"):
            m.Fence(holder, api).acquire(expected_prior_token=7)
        self.assertEqual(api.patches, [])

    def test_live_recovering_lease_cannot_be_acquired(self):
        m = self.fence_module
        predecessor = self.holder(m, run="100-recovery", job="recover")
        successor = self.holder(m, run="101")
        api = self.FakeApi(self.lease(predecessor, token=9, phase="recovering"))
        with self.assertRaisesRegex(m.Conflict, "already owned"):
            m.Fence(successor, api).acquire(expected_prior_token=9)
        self.assertEqual(api.patches, [])

    def test_expired_recovering_lease_can_be_acquired_by_successor(self):
        # 2026-09-02: recover GET 403'd cronjob/hive-forgejo-token-reconciler,
        # left phase=recovering, TTL GC'd the Job, and execute Conflict'd
        # forever. A kube-expired recovering Lease is an abandoned owner.
        m = self.fence_module
        predecessor = self.holder(m, run="100-recovery", job="recover")
        successor = self.holder(m, run="101")
        api = self.FakeApi(self.lease(
            predecessor, token=9, phase="recovering",
            renew_time="2020-01-01T00:00:00Z",
        ))
        state = m.Fence(successor, api).acquire(expected_prior_token=9)
        self.assertEqual(state.token, 10)
        self.assertEqual(state.phase, "acquiring")
        self.assertEqual(
            json.loads(api.lease["spec"]["holderIdentity"])["run_id"], "101",
        )

    def test_two_recovery_contenders_have_one_resource_version_winner(self):
        m = self.fence_module
        predecessor = self.holder(m)
        recovery = self.holder(m, run="100-recovery", job="recover")
        api = self.FakeApi(self.lease(predecessor))
        stale_state = m.FenceState.from_lease(api.lease)
        winner = m.Fence(predecessor, api).takeover(stale_state, recovery)
        self.assertEqual(winner.token, 8)
        with self.assertRaises(RuntimeError):
            m.Fence(predecessor, api).takeover(stale_state, recovery)
        tests = {item["path"] for item in api.patches[0] if item["op"] == "test"}
        self.assertEqual(tests, {
            "/metadata/resourceVersion", "/spec/holderIdentity",
            "/metadata/annotations/shizuha.io~1fence-token",
            "/metadata/annotations/shizuha.io~1phase",
        })

    def test_monotonic_expiry_relist_and_read_failure_fail_closed(self):
        m = self.fence_module
        holder = self.holder(m)
        state = m.FenceState(holder.serialized(), 2, "active", "5", 90)
        now = [1000.0]
        observer = m.LeaseObserver(lambda: state, lambda: now[0])
        self.assertFalse(observer.observe()[1])
        now[0] += 90
        self.assertTrue(observer.observe()[1])
        observer.relist_after_gap()  # watch 410/history gap
        now[0] += 500
        self.assertFalse(observer.observe()[1])
        failed = m.LeaseObserver(lambda: (_ for _ in ()).throw(m.FenceError("ambiguous 404")))
        with self.assertRaisesRegex(m.FenceError, "ambiguous"):
            failed.observe()
        self.assertIsNone(failed.observed_at)

    def test_completed_is_not_recovered_and_all_boundaries_test_target_cas(self):
        m = self.fence_module
        holder = self.holder(m)
        completed = self.FakeApi(self.lease(holder, phase="completed"))
        with self.assertRaisesRegex(m.Conflict, "cannot be recovered"):
            m.Fence(holder, completed).takeover(
                m.FenceState.from_lease(completed.lease),
                self.holder(m, run="100-recovery", job="recover"),
            )
        live = self.FakeApi(self.lease(holder))
        fence = m.Fence(holder, live)
        fence.token = 7
        for mutation in (
            "Deployment", "Service", "Probe", "Job", "ConfigMap", "Secret",
            "NetworkPolicy", "phase", "INV-F", "cleanup", "disarm",
        ):
            target = {
                "kind": mutation,
                "metadata": {
                    "resourceVersion": "44",
                    "annotations": {
                        "shizuha.io/ring-generation": holder.generation,
                        "shizuha.io/fence-token": "7",
                    },
                },
            }
            self.assertEqual(
                [item["op"] for item in fence.target_tests(target)],
                ["test", "test", "test"],
            )
            target["metadata"]["annotations"]["shizuha.io/fence-token"] = "6"
            with self.assertRaisesRegex(m.Conflict, "stale"):
                fence.target_tests(target)

    def test_typed_request_rejects_free_form_attack_surface(self):
        m, e = self.fence_module, self.executor_module
        holder = self.holder(m)
        request = {
            "apiVersion": "shizuha.io/hive-ring-request-v1",
            "operation": "release", "holder": dataclasses.asdict(holder),
            "image": f"localhost:30500/shizuha-hive:hive-{SHA[:7]}",
            "event_name": "push", "hold": False, "hive725_phase": "phase1",
            "source_tree": "d" * 40, "prior_fence_token": 6,
        }
        self.assertEqual(e.validate_request(request)[0], holder)
        for field in ("command", "shell", "patch", "gvk", "namespace", "name"):
            with self.assertRaisesRegex(m.FenceError, "schema"):
                e.validate_request({**request, field: "forged"})

    def test_fleet_liveness_cronjob_is_a_fenced_release_target(self):
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m)

        self.assertTrue(e.target_is_protected(
            holder,
            "shizuha-hive",
            "cronjob",
            "fleet-agent-liveness-reconciler",
        ))

        release = (ROOT / ".forgejo/scripts/ring-release-fleet.sh").read_text()
        self.assertIn('"suspend": False,', release)
        self.assertIn(
            'LIVENESS_CRON_SUSPENDED="$(.forgejo/scripts/kubectl -n shizuha-hive get cronjob fleet-agent-liveness-reconciler',
            release,
        )
        self.assertIn(
            '[ "$LIVENESS_CRON_SUSPENDED" = "false" ]',
            release,
        )

    def test_fleet_compute_billing_cronjob_is_a_fenced_release_target(self):
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m)

        self.assertTrue(e.target_is_protected(
            holder,
            "shizuha-hive",
            "cronjob",
            "hive-compute-billing",
        ))

        release = (ROOT / ".forgejo/scripts/ring-release-fleet.sh").read_text()
        self.assertIn('name": "hive-compute-billing"', release)
        self.assertIn("bill_fleet_compute", release)

        recover = list(yaml.safe_load_all(
            (ROOT / "k8s" / "hive-ring-executor-rbac.yaml").read_text()
        ))
        recover_role = next(
            item for item in recover
            if item.get("kind") == "Role"
            and item.get("metadata", {}).get("name") == "hive-ring-recover"
            and item.get("metadata", {}).get("namespace") == "shizuha-hive"
        )
        cron_rule = next(
            rule for rule in recover_role["rules"]
            if rule.get("resources") == ["cronjobs"]
        )
        self.assertIn("hive-compute-billing", cron_rule["resourceNames"])

    def test_fleet_forgejo_token_reconciler_is_a_fenced_release_target(self):
        # 2026-09-02: ring-release-fleet.sh installs cronjob/hive-forgejo-token-reconciler
        # (ORIG-15) but it was missing from protected_targets. Fenced kubectl
        # rejected the apply and fleet execute rolled hive-2b4c7e5 back to
        # hive-a85cceb (HIVE-580 class).
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m)

        self.assertTrue(e.target_is_protected(
            holder, "shizuha-hive", "cronjob", "hive-forgejo-token-reconciler",
        ))

        release = (ROOT / ".forgejo/scripts/ring-release-fleet.sh").read_text()
        self.assertIn("hive-forgejo-token-reconciler", release)
        self.assertIn("reconcile_agent_forgejo_tokens", release)

        recover = list(yaml.safe_load_all(
            (ROOT / "k8s" / "hive-ring-executor-rbac.yaml").read_text()
        ))
        recover_role = next(
            item for item in recover
            if item.get("kind") == "Role"
            and item.get("metadata", {}).get("name") == "hive-ring-recover"
            and item.get("metadata", {}).get("namespace") == "shizuha-hive"
        )
        cron_rule = next(
            rule for rule in recover_role["rules"]
            if rule.get("resources") == ["cronjobs"]
        )
        self.assertIn("hive-forgejo-token-reconciler", cron_rule["resourceNames"])

        # Pre-submit recover RBAC (applied before ring-executor submit) must
        # also grant GET, or recover dies on the July hive-ring-recover Role
        # and the fence stays recovering.
        recover_pre = list(yaml.safe_load_all(
            (ROOT / "k8s" / "hive-ring-recover-rbac.yaml").read_text()
        ))
        forgejo_role = next(
            item for item in recover_pre
            if item
            and item.get("kind") == "Role"
            and item.get("metadata", {}).get("name") == "hive-ring-recover-forgejo-token"
        )
        self.assertEqual(
            forgejo_role["rules"][0]["resourceNames"],
            ["hive-forgejo-token-reconciler"],
        )

    def test_fleet_golden_path_validation_targets_are_fenced_release_targets(self):
        # HIVE-580 regression (HiveReleaseRollback 2026-08-30): the golden-path
        # PrometheusRule + CronJob installs were added to ring-release-fleet.sh
        # but NOT to protected_targets, so the fenced kubectl facade rejected
        # `apply --dry-run=server -f k8s/hive-golden-path-alerts.yaml`
        # (FenceError "protected target is not allowlisted") and the fleet
        # release rolled back at phase2 stage=1. Both targets must be fenced.
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m)

        self.assertTrue(e.target_is_protected(
            holder, "shizuha-hive", "prometheusrule", "hive-golden-path",
        ))
        self.assertTrue(e.target_is_protected(
            holder, "shizuha-hive", "cronjob", "hive-golden-path-validation",
        ))
        # HIVE-2266 (recurrence of PLAT-7248): the fleet release ALSO creates a
        # golden-path validation Job via `kubectl create job
        # --from=cronjob/hive-golden-path-validation` named
        # hive-golden-path-validation-deploy-<sha7>-<unix_ts>. PLAT-7248 fenced
        # the CronJob + PrometheusRule but missed this Job, so the create was
        # rejected and the release rolled back at phase2 stage=1 again.
        self.assertTrue(e.target_is_protected(
            holder, "shizuha-hive", "job",
            f"hive-golden-path-validation-deploy-{holder.release_sha[:7]}-1788135404",
        ))
        # PLAT-7825: evidence Job name is SHA7 + first 12 hex of ring generation,
        # not two decimal groups. Fleet create was rejected as not allowlisted.
        self.assertTrue(e.target_is_protected(
            holder, "origin", "job",
            f"hive-ring-evidence-fleet-{holder.release_sha[:7]}-3ac86f5844fb",
        ))
        self.assertTrue(e.target_is_protected(
            holder, "origin", "configmap",
            f"hive-ring-evidence-fleet-{holder.release_sha[:7]}-3ac86f5844fb",
        ))

        release = (ROOT / ".forgejo/scripts/ring-release-fleet.sh").read_text()
        self.assertIn("GOLDEN_PATH_ALERTS=k8s/hive-golden-path-alerts.yaml", release)
        self.assertIn("hive-golden-path-validation", release)

        recover = list(yaml.safe_load_all(
            (ROOT / "k8s" / "hive-ring-executor-rbac.yaml").read_text()
        ))
        recover_role = next(
            item for item in recover
            if item.get("kind") == "Role"
            and item.get("metadata", {}).get("name") == "hive-ring-recover"
            and item.get("metadata", {}).get("namespace") == "shizuha-hive"
        )
        rule_names = next(
            rule["resourceNames"] for rule in recover_role["rules"]
            if rule.get("apiGroups") == ["monitoring.coreos.com"]
            and rule.get("resources") == ["prometheusrules"]
        )
        self.assertIn("hive-golden-path", rule_names)
        extra = list(yaml.safe_load_all(
            (ROOT / "k8s" / "hive-ring-recover-rbac.yaml").read_text()
        ))
        extra_role = next(
            item for item in extra
            if item.get("kind") == "Role"
            and item.get("metadata", {}).get("name") == "hive-ring-recover-golden-path"
        )
        extra_names = [
            name
            for rule in extra_role["rules"]
            if rule.get("apiGroups") == ["monitoring.coreos.com"]
            and rule.get("resources") == ["prometheusrules"]
            for name in (rule.get("resourceNames") or [])
        ]
        self.assertIn("hive-golden-path", extra_names)

    def test_fleet_claw_idle_monitoring_targets_are_fenced_release_targets(self):
        # 2026-09-01: ring-release-fleet.sh applies monitoring ServiceMonitor
        # + PrometheusRule hive-claw-idle, but they were absent from
        # protected_targets. Fenced kubectl rejected the apply and fleet
        # execute 35694 rolled hive-96df61f back to dab2088 (HIVE-580 class).
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m)

        self.assertTrue(e.target_is_protected(
            holder, "monitoring", "prometheusrule", "hive-claw-idle",
        ))
        self.assertTrue(e.target_is_protected(
            holder, "monitoring", "servicemonitor", "hive-claw-idle",
        ))

        release = (ROOT / ".forgejo/scripts/ring-release-fleet.sh").read_text()
        self.assertIn("name: hive-claw-idle", release)
        self.assertIn("kind: ServiceMonitor", release)

        recover = list(yaml.safe_load_all(
            (ROOT / "k8s" / "hive-ring-executor-rbac.yaml").read_text()
        ))
        monitoring_exec = next(
            item for item in recover
            if item.get("kind") == "Role"
            and item.get("metadata", {}).get("name") == "hive-ring-monitoring"
            and item.get("metadata", {}).get("namespace") == "monitoring"
        )
        exec_resources = {
            tuple(rule.get("resources") or [])
            for rule in monitoring_exec["rules"]
            if "monitoring.coreos.com" in (rule.get("apiGroups") or [])
        }
        self.assertTrue(
            any("servicemonitors" in resources for resources in exec_resources),
            exec_resources,
        )
        monitoring_recover = next(
            item for item in recover
            if item.get("kind") == "Role"
            and item.get("metadata", {}).get("name") == "hive-ring-monitoring-recover"
            and item.get("metadata", {}).get("namespace") == "monitoring"
        )
        recover_rule_names = [
            name
            for rule in monitoring_recover["rules"]
            if "prometheusrules" in (rule.get("resources") or [])
            for name in (rule.get("resourceNames") or [])
        ]
        self.assertIn("hive-claw-idle", recover_rule_names)
        recover_sm = [
            name
            for rule in monitoring_recover["rules"]
            if "servicemonitors" in (rule.get("resources") or [])
            for name in (rule.get("resourceNames") or [])
        ]
        self.assertIn("hive-claw-idle", recover_sm)

    def test_public_tls_policy_is_an_exact_fleet_target_with_presubmit_recovery_authority(self):
        e, m = self.executor_module, self.fence_module
        self.assertTrue(e.target_is_protected(self.holder(m), 'shizuha-hive',
                                            'networkpolicy', 'allow-platform-egress'))
        self.assertFalse(e.target_is_protected(self.holder(m), 'shizuha-hive',
                                             'networkpolicy', 'unrelated-egress'))
        self.assertFalse(e.target_is_protected(self.holder(m, ring='canary'), 'shizuha-hive',
                                             'networkpolicy', 'allow-platform-egress'))
        pre = [doc for doc in yaml.safe_load_all(
            (ROOT / 'k8s/hive-ring-recover-rbac.yaml').read_text()) if doc]
        role = next(doc for doc in pre if doc['kind'] == 'Role'
                    and doc['metadata']['name'] == 'hive-ring-recover-platform-egress')
        self.assertEqual(role['metadata']['namespace'], 'shizuha-hive')
        self.assertEqual(role['rules'], [{'apiGroups':['networking.k8s.io'],
            'resources':['networkpolicies'], 'resourceNames':['allow-platform-egress'], 'verbs':['get','patch']}])
        binding = next(doc for doc in pre if doc['kind'] == 'RoleBinding'
                       and doc['metadata']['name'] == role['metadata']['name'])
        self.assertEqual(binding['subjects'], [{'kind':'ServiceAccount', 'name':'hive-ring-recover', 'namespace':'origin'}])
        roles = [doc for doc in yaml.safe_load_all(
            (ROOT / 'k8s/hive-ring-executor-rbac.yaml').read_text()) if doc]
        final = next(doc for doc in roles if doc['kind'] == 'Role'
                     and doc['metadata']['namespace'] == 'shizuha-hive'
                     and doc['metadata']['name'] == 'hive-ring-recover')
        rule = next(rule for rule in final['rules'] if rule['resources'] == ['networkpolicies'])
        self.assertIn('allow-platform-egress', rule['resourceNames'])
        self.assertEqual(rule['verbs'], ['get','patch'])

    def test_recovery_claims_legacy_platform_egress_then_resumes_without_reclaim(self):
        e, m = self.executor_module, self.fence_module
        recovery = self.holder(m, run='100-recovery', job='recover')
        lease = self.lease(recovery, token=8, phase='recovering')
        policy = next(doc for doc in yaml.safe_load_all(
            (ROOT / 'k8s/shizuha-hive-netpol.yaml').read_text())
            if doc and doc['metadata']['name'] == 'allow-platform-egress')
        policy['metadata'].update(uid='existing-platform-egress', resourceVersion='44')

        class Api(self.FakeApi):
            def __init__(inner):
                super().__init__(lease)
                inner.target = json.loads(json.dumps(policy))
                inner.patches = []

            def run(inner, args, *, stdin=None, check=True):
                if '-l' in args:
                    return subprocess.CompletedProcess(args, 0, '{"items":[]}', '')
                offset = args.index('get')
                if args[offset+1:offset+3] != ['networkpolicy','allow-platform-egress']:
                    return subprocess.CompletedProcess(args, 1, '', 'Error from server (NotFound)')
                return subprocess.CompletedProcess(args, 0, json.dumps(inner.target), '')

            def json_patch(inner, namespace, resource, name, operations):
                if resource == 'lease':
                    return super(Api, inner).json_patch(namespace, resource, name, operations)
                inner.patches.append(operations)
                assert operations[0] == {'op':'test','path':'/metadata/resourceVersion','value':'44'}
                assert all(op['path'].startswith('/metadata/') for op in operations)
                inner.target['metadata']['annotations'] = {
                    'shizuha.io/ring-generation':recovery.generation, 'shizuha.io/fence-token':'8'}
                return inner.target

        api = Api()
        fence = m.Fence(recovery, api)
        fence.token = 8
        before_spec = json.loads(json.dumps(policy['spec']))
        e.claim_existing_targets(fence, allow_absent=True, expected_token_ceiling=7)
        self.assertEqual(len(api.patches), 1)
        self.assertEqual(api.target['spec'], before_spec)
        e.claim_existing_targets(fence, allow_absent=True, expected_token_ceiling=7)
        self.assertEqual(len(api.patches), 1)
        for key in ('app.kubernetes.io/managed-by', 'shizuha.io/policy'):
            api.target = json.loads(json.dumps(policy))
            del api.target['metadata']['labels'][key]
            with self.assertRaisesRegex(m.Conflict, 'invalid predecessor ownership'):
                e.claim_existing_targets(fence, allow_absent=True, expected_token_ceiling=7)
        api.target = json.loads(json.dumps(policy))
        api.target['metadata']['annotations'] = {
            'shizuha.io/ring-generation':'another-writer', 'shizuha.io/fence-token':'9'}
        with self.assertRaisesRegex(m.Conflict, 'newer fence token'):
            e.claim_existing_targets(fence, allow_absent=True, expected_token_ceiling=7)
        self.assertEqual(len(api.patches), 1)

    def test_fleet_tier_configmap_is_a_fenced_release_target(self):
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m)

        self.assertTrue(e.target_is_protected(
            holder, "shizuha-hive", "configmap", "hive-tiers",
        ))

    def test_recovery_bootstraps_only_the_exact_legacy_tier_configmap(self):
        e = self.executor_module
        labels = {
            "app.kubernetes.io/name": "hive-api",
            "app.kubernetes.io/component": "tier-config",
            "app.kubernetes.io/managed-by": "shizuha-hive",
        }
        self.assertTrue(e.is_recovery_bootstrap_target(
            "shizuha-hive", "configmap", "hive-tiers",
            {"metadata": {"labels": labels}},
        ))
        for key in labels:
            forged = dict(labels)
            forged.pop(key)
            self.assertFalse(e.is_recovery_bootstrap_target(
                "shizuha-hive", "configmap", "hive-tiers",
                {"metadata": {"labels": forged}},
            ))
        self.assertFalse(e.is_recovery_bootstrap_target(
            "shizuha-hive", "configmap", "other",
            {"metadata": {"labels": labels}},
        ))

    def submit_observation_fixture(self, observations, *, phase="active", mutation_timeout=False):
        """Drive submit's actual create -> observe -> verdict sequence; fake only Kubernetes I/O."""
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m)
        now = [0.0]
        args = type("Args", (), {
            "run_id": holder.run_id, "run_number": holder.run_number,
            "job": holder.job, "ring": holder.ring,
            "release_sha": holder.release_sha, "restored_sha": holder.restored_sha,
            "kubectl": "kubectl", "event_name": "push", "hold": "false",
            "hive725_phase": "phase1", "source_tree": "d" * 40,
            "operation": "release",
        })()

        class Api:
            def __init__(inner):
                inner.calls = []
                inner.observations = 0
                inner.created = set()

            def get_lease(inner, _name):
                return self.lease(holder, phase=phase)

            def run(inner, command, *, stdin=None, check=True):
                inner.calls.append(tuple(command))
                if command[0] == "create":
                    if mutation_timeout:
                        raise subprocess.TimeoutExpired(command, 45)
                    body = json.loads(stdin)
                    inner.created.add((body["kind"], body["metadata"]["name"]))
                if "logs" in command:
                    return subprocess.CompletedProcess(command, 0, "FINAL-JOB-DIAGNOSTICS\n", "")
                if "get" in command and "job" in command:
                    name = command[command.index("job") + 1]
                    if name == e.job_name(holder, e.PREPULL_SUFFIX):
                        # PLAT-8251: the fixture models a warm executor node —
                        # the pre-pull succeeds without consuming the execute
                        # -phase observation script.
                        return subprocess.CompletedProcess(
                            command, 0, json.dumps({"status": {"succeeded": 1}}), "",
                        )
                    if name not in getattr(inner, "metadata_read", set()):
                        inner.metadata_read = getattr(inner, "metadata_read", set()) | {name}
                        return subprocess.CompletedProcess(command, 0, json.dumps({
                            "metadata": {"name": name, "uid": "uid-" + name},
                        }), "")
                    inner.observations += 1
                    outcome = observations[min(inner.observations - 1, len(observations) - 1)]
                    if outcome == "timeout":
                        now[0] += 45
                        raise subprocess.TimeoutExpired(command, 45)
                    if outcome == "real_timeout":
                        # A real local child is killed by subprocess's timeout;
                        # no API outage, production call, or serving change is induced.
                        return subprocess.run(
                            [sys.executable, "-c", "import time; time.sleep(5)"],
                            capture_output=True, text=True, timeout=0.03,
                        )
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return subprocess.CompletedProcess(
                        command, 0, outcome if isinstance(outcome, str) else json.dumps({"status": outcome}), "",
                    )
                return subprocess.CompletedProcess(command, 0, "", "")

        return holder, args, Api(), now

    def test_submit_observation_timeout_does_not_recreate_jobs_or_guess_completion(self):
        e = self.executor_module
        cases = [
            (["real_timeout", {"succeeded": 1}], "active", 0),
            (["timeout", {"failed": 1}], "active", 1),
            (["timeout", {"failed": 1}], "committed", e.POST_COMMIT_FAILURE),
        ]
        for observations, phase, expected in cases:
            with self.subTest(observations=observations, phase=phase):
                holder, args, api, now = self.submit_observation_fixture(observations, phase=phase)
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(e, "Kubectl", return_value=api),
                    mock.patch.object(e.time, "monotonic", side_effect=lambda: now[0]),
                    mock.patch.object(e.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)),
                    mock.patch.object(e.sys, "stdout", stdout),
                    mock.patch.object(e.sys, "stderr", stderr),
                ):
                    self.assertEqual(e.submit(args), expected)
                self.assertEqual(api.observations, 2)
                self.assertEqual(sum(command[0] == "create" for command in api.calls), 4)  # PLAT-8251: +pre-pull
                self.assertIn(("Job", e.job_name(holder, "execute")), api.created)
                self.assertIn("observation timed out", stderr.getvalue())
                if expected:
                    self.assertIn("FINAL-JOB-DIAGNOSTICS", stdout.getvalue())

    def test_submit_repeated_observation_timeouts_reach_original_deadline_with_diagnostics(self):
        e = self.executor_module
        _, args, api, now = self.submit_observation_fixture(["timeout"])
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(e, "Kubectl", return_value=api),
            mock.patch.object(e.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(e.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)),
            mock.patch.object(e.sys, "stdout", stdout),
            mock.patch.object(e.sys, "stderr", stderr),
        ):
            self.assertEqual(e.submit(args), 1)
        self.assertGreater(api.observations, 1)
        self.assertGreaterEqual(now[0], 7500)
        self.assertLessEqual(now[0], 7545)
        self.assertEqual(sum(command[0] == "create" for command in api.calls), 4)  # PLAT-8251: +pre-pull
        self.assertIn("observation deadline", stderr.getvalue())
        self.assertIn("FINAL-JOB-DIAGNOSTICS", stdout.getvalue())

    def test_submit_keeps_permission_invalid_json_and_mutation_timeouts_terminal(self):
        e, m = self.executor_module, self.fence_module
        for outcome, expected in [(m.FenceError("Forbidden"), m.FenceError), ("not-json", json.JSONDecodeError)]:
            with self.subTest(outcome=outcome):
                _, args, api, now = self.submit_observation_fixture([outcome])
                with mock.patch.object(e, "Kubectl", return_value=api), self.assertRaises(expected):
                    e.submit(args)
                self.assertEqual(api.observations, 1)
        _, args, api, _ = self.submit_observation_fixture([{"succeeded": 1}], mutation_timeout=True)
        with mock.patch.object(e, "Kubectl", return_value=api), self.assertRaises(subprocess.TimeoutExpired):
            e.submit(args)
        self.assertEqual(sum(command[0] == "create" for command in api.calls), 1)
        self.assertEqual(api.observations, 0)

    def test_submit_failed_job_keeps_its_verdict_when_diagnostic_read_times_out(self):
        e = self.executor_module
        _, args, api, _ = self.submit_observation_fixture([{"failed": 1}])
        original_run = api.run

        def run_with_unavailable_logs(command, **kwargs):
            if "logs" in command:
                raise subprocess.TimeoutExpired(command, 45)
            return original_run(command, **kwargs)

        stderr = io.StringIO()
        with (
            mock.patch.object(e, "Kubectl", return_value=api),
            mock.patch.object(api, "run", side_effect=run_with_unavailable_logs),
            mock.patch.object(e.sys, "stderr", stderr),
        ):
            self.assertEqual(e.submit(args), 1)
        self.assertEqual(api.observations, 1)
        self.assertIn("logs unavailable", stderr.getvalue())

    def test_execute_final_observation_retries_timeout_but_requires_fresh_fence_authority(self):
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m)
        successor = self.holder(m, run="101")
        for verdict in ("committed", "changed-owner", "changed-token", "recovering", "timeout", "forbidden"):
            with self.subTest(verdict=verdict):
                now = [0.0]

                class Api(self.FakeApi):
                    completed = False
                    final_reads = 0

                    def get_lease(inner, name):
                        if inner.completed:
                            inner.final_reads += 1
                            if inner.final_reads == 1 or verdict == "timeout":
                                now[0] += 45
                                raise subprocess.TimeoutExpired(["kubectl", "get", "lease", name], 45)
                            if verdict == "forbidden":
                                raise m.FenceError("Forbidden")
                        return super().get_lease(name)

                api = Api(self.lease(holder, phase="completed"))

                def child_finished():
                    api.completed = True
                    api.lease["metadata"]["annotations"]["shizuha.io/phase"] = "committed"
                    if verdict == "changed-owner":
                        api.lease["spec"]["holderIdentity"] = successor.serialized()
                    elif verdict == "changed-token":
                        api.lease["metadata"]["annotations"]["shizuha.io/fence-token"] = "99"
                    elif verdict == "recovering":
                        api.lease["metadata"]["annotations"]["shizuha.io/phase"] = "recovering"
                    return 0

                child = mock.Mock()
                child.wait.side_effect = child_finished
                stderr = io.StringIO()
                with (
                    mock.patch.object(e, "Kubectl", return_value=api),
                    mock.patch.object(e, "load_request", return_value=(holder, "image", {
                        "prior_fence_token": 7, "hold": False, "event_name": "push",
                        "hive725_phase": "phase1", "source_tree": "d" * 40, "operation": "release",
                    })),
                    mock.patch.object(e, "claim_existing_targets"),
                    mock.patch.object(e.threading, "Thread"),
                    mock.patch.object(e.subprocess, "Popen", return_value=child) as popen,
                    mock.patch.object(e.time, "monotonic", side_effect=lambda: now[0]),
                    mock.patch.object(e.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)),
                    mock.patch.object(e.sys, "stderr", stderr),
                ):
                    args = type("Args", (), {"request": "hive-ring-request-fleet-100"})()
                    if verdict == "committed":
                        self.assertEqual(e.execute(args), 0)
                    else:
                        with self.assertRaises(m.FenceError):
                            e.execute(args)
                popen.assert_called_once()
                child.wait.assert_called_once()
                self.assertEqual(len(api.patches), 2)  # acquire + active; never replay the release/CAS.
                self.assertIn("observation timed out", stderr.getvalue())
                if verdict == "timeout":
                    self.assertGreaterEqual(now[0], 7500)
                    self.assertLessEqual(now[0], 7545)

    def test_submit_emits_bounded_job_logs_on_failure_and_timeout(self):
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m, ring="canary", job="deploy-canary")
        args = type("Args", (), {
            "run_id": holder.run_id, "run_number": holder.run_number,
            "job": holder.job, "ring": holder.ring,
            "release_sha": holder.release_sha, "restored_sha": holder.restored_sha,
            "kubectl": "kubectl", "event_name": "push", "hold": "false",
            "hive725_phase": "phase1", "source_tree": "d" * 40,
            "operation": "release",
        })()

        class SubmitApi:
            def __init__(inner, status):
                inner.status = status
                inner.calls = []
                inner.execute_gets = 0

            def get_lease(inner, _name):
                return self.lease(holder)

            def run(inner, command, *, stdin=None, check=True):
                inner.calls.append((tuple(command), check))
                if "logs" in command:
                    return subprocess.CompletedProcess(
                        command, 0,
                        "STDOUT-START\n" + "o" * 40_000 + "\nSTDOUT-DECISIVE\n",
                        "STDERR-START\n" + "e" * 40_000 + "\nSTDERR-DECISIVE\n",
                    )
                if "get" in command and "job" in command:
                    name = command[command.index("job") + 1]
                    if name == e.job_name(holder, e.PREPULL_SUFFIX):
                        # PLAT-8251: the pre-pull phase observes the same status
                        # script, so the failure/timeout paths exercise through it.
                        return subprocess.CompletedProcess(
                            command, 0, json.dumps({"status": inner.status}), "",
                        )
                    if "-execute-" in name:
                        inner.execute_gets += 1
                        if inner.execute_gets > 1:
                            return subprocess.CompletedProcess(
                                command, 0, json.dumps({"status": inner.status}), "",
                            )
                    return subprocess.CompletedProcess(
                        command, 0,
                        json.dumps({"metadata": {"name": name, "uid": f"uid-{name}"}}),
                        "",
                    )
                return subprocess.CompletedProcess(command, 0, "", "")

        for status, clock in (({"failed": 1}, [0.0, 1.0, 1.0, 1.0]), ({}, [0.0, 1.0, 3001.0])):
            with self.subTest(status=status):
                api = SubmitApi(status)
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(e, "Kubectl", return_value=api),
                    mock.patch.object(e.time, "monotonic", side_effect=clock),
                    mock.patch.object(e.time, "sleep"),
                    mock.patch.object(e.sys, "stdout", stdout),
                    mock.patch.object(e.sys, "stderr", stderr),
                ):
                    self.assertEqual(e.submit(args), 1)

                self.assertIn("STDOUT-DECISIVE", stdout.getvalue())
                self.assertNotIn("STDOUT-START", stdout.getvalue())
                self.assertIn("STDERR-DECISIVE", stderr.getvalue())
                self.assertNotIn("STDERR-START", stderr.getvalue())
                self.assertLess(len(stdout.getvalue()), e.JOB_LOG_MAX_CHARS + 200)
                self.assertLess(len(stderr.getvalue()), e.JOB_LOG_MAX_CHARS + 500)
                log_call = next(call for call, check in api.calls if "logs" in call)
                self.assertIn("--tail=120", log_call)
                self.assertFalse(next(check for call, check in api.calls if "logs" in call))
                poll_index = max(
                    index for index, (call, _) in enumerate(api.calls)
                    if "get" in call and "job" in call
                )
                log_index = next(
                    index for index, (call, _) in enumerate(api.calls) if "logs" in call
                )
                self.assertLess(poll_index, log_index)

    def test_facade_parses_global_flags_normalizes_gvks_and_rejects_forgery(self):
        f, m = self.facade_module, self.fence_module
        holder = self.holder(m)
        self.assertEqual(
            f.command(["-n", "shizuha-hive", "patch", "deploy/hive"]),
            "patch",
        )
        with self.assertRaisesRegex(m.FenceError, "not allowlisted"):
            f.command(["-n", "kube-system", "edit", "deployment/coredns"])
        self.assertEqual(f.normalized_kind("Probe"), "probe.monitoring.coreos.com")
        self.assertEqual(f.normalized_kind("PrometheusRule"), "prometheusrule")
        self.assertEqual(
            f.normalized_kind("probes.monitoring.coreos.com"),
            "probe.monitoring.coreos.com",
        )

    def test_job_create_drops_server_dry_run_controller_identity(self):
        f, m = self.facade_module, self.fence_module
        holder = self.holder(m)
        value = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": "evidence",
                "uid": "dry-run-uid",
                "labels": {
                    "app": "evidence",
                    "batch.kubernetes.io/controller-uid": "dry-run-uid",
                    "batch.kubernetes.io/job-name": "evidence",
                },
            },
            "spec": {
                "selector": {"matchLabels": {"batch.kubernetes.io/controller-uid": "dry-run-uid"}},
                "template": {"metadata": {"labels": {
                    "app": "evidence",
                    "controller-uid": "dry-run-uid",
                    "job-name": "evidence",
                    "batch.kubernetes.io/controller-uid": "dry-run-uid",
                    "batch.kubernetes.io/job-name": "evidence",
                }}},
            },
        }
        cleaned = f.clean_for_create(value)
        self.assertNotIn("uid", cleaned["metadata"])
        self.assertNotIn("selector", cleaned["spec"])
        self.assertEqual(cleaned["metadata"]["labels"], {"app": "evidence"})
        self.assertEqual(cleaned["spec"]["template"]["metadata"]["labels"], {"app": "evidence"})

        service = f.clean_for_create({
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "hive-canary"},
            "spec": {
                "clusterIP": "10.43.0.0",
                "clusterIPs": ["10.43.0.0"],
                "ipFamilies": ["IPv4"],
                "ipFamilyPolicy": "SingleStack",
                "selector": {"app": "hive-canary"},
                "ports": [{"port": 8030, "nodePort": 0}],
            },
        })
        self.assertNotIn("clusterIP", service["spec"])
        self.assertNotIn("clusterIPs", service["spec"])
        self.assertNotIn("ipFamilies", service["spec"])
        self.assertNotIn("ipFamilyPolicy", service["spec"])
        self.assertNotIn("nodePort", service["spec"]["ports"][0])
        self.assertTrue(f.allowed(holder, "shizuha-hive", "deployment", "hive"))
        self.assertTrue(
            f.allowed(
                holder, "shizuha-hive", "job",
                f"ci-migrate-hive-{holder.release_sha[:7]}",
            )
        )
        for target in (
            ("kube-system", "deployment", "coredns"),
            ("shizuha-hive", "secret", "hive-connect-hmac"),
            ("monitoring", "prometheusrule", "forged-rule"),
            ("shizuha-hive", "job", "ci-migrate-hive-forged"),
        ):
            self.assertFalse(f.allowed(holder, *target), target)

    def test_apply_dry_run_uses_server_side_renderer_without_legacy_annotation(self):
        f = self.facade_module
        rendered = f.with_server_dry_run(["apply", "-f", "-"])
        self.assertIn("--server-side", rendered)
        self.assertIn("--force-conflicts", rendered)
        self.assertIn("--field-manager=hive-ring-dry-run", rendered)
        self.assertIn("--dry-run=server", rendered)
        self.assertEqual(rendered[-2:], ["-o", "json"])

        explicit = f.with_server_dry_run([
            "apply", "--server-side", "--force-conflicts",
            "--field-manager=release-owner", "-f", "manifest.yaml",
        ])
        self.assertEqual(explicit.count("--server-side"), 1)
        self.assertEqual(explicit.count("--force-conflicts"), 1)
        self.assertIn("--field-manager=release-owner", explicit)
        self.assertNotIn("--field-manager=hive-ring-dry-run", explicit)

    def test_cas_rejection_names_the_exact_typed_resource(self):
        f, m = self.facade_module, self.fence_module
        holder = self.holder(m)

        class RejectingApi:
            def json_patch(self, *_args):
                raise m.FenceError("The request is invalid")

            def run(self, args, **_kwargs):
                return subprocess.CompletedProcess(args, 0, json.dumps(current), "")

        fence = type("RejectingFence", (), {
            "holder": holder,
            "token": 7,
            "api": RejectingApi(),
            "target_tests": lambda _self, _current: [],
        })()
        current = {
            "metadata": {
                "resourceVersion": "44",
                "annotations": {
                    "shizuha.io/ring-generation": holder.generation,
                    "shizuha.io/fence-token": "7",
                },
            },
            "spec": {},
        }
        desired = {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {
                "name": "hive725-non-connect-abcdef0",
                "namespace": "shizuha-hive",
            },
            "spec": {},
        }
        with self.assertRaisesRegex(
            m.FenceError,
            "CAS update rejected for shizuha-hive/job/hive725-non-connect-abcdef0",
        ):
            f.cas_replace(fence, current, desired)

    def test_cas_retries_only_after_owned_resource_version_advances(self):
        f, m = self.facade_module, self.fence_module
        holder = self.holder(m)
        current = {
            "metadata": {
                "uid": "uid-1", "resourceVersion": "44",
                "annotations": {
                    "shizuha.io/ring-generation": holder.generation,
                    "shizuha.io/fence-token": "7",
                },
            },
            "spec": {"template": {"spec": {"containers": [{"image": "old"}]}}},
        }
        refreshed = json.loads(json.dumps(current))
        refreshed["metadata"]["resourceVersion"] = "45"

        class RacingApi:
            def __init__(self):
                self.patches = 0

            def json_patch(self, _ns, _kind, _name, _operations):
                self.patches += 1
                if self.patches == 1:
                    raise m.FenceError("The request is invalid")
                return {"metadata": {"resourceVersion": "46"}}

            def run(self, args, **_kwargs):
                return subprocess.CompletedProcess(args, 0, json.dumps(refreshed), "")

        api = RacingApi()

        def target_tests(_self, value):
            annotations = value["metadata"]["annotations"]
            self.assertEqual(annotations["shizuha.io/fence-token"], "7")
            return [{
                "op": "test", "path": "/metadata/resourceVersion",
                "value": value["metadata"]["resourceVersion"],
            }]

        fence = type("RacingFence", (), {
            "holder": holder, "token": 7, "api": api,
            "target_tests": target_tests,
        })()
        desired = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "hive-codex-refresh", "namespace": "shizuha-hive"},
            "spec": {"template": {"spec": {"containers": [{"image": "new"}]}}},
        }
        with mock.patch.object(f.time, "sleep"):
            result = f.cas_replace(fence, current, desired)
        self.assertEqual(result["metadata"]["resourceVersion"], "46")
        self.assertEqual(api.patches, 2)

    def test_facade_resolves_every_non_manifest_mutation_class_exactly_once(self):
        f = self.facade_module
        cases = (
            (["-n", "shizuha-hive", "patch", "deploy/hive", "--type=merge", "-p", "{}"], "patch", ("shizuha-hive", "deployment", "hive")),
            (["patch", "-n", "shizuha-hive", "deploy/hive", "--type=merge", "--patch-file=/tmp/patch.json"], "patch", ("shizuha-hive", "deployment", "hive")),
            (["-n", "shizuha-hive", "set", "image", "deploy/hive", "hive=x"], "set", ("shizuha-hive", "deployment", "hive")),
            (["-n", "shizuha-hive", "set", "env", "deploy/hive", "A=b"], "set", ("shizuha-hive", "deployment", "hive")),
            (["-n", "shizuha-hive", "label", "deploy", "hive", "build_sha=x"], "label", ("shizuha-hive", "deployment", "hive")),
            (["-n", "shizuha-hive", "annotate", "deploy/hive", "a=b"], "annotate", ("shizuha-hive", "deployment", "hive")),
            (["-n", "shizuha-hive", "delete", "job", "ci-migrate-hive-abc"], "delete", ("shizuha-hive", "job", "ci-migrate-hive-abc")),
            (["-n", "shizuha-hive", "scale", "deploy/hive", "--replicas=2"], "scale", ("shizuha-hive", "deployment", "hive")),
            (["-n", "shizuha-hive", "create", "job", "sample", "--from=cronjob/x"], "create", ("shizuha-hive", "job", "sample")),
        )
        for args, command, target in cases:
            with self.subTest(args=args):
                self.assertEqual(f.simple_targets(args, command), [target])

        class SelectorApi:
            def run(self, _args):
                body = {"items": [{"metadata": {"name": "probe-shizuha-hive-fleet-abcdef0"}}]}
                return subprocess.CompletedProcess(_args, 0, json.dumps(body), "")

        selector_fence = type("SelectorFence", (), {"api": SelectorApi()})()
        self.assertEqual(
            f.selector_targets(
                selector_fence,
                ["-n", "monitoring", "delete", "probe", "-l", "service=hive,release_ring=fleet"],
                "delete",
            ),
            [("monitoring", "probe.monitoring.coreos.com", "probe-shizuha-hive-fleet-abcdef0")],
        )
        self.assertTrue(f.has_selector(["delete", "probe", "--selector=service=hive,release_ring=canary"]))
        with self.assertRaisesRegex(self.fence_module.FenceError, "not an allowlisted"):
            f.selector_targets(
                selector_fence,
                ["-n", "monitoring", "delete", "probe", "-l", "app=forged"],
                "delete",
            )

    def test_facade_preserves_dry_run_and_metadata_and_preconditions_delete(self):
        f = self.facade_module
        self.assertTrue(f.has_server_dry_run(["apply", "--dry-run=server", "-f", "x"]))
        self.assertTrue(f.has_server_dry_run(["apply", "--dry-run", "server", "-f", "x"]))
        self.assertFalse(f.has_server_dry_run(["apply", "--dry-run=client", "-f", "x"]))
        self.assertTrue(f.ignores_not_found(["delete", "job", "x", "--ignore-not-found"]))
        self.assertTrue(f.ignores_not_found(["delete", "job", "x", "--ignore-not-found=true"]))
        self.assertFalse(f.ignores_not_found(["delete", "job", "x"]))
        self.assertTrue(f.waits_for_delete(["delete", "job", "x"]))
        self.assertFalse(f.waits_for_delete(["delete", "job", "x", "--wait=false"]))

        class Api:
            def __init__(self):
                self.patches = []
                self.runs = []

            def json_patch(self, namespace, kind, name, operations):
                self.patches.append((namespace, kind, name, operations))
                return {"metadata": {"uid": "uid-7", "resourceVersion": "92"}}

            def run(self, args, *, stdin=None, check=True):
                self.runs.append((args, stdin, check))
                return subprocess.CompletedProcess(args, 0, "", "")

        class FacadeFence:
            def __init__(self):
                self.holder = type("H", (), {"generation": "generation-7"})()
                self.token = 7
                self.api = Api()
                self.validations = 0

            def validate(self):
                self.validations += 1

            def target_tests(self, _current):
                return [
                    {"op": "test", "path": "/metadata/resourceVersion", "value": "91"},
                    {"op": "test", "path": "/metadata/annotations/shizuha.io~1ring-generation", "value": "generation-7"},
                    {"op": "test", "path": "/metadata/annotations/shizuha.io~1fence-token", "value": "7"},
                ]

        fence = FacadeFence()
        current = {
            "metadata": {
                "name": "hive", "namespace": "shizuha-hive", "resourceVersion": "91",
                "labels": {"controller-owned": "keep"},
                "annotations": {
                    "controller-owned": "keep",
                    "shizuha.io/ring-generation": "generation-7",
                    "shizuha.io/fence-token": "7",
                },
            },
            "spec": {"replicas": 1},
        }
        desired = {
            "kind": "Deployment",
            "metadata": {"name": "hive", "namespace": "shizuha-hive", "labels": {"build_sha": SHA}},
            "spec": {"replicas": 2},
        }
        f.cas_replace(fence, current, desired)
        operations = fence.api.patches[0][3]
        labels = next(item["value"] for item in operations if item["path"] == "/metadata/labels")
        annotations = next(item["value"] for item in operations if item["path"] == "/metadata/annotations")
        self.assertEqual(labels["controller-owned"], "keep")
        self.assertEqual(labels["build_sha"], SHA)
        self.assertEqual(annotations["controller-owned"], "keep")
        self.assertEqual(annotations["shizuha.io/fence-token"], "7")

        f.raw_delete(
            fence, "shizuha-hive", "deployment", "hive", current,
            wait_for_delete=True,
        )
        args, body, _ = fence.api.runs[-2]
        self.assertEqual(args[0], "delete")
        self.assertIn("/apis/apps/v1/namespaces/shizuha-hive/deployments/hive", args[1])
        options = json.loads(body)
        self.assertEqual(options["preconditions"], {"uid": "uid-7", "resourceVersion": "92"})
        self.assertEqual(fence.api.runs[-1][0][:4], [
            "-n", "shizuha-hive", "wait", "--for=delete",
        ])
        self.assertGreaterEqual(fence.validations, 1)

    def test_service_cas_omits_allocator_owned_fields_from_legacy_object(self):
        f = self.facade_module

        class Api:
            def __init__(self):
                self.operations = None

            def json_patch(self, _namespace, _kind, _name, operations):
                self.operations = operations
                return {"metadata": {"resourceVersion": "92"}}

        class FacadeFence:
            def __init__(self):
                self.holder = type("H", (), {"generation": "generation-7"})()
                self.token = 7
                self.api = Api()

            def target_tests(self, _current):
                return [{"op": "test", "path": "/metadata/resourceVersion", "value": "91"}]

        fence = FacadeFence()
        current = {
            "kind": "Service",
            "metadata": {"name": "hive-canary", "namespace": "shizuha-hive"},
            "spec": {
                "clusterIP": "10.43.0.0",
                "clusterIPs": ["10.43.0.0"],
                "ipFamilies": ["IPv4"],
                "ipFamilyPolicy": "SingleStack",
                "selector": {"build_sha": OLD_SHA},
                "ports": [{"port": 8030, "protocol": "TCP"}],
            },
        }
        desired = {
            "kind": "Service",
            "metadata": {"name": "hive-canary", "namespace": "shizuha-hive"},
            "spec": {
                "clusterIP": "10.43.0.0",
                "clusterIPs": ["10.43.0.0"],
                "ipFamilies": ["IPv4"],
                "ipFamilyPolicy": "SingleStack",
                "selector": {"build_sha": SHA},
                "ports": [{"port": 8030, "protocol": "TCP"}],
                "type": "ClusterIP",
            },
        }
        f.cas_replace(fence, current, desired)
        paths = {item["path"] for item in fence.api.operations}
        self.assertNotIn("/spec", paths)
        for field in f.SERVICE_ALLOCATOR_FIELDS:
            self.assertNotIn(f"/spec/{field}", paths)
        self.assertIn("/spec/selector", paths)
        self.assertIn("/spec/ports", paths)

    def test_imperative_set_image_patches_only_the_dry_run_delta(self):
        f = self.facade_module

        class Api:
            def __init__(self):
                self.operations = None

            def json_patch(self, _namespace, _kind, _name, operations):
                self.operations = operations
                return {"metadata": {"resourceVersion": "92"}}

        class FacadeFence:
            def __init__(self):
                self.holder = type("H", (), {"generation": "generation-7"})()
                self.token = 7
                self.api = Api()

            def target_tests(self, _current):
                return [{"op": "test", "path": "/metadata/resourceVersion", "value": "91"}]

        current = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "hive-codex-refresh", "namespace": "shizuha-hive",
                "resourceVersion": "91",
                "annotations": {
                    "deployment.kubernetes.io/revision": "4",
                    "shizuha.io/ring-generation": "generation-7",
                    "shizuha.io/fence-token": "7",
                },
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "hive-codex-refresh"}},
                "template": {"spec": {"containers": [
                    {"name": "codex-refresh", "image": "registry/hive:old"},
                ]}},
            },
        }
        desired = json.loads(json.dumps(current))
        desired["spec"]["template"]["spec"]["containers"][0]["image"] = "registry/hive:new"
        fence = FacadeFence()
        f.cas_imperative_update(
            fence, current, desired,
            ["-n", "shizuha-hive", "set", "image", "deploy/hive-codex-refresh", "codex-refresh=registry/hive:new"],
            "",
        )
        operations = fence.api.operations
        paths = [operation["path"] for operation in operations]
        self.assertNotIn("/spec", paths)
        self.assertNotIn("/spec/selector", paths)
        image = next(
            operation for operation in operations
            if operation["path"] == "/spec/template/spec/containers/0/image"
        )
        self.assertEqual(image["value"], "registry/hive:new")
        self.assertIn({
            "op": "test",
            "path": "/spec/template/spec/containers/0/name",
            "value": "codex-refresh",
        }, operations)
        self.assertIn("/metadata/resourceVersion", paths)

    def test_production_order_apply_base_then_set_named_image_confines_intent(self):
        f, m = self.facade_module, self.fence_module
        holder = self.holder(m)
        manifest_path = ROOT / "k8s" / "hive-codex-refresh-deploy.yaml"
        state = yaml.safe_load(manifest_path.read_text())
        state["metadata"]["resourceVersion"] = "91"
        annotations = state["metadata"].setdefault("annotations", {})
        annotations["shizuha.io/ring-generation"] = holder.generation
        annotations["shizuha.io/fence-token"] = "7"
        container = state["spec"]["template"]["spec"]["containers"][0]
        container["image"] = "registry/hive:old"

        class Api:
            def __init__(self):
                self.state = json.loads(json.dumps(state))
                self.patches = []

            def run(inner, args, *, stdin=None, check=True):
                if "get" in args:
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps(inner.state), "",
                    )
                raise AssertionError(args)

            def json_patch(inner, _namespace, _kind, _name, operations):
                inner.patches.append(json.loads(json.dumps(operations)))
                for operation in operations:
                    if operation["op"] == "test":
                        continue
                    parts = [
                        item.replace("~1", "/").replace("~0", "~")
                        for item in operation["path"].split("/")[1:]
                    ]
                    cursor = inner.state
                    for part in parts[:-1]:
                        cursor = cursor[int(part)] if isinstance(cursor, list) else cursor.setdefault(part, {})
                    leaf = parts[-1]
                    if operation["op"] == "remove":
                        (cursor.pop(int(leaf)) if isinstance(cursor, list) else cursor.pop(leaf, None))
                    elif isinstance(cursor, list):
                        cursor[int(leaf)] = operation["value"]
                    else:
                        cursor[leaf] = operation["value"]
                inner.state["metadata"]["resourceVersion"] = str(
                    int(inner.state["metadata"]["resourceVersion"]) + 1
                )
                return json.loads(json.dumps(inner.state))

        api = Api()

        class FacadeFence:
            def __init__(self, *_args):
                self.holder = holder
                self.token = 7
                self.api = api

            def validate(self, *_args):
                return None

            def target_tests(self, current):
                return [
                    {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
                    {"op": "test", "path": "/metadata/annotations/shizuha.io~1ring-generation", "value": holder.generation},
                    {"op": "test", "path": "/metadata/annotations/shizuha.io~1fence-token", "value": "7"},
                ]

        def dry_run(command, **kwargs):
            if "set" in command and "image" in command:
                desired = json.loads(json.dumps(api.state))
                desired["spec"]["template"]["spec"]["containers"][0]["image"] = "registry/hive:new"
                # Model admission/controller material that a generic full-object
                # diff would wrongly promote into command intent.
                desired["metadata"]["annotations"]["controller.example/default"] = "changed"
                desired["spec"]["selector"] = {"matchLabels": {"forged": "default"}}
                return subprocess.CompletedProcess(command, 0, json.dumps(desired), "")
            desired = json.loads(kwargs["input"])
            return subprocess.CompletedProcess(command, 0, json.dumps(desired), "")

        apply_args = ["kubectl", "apply", "-f", str(manifest_path)]
        set_args = [
            "kubectl", "-n", "shizuha-hive", "set", "image",
            "deploy/hive-codex-refresh", "codex-refresh=registry/hive:new",
        ]
        with (
            mock.patch.object(f, "Holder", type("PatchedHolder", (), {"from_env": staticmethod(lambda: holder)})),
            mock.patch.object(f, "Fence", FacadeFence),
            mock.patch.object(f, "Kubectl", return_value=api),
            mock.patch.object(f.subprocess, "run", side_effect=dry_run),
            mock.patch.object(f.sys, "stdin", io.StringIO("")),
            mock.patch.object(f.sys, "argv", apply_args),
            mock.patch.dict(os.environ, {"RING_FENCE_TOKEN": "7"}),
        ):
            self.assertEqual(f.main(), 0)
        with (
            mock.patch.object(f, "Holder", type("PatchedHolder", (), {"from_env": staticmethod(lambda: holder)})),
            mock.patch.object(f, "Fence", FacadeFence),
            mock.patch.object(f, "Kubectl", return_value=api),
            mock.patch.object(f.subprocess, "run", side_effect=dry_run),
            mock.patch.object(f.sys, "stdin", io.StringIO("")),
            mock.patch.object(f.sys, "argv", set_args),
            mock.patch.dict(os.environ, {"RING_FENCE_TOKEN": "7"}),
        ):
            self.assertEqual(f.main(), 0)

        imperative = api.patches[-1]
        self.assertIn(
            {"op": "test", "path": "/spec/template/spec/containers/0/name", "value": "codex-refresh"},
            imperative,
        )
        self.assertIn(
            {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "registry/hive:new"},
            imperative,
        )
        self.assertNotIn("/spec", {item["path"] for item in imperative})
        self.assertEqual(
            api.state["spec"]["selector"],
            state["spec"]["selector"],
        )
        self.assertNotIn("controller.example/default", api.state["metadata"]["annotations"])

    def test_each_imperative_verb_is_confined_to_declared_paths_and_stale_fenced(self):
        f, m = self.facade_module, self.fence_module

        class Api:
            def __init__(self):
                self.operations = None

            def json_patch(self, _namespace, _kind, _name, operations):
                self.operations = operations
                return {"metadata": {"resourceVersion": "92"}}

        class FacadeFence:
            def __init__(self, stale=False):
                self.holder = type("H", (), {"generation": "generation-7"})()
                self.token = 7
                self.api = Api()
                self.stale = stale

            def target_tests(self, _current):
                if self.stale:
                    raise m.Conflict("stale generation")
                return [
                    {"op": "test", "path": "/metadata/resourceVersion", "value": "91"},
                    {"op": "test", "path": "/metadata/annotations/shizuha.io~1ring-generation", "value": "generation-7"},
                    {"op": "test", "path": "/metadata/annotations/shizuha.io~1fence-token", "value": "7"},
                ]

        current = {
            "kind": "Deployment",
            "metadata": {
                "name": "hive", "namespace": "shizuha-hive", "resourceVersion": "91",
                "labels": {"build_sha": OLD_SHA},
                "annotations": {
                    "shizuha.io/ring-generation": "generation-7",
                    "shizuha.io/fence-token": "7",
                },
            },
            "spec": {
                "replicas": 2,
                "selector": {"matchLabels": {"app": "hive"}},
                "template": {"spec": {"containers": [{
                    "name": "hive", "image": "registry/hive:old",
                    "env": [{"name": "KEEP", "value": "1"}],
                }]}},
            },
        }
        cases = []
        desired = json.loads(json.dumps(current))
        desired["metadata"]["labels"]["build_sha"] = SHA
        cases.append((
            ["-n", "shizuha-hive", "label", "deploy/hive", f"build_sha={SHA}", "--overwrite"],
            "", desired, {"/metadata/labels/build_sha"},
        ))
        desired = json.loads(json.dumps(current))
        desired["metadata"]["annotations"]["last-successful-sha"] = SHA
        cases.append((
            ["-n", "shizuha-hive", "annotate", "deploy/hive", f"last-successful-sha={SHA}", "--overwrite"],
            "", desired, {"/metadata/annotations/last-successful-sha"},
        ))
        desired = json.loads(json.dumps(current))
        desired["spec"]["replicas"] = 3
        cases.append((
            ["-n", "shizuha-hive", "scale", "deploy/hive", "--replicas=3"],
            "", desired, {"/spec/replicas"},
        ))
        desired = json.loads(json.dumps(current))
        desired["spec"]["template"]["spec"]["nodeSelector"] = {"shizuha.io/platform": "true"}
        payload = '{"spec":{"template":{"spec":{"nodeSelector":{"shizuha.io/platform":"true"}}}}}'
        cases.append((
            ["-n", "shizuha-hive", "patch", "deploy/hive", "--type=merge", "--patch-file=/dev/stdin"],
            payload, desired, {"/spec/template/spec/nodeSelector/shizuha.io~1platform"},
        ))
        desired = json.loads(json.dumps(current))
        desired["spec"]["template"]["spec"]["containers"][0]["env"].append({
            "name": "HIVE_CONNECT_ENFORCE_DEDICATED_LISTENER", "value": "true",
        })
        cases.append((
            ["-n", "shizuha-hive", "set", "env", "deploy/hive", "--containers=hive", "HIVE_CONNECT_ENFORCE_DEDICATED_LISTENER=true"],
            "", desired, {"/spec/template/spec/containers/0/env"},
        ))

        for args, stdin, desired, expected_mutations in cases:
            with self.subTest(args=args):
                fence = FacadeFence()
                f.cas_imperative_update(fence, current, desired, args, stdin)
                operations = fence.api.operations
                mutation_paths = {
                    item["path"] for item in operations if item["op"] != "test"
                }
                self.assertEqual(mutation_paths, expected_mutations)
                if args[2:4] == ["set", "env"]:
                    self.assertIn({
                        "op": "test",
                        "path": "/spec/template/spec/containers/0/name",
                        "value": "hive",
                    }, operations)
                self.assertNotIn("/spec", mutation_paths)
                self.assertNotIn("/metadata", mutation_paths)
                with self.assertRaisesRegex(m.Conflict, "stale generation"):
                    f.cas_imperative_update(
                        FacadeFence(stale=True), current, desired, args, stdin,
                    )

    def test_imperative_update_retries_invalid_request_after_resource_version_advances(self):
        # Fleet STREAM promote 2026-09-06: image rollout succeeded, then the
        # next annotate/set env JSON Patch lost the resourceVersion CAS and
        # k3s returned generic 422 Invalid. Retry only when RV advanced.
        f, m = self.facade_module, self.fence_module
        sha = "a" * 40

        class Api:
            def __init__(inner):
                inner.patches = []
                inner.target = {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {
                        "name": "hive", "namespace": "shizuha-hive",
                        "uid": "stable-uid", "resourceVersion": "91",
                        "annotations": {
                            "hive725-phase1-sha": "old",
                            "shizuha.io/ring-generation": "generation-7",
                            "shizuha.io/fence-token": "7",
                        },
                    },
                }

            def run(inner, args, *, stdin=None, check=True):
                return subprocess.CompletedProcess(
                    args, 0, json.dumps(inner.target), "",
                )

            def json_patch(inner, _namespace, _kind, _name, operations):
                inner.patches.append(json.loads(json.dumps(operations)))
                if len(inner.patches) == 1:
                    inner.target["metadata"]["resourceVersion"] = "92"
                    raise m.FenceError(
                        "Kubernetes API failure rc=1: The request is invalid: "
                        "the server rejected our request due to an error in our request"
                    )
                inner.target["metadata"]["annotations"]["hive725-phase1-sha"] = sha
                inner.target["metadata"]["resourceVersion"] = "93"
                return json.loads(json.dumps(inner.target))

        class FacadeFence:
            holder = type("H", (), {"generation": "generation-7"})()
            token = 7
            api = Api()

            @staticmethod
            def target_tests(live):
                return [{
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": str(live["metadata"]["resourceVersion"]),
                }]

        current = json.loads(json.dumps(FacadeFence.api.target))
        desired = json.loads(json.dumps(current))
        desired["metadata"]["annotations"]["hive725-phase1-sha"] = sha
        args = [
            "-n", "shizuha-hive", "annotate", "deploy/hive",
            f"hive725-phase1-sha={sha}", "--overwrite",
        ]
        result = f.cas_imperative_update(FacadeFence(), current, desired, args, "")
        self.assertEqual(len(FacadeFence.api.patches), 2)
        self.assertEqual(result["metadata"]["annotations"]["hive725-phase1-sha"], sha)
        self.assertIn(
            {"op": "test", "path": "/metadata/resourceVersion", "value": "92"},
            FacadeFence.api.patches[1],
        )

    def test_imperative_update_does_not_retry_unchanged_invalid_request(self):
        f, m = self.facade_module, self.fence_module

        class Api:
            def __init__(inner):
                inner.patch_count = 0
                inner.target = {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {
                        "name": "hive", "namespace": "shizuha-hive",
                        "uid": "stable-uid", "resourceVersion": "91",
                        "annotations": {"hive725-phase1-sha": "old"},
                    },
                }

            def run(inner, args, *, stdin=None, check=True):
                return subprocess.CompletedProcess(
                    args, 0, json.dumps(inner.target), "",
                )

            def json_patch(inner, _namespace, _kind, _name, _operations):
                inner.patch_count += 1
                raise m.FenceError("deterministic invalid patch")

        class FacadeFence:
            holder = type("H", (), {"generation": "generation-7"})()
            token = 7
            api = Api()

            @staticmethod
            def target_tests(_live):
                return [{"op": "test", "path": "/metadata/resourceVersion", "value": "91"}]

        current = json.loads(json.dumps(FacadeFence.api.target))
        desired = json.loads(json.dumps(current))
        desired["metadata"]["annotations"]["hive725-phase1-sha"] = "a" * 40
        args = [
            "-n", "shizuha-hive", "annotate", "deploy/hive",
            "hive725-phase1-sha=" + "a" * 40, "--overwrite",
        ]
        with self.assertRaisesRegex(m.FenceError, "deterministic invalid patch"):
            f.cas_imperative_update(FacadeFence(), current, desired, args, "")
        self.assertEqual(FacadeFence.api.patch_count, 1)

    def test_imperative_metadata_guards_reject_reserved_alias_and_traversal_paths(self):
        f, m = self.facade_module, self.fence_module

        class Api:
            def json_patch(self, _namespace, _kind, _name, _operations):
                return {"metadata": {"resourceVersion": "92"}}

        class FacadeFence:
            holder = type("H", (), {"generation": "generation-7"})()
            token = 7
            api = Api()

            @staticmethod
            def target_tests(_current):
                return [{"op": "test", "path": "/metadata/resourceVersion", "value": "91"}]

        current = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "hive", "namespace": "shizuha-hive", "resourceVersion": "91",
                "labels": {"build_sha": OLD_SHA, "pod-template-hash": "owned"},
                "annotations": {
                    "deployment.kubernetes.io/revision": "4",
                    "shizuha.io/ring-generation": "generation-7",
                    "shizuha.io/fence-token": "7",
                },
            },
            "spec": {"replicas": 1},
        }
        forbidden = (
            (
                ["-n", "shizuha-hive", "label", "deploy/hive", "pod-template-hash=forged", "--overwrite"],
                "",
                {**current, "metadata": {**current["metadata"], "labels": {**current["metadata"]["labels"], "pod-template-hash": "forged"}}},
            ),
            (
                ["-n", "shizuha-hive", "annotate", "deploy/hive", "shizuha.io/ring-generation=forged", "--overwrite"],
                "",
                {**current, "metadata": {**current["metadata"], "annotations": {**current["metadata"]["annotations"], "shizuha.io/ring-generation": "forged"}}},
            ),
            (
                ["-n", "shizuha-hive", "patch", "deploy/hive", "--type=merge", "--patch-file=/dev/stdin"],
                '{"metadata":{"annotations":{"shizuha.io/fence-token":"8"}}}',
                {**current, "metadata": {**current["metadata"], "annotations": {**current["metadata"]["annotations"], "shizuha.io/fence-token": "8"}}},
            ),
            (
                ["-n", "shizuha-hive", "patch", "deploy/hive", "--type=json", "--patch-file=/dev/stdin"],
                '[{"op":"replace","path":"/metadata/annotations/shizuha.io~1ring-generation","value":"forged"}]',
                current,
            ),
        )
        for args, stdin, desired in forbidden:
            with self.subTest(args=args):
                with self.assertRaisesRegex(m.FenceError, "guard-owned metadata"):
                    f.cas_imperative_update(FacadeFence(), current, desired, args, stdin)

        for pointer in (
            "/metadata/annotations",
            "/metadata/annotations/user.example/key/child",
            "/metadata/annotations/user.example~2key",
        ):
            with self.subTest(pointer=pointer):
                with self.assertRaises(m.FenceError):
                    path = f._parse_pointer(pointer)
                    f._validate_imperative_path(path)

        for path in (
            ["metadata", "annotations", "last-successful-sha"],
            ["metadata", "annotations", "shizuha.io/last-successful-sha"],
            ["metadata", "labels", "build_sha"],
        ):
            f._validate_imperative_path(path)

        # Origin 38401: last-successful CAS tests /metadata/resourceVersion.
        f._validate_imperative_path(["metadata", "resourceVersion"], op="test")
        with self.assertRaisesRegex(m.FenceError, "cannot rewrite resourceVersion"):
            f._validate_imperative_path(["metadata", "resourceVersion"], op="replace")
        with self.assertRaisesRegex(m.FenceError, "cannot rewrite resourceVersion"):
            f._validate_imperative_path(["metadata", "resourceVersion"])

    def test_json_patch_accepts_resourceversion_cas_test_for_release_anchor(self):
        """record-successful-release.py's atomic patch must compile.

        Origin 38401 fleet-gated 7493175, then refused the rollback-anchor
        advance because the fence rejected the JSON Patch `test` of
        /metadata/resourceVersion.
        """
        f, m = self.facade_module, self.fence_module
        current = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "hive",
                "namespace": "shizuha-hive",
                "uid": "uid-1",
                "resourceVersion": "91",
                "labels": {"build_sha": "a" * 40},
                "annotations": {
                    "last-successful-sha": "b" * 40,
                    "shizuha.io/last-successful-sha": "b" * 40,
                },
            },
            "spec": {
                "replicas": 2,
                "template": {
                    "metadata": {"labels": {"build_sha": "a" * 40}},
                    "spec": {"containers": [{"name": "hive", "image": "hive-aaaaaaa"}]},
                },
            },
        }
        desired = json.loads(json.dumps(current))
        sha = "a" * 40
        stdin = json.dumps([
            {"op": "test", "path": "/metadata/resourceVersion", "value": "91"},
            {"op": "test", "path": "/metadata/labels/build_sha", "value": sha},
            {"op": "test", "path": "/spec/template/metadata/labels/build_sha", "value": sha},
            {"op": "test", "path": "/spec/template/spec/containers/0/image", "value": "hive-aaaaaaa"},
            {"op": "add", "path": "/metadata/annotations/last-successful-sha", "value": sha},
            {"op": "add", "path": "/metadata/annotations/shizuha.io~1last-successful-sha", "value": sha},
        ])
        args = [
            "-n", "shizuha-hive", "patch", "deployment", "hive",
            "--type=json", "--patch-file=/dev/stdin", "-o", "json",
        ]
        ops = f._patch_operations(args, stdin, current, desired)
        self.assertEqual(ops[0]["path"], "/metadata/resourceVersion")
        self.assertEqual(ops[0]["op"], "test")
        rewrite = json.dumps([
            {"op": "replace", "path": "/metadata/resourceVersion", "value": "forged"},
        ])
        with self.assertRaisesRegex(m.FenceError, "cannot rewrite resourceVersion"):
            f._patch_operations(args, rewrite, current, desired)

    def test_fleet_golden_path_wait_treats_failed_as_terminal(self):
        release = (SCRIPTS / "ring-release-fleet.sh").read_text()
        self.assertIn("status.failed", release)
        self.assertIn("designed RED while HIVE-566 is deferred", release)
        self.assertNotIn(
            'wait --for=condition=complete \\\n  "job/${GOLDEN_PATH_JOB}" --timeout=1800s',
            release,
        )

    def test_evidence_configmap_uses_create_without_last_applied_annotation(self):
        capture = (SCRIPTS / "capture-ring-evidence.sh").read_text()
        self.assertIn('kubectl create -f "$CONFIGMAP_JSON"', capture)
        self.assertIn('metadata["ownerReferences"]', capture)
        self.assertIn('"shizuha.io/evidence-sha256"', capture)
        self.assertIn('"shizuha.io/evidence-bytes"', capture)
        self.assertIn('metadata.get("uid")', capture)
        self.assertNotIn('label configmap "$NAME"', capture)
        self.assertNotIn('patch configmap "$NAME"', capture)
        self.assertNotIn("kubectl apply -f -", capture)

    def test_large_json_patch_is_streamed_and_delete_refreshes_controller_race(self):
        m, f = self.fence_module, self.facade_module
        large = [{"op": "add", "path": "/data/evidence", "value": "x" * 700_000}]
        response = subprocess.CompletedProcess(
            ["kubectl"], 0, '{"metadata":{"resourceVersion":"92"}}', "",
        )
        with mock.patch.object(m.subprocess, "run", return_value=response) as run_patch:
            m.Kubectl("kubectl").json_patch("origin", "configmap", "evidence", large)
        args = run_patch.call_args.args[0]
        kwargs = run_patch.call_args.kwargs
        self.assertIn("--patch-file=/dev/stdin", args)
        self.assertNotIn("-p", args)
        self.assertGreater(len(kwargs["input"]), 700_000)

        with mock.patch.object(m.subprocess, "run", return_value=response) as run_patch:
            m.Kubectl("kubectl").run([
                "-n", "shizuha-hive", "rollout", "status",
                "deployment/hive", "--timeout=1800s",
            ])
        self.assertEqual(run_patch.call_args.kwargs["timeout"], 1815)

        class Api:
            def __init__(self):
                self.patch_attempts = 0
                self.runs = []

            def json_patch(self, _namespace, _kind, _name, _operations):
                self.patch_attempts += 1
                if self.patch_attempts == 1:
                    raise m.Conflict("controller advanced resourceVersion")
                return {"metadata": {"uid": "uid-7", "resourceVersion": "93"}}

            def run(self, args, *, stdin=None, check=True):
                self.runs.append((args, stdin, check))
                if "get" in args:
                    body = {
                        "metadata": {
                            "name": "hive-canary", "uid": "uid-7",
                            "resourceVersion": "92",
                            "annotations": {
                                "shizuha.io/ring-generation": "generation-7",
                                "shizuha.io/fence-token": "7",
                            },
                        },
                    }
                    return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
                return subprocess.CompletedProcess(args, 0, "", "")

        class FacadeFence:
            def __init__(self):
                self.holder = type("H", (), {"generation": "generation-7"})()
                self.token = 7
                self.api = Api()
                self.validations = 0

            def validate(self):
                self.validations += 1

            def target_tests(self, current):
                annotations = current["metadata"]["annotations"]
                if annotations["shizuha.io/ring-generation"] != "generation-7":
                    raise m.Conflict("generation changed")
                if annotations["shizuha.io/fence-token"] != "7":
                    raise m.Conflict("token changed")
                return [{
                    "op": "test", "path": "/metadata/resourceVersion",
                    "value": current["metadata"]["resourceVersion"],
                }]

        fence = FacadeFence()
        stale = {
            "metadata": {
                "name": "hive-canary", "uid": "uid-7", "resourceVersion": "91",
                "annotations": {
                    "shizuha.io/ring-generation": "generation-7",
                    "shizuha.io/fence-token": "7",
                },
            },
        }
        with mock.patch.object(f.time, "sleep"):
            f.raw_delete(
                fence, "shizuha-hive", "deployment", "hive-canary", stale,
                wait_for_delete=False,
            )
        self.assertEqual(fence.api.patch_attempts, 2)
        self.assertGreaterEqual(fence.validations, 2)
        delete_args, delete_body, _ = fence.api.runs[-1]
        self.assertEqual(delete_args[0], "delete")
        self.assertEqual(json.loads(delete_body)["preconditions"]["resourceVersion"], "93")

    def test_recovery_retries_evidence_without_repeating_rollback(self):
        e, m = self.executor_module, self.fence_module
        predecessor = self.holder(m)
        recovery = self.holder(m, run="100-recovery", job="recover")
        api = self.FakeApi(self.lease(recovery, token=8, phase="rollback-started"))
        calls = []
        call_envs = []

        def fake_run(args, **kwargs):
            calls.append(tuple(str(item) for item in args))
            call_envs.append(kwargs.get("env", {}))
            is_record = any("record-rollout-failure.sh" in str(item) for item in args)
            record_count = sum(
                any("record-rollout-failure.sh" in item for item in call)
                for call in calls
            )
            rc = 1 if is_record and record_count == 1 else 0
            return subprocess.CompletedProcess(args, rc, "", "")

        args = type("Args", (), {"request": "hive-ring-request-fleet-100"})()
        with (
            mock.patch.object(e, "Kubectl", return_value=api),
            mock.patch.object(e, "load_request", return_value=(
                predecessor, "image", {"prior_fence_token": 7, "operation": "release"},
            )),
            mock.patch.object(e.subprocess, "run", side_effect=fake_run),
        ):
            # HIVE-2819 (D2): the first recover's evidence recording fails, but
            # the rollback already converged — recover must NOT mask that with
            # a nonzero exit (old contract returned 1 here). It downgrades the
            # delivery failure to a ::warning::, parks the lease at
            # evidence-pending, and a later recover retries ONLY the evidence.
            self.assertEqual(e.recover(args), 0)
            self.assertEqual(m.FenceState.from_lease(api.lease).phase, "evidence-pending")
            self.assertEqual(e.recover(args), 0)
            self.assertEqual(m.FenceState.from_lease(api.lease).phase, "completed")
        rollback_calls = [call for call in calls if any("ring-recovery.py" in item for item in call)]
        record_calls = [call for call in calls if any("record-rollout-failure.sh" in item for item in call)]
        self.assertEqual(len(rollback_calls), 1)
        self.assertEqual(len(record_calls), 2)
        for call, env in zip(calls, call_envs, strict=True):
            if any("ring-recovery.py" in item or "record-rollout-failure.sh" in item for item in call):
                self.assertTrue(env["PATH"].startswith("/app/.forgejo/scripts:"))
                self.assertEqual(env["RING_KUBECTL_REAL"], "/usr/local/bin/kubectl")

    def test_partial_fleet_failure_restores_backend_and_later_refresh_target(self):
        r, m = self.recovery_module, self.fence_module
        holder = self.holder(m)
        bad_image = f"localhost:30500/shizuha-hive:hive-{SHA[:7]}"
        good_image = f"localhost:30500/shizuha-hive:hive-{OLD_SHA[:7]}"

        def deployment(name, container):
            return {
                "metadata": {
                    "name": name,
                    "resourceVersion": "91",
                    "labels": {"build_sha": SHA, "release_ring": "fleet"},
                    "annotations": {
                        "last-successful-sha": SHA,
                        "shizuha.io/last-successful-sha": SHA,
                        "shizuha.io/ring-generation": holder.generation,
                        "shizuha.io/fence-token": "7",
                    },
                },
                "spec": {"template": {
                    "metadata": {"labels": {"build_sha": SHA, "release_ring": "fleet"}},
                    "spec": {"containers": [{"name": container, "image": bad_image}]},
                }},
            }

        class Api:
            def __init__(self):
                self.deployments = {
                    "hive": deployment("hive", "hive"),
                    "hive-api": deployment("hive-api", "hive-api"),
                    "hive-codex-refresh": deployment("hive-codex-refresh", "codex-refresh"),
                }
                self.patched = []

            def run(inner, args, *, stdin=None, check=True):
                if "get" in args:
                    name = args[args.index("deployment") + 1]
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps(inner.deployments[name]), "",
                    )
                return subprocess.CompletedProcess(args, 0, "", "")

            def json_patch(inner, _namespace, _resource, name, operations):
                inner.patched.append(name)
                target = inner.deployments[name]
                for operation in operations:
                    if operation["op"] == "test":
                        continue
                    parts = [
                        item.replace("~1", "/").replace("~0", "~")
                        for item in operation["path"].split("/")[1:]
                    ]
                    cursor = target
                    for part in parts[:-1]:
                        cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
                    leaf = parts[-1]
                    if isinstance(cursor, list):
                        cursor[int(leaf)] = operation["value"]
                    else:
                        cursor[leaf] = operation["value"]
                return target

        class Fence:
            def __init__(self):
                self.holder = holder
                self.token = 7
                self.api = Api()
                self.validations = 0

            def target_tests(self, target):
                return [{
                    "op": "test", "path": "/metadata/resourceVersion",
                    "value": target["metadata"]["resourceVersion"],
                }]

            def validate(self, *_args):
                self.validations += 1

        fence = Fence()
        with mock.patch.object(
            r.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            r.rollback_fleet(fence)
        self.assertEqual(fence.api.patched, ["hive", "hive-codex-refresh", "hive-api"])
        self.assertEqual(
            fence.api.deployments["hive"]["spec"]["template"]["spec"]["containers"][0]["image"],
            good_image,
        )
        self.assertEqual(
            fence.api.deployments["hive-codex-refresh"]["spec"]["template"]["spec"]["containers"][0]["image"],
            good_image,
        )
        self.assertEqual(
            fence.api.deployments["hive-api"]["spec"]["template"]["spec"]["containers"][0]["image"],
            good_image,
        )
        self.assertGreaterEqual(fence.validations, 2)

    def test_commit_phase_race_does_not_kill_post_commit_work(self):
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m, ring="fleet")

        class CommitWonFence:
            token = 7

            def __init__(self):
                self.holder = holder

            def renew(self):
                raise m.Conflict("resourceVersion race")

            def read(self):
                return m.FenceState(
                    holder=holder.serialized(), token=7, phase="committed",
                    resource_version="9", duration=90,
                )

        child = type("Child", (), {"pid": 12345})()
        stop = type("Stop", (), {"wait": lambda _self, _delay: False})()
        failures = []
        with mock.patch.object(e.os, "killpg") as killpg:
            e._renew_loop(CommitWonFence(), stop, failures, child)
        self.assertEqual(failures, [])
        killpg.assert_not_called()

    def test_recovery_claims_dynamic_targets_with_predecessor_cas(self):
        e, m = self.executor_module, self.fence_module
        predecessor = self.holder(m)
        recovery = self.holder(m, run="100-recovery", job="recover")
        lease = self.lease(recovery, token=8, phase="recovering")
        names = {
            ("shizuha-hive", "job"): f"hive-identity-reconciler-deploy-{SHA[:7]}-123",
            ("monitoring", "probe.monitoring.coreos.com"): f"probe-shizuha-hive-fleet-{SHA[:7]}",
            ("origin", "configmap"): f"hive-ring-evidence-fleet-{SHA[:7]}-3ac86f5844fb",
            ("origin", "job"): f"hive-ring-evidence-fleet-{SHA[:7]}-3ac86f5844fb",
        }

        class Api(self.FakeApi):
            def __init__(self, lease_body):
                super().__init__(lease_body)
                self.target_patches = []

            def run(inner, args, *, stdin=None, check=True):
                namespace = args[args.index("-n") + 1]
                resource = args[args.index("get") + 1]
                key = (namespace, resource)
                if args[-2:] == ["-o", "json"] and len(args) == 6:
                    name = names.get(key)
                    items = [] if name is None else [{"metadata": {"name": name}}]
                    return subprocess.CompletedProcess(args, 0, json.dumps({"items": items}), "")
                name = args[args.index("get") + 2]
                if names.get(key) != name:
                    return subprocess.CompletedProcess(args, 1, "", "Error from server (NotFound)")
                body = {
                    "metadata": {
                        "name": name,
                        "resourceVersion": "44",
                        "annotations": {
                            "shizuha.io/ring-generation": predecessor.generation,
                            "shizuha.io/fence-token": "7",
                        },
                    },
                }
                return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

            def json_patch(inner, namespace, resource, name, operations):
                if resource == "lease":
                    return super(Api, inner).json_patch(namespace, resource, name, operations)
                inner.target_patches.append((namespace, resource, name, operations))
                return {"metadata": {"resourceVersion": "45"}}

        api = Api(lease)
        fence = m.Fence(recovery, api)
        fence.token = 8
        e.claim_existing_targets(
            fence,
            allow_absent=True,
            expected_generation=predecessor.generation,
            expected_token=7,
            discover_dynamic=True,
        )
        self.assertEqual(
            {(namespace, resource, name) for namespace, resource, name, _ in api.target_patches},
            {(namespace, resource, name) for (namespace, resource), name in names.items()},
        )
        for _, _, _, operations in api.target_patches:
            tests = {(item["path"], item["value"]) for item in operations if item["op"] == "test"}
            self.assertIn(("/metadata/annotations/shizuha.io~1ring-generation", predecessor.generation), tests)
            self.assertIn(("/metadata/annotations/shizuha.io~1fence-token", "7"), tests)

    def test_recovery_claim_accepts_partially_claimed_predecessor_tokens(self):
        e, m = self.executor_module, self.fence_module
        recovery = self.holder(m, run="100-recovery", job="recover", ring="canary")
        lease = self.lease(recovery, token=8, phase="recovering")

        class Api(self.FakeApi):
            def __init__(inner, lease_body):
                super().__init__(lease_body)
                inner.targets = {
                    ("deployment", "hive-canary"): {
                        "metadata": {
                            "name": "hive-canary", "uid": "deployment-uid",
                            "resourceVersion": "44",
                            "annotations": {
                                "shizuha.io/ring-generation": "partially-claimed-generation",
                                "shizuha.io/fence-token": "7",
                            },
                        },
                    },
                    ("service", "hive-canary"): {
                        "metadata": {
                            "name": "hive-canary", "uid": "service-uid",
                            "resourceVersion": "45",
                            "annotations": {
                                "shizuha.io/ring-generation": "last-committed-generation",
                                "shizuha.io/fence-token": "6",
                            },
                        },
                    },
                }
                inner.target_patches = []

            def run(inner, args, *, stdin=None, check=True):
                if "-l" in args:
                    return subprocess.CompletedProcess(args, 0, json.dumps({"items": []}), "")
                resource = args[args.index("get") + 1]
                name = args[args.index("get") + 2]
                target = inner.targets.get((resource, name))
                if target is None:
                    return subprocess.CompletedProcess(args, 1, "", "Error from server (NotFound)")
                return subprocess.CompletedProcess(args, 0, json.dumps(target), "")

            def json_patch(inner, namespace, resource, name, operations):
                if resource == "lease":
                    return super(Api, inner).json_patch(namespace, resource, name, operations)
                inner.target_patches.append((resource, name, operations))
                return {"metadata": {"resourceVersion": "46"}}

        api = Api(lease)
        fence = m.Fence(recovery, api)
        fence.token = 8
        e.claim_existing_targets(
            fence, allow_absent=True, expected_token_ceiling=7,
        )
        self.assertEqual(len(api.target_patches), 2)
        tested_tokens = {
            item["value"]
            for _, _, operations in api.target_patches
            for item in operations
            if item["op"] == "test"
            and item["path"] == "/metadata/annotations/shizuha.io~1fence-token"
        }
        self.assertEqual(tested_tokens, {"6", "7"})

        api.targets[("service", "hive-canary")]["metadata"]["annotations"][
            "shizuha.io/fence-token"
        ] = "9"
        with self.assertRaisesRegex(m.Conflict, "newer fence token"):
            e.claim_existing_targets(
                fence, allow_absent=True, expected_token_ceiling=7,
            )

        for target in api.targets.values():
            target["metadata"]["annotations"].update({
                "shizuha.io/ring-generation": recovery.generation,
                "shizuha.io/fence-token": "8",
            })
        api.target_patches.clear()
        e.claim_existing_targets(
            fence, allow_absent=True, expected_token_ceiling=7,
        )
        self.assertEqual(api.target_patches, [])

    def test_recovery_bootstraps_only_the_labelled_legacy_liveness_cronjob(self):
        e, m = self.executor_module, self.fence_module
        recovery = self.holder(m, run="100-recovery", job="recover")
        lease = self.lease(recovery, token=8, phase="recovering")

        class Api(self.FakeApi):
            def __init__(inner, lease_body):
                super().__init__(lease_body)
                inner.patches = []

            def run(inner, args, *, stdin=None, check=True):
                if "-l" in args:
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps({"items": []}), "",
                    )
                resource = args[args.index("get") + 1]
                name = args[args.index("get") + 2]
                if (resource, name) != (
                    "cronjob", "fleet-agent-liveness-reconciler",
                ):
                    return subprocess.CompletedProcess(
                        args, 1, "", "Error from server (NotFound)",
                    )
                body = {
                    "metadata": {
                        "name": name,
                        "uid": "legacy-liveness-uid",
                        "resourceVersion": "44",
                        "labels": {
                            "app": "hive",
                            "app.kubernetes.io/component": "liveness-reconciler",
                        },
                        "annotations": {
                            "kubectl.kubernetes.io/last-applied-configuration": "legacy",
                        },
                    },
                }
                return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

            def json_patch(inner, namespace, resource, name, operations):
                if resource == "lease":
                    return super(Api, inner).json_patch(
                        namespace, resource, name, operations,
                    )
                inner.patches.append((resource, name, operations))
                return {"metadata": {"resourceVersion": "45"}}

        api = Api(lease)
        fence = m.Fence(recovery, api)
        fence.token = 8
        e.claim_existing_targets(
            fence, allow_absent=True, expected_token_ceiling=7,
        )

        self.assertEqual(len(api.patches), 1)
        _, _, operations = api.patches[0]
        tests = [item["path"] for item in operations if item["op"] == "test"]
        self.assertEqual(tests, ["/metadata/resourceVersion"])
        added = {
            item["path"]: item["value"]
            for item in operations
            if item["op"] == "add"
        }
        self.assertEqual(added[
            "/metadata/annotations/shizuha.io~1ring-generation"
        ], recovery.generation)
        self.assertEqual(added[
            "/metadata/annotations/shizuha.io~1fence-token"
        ], "8")

    def test_new_request_recovers_expired_orphan_recovery_owner(self):
        e, m = self.executor_module, self.fence_module
        predecessor = self.holder(m, run="101")
        orphan = self.holder(
            m, run="100-recovery", job="recover", sha="c" * 40,
        )
        api = self.FakeApi(self.lease(orphan, token=18, phase="recovering"))
        observed = m.FenceState.from_lease(api.lease)
        calls = []

        class ExpiredObserver:
            def __init__(inner, _read):
                pass

            def observe(inner):
                return observed, True

        def fake_run(args, **kwargs):
            calls.append(tuple(str(item) for item in args))
            return subprocess.CompletedProcess(args, 0, "", "")

        args = type("Args", (), {"request": "hive-ring-request-fleet-101"})()
        with (
            mock.patch.object(e, "Kubectl", return_value=api),
            mock.patch.object(e, "load_request", return_value=(
                predecessor, "image", {"prior_fence_token": 18, "operation": "release"},
            )),
            mock.patch.object(e, "LeaseObserver", ExpiredObserver),
            mock.patch.object(e, "claim_existing_targets") as claim,
            mock.patch.object(e.subprocess, "run", side_effect=fake_run),
        ):
            self.assertEqual(e.recover(args), 0)

        final = m.FenceState.from_lease(api.lease)
        final_holder = m.Holder.from_dict(json.loads(final.holder))
        self.assertEqual(final.phase, "completed")
        self.assertEqual(final.token, 19)
        self.assertEqual(final_holder.release_sha, "c" * 40)
        claim.assert_called_once()
        self.assertEqual(claim.call_args.kwargs["expected_token_ceiling"], 18)
        record = next(call for call in calls if any("record-rollout-failure.sh" in item for item in call))
        self.assertIn("c" * 40, record)

    def test_target_claim_retries_only_after_owned_object_resource_version_advances(self):
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m, ring="canary", job="deploy-canary")
        lease = self.lease(holder, token=7, phase="acquiring")

        class Api(self.FakeApi):
            def __init__(inner, lease_body):
                super().__init__(lease_body)
                inner.target = {
                    "metadata": {
                        "name": "hive-canary", "uid": "stable-uid",
                        "resourceVersion": "44",
                        "annotations": {
                            "shizuha.io/ring-generation": "previous-generation",
                            "shizuha.io/fence-token": "6",
                        },
                    },
                }
                inner.target_patches = []

            def run(inner, args, *, stdin=None, check=True):
                if "-l" in args:
                    return subprocess.CompletedProcess(args, 0, json.dumps({"items": []}), "")
                resource = args[args.index("get") + 1]
                name = args[args.index("get") + 2]
                if resource != "deployment" or name != "hive-canary":
                    return subprocess.CompletedProcess(args, 1, "", "Error from server (NotFound)")
                return subprocess.CompletedProcess(args, 0, json.dumps(inner.target), "")

            def json_patch(inner, namespace, resource, name, operations):
                if resource == "lease":
                    return super(Api, inner).json_patch(namespace, resource, name, operations)
                inner.target_patches.append(json.loads(json.dumps(operations)))
                if len(inner.target_patches) == 1:
                    inner.target["metadata"]["resourceVersion"] = "45"
                    raise m.FenceError(
                        "Kubernetes API failure rc=1: The request is invalid: "
                        "the server rejected our request due to an error in our request"
                    )
                inner.target["metadata"]["annotations"].update({
                    "shizuha.io/ring-generation": holder.generation,
                    "shizuha.io/fence-token": "7",
                })
                return json.loads(json.dumps(inner.target))

        api = Api(lease)
        fence = m.Fence(holder, api)
        fence.token = 7
        e.claim_existing_targets(fence, allow_absent=True)
        self.assertEqual(len(api.target_patches), 2)
        self.assertIn(
            {"op": "test", "path": "/metadata/resourceVersion", "value": "45"},
            api.target_patches[1],
        )

    def test_target_claim_does_not_retry_unchanged_invalid_request(self):
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m, ring="canary", job="deploy-canary")
        lease = self.lease(holder, token=7, phase="acquiring")

        class Api(self.FakeApi):
            def __init__(inner, lease_body):
                super().__init__(lease_body)
                inner.patch_count = 0
                inner.target = {
                    "metadata": {
                        "name": "hive-canary", "uid": "stable-uid",
                        "resourceVersion": "44", "annotations": {},
                    },
                }

            def run(inner, args, *, stdin=None, check=True):
                if "-l" in args:
                    return subprocess.CompletedProcess(args, 0, json.dumps({"items": []}), "")
                resource = args[args.index("get") + 1]
                name = args[args.index("get") + 2]
                if resource != "deployment" or name != "hive-canary":
                    return subprocess.CompletedProcess(args, 1, "", "Error from server (NotFound)")
                return subprocess.CompletedProcess(args, 0, json.dumps(inner.target), "")

            def json_patch(inner, namespace, resource, name, operations):
                if resource == "lease":
                    return super(Api, inner).json_patch(namespace, resource, name, operations)
                inner.patch_count += 1
                raise m.FenceError("deterministic invalid patch")

        api = Api(lease)
        fence = m.Fence(holder, api)
        fence.token = 7
        with self.assertRaisesRegex(
            m.FenceError,
            "CAS claim rejected for shizuha-hive/deployment/hive-canary",
        ):
            e.claim_existing_targets(fence, allow_absent=True)
        self.assertEqual(api.patch_count, 1)

    def test_executor_jobs_are_pinned_off_the_runner_plane(self):
        # HIVE-2406: the lease keeper must not share the runner plane's fate —
        # a runner-plane freeze (seat darkness) killed the executor pods, the
        # 90s lease expired, and the recover Job rolled back HEALTHY releases
        # (72db199, 5806714a). Both executor Jobs pin to the stable control
        # -plane node (s1) and tolerate its PLAT-5936 maintenance taint, so
        # seat darkness no longer reaches the renewal loop while a genuinely
        # dead Job still stops renewing (the fail-safe is preserved).
        e, m = self.executor_module, self.fence_module
        for mode in ("execute", "recover"):
            manifest = e._job_manifest(self.holder(m, ring="canary"), "image", mode)
            pod = manifest["spec"]["template"]["spec"]
            self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "s1"})
            self.assertTrue(any(
                t.get("key") == "operator" and t.get("value") == "unschedulable"
                and t.get("effect") == "NoSchedule" for t in pod["tolerations"]
            ))
            self.assertTrue(any(
                t.get("key") == "node.shizuha/workstation" and t.get("value") == "true"
                and t.get("effect") == "NoSchedule" for t in pod["tolerations"]
            ))

    def test_prepull_job_is_pinned_identically_and_tokenless(self):
        # PLAT-8251: the pre-pull warms the executor node's image cache before
        # the executor Jobs exist, so the observation budget is never burned on
        # a first-pull (run 2686: cold s1, zero hive images, 50-min deadline
        # expired mid-pull). It must pin IDENTICALLY to the executor (same
        # nodeSelector + both s1 tolerations), need no API access, run a no-op
        # command, and carry a budget far tighter than the canary observation
        # window so a pull failure fails loud fast.
        e, m = self.executor_module, self.fence_module
        manifest = e._prepull_job_manifest(self.holder(m, ring="canary"), "image")
        self.assertEqual(manifest["kind"], "Job")
        self.assertEqual(manifest["metadata"]["name"], e.job_name(self.holder(m, ring="canary"), e.PREPULL_SUFFIX))
        spec = manifest["spec"]
        self.assertEqual(spec["activeDeadlineSeconds"], e.PREPULL_DEADLINE_SECONDS)
        self.assertLess(spec["activeDeadlineSeconds"], 3000)
        self.assertIn("ttlSecondsAfterFinished", spec)
        pod = spec["template"]["spec"]
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "s1"})
        self.assertTrue(any(
            t.get("key") == "operator" and t.get("value") == "unschedulable"
            and t.get("effect") == "NoSchedule" for t in pod["tolerations"]
        ))
        self.assertTrue(any(
            t.get("key") == "node.shizuha/workstation" and t.get("value") == "true"
            and t.get("effect") == "NoSchedule" for t in pod["tolerations"]
        ))
        self.assertIs(pod["automountServiceAccountToken"], False)
        container = pod["containers"][0]
        self.assertEqual(container["image"], "image")
        self.assertEqual(container["command"], ["/app/.venv/bin/python", "-c", "pass"])

    def test_submit_gates_executor_creation_on_prepull_failure(self):
        # PLAT-8251: a failed pre-pull must fail loud BEFORE the executor Jobs
        # are created — the old shape burned the whole observation budget on a
        # cold pull while the Job was legitimately Active.
        e, m = self.executor_module, self.fence_module
        holder = self.holder(m, ring="canary")
        now = [0.0]
        args = type("Args", (), {
            "run_id": holder.run_id, "run_number": holder.run_number,
            "job": holder.job, "ring": holder.ring,
            "release_sha": holder.release_sha, "restored_sha": holder.restored_sha,
            "kubectl": "kubectl", "event_name": "push", "hold": "false",
            "hive725_phase": "phase1", "source_tree": "d" * 40,
            "operation": "release",
        })()

        class Api:
            def __init__(inner):
                inner.calls = []
                inner.created = set()

            def get_lease(inner, _name):
                return self.lease(holder, phase="active")

            def run(inner, command, *, stdin=None, check=True):
                inner.calls.append(tuple(command))
                if command[0] == "create":
                    body = json.loads(stdin)
                    inner.created.add((body["kind"], body["metadata"]["name"]))
                if "logs" in command:
                    return subprocess.CompletedProcess(command, 0, "PREPULL-DIAGNOSTICS\n", "")
                if "get" in command and "job" in command:
                    name = command[command.index("job") + 1]
                    if name == e.job_name(holder, e.PREPULL_SUFFIX):
                        return subprocess.CompletedProcess(
                            command, 0, json.dumps({"status": {"failed": 1}}), "",
                        )
                return subprocess.CompletedProcess(command, 0, "", "")

        api = Api()
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(e, "Kubectl", return_value=api),
            mock.patch.object(e.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(e.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)),
            mock.patch.object(e.sys, "stdout", stdout),
            mock.patch.object(e.sys, "stderr", stderr),
        ):
            self.assertEqual(e.submit(args), 1)
        self.assertEqual(api.created, {("Job", e.job_name(holder, e.PREPULL_SUFFIX))})
        self.assertNotIn(("Job", e.job_name(holder, "execute")), api.created)
        self.assertNotIn(("Job", e.job_name(holder, "recover")), api.created)
        self.assertIn("pre-pull failed", stderr.getvalue())
        self.assertIn("PREPULL-DIAGNOSTICS", stdout.getvalue())

    def test_submit_creates_prepull_before_executor_jobs(self):
        # PLAT-8251: on the success path the pre-pull Job is the FIRST create,
        # and the executor Jobs exist only after it concluded.
        e, m = self.executor_module, self.fence_module
        holder, args, api, now = self.submit_observation_fixture([{"succeeded": 1}])
        created_order = []
        original_run = api.run
        def recording_run(command, *, stdin=None, check=True):
            if command[0] == "create" and stdin:
                created_order.append(json.loads(stdin)["metadata"]["name"])
            return original_run(command, stdin=stdin, check=check)
        api.run = recording_run
        with (
            mock.patch.object(e, "Kubectl", return_value=api),
            mock.patch.object(e.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(e.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)),
        ):
            self.assertEqual(e.submit(args), 0)
        self.assertEqual(created_order[0], e.job_name(holder, e.PREPULL_SUFFIX))
        self.assertIn(e.job_name(holder, "execute"), created_order)
        self.assertIn(e.job_name(holder, "recover"), created_order)

    def test_recovery_job_budget_includes_executor_expiry_and_rollback(self):
        e, m = self.executor_module, self.fence_module
        for ring, execute_deadline, minimum_recovery in (
            ("canary", 3000, 3000 + 90 + 600),
            ("fleet", 7500, 7500 + 90 + 2100),
        ):
            holder = self.holder(m, ring=ring)
            execute = e._job_manifest(holder, "image", "execute")
            recover = e._job_manifest(holder, "image", "recover")
            self.assertEqual(execute["spec"]["activeDeadlineSeconds"], execute_deadline)
            self.assertGreaterEqual(recover["spec"]["activeDeadlineSeconds"], minimum_recovery)

    def test_runner_is_submit_only_and_is_downgraded_before_r1_r2(self):
        workflow = (ROOT / ".forgejo/workflows/deploy.yml").read_text()
        for marker in (
            "- name: R1 — deploy, soak, rollback fail-loud",
            "- name: R2 — Fleet deploy + verify",
        ):
            segment = workflow.split(marker, 1)[1].split("\n      - name:", 1)[0]
            self.assertIn("ring-executor.py submit", segment)
            self.assertIn("monotonic-release-guard.sh", segment)
            self.assertNotRegex(
                segment, r"kubectl\s+.*\b(apply|patch|set|delete|label|annotate)\b",
            )
        release = "\n".join(
            (SCRIPTS / name).read_text()
            for name in ("ring-release-canary.sh", "ring-release-fleet.sh")
        )
        self.assertNotIn("monotonic-release-guard.sh", release)
        fleet_segment = workflow.split("- name: R2 — Fleet deploy + verify", 1)[1]
        fleet_submit = fleet_segment.index("--ring fleet")
        cleanup_submit = fleet_segment.index("--operation cleanup-canary")
        self.assertLess(fleet_submit, cleanup_submit)
        self.assertIn("--ring canary", fleet_segment[cleanup_submit:])
        cleanup_guard = fleet_segment.index(
            'if [ "$FLEET_RC" -eq 0 ] || [ "$FLEET_RC" -eq 20 ]'
        )
        self.assertLess(cleanup_guard, cleanup_submit)
        self.assertNotIn("hive-canary", (SCRIPTS / "ring-release-fleet.sh").read_text())

    def test_inv_f_commits_fence_before_post_commit_work(self):
        workflow = (ROOT / ".forgejo/workflows/deploy.yml").read_text()
        release = (SCRIPTS / "ring-release-fleet.sh").read_text()
        anchor = release.index("INV-F anchor persistence mismatch")
        commit = release.index("ring-executor.py commit", anchor)
        post_commit = release.index("record-deployment-event.sh", commit)
        self.assertLess(anchor, commit)
        self.assertLess(commit, post_commit)
        self.assertIn('can-i patch deployments.apps -n shizuha-hive)" = no', workflow)
        manifests = list(yaml.safe_load_all(
            (ROOT / "k8s/hive-ring-executor-rbac.yaml").read_text()
        ))
        service_accounts = {
            item["metadata"]["name"] for item in manifests
            if item and item.get("kind") == "ServiceAccount"
        }
        self.assertEqual(service_accounts, {
            "hive-ring-submit", "hive-ring-execute", "hive-ring-recover",
        })

    def test_on_demand_reconciler_resolves_live_hive_image_by_name(self):
        release = (SCRIPTS / "ring-release-fleet.sh").read_text()
        block = release.split("# HIVE-628: the on-demand reconciler cron", 1)[1].split(
            'echo "Installing on-demand fleet reconciler CronJob"', 1
        )[0]
        self.assertIn(
            "containers[?(@.name==\"hive\")].image",
            block,
        )
        self.assertNotIn("containers[0].image", block)


if __name__ == "__main__":
    unittest.main()
