"""HIVE-396 discovery backlog materialization from clustered Theme rows.

This module is the HIVE-381 slice-4 bridge: it turns analytics-owned Theme rows
into Product-owned Pulse discovery items. The invariant is intentionally strict:
only concrete Theme rows may create discovery items, and every created Pulse item
carries the theme id/count/anonymized evidence contract before Theme.backlog_ref
is written.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import requests
from django.conf import settings
from django.db import transaction

from .models import Theme

DEFAULT_PROJECT_ID = getattr(settings, 'HIVE_DISCOVERY_BACKLOG_PROJECT_ID', 9)
DEFAULT_PARENT_ITEM_ID = getattr(settings, 'HIVE_DISCOVERY_BACKLOG_PARENT_ITEM_ID', 'HIVE-396')

SEVERITY_WEIGHTS = {'high': 50, 'medium': 30, 'normal': 10, 'low': 0}
SENTIMENT_WEIGHTS = {'negative': 15, 'mixed': 8, 'neutral': 0, 'positive': -5}

QUESTION_BY_LABEL = {
    'login/authentication friction': 'Which auth/session failure blocks activation or task progress, and where does the user recover?',
    'billing or pricing confusion': 'Which financial/cost label, snapshot, or CTA confuses users enough to block action?',
    'navigation or onboarding friction': 'Which top-5 action is least findable from the command center/onboarding path?',
}


@dataclass(frozen=True)
class PulseTaskResult:
    item_key: str


@dataclass(frozen=True)
class UrgentDiscoveryAlert:
    theme_id: int
    item_key: str
    rank_score: int
    surface: str
    label: str
    recipient_username: str
    message: str


class PulseMaterializationError(RuntimeError):
    """Raised when Pulse rejects discovery item creation."""


def rank_score(theme: Theme) -> int:
    """Return the HIVE-396 v0.1 rank score for a Theme row."""
    severity = SEVERITY_WEIGHTS.get((theme.severity or '').lower(), 10)
    frequency = min(max(theme.member_count or 0, 0), 20) * 2
    velocity = min(max(float(theme.velocity or 0), 0.0), 10.0) * 3
    sentiment = SENTIMENT_WEIGHTS.get((theme.sentiment_trend or '').lower(), 0)
    recency = 0
    if theme.last_seen:
        from django.utils import timezone

        age_days = (timezone.now() - theme.last_seen).days
        if age_days <= 7:
            recency = 10
        elif age_days <= 30:
            recency = 5
    scope = 10 if theme.scope == 'platform' else 0
    return int(severity + frequency + velocity + sentiment + recency + scope)


def priority_for_score(score: int) -> str:
    if score >= 90:
        return 'urgent'
    if score >= 70:
        return 'high'
    if score >= 40:
        return 'normal'
    return 'low'


def assignment_for_theme(theme: Theme) -> dict:
    """Return valid Pulse routing fields for a Theme.

    Until HIVE-380 exposes a machine-readable trio roster, the resolver must not
    pretend that an arbitrary PM name is a Pulse assignment group. Route to the
    Product team and leave an auditable label for unresolved surfaces.
    """
    known_surfaces = {'home', 'hive', 'pulse', 'admin'}
    labels = ['hive-396', 'discovery-backlog', 'generated-from-theme', f'surface-{theme.surface or "unknown"}']
    if not theme.surface or theme.surface not in known_surfaces:
        labels.append('needs-surface-owner')
    return {'assignment_group': 'product', 'labels': labels}


def discovery_item_description(theme: Theme, score: int) -> str:
    exemplars = theme.anonymized_exemplars or []
    exemplar_lines = '\n'.join(f'- {item}' for item in exemplars[:3]) or '- (none recorded)'
    question = QUESTION_BY_LABEL.get(
        theme.label,
        f'What user problem does the {theme.surface or "unknown"} / {theme.label} theme represent, and should the trio act on it?',
    )
    org_scope = 'platform-wide aggregate' if theme.scope == 'platform' else f'org-scoped theme for org {theme.org_id}'
    return (
        f'Materialized HIVE-396 discovery backlog item from concrete Theme `{theme.id}`.\n\n'
        f'**Theme:** `{theme.id}` ({org_scope})  \n'
        f'**Surface:** `{theme.surface or "unknown"}`  \n'
        f'**Label:** `{theme.label}`  \n'
        f'**Member count:** {theme.member_count}  \n'
        f'**Severity:** `{theme.severity}`  \n'
        f'**Sentiment trend:** `{theme.sentiment_trend}`  \n'
        f'**Velocity:** {theme.velocity}  \n'
        f'**Rank score:** {score}\n\n'
        f'**Summary:** {theme.summary}\n\n'
        '**Anonymized exemplars (no raw cross-tenant feedback):**\n'
        f'{exemplar_lines}\n\n'
        f'**Product question:** {question}\n\n'
        '**First trio action:**\n'
        '- Inspect the anonymized theme evidence and any linked org-scoped supporting data available to the owning trio.\n'
        '- Decide: validated opportunity, not-a-problem, needs-more-data, or blocked.\n'
        '- If validated, create/link the design or implementation work and record the outcome here.\n\n'
        '**Privacy invariant:** Product/backlog views use theme counts, summaries, and anonymized exemplars only; raw feedback text never appears in this item.\n\n'
        '**Backlink contract:** after Pulse creates this item, the materializer writes this Pulse key to `theme.backlog_ref` so reruns are idempotent.\n\n'
        'Parent spec: wiki `a2bbd443-bd85-4a8b-ab9d-9394f7673591`; source task HIVE-396.'
    )


def payload_for_theme(theme: Theme, *, project_id: int = DEFAULT_PROJECT_ID, parent_item_id: str = DEFAULT_PARENT_ITEM_ID) -> dict:
    score = rank_score(theme)
    routing = assignment_for_theme(theme)
    return {
        'title': f'[Discovery][{theme.surface or "unknown"}] {theme.label}',
        'description': discovery_item_description(theme, score),
        'priority': priority_for_score(score),
        'project_id': project_id,
        'workflow': 'feature-request-rfc',
        # REST Pulse API historically accepts workflow_name; MCP accepts workflow.
        'workflow_name': 'feature-request-rfc',
        'parent_item_id': parent_item_id,
        'assignment_group': routing['assignment_group'],
        'labels': routing['labels'],
        'tags': [theme.label],
    }



def surface_pm_username(theme: Theme) -> str:
    mapping = getattr(settings, 'HIVE_DISCOVERY_SURFACE_PM_USERNAMES', None) or {}
    if isinstance(mapping, dict):
        value = mapping.get(theme.surface or '') or mapping.get('default')
        if value:
            return str(value).lstrip('@')
    return 'nao'


def urgent_alert_for_theme(theme: Theme, item_key: str, score: int) -> UrgentDiscoveryAlert:
    recipient = surface_pm_username(theme)
    surface = theme.surface or 'unknown'
    message = (
        f'🚨 Urgent discovery theme materialized: [{surface}] {theme.label}\n\n'
        f'Pulse item: {item_key}\n'
        f'Theme: {theme.id}; rank_score={score}; severity={theme.severity}; '
        f'member_count={theme.member_count}; sentiment={theme.sentiment_trend}.\n\n'
        'Please acknowledge in the discovery backlog and pull/reject in the next product cadence. '
        'This notification contains only theme metadata and anonymized summary context, not raw feedback text.'
    )
    return UrgentDiscoveryAlert(
        theme_id=theme.id,
        item_key=item_key,
        rank_score=score,
        surface=surface,
        label=theme.label,
        recipient_username=recipient,
        message=message,
    )


def send_connect_urgent_dm(alert: UrgentDiscoveryAlert) -> None:
    """Send the HIVE-381 emerging-issue push through the existing Connect DM plane."""
    base = (getattr(settings, 'SHIZUHA_CONNECT_INTERNAL_URL', '') or getattr(settings, 'SHIZUHA_PLATFORM_URL', '')).rstrip('/')
    if not base:
        raise PulseMaterializationError('SHIZUHA_CONNECT_INTERNAL_URL/SHIZUHA_PLATFORM_URL is not configured')
    token = getattr(settings, 'HIVE_CONNECT_API_TOKEN', '') or getattr(settings, 'HIVE_PULSE_API_TOKEN', '') or getattr(settings, 'HIVE_ID_FLEET_PROVISIONER_TOKEN', '')
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    resp = requests.post(
        f'{base}/connect/api/messaging/dm/',
        json={'recipient_username': alert.recipient_username, 'content': alert.message},
        headers=headers,
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        raise PulseMaterializationError(f'Connect urgent discovery DM failed ({resp.status_code}): {resp.text[:300]}')


def _extract_task_key(response_payload: dict) -> str:
    for key in ('item_key', 'task_key', 'key'):
        value = response_payload.get(key)
        if value:
            return str(value)
    item = response_payload.get('item') or response_payload.get('task') or {}
    for key in ('item_key', 'task_key', 'key'):
        value = item.get(key) if isinstance(item, dict) else None
        if value:
            return str(value)
    raise PulseMaterializationError(f'Pulse response did not include task key: {response_payload!r}')


def create_pulse_task(payload: dict) -> PulseTaskResult:
    """Create a Pulse task via the internal Pulse API."""
    base = getattr(settings, 'HIVE_PULSE_API_URL', '').rstrip('/')
    if not base:
        raise PulseMaterializationError('HIVE_PULSE_API_URL is not configured')
    token = getattr(settings, 'HIVE_PULSE_API_TOKEN', '') or getattr(settings, 'HIVE_ID_FLEET_PROVISIONER_TOKEN', '')
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    resp = requests.post(f'{base}/tasks/', json=payload, headers=headers, timeout=15)
    if resp.status_code not in (200, 201):
        raise PulseMaterializationError(f'Pulse task create failed ({resp.status_code}): {resp.text[:300]}')
    return PulseTaskResult(item_key=_extract_task_key(resp.json() or {}))


def materialize_discovery_backlog(
    *,
    create_task: Callable[[dict], PulseTaskResult] = create_pulse_task,
    urgent_notifier: Callable[[UrgentDiscoveryAlert], None] | None = send_connect_urgent_dm,
    project_id: int = DEFAULT_PROJECT_ID,
    parent_item_id: str = DEFAULT_PARENT_ITEM_ID,
    limit: int = 100,
) -> dict:
    """Create Pulse discovery items for concrete, unbacklinked Theme rows."""
    urgent_alerts: list[UrgentDiscoveryAlert] = []
    created = []
    with transaction.atomic():
        themes = list(
            Theme.objects
            .select_for_update()
            .filter(status__in=('new', 'triaged'), backlog_ref='')
            .order_by('id')
        )
        ranked = sorted(themes, key=rank_score, reverse=True)[:limit]
        for theme in ranked:
            payload = payload_for_theme(theme, project_id=project_id, parent_item_id=parent_item_id)
            result = create_task(payload)
            if not result.item_key:
                raise PulseMaterializationError(f'create_task returned no item_key for theme {theme.id}')
            score = rank_score(theme)
            theme.backlog_ref = result.item_key
            theme.save(update_fields=['backlog_ref', 'updated_at'])
            if score >= 90 and urgent_notifier:
                urgent_alerts.append(urgent_alert_for_theme(theme, result.item_key, score))
            created.append({'theme_id': theme.id, 'item_key': result.item_key, 'rank_score': score})

    for alert in urgent_alerts:
        urgent_notifier(alert)
    return {'created': len(created), 'items': created}
