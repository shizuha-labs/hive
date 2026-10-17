from django.db import migrations


LEGACY_HIBERNATION_REASON = "Legacy auto-resumable scale-to-zero state"


def repair_legacy_hibernation_rollout_race(apps, schema_editor):
    """Restore rows collapsed by an old consumer during the 0037 rollout.

    Migration 0037 intentionally made every legacy stopped row auto-resumable.
    Its additive schema/data step ran before the new application pods rolled.
    In that window, an old rt-fleet observation consumer could persist
    ``status='stopped'`` again without knowing about the two new metadata
    fields.  The exact reason marker and non-null timestamp uniquely identify
    those rows; explicit post-migration operator stops clear both fields.
    """
    FleetAgent = apps.get_model("fleet", "FleetAgent")
    FleetAgent.objects.filter(
        status="stopped",
        hibernation_reason=LEGACY_HIBERNATION_REASON,
        hibernated_at__isnull=False,
    ).update(status="hibernated")


def noop_reverse(apps, schema_editor):
    # Reversing the repair must not collapse auto-resumable intent again.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("fleet", "0037_agent_hibernation_state"),
    ]

    operations = [
        migrations.RunPython(
            repair_legacy_hibernation_rollout_race,
            noop_reverse,
        ),
    ]
