"""HIVE-360/HIVE-614 fail-loud observability for Connect personal-agent delivery.

PLAT-1254 Fail-Loud Mechanisms standard applied to the async
``_process_connect_delivery`` reply path:

- **Liveness signal:** every inbound Connect DM records exactly ONE Hive-side
  outcome here; the counters are exposed as Prometheus metrics at
  ``GET /v1/internal/connect/delivery/metrics`` so the mechanism's health is
  observable (accepted vs bridge_accepted vs reply_failed vs agent_not_running).
- **Failure path → whose DM (not log-only):** a persistent async reply failure
  increments ``hive_connect_delivery_reply_failed_total{stage=…}``; the
  ``k8s/hive-connect-delivery-alerts.yaml`` PrometheusRule fires on any increase
  and routes to the owning team via Alertmanager→Connect DM (the org's canonical
  fail-loud channel), carrying the failing stage. The reply path ALSO logs a
  ``level:error`` line with stable_id/conversation_id/message_id/stage for triage.
- **Durable accepted→Hive-handoff signal:** accepted message IDs are added to a
  Redis sorted set before the daemon thread starts and removed when the thread
  records ``bridge_accepted`` (runtime bridge HTTP accept — Hive handoff done),
  ``reply_posted`` (legacy/true user-visible reply if ever signalled), or
  ``reply_failed``. If the worker process dies, Redis retains the member, so
  Prometheus can alert on the pending count and oldest age instead of trying to
  pair unrelated rolling counters.

HIVE-1276: ``bridge_accepted`` is NOT a user-visible Connect reply. The runtime
owns the eventual reply-or-failure post after the bridge 202. Do not treat
bridge HTTP accept as ``reply_posted``.
"""
import time

from django.core.cache import cache

_TTL = 7 * 24 * 60 * 60  # counters are cache-backed; refreshed on every write
_KEY = 'hive:con360:delivery'
_PENDING_KEY = 'hive:con614:delivery:pending'
_PENDING_FALLBACK_KEY = f'{_PENDING_KEY}:fallback'

# Outcomes of ONE inbound delivery (Hive-side accounting).
OUTCOMES = (
    'accepted',          # running+deliverable agent; async worker spawned
    'bridge_accepted',   # runtime bridge HTTP accept (Hive handoff complete; not a user reply)
    'reply_posted',      # user-visible reply posted back to Connect (success)
    'reply_failed',      # async reply could not be produced/posted (fail-loud)
    'agent_not_running', # visible terminal: agent stopped OR running-but-undeliverable
    'unknown_agent',     # visible terminal status: agent unknown/deleted
    'ignored',           # empty content
    'duplicate',         # idempotent replay
    'invalid',           # missing required fields (400)
    'auth_failed',       # missing/invalid/stale HMAC authentication
    'rate_limited',      # authenticated owner/agent tuple exceeded its bound
    'binding_required',  # required signed owner/org/message binding absent
    'owner_mismatch',    # signed owner does not own target claw
    'organization_mismatch', # signed org is not owner's canonical personal org
    'participant_mismatch',  # signed agent participant does not match stable id
)
# Keep historical stages for counter continuity (cortex/connect_post from the
# pre-HIVE-610 path) plus the runtime-bridge stage used by the current
# personal-agent delivery path (HIVE-610), and the HIVE-614 durable-delivery
# pre-thread stages (reservation_store, pending_store, thread_start). Without
# an entry here a failure is recorded in cache but omitted from Prometheus
# output.
FAILURE_STAGES = (
    'reservation_store', 'pending_store', 'thread_start', 'cortex',
    'connect_post', 'runtime_bridge', 'unknown',
)

# HIVE-1766: tenant-boundary mismatch outcomes carry a per-owner breakdown so
# the production security alert can allowlist expected QA-fixture negative-test
# denials (e.g. qa-hive-fixture owner 252) instead of firing on every burst.
MISMATCH_OUTCOMES = frozenset({
    'owner_mismatch', 'organization_mismatch', 'participant_mismatch',
})

# PLAT-7972: HIVE-725 ring-release harness negative probes carry `release_probe=`
# / `cutover_probe=` query params. Requests attributed to a probe marker are
# recorded in a dedicated probe series (probe="release"/"cutover") instead of
# the base series (probe=""), so the security alerts
# (HiveConnectDeliveryInvalidSignature / TenantBoundaryDenied /
# AbnormalDenialVolume) can exclude intentional rollout probes via probe=""
# while real unsigned/mismatched delivery traffic still alerts unchanged.
PROBE_MARKERS = frozenset({'release', 'cutover'})


