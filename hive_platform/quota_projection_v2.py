"""V2 positive-schema quota projection with provider-reported Grok plan names.

Keep the migration-bound V1 implementation immutable. Grok display names are
an explicit enum, so arbitrary provider strings cannot enter stored snapshots.
"""
from typing import Any

from hive_platform.quota_projection_v1 import project_quota_snapshot as _project_v1

QUOTA_SCHEMA_VERSION = 2
_GROK_PLANS = {
    name.casefold(): name for name in (
        'Free', 'X Basic', 'X Premium', 'X Premium+',
        'SuperGrok Lite', 'SuperGrok', 'SuperGrok Heavy',
    )
}


def normalize_grok_plan(value: Any) -> str | None:
    """Only known provider tier names may leave the fetcher or enter storage."""
    return _GROK_PLANS.get(value.strip().casefold()) if isinstance(value, str) and len(value) <= 120 else None


def project_quota_snapshot(value: Any) -> dict[str, Any]:
    out = _project_v1(value)
    if not out:
        return out
    out['quota_schema_version'] = QUOTA_SCHEMA_VERSION
    if out.get('source') == 'grok_xai_usage':
        out['plan'] = normalize_grok_plan(value.get('plan'))
    return out
