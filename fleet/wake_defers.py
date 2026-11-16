"""Durable registry + Prometheus exposition for deferred connect-wakes (PLAT-9052).

The connect-wake DEFER (fleet.views connect-wake, operator 2026-08-08) promises
"reconcile grants when a slot frees" but, before this module, left no durable
trace: the deferral existed only as a log line. A seat starved at replicas=0
while holding assigned work was invisible until lane-health classified the
stall >=24h later (the PLAT-8788 / kai shape: 19 DEFERs across ~10 agents in
25h with zero grant-release events).

House rules (HIVE-615 v9 §5.3, harness_versions.harness_upgrade_failure_to_prometheus):

  * the ALERTING series are UNLABELED and emitted ALWAYS -- 0 when nothing is
    deferred. An absent-when-healthy series makes "nothing is wrong" and "the
    exporter is broken" identical, and a labeled gauge that stops being
    emitted on resolution leaves Prometheus serving the last value until
    staleness while the healthy sample is a DIFFERENT series.
  * identity lives on a companion info series (email), emitted only while the
    deferral is outstanding.
  * free text (reason/detail) is never a label -- cardinality. It stays on the
    durable log line for the responder.

Registry semantics: one entry per canonical agent email holding the FIRST
defer epoch. Re-defers of the same seat do not reset the age -- the
starvation clock started at the first deferred wake. Entries clear when the
wake path observes the seat running again (already_running), starts it
(started), or the seat is deliberately not wakeable (operator_stopped /
archived -- the registry tracks wake starvation, not operator intent).

A seat scaled up OUTSIDE the wake path (manual devops scale, drift
reconciler) leaves a stale entry until its next wake attempt self-corrects
it; accepted for v1 -- the info series names the seat, so a responder
reconciles in one look instead of the registry silently guessing.

Cache-backed like the harness failure fact: fleet-visible, best-effort. A
cache flush resets the registry; the DEFER log lines remain the durable
record. Read-modify-write between the two hive replicas is not locked --
defers are rare (tens per day) so the lost-update window is negligible, and
a repeated wake request re-records the entry.
"""

from __future__ import annotations

import logging
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

WAKE_DEFERRED_KEY = 'hive:wake-defers:v1'

# An entry older than this is treated as registry rot and dropped at
# exposition time: a deferral outstanding for days means the seat woke
# through a path that never cleared it (or the seat is gone), and aging it
# forever would pin a permanent page. The 24h bound matches the lane-health
# classification lag the series exists to beat.
MAX_DEFER_AGE_SECONDS = 24 * 3600

# PLAT-10630: per-pass fan-out bound for the reconciliation. The REAL limiter
# of a grant pass is `lease_wake_available` — a full pool refuses every grant
# after the freed slots are consumed, so the drain-to-capacity semantics come
# from the lease board, not from this budget. The budget only bounds one
# pass's scale-up fan-out so a deep backlog (observed 2026-10-01: 38
# outstanding seats) converges over successive passes instead of booting the
# whole backlog into the pool in one instant. Event-driven callers
# (stop/hibernate lease-release boundaries) and the periodic sweep host
# (reconcile_on_demand_agents, the 10-min lease reconcile) both drain through
# this same bound.
RECONCILE_MAX_GRANTS_PER_PASS = 8

# PLAT-13371: age-based grant raise. A defer outstanding past this age is
# STARVATION-CLASS, not queue-position: its per-pass grant budget is raised
# (it may consume from the aged budget beyond the caller's ``max_grants``
# cap, bounded by AGED_GRANTS_PER_PASS) because a 27-seat aged cohort drains
# one seat per reconcile tick under a tight budget while holding 399 assigned
# tasks (~1,667 failover unassigns/day). The lease board remains the REAL
# capacity limiter — the raise cannot conjure a slot a full pool doesn't
# have; it only stops the budget from throttling a grant the pool can serve.
GRANT_RAISE_AGE_SECONDS = 30 * 60
AGED_GRANTS_PER_PASS = 8

# PLAT-13371 companion registry: per-seat first epoch at which a reconcile
# pass observed ``lease unavailable`` for an outstanding defer. Cleared when
# the seat is granted, cleared, or observed lease-available. Feeds the
# aged-lease-unavailable paging series (a defer old AND lease-fenced is the
# silent-skip shape the relay audit three-for-three'd on).
WAKE_LEASE_UNAVAILABLE_KEY = 'hive:wake-defers-lease-unavailable:v1'


