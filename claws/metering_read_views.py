"""PLAT-9402 Slice A — the Metering Core read plane (HLD §9).

The core was write/admission-only: every existing route is an internal
ingest/reservation/statement POST. The R2 usage portal (PLAT-9402 slice B)
must be fed "exclusively via the neutral aggregate plane" (epic invariant),
which requires account-scoped READS over the rebuildable projections that
already exist — no new tables:

* ``GET /internal/v1/accounts/{id}/usage``          — UsageRollup slices + as-of watermark
* ``GET /internal/v1/accounts/{id}/entitlements``   — EntitlementBalance + exact grant generations
* ``GET /internal/v1/accounts/{id}/spend-balance``  — redacted available/reserved/held projection
* ``GET /api/v1/usage/summary``                     — ID-subject-scoped one-call summary for the portal

Auth shapes (both fail closed):

* Internal service audience — the established ``_authenticated`` service-token
  check (``X-Hive-Internal-Token`` / Bearer), same as every other internal
  metering route. A producer service reads any account it is authorized for.
* ID-subject scope — ``FederatedJWTAuthentication`` (Shizuha-ID JWT). The
  subject variant resolves the CALLER'S OWN default BillingAccount via the
  server-derived ``resolve_default_account`` path; a body/query account id is
  never trusted as the resolution target (HLD §2/§2.1 — the same rule as
  ``accounts:resolve``). Two-account isolation is therefore structural: the
  account id never enters the query from client input.

Quantities serialize as decimal strings — the portal renders them verbatim and
never does float math client-side (PLAT-9402 design note).
"""
import logging

from django.db.models import Max, Sum
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import FederatedJWTAuthentication
from .metering_core import MeteringCoreError, resolve_default_account
from .metering_models import (
    BillingAccount,
    EntitlementBalance,
    EntitlementGrant,
    UsageRollup,
)
from .metering_views import _authenticated

logger = logging.getLogger(__name__)


def _dec(value) -> str:
    """Decimal → string (the portal's display contract; no float math)."""
    return format(value, 'f') if value is not None else '0'


def _parse_period(request):
    """Optional ?period_start=/?period_end= ISO filters; None = unbounded."""
    from django.utils.dateparse import parse_datetime

    period_start = parse_datetime(request.query_params.get('period_start') or '')
    period_end = parse_datetime(request.query_params.get('period_end') or '')
    if request.query_params.get('period_start') and period_start is None:
        raise MeteringCoreError('period_start is not a valid ISO datetime.', code='bad_period')
    if request.query_params.get('period_end') and period_end is None:
        raise MeteringCoreError('period_end is not a valid ISO datetime.', code='bad_period')
    return period_start, period_end


def _usage_payload(account: BillingAccount, period_start, period_end) -> dict:
    rollups = UsageRollup.objects.filter(billing_account=account).order_by('-period_end', 'meter_key')
    if period_start is not None:
        rollups = rollups.filter(period_end__gte=period_start)
    if period_end is not None:
        rollups = rollups.filter(period_start__lte=period_end)
    watermark = rollups.aggregate(as_of=Max('rebuilt_at'))['as_of']
    return {
        'account_id': str(account.id),
        # As-of semantics (PLAT-9402 acceptance): rollups are rebuilt from
        # immutable UsageEvent rows, so consumption is eventually consistent;
        # the watermark is the newest rebuild the account's slices have seen.
        'usage_watermark': watermark.isoformat() if watermark else None,
        'rollups': [
            {
                'period_start': r.period_start.isoformat(),
                'period_end': r.period_end.isoformat(),
                'meter_key': r.meter_key,
                'meter_version': r.meter_version,
                'unit': r.unit,
                'dimensions': r.dimensions,
                'gross_quantity': _dec(r.gross_quantity),
                'generation': r.generation,
                'event_count': r.event_count,
            }
            for r in rollups[:500]
        ],
    }


def _entitlements_payload(account: BillingAccount) -> dict:
    balances = (
        EntitlementBalance.objects.filter(billing_account=account)
        .select_related('grant', 'grant__meter_definition')
        .order_by('-updated_at')
    )
    return {
        'account_id': str(account.id),
        'entitlements': [
            {
                'grant_key': b.grant.grant_key,
                'grant_version': b.grant.version,
                'behavior': b.grant.behavior,
                'meter_key': (
                    b.grant.meter_definition.meter_key
                    if b.grant.meter_definition_id else ''
                ),
                'reset_generation': b.reset_generation,
                'granted_amount': _dec(b.granted_amount),
                'reserved_amount': _dec(b.reserved_amount),
                'consumed_amount': _dec(b.consumed_amount),
                'available_amount': _dec(b.available_amount),
                'period_start_utc': b.period_start_utc.isoformat(),
                'period_end_utc': b.period_end_utc.isoformat(),
                'updated_at': b.updated_at.isoformat(),
            }
            for b in balances[:200]
        ],
    }


