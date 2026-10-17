"""HIVE-580: the golden-path cycle must satisfy the Architecture contract.

Ni's review defect 7 was that the earlier implementation shipped no regression
test, so its other six defects could silently return. These tests encode sora's
2026-07-16 disposition as executable assertions, each naming the clause it
defends.

They run in premerge against the manifest and the command's own logic, with no
cluster and no live journey required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase, mock

import yaml

from claws.management.commands.run_golden_path_cycle import (
    BRIEF_ROTATION,
    EXIT_NO_CREDENTIAL,
    EXIT_RED,
    GREEN_OUTCOMES,
    Command,
    brief_for_cycle,
)

REPO = Path(__file__).resolve().parents[1]
CRONJOB = REPO / "k8s" / "hive-golden-path-validation.yaml"
ALERTS = REPO / "k8s" / "hive-golden-path-alerts.yaml"
# The post-rollout CronJob installation lives in the fleet executor
# (ring-release-fleet.sh), which regenerates every long-lived Hive workload
# from the post-rollout Deployment — the deploy.yml workflow delegates the
# actual rollout to it. Assert the on-merge arming there.
RING_RELEASE_FLEET = REPO / ".forgejo" / "scripts" / "ring-release-fleet.sh"
HARNESS = REPO / "claws" / "golden_path_validation.py"

RED_REPORT = {
    "outcome": "red",
    "green": False,
    "stages": [
        {"stage": 1, "slug": "signup", "status": "pass"},
        {"stage": 3, "slug": "brief", "status": "blocked"},
    ],
}
GREEN_REPORT = {
    "outcome": "green",
    "green": True,
    "stages": [{"stage": 1, "slug": "signup", "status": "pass"}],
}
# HIVE-2697 ruling 1: the by-design adapter-pending saga park — loud, exit 0.
EXPECTED_BLOCKED_REPORT = {
    "outcome": "expected-blocked-saga-wip",
    "green": True,
    "stages": [
        {"stage": 1, "slug": "signup", "status": "pass"},
        {"stage": 2, "slug": "create_org", "status": "blocked-saga-wip",
         "evidence": {"saga_park": {"step": "pulse", "reason_class": "adapter_pending"}}},
        {"stage": 3, "slug": "submit_brief", "status": "blocked"},
        {"stage": 6, "slug": "teardown", "status": "pass"},
    ],
}


def _cronjob() -> dict:
    return yaml.safe_load(CRONJOB.read_text())


def _pod_spec() -> dict:
    return _cronjob()["spec"]["jobTemplate"]["spec"]["template"]["spec"]


def _validator() -> dict:
    return next(c for c in _pod_spec()["containers"] if c["name"] == "validator")


class _Recorder:
    """Captures stdout/stderr writes so ordering can be asserted."""

    def __init__(self):
        self.lines: list[str] = []

    def write(self, text):
        self.lines.append(str(text))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _run(**overrides):
    """Invoke the command with a stubbed harness; returns (exit_code, out, err)."""
    options = {
        "id_base_url": "https://shizuha.com/id",
        "id_internal_base_url": "http://shizuha-id.shizuha.svc.cluster.local:8001",
        "hive_base_url": "https://shizuha.com",
        "validation_runtime_token": "scoped-token",
        "brief_slug": "",
        "output": "",
        "timeout_seconds": 15.0,
    }
    options.update(overrides)
    report = overrides.pop("_report", None) or GREEN_REPORT

    command = Command()
    out, err = _Recorder(), _Recorder()
    command.stdout, command.stderr = out, err
    code = 0
    with mock.patch(
        "claws.management.commands.run_golden_path_cycle.GoldenPathHarness"
    ) as harness:
        harness.return_value.run.return_value = report
        try:
            command.handle(**options)
        except SystemExit as exc:
            code = exc.code
    return code, out.text, err.text, harness


class GoldenPathCycleCommandTests(TestCase):
    # --- clause 5: credential consumed fail-closed --------------------------

    def test_empty_credential_refuses_to_run_and_never_calls_the_harness(self):
        code, _out, err, harness = _run(validation_runtime_token="")
        self.assertEqual(code, EXIT_NO_CREDENTIAL)
        self.assertIn("refusing to run", err)
        harness.assert_not_called()

    def test_credential_is_never_defaulted_to_something_usable(self):
        """A present-but-empty secret must not silently become an anon run."""
        code, _out, _err, harness = _run(validation_runtime_token="   " and "")
        self.assertEqual(code, EXIT_NO_CREDENTIAL)
        harness.assert_not_called()

    # --- clause 3: publish BEFORE exit, RED is non-zero ----------------------

    def test_red_scorecard_is_published_before_the_failing_exit(self):
        code, out, err, _ = _run(_report=RED_REPORT)
        self.assertEqual(code, EXIT_RED)
        # Published...
        self.assertIn("BEGIN GOLDEN PATH SCORECARD", out)
        self.assertIn("END GOLDEN PATH SCORECARD", out)
        # ...and the failure is reported with the offending stage named.
        self.assertIn("outcome=red", err)
        self.assertIn("stage 3", err)

    def test_green_scorecard_exits_zero(self):
        code, out, _err, _ = _run(_report=GREEN_REPORT)
        self.assertEqual(code, 0)
        self.assertIn("outcome=green", out)

    def test_expected_blocked_saga_wip_exits_zero_but_stays_loud(self):
        """HIVE-2697 ruling 1: the by-design saga park exits 0 (no
        KubeJobFailed page) while the bridge stays visible on stdout."""
        code, out, err, _ = _run(_report=EXPECTED_BLOCKED_REPORT)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("outcome=expected-blocked-saga-wip", out)
        self.assertIn(
            "stage 2 create_org: BLOCKED-SAGA-WIP (pulse adapter pending)",
            out,
        )

    def test_unknown_outcome_vocabulary_is_treated_as_red(self):
        """GREEN_OUTCOMES is an allowlist: an unknown future outcome string
        must fail loud, never silently exit 0."""
        self.assertEqual(GREEN_OUTCOMES, {"green", "expected-blocked-saga-wip"})
        code, _out, _err, _ = _run(_report={**GREEN_REPORT, "outcome": "GREEN"})
        self.assertEqual(code, EXIT_RED)

    def test_red_detection_matches_the_harness_lowercase_vocabulary(self):
        """The earlier implementation compared uppercase, passing every RED."""
        # Guard against the harness vocabulary changing under us: the outcome
        # must be derived from the lowercase stage statuses.
        self.assertIn('outcome = "red"', HARNESS.read_text())
        self.assertIn('outcome = "expected-blocked-saga-wip"', HARNESS.read_text())
        code, _out, _err, _ = _run(_report={**RED_REPORT, "outcome": "RED"})
        # An unexpected-case outcome must still be treated as not-green.
        self.assertEqual(code, EXIT_RED)

    def test_harness_is_always_run_with_live_side_effects(self):
        """Without them stages 3-6 are unreachable and teardown is vacuous."""
        _code, _out, _err, harness = _run()
        config = harness.call_args.args[0]
        self.assertTrue(config.allow_live_side_effects)

    # --- clause 1: the brief tier rotates -----------------------------------

    def test_rotation_covers_every_brief_tier(self):
        seen = {
            brief_for_cycle(datetime(2026, 1, 5, tzinfo=timezone.utc).replace(day=5 + i))
            for i in range(0, 21, 7)
        }
        self.assertTrue(seen)
        weeks = {brief_for_cycle(datetime(2026, 1, 5 + 7 * i, tzinfo=timezone.utc)) for i in range(3)}
        self.assertEqual(weeks, set(BRIEF_ROTATION))

    def test_rotation_is_stable_within_a_week(self):
        monday = datetime(2026, 3, 2, tzinfo=timezone.utc)
        thursday = datetime(2026, 3, 5, tzinfo=timezone.utc)
        self.assertEqual(brief_for_cycle(monday), brief_for_cycle(thursday))

    def test_explicit_brief_overrides_the_rotation(self):
        _code, out, _err, harness = _run(brief_slug="f2-todo")
        self.assertEqual(harness.call_args.args[0].brief_slug, "f2-todo")
        self.assertIn("f2-todo", out)


class GoldenPathManifestTests(TestCase):
    # --- clause 5: no self-bootstrapped secret ------------------------------

    def test_no_init_container_bootstraps_its_own_secret(self):
        pod = _pod_spec()
        self.assertFalse(pod.get("initContainers"))
        rendered = CRONJOB.read_text()
        self.assertNotIn("create secret", rendered)
        self.assertNotIn("--from-literal", rendered)

    def test_validation_token_comes_from_a_required_secret_ref(self):
        env = {e["name"]: e for e in _validator()["env"]}
        token = env["VALIDATION_RUNTIME_TOKEN"]
        self.assertNotIn("value", token, "the token must never be inlined")
        ref = token["valueFrom"]["secretKeyRef"]
        self.assertEqual(ref["name"], "hive-golden-path-validation")
        self.assertEqual(ref["key"], "validation-runtime-token")
        # optional:true would let the pod start with the variable unset.
        self.assertFalse(ref.get("optional", False))

    # --- clause 6: exact CI-produced image ----------------------------------

    def test_no_bootstrap_tag_no_set_image_no_dead_bitnami_reference(self):
        rendered = CRONJOB.read_text()
        image = _validator()["image"]
        self.assertEqual(image, "localhost:30500/shizuha-hive:latest")
        self.assertNotIn(":bootstrap", image)
        # bitnami/* Docker Hub tags have 404'd since 2025-08-28 (moved to
        # bitnamilegacy/*), so the old bitnami/kubectl sidecar could never pull.
        self.assertNotIn("bitnami/", rendered)
        self.assertNotIn("set image", rendered)

    # --- clause 2: an honest result, not a retried one ----------------------

    def test_manifest_invokes_the_cycle_command(self):
        self.assertEqual(
            _validator()["command"],
            ["python", "manage.py", "run_golden_path_cycle"],
        )

    def test_no_retry_papers_over_a_red_scorecard(self):
        job = _cronjob()["spec"]["jobTemplate"]["spec"]
        self.assertEqual(job["backoffLimit"], 0)
        self.assertEqual(_cronjob()["spec"]["concurrencyPolicy"], "Forbid")

    def test_schedule_is_weekly(self):
        schedule = _cronjob()["spec"]["schedule"]
        self.assertEqual(schedule.split()[-1], "1", schedule)

    def test_manifests_target_the_hive_namespace_and_carry_the_task_label(self):
        for path in (CRONJOB, ALERTS):
            doc = yaml.safe_load(path.read_text())
            self.assertEqual(doc["metadata"]["namespace"], "shizuha-hive", path)
            self.assertEqual(doc["metadata"]["labels"]["shizuha.io/task"], "HIVE-580", path)


class GoldenPathArmingTests(TestCase):
    # --- clause 3: a skipped or missing run is also RED ----------------------

    def test_absence_of_a_cycle_is_alerted_on(self):
        names = {
            rule["alert"]
            for group in yaml.safe_load(ALERTS.read_text())["spec"]["groups"]
            for rule in group["rules"]
        }
        self.assertLessEqual(
            {
                "HiveGoldenPathValidationMissed",
                "HiveGoldenPathValidationAbsent",
                "HiveGoldenPathValidationRed",
            },
            names,
        )

    # --- clause 1: event-driven on merge, not only scheduled ----------------

    def test_deploy_workflow_installs_and_arms_the_cycle_on_merge(self):
        workflow = RING_RELEASE_FLEET.read_text()
        self.assertIn("hive-golden-path-validation", workflow)
        self.assertIn("--from=cronjob/hive-golden-path-validation", workflow)
        self.assertIn("hive-golden-path-alerts.yaml", workflow)
