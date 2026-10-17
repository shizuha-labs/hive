from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('fleet', '0042_backfill_all_managed_grok_fallbacks'),
    ]

    operations = [
        migrations.AlterField(
            model_name='runtimechange',
            name='state',
            field=models.CharField(
                choices=[
                    ('ACCEPTED', 'Accepted'),
                    ('ACTUATOR_PENDING', 'Actuator pending'),
                    ('ROLLING', 'Rolling'),
                    ('HEALTHY', 'Healthy'),
                    ('SUPERSEDED', 'Superseded'),
                    ('FAILED', 'Failed'),
                    ('ROLLBACK_PENDING', 'Rollback pending'),
                    ('ROLLED_BACK', 'Rolled back'),
                    ('ROLLBACK_FAILED', 'Rollback failed'),
                ],
                db_index=True,
                default='ACCEPTED',
                max_length=32,
            ),
        ),
    ]