def _load() -> dict:
    data = cache.get(WAKE_DEFERRED_KEY)
    if isinstance(data, dict) and isinstance(data.get('defers'), dict):
        return data
    return {'defers': {}, 'total': 0}


def _save(data: dict) -> None:
    cache.set(WAKE_DEFERRED_KEY, data, None)


def record_wake_defer(email: str, reason: str = '', *, now: float | None = None) -> None:
    """Record (or refresh) a deferred wake for ``email``.

    The first-defer epoch is preserved on re-defer: the starvation age must
    not reset because the requester retried.
    """
    if not email:
        return
    stamp = float(now) if now is not None else time.time()
    data = _load()
    defers = data['defers']
    if email not in defers:
        defers[email] = stamp
    data['total'] = int(data.get('total') or 0) + 1
    _save(data)


def clear_wake_defer(email: str) -> None:
    """Clear the outstanding deferral for ``email`` (wake observed)."""
    if not email:
        return
    data = _load()
    if email in data['defers']:
        del data['defers'][email]
        _save(data)


def outstanding_wake_defers(*, now: float | None = None) -> dict:
    """Snapshot of outstanding deferrals: {email: first_defer_epoch}.

    Entries past MAX_DEFER_AGE_SECONDS are pruned here (registry rot, see
    module docstring) so the exposition never ages a dead entry forever.
    """
    stamp = float(now) if now is not None else time.time()
    data = _load()
    defers = {
        email: epoch for email, epoch in data['defers'].items()
        if stamp - epoch <= MAX_DEFER_AGE_SECONDS
    }
    if len(defers) != len(data['defers']):
        data['defers'] = defers
        _save(data)
    return defers


def _load_lease_unavailable() -> dict:
    data = cache.get(WAKE_LEASE_UNAVAILABLE_KEY)
    if isinstance(data, dict):
        return data
    return {}


def _save_lease_unavailable(data: dict) -> None:
    cache.set(WAKE_LEASE_UNAVAILABLE_KEY, data, None)


def _clear_lease_unavailable(email: str, ckey: str | None = None) -> None:
    data = _load_lease_unavailable()
    changed = False
    for key in (email, ckey):
        if key and key in data:
            del data[key]
            changed = True
    if changed:
        _save_lease_unavailable(data)


def _mark_lease_unavailable(email: str, *, now: float) -> bool:
    """Record lease-unavailable persistence for ``email``; True when this is
    the FIRST observation past the page threshold (edge for the paged log)."""
    data = _load_lease_unavailable()
    entry = data.get(email)
    if not isinstance(entry, dict) or 'first' not in entry:
        data[email] = {'first': now, 'paged': False}
        _save_lease_unavailable(data)
        return False
    first = float(entry.get('first') or now)
    paged = bool(entry.get('paged'))
    if not paged and now - first > GRANT_RAISE_AGE_SECONDS:
        entry['paged'] = True
        _save_lease_unavailable(data)
        return True
    return False


def aged_lease_unavailable_count(*, now: float | None = None) -> int:
    """Seats whose defer is past the raise age AND lease-fenced (paged class)."""
    stamp = float(now) if now is not None else time.time()
    defers = outstanding_wake_defers(now=stamp)
    data = _load_lease_unavailable()
    count = 0
    for email, epoch in defers.items():
        entry = data.get(email)
        if isinstance(entry, dict) and stamp - float(entry.get('first') or stamp) > GRANT_RAISE_AGE_SECONDS:
            count += 1
    return count


