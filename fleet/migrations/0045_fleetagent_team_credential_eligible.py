import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0044_pin_agents_drop_all_fallback_chains'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='hivecredential',
            name='fleet_hivecred_active_team_unique',
        ),
        migrations.AddField(
            model_name='hivecredential',
            name='organization_slug',
            field=models.CharField(db_index=True, default='shizuha', help_text='Organization owning a team-scoped grant; ignored for agent-scoped grants.', max_length=100),
        ),
        migrations.AlterModelOptions(
            name='hivecredential',
            options={'ordering': ['scope', 'organization_slug', 'team_slug', 'provider', 'purpose']},
        ),
        migrations.AddConstraint(
            model_name='hivecredential',
            constraint=models.UniqueConstraint(condition=models.Q(('is_active', True), ('scope', 'team')), fields=('scope', 'organization_slug', 'team_slug', 'provider', 'purpose'), name='fleet_hivecred_active_team_unique'),
        ),
        migrations.CreateModel(
            name='FleetAgentTeamCredentialEligibility',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('organization_slug', models.CharField(db_index=True, default='shizuha', max_length=100)),
                ('team_slug', models.CharField(db_index=True, max_length=100)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('actor_user_id', models.CharField(blank=True, max_length=100)),
                ('reason', models.CharField(blank=True, max_length=300)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('agent', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='team_credential_eligibilities', to='fleet.fleetagent')),
            ],
            options={
                'ordering': ['organization_slug', 'team_slug', 'agent__agent_username'],
                'indexes': [models.Index(fields=['organization_slug', 'team_slug', 'is_active'], name='fleet_elig_org_team_active_idx')],
                'constraints': [models.UniqueConstraint(fields=('agent', 'organization_slug', 'team_slug'), name='fleet_agent_team_credential_eligibility_unique')],
            },
        ),
        migrations.CreateModel(
            name='FleetAgentTeamCredentialEligibilityAudit',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('action', models.CharField(choices=[('grant', 'Grant'), ('revoke', 'Revoke')], max_length=16)),
                ('actor_user_id', models.CharField(max_length=100)),
                ('agent_username', models.CharField(max_length=100)),
                ('organization_slug', models.CharField(default='shizuha', max_length=100)),
                ('team_slug', models.CharField(max_length=100)),
                ('redacted_before', models.JSONField(blank=True, default=dict)),
                ('redacted_after', models.JSONField(blank=True, default=dict)),
                ('message', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('eligibility', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='audit_events', to='fleet.fleetagentteamcredentialeligibility')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
