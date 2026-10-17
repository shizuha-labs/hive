from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import migrations
from django.utils import timezone
from django.utils.dateparse import parse_datetime


def _future_reset(snapshot, now_utc):
    if str((snapshot or {}).get('source') or '') != 'grok_xai_usage':
        return None
    if str((snapshot or {}).get('confidence') or '') not in {'ok', 'exact'}:
        return None
    measured = []
    for window in (snapshot or {}).get('windows') or []:
        if not isinstance(window, dict):
            continue
        left = window.get('left_percent')
        if left is None and window.get('used_percent') is not None:
            try:
                left = 100.0 - float(window['used_percent'])
            except (TypeError, ValueError):
                left = None
        try:
            left = float(left) if left is not None else None
        except (TypeError, ValueError):
            left = None
        if left is None:
            continue
        try:
            reset = parse_datetime(str(window.get('resets_at') or ''))
        except Exception:
            reset = None
        if reset is not None and timezone.is_naive(reset):
            reset = timezone.make_aware(reset, dt_timezone.utc)
        measured.append((left, reset))
    if not measured or any(left > 0 for left, _reset in measured):
        return None
    future = [reset for _left, reset in measured if reset and reset > now_utc]
    return max(future) if future else None


def _for_storage(value):
    if getattr(settings, 'USE_TZ', False) or timezone.is_naive(value):
        return value
    return timezone.make_naive(value, dt_timezone.utc)


def preserve_confirmed_xai_credit_cooldowns(apps, schema_editor):
    try:
        TokenEntry = apps.get_model('claws', 'TokenEntry')
    except LookupError:
        # ADR-0080 / HIVE-2018: TokenEntry moved to hive_platform. During fresh
        # replay it is still in claws at this point in the graph; when invoked
        # with the current state (after claws/0040) use the hive_platform home.
        TokenEntry = apps.get_model('hive_platform', 'TokenEntry')
    now_utc = datetime.now(dt_timezone.utc)
    now_storage = _for_storage(now_utc)
    for entry in TokenEntry.objects.filter(
        pool__provider='xai',
        active=True,
        agent_leasing_enabled=True,
        deleted_at__isnull=True,
    ).iterator():
        reset = _future_reset(entry.quota_snapshot or {}, now_utc)
        if reset is None:
            continue
        reset_storage = _for_storage(reset)
        fields = []
        if entry.cooldown_until is None or entry.cooldown_until < reset_storage:
            entry.cooldown_until = reset_storage
            fields.append('cooldown_until')
        if entry.last_status != 'rate_limited':
            entry.last_status = 'rate_limited'
            fields.append('last_status')
        if entry.last_rate_limit_at is None:
            entry.last_rate_limit_at = now_storage
            fields.append('last_rate_limit_at')
        if fields:
            entry.save(update_fields=[*fields, 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('claws', '0033_tokenstickyassignment'),
    ]

    operations = [
        migrations.RunPython(
            preserve_confirmed_xai_credit_cooldowns,
            migrations.RunPython.noop,
        ),
    ]
