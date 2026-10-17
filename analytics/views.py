"""HIVE-393 analytics ingest endpoint (HLD 89891ffa §5.1 / §6).

`POST /api/analytics/events` — batched, authenticated. The tenant of every event
is authorized against the caller's VERIFIED JWT `organization_memberships`; a
client-declared org the caller doesn't belong to is rejected (403) and nothing is
written (STRIDE §6 Spoofing). Append-only, async-friendly, size-bounded.
"""
import logging

from django.utils.dateparse import parse_datetime
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from .ingest import (
    IngestError, MAX_BATCH, authorize_org, user_ref_for, validate_event,
)
from .models import AnalyticsEvent

logger = logging.getLogger(__name__)


class AnalyticsIngestRateThrottle(SimpleRateThrottle):
    """Per-user+org throttle for authenticated analytics batches (HIVE-399).

    A batch may contain events for more than one org when a multi-org principal
    explicitly names each org. We therefore check and increment one throttle
    bucket per authorized org in the batch. If any bucket is over limit, the
    request is rejected before the view writes anything. Invalid/ambiguous org
    payloads are not throttled here; the view returns the canonical 400/403
    error and preserves the HIVE-381/HIVE-393 batch-atomic invariant.
    """
    scope = 'analytics-ingest'

    def _principal_id(self, request):
        user = getattr(request, 'user', None)
        return getattr(user, 'user_id', None) or getattr(user, 'id', None)

    def _cache_keys(self, request):
        user = getattr(request, 'user', None)
        if not (user and getattr(user, 'is_authenticated', False)):
            return []
        user_id = self._principal_id(request)
        if user_id is None:
            return []
        payload = request.data if isinstance(request.data, dict) else {}
        events = payload.get('events')
        if not isinstance(events, list) or not events or len(events) > MAX_BATCH:
            return []
        org_ids = set()
        try:
            for raw in events:
                proposed_org = (raw or {}).get('org_id') if isinstance(raw, dict) else None
                org_ids.add(authorize_org(user, proposed_org))
        except IngestError:
            return []
        return [
            self.cache_format % {'scope': self.scope, 'ident': f'user:{user_id}:org:{org_id}'}
            for org_id in sorted(org_ids)
        ]

    def get_cache_key(self, request, view):
        # allow_request handles multiple per-org cache keys atomically.
        return None

    def allow_request(self, request, view):
        if self.rate is None:
            return True
        keys = self._cache_keys(request)
        if not keys:
            return True
        self.now = self.timer()
        histories = {}
        for key in keys:
            history = self.cache.get(key, [])
            while history and history[-1] <= self.now - self.duration:
                history.pop()
            if len(history) >= self.num_requests:
                self.history = history
                return self.throttle_failure()
            histories[key] = history
        for key, history in histories.items():
            history.insert(0, self.now)
            self.cache.set(key, history, self.duration)
        self.history = next(iter(histories.values()), [])
        return True


