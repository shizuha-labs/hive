"""HIVE-253 P4: FleetAgent.managing_daemon FK to track which daemon adopted each agent."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0006_daemonregistry_ws_channel'),
    ]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='managing_daemon',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='managed_agents',
                to='fleet.daemonregistry',
                help_text='The fleet daemon that adopted and now manages this agent (P4 — null = direct k8s provisioning).',
            ),
        ),
    ]
