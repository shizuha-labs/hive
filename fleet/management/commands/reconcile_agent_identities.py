"""HIVE-599: reconcile Shizuha-ID account_type for Hive-managed agents.

Hive is the source of truth for which accounts are agents (they are managed
FleetAgent rows). When an agent's ID user is mis-tagged ``account_type='human'``
it drops out of the ID agent inventory and the Hive UI shows a false
"Identity drift" badge (identity_unresolved). This command sweeps managed
FleetAgents, finds ID users that exist + are active but are NOT tagged
``agent``, and asks ID to correct the classification via the internal
``reconcile_account_type`` action (staff/service-token gated on the ID side).

It NEVER creates ID users and NEVER touches ownership/credentials. Stale,
stopped, never-provisioned rows are retired through Hive's durable tombstone
path after a safety grace period so daemon snapshots cannot recreate them.

Run on-demand or from the reconcile CronJob (hourly). Idempotent: a
already-correct account reports changed=false and no write happens.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from fleet.external_agents import _id_get, _id_internal_base
import requests

logger = logging.getLogger(__name__)


def _reconcile_actor_id() -> int | None:
    """The service actor id ID expects. Defaults to the operator/system id."""
    raw = getattr(settings, 'HIVE_RECONCILE_ACTOR_USER_ID', None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = 'Reconcile ID account_type=agent for Hive-managed agents (HIVE-599).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reap-archived-tombstones', action='store_true',
            help='Retire archived + never-provisioned identity orphans through '
                 'Hive durable tombstones (compatibility flag).')
        parser.add_argument(
            '--reap-stale-orphans', action='store_true',
            help='Retire stopped + never-provisioned identity orphans older than '
                 'the safety grace period through Hive durable tombstones.')
        parser.add_argument(
            '--orphan-grace-hours', type=int, default=24,
            help='Minimum FleetAgent row age for --reap-stale-orphans (default: 24).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report mismatches without asking ID to fix them.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Cap the number of reconciles this run (0 = no cap).')

    def handle(self, *args, **opts):
        from fleet.models import FleetAgent, FleetAgentEvent

        dry = opts['dry_run']
        limit = opts['limit'] or 0
        base = _id_internal_base()
        if not base:
            self.stderr.write('id internal base not configured; aborting')
            return

        # Auth is network-trust (in-cluster call to ID's ingress-guarded internal
        # endpoint); a staff actor id or service token further authorize if set.
        actor_id = _reconcile_actor_id()
        service_token = getattr(settings, 'INTERNAL_SERVICE_TOKEN', '') or ''

        agents_by_username = {
            a.agent_username: a
            for a in FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED)
            if a.agent_username
        }
        usernames = sorted(agents_by_username)

        # One bulk read of the current agent inventory (account_type=agent set).
        inv = _id_get('/api/internal/agents/list/', {'active': 'true'}) or {}
        agent_usernames = {
            (r.get('username') or '').strip().lower()
            for r in (inv.get('agents') or [])
        }

        mistagged, orphans, inactive, reconciled, failed = [], [], [], [], []
        for un in usernames:
            if un.lower() in agent_usernames:
                continue  # already a proper agent account — nothing to do
            # Not in the agent inventory: either a mis-tag (user exists, wrong
            # type) or a true orphan (no user). Probe the per-user endpoint.
            rec = _id_get('/api/internal/agents/', {'username': un}) or {}
            if not rec.get('exists'):
                orphans.append(un)
                continue
            if not rec.get('is_active', True):
                # An inactive identity can be a deliberate administrative stop.
                # Hive remains authoritative for agent existence, so preserve it
                # for explicit recovery instead of treating it as a missing user.
                inactive.append(un)
                continue
            mistagged.append(un)

        self.stdout.write(
            f'managed={len(usernames)} '
            f'already_agent={len(usernames) - len(mistagged) - len(orphans) - len(inactive)} '
            f'mistagged={len(mistagged)} orphans={len(orphans)} inactive={len(inactive)}')
        if orphans:
            self.stdout.write(
                f'IDENTITY ORPHANS (no ID user — retired when lifecycle safety gates pass): {orphans}'
            )
        if inactive:
            self.stdout.write(
                f'INACTIVE IDENTITIES (preserved; Hive existence remains authoritative): {inactive}'
            )

        # Hive is the desired-state authority. Retire eligible identity orphans
        # through the same durable tombstone path as the DELETE API; physically
        # deleting the FleetAgent row first lets the next daemon snapshot recreate
        # it. Archived rows are immediately eligible. Unarchived rows must be
        # stopped, never-provisioned, and older than the grace period.
        if (opts.get('reap_archived_tombstones') or opts.get('reap_stale_orphans')) and orphans:
            from fleet.models import FleetAgentArchive
            archived_set = {a.agent_username.lower() for a in FleetAgentArchive.objects.all()}
            grace_hours = max(1, int(opts.get('orphan_grace_hours') or 24))
            cutoff = timezone.now() - timedelta(hours=grace_hours)
            reaped = []
            for un in orphans:
                agent = agents_by_username.get(un)
                if not agent:
                    continue
                is_archived = un.lower() in archived_set
                never_prov = agent.provisioned_at is None
                stopped = str(agent.status) in {
                    FleetAgent.Status.HIBERNATED,
                    FleetAgent.Status.STOPPED,
                }
                stale = bool(agent.created_at and agent.created_at <= cutoff)
                eligible_archived = (
                    opts.get('reap_archived_tombstones')
                    and is_archived and never_prov and stopped
                )
                eligible_stale = (
                    opts.get('reap_stale_orphans')
                    and stale and never_prov and stopped
                )
                if not (eligible_archived or eligible_stale):
                    continue
                if dry:
                    self.stdout.write(f'[dry-run] would retire identity orphan {un}')
                    reaped.append(un)
                    continue
                from fleet.lifecycle import retire_fleet_agent
                retire_fleet_agent(
                    agent,
                    actor='hive-identity-reconciler',
                    reason=(
                        'archived identity orphan retired'
                        if is_archived else
                        f'stale identity orphan retired after {grace_hours}h grace'
                    ),
                )
                reaped.append(un)
                logger.info('retired identity orphan %s', un)
            if reaped:
                self.stdout.write(f'RETIRED identity orphans: {reaped}')

        for un in mistagged:
            if dry:
                self.stdout.write(f'[dry-run] would reconcile account_type=agent for {un}')
                continue
            if limit and len(reconciled) >= limit:
                break
            body = {'username': un, 'action': 'reconcile_account_type'}
            if actor_id:
                body['actor_user_id'] = actor_id
            headers = {'X-Internal-Service-Token': service_token} if service_token else {}
            try:
                r = requests.patch(f'{base}/api/internal/agents/', json=body,
                                    headers=headers, timeout=8)
                if r.status_code >= 400:
                    failed.append((un, r.status_code, r.text[:120]))
                    logger.warning('reconcile %s failed: %s %s', un, r.status_code, r.text[:120])
                else:
                    try:
                        response_body = r.json() if r.content else {}
                    except ValueError:
                        response_body = {}
                    changed = bool(response_body.get('changed')) if isinstance(response_body, dict) else True
                    FleetAgentEvent.objects.create(
                        agent=agents_by_username[un],
                        event_type='identity_reconciled',
                        message=f'Shizuha-ID account_type reconciled to agent (changed={changed}).',
                    )
                    reconciled.append(un)
                    logger.info('reconciled account_type=agent for %s', un)
            except Exception as exc:
                failed.append((un, 'exc', str(exc)))
                logger.warning('reconcile %s raised: %s', un, exc)

        self.stdout.write(f'reconciled={reconciled}')
        if failed:
            self.stderr.write(f'FAILED={failed}')
