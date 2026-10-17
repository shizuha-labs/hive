"""Authenticated internal HTTP surface for Metering Core event ingestion + reservations."""
import hmac

from django.conf import settings
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .metering_events import (
    MeteringEventConflict,
    MeteringEventError,
    ingest_usage_cloudevent,
    ingest_usage_cloudevents,
)
from .metering_reservations import (
    ReservationError,
    close_spend_lease,
    commit_reservation,
    cutover_admission_writer,
    issue_spend_lease,
    release_reservation,
    reserve_quota,
    resolve_orphaned_hold,
    start_reservation,
)
from datetime import datetime

from django.utils.dateparse import parse_datetime

from .metering_core import MeteringCoreError, resolve_default_account
from .metering_models import BillingAccount, BillingStatement
from .metering_statements import (
    PeriodLagError,
    StatementError,
    StatementStateError,
    begin_closing,
    export_statement_draft,
    finalize_statement,
    open_adjustment_statement,
    open_or_get_statement,
    rate_statement,
)


class CloudEventJSONParser(JSONParser):
    media_type = 'application/cloudevents+json'


class CloudEventBatchJSONParser(JSONParser):
    media_type = 'application/cloudevents-batch+json'


def _authenticated(request) -> bool:
    expected = str(getattr(settings, 'METERING_CORE_SERVICE_TOKEN', '') or '').strip()
    if not expected:
        expected = str(getattr(settings, 'HIVE_INTERNAL_AUTH_TOKEN', '') or '').strip()
    if not expected:
        return False
    supplied = str(request.META.get('HTTP_X_HIVE_INTERNAL_TOKEN', '') or '').strip()
    authorization = str(request.META.get('HTTP_AUTHORIZATION', '') or '')
    if authorization.startswith('Bearer '):
        supplied = authorization[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


class _UsageEventIngestBase(APIView):
    authentication_classes = []
    permission_classes = []
    parser_classes = [CloudEventJSONParser, CloudEventBatchJSONParser, JSONParser]

    def _auth_error(self, request):
        if _authenticated(request):
            return None
        return Response(
            {'error': 'internal_service_authentication_required'},
            status=status.HTTP_403_FORBIDDEN,
        )

    @staticmethod
    def _event_error(exc):
        response_status = (
            status.HTTP_409_CONFLICT
            if isinstance(exc, MeteringEventConflict)
            else status.HTTP_400_BAD_REQUEST
        )
        return Response(exc.as_dict(), status=response_status)


class InternalUsageEventIngestView(_UsageEventIngestBase):
    """POST /internal/v1/usage/events — one CloudEvents 1.0 usage fact."""

    def post(self, request):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        try:
            result = ingest_usage_cloudevent(request.data)
        except MeteringEventError as exc:
            return self._event_error(exc)
        return Response(
            result.as_dict(),
            status=status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED,
        )


class InternalUsageEventBatchIngestView(_UsageEventIngestBase):
    """POST /internal/v1/usage/events:batch — atomic CloudEvents JSON batch."""

    def post(self, request):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        try:
            results = ingest_usage_cloudevents(request.data)
        except MeteringEventError as exc:
            return self._event_error(exc)
        replay_only = all(result.replayed for result in results)
        return Response(
            {
                'status': 'replayed' if replay_only else 'accepted',
                'results': [result.as_dict() for result in results],
            },
            status=status.HTTP_200_OK if replay_only else status.HTTP_201_CREATED,
        )


class _ReservationBase(APIView):
    authentication_classes = []
    permission_classes = []

    def _auth_error(self, request):
        if _authenticated(request):
            return None
        return Response(
            {'error': 'internal_service_authentication_required'},
            status=status.HTTP_403_FORBIDDEN,
        )

    @staticmethod
    def _idem_key(request) -> str:
        return str(
            request.headers.get('Idempotency-Key')
            or request.META.get('HTTP_IDEMPOTENCY_KEY')
            or request.data.get('idempotency_key')
            or ''
        ).strip()

    @staticmethod
    def _reservation_error(exc: ReservationError):
        return Response(exc.as_dict(), status=exc.http_status)


class InternalAccountResolveView(_ReservationBase):
    """POST /internal/v1/accounts:resolve

    R1B (PLAT-7006): server-derived get-or-create of an owner's DEFAULT
    BillingAccount.  The authenticated producer names the owner subject; the
    core derives the canonical payer — a body-supplied account id is never
    trusted as the resolution target (HLD §2/§2.1).  Idempotent: the same
    owner resolves to the same account on every call.
    """

    def post(self, request):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        data = request.data if isinstance(request.data, dict) else {}
        account_type = str(data.get('account_type') or '').strip()
        owner_user_id = data.get('owner_user_id')
        owner_org_id = str(data.get('owner_org_id') or '').strip()
        try:
            if owner_user_id not in (None, ''):
                owner_user_id = int(owner_user_id)
            else:
                owner_user_id = None
        except (TypeError, ValueError):
            return Response(
                {'error': 'invalid_owner', 'detail': 'owner_user_id must be an integer.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            account = resolve_default_account(
                account_type=account_type,
                owner_user_id=owner_user_id,
                owner_org_id=owner_org_id,
                display_name=str(data.get('display_name') or ''),
            )
        except MeteringCoreError as exc:
            return Response(
                {'error': 'resolve_failed', 'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:  # noqa: BLE001 — map unexpected to 500
            return Response(
                {'error': 'resolve_failed', 'detail': str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            {
                'billing_account_id': str(account.id),
                'account_type': account.account_type,
                'currency': account.currency,
                'collection_status': account.collection_status,
                'admission_epoch': account.admission_epoch,
                'policy_generation': account.policy_generation,
                'created': account.created_at.isoformat().replace('+00:00', 'Z'),
            },
            status=status.HTTP_200_OK,
        )


class InternalQuotaReservationCreateView(_ReservationBase):
    """POST /internal/v1/quota-reservations"""

    def post(self, request):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = reserve_quota(
                idempotency_key=self._idem_key(request),
                meter=str(data.get('meter') or ''),
                meter_version=int(data.get('meter_version') or 1),
                maximum_quantity=data.get('maximum_quantity')
                or data.get('maximum_measurements')
                or data.get('quantity')
                or 0,
                unit=str(data.get('unit') or ''),
                resource_uid=str(
                    data.get('resource_uid')
                    or (data.get('resource_ref') or {}).get('id')
                    or data.get('resource_ref')
                    or ''
                ),
                billing_account_id=data.get('billing_account_id')
                or data.get('expected_billing_account_id'),
                expected_billing_account_id=data.get('expected_billing_account_id'),
                producer_principal=str(
                    data.get('producer_principal')
                    or request.META.get('HTTP_X_METERING_PRODUCER')
                    or 'anonymous-producer'
                ),
                product=str(data.get('product') or 'hive'),
                writer_generation=(
                    int(data['writer_generation'])
                    if data.get('writer_generation') is not None
                    else None
                ),
                dimensions=data.get('dimensions') or {},
                ttl_seconds=int(data.get('ttl_seconds') or 300),
            )
        except ReservationError as exc:
            return self._reservation_error(exc)
        except Exception as exc:  # noqa: BLE001 — map unexpected to 400
            return Response({'error': 'reserve_failed', 'detail': str(exc)}, status=400)
        code = status.HTTP_200_OK if result.get('status') in {
            'committed', 'released', 'expired_unstarted', 'orphaned_hold', 'started', 'reserved',
        } and result.get('decision_id') else status.HTTP_201_CREATED
        # Prefer 201 on first create; replay still 200 when already terminal/reserved.
        # Client distinguishes via decision_id stability.
        return Response(result, status=status.HTTP_201_CREATED if result.get('allowed') is not None else code)


class InternalQuotaReservationStartView(_ReservationBase):
    def post(self, request, reservation_id):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = start_reservation(
                reservation_id,
                idempotency_key=self._idem_key(request),
                occurrence_ref=str(data.get('occurrence_ref') or ''),
                outbox_ref=str(data.get('outbox_ref') or ''),
                producer_principal=str(
                    data.get('producer_principal')
                    or request.META.get('HTTP_X_METERING_PRODUCER')
                    or 'anonymous-producer'
                ),
            )
        except ReservationError as exc:
            return self._reservation_error(exc)
        return Response(result, status=status.HTTP_200_OK)


class InternalQuotaReservationCommitView(_ReservationBase):
    def post(self, request, reservation_id):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = commit_reservation(
                reservation_id,
                idempotency_key=self._idem_key(request),
                actual_quantity=data.get('actual_quantity') or data.get('committed_actual') or 0,
                held_unknown_quantity=data.get('held_unknown_quantity') or data.get('held_unknown') or 0,
                terminal_proof_sha256=str(data.get('terminal_proof_sha256') or ''),
                terminal_proof_jws=str(data.get('terminal_proof_jws') or ''),
                producer_principal=str(
                    data.get('producer_principal')
                    or request.META.get('HTTP_X_METERING_PRODUCER')
                    or 'anonymous-producer'
                ),
            )
        except ReservationError as exc:
            return self._reservation_error(exc)
        return Response(result, status=status.HTTP_200_OK)


class InternalQuotaReservationReleaseView(_ReservationBase):
    def post(self, request, reservation_id):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = release_reservation(
                reservation_id,
                idempotency_key=self._idem_key(request),
                no_start_proof_sha256=str(data.get('no_start_proof_sha256') or ''),
                no_start_proof_jws=str(data.get('no_start_proof_jws') or ''),
                producer_principal=str(
                    data.get('producer_principal')
                    or request.META.get('HTTP_X_METERING_PRODUCER')
                    or 'anonymous-producer'
                ),
            )
        except ReservationError as exc:
            return self._reservation_error(exc)
        return Response(result, status=status.HTTP_200_OK)


class InternalQuotaReservationResolveOrphanView(_ReservationBase):
    """POST .../resolve-orphan — finish held_unknown after ORPHANED_HOLD (PLAT-4484 P2-2)."""

    def post(self, request, reservation_id):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = resolve_orphaned_hold(
                reservation_id,
                idempotency_key=self._idem_key(request),
                actual_quantity=data.get('actual_quantity') or data.get('committed_actual') or 0,
                released_quantity=data.get('released_quantity', data.get('released_unused')),
                terminal_proof_sha256=str(data.get('terminal_proof_sha256') or ''),
                terminal_proof_jws=str(data.get('terminal_proof_jws') or ''),
                producer_principal=str(
                    data.get('producer_principal')
                    or request.META.get('HTTP_X_METERING_PRODUCER')
                    or 'anonymous-producer'
                ),
            )
        except ReservationError as exc:
            return self._reservation_error(exc)
        return Response(result, status=status.HTTP_200_OK)


class InternalSpendLeaseCreateView(_ReservationBase):
    def post(self, request):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = issue_spend_lease(
                idempotency_key=self._idem_key(request),
                billing_account_id=data.get('billing_account_id'),
                resource_uid=str(data.get('resource_uid') or ''),
                meter_key=str(data.get('meter') or data.get('meter_key') or ''),
                quantity=data.get('quantity') or data.get('authorized_quantity') or 0,
                producer_principal=str(
                    data.get('producer_principal')
                    or request.META.get('HTTP_X_METERING_PRODUCER')
                    or 'anonymous-producer'
                ),
                parent_reservation_id=(
                    str(data['parent_reservation_id'])
                    if data.get('parent_reservation_id')
                    else None
                ),
                ttl_seconds=int(data.get('ttl_seconds') or 60),
            )
        except ReservationError as exc:
            return self._reservation_error(exc)
        return Response(result, status=status.HTTP_201_CREATED)


class InternalSpendLeaseCloseView(_ReservationBase):
    def post(self, request, lease_id):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = close_spend_lease(
                lease_id,
                idempotency_key=self._idem_key(request),
                debited_quantity=data.get('debited_quantity') or data.get('quantity') or 0,
                producer_principal=str(
                    data.get('producer_principal')
                    or request.META.get('HTTP_X_METERING_PRODUCER')
                    or 'anonymous-producer'
                ),
            )
        except ReservationError as exc:
            return self._reservation_error(exc)
        return Response(result, status=status.HTTP_200_OK)


class InternalAdmissionWriterCutoverView(_ReservationBase):
    def post(self, request, product):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = cutover_admission_writer(
                product=product,
                action=str(data.get('action') or ''),
                source_watermark=str(data.get('source_watermark') or ''),
                cutover_manifest_sha256=str(data.get('cutover_manifest_sha256') or ''),
            )
        except ReservationError as exc:
            return self._reservation_error(exc)
        return Response(result, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# PLAT-4485 — statement drafts + period management
# ---------------------------------------------------------------------------

def _parse_required_dt(value, field: str):
    if value is None or value == '':
        raise StatementError(f'{field} is required')
    if isinstance(value, datetime):
        return value
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise StatementError(f'{field} must be an ISO-8601 datetime')
    return parsed


class InternalBillingStatementDraftView(APIView):
    """POST /internal/v2/billing/statements/drafts

    Source-neutral Metering → Books draft. Deterministic statement_id from the
    business key. Idempotency-Key: statement:<id>:r<revision>.
    """

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        if not _authenticated(request):
            return Response(
                {'error': 'internal_service_authentication_required'},
                status=status.HTTP_403_FORBIDDEN,
            )
        data = request.data if isinstance(request.data, dict) else {}
        try:
            action = str(data.get('action') or 'export_draft').strip()
            allowed_actions = {
                'rate',
                'export_draft',
                'rate_and_export',
                'begin_closing',
                'finalize',
                'open_adjustment',
            }
            if action not in allowed_actions:
                raise StatementError(f'unknown action: {action}')

            # Adjustments derive their entire business key from the finalized
            # original. Resolve and validate it before creating any row so an
            # invalid request cannot leave an orphan ordinary/adjustment statement.
            if action == 'open_adjustment':
                original_statement_id = str(data.get('original_statement_id') or '').strip()
                if not original_statement_id:
                    raise StatementError('original_statement_id is required')
                try:
                    original = BillingStatement.objects.select_related('billing_account').get(
                        statement_id=original_statement_id
                    )
                except BillingStatement.DoesNotExist as exc:
                    raise StatementError('original statement not found') from exc
                if original.status != BillingStatement.Status.FINALIZED:
                    raise StatementStateError('adjustments only for finalized statements')
                adj = open_adjustment_statement(
                    original,
                    reason=str(data.get('reason') or ''),
                )
                return Response(
                    {
                        'statement_id': adj.statement_id,
                        'status': adj.status,
                        'statement_kind': adj.statement_kind,
                        'adjustment_sequence': adj.adjustment_sequence,
                        'original_statement_id': adj.original_statement_id,
                    },
                    status=status.HTTP_201_CREATED,
                )

            account_id = data.get('billing_account_id')
            if not account_id:
                raise StatementError('billing_account_id is required')
            try:
                account = BillingAccount.objects.get(pk=account_id)
            except (BillingAccount.DoesNotExist, ValueError) as exc:
                raise StatementError('billing_account_id not found') from exc

            period_start = _parse_required_dt(data.get('period_start'), 'period_start')
            period_end = _parse_required_dt(data.get('period_end'), 'period_end')

            statement = open_or_get_statement(
                billing_account=account,
                period_start=period_start,
                period_end=period_end,
                seller_legal_entity_id=str(data.get('seller_legal_entity_id') or ''),
                seller_gst_registration_id=str(data.get('seller_gst_registration_id') or ''),
                books_organization_id=str(data.get('books_organization_id') or ''),
                currency=str(data.get('currency') or account.currency or 'INR'),
                statement_kind=str(
                    data.get('statement_kind') or BillingStatement.Kind.ORDINARY
                ),
                adjustment_sequence=data.get('adjustment_sequence'),
                original_statement_id=str(data.get('original_statement_id') or ''),
            )

            required_wm = data.get('required_producer_watermarks') or {}
            if not isinstance(required_wm, dict):
                required_wm = {}

            if action in ('rate', 'export_draft', 'rate_and_export'):
                if statement.status in {
                    BillingStatement.Status.OPEN,
                    BillingStatement.Status.CLOSING,
                }:
                    rate_statement(
                        statement,
                        required_producer_watermarks=required_wm,
                        force_override_lag_sources=data.get('force_override_lag_sources') or [],
                    )
                    statement.refresh_from_db()

            if action == 'rate':
                return Response(
                    {
                        'statement_id': statement.statement_id,
                        'status': statement.status,
                        'generation': statement.generation,
                    },
                    status=status.HTTP_200_OK,
                )

            if action in ('export_draft', 'rate_and_export'):
                idem = str(
                    request.headers.get('Idempotency-Key')
                    or request.META.get('HTTP_IDEMPOTENCY_KEY')
                    or data.get('idempotency_key')
                    or ''
                ).strip()
                if_match = str(
                    request.headers.get('If-Match')
                    or request.META.get('HTTP_IF_MATCH')
                    or data.get('if_match')
                    or ''
                ).strip()
                payload = export_statement_draft(
                    statement,
                    idempotency_key=idem,
                    if_match=if_match,
                )
                return Response(payload, status=status.HTTP_200_OK)

            if action == 'begin_closing':
                begin_closing(statement)
                statement.refresh_from_db()
                return Response(
                    {
                        'statement_id': statement.statement_id,
                        'status': statement.status,
                        'closing_grace_until': (
                            statement.closing_grace_until.isoformat()
                            if statement.closing_grace_until
                            else None
                        ),
                    },
                    status=status.HTTP_200_OK,
                )

            if action == 'finalize':
                finalize_statement(
                    statement,
                    books_document_ref=str(data.get('books_document_ref') or ''),
                )
                statement.refresh_from_db()
                return Response(
                    {
                        'statement_id': statement.statement_id,
                        'status': statement.status,
                        'books_document_ref': statement.books_document_ref,
                    },
                    status=status.HTTP_200_OK,
                )

        except PeriodLagError as exc:
            return Response(
                {'error': exc.code, 'detail': str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except StatementStateError as exc:
            return Response(
                {'error': exc.code, 'detail': str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except StatementError as exc:
            return Response(
                {'error': getattr(exc, 'code', 'statement_error'), 'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
