"""HIVE-393 (HLD 89891ffa §4, ADR-0013): the `analytics_event` v1 store.

Append-only product-analytics events. `org_id` is the tenant key and is ALWAYS
server-derived/authorized (never client-declared) — see analytics/ingest.py. The
HLD calls for monthly range-partitioning of this table for retention/scale; that
is a follow-on raw-SQL migration (the ORM shape here is partition-compatible).
"""
import uuid

from django.db import models

SURFACES = [('home', 'home'), ('hive', 'hive'), ('pulse', 'pulse'), ('admin', 'admin')]
EVENT_TYPES = [
    ('page_view', 'page_view'),
    ('feature_use', 'feature_use'),
    ('drop_off', 'drop_off'),
    ('error_surface', 'error_surface'),
    ('journey_step', 'journey_step'),
    ('journey_complete', 'journey_complete'),
]


class AnalyticsEvent(models.Model):
    schema_version = models.IntegerField(default=1)
    event_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    # Tenant key — server-derived from the authenticated session (ADR-0013 dec.2).
    org_id = models.BigIntegerField(db_index=True)
    # Pseudonymous, per-(user,org) stable hash — no raw PII.
    user_ref = models.CharField(max_length=64)
    surface = models.CharField(max_length=16, choices=SURFACES)
    event_type = models.CharField(max_length=24, choices=EVENT_TYPES)
    target = models.TextField(blank=True, default='')
    session_id = models.UUIDField(null=True, blank=True)
    ts = models.DateTimeField(db_index=True)
    props = models.JSONField(default=dict, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analytics_event'
        indexes = [
            models.Index(fields=['org_id', 'ts'], name='ae_org_ts_idx'),
            models.Index(fields=['org_id', 'surface', 'event_type'], name='ae_org_surface_type_idx'),
        ]

    def __str__(self):
        return f'AnalyticsEvent<{self.surface}/{self.event_type} org={self.org_id}>'


FEEDBACK_SOURCES = [
    ('support_ticket', 'support_ticket'),    # HIVE-322 customer-support tickets
    ('feedback_widget', 'feedback_widget'),  # in-product widget
    ('connect', 'connect'),                  # Connect message (scope: HLD §7 Q2)
    ('glitchtip', 'glitchtip'),              # GlitchTip error/session signal
]
SENTIMENTS = [
    ('positive', 'positive'), ('neutral', 'neutral'),
    ('negative', 'negative'), ('mixed', 'mixed'),
]


class FeedbackEvent(models.Model):
    """feedback_event v1 (HLD 89891ffa §4, HIVE-394 slice 2).

    Every feedback channel funnels into this ONE tenant-scoped table so slice 3's
    clustering job reads a single source. Shares the analytics_event security model:
    ``org_id`` is server-derived (analytics.ingest.authorize_org), ``user_ref`` is a
    pseudonymous per-(user,org) hash (analytics.ingest.user_ref_for). Additive
    fields don't bump ``schema_version``; a removal/rename/semantic change does.

    Security (HLD §6): ``text`` MAY contain PII → tenant-scoped, access-controlled,
    erasure-eligible. ``sentiment``/``theme_id`` are null on ingest and written ONLY
    by the clustering job (slice 3) — never by the ingest path.
    """
    schema_version = models.IntegerField(default=1)
    event_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    # Tenant key — server-derived from the authenticated session (ADR-0013 dec.2).
    org_id = models.BigIntegerField(db_index=True)
    source = models.CharField(max_length=32, choices=FEEDBACK_SOURCES)
    # Upstream id (ticket / message / GlitchTip issue) — dedup + backlink. Empty
    # only for sources with no stable upstream id.
    source_ref = models.TextField(blank=True, default='')
    user_ref = models.CharField(max_length=64, blank=True, default='')
    surface = models.CharField(max_length=16, choices=SURFACES, blank=True, default='')
    # Free text — MAY contain PII. Tenant-scoped, access-controlled, erasure-eligible.
    text = models.TextField(blank=True, default='')
    # Clustering-job-owned (slice 3); null on ingest.
    sentiment = models.CharField(max_length=16, choices=SENTIMENTS, null=True, blank=True)
    # Soft ref to the (analytics-owned, slice 3) theme table; nullable bigint, not a
    # hard FK, so slice 2 doesn't couple to slice 3's theme schema.
    theme_id = models.BigIntegerField(null=True, blank=True)
    ts = models.DateTimeField(auto_now_add=True, db_index=True)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'feedback_event'
        indexes = [
            models.Index(fields=['org_id', '-ts'], name='fe_org_ts_idx'),
            models.Index(fields=['source'], name='fe_source_idx'),
            models.Index(fields=['theme_id'], name='fe_theme_idx'),
        ]
        constraints = [
            # Tenant-scoped idempotency (HLD §5.2): a (source, source_ref) pair
            # ingests once per org. `feedback_widget.client_ref` is
            # client-controlled, so global source_ref dedupe would leak/suppress
            # cross-tenant feedback.
            models.UniqueConstraint(
                fields=['org_id', 'source', 'source_ref'],
                condition=~models.Q(source_ref=''),
                name='uniq_feedback_org_source_ref',
            ),
        ]

    def __str__(self):
        return f'FeedbackEvent<{self.source}:{self.source_ref or self.event_id} org={self.org_id}>'


THEME_SCOPES = [('org', 'org'), ('platform', 'platform')]
THEME_STATUSES = [
    ('new', 'new'), ('triaged', 'triaged'),
    ('in_discovery', 'in_discovery'), ('resolved', 'resolved'),
]


class Theme(models.Model):
    """HIVE-395 clustering output (HLD 89891ffa §4/§6).

    Per-org themes may point back to raw tenant-scoped feedback via
    FeedbackEvent.theme_id. Platform-wide themes are the cross-tenant surface:
    they MUST contain only counts + anonymized exemplars/summary — never another
    tenant's raw feedback text.
    """
    scope = models.CharField(max_length=16, choices=THEME_SCOPES)
    org_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    label = models.CharField(max_length=160)
    summary = models.TextField(blank=True, default='')
    surface = models.CharField(max_length=16, choices=SURFACES, blank=True, default='')
    member_count = models.IntegerField(default=0)
    sentiment_trend = models.CharField(max_length=16, choices=SENTIMENTS, default='neutral')
    velocity = models.FloatField(default=0.0)
    severity = models.CharField(max_length=16, default='normal')
    first_seen = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=THEME_STATUSES, default='new')
    backlog_ref = models.CharField(max_length=64, blank=True, default='')
    anonymized_exemplars = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'theme'
        indexes = [
            models.Index(fields=['scope', 'org_id', 'surface'], name='theme_scope_org_surface_idx'),
            models.Index(fields=['scope', 'label'], name='theme_scope_label_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['scope', 'org_id', 'surface', 'label'],
                name='uniq_theme_scope_org_surface_label',
            ),
        ]

    def __str__(self):
        return f'Theme<{self.scope}:{self.label} org={self.org_id}>'
