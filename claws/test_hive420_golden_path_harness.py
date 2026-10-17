from unittest import TestCase, mock

from claws.golden_path_validation import GoldenPathHarness, HarnessConfig, StageStatus


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = payload if isinstance(payload, str) else ""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class GoldenPathHarnessTests(TestCase):
    def test_records_pending_signup_as_stage_gap_without_leaking_password(self):
        config = HarnessConfig(
            run_id="gp-test",
            id_base_url="https://id.example.test",
            hive_base_url="https://hive.example.test",
            allow_live_side_effects=True,
        )

        with mock.patch("requests.Session.request", return_value=_Response(201, {
            "user": {"id": 123, "email": "gp-test@qafixture.shizuha.com"},
            "pending_approval": True,
        })) as request:
            report = GoldenPathHarness(config).run()

        register_calls = [
            c for c in request.call_args_list
            if len(c.args) >= 2 and c.args[0] == "POST" and c.args[1].endswith("/api/auth/register/")
        ]
        self.assertTrue(register_calls)
        sent_username = register_calls[0].kwargs["json"]["username"]
        # Regression: username must satisfy the ID register validator
        # (letters, numbers, underscores only) or signup 400s and the whole
        # golden-path cycle goes red (KubeJobFailed on the deploy CronJob).
        self.assertRegex(sent_username, r"^[A-Za-z0-9_]+$")

        stage1 = report["stages"][0]
        self.assertEqual(stage1["status"], StageStatus.FAIL)
        self.assertIn("signup_pending_approval_no_jwt", stage1["gaps"])
        self.assertNotIn("password", str(stage1).lower())
        self.assertEqual(report["outcome"], "red")
        self.assertEqual(report["stages"][1]["blocked_by"], ["signup"])

    def test_records_manual_review_provisioning_gap_and_still_attempts_teardown(self):
        config = HarnessConfig(
            run_id="gp-test",
            id_base_url="https://id.example.test",
            hive_base_url="https://hive.example.test",
            validation_runtime_token="validation-token",
            allow_live_side_effects=True,
        )
        responses = iter([
            _Response(201, {"user": {"id": 321}, "tokens": {"access": "jwt-secret"}}),
            _Response(201, {
                "id": "run-1",
                "status": "manual_review",
                "current_step": "id_org",
                "progress": {"done": 1, "total": 10, "percent": 10},
            }),
            _Response(204, ""),
        ])

        with mock.patch("requests.Session.request", side_effect=lambda *args, **kwargs: next(responses)) as request:
            report = GoldenPathHarness(config).run()

        self.assertEqual(request.call_count, 3)
        stage1, stage2, *_middle, stage6 = report["stages"]
        self.assertEqual(stage1["status"], StageStatus.PASS)
        self.assertEqual(stage2["status"], StageStatus.FAIL)
        self.assertIn("org_provisioning_stops_at_manual_review_before_side_effects", stage2["gaps"])
        self.assertEqual(stage6["status"], StageStatus.PASS)
        teardown_call = request.call_args_list[-1]
        self.assertEqual(teardown_call.args[0], "DELETE")
        self.assertIn("/api/internal/test-tenants/321/deprovision/", teardown_call.args[1])

    def test_expected_saga_park_emits_blocked_saga_wip_and_exits_green(self):
        """HIVE-2697 ruling 1 (reworked HIVE-2715): the by-design
        adapter-pending park at the registry-derived next un-implemented leg
        (`pulse`, after the adapter-backed id_org leg) is loud (stage status +
        evidence + outcome) but NOT red."""
        config = HarnessConfig(
            run_id="gp-test",
            id_base_url="https://id.example.test",
            id_internal_base_url="http://id.internal:8001",
            hive_base_url="https://hive.example.test",
            validation_runtime_token="validation-token",
            allow_live_side_effects=True,
        )
        responses = iter([
            _Response(201, {"user": {"id": 321}, "tokens": {"access": "jwt-secret"}}),
            _Response(201, {
                "id": "run-1",
                "status": "manual_review",
                "current_step": "pulse",
                "status_message": "Waiting for adapter implementation before step pulse.",
                "steps": [
                    {"slug": "validate_request", "status": "done", "message": "", "side_effectful": False},
                    {"slug": "id_org", "status": "done",
                     "message": "Real adapter completed; resource ref verified.",
                     "side_effectful": True},
                    {"slug": "pulse", "status": "manual_review",
                     "message": "Adapter not enabled yet; stopping before external side effects.",
                     "side_effectful": True},
                ],
            }),
            # stage 6: hive-side saga deprovision, then ID user deprovision
            _Response(200, {"id": "run-1", "status": "torn_down"}),
            _Response(200, {"message": "deprovisioned"}),
        ])

        with mock.patch("requests.Session.request", side_effect=lambda *args, **kwargs: next(responses)) as request:
            report = GoldenPathHarness(config).run()

        stage1, stage2, *_middle, stage6 = report["stages"]
        self.assertEqual(stage1["status"], StageStatus.PASS)
        self.assertEqual(stage2["status"], StageStatus.BLOCKED_SAGA_WIP)
        self.assertEqual(stage2["gaps"], [])
        self.assertEqual(stage2["evidence"]["saga_park"]["step"], "pulse")
        self.assertEqual(stage2["evidence"]["saga_park"]["reason_class"], "adapter_pending")
        self.assertEqual(report["outcome"], "expected-blocked-saga-wip")
        self.assertTrue(report["green"])
        self.assertEqual(report["metrics"]["stage_blocked_saga_wip_count"], 1)

        # Ruling 2: teardown closes the saga hive-side AND deletes the
        # qafixture user ID-side (via the INTERNAL url — the public ingress
        # 404s /api/internal/*).
        self.assertEqual(request.call_count, 4)
        saga_call, id_call = request.call_args_list[-2], request.call_args_list[-1]
        self.assertEqual(saga_call.args[0], "POST")
        self.assertIn("/org-provisioning-runs/run-1/deprovision/", saga_call.args[1])
        self.assertEqual(saga_call.kwargs["headers"]["Authorization"], "Bearer jwt-secret")
        self.assertEqual(id_call.args[0], "DELETE")
        self.assertTrue(id_call.args[1].startswith("http://id.internal:8001/"))
        self.assertIn("/api/internal/test-tenants/321/deprovision/", id_call.args[1])
        self.assertEqual(stage6["status"], StageStatus.PASS)
        self.assertEqual(stage6["gaps"], [])

    def test_saga_park_with_unexpected_reason_still_fails(self):
        """Honesty guard: a manual_review park whose reason class is NOT the
        adapter-pending marker is a real failure, not the expected bridge."""
        config = HarnessConfig(
            run_id="gp-test",
            id_base_url="https://id.example.test",
            hive_base_url="https://hive.example.test",
            validation_runtime_token="validation-token",
            allow_live_side_effects=True,
        )
        responses = iter([
            _Response(201, {"user": {"id": 321}, "tokens": {"access": "jwt-secret"}}),
            _Response(201, {
                "id": "run-1",
                "status": "manual_review",
                "current_step": "id_org",
                "steps": [
                    {"slug": "id_org", "status": "manual_review",
                     "message": "ID service returned 503 during org creation.",
                     "side_effectful": True},
                ],
            }),
            _Response(200, {"message": "deprovisioned"}),
            _Response(200, {"message": "deprovisioned"}),
        ])

        with mock.patch("requests.Session.request", side_effect=lambda *args, **kwargs: next(responses)):
            report = GoldenPathHarness(config).run()

        stage2 = report["stages"][1]
        self.assertEqual(stage2["status"], StageStatus.FAIL)
        self.assertIn("org_provisioning_stops_at_manual_review_before_side_effects", stage2["gaps"])
        self.assertEqual(report["outcome"], "red")
        self.assertFalse(report["green"])

    def test_saga_park_at_unexpected_step_still_fails(self):
        """Honesty guard (HIVE-2715 shrink constraint): an adapter-pending park
        at a step that HAS an adapter (not the registry-derived expected park
        step) keeps failing the job — the bridge may only label the NEXT
        un-implemented leg."""
        config = HarnessConfig(
            run_id="gp-test",
            id_base_url="https://id.example.test",
            hive_base_url="https://hive.example.test",
            validation_runtime_token="validation-token",
            allow_live_side_effects=True,
        )
        responses = iter([
            _Response(201, {"user": {"id": 321}, "tokens": {"access": "jwt-secret"}}),
            _Response(201, {
                "id": "run-1",
                "status": "manual_review",
                "current_step": "id_org",
                "steps": [
                    {"slug": "id_org", "status": "manual_review",
                     "message": "Adapter not enabled yet; stopping before external side effects.",
                     "side_effectful": True},
                ],
            }),
            _Response(200, {"message": "deprovisioned"}),
            _Response(200, {"message": "deprovisioned"}),
        ])

        with mock.patch("requests.Session.request", side_effect=lambda *args, **kwargs: next(responses)):
            report = GoldenPathHarness(config).run()

        stage2 = report["stages"][1]
        self.assertEqual(stage2["status"], StageStatus.FAIL)
        self.assertEqual(report["outcome"], "red")

    def test_teardown_failure_on_saga_parked_run_fails_the_stage(self):
        """Contract clause 4: strict teardown — residue (either cleanup leg
        failing) is a real failure, never papered over."""
        config = HarnessConfig(
            run_id="gp-test",
            id_base_url="https://id.example.test",
            hive_base_url="https://hive.example.test",
            validation_runtime_token="validation-token",
            allow_live_side_effects=True,
        )
        responses = iter([
            _Response(201, {"user": {"id": 321}, "tokens": {"access": "jwt-secret"}}),
            _Response(201, {
                "id": "run-1",
                "status": "manual_review",
                "current_step": "pulse",
                "steps": [
                    {"slug": "validate_request", "status": "done", "message": "", "side_effectful": False},
                    {"slug": "id_org", "status": "done",
                     "message": "Real adapter completed; resource ref verified.",
                     "side_effectful": True},
                    {"slug": "pulse", "status": "manual_review",
                     "message": "Adapter not enabled yet; stopping before external side effects.",
                     "side_effectful": True},
                ],
            }),
            # saga deprovision fails, ID deprovision fails
            _Response(404, {"error": "not found"}),
            _Response(404, {"error": "User not found"}),
        ])

        with mock.patch("requests.Session.request", side_effect=lambda *args, **kwargs: next(responses)):
            report = GoldenPathHarness(config).run()

        stage6 = report["stages"][-1]
        self.assertEqual(stage6["status"], StageStatus.FAIL)
        self.assertIn("teardown_saga_deprovision_failed", stage6["gaps"])
        self.assertIn("teardown_deprovision_failed", stage6["gaps"])
        self.assertEqual(report["outcome"], "red")

    def test_side_effect_gate_blocks_live_calls_by_default(self):
        config = HarnessConfig(run_id="gp-test", id_base_url="https://id.example.test")
        with mock.patch("requests.Session.request") as request:
            report = GoldenPathHarness(config).run()
        request.assert_not_called()
        self.assertEqual(report["stages"][0]["status"], StageStatus.BLOCKED)
        self.assertIn("signup_live_side_effects_disabled", report["stages"][0]["gaps"])
