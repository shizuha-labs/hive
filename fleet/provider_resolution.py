"""HIVE-2161 S3-lite: org provider resolution (fail-closed, HLD HIVE-336).

Pure resolution per the HLD resolution contract (wiki 8ff4fde0): given an
org-scoped ``HiveOrgProviderReference``, resolve the effective model for a
runtime class with explicit precedence and same-org assertions at resolve
time. This module NEVER issues leases and NEVER returns key material — the
TokenPool lease wiring lands as the follow-up slice; until then any active
BYO reference resolves to a **fail-closed hold** so a customer org's agents
can never silently start on Shizuha-owned keys with the org's model choice
(HLD Router Core v18 §5).

Gate decision (kumo, architecture, 2026-09-10, HIVE-2161 comment 696427):
fail-closed sequencing confirmed with four binding refinements, all
implemented here —

1. Distinct hold reason ``lease_wiring_pending`` (never overload
   ``no_provider_configured``).
2. Loud hold: every decision emits a counter with its reason; the card
   projection consumes :func:`card_failure_note` for the org-scoped note.
3. ``default_models_by_runtime`` is additive/null-safe; slice-1's flat
   ``default_models`` list stays live in the precedence chain.
4. Same-org assertions at resolve time (write-time validation alone is not
   the invariant).
5. The slug↔org_id identity bridge fails LOUD (kumo refinement #5,
   2026-09-10 comment): a slug→org_id lookup that itself fails or is
   ambiguous raises :class:`IdentityUnresolved` (reason
   ``identity_unresolved``) — it must NEVER fall through to the non-BYO
   path, which would silently put a genuinely-BYO org onto Shizuha keys
   (HLD Router Core v18 §5 entered through an identity gap).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

# HIVE-1862 reason enum + the S3-lite hold value (kumo refinement #1: its own
# enum member so operators can distinguish "BYO setup incomplete" from
# "platform wiring gap").
REASON_NO_PROVIDER_CONFIGURED = 'no_provider_configured'
REASON_LEASE_WIRING_PENDING = 'lease_wiring_pending'
REASON_IDENTITY_UNRESOLVED = 'identity_unresolved'
REASON_KEY_INVALID = 'key_invalid'
REASON_PROVIDER_QUOTA_EXHAUSTED = 'provider_quota_exhausted'
REASON_ENDPOINT_UNREACHABLE = 'endpoint_unreachable'
REASON_INVARIANT_VIOLATION = 'invariant_violation'
# HIVE-2161 F1 (kumo review 2026-09-12, blocking): lifecycle states must never
# silently take the non-BYO path. Own enum members (refinement #1 pattern —
# never overload no_provider_configured): an explicitly disabled reference is a
# fault needing operator attention; verifying/probation are waiting states
# (BYO setup in progress). Both HOLD the wake fail-closed; flipping DISABLED to
# an explicit Shizuha-keys fall-through requires an explicit architecture
# ruling, not a silent default.
REASON_PROVIDER_DISABLED = 'provider_disabled'
REASON_PROVIDER_LIFECYCLE_PENDING = 'provider_lifecycle_pending'

# Reference statuses that make a BYO reference resolution-eligible. VERIFYING/
# PROBATION are lifecycle states, not serving states; INVALID/EXHAUSTED map to
# their own typed reasons below.
_SERVABLE_STATUSES = {'active'}
_STATUS_TO_REASON = {
    'invalid': REASON_KEY_INVALID,
    'exhausted': REASON_PROVIDER_QUOTA_EXHAUSTED,
    'disabled': REASON_PROVIDER_DISABLED,
    'mismatch': REASON_KEY_INVALID,
    'verifying': REASON_PROVIDER_LIFECYCLE_PENDING,
    'probation': REASON_PROVIDER_LIFECYCLE_PENDING,
}

# Follow-up slice flips this to True together with the TokenPool lease
# issuance; the resolver's shape already carries the lease-handle slot.
LEASE_WIRING_COMPLETE = False

# Registry hook: callable(slug) → unique org id. WIRED (HIVE-2622 registry
# slice): the authoritative slug→org_id surface is Admin's INTERNAL
# organization endpoint (service-token auth, ClusterIP-only) — the same
# trust boundary fleet already consumes for rosters. The lookup maps
# strictly onto the refinement-#5 contract: 200 → unique org id (shape 1
# hit), 404 → None (shape 1 miss — genuinely non-BYO, the ONLY signal that
# maps to the non-BYO path), any other status or transport failure → raise
# (shape 2 — hold LOUD with ``identity_unresolved``, never defensively
# resolved as non-BYO, which would silently put a genuinely-BYO org onto
# Shizuha-owned keys).
def default_admin_org_slug_lookup(slug: str) -> Any:
    """Authoritative slug→org_id lookup via Admin's internal org endpoint.

    Lazy imports by design so the module stays importable in pure unit
    contexts. Admin 404 means "no such org" — a successful lookup with zero
    rows (shape 1 miss → None, the only non-BYO signal). Anything else
    (transport error, non-404 failure, malformed payload) raises so the
    caller holds LOUD (shape 2) instead of guessing.
    """
    import requests

    norm = str(slug or '').strip().lower()
    if not norm or norm in {'__all__', 'all', '*'}:
        raise IdentityUnresolved('empty organization_slug')
    # Sara P2 (pre-merge hunk, comment 775988): a roster-sourced slug is
    # interpolated into the Admin URL — a slug containing '/' or '..' would
    # hit a different Admin path, and a 404 from that WRONG path would be
    # misread as a shape-1 miss → non-BYO classification from a malformed
    # slug. Reject anything outside the slug vocabulary LOUD (shape 2), and
    # percent-encode as defense-in-depth so even a vocabulary change can't
    # redirect the path.
    if not re.fullmatch(r'[a-z0-9-]+', norm):
        _incr('org_provider_grant_total', {'outcome': 'identity_unresolved'})
        raise IdentityUnresolved(
            'organization_slug %r contains characters outside the slug '
            'vocabulary [a-z0-9-]' % (norm,)
        )
    base = (getattr(settings, 'HIVE_ADMIN_API_URL', '') or '').rstrip('/')
    if not base:
        raise IdentityUnresolved('HIVE_ADMIN_API_URL is not configured')
    try:
        from urllib.parse import quote

        resp = requests.get(
            f'{base}/internal/organizations/{quote(norm, safe="")}/',
            headers=_admin_service_headers(),
            timeout=8,
        )
    except Exception as exc:  # noqa: BLE001 — transport failure is shape 2, loud
        raise IdentityUnresolved(
            'admin internal organization lookup failed for %r: %s' % (norm, exc)
        ) from exc
    if resp.status_code == 404:
        return None  # shape 1 miss: lookup succeeded, zero rows → non-BYO
    if resp.status_code >= 400:
        raise IdentityUnresolved(
            'admin internal organization lookup rejected %r (HTTP %d)'
            % (norm, resp.status_code)
        )
    try:
        payload = resp.json() or {}
    except ValueError as exc:
        raise IdentityUnresolved(
            'admin internal organization lookup returned a malformed payload '
            'for %r' % (norm,)
        ) from exc
    org_id = payload.get('id')
    try:
        return int(org_id)
    except (TypeError, ValueError) as exc:
        raise IdentityUnresolved(
            'admin internal organization lookup returned a non-numeric id '
            'for %r: %r' % (norm, org_id)
        ) from exc


def _admin_service_headers() -> dict:
    """Service-token headers for the Admin internal API (ClusterIP-only).

    Mirrors the fleet roster-consumption boundary: the shared fleet service
    token authenticates the SERVICE, never a caller — the endpoint is
    ClusterIP-only and the lookup is slug-scoped read-only.
    """
    import os

    token = (
        os.environ.get('HIVE_FLEET_SERVICE_TOKEN')
        or getattr(settings, 'HIVE_FLEET_SERVICE_TOKEN', '')
        or ''
    ).strip()
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def _default_slug_lookup(slug: str) -> Any:
    raise IdentityUnresolved(
        'slug→org_id lookup not wired (registry slice pending); refusing '
        'non-BYO fall-through for %r' % (slug,)
    )


# Registry hook bound to the wired authoritative lookup (defined above).
ORG_SLUG_LOOKUP: Any = default_admin_org_slug_lookup


def _resolve_lookup(slug_lookup: Any) -> Any:
    return slug_lookup or ORG_SLUG_LOOKUP or _default_slug_lookup

# HLD liveness signals (Router Core v18 §8): counters keyed by reason, never
# by credential or model secret material. Backed by the same Redis metric
# counters the rest of fleet uses (metrics._incr_metric) when available;
# imported lazily so the module stays importable in pure unit contexts.
_COUNTER_NS = 'provider_resolution'


def _incr(name: str, labels: dict[str, str] | None = None) -> None:
    try:
        from .metrics import _incr_metric  # noqa: PLC0415 — lazy by design

        key = f'{_COUNTER_NS}:{name}'
        for label, value in sorted((labels or {}).items()):
            key += f':{_label(value)}'
        _incr_metric(key)
    except Exception:  # noqa: BLE001 — counters must never break resolution
        pass


def _label(value: Any) -> str:
    return re.sub(r'[^A-Za-z0-9._-]', '_', str(value))[:80]


@dataclass(frozen=True)
class Resolution:
    """Redacted resolution outcome (HLD ResolvedProviderContext, S3-lite)."""

    ok: bool
    model_id: str = ''
    reason: str = ''
    organization_id: int | None = None
    reference_id: int | None = None
    provider_type: str = ''
    # Slot for the follow-up lease slice; always None until
    # LEASE_WIRING_COMPLETE. Never key material — a handle, per the HLD.
    lease_handle: Any = field(default=None)
    lease_wiring_pending: bool = False

    @property
    def unavailable(self) -> bool:
        return not self.ok


def _same_org_assert(reference_org_id: int, organization_id: int) -> None:
    """Resolve-time same-org assertion (kumo refinement #4, binding).

    Raises before any data is read into a decision if the reference does not
    belong to the requesting org — a malformed/mismatched row is an invariant
    violation, not a fallback miss (HLD §Isolation invariant).
    """
    if int(reference_org_id) != int(organization_id):
        _incr('invariant_violation_total', {
            'reference_org': _label(reference_org_id),
        })
        raise InvariantViolation(
            'provider reference org %s does not match requesting org %s'
            % (reference_org_id, organization_id)
        )


class InvariantViolation(Exception):
    """Cross-org reference observed at resolve time — fail loud, deny."""


class IdentityUnresolved(Exception):
    """Shape-2 identity-bridge failure (kumo refinement #5, binding).

    The slug→org_id lookup itself failed or was ambiguous while an active
    BYO reference may exist. This must hold LOUD with its own reason code —
    it must NEVER be treated as "no BYO reference" (the non-BYO path),
    because a silent fall-through here puts a genuinely-BYO org onto
    Shizuha-owned keys: the HLD Router Core v18 §5 violation entered through
    an identity gap instead of an explicit decision.
    """

    reason = REASON_IDENTITY_UNRESOLVED

    def __init__(self, detail: str = '') -> None:
        super().__init__(detail or 'organization identity unresolved')
        self.detail = detail or 'organization identity unresolved'


def resolve_org_id_from_slug(
    organization_slug: Any, lookup: Any, *, count: bool = True,
) -> int | None:
    """Bridge ``TeamMembershipRole.organization_slug`` (str) → reference
    ``org_id`` (int). Kumo refinement #5 contract (binding on the PR):

    - **shape 1 hit** — ``lookup(slug)`` returns a unique org id → returned
      as ``int``.
    - **shape 1 miss** — ``lookup(slug)`` returns ``None`` (lookup succeeded,
      zero active rows) → ``None`` returned; the caller proceeds down the
      non-BYO path byte-for-byte unchanged. This is the ONLY signal that
      maps to non-BYO.
    - **shape 2** — the lookup itself fails or is ambiguous (it raises, or
      returns multiple candidates) → :class:`IdentityUnresolved` raised.
      Never ``None``: an unresolvable/ambiguous identity must hold loudly
      (``identity_unresolved``), never silently resolve as non-BYO.

    ``lookup`` contract: callable(slug) → unique org id (int/str), ``None``
    for "no active org rows", a collection of >1 candidates for ambiguous,
    or raise for lookup failure. Counters are emitted for every outcome so
    a systematically broken join surfaces in minutes (loud, observable,
    recoverable — the point of refinement #5). ``count=False`` silences
    counters for high-volume read projections (agent-card grids) so the
    wake-decision metric signal stays truthful.
    """
    slug = str(organization_slug or '').strip()
    if not slug:
        if count:
            _incr('identity_bridge_total', {'outcome': 'identity_unresolved'})
        raise IdentityUnresolved('empty organization_slug')

    try:
        result = lookup(slug)
    except Exception as exc:  # noqa: BLE001 — lookup failure is shape 2, loud
        if count:
            _incr('identity_bridge_total', {'outcome': 'identity_unresolved'})
        raise IdentityUnresolved(
            'slug→org_id lookup failed for %r: %s' % (slug, exc)
        ) from exc

    if result is None:
        # Shape 1 miss: lookup succeeded, zero active rows → genuinely
        # non-BYO. Byte-for-byte unchanged path.
        if count:
            _incr('identity_bridge_total', {'outcome': 'no_rows'})
        return None

    if isinstance(result, (list, tuple, set, frozenset)):
        if len(result) == 1:
            result = next(iter(result))
        elif len(result) == 0:
            # A lookup that signals "no rows" as an empty collection is
            # still a successful lookup — same as None.
            if count:
                _incr('identity_bridge_total', {'outcome': 'no_rows'})
            return None
        else:
            if count:
                _incr('identity_bridge_total', {'outcome': 'identity_unresolved'})
            raise IdentityUnresolved(
                'ambiguous slug→org_id match for %r: %d candidates'
                % (slug, len(result))
            )

    try:
        org_id = int(result)
    except (TypeError, ValueError) as exc:
        if count:
            _incr('identity_bridge_total', {'outcome': 'identity_unresolved'})
        raise IdentityUnresolved(
            'slug→org_id lookup returned non-numeric id for %r: %r'
            % (slug, result)
        ) from exc

    if count:
        _incr('identity_bridge_total', {'outcome': 'resolved'})
    return org_id


def _model_from_reference(ref: Any, runtime_class: str) -> str:
    """Precedence chain (kumo refinement #3): per-runtime dict → flat list.

    ``default_models_by_runtime`` is additive and may be absent (slice-1
    rows) or null — both are skipped without error. The flat list stays live
    as the next candidate. No backfill, no destructive change.
    """
    by_runtime = getattr(ref, 'default_models_by_runtime', None) or {}
    if isinstance(by_runtime, dict):
        candidate = by_runtime.get(runtime_class) or by_runtime.get(
            _label(runtime_class)
        )
        if candidate:
            return str(candidate)
    flat = getattr(ref, 'default_models', None) or []
    if isinstance(flat, (list, tuple)) and flat:
        return str(flat[0])
    return ''


def resolve_model(
    *,
    organization_id: int,
    runtime_class: str,
    reference: Any = None,
    agent_override_model: str = '',
    count: bool = True,
) -> Resolution:
    """Resolve the effective model for one org/runtime-class.

    ``reference`` is the org's ``HiveOrgProviderReference`` row (or None for
    a non-BYO org). ``agent_override_model`` is the same-org per-agent
    override already validated at write time; it is re-asserted here only as
    a model candidate — the override's org match is the caller's assertion
    plus the reference-level check below.

    Non-BYO orgs (``reference is None``) resolve to
    ``ok=False, reason=no_provider_configured`` — callers treat that exactly
    as "no BYO reference" and keep their existing non-BYO behavior
    (byte-for-byte unchanged, gate condition).

    ``count=False`` silences the decision counters for high-volume read
    projections (agent-card grids) so the wake-decision metric signal stays
    truthful. Invariant violations still raise regardless of ``count``.
    """
    runtime_class = _label(runtime_class) or 'default'

    if reference is None:
        if count:
            _incr('unavailable_total', {'reason': REASON_NO_PROVIDER_CONFIGURED})
        return Resolution(
            ok=False,
            reason=REASON_NO_PROVIDER_CONFIGURED,
            organization_id=organization_id,
        )

    ref_org = getattr(reference, 'org_id', None)
    if ref_org is None:
        _incr('invariant_violation_total', {'reason': 'missing_org_id'})
        raise InvariantViolation('provider reference row carries no org_id')
    _same_org_assert(ref_org, organization_id)

    status = str(getattr(reference, 'status', '') or '').strip().lower()
    if status not in _SERVABLE_STATUSES:
        reason = _STATUS_TO_REASON.get(status, REASON_NO_PROVIDER_CONFIGURED)
        if count:
            _incr('unavailable_total', {'reason': reason})
        return Resolution(
            ok=False,
            reason=reason,
            organization_id=organization_id,
            reference_id=getattr(reference, 'id', None),
            provider_type=str(getattr(reference, 'provider_type', '') or ''),
        )

    # Precedence: per-agent override (same-org, write-validated) →
    # per-runtime default → flat list head.
    model = (agent_override_model or '').strip()
    if not model:
        model = _model_from_reference(reference, runtime_class)

    if not model:
        if count:
            _incr('unavailable_total', {'reason': REASON_NO_PROVIDER_CONFIGURED})
        return Resolution(
            ok=False,
            reason=REASON_NO_PROVIDER_CONFIGURED,
            organization_id=organization_id,
            reference_id=getattr(reference, 'id', None),
            provider_type=str(getattr(reference, 'provider_type', '') or ''),
        )

    if not LEASE_WIRING_COMPLETE:
        # Fail-closed hold (gate decision): the org's model choice is known
        # but starting the agent now would run it on Shizuha-owned keys —
        # the HLD-forbidden fallback. Hold loudly with the dedicated reason.
        if count:
            _incr('unavailable_total', {'reason': REASON_LEASE_WIRING_PENDING})
        return Resolution(
            ok=False,
            reason=REASON_LEASE_WIRING_PENDING,
            organization_id=organization_id,
            reference_id=getattr(reference, 'id', None),
            provider_type=str(getattr(reference, 'provider_type', '') or ''),
            model_id=model,
            lease_wiring_pending=True,
        )

    if count:
        _incr('success_total', {'provider_type': _label(getattr(reference, 'provider_type', ''))})
    return Resolution(
        ok=True,
        model_id=model,
        organization_id=organization_id,
        reference_id=getattr(reference, 'id', None),
        provider_type=str(getattr(reference, 'provider_type', '') or ''),
    )


def card_failure_note(resolution: Resolution, *, include_model_id: bool = True) -> str:
    """Org-scoped failure-contract note for the agent-card projection.

    Loud hold (kumo refinement #2): a BYO org in the hold window never sees
    a bare unexplained failure. Returns '' for ok resolutions (no note).
    ``include_model_id=False`` for batched grid notes (N1, kumo review
    2026-09-12): the grid resolves with runtime_class='' and may name a
    different model than the wake hold — omit the id there.
    """
    if resolution.ok:
        return ''
    if resolution.reason == REASON_LEASE_WIRING_PENDING:
        if include_model_id:
            return (
                'BYO provider configured (model %s) — inference held: platform '
                'lease wiring pending; the agent will not run on Shizuha-owned '
                'keys. Org-scoped: other teams are unaffected.' % (
                    resolution.model_id or 'org default',
                )
            )
        return (
            'BYO provider configured — inference held: platform lease wiring '
            'pending; the agent will not run on Shizuha-owned keys. '
            'Org-scoped: other teams are unaffected.'
        )
    if resolution.reason == REASON_IDENTITY_UNRESOLVED:
        return (
            'BYO provider check held: organization identity could not be '
            'resolved (slug→org bridge failed or ambiguous). The agent will '
            'not run on Shizuha-owned keys while identity is unresolved — '
            'operator attention required (org-scoped).'
        )
    if resolution.reason == REASON_NO_PROVIDER_CONFIGURED:
        return 'No BYO provider configured for this org (org-scoped).'
    if resolution.reason == REASON_PROVIDER_DISABLED:
        # F1: name the real state — never the false "no BYO configured" when
        # a reference row exists.
        return (
            'BYO provider disabled for this org — re-enable or rotate the '
            'reference before the agent can run on the org\u2019s provider; '
            'the agent will not run on Shizuha-owned keys meanwhile '
            '(org-scoped).'
        )
    if resolution.reason == REASON_PROVIDER_LIFECYCLE_PENDING:
        return (
            'BYO provider reference is verifying/probation — inference held '
            'until the reference becomes servable; the agent will not run on '
            'Shizuha-owned keys meanwhile (org-scoped).'
        )
    if resolution.reason == REASON_KEY_INVALID:
        return 'BYO provider key invalid/revoked — re-validate or rotate (org-scoped).'
    if resolution.reason == REASON_PROVIDER_QUOTA_EXHAUSTED:
        return 'BYO provider quota exhausted — org-scoped, not a fleet incident.'
    if resolution.reason == REASON_ENDPOINT_UNREACHABLE:
        return 'BYO provider endpoint unreachable — org-scoped backoff active.'
    return 'Provider unavailable: %s (org-scoped).' % resolution.reason


def _agent_username(agent: Any) -> str:
    if isinstance(agent, dict):
        return str(agent.get('agent_username') or '').strip().lower()
    return str(getattr(agent, 'agent_username', '') or '').strip().lower()


def resolve_agent_org_provider(
    agent: Any,
    *,
    runtime_class: str = '',
    slug_lookup: Any = None,
) -> Resolution:
    """Agent → org → ``HiveOrgProviderReference`` → :class:`Resolution`.

    The wake-decision pipeline (provisioner ``_lease_start_decision``):
    resolves the agent's org adjacency from the roster edge
    (``TeamMembershipRole``), bridges slug→org_id via the registry hook
    (kumo refinement #5 — unwired/failed/ambiguous identity raises
    :class:`IdentityUnresolved`), then resolves the org's active BYO
    reference fail-closed. Non-BYO agents (no roster adjacency, or a
    successful lookup with no active reference) resolve
    ``no_provider_configured`` and the caller keeps its existing behavior
    byte-for-byte.

    Raises:
        IdentityUnresolved: shape-2 identity-bridge failure (loud hold).
        InvariantViolation: cross-org row or ambiguous active references.
    """
    from .models import HiveOrgProviderReference, TeamMembershipRole

    username = _agent_username(agent)
    slugs = set(
        TeamMembershipRole.objects.filter(
            agent_identifier=username, enabled=True,
        ).exclude(organization_slug='')
        .values_list('organization_slug', flat=True)
    )
    if not slugs:
        # No org adjacency → genuinely non-BYO → unchanged path.
        return Resolution(ok=False, reason=REASON_NO_PROVIDER_CONFIGURED)
    if len(slugs) > 1:
        raise IdentityUnresolved(
            'agent %r maps to %d organizations (%s); ambiguous adjacency'
            % (username, len(slugs), ', '.join(sorted(slugs)))
        )

    org_id = resolve_org_id_from_slug(
        next(iter(slugs)), _resolve_lookup(slug_lookup),
    )
    # HIVE-2161 F1 (kumo review 2026-09-12, blocking): query the org's
    # references WITHOUT a status pre-filter. Filtering status='active' here
    # made every lifecycle state (verifying/probation/invalid/exhausted/
    # disabled/mismatch) resolve as "no reference" → no_provider_configured →
    # the agent woke on Shizuha-owned keys — the ruled hazard entered through
    # a lifecycle gap, and it left the typed _STATUS_TO_REASON mapping dead
    # code in production. resolve_model already types non-servable statuses.
    org_refs = list(
        HiveOrgProviderReference.objects.filter(org_id=org_id)
        .order_by('id')
    )
    active_refs = [
        ref for ref in org_refs
        if str(getattr(ref, 'status', '') or '').strip().lower() == 'active'
    ]
    if len(active_refs) > 1:
        _incr('invariant_violation_total', {'reason': 'multiple_active_refs'})
        raise InvariantViolation(
            'org %s carries %d active provider references; expected at most 1'
            % (org_id, len(active_refs))
        )
    if active_refs:
        reference = active_refs[0]
    elif org_refs:
        # Latest non-servable reference (highest id = most recently created;
        # the rotate flow appends). Its lifecycle status types the hold.
        reference = org_refs[-1]
    else:
        reference = None
    return resolve_model(
        organization_id=org_id,
        runtime_class=runtime_class,
        reference=reference,
    )


def org_provider_notes(agents: Any, *, slug_lookup: Any = None) -> dict[str, str]:
    """Batched org-scoped card notes for an Agents-grid page (quiet).

    Returns ``{agent_username_lower: note}`` for agents whose org BYO
    resolution is unavailable INSIDE an org context (lease-wiring hold,
    invalid/exhausted key, unresolved identity, ambiguous adjacency).
    Agents without a note are absent from the mapping — the caller adds the
    field only when a note exists, so non-BYO orgs are byte-for-byte
    unchanged. Counters are silenced (``count=False``) so the wake-decision
    metrics stay truthful; the card note itself is the loud surface here.

    ``slug_lookup`` overrides the registry hook (tests); production passes
    nothing and gets the wired hook (or the loud unwired fallback).

    Non-fatal by design: callers wrap this in try/except and skip
    annotation on failure.
    """
    from .models import HiveOrgProviderReference, TeamMembershipRole

    identifiers: list[str] = []
    for agent in agents or []:
        name = _agent_username(agent)
        if name:
            identifiers.append(name)
    if not identifiers:
        return {}

    # Fleet-wide short-circuit: with zero references at all no org can be
    # BYO-anything, so every agent takes the unchanged path. One cheap indexed
    # query guards the entire hot polling path. (F1: lifecycle-state rows also
    # annotate now, so the guard must not filter on status.)
    if not HiveOrgProviderReference.objects.exists():
        return {}

    slug_map: dict[str, set[str]] = {}
    for agent_identifier, organization_slug in (
        TeamMembershipRole.objects.filter(
            agent_identifier__in=identifiers, enabled=True,
        ).exclude(organization_slug='')
        .values_list('agent_identifier', 'organization_slug')
    ):
        slug_map.setdefault(agent_identifier, set()).add(organization_slug)
    if not slug_map:
        return {}

    lookup = _resolve_lookup(slug_lookup)

    # Bridge every distinct slug once; collect the per-agent identity state.
    org_ids_by_slug: dict[str, int | None] = {}
    identity_errors: dict[str, IdentityUnresolved] = {}
    for slugs in slug_map.values():
        for slug in slugs:
            if slug in org_ids_by_slug or slug in identity_errors:
                continue
            try:
                org_ids_by_slug[slug] = resolve_org_id_from_slug(
                    slug, lookup, count=False,
                )
            except IdentityUnresolved as exc:
                identity_errors[slug] = exc

    wanted_org_ids = {
        org_id for org_id in org_ids_by_slug.values() if org_id is not None
    }
    refs_by_org: dict[int, list[Any]] = {}
    if wanted_org_ids:
        # F1: no status pre-filter — lifecycle-state references must surface
        # their real state in the grid note, never the false "no BYO
        # configured". Active refs are still split out below for the
        # multiple-active invariant.
        for ref in (
            HiveOrgProviderReference.objects.filter(
                org_id__in=wanted_org_ids,
            ).order_by('id')
        ):
            refs_by_org.setdefault(ref.org_id, []).append(ref)

    notes: dict[str, str] = {}
    for name in identifiers:
        slugs = slug_map.get(name)
        if not slugs:
            continue  # no org adjacency → non-BYO → unchanged
        if len(slugs) > 1:
            notes[name] = card_failure_note(Resolution(
                ok=False, reason=REASON_IDENTITY_UNRESOLVED,
            ))
            continue
        slug = next(iter(slugs))
        if slug in identity_errors:
            notes[name] = card_failure_note(Resolution(
                ok=False, reason=REASON_IDENTITY_UNRESOLVED,
            ))
            continue
        org_id = org_ids_by_slug.get(slug)
        if org_id is None:
            continue  # successful lookup, no active org rows → non-BYO
        org_refs = refs_by_org.get(org_id) or []
        active_refs = [
            ref for ref in org_refs
            if str(getattr(ref, 'status', '') or '').strip().lower() == 'active'
        ]
        if len(active_refs) > 1:
            notes[name] = card_failure_note(Resolution(
                ok=False, reason=REASON_INVARIANT_VIOLATION,
            ))
            continue
        if active_refs:
            reference = active_refs[0]
        elif org_refs:
            reference = org_refs[-1]  # latest non-servable reference
        else:
            reference = None
        resolution = resolve_model(
            organization_id=org_id,
            runtime_class='',
            reference=reference,
            count=False,
        )
        # N1 (kumo review 2026-09-12): the batched grid path resolves with
        # runtime_class='' — the model it names can differ from the one the
        # wake hold resolves with the agent's real bridge class. Omit the
        # model id from batched notes; the wake-decision surface stays the
        # authoritative model naming.
        note = card_failure_note(resolution, include_model_id=False)
        if note:
            notes[name] = note
    return notes
