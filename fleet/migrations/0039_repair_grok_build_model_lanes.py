from django.db import migrations


GROK_MODELS = {
    'grok-4.5',
    'cortex/grok-4.5',
    'cortex/xai/grok-4.5',
}


def repair_grok_build_model_lanes(apps, schema_editor):
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    for agent in FleetAgent.objects.iterator():
        method = str((agent.mcp_config or {}).get('execution_method') or '').strip()
        if method != 'grok_build' or str(agent.model or '').strip().lower() in GROK_MODELS:
            continue
        agent.model = 'cortex/grok-4.5'
        pending = dict(agent.pending_config or {})
        if pending.get('model'):
            pending['model'] = 'cortex/grok-4.5'
            agent.pending_config = pending
            agent.save(update_fields=['model', 'pending_config', 'updated_at'])
        else:
            agent.save(update_fields=['model', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0038_repair_hibernation_rollout_race'),
    ]

    operations = [
        migrations.RunPython(repair_grok_build_model_lanes, migrations.RunPython.noop),
    ]
