from django.db import migrations


TARGET_AGENTS = {'goro', 'hana', 'nagi'}
GROK_MODELS = {'grok-4.5', 'cortex/grok-4.5', 'cortex/xai/grok-4.5'}
DEEPSEEK_MODEL = 'cortex/DeepSeek-V4-Flash'


def enable_managed_grok_deepseek_fallback(apps, schema_editor):
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    for agent in FleetAgent.objects.filter(
        agent_username__in=TARGET_AGENTS,
        status='running',
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


def disable_managed_grok_deepseek_fallback(apps, schema_editor):
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    for agent in FleetAgent.objects.filter(agent_username__in=TARGET_AGENTS).iterator():
        model = str(agent.model or '').strip()
        chain = [
            {'method': 'grok_build', 'model': model},
            {'method': 'shizuha', 'model': DEEPSEEK_MODEL},
        ]
        if agent.model_fallbacks != chain:
            continue
        pending = dict(agent.pending_config or {})
        # Queue an explicit empty list so rollback reaches the daemon too; merely
        # removing the outbox key would clear Hive state but leave an already-
        # applied runtime chain behind.
        pending['model_fallbacks'] = []
        agent.model_fallbacks = []
        agent.pending_config = pending
        agent.save(update_fields=['model_fallbacks', 'pending_config', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0039_repair_grok_build_model_lanes'),
    ]

    operations = [
        migrations.RunPython(
            enable_managed_grok_deepseek_fallback,
            disable_managed_grok_deepseek_fallback,
        ),
    ]
