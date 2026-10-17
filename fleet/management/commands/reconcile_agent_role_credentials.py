"""Role-scoped credential projection reconciler (PLAT-5372).

## The gap it closes

PLAT-5371 delivered the accounting-portal credentials to the accounting seat
(``banto``) by projecting the material into ``banto-agent-creds`` — the per-seat
Secret the daemon renderer owns. That survives routine re-renders (3-way merge
preserves keys the renderer does not name) but NOT a deprovision/reprovision:
``fleet/provisioner.py`` deletes the seat Secret on teardown, and the renderer
recreates it with only AGENT_PASSWORD/GITHUB_TOKEN/CONTEXT_PROMPT. Nothing
re-injects the accounting-portal keys, so the accounting seat silently loses
them — the exact "counted, not checked" class PLAT-5045's auditor exists to
close.

## How it works (durable)

Team-slug based and resolved from the Pulse TeamRoster the way ``WRITE_TEAMS``
is in ``reconcile_agent_github_identity`` — a seat gets a scope by construction
from its team, never by a hand-maintained per-agent list.

For every active seat whose team carries a scope (see
``fleet.provisioner.ROLE_CREDENTIAL_SCOPES``), this command calls
``ensure_agent_role_credentials`` which PATCHes the scope's keys from the single
shared fleet-namespace Secret into the seat's ``<user>-agent-creds`` Secret.
Credential bytes stay in one object; the grant is declarative and team-resolved;
a reprovision re-establishes it (the provision path calls the same helper).

Dry-run by default; ``--apply`` persists. Kill-switch
``HIVE_ROLE_CREDENTIALS_RECONCILE_ENABLED=0``.
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from fleet.models import FleetAgent
from fleet.on_demand import memberships_by_email
from fleet.provisioner import ROLE_CREDENTIAL_SCOPES, ensure_agent_role_credentials


class Command(BaseCommand):
    help = (
        'Re-establish role-scoped credential projections (PLAT-5372) on every '
        'active seat whose team carries a scope. Dry-run unless --apply.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Persist projections (default: dry-run).')
        parser.add_argument('--only', default='', help='Comma-separated agent usernames to limit to.')
        parser.add_argument('--namespace', default='', help='Override the fleet namespace (default: settings).')

    def handle(self, *args, **opts):
        if os.environ.get('HIVE_ROLE_CREDENTIALS_RECONCILE_ENABLED', '1') != '1':
            self.stdout.write('HIVE_ROLE_CREDENTIALS_RECONCILE_ENABLED=0 — skipping')
            return
        apply = opts['apply']
        only = {u.strip().lower() for u in opts['only'].split(',') if u.strip()}
        if not ROLE_CREDENTIAL_SCOPES:
            self.stdout.write('No role credential scopes configured — nothing to reconcile.')
            return

        rosters = self._fetch_pulse_rosters()
        if not rosters:
            raise CommandError('No Pulse rosters available; refusing to reconcile role credentials.')
        email_to_teams = memberships_by_email(rosters)

        projected = skipped = 0
        for agent in FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED).order_by('agent_username'):
            uname = (agent.agent_username or '').strip().lower()
            if not uname or (only and uname not in only):
                continue
            if not apply:
                # Dry-run: report which scopes WOULD apply, without touching k8s.
                from fleet.provisioner import _agent_teams_from_roster
                teams = _agent_teams_from_roster(agent, email_to_teams)
                wanted = [scope for scope, spec in ROLE_CREDENTIAL_SCOPES.items()
                          if set(spec.get('teams') or set()) & teams]
                self.stdout.write(f'[dry-run] {uname} teams={sorted(teams) or "-"} -> scopes={sorted(wanted) or "-"}')
                continue
            try:
                done = ensure_agent_role_credentials(agent, email_to_teams=email_to_teams)
            except Exception as exc:  # noqa: BLE001 — one bad seat must not stop the sweep
                self.stdout.write(self.style.WARNING(f'  {uname}: failed ({exc})'))
                skipped += 1
                continue
            if done:
                projected += 1
                self.stdout.write(self.style.SUCCESS(f'{uname}: projected scopes {sorted(done)}'))
            else:
                skipped += 1

        mode = 'APPLY' if apply else 'DRY-RUN'
        self.stdout.write(f'{mode}: projected={projected} skipped/no-scope={skipped}')

    def _fetch_pulse_rosters(self):
        from fleet.pulse_auth import pulse_fleet_rosters
        return pulse_fleet_rosters()