def _escape_label(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def wake_defers_to_prometheus(*, now: float | None = None) -> str:
    """Prometheus text for the deferred-wake fact (PLAT-9052, san direction 3).

        hive_wake_deferred_total         cumulative defers recorded
        hive_wake_deferred_outstanding   seats currently deferred
        hive_wake_deferred_age_seconds   age of the OLDEST outstanding, 0 if none
        hive_wake_deferred_info{email}   1 per outstanding seat (identity only)

    The alerting series are unlabeled and always present (0 when healthy) so
    a resolution drives the SAME series to 0 in place and a scrape gap is
    distinguishable from a healthy fleet. Paging rules (san, PLAT-9052):
    age > 30m, or a second distinct seat outstanding within 24h.
    """
    stamp = float(now) if now is not None else time.time()
    data = _load()
    defers = outstanding_wake_defers(now=stamp)
    total = int(data.get('total') or 0)
    outstanding = len(defers)
    oldest = min(defers.values()) if defers else None
    age = max(0.0, stamp - oldest) if oldest is not None else 0.0
    lines = [
        '# HELP hive_wake_deferred_total Cumulative connect-wake deferrals recorded (lease pool full / await_reconcile_grant).',
        '# TYPE hive_wake_deferred_total counter',
        f'hive_wake_deferred_total {total}',
        '# HELP hive_wake_deferred_outstanding Seats with a wake currently deferred.',
        '# TYPE hive_wake_deferred_outstanding gauge',
        f'hive_wake_deferred_outstanding {outstanding}',
        '# HELP hive_wake_deferred_age_seconds Age of the oldest outstanding deferred wake, 0 when none.',
        '# TYPE hive_wake_deferred_age_seconds gauge',
        f'hive_wake_deferred_age_seconds {age:.0f}',
        '# HELP hive_wake_deferred_info Outstanding deferred-wake identity (one series per seat, only while outstanding).',
        '# TYPE hive_wake_deferred_info gauge',
    ]
    # PLAT-13371: aged + lease-fenced seats — the silent-skip class. Unlabeled
    # and always present (0 when none) per the house alerting-series rule.
    lines.extend([
        '# HELP hive_wake_deferred_lease_unavailable_aged Seats deferred past the grant-raise age whose latest reconcile observed lease unavailable.',
        '# TYPE hive_wake_deferred_lease_unavailable_aged gauge',
        f'hive_wake_deferred_lease_unavailable_aged {aged_lease_unavailable_count(now=stamp)}',
    ])
    for email in sorted(defers):
        lines.append(f'hive_wake_deferred_info{{email="{_escape_label(email)}"}} 1')
    return '\n'.join(lines) + '\n'


def reconcile_deferred_wakes(*, max_grants: int | None = None, now: float | None = None) -> dict:
    """Grant outstanding deferred connect-wakes while lease capacity exists.

    Implements the second half of the connect-wake DEFER promise ("reconcile
    grants when a slot frees", PLAT-9052): the DEFER path records the
    starvation in this registry, and this reconciliation grants the OLDEST
    outstanding deferral(s) once `lease_wake_available` goes True —
    event-driven from the stop/hibernate paths (which call this right after
    `release_leases_for_agent` frees a slot) AND from the periodic sweep host
    (reconcile_on_demand_agents' 10-min lease reconcile, PLAT-10630: the
    09-17 cascade proved an event-driven-only reaper cannot drain a backlog
    when no seat happens to stop — 38 outstanding seats with zero
    grant-release events). Without either path a deferred seat stays at
    replicas=0 until someone sends ANOTHER connect_dm.

    Grant fan-out per pass is bounded by ``max_grants`` (default
    `RECONCILE_MAX_GRANTS_PER_PASS`): the lease board is the real capacity
    limiter, the budget only bounds one pass's fan-out so a deep backlog
    converges over successive passes instead of a single thundering scale-up.
    An explicit ``max_grants=1`` keeps the legacy one-grant-per-call shape
    for fresh defers.

    A defer outstanding past ``GRANT_RAISE_AGE_SECONDS`` (30 minutes) is
    starvation-class and may consume from the raised aged budget beyond the
    caller's ``max_grants`` cap, bounded by ``AGED_GRANTS_PER_PASS``. Fresh
    defers keep the caller's budget shape exactly. ``aged_granted`` counts
    aged grants made inside the normal budget too, so the extra headroom
    past a spent budget is at most ``AGED_GRANTS_PER_PASS`` and is somewhat
    smaller than 8 — conservative by design. The raise only stops the budget
    from throttling a grant the pool can serve. A full pool still refuses
    every grant, and the seat then waits with a page instead of silently
    starving.

    Semantics mirror ConnectWakeAgentView exactly (same fences, same
    fail-closed direction): archived / operator-stopped / already-running
    seats drop their entry (the registry tracks starvation, not history);
    a seat that still cannot get lease authority keeps its entry and the
    starvation clock keeps running. Never raises — a reconciliation fault
    must not break the stop path that invoked it.
    """
    now_value = float(now) if now is not None else time.time()
    defers = outstanding_wake_defers(now=now_value)
    if not defers:
        return {'considered': 0, 'granted': [], 'skipped': {}}
    granted: list[str] = []
    skipped: dict[str, str] = {}
    try:
        from fleet.on_demand import (
            agent_enabled as _agent_enabled,
            agent_eligible_to_start as _agent_eligible_to_start,
            canonical_wake_key as _canonical_wake_key,
            lifecycle_agent_emails as _lifecycle_emails,
        )
        from fleet.model_lease import lease_wake_available
        from fleet.lifecycle_fleet import (
            list_lifecycle_agents,
            set_lifecycle_agent_enabled,
        )
        agents = list_lifecycle_agents(include_disabled=True)
    except Exception as exc:  # noqa: BLE001 — fail closed, entries survive
        return {'considered': len(defers), 'granted': [], 'skipped': {
            email: f'resolve failed: {exc}'[:120] for email in defers}}

    # PLAT-10630: default fan-out is the bounded multi-grant budget; the lease
    # board remains the real capacity limiter (see RECONCILE_MAX_GRANTS_PER_PASS).
    try:
        pass_budget = max(1, int(
            RECONCILE_MAX_GRANTS_PER_PASS if max_grants is None else max_grants))
    except (TypeError, ValueError):
        pass_budget = RECONCILE_MAX_GRANTS_PER_PASS

    aged_granted = 0

    for email, epoch in sorted(defers.items(), key=lambda kv: (kv[1], kv[0])):
        aged = (now_value - epoch) > GRANT_RAISE_AGE_SECONDS
        # PLAT-13371: aged defers may consume from the raised budget beyond the
        # caller's ``max_grants`` cap (bounded by AGED_GRANTS_PER_PASS); fresh
        # defers keep the caller's budget exactly as before.
        if len(granted) >= pass_budget and not (
            aged and aged_granted < AGED_GRANTS_PER_PASS
        ):
            skipped[email] = 'grant budget exhausted this pass'
            continue
        try:
            agent = next(
                (a for a in agents if email in _lifecycle_emails(a)), None)
            if agent is None:
                skipped[email] = 'unknown_agent'
                continue
            # PLAT-11902/PLAT-12264: converge legacy spelling-keyed entries
            # onto the seat's canonical registry key so a dual-spelling seat
            # carries ONE starvation clock. Clearing both keys here retires
            # pre-canonicalization entries on the seat's next reconcile.
            ckey = _canonical_wake_key(agent) or email
            username = str(agent.get('agent_username') or agent.get('username') or '')
            from fleet.views import _is_archived_username
            if _is_archived_username(username):
                clear_wake_defer(email)
                if ckey != email:
                    clear_wake_defer(ckey)
                _clear_lease_unavailable(email, ckey)
                skipped[email] = 'archived'
                continue
            if _agent_enabled(agent):
                clear_wake_defer(email)
                if ckey != email:
                    clear_wake_defer(ckey)
                _clear_lease_unavailable(email, ckey)
                skipped[email] = 'already_running'
                continue
            if not _agent_eligible_to_start(agent):
                clear_wake_defer(email)
                if ckey != email:
                    clear_wake_defer(ckey)
                _clear_lease_unavailable(email, ckey)
                skipped[email] = 'operator_stopped'
                continue
            wake_ok, wake_reason = lease_wake_available(agent=agent)
            if not wake_ok:
                # Pool still full (or authority unconfirmable): leave the
                # entry — the starvation clock keeps running.
                skipped[email] = f'lease unavailable ({wake_reason})'
                # PLAT-13371: persistence tracking + edge-triggered page.
                if _mark_lease_unavailable(email, now=now_value):
                    logger.error(
                        'connect-wake: LEASE-UNAVAILABLE PAGED %s — deferred '
                        'since epoch %s (age %.0fs > %ss) and lease_wake_available '
                        'has been false across reconcile passes for >%ss; the seat '
                        'holds assigned work at replicas=0 and needs a slot or '
                        'operator intervention.',
                        email, epoch, now_value - epoch,
                        GRANT_RAISE_AGE_SECONDS, GRANT_RAISE_AGE_SECONDS,
                    )
                continue
            ok, detail = set_lifecycle_agent_enabled(str(agent.get('id') or ''), True)
            if ok:
                clear_wake_defer(email)
                if ckey != email:
                    clear_wake_defer(ckey)
                _clear_lease_unavailable(email, ckey)
                if aged:
                    aged_granted += 1
                granted.append(email)
                logger.info(
                    'connect-wake: RECONCILE-GRANT %s (deferred since epoch %s)',
                    email, epoch,
                )
            else:
                skipped[email] = f'start failed: {str(detail)[:100]}'
        except Exception as exc:  # noqa: BLE001 — per-seat fail closed
            skipped[email] = f'reconcile fault: {exc}'[:120]
    if granted or skipped:
        logger.info(
            'connect-wake: reconcile pass considered=%s granted=%s skipped=%s',
            len(defers), granted, skipped,
        )
    return {'considered': len(defers), 'granted': granted, 'skipped': skipped}
