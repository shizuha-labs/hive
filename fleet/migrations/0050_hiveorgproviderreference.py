"""HIVE-1862/1889: HiveOrgProviderReference — thin org-scoped Cortex provider mirror."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0049_ceo_office_full_agent_mcp'),
    ]

    operations = [
        migrations.CreateModel(
            name='HiveOrgProviderReference',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('org_id', models.IntegerField(db_index=True)),
                ('cortex_provider_id', models.UUIDField(db_index=True)),
                ('cortex_key_ref', models.CharField(blank=True, default='', max_length=200)),
                ('default_models', models.JSONField(blank=True, default=list)),
                ('status', models.CharField(choices=[('active', 'Active'), ('verifying', 'Verifying'), ('probation', 'Probation'), ('disabled', 'Disabled'), ('mismatch', 'Mismatch')], default='active', max_length=20)),
                ('per_agent_overrides', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'hive org provider reference',
                'ordering': ['org_id', 'cortex_provider_id'],
            },
        ),
        migrations.AddConstraint(
            model_name='hiveorgproviderreference',
            constraint=models.UniqueConstraint(fields=('org_id', 'cortex_provider_id'), name='uniq_hive_org_provider_ref'),
        ),
    ]
