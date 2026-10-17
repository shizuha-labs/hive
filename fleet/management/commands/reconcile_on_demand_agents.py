"""Reconcile on-demand fleet agents from Pulse workload + roster state."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import logging
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

from fleet.on_demand import (
    ACTIONABLE_BUCKETS,
    OnDemandAction,
    agent_running,
    agent_username,
    canon_team,
    decide_on_demand_actions,
    memberships_by_email,
    roster_team_key,
    scoped_team_key,
)
from fleet.model_lease import reconcile_model_lease
from fleet.lifecycle_fleet import (
    converge_running_lifecycle_agent,
    hibernate_lifecycle_agent,
    list_lifecycle_agents,
    set_lifecycle_agent_enabled,
)


@dataclass(frozen=True)
class ConnectLifecycleSnapshot:
    """Connect inputs plus an explicit freshness/availability signal."""

    available: bool
    # Historical field name retained to avoid widening the pure policy API.
    # Values are now replayable, undelivered messages — never UI unread badges.
    unread_by_email: dict[str, int]
    last_dm_at_by_email: dict[str, str]
    error: str = ''


def _split_csv(values):
    out = []
    for value in values or []:
        for part in str(value).split(','):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _items(payload: Any, key: str | None = None) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    if key and isinstance(payload.get(key), list):
        return [x for x in payload[key] if isinstance(x, dict)]
    for candidate in ('teams', 'rosters', 'results', 'items'):
        if isinstance(payload.get(candidate), list):
            return [x for x in payload[candidate] if isinstance(x, dict)]
    return []


def _normalize_team_workload(workload, rosters, snapshot_teams=None):
    """Translate Pulse team-id workload keys to the roster slugs policy uses.

    Pulse `/admin/org-chart/snapshot/` keys ``team_workload`` by numeric team
    id (and occasionally ``roster:<slug>``). Pulse ``/admin/team-roster/`` rows
    often have only ``team_name`` and **no** ``id``. Without the snapshot
    teams list, id→slug aliases never form, every team looks empty, and the
    on-demand reconciler false-stops agents that still have team work.
    """
    alias_targets: dict[str, set[str]] = {}

    def _register(target: str, *values) -> None:
        if not target:
            return
        for value in values:
            if value not in (None, ''):
                alias_targets.setdefault(canon_team(value), set()).add(target)

    # Prefer snapshot team metadata (id + display name) — authoritative for
    # numeric team_workload keys from org-chart/snapshot.
    for team in snapshot_teams or []:
        if not isinstance(team, dict):
            continue
        team_name = (
            team.get('slug') or team.get('team_name') or team.get('name')
        )
        target = scoped_team_key(
            team_name,
            team.get('organization_slug')
            or team.get('org_slug')
            or team.get('organization'),
        )
        _register(
            target,
            team.get('id'),
            team.get('team_id'),
            team.get('slug'),
            team.get('team_name'),
            team.get('name'),
            target,
        )

    for roster in rosters or []:
        if not isinstance(roster, dict):
            continue
        target = roster_team_key(roster)
        _register(
            target,
            roster.get('id'),
            roster.get('team_id'),
            roster.get('slug'),
            roster.get('team_name'),
            roster.get('name'),
            target,
        )

    normalized = {}
    for key, counters in (workload or {}).items():
        raw = str(key or '')
        # Snapshot sometimes emits "roster:<slug>" for non-admin teams.
        if raw.lower().startswith('roster:'):
            raw = raw.split(':', 1)[1]
        raw_key = canon_team(raw)
        targets = alias_targets.get(raw_key, set())
        # A bare slug such as "accounting" is ambiguous when several
        # organizations own a same-named team. Numeric snapshot IDs and scoped
        # aliases resolve exactly; ambiguous legacy keys remain isolated rather
        # than leaking one organization's demand into another.
        team = next(iter(targets)) if len(targets) == 1 else raw_key
        target = normalized.setdefault(team, {})
        for bucket, value in (counters or {}).items():
            try:
                target[bucket] = target.get(bucket, 0) + int(value or 0)
            except (TypeError, ValueError):
                continue
    return normalized


def observed_native_replicas(username: str) -> int | None:
    """Native Deployment spec.replicas, or None when unknown / not native."""
    try:
        from fleet.models import FleetAgent
        from fleet import provisioner

        qs = (
            FleetAgent.objects
            .filter(agent_username=username)
            .exclude(status=FleetAgent.Status.DELETED)
        )
        row = qs.filter(managing_daemon__isnull=True).first()
        if row is None:
            # Daemon-linked but still a Hive-rendered k3s Deployment.
            row = next(
                (
                    candidate
                    for candidate in qs
                    if (getattr(candidate, 'deployment_name', None) or '').strip()
                ),
                None,
            )
        if row is None:
            return None
        return provisioner.current_replicas(row)
    except Exception:  # noqa: BLE001 — observation must not break the sweep
        return None


def observed_native_ready(username: str) -> bool | None:
    """True if the native Deployment has a ready replica.

    False means spec.replicas>=1 but ready_replicas=0 (CrashLoop /
    CreateContainerConfigError / image-pull). None is unreadable or
    scaled to 0 — callers fail closed except the existing replicas==0
    exception.
    """
    try:
        from fleet.models import FleetAgent
        from fleet import provisioner

        qs = (
            FleetAgent.objects
            .filter(agent_username=username)
            .exclude(status=FleetAgent.Status.DELETED)
        )
        row = qs.filter(managing_daemon__isnull=True).first()
        if row is None:
            row = next(
                (
                    candidate
                    for candidate in qs
                    if (getattr(candidate, 'deployment_name', None) or '').strip()
                ),
                None,
            )
        if row is None:
            return None
        return provisioner.deployment_runtime_ready(row)
    except Exception:  # noqa: BLE001 — observation must not break the sweep
        return None


def runtime_idle_confirmation(username: str) -> tuple[bool, str]:
    """Require an uncached, fresh, explicit idle observation for automation.

    Unknown is not idle.  Automatic lifecycle writes are destructive to an
    in-flight stream, so transport failure, a missing principal, stale clocks,
    and unrecognized states all fail closed and defer to the next sweep.
    """
    try:
        from ...external_agents import fresh_agent_telemetry, telemetry_fields
        from ...model_lease import _agent_dict_telemetry_fresh

        snapshot = (fresh_agent_telemetry() or {}).get(str(username or '').lower())
        if not snapshot:
            return False, 'fresh runtime idle evidence unavailable'
        fields = dict(telemetry_fields(snapshot) or {})
        state = str(fields.get('activity_state') or '').strip().lower()
        if not _agent_dict_telemetry_fresh(fields):
            return False, f'runtime activity telemetry is stale (state={state or "unknown"})'
        if state != 'idle':
            return False, f'runtime reports {state or "unknown"} at apply time'
        return True, 'runtime reports fresh idle at apply time'
    except Exception as exc:  # noqa: BLE001 — automatic stop fails closed
        return False, f'fresh runtime idle check failed: {exc.__class__.__name__}'


def stop_deferred_by_fresh_activity(action) -> str | None:
    """Last-instant affirmative-idle fence for automatic scheduler stops.

    The lease decision judged rui idle from a snapshot taken before its
    post-roll heartbeat turn began; the hibernate landed 7 seconds before the
    turn's cortex request and killed a 272K cold rebuild mid-stream. A stop
    decided from OLD evidence must re-check uncached gateway telemetry at apply
    time.  Only a fresh explicit ``idle`` may proceed; every ambiguous state
    defers, and every retry calls this function again.

    Exception: a native Deployment already at 0 replicas has no stream to
    protect. Deferring that stop left FleetAgent.status=running forever
    (sato 2026-08-12) so the next grant treated the ghost as already up.

    Exception: spec.replicas>=1 but ready_replicas=0 (recon/tomo 2026-08-31
    CreateContainerConfigError). There is no agent process and therefore no
    in-flight stream; unavailable telemetry would otherwise DEFER STOP
    forever while the Deployment stays at 1.

    Exception: ``interrupt_ok`` (HIVE-1712). The lease planner already spent
    the 900s active-turn grace on a *non-holder* that never went idle and
    still said stop. Re-checking "working" here would defer forever (kei
    2026-08-15: every 10-min sweep logged DEFER STOP while she kept a 392k
    DeepSeek prefix warm without a lease).

    Exception: lease leftover + telemetry unavailable (operator 2026-09-01).
    ``does not hold the model lease`` plus missing gateway evidence used to
    DEFER STOP forever (lex/lina/hana/banto). They kept 30m Cortex KV homes,
    21 protected books filled 20 GLM seats, and homeless waiters 503'd
    ``residency_full``. A non-holder with no idle proof is not a stream we
    can protect; yield+hibernate so the seat can be granted.
    """
    if getattr(action, 'action', None) != 'stop':
        return None
    if getattr(action, 'interrupt_ok', False):
        return None
    allowed, detail = runtime_idle_confirmation(action.username)
    if allowed:
        return None
    if 'unavailable' in str(detail or ''):
        replicas = observed_native_replicas(action.username)
        if replicas == 0:
            return None
        if observed_native_ready(action.username) is False:
            return None
        reason = str(getattr(action, 'reason', '') or '').lower()
        if 'does not hold the model lease' in reason:
            return None
    return detail


class Command(BaseCommand):
    help = (
        'Start/stop cold-startable rt-fleet agents from Pulse team workload. '
        'Dry-run by default; pass --apply to call the runtime-fleet control API.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Apply start/stop actions.')
        parser.add_argument(
            '--reconcile-org-memberships',
            action='store_true',
            help=(
                'Ensure every resolvable Pulse roster identity has its parent '
                'Admin organization membership before lifecycle decisions.'
            ),
        )
        parser.add_argument(
            '--reconcile-capabilities',
            action='store_true',
            help=(
                'Persist and push each agent capability policy as an '
                'organization-qualified Hive desired runtime config.'
            ),
        )
        parser.add_argument(
            '--on-demand-team',
            action='append',
            default=[],
            help='Comma-separated or repeated team slug(s) eligible for cold stop/start.',
        )
        parser.add_argument(
            '--warm-team',
            action='append',
            default=[],
            help='Comma-separated or repeated team slug(s) that must stay warm.',
        )
        parser.add_argument(
            '--snapshot-file',
            default='',
            help=(
                'Optional JSON file containing agents, rosters, workload, and team_workload. '
                'Useful for audits/tests without calling Pulse.'
            ),
        )

    def handle(self, *args, **options):
        apply = bool(options['apply'])
        # --on-demand-team / --warm-team are deprecated no-ops (operator
        # 2026-07-13: every team is cold-startable, "warm" = recently active).
        try:
            from fleet.provisioner import clear_stale_personal_isolation_blocks
            cleared = clear_stale_personal_isolation_blocks()
        except Exception:
            # SimpleTestCase on-demand tests forbid ORM. Production still
            # continues the lease/wake pass if this cleanup cannot run.
            cleared = 0
        if cleared:
            self.stdout.write(
                f'cleared stale personal-isolation blocks: {cleared}'
            )

        snapshot_teams = []
        snapshot: dict = {}
        if options['snapshot_file']:
            payload = self._read_snapshot_file(options['snapshot_file'])
            agents = payload.get('agents') or list_lifecycle_agents(include_disabled=True)
            rosters = payload.get('rosters') or payload.get('teams') or []
            workload = payload.get('workload') or {}
            team_workload = payload.get('team_workload') or {}
            snapshot_teams = payload.get('snapshot_teams') or payload.get('org_teams') or []
            snapshot = payload if isinstance(payload, dict) else {}
        else:
            agents = list_lifecycle_agents(include_disabled=True)
            # PLAT-5265: capacity is decided HERE, but the provider-unavailable
            # signals were only ever applied on the API/UI read paths, so this
            # list carried none of them and a provider-dead running seat counted
            # as usable capacity. Apply the same enrichment the views apply.
            self._apply_health_signals(agents)
            rosters = self._fetch_pulse_rosters()
            snapshot = self._fetch_pulse_snapshot()
            workload = snapshot.get('workload') or {}
            team_workload = snapshot.get('team_workload') or {}
            # org-chart/snapshot exposes numeric team ids under "teams".
            snapshot_teams = snapshot.get('teams') or []

        team_workload = _normalize_team_workload(
            team_workload,
            rosters,
            snapshot_teams=snapshot_teams,
        )

        # HIVE-628/PLAT-4490: one Connect snapshot provides pending-delivery
        # counts for wake decisions plus latest inbound-DM timestamps for stop
        # grace. UI unread badges are deliberately not lifecycle inputs.
        # Fail closed for STOPS only: without this snapshot the reconciler
        # cannot prove hibernation is safe, but workload-proven starts must
        # still run so a Connect outage cannot strand assigned work. The
        # command exits non-zero after safe starts, making the partial outcome
        # visible to the dedicated failed-Job alert.
        connect_state = self._fetch_connect_state()

        if not rosters:
            raise CommandError('No Pulse rosters available; refusing to make lifecycle decisions.')

        capability_result = None
        if options['reconcile_capabilities']:
            from fleet.capability_reconcile import (
                apply_capability_reconcile,
                plan_capability_reconcile,
            )
            from fleet.lifecycle_fleet import configure_lifecycle_agent

            capability_plans = plan_capability_reconcile(agents, rosters)
            drifted = [plan for plan in capability_plans if plan['updates']]
            if apply:
                capability_result = apply_capability_reconcile(
                    capability_plans,
                    configure_lifecycle_agent,
                )
            else:
                capability_result = {
                    'checked_agents': len(capability_plans),
                    'drifted_agents': len(drifted),
                    'updated_agents': 0,
                    'errors': [],
                }
            self.stdout.write(
                f'capabilities: checked={capability_result["checked_agents"]} '
                f'drifted={capability_result["drifted_agents"]} '
                f'updated={capability_result["updated_agents"]} '
                f'errors={len(capability_result["errors"])}'
            )
            for plan in drifted:
                self.stdout.write(
                    f'capability-drift {plan["agent_username"] or plan["email"]} '
                    f'orgs={",".join(plan["organization_scopes"]) or "-"} '
                    f'fields={",".join(sorted(plan["updates"]))}'
                )

        # Organization-membership repair shares this scheduled command as a
        # backstop, but it is not a prerequisite for lifecycle decisions. A
        # transient Admin/ID read failure used to abort here, before the
        # on-demand policy ran, leaving excess idle provider seats alive until
        # a later fully-green sweep. Preserve the failure for the CronJob alert
        # while still completing the independently safe lifecycle work.
        org_membership_error = None
        if options['reconcile_org_memberships']:
            try:
                self._reconcile_roster_org_memberships(rosters)
            except CommandError as exc:
                org_membership_error = str(exc)
                self.stdout.write(self.style.ERROR(
                    'org-memberships: repair incomplete; continuing lifecycle '
                    f'reconcile before failing the job: {org_membership_error}'
                ))

        # Archive is a Hive desired state, not merely a start exclusion. Enforce
        # it every sweep so another controller cannot resurrect an archived
        # runtime and leave it consuming compute forever.
        from fleet.models import archived_usernames
        archived = archived_usernames()
        archived_actions = []
        if archived:
            archived_actions = [
                OnDemandAction(
                    action='stop',
                    agent_id=str(agent.get('id') or ''),
                    username=agent_username(agent),
                    reason='Hive archive marker requires stopped state',
                )
                for agent in agents
                if agent_username(agent) in archived
                and agent_running(agent)
                and agent.get('id')
            ]
            before = len(agents)
            agents = [
                a for a in agents
                if str(a.get('username') or a.get('agent_username') or '').lower() not in archived
            ]
            skipped = before - len(agents)
            if skipped:
                self.stdout.write(f'excluding {skipped} archived agent(s) from demand starts')

        # Which of the fleet's Cortex models currently have zero serviceable
        # capacity (operator 2026-08-06: hibernate agents on a credit-dry
        # model, auto-resume when Cortex re-lists it). Fail-open frozenset().
        from fleet.model_serviceability import unserviceable_cortex_models
        unserviceable = unserviceable_cortex_models(
            {str(a.get('model') or '') for a in agents},
        )
        if unserviceable:
            self.stdout.write(
                f'models with no serviceable capacity: {sorted(unserviceable)}',
            )

        actions = decide_on_demand_actions(
            agents=agents,
            rosters=rosters,
            workload_by_email=workload,
            workload_by_team=team_workload,
            unread_by_email=connect_state.unread_by_email,
            last_dm_at_by_email=connect_state.last_dm_at_by_email,
            unserviceable_models=unserviceable,
        )
        if not connect_state.available:
            actions = [action for action in actions if action.action == 'start']
        actions = archived_actions + actions

        # ── Agent→model exclusivity lease (fleet/model_lease.py) ─────────────
        # Runs AFTER the ordinary decision and OVERRIDES it for pool members, so
        # this command stays the single writer of agent lifecycle. Two controllers
        # both driving replica counts would fight: hibernation would start a pool
        # agent because its team has work, while the lease scheduler was holding it
        # down to protect another agent's KV cache.
        try:
            # Refresh only the activity projection immediately before lease
            # planning.  The earlier health/UI enrichment intentionally uses a
            # short cache; lease expiry/handoff cannot.  A later per-revoke
            # callback below performs a second last-instant confirmation.
            if apply:
                from fleet.external_agents import (
                    fresh_agent_telemetry,
                    telemetry_fields,
                )
                fresh_telemetry = fresh_agent_telemetry() or {}
                for agent in agents:
                    telemetry = fresh_telemetry.get(agent_username(agent))
                    if telemetry:
                        agent.update(telemetry_fields(telemetry, configured=agent))
            # Native-lifecycle reconciliation (PLAT-6618 class, PLAT-302
            # phantom-holders wave): DB lifecycle-plane rows drift — jun/kai
            # hibernated under a completed completed-status receipt while the
            # health/UI enriched rows still read `running`, so decide_lease's
            # not-running fence never saw the seat dormant.  Overlay the
            # durable receipt intent before pooling so the SAME tick that
            # writes the board reconciles the holder set.  Non-fatal.
            from fleet.native_lifecycle_projection import (
                overlay_completed_native_lifecycle,
            )
            try:
                agents = overlay_completed_native_lifecycle(agents)
            except Exception:
                logger.exception('serve-time native lifecycle receipt unavailable')
            lease_result = reconcile_model_lease(
                agents=agents,
                email_to_teams=memberships_by_email(rosters),
                workload_by_team=team_workload,
                actionable_buckets=ACTIONABLE_BUCKETS,
                apply=apply,
                # Hive worthiness for sprints: assigned tasks, Connect DMs, alerts.
                workload_by_email=workload,
                unread_by_email=connect_state.unread_by_email,
                alerts_by_email=(snapshot.get('alerts_by_email')
                                 or snapshot.get('alert_workload')
                                 or {}),
                can_handoff=(runtime_idle_confirmation if apply else None),
            )
        except Exception as exc:  # noqa: BLE001 — leasing must never break hibernation
            self.stdout.write(self.style.WARNING(f'model-lease: skipped ({exc})'))
            lease_result = {'scale': {}, 'log': []}
        for line in lease_result.get('log') or []:
            self.stdout.write(f'model-lease: {line}')

        # PLAT-10630: periodic deferred-wake grant sweep. The stop/hibernate
        # paths reconcile event-driven, but the 2026-10-01 cascade (38 seats
        # outstanding, zero grant-release events) proved a backlog cannot
        # drain when no seat happens to stop. This 10-min lease reconcile is
        # the sweep host: it runs right after the lease tick, so slots the
        # planner just granted or freed are visible to the grant pass. Grants
        # are real lifecycle writes — apply-mode only, and fenced so a
        # reconcile fault can never break this command's own hibernation duty.
        if apply:
            try:
                from fleet.wake_defers import reconcile_deferred_wakes
                wake_result = reconcile_deferred_wakes()
                granted = wake_result.get('granted') or []
                skipped = wake_result.get('skipped') or {}
                self.stdout.write(
                    f'wake-defer: periodic reconcile pass considered='
                    f'{wake_result.get("considered", 0)} granted={len(granted)}'
                    f'{(" (" + ", ".join(granted) + ")") if granted else ""}'
                    f' skipped={len(skipped)}'
                )
            except Exception as exc:  # noqa: BLE001 — drain must never break reconcile
                self.stdout.write(self.style.WARNING(
                    f'wake-defer: periodic reconcile skipped ({exc})'))
        lease_scale = lease_result.get('scale') or {}
        lease_interrupt_ok = {
            str(name) for name in (lease_result.get('interrupt_ok') or set()) if name
        }
        if lease_scale:
            agent_id_by_username = {
                agent_username(a): str(a.get('id') or '')
                for a in agents if a.get('id')
            }
            running_by_username = {agent_username(a): agent_running(a) for a in agents}
            actions = [a for a in actions if a.username not in lease_scale]
            for uname, should_run in lease_scale.items():
                aid = agent_id_by_username.get(uname)
                if not aid:
                    continue
                claimed_running = running_by_username.get(uname)
                if claimed_running == should_run:
                    if should_run:
                        # Ghost holder: Hive status=running but deploy is 0.
                        # Skipping START here wasted nami's DeepSeek slot
                        # (2026-08-12): grant succeeded, pod never came up.
                        replicas = observed_native_replicas(uname)
                        if replicas is None or replicas > 0:
                            if apply and replicas is not None and replicas > 0:
                                converge_running_lifecycle_agent(aid)
                            continue
                    else:
                        # Ghost living: Hive already "not running" (hibernated)
                        # but the Deployment still has replicas and can hit
                        # Cortex. Skipping STOP here left ryo living for hours
                        # after hibernated_at (2026-08-28).
                        replicas = observed_native_replicas(uname)
                        if replicas is None or replicas <= 0:
                            continue
                ghost_living = (not should_run and not claimed_running)
                actions.append(OnDemandAction(
                    action='start' if should_run else 'stop',
                    agent_id=aid,
                    username=uname,
                    reason=('holds the model lease' if should_run
                            else 'does not hold the model lease'),
                    interrupt_ok=(
                        not should_run
                        and (uname in lease_interrupt_ok or ghost_living)
                    ),
                ))
        else:
            # Lease reconcile produced no authoritative scale (transient board
            # unreadable / pool snapshot failure). Do NOT let an ordinary
            # on-demand start boot a governed pool member into a full pool —
            # that is the confirmed kei/ryo flush: cold-boot, lease-gated no-op,
            # re-hibernated by the next sweep. Fall back to a live lease-gate
            # read for governed members; ungoverned agents start normally.
            from fleet.model_lease import lease_wake_available
            agent_by_username = {
                agent_username(a): a for a in agents if agent_username(a)
            }
            gated = []
            for action in actions:
                if action.action != 'start' or action.username not in agent_by_username:
                    gated.append(action)
                    continue
                wake_ok, wake_reason = lease_wake_available(
                    agent=agent_by_username[action.username],
                )
                if wake_reason == 'no_lease_pool' or wake_ok:
                    gated.append(action)
                    continue
                self.stdout.write(self.style.WARNING(
                    f'{"APPLY" if apply else "DRY-RUN"}: DEFER START {action.username} — '
                    f'lease pool full/unknown ({wake_reason}); reconcile grants when a slot frees'
                ))
            actions = gated

        # PLAT-5861 rollout repair: the legacy runtime represented automatic
        # hibernation with the operator kill-switch.  Hive already knows the
        # seat is hibernated, but Pulse cannot assign it until the runtime
        # exports that typed state. Reassert only mismatched daemon-managed
        # seats through the normal actuator. A start/stop chosen earlier in
        # this production-order pass wins and suppresses the repair.
        acted_agent_ids = {action.agent_id for action in actions}
        for agent in agents:
            aid = str(agent.get('id') or '')
            if (
                aid
                and aid not in acted_agent_ids
                and agent.get('lifecycle_plane') == 'runtime'
                and str(agent.get('status') or '').lower() == 'hibernated'
                and str(agent.get('lifecycle_state') or '').lower() != 'hibernated'
            ):
                actions.append(OnDemandAction(
                    action='sync_hibernation',
                    agent_id=aid,
                    username=agent_username(agent),
                    reason='reassert Hive hibernation intent to runtime exporter',
                ))
                acted_agent_ids.add(aid)

        if apply:
            skip_ids = {
                action.agent_id for action in actions
                if action.action in {'stop', 'sync_hibernation'}
            }
            self._converge_running_native_release(agents, skip_ids=skip_ids)

        mode = 'APPLY' if apply else 'DRY-RUN'
        self.stdout.write(f'{mode}: {len(actions)} on-demand action(s)')
        failed_actions = []

        def apply_lifecycle_action(action):
            if action.action == 'start':
                return set_lifecycle_agent_enabled(action.agent_id, True)
            if action.username in archived:
                # Archive is an operator-owned durable exclusion, never an
                # auto-resumable scheduler hibernation.
                return set_lifecycle_agent_enabled(action.agent_id, False)
            return hibernate_lifecycle_agent(
                action.agent_id,
                reason=action.reason,
            )

        for action in actions:
            deferral = (
                stop_deferred_by_fresh_activity(action)
                if (
                    apply
                    and action.action == 'stop'
                    and action.username not in archived
                ) else None
            )
            if deferral:
                self.stdout.write(self.style.WARNING(
                    f'DEFER STOP {action.username} ({action.agent_id}): {deferral} '
                    f'— original reason: {action.reason}; next sweep re-decides'
                ))
                continue
            line = (
                f'{action.action.upper()} {action.username} ({action.agent_id}) '
                f'teams={",".join(action.teams) or "-"} reason={action.reason}'
            )
            if not apply:
                self.stdout.write(f'[dry-run] {line}')
                continue
            ok, detail = apply_lifecycle_action(action)
            if ok:
                self.stdout.write(self.style.SUCCESS(f'{line}: ok {detail}'))
            elif action.action == 'start' and isinstance(detail, dict) and detail.get('lease_waiting') is True:
                self.stdout.write(self.style.WARNING(f'DEFER {line}: {detail}'))
            else:
                self.stdout.write(self.style.ERROR(f'{line}: failed {detail}'))
                failed_actions.append((action, detail))

        # The runtime control endpoint can be briefly unavailable while its
        # event loop is applying a large reconcile batch. Retrying failures at
        # the tail lets the other lifecycle writes drain that contention first.
        # Never report a green CronJob when Hive's desired state was rejected:
        # persistent failures must alert and remain eligible next sweep.
        if apply and failed_actions:
            remaining = []
            persistent_start_failures = []
            for action, first_detail in failed_actions:
                deferral = (
                    stop_deferred_by_fresh_activity(action)
                    if (
                        action.action == 'stop'
                        and action.username not in archived
                    ) else None
                )
                if deferral:
                    self.stdout.write(self.style.WARNING(
                        f'DEFER RETRY STOP {action.username} ({action.agent_id}): '
                        f'{deferral} — original reason: {action.reason}; '
                        'next sweep re-decides'
                    ))
                    continue
                ok, detail = apply_lifecycle_action(action)
                line = f'RETRY {action.action.upper()} {action.username} ({action.agent_id})'
                if ok:
                    self.stdout.write(self.style.SUCCESS(f'{line}: ok {detail}'))
                elif action.action == 'start' and isinstance(detail, dict) and detail.get('lease_waiting') is True:
                    self.stdout.write(self.style.WARNING(f'DEFER {line}: {detail}'))
                else:
                    self.stdout.write(self.style.ERROR(
                        f'{line}: failed {detail} (first failure: {first_detail})'
                    ))
                    if action.action == 'start':
                        # HIVE-2893 hardening (a): one seat's persistent start
                        # failure (e.g. the leftover-husk fence) must not fail
                        # the whole reconcile run — the lease tick and wake-defer
                        # passes above already ran, and the other seats' desired
                        # states still need their retry. Stay loud (ERROR
                        # summary below) and keep the seat eligible for the next
                        # sweep instead of failing the CronJob every 10 minutes
                        # while the fleet starves behind a full pool.
                        # HIVE-2893 hardening (b): a lease holder that fails to
                        # start squats its slot; yield it now.
                        self._yield_lease_on_start_failure(action, agents)
                        persistent_start_failures.append(action)
                    else:
                        remaining.append(action)
            if persistent_start_failures:
                names = ', '.join(action.username for action in persistent_start_failures)
                self.stdout.write(self.style.ERROR(
                    f'RECONCILE INCOMPLETE: persistent start failure(s) after retry: '
                    f'{names} — held leases yielded where possible; seat(s) remain '
                    f'eligible next sweep (HIVE-2893: was a run-fatal CommandError)'
                ))
            if remaining:
                names = ', '.join(action.username for action in remaining)
                raise CommandError(
                    f'Lifecycle reconcile incomplete after retry: {names}'
                )

        capability_errors = (capability_result or {}).get('errors') or []
        if not connect_state.available:
            safe_count = len(actions)
            raise CommandError(
                'PARTIAL reconcile: Connect lifecycle snapshot unavailable; '
                f'applied/emitted {safe_count} safe lifecycle action(s), skipped idle '
                f'stop decisions: {connect_state.error}; '
                f'capability_errors={len(capability_errors)}'
            )
        if capability_errors:
            names = ', '.join(str(row.get('agent') or row.get('email') or '?') for row in capability_errors)
            raise CommandError(f'Capability reconcile incomplete: {names}')
        if org_membership_error:
            raise CommandError(
                'PARTIAL reconcile: lifecycle decisions completed; '
                f'{org_membership_error}'
            )

    def _yield_lease_on_start_failure(self, action, agents):
        """HIVE-2893 hardening (b): a lease holder whose start persistently
        fails (e.g. the leftover-husk fence) must yield its slot within a tick
        instead of squatting it while unbootable. Best-effort and fenced: any
        fault logs a warning and moves on — the next sweep re-decides."""
        try:
            from fleet.model_lease import (
                CortexLeaseClient, _canonical_pool_model,
                all_pool_configs, principal_for,
            )
            agent_row = next(
                (a for a in agents if agent_username(a) == action.username), None)
            model = _canonical_pool_model(
                (agent_row or {}).get('effective_model')
                or (agent_row or {}).get('model') or '')
            governed = next(
                (c for c in all_pool_configs()
                 if _canonical_pool_model(c.get('model_id') or '') == model),
                None)
            if not governed:
                return
            ok = CortexLeaseClient(timeout=10.0).revoke(
                str(governed['model_id']).strip(),
                holder=principal_for(action.username),
                force=True,
                reason='yield-lease-start-failed (HIVE-2893)',
            )
            if ok:
                self.stdout.write(self.style.WARNING(
                    f'LEASE YIELD {action.username}: start persistently failed; '
                    f'yielded the {governed["model_id"]} lease so the planner can '
                    f're-seat the slot within a tick'))
            else:
                self.stdout.write(self.style.WARNING(
                    f'LEASE YIELD {action.username}: revoke rejected; '
                    f'slot re-decided next sweep'))
        except Exception as exc:  # noqa: BLE001 — yielding must never break reconcile
            self.stdout.write(self.style.WARNING(
                f'LEASE YIELD {action.username}: skipped ({exc})'))

    def _reconcile_roster_org_memberships(self, rosters):
        from fleet.org_membership_sync import reconcile_roster_org_memberships

        summary = reconcile_roster_org_memberships(rosters)
        self.stdout.write(
            'org-memberships: checked={checked} created={created} existing={existing} '
            'unresolved={unresolved_count} failures={failure_count}'.format(
                **summary,
                unresolved_count=len(summary['unresolved']),
                failure_count=len(summary['failures']),
            )
        )
        if summary['unresolved']:
            self.stdout.write(
                self.style.WARNING(
                    'org-memberships: roster identities missing from Shizuha ID: '
                    + ', '.join(summary['unresolved'])
                )
            )
        if summary['failures']:
            # HIVE-2407: include the per-user detail (the captured 4xx reason) —
            # email:status alone made recurring sync failures undiagnosable.
            detail = '; '.join(
                f"{row['email']}:{row['status']}: {row.get('detail', '')}".rstrip(': ')
                for row in summary['failures']
            )
            raise CommandError(f'Organization membership reconcile failed: {detail}')
        return summary

    def _read_snapshot_file(self, path):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except OSError as exc:
            raise CommandError(f'Cannot read snapshot file {path}: {exc}') from exc
        except json.JSONDecodeError as exc:
            raise CommandError(f'Invalid JSON snapshot file {path}: {exc}') from exc

    def _pulse_get(self, path, *, params=None):
        from fleet.pulse_auth import pulse_fleet_get
        return pulse_fleet_get(
            path, params=params,
            warning=lambda message: self.stdout.write(self.style.WARNING(message)),
        )

    def _fetch_connect_state(self):
        """Return Connect inputs with availability represented explicitly."""
        base = (os.environ.get(
            'CONNECT_INTERNAL_API_URL',
            'http://shizuha-connect.shizuha.svc.cluster.local:8019/api',
        ) or '').rstrip('/')
        try:
            resp = requests.get(
                f'{base}/internal/unread-recipients/',
                headers={'X-Internal-Service': 'shizuha-hive'},
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json() or {}
            # PLAT-4490: omission is not evidence of zero unread/recent DMs.
            # Connect must explicitly attest that this is the complete
            # authoritative snapshot; otherwise stops fail closed while safe
            # workload starts still proceed through the typed partial path.
            if (
                payload.get('complete') is not True
                or payload.get('lifecycle_delivery_complete') is not True
            ):
                raise ValueError('Connect lifecycle snapshot is incomplete')
            rows = payload.get('recipients') or []
        except Exception as exc:  # noqa: BLE001 — typed partial outcome below
            return ConnectLifecycleSnapshot(
                available=False,
                unread_by_email={},
                last_dm_at_by_email={},
                error=f'{exc.__class__.__name__}: {exc}',
            )
        unread = {}
        last_dm_at = {}
        for row in rows:
            email = str(row.get('user_email') or '').strip().lower()
            if not email:
                continue
            unread[email] = unread.get(email, 0) + int(
                row.get('pending_delivery_total') or 0
            )
            timestamp = row.get('last_dm_at') or ''
            if timestamp:
                last_dm_at[email] = str(timestamp)
        return ConnectLifecycleSnapshot(
            available=True,
            unread_by_email=unread,
            last_dm_at_by_email=last_dm_at,
        )

    def _converge_running_native_release(self, agents, *, skip_ids):
        """Idle-fenced native generation roll for every live home, not only holders.

        Publishing ``hive-native-runtime-release`` is the event; this command
        is the existing actuator. Lease-keeper converge is not enough: idle
        non-holders keep their homes and were otherwise stranded on the
        previous generation until a later wake.
        """
        skip_ids = {str(item) for item in (skip_ids or set()) if item}
        ordered = sorted(
            agents or [],
            key=lambda agent: 0 if str(agent.get('activity_state') or '') == 'idle' else 1,
        )
        for agent in ordered:
            aid = str(agent.get('id') or '')
            uname = agent_username(agent)
            if not aid or aid in skip_ids or not agent_running(agent):
                continue
            replicas = observed_native_replicas(uname)
            if replicas is None or replicas <= 0:
                continue
            ok, detail = converge_running_lifecycle_agent(aid)
            self.stdout.write(
                f'native-release: converge {uname} replicas={replicas} '
                f'ok={bool(ok)} converged={bool(isinstance(detail, dict) and detail.get("converged"))}'
            )

    def _apply_health_signals(self, agents):
        """Enrich reconciler agents with provider health and heartbeat outcome.

        Fails OPEN deliberately: if telemetry is unreachable the reconciler
        keeps its previous behaviour (running == capacity) rather than
        refusing to reconcile. That can under-report a dead seat for one pass,
        which is strictly better than halting on-demand starts entirely.
        """
        try:
            from ...agent_health import apply_provider_unavailable_health
            from ...external_agents import agent_telemetry, telemetry_fields

            telemetry = agent_telemetry()
            for agent in agents:
                snapshot = telemetry.get(agent_username(agent))
                if snapshot:
                    # The Agents API already applies this projection. Apply the
                    # same source to the lifecycle decision path so structured
                    # queue_empty/all_blocked outcomes cannot exist in the UI
                    # while remaining invisible to scale-down.
                    agent.update(telemetry_fields(snapshot, configured=agent))
            apply_provider_unavailable_health(agents, telemetry)
            from ...lifecycle_fleet import stamp_native_runtime_ready
            stamp_native_runtime_ready(agents)
        except Exception as exc:  # noqa: BLE001 — health must never break reconciliation
            self.stdout.write(self.style.WARNING(
                f'health-signals: skipped ({exc}); '
                f'reconciling without provider-unavailable data (PLAT-5265)'
            ))
        return agents

    def _fetch_pulse_rosters(self):
        payload = self._pulse_get('/admin/team-roster/')
        return _items(payload)

    def _fetch_pulse_snapshot(self):
        # Lifecycle demand spans every Pulse organization.  The snapshot
        # endpoint defaults to the primary org, which silently omitted queues
        # such as ``digital-accounting`` and left their stopped seats asleep.
        return self._pulse_get(
            '/admin/org-chart/snapshot/?organization_slug=all'
        ) or {}
