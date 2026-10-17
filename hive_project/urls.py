from django.contrib import admin
from django.urls import path, include
from claws import metering_read_views, metering_views
from fleet.capability_evidence_views import CapabilityEvidenceView
from fleet import views as fleet_views

urlpatterns = [
    # Metering Core's product-neutral, in-cluster contract. These deliberately
    # sit outside the historical /hive/api prefix; InternalApiIngressGuardMiddleware
    # still blocks ingress-borne requests because the path contains /internal/.
    path('internal/v1/usage/events', metering_views.InternalUsageEventIngestView.as_view()),
    path('internal/v1/usage/events/', metering_views.InternalUsageEventIngestView.as_view()),
    path('internal/v1/usage/events:batch', metering_views.InternalUsageEventBatchIngestView.as_view()),
    path('internal/v1/usage/events:batch/', metering_views.InternalUsageEventBatchIngestView.as_view()),
    # PLAT-7006 (R1B) — server-derived default BillingAccount resolution for producers
    path('internal/v1/accounts:resolve', metering_views.InternalAccountResolveView.as_view()),
    path('internal/v1/accounts:resolve/', metering_views.InternalAccountResolveView.as_view()),
    # PLAT-4485 — Metering → Books statement drafts
    path('internal/v2/billing/statements/drafts', metering_views.InternalBillingStatementDraftView.as_view()),
    path('internal/v2/billing/statements/drafts/', metering_views.InternalBillingStatementDraftView.as_view()),

    # PLAT-9402 Slice A — the Metering Core read plane (HLD §9). Internal
    # service audience reads any authorized account; the /api/v1/usage/summary
    # subject variant resolves the CALLER'S OWN account server-side (the
    # account id never comes from client input — HLD §2/§2.1).
    path(
        'internal/v1/accounts/<uuid:account_id>/usage',
        metering_read_views.InternalAccountUsageView.as_view(),
    ),
    path(
        'internal/v1/accounts/<uuid:account_id>/usage/',
        metering_read_views.InternalAccountUsageView.as_view(),
    ),
    path(
        'internal/v1/accounts/<uuid:account_id>/entitlements',
        metering_read_views.InternalAccountEntitlementsView.as_view(),
    ),
    path(
        'internal/v1/accounts/<uuid:account_id>/entitlements/',
        metering_read_views.InternalAccountEntitlementsView.as_view(),
    ),
    path(
        'internal/v1/accounts/<uuid:account_id>/spend-balance',
        metering_read_views.InternalAccountSpendBalanceView.as_view(),
    ),
    path(
        'internal/v1/accounts/<uuid:account_id>/spend-balance/',
        metering_read_views.InternalAccountSpendBalanceView.as_view(),
    ),
    path('api/v1/usage/summary', metering_read_views.SubjectUsageSummaryView.as_view()),
    path('api/v1/usage/summary/', metering_read_views.SubjectUsageSummaryView.as_view()),

    # PLAT-4484 — atomic quota reservations + spend leases + writer cutover
    path('internal/v1/quota-reservations', metering_views.InternalQuotaReservationCreateView.as_view()),
    path('internal/v1/quota-reservations/', metering_views.InternalQuotaReservationCreateView.as_view()),
    path(
        'internal/v1/quota-reservations/<uuid:reservation_id>/start',
        metering_views.InternalQuotaReservationStartView.as_view(),
    ),
    path(
        'internal/v1/quota-reservations/<uuid:reservation_id>/start/',
        metering_views.InternalQuotaReservationStartView.as_view(),
    ),
    path(
        'internal/v1/quota-reservations/<uuid:reservation_id>/commit',
        metering_views.InternalQuotaReservationCommitView.as_view(),
    ),
    path(
        'internal/v1/quota-reservations/<uuid:reservation_id>/commit/',
        metering_views.InternalQuotaReservationCommitView.as_view(),
    ),
    path(
        'internal/v1/quota-reservations/<uuid:reservation_id>/release',
        metering_views.InternalQuotaReservationReleaseView.as_view(),
    ),
    path(
        'internal/v1/quota-reservations/<uuid:reservation_id>/release/',
        metering_views.InternalQuotaReservationReleaseView.as_view(),
    ),
    path(
        'internal/v1/quota-reservations/<uuid:reservation_id>/resolve-orphan',
        metering_views.InternalQuotaReservationResolveOrphanView.as_view(),
    ),
    path(
        'internal/v1/quota-reservations/<uuid:reservation_id>/resolve-orphan/',
        metering_views.InternalQuotaReservationResolveOrphanView.as_view(),
    ),
    path('internal/v1/spend-leases', metering_views.InternalSpendLeaseCreateView.as_view()),
    path('internal/v1/spend-leases/', metering_views.InternalSpendLeaseCreateView.as_view()),
    path(
        'internal/v1/spend-leases/<str:lease_id>/close',
        metering_views.InternalSpendLeaseCloseView.as_view(),
    ),
    path(
        'internal/v1/spend-leases/<str:lease_id>/close/',
        metering_views.InternalSpendLeaseCloseView.as_view(),
    ),
    path(
        'internal/v1/admission-writers/<str:product>/cutover',
        metering_views.InternalAdmissionWriterCutoverView.as_view(),
    ),
    path(
        'internal/v1/admission-writers/<str:product>/cutover/',
        metering_views.InternalAdmissionWriterCutoverView.as_view(),
    ),
    path('internal/v1/fleet/capability-evidence', CapabilityEvidenceView.as_view()),
    path('internal/v1/fleet/capability-evidence/', CapabilityEvidenceView.as_view()),
    path('api/internal/company-state/fleet/v1', fleet_views.FleetCompanyStateView.as_view()),
    path('api/internal/company-state/fleet/v1/', fleet_views.FleetCompanyStateView.as_view()),
    path(
        'internal/v1/fleet/lifecycle-availability',
        fleet_views.FleetLifecycleAvailabilityView.as_view(),
    ),
    path(
        'internal/v1/fleet/lifecycle-availability/',
        fleet_views.FleetLifecycleAvailabilityView.as_view(),
    ),
    path(
        'internal/v1/fleet/seat-lifecycle-metrics',
        fleet_views.FleetSeatLifecycleMetricsView.as_view(),
    ),
    path(
        'internal/v1/fleet/seat-lifecycle-metrics/',
        fleet_views.FleetSeatLifecycleMetricsView.as_view(),
    ),
    path('hive/admin/', admin.site.urls),
    path('hive/api/', include('claws.urls')),
    path('hive/api/', include('fleet.urls')),
    path('hive/api/', include('waitlist.urls')),
    path('hive/api/', include('analytics.urls')),
    path('hive/', include('claws.ui_urls')),
    path('hive/', include('fleet.ui_urls')),
    path('hive/', include('waitlist.ui_urls')),
    # Direct paths (without prefix, for in-cluster access)
    path('api/', include('claws.urls')),
    path('api/', include('fleet.urls')),
    path('api/', include('waitlist.urls')),
    path('api/', include('analytics.urls')),
]
