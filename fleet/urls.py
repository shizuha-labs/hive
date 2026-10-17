from django.urls import path
from . import views
from . import org_provider_views

# All routes dual-registered (trailing-slash + no-slash) per HIVE convention.
_routes = [
    ('v1/fleet/context-budget', views.FleetContextBudgetView.as_view(), 'fleet-context-budget'),
    ('v1/fleet/context-budget/metrics', views.FleetContextBudgetMetricsView.as_view(), 'fleet-context-budget-metrics'),
    ('v1/fleet/organizations', views.FleetOrganizationListView.as_view(), 'fleet-organizations'),
    ('v1/fleet/personal-agent', views.PersonalAgentEnsureView.as_view(), 'fleet-personal-agent'),
    ('v1/fleet/pulse-snapshot', views.FleetPulseSnapshotView.as_view(), 'fleet-pulse-snapshot'),
    ('v1/fleet/internal/connect-wake', views.ConnectWakeAgentView.as_view(), 'fleet-connect-wake'),
    ('v1/fleet/internal/ensure-personal-agent', views.InternalEnsurePersonalAgentView.as_view(), 'fleet-internal-ensure-personal-agent'),
    ('v1/fleet/runtime-options', views.FleetRuntimeOptionsView.as_view(), 'fleet-runtime-options'),
    ('v1/fleet/capabilities/audit', views.CapabilityAuditView.as_view(), 'fleet-capability-audit'),
    ('v1/fleet/capabilities', views.CapabilityListView.as_view(), 'fleet-capability-list'),
    ('v1/fleet/capabilities/<str:slug>', views.CapabilityDetailView.as_view(), 'fleet-capability-detail'),
    ('v1/fleet/capabilities/<str:slug>/<str:action>', views.CapabilityActionView.as_view(), 'fleet-capability-action'),
    ('v1/fleet/teams', views.FleetTeamRosterView.as_view(), 'fleet-team-roster'),
    ('v1/fleet/team-coverage', views.FleetTeamCoverageView.as_view(), 'fleet-team-coverage'),
    ('v1/fleet/teams/<str:team_name>', views.FleetTeamDetailView.as_view(), 'fleet-team-detail'),
    ('v1/fleet/teams/<str:team_name>/members', views.FleetTeamMembersView.as_view(), 'fleet-team-members'),
    ('v1/fleet/teams/<str:team_name>/capabilities', views.TeamCapabilitiesView.as_view(), 'fleet-team-capabilities'),
    ('v1/fleet/credentials', views.FleetCredentialListView.as_view(), 'fleet-credential-list'),
    ('v1/fleet/credentials/self-upsert', views.FleetCredentialSelfUpsertView.as_view(), 'fleet-credential-self-upsert'),
    ('v1/fleet/credentials/<uuid:credential_id>', views.FleetCredentialDetailView.as_view(), 'fleet-credential-detail'),
    ('v1/fleet/credentials/<uuid:credential_id>/audit', views.FleetCredentialAuditView.as_view(), 'fleet-credential-audit'),
    ('v1/fleet/credential-requests', views.FleetCredentialRequestListView.as_view(), 'fleet-credential-request-list'),
    ('v1/fleet/credential-requests/expire', views.FleetCredentialRequestExpireView.as_view(), 'fleet-credential-request-expire'),
    ('v1/fleet/credential-requests/<uuid:request_id>/deny', views.FleetCredentialRequestDenyView.as_view(), 'fleet-credential-request-deny'),
    ('v1/fleet/agent-templates', views.FleetAgentTemplateListView.as_view(), 'fleet-agent-template-list'),
    ('v1/fleet/agent-templates/<str:template_slug>', views.FleetAgentTemplateDetailView.as_view(), 'fleet-agent-template-detail'),
    ('v1/fleet/agent-cards', views.FleetAgentCardListView.as_view(), 'fleet-agent-card-list'),
    ('v1/fleet/agents', views.FleetAgentListView.as_view(), 'fleet-agent-list'),
    # PLAT-1063: registered BEFORE the <agent_id> route so it isn't captured as an id.
    ('v1/fleet/agents/cache-stats', views.FleetAgentIndexCacheStatsView.as_view(), 'fleet-agent-index-cache-stats'),
    ('v1/fleet/agents/<str:agent_id>', views.FleetAgentDetailView.as_view(), 'fleet-agent-detail'),
    ('v1/fleet/agents/<str:agent_id>/runtime-changes/<uuid:change_id>', views.FleetRuntimeChangeView.as_view(), 'fleet-runtime-change'),
    ('v1/fleet/agents/<str:agent_id>/enable', views.FleetAgentEnableView.as_view(), 'fleet-agent-enable'),
    ('v1/fleet/agents/<str:agent_id>/archive', views.FleetAgentArchiveView.as_view(), 'fleet-agent-archive'),
    ('v1/fleet/agents/<str:agent_id>/unarchive', views.FleetAgentUnarchiveView.as_view(), 'fleet-agent-unarchive'),
    ('v1/fleet/agents/<str:agent_id>/disable', views.FleetAgentDisableView.as_view(), 'fleet-agent-disable'),
    ('v1/fleet/agents/<str:agent_id>/stop', views.FleetAgentStopView.as_view(), 'fleet-agent-stop'),
    ('v1/fleet/agents/<str:agent_id>/restart', views.FleetAgentRestartView.as_view(), 'fleet-agent-restart'),
    ('v1/fleet/agents/<str:agent_id>/reset-session', views.FleetAgentResetSessionView.as_view(), 'fleet-agent-reset-session'),
    ('v1/fleet/agents/<str:agent_id>/switch-backend', views.FleetAgentSwitchBackendView.as_view(), 'fleet-agent-switch-backend'),
    ('v1/fleet/agents/<str:agent_id>/claim', views.FleetAgentClaimView.as_view(), 'fleet-agent-claim'),
    ('v1/fleet/agents/<str:agent_id>/events', views.FleetAgentEventsView.as_view(), 'fleet-agent-events'),
    ('v1/fleet/agents/<str:agent_id>/activity', views.FleetAgentActivityView.as_view(), 'fleet-agent-activity'),
    ('v1/fleet/agents/<str:agent_id>/status', views.FleetAgentStatusView.as_view(), 'fleet-agent-status'),
    ('v1/fleet/agents/<str:agent_id>/team-placement', views.FleetAgentTeamPlacementView.as_view(), 'fleet-agent-team-placement'),
    ('v1/fleet/agents/<str:agent_id>/effective-capabilities', views.AgentEffectiveCapabilitiesView.as_view(), 'fleet-agent-effective-capabilities'),
    ('v1/fleet/agents/<str:agent_id>/mcp-projection', views.AgentMcpProjectionView.as_view(), 'fleet-agent-mcp-projection'),
    ('v1/fleet/agents/<str:agent_id>/runtime-lane', views.AgentRuntimeLaneView.as_view(), 'fleet-agent-runtime-lane'),
    ('v1/fleet/agents/<str:agent_id>/capability-overrides', views.AgentCapabilityOverridesView.as_view(), 'fleet-agent-capability-overrides'),
    ('v1/fleet/efficiency', views.FleetEfficiencyView.as_view(), 'fleet-efficiency'),
    ('v1/fleet/runtime-image', views.FleetRuntimeImageView.as_view(), 'fleet-runtime-image'),
    ('v1/fleet/native-runtime-image', views.FleetNativeRuntimeImageView.as_view(), 'fleet-native-runtime-image'),
    ('v1/fleet/harness-versions', views.FleetHarnessVersionsView.as_view(), 'fleet-harness-versions'),
    ('v1/fleet/harness-versions/<str:harness>/upgrade', views.FleetHarnessUpgradeView.as_view(), 'fleet-harness-upgrade'),
    ('v1/fleet/recovery-actions/metrics', views.FleetRecoveryActionMetricsView.as_view(), 'fleet-recovery-action-metrics'),
    ('v1/fleet/daemon/identity-health', views.DaemonIdentityHealthView.as_view(), 'fleet-daemon-identity-health'),
    ('v1/fleet/daemon/identity-roster', views.DaemonIdentityRosterView.as_view(), 'fleet-daemon-identity-roster'),
    # HIVE-1889 (S3): org Settings > Providers — thin mirror of org-scoped
    # Cortex provider refs (HIVE-1862 contract). No raw keys in Hive.
    ('v1/org-provider-refs', org_provider_views.OrgProviderReferenceListView.as_view(), 'org-provider-ref-list'),
    ('v1/org-provider-refs/provision', org_provider_views.OrgProviderReferenceProvisionView.as_view(), 'org-provider-ref-provision'),
    ('v1/org-provider-refs/<int:ref_id>', org_provider_views.OrgProviderReferenceDetailView.as_view(), 'org-provider-ref-detail'),
    ('v1/org-provider-refs/<int:ref_id>/validate', org_provider_views.OrgProviderReferenceValidateView.as_view(), 'org-provider-ref-validate'),
    ('v1/org-provider-refs/<int:ref_id>/rotate', org_provider_views.OrgProviderReferenceRotateView.as_view(), 'org-provider-ref-rotate'),
    ('v1/org-provider-refs/<int:ref_id>/disable', org_provider_views.OrgProviderReferenceDisableView.as_view(), 'org-provider-ref-disable'),
    # HIVE-2622 S4 deliverable 2: request-time org-scoped credential lease —
    # the BYO runtime resolves agent→org→provider here (broker-gated, org
    # server-derived from the token owner scope; loud gate until the flip).
    ('v1/org-provider-lease', org_provider_views.OrgProviderLeaseView.as_view(), 'org-provider-lease'),
]

urlpatterns = []
for _route, _view, _name in _routes:
    urlpatterns.append(path(_route + '/', _view, name=_name))
    urlpatterns.append(path(_route, _view))
