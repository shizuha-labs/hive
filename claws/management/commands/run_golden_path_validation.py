"""Run the HIVE-420 live golden-path validation harness."""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand

from claws.golden_path_validation import GoldenPathHarness, HarnessConfig, default_run_id, dump_report


class Command(BaseCommand):
    help = "Run the HIVE-420 golden-path validation harness and emit a structured run record."

    def add_arguments(self, parser):
        parser.add_argument("--run-id", default=default_run_id())
        parser.add_argument("--id-base-url", default="")
        # HIVE-2697: in-cluster internal ID URL for the /api/internal/*
        # teardown deprovision call (network-gated off the public ingress).
        parser.add_argument("--id-internal-base-url", default="")
        parser.add_argument("--hive-base-url", default="")
        parser.add_argument("--validation-runtime-token", default="")
        parser.add_argument("--template-slug", default="tech-company")
        parser.add_argument("--plan-slug", default="team")
        parser.add_argument("--brief-slug", default="f0-coffee-shop", choices=["f0-coffee-shop", "f1-snake", "f2-todo"])
        parser.add_argument("--brief-submit-url", default="")
        parser.add_argument("--execution-status-url", default="")
        parser.add_argument("--artifact-url", default="")
        parser.add_argument("--timeout-seconds", type=float, default=15.0)
        parser.add_argument(
            "--allow-live-side-effects",
            action="store_true",
            help="Actually create/delete test accounts and call live provisioning surfaces. Without this, stages record blocked gaps only.",
        )
        parser.add_argument("--output", default="", help="Optional file path for the JSON run record.")
        parser.add_argument("--compact-json", action="store_true")
        parser.add_argument("--fail-on-red", action="store_true", help="Exit non-zero when any stage fails/blocks.")

    def handle(self, *args, **options):
        config = HarnessConfig(
            run_id=options["run_id"],
            id_base_url=options["id_base_url"],
            id_internal_base_url=options["id_internal_base_url"],
            hive_base_url=options["hive_base_url"],
            validation_runtime_token=options["validation_runtime_token"],
            template_slug=options["template_slug"],
            plan_slug=options["plan_slug"],
            brief_slug=options["brief_slug"],
            timeout_seconds=options["timeout_seconds"],
            allow_live_side_effects=options["allow_live_side_effects"],
            brief_submit_url=options["brief_submit_url"],
            execution_status_url=options["execution_status_url"],
            artifact_url=options["artifact_url"],
        )
        report = GoldenPathHarness(config).run()
        rendered = dump_report(report, indent=None if options["compact_json"] else 2)
        if options["output"]:
            path = Path(options["output"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered + "\n", encoding="utf-8")
        self.stdout.write(rendered)
        if options["fail_on_red"] and not report["green"]:
            raise SystemExit(2)
