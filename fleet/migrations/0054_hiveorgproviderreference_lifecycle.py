"""HIVE-2161: HiveOrgProviderReference lifecycle + audit fields (HLD HIVE-336)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0053_fleetagent_runtime_lifecycle_state'),
    ]

    operations = [
        migrations.AlterField(
            model_name='hiveorgproviderreference',
            name='status',
            field=models.CharField(
                choices=[
                    ('active', 'Active'),
                    ('verifying', 'Verifying'),
                    ('probation', 'Probation'),
                    ('disabled', 'Disabled'),
                    ('mismatch', 'Mismatch'),
                    ('invalid', 'Invalid'),
                    ('exhausted', 'Exhausted'),
                ],
                default='active',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='hiveorgproviderreference',
            name='provider_type',
            field=models.CharField(
                choices=[
                    ('openai', 'OpenAI'),
                    ('anthropic', 'Anthropic'),
                    ('openai_compatible', 'OpenAI-compatible'),
                ],
                default='openai_compatible',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='hiveorgproviderreference',
            name='display_name',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='hiveorgproviderreference',
            name='last_validation_result',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Redacted outcome code: ok | auth_failed | rate_limited | '
                          'endpoint_unreachable | schema_mismatch. Never a raw body.',
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name='hiveorgproviderreference',
            name='created_by_principal',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='hiveorgproviderreference',
            name='updated_by_principal',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
    ]