class AnalyticsIngestView(APIView):
    """Accept a batch of analytics events from an authenticated product surface."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [AnalyticsIngestRateThrottle]

    def post(self, request):
        user = request.user
        user_id = getattr(user, 'user_id', None) or getattr(user, 'id', None)
        if user_id is None:
            return Response({'error': 'no authenticated principal'}, status=status.HTTP_401_UNAUTHORIZED)

        payload = request.data if isinstance(request.data, dict) else {}
        events = payload.get('events')
        if not isinstance(events, list) or not events:
            return Response({'error': 'events (non-empty list) required'}, status=status.HTTP_400_BAD_REQUEST)
        if len(events) > MAX_BATCH:
            return Response({'error': f'batch too large (max {MAX_BATCH})'}, status=status.HTTP_400_BAD_REQUEST)

        rows = []
        for raw in events:
            try:
                # org_id is AUTHORIZED against the session's verified memberships —
                # the client value is a proposal, never trusted (ADR-0013 dec.2).
                org_id = authorize_org(user, (raw or {}).get('org_id') if isinstance(raw, dict) else None)
                fields = validate_event(raw)
            except IngestError as exc:
                # Reject the WHOLE batch on any bad/forged event — no partial writes,
                # so a cross-tenant attempt can never smuggle one event through.
                return Response({'error': str(exc.message)}, status=exc.status)

            ts = None
            if isinstance(raw, dict) and raw.get('ts'):
                ts = parse_datetime(str(raw.get('ts')))
            if ts is None:
                ts = timezone.now()

            session_id = raw.get('session_id') if isinstance(raw, dict) else None

            rows.append(AnalyticsEvent(
                org_id=org_id,
                user_ref=user_ref_for(user_id, org_id),
                surface=fields['surface'],
                event_type=fields['event_type'],
                target=fields['target'],
                session_id=session_id or None,
                ts=ts,
                props=fields['props'],
            ))

        AnalyticsEvent.objects.bulk_create(rows)
        return Response({'accepted': len(rows)}, status=status.HTTP_202_ACCEPTED)


# ── HIVE-394: feedback intake (HLD §5.2) — funnels ≥2 channels into feedback_event ──

from .feedback import ingest_support_ticket, record_feedback_event  # noqa: E402
from .models import FeedbackEvent  # noqa: E402


def _principal_id(user):
    return getattr(user, 'user_id', None) or getattr(user, 'id', None)


class FeedbackWidgetView(APIView):
    """POST /api/feedback/widget — end-user in-product feedback. The tenant is
    AUTHORIZED against the caller's verified memberships (authorize_org); a
    client-proposed org the caller doesn't belong to is rejected 403."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'discovery-feedback-ingest'

    def post(self, request):
        user = request.user
        user_id = _principal_id(user)
        if user_id is None:
            return Response({'error': 'no authenticated principal'}, status=status.HTTP_401_UNAUTHORIZED)
        payload = request.data if isinstance(request.data, dict) else {}
        text = (payload.get('text') or '').strip()
        if not text:
            return Response({'error': 'text required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            org_id = authorize_org(user, payload.get('org_id'))
            event, created = record_feedback_event(
                org_id=org_id, source='feedback_widget',
                source_ref=(payload.get('client_ref') or '').strip(),
                text=text, user_id=user_id,
                surface=payload.get('surface') or '',
                meta=payload.get('meta') if isinstance(payload.get('meta'), dict) else {},
            )
        except IngestError as exc:
            return Response({'error': str(exc.message)}, status=exc.status)
        return Response(
            {'event_id': str(event.event_id), 'created': created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class FeedbackSupportIngestView(APIView):
    """POST /api/feedback/support — trusted support-service adapter (HIVE-322).
    Staff/service identity only; org_id is the ticket's customer org (a trusted
    server attribution, distinct from an untrusted end-user client)."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'discovery-feedback-ingest'

    def post(self, request):
        user = request.user
        if not (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False)):
            return Response({'error': 'support-ticket ingest is service-only'}, status=status.HTTP_403_FORBIDDEN)
        payload = request.data if isinstance(request.data, dict) else {}
        org_id = payload.get('org_id')
        ticket_id = payload.get('ticket_id')
        if not org_id or not ticket_id:
            return Response({'error': 'org_id and ticket_id required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            event, created = ingest_support_ticket(
                org_id=int(org_id), ticket_id=ticket_id, body=payload.get('body', ''),
                reporter_id=payload.get('reporter_id'),
                surface=payload.get('surface') or '',
                meta=payload.get('meta') if isinstance(payload.get('meta'), dict) else {},
            )
        except (IngestError, ValueError) as exc:
            msg = getattr(exc, 'message', str(exc))
            return Response({'error': str(msg)}, status=getattr(exc, 'status', 400))
        return Response(
            {'event_id': str(event.event_id), 'created': created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class FeedbackListView(APIView):
    """GET /api/feedback — org-scoped read of the caller's OWN tenant feedback
    (Model-A). Never serves another tenant's raw feedback; platform-wide
    aggregation (counts + anonymized exemplars only, §6) is the clustering surface."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            org_id = authorize_org(request.user, request.query_params.get('org_id'))
        except IngestError as exc:
            return Response({'error': str(exc.message)}, status=exc.status)
        qs = FeedbackEvent.objects.filter(org_id=org_id).order_by('-ts')
        source = request.query_params.get('source')
        if source:
            qs = qs.filter(source=source)
        rows = [{
            'event_id': str(e.event_id), 'source': e.source, 'source_ref': e.source_ref,
            'user_ref': e.user_ref, 'surface': e.surface, 'text': e.text,
            'sentiment': e.sentiment, 'theme_id': e.theme_id,
            'ts': e.ts.isoformat(), 'meta': e.meta,
        } for e in qs[:200]]
        return Response({'results': rows})
