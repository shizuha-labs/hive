"""
HIVE-127 — Token/Credential Coordinator authority & isolation enforcement.

This module is the *enforcement* layer on top of the HIVE-123 storage foundation
(``TokenPool`` / ``TokenEntry`` / ``TokenLease``). It implements the per-owner
isolation contract (HIVE wiki ``98ba5b0c…``, parent epic HIVE-121):

  §2 Authority model
    - The acting owner is derived ONLY from a verified JWT claim — never from the
      request body, query string, label, or a caller-supplied ``X-*`` header.
    - Raw provider-token reads (``model-token``) require BOTH a Coordinator
      audience (``aud``) in the verified token AND a broker/workload role bound to
      a pod/agent identity. User/admin JWTs are refused the raw-token path.
    - Admin CRUD addresses entries by opaque ``entry_id``; labels are display-only
      and never authority-bearing.

  §3 Application-layer isolation
    - Every lookup is scoped by ``(owner_kind, owner_id, provider)`` BEFORE it can
      resolve an entry, so a non-owner result is indistinguishable from absent.
    - Non-owner read/mutate/report attempts return 404 (never 403) — no
      enumeration leak.

Nothing here trusts a request body for identity. The owner scope returned by
``resolve_owner`` is the only owner a request may ever touch.
"""
from dataclasses import dataclass

from django.conf import settings

from hive_platform.models import TokenPool


def coordinator_audience() -> str:
    """The audience a token must carry to address the Coordinator at all."""
    return getattr(settings, 'HIVE_COORDINATOR_AUDIENCE', 'hive-coordinator')


def broker_scope() -> str:
    """The scope/claim marking a token as a broker/workload (raw-token role)."""
    return getattr(settings, 'HIVE_COORDINATOR_BROKER_SCOPE', 'coordinator:model-token')


@dataclass(frozen=True)
class OwnerScope:
    """The single owner a request is allowed to act on. Derived from a verified
    claim only — constructing one from request body data is impossible by design."""

    owner_kind: str
    owner_id: str

    def pool(self, provider):
        """The caller-owned pool for ``provider``, or ``None`` — owner-scoped lookup."""
        return TokenPool.objects.filter(
            owner_kind=self.owner_kind,
            owner_id=self.owner_id,
            provider=provider,
        ).first()


def _claims(request):
    """The validated token claims (``request.auth`` is the simplejwt AccessToken)."""
    auth = getattr(request, 'auth', None)
    if auth is None:
        return None
    # AccessToken supports .get(); a plain dict (tests) does too.
    return auth


def _aud_values(claims) -> list:
    raw = claims.get('aud') if claims is not None else None
    if not raw:
        return []
    return [raw] if isinstance(raw, str) else list(raw)


def _scope_values(claims) -> list:
    raw = claims.get('scope') if claims is not None else None
    if not raw:
        return []
    if isinstance(raw, str):
        return raw.split()
    return list(raw)


def has_coordinator_audience(request) -> bool:
    return coordinator_audience() in _aud_values(_claims(request))


# Native ID client_credentials derives aud from the scope's service prefix.
# This is the existing fleet-org broker role, expressed as a revocable client;
# it grants no user pool, staff permission, or caller-selected owner.
COORDINATOR_SERVICE_CLIENT_ID = 'hive-coordinator-broker'
COORDINATOR_SERVICE_SCOPE = 'hive-coordinator:model-token'


def is_coordinator_service_claims(claims) -> bool:
    if claims is None:
        return False
    return (
        claims.get('token_type') == 'access'
        and claims.get('is_service') is True
        and claims.get('client_id') == COORDINATOR_SERVICE_CLIENT_ID
        and claims.get('sub') == 'service:' + COORDINATOR_SERVICE_CLIENT_ID
        and _aud_values(claims) == [coordinator_audience()]
        and _scope_values(claims) == [COORDINATOR_SERVICE_SCOPE]
        and not any(key in claims for key in ('owner_kind', 'owner_id', 'org_workload', 'user_id'))
    )


def is_coordinator_broker_claims(claims) -> bool:
    """One canonical role predicate for middleware and coordinator routes.

    Service tokens may only use the exact revocable fleet-org client contract;
    they cannot fall through into the historical owner/workload token role.
    """
    if claims is None:
        return False
    if claims.get('is_service') or claims.get('client_id') == COORDINATOR_SERVICE_CLIENT_ID:
        return is_coordinator_service_claims(claims)
    return (coordinator_audience() in _aud_values(claims)
            and broker_scope() in _scope_values(claims))


def is_broker_request(request) -> bool:
    return is_coordinator_broker_claims(_claims(request))


def broker_identity(request) -> str:
    """Stable workload identity of a broker token, for lease binding (§3).

    Prefers an explicit workload/agent claim, then the subject, then the user id.
    Never falls back to anything caller-controllable in the body.
    """
    claims = _claims(request)
    if claims is None:
        return ''
    for key in ('workload_id', 'agent_id', 'client_id', 'sub'):
        val = claims.get(key)
        if val:
            return str(val)
    uid = claims.get('user_id')
    return str(uid) if uid else ''


def resolve_owner(request) -> OwnerScope:
    """The owner scope a request may act on, from verified claims ONLY (§2).

    For a broker token, the owner it acts for is carried in the minted claims
    (``owner_kind``/``owner_id`` set by shizuha-id at issue time), falling back to
    the org pool for fleet-internal workloads. For a user/admin JWT, the owner is
    the authenticated user. In no case is the owner read from the request body.
    """
    claims = _claims(request)
    if claims is not None:
        # Canonical service ownership is fixed before legacy explicit claims.
        # Invalid service claims never select a user or foreign org pool.
        if claims.get('is_service') or claims.get('client_id') == COORDINATOR_SERVICE_CLIENT_ID:
            if is_coordinator_service_claims(claims):
                return OwnerScope(owner_kind=TokenPool.OwnerKind.ORG, owner_id=TokenPool.ORG_OWNER_ID)
            return OwnerScope(owner_kind=TokenPool.OwnerKind.USER, owner_id='')
        owner_kind = claims.get('owner_kind')
        owner_id = claims.get('owner_id')
        if owner_kind and owner_id is not None:
            return OwnerScope(owner_kind=str(owner_kind), owner_id=str(owner_id))
        if is_broker_request(request) and claims.get('owner_id') is None:
            # A broker token with the org-fallback audience but no explicit owner
            # acts on the synthetic org pool, never on an arbitrary user.
            if claims.get('org_workload'):
                return OwnerScope(
                    owner_kind=TokenPool.OwnerKind.ORG,
                    owner_id=TokenPool.ORG_OWNER_ID,
                )
    user = getattr(request, 'user', None)
    uid = getattr(user, 'id', None)
    return OwnerScope(owner_kind=TokenPool.OwnerKind.USER, owner_id=str(uid) if uid else '')
