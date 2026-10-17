# HIVE-296 P1: AgentRuntimeDesiredState, AgentHealthSnapshot, AgentHealthIssue

import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0010_merge_hive273_hivecredential_grants'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentRuntimeDesiredState',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('agent', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='desired_state',
                    to='fleet.fleetagent',
                )),
                ('platform_user_id', models.CharField(blank=True, max_length=64)),
                ('canonical_username', models.CharField(blank=True, max_length=150)),
                ('canonical_email', models.CharField(blank=True, max_length=254)),
                ('team_memberships', models.JSONField(blank=True, default=list)),
                ('effective_capabilities', models.JSONField(blank=True, default=list)),
                ('skills', models.JSONField(blank=True, default=dict)),
                ('mcp_access', models.JSONField(blank=True, default=list)),
                ('runtime_profile', models.JSONField(blank=True, default=dict)),
                ('credential_grant_expectations', models.JSONField(blank=True, default=list)),
                ('desired_generation', models.PositiveIntegerField(default=1)),
                ('desired_config_hash', models.CharField(blank=True, max_length=64)),
                ('previous_generation', models.PositiveIntegerField(blank=True, null=True)),
                ('source', models.CharField(
                    choices=[
                        ('hive_ui', 'Hive UI'),
                        ('team_roster_sync', 'Team Roster Sync'),
                        ('capability_recompute', 'Capability Recompute'),
                        ('admin_override', 'Admin Override'),
                    ],
                    default='hive_ui',
                    max_length=32,
                )),
                ('source_actor', models.CharField(blank=True, max_length=150)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'verbose_name': 'Agent runtime desired state'},
        ),
        migrations.CreateModel(
            name='AgentHealthSnapshot',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('agent', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='health_snapshots',
                    to='fleet.fleetagent',
                )),
                ('daemon', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='health_snapshots',
                    to='fleet.daemonregistry',
                )),
                ('runtime_id', models.CharField(blank=True, max_length=256)),
                ('container_pod_id', models.CharField(blank=True, max_length=256)),
                ('runtime_username', models.CharField(blank=True, max_length=150)),
                ('runtime_email', models.CharField(blank=True, max_length=254)),
                ('last_heartbeat_at', models.DateTimeField(blank=True, null=True)),
                ('last_heartbeat_identity_email', models.CharField(blank=True, max_length=254)),
                ('platform_user_id', models.CharField(blank=True, max_length=64)),
                ('canonical_email', models.CharField(blank=True, max_length=254)),
                ('account_type', models.CharField(blank=True, max_length=50)),
                ('account_active', models.BooleanField(blank=True, null=True)),
                ('alias_resolution_evidence', models.JSONField(blank=True, default=dict)),
                ('desired_generation_seen', models.PositiveIntegerField(blank=True, null=True)),
                ('applied_generation', models.PositiveIntegerField(blank=True, null=True)),
                ('observed_generation', models.PositiveIntegerField(blank=True, null=True)),
                ('observed_config_hash', models.CharField(blank=True, max_length=64)),
                ('apply_status', models.CharField(
                    choices=[('ok', 'OK'), ('pending', 'Pending'), ('failed', 'Failed'), ('unknown', 'Unknown')],
                    default='unknown',
                    max_length=16,
                )),
                ('last_apply_error', models.TextField(blank=True)),
                ('effective_capabilities', models.JSONField(blank=True, default=list)),
                ('skills', models.JSONField(blank=True, default=dict)),
                ('mcp_access', models.JSONField(blank=True, default=list)),
                ('credential_grant_health', models.JSONField(blank=True, default=dict)),
                ('missing_grants', models.JSONField(blank=True, default=list)),
                ('extra_grants', models.JSONField(blank=True, default=list)),
                ('pulse_assignable', models.BooleanField(blank=True, null=True)),
                ('roster_assignable_reason', models.TextField(blank=True)),
                ('team_roster_memberships', models.JSONField(blank=True, default=list)),
                ('provider_health', models.JSONField(blank=True, default=dict)),
                ('snapshot_type', models.CharField(
                    choices=[('full', 'Full'), ('delta', 'Delta')],
                    default='full',
                    max_length=8,
                )),
                ('source_error', models.TextField(blank=True)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={'ordering': ['-received_at']},
        ),
        migrations.CreateModel(
            name='AgentHealthIssue',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('snapshot', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='issues',
                    to='fleet.agenthealthsnapshot',
                )),
                ('agent', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='health_issues',
                    to='fleet.fleetagent',
                )),
                ('issue_type', models.CharField(
                    choices=[
                        ('identity_alias_drift', 'Identity alias drift'),
                        ('identity_unresolved', 'Identity unresolved'),
                        ('platform_user_mismatch', 'Platform user mismatch'),
                        ('inactive_or_wrong_account_type', 'Inactive or wrong account type'),
                        ('credential_grant_missing', 'Credential grant missing'),
                        ('provider_unavailable_or_quota', 'Provider unavailable or quota'),
                        ('roster_unassignable', 'Roster unassignable'),
                        ('heartbeat_identity_mismatch', 'Heartbeat identity mismatch'),
                        ('desired_generation_unseen', 'Desired generation unseen'),
                        ('applied_generation_stale', 'Applied generation stale'),
                        ('observed_config_drift', 'Observed config drift'),
                        ('capability_grant_missing', 'Capability grant missing'),
                        ('unexpected_capability_grant', 'Unexpected capability grant'),
                        ('mcp_access_drift', 'MCP access drift'),
                        ('skill_profile_drift', 'Skill/profile drift'),
                    ],
                    db_index=True,
                    max_length=64,
                )),
                ('severity', models.CharField(
                    choices=[
                        ('critical', 'Critical'), ('high', 'High'), ('medium', 'Medium'),
                        ('low', 'Low'), ('info', 'Info'),
                    ],
                    default='medium',
                    max_length=16,
                )),
                ('service', models.CharField(blank=True, max_length=64)),
                ('field_path', models.CharField(blank=True, max_length=256)),
                ('expected', models.JSONField(blank=True, null=True)),
                ('actual', models.JSONField(blank=True, null=True)),
                ('detail', models.TextField(blank=True)),
                ('status', models.CharField(
                    choices=[('open', 'Open'), ('resolved', 'Resolved')],
                    db_index=True,
                    default='open',
                    max_length=16,
                )),
                ('first_seen', models.DateTimeField()),
                ('last_seen', models.DateTimeField()),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('recovery_policy', models.CharField(blank=True, max_length=100)),
                ('recovery_action_id', models.UUIDField(blank=True, null=True)),
            ],
            options={'ordering': ['-last_seen']},
        ),
        migrations.AddIndex(
            model_name='agenthealthsnapshot',
            index=models.Index(fields=['agent', '-received_at'], name='fleet_snap_agent_recv_idx'),
        ),
        migrations.AddIndex(
            model_name='agenthealthsnapshot',
            index=models.Index(fields=['daemon', '-received_at'], name='fleet_snap_daemon_recv_idx'),
        ),
        migrations.AddIndex(
            model_name='agenthealthissue',
            index=models.Index(fields=['agent', 'status'], name='fleet_issue_agent_status_idx'),
        ),
        migrations.AddIndex(
            model_name='agenthealthissue',
            index=models.Index(fields=['issue_type', 'status'], name='fleet_issue_type_status_idx'),
        ),
    ]
