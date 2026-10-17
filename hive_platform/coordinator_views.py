"""
HIVE-127 — Token/Credential Coordinator API (isolation-enforcing).

Endpoints (all under ``/hive/api/v1/coordinator/``):

    POST   model-token                 broker/workload only — read a raw provider
                                       token for the caller's owner pool + issue a
                                       single-use status lease. User/admin JWTs are
                                       refused (no plaintext path).
    GET    token-entries               admin CRUD — list ONLY the caller-owned
    POST   token-entries               entries (metadata, never plaintext); create.
    GET    token-entries/<entry_id>    owner-scoped detail; non-owner -> 404.
    PATCH  token-entries/<entry_id>    relabel / (de)activate / set cooldown.
    DELETE token-entries/<entry_id>    delete; non-owner -> 404, no state change.
    POST   report-status               broker only, lease-bound: cool/deactivate an
                                       entry ONLY with a valid unexpired lease whose
                                       broker + entry match. Anything else -> 404.
    POST   /v1/qa/tokenpool-allocator  staff-only HIVE-1824 live allocator suite;
                                       no provider token, lease, or egress path.

Every per-entry route resolves the entry through the caller's verified owner scope
FIRST (``coordinator.resolve_owner``); a non-owner entry is indistinguishable from
an absent one (HTTP 404, never 403) — no enumeration leak (contract §3).
"""
from datetime import timedelta
import hashlib
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response

from hive_platform.models import TokenPool, TokenEntry, TokenLease, TokenStickyAssignment
from . import coordinator
from . import token_coordinator

# Identical body for every non-owner / absent / non-broker resource outcome, so a
# caller cannot distinguish "exists but not yours" from "does not exist" (§5.8).
NOT_FOUND = Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)


def _pool_dry_response(provider, scopes):
    """404 for a dry pool, plus the earliest moment it could serve again.

    Shape stays identical to the opaque _not_found() the rest of this view uses;
    `retry_after_seconds` is additive so an older broker simply ignores it.
    None means "no known horizon" — the broker must fall back to its own short
    TTL rather than assume the pool is permanently dead.
    """
    from django.utils import timezone as _tz

    from hive_platform.models import TokenEntry

    now = _tz.now()
    horizons = [
        (entry.cooldown_until - now).total_seconds()
        for entry in TokenEntry.objects.for_scopes(scopes, provider)
        if getattr(entry, 'cooldown_until', None) and entry.cooldown_until > now
    ]
    retry_after = int(min(horizons)) if horizons else None
    body = {'error': 'Not found', 'reason': 'pool_dry'}
    if retry_after and retry_after > 0:
        # Cap the advertised horizon: a broker should re-check periodically even
        # for a long cooldown, in case an operator adds a fresh account.
        body['retry_after_seconds'] = min(retry_after, 3600)
    return Response(body, status=status.HTTP_404_NOT_FOUND)


def _not_found():
    return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)


def _requested_model_for_lease(data, sticky_key: str) -> str:
    """Model the broker is leasing for.

    An explicit ``model`` wins. Current sidecars omit it and send
    ``sticky_key=agent:<username>``; the fleet row's model covers those pods
    until the sidecar image forwards the query param.
    """
    explicit = str((data or {}).get('model') or '').strip()[:128]
    if explicit:
        return explicit
    if not sticky_key.startswith('agent:'):
        return ''
    username = sticky_key[len('agent:'):].strip().lower()[:47]
    if not username:
        return ''
    from fleet.models import FleetAgent
    model = (
        FleetAgent.objects.filter(agent_username=username)
        .values_list('model', flat=True)
        .first()
    )
    return str(model or '').strip()[:128]


def _lease_ttl() -> int:
    return int(getattr(settings, 'HIVE_COORDINATOR_LEASE_TTL', 900))


