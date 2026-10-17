from django.urls import path
from . import views
from . import cortex_keys
from hive_platform import coordinator_views
from . import metering_views
from . import org_usage

# HLD "Hive UI" §5 contract. Mounted under /hive/api/ and /api/ (see hive_project/urls.py),
# so the effective paths are e.g. /hive/api/v1/claws/ and /api/v1/claws/.
#
# Each §5 route is registered BOTH with and without a trailing slash. The HLD
# documents the no-slash form (/v1/claws); the UI client + DRF browsable API use
# the trailing-slash form. With APPEND_SLASH=False there is no redirect between
# them, so both must be explicit routes or the documented contract 404s.
_routes = [
    ('v1/me', views.MeView.as_view(), 'me'),
    ('v1/me/services', views.MeServicesView.as_view(), 'me-services'),
    # HIVE-76: same-origin proxy to the caller's own Cortex inference keys.
    ('v1/cortex/api-keys', cortex_keys.CortexApiKeysView.as_view(), 'cortex-api-keys'),
    ('v1/cortex/api-keys/<uuid:key_id>', cortex_keys.CortexApiKeyDetailView.as_view(), 'cortex-api-key-detail'),
    ('v1/token-pools', views.TokenPoolListView.as_view(), 'token-pool-list'),
    ('v1/token-pools/<uuid:pool_id>', views.TokenPoolDetailView.as_view(), 'token-pool-detail'),
    ('v1/token-pools/<uuid:pool_id>/entries', views.TokenEntryListView.as_view(), 'token-entry-list'),
    ('v1/token-pools/<uuid:pool_id>/entries/<uuid:entry_id>', views.TokenEntryDetailView.as_view(), 'token-entry-detail'),
    ('v1/token-pools/<uuid:pool_id>/entries/<uuid:entry_id>/disable', views.TokenEntryDisableView.as_view(), 'token-entry-disable'),
    ('v1/token-pools/<uuid:pool_id>/entries/<uuid:entry_id>/check', views.TokenEntryCheckView.as_view(), 'token-entry-check'),
    ('v1/token-pools/<uuid:pool_id>/entries/<uuid:entry_id>/redeem-reset', views.TokenEntryRedeemResetView.as_view(), 'token-entry-redeem-reset'),
    # Multi-provider credential health board (Claude / Codex / Grok) — operational
    # status + cooldown ETAs (not provider billing "tokens left" meters).
    ('v1/credential-health', views.CredentialHealthOverviewView.as_view(), 'credential-health'),
    # HIVE-581: staff-only browser device-code auth for org Codex accounts.
    ('v1/codex-device-auth/start', views.CodexDeviceAuthStartView.as_view(), 'codex-device-auth-start'),
    ('v1/codex-device-auth/poll', views.CodexDeviceAuthPollView.as_view(), 'codex-device-auth-poll'),
    ('v1/grok-device-auth/start', views.GrokDeviceAuthStartView.as_view(), 'grok-device-auth-start'),
    ('v1/grok-device-auth/poll', views.GrokDeviceAuthPollView.as_view(), 'grok-device-auth-poll'),
    # Claude/Anthropic OAuth code-paste auth (official Claude Code client) → org pool.
    ('v1/claude-oauth/start', views.ClaudeOAuthStartView.as_view(), 'claude-oauth-start'),
    ('v1/claude-oauth/exchange', views.ClaudeOAuthExchangeView.as_view(), 'claude-oauth-exchange'),
    # Google Antigravity OAuth code-paste auth → platform pool.
    ('v1/antigravity-oauth/start', views.AntigravityOAuthStartView.as_view(), 'antigravity-oauth-start'),
    ('v1/antigravity-oauth/exchange', views.AntigravityOAuthExchangeView.as_view(), 'antigravity-oauth-exchange'),
    ('v1/antigravity-oauth/import', views.AntigravityTokenImportView.as_view(), 'antigravity-oauth-import'),
    # HIVE-584: source Codex/OpenAI tokens from HiveCredential (migrate off host credentials.json).
    ('v1/codex/token', views.CodexTokenView.as_view(), 'codex-token'),
    ('v1/tiers', views.TierListView.as_view(), 'tiers'),
    ('v1/org-templates', views.OrgTemplateListView.as_view(), 'org-template-list'),
    ('v1/org-templates/<slug:template_slug>', views.OrgTemplateDetailView.as_view(), 'org-template-detail'),
    ('v1/marketplace/templates', views.MarketplaceTemplateListView.as_view(), 'marketplace-template-list'),
    ('v1/marketplace/templates/<slug:template_slug>', views.MarketplaceTemplateDetailView.as_view(), 'marketplace-template-detail'),
    ('v1/org-provisioning-runs', views.OrgProvisioningRunListCreateView.as_view(), 'org-provisioning-run-list'),
    ('v1/org-provisioning-runs/<uuid:run_id>', views.OrgProvisioningRunDetailView.as_view(), 'org-provisioning-run-detail'),
    ('v1/org-provisioning-runs/<uuid:run_id>/advance', views.OrgProvisioningRunAdvanceView.as_view(), 'org-provisioning-run-advance'),
    # HIVE-2697 ruling 2: terminal cleanup path for saga-parked (manual_review) runs.
    ('v1/org-provisioning-runs/<uuid:run_id>/deprovision', views.OrgProvisioningRunDeprovisionView.as_view(), 'org-provisioning-run-deprovision'),
    # VEN-178 S2 (HIVE-777): dry-run deploy plan for a function-pod template into an existing org.
    ('v1/org-provisioning-plans', views.OrgProvisioningPlanView.as_view(), 'org-provisioning-plan'),
    ('v1/claws', views.ClawListView.as_view(), 'claw-list'),
    ('v1/connect/agents', views.ClawConnectAgentsView.as_view(), 'connect-agent-list'),
    # HIVE-355: Connect DM → personal-agent delivery. Connect POSTs here
    # (HIVE_CONNECT_DELIVERY_URL) to hand off a user DM; Hive acks fast + replies async.
    ('v1/internal/connect/messages', views.InternalConnectMessageView.as_view(), 'internal-connect-message'),
    # HIVE-360: fail-loud liveness/invariant metrics for the delivery path above.
    ('v1/internal/connect/delivery/metrics', views.InternalConnectDeliveryMetricsView.as_view(), 'internal-connect-delivery-metrics'),
    # HIVE-408: fail-loud metric for billing usage-metering record failures.
    ('v1/internal/usage-metering/metrics', views.InternalUsageMeteringMetricsView.as_view(), 'internal-usage-metering-metrics'),
    # PLAT-4482: neutral Metering Core ingestion aliases under Hive's existing
    # /api/v1/internal convention. The canonical service paths are additionally
    # mounted at /internal/v1/usage/events[:batch] in hive_project/urls.py.
    ('v1/internal/usage/events', metering_views.InternalUsageEventIngestView.as_view(), 'internal-usage-event-ingest'),
    ('v1/internal/usage/events:batch', metering_views.InternalUsageEventBatchIngestView.as_view(), 'internal-usage-event-batch-ingest'),
    # HIVE-585: fail-loud metric for revoked Codex/OpenAI refresh tokens.
    ('v1/internal/codex-refresh/metrics', views.InternalCodexRefreshMetricsView.as_view(), 'internal-codex-refresh-metrics'),
    # Back-compat alias for the HIVE-583 PR/test path.
    ('v1/internal/codex/refresh/metrics', views.InternalCodexRefreshMetricsView.as_view(), 'internal-codex-refresh-metrics-legacy'),
    # HIVE-2011: fail-loud stale-Running metric for the Claw idle-recycle worker.
    ('v1/internal/claw-idle/metrics', views.InternalClawIdleMetricsView.as_view(), 'internal-claw-idle-metrics'),
    # HIVE-68/HIVE-83: mine/ prefix must come before <uuid:claw_id> even though
    # Django's uuid converter won't match 'mine'; explicit ordering is clearer.
    ('v1/claws/mine', views.ClawMineView.as_view(), 'claw-mine'),
    ('v1/claws/mine/channel-token', views.ClawChannelTokenView.as_view(), 'claw-channel-token'),
    # HIVE-201: WhatsApp pairing uses the Claw/OpenClaw-native pairing surface.
    ('v1/claws/mine/wa-link', views.ClawWhatsAppLinkView.as_view(), 'claw-wa-link'),
    ('v1/claws/<uuid:claw_id>/whatsapp/links', views.ClawWhatsAppLinksView.as_view(), 'claw-whatsapp-links'),
    ('v1/claws/<uuid:claw_id>/whatsapp/links/<str:code>', views.ClawWhatsAppLinkDetailView.as_view(), 'claw-whatsapp-link-detail'),
    ('v1/claws/<uuid:claw_id>', views.ClawDetailView.as_view(), 'claw-detail'),
    ('v1/claws/<uuid:claw_id>/start', views.ClawStartView.as_view(), 'claw-start'),
    ('v1/claws/<uuid:claw_id>/stop', views.ClawStopView.as_view(), 'claw-stop'),
    ('v1/claws/<uuid:claw_id>/undo-delete', views.ClawUndoDeleteView.as_view(), 'claw-undo-delete'),
    ('v1/claws/<uuid:claw_id>/events', views.ClawEventsView.as_view(), 'claw-events'),
    ('v1/claws/<uuid:claw_id>/status', views.ClawStatusView.as_view(), 'claw-status'),
    # HIVE-198 E1: claw proactive-event source — service-auth (X-Hive-Internal-Token,
    # self-claw via _get_internal_claw) → owner push via ID internal/push/send.
    ('v1/claws/<uuid:claw_id>/notify', views.ClawNotifyView.as_view(), 'claw-notify'),
    ('v1/claws/<uuid:claw_id>/dashboard-url', views.ClawDashboardUrlView.as_view(), 'claw-dashboard-url'),
    ('v1/claws/<uuid:claw_id>/whatsapp/pairing', views.ClawWhatsAppPairingView.as_view(), 'claw-whatsapp-pairing'),
    ('v1/owner/claws/<uuid:claw_id>/data-actions', views.OwnerDataActionsView.as_view(), 'owner-claw-data-actions'),
    ('v1/internal/claws/<uuid:claw_id>/auth-state', views.InternalAuthStateView.as_view(), 'internal-claw-auth-state'),

    # HIVE-4/HIVE-7 control-plane foundation routes. Owner/admin/internal prefixes
    # make downstream export/offboarding work attachable without changing URL shape.
    ('v1/owner/claws/<uuid:claw_id>/export', views.OwnerClawExportView.as_view(), 'owner-claw-export'),
    ('v1/owner/claws/<uuid:claw_id>/export/<str:job_id>', views.OwnerClawExportView.as_view(), 'owner-claw-export-job'),
    ('v1/owner/claws/<uuid:claw_id>/exports', views.OwnerClawExportView.as_view(), 'owner-claw-exports'),
    ('v1/owner/claws/<uuid:claw_id>/hard-delete', views.OwnerHardDeleteView.as_view(), 'owner-claw-hard-delete'),
    ('v1/owner/claws/<uuid:claw_id>/cancel-delete', views.OwnerCancelDeleteView.as_view(), 'owner-claw-cancel-delete'),
    ('v1/admin/claws/<uuid:claw_id>/hold/set', views.AdminLegalHoldSetView.as_view(), 'admin-claw-hold-set'),
    ('v1/admin/claws/<uuid:claw_id>/hold/clear', views.AdminLegalHoldClearView.as_view(), 'admin-claw-hold-clear'),
    ('v1/admin/claws/<uuid:claw_id>/reclaim-restore', views.AdminReclaimRestoreView.as_view(), 'admin-claw-reclaim-restore'),
    ('v1/admin/claws/<uuid:claw_id>/export', views.AdminClawExportView.as_view(), 'admin-claw-export'),
    ('v1/admin/claws/<uuid:claw_id>/export/<str:job_id>', views.AdminClawExportView.as_view(), 'admin-claw-export-job'),
    ('v1/internal/claws/<uuid:claw_id>/hold-state', views.InternalHoldStateView.as_view(), 'internal-claw-hold-state'),
    ('v1/internal/claws/<uuid:claw_id>/verify-channel-token', views.InternalChannelTokenVerifyView.as_view(), 'internal-claw-verify-channel-token'),
    ('v1/internal/claws/<uuid:claw_id>/whatsapp/message', views.InternalWhatsAppMessageView.as_view(), 'internal-claw-whatsapp-message'),
    # HIVE-201 legacy: retained for compatibility; new Claw pairing does not use Ori LINK codes.
    ('v1/internal/wa/bind', views.InternalWhatsAppBindView.as_view(), 'internal-wa-bind'),

    # HIVE-127: Token/Credential Coordinator — per-owner isolation enforcement.
    # model-token + report-status are broker/workload-gated; token-entries CRUD is
    # owner-scoped (non-owner entry -> 404, no enumeration leak).
    ('v1/coordinator/model-token', coordinator_views.ModelTokenView.as_view(), 'coordinator-model-token'),
    ('v1/coordinator/report-status', coordinator_views.ReportStatusView.as_view(), 'coordinator-report-status'),
    # HIVE-314 slice 2 (HIVE-2166): broker-gated org provider resolution for the
    # claude-bridge spawn hook. Broker-gated + non-enumerable; org from minted
    # claims; fail-closed Admin-SoT membership proof; no key material in responses.
    ('v1/coordinator/org-provider-resolve', coordinator_views.OrgProviderResolveView.as_view(), 'coordinator-org-provider-resolve'),
    ('v1/coordinator/token-entries', coordinator_views.TokenEntryListCreateView.as_view(), 'coordinator-token-entries'),
    ('v1/coordinator/token-entries/<uuid:entry_id>', coordinator_views.TokenEntryDetailView.as_view(), 'coordinator-token-entry-detail'),
    # HIVE-1824: staff-only, provider-token-free live allocator QA suite.  The
    # synthetic owner scope is uncommitted and deleted before the response.
    ('v1/qa/tokenpool-allocator', coordinator_views.TokenPoolAllocatorQaView.as_view(), 'qa-tokenpool-allocator'),
    # HIVE-314 S4: per-org usage/consumption view proxied from the metering
    # pipeline (org-owner-only visibility, no cross-org leak).
    ('v1/org-usage', org_usage.OrgUsageView.as_view(), 'org-usage'),
]

urlpatterns = [
    path('health/', views.HealthView.as_view(), name='health'),
    path('health', views.HealthView.as_view()),
    # HIVE-342: monitoring-only drift signal — never point kubelet probes here.
    path('health/isolation/', views.IsolationHealthView.as_view(), name='health-isolation'),
    path('health/isolation', views.IsolationHealthView.as_view()),
]

for _route, _view, _name in _routes:
    # Trailing-slash form is canonical (carries the route name for reverse()).
    urlpatterns.append(path(_route + '/', _view, name=_name))
    # No-slash alias for the documented §5 paths (no name → no reverse() ambiguity).
    urlpatterns.append(path(_route, _view))
