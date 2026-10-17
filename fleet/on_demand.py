"""On-demand fleet lifecycle policy.

Hive is the source of truth for agent lifecycle. This module is intentionally
pure: it decides which rt-fleet agents should start/stop from a Pulse roster and
workload snapshot, and leaves the caller to apply those actions through the
normal runtime-fleet control API.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable


TEAM_SCOPE_SEPARATOR = '::'

TEAM_ALIASES = {
    # Pulse's Admin team is displayed as Research, while the canonical routing
    # roster is research-analytics. Keep workload and membership keys identical.
    'research': 'research-analytics',
}


ACTIVE_BUCKETS = (
    'open',
    'todo',
    'doing',
    'in_progress',
    'review',
    'in_review',
    'blocked',
    'scheduled',
    'backlog',
)

# HIVE-651 (operator 2026-07-11): the keep-warm / start decision must count only
# ACTIONABLE work — a `blocked` task is waiting on a blocker, `scheduled` is
# future-due, and `backlog` is explicitly not-active-WIP. None are actionable
# NOW, so none should hold an agent warm or wake it. An agent whose whole queue
# is blocked/scheduled/backlog has nothing to do → hibernate it; the 10-min sweep
# re-starts it the moment a blocker clears / a scheduled task comes due (the
# status flips into an actionable bucket) or an unread DM arrives (connect-wake).
ACTIONABLE_BUCKETS = (
    'open',
    'todo',
    'doing',
    'in_progress',
    'review',
    'in_review',
    # Unstarted triage is work a teammate can pick up now. Omitting it left
    # Engineering DeepSeek seats idle while Pulse showed a pile of
    # needs_triage with nobody leased (2026-08-22).
    'needs_triage',
)

# These outcomes prove that the latest heartbeat turn did not perform runnable
# work. Heartbeat execution itself updates last_active_at, so treating that
# timestamp as generic activity grace lets an idle agent keep itself resident
# forever: every queue_empty/all_blocked turn renews the five-minute grace
# before the ten-minute lifecycle reconcile runs.
#
# not_observed: queue-blind heartbeats (no Pulse snapshot) with no progress.
# needs_help is handled separately: it is non-productive only when ready=0
# (empty-queue / no-snapshot storms). needs_help with ready>0 means stuck
# real work — keep the seat warm so the agent can still be helped/worked.
NON_PRODUCTIVE_HEARTBEAT_OUTCOMES = frozenset({
    'queue_empty',
    'all_blocked',
    'future_due',
    'not_observed',
})

DEFAULT_WARM_TEAMS = frozenset({
    'admin-ops',
    'architecture',
    'engineering',
    'merge',
    'devops',
    'qa',
    'review',
})

# Operator 2026-08-15 / 2026-08-17: CEO Office is a live conversation /
# home-voice team. Interaction latency is the product. This is NOT the
# 2026-07-13 static "warm teams" list (review, engineering, …) that kept
# idle compute burning. Only teams whose charter is instant human
# conversation belong here — and Hive must NEVER hibernate a member of
# this set (idle, rate-limited, or catalog-unserviceable). Operator
# disable (status=stopped) still wins.
# Operator 2026-08-21: CEO Office is voice-to-voice only (live: Hina + Ena
# on grok-voice-think-fast-2.0). A DeepSeek/text keep-resident on this
# team is a leased-slot leak (Yuna idle 43h). Put text seats elsewhere.
# Operator 2026-09-22: ceo-office REMOVED from keep-resident. Idle voice
# seats (hina idle-ticking every 30m with zero work) hold Cortex leases and
# KV residency for nothing, and poisoned provider badges paint them
# Unavailable. Hive may now hibernate ceo-office members under the normal
# on-demand rules. Compensating guarantee: the wake path must be fast — a
# connect voice-call wake is priority-handled (preempt-capable), and the
# eternal session on the workspace PVC means a wake resumes with full
# context instead of a cold start. Operator disable (status=stopped) still
# wins.
KEEP_RESIDENT_TEAM_SLUGS = frozenset()


DEFAULT_ON_DEMAND_TEAMS = frozenset({
    'accounting',
    'customer-support',
    'design',
    'documentation',
    'governance',
    'human-ops',
    'legal',
    'marketing',
    'people',
    'product',
    'program-management',
    'research-analytics',
    'sales',
    'sec-ops',
    'security',
    'social-media',
    'trading-engineering',
    # Org-scoped business teams (operator 2026-07-11): accounting/quant/trading
    # work is periodic + heavily scheduled/blocked — hibernate when no
    # actionable work, wake when a scheduled task comes due or a DM arrives.
    'digital-accounting',
    'personal-accounting',
    'digital-quant',
    'securities-engineering',
})


@dataclass(frozen=True)
class OnDemandAction:
    action: str
    agent_id: str
    username: str
    reason: str
    teams: tuple[str, ...] = ()
    # HIVE-1712: lease planner already spent the 900s active-turn grace and
    # still said stop. The apply-time idle fence must not re-extend that
    # grace forever for a non-holder that never goes idle.
    interrupt_ok: bool = False


def canon_team(value: str | None) -> str:
    normalized = str(value or '').strip().lower().replace('_', '-').replace(' ', '-')
    if TEAM_SCOPE_SEPARATOR in normalized:
        organization, team = normalized.rsplit(TEAM_SCOPE_SEPARATOR, 1)
        return f'{organization}{TEAM_SCOPE_SEPARATOR}{TEAM_ALIASES.get(team, team)}'
    return TEAM_ALIASES.get(normalized, normalized)


def canon_organization(value: str | None) -> str:
    return str(value or '').strip().lower().replace('_', '-').replace(' ', '-')


def scoped_team_key(team: str | None, organization: str | None = None) -> str:
    """Return the stable organization-qualified identity for a Pulse team."""
    team_slug = canon_team(team)
    if not team_slug:
        return ''
    organization_slug = canon_organization(organization)
    return (
        f'{organization_slug}{TEAM_SCOPE_SEPARATOR}{team_slug}'
        if organization_slug
        else team_slug
    )


def roster_team_key(roster: dict[str, Any]) -> str:
    return scoped_team_key(
        roster.get('team_name')
        or roster.get('slug')
        or roster.get('team_slug')
        or roster.get('id')
        or roster.get('name'),
        roster.get('organization_slug')
        or roster.get('org_slug')
        or roster.get('organization'),
    )


def team_slug_from_key(value: str | None) -> str:
    """Return the unqualified slug when a caller only needs team policy."""
    return canon_team(str(value or '').rsplit(TEAM_SCOPE_SEPARATOR, 1)[-1])


def canon_email(value: str | None) -> str:
    return str(value or '').strip().lower()


def agent_username(agent: dict[str, Any]) -> str:
    return str(agent.get('agent_username') or agent.get('username') or '').strip().lower()


def agent_email(agent: dict[str, Any]) -> str:
    email = canon_email(agent.get('email'))
    if email:
        return email
    username = agent_username(agent)
    return f'{username}@shizuha.com' if username else ''


def lifecycle_agent_emails(agent: dict[str, Any]) -> set[str]:
    """Emails Connect-wake may use to find this seat.

    Native org agents are projected as ``user@shizuha.com``. Personal Shizuha
    identities (and Connect participant rows) use ``user@agents.shizuha.io``.
    Matching only one domain made first-DM wake return unknown_agent.
    """
    emails: set[str] = set()
    primary = agent_email(agent)
    if primary:
        emails.add(primary)
    username = agent_username(agent)
    if username:
        emails.add(f'{username}@shizuha.com')
        emails.add(f'{username}@agents.shizuha.io')
    extra = agent.get('emails') or agent.get('email_aliases') or ()
    if isinstance(extra, (list, tuple, set)):
        emails.update(canon_email(item) for item in extra)
    return {canon_email(item) for item in emails if item}


def agent_enabled(agent: dict[str, Any]) -> bool:
    return bool(agent.get('enabled'))


def agent_running(agent: dict[str, Any]) -> bool:
    return agent_enabled(agent) and str(agent.get('status') or '').lower() in {
        'running',
        'starting',
        'provisioning',
        'pending',
    }


# PLAT-5154 / PLAT-5265: process-running is NOT the same as able to do work.
#
# An agent can be enabled and status=running while its provider is dead — no
# model token, so it cannot take a turn. Counting such a seat as capacity is
# what starved Security twice: Ren was process-running with
# provider_unavailable=true, so the healthy STOPPED fallback (Akira) never
# woke, and two items sat unrouted (oldest MAIL-62 at 400h).
#
# These are the same signals the UI already renders unavailable rather than
# green — `_apply_provider_unavailable_health` sets availability_status
# 'unavailable' on exactly this condition.
_DEMAND_BLOCKING_AVAILABILITY = {'unavailable', 'offline', 'inactive'}

# Bridge recent_errors / messages that mean "this seat cannot take a model turn".
# Hibernating these agents is rest-until-workable (same roster member wakes after
# the signal ages out) — not permanent culling.
_RATE_LIMIT_HINT = re.compile(
    r'rate[_\s-]?limit|usage limit|hit your usage|quota.?exhaust|too many requests',
    re.IGNORECASE,
)
# Codex/ChatGPT (and similar) refuse a pinned model on every turn. That is not
# a blip: the seat cannot absorb WIP, so it is the same class as a dead
# provider — hibernate and let Pulse drain to a working peer.
_MODEL_UNSUPPORTED_HINT = re.compile(
    r'model is not supported|unsupported model|not supported when using Codex'
    r'|model_not_found|does not support .{0,40}model',
    re.IGNORECASE,
)
_ENOSPC_HINT = re.compile(
    r'ENOSPC|SQLITE_IOERR_SHMSIZE|no space left on device',
    re.IGNORECASE,
)
# How long a rate/usage-limit error keeps an agent non-capacity / unstartable.
# Long enough to cover multi-hour Codex caps; short enough that agents re-try
# after recovery without a manual wake.
DEFAULT_RATE_LIMIT_COOLDOWN_MINUTES = 6 * 60


def _error_message(err: Any) -> str:
    if isinstance(err, dict):
        return str(err.get('msg') or err.get('message') or err.get('error') or '')
    return str(err or '')


def _error_ts_ms(err: Any) -> int | None:
    if not isinstance(err, dict):
        return None
    for key in ('ts', 'timestamp', 'at', 'time'):
        raw = err.get(key)
        if raw is None:
            continue
        try:
            num = float(raw)
        except (TypeError, ValueError):
            continue
        # Accept seconds or milliseconds.
        return int(num if num > 1_000_000_000_000 else num * 1000)
    return None


def agent_rate_limited(
    agent: dict[str, Any],
    *,
    max_age_minutes: int = DEFAULT_RATE_LIMIT_COOLDOWN_MINUTES,
    now: datetime | None = None,
) -> bool:
    """True when recent telemetry shows a provider rate/usage limit for this seat.

    A rate-limited agent cannot absorb work: do not count it as team capacity,
    do not keep it warm burning retries, and do not START it until the signal
    ages out (then the normal personal-work / team-demand wake path re-tries).
    """
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    max_age_ms = max(0, int(max_age_minutes)) * 60 * 1000

    reason = str(
        agent.get('provider_unavailable_reason')
        or agent.get('providerUnavailableReason')
        or ''
    )
    if reason and _RATE_LIMIT_HINT.search(reason):
        return True

    for err in agent.get('recent_errors') or []:
        msg = _error_message(err)
        category = ''
        if isinstance(err, dict):
            category = str(err.get('category') or '')
        if not (_RATE_LIMIT_HINT.search(msg) or _RATE_LIMIT_HINT.search(category)):
            continue
        ts = _error_ts_ms(err)
        if ts is None:
            # Undated rate-limit on a still-running agent: honor it (live poison).
            if agent_running(agent):
                return True
            continue
        age = now_ms - ts
        if 0 <= age <= max_age_ms:
            return True
    return False


_CANNOT_TURN_HIBERNATE_REASON = 'runtime cannot take a model turn'


def _still_on_poisoned_codex_lane(agent: dict[str, Any]) -> bool:
    """True when this seat is still the Codex/ChatGPT lane that cannot turn.

    A later Hive model switch to SCLI/Cortex must be allowed to start; the
    hibernate reason and leftover error rows otherwise pin the seat forever.
    """
    method = str(agent.get('execution_method') or '').strip().lower()
    model = str(
        agent.get('effective_model') or agent.get('model') or ''
    ).strip().lower()
    return method in {'codex_app_server', 'codex'} or model.startswith('gpt-')


def agent_runtime_cannot_turn(agent: dict[str, Any]) -> bool:
    """True when this running seat cannot execute a model turn.

    Operator 2026-08-22: Jun stayed Running on Codex/gpt-5.6-sol with 8 doing
    while every heartbeat failed ``model is not supported when using Codex
    with a ChatGPT account``. Personal WIP then blocked on-demand stop, so
    Pulse never drained Engineering onto DeepSeek peers. A crashlooping
    native pod (Sara ENOSPC) is the same shape: process-running, zero turns.

    The hibernate path used to look only at *running* telemetry. Ten minutes
    later the same seat was hibernated, undated Codex errors were ignored,
    ``eligible_to_start`` became true, and assigned-work START woke aoi/ichi/
    mika/ni/zen/shizuha back into the poison (live 2026-08-22 08:40). WIP
    stayed seated on oscillating Codex, so DeepSeek FILL had nobody waiting.
    """
    if agent.get('runtime_ready') is False:
        return True
    reason = str(
        agent.get('provider_unavailable_reason')
        or agent.get('providerUnavailableReason')
        or ''
    )
    if _MODEL_UNSUPPORTED_HINT.search(reason) or _ENOSPC_HINT.search(reason):
        return True
    hib_reason = str(agent.get('hibernation_reason') or '')
    if _CANNOT_TURN_HIBERNATE_REASON in hib_reason.lower():
        if _still_on_poisoned_codex_lane(agent) or agent.get('runtime_ready') is False:
            return True
    try:
        if int(agent.get('health_consecutive_errors') or 0) >= 3 and agent_running(agent):
            return True
    except (TypeError, ValueError):
        pass
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    max_age_ms = 15 * 60 * 1000
    for err in agent.get('recent_errors') or []:
        msg = _error_message(err)
        if not (_MODEL_UNSUPPORTED_HINT.search(msg) or _ENOSPC_HINT.search(msg)):
            continue
        ts = _error_ts_ms(err)
        if ts is None:
            # Undated poison is live on a running seat. On a hibernated Codex
            # seat it is also live — ignoring it is the START/STOP flap above.
            # A model-switch off Codex must still be allowed to start.
            if agent_running(agent) or _still_on_poisoned_codex_lane(agent):
                return True
            continue
        if 0 <= now_ms - ts <= max_age_ms:
            return True
    return False


def agent_eligible_to_start(agent: dict[str, Any]) -> bool:
    """True when policy may wake this zero-scaled agent.

    Hibernation is a scheduler state; explicit stop/disable is an operator kill
    switch and must survive task, DM, and lease demand.
    """
    if str(agent.get('status') or '').strip().lower() in {'stopped', 'disabled'}:
        return False
    # Use the same explicit health gates as the capacity predicate. A
    # hibernated provider-dead seat cannot satisfy the demand that wakes it;
    # starting it only creates a START/STOP oscillation on consecutive
    # reconciles. Fresh telemetry naturally makes it eligible again when the
    # poison clears or ages out upstream.
    if agent.get('provider_unavailable') is True:
        return False
    if agent.get('health_token_pool_unavailable') is True:
        return False
    if agent_rate_limited(agent):
        return False
    if agent_runtime_cannot_turn(agent):
        return False
    availability = str(agent.get('availability_status') or '').strip().lower()
    if availability in _DEMAND_BLOCKING_AVAILABILITY:
        return False
    return True


def agent_satisfies_demand(agent: dict[str, Any]) -> bool:
    """True when a running agent can actually absorb demand.

    Deliberately compatible when health is ABSENT: only an EXPLICIT unhealthy
    signal disqualifies a seat. An agent with no telemetry yet still counts, as
    it did before — otherwise a fresh or telemetry-less agent would be treated
    as dead and the reconciler would over-start seats.

    Rate/usage-limited seats are not capacity: leaving them warm burns retries
    and blocks waking a healthy peer (same class as provider_unavailable).
    """
    if not agent_running(agent):
        return False
    if agent.get('provider_unavailable') is True:
        return False
    if agent.get('health_token_pool_unavailable') is True:
        return False
    if agent_rate_limited(agent):
        return False
    if agent_runtime_cannot_turn(agent):
        return False
    availability = str(agent.get('availability_status') or '').strip().lower()
    if availability in _DEMAND_BLOCKING_AVAILABILITY:
        return False
    return True


def _parse_timestamp(timestamp_str: str | None) -> datetime | None:
    if not timestamp_str:
        return None
    try:
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (TypeError, ValueError):
        return None


def _within_grace_window(
    timestamp_str: str | None,
    grace_minutes: int,
) -> bool:
    """Return True if the given ISO-8601 timestamp is within the grace window."""
    if grace_minutes <= 0:
        return False
    ts = _parse_timestamp(timestamp_str)
    if ts is None:
        return False
    return (datetime.now(timezone.utc) - ts) < timedelta(minutes=grace_minutes)


def _heartbeat_ready_task_count(agent: dict[str, Any]) -> int | None:
    raw = agent.get('heartbeat_ready_task_count')
    if raw is None:
        raw = agent.get('heartbeatReadyTaskCount')
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _heartbeat_is_non_productive(agent: dict[str, Any]) -> bool:
    """True when the latest structured heartbeat did not evidence runnable work.

    needs_help with ready>0 is productive intent (stuck on real work); needs_help
    with ready=0 is the empty/no-snapshot storm that must not hold grace.
    """
    outcome = str(agent.get('heartbeat_outcome') or '').strip().lower()
    if outcome in NON_PRODUCTIVE_HEARTBEAT_OUTCOMES:
        return True
    if outcome == 'needs_help':
        ready = _heartbeat_ready_task_count(agent)
        # Unknown ready count + needs_help: treat as non-productive so
        # queue-blind storms (ready omitted on envelope) still hibernate.
        return ready is None or ready <= 0
    return False


def _activity_grace_protects(agent: dict[str, Any], grace_minutes: int) -> bool:
    """Whether recent activity is evidence of work rather than an empty heartbeat.

    We bypass grace when a fresh, explicitly non-productive heartbeat was
    observed at the same time as (or just after) last_active_at — empty
    heartbeats refresh last_active_at and would otherwise renew forever.

    Idle agents with *no* structured heartbeat_outcome are the same waste class:
    paid turns that never write telemetry still bump last_active_at, so treating
    missing outcome as fail-closed grace keeps high-token idle seats warm. Only
    non-idle activity_state (working / mid-turn) still gets fail-closed keep
    when outcome is absent. Assigned work and unread/recent DMs remain stronger
    guards before this helper runs.
    """
    activity_at = _parse_timestamp(
        agent.get('last_active_at') or agent.get('lastActiveAt')
    )
    if activity_at is None or (
        datetime.now(timezone.utc) - activity_at
    ) >= timedelta(minutes=grace_minutes):
        return False

    if _heartbeat_is_non_productive(agent):
        heartbeat_at = _parse_timestamp(agent.get('heartbeat_observed_at'))
        if heartbeat_at is None:
            return True
        return heartbeat_at + timedelta(seconds=5) < activity_at

    # Productive or unknown outcome.
    outcome = str(agent.get('heartbeat_outcome') or '').strip().lower()
    if not outcome:
        act = str(agent.get('activity_state') or '').strip().lower()
        # No outcome AND no activity_state is the emptiest possible telemetry:
        # the only thing we know is that last_active_at moved, and a heartbeat
        # moves it whether or not the turn did any work. Granting grace on that
        # keeps the seat resident forever — the exact "high-token idle seat"
        # this helper exists to catch.
        #
        # This branch used to withhold grace only when activity_state was
        # EXPLICITLY 'idle'/'sleeping', so an agent reporting neither field fell
        # through to the keep below and was protected indefinitely (banto,
        # observed 2026-08-05 with heartbeat_outcome=None and
        # activity_state=None). Absent telemetry is not evidence of work.
        #
        # Only a POSITIVE non-idle signal ('working', mid-turn, ...) still gets
        # the fail-closed keep, which is what the missing-outcome carve-out was
        # for. Assigned work and unread/recent DMs are checked before this
        # helper runs, so a genuinely busy agent never depends on this branch.
        if act in {'idle', 'sleeping'} or not act:
            return False
    return True


def workload_count(workload: dict[str, Any] | None, buckets: Iterable[str] = ACTIVE_BUCKETS) -> int:
    if not isinstance(workload, dict):
        return 0
    total = 0
    for bucket in buckets:
        try:
            total += int(workload.get(bucket, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def roster_members_by_team(rosters: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for roster in rosters or []:
        if not isinstance(roster, dict):
            continue
        if roster.get('is_active') is False:
            continue
        # Team names are not globally unique. Pulse legitimately has separate
        # Accounting/Review/etc. rosters in several organizations. Collapsing
        # them to a bare slug made each later row overwrite the previous row,
        # leaving earlier agents "unmapped" and therefore permanently protected
        # from hibernation by the fail-closed stop rule.
        team = roster_team_key(roster)
        if not team:
            continue
        members = out.setdefault(team, [])
        for tier in roster.get('tiers') or []:
            for member in tier or []:
                email = ''
                if isinstance(member, str):
                    email = canon_email(member)
                elif isinstance(member, dict):
                    email = canon_email(member.get('email') or member.get('user_email') or member.get('agent_email'))
                if email and email not in members:
                    members.append(email)
        for field in ('members', 'member_emails', 'agents', 'users'):
            for member in roster.get(field) or []:
                email = canon_email(member if isinstance(member, str) else member.get('email') if isinstance(member, dict) else '')
                if email and email not in members:
                    members.append(email)
    return out


def memberships_by_email(rosters: Iterable[dict[str, Any]]) -> dict[str, set[str]]:
    team_members = roster_members_by_team(rosters)
    out: dict[str, set[str]] = {}
    for team, members in team_members.items():
        for email in members:
            out.setdefault(email, set()).add(team)
    return out


def agent_warm_pool_size(agent: dict[str, Any]) -> int:
    """Reserved resident seats from Hive ``warm_pool_size`` (mcp_config or flat).

    0 = on-demand (hibernate when idle). >=1 = keep this seat running so a
    conversational caller does not pay a cold-start. Not a static team list.
    """
    raw = agent.get('warm_pool_size')
    if raw in (None, ''):
        mcp = agent.get('mcp_config')
        raw = mcp.get('warm_pool_size') if isinstance(mcp, dict) else 0
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def teams_keep_resident(teams: Iterable[str] | None) -> tuple[str, ...]:
    """Unqualified slugs from ``teams`` that are latency-resident."""
    found = {
        team_slug_from_key(team)
        for team in (teams or ())
        if team_slug_from_key(team) in KEEP_RESIDENT_TEAM_SLUGS
    }
    return tuple(sorted(found))


def agent_reserved_warm_seat(
    agent: dict[str, Any],
    teams: Iterable[str] | None = None,
) -> bool:
    """True when this seat must stay up for a conversational caller.

    Operator 2026-08-15: CEO Office membership itself is the exception
    (latency first). ``warm_pool_size >= 1`` remains the per-agent Hive
    reservation for seats that are not on a keep-resident team.
    """
    return agent_warm_pool_size(agent) >= 1 or bool(teams_keep_resident(teams))


def agent_model_unserviceable(
    agent: dict[str, Any],
    unserviceable_models: frozenset[str] | None,
) -> bool:
    """True when Cortex currently serves no capacity for this agent's model.

    Operator 2026-08-06 (grok credit exhaustion): agents on a model with zero
    serviceable credentials must not burn turns on guaranteed provider_pool_dry
    errors — hibernate them and resume automatically when capacity returns
    (Cortex re-lists the model the moment a quota bench lapses).
    """
    if not unserviceable_models:
        return False
    model = str(agent.get('model') or '').strip().removeprefix('cortex/').lower()
    return bool(model) and model in unserviceable_models


def decide_on_demand_actions(
    *,
    agents: Iterable[dict[str, Any]],
    rosters: Iterable[dict[str, Any]],
    workload_by_email: dict[str, dict[str, Any]] | None,
    workload_by_team: dict[str, dict[str, Any]] | None,
    on_demand_teams: Iterable[str] = DEFAULT_ON_DEMAND_TEAMS,
    warm_teams: Iterable[str] = DEFAULT_WARM_TEAMS,
    unread_by_email: dict[str, int] | None = None,
    last_dm_at_by_email: dict[str, str] | None = None,
    activity_grace_minutes: int = 5,
    dm_grace_minutes: int = 5,
    unserviceable_models: frozenset[str] | None = None,
) -> list[OnDemandAction]:
    """Return start/stop actions for cold-startable teams.

    Operator 2026-07-13: NO static warm/on-demand team lists for ordinary
    compute-saving teams. "Warm" means RECENTLY ACTIVE (the activity/DM grace
    windows below), plus the explicit exceptions below. An agent hibernates
    only when idle AND no actionable work on itself or any of its teams.
    ``on_demand_teams``/``warm_teams`` are accepted for backward compatibility
    and IGNORED.

    Fail-closed rules:
    - If an agent is missing from Pulse roster membership, never stop it.
    - A recently-active agent (activity grace window) is never stopped.
    - Operator 2026-08-15 / 2026-08-17: a member of
      ``KEEP_RESIDENT_TEAM_SLUGS`` (CEO Office) is never idle-hibernated
      for empty Pulse work. They still consume a real model lease
      (operator 2026-08-18) — lease-stop applies when they do not hold
      a slot. Operator disable (status=stopped) still wins.
    - An agent with ``warm_pool_size >= 1`` is a reserved conversational seat
      (Hive SoT) and stays resident the same way.
    - Stops require no personal work and enough remaining effective seats to
      cover every team's current actionable demand.

    HIVE-628 (operator 2026-07-11): ``unread_by_email`` is the Connect
    unread-DM count per agent email. A stopped agent with unread messages is
    STARTED (sweep backstop behind the event-driven connect-wake, HIVE-602),
    and unread messages BLOCK a stop — hive shutting an agent down for
    compute savings must never strand a delivered-but-unprocessed message.

    PLAT-4490 (operator 2026-07-12): stop safeguards.
    ``last_dm_at_by_email`` maps agent email to the ISO-8601 timestamp of
    the most recent DM received. ``activity_grace_minutes`` and
    ``dm_grace_minutes`` define the grace windows: an agent is not stopped
    if it received a DM or had recent activity within the respective window.
    """
    agents_list = list(agents or [])
    # on_demand_teams / warm_teams intentionally ignored (operator 2026-07-13).
    email_to_teams = memberships_by_email(rosters)
    team_to_members = roster_members_by_team(rosters)
    workload_by_email = {canon_email(k): v for k, v in (workload_by_email or {}).items()}
    unread_by_email = {canon_email(k): int(v or 0) for k, v in (unread_by_email or {}).items()}
    last_dm_at_by_email = {
        canon_email(k): str(v)
        for k, v in (last_dm_at_by_email or {}).items()
        if canon_email(k) and v
    }
    workload_by_team = {canon_team(k): v for k, v in (workload_by_team or {}).items()}
    agent_by_email = {agent_email(agent): agent for agent in agents_list if agent_email(agent)}

    actions: list[OnDemandAction] = []
    acted_agents: set[str] = set()
    planned_start_emails: set[str] = set()

    for agent in agents_list:
        aid = str(agent.get('id') or '')
        email = agent_email(agent)
        if not aid or not email or agent_enabled(agent):
            continue
        member_teams = email_to_teams.get(email, set())
        status = str(agent.get('status') or '').strip().lower()
        # Parked signup seats stay down until a real owner DM or explicit
        # Hive enable. Team demand / keep-resident must not treat them as
        # ordinary workers (ADR-0080 / HIVE-2007).
        if str(agent.get('hibernation_reason') or '') == 'default_until_first_use':
            if (
                unread_by_email.get(email, 0) > 0
                and agent_eligible_to_start(agent)
                and not agent_model_unserviceable(agent, unserviceable_models)
            ):
                actions.append(OnDemandAction(
                    action='start',
                    agent_id=aid,
                    username=agent_username(agent),
                    reason=f'stopped agent has {unread_by_email[email]} unread Connect message(s)',
                    teams=tuple(sorted(member_teams)),
                ))
                acted_agents.add(aid)
                planned_start_emails.add(email)
            continue
        # Operator disable is desired-off. A keep-resident seat that on-demand
        # hibernated (status=hibernated) must be woken even when the catalog
        # or rate-limit signal that caused the hibernate is still armed —
        # otherwise CEO Office stays at replicas=0 until the next false-dead
        # sweep (Hina Voice, 2026-08-17).
        if (
            status == 'hibernated'
            and teams_keep_resident(member_teams)
        ):
            actions.append(OnDemandAction(
                action='start',
                agent_id=aid,
                username=agent_username(agent),
                reason=(
                    'keep-resident team '
                    + ','.join(teams_keep_resident(member_teams))
                    + ' (never hibernate)'
                ),
                teams=tuple(sorted(member_teams)),
            ))
            acted_agents.add(aid)
            planned_start_emails.add(email)
            continue
        # Rate-limited / catalog-dead: leave hibernated until the signal ages
        # out. Waking them only restarts the paid retry storm; the same
        # personal work / DMs will re-wake once they are eligible_to_start
        # again. Keep-resident teams already returned above.
        if not agent_eligible_to_start(agent):
            continue
        if agent_model_unserviceable(agent, unserviceable_models):
            # No capacity behind this agent's model: waking it only buys a
            # provider_pool_dry loop. The same work/DM demand re-wakes it as
            # soon as Cortex serves the model again.
            continue
        if workload_count(workload_by_email.get(email), ACTIONABLE_BUCKETS) > 0:
            actions.append(OnDemandAction(
                action='start',
                agent_id=aid,
                username=agent_username(agent),
                reason='stopped agent has assigned work',
                teams=tuple(sorted(email_to_teams.get(email, set()))),
            ))
            acted_agents.add(aid)
            planned_start_emails.add(email)
            continue
        if unread_by_email.get(email, 0) > 0:
            actions.append(OnDemandAction(
                action='start',
                agent_id=aid,
                username=agent_username(agent),
                reason=f'stopped agent has {unread_by_email[email]} unread Connect message(s)',
                teams=tuple(sorted(email_to_teams.get(email, set()))),
            ))
            acted_agents.add(aid)
            planned_start_emails.add(email)
            continue
        if agent_reserved_warm_seat(agent, member_teams):
            # Hive disable (status=stopped) is desired-off. Only wake a
            # keep-resident seat that on-demand hibernated (status=hibernated).
            if str(agent.get('status') or '').strip().lower() != 'hibernated':
                continue
            resident = teams_keep_resident(member_teams)
            if agent_warm_pool_size(agent) >= 1:
                reason = (
                    f'reserved warm-pool seat (warm_pool_size='
                    f'{agent_warm_pool_size(agent)})'
                )
            else:
                reason = (
                    'keep-resident team '
                    + ','.join(resident)
                    + ' (interaction latency)'
                )
            actions.append(OnDemandAction(
                action='start',
                agent_id=aid,
                username=agent_username(agent),
                reason=reason,
                teams=tuple(sorted(member_teams)),
            ))
            acted_agents.add(aid)
            planned_start_emails.add(email)

    team_target_running: dict[str, int] = {}
    for team in sorted(team_to_members):
        actionable_count = workload_count(
            workload_by_team.get(team),
            ACTIONABLE_BUCKETS,
        )
        members = team_to_members.get(team) or []
        agent_members = [
            email for email in members
            if email in agent_by_email
        ]
        # The historical "one running member satisfies team demand" shortcut
        # recreated a one-agent-per-team cap: Review accumulated 100 actionable
        # items on Revi while stopped peer Kei never woke. Scale roster seats to
        # actionable demand, bounded by real agent members. Personal-work starts
        # above already count toward this target.
        target_running = min(actionable_count, len(agent_members))
        team_target_running[team] = target_running
        if target_running <= 0:
            continue
        # PLAT-5265: capacity means "can absorb demand", not "has a process".
        # A provider-dead running seat must NOT suppress waking a healthy
        # stopped peer — that is the regression that starved Security twice.
        running_count = sum(
            1 for email in agent_members
            if (
                agent_satisfies_demand(agent_by_email[email])
                or email in planned_start_emails
            )
        )
        if running_count >= target_running:
            continue
        for email in agent_members:
            agent = agent_by_email.get(email)
            if not agent or agent_enabled(agent):
                continue
            if str(agent.get('hibernation_reason') or '') == 'default_until_first_use':
                continue
            if not agent_eligible_to_start(agent):
                continue
            if agent_model_unserviceable(agent, unserviceable_models):
                continue
            aid = str(agent.get('id') or '')
            if not aid or aid in acted_agents:
                continue
            actions.append(OnDemandAction(
                action='start',
                agent_id=aid,
                username=agent_username(agent),
                reason=(
                    f'on-demand team {team} has {actionable_count} actionable '
                    f'item(s); scaling roster to {target_running} running member(s)'
                ),
                teams=(team,),
            ))
            acted_agents.add(aid)
            planned_start_emails.add(email)
            running_count += 1
            if running_count >= target_running:
                break

    # Keep a mutable projection of effective capacity after the planned starts.
    # The start path above scales UP to the demand target; this set lets the
    # stop path symmetrically scale DOWN when demand falls. Previously any
    # non-zero team workload protected every running member forever, so a team
    # that fell from three actionable items to two retained a third idle Codex
    # seat and paid for its 15-minute empty heartbeats indefinitely.
    remaining_capacity_emails = {
        email
        for email, agent in agent_by_email.items()
        if (
            agent_satisfies_demand(agent)
            # A seat on a model with zero serviceable capacity is not capacity:
            # counting it here made its own removal look team-unsafe, so the
            # dead seat kept running its provider_pool_dry loop forever.
            and not agent_model_unserviceable(agent, unserviceable_models)
            and not agent_runtime_cannot_turn(agent)
        ) or email in planned_start_emails
    }
    running_agents = (
        agent for agent in agents_list
        if agent_running(agent)
    )

    for agent in running_agents:
        aid = str(agent.get('id') or '')
        email = agent_email(agent)
        if not aid or aid in acted_agents:
            continue
        teams = email_to_teams.get(email)
        if not teams:
            # Fail-closed: an agent Hive can't map to a Pulse team is never
            # auto-stopped (it may be doing off-roster work).
            continue
        # Operator 2026-08-17: the keep-resident whitelist (CEO Office) is
        # never hibernated. Idle / rate-limit / catalog-dead used to take
        # Hina down every 10 minutes; a talk seat must stay resident.
        if teams_keep_resident(teams):
            continue
        # Operator 2026-07-13: NO static warm/on-demand team lists. "Warm" means
        # RECENTLY ACTIVE (the activity/DM grace windows below, PLAT-4490). Every
        # team is cold-startable; the only thing that keeps an idle agent
        # resident is recent activity, a recent/unread DM, or actionable work.
        #
        # Rate-limited seats are the exception: they cannot process assigned
        # work or DMs until the provider recovers. Hibernate them (rest, not
        # cull) so we stop paid retries and can wake a healthy peer instead.
        # Keep-resident teams already returned above.
        rate_limited = agent_rate_limited(agent)
        # A model with zero serviceable capacity is the same shape as a
        # rate-limited seat: the agent cannot process work or DMs, so assigned
        # work must not keep it resident burning guaranteed-fail turns
        # (operator 2026-08-06: hibernate to save resources; resume when
        # capacity is back).
        model_dead = agent_model_unserviceable(agent, unserviceable_models)
        cannot_turn = agent_runtime_cannot_turn(agent)
        if (
            not rate_limited and not model_dead and not cannot_turn
            and workload_count(workload_by_email.get(email), ACTIONABLE_BUCKETS) > 0
        ):
            continue
        if not rate_limited and not model_dead and not cannot_turn and unread_by_email.get(email, 0) > 0:
            # Unread Connect messages block scale-down: the agent is about to
            # process a delivered DM; stopping now would strand it (HIVE-628).
            continue
        # PLAT-4490: DM grace timer — don't stop an agent within N min of
        # receiving a DM (it may be mid-processing). Rate-limited seats skip
        # this: they cannot process the DM either.
        if (
            not rate_limited and not model_dead and not cannot_turn
            and _within_grace_window(last_dm_at_by_email.get(email), dm_grace_minutes)
        ):
            continue
        # PLAT-4490: recent-activity grace window — don't stop an effective
        # agent mid-turn / mid-activity. A structured queue_empty/all_blocked/
        # future_due heartbeat is not work, however: heartbeat execution itself
        # updates last_active_at and otherwise renews this grace forever. Pending
        # and recent DMs plus assigned/team work above remain stronger guards.
        if (
            not model_dead and not cannot_turn
            and agent_satisfies_demand(agent)
            and _activity_grace_protects(agent, activity_grace_minutes)
        ):
            continue
        # Operator 2026-09-22 note: unserviceable-model seats hibernate
        # (model_dead branch below) — hibernation is scale-to-zero, NOT the
        # 2026-08-17 stop->restart->retry storm, and demand re-wakes the seat
        # when the model returns. The catalog-glitch protection now lives in
        # that recovery path, not in keeping seats hot.
        # Explicit Hive warm_pool_size reservation (non-whitelist teams).
        # Rate-limited / model-dead seats still hibernate — they cannot
        # take a turn. CEO Office already returned above.
        if (
            not rate_limited and not model_dead and not cannot_turn
            and agent_reserved_warm_seat(agent, teams)
        ):
            continue

        # A multi-team member can be excess in one roster but load-bearing in
        # another. Stop it only when removing its effective capacity keeps every
        # team at or above that team's demand-derived target.
        # Rate-limited agents are already excluded from remaining_capacity_emails
        # (agent_satisfies_demand is False), so they may always stop.
        removal_is_safe = True
        if email in remaining_capacity_emails:
            for team in teams:
                target = team_target_running.get(team, 0)
                members = team_to_members.get(team) or []
                current_capacity = sum(
                    1 for member_email in members
                    if member_email in remaining_capacity_emails
                )
                if current_capacity - 1 < target:
                    removal_is_safe = False
                    break
        if not removal_is_safe:
            continue

        if model_dead:
            model = str(agent.get('model') or '')
            reason = (
                f'model {model} has no serviceable capacity (all provider '
                'credentials exhausted/cooling); hibernating until capacity returns'
            )
        elif cannot_turn:
            reason = (
                'runtime cannot take a model turn; hibernating so Pulse can '
                'rebalance assigned work'
            )
        elif rate_limited:
            reason = (
                'provider rate/usage limited; hibernating until capacity recovers'
            )
        else:
            active_targets = [
                f'{team}={team_target_running.get(team, 0)}'
                for team in sorted(teams)
                if team_target_running.get(team, 0) > 0
            ]
            reason = (
                'idle running seat exceeds current actionable team demand: '
                + ', '.join(active_targets)
                if active_targets
                else 'idle on-demand agent has no personal or team work'
            )
        actions.append(OnDemandAction(
            action='stop',
            agent_id=aid,
            username=agent_username(agent),
            reason=reason,
            teams=tuple(sorted(teams)),
        ))
        acted_agents.add(aid)
        remaining_capacity_emails.discard(email)

    return actions