def _quota_routing_rank(entry: TokenEntry) -> tuple:
    """Prefer fresh, measured subscription capacity — and drain it evenly.

    Two jobs (HIVE-796):

    1. Tier by evidence.  Credentials with a fresh, trusted quota snapshot and
       proven remaining capacity rank first (tier 0); opaque/stale/unsupported
       telemetry ranks neutral (tier 1); fresh proven exhaustion ranks last
       (tier 2).  Exhausted entries are not removed entirely because an
       account with a model-specific allowance may still be usable even when a
       coarse aggregate window is at zero; real-traffic status reporting
       remains authoritative for cooldowns.

    2. Even drain WITHIN the proven-capacity tier, for ALL providers.  Rank by
       most-remaining-quota in 10% bands so sibling accounts drain uniformly
       (operator 2026-07-16: xAI org-xai-302a608a at 71% credits while
       org-xai-e4dd960e sat at 100% never used; Claude 53% vs 98%).  Banding
       keeps the ranking stable within a burst so the live-lease-count
       tie-break can still spread simultaneous callers.

    Confidence is gated PER PROVIDER (merged from main): Claude/Anthropic full
    -login bundles expose exact 5h/weekly windows, so only 'exact' is trusted;
    xAI/Grok device-flow accounts expose measured SuperGrok credits, so 'ok'
    is trusted too; OpenAI/Codex accounts expose exact usage windows via the
    wham usage endpoint (codex.bar parity), so 'exact' is trusted.  Providers
    without an entry are never ranked on telemetry we have not validated —
    they stay neutral.

    Returns (tier, band).  Callers that only care about failover MUST compare
    element [0]; comparing the whole tuple also compares the drain band, which
    would re-place a sticky assignment every 10% of drain (see
    _place_xai_sticky_assignment).
    """
    accepted_confidence = {
        TokenPool.Provider.ANTHROPIC: {'exact'},
        TokenPool.Provider.OPENAI: {'exact'},
        TokenPool.Provider.XAI: {'ok', 'exact'},
    }.get(entry.pool.provider)
    if accepted_confidence is None:
        return (1, 0)
    quota = token_coordinator.quota_snapshot_status(entry.quota_snapshot or {})
    if quota.get('status') != 'ok' or quota.get('confidence') not in accepted_confidence:
        return (1, 0)
    minimum = quota.get('min_left_percent')
    if minimum is None:
        return (1, 0)
    if float(minimum) <= 0:
        return (2, 0)
    return (0, -int(float(minimum) // 10))


def _sticky_assignment_hash(owner, provider: str, sticky_key: str) -> str:
    """Return a non-reversible, owner-scoped identity for durable placement."""
    material = f'{owner.owner_kind}\0{owner.owner_id}\0{provider}\0{sticky_key}'
    return hashlib.sha256(material.encode('utf-8', 'ignore')).hexdigest()


def _place_xai_sticky_assignment(candidates, owner, sticky_key, now):
    """Choose the least-assigned healthy xAI account, transactionally.

    Account assignment—not request count—is the fairness unit. That distributes
    independent agents/sessions while keeping each conversation on one provider
    account for prefix-cache locality. Locking the small set of owner pools makes
    simultaneous first requests observe each other's assignment.
    """
    provider = TokenPool.Provider.XAI
    digest = _sticky_assignment_hash(owner, provider, sticky_key)
    ttl = int(getattr(settings, 'HIVE_TOKENPOOL_STICKY_TTL', 7 * 24 * 3600))
    expires_at = now + timedelta(seconds=ttl)
    candidate_by_id = {entry.id: entry for entry in candidates}
    # HIVE-796 merge: _quota_routing_rank returns (tier, drain_band), and the
    # two halves of this function need DIFFERENT comparisons (review @haru):
    #
    #   RETENTION  — tier only. Comparing the full tuple would re-place a
    #     healthy assignment every time its account crossed a 10% band, churning
    #     the agent/account binding on the one provider whose prefix cache is
    #     account-scoped.
    #   PLACEMENT  — full rank. A NEW identity must honour the drain band, or
    #     even drain is defeated: with tier alone, placement falls through to
    #     assignment count and a fresh caller can land on a 20%-remaining
    #     account while a 100%-remaining sibling idles. That is precisely the
    #     skew this task exists to correct.
    #
    # Rank each candidate once; the two halves then read the part they need.
    ranks = {entry.id: _quota_routing_rank(entry) for entry in candidates}
    best_tier_value = min(rank[0] for rank in ranks.values())
    best_tier = [
        entry for entry in candidates
        if ranks[entry.id][0] == best_tier_value
    ]

    with transaction.atomic():
        # Serialize placements within the owner pool(s). TokenEntry row locks are
        # insufficient because two new identities can otherwise both count zero.
        list(
            TokenPool.objects.select_for_update()
            .filter(id__in=sorted({entry.pool_id for entry in best_tier}, key=str))
            .order_by('id')
            .values_list('id', flat=True)
        )
        assignment = (
            TokenStickyAssignment.objects.select_for_update()
            .filter(provider=provider, sticky_key_hash=digest)
            .first()
        )
        if (
            assignment is not None
            and assignment.expires_at > now
            and assignment.entry_id in candidate_by_id
            and ranks[assignment.entry_id][0] == best_tier_value
        ):
            chosen = candidate_by_id[assignment.entry_id]
            assignment.expires_at = expires_at
            assignment.save(update_fields=['expires_at', 'last_seen_at'])
            return chosen, digest

        # New/replacement placement is quota-weighted: narrow to the best FULL
        # rank (tier AND drain band) inside the best tier, then balance by
        # least-assigned only WITHIN that band. Balancing across bands is what
        # let a new caller land on the most-drained sibling.
        best_rank = min(ranks[entry.id] for entry in best_tier)
        placement_pool = [
            entry for entry in best_tier if ranks[entry.id] == best_rank
        ]
        counts = {
            row['entry_id']: row['count']
            for row in (
                TokenStickyAssignment.objects
                .filter(entry_id__in=[entry.id for entry in placement_pool], expires_at__gt=now)
                .values('entry_id')
                .annotate(count=Count('id'))
            )
        }
        chosen = min(placement_pool, key=lambda entry: (
            counts.get(entry.id, 0), entry.live_leases, entry.priority, entry.created_at,
        ))
        TokenStickyAssignment.objects.update_or_create(
            provider=provider,
            sticky_key_hash=digest,
            defaults={'entry': chosen, 'expires_at': expires_at},
        )
        return chosen, digest


def _refresh_xai_sticky_assignment(digest: str, entry: TokenEntry, now):
    """Bind a placement to the account that actually yielded a usable token."""
    ttl = int(getattr(settings, 'HIVE_TOKENPOOL_STICKY_TTL', 7 * 24 * 3600))
    TokenStickyAssignment.objects.update_or_create(
        provider=TokenPool.Provider.XAI,
        sticky_key_hash=digest,
        defaults={
            'entry': entry,
            'expires_at': now + timedelta(seconds=ttl),
        },
    )


def _order_token_candidates(
    candidates,
    *,
    owner,
    provider: str,
    now,
    sticky_key: str = '',
    preferred_entry_id: str = '',
):
    """Order already-eligible entries at the production allocation boundary.

    This is deliberately the one shared boundary used by the broker raw-token
    path and the provider-token-free HIVE-1824 live QA fixture.  It performs no
    token decryption, refresh, provider request, or lease write; those happen
    only after the production caller receives this ordered candidate list.
    """
    candidates = list(candidates)
    candidates.sort(key=lambda entry: (
        _quota_routing_rank(entry),
        entry.live_leases,
        entry.priority,
        entry.created_at,
    ))

    if not candidates:
        return candidates, ''

    # HIVE-796: a pin/preference must not promote an entry whose fresh trusted
    # quota is proven exhausted while a healthier sibling exists.  When every
    # candidate is exhausted, promotion applies again as the safe last resort.
    def _promotable(entry):
        if _quota_routing_rank(entry)[0] < 2:
            return True
        return all(_quota_routing_rank(candidate)[0] >= 2 for candidate in candidates)

    # xAI keeps the durable least-assigned allocator.  Other providers retain
    # their cache-backed optimization until their durable migration lands.
    xai_sticky_digest = ''
    if sticky_key and provider == TokenPool.Provider.XAI:
        assigned, xai_sticky_digest = _place_xai_sticky_assignment(
            candidates, owner, sticky_key, now,
        )
        candidates.remove(assigned)
        candidates.insert(0, assigned)
    elif sticky_key:
        from django.core.cache import cache as sticky_cache

        try:
            pinned_id = sticky_cache.get(
                f'hive:tokenpool:sticky:{provider}:{sticky_key}'
            )
        except Exception:
            pinned_id = None  # stickiness is an optimization — never break leasing
        if pinned_id:
            pinned = next(
                (entry for entry in candidates if str(entry.id) == str(pinned_id)),
                None,
            )
            if pinned is not None and _promotable(pinned):
                candidates.remove(pinned)
                candidates.insert(0, pinned)

    if preferred_entry_id:
        preferred = next(
            (
                entry for entry in candidates
                if str(entry.id) == str(preferred_entry_id)
            ),
            None,
        )
        if preferred is not None and _promotable(preferred):
            candidates.remove(preferred)
            candidates.insert(0, preferred)

    return candidates, xai_sticky_digest


def _broker_owner_scopes(owner) -> list:
    """Owner scopes a broker request may draw tokens from, in priority order.

    Personal-ownership model (operator 2026-07-11): credentials belong to the
    USER who added them, not the org — "the credentials are mine, the agents
    using them are mine". Fleet-internal workloads (the synthetic org scope)
    therefore draw from the PLATFORM OWNER's personal pool first, then the
    shared org pool (legacy residence / genuinely org-shared credentials).
    This union makes the org→user credential migration safe in either order:
    tokens keep serving whether an entry lives in the org pool or the owner's.
    """
    scopes = [(owner.owner_kind, owner.owner_id)]
    if owner.owner_kind == TokenPool.OwnerKind.ORG and owner.owner_id == TokenPool.ORG_OWNER_ID:
        fleet_owner = str(getattr(settings, 'HIVE_FLEET_OWNER_USER_ID', '') or '').strip()
        if fleet_owner:
            scopes.insert(0, (TokenPool.OwnerKind.USER, fleet_owner))
    return scopes


def _eligible_token_candidates(scopes, provider: str, now):
    """Load the exact owner/provider-scoped allocator candidate set."""
    candidates = list(
        TokenEntry.objects
        .active_for_scopes(scopes, provider, now=now)
        .select_related('pool')
        .annotate(
            live_leases=Count(
                'leases', filter=Q(leases__expires_at__gt=now)
            )
        )
    )
    return [
        entry for entry in candidates
        if token_coordinator.quota_routing_eligible(entry, now=now)
    ]


def _entry_metadata(entry: TokenEntry) -> dict:
    """Display/metadata view of an entry — NEVER includes token plaintext (§2)."""
    return {
        'entry_id': str(entry.id),
        'label': entry.label,
        'provider': entry.pool.provider,
        'active': entry.active,
        'agent_leasing_enabled': entry.agent_leasing_enabled,
        'priority': entry.priority,
        'cooldown_until': entry.cooldown_until.isoformat() if entry.cooldown_until else None,
        'token_fingerprint': entry.token_fingerprint,
        'token_hint': getattr(entry, 'token_hint', '') or '',
        'created_at': entry.created_at.isoformat(),
        'updated_at': entry.updated_at.isoformat(),
    }


def _owned_entry(request, entry_id):
    """Resolve an entry within the caller's verified owner scope, else None.

    The owner scope is derived from the verified token (``resolve_owner``) and the
    filter pins ``pool__owner_*`` to it, so an ``entry_id`` belonging to another
    owner simply does not match — it can never be read, mutated, or even confirmed
    to exist.
    """
    owner = coordinator.resolve_owner(request)
    if not owner.owner_id:
        return None
    return (
        TokenEntry.objects
        .filter(
            id=entry_id,
            pool__owner_kind=owner.owner_kind,
            pool__owner_id=owner.owner_id,
            deleted_at__isnull=True,
        )
        .select_related('pool')
        .first()
    )


class ModelTokenView(APIView):
    """POST model-token — broker/workload-gated raw token read + lease issue (§2)."""

    def post(self, request):
        # §2: raw-token path requires a verified broker/workload token (Coordinator
        # audience + broker role). A user/admin JWT is refused here — and because a
        # broker token is required, we answer non-brokers with 404 so the endpoint
        # itself is non-enumerable to ordinary callers (§5.2 / §5.8).
        if not coordinator.is_broker_request(request):
            return _not_found()

        provider = (request.data or {}).get('provider')
        if provider not in dict(TokenPool.Provider.choices):
            return _not_found()

        # HIVE-796: caller-identity lease stickiness. Upstream provider prefix
        # caches (x.ai/Anthropic/OpenAI) are PER-ACCOUNT — re-leasing a
        # different subscription account to the SAME agent forces a full
        # re-prefill of its 100-400K context upstream (2026-07-16 grok TTFT
        # incident: per-request account rotation = 20s+ TTFTs). Cortex sends
        # sticky_key=user:<id> (CTX-482); we pin that caller to one entry while
        # it stays healthy, fail over to the healthiest sibling when it isn't,
        # and UPDATE the pin on failover (no flap-back).
        sticky_key = str((request.data or {}).get('sticky_key') or '').strip()[:128]
        preferred_entry_id = str(
            (request.data or {}).get('preferred_entry_id') or ''
        ).strip()[:64]
        # A caller that has positively identified its current lease as unusable
        # must be able to rotate away from it without defeating HIVE-796
        # stickiness. Exclusion is evaluated only inside the already owner-scoped,
        # provider-scoped candidate set; it cannot expose or select another
        # owner's entry.
        exclude_entry_id = str(
            (request.data or {}).get('exclude_entry_id') or ''
        ).strip()[:64]
        force_refresh = str(
            (request.data or {}).get('force_refresh') or ''
        ).strip().lower() in {'1', 'true', 'yes', 'on'}

        owner = coordinator.resolve_owner(request)
        if not owner.owner_id:
            return _not_found()

        # Personal-ownership model: fleet (org-scope) brokers draw from the
        # platform owner's personal pool + the shared org pool (see helper).
        scopes = _broker_owner_scopes(owner)
        now = timezone.now()
        candidates = _eligible_token_candidates(scopes, provider, now)
        if not candidates:
            # PLAT-881 self-heal on the BROKER path. Before declaring the pool dry
            # (which 503s every claude agent at spawn), attempt to auto-revive a
            # dead-but-recoverable token whose recovery backoff has lapsed, then
            # re-query once. This recovery previously lived ONLY in
            # select_token_for_owner; the broker's raw-token path bypassed it, so an
            # auth-killed token like cl2-primary sat active=False ~13h after its
            # cooldown lapsed and dried the coordinator pool (2026-06-26 half-fleet
            # outage; recurred 2026-06-27). OAuth tokens (sk-ant-oat01-) can't be
            # probed, so recover_expired_dead_tokens revives them optimistically — a
            # genuinely dead token is re-killed by the next real auth report, so the
            # retry rate is bounded by the backoff, never a crash loop.
            recovered = token_coordinator.recover_expired_dead_tokens(
                provider=provider,
                owner_scope=TokenEntry.objects.for_scopes(scopes, provider),
            ).get('recovered')
            if recovered:
                now = timezone.now()
                candidates = _eligible_token_candidates(scopes, provider, now)
        if not candidates:
            # PLAT-881 companion: the pool may also be dry because EVERY token is
            # active but sitting in a lingering rate-limit cooldown (both org Codex
            # accounts throttled in the same window — the 2026-06-26/27 codex fleet
            # crash-loops that only cleared on a manual cooldown wipe). Clear stale
            # (not fresh) cooldowns before declaring dry, then re-query once. Bounded:
            # a still-throttled token is re-cooled by the next rate-limit report.
            cleared = token_coordinator.recover_stale_cooldown_tokens(
                provider=provider,
                owner_scope=TokenEntry.objects.for_scopes(scopes, provider),
            ).get('cleared')
            if cleared:
                now = timezone.now()
                candidates = _eligible_token_candidates(scopes, provider, now)
        if not candidates:
            # Default-deny: no org-pool borrow unless explicitly enabled per owner.
            # Tell the broker HOW LONG it will stay dry. Without this Cortex can
            # only guess a TTL, so it re-advertised grok-4.5 as routable every
            # few minutes and agents kept probing a pool whose accounts were all
            # cooling for 11-160h (operator 2026-08-05). This is not an
            # enumeration leak: the caller is already broker-authenticated and
            # the number is an aggregate horizon, never per-entry identity.
            return _pool_dry_response(provider, scopes)
        if exclude_entry_id:
            candidates = [
                entry for entry in candidates
                if str(entry.id) != exclude_entry_id
            ]
            if not candidates:
                return _not_found()
        # Drop accounts that cannot serve the requested model BEFORE sticky
        # promotion. A pinned Go row must not win a gpt-6-astra lease.
        requested_model = _requested_model_for_lease(request.data, sticky_key)
        if requested_model:
            capable = [
                entry for entry in candidates
                if token_coordinator.model_supports_entry(entry, requested_model)
            ]
            if not capable:
                return _pool_dry_response(provider, scopes)
            candidates = capable
        candidates, xai_sticky_digest = _order_token_candidates(
            candidates,
            owner=owner,
            provider=provider,
            now=now,
            sticky_key=sticky_key,
            preferred_entry_id=preferred_entry_id,
        )
        selected = None
        for entry in candidates:
            try:
                # FLAP-FREE: serve a REFRESHED token. _selection_from_entry refreshes
                # the Codex/OpenAI access token from its (rotating) refresh_token when
                # near expiry (non-openai entries pass through unchanged), so the broker
                # raw-token path never serves a stale/expired codex token — the root of
                # the codex availability flap (broker path previously did entry.get_token()
                # with no refresh, serving the ~6h-stale import).
                selection = token_coordinator._selection_from_entry(
                    entry,
                    refresh_openai=True,
                    force_openai_refresh=(
                        force_refresh and provider == TokenPool.Provider.OPENAI
                    ),
                    # Codex external-auth waits 10s for its host refresh callback.
                    openai_refresh_timeout_seconds=5,
                )
                token = selection.token
            except token_coordinator.OAuthRefreshError as exc:
                # Try the next candidate without serving stale material. A terminal
                # OpenAI token-endpoint response parks the revoked rotating grant
                # until exact-row re-auth; transient OpenAI and xAI failures cool.
                if isinstance(exc, token_coordinator.OpenAIOAuthRefreshError):
                    token_coordinator.apply_openai_refresh_failure(entry, exc)
                elif isinstance(exc, token_coordinator.AnthropicOAuthRefreshError):
                    token_coordinator.apply_anthropic_refresh_failure(entry, exc)
                else:
                    token_coordinator.report_token_status(
                        entry.id,
                        error_code='oauth_refresh_failed',
                        cooldown_seconds=5 * 60,
                    )
                continue
            except ValueError:
                # A stale row encrypted under an old KEK must not turn the raw
                # broker path into a 500/retry storm. Park it for admin repair and
                # keep trying any other eligible entries.
                entry.active = False
                entry.last_status = TokenEntry.LastStatus.AUTH_FAILED
                entry.last_checked_at = timezone.now()
                entry.save(update_fields=['active', 'last_status', 'last_checked_at', 'updated_at'])
                continue
            selected = (entry, token)
            break
        if selected is None:
            return _not_found()

        entry, token = selected
        if xai_sticky_digest:
            _refresh_xai_sticky_assignment(xai_sticky_digest, entry, timezone.now())
        elif sticky_key:
            from django.core.cache import cache as _sticky_cache
            _sticky_ttl = int(getattr(settings, 'HIVE_TOKENPOOL_STICKY_TTL', 7 * 24 * 3600))
            try:
                _sticky_cache.set(
                    f'hive:tokenpool:sticky:{provider}:{sticky_key}', str(entry.id), timeout=_sticky_ttl,
                )
            except Exception:
                pass  # stickiness is an optimization — never break leasing
        leased_at = timezone.now()
        # A durable lease and its usage metric are one accounting event. The F()
        # update avoids lost increments under concurrent brokers; active_sessions
        # belongs to the separate pick_token/release_session lifecycle.
        with transaction.atomic():
            if entry.pool.provider == TokenPool.Provider.ANTHROPIC:
                current = TokenEntry.objects.select_for_update().get(pk=entry.pk)
                if (
                    not current.active
                    or current.token_fingerprint != entry.token_fingerprint
                    or (current.cooldown_until and current.cooldown_until > leased_at)
                ):
                    return _not_found()
            lease = TokenLease.objects.create(
                entry=entry,
                broker_identity=coordinator.broker_identity(request),
                owner_kind=entry.pool.owner_kind,
                owner_id=entry.pool.owner_id,
                provider=entry.pool.provider,
                expires_at=leased_at + timedelta(seconds=_lease_ttl()),
            )
            TokenEntry.objects.filter(pk=entry.pk).update(
                usage_count=F('usage_count') + 1,
                last_used_at=leased_at,
                updated_at=leased_at,
            )
        return Response({
            'token': token,
            'entry_id': str(entry.id),
            'label': entry.label,
            'provider': entry.pool.provider,
            'lease_id': str(lease.id),
            'lease_expires_at': lease.expires_at.isoformat(),
        })


class TokenPoolAllocatorQaView(APIView):
    """Run the HIVE-796 allocator contract without provider credentials/egress.

    The fixture is staff-only and purpose-bound to HIVE-1824.  It creates a
    synthetic owner scope inside one database transaction, exercises the exact
    production candidate query + ordering boundary, and deletes every row before
    commit.  Other transactions can never observe the uncommitted scope; a crash
    rolls the whole fixture back instead of leaving residue.
    """

    fixture_key = 'HIVE-1824'

    def post(self, request):
        if (
            not getattr(request.user, 'is_staff', False)
            or (request.data or {}).get('fixture') != self.fixture_key
        ):
            return _not_found()

        provider = TokenPool.Provider.XAI
        now = timezone.now()
        # quota freshness is defined in aware UTC; passing Hive's USE_TZ=False
        # wall clock into _quota_now would relabel local time as UTC.
        quota_now = token_coordinator._quota_now()
        future_reset = quota_now + timedelta(hours=4)
        fixture_nonce = uuid.uuid4().hex
        fixture_owner_id = (
            f'qa-hive1824-{getattr(request.user, "id", "unknown")}-'
            f'{fixture_nonce}'
        )[:128]
        owner = coordinator.OwnerScope(
            owner_kind=TokenPool.OwnerKind.USER,
            owner_id=fixture_owner_id,
        )
        scopes = [(owner.owner_kind, owner.owner_id)]

        def quota_snapshot(left_percent, *, reset_at=None):
            window = {'id': 'credits', 'left_percent': left_percent}
            if reset_at is not None:
                window['resets_at'] = reset_at.isoformat()
            return {
                'source': 'grok_xai_usage',
                'confidence': 'ok',
                'fetched_at': quota_now.isoformat(),
                'windows': [window],
            }

        def set_proven_zero(entry):
            entry.quota_snapshot = quota_snapshot(0, reset_at=future_reset)
            entry.save(update_fields=['quota_snapshot', 'updated_at'])

        with transaction.atomic():
            pool = TokenPool.objects.create(
                owner_kind=owner.owner_kind,
                owner_id=owner.owner_id,
                provider=provider,
                display_name='HIVE-1824 ephemeral allocator QA fixture',
            )

            shapes = {
                'fresh_trusted_quota': [],
                'coarse_zero_no_future_reset': [],
                'proven_zero_future_reset': [],
                'cooldown': [],
                'auth_failed': [],
                'revoked': [],
            }

            def create_entry(
                shape,
                label,
                *,
                snapshot,
                active=True,
                cooldown_until=None,
                last_status=TokenEntry.LastStatus.UNKNOWN,
                priority=0,
            ):
                # Empty secret fields are intentional: this boundary never calls
                # set_token/get_token/_selection_from_entry.  The fingerprint is
                # only a fixture-row uniqueness key, not provider material.
                entry = TokenEntry.objects.create(
                    pool=pool,
                    label=label,
                    active=active,
                    cooldown_until=cooldown_until,
                    last_status=last_status,
                    priority=priority,
                    quota_snapshot=snapshot,
                    token_ciphertext='',
                    wrapped_dek='',
                    dek_key_id='qa-fixture-no-key',
                    token_fingerprint=hashlib.sha256(
                        f'{fixture_nonce}\0{label}'.encode()
                    ).hexdigest(),
                    token_hint='',
                )
                shapes[shape].append(entry)
                return entry

            fresh_high = create_entry(
                'fresh_trusted_quota',
                'fresh-high',
                snapshot=quota_snapshot(92),
                priority=9,
            )
            fresh_mid = create_entry(
                'fresh_trusted_quota',
                'fresh-mid',
                snapshot=quota_snapshot(61),
                priority=0,
            )
            coarse_entries = [
                create_entry(
                    'coarse_zero_no_future_reset',
                    f'coarse-zero-{index}',
                    snapshot=quota_snapshot(0),
                    priority=index,
                )
                for index in range(2)
            ]
            for index in range(2):
                create_entry(
                    'proven_zero_future_reset',
                    f'proven-zero-{index}',
                    snapshot=quota_snapshot(0, reset_at=future_reset),
                )
                create_entry(
                    'cooldown',
                    f'cooldown-{index}',
                    snapshot=quota_snapshot(80 - index),
                    cooldown_until=now + timedelta(minutes=2 + index),
                )
                create_entry(
                    'auth_failed',
                    f'auth-failed-{index}',
                    snapshot=quota_snapshot(80 - index),
                    last_status=TokenEntry.LastStatus.AUTH_FAILED,
                )
                create_entry(
                    'revoked',
                    f'revoked-{index}',
                    snapshot=quota_snapshot(80 - index),
                    active=False,
                    last_status=TokenEntry.LastStatus.AUTH_FAILED,
                )

            # Same-instrument positive controls: prove the fixture rows and a
            # selectable result are visible before accepting any empty/dry result.
            visible_shape_counts = {
                shape: TokenEntry.objects.filter(
                    id__in=[entry.id for entry in entries],
                    pool=pool,
                ).count()
                for shape, entries in shapes.items()
            }
            initial_candidates = _eligible_token_candidates(scopes, provider, now)
            initial_candidate_refs = sorted(entry.label for entry in initial_candidates)
            initial_candidate_ranks = {
                entry.label: list(_quota_routing_rank(entry))
                for entry in initial_candidates
            }
            initial_candidate_quota_status = {
                entry.label: token_coordinator.quota_snapshot_status(
                    entry.quota_snapshot or {},
                )
                for entry in initial_candidates
            }

            def select(sticky_key):
                candidates = _eligible_token_candidates(scopes, provider, now)
                ordered, digest = _order_token_candidates(
                    candidates,
                    owner=owner,
                    provider=provider,
                    now=now,
                    sticky_key=sticky_key,
                )
                if not ordered:
                    return None, digest, []
                selected = ordered[0]
                if digest:
                    _refresh_xai_sticky_assignment(digest, selected, now)
                return selected, digest, ordered

            returning_first, returning_digest, _ = select('qa:returning')
            returning_second, _, _ = select('qa:returning')
            new_key, _, _ = select('qa:new-key')

            # Cooldown is the other production failover boundary.  Clearing the
            # old account later must not flap the updated affinity back.
            cooldown_first, _, _ = select('qa:cooldown-failover')
            fresh_high.cooldown_until = now + timedelta(minutes=5)
            fresh_high.save(update_fields=['cooldown_until', 'updated_at'])
            cooldown_failover, _, _ = select('qa:cooldown-failover')
            fresh_high.cooldown_until = None
            fresh_high.save(update_fields=['cooldown_until', 'updated_at'])
            cooldown_no_flap_back, _, _ = select('qa:cooldown-failover')

            # Production-order failover: pin -> proven exhaustion -> healthy
            # sibling -> original recovery. Retention must not flap back.
            set_proven_zero(fresh_high)
            failover, _, _ = select('qa:returning')
            fresh_high.quota_snapshot = quota_snapshot(100)
            fresh_high.save(update_fields=['quota_snapshot', 'updated_at'])
            no_flap_back, _, _ = select('qa:returning')

            # Remove both positive-capacity siblings from eligibility.  The two
            # coarse zero entries (no future reset) must remain a safe last
            # resort instead of producing a false pool_dry.
            set_proven_zero(fresh_high)
            set_proven_zero(fresh_mid)
            coarse_selected, _, coarse_candidates = select('qa:coarse-zero')

            # Now make the last-resort zeros authoritative until a future reset.
            # With every other shape cooled/auth-failed/revoked, the exact
            # production dry-pool response must be opaque 404 + bounded horizon.
            for entry in coarse_entries:
                set_proven_zero(entry)
            dry_candidates = _eligible_token_candidates(scopes, provider, now)
            dry_response = _pool_dry_response(provider, scopes)

            entry_ids = list(
                TokenEntry.objects.filter(pool=pool).values_list('id', flat=True)
            )
            sticky_ids = list(
                TokenStickyAssignment.objects.filter(
                    entry_id__in=entry_ids,
                ).values_list('id', flat=True)
            )
            pre_teardown = {
                'pool_rows': 1,
                'entry_rows': len(entry_ids),
                'sticky_assignments': len(sticky_ids),
                'lease_rows': TokenLease.objects.filter(
                    entry_id__in=entry_ids,
                ).count(),
                'secret_material_rows': TokenEntry.objects.filter(
                    id__in=entry_ids,
                ).exclude(token_ciphertext='').count(),
            }

            pool_id = pool.id
            pool.delete()
            residue = {
                'pool_rows': TokenPool.objects.filter(id=pool_id).count(),
                'entry_rows': TokenEntry.objects.filter(id__in=entry_ids).count(),
                'sticky_assignments': TokenStickyAssignment.objects.filter(
                    id__in=sticky_ids,
                ).count(),
                'lease_rows': TokenLease.objects.filter(
                    entry_id__in=entry_ids,
                ).count(),
            }

        observations = {
            'returning_sticky': {
                'first': returning_first.label if returning_first else None,
                'returning': returning_second.label if returning_second else None,
                'passed': bool(
                    returning_first
                    and returning_second
                    and returning_first.id == returning_second.id
                ),
            },
            'new_key_best_band': {
                'selected': new_key.label if new_key else None,
                'passed': bool(new_key and new_key.id == fresh_high.id),
            },
            'cooldown_failover_no_flap_back': {
                'initial': cooldown_first.label if cooldown_first else None,
                'failed_over_to': (
                    cooldown_failover.label if cooldown_failover else None
                ),
                'after_cooldown_clear': (
                    cooldown_no_flap_back.label if cooldown_no_flap_back else None
                ),
                'passed': bool(
                    cooldown_first
                    and cooldown_failover
                    and cooldown_no_flap_back
                    and cooldown_first.id == fresh_high.id
                    and cooldown_failover.id == fresh_mid.id
                    and cooldown_no_flap_back.id == cooldown_failover.id
                ),
            },
            'exhaustion_failover_no_flap_back': {
                'failed_over_to': failover.label if failover else None,
                'after_original_recovery': no_flap_back.label if no_flap_back else None,
                'passed': bool(
                    failover
                    and no_flap_back
                    and failover.id == fresh_mid.id
                    and no_flap_back.id == failover.id
                ),
            },
            'coarse_zero_last_resort': {
                'candidate_refs': sorted(entry.label for entry in coarse_candidates),
                'selected': coarse_selected.label if coarse_selected else None,
                'passed': bool(
                    coarse_selected
                    and len(coarse_candidates) == 2
                    and coarse_selected in coarse_entries
                ),
            },
            'genuinely_dry_pool': {
                'candidate_count': len(dry_candidates),
                'response': dict(dry_response.data),
                'status': dry_response.status_code,
                'passed': bool(
                    not dry_candidates
                    and dry_response.status_code == status.HTTP_404_NOT_FOUND
                    and dry_response.data.get('reason') == 'pool_dry'
                    and 0 < dry_response.data.get('retry_after_seconds', 0) <= 3600
                ),
            },
        }
        positive_controls = {
            'rows_visible_by_shape': visible_shape_counts,
            'initial_candidate_refs': initial_candidate_refs,
            'initial_candidate_ranks': initial_candidate_ranks,
            'initial_candidate_quota_status': initial_candidate_quota_status,
            'initial_candidate_count': len(initial_candidates),
            'selection_result_visible': returning_first is not None,
            'passed': bool(
                all(count == 2 for count in visible_shape_counts.values())
                and len(initial_candidates) == 4
                and returning_first is not None
            ),
        }
        teardown_passed = not any(residue.values())
        passed = bool(
            positive_controls['passed']
            and all(observation['passed'] for observation in observations.values())
            and pre_teardown['lease_rows'] == 0
            and pre_teardown['secret_material_rows'] == 0
            and teardown_passed
        )
        return Response({
            'fixture': self.fixture_key,
            'provider': provider,
            'owner_scope': 'requester-isolated-ephemeral',
            'passed': passed,
            'positive_controls': positive_controls,
            'observations': observations,
            'safety': {
                'provider_credential_rows': pre_teardown['secret_material_rows'],
                'leases_issued': pre_teardown['lease_rows'],
                'provider_egress_path': 'not-invoked',
                'transaction_visibility': 'uncommitted-then-deleted',
            },
            'teardown': {
                'before': pre_teardown,
                'residue': residue,
                'passed': teardown_passed,
            },
        })


class CoordinatorOwnerAPIView(APIView):
    """Owner administration excludes the narrowly scoped broker workload client."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if (request.auth or {}).get('client_id') == coordinator.COORDINATOR_SERVICE_CLIENT_ID:
            from rest_framework.exceptions import NotFound
            raise NotFound()


class TokenEntryListCreateView(CoordinatorOwnerAPIView):
    """GET list (owner-scoped, metadata only) / POST create — admin CRUD (§2/§5.1)."""

    def get(self, request):
        owner = coordinator.resolve_owner(request)
        if not owner.owner_id:
            return Response({'entries': []})
        entries = (
            TokenEntry.objects
            .filter(
                pool__owner_kind=owner.owner_kind,
                pool__owner_id=owner.owner_id,
                deleted_at__isnull=True,
            )
            .select_related('pool')
            .order_by('pool__provider', 'priority', 'created_at')
        )
        return Response({'entries': [_entry_metadata(e) for e in entries]})

    def post(self, request):
        body = request.data or {}
        provider = body.get('provider')
        token = body.get('token')
        label = (body.get('label') or '').strip()
        if provider not in dict(TokenPool.Provider.choices) or not token or not label:
            return Response(
                {'error': 'provider, token and label are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        owner = coordinator.resolve_owner(request)
        if not owner.owner_id:
            return _not_found()
        # owner is taken from the verified claim, NEVER from the body.
        pool, _ = TokenPool.objects.get_or_create(
            owner_kind=owner.owner_kind,
            owner_id=owner.owner_id,
            provider=provider,
        )
        entry = TokenEntry(pool=pool, label=label, priority=int(body.get('priority', 0) or 0))
        entry.set_token(token)
        entry.save()
        return Response(_entry_metadata(entry), status=status.HTTP_201_CREATED)


class TokenEntryDetailView(CoordinatorOwnerAPIView):
    """GET/PATCH/DELETE a single entry within the caller's owner scope (§3/§5.3/§5.4)."""

    def get(self, request, entry_id):
        entry = _owned_entry(request, entry_id)
        if entry is None:
            return _not_found()
        return Response(_entry_metadata(entry))

    def patch(self, request, entry_id):
        entry = _owned_entry(request, entry_id)
        if entry is None:
            return _not_found()
        body = request.data or {}
        if 'agent_leasing_enabled' in body:
            return Response(
                {
                    'error': (
                        'Agent leasing is owner-controlled; use the owner '
                        'token-pools API to reserve or release this credential.'
                    ),
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if 'label' in body:
            entry.label = str(body['label']).strip() or entry.label
        if 'active' in body:
            entry.active = bool(body['active'])
        if 'priority' in body:
            try:
                entry.priority = int(body['priority'])
            except (TypeError, ValueError):
                pass
        if 'cooldown_seconds' in body:
            secs = body['cooldown_seconds']
            if secs in (None, 0, '0'):
                entry.cooldown_until = None
            else:
                entry.cooldown_until = timezone.now() + timedelta(seconds=int(secs))
        if 'token' in body and body['token']:
            # Rotate the secret in place (same entry_id, new material).
            entry.set_token(body['token'])
        entry.save()
        return Response(_entry_metadata(entry))

    def delete(self, request, entry_id):
        entry = _owned_entry(request, entry_id)
        if entry is None:
            return _not_found()
        from hive_platform.token_coordinator import tombstone_token_entry
        deletion = tombstone_token_entry(
            entry,
            actor=str(getattr(request.user, 'email', '') or getattr(request.user, 'id', '')),
        )
        if deletion['active_leases']:
            return Response(
                {
                    'deleted': True,
                    'active_leases': deletion['active_leases'],
                    'lease_expires_at': (
                        deletion['lease_expires_at'].isoformat()
                        if deletion['lease_expires_at'] else None
                    ),
                },
                status=status.HTTP_202_ACCEPTED,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReportStatusView(APIView):
    """POST report-status — broker-only, lease-bound cool/deactivate (§3/§5.5).

    Anti-poisoning: the cooldown/deactivation is applied ONLY when the presented
    ``lease_id`` is a valid, unexpired lease that was issued to THIS broker for THE
    SAME ``entry_id``. A guessed entry_id, a missing/expired/wrong-broker lease, or
    a lease for a different entry all resolve to 404 with no state change. An
    idempotent retry of a still-valid lease is safe.
    """

    def post(self, request):
        if not coordinator.is_broker_request(request):
            return _not_found()
        body = request.data or {}
        lease_id = body.get('lease_id')
        entry_id = body.get('entry_id')
        if not lease_id or not entry_id:
            return _not_found()

        lease = TokenLease.objects.filter(id=lease_id).select_related('entry__pool').first()
        if lease is None or not lease.is_valid():
            return _not_found()
        # The lease must bind to THIS broker and the SAME entry it was issued for.
        if lease.broker_identity != coordinator.broker_identity(request):
            return _not_found()
        if str(lease.entry_id) != str(entry_id):
            return _not_found()

        entry = lease.entry
        action = body.get('action', 'cool')
        # HIVE-1747: Cortex sends classification.reason (auth_failed/rate_limited/
        # quota_exhausted). Persist it so the pre-dry self-heal sweep can hold a
        # provider-confirmed quota-exhausted credential non-routable for its
        # requested window even when the usage API is unreadable.
        reason = str(body.get('reason', '') or '').strip()
        if action == 'deactivate':
            token_coordinator.apply_status_report(entry, 'auth_failed', reason=reason or 'auth_failed')
        else:  # cool (default): rate-limit backoff
            secs = int(body.get('cooldown_seconds', 300) or 300)
            token_coordinator.apply_status_report(entry, 'rate_limited', retry_after=secs, reason=reason)

        # A raw OAuth token's quota/auth state is global to that credential, even
        # if the same token was copied into another owner/pool during migration or
        # canary work. Cool/deactivate same-fingerprint peers too, otherwise the
        # broker can immediately re-lease the identical exhausted token under a
        # different label and keep the agent "alive" in a quota loop.
        peers = (
            TokenEntry.objects
            .filter(
                pool__provider=entry.pool.provider,
                token_fingerprint=entry.token_fingerprint,
                deleted_at__isnull=True,
            )
            .exclude(id=entry.id)
            .select_related('pool')
        )
        for peer in peers:
            if action == 'deactivate':
                token_coordinator.apply_status_report(peer, 'auth_failed', reason=reason or 'auth_failed')
            else:
                token_coordinator.apply_status_report(peer, 'rate_limited', retry_after=secs, reason=reason)
        entry.refresh_from_db()
        return Response({
            'entry_id': str(entry.id),
            'active': entry.active,
            'cooldown_until': entry.cooldown_until.isoformat() if entry.cooldown_until else None,
            'applied': action,
            'peer_entries_updated': peers.count(),
        })


class OrgProviderResolveView(APIView):
    """POST org-provider-resolve — broker-gated org provider resolution (HIVE-314
    slice 2; contract HIVE-1862 §3, runtime carrier HIVE-2166).

    The claude-bridge spawn hook (``resolveOrgProviderForSpawn``) asks this
    endpoint, via the sidecar broker channel, which org-scoped Cortex provider
    applies to an agent. Response contract (redacted — key material never
    crosses this boundary; ``cortex_key_ref`` is an opaque id, not a secret):

        {"outcome": "resolved",    "org": <slug>, "provider": {...}, ...}   (post-HIVE-1889)
        {"outcome": "unavailable", "org": <slug>, "reason": "no_provider_configured"}

    Trust boundary (all four load-bearing):
    1. Broker-gated: a user/admin JWT is answered with the same 404 envelope as
       everything else — the endpoint is non-enumerable to ordinary callers.
    2. The ORG is server-derived from the broker token's minted claims
       (``owner_kind=org`` + ``owner_id=<slug>``); a body-supplied org is never
       trusted (§2 — client runtime eligibility is server-derived).
    3. Membership is proven against the ADMIN org-tenancy boundary (Admin is the
       SoT for organization membership — ``fleet/org_membership_sync`` reads
       ``/internal/organizations/<slug>/members/``), FAIL-CLOSED: if membership
       cannot be proven (Admin unavailable, org absent, principal not a member)
       the resolution is refused with the same 404 envelope. A local Hive table
       is never authoritative for membership.
    4. Until HIVE-1889 lands the ``HiveOrgProviderReference`` model, every
       membership-proven resolution returns ``no_provider_configured`` — the
       runtime's normal unavailable outcome (existing per-agent/fallback token
       path proceeds unchanged; pre-cutover inert by construction).
    """

    def post(self, request):
        if not coordinator.is_broker_request(request):
            return _not_found()

        owner = coordinator.resolve_owner(request)
        org_slug = str(owner.owner_id or '').strip().lower() if (
            owner.owner_kind == TokenPool.OwnerKind.ORG
        ) else ''
        if not org_slug:
            # A broker token without an org owner scope has no org provider to
            # resolve; same opaque envelope (non-enumerable).
            return _not_found()

        principal = str((request.data or {}).get('principal') or '').strip().lower()
        if not principal or '@' not in principal:
            return _not_found()

        # Fail-closed Admin-SoT membership proof. _existing_org_member_emails
        # returns (None, error) on ANY service failure — unproven membership is
        # refused, never assumed.
        from fleet.org_membership_sync import _existing_org_member_emails

        members, membership_error = _existing_org_member_emails(org_slug)
        if members is None or principal not in members:
            return _not_found()

        # HIVE-1889 lands HiveOrgProviderReference; until then the registry is
        # empty by construction and every proven resolution is honestly
        # "no provider configured for this org". The lookup step replaces this
        # block without changing the envelope or the trust boundary above.
        outcome = {
            'outcome': 'unavailable',
            'org': org_slug,
            'reason': 'no_provider_configured',
        }
        import logging

        logging.getLogger('hive.coordinator').info(
            'org-provider-resolve broker=%s org=%s principal=%s outcome=%s',
            coordinator.broker_identity(request), org_slug, principal, outcome['outcome'],
        )
        return Response(outcome)
