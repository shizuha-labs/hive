"""Rewrite stored grok-4.6 pins to grok-4.7.

FleetAgent.save remaps on the next write. This migration does the same
prefix-preserving rewrite at deploy so a leftover pin does not wait for a
random save, and does not fall through to GLM. Voice and imagine ids are
untouched. Queryset.update skips save() so hibernated rows stay hibernated.
"""
import re

from django.db import migrations


_GROK_46 = re.compile(r'grok-4\.6(?:-latest)?', re.IGNORECASE)


def _canonicalize(model: str) -> str:
    raw = (model or '').strip()
    if not raw:
        return raw
    lowered = raw.lower()
    if 'grok-voice' in lowered or 'grok-imagine' in lowered:
        return raw
    if _GROK_46.search(raw) is None:
        return raw
    return _GROK_46.sub('grok-4.7', raw)


def remap_grok_46_pins(apps, schema_editor):
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    for row in FleetAgent.objects.all().iterator():
        new = _canonicalize(row.model or '')
        if new != (row.model or ''):
            FleetAgent.objects.filter(pk=row.pk).update(model=new)


def noop_reverse(apps, schema_editor):
    """Not restored — grok-4.6 is deprecated."""


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0061_hiveorgcredentiallease'),
    ]

    operations = [
        migrations.RunPython(remap_grok_46_pins, noop_reverse),
    ]