def _incr(key):
    try:
        cache.add(key, 0, _TTL)
        cache.incr(key)
    except Exception:
        try:
            cache.set(key, (cache.get(key) or 0) + 1, _TTL)
        except Exception:
            pass


def record(outcome, *, stage='', claw_owner='', probe=''):
    """Record ONE terminal delivery outcome (and, for reply_failed, the stage).

    ``claw_owner`` is tracked for the tenant-boundary mismatch outcomes so the
    alert rule can distinguish expected QA-fixture negative-test denials from
    real cross-tenant attempts (HIVE-1766).

    ``probe`` attributes HIVE-725 ring-release harness negative probes
    (PLAT-7972): a probe-attributed request increments ONLY its dedicated
    ``probe=<marker>`` series — it does not count as a real delivery denial,
    so the security alerting series (probe="") stays free of rollout noise.
    """
    if probe in PROBE_MARKERS:
        _incr(f'{_KEY}:{outcome}:probe:{probe}')
        return
    _incr(f'{_KEY}:{outcome}')
    if claw_owner and outcome in MISMATCH_OUTCOMES:
        owner_key = f'{_KEY}:{outcome}:owner:{claw_owner}'
        _incr(owner_key)
        # Track the known owner so snapshot() can enumerate without a backend
        # scan (works for both LocMemCache and Redis-backed Django cache).
        list_key = f'{_KEY}:{outcome}:owners'
        try:
            owners = list(cache.get(list_key) or [])
            if claw_owner not in owners:
                owners.append(claw_owner)
                cache.set(list_key, owners, _TTL)
        except Exception:
            pass
    if outcome == 'reply_failed':
        _incr(f'{_KEY}:reply_failed:{stage or "unknown"}')


def _redis_pending_store():
    """Return the Redis client + namespaced key used by Django's cache.

    Unit tests use LocMemCache, so callers fall back to a cache-backed mapping
    when this backend-specific capability is absent. Production uses the shared
    Redis cache configured in ``hive_project.settings``.
    """
    backend = getattr(cache, '_cache', None)
    get_client = getattr(backend, 'get_client', None)
    make_key = getattr(cache, 'make_key', None)
    if not callable(get_client) or not callable(make_key):
        return None, None
    return get_client(_PENDING_KEY, write=True), make_key(_PENDING_KEY)


def _fallback_pending_update(message_id, started_at=None, *, remove=False):
    try:
        pending = dict(cache.get(_PENDING_FALLBACK_KEY) or {})
        if remove:
            pending.pop(str(message_id), None)
        else:
            pending.setdefault(str(message_id), float(started_at or time.time()))
        # Pending work is an invariant record, not a rolling telemetry sample.
        # Keep it until the matching terminal path explicitly removes it; an
        # age-based expiry would make a permanently lost delivery silently
        # disappear from the alert after the timeout.
        cache.set(_PENDING_FALLBACK_KEY, pending, timeout=None)
        return pending
    except Exception:
        return {}


def _fallback_pending_snapshot():
    try:
        return dict(cache.get(_PENDING_FALLBACK_KEY) or {})
    except Exception:
        return {}


def mark_pending(message_id, *, started_at=None):
    """Durably mark an accepted message pending before its worker starts.

    Return ``True`` only after the invariant record is stored.  Callers must not
    acknowledge a delivery when this returns ``False``; accepting without the
    record would recreate the silent-loss gap this module closes.
    """
    message_id = str(message_id)
    started_at = float(started_at or time.time())
    try:
        client, key = _redis_pending_store()
        if client is not None:
            client.zadd(key, {message_id: started_at}, nx=True)
            return True
    except Exception:
        pass
    return bool(_fallback_pending_update(message_id, started_at))


def finish_pending(message_id):
    """Remove a message after its worker records a terminal outcome."""
    message_id = str(message_id)
    try:
        client, key = _redis_pending_store()
        if client is not None:
            client.zrem(key, message_id)
            return
    except Exception:
        pass
    _fallback_pending_update(message_id, remove=True)


