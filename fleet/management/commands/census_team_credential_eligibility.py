"""Fail-loud census for the team-credential-eligibility projection.

PLAT-10906 acceptance: a zero-row ``FleetAgentTeamCredentialEligibility``
projection after the fleet has been alive and populated is never a valid
steady state — it is the bootstrap-gap signature (the writer never fired).
This command is the framework-side tripwire: run on a CronJob, exit non-zero
so the paired PrometheusRule (kube_job_status_failed, the HIVE-580 golden-path
pattern) surfaces RED instead of letting an empty projection stay silent.

Exit codes: 0 = healthy (rows exist, or the fleet is genuinely too young or
too small for the census to be meaningful); 2 = RED (zero rows on an aged,
populated fleet). Anything else is an error.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from fleet.models import (
    FleetAgent,
    FleetAgentTeamCredentialEligibility,
)


class Command(BaseCommand):
    help = (
        'RED (exit 2) when the team-credential-eligibility projection has zero '
        'rows while the fleet is aged and populated — the PLAT-10906 '
        'bootstrap-gap signature.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--min-active-agents', type=int, default=10,
            help='Minimum active FleetAgent count for the census to judge (default 10).',
        )
        parser.add_argument(
            '--bootstrap-age-days', type=int, default=7,
            help='Minimum fleet age in days (oldest FleetAgent row) before judging (default 7).',
        )

    def handle(self, *args, **options):
        min_active = int(options['min_active_agents'])
        age_days = int(options['bootstrap_age_days'])
        if min_active < 1 or age_days < 1:
            self.stderr.write('min-active-agents and bootstrap-age-days must be >= 1')
            raise SystemExit(2)

        now = timezone.now()
        # A "populated" fleet = live seats (running or hibernated policy
        # scale-to-zero). Pending/failed/deleted rows do not evidence activity.
        live_statuses = (
            FleetAgent.Status.RUNNING,
            FleetAgent.Status.HIBERNATED,
        )
        live_agents = FleetAgent.objects.filter(status__in=live_statuses)
        active_count = live_agents.count()
        oldest = live_agents.order_by('created_at').values_list('created_at', flat=True).first()
        fleet_age_days = ((now - oldest).total_seconds() / 86400) if oldest else 0.0
        row_count = FleetAgentTeamCredentialEligibility.objects.count()

        if fleet_age_days < age_days or active_count < min_active:
            self.stdout.write(
                f'census skipped: fleet too young/small for judgment '
                f'(active_agents={active_count} < {min_active} or '
                f'age={fleet_age_days:.1f}d < {age_days}d); '
                f'projection_rows={row_count}'
            )
            return

        if row_count > 0:
            self.stdout.write(
                f'census GREEN: projection_rows={row_count} '
                f'(active_agents={active_count}, fleet_age={fleet_age_days:.1f}d)'
            )
            return

        self.stderr.write(
            'census RED: fleet_fleetagentteamcredentialeligibility has ZERO rows '
            f'on an aged, populated fleet (active_agents={active_count}, '
            f'fleet_age={fleet_age_days:.1f}d >= {age_days}d). The projection '
            'writer has never fired — the PLAT-10906 bootstrap-gap signature. '
            'Provision sanctioned seats with '
            '`manage.py grant_team_credential_eligibility` (bounded exact-seat '
            'writer); do NOT hand-insert rows and do NOT run the whole-roster '
            'reconcile as a fix.'
        )
        raise SystemExit(2)