def _spend_payload(account: BillingAccount) -> dict:
    """Redacted projection: aggregate available/reserved/held only.

    Per-grant detail stays behind /entitlements; the spend projection is the
    portal's headline number and exposes no grant-level rows (HLD §9).
    """
    from decimal import Decimal as _Decimal

    agg = EntitlementBalance.objects.filter(billing_account=account).aggregate(
        available=Sum('available_amount'), reserved=Sum('reserved_amount'),
    )
    # Sum() over sqlite yields int/float; over Postgres, Decimal — and the
    # aggregate loses the column's 6-dp scale either way. Quantize so the
    # decimal-string contract is identical in both environments.
    scale = _Decimal('0.000001')
    available = (_Decimal(str(agg['available'])) if agg['available'] is not None else _Decimal('0')).quantize(scale)
    reserved = (_Decimal(str(agg['reserved'])) if agg['reserved'] is not None else _Decimal('0')).quantize(scale)
    held = max(reserved, _Decimal('0'))
    return {
        'account_id': str(account.id),
        'spend': {
            'available': _dec(available),
            'reserved': _dec(reserved),
            'held': _dec(held),
        },
    }


class _MeteringInternalReadBase(APIView):
    """Internal service audience — the established service-token check."""

    authentication_classes = []
    permission_classes = []

    def _auth_error(self, request):
        if _authenticated(request):
            return None
        return Response(
            {'error': 'internal_service_authentication_required'},
            status=status.HTTP_403_FORBIDDEN,
        )

    def _account_or_404(self, account_id):
        try:
            return BillingAccount.objects.get(id=account_id)
        except (BillingAccount.DoesNotExist, ValueError):
            return None


class InternalAccountUsageView(_MeteringInternalReadBase):
    """GET /internal/v1/accounts/{id}/usage — rollup slices + as-of watermark."""

    def get(self, request, account_id):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        account = self._account_or_404(account_id)
        if account is None:
            return Response({'error': 'account_not_found'}, status=status.HTTP_404_NOT_FOUND)
        try:
            period_start, period_end = _parse_period(request)
        except MeteringCoreError as exc:
            return Response(exc.as_dict(), status=status.HTTP_400_BAD_REQUEST)
        return Response(_usage_payload(account, period_start, period_end))


class InternalAccountEntitlementsView(_MeteringInternalReadBase):
    """GET /internal/v1/accounts/{id}/entitlements — balances + grant generations."""

    def get(self, request, account_id):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        account = self._account_or_404(account_id)
        if account is None:
            return Response({'error': 'account_not_found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_entitlements_payload(account))


class InternalAccountSpendBalanceView(_MeteringInternalReadBase):
    """GET /internal/v1/accounts/{id}/spend-balance — redacted projection."""

    def get(self, request, account_id):
        auth_error = self._auth_error(request)
        if auth_error:
            return auth_error
        account = self._account_or_404(account_id)
        if account is None:
            return Response({'error': 'account_not_found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_spend_payload(account))


class SubjectUsageSummaryView(APIView):
    """GET /api/v1/usage/summary — the portal's one-call read (ID-subject scope).

    FederatedJWTAuthentication resolves the Shizuha-ID subject; the account is
    then derived SERVER-SIDE via ``resolve_default_account`` — the caller never
    names an account id (HLD §2/§2.1). Two-account isolation is structural:
    the query key never comes from client input.
    """

    authentication_classes = [FederatedJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account = resolve_default_account(
            account_type=BillingAccount.AccountType.INDIVIDUAL,
            owner_user_id=request.user.id,
        )
        try:
            period_start, period_end = _parse_period(request)
        except MeteringCoreError as exc:
            return Response(exc.as_dict(), status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'account': {
                'id': str(account.id),
                'account_type': account.account_type,
                'display_name': account.display_name,
            },
            'usage': _usage_payload(account, period_start, period_end),
            'entitlements': _entitlements_payload(account),
            'spend': _spend_payload(account)['spend'],
        })
