"""Hive-side scheduler for agent→model exclusivity leases.

Cortex enforces the lease on the request path (``inference/model_leases.py``);
this module decides WHO should hold it. The split exists because neither service
can do both: Cortex has no idea which agents exist or whether Pulse has work for
them, and Hive is not on the request path.

WHY LEASES AND NOT TIME-SHARING. DeepSeek-V4-Flash has one live backend with
``max_concurrent=1``, so exactly one request is ever in flight no matter what we
do. Interleaving agents through that slot does not buy parallelism — there is
none to buy — it only evicts the previous agent's prefix cache. A cold 80-145k
session re-prefills at ~1.3k tok/s ≈ 80-112s (wiki 944efbda / PLAT-4189). A
15-minute lease pays that once; round-robin pays it nearly every request.

THE STARVATION TRAP THIS AVOIDS (operator, 2026-07-25). It is tempting to gate
eligibility on "does this agent have Pulse tasks assigned". Do not: Pulse assigns
work to STARTED agents, and Hive starts agents whose TEAM has pending work. A
stopped agent therefore has no assignments, would look like it wants nothing,
would never be granted a lease, and would never start — with 10 teams and one
admission slot, nine teams stall forever. Eligibility is therefore evaluated at
the TEAM level, the same signal ``decide_on_demand_actions`` already uses to wake
an agent, which stays truthful while the agent is stopped.

Fairness is least-recently-held first among *quiet* seats. A holder still
spending the model (Cortex ``idle_for_s`` inside the idle window) keeps the
lease — max-hold is not a round-robin interrupt of loaded work. Quiet seats
rotate so every team with work is served; a 287k GLM session that turned 74s
ago is not a quiet seat.

STOPPED HOLDERS MUST DROP THE LEASE (operator 2026-07-28 / Nami incident). A
lease is exclusivity for a living agent that can actually spend the slot. If Hive
stops/hibernates/disables the holder (or the pod is otherwise not running), the
board entry must be force-revoked immediately — even inside min-hold. Leaving
``agent-nami`` scaled to 0 while she still held DeepSeek pinned the whole pool
for ~15–45 minutes. Enforcement is dual-path:

1. **Event-driven** — every stop/disable path calls ``release_leases_for_agent``.
2. **Reconcile backstop** — each lease tick force-revokes any board holder whose
   agent is not currently running (covers races, external scale-downs, partial
   failures of the event path).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from django.core.cache import cache
from django.conf import settings

logger = logging.getLogger(__name__)

# Operator-tunable model-lease cache (unrelated to reviewed runtime releases).
POOL_CACHE_KEY = 'fleet:model-lease-pool'
# principal → {model_id, ...} last observed / written by Hive. Cortex board is
# still SoT for enforcement; this cache only speeds stop-time multi-model scan
# and survives a single reconcile miss.
HOLDINGS_CACHE_KEY = 'fleet:model-lease-holdings'
# principal → epoch of last leftover-home yield. Reconcile must not POST
# yield-homes for every hibernated pool member every tick.
LEFTOVER_HOME_YIELD_CACHE_KEY = 'fleet:leftover-home-yielded:v1'
LEFTOVER_HOME_YIELD_TTL_S = 60 * 60
# Principals whose last revoke was preempt-for-keep-resident. That is not a
# finished sprint — they go to the front of the next fill so Yuna's honest
# seat does not also push Engineering to the back of LRH (Saki 2026-08-18).
PREEMPT_RETRY_CACHE_KEY = 'fleet:model-lease-preempt-retry'
# HIVE-2841: admin-editable critical-team lease floor (fleet.models.
# LeaseCriticalTeamPolicy). Cached briefly so a policy edit lands on the next
# lease tick without a deploy, while a reconcile burst does not re-query the
# row per pool per tick.
CRITICAL_TEAM_POLICY_CACHE_KEY = 'fleet:lease-critical-team-policy'
CRITICAL_TEAM_POLICY_TTL_S = 60

DEFAULT_MIN_HOLD_SECONDS = 15 * 60
DEFAULT_MAX_HOLD_SECONDS = 45 * 60
DEFAULT_IDLE_RELEASE_SECONDS = 3 * 60
# Operator 2026-09-06: Hive may occupy 95% of Cortex admission
# (supersedes 2026-09-01 90% headroom). Operator 2026-09-09: the cap is
# floor(95%), never 100% when seats >= 2 — one unleased seat stays for
# TUI / homeless newcomers so interactive GLM is not packed out.
HIVE_CORTEX_ADMISSION_FRACTION = 0.95


@dataclass(frozen=True)
class PoolMember:
    username: str
    email: str
    teams: tuple[str, ...] = ()
    # Epoch seconds of this member's last lease; 0.0 = never held, which sorts
    # first so a newly added agent is served before any incumbent repeats.
    last_held_at: float = 0.0


@dataclass
class LeaseDecision:
    action: str                      # grant | revoke | keep | none
    holder: str | None = None        # principal the action applies to
    reason: str = ''
    force: bool = False              # revoke inside min-hold (stale holder only)
    voluntary: bool = False          # holder releasing itself (always permitted)
    waiting: tuple[str, ...] = field(default_factory=tuple)


def hive_admission_cap(admission: int) -> int:
    """Hive wake slots: ``floor(95% of Cortex admission)``.

    Never 100% when there are ≥2 seats — one unleased seat stays for TUI /
    homeless newcomers (operator 2026-09-09). A 1-seat fleet still returns 1
    so Hive can wake on MiniMax-class replicas. ``int(x * 0.95)`` is the
    same as floor for positives; ``math.floor`` is the named policy.
    """
    try:
        seats = int(admission or 0)
    except (TypeError, ValueError):
        seats = 0
    if seats <= 0:
        return 0
    cap = math.floor(seats * HIVE_CORTEX_ADMISSION_FRACTION)
    if seats >= 2:
        return max(0, min(int(cap), seats - 1))
    return max(1, int(cap))


def residency_aware_max_holders(
    board: dict | None,
    *,
    waiter_count: int,
    holder_count: int,
) -> int:
    """Raise max_holders toward 95% of Cortex admission, never 100%.

    Operator 2026-09-06: Hive occupies at least 95% of live Cortex seats so
    fleet agents can spend admission on assigned work. One spare seat stays
    unleased for a homeless newcomer. When every remaining seat is
    TTL-protected, stop raising so we do not stampede into residency_full
    503s. A stored ceiling above 95% (leftover 100% board) shrinks on this
    tick.
    """
    current = max(1, int((board or {}).get('max_holders') or 1))
    try:
        admission = int((board or {}).get('admission_capacity') or 0)
    except (TypeError, ValueError):
        admission = 0
    hive_cap = hive_admission_cap(admission)
    if hive_cap <= 0:
        return current
    current = min(current, hive_cap)
    res = (board or {}).get('residency')
    res = res if isinstance(res, dict) else {}
    if waiter_count <= 0:
        return current
    if res.get('full'):
        return current
    grantable = res.get('grantable')
    if grantable is None:
        grantable = max(0, hive_cap - holder_count)
    try:
        grantable = max(0, int(grantable))
    except (TypeError, ValueError):
        grantable = max(0, hive_cap - holder_count)
    if grantable <= 0:
        return current
    target = min(hive_cap, holder_count + min(int(waiter_count), grantable))
    return max(current, target)


def principal_for(username: str) -> str:
    """Cortex principal name for a fleet agent.

    Cortex attributes traffic by ``APIKey.name``, which for fleet agents is
    ``agent-<username>``; usage rows and the lease board therefore name a
    principal identically and can be joined without a mapping table.
    """
    return f'agent-{str(username).strip()}'


def critical_team_policy() -> dict[str, int]:
    """``team_slug -> min_leases`` from the admin-editable floor policy.

    HIVE-2841: the reservation rule must be configurable in Hive itself, not a
    constant baked for one incident. Reads ``fleet.models.
    LeaseCriticalTeamPolicy`` (Django-admin editable) through a short cache so
    adding/disabling/editing a row changes who the scheduler reserves on the
    next lease tick. Fails OPEN to "no floor" on any read error — a broken
    policy surface must never take the pool down (same posture as the pools
    config cache). Team slugs are canonicalized through
    ``fleet.on_demand.canon_team`` so a Pulse roster key and the policy row
    always join.
    """
    cached = cache.get(CRITICAL_TEAM_POLICY_CACHE_KEY)
    if isinstance(cached, dict):
        return cached
    policy: dict[str, int] = {}
    try:
        from .models import LeaseCriticalTeamPolicy
        from .on_demand import TEAM_SCOPE_SEPARATOR, canon_team

        for row in LeaseCriticalTeamPolicy.objects.filter(enabled=True).only(
                'team_slug', 'min_leases'):
            # Same tail normalization as _team_slugs: "shizuha::merge" and
            # "merge" policy rows must be equivalent (PLAT-11046).
            slug = canon_team(row.team_slug).rsplit(TEAM_SCOPE_SEPARATOR, 1)[-1]
            if not slug:
                continue
            try:
                min_leases = max(1, int(row.min_leases or 1))
            except (TypeError, ValueError):
                min_leases = 1
            policy[slug] = min_leases
    except Exception:  # noqa: BLE001 — policy surface must never break leasing
        logger.warning('critical-team lease floor policy read failed; floor disabled', exc_info=True)
        return {}
    cache.set(CRITICAL_TEAM_POLICY_CACHE_KEY, policy, CRITICAL_TEAM_POLICY_TTL_S)
    return policy


def critical_team_floor_state(
    *,
    members: list[PoolMember],
    wants: set[str],
    holder_usernames: set[str],
    policy: dict[str, int],
    ready: set[str] | None = None,
) -> dict:
    """Pure critical-team floor computation for one pool tick (HIVE-2841).

    Returns ``{'reserved': set[str], 'targets': list[tuple[str, list[str]]]}``:
      - ``reserved`` — holders that ARE the coverage for a met critical team.
        They are keep-fenced exactly like keep-resident seats: the ordinary
        unused/max-hold revoke must not drop the merge seat the floor just
        seated (churn guard, same honest-seat doctrine as 2026-08-18).
      - ``targets`` — unmet ``(team_slug, [candidate usernames])`` in FAIR
        order: teams sorted by the best (least-recently-held) candidate they
        can field, so when supply is short the stalest critical team is served
        first; candidates within a team are LRH-ordered and capped at the
        team's remaining deficit (``min_leases`` minus holders it already has).

    Holders and candidates must be in ``wants``: the floor arbitrates demand fairly, it
    does not manufacture wake demand for idle critical seats (starvation-trap
    doctrine — team-queue worthiness already keeps a working critical team in
    ``wants``).

    ``ready`` — bare usernames whose tenure currently holds UNSTARTED assigned
    Pulse work (ready/open/todo), as computed by the caller's worthiness pass
    (HIVE-2862 follow-up). When multiple waiters can field the same target,
    a ready waiter seats before any idle one regardless of lease age — the
    reserved seat must not be spent on an idle cross-org seat while the team
    that owns the board's urgent item has a waiter waiting behind it. None
    (default) preserves pure LRH selection.  Inter-target order also ranks
    ready-fielded targets ahead of idle-fielded ones before lease age.
    """
    ready = ready or set()
    if not policy:
        return {'reserved': set(), 'targets': []}
    member_by_uname = {m.username: m for m in members}

    def _team_slugs(member: PoolMember | None) -> set[str]:
        if member is None:
            return set()
        from .on_demand import TEAM_SCOPE_SEPARATOR, canon_team

        # PLAT-11046 forensics: canon_team PRESERVES the org scope separator,
        # so roster teams arrive org-qualified ("shizuha::merge") while the
        # policy surface's documented contract is the unqualified slug
        # ("merge"). Compare unqualified tails on both sides or the floor can
        # never match its own policy rows against real rosters.
        return {
            canon_team(t).rsplit(TEAM_SCOPE_SEPARATOR, 1)[-1]
            for t in (member.teams or ()) if canon_team(t)
        }

    reserved: set[str] = set()
    deficits: list[tuple[str, int]] = []
    for slug, min_leases in policy.items():
        holders_in_team = [
            uname for uname in holder_usernames
            if uname in wants and slug in _team_slugs(member_by_uname.get(uname))
        ]
        if len(holders_in_team) >= min_leases:
            reserved.update(holders_in_team)
            continue
        deficits.append((slug, min_leases - len(holders_in_team)))

    targets: list[tuple[str, list[str]]] = []
    ranked: list[tuple[float, str, list[str]]] = []
    for slug, deficit in deficits:
        candidates = sorted(
            (
                m for m in members
                if m.username in wants
                and m.username not in holder_usernames
                and slug in _team_slugs(m)
            ),
            key=lambda m: (0 if m.username in ready else 1, m.last_held_at, m.username),
        )
        picked = [m.username for m in candidates[:max(0, deficit)]]
        if picked:
            best = candidates[0]
            ranked.append((0 if best.username in ready else 1, best.last_held_at, slug, picked))
    ranked.sort(key=lambda row: (row[0], row[1], row[2]))
    targets = [(slug, picked) for _, _, slug, picked in ranked]
    return {'reserved': reserved, 'targets': targets}


def unmet_floor_seaters(
    *,
    candidates: list[PoolMember],
    holder_usernames: set[str],
    policy: dict[str, int],
) -> set[str]:
    """Bare usernames of ``candidates`` that seat an UNMET critical-team floor.

    Pure helper for the grant path (PLAT-11442 class, PLAT-6638 verify leg):
    a candidate seats a floor when any team it fields has a deficit —
    ``min_leases`` minus the holders it already fields — still unmet.  Fellow
    holders are counted per team by membership, so counting a holder's fellow
    teams as unmet never happens (the bounced-kai class, 2026-08-22).

    Empty policy ⇒ no seater (arbitration is a no-op — same mirror as
    ``critical_team_floor_state``).
    """
    if not policy:
        return set()
    member_by_uname = {m.username: m for m in candidates}

    def _team_slugs(member: PoolMember | None) -> set[str]:
        if member is None:
            return set()
        from .on_demand import canon_team

        return {canon_team(t) for t in (member.teams or ()) if canon_team(t)}

    seaters: set[str] = set()
    bare_holder_usernames = {uname.removeprefix('agent-') for uname in holder_usernames}
    for m in candidates:
        for slug in _team_slugs(m):
            min_leases = int(policy.get(slug, 0) or 0)
            if min_leases <= 0:
                continue
            holders_in_team = [
                uname for uname in bare_holder_usernames
                if slug in _team_slugs(member_by_uname.get(uname))
            ]
            if len(holders_in_team) < min_leases:
                seaters.add(m.username)
                break
    return seaters


def _normalize_pool_entry(raw: dict) -> dict:
    cfg = dict(raw or {})
    cfg.setdefault('min_hold_seconds', DEFAULT_MIN_HOLD_SECONDS)
    cfg.setdefault('max_hold_seconds', DEFAULT_MAX_HOLD_SECONDS)
    cfg.setdefault('idle_release_seconds', DEFAULT_IDLE_RELEASE_SECONDS)
    cfg.setdefault('max_holders', 1)
    cfg.setdefault('enabled', bool(cfg.get('model_id')))
    cfg['members'] = [str(m).strip() for m in (cfg.get('members') or []) if str(m).strip()]
    cfg['model_id'] = str(cfg.get('model_id') or '').strip()
    try:
        cfg['max_holders'] = max(1, int(cfg.get('max_holders') or 1))
    except (TypeError, ValueError):
        cfg['max_holders'] = 1
    return cfg


def pool_config() -> dict:
    """Current primary pool config: ``{model_id, members: [username], holds…}``.

    Cache-backed so the pool can be changed live; falls back to settings so a
    cold cache is a well-defined state rather than an empty pool that silently
    disables leasing.

    Prefer ``all_pool_configs()`` when multiple models are leased (DeepSeek + GLM).
    """
    pools = all_pool_configs()
    return pools[0] if pools else _normalize_pool_entry({})


DEFAULT_CORTEX_LEASED_MODELS = 'GLM-5.3-Flash'


def cortex_leased_model_ids() -> list[str]:
    """Canonical Cortex-leased serving ids Hive must govern.

    Operator 2026-08-27/31: Hive wake cap = Cortex admission cap. The live
    pool cache is not that contract — it was left on DeepSeek/Qwen after
    agents pinned to GLM-5.3-Flash, so ``lease_wake_available`` returned
    ``no_lease_pool`` and 19 GLM runtimes hammered 4 homes.
    """
    raw = getattr(settings, 'HIVE_CORTEX_LEASED_MODELS', '') or ''
    if not str(raw).strip():
        raw = DEFAULT_CORTEX_LEASED_MODELS
    return [part.strip() for part in str(raw).split(',') if part.strip()]


def _fleet_pins_by_canonical() -> dict[str, set[str]]:
    """Hive SoT model → usernames. Empty on SimpleTestCase / DB blip."""
    by_model: dict[str, set[str]] = {}
    try:
        from .models import FleetAgent
        rows = FleetAgent.objects.exclude(
            status__in=(FleetAgent.Status.DELETING, FleetAgent.Status.DELETED),
        ).values_list('agent_username', 'model')
        for username, model in rows:
            key = _canonical_pool_model(model)
            uname = str(username or '').strip()
            if key and uname:
                by_model.setdefault(key, set()).add(uname)
    except Exception:  # noqa: BLE001 — SimpleTestCase / DB outage
        pass
    return by_model


def _fleet_pinned_canonical_models() -> set[str]:
    """Hive SoT models currently pinned (never subtract on a DB blip)."""
    return set(_fleet_pins_by_canonical())


def _overlay_cortex_leased_pools(pools: list[dict]) -> list[dict]:
    """Add a pool for every Cortex-leased model Hive agents actually pin."""
    seen = {_canonical_pool_model(p.get('model_id')) for p in pools}
    pins = _fleet_pins_by_canonical()
    extra = []
    defaults = {
        'min_hold_seconds': DEFAULT_MIN_HOLD_SECONDS,
        'max_hold_seconds': DEFAULT_MAX_HOLD_SECONDS,
        'idle_release_seconds': DEFAULT_IDLE_RELEASE_SECONDS,
        'max_holders': 1,
    }
    for model_id in cortex_leased_model_ids():
        key = _canonical_pool_model(model_id)
        if key in seen:
            continue
        if pins and key not in pins:
            continue
        extra.append(_normalize_pool_entry({
            **defaults,
            'model_id': model_id,
            'members': sorted(pins.get(key) or ()),
            'enabled': True,
        }))
        seen.add(key)
    return list(pools) + extra


def all_pool_configs() -> list[dict]:
    """All enabled model-lease pools (sprint turn-taking per model).

    Cache shapes:
      - Legacy single: ``{model_id, members, ...}``
      - Multi: ``{pools: [{model_id, members, ...}, ...], min_hold_seconds?, ...}``
    """
    cached = cache.get(POOL_CACHE_KEY)
    defaults = {
        'min_hold_seconds': DEFAULT_MIN_HOLD_SECONDS,
        'max_hold_seconds': DEFAULT_MAX_HOLD_SECONDS,
        'idle_release_seconds': DEFAULT_IDLE_RELEASE_SECONDS,
        'max_holders': 1,
    }
    out: list[dict] = []
    if isinstance(cached, dict) and isinstance(cached.get('pools'), list):
        for raw in cached['pools']:
            if not isinstance(raw, dict):
                continue
            entry = _normalize_pool_entry({**defaults, **{
                k: cached[k] for k in defaults if k in cached
            }, **raw})
            if entry.get('enabled') and entry.get('model_id') and entry.get('members'):
                out.append(entry)
    elif isinstance(cached, dict) and cached.get('model_id'):
        entry = _normalize_pool_entry({**defaults, **cached})
        if entry.get('enabled') and entry.get('members'):
            out.append(entry)
    else:
        # Settings seed (single model)
        entry = _normalize_pool_entry({
            **defaults,
            'model_id': getattr(settings, 'HIVE_MODEL_LEASE_MODEL', '') or '',
            'members': list(getattr(settings, 'HIVE_MODEL_LEASE_MEMBERS', []) or []),
        })
        if entry.get('enabled') and entry.get('members'):
            out.append(entry)
    return _overlay_cortex_leased_pools(out)


def set_pool_config(cfg: dict) -> dict:
    """Store pool config. Accepts legacy single-model or multi ``{pools:[…]}``."""
    cache.set(POOL_CACHE_KEY, cfg, None)
    pools = all_pool_configs()
    return pools[0] if pools else _normalize_pool_entry({})


def _canonical_pool_model(value: str | None) -> str:
    """Normalize Hive/Cortex spellings for model-pool membership joins."""
    model = str(value or '').strip()
    if model.lower().startswith('cortex/'):
        model = model.split('/', 1)[1]
    return model.casefold()


def pool_configs_for_agents(configs: list[dict], agents) -> list[dict]:
    """Reconcile configured lease pools with Hive's current agent model SoT.

    Pool hold windows remain operator configuration, but membership is derived
    from each agent's effective model.  A hand-maintained member list becomes
    stale immediately after a Hive model edit: the moved agent can bypass the
    old model's lease while the wrong pool starts/stops it.  That caused
    Recon/Ren to interleave through DeepSeek's single slot while Rui was started
    as the GLM holder even after Hive desired DeepSeek.

    ``agents`` is the complete lifecycle inventory supplied by the on-demand
    reconciler.  Claw rows are intentionally excluded: their API-key principals
    are ``hive-claw-*`` and Cortex's lease design explicitly observes rather
    than governs them.  If a configured model has no match, retain its explicit
    members fail-safe; a transiently incomplete inventory must not erase a pool.

    Membership is additionally UNIONed with FleetAgent DB rows (HIVE-1714,
    operator 2026-08-11 "we might also wish to see that these admitted agents
    are in fact hive leased").  The runtime inventory merges the rt-fleet
    daemon list; while that daemon was flapping it "served empty" and the
    inventory shrank 44 -> 21, so the governed set at Cortex was synced DOWN
    to whichever subset survived the merge that tick — 30 DeepSeek agents
    (hiro/nagi/san/shion among them, all admitted at that moment) silently
    escaped lease governance.  Hive's DB is the config SoT for who runs which
    model; liveness planes may only add rows (a zombie the DB forgot), never
    subtract them.
    """
    by_model: dict[str, set[str]] = {}
    try:
        from .models import FleetAgent
        rows = FleetAgent.objects.exclude(
            status__in=(FleetAgent.Status.DELETING, FleetAgent.Status.DELETED),
        ).values_list('agent_username', 'model')
        for username, model in rows:
            username = str(username or '').strip()
            model = _canonical_pool_model(model)
            if username and model:
                by_model.setdefault(model, set()).add(username)
    except Exception:  # noqa: BLE001 — SimpleTestCase / DB outage: inventory-only
        pass
    for agent in agents or ():
        if str(agent.get('runtime_source') or '').strip().casefold() == 'claw':
            continue
        agent_id = str(agent.get('id') or '')
        if agent_id.startswith('claw:'):
            continue
        username = str(
            agent.get('username') or agent.get('agent_username') or ''
        ).strip()
        model = _canonical_pool_model(
            agent.get('effective_model') or agent.get('model')
        )
        if username and model:
            by_model.setdefault(model, set()).add(username)

    resolved: list[dict] = []
    for raw in configs or ():
        cfg = dict(raw)
        members = sorted(by_model.get(_canonical_pool_model(cfg.get('model_id'))) or ())
        if members:
            cfg['members'] = members
        elif by_model or agents:
            # Complete inventory, nobody pinned here (Qwen3.8 after MiniMax H3
            # took i9-ws). Keeping the stale member list lets this dead pool
            # overwrite DeepSeek holder status in lease_status_by_username.
            cfg['members'] = []
        resolved.append(cfg)
    return resolved


def lease_status_by_username(*, agents, client=None, configs=None) -> dict[str, dict]:
    """Return the operator-facing lease state for each governed fleet agent.

    This is a read projection only; Cortex remains the enforcement source of
    truth and Hive remains the policy/lifecycle source of truth.  The Agents UI
    needs both to distinguish a healthy blue hibernation (waiting for a sprint)
    from an explicit operator stop.

    Board failures are represented as ``unknown`` rather than raising or
    suppressing the agent row.  Callers should cache the assembled fleet index,
    not this function independently, so lifecycle invalidation refreshes the
    displayed holder and hibernation reason together.
    """
    agent_rows = list(agents or ())
    agent_by_username = {
        str(
            agent.get('agent_username') or agent.get('username') or ''
        ).strip(): agent
        for agent in agent_rows
    }
    configured = list(configs) if configs is not None else all_pool_configs()
    configs = pool_configs_for_agents(configured, agent_rows)
    client = client or CortexLeaseClient(timeout=10.0)
    out: dict[str, dict] = {}
    for cfg in configs:
        model = str(cfg.get('model_id') or '').strip()
        if not model:
            continue
        board = client.board(model)
        holders_list = []
        if isinstance(board, dict):
            raw_holders = board.get('holders')
            if isinstance(raw_holders, list) and raw_holders:
                holders_list = [h for h in raw_holders if isinstance(h, dict)]
            elif board.get('holder'):
                holders_list = [board['holder']]
        holder_by_principal = {
            str(h.get('principal') or '').strip(): h for h in holders_list
        }
        governed = set((board or {}).get('governed_principals') or ())
        eligible = set((board or {}).get('eligible_principals') or ())
        try:
            max_holders = max(1, int(
                (board or {}).get('max_holders') or cfg.get('max_holders') or 1
            ))
        except (TypeError, ValueError):
            max_holders = 1
        holder_count = len(holder_by_principal)
        slots_free = max(0, max_holders - holder_count)
        try:
            admission_now = (
                int(board.get('admission_capacity'))
                if isinstance(board, dict) and board.get('admission_capacity') is not None
                else None
            )
        except (TypeError, ValueError):
            admission_now = None
        if admission_now == 0:
            # Retired serving id (Qwen3.8-27B-Q2). Do not project this board
            # over a live DeepSeek holder row.
            continue
        # Stable label for "behind @X" when capacity is actually full — longest
        # held incumbent is what operators perceive as blocking the queue.
        capacity_holder = ''
        if holder_by_principal:
            capacity_holder = str(max(
                holder_by_principal.values(),
                key=lambda h: (
                    int(h.get('held_for_s') or 0),
                    str(h.get('principal') or ''),
                ),
            ).get('principal') or '') or next(iter(holder_by_principal), '')
        for username in cfg.get('members') or ():
            principal = principal_for(username)
            lifecycle_status = str(
                (agent_by_username.get(str(username)) or {}).get('status') or ''
            ).strip().lower()
            mine = holder_by_principal.get(principal)
            is_eligible = principal in eligible
            # Projection must track FREE SLOTS + worthiness. Previously any
            # non-holder was labeled "waiting" whenever ANY lease was held —
            # so Banto (only blocked/scheduled work, not eligible) showed
            # "Waiting behind @kei" while max_holders had free slots
            # (operator 2026-08-06). States:
            #   holder   — currently on the board
            #   waiting  — worthy AND capacity full (true queue)
            #   eligible — worthy AND free slot (fill loop should grant)
            #   idle     — governed but not worthy (no actionable work)
            #   disabled / unknown — lifecycle / board failure
            if board is not None and mine is not None:
                state = 'holder'
            elif lifecycle_status in {'stopped', 'disabled'}:
                state = 'disabled'
            elif board is None:
                state = 'unknown'
            elif is_eligible and slots_free <= 0:
                state = 'waiting'
            elif is_eligible:
                state = 'eligible'
            else:
                state = 'idle'
            holder_data = mine or {}
            # Only show "behind @X" when truly capacity-blocked. Free-slot
            # eligible/idle agents must not blame a random incumbent.
            behind = (
                principal if mine
                else (capacity_holder if state == 'waiting' else None)
            )
            existing = out.get(str(username))
            if existing and existing.get('is_holder') and state != 'holder':
                continue
            out[str(username)] = {
                'managed': True,
                'model': model,
                'principal': principal,
                'state': state,
                'is_holder': state == 'holder',
                'governed': principal in governed if board is not None else None,
                'eligible': is_eligible if board is not None else None,
                'holder': behind,
                'holders': sorted(holder_by_principal.keys()),
                'max_holders': max_holders,
                'holder_count': holder_count,
                'slots_free': slots_free,
                'reason': str(holder_data.get('reason') or ''),
                'held_for_s': holder_data.get('held_for_s'),
                'expires_in_s': holder_data.get('expires_in_s'),
                'idle_for_s': holder_data.get('idle_for_s'),
            }
    return out


def unseated_workload_by_team(
    *,
    members: list[PoolMember],
    by_username: dict,
    workload_by_email,
    email_to_teams,
    agents=None,
) -> dict:
    """Team workload that is assigned to a seat that cannot currently execute.

    in_progress/review on a hibernated/cannot-turn owner is not 'seated' —
    Pulse has not moved it yet, and treating it as teammate WIP starved
    DeepSeek fill (2026-08-22: Jun Codex dead, Engineering todos stayed
    'already seated', so Hive leased 4 and FILL none of the remaining 4).
    Scan the full lifecycle inventory, not just this model pool: Jun is not a
    DeepSeek member, but her Engineering WIP must wake DeepSeek teammates.
    """
    from .on_demand import agent_email, agent_runtime_cannot_turn

    unavailable_emails: set[str] = set()
    inventory = list(agents or ()) or [
        by_username.get(m.username) for m in members if by_username.get(m.username)
    ]
    for agent in inventory:
        if not agent:
            continue
        status = str(agent.get('status') or '').strip().lower()
        email = agent_email(agent)
        if not email:
            continue
        if status in {'hibernated', 'stopped', 'disabled'} or agent.get('runtime_ready') is False:
            unavailable_emails.add(email)
        elif agent_runtime_cannot_turn(agent):
            unavailable_emails.add(email)
    for member in members:
        agent = by_username.get(member.username) or {}
        status = str(agent.get('status') or '').strip().lower()
        if status in {'hibernated', 'stopped', 'disabled'} or agent.get('runtime_ready') is False:
            unavailable_emails.add(member.email)
        elif agent_runtime_cannot_turn(agent):
            unavailable_emails.add(member.email)
    out: dict[str, dict[str, int]] = {}
    for email, buckets in (workload_by_email or {}).items():
        email_key = str(email or '').strip().lower()
        if email_key not in unavailable_emails:
            continue
        teams = email_to_teams.get(email_key) if email_to_teams else None
        if not teams:
            # Fall back to the pool member's teams.
            for member in members:
                if member.email == email_key:
                    teams = member.teams
                    break
        for team in teams or ():
            dest = out.setdefault(str(team), {})
            if not isinstance(buckets, dict):
                continue
            for name, raw in buckets.items():
                try:
                    dest[str(name)] = dest.get(str(name), 0) + int(raw or 0)
                except (TypeError, ValueError):
                    continue
    return out


def team_has_actionable_work(teams, workload_by_team, actionable_buckets) -> bool:
    """True when ANY of ``teams`` has actionable Pulse work.

    Team-level on purpose — see the starvation-trap note in the module docstring.
    """
    for team in teams or ():
        buckets = (workload_by_team or {}).get(team) or {}
        for name in actionable_buckets:
            try:
                if int(buckets.get(name, 0) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _bucket_count(workload: dict | None, buckets) -> int:
    if not isinstance(workload, dict):
        return 0
    total = 0
    for name in buckets or ():
        try:
            total += int(workload.get(name, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


_REVIEW_LANE_TEAMS = frozenset({'review', 'reviews'})
_REVIEW_HANDOFF_BUCKETS = frozenset({'in_review', 'review'})


def _is_review_lane(teams) -> bool:
    """True when this agent sits on the Review team (PLAT-7032 KEEP scope)."""
    return any(str(t or '').strip().lower() in _REVIEW_LANE_TEAMS for t in (teams or ()))


def _actionable_buckets_for_personal_keep(teams, actionable_buckets) -> tuple:
    """in_review is KEEP/WAKE only for review-lane seats.

    Research/devops/merge owners of in_review (or awaiting_merge mapped into
    that bucket) are waiting on another lane — not spending the GLM home.
    Scout 2026-08-31: 255k parked KV, 31 queue-blind heartbeats, 2 in_review
    items, lease_idle_waste True, and PLAT-7032 KEEP still pinned a TP4.

    `doing` is never KEEP/WAKE on its own: a stale doing row is how Yuki
    kept a GLM lease while heartbeats said no ready work. `in_progress`
    still wakes a *hibernated* seat so it can resume; running idle
    holders with only in_progress already yield via nonproductive_heartbeat.
    """
    buckets = tuple(actionable_buckets or ())
    stuck = frozenset({'doing'})
    if _is_review_lane(teams):
        return tuple(
            b for b in buckets
            if str(b or '').strip().lower() not in stuck
        )
    return tuple(
        b for b in buckets
        if str(b or '').strip().lower() not in (_REVIEW_HANDOFF_BUCKETS | stuck)
    )


def agent_has_assigned_work(email: str, workload_by_email, actionable_buckets) -> bool:
    """True when this agent email has actionable Pulse assignments of their own."""
    email = str(email or '').strip().lower()
    if not email:
        return False
    return _bucket_count((workload_by_email or {}).get(email), actionable_buckets) > 0


def agent_has_unread_dm(email: str, unread_by_email) -> bool:
    """True when Connect has undelivered/unread work for this agent email."""
    email = str(email or '').strip().lower()
    if not email:
        return False
    try:
        return int((unread_by_email or {}).get(email, 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def _email_identity_keys(email: str) -> tuple[str, ...]:
    """Pulse may store @agents.shizuha.io while Hive cards key @shizuha.com."""
    email = str(email or '').strip().lower()
    if not email:
        return ()
    keys = {email}
    if '@' in email:
        local, _, domain = email.partition('@')
        if domain in {'shizuha.com', 'agents.shizuha.io'} and local:
            keys.add(f'{local}@shizuha.com')
            keys.add(f'{local}@agents.shizuha.io')
    return tuple(keys)


def agent_has_assigned_alert(email: str, alerts_by_email) -> bool:
    """True when Pulse has assigned alerts for this agent email."""
    if not alerts_by_email:
        return False
    try:
        for key in _email_identity_keys(email):
            if int((alerts_by_email or {}).get(key, 0) or 0) > 0:
                return True
    except (TypeError, ValueError):
        return False
    return False


def agent_lease_worthiness(
    *,
    email: str,
    teams,
    workload_by_team,
    workload_by_email,
    unread_by_email,
    alerts_by_email,
    actionable_buckets,
    heartbeat_outcome: str = '',
    agent_status: str = '',
    unseated_workload_by_team=None,
    activity_phase: str = '',
    activity_phase_for_ms=None,
    lease_idle_waste: bool = False,
) -> tuple[bool, str, bool]:
    """Whether this agent deserves a model lease, and why.

    Returns ``(wants, reason, personal)`` where ``personal`` is True when the
    signal is agent-specific (assigned tasks / DM / alert) rather than only
    team-queue (anti-starvation wake path).

    Priority of reasons (first match wins for logging):
      1. unread_dm — Connect undelivered message
      2. assigned_alert — Pulse assigned alert
      3. running + nonproductive heartbeat / idle-waste — drop, even when
         Pulse still lists ready/open/todo. Live 2026-09-15: kei was
         assigned_tasks because awaiting_deploy bucketed as open, while
         pulse_get_my_work returned readyTaskCount=0 / queue_empty. The
         in-pod look is SoT for a running seat. Hibernated seats still
         wake on Pulse ready (they have not looked this tenure).
      4. assigned_tasks (unstarted) — ready/open/todo/needs_triage
      5. assigned_tasks (in_review/review) — a *review-lane* seat that owns
         in_review work is productive even when its latest heartbeat reports
         queue_empty: it is still holding the lane. Do NOT hibernate it on a
         nonproductive heartbeat (PLAT-7032). in_progress is NOT included
         (Hiro/rei 2026-08-21). Non-review owners of in_review do not KEEP.
         A running idle-waste holder yields even on the review lane (Scout
         2026-08-31).
      6. team_queue — unstarted team work may wake a stopped agent.
         Not applied to a *running* seat whose latest heartbeat is
         queue_empty / all_blocked / future_due / not_observed.

    Team-queue remains so a stopped agent whose team has work can still win a
    lease and be scaled up — the starvation trap documented above.
    """
    # `doing` is the same class as `in_progress`: Pulse still lists the
    # row, but a queue_empty / idle holder is not spending the GLM home on
    # it (Yuki 2026-09-02: Hive "1 doing" + Live activity "No ready work"
    # while pulse_get_my_tasks was absent). Unstarted ready/open/todo still
    # KEEP — that work can actually start.
    wake_buckets = tuple(
        b for b in actionable_buckets
        if str(b).strip().lower() in {
            'ready', 'open', 'todo', 'needs_triage',
        }
    )
    if agent_has_unread_dm(email, unread_by_email):
        return True, 'unread_dm', True
    if agent_has_assigned_alert(email, alerts_by_email):
        return True, 'assigned_alert', True
    from .on_demand import NON_PRODUCTIVE_HEARTBEAT_OUTCOMES, _heartbeat_is_non_productive
    from .activity_phase import LEASE_IDLE_WASTE_MIN_MS, normalize_activity_phase
    outcome = str(heartbeat_outcome or '').strip().lower()
    status = str(agent_status or '').strip().lower()
    phase = normalize_activity_phase(activity_phase) or ''
    try:
        idle_ms = float(activity_phase_for_ms) if activity_phase_for_ms is not None else None
    except (TypeError, ValueError):
        idle_ms = None
    idle_long = (
        status == 'running'
        and phase == 'idle'
        and idle_ms is not None
        and idle_ms >= LEASE_IDLE_WASTE_MIN_MS
    )
    # A running holder sitting idle on a scarce GLM home is waste even when
    # Pulse still lists ready/open/todo or in_review. Yield before KEEP so
    # Scout-class queue-blind parks and kei-class awaiting_deploy-as-open
    # snapshots cannot pin a TP4. Review-lane KEEP below still covers a
    # queue_empty review seat that is *not* idle-waste / nonproductive.
    if status == 'running' and (bool(lease_idle_waste) or idle_long):
        return False, 'nonproductive_heartbeat', False
    review_keep = (
        _is_review_lane(teams)
        and _bucket_count(workload_by_email.get(email), ('in_review', 'review')) > 0
    )
    if (
        status == 'running'
        and (
            outcome in NON_PRODUCTIVE_HEARTBEAT_OUTCOMES
            or _heartbeat_is_non_productive({'heartbeat_outcome': outcome})
        )
        and not review_keep
    ):
        return False, 'nonproductive_heartbeat', False
    if agent_has_assigned_work(email, workload_by_email, wake_buckets):
        return True, 'assigned_tasks', True
    # Personal in_review/review WIP is a KEEP signal that outranks a
    # nonproductive heartbeat *for the review lane*: a review seat between
    # reviews is still holding the lane (PLAT-7032). in_progress is
    # deliberately NOT included (Hiro/rei 2026-08-21). Non-review owners of
    # in_review are waiting on another lane and must not pin GLM.
    if review_keep:
        return True, 'assigned_tasks', True
    # Wake a hibernated seat that owns personal in_progress/review work. A
    # hibernated seat is not 'running', so the idle-waste drop above does
    # not apply to it; this check scales it back up (PLAT-7032 keeps the
    # in_review/review wake path intact for review seats only).
    if agent_has_assigned_work(
        email, workload_by_email,
        _actionable_buckets_for_personal_keep(teams, actionable_buckets),
    ):
        return True, 'assigned_tasks', True
    # Unstarted team work may wake a hibernated teammate (starvation trap).
    # in_progress/review is already seated — treating it as team_queue re-grants
    # the seat we just revoked (mio/nova 2026-08-21 after the queue_empty fix).
    if team_has_actionable_work(teams, workload_by_team, wake_buckets):
        return True, 'team_queue', False
    if team_has_actionable_work(teams, unseated_workload_by_team, actionable_buckets):
        return True, 'team_queue', False
    return False, '', False


def _board_holder_principals(board: dict | None) -> set[str]:
    """Principals currently on the lease board (multi-holder + legacy shapes)."""
    if not isinstance(board, dict):
        return set()
    holders = []
    raw_holders = board.get('holders')
    if isinstance(raw_holders, list) and raw_holders:
        holders = [h for h in raw_holders if isinstance(h, dict) and h.get('principal')]
    elif isinstance(board.get('holder'), dict) and board['holder'].get('principal'):
        holders = [board['holder']]
    return {str(h.get('principal') or '').strip() for h in holders if str(h.get('principal') or '').strip()}


def lease_wake_available(*, agent, client=None, configs=None) -> tuple[bool, str]:
    """Whether this agent already has authority to start its governed runtime.

    Connect's DM fanout wakes a stopped agent the moment a message lands
    (HIVE-602), but waking into a full lease pool is pure churn: the agent
    boots, cannot hold the model (Cortex's request path is lease-gated), and
    the next 10-min reconcile hibernates it again as "does not hold the model
    lease" (observed 2026-08-08: kei/ryo flapping on a 7/7 DeepSeek pool).

    Keep-resident / CEO Office seats stay running by **holding a lease**, not
    by bypassing the board (operator 2026-08-18). A full pool defers their
    wake the same as any other governed agent; the lease tick grants them a
    real slot (preempting a non-reserved holder if needed) and then starts
    them.

    Returns ``(available, reason)``:
      - (False, 'lease_pool_full')  — governed pool has no free slot and this
        agent is not already a holder/deadline-holder: defer the wake. The
        10-min lease reconcile grants a slot the moment one frees (the unread
        DM keeps this agent worthy/eligible), then scales it up.
      - (True,  'no_lease_pool')    — agent's model is not governed by a pool
        (or its model is unknown): no lease to wait for, wake normally.
      - (True,  'already_holder')   — already on the board; waking is required.
      - (False, 'await_reconcile_grant') — capacity is available, but only the
        ordinary planner selects a worthy candidate and grants its lease.
        Reading a free slot does not reserve it: concurrent wakes must not all
        boot before Cortex's request path can grant the first caller a lease.
      - (False, 'board_unreadable') — cannot read the board; cannot confirm a
        slot. Strict per operator 2026-08-08 ("only wake when a slot is
        available"): defer rather than risk the boot-then-rehibernate flap.
        The unread DM or team work keeps the agent worthy/eligible, so the next
        readable 10-min lease reconcile grants a slot and scales it up; nothing
        is stranded, only delayed until the board is confirmable.
      - (False, 'board_not_governed') — Cortex does not recognize the configured
        lease board; its empty default is not evidence of free capacity.
    """
    username = str(
        agent.get('username') or agent.get('agent_username') or ''
    ).strip()
    model = _canonical_pool_model(
        agent.get('effective_model') or agent.get('model') or ''
    )
    if not username or not model:
        return True, 'no_lease_pool'
    # Match the 10-min reconcile client. timeout=2.0 turned a Cortex blip
    # into board_unreadable and stuck waiters on a stale HIVE-1713 message
    # (goro/hiro/shion 2026-09-02) while the reconcile path still read the board.
    client = client or CortexLeaseClient(timeout=10.0)
    configured = list(configs) if configs is not None else all_pool_configs()
    governed: dict | None = None
    for cfg in configured:
        if _canonical_pool_model(cfg.get('model_id') or '') == model:
            governed = cfg
            break
    if governed is None:
        return True, 'no_lease_pool'
    # Normalize only the Hive membership join. Cortex resource IDs preserve
    # case: glm-5.3-flash returns an empty, unleased board while GLM-5.3-Flash
    # can be full. Use the same configured ID as the planner and Agents page.
    board = client.board(str(governed['model_id']).strip())
    if board is None:
        return False, 'board_unreadable'
    if board.get('leased') is False:
        return False, 'board_not_governed'
    holders = _board_holder_principals(board)
    principal = principal_for(username)
    max_holders = max(1, int(board.get('max_holders') or governed.get('max_holders') or 1))
    slots_free = max(0, max_holders - len(holders))
    if principal in holders:
        return True, 'already_holder'
    # Cortex KV homes are the physical DeepSeek seats (operator 2026-08-25):
    # Hive must not boot a 9th agent while 8 TTL-protected homes are full.
    # A leftover hibernated home (revi) still occupies a seat even when the
    # lease board has a free holder slot.
    res = board.get('residency') if isinstance(board.get('residency'), dict) else {}
    resident_principals = {
        str(p).strip() for p in (res.get('principals') or []) if str(p).strip()
    }
    if principal in resident_principals and slots_free > 0:
        return False, 'await_reconcile_grant'
    if principal in resident_principals:
        # Leftover home without a Hive lease while the holder board is full:
        # waking this agent keeps a non-holder living and hitting Cortex
        # (ryo connect-wake 2026-08-28). Defer; the lease tick grants a
        # real slot or hibernate releases the leftover home.
        return False, 'lease_pool_full'
    if res.get('full'):
        return False, 'kv_residency_full'
    if slots_free > 0:
        return False, 'await_reconcile_grant'
    return False, 'lease_pool_full'


def _wake_preempt_signals(fetch_signals=None):
    """Worthiness inputs for the wake-preempt fence, or all-``None``.

    Live path reuses the reconcile command's exact fetchers so the PLAT-4490
    Connect completeness contract and the all-orgs Pulse snapshot have one
    source of truth. Any failure is ``None`` — the caller must then defer
    (fail-closed: an unrankable board must never be preempted).
    """
    if fetch_signals is not None:
        try:
            got = fetch_signals()
        except Exception:  # noqa: BLE001 — signals must never break the wake
            return None, None, None, None
        if not got:
            return None, None, None, None
        if len(got) == 3:
            workload, unread, alerts = got
            rosters = []
        else:
            workload, unread, alerts, rosters = got
        return (workload or {}), (unread or {}), (alerts or {}), (rosters or [])
    try:
        from .management.commands.reconcile_on_demand_agents import Command

        command = Command()
        payload = command._fetch_pulse_snapshot() or {}
        if not payload:
            return None, None, None, None
        workload = payload.get('workload') or {}
        alerts = payload.get('alerts_by_email') or payload.get('alert_workload') or {}
        rosters = payload.get('rosters') or payload.get('teams') or []
        if not rosters:
            rosters = command._fetch_pulse_rosters() or []
        connect = command._fetch_connect_state()
        if not getattr(connect, 'available', False):
            return None, None, None, None
        return workload, dict(connect.unread_by_email or {}), alerts, rosters
    except Exception:  # noqa: BLE001 — signals must never break the wake
        return None, None, None, None


def wake_preempt_lease_slot(
    *,
    agent: dict,
    reason: str = '',
    agents: list | None = None,
    client: CortexLeaseClient | None = None,
    configs=None,
    now: float | None = None,
    fetch_signals=None,
    idle_confirmation=None,
) -> tuple[bool, str]:
    """PLAT-9052 direction-2: fenced wake-time preemption of a quiet holder.

    The connect-wake path defers on ``lease_pool_full`` regardless of the
    wakee's worthiness, and the 10-min reconcile's voluntary revokes can be
    blocked indefinitely by the apply-time idle fence (PLAT-9052 mode 1) — a
    fence that defers forever is functionally a leak. A wake for a seat
    holding non-blocked assigned/due work must therefore be able to take a
    slot at wake time (PLAT-7032 doctrine), the same way the reconcile's
    reserved pass already preempts a non-reserved holder for keep-resident
    seats.

    Fences (every one fail-closed to "defer"):
      1. Only the unambiguous full-board case is preemptable; a board that
         since freed a slot defers back to the ordinary reconcile grant.
      2. A keep-resident / warm-pool wakee never bypasses the board
         (operator 2026-08-18) — the reconcile's reserved pass grants it.
      3. The wakee must hold a PERSONAL signal (unread DM — attested by the
         connect_dm wake itself per HIVE-602 — assigned alert, or unstarted
         assigned tasks). Team-queue-only wakes keep deferring.
      4. Worthiness signals (Pulse workload/alerts + Connect undelivered +
         rosters) must be fetchable; otherwise the board cannot be ranked
         and nobody is evicted.
      5. Victims exclude reserved seats, holders whose runtime is not
         fresh-idle at apply time (the reconcile's own
         ``runtime_idle_confirmation`` fence), and holders carrying their
         own personal signal — work never evicts work.
      6. Revoke then grant; a failed grant leaves the slot to the 10-min
         tick (same recovery as the reconcile's reserved pass).

    Returns ``(preempted, detail)``. ``preempted`` True means the wakee now
    holds a real grant and the caller must proceed with the ordinary start.
    """
    import time as _time

    now = now if now is not None else _time.time()
    username = str(agent.get('username') or agent.get('agent_username') or '').strip()
    model = _canonical_pool_model(agent.get('effective_model') or agent.get('model') or '')
    email = str(agent.get('email') or '').strip().lower()
    if not username or not model:
        return False, 'no_lease_pool'

    from .on_demand import agent_reserved_warm_seat
    if agent_reserved_warm_seat(agent):
        return False, 'wakee_reserved'

    workload_by_email, unread_by_email, alerts_by_email, rosters = _wake_preempt_signals(
        fetch_signals,
    )
    if workload_by_email is None:
        return False, 'worthiness_signals_unavailable'

    from .on_demand import ACTIONABLE_BUCKETS, memberships_by_email
    teams_by_email = memberships_by_email(rosters or [])

    wants, want_reason, personal = agent_lease_worthiness(
        email=email,
        teams=sorted(teams_by_email.get(email) or ()),
        workload_by_team={},
        workload_by_email=workload_by_email,
        unread_by_email=unread_by_email,
        alerts_by_email=alerts_by_email,
        actionable_buckets=ACTIONABLE_BUCKETS,
    )
    if not (wants and personal):
        # HIVE-602 contract: Connect calls connect-wake the moment a message
        # lands for a STOPPED agent, so the DM is undelivered by construction
        # even if the Connect snapshot lags the just-landed message.
        if str(reason or '').strip().lower().startswith('connect_dm'):
            wants, personal, want_reason = True, True, 'connect_dm_attested'
        else:
            return False, f'wakee_not_personally_worthy ({want_reason})'

    client = client or CortexLeaseClient(timeout=10.0)
    configured = list(configs) if configs is not None else all_pool_configs()
    governed = next(
        (c for c in configured
         if _canonical_pool_model(c.get('model_id') or '') == model),
        None,
    )
    if governed is None:
        return False, 'no_lease_pool'
    model_id = str(governed['model_id']).strip()
    board = client.board(model_id)
    if board is None:
        return False, 'board_unreadable'
    if board.get('leased') is False:
        return False, 'board_not_governed'

    holders = [
        h for h in (board.get('holders') or [])
        if isinstance(h, dict) and str(h.get('principal') or '').strip()
    ]
    if not holders and isinstance(board.get('holder'), dict) and board['holder'].get('principal'):
        holders = [board['holder']]
    principal = principal_for(username)
    if principal in {str(h.get('principal') or '').strip() for h in holders}:
        return False, 'already_holder'
    max_holders = max(1, int(board.get('max_holders') or governed.get('max_holders') or 1))
    if len(holders) < max_holders:
        # The starvation resolved while we looked: defer to the ordinary
        # reconcile grant (a free slot is still not a reservation).
        return False, 'slot_freed_since_defer'
    res = board.get('residency') if isinstance(board.get('residency'), dict) else {}
    if principal in {str(p).strip() for p in (res.get('principals') or []) if str(p).strip()}:
        return False, 'await_reconcile_grant'

    agent_by_username = {}
    for a in (agents or []):
        uname = str(a.get('username') or a.get('agent_username') or '').strip().lower()
        if uname:
            agent_by_username[uname] = a

    reserved_usernames = {
        uname
        for uname in (
            _username_from_principal(str(h.get('principal') or '').strip())
            for h in holders
        )
        if uname
        and agent_reserved_warm_seat(agent_by_username.get(uname) or {})
        and _agent_dict_auto_wakeable(agent_by_username.get(uname))
    }

    if idle_confirmation is None:
        from .management.commands.reconcile_on_demand_agents import (
            runtime_idle_confirmation as _live_idle_confirmation,
        )
        idle_confirmation = _live_idle_confirmation

    def _has_personal_signal(uname: str) -> bool:
        agent_row = agent_by_username.get(uname) or {}
        holder_email = str(agent_row.get('email') or '').strip().lower()
        if agent_has_unread_dm(holder_email, unread_by_email):
            return True
        if agent_has_assigned_alert(holder_email, alerts_by_email):
            return True
        return _bucket_count(workload_by_email.get(holder_email), ACTIONABLE_BUCKETS) > 0

    active: set[str] = set()
    candidates: list[dict] = []
    for row in holders:
        uname = _username_from_principal(str(row.get('principal') or '').strip())
        if not uname or uname in reserved_usernames:
            continue
        # Fresh-idle fence (the reconcile's apply-time fence, inverted): a
        # holder that is not affirmatively fresh-idle is treated as active
        # and is never evicted — unknown/stale/working all fail closed.
        try:
            idle_ok, _why = idle_confirmation(uname)
        except Exception:  # noqa: BLE001 — fence failure must not evict
            idle_ok = False
        if not idle_ok:
            active.add(uname)
            continue
        if _has_personal_signal(uname):
            continue
        candidates.append(row)

    # HIVE-2862: over-floor waste-holders yield first. A team holding MORE
    # leases than its floor contributes an idle lease only to the pool's loss;
    # releasing it frees a seat for a waiter whose team has actionable work
    # without interrupting any productive claim (fresh-idle + personal-signal
    # fences above still gate every candidate).
    yield_priority = _idle_waste_yield_priorities(
        candidates,
        all_holders=holders,
        agents=agents,
        teams_by_email=teams_by_email,
        floor=critical_team_policy(),
    )

    victim = reserved_preempt_victim(candidates, reserved_usernames, active=active,
                                     yield_priority=yield_priority)
    if victim is None:
        return False, 'no_preemptable_holder'

    if not client.revoke(model_id, holder=victim, force=True,
                         reason=f'preempt-for-connect-wake:{email or username}'):
        return False, 'revoke_failed'
    _mark_preempt_retry(victim, now)
    if not client.grant(
        model_id, principal,
        min_hold_s=int(governed['min_hold_seconds']),
        max_hold_s=int(governed['max_hold_seconds']),
        reason='connect-wake-preempt',
        max_holders=max_holders,
    ):
        # The victim stays revoked and the freed slot goes to the 10-min
        # tick (same recovery as the reconcile's reserved pass).
        return False, 'grant_failed_after_revoke'
    _record_holding(principal, model_id)
    return True, f'preempted {victim} for {email or username} ({want_reason})'


def decide_lease(
    *,
    now: float,
    members: list[PoolMember],
    wants: set[str],
    current: dict | None,
    min_hold_seconds: int = DEFAULT_MIN_HOLD_SECONDS,
    max_hold_seconds: int = DEFAULT_MAX_HOLD_SECONDS,
    idle_release_seconds: int = DEFAULT_IDLE_RELEASE_SECONDS,
    running: set[str] | None = None,
    personal: set[str] | None = None,
    assigned: set[str] | None = None,
    active: set[str] | None = None,
    reserved: set[str] | None = None,
    preempt_retry: set[str] | None = None,
    holder_usernames: set[str] | None = None,
    critical_floor: dict[str, int] | None = None,
) -> LeaseDecision:
    """Pure lease state machine.

    ``wants`` holds usernames Hive considers worthy of the model (assigned work,
    unread DM, alert, or team queue). ``current`` is the Cortex board's
    ``holder`` dict, or None when free.

    ``running`` — usernames process-running; stopped holders are force-revoked.
    ``personal`` — subset of ``wants`` with agent-specific signals; preferred
    over team-queue-only candidates when granting (less waste on idle wakees).
    ``assigned`` — subset of ``personal`` with assigned Pulse tasks. Preferred
    over unread-DM / alert-only personal so a 1-admission Qwen seat goes to
    revi's security WIP instead of reika's leftover DM (2026-08-22).
    ``active`` — usernames whose runtime reports an in-progress turn/tool phase.
    Active holders are never preempted: max-hold is a fairness boundary, not
    permission to interrupt work.  The next quiet observation performs the
    handoff. Cortex ``idle_for_s`` is the same fence for the gap between
    turns: a holder who used the model inside ``idle_release_seconds`` is
    still loaded, even if runtime telemetry already dropped to quiet.
    ``reserved`` — keep-resident / CEO Office usernames. They occupy one honest
    seat continuously (operator 2026-08-18). Unused/idle/max-hold must not
    revoke them: live 2026-08-18 unused-revoked Yuna (idle 522s) then the
    reserved pass preempted Saki to put Yuna back — musical chairs that
    evicted Engineering while Yuna stayed unused.
    ``holder_usernames`` — every current board holder (bare usernames). Unused
    revoke only fires when someone who is NOT already a holder is waiting;
    counting fellow holders as waiters bounced kai/nagi/sara every 10 minutes
    (2026-08-22) so they never sent a Cortex turn.

    ``critical_floor`` — the admin-editable team→min_leases policy.  When
    present, the grant path seats an UNMET floor COVERAGE ahead of ordinary
    fairness signals among the waiters (PLAT-6618 verify leg, 2026-10-07:
    slots freed by hibernate won here and seated fellow-team waiters while
    the critical team stayed critical).  No policy ⇒ no-op, same mirror as
    ``critical_team_floor_state``.
    """
    by_principal = {principal_for(m.username): m for m in members}
    waiting = tuple(sorted(principal_for(u) for u in wants))
    personal = personal or set()
    assigned = assigned or set()
    active = active or set()
    reserved = reserved or set()
    preempt_retry = preempt_retry or set()
    floor_seaters = unmet_floor_seaters(
        candidates=list(members),
        holder_usernames=holder_usernames or set(),
        policy=critical_floor or {},
    ) if critical_floor else set()

    if current is None:
        if not wants:
            return LeaseDecision('none', reason='no worthy agent (no tasks/DM/alert/team queue)',
                                 waiting=waiting)
        # Prefer an already-active runtime before every ordinary fairness signal.
        # Cortex intentionally drops a lease from its readable board at hard
        # expiry.  If that happens during a long turn, the holder therefore
        # re-enters this FREE-slot path alongside normal waiters.  Letting a
        # merely waiting principal win here hands the warm slot away mid-turn;
        # active continuity must win and fairness resumes at the next quiet
        # observation.
        #
        # Otherwise prefer: keep-resident-preempt retry → personal signal →
        # running → least-recently-held → name.
        candidates = sorted(
            (m for m in members if m.username in wants),
            key=lambda m: (
                0 if m.username in floor_seaters else 1,
                0 if m.username in preempt_retry else 1,
                0 if m.username in active else 1,
                0 if m.username in assigned else 1,
                0 if m.username in personal else 1,
                0 if (running is None or m.username in running) else 1,
                m.last_held_at,
                m.username,
            ),
        )
        chosen = candidates[0]
        if chosen.username in active:
            why = 'active-turn-continuity'
        else:
            if chosen.username in assigned:
                why = 'assigned-tasks'
            elif chosen.username in personal:
                why = 'personal-work'
            else:
                why = 'team-queue'
        return LeaseDecision(
            'grant', holder=principal_for(chosen.username),
            reason=f'{why} least-recently-held of {len(candidates)} waiting',
            waiting=waiting,
        )

    holder = str(current.get('principal') or '')
    held_for = float(current.get('held_for_s') or 0)
    idle_for = float(current.get('idle_for_s') or 0)
    member = by_principal.get(holder)
    holder_username = holder.removeprefix('agent-') if holder.startswith('agent-') else holder

    # A holder that left the pool must lose the model immediately; it is no longer
    # governed, so leaving it in place would pin the slot against everyone else.
    if member is None:
        return LeaseDecision('revoke', holder=holder, force=True,
                             reason='holder is no longer a pool member', waiting=waiting)

    # STOPPED / hibernated / disabled holder: drop immediately (force). Min-hold
    # protects live KV warmth, not a Deployment scaled to 0.
    if running is not None and holder_username not in running:
        return LeaseDecision(
            'revoke', holder=holder, force=True,
            reason='holder agent is not running',
            waiting=waiting,
        )

    # Reserved seats stay on the board by holding. Revoking an unused Yuna
    # just to preempt a working/waiting agent on the next pass is the same
    # bypass we withdrew: she must consume a real slot, not shuffle others
    # off so she can sit idle on a freshly stolen one.
    if holder_username in reserved:
        return LeaseDecision(
            'keep', holder=holder,
            reason='keep-resident occupies an honest seat',
            waiting=waiting,
        )

    held_usernames = set(holder_usernames) if holder_usernames is not None else {holder_username}
    held_usernames.add(holder_username)
    others_waiting = {u for u in wants if u not in held_usernames}

    # Never trade correctness for fairness. A long tool call can make Cortex's
    # request-level ``idle_for_s`` look idle even though the agent is actively
    # changing state in another service or checkout. Runtime activity telemetry
    # is the stronger interruption fence; defer every voluntary/preemptive
    # handoff until the holder reaches a quiet boundary.
    if holder_username in active:
        return LeaseDecision(
            'keep', holder=holder,
            reason='holder runtime is actively working; handoff deferred',
            waiting=waiting,
        )

    # Unused grant: the holder never sent a Cortex request after the lease
    # (idle_for ~= held_for). Min-hold protects KV warmth, not an empty
    # claim. Live 2026-08-17: saki held 21m with idle_for==held_for while
    # 13 waiters sat hibernated.
    unused = (
        held_for > 0
        and abs(idle_for - held_for) <= 2
    )
    if unused and others_waiting and idle_for >= idle_release_seconds:
        return LeaseDecision(
            'revoke', holder=holder, force=True, voluntary=True,
            reason=(
                f'unused lease idle {int(idle_for)}s with '
                f'{len(others_waiting)} waiting'
            ),
            waiting=waiting,
        )

    # Queue drained: release NOW, before min-hold. Min-hold protects against
    # PREEMPTION, never against a holder with nothing left to do — holding an idle
    # lease is exactly the throughput loss this design exists to avoid.
    if member.username not in wants:
        return LeaseDecision('revoke', holder=holder, voluntary=True,
                             reason='holder team queue drained', waiting=waiting)

    if not others_waiting:
        return LeaseDecision('keep', holder=holder,
                             reason='holder still has work and nobody is waiting',
                             waiting=waiting)

    if held_for >= max_hold_seconds:
        # Wall-clock max-hold is a parked-seat fairness bound, not a loaded-
        # session interrupt. Cortex last_activity (idle_for) is the refresh:
        # every GLM turn slides the right to keep the seat. Ni 2026-09-09:
        # 45m of 287k turns, 74s since last token, max-hold revoked +
        # yield-homes wiped the KV, then fill re-granted ni — 786s cold hop.
        # A static bump to "multiple hours" would only delay the same wipe.
        if idle_for < idle_release_seconds:
            return LeaseDecision(
                'keep', holder=holder,
                reason=(
                    f'loaded holder idle {int(idle_for)}s; '
                    f'max-hold refresh (held {int(held_for)}s)'
                ),
                waiting=waiting,
            )
        return LeaseDecision('revoke', holder=holder,
                             reason=f'max hold {max_hold_seconds}s reached with {len(others_waiting)} waiting',
                             waiting=waiting)

    if held_for >= min_hold_seconds and idle_for >= idle_release_seconds:
        return LeaseDecision('revoke', holder=holder,
                             reason=f'idle {int(idle_for)}s past min hold with {len(others_waiting)} waiting',
                             waiting=waiting)

    return LeaseDecision('keep', holder=holder,
                         reason=f'min hold protects KV warmth ({int(held_for)}s held)',
                         waiting=waiting)


def scale_plan(*, members: list[PoolMember], holder: str | None = None,
               holders: set[str] | list[str] | None = None,
               active: set[str] | None = None) -> dict[str, bool]:
    """Desired running-state: current holders and active runtimes stay up.

    ``holders`` is the multi-holder set (principals). ``holder`` is kept for
    single-holder callers and is merged into the set when provided.

    Activity is an interruption fence, not a lease grant.  A runtime can be
    active but absent from Cortex's board when its hard lease expires between
    request admission and this periodic Hive tick.  Keeping it running lets the
    in-flight response finish; the next quiet tick may hibernate it normally.
    """
    active = active or set()
    wanted: set[str] = set()
    if holders:
        wanted.update(str(h) for h in holders if h)
    if holder:
        wanted.add(str(holder))
    return {
        m.username: (
            principal_for(m.username) in wanted or m.username in active
        )
        for m in members
    }


def _username_from_principal(principal: str) -> str:
    name = str(principal or '').strip()
    return name.removeprefix('agent-') if name.startswith('agent-') else name


def _overflow_revoke_victim(
    holders: list[dict],
    *,
    active: set[str] | None = None,
    reserved: set[str] | None = None,
    residents: set[str] | None = None,
) -> str | None:
    """Idlest non-active non-reserved holder when the Hive wake cap shrinks.

    Prefer a *homeless* holder. Revoking a still-homed seat is how nagi
    lost the lease while Redis kept the 30m book and kei took the slot
    (2026-09-02). Only fall back to a homed holder when every extra seat
    is homed — leftover yield then deletes the book after hibernate.
    """
    active = active or set()
    reserved = reserved or set()
    residents = residents or set()

    def _rank(*, skip_active: bool, skip_residents: bool) -> list[tuple[float, str]]:
        ranked: list[tuple[float, str]] = []
        for hd in holders:
            if not isinstance(hd, dict):
                continue
            principal = str(hd.get('principal') or '').strip()
            if not principal:
                continue
            uname = _username_from_principal(principal)
            if skip_active and (uname in active or principal in active):
                continue
            if uname in reserved or principal in reserved:
                continue
            if skip_residents and (
                principal in residents or uname in residents
                or f'agent-{uname}' in residents
            ):
                continue
            try:
                idle = float(hd.get('idle_for_s') or 0)
            except (TypeError, ValueError):
                idle = 0.0
            ranked.append((idle, principal))
        ranked.sort(reverse=True)
        return ranked

    for skip_active, skip_residents in (
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ):
        ranked = _rank(skip_active=skip_active, skip_residents=skip_residents)
        if ranked:
            return ranked[0][1]
    return None


def reserved_preempt_victim(
    holders: list[dict],
    reserved_usernames: set[str],
    *,
    active: set[str] | None = None,
    yield_priority: dict[str, int] | None = None,
) -> str | None:
    """Pick a non-reserved, non-active holder so a keep-resident can take a real slot.

    Honest lease math (operator 2026-08-18): CEO Office stays up by occupying
    one of the N holder seats, not by running on shared/unleased capacity.
    Prefer an unused grant, then the idlest non-reserved holder. Never
    interrupt an in-flight reserved-or-active seat.

    HIVE-2862: ``yield_priority`` optionally orders candidates so that a
    ``lease_idle_waste`` holder from a team ABOVE its floor yields first —
    an over-floor team releasing an idle lease is pure waste release, while
    the remaining candidates are productive claims. Priority ranks BEFORE
    the unused/unused ordering so the fairest release wins when supply is
    short; without the map the behavior is unchanged (missing entries
    default to 0).
    """
    active = active or set()
    priorities = yield_priority or {}
    ranked: list[tuple] = []
    for hd in holders:
        if not isinstance(hd, dict):
            continue
        principal = str(hd.get('principal') or '').strip()
        if not principal:
            continue
        uname = _username_from_principal(principal)
        if uname in reserved_usernames or uname in active:
            continue
        try:
            idle = float(hd.get('idle_for_s') or 0)
        except (TypeError, ValueError):
            idle = 0.0
        try:
            held = float(hd.get('held_for_s') or 0)
        except (TypeError, ValueError):
            held = 0.0
        unused = held > 0 and abs(idle - held) <= 2
        ranked.append((int(priorities.get(principal, 0)), 0 if unused else 1, -idle, principal))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][3]


def _idle_waste_yield_priorities(
    candidates: list[dict],
    *,
    all_holders: list[dict],
    agents: list | None,
    teams_by_email: dict[str, set[str]],
    floor: dict[str, int],
) -> dict[str, int]:
    """Rank preempt candidates so over-floor ``lease_idle_waste`` holders yield first.

    HIVE-2862. For every candidate row, priority is 0 when the holder is
    running-but-idle (``lease_idle_waste``) AND one of its teams holds MORE
    board leases than that team's floor — pure waste release from an
    over-favored team. Everything else ranks 1 (productive claims, reserved
    seats, waste inside the floor). Candidates carrying their own actionable
    work never reach this function (the personal-signal fence filters them).

    ``teams_by_email`` maps an agent email to its team slugs (unqualified).
    Counts include every board holder, reserved or not — a reserved seat is
    still its team's occupancy. Read errors fail OPEN to no priority so the
    parity helper can never block a wake.
    """
    agent_by_username = {}
    for row in (agents or []):
        uname = str(row.get('username') or row.get('agent_username') or '').strip().lower()
        if uname:
            agent_by_username[uname] = row

    holder_email_by_principal: dict[str, str] = {}
    for row in all_holders or []:
        principal = str(row.get('principal') or '').strip()
        if not principal:
            continue
        uname = _username_from_principal(principal)
        email = str((agent_by_username.get(uname) or {}).get('email') or '').strip().lower()
        if email:
            holder_email_by_principal[principal] = email

    try:
        team_counts = _idle_waste_team_counts(all_holders, agent_by_username, teams_by_email)
        priorities: dict[str, int] = {}
        for row in candidates or []:
            principal = str(row.get('principal') or '').strip()
            if not principal:
                continue
            uname = _username_from_principal(principal)
            email = holder_email_by_principal.get(principal) or ''
            if not email:
                continue
            if not _is_lease_idle_waste_row(agent_by_username.get(uname) or {}):
                continue
            over_floor = False
            for slug in (teams_by_email.get(email) or ()):
                if int(team_counts.get(slug, 0)) > int(floor.get(slug, 0) or 0):
                    over_floor = True
                    break
            priorities[principal] = 0 if over_floor else 1
        return priorities
    except Exception:  # noqa: BLE001 — parity must never block a wake
        logger.warning('idle-waste yield priority computation failed; falling back to LRH', exc_info=True)
        return {}


def _idle_waste_team_counts(all_holders, agent_by_username, teams_by_email):
    """Per-team board-lease counts across every holder, reserved or not."""
    counts = {}
    for row in all_holders or []:
        principal = str(row.get('principal') or '').strip()
        if not principal:
            continue
        uname = _username_from_principal(principal)
        email = str((agent_by_username.get(uname) or {}).get('email') or '').strip().lower()
        if not email:
            continue
        for slug in (teams_by_email.get(email) or ()):
            counts[slug] = counts.get(slug, 0) + 1
    return counts


def _is_lease_idle_waste_row(agent_row):
    """Waste flag as stamped by activity_phase.annotate on the agent card."""
    return bool(agent_row.get('lease_idle_waste'))


# ── Cortex board client ──────────────────────────────────────────────────────

HISTORY_CACHE_KEY = 'fleet:model-lease-history'


def _stamp_last_held(
    members: list[PoolMember],
    history: dict,
    holder: str,
    now: float,
    *,
    persist: bool = True,
) -> tuple[list[PoolMember], dict]:
    """Record that ``holder`` just used a lease so they lose same-tick LRH."""
    uname = str(holder or '').removeprefix('agent-').strip()
    if not uname:
        return members, history
    if not isinstance(history, dict):
        history = {}
    history = dict(history)
    history[uname] = float(now)
    if persist:
        cache.set(HISTORY_CACHE_KEY, history, None)
    stamped: list[PoolMember] = []
    for member in members:
        if member.username == uname:
            stamped.append(PoolMember(
                username=member.username,
                email=member.email,
                teams=member.teams,
                last_held_at=float(now),
            ))
        else:
            stamped.append(member)
    return stamped, history


def _preempt_retry_usernames() -> set[str]:
    raw = cache.get(PREEMPT_RETRY_CACHE_KEY) or {}
    if not isinstance(raw, dict):
        return set()
    return {str(name).strip() for name in raw if str(name).strip()}


def _mark_preempt_retry(username: str, now: float) -> None:
    uname = _username_from_principal(username)
    if not uname:
        return
    raw = cache.get(PREEMPT_RETRY_CACHE_KEY) or {}
    if not isinstance(raw, dict):
        raw = {}
    raw = dict(raw)
    raw[uname] = float(now)
    cache.set(PREEMPT_RETRY_CACHE_KEY, raw, None)


def _clear_preempt_retry(username: str) -> None:
    uname = _username_from_principal(username)
    if not uname:
        return
    raw = cache.get(PREEMPT_RETRY_CACHE_KEY) or {}
    if not isinstance(raw, dict) or uname not in raw:
        return
    raw = dict(raw)
    raw.pop(uname, None)
    cache.set(PREEMPT_RETRY_CACHE_KEY, raw, None)


def _record_holding(principal: str, model: str) -> None:
    """Remember that ``principal`` holds ``model`` (best-effort cache)."""
    principal = str(principal or '').strip()
    model = str(model or '').strip()
    if not principal or not model:
        return
    holdings = cache.get(HOLDINGS_CACHE_KEY) or {}
    if not isinstance(holdings, dict):
        holdings = {}
    models = set(holdings.get(principal) or [])
    models.add(model)
    holdings[principal] = sorted(models)
    cache.set(HOLDINGS_CACHE_KEY, holdings, None)


def _clear_holding(principal: str, model: str | None = None) -> None:
    principal = str(principal or '').strip()
    if not principal:
        return
    holdings = cache.get(HOLDINGS_CACHE_KEY) or {}
    if not isinstance(holdings, dict) or principal not in holdings:
        return
    if model is None:
        holdings.pop(principal, None)
    else:
        models = [m for m in (holdings.get(principal) or []) if m != model]
        if models:
            holdings[principal] = models
        else:
            holdings.pop(principal, None)
    cache.set(HOLDINGS_CACHE_KEY, holdings, None)


def _agent_dict_running(agent: dict | None) -> bool:
    """True when a lifecycle-agent dict is process-running (not just enabled)."""
    if not agent:
        return False
    # A CrashLoop / unready native pod is not a living holder. Leaving Sara
    # "running" on a full 10Gi workspace pinned a DeepSeek slot for 50+ min
    # while Engineering WIP sat on idle Codex Jun (2026-08-22).
    if agent.get('runtime_ready') is False:
        return False
    status = str(agent.get('status') or '').lower()
    if status in {'running', 'starting', 'provisioning', 'pending'}:
        # Prefer explicit enabled=False as not running when present
        if 'enabled' in agent and agent.get('enabled') is False:
            return False
        return True
    return False


def _agent_dict_active(
    agent: dict | None,
    *,
    now: float | None = None,
    max_age_seconds: int = 120,
) -> bool:
    """True when runtime telemetry proves an agent is inside productive work.

    ``activity_state`` is projected from the authenticated gateway telemetry
    used by the Agents page and the on-demand controller.  Explicit ``busy`` is
    accepted for runtime planes that expose the bridge health contract directly.
    Unknown/missing telemetry is not called active here; existing hold windows
    remain the fail-safe for older runtimes.

    A structured non-productive heartbeat (queue_empty / all_blocked / …)
    is not an in-flight turn. Skip-model preflight still stamps
    ``activity_state=working`` and refreshes last_active_at, which is how
    cora/reika KEEP-active occupied DeepSeek/Qwen seats while Pulse was
    empty (2026-08-22). Tool/responding still fences a real mid-turn that
    has not yet rewritten the previous skip outcome.
    """
    if not agent:
        return False
    from .on_demand import _heartbeat_is_non_productive
    from .activity_phase import resolve_activity_phase
    if _heartbeat_is_non_productive(agent):
        phase = resolve_activity_phase(agent)
        if phase not in {'tool', 'responding'}:
            return False
    if agent.get('busy') is True:
        return True
    from .activity_phase import is_productive_activity
    state_active = is_productive_activity(agent) or str(
        agent.get('activity_state')
        or agent.get('activityState')
        or ''
    ).strip().lower() in {
        'working',
        'busy',
        'running_turn',
        'running-tool',
        'tool',
        'thinking',
        'responding',
    }
    if not state_active:
        return False

    # Connect retains the latest telemetry snapshot. A dead bridge may leave
    # ``activity_state=working`` behind indefinitely, so freshness is part of
    # the interruption fence; otherwise one stale row can pin a scarce model
    # lease forever. Direct ``busy=True`` above is a same-request runtime health
    # observation and does not need this transported timestamp.
    return _agent_dict_telemetry_fresh(
        agent,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def _agent_dict_telemetry_fresh(
    agent: dict | None,
    *,
    now: float | None = None,
    max_age_seconds: int = 120,
) -> bool:
    """Whether a runtime activity observation is fresh enough to actuate on."""
    if not agent:
        return False
    raw_ts = agent.get('telemetry_ts') or agent.get('telemetryTs')
    if raw_ts in (None, ''):
        return False
    try:
        observed = float(raw_ts)
        if observed > 100_000_000_000:
            observed /= 1000
    except (TypeError, ValueError):
        try:
            from datetime import datetime
            observed = datetime.fromisoformat(
                str(raw_ts).replace('Z', '+00:00')
            ).timestamp()
        except (TypeError, ValueError):
            return False
    if now is None:
        import time
        now = time.time()
    age = float(now) - observed
    return -30 <= age <= max(0, int(max_age_seconds))


def _agent_dict_auto_wakeable(agent: dict | None) -> bool:
    """Whether Hive policy may grant a lease and wake this agent.

    ``stopped``/``disabled`` is an explicit operator kill switch.  Automatic
    scale-to-zero uses the distinct ``hibernated`` state, which remains a valid
    scheduler candidate.  Keep stopped principals governed at Cortex (so a
    stray process cannot bypass the pool), but never put them in the eligible
    set or select them as a holder.
    """
    if not agent or agent.get('archived'):
        return False
    status = str(agent.get('status') or '').strip().lower()
    if status in {'stopped', 'disabled', 'deleting', 'deleted'}:
        return False
    # A deployed-but-unready seat cannot spend a DeepSeek slot (broker 401 /
    # CrashLoop). Hibernated waiters are not stamped False, so they stay
    # GRANT+START-able.
    if agent.get('runtime_ready') is False:
        return False
    return True


def release_leases_for_agent(
    username: str,
    *,
    client: CortexLeaseClient | None = None,
    reason: str = 'agent stopped',
) -> dict:
    """Force-revoke every Cortex model lease held by ``username``.

    Call on stop/disable (event-driven). Safe to call when the agent holds
    nothing: returns ``{'released': [], 'errors': []}``. Never raises.
    """
    uname = str(username or '').strip()
    if not uname:
        return {'released': [], 'errors': [], 'principal': ''}
    principal = principal_for(uname) if not uname.startswith('agent-') else uname
    uname_bare = principal.removeprefix('agent-')
    client = client or CortexLeaseClient()
    released: list[str] = []
    errors: list[str] = []

    # Models to check: pool configs + any remembered holdings for this principal.
    models: set[str] = set()
    for cfg in all_pool_configs():
        mid = str(cfg.get('model_id') or '').strip()
        if mid:
            models.add(mid)
    holdings = cache.get(HOLDINGS_CACHE_KEY) or {}
    if isinstance(holdings, dict):
        for m in holdings.get(principal) or []:
            if m:
                models.add(str(m))
        for m in holdings.get(uname_bare) or []:
            if m:
                models.add(str(m))

    yielded: list[str] = []
    for model in sorted(models):
        # Hibernate/stop must free the Cortex KV *home*, not just the Hive
        # lease. Leftover Redis books kept i7-tp4 full of scout/sara after
        # Hive had already revoked (operator 2026-08-22). yield with
        # release=true DELETES the book; a bench yield still cannot.
        if client.yield_homes(model, principal, reason=reason or 'agent stopped'):
            yielded.append(model)
        board = client.board(model)
        if board is None:
            errors.append(f'{model}: board unreadable')
            continue
        holders = {
            str(h.get('principal') or '')
            for h in (board.get('holders') or [])
            if isinstance(h, dict)
        }
        if not holders and isinstance(board.get('holder'), dict):
            holders = {str((board.get('holder') or {}).get('principal') or '')}
        if principal not in holders and f'agent-{uname_bare}' not in holders and uname_bare not in holders:
            _clear_holding(principal, model)
            continue
        holder = principal if principal in holders else (
            f'agent-{uname_bare}' if f'agent-{uname_bare}' in holders else uname_bare
        )
        if client.revoke(model, holder=str(holder), force=True, reason=reason):
            released.append(model)
            _clear_holding(principal, model)
            _clear_holding(uname_bare, model)
            logger.info(
                'model-lease: released %s from %s on stop (%s)',
                model, principal, reason,
            )
        else:
            errors.append(f'{model}: revoke failed')

    return {
        'released': released,
        'yielded': yielded,
        'errors': errors,
        'principal': principal,
    }


class CortexLeaseClient:
    """Thin client for Cortex's ``/v1/admin/model-leases/`` endpoints.

    Every method returns None / False on failure rather than raising: the lease
    board is an optimization, and a Cortex blip must degrade to "leave the fleet
    alone this tick", never to a reconciler crash that also stops ordinary
    hibernation from running.
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 10.0):
        raw = (base_url or getattr(settings, 'CORTEX_BASE_URL', '') or '').rstrip('/')
        # CORTEX_BASE_URL may already carry the /v1 suffix depending on deployment.
        self.base = raw[:-3].rstrip('/') if raw.endswith('/v1') else raw
        self.api_key = api_key or getattr(settings, 'HIVE_CORTEX_LEASE_API_KEY', '') \
            or getattr(settings, 'CORTEX_API_KEY', '')
        self.timeout = timeout

    def _url(self, model: str, suffix: str = '') -> str:
        return f'{self.base}/v1/admin/model-leases/{model}/{suffix}'

    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}

    def _request(self, method: str, url: str, **kwargs):
        """Retry ClusterIP connection-refused. One dead Cortex replica of two
        (gx10-1 kube-router FW chain reuse, 2026-08-22) 111s half the Hive
        ticks and leaves the fleet unchanged for 10 minutes."""
        import time
        import requests
        last_exc = None
        for attempt in range(3):
            try:
                return requests.request(
                    method, url, headers=self._headers(), timeout=self.timeout,
                    **kwargs,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.15 * (attempt + 1))
                    continue
                raise
        raise last_exc  # pragma: no cover

    def board(self, model: str) -> dict | None:
        try:
            r = self._request('GET', self._url(model))
            if r.status_code != 200:
                logger.warning('model-lease: board GET %s -> %s %s', model, r.status_code, r.text[:160])
                return None
            return r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning('model-lease: board GET %s failed: %s', model, exc)
            return None

    def set_governed(self, model: str, principals) -> bool:
        try:
            r = self._request(
                'PUT', self._url(model, 'governed/'),
                json={'principals': sorted(principals)},
            )
            if r.status_code != 200:
                logger.warning(
                    'model-lease: governed PUT %s -> %s %s',
                    model, r.status_code, (r.text or '')[:160],
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning('model-lease: governed PUT %s failed: %s', model, exc)
            return False

    def set_eligible(self, model: str, principals) -> bool:
        """Hive-worthiness set: only these principals may auto-acquire a free lease."""
        try:
            r = self._request(
                'PUT', self._url(model, 'eligible/'),
                json={'principals': sorted(principals)},
            )
            if r.status_code != 200:
                logger.warning(
                    'model-lease: eligible PUT %s -> %s %s',
                    model, r.status_code, (r.text or '')[:160],
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning('model-lease: eligible PUT %s failed: %s', model, exc)
            return False

    def grant(self, model: str, holder: str, *, min_hold_s: int, max_hold_s: int,
              reason: str, max_holders: int | None = None) -> bool:
        payload = {
            'holder': holder,
            'min_hold_s': min_hold_s,
            'max_hold_s': max_hold_s,
            'reason': reason,
        }
        if max_holders is not None:
            payload['max_holders'] = int(max_holders)
        try:
            r = self._request('POST', self._url(model, 'grant/'), json=payload)
            if r.status_code == 409:
                # Another writer won the race; next tick re-reads the board.
                logger.info('model-lease: grant %s->%s conflicted: %s', model, holder, r.text[:160])
                return False
            if r.status_code != 200:
                logger.warning(
                    'model-lease: grant %s->%s -> %s %s',
                    model, holder, r.status_code, (r.text or '')[:160],
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning('model-lease: grant %s->%s failed: %s', model, holder, exc)
            return False

    def revoke(self, model: str, *, holder: str | None, force: bool, reason: str,
               expected_lease_id: str | None = None) -> bool:
        payload = {'force': force, 'reason': reason}
        if holder:
            payload['holder'] = holder
        if expected_lease_id:
            payload['expected_lease_id'] = str(expected_lease_id)
        try:
            r = self._request('POST', self._url(model, 'revoke/'), json=payload)
            if r.status_code != 200:
                if r.status_code == 409:
                    logger.info(
                        'model-lease: stale revoke %s holder=%s expected=%s: %s',
                        model, holder, expected_lease_id, r.text[:160],
                    )
                return False
            if holder:
                try:
                    return bool((r.json() or {}).get('revoked'))
                except Exception:
                    return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning('model-lease: revoke %s failed: %s', model, exc)
            return False

    def yield_homes(self, model: str, principal: str, *, reason: str = '') -> bool:
        """Hive lifecycle: delete the principal's Cortex KV home.

        Sends ``release=true`` so Cortex actually removes the Redis book
        (TTL-0 is a no-op for Hive principals). Bench callers that omit
        release still cannot steal a running agent's prefix.
        """
        try:
            r = self._request(
                'POST',
                self._url(model, 'yield-homes/'),
                json={'principal': principal, 'reason': reason, 'release': True},
            )
            if r.status_code != 200:
                logger.warning(
                    'model-lease: yield-homes %s %s -> %s %s',
                    model, principal, r.status_code, r.text[:160],
                )
                return False
            try:
                payload = r.json() or {}
            except Exception:
                payload = {}
            try:
                count = int(payload.get('count') or 0)
            except (TypeError, ValueError):
                count = 0
            if count <= 0:
                # HTTP 200 with yielded=[] is a SCAN miss or a already-gone
                # book. Caching that as success left agent-ni's Redis home
                # parked after hibernate (2026-09-01: "yielded 0 homes").
                logger.info(
                    'model-lease: yield-homes %s %s deleted 0 keys — will retry',
                    model, principal,
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning('model-lease: yield-homes %s %s failed: %s', model, principal, exc)
            return False


def _yield_unpinned_residents(*, model: str, agents, client, out: dict) -> None:
    """Delete Cortex KV homes for principals Hive no longer pins on ``model``.

    Bounded by the residents board, not the fleet. Empty-pool ticks and
    converted-off running seats use this so a Metal Redis book cannot outlive
    the Hive model PATCH (Nova 2026-09-18).
    """
    yielder = getattr(client, 'yield_homes', None)
    if not callable(yielder):
        return False
    board = client.board(model)
    if not isinstance(board, dict):
        return False
    res_info = board.get('residency') if isinstance(board.get('residency'), dict) else {}
    resident_principals = {
        str(p).strip() for p in (res_info.get('principals') or []) if str(p).strip()
    }
    if not resident_principals:
        return True
    by_username = {}
    for agent in agents or ():
        uname = str(agent.get('username') or agent.get('agent_username') or '').strip()
        if uname:
            by_username[uname] = agent
    decisions_log = out.setdefault('log', [])
    for principal in sorted(resident_principals):
        uname = _username_from_principal(principal)
        agent = by_username.get(uname) or {}
        pinned = _canonical_pool_model(
            agent.get('effective_model') or agent.get('model') or ''
        )
        if pinned and pinned == _canonical_pool_model(model):
            continue
        ok = yielder(model, principal, reason=f'converted off {model}')
        if ok:
            decisions_log.append(f'YIELD converted-off principal={principal}')
    return True


def _reconcile_one_pool(*, cfg, agents, email_to_teams, workload_by_team, actionable_buckets,
                        client: CortexLeaseClient, now: float, apply: bool,
                        workload_by_email=None, unread_by_email=None,
                        alerts_by_email=None, can_handoff=None) -> dict:
    """One model pool tick — see ``reconcile_model_lease``."""
    out = {
        'decision': None,
        'holder': None,
        'scale': {},
        'log': [],
        'config': cfg,
        'model_id': cfg.get('model_id'),
        'interrupt_ok': set(),
    }
    if not cfg.get('enabled') or not cfg.get('model_id'):
        out['log'].append('model leasing disabled or pool empty')
        return out
    if not cfg.get('members'):
        # Converted-off leftovers: Hive remapped every pin off this model so
        # membership is empty, but Cortex Redis homes can remain (Nova Metal
        # 2026-09-18). An early return skipped leftover yield entirely.
        if apply:
            _yield_unpinned_residents(
                model=cfg['model_id'], agents=agents, client=client, out=out,
            )
        out['log'].append('pool empty; yielded unpinned leftovers')
        return out

    model = cfg['model_id']
    history = cache.get(HISTORY_CACHE_KEY) or {}
    workload_by_email = workload_by_email or {}
    unread_by_email = unread_by_email or {}
    alerts_by_email = alerts_by_email or {}

    by_username = {}
    for agent in agents or ():
        uname = str(agent.get('username') or agent.get('agent_username') or '').strip()
        if uname:
            by_username[uname] = agent

    members: list[PoolMember] = []
    for uname in cfg['members']:
        agent = by_username.get(uname)
        email = str((agent or {}).get('email') or f'{uname}@shizuha.com').lower()
        teams = tuple(sorted(email_to_teams.get(email, set()) or set()))
        members.append(PoolMember(username=uname, email=email, teams=teams,
                                  last_held_at=float(history.get(uname, 0.0) or 0.0)))

    unseated_by_team = unseated_workload_by_team(
        members=members,
        by_username=by_username,
        workload_by_email=workload_by_email,
        email_to_teams=email_to_teams,
        agents=agents,
    )

    wants: set[str] = set()
    personal: set[str] = set()
    assigned: set[str] = set()
    want_reasons: dict[str, str] = {}
    for m in members:
        if not _agent_dict_auto_wakeable(by_username.get(m.username)):
            continue
        agent_row = by_username.get(m.username) or {}
        ok, reason, is_personal = agent_lease_worthiness(
            email=m.email, teams=m.teams,
            workload_by_team=workload_by_team,
            workload_by_email=workload_by_email,
            unread_by_email=unread_by_email,
            alerts_by_email=alerts_by_email,
            actionable_buckets=actionable_buckets,
            heartbeat_outcome=str(agent_row.get('heartbeat_outcome') or ''),
            agent_status=str(agent_row.get('status') or ''),
            unseated_workload_by_team=unseated_by_team,
            activity_phase=str(agent_row.get('activity_phase') or ''),
            activity_phase_for_ms=agent_row.get('activity_phase_for_ms'),
            lease_idle_waste=bool(agent_row.get('lease_idle_waste')),
        )
        if ok:
            wants.add(m.username)
            want_reasons[m.username] = reason
            if is_personal:
                personal.add(m.username)
            if reason == 'assigned_tasks':
                assigned.add(m.username)
    running = {
        m.username for m in members
        if _agent_dict_running(by_username.get(m.username))
    }
    active = {
        m.username for m in members
        if _agent_dict_active(by_username.get(m.username), now=now)
    }

    # An in-progress turn is itself temporary lease worthiness.  Queue/DM
    # signals can drain while a final model call is still running, and Cortex
    # removes a hard-expired holder from the board before Hive reads it.  Fold
    # fresh active+running runtimes into the candidate/eligible set so an
    # expired holder reclaims the newly free warm slot ahead of ordinary
    # waiters.  Once telemetry reaches idle, this synthetic worthiness
    # disappears and the normal handoff rules apply.
    #
    # HIVE-1712 (operator 2026-08-11): the fence is a GRACE, not a lease. A
    # constantly-working non-holder stayed "active" every tick and was never
    # hibernated — 15+ concurrent runtimes against 8 lease slots thrashed
    # every lane's KV (eviction carousel, p50 271s). A non-holder may finish
    # its current work within the grace window; past it, the fence drops and
    # scale_plan hibernates it (its queued work re-wakes it when a slot
    # frees). Holders reset their grace on every tick.
    board = client.board(model)
    if board is None:
        out['log'].append(f'{model}: cortex lease board unreadable; leaving fleet unchanged')
        return out
    current_holder_usernames = {
        _username_from_principal(principal) for principal in _board_holder_principals(board)
    }
    active_continuity = active & running
    try:
        admission_now = int((board or {}).get('admission_capacity') or 0)
    except (TypeError, ValueError):
        admission_now = 0
    # 1-seat listings (Vision-Metal ds4): the 900s active-fence is how two
    # Hive agents stay Running against one engine. GLM keeps the grace so a
    # mid-turn expiry can finish; a 1-admission pool has no spare seat.
    exclusive_one_seat = admission_now == 1
    if exclusive_one_seat:
        dropped = {
            username for username in list(active_continuity)
            if username not in current_holder_usernames
        }
        if dropped:
            active_continuity -= dropped
            out['interrupt_ok'] = set(dropped)
            out['log'].append(
                '1-seat exclusive: drop non-holder active fence for %s'
                % ','.join(sorted(dropped))
            )
    try:
        grace_s = int(getattr(settings, 'HIVE_MODEL_LEASE_ACTIVE_GRACE_S', 900) or 900)
    except (TypeError, ValueError):
        grace_s = 900
    grace_key = 'fleet:model-lease-nonholder-active:' + _canonical_pool_model(model)
    seen = cache.get(grace_key) or {}
    next_seen = {}
    expired_fence = set()
    for username in list(active_continuity):
        if username in current_holder_usernames:
            continue
        first = float(seen.get(username) or now)
        next_seen[username] = first
        if now - first > grace_s:
            expired_fence.add(username)
    if expired_fence:
        active_continuity -= expired_fence
        out['interrupt_ok'] = set(out.get('interrupt_ok') or set()) | expired_fence
        out['log'].append(
            'active-fence grace expired (>%ds): %s no longer lease-worthy by '
            'activity alone' % (grace_s, ','.join(sorted(expired_fence)))
        )
    for username in active_continuity:
        wants.add(username)
        want_reasons[username] = 'active_turn_continuity'

    cortex_leases = board.get('leased') is not False
    if not cortex_leases:
        # Hive has a pool but Cortex CORTEX_LEASED_MODELS does not include this
        # model. PUT governed/grant 400 not_leased_model (live 2026-08-22:
        # Qwen3.8-27B-Q2). The request gate still SERVES the model; only the
        # board write is refused. Keep scaling at most admission holders so
        # documentation/review/QA/security Qwen seats are not hibernated
        # forever behind a silent GRANT failed.
        out['log'].append(
            f'{model}: cortex is not leasing this model; Hive will start at '
            f'most {max(1, int(board.get("admission_capacity") or cfg.get("max_holders") or 1))} '
            'worthy agent(s) without a board grant'
        )

    # Pool membership is the source of truth for who the gate governs.
    wanted_governed = {principal_for(m.username) for m in members}
    if cortex_leases and set(board.get('governed_principals') or []) != wanted_governed:
        if apply and client.set_governed(model, wanted_governed):
            out['log'].append(f'{model}: synced governed set to {sorted(wanted_governed)}')
        elif not apply:
            out['log'].append(f'{model}: [dry-run] would sync governed set to {sorted(wanted_governed)}')

    # Hive-worthiness: only these principals may auto-acquire a free lease on
    # Cortex (idle heartbeats must not steal the sprint). Empty wants → empty
    # eligible (nobody auto-grants until someone is worthy).
    #
    # Keep-resident / CEO Office seats stay running by HOLDING a lease
    # (operator 2026-08-18). They are added to ``wants`` so they count in
    # the N-holder math; a later pass preempts a non-reserved holder if the
    # board is already full. They must not run on shared/unleased capacity.
    from .on_demand import agent_reserved_warm_seat
    reserved_usernames = {
        m.username for m in members
        if agent_reserved_warm_seat(by_username.get(m.username) or {}, m.teams)
        and _agent_dict_auto_wakeable(by_username.get(m.username))
    }
    # HIVE-2841 critical-team floor: holders that ARE the coverage for a met
    # critical team are keep-fenced like keep-resident seats BEFORE the
    # per-holder revoke/keep pass, so an unused floor seat is not dropped and
    # re-seated next tick (musical-chairs guard, same honest-seat doctrine).
    floor_state = critical_team_floor_state(
        members=members, wants=wants,
        holder_usernames=current_holder_usernames,
        policy=critical_team_policy(),
        ready=assigned,
    )
    reserved_usernames |= floor_state['reserved']
    for uname in reserved_usernames:
        wants.add(uname)
        want_reasons.setdefault(uname, 'keep_resident')
        personal.add(uname)
    wanted_eligible = {principal_for(u) for u in wants}
    board_eligible = set(board.get('eligible_principals') or [])
    if cortex_leases and board_eligible != wanted_eligible:
        if apply and client.set_eligible(model, wanted_eligible):
            out['log'].append(
                f'{model}: synced eligible (worthy) set to {sorted(wanted_eligible) or "[]"}'
            )
        elif not apply:
            out['log'].append(
                f'{model}: [dry-run] would sync eligible to {sorted(wanted_eligible) or "[]"}'
            )
    if want_reasons:
        out['log'].append(
            f'{model}: worthiness { {u: want_reasons[u] for u in sorted(want_reasons)} }'
        )

    # CORTEX IS AUTHORITATIVE for how many leases the model can carry: it derives
    # max_holders from LIVE admission capacity (floor(capacity * fraction), with a
    # free-slot floor). Hive's pools config is only a fallback for when the board
    # cannot be read.
    #
    # This path previously used cfg alone while the status path (see
    # pool_status) already preferred the board — so Hive decided with a stale
    # ceiling. Observed 2026-08-04:
    #
    #   Hive   max_holders=4, holders=[kai,kei,nova,ryo,shion]  -> free_slots=0
    #   Cortex max_holders=7, holder_count=5                    -> slots_free=2
    #
    # free_slots = max(0, 4-5) = 0, so the fill loop below never ran and agents
    # WITH actionable work (rei: 7 review, rui: 3 review, fumi: 1 doing) stayed
    # hibernated showing "Waiting behind @kai" while two real slots sat idle.
    max_holders = max(1, int(
        (board or {}).get('max_holders') or cfg.get('max_holders') or 1
    ))
    # Board may expose multi-holder `holders` list (new) or singular `holder` (legacy).
    live_holders_raw = board.get('holders')
    if isinstance(live_holders_raw, list) and live_holders_raw:
        live_holder_dicts = [h for h in live_holders_raw if isinstance(h, dict) and h.get('principal')]
    elif board.get('holder'):
        live_holder_dicts = [board['holder']]
    else:
        live_holder_dicts = []

    # Keep holdings cache aligned with the live board so stop-time release can
    # find the model even if pools config later shrinks.
    for hd in live_holder_dicts:
        _record_holding(str(hd.get('principal') or ''), model)

    remaining_holders: list[dict] = list(live_holder_dicts)
    decisions_log: list[str] = []
    last_decision = None
    preempt_retry = _preempt_retry_usernames()
    board_holder_usernames = {
        str(h.get('principal') or '').removeprefix('agent-')
        for h in live_holder_dicts
        if str(h.get('principal') or '').strip()
    }
    res_info = board.get('residency') if isinstance(board.get('residency'), dict) else {}
    resident_principals = {
        str(p).strip() for p in (res_info.get('principals') or []) if str(p).strip()
    }
    residency_full = bool(res_info.get('full'))

    admission_raw = (board or {}).get('admission_capacity')
    try:
        admission_now = int(admission_raw) if admission_raw is not None else None
    except (TypeError, ValueError):
        admission_now = None
    if admission_now == 0:
        # Explicit zero seats (Qwen after MiniMax H3 took i9-ws). Missing
        # admission_capacity is unknown — do not mass-revoke those boards.
        active = set()
        active_continuity = set()
    if admission_now == 0 and remaining_holders:
        # Qwen3.8-27B-Q2 after MiniMax H3 took i9-ws: Cortex reports
        # admission_capacity=0 but KEEP still held nami because the runtime
        # looked "active". A zero-seat model must not occupy a Hive holder.
        decisions_log.append(
            f'REVOKE all holders: admission_capacity={admission_now}'
        )
        for hd in list(remaining_holders):
            holder = str(hd.get('principal') or '')
            if not holder:
                continue
            if apply:
                ok = client.revoke(
                    model,
                    holder=holder,
                    force=True,
                    reason='model has no serviceable admission capacity',
                )
                if not ok:
                    continue
                _clear_holding(holder, model)
            remaining_holders = [
                h for h in remaining_holders
                if str(h.get('principal') or '') != holder
            ]
            board_holder_usernames.discard(holder.removeprefix('agent-'))
            running = running - {holder.removeprefix('agent-')}
        live_holder_dicts = list(remaining_holders)

    # Principals revoked this tick must not fill the slot they just vacated.
    # Assigned-tasks sorts ahead of last_held_at, so stamping history is not
    # enough: live 2026-09-15 unused-revoked hiro/nami/mio then re-granted
    # them the same tick while jun/kumo waited on team_queue.
    skip_fill: set[str] = set()

    # 1) Revoke/keep each current holder independently.
    for hd in list(live_holder_dicts):
        decision = decide_lease(
            now=now, members=members, wants=wants, current=hd,
            min_hold_seconds=int(cfg['min_hold_seconds']),
            max_hold_seconds=int(cfg['max_hold_seconds']),
            idle_release_seconds=int(cfg['idle_release_seconds']),
            running=running,
            personal=personal,
            assigned=assigned,
            active=active,
            reserved=reserved_usernames,
            preempt_retry=preempt_retry,
            holder_usernames=board_holder_usernames,
            critical_floor=critical_team_policy(),
        )
        last_decision = decision
        # One home = one lease. The 2026-09-02 leftover (nagi kept turning
        # after a revoke that did not yield) is fixed by yield_homes on this
        # revoke path, not by refusing to revoke. Skipping idle/drained
        # revokes because Redis still had a book left 14 silent residencies
        # against 4 in-flight GLM admits (operator 2026-09-06).
        decisions_log.append(
            f'{decision.action.upper()} holder={decision.holder or "-"} reason={decision.reason}'
        )
        if decision.action == 'revoke' and apply:
            # Planning telemetry can age while the command reads Pulse/Connect
            # and applies other controllers.  Ordinary scheduler handoffs need
            # affirmative idle proof immediately before the irreversible board
            # mutation.  Explicit/forced cleanup (stopped or removed member)
            # retains its operator/lifecycle semantics.
            if can_handoff is not None and not decision.force:
                username = str(decision.holder or '').removeprefix('agent-')
                try:
                    allowed, detail = can_handoff(username)
                except Exception as exc:  # noqa: BLE001 — fail closed
                    allowed, detail = False, f'idle confirmation failed: {exc.__class__.__name__}'
                if not allowed:
                    decisions_log.append(
                        f'DEFER REVOKE holder={decision.holder or "-"} reason={detail}'
                    )
                    continue
            ok = client.revoke(
                model,
                holder=(decision.holder if (decision.voluntary or decision.force) else decision.holder),
                force=decision.force,
                reason=decision.reason,
                expected_lease_id=(
                    (str(hd.get('lease_id') or '') or None)
                    if not decision.force else None
                ),
            )
            if ok and decision.holder:
                _clear_holding(str(decision.holder), model)
                remaining_holders = [
                    h for h in remaining_holders
                    if str(h.get('principal') or '') != str(decision.holder)
                ]
                board_holder_usernames.discard(
                    str(decision.holder).removeprefix('agent-')
                )
                yielder = getattr(client, 'yield_homes', None)
                if callable(yielder) and yielder(
                    model, str(decision.holder), reason=decision.reason,
                ):
                    decisions_log.append(
                        f'YIELD homes principal={decision.holder}'
                    )
                # Stamp this tick so fill cannot re-elect the agent we just
                # rotated off. Hive history is otherwise only written on GRANT,
                # and Cortex auto-acquire never updates it — live 2026-08-12
                # revoked idle rei/ryo then immediately re-granted them as
                # least-recently-held of the waiters.
                members, history = _stamp_last_held(
                    members, history, decision.holder, now, persist=True,
                )
                running = running - {str(decision.holder).removeprefix('agent-')}
                skip_fill.add(str(decision.holder))
                skip_fill.add(_username_from_principal(decision.holder))
            # if revoke failed, leave holder in remaining
        elif decision.action == 'revoke' and not apply:
            remaining_holders = [
                h for h in remaining_holders
                if str(h.get('principal') or '') != str(decision.holder)
            ]
            if decision.holder:
                members, history = _stamp_last_held(
                    members, history, decision.holder, now, persist=False,
                )
                running = running - {str(decision.holder).removeprefix('agent-')}
                skip_fill.add(str(decision.holder))
                skip_fill.add(_username_from_principal(decision.holder))

    # 2) Fill free slots up to max_holders from worthy candidates.
    holder_principals = {str(h.get('principal') or '') for h in remaining_holders}
    waiter_count = sum(
        1 for u in wants if principal_for(u) not in holder_principals
    )
    raised = residency_aware_max_holders(
        board,
        waiter_count=waiter_count,
        holder_count=len(holder_principals),
    )
    if raised != max_holders:
        tag = 'RAISE' if raised > max_holders else 'SHRINK'
        decisions_log.append(
            f'{tag} max_holders {max_holders}->{raised} '
            f'(waiters={waiter_count} grantable='
            f'{((board or {}).get("residency") or {}).get("grantable")} '
            f'admission={board.get("admission_capacity")})'
        )
        max_holders = raised
    checked_overflow = set()
    while len(remaining_holders) > max_holders:
        victim = _overflow_revoke_victim(
            [holder for holder in remaining_holders
             if str(holder.get('principal') or '') not in checked_overflow],
            active=active_continuity,
            reserved=reserved_usernames,
            residents=resident_principals,
        )
        if not victim:
            break
        checked_overflow.add(victim)
        if apply:
            observed = next(holder for holder in remaining_holders
                            if str(holder.get('principal') or '') == victim)
            observed_lease_id = observed.get('lease_id')
            if not isinstance(observed_lease_id, str) or not observed_lease_id.strip():
                decisions_log.append(
                    f'DEFER REVOKE holder={victim} reason=hive_admission_cap; observed lease identity unavailable'
                )
                continue
            allowed, detail = False, 'fresh runtime idle confirmation unavailable'
            if can_handoff is not None:
                try:
                    allowed, detail = can_handoff(_username_from_principal(victim))
                except Exception as exc:
                    detail = f'idle confirmation failed: {exc.__class__.__name__}'
            if allowed is not True:
                decisions_log.append(
                    f'DEFER REVOKE holder={victim} reason=hive_admission_cap; {detail}'
                )
                continue
            ok = client.revoke(
                model, holder=victim, force=False,
                reason='hive admission cap 95% of Cortex',
                expected_lease_id=observed_lease_id,
            )
            if not ok:
                decisions_log.append(
                    f'DEFER REVOKE holder={victim} reason=hive_admission_cap; revoke not confirmed'
                )
                continue
            _clear_holding(victim, model)
            yielder = getattr(client, 'yield_homes', None)
            if callable(yielder):
                yielder(model, victim, reason='hive admission cap 95% of Cortex')
        elif _username_from_principal(victim) in active:
            decisions_log.append(
                f'DEFER REVOKE holder={victim} reason=hive_admission_cap; runtime is actively working'
            )
            continue
        decisions_log.append(
            f'REVOKE holder={victim} reason=hive_admission_cap'
        )
        remaining_holders = [
            h for h in remaining_holders
            if str(h.get('principal') or '') != victim
        ]
        board_holder_usernames.discard(victim.removeprefix('agent-'))
        running = running - {victim.removeprefix('agent-')}
        holder_principals.discard(victim)
        skip_fill.add(victim)
        skip_fill.add(_username_from_principal(victim))
    free_slots = max(0, max_holders - len(holder_principals))
    if admission_now == 0:
        free_slots = 0
    res_info = board.get('residency') if isinstance(board.get('residency'), dict) else {}
    resident_principals = {
        str(p).strip() for p in (res_info.get('principals') or []) if str(p).strip()
    }
    residency_full = bool(res_info.get('full'))
    # One admission seat, and the only protected home belongs to someone who
    # is not the holder and not mid-turn. Fill would refuse kv_residency_full,
    # and leftover yield runs only after that refusal — and only once Cortex
    # lists the model. Qwen3.8-27B 2026-09-24: the model was not leased, Reika's
    # idle home stayed protected, fill granted nobody, and both pins were
    # hibernated. Release the blocking home first. Multi-seat pools keep the
    # riku rule and do not delete a living leftover's book every tick.
    if (
        exclusive_one_seat
        and residency_full
        and callable(getattr(client, 'yield_homes', None))
    ):
        for principal in sorted(resident_principals):
            if principal in holder_principals:
                continue
            uname = _username_from_principal(principal)
            if uname in wants:
                continue
            agent = by_username.get(uname) or {}
            if _agent_dict_active(agent, now=now):
                continue
            if apply and not client.yield_homes(
                model, principal, reason='1-seat idle resident blocking fill',
            ):
                continue
            decisions_log.append(
                f'YIELD idle-resident principal={principal}'
            )
            resident_principals.discard(principal)
        if not resident_principals:
            residency_full = False
    # 1.5) Critical-team lease floor (HIVE-2841). Guarantees each configured
    # critical team at least ``min_leases`` holders, fairly across critical
    # teams when supply is short (LRH team order), without starving the rest
    # of the pool beyond that floor: only the deficit seats are reserved, and
    # preemption takes a non-reserved QUIET holder (reserved_preempt_victim
    # excludes reserved + active holders — work never evicts work). Free slots
    # go to unmet critical teams BEFORE the ordinary fairness fill below.
    #
    # Mirror of the keep-resident pass: planning runs even when apply=False so
    # dry-run logs show the floor decision; board mutations only on apply.
    if admission_now != 0:
        # Preemption is only legitimate when the board started this tick FULL:
        # with a free slot the floor takes free seats in fair order and a
        # still-unmet team rotates in on a later tick (LRH) — evicting a
        # productive quiet holder while a free seat exists is never needed.
        floor_may_preempt = len(holder_principals) >= max_holders
        for slug, candidates in floor_state['targets']:
            for uname in candidates:
                principal = principal_for(uname)
                if principal in holder_principals:
                    continue
                if len(holder_principals) >= max_holders:
                    if not floor_may_preempt:
                        decisions_log.append(
                            f'FLOOR GRANT deferred team={slug} '
                            '(board had free slots this tick; rotates in later)'
                        )
                        break
                    victim = reserved_preempt_victim(
                        remaining_holders, reserved_usernames,
                        active=active_continuity,
                    )
                    if victim is None:
                        decisions_log.append(
                            f'FLOOR GRANT blocked team={slug} principal={principal} '
                            '(no preemptable non-reserved quiet holder)'
                        )
                        break
                    if apply:
                        ok = client.revoke(
                            model, holder=victim, force=True,
                            reason='preempt-for-critical-team-floor',
                        )
                        if not ok:
                            decisions_log.append(
                                f'REVOKE failed holder={victim} for floor team={slug}'
                            )
                            continue
                    holder_principals.discard(victim)
                    remaining_holders = [
                        h for h in remaining_holders
                        if str(h.get('principal') or '') != victim
                    ]
                    _mark_preempt_retry(victim, now)
                    preempt_retry.add(_username_from_principal(victim))
                    decisions_log.append(
                        f'REVOKE holder={victim} '
                        f'reason=preempt-for-critical-team-floor team={slug}'
                    )
                floor_reason = f'critical-team-floor team={slug}'
                if apply and cortex_leases:
                    ok = client.grant(
                        model, principal,
                        min_hold_s=int(cfg['min_hold_seconds']),
                        max_hold_s=int(cfg['max_hold_seconds']),
                        reason=floor_reason,
                        max_holders=max_holders,
                    )
                    if not ok:
                        decisions_log.append(f'GRANT failed for floor {principal}')
                        continue
                    history[uname] = now
                    cache.set(HISTORY_CACHE_KEY, history, None)
                    _record_holding(principal, model)
                holder_principals.add(principal)
                remaining_holders.append({'principal': principal})
                # The just-seated candidate IS now the coverage for a met
                # critical team: reserve immediately so a later team's floor
                # pass in this same tick cannot preempt him (musical-chairs
                # guard — otherwise two unmet teams trade one seat forever).
                reserved_usernames.add(uname)
                decisions_log.append(f'GRANT holder={principal} reason={floor_reason}')
        free_slots = max(0, max_holders - len(holder_principals))
    while free_slots > 0:
        # Exclude current holders from wants so decide_lease grants a NEW principal.
        open_wants = {
            u for u in wants
            if principal_for(u) not in holder_principals
            and principal_for(u) not in skip_fill
            and u not in skip_fill
        }
        # One KV home is one lease seat (operator 2026-08-27). Fill leftover
        # lease slots with already-homed principals first. When every home is
        # TTL-protected, do not wake a 9th waiter into residency_full.
        homed_wants = {
            u for u in open_wants if principal_for(u) in resident_principals
        }
        if homed_wants:
            open_wants = homed_wants
        elif residency_full:
            decisions_log.append('FILL none reason=kv_residency_full')
            break
        decision = decide_lease(
            now=now, members=members, wants=open_wants, current=None,
            min_hold_seconds=int(cfg['min_hold_seconds']),
            max_hold_seconds=int(cfg['max_hold_seconds']),
            idle_release_seconds=int(cfg['idle_release_seconds']),
            running=running,
            personal=personal,
            assigned=assigned,
            active=active,
            reserved=reserved_usernames,
            preempt_retry=preempt_retry,
            holder_usernames=board_holder_usernames,
            critical_floor=critical_team_policy(),
        )
        last_decision = decision
        if decision.action != 'grant' or not decision.holder:
            decisions_log.append(f'FILL none reason={decision.reason}')
            break
        decisions_log.append(
            f'GRANT holder={decision.holder} reason={decision.reason}'
        )
        if apply:
            if cortex_leases:
                ok = client.grant(
                    model, decision.holder,
                    min_hold_s=int(cfg['min_hold_seconds']),
                    max_hold_s=int(cfg['max_hold_seconds']),
                    reason=decision.reason,
                    max_holders=max_holders,
                )
                if not ok:
                    decisions_log.append(f'GRANT failed for {decision.holder}')
                    skip_fill.add(str(decision.holder))
                    skip_fill.add(_username_from_principal(decision.holder))
                    continue
            else:
                decisions_log.append(
                    f'GRANT local-only holder={decision.holder} '
                    f'(cortex is not leasing {model})'
                )
            history[decision.holder.removeprefix('agent-')] = now
            cache.set(HISTORY_CACHE_KEY, history, None)
            _record_holding(decision.holder, model)
            _clear_preempt_retry(decision.holder)
            preempt_retry.discard(_username_from_principal(decision.holder))
        holder_principals.add(str(decision.holder))
        remaining_holders.append({'principal': decision.holder})
        free_slots = max(0, max_holders - len(holder_principals))

    # The board is shared with Cortex's request-path auto-acquire.  Never drive
    # lifecycle from our speculative read/modify snapshot: another request may
    # have won a grant race, or a revoke may have become a no-op.  Re-read after
    # every apply batch and use that board as the scale source.  If the final
    # read is unavailable, fail closed for hibernation by retaining every
    # currently running pool member for this tick.
    live_holder_rows: list[dict] = list(remaining_holders)
    if apply and cortex_leases:
        final_board = client.board(model)
        if final_board is None:
            decisions_log.append(
                'FINAL BOARD unreadable; preserving all currently running members'
            )
            holder_principals.update(principal_for(u) for u in running)
        else:
            final_raw = final_board.get('holders')
            if isinstance(final_raw, list) and final_raw:
                final_holders = [
                    h for h in final_raw
                    if isinstance(h, dict) and h.get('principal')
                ]
            elif final_board.get('holder'):
                final_holders = [final_board['holder']]
            else:
                final_holders = []
            holder_principals = {
                str(h.get('principal') or '') for h in final_holders
            }
            live_holder_rows = final_holders
            for hd in final_holders:
                _record_holding(str(hd.get('principal') or ''), model)

    # Reserved seats occupy a real holder slot. If the board is full, preempt
    # a non-reserved quiet holder rather than leaving Yuna running unleased.
    grant_fn = getattr(client, 'grant', None)
    revoke_fn = getattr(client, 'revoke', None)
    for uname in sorted(reserved_usernames):
        principal = principal_for(uname)
        if principal in holder_principals:
            continue
        if len(holder_principals) > max_holders:
            decisions_log.append(
                f'RESERVED GRANT blocked principal={principal} '
                '(capacity trim awaiting safe handoff)'
            )
            continue
        if len(holder_principals) >= max_holders:
            victim = reserved_preempt_victim(
                live_holder_rows, reserved_usernames, active=active_continuity,
            )
            if victim is None:
                decisions_log.append(
                    f'RESERVED GRANT blocked principal={principal} '
                    f'(no preemptable non-reserved holder)'
                )
                continue
            if apply and callable(revoke_fn):
                ok = revoke_fn(
                    model, holder=victim, force=True,
                    reason='preempt-for-keep-resident',
                )
                if not ok:
                    decisions_log.append(
                        f'REVOKE failed holder={victim} for reserved {principal}'
                    )
                    continue
            holder_principals.discard(victim)
            live_holder_rows = [
                h for h in live_holder_rows
                if str(h.get('principal') or '') != victim
            ]
            _mark_preempt_retry(victim, now)
            preempt_retry.add(_username_from_principal(victim))
            decisions_log.append(
                f'REVOKE holder={victim} reason=preempt-for-keep-resident'
            )
        if apply and cortex_leases and callable(grant_fn):
            ok = grant_fn(
                model, principal,
                min_hold_s=int(cfg['min_hold_seconds']),
                max_hold_s=int(cfg['max_hold_seconds']),
                reason='keep-resident renew',
                max_holders=max_holders,
            )
            if not ok:
                decisions_log.append(f'GRANT failed for reserved {principal}')
                continue
            history[uname] = now
            cache.set(HISTORY_CACHE_KEY, history, None)
            _record_holding(principal, model)
        holder_principals.add(principal)
        live_holder_rows.append({'principal': principal})
        decisions_log.append(f'GRANT holder={principal} reason=keep-resident')

    # One KV home is one lease seat (operator 2026-08-27 / 2026-09-01).
    # Hibernate/stop leftovers must DELETE the Redis book. Running idle
    # non-holders must too: lex/lina/hana/banto kept 30m GLM homes after
    # Hive deferred hibernate on missing telemetry, 21 protected homes
    # filled 20 seats, and shion 503'd residency_full. In-flight
    # (``_agent_dict_active``) is the only leftover that keeps the book.
    # Event path is release_leases_for_agent; this is the reconcile backstop.

    final_holder_usernames = {_username_from_principal(principal) for principal in holder_principals}
    cache.set(grace_key, {username: first for username, first in next_seen.items()
                          if username not in final_holder_usernames}, timeout=6 * 3600)
    out['interrupt_ok'].difference_update(final_holder_usernames)
    out['decision'] = last_decision
    out['holder'] = next(iter(sorted(holder_principals)), None)
    out['holders'] = sorted(holder_principals)
    # Homeless waiters cannot finish a turn when every KV seat is
    # TTL-protected (operator 2026-08-27). The 15m active-grace otherwise
    # keeps haru/ren running while they hammer residency_full.
    if residency_full:
        dropped = {
            uname for uname in active_continuity
            if principal_for(uname) not in holder_principals
            and principal_for(uname) not in resident_principals
        }
        if dropped:
            active_continuity -= dropped
            for uname in sorted(dropped):
                decisions_log.append(
                    f'SCALE off principal={principal_for(uname)} '
                    f'reason=kv_residency_full'
                )
    out['scale'] = scale_plan(
        members=members,
        holders=holder_principals,
        active=active_continuity,
    )
    yielder = getattr(client, 'yield_homes', None)
    if apply and cortex_leases and callable(yielder):
        starting = {
            str(u) for u, on in (out.get('scale') or {}).items() if on
        }
        holder_usernames = {
            str(p).removeprefix('agent-') for p in holder_principals if p
        }
        recent = cache.get(LEFTOVER_HOME_YIELD_CACHE_KEY) or {}
        if not isinstance(recent, dict):
            recent = {}
        dirty = False
        leftover_usernames = []
        seen_leftover = set()
        for m in members:
            if m.username not in seen_leftover:
                leftover_usernames.append(m.username)
                seen_leftover.add(m.username)
        # Residents board is the leftover set. Scanning every FleetAgent on
        # every pool POSTed yield-homes for ~70 GLM seats against
        # Vision-Metal (no KV homes) and stretched the 10-min reconcile to
        # 8 minutes — the Metal holder was only scaled up at the end of the
        # job (2026-09-09). Keep the full-fleet hibernated scan only when
        # the board is actually full (SCAN-miss backstop).
        for principal in resident_principals:
            uname = _username_from_principal(principal)
            if uname and uname not in seen_leftover:
                leftover_usernames.append(uname)
                seen_leftover.add(uname)
        if residency_full:
            for uname in by_username:
                if uname not in seen_leftover:
                    leftover_usernames.append(uname)
                    seen_leftover.add(uname)
        for uname in leftover_usernames:
            agent = by_username.get(uname) or {}
            status = str(agent.get('status') or '').strip().lower()
            hibernated = status in {'hibernated', 'stopped', 'disabled'}
            pinned = _canonical_pool_model(
                agent.get('effective_model') or agent.get('model') or ''
            )
            converted_off = bool(pinned) and pinned != _canonical_pool_model(model)
            if not hibernated and not converted_off:
                if uname in starting or uname in holder_usernames:
                    continue
                if principal_for(uname) in holder_principals:
                    continue
            # Hibernate/stop must always delete the Redis book even if the
            # Deployment is still 1/1 (reika 2026-09-02: Hive hibernated,
            # k8s replicas=1, 30m home, ichi 503 residency_full).
            # Keep the book only for a living *running* leftover mid-turn
            # ON THIS MODEL. A running GLM agent converted off Vision-Metal
            # must still drop the Metal home (Nova 2026-09-18).
            if status not in {'hibernated', 'stopped', 'disabled'} and not converted_off:
                if _agent_dict_active(agent, now=now) or _agent_dict_running(agent):
                    continue
            principal = principal_for(uname)
            try:
                last = float(recent.get(principal) or 0)
            except (TypeError, ValueError):
                last = 0.0
            # A 1h no-book cache used to skip the next tick even after the
            # agent re-homed (kumo/ichi/jun 2026-09-16: hibernate yield-0
            # while still 1/1, next heartbeat minted a new Redis book, KV
            # Residents kept the 30m home). Anyone currently on the
            # residents board must be retried until count>0.
            if (
                last
                and (now - last) < LEFTOVER_HOME_YIELD_TTL_S
                and not residency_full
                and principal not in resident_principals
            ):
                continue
            status = str(agent.get('status') or '').strip().lower() or 'not-running'
            ok = yielder(model, principal, reason=f'{status} leftover home')
            if ok:
                decisions_log.append(f'YIELD leftover principal={principal}')
                recent[principal] = now
                dirty = True
            elif (
                principal not in resident_principals
                and not residency_full
            ):
                # HTTP 200 count=0 is "no book", not a SCAN miss, when this
                # principal is absent from the residents board. Caching that
                # used to be forbidden globally (ni 2026-09-01); retrying it
                # for the whole fleet every tick is the Vision-Metal storm.
                recent[principal] = now
                dirty = True
        if dirty:
            cache.set(LEFTOVER_HOME_YIELD_CACHE_KEY, recent, None)
    out['log'].append(
        f'{model}: max_holders={max_holders} holders={sorted(holder_principals) or "-"} '
        f'wants={sorted(wants) or "-"} running={sorted(running) or "-"} '
        f'active={sorted(active) or "-"} decisions=[{"; ".join(decisions_log) or "-"}]'
    )
    return out


def persist_retired_fleet_model_remaps() -> list[str]:
    """Rewrite FleetAgent rows still pinned to a retired serving id.

    Event path is Hive PATCH. This is the lease-tick backstop so a bulk SQL
    conversion or a Metal row nobody PATCHed still remaps at persist (which
    is what ``FleetAgent.save`` already does) and the next PATCH-shaped
    converge can fire.
    """
    from .runtime_lane import remap_retired_fleet_model

    changed: list[str] = []
    try:
        from .models import FleetAgent
        rows = FleetAgent.objects.exclude(
            status__in=(FleetAgent.Status.DELETING, FleetAgent.Status.DELETED),
        ).only('id', 'agent_username', 'model', 'status')
        for row in rows.iterator():
            new = remap_retired_fleet_model(row.model)
            if new == (row.model or ''):
                continue
            old = row.model
            row.model = new
            row.save(update_fields=['model', 'updated_at'])
            changed.append(str(row.agent_username or ''))
            try:
                from .lifecycle_fleet import apply_k3s_lane_after_patch
                apply_k3s_lane_after_patch(row, previous_model=old)
            except Exception:  # noqa: BLE001
                logger.exception(
                    'retired-model remap: k3s converge failed for %s',
                    row.agent_username,
                )
    except Exception:  # noqa: BLE001 — SimpleTestCase / DB outage
        return changed
    if changed:
        logger.info('retired-model remap: %s', ','.join(changed))
    return changed


def drain_retired_model_homes(*, client, agents, out: dict) -> None:
    """Yield leftover KV homes on serving ids Hive has retired.

    ``all_pool_configs`` no longer overlays Vision-Metal once nobody pins it,
    so the Metal pool tick never ran leftover yield. Drain the retired Cortex
    ids directly; bounded by residents-board size.
    """
    from .runtime_lane import RETIRED_CORTEX_SERVING_IDS

    seen = {
        _canonical_pool_model(one.get('model_id') or (one.get('config') or {}).get('model_id'))
        for one in (out.get('pools') or [])
        if isinstance(one, dict)
    }
    for model_id in RETIRED_CORTEX_SERVING_IDS:
        if _canonical_pool_model(model_id) in seen:
            continue
        try:
            reached = _yield_unpinned_residents(
                model=model_id, agents=agents, client=client, out=out,
            )
        except Exception:  # noqa: BLE001
            logger.exception('retired-model drain failed for %s', model_id)
            return
        if reached is False:
            return


def reconcile_model_lease(*, agents, email_to_teams, workload_by_team, actionable_buckets,
                          client: CortexLeaseClient | None = None, now: float | None = None,
                          apply: bool = False,
                          workload_by_email=None, unread_by_email=None,
                          alerts_by_email=None, can_handoff=None) -> dict:
    """Run one lease tick across ALL configured model pools.

    Returns ``{'decision', 'holder', 'scale', 'log', 'pools': [...]}``.
    ``scale`` merges all pools (username → should_run); an agent is kept running
    if ANY pool wants them as holder.

    Worthiness inputs (Hive is SoT for "who deserves a sprint"):
      - workload_by_email — per-agent Pulse actionable assignments
      - unread_by_email — Connect undelivered DMs
      - alerts_by_email — assigned Pulse alerts
      - workload_by_team — team queue (anti-starvation wake)
    """
    import time as _time

    now = now if now is not None else _time.time()
    client = client or CortexLeaseClient()
    persist_retired_fleet_model_remaps()
    configured_pools = all_pool_configs()
    pools = pool_configs_for_agents(configured_pools, agents)
    out = {
        'decision': None,
        'holder': None,
        'scale': {},
        'log': [],
        'config': None,
        'pools': [],
        'interrupt_ok': set(),
    }
    if not pools:
        out['log'].append('model leasing disabled or pool empty')
        return out

    for configured, resolved in zip(configured_pools, pools):
        before = sorted(configured.get('members') or ())
        after = sorted(resolved.get('members') or ())
        if before != after:
            out['log'].append(
                f"{resolved.get('model_id')}: derived pool membership from Hive model SoT "
                f"(was={before}, now={after})"
            )

    merged_scale: dict[str, bool] = {}
    for cfg in pools:
        one = _reconcile_one_pool(
            cfg=cfg, agents=agents, email_to_teams=email_to_teams,
            workload_by_team=workload_by_team, actionable_buckets=actionable_buckets,
            client=client, now=now, apply=apply,
            workload_by_email=workload_by_email,
            unread_by_email=unread_by_email,
            alerts_by_email=alerts_by_email,
            can_handoff=can_handoff,
        )
        out['pools'].append(one)
        out['log'].extend(one.get('log') or [])
        # Keep last decision for backward-compat log readers
        out['decision'] = one.get('decision')
        out['holder'] = one.get('holder')
        out['config'] = cfg
        for uname, should_run in (one.get('scale') or {}).items():
            # OR across pools: run if any model lease needs this agent up
            merged_scale[uname] = bool(merged_scale.get(uname)) or bool(should_run)
        out['interrupt_ok'].update(one.get('interrupt_ok') or ())
    out['scale'] = merged_scale
    if apply:
        drain_retired_model_homes(client=client, agents=agents, out=out)
    return out


def reconcile_ghost_lease_holders(*, client: CortexLeaseClient | None = None) -> dict:
    """Periodic reconciliation: dormant seats must never hold leases or KV homes.

    Operator 2026-09-22: hiro (start-deferred, not even running) and goro
    (hibernated) held protected residency on GLM backends — each wasted
    admission slot and parked KV. The event-driven release
    (``release_leases_for_agent`` on stop/disable) can be missed by paths that
    predate it, by start-deferred seats that never started, or when Cortex
    re-protects residency after the release. This pass re-checks the board for
    every dormant seat and revokes ghosts. Idempotent; safe to run often.
    """
    client = client or CortexLeaseClient()
    from fleet.models import FleetAgent
    from .management.commands.reconcile_on_demand_agents import observed_native_replicas

    # Ground truth is the OBSERVED deployment (replicas==0 means dormant),
    # NOT FleetAgent.status — hiro's row said 'running' while its deployment
    # was scaled to zero and start-deferred (operator 2026-09-22). DB status
    # drifts; the cluster does not.
    dormant = set()
    for uname in FleetAgent.objects.exclude(
            status__in=['stopped', 'deleted', 'archived'],
    ).values_list('agent_username', flat=True):
        if not uname:
            continue
        replicas = observed_native_replicas(uname)
        if replicas is None or replicas <= 0:
            dormant.add(uname)
    if not dormant:
        return {'checked': 0, 'released': []}

    released: list[str] = []
    checked = 0
    for cfg in all_pool_configs():
        model = str(cfg.get('model_id') or '').strip()
        if not model:
            continue
        board = client.board(model)
        if not isinstance(board, dict):
            continue
        checked += 1
        ghosts: set[str] = set()
        for h in (board.get('holders') or []):
            principal = str((h or {}).get('principal') or '')
            if principal.startswith('agent-') and principal.removeprefix('agent-') in dormant:
                ghosts.add(principal)
        residency = board.get('residency') or {}
        for p in (residency.get('principals') or []):
            sp = str(p or '').split()
            if sp and sp[0].startswith('agent-') and sp[0].removeprefix('agent-') in dormant:
                ghosts.add(sp[0])
        for principal in sorted(ghosts):
            result = release_leases_for_agent(
                principal, client=client,
                reason='ghost reconciliation — seat dormant but still holding lease/home',
            )
            got = result.get('released') or []
            if got:
                released.append(f'{principal}: {got}')
    return {'checked': checked, 'released': released}
