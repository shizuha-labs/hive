# PLAT-1056: FleetAgent.shared_workspace — permanent /shared RWX mount flag.
# Data step flags scout + nori (matches ichi's 2026-06-29 live patch) so a
# re-provision reproduces the Scout→Nori /shared handoff from source.

from django.db import migrations, models


def flag_scout_nori(apps, schema_editor):
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    FleetAgent.objects.filter(
        agent_username__in=['scout', 'nori'],
    ).update(shared_workspace=True)


def unflag_scout_nori(apps, schema_editor):
    FleetAgent = apps.get_model('fleet', 'FleetAgent')
    FleetAgent.objects.filter(
        agent_username__in=['scout', 'nori'],
    ).update(shared_workspace=False)


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0010_merge_hive273_hivecredential_grants'),
    ]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='shared_workspace',
            field=models.BooleanField(
                default=False,
                help_text='Mount the fleet-wide agent-shared-workspace RWX PVC at /shared '
                          '(cross-agent file handoff, e.g. Scout→Nori news catalog, PLAT-1056). '
                          'Only for agents that need cross-agent sharing — not the whole fleet.',
            ),
        ),
        migrations.RunPython(flag_scout_nori, unflag_scout_nori),
    ]
