from django.db import migrations, models
import uuid
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0055_fleetagent_native_lifecycle_receipt'),
    ]

    operations = [
        migrations.CreateModel(
            name='FleetRuntimeOutbox',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('source', models.CharField(db_index=True, max_length=255)),
                ('event_id', models.CharField(max_length=255)),
                ('event_type', models.CharField(max_length=255)),
                ('occurred_at', models.DateTimeField(db_index=True)),
                ('recorded_at', models.DateTimeField(auto_now_add=True)),
                ('subject', models.CharField(max_length=255)),
                ('interval_start', models.DateTimeField()),
                ('interval_end', models.DateTimeField()),
                ('quantity', models.DecimalField(decimal_places=6, max_digits=24)),
                ('envelope', models.JSONField()),
                ('payload_sha256', models.CharField(max_length=64)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('delivered', 'Delivered'), ('failed', 'Failed'), ('conflict', 'Conflict')], db_index=True, default='pending', max_length=16)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('next_attempt_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('last_error', models.TextField(blank=True)),
                ('delivered_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'fleet_fleetruntimeoutbox',
                'ordering': ['next_attempt_at', 'recorded_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='fleetruntimeoutbox',
            constraint=models.UniqueConstraint(fields=('source', 'event_id'), name='uniq_fleet_runtime_outbox_source_id'),
        ),
        migrations.AddIndex(
            model_name='fleetruntimeoutbox',
            index=models.Index(fields=['status', 'next_attempt_at'], name='fleet_runtime_outbox_due_idx'),
        ),
    ]