def pending_snapshot(*, now=None):
    """Return durable pending count and oldest age in seconds."""
    now = float(now or time.time())
    try:
        client, key = _redis_pending_store()
        if client is not None:
            count = int(client.zcard(key))
            oldest = client.zrange(key, 0, 0, withscores=True)
            age = max(0.0, now - float(oldest[0][1])) if oldest else 0.0
            return {'count': count, 'oldest_age_seconds': age}
    except Exception:
        pass
    pending = _fallback_pending_snapshot()
    ages = [max(0.0, now - float(value)) for value in pending.values()]
    return {
        'count': len(pending),
        'oldest_age_seconds': max(ages) if ages else 0.0,
    }


def snapshot() -> dict:
    snap = {o: int(cache.get(f'{_KEY}:{o}') or 0) for o in OUTCOMES}
    snap['reply_failed_stages'] = {
        st: int(cache.get(f'{_KEY}:reply_failed:{st}') or 0)
        for st in FAILURE_STAGES
        if cache.get(f'{_KEY}:reply_failed:{st}')
    }
    # HIVE-1766: per-owner breakdown for the tenant-boundary mismatch outcomes.
    snap['owners'] = {}
    for o in MISMATCH_OUTCOMES:
        owners = {}
        try:
            for owner in (cache.get(f'{_KEY}:{o}:owners') or []):
                owners[str(owner)] = int(cache.get(f'{_KEY}:{o}:owner:{owner}') or 0)
        except Exception:
            owners = {}
        snap['owners'][o] = owners
    # PLAT-7972: probe-attributed counts (harness negative probes), kept
    # separate from the base outcomes so alerting can exclude them.
    snap['probes'] = {}
    for o in OUTCOMES:
        probes = {}
        for p in sorted(PROBE_MARKERS):
            v = int(cache.get(f'{_KEY}:{o}:probe:{p}') or 0)
            if v:
                probes[p] = v
        if probes:
            snap['probes'][o] = probes
    snap['pending'] = pending_snapshot()
    return snap


def to_prometheus() -> str:
    snap = snapshot()
    lines = [
        '# HELP hive_connect_delivery_total Connect personal-agent delivery '
        'terminal outcomes (HIVE-355/HIVE-360).',
        '# TYPE hive_connect_delivery_total counter',
    ]
    for o in OUTCOMES:
        if o in MISMATCH_OUTCOMES:
            # HIVE-1766: emit per-owner series so the alert rule can allowlist
            # expected QA-fixture negative-test denials (claw_owner!="252").
            owners = snap['owners'].get(o) or {}
            for owner, count in sorted(owners.items()):
                lines.append(
                    f'hive_connect_delivery_total{{outcome="{o}", claw_owner="{owner}", probe=""}} {count}'
                )
        else:
            lines.append(f'hive_connect_delivery_total{{outcome="{o}", claw_owner="", probe=""}} {snap[o]}')
    # PLAT-7972: probe-attributed series (probe="release"/"cutover") — the
    # security alert expressions filter on probe="" so intentional harness
    # negative probes never page security, while remaining visible here.
    for o, probes in (snap.get('probes') or {}).items():
        for p, count in sorted(probes.items()):
            lines.append(
                f'hive_connect_delivery_total{{outcome="{o}", claw_owner="", probe="{p}"}} {count}'
            )
    lines.append('# HELP hive_connect_delivery_reply_failed_total Persistent async '
                 'reply failures by stage (fail-loud — alerted).')
    lines.append('# TYPE hive_connect_delivery_reply_failed_total counter')
    for st, v in snap['reply_failed_stages'].items():
        lines.append(f'hive_connect_delivery_reply_failed_total{{stage="{st}"}} {v}')
    lines.extend([
        '# HELP hive_connect_delivery_pending Accepted deliveries without a '
        'recorded terminal outcome.',
        '# TYPE hive_connect_delivery_pending gauge',
        f'hive_connect_delivery_pending {snap["pending"]["count"]}',
        '# HELP hive_connect_delivery_oldest_pending_age_seconds Age of the '
        'oldest accepted delivery without a terminal outcome.',
        '# TYPE hive_connect_delivery_oldest_pending_age_seconds gauge',
        'hive_connect_delivery_oldest_pending_age_seconds '
        f'{snap["pending"]["oldest_age_seconds"]:.3f}',
    ])
    return '\n'.join(lines) + '\n'
