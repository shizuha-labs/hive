"""
0004 — FleetAgent behavioral fields (HIVE-133)

Adds 7 fields that bring FleetAgent into parity with agents.json:
  context_prompt, personality_traits, model_fallbacks, model_overrides,
  reasoning_effort, env, failover_chain_id
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0003_fleetagent_s1_scheduling'),
    ]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='context_prompt',
            field=models.TextField(
                blank=True,
                help_text='CONTEXT_PROMPT env injected into the agent container (maps to contextPrompt in agents.json).',
            ),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='personality_traits',
            field=models.JSONField(
                default=dict,
                blank=True,
                help_text='Personality configuration dict (personalityTraits in agents.json); injected as PERSONALITY_TRAITS JSON env.',
            ),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='model_fallbacks',
            field=models.JSONField(
                default=list,
                blank=True,
                help_text='Fallback chain list [{model, method, reasoningEffort}] (modelFallbacks); injected as MODEL_FALLBACKS JSON env.',
            ),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='model_overrides',
            field=models.JSONField(
                default=dict,
                blank=True,
                help_text='Per-method model/effort override dict (modelOverrides); injected as MODEL_OVERRIDES JSON env.',
            ),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='reasoning_effort',
            field=models.CharField(
                max_length=50,
                blank=True,
                help_text='REASONING_EFFORT env (e.g. low, medium, high, max); maps to reasoningEffort/thinkingLevel in agents.json.',
            ),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='env',
            field=models.JSONField(
                default=dict,
                blank=True,
                help_text='Per-agent env var overrides injected directly into the agent container (string→string dict).',
            ),
        ),
        migrations.AddField(
            model_name='fleetagent',
            name='failover_chain_id',
            field=models.CharField(
                max_length=100,
                blank=True,
                help_text='FAILOVER_CHAIN_ID env — identifies the failover chain this agent belongs to (failoverChainId in agents.json).',
            ),
        ),
    ]
