from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0022_hivecredentialrequest'),
    ]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='pending_config',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='HIVE-597: desired-config updates accepted by the API but not yet '
                          'confirmed applied by the managing daemon. Written on every config '
                          'push (instantly, even when the daemon-link is down), replayed on '
                          'daemon reconnect, protected from runtime-delta clobber, and '
                          'cleared when the daemon confirms with a config_applied delta.',
            ),
        ),
    ]
