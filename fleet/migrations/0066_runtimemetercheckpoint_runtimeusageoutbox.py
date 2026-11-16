from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0065_deprecate_grok_47'),
    ]

    operations = [
        migrations.CreateModel(
            name='RuntimeMeterCheckpoint',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('last_emitted_at', models.DateTimeField()),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('agent', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='runtime_meter_checkpoint', to='fleet.fleetagent')),
            ],
            options={
                'verbose_name': 'runtime meter checkpoint',
                'verbose_name_plural': 'runtime meter checkpoints',
            },
        ),
        migrations.CreateModel(
            name='RuntimeUsageOutbox',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(max_length=512)),
                ('event_id', models.UUIDField(default=django.db.models.fields.uuid.uuid4, editable=False, unique=True)),
                ('event_type', models.CharField(default='io.shizuha.usage.measured.v1', max_length=255)),
                ('subject', models.CharField(max_length=512)),
                ('resource_type', models.CharField(default='agent.runtime', max_length=64)),
                ('resource_id', models.CharField(max_length=128)),
                ('occurred_at', models.DateTimeField()),
                ('interval_start', models.DateTimeField()),
                ('interval_end', models.DateTimeField()),
                ('data', models.JSONField()),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('delivered', 'Delivered'), ('failed', 'Failed')], default='pending', max_length=16)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('last_error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('delivered_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.AddIndex(
            model_name='runtimeusageoutbox',
            index=models.Index(fields=['status', 'created_at'], name='runtime_outbox_status_idx'),
        ),
    ]
