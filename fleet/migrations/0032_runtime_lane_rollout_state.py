import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('fleet', '0031_plat4172_needs_help')]

    operations = [
        migrations.CreateModel(
            name='RuntimeChange',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('base_generation', models.PositiveIntegerField()),
                ('desired_generation', models.PositiveIntegerField()),
                ('requested_patch', models.JSONField(blank=True, default=dict)),
                ('resolved_lane', models.JSONField(default=dict)),
                ('previous_lane', models.JSONField(blank=True, default=dict)),
                ('registry_version', models.CharField(max_length=64)),
                ('runtime_release_id', models.CharField(max_length=160)),
                ('lane_digest', models.CharField(max_length=64)),
                ('state', models.CharField(choices=[('ACCEPTED', 'Accepted'), ('ACTUATOR_PENDING', 'Actuator pending'), ('ROLLING', 'Rolling'), ('HEALTHY', 'Healthy'), ('FAILED', 'Failed'), ('ROLLBACK_PENDING', 'Rollback pending'), ('ROLLED_BACK', 'Rolled back'), ('ROLLBACK_FAILED', 'Rollback failed')], db_index=True, default='ACCEPTED', max_length=32)),
                ('failure_code', models.CharField(blank=True, max_length=80)),
                ('failure_evidence', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('agent', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='runtime_changes', to='fleet.fleetagent')),
                ('rollback_of', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rollback_change', to='fleet.runtimechange')),
            ],
            options={'ordering': ['-desired_generation']},
        ),
        migrations.AddConstraint(
            model_name='runtimechange',
            constraint=models.UniqueConstraint(fields=('agent', 'desired_generation'), name='uniq_runtime_change_agent_generation'),
        ),
        migrations.AddIndex(
            model_name='runtimechange',
            index=models.Index(fields=['agent', 'state'], name='fleet_rtchg_agent_state_idx'),
        ),
        migrations.CreateModel(
            name='RuntimeLaneOutbox',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('payload', models.JSONField(default=dict)),
                ('attempt_count', models.PositiveIntegerField(default=0)),
                ('last_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('delivered_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('last_error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('change', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='outbox', to='fleet.runtimechange')),
            ],
        ),
    ]
