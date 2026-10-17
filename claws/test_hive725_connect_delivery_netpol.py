import re
from pathlib import Path

import yaml
from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]


class ConnectDeliveryNetworkPolicyManifestTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.policies = {
            doc["metadata"]["name"]: doc
            for doc in yaml.safe_load_all(
                (ROOT / "k8s/shizuha-hive-netpol.yaml").read_text()
            )
            if doc and doc.get("kind") == "NetworkPolicy"
        }
        cls.service = next(
            doc
            for doc in yaml.safe_load_all(
                (ROOT / "k8s/hive-connect-delivery-boundary.yaml").read_text()
            )
            if doc and doc.get("kind") == "Service"
        )
        cls.boundary_policies = {
            doc["metadata"]["name"]: doc
            for path in (
                ROOT / "k8s/hive-connect-delivery-preflight-netpol.yaml",
                ROOT / "k8s/hive-connect-delivery-boundary.yaml",
            )
            for doc in yaml.safe_load_all(path.read_text())
            if doc and doc.get("kind") == "NetworkPolicy"
        }
        cls.postrollout_policies = {
            doc["metadata"]["name"]: doc
            for doc in yaml.safe_load_all(
                (
                    ROOT
                    / "k8s/hive-connect-delivery-postrollout-netpol.yaml"
                ).read_text()
            )
            if doc and doc.get("kind") == "NetworkPolicy"
        }
        cls.live_backend = next(
            doc
            for doc in yaml.safe_load_all(
                (ROOT / "k8s/hive-backend-live.yaml").read_text()
            )
            if doc and doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "hive"
        )
        cls.deploy_workflow = "\n".join(
            (ROOT / path).read_text()
            for path in (
                ".forgejo/workflows/deploy.yml",
                ".forgejo/scripts/ring-release-canary.sh",
                ".forgejo/scripts/ring-release-fleet.sh",
            )
        )

    def test_dedicated_service_selects_live_hive_backend_and_only_port_8031(self):
        self.assertEqual(self.service["metadata"]["namespace"], "shizuha-hive")
        self.assertEqual(
            self.service["spec"]["selector"],
            {
                "app": "hive",
                "app.kubernetes.io/component": "backend",
            },
        )
        self.assertEqual(
            self.service["spec"]["ports"],
            [
                {
                    "name": "connect-delivery",
                    "protocol": "TCP",
                    "port": 8031,
                    "targetPort": 8031,
                }
            ],
        )

    def test_delivery_policy_requires_exact_connect_namespace_and_backend_labels(self):
        policy = self.policies["allow-connect-delivery-ingress"]
        self.assertEqual(
            policy["spec"]["podSelector"],
            {
                "matchLabels": {
                    "app": "hive",
                    "app.kubernetes.io/component": "backend",
                }
            },
        )
        rule = policy["spec"]["ingress"]
        self.assertEqual(len(rule), 1)
        self.assertEqual(rule[0]["ports"], [{"protocol": "TCP", "port": 8031}])
        self.assertEqual(
            rule[0]["from"],
            [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "shizuha"}
                    },
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/name": "shizuha-connect",
                            "app.kubernetes.io/component": "backend",
                        }
                    },
                }
            ],
        )

    def test_broad_intra_node_and_app_allows_exclude_delivery_port(self):
        for name in (
            "allow-intra-namespace",
            "allow-node-healthchecks",
            "allow-app-service-ports",
        ):
            ports = {
                port["port"]
                for rule in self.policies[name]["spec"]["ingress"]
                for port in rule.get("ports", [])
            }
            self.assertNotIn(8031, ports, name)
            self.assertTrue(ports, f"{name} must be port-scoped")

    def test_atomic_rollout_slice_matches_canonical_policies(self):
        expected = {
            "allow-intra-namespace",
            "allow-node-healthchecks",
            "allow-connect-delivery-ingress",
        }
        self.assertEqual(set(self.boundary_policies), expected)
        for name in expected - {"allow-node-healthchecks"}:
            self.assertEqual(self.boundary_policies[name], self.policies[name], name)
        self.assertEqual(
            self.postrollout_policies,
            {
                "allow-node-healthchecks": self.policies[
                    "allow-node-healthchecks"
                ]
            },
        )
        preflight_sources = self.boundary_policies[
            "allow-node-healthchecks"
        ]["spec"]["ingress"][0]["from"]
        canonical_sources = self.policies[
            "allow-node-healthchecks"
        ]["spec"]["ingress"][0]["from"]
        self.assertEqual(
            preflight_sources,
            canonical_sources + [{"ipBlock": {"cidr": "10.42.0.0/16"}}],
        )

    def test_preflight_narrows_policies_before_dual_port_rollout(self):
        workflow = self.deploy_workflow
        preflight = workflow.index(
            "-f k8s/hive-connect-delivery-preflight-netpol.yaml"
        )
        rollout = workflow.index(
            "kubectl patch -n shizuha-hive deploy/hive --type=merge "
            "--patch-file=/tmp/hive-codex-refresh-deploy-patch.json"
        )
        service = workflow.index(
            "-f k8s/hive-connect-delivery-boundary.yaml"
        )
        postrollout = workflow.index(
            "-f k8s/hive-connect-delivery-postrollout-netpol.yaml"
        )
        self.assertLess(preflight, rollout)
        self.assertLess(rollout, postrollout)
        self.assertLess(postrollout, service)
        self.assertLess(rollout, service)

    def test_live_backend_declares_hmac_ref_listener_and_final_denial(self):
        container = next(
            item
            for item in self.live_backend["spec"]["template"]["spec"]["containers"]
            if item["name"] == "hive"
        )
        self.assertIn(
            {"containerPort": 8031, "name": "connect-delivery"},
            container["ports"],
        )
        env = {item["name"]: item for item in container["env"]}
        self.assertEqual(
            env["HIVE_CONNECT_HMAC_SECRET"]["valueFrom"]["secretKeyRef"],
            {"name": "hive-connect-hmac", "key": "secret"},
        )
        self.assertEqual(
            env["HIVE_CONNECT_ENFORCE_DEDICATED_LISTENER"]["value"], "true"
        )
        for name in ("readinessProbe", "livenessProbe"):
            probe = container[name]
            self.assertNotIn("httpGet", probe)
            command = probe["exec"]["command"]
            self.assertEqual(command[:2], ["python", "-c"])
            self.assertIn("http://127.0.0.1:8030/api/health/", command[2])

    def test_release_patch_forces_backend_probes_to_loopback_exec(self):
        workflow = self.deploy_workflow
        self.assertIn(
            'for probe_name in ("readinessProbe", "livenessProbe")',
            workflow,
        )
        self.assertIn("http://127.0.0.1:8030/api/health/", workflow)
        self.assertIn(
            'probe["exec"] = {"command": probe_command}',
            workflow,
        )

    def test_migration_job_retries_only_transient_startup_failures(self):
        workflow = self.deploy_workflow
        self.assertIn(
            '"labels": {"app": "ci-migrate", "service": "hive"}',
            workflow,
        )
        self.assertIn(
            "/app/.forgejo/scripts/run-migrations-with-retry.sh",
            workflow,
        )
        self.assertIn('"action": "FailJob"', workflow)

    def test_cutover_is_two_explicit_exact_sha_stages(self):
        workflow = self.deploy_workflow
        self.assertIn("hive725_phase:", workflow)
        self.assertIn('REQUESTED_HIVE725_PHASE: ${{ github.event.inputs.hive725_phase', workflow)
        self.assertIn('[ "$H725_MODE" != phase1 ]', workflow)
        self.assertIn('"hive725-phase1-sha=${SHA}"', workflow)
        self.assertIn('"hive725-cutover-complete-sha=${SHA}"', workflow)
        self.assertIn(
            "HIVE-725 phase 1 complete; merge/deploy Connect signer+URL, "
            "then exact-SHA finalize",
            workflow,
        )
        phase1_annotation = workflow.index('"hive725-phase1-sha=${SHA}" --overwrite')
        final_denial = workflow.index(
            "HIVE_CONNECT_ENFORCE_DEDICATED_LISTENER=true",
            phase1_annotation,
        )
        self.assertLess(phase1_annotation, final_denial)
        endpoint_convergence = workflow.index(
            "wait-hive-connect-cutover-convergence.py",
            final_denial,
        )
        legacy_denial = workflow.index(
            "legacy 8030 delivery denied: HTTP 404",
            endpoint_convergence,
        )
        self.assertLess(final_denial, endpoint_convergence)
        self.assertLess(endpoint_convergence, legacy_denial)
        data_plane_guard = workflow[endpoint_convergence:legacy_denial]
        self.assertIn("deadline = time.monotonic() + 180", data_plane_guard)
        self.assertIn("required_consecutive = 6", data_plane_guard)
        self.assertIn('"Connection": "close"', data_plane_guard)
        self.assertIn("?cutover_probe={uuid.uuid4().hex}", data_plane_guard)
        self.assertIn("consecutive >= required_consecutive", data_plane_guard)

    def test_legacy_data_plane_probe_is_syntactically_executable(self):
        fleet_script = (ROOT / ".forgejo/scripts/ring-release-fleet.sh").read_text()
        convergence = fleet_script.index(
            "wait-hive-connect-cutover-convergence.py"
        )
        command = fleet_script.index("-- python -c '", convergence)
        source_start = command + len("-- python -c '")
        source_end = fleet_script.index(
            "\n  '\n  .forgejo/scripts/kubectl -n shizuha-hive annotate",
            source_start,
        )
        source = fleet_script[source_start:source_end]
        compile(source, "hive725-legacy-data-plane-probe", "exec")

    def test_four_pr_merge_order_keeps_delivery_available_at_verifier_boundary(self):
        workflow = self.deploy_workflow
        provisions = [
            index
            for index in range(len(workflow))
            if workflow.startswith(
                ".forgejo/scripts/ensure-hive-connect-hmac.sh shizuha shizuha-hive",
                index,
            )
        ]
        self.assertEqual(len(provisions), 2)
        fleet_provision = provisions[-1]
        patch = workflow.index(
            "kubectl patch -n shizuha-hive deploy/hive --type=merge "
            "--patch-file=/tmp/hive-codex-refresh-deploy-patch.json"
        )
        self.assertLess(fleet_provision, patch)
        self.assertIn(
            'container, "HIVE_CONNECT_HMAC_SECRET", "hive-connect-hmac", "secret",',
            workflow,
        )
        self.assertNotIn(
            'if item.get("name") != "HIVE_CONNECT_HMAC_SECRET"',
            workflow,
        )

        helper = (ROOT / ".forgejo/scripts/ensure-hive-connect-hmac.sh").read_text()
        self.assertIn('secret_name="hive-connect-hmac"', helper)
        self.assertIn('refusing implicit rotation', helper)
        self.assertIn('openssl rand -hex 32', helper)

        # The listener-only head must roll before Connect points at 8031
        # (HIVE-1334 / coordinated hive#54 → deploy#144 → connect#26 → finalize).
        # Connect signer/URL preflight and signed-authority probes run only when
        # H725_MODE != phase1 (steady/finalize) — never gated on the in-tree
        # verifier source marker, which would brick phase1 as soon as the auth
        # PR lands.
        marker = "HIVE613_CONNECT_VERIFIER_CONTRACT = 'signed-owner-bound-v1'"
        self.assertIn(marker, workflow)
        self.assertNotIn(
            '[ "$H725_MODE" != phase1 ] || [ "$H725_VERIFIER_SOURCE" = 1 ]',
            workflow,
        )
        # Preflight + post-roll authority probe both use the phase1-excluding gate
        # (plus the existing phase1-only annotation gate at finalize).
        self.assertGreaterEqual(
            workflow.count('if [ "$H725_MODE" != phase1 ]; then'),
            3,
        )

    def test_finalization_exercises_real_connect_authority_selector(self):
        self.assertIn(
            "dispatch_hive_agent_message(conversation, message, agent)",
            self.deploy_workflow,
        )
        self.assertIn(
            "production Connect dispatcher/authority probe: accepted",
            self.deploy_workflow,
        )
        self.assertIn("transaction.set_rollback(True)", self.deploy_workflow)

    def test_live_probe_is_pinned_to_named_internal_qa_fixture(self):
        workflow = self.deploy_workflow
        self.assertIn(
            'fixture_id = "a2b6cc91-ff1a-4b19-a7ec-20c41738dedc"',
            workflow,
        )
        self.assertIn('owner_id=252', workflow)
        self.assertIn('owner_username="qa-hive-fixture"', workflow)
        self.assertIn('agent_type=Claw.AgentType.CLAW', workflow)
        self.assertIn(
            'named internal QA Claw unavailable for HIVE-725 probe',
            workflow,
        )
        self.assertNotIn(
            'for claw in Claw.objects.exclude(status=Claw.Status.DELETED)',
            workflow,
        )

    def test_rollout_requires_signed_denials_controls_and_phase_local_rollback(self):
        required_contract = (
            "production Connect dispatcher/authority probe: accepted",
            "unsigned delivery denied: HTTP",
            "cross-owner delivery denied: HTTP",
            "dns=ok ordinary_8030=",
            "authorized Connect 8031 TCP control: accepted",
            "dedicated_8031=policy-denied(rc=",
            "legacy 8030 delivery denied: HTTP 404",
            "/tmp/h725-phase1-proven",
            "HIVE_CONNECT_ENFORCE_DEDICATED_LISTENER=false",
        )
        for marker in required_contract:
            self.assertIn(marker, self.deploy_workflow)
        denial_start = self.deploy_workflow.index("def send(payload, *, signed):")
        denial_end = self.deploy_workflow.index(
            "cross-owner delivery denied: HTTP", denial_start
        )
        denial_probe = self.deploy_workflow[denial_start:denial_end]
        self.assertIn("deadline = time.monotonic() + 120", denial_probe)
        self.assertIn("status in {408, 425, 429}", denial_probe)
        self.assertIn("500 <= status <= 599", denial_probe)
        self.assertIn('"Connection": "close"', denial_probe)
        self.assertIn("?release_probe={uuid.uuid4().hex}", denial_probe)
        self.assertIn("timestamp = str(int(time.time()))", denial_probe)

    def test_negative_probe_variables_are_deferred_to_the_probe_container(self):
        start = self.deploy_workflow.index("NEG_JOB=")
        end = self.deploy_workflow.index("\nYAML", start)
        heredoc = self.deploy_workflow[start:end]
        unsafe = re.findall(
            r"(?<!\\)\$(?:\?|control_rc\b|control\b|rc\b|\()",
            heredoc,
        )
        self.assertEqual(
            unsafe,
            [],
            "probe-shell variables must not expand in the set -u release runner",
        )

    def test_l4_denial_accepts_only_proven_cni_rejection_modes(self):
        workflow = self.deploy_workflow
        positive = workflow.index("authorized Connect 8031 TCP control: accepted")
        negative = workflow.index('NEG_JOB="hive725-non-connect-${SHORT}"')
        self.assertLess(positive, negative)
        self.assertIn("socket.create_connection(", workflow)
        self.assertIn("7|28) ;;", workflow)
        self.assertIn(
            "expected policy denial (curl 7 or 28)",
            workflow,
        )
        self.assertNotIn("expected policy timeout (28)", workflow)

    def test_boundary_rollback_tolerates_reconciler_resource_version_changes(self):
        self.assertIn(
            "kubectl apply --server-side --force-conflicts",
            self.deploy_workflow,
        )
        self.assertIn(
            "--field-manager=hive-release-ring-rollback",
            self.deploy_workflow,
        )

    def test_forward_boundary_apply_tolerates_reconciler_resource_version_changes(self):
        for manifest in (
            "k8s/hive-connect-delivery-preflight-netpol.yaml",
            "k8s/hive-connect-delivery-boundary.yaml",
        ):
            apply = self.deploy_workflow.index(f"-f {manifest}")
            prefix = self.deploy_workflow[max(0, apply - 180) : apply]
            self.assertIn("kubectl apply --server-side --force-conflicts", prefix)
            self.assertIn("--field-manager=hive-release-ring", prefix)

    def test_release_authority_is_probed_before_any_fleet_mutation(self):
        authority = self.deploy_workflow.index(
            "- name: Verify Hive boundary release authority"
        )
        fleet_policy = self.deploy_workflow.index(
            "- name: Reconcile fleet agent connectivity policy"
        )
        rollout = self.deploy_workflow.index("- name: R2 — Fleet deploy + verify")
        self.assertLess(authority, fleet_policy)
        self.assertLess(authority, rollout)

        probe = self.deploy_workflow[authority:fleet_policy]
        for policy in (
            "fleet-agent-default-deny",
            "fleet-agent-allow-egress",
            "fleet-agent-allow-platform-ingress",
            "fleet-allow-hostplane-ssh-egress-v2",
        ):
            self.assertIn(policy, probe)
        self.assertIn("for verb in get patch update; do", probe)
        self.assertIn("for verb in create delete; do", probe)
        self.assertIn("networkpolicies.networking.k8s.io", probe)
        self.assertIn("-n shizuha-fleet", probe)
        self.assertNotIn(" list ", probe)
        self.assertNotIn(" watch ", probe)
        self.assertNotIn(
            "--as=system:serviceaccount:origin:origin-ci-deployer", probe
        )
