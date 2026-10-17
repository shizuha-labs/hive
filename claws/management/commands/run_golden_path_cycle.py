"""HIVE-580: one scheduled golden-path cycle — rotate, run, publish, then exit.

This wraps the HIVE-420 harness (`run_golden_path_validation`) with the parts
the recurring cycle needs, per sora's 2026-07-16 Architecture disposition:

  * clause 1 — brief tier rotates across cycles so all three tiers get exercised
  * clause 3 — the scorecard is PUBLISHED before the process exits, and only
    then does a RED/BLOCKED outcome return non-zero
  * clause 5 — the scoped validation credential is consumed fail-closed

It exists as a management command rather than as shell inside the CronJob so the
ordering and exit-code rules are unit-testable in premerge, and so the manifest
and the deploy workflow can both invoke one implementation instead of carrying
duplicate copies of a shell blob that would drift apart.

Exit codes:
  0  scorecard is green OR expected-blocked-saga-wip (HIVE-2697 ruling 1: the
     by-design adapter-pending saga park is reported loudly but does not page)
  2  scorecard is RED/BLOCKED (a real result — published first)
  3  the scoped validation credential is missing or empty (refused to run)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand

from claws.golden_path_validation import (
    GoldenPathHarness,
    HarnessConfig,
    StageStatus,
    default_run_id,
    dump_report,
)

# Rotated in this order, one per cycle, so a weekly schedule covers all three
# tiers roughly every three weeks instead of only ever exercising f0.
BRIEF_ROTATION = ("f0-coffee-shop", "f1-snake", "f2-todo")

EXIT_RED = 2
EXIT_NO_CREDENTIAL = 3

# HIVE-2697 ruling 1: outcomes that close the cycle successfully. Anything
# else (including unknown future outcome strings) is treated as red.
GREEN_OUTCOMES = frozenset({"green", "expected-blocked-saga-wip"})


def brief_for_cycle(now: datetime | None = None) -> str:
    """Pick this cycle's brief tier from the ISO week number.

    Deterministic (no stored state, so it survives a recreated CronJob) and
    stable within a week, so a re-run of the same cycle validates the same tier.
    """
    moment = now or datetime.now(timezone.utc)
    return BRIEF_ROTATION[moment.isocalendar().week % len(BRIEF_ROTATION)]


class Command(BaseCommand):
    help = "Run one scheduled golden-path validation cycle and publish its scorecard."

    def add_arguments(self, parser):
        parser.add_argument("--id-base-url", default=os.environ.get("ID_BASE_URL", ""))
        # HIVE-2697: the teardown's /api/internal/* deprovision call is
        # network-gated off the public ingress, so it must target the
        # in-cluster internal ID service URL.
        parser.add_argument(
            "--id-internal-base-url",
            default=os.environ.get("SHIZUHA_ID_INTERNAL_URL", ""),
        )
        parser.add_argument("--hive-base-url", default=os.environ.get("HIVE_BASE_URL", ""))
        parser.add_argument(
            "--validation-runtime-token",
            default=os.environ.get("VALIDATION_RUNTIME_TOKEN", ""),
        )
        parser.add_argument(
            "--brief-slug",
            default="",
            help="Override the rotation (defaults to this ISO week's tier).",
        )
        parser.add_argument("--output", default="/tmp/golden-path-scorecard.json")
        parser.add_argument("--timeout-seconds", type=float, default=15.0)

    def handle(self, *args, **options):
        token = options["validation_runtime_token"]
        if not token:
            # Clause 5, fail-closed. The CronJob's secretKeyRef is required, so a
            # missing Secret already stops the pod; this catches present-but-empty,
            # which would otherwise validate an ANONYMOUS journey and could look
            # green for the wrong reason.
            self.stderr.write(
                "::error::validation-runtime-token is missing or empty — refusing to run.\n"
                "It is provisioned outside this workload; this job never mints its own."
            )
            raise SystemExit(EXIT_NO_CREDENTIAL)

        brief = options["brief_slug"] or brief_for_cycle()
        self.stdout.write(f"golden-path cycle: brief={brief}")

        report = GoldenPathHarness(
            HarnessConfig(
                run_id=default_run_id(),
                id_base_url=options["id_base_url"],
                id_internal_base_url=options["id_internal_base_url"],
                hive_base_url=options["hive_base_url"],
                validation_runtime_token=token,
                brief_slug=brief,
                timeout_seconds=options["timeout_seconds"],
                # Without live side effects stages 3-6 can never be attempted and
                # teardown would be vacuous, so the cycle would prove nothing.
                allow_live_side_effects=True,
            )
        ).run()

        rendered = dump_report(report)

        # Clause 3: publish BEFORE exiting. The harness's own --fail-on-red flag
        # raises SystemExit the instant the JSON is written, which would skip
        # publication entirely on exactly the runs that most need publishing.
        if options["output"]:
            path = Path(options["output"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered + "\n", encoding="utf-8")
        self.stdout.write("----- BEGIN GOLDEN PATH SCORECARD -----")
        self.stdout.write(rendered)
        self.stdout.write("----- END GOLDEN PATH SCORECARD -----")

        # The harness emits LOWERCASE statuses. The earlier implementation
        # compared against uppercase, so every RED run was treated as a pass.
        outcome = report["outcome"]
        if outcome not in GREEN_OUTCOMES:
            failed = [
                f"stage {s['stage']} ({s['slug']}): {s['status']}"
                for s in report["stages"]
                if s["status"] != "pass"
            ]
            self.stderr.write(f"::error::golden path scorecard outcome={outcome} brief={brief}")
            for line in failed:
                self.stderr.write(f"::error::  {line}")
            raise SystemExit(EXIT_RED)

        # HIVE-2697 ruling 1: the bridge must be visible, not silent. A
        # saga-parked stage gets its own loud scorecard line on stdout (not
        # ::error — it is the expected steady state while the id_org adapter
        # is the HIVE-418 epic's unimplemented leg).
        for s in report["stages"]:
            if s["status"] == StageStatus.BLOCKED_SAGA_WIP:
                park = (s.get("evidence") or {}).get("saga_park") or {}
                parked_step = park.get("step") or "unknown-step"
                self.stdout.write(
                    f"stage {s['stage']} {s['slug']}: "
                    f"BLOCKED-SAGA-WIP ({parked_step} adapter pending)"
                )
        self.stdout.write(f"golden path scorecard outcome={outcome} brief={brief}")
