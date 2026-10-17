"""HIVE-395 event-driven feedback clustering + emerging-issue alert hook.

This is the slice-3 core that turns normalized FeedbackEvent rows into Theme
rows. The production agent can swap the deterministic classifier for Cortex/GLM
labels later; the isolation contract is enforced here already: platform-wide
(cross-tenant) themes expose only counts + anonymized exemplars, never raw
feedback text from any tenant.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable

from django.core.cache import cache
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .models import FeedbackEvent, Theme

CLUSTERING_WATERMARK_KEY = 'hive395:feedback-clustering-watermark'

NEGATIVE_WORDS = {
    'bad', 'broken', 'bug', 'crash', 'error', 'failed', 'failing', 'friction',
    'hard', 'issue', 'missing', 'pain', 'slow', 'stuck', 'terrible', 'timeout',
}
POSITIVE_WORDS = {'good', 'great', 'love', 'fast', 'helpful', 'clear', 'works'}
TOPIC_KEYWORDS = [
    ('login/authentication friction', {'auth', 'login', 'signin', 'password', 'session', 'token'}),
    ('performance or latency issue', {'slow', 'latency', 'timeout', 'lag', 'delay', 'performance'}),
    ('crash or error surfaced', {'crash', 'error', 'failed', 'failure', '500', 'exception'}),
    ('billing or pricing confusion', {'billing', 'price', 'pricing', 'invoice', 'payment'}),
    ('navigation or onboarding friction', {'navigation', 'onboarding', 'confusing', 'lost', 'hard'}),
]


@dataclass(frozen=True)
class ThemeAlert:
    """Payload for the existing alert→on-call adapter.

    The caller wires this to Pulse item creation + Connect DM to the surface PM;
    this module deliberately does not invent a new notification plane.
    """
    theme_id: int
    surface: str
    label: str
    severity: str
    member_count: int
    scope: str


def mark_feedback_watermark(delta: int = 1) -> None:
    """Event-driven trigger: feedback ingest increments this watermark.

    A worker/agent calls process_feedback_watermark when it observes the counter.
    Fail-open: cache outages must never break the user feedback path.
    """
    try:
        cache.add(CLUSTERING_WATERMARK_KEY, 0, timeout=None)
        cache.incr(CLUSTERING_WATERMARK_KEY, delta)
    except Exception:
        pass


def consume_feedback_watermark() -> int:
    try:
        value = int(cache.get(CLUSTERING_WATERMARK_KEY) or 0)
        cache.set(CLUSTERING_WATERMARK_KEY, 0, timeout=None)
        return value
    except Exception:
        return 0


def _tokens(text: str) -> set[str]:
    return set(re.findall(r'[a-z0-9]+', (text or '').lower()))


def classify_feedback(text: str) -> tuple[str, str]:
    tokens = _tokens(text)
    for label, keys in TOPIC_KEYWORDS:
        if tokens & keys:
            break
    else:
        label = 'general product feedback'
    neg = len(tokens & NEGATIVE_WORDS)
    pos = len(tokens & POSITIVE_WORDS)
    if neg and pos:
        sentiment = 'mixed'
    elif neg:
        sentiment = 'negative'
    elif pos:
        sentiment = 'positive'
    else:
        sentiment = 'neutral'
    return label, sentiment


def anonymized_exemplar(label: str, surface: str) -> str:
    surface_part = f' on {surface}' if surface else ''
    return f'Anonymized exemplar: {label}{surface_part}'


def _severity(member_count: int, sentiments: Iterable[str]) -> str:
    counts = Counter(sentiments)
    if member_count >= 5 or counts['negative'] >= 3:
        return 'high'
    if member_count >= 3 or counts['negative'] >= 1:
        return 'medium'
    return 'normal'


def _trend(sentiments: Iterable[str]) -> str:
    counts = Counter(sentiments)
    if counts['negative'] and counts['positive']:
        return 'mixed'
    if counts['negative']:
        return 'negative'
    if counts['positive']:
        return 'positive'
    return 'neutral'


def _upsert_theme(*, scope, org_id, surface, label, events, sentiments) -> tuple[Theme, bool]:
    first_seen = min(e.ts for e in events)
    last_seen = max(e.ts for e in events)
    member_count = len(events)
    theme, created = Theme.objects.update_or_create(
        scope=scope, org_id=org_id, surface=surface or '', label=label,
        defaults={
            'summary': f'{member_count} feedback signal(s) clustered as {label}',
            'member_count': member_count,
            'sentiment_trend': _trend(sentiments),
            'velocity': float(member_count),
            'severity': _severity(member_count, sentiments),
            'first_seen': first_seen,
            'last_seen': last_seen,
            'anonymized_exemplars': [anonymized_exemplar(label, surface or '')],
        },
    )
    return theme, created


def _cumulative_platform_groups() -> dict[tuple[str, str], list[tuple[FeedbackEvent, str]]]:
    """Group all clustered feedback for platform materialization.

    Org clustering is event-driven and processes only unassigned rows, but the
    platform theme is cumulative: matching feedback from org A and org B can
    arrive in separate watermark runs. Rebuild platform candidates from all
    already-org-clustered events so the second run can materialize the
    cross-tenant aggregate without exposing raw tenant text.
    """
    platform_groups = defaultdict(list)
    for event in FeedbackEvent.objects.filter(theme_id__isnull=False).order_by('ts'):
        label, sentiment = classify_feedback(event.text)
        platform_groups[(event.surface or '', label)].append((event, sentiment))
    return platform_groups


@transaction.atomic
def cluster_unassigned_feedback(*, limit: int = 500, alert_callback: Callable[[ThemeAlert], None] | None = None) -> dict:
    """Cluster unassigned feedback rows into org + platform Theme rows.

    Returns counters for the agent run. Platform themes are materialized only
    when the same label/surface appears in >=2 orgs; they carry counts and
    anonymized exemplars only. Raw feedback remains reachable solely via the
    per-org FeedbackEvent rows and their org-scoped Theme.
    """
    rows = list(FeedbackEvent.objects.filter(theme_id__isnull=True).order_by('ts')[:limit])
    if not rows:
        return {'processed': 0, 'org_themes': 0, 'platform_themes': 0, 'alerts': 0}

    grouped = defaultdict(list)
    sentiments_by_event = {}
    for event in rows:
        label, sentiment = classify_feedback(event.text)
        sentiments_by_event[event.id] = sentiment
        grouped[(event.org_id, event.surface or '', label)].append(event)

    org_themes = []
    for (org_id, surface, label), events in grouped.items():
        sentiments = [sentiments_by_event[e.id] for e in events]
        theme, _ = _upsert_theme(scope='org', org_id=org_id, surface=surface, label=label,
                                 events=events, sentiments=sentiments)
        FeedbackEvent.objects.filter(id__in=[e.id for e in events]).update(
            theme_id=theme.id,
            sentiment=_trend(sentiments),
        )
        org_themes.append((theme, events, sentiments))

    platform_groups = _cumulative_platform_groups()

    alerts = 0
    platform_count = 0
    for (surface, label), event_sentiments in platform_groups.items():
        orgs = {event.org_id for event, _ in event_sentiments}
        if len(orgs) < 2:
            continue
        events = [event for event, _ in event_sentiments]
        sentiments = [sentiment for _, sentiment in event_sentiments]
        platform_theme, created = _upsert_theme(scope='platform', org_id=None, surface=surface, label=label,
                                                events=events, sentiments=sentiments)
        platform_count += 1
        if created and platform_theme.severity in {'medium', 'high'} and alert_callback:
            alert_callback(ThemeAlert(
                theme_id=platform_theme.id,
                surface=platform_theme.surface,
                label=platform_theme.label,
                severity=platform_theme.severity,
                member_count=platform_theme.member_count,
                scope=platform_theme.scope,
            ))
            alerts += 1

    return {'processed': len(rows), 'org_themes': len(org_themes), 'platform_themes': platform_count, 'alerts': alerts}


def process_feedback_watermark(*, alert_callback: Callable[[ThemeAlert], None] | None = None,
                               limit: int = 500) -> dict:
    """Agent job entrypoint: process only when ingest advanced the watermark.

    The hourly safety-net may call cluster_unassigned_feedback directly; this is
    the event-driven path required by HLD §5.3. A single watermark can represent
    more feedback rows than one clustering page, so drain all currently pending
    rows before clearing the event-driven run; otherwise the tail would remain
    unassigned until unrelated new feedback re-armed the watermark.
    """
    watermark = consume_feedback_watermark()
    if watermark <= 0:
        return {'processed': 0, 'org_themes': 0, 'platform_themes': 0, 'alerts': 0, 'watermark': 0}

    totals = {'processed': 0, 'org_themes': 0, 'platform_themes': 0, 'alerts': 0}
    while True:
        batch = cluster_unassigned_feedback(limit=limit, alert_callback=alert_callback)
        for key in totals:
            totals[key] += batch[key]
        if batch['processed'] == 0 or not FeedbackEvent.objects.filter(theme_id__isnull=True).exists():
            break

    totals['watermark'] = watermark
    return totals
