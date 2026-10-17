from django.contrib import admin

from .models import (
    AgentCapabilityOverride,
    CapabilityAuditEvent,
    CapabilityDefinition,
    FleetAgent,
    FleetAgentActivitySnapshot,
    FleetAgentEvent,
    LeaseCriticalTeamPolicy,
    TeamMembershipRole,
    TeamRequiredCapability,
)


@admin.register(FleetAgent)
class FleetAgentAdmin(admin.ModelAdmin):
    list_display = ('agent_username', 'status', 'resource_profile', 'arch', 'isolated', 'cohort', 'updated_at')
    list_filter = ('status', 'resource_profile', 'arch', 'isolated', 'hostplane_ssh', 'sandbox_required')
    search_fields = ('agent_username', 'agent_id', 'display_name')
    readonly_fields = ('id', 'namespace', 'created_at', 'updated_at', 'provisioned_at', 'last_active_at')


@admin.register(FleetAgentEvent)
class FleetAgentEventAdmin(admin.ModelAdmin):
    list_display = ('agent', 'event_type', 'created_at')
    list_filter = ('event_type',)
    search_fields = ('agent__agent_username', 'message')


@admin.register(FleetAgentActivitySnapshot)
class FleetAgentActivitySnapshotAdmin(admin.ModelAdmin):
    list_display = ('agent', 'newest_event_at', 'captured_at', 'pod_name', 'source_state_at_capture')
    search_fields = ('agent__agent_username', 'pod_name')
    readonly_fields = ('captured_at',)


@admin.register(CapabilityDefinition)
class CapabilityDefinitionAdmin(admin.ModelAdmin):
    list_display = ('slug', 'display_name', 'risk_tier', 'activation_status', 'version', 'updated_at')
    list_filter = ('risk_tier', 'activation_status', 'lifecycle_status')
    search_fields = ('slug', 'display_name', 'description')
    readonly_fields = ('definition_hash', 'version', 'created_at', 'updated_at')


@admin.register(TeamRequiredCapability)
class TeamRequiredCapabilityAdmin(admin.ModelAdmin):
    list_display = ('team_slug', 'capability', 'enabled', 'updated_at')
    list_filter = ('enabled', 'capability__risk_tier')
    search_fields = ('team_slug', 'capability__slug', 'reason')


@admin.register(TeamMembershipRole)
class TeamMembershipRoleAdmin(admin.ModelAdmin):
    list_display = ('agent_identifier', 'team_slug', 'organization_slug', 'role_title', 'membership_role', 'tier', 'enabled', 'updated_at')
    list_filter = ('enabled', 'membership_role', 'team_slug', 'organization_slug')
    search_fields = ('agent_identifier', 'team_slug', 'organization_slug', 'role_title', 'reason')


@admin.register(AgentCapabilityOverride)
class AgentCapabilityOverrideAdmin(admin.ModelAdmin):
    list_display = ('agent_identifier', 'capability', 'action', 'expires_at', 'updated_at')
    list_filter = ('action', 'capability__risk_tier')
    search_fields = ('agent_identifier', 'capability__slug', 'reason')


@admin.register(CapabilityAuditEvent)
class CapabilityAuditEventAdmin(admin.ModelAdmin):
    list_display = ('action', 'target_type', 'target_key', 'actor', 'created_at')
    list_filter = ('action', 'target_type', 'runtime_reconcile_outcome')
    search_fields = ('target_key', 'actor', 'reason', 'diagnostic')
    readonly_fields = ('before_hash', 'after_hash', 'created_at')


@admin.register(LeaseCriticalTeamPolicy)
class LeaseCriticalTeamPolicyAdmin(admin.ModelAdmin):
    """HIVE-2841: edit the critical-team lease floor without a deploy.

    The lease reconcile reads this table (60s cache); adding, disabling, or
    editing a row changes who the scheduler reserves on the next tick.
    """
    list_display = ('team_slug', 'min_leases', 'enabled', 'updated_at')
    list_filter = ('enabled',)
    search_fields = ('team_slug', 'note')
