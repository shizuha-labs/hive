"""HIVE-235 P1 fix: add ws_channel to DaemonRegistry for discriminated-disconnect guard."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0005_daemoncredential_daemonregistry'),
    ]

    operations = [
        migrations.AddField(
            model_name='daemonregistry',
            name='ws_channel',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Channel name of the current active WS connection (for disconnect guard).',
                max_length=256,
            ),
        ),
    ]
