from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0023_fleetagent_pending_config'),
    ]

    operations = [
        migrations.AddField(
            model_name='daemonregistry',
            name='agent_runtime_image',
            field=models.CharField(blank=True, default='', max_length=512),
        ),
        migrations.AddField(
            model_name='daemonregistry',
            name='harness_report',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
