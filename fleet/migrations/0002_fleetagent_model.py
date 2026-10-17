from django.db import migrations, models


class Migration(migrations.Migration):
    """HIVE-70: add model field to FleetAgent for MODEL env var injection."""

    dependencies = [
        ('fleet', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='model',
            field=models.CharField(
                blank=True,
                help_text='MODEL env var for the agent runtime (e.g. cortex/GLM-5.2). Blank → runtime default.',
                max_length=200,
            ),
        ),
    ]
