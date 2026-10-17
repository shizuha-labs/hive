"""Remap retired Vision-Metal / Qwen3.8-Q2 pins onto GLM-5.3-Flash.

Hive SoT already converted the live fleet, but FleetAgent.save() only remaps
when a row is written. A leftover Metal pin would keep SCLI POSTing the
deprecated ds4 slot. Rewrite at migrate so persist matches the remap table.
Does not call k8s/Cortex — occupancy drop is the PATCH/lease-tick path.
"""
from django.db import migrations


_RETIRED = {
    'cortex/qwen3.8-27b-q2': 'cortex/GLM-5.3-Flash',
    'qwen3.8-27b-q2': 'cortex/GLM-5.3-Flash',
    'vllm/qwen3.8-27b-q2': 'cortex/GLM-5.3-Flash',
    'cortex/deepseek-v4-flash-vision-metal': 'cortex/GLM-5.3-Flash',
    'deepseek-v4-flash-vision-metal': 'cortex/GLM-5.3-Flash',
    'vllm/deepseek-v4-flash-vision-metal': 'cortex/GLM-5.3-Flash',
}
_SUFFIXES = ('qwen3.8-27b-q2', 'deepseek-v4-flash-vision-metal')


def _remap(model: str) -> str:
    raw = (model or '').strip()
    if not raw:
        return raw
    lowered = raw.lower()
    if lowered in _RETIRED:
        return _RETIRED[lowered]
    for suffix in _SUFFIXES:
        if lowered == suffix or lowered.endswith('/' + suffix) or lowered.endswith(':' + suffix):
            return 'cortex/GLM-5.3-Flash'
    return raw


def remap_retired_pins(apps, schema_editor):
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    for row in FleetAgent.objects.all().iterator():
        new = _remap(row.model)
        if new != (row.model or ''):
            row.model = new
            row.save(update_fields=['model'])


def noop_reverse(apps, schema_editor):
    """Not restored — Metal is deprecated."""


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0058_merge_0057_fleetagentactivitysnapshot_0057_merge_0055_0056'),
    ]

    operations = [
        migrations.RunPython(remap_retired_pins, noop_reverse),
    ]
