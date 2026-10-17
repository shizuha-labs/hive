import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='AnalyticsEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('schema_version', models.IntegerField(default=1)),
                ('event_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('org_id', models.BigIntegerField(db_index=True)),
                ('user_ref', models.CharField(max_length=64)),
                ('surface', models.CharField(choices=[('home', 'home'), ('hive', 'hive'), ('pulse', 'pulse'), ('admin', 'admin')], max_length=16)),
                ('event_type', models.CharField(choices=[('page_view', 'page_view'), ('feature_use', 'feature_use'), ('drop_off', 'drop_off'), ('error_surface', 'error_surface'), ('journey_step', 'journey_step'), ('journey_complete', 'journey_complete')], max_length=24)),
                ('target', models.TextField(blank=True, default='')),
                ('session_id', models.UUIDField(blank=True, null=True)),
                ('ts', models.DateTimeField(db_index=True)),
                ('props', models.JSONField(blank=True, default=dict)),
                ('ingested_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={'db_table': 'analytics_event'},
        ),
        migrations.AddIndex(
            model_name='analyticsevent',
            index=models.Index(fields=['org_id', 'ts'], name='ae_org_ts_idx'),
        ),
        migrations.AddIndex(
            model_name='analyticsevent',
            index=models.Index(fields=['org_id', 'surface', 'event_type'], name='ae_org_surface_type_idx'),
        ),
    ]
