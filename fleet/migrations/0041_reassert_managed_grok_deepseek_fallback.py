from django.db import migrations


TARGET_AGENTS = {'goro', 'hana', 'nagi'}
ENABLED_STATUSES = {'running', 'hibernated'}
GROK_MODELS = {'grok-4.5', 'cortex/grok-4.5', 'cortex/xai/grok-4.5'}
DEEPSEEK_MODEL = 'cortex/DeepSeek-V4-Flash'


def reassert_managed_grok_deepseek_fallback(apps, schema_editor):
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    for agent in FleetAgent.objects.filter(
        agent_username__in=TARGET_AGENTS,
        status__in=ENABLED_STATUSES,
    ).iterator():
        method = str((agent.mcp_config or {}).get('execution_method') or '').strip()
        model = str(agent.model or '').strip()
        if method != 'grok_build' or model.lower() not in GROK_MODELS:
            continue
        chain = [
            {'method': 'grok_build', 'model': model},
            {'method': 'shizuha', 'model': DEEPSEEK_MODEL},
        ]
        pending = dict(agent.pending_config or {})
        pending['model_fallbacks'] = chain
        agent.model_fallbacks = chain
        agent.pending_config = pending
        agent.save(update_fields=['model_fallbacks', 'pending_config', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('claws', '0034_preserve_confirmed_xai_credit_cooldowns'),
        ('fleet', '0040_enable_managed_grok_deepseek_fallback'),
    ]

    operations = [
        migrations.RunPython(
            reassert_managed_grok_deepseek_fallback,
            migrations.RunPython.noop,
        ),
    ]
