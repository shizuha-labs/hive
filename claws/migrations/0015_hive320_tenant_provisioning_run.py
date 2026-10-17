# Generated for HIVE-320 tenant org provisioning saga.

import uuid
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ('claws', '0014_merge_0013_runtime_identity_active_scope'),
        ('claws', '0014_merge_20260702_1905'),
        ('claws', '0014_merge_20260702_1915'),
    ]

    operations = [
        migrations.CreateModel(
            name='TenantProvisioningRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('idempotency_key', models.CharField(help_text='Stable caller/run key; repeated calls return/resume the same saga.', max_length=128, unique=True)),
                ('requester_user_id', models.IntegerField(db_index=True)),
                ('requester_email', models.EmailField(blank=True, default='', max_length=254)),
                ('org_name', models.CharField(max_length=160)),
                ('org_slug', models.SlugField(db_index=True, max_length=80)),
                ('template_slug', models.SlugField(db_index=True, max_length=80)),
                ('plan_slug', models.SlugField(blank=True, default='', max_length=80)),
                ('dry_run', models.BooleanField(db_index=True, default=False)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('ready', 'Ready'), ('failed', 'Failed'), ('manual_review', 'Manual review'), ('tearing_down', 'Tearing down'), ('torn_down', 'Torn down')], db_index=True, default='pending', max_length=20)),
                ('current_step', models.CharField(blank=True, default='', max_length=80)),
                ('step_state', models.JSONField(blank=True, default=dict)),
                ('resource_refs', models.JSONField(blank=True, default=dict)),
                ('audit_events', models.JSONField(blank=True, default=list)),
                ('status_message', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['org_slug', 'template_slug'], name='claws_tenan_org_slu_1ce6fa_idx'),
                    models.Index(fields=['requester_user_id', 'created_at'], name='claws_tenan_request_437852_idx'),
                    models.Index(fields=['status', 'updated_at'], name='claws_tenan_status_f00c0a_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(condition=Q(('dry_run', False), ('status__in', ['pending', 'running', 'ready', 'manual_review'])), fields=('org_slug',), name='uniq_active_tenant_provisioning_org_slug'),
                ],
            },
        ),
    ]
