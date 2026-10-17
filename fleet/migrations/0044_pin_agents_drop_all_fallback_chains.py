"""Every agent is pinned to one backend — clear all persisted fallback chains.

Operator directive 2026-08-04: "perhaps there should be zero concept of a
'fallback' .. the agent needs to be pinned to a backend and that's it .. to make
the life simpler".

This reverses 0041_reassert_managed_grok_deepseek_fallback and
0042_backfill_all_managed_grok_fallbacks, which wrote the
`grok primary -> cortex/DeepSeek-V4-Flash` tail onto Grok Build agents.
`managed_model_fallbacks` now returns [] for every lane, so any row still
carrying a chain is stale data that would keep the pod's inline failover loop
alive with a second step.

FleetAgent.save() recomputes model_fallbacks at the model chokepoint, but rows
are only rewritten when something touches them — a pinned-and-idle agent could
keep its chain indefinitely. Clear them explicitly.
"""
from django.db import migrations


def clear_fallback_chains(apps, schema_editor):
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    # .update() deliberately bypasses save(): this is a data correction, and we
    # do not want per-row lane re-derivation or config-push side effects firing
    # for every agent during a migration.
    FleetAgent.objects.exclude(model_fallbacks=[]).update(model_fallbacks=[], failover_chain_id='')


def noop_reverse(apps, schema_editor):
    """Deliberately not restored.

    Re-adding chains is a policy decision, not a schema one — it would have to
    come back through managed_model_fallbacks, not by resurrecting rows.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0043_runtimechange_superseded_state'),
    ]

    operations = [
        migrations.RunPython(clear_fallback_chains, noop_reverse),
    ]
