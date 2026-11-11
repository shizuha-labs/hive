"""Rewrite stored grok-4.7 pins to GLM-5.3-Flash.

Successor to 0062_deprecate_grok_46. The xai free lane for grok-4.7 is no
longer advertised by the Cortex router (observed 2026-11-10: /v1/models
returns only cortex/GLM-5.3-Flash and cortex/Qwen3.8-27B) and calls fail
with rate_limited even before that, so any leftover pin wedges the agent on
its next turn (live datum: agent-yuma, PLAT-13009 follow-up).

Same contract as 0062: prefix-preserving rewrite at deploy so a leftover pin
does not wait for a random save and does not fall through to an unintended
model. Voice and imagine ids are untouched. Queryset.update skips save() so
hibernated rows stay hibernated. One exception to prefix preservation: the
xai cohort (xai:* and cortex/xai/*) is rewritten wholesale to
cortex/GLM-5.3-Flash, because the xai lane is no longer a catalog listing
(provider unconfigured; HIVE-1937) and an explicit provider prefix would
route to that dead provider and crash-loop at startup.
"""
import re

from django.db import migrations


_GROK_47 = re.compile(r'grok-4\.7(?:-latest)?', re.IGNORECASE)


def _canonicalize(model: str) -> str:
    raw = (model or '').strip()
    if not raw:
        return raw
    lowered = raw.lower()
    if 'grok-voice' in lowered or 'grok-imagine' in lowered:
        return raw
    if _GROK_47.search(raw) is None:
        return raw
    # xai lane gone: not a catalog listing, provider unconfigured (HIVE-1937) — rewrite wholesale.
    if lowered.startswith('xai:') or '/xai/' in lowered:
        return 'cortex/GLM-5.3-Flash'
    return _GROK_47.sub('GLM-5.3-Flash', raw)


def remap_grok_47_pins(apps, schema_editor):
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    for row in FleetAgent.objects.all().iterator():
        new = _canonicalize(row.model or '')
        if new != (row.model or ''):
            FleetAgent.objects.filter(pk=row.pk).update(model=new)


def noop_reverse(apps, schema_editor):
    """Not restored — grok-4.7 free lane is deprecated."""


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0064_seed_critical_team_policy'),
    ]

    operations = [
        migrations.RunPython(remap_grok_47_pins, noop_reverse),
    ]
