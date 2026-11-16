"""Emit runtime-hours meters for live fleet agents (PLAT-9124).

For every RUNNING FleetAgent, meter the elapsed interval since the per-agent
checkpoint (transactional: checkpoint advance + outbox insert commit
together). The payer is server-derived via the accounts:resolve pattern —
the platform org's default BillingAccount; never client-chosen.

Run periodically (cadence bound: one event meters at most 1.0 hour, so a
stalled emitter catches up in bounded increments):
    python manage.py emit_runtime_hours_meters
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from claws.metering_core import MeteringCoreError, resolve_default_account
from fleet.models import FleetAgent
from fleet.runtime_metering.events import enqueue_runtime_hours

logger = logging.getLogger(__name__)

# The internal fleet's platform org (single-org deployment reality; the same
# default the codebase resolves elsewhere for Hive-managed platform agents).
PLATFORM_ORG_ID = 'shizuha'


class Command(BaseCommand):
    help = 'Emit io.shizuha.usage.measured.v1 runtime-hours meters for live fleet agents'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=500)

    def handle(self, *args, **options):
        now = timezone.now()
        account = resolve_default_account(
            account_type='organization', owner_org_id=PLATFORM_ORG_ID,
        )
        agents = (
            FleetAgent.objects.filter(status=FleetAgent.Status.RUNNING)
            .order_by('pk')[: max(options['limit'], 1)]
        )
        emitted = 0
        skipped = 0
        for agent in agents:
            try:
                row = enqueue_runtime_hours(agent=agent, subject=str(account.id), now=now)
            except MeteringCoreError as exc:
                logger.warning('runtime metering: account resolve failed for %s: %s', agent.pk, exc)
                skipped += 1
                continue
            if row is not None:
                emitted += 1
            else:
                skipped += 1
        self.stdout.write(f'emitted={emitted} skipped={skipped}')
