"""Bill reserved Hive fleet CPU/RAM in Hane (hourly).

Own-fleet Cortex *inference* is not a trade. Hive *compute* is: the owner
pays Hane for the box their agents reserve. Hibernated/stopped seats are free.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

import requests
from django.conf import settings
from django.utils import timezone

from .models import FleetAgent
from .provisioner import DEFAULT_RESOURCE_PROFILES

logger = logging.getLogger(__name__)

HANE_PER_CPU_HOUR = Decimal(os.environ.get('HIVE_HANE_PER_CPU_HOUR', '1'))
HANE_PER_GIB_HOUR = Decimal(os.environ.get('HIVE_HANE_PER_GIB_HOUR', '0.5'))
DEFAULT_PAYER_USER_ID = int(os.environ.get('HIVE_COMPUTE_BILLING_USER_ID', '3') or 3)
BILLABLE_STATUSES = frozenset({FleetAgent.Status.RUNNING})
KIND = 'hive_compute'


def parse_cpu_cores(value) -> Decimal:
    raw = str(value or '').strip().lower()
    if not raw:
        return Decimal('0')
    if raw.endswith('m'):
        return Decimal(raw[:-1] or '0') / Decimal('1000')
    return Decimal(raw)


def parse_memory_gib(value) -> Decimal:
    raw = str(value or '').strip()
    if not raw:
        return Decimal('0')
    suffixes = (
        ('Ti', Decimal('1024')), ('T', Decimal('1000')),
        ('Gi', Decimal('1')), ('G', Decimal('1')),
        ('Mi', Decimal('1') / Decimal('1024')), ('M', Decimal('1') / Decimal('1000')),
        ('Ki', Decimal('1') / Decimal('1048576')), ('K', Decimal('1') / Decimal('1000000')),
    )
    for suffix, mul in suffixes:
        if raw.endswith(suffix):
            return Decimal(raw[:-len(suffix)] or '0') * mul
    return Decimal(raw)


def resources_for(agent: FleetAgent) -> tuple[Decimal, Decimal]:
    profiles = getattr(settings, 'FLEET_RESOURCE_PROFILES', None) or DEFAULT_RESOURCE_PROFILES
    spec = profiles.get(agent.resource_profile) or profiles.get('standard') or DEFAULT_RESOURCE_PROFILES['standard']
    requests = dict((spec.get('requests') or {}))
    overrides = (agent.resource_overrides or {}).get('requests') or {}
    requests.update({k: v for k, v in overrides.items() if v})
    return parse_cpu_cores(requests.get('cpu')), parse_memory_gib(requests.get('memory'))


def payer_user_id(agent: FleetAgent) -> int:
    owner = int(getattr(agent, 'owner_id', None) or 0)
    if owner > 0:
        return owner
    return int(DEFAULT_PAYER_USER_ID)


def micros_for(cpu_cores: Decimal, memory_gib: Decimal, hours: Decimal = Decimal('1')) -> int:
    hane = (cpu_cores * HANE_PER_CPU_HOUR + memory_gib * HANE_PER_GIB_HOUR) * hours
    return int((hane * Decimal('1000000')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def completed_hours(now: datetime | None = None, *, count: int = 1) -> list[datetime]:
    # Hive Django is USE_TZ=False / America/Chicago. timezone.now() is naive
    # Chicago wall time — tagging that as UTC would bill the wrong clock hour
    # and double-charge once a later UTC run used the real key. Meter hours
    # are always UTC.
    if now is None:
        current = datetime.now(dt_timezone.utc)
    elif timezone.is_naive(now):
        current = now.replace(tzinfo=dt_timezone.utc)
    else:
        current = now.astimezone(dt_timezone.utc)
    end = current.replace(minute=0, second=0, microsecond=0)
    hours = max(1, min(int(count or 1), 168))
    return [end - timedelta(hours=offset) for offset in range(hours, 0, -1)]


def billable_agents() -> Iterable[FleetAgent]:
    return FleetAgent.objects.filter(status__in=BILLABLE_STATUSES).iterator()


def _cortex_spend(*, subject_user_id: int, micros: int, idempotency_key: str, metadata: dict) -> dict:
    base = (getattr(settings, 'CORTEX_BASE_URL', '') or 'http://cortex.shizuha-cortex.svc.cluster.local:8040').rstrip('/')
    token = (
        getattr(settings, 'HIVE_CORTEX_LEASE_API_KEY', '')
        or os.environ.get('HIVE_CORTEX_LEASE_API_KEY', '')
        or ''
    ).strip()
    if not token:
        raise RuntimeError('HIVE_CORTEX_LEASE_API_KEY is not set')
    response = requests.post(
        f'{base}/v1/internal/hane/spend',
        json={
            'user_id': subject_user_id,
            'micros': micros,
            'idempotency_key': idempotency_key,
            'metadata': metadata,
            'label': metadata.get('label') or f"Hive compute · {metadata.get('agent')}",
        },
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        timeout=15,
    )
    if response.status_code not in (200, 402):
        raise RuntimeError(f'Cortex Hane spend HTTP {response.status_code}: {response.text[:300]}')
    return response.json() if response.content else {}


def bill_hour(hour_start: datetime, *, dry_run: bool = False) -> dict:
    hour_key = hour_start.astimezone(dt_timezone.utc).strftime('%Y%m%d%H')
    summary = {
        'hour': hour_start.isoformat(),
        'agents': 0,
        'charged_micros': 0,
        'shortfall_micros': 0,
        'skipped': 0,
        'errors': 0,
    }
    for agent in billable_agents():
        cpu, ram = resources_for(agent)
        micros = micros_for(cpu, ram)
        if micros <= 0:
            summary['skipped'] += 1
            continue
        payer = payer_user_id(agent)
        summary['agents'] += 1
        meta = {
            'kind': KIND,
            'agent': agent.agent_username,
            'cpu_cores': str(cpu),
            'memory_gib': str(ram),
            'hour': hour_key,
            'label': f'Hive compute · {agent.agent_username}',
        }
        if dry_run:
            summary['charged_micros'] += micros
            continue
        try:
            result = _cortex_spend(
                subject_user_id=payer,
                micros=micros,
                idempotency_key=f'hive-compute:{agent.id}:{hour_key}',
                metadata=meta,
            )
        except Exception:
            logger.exception('hive compute bill failed agent=%s hour=%s', agent.agent_username, hour_key)
            summary['errors'] += 1
            continue
        summary['charged_micros'] += int(result.get('charged_micros') or 0)
        summary['shortfall_micros'] += int(result.get('shortfall_micros') or 0)
    return summary


def bill_recent_hours(*, hours: int = 1, dry_run: bool = False) -> list[dict]:
    return [bill_hour(start, dry_run=dry_run) for start in completed_hours(count=hours)]
