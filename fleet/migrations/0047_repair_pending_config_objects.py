"""Repair and constrain the FleetAgent durable config outbox to JSON objects."""

from django.db import migrations, models

import fleet.pending_config


def repair_pending_config_objects(apps, schema_editor):
    FleetAgent = apps.get_model("fleet", "FleetAgent")
    malformed_ids = [
        row_id
        for row_id, pending_config in (
            FleetAgent.objects.values_list("pk", "pending_config").iterator()
        )
        if not isinstance(pending_config, dict)
    ]
    if malformed_ids:
        # Historical migration models do not run current FleetAgent.save(). A
        # bounded set update deliberately repairs every JSON scalar/array before
        # application code with the strict model boundary starts serving.
        FleetAgent.objects.filter(pk__in=malformed_ids).update(pending_config={})


def noop_reverse(apps, schema_editor):
    """Invalid legacy JSON values are deliberately not resurrected."""


class Migration(migrations.Migration):

    dependencies = [
        ("fleet", "0046_fleetagent_hostplane_ssh"),
    ]

    operations = [
        migrations.RunPython(repair_pending_config_objects, noop_reverse),
        migrations.AlterField(
            model_name="fleetagent",
            name="pending_config",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="HIVE-597: desired-config updates accepted by the API but not yet "
                "confirmed applied by the managing daemon. Written on every config "
                "push (instantly, even when the daemon-link is down), replayed on "
                "daemon reconnect, protected from runtime-delta clobber, and "
                "cleared when the daemon confirms with a config_applied delta.",
                validators=[fleet.pending_config.validate_pending_config_mapping],
            ),
        ),
    ]
