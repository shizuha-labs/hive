from django.db import migrations


ENABLED_STATUSES = {'running', 'hibernated'}
GROK_MODELS = {'grok-4.5', 'cortex/grok-4.5', 'cortex/xai/grok-4.5'}
DEEPSEEK_MODEL = 'cortex/DeepSeek-V4-Flash'


def backfill_all_managed_grok_fallbacks(apps, schema_editor):
    """Apply the managed Grok tail by lane, never by a frozen name allowlist."""
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    for agent in FleetAgent.objects.filter(status__in=ENABLED_STATUSES).iterator():
        method = str((agent.mcp_config or {}).get('execution_method') or '').strip()
        model = str(agent.model or '').strip()
        if method != 'grok_build' or model.lower() not in GROK_MODELS:
            continue
        chain = [
            {'method': 'grok_build', 'model': model},
            {'method': 'shizuha', 'model': DEEPSEEK_MODEL},
        ]
        pending = dict(agent.pending_config or {})
        if agent.model_fallbacks == chain and pending.get('model_fallbacks') == chain:
            continue
        pending['model_fallbacks'] = chain
        agent.model_fallbacks = chain
        agent.pending_config = pending
        agent.save(update_fields=['model_fallbacks', 'pending_config', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0041_reassert_managed_grok_deepseek_fallback'),
    ]

    operations = [
        migrations.RunPython(
            backfill_all_managed_grok_fallbacks,
            migrations.RunPython.noop,
        ),
    ]
