"""Grant or revoke exact-team credential eligibility for NAMED seats only.

PLAT-10906 bootstrap-gap origin fix. The whole-roster reconcile
(``reconcile_team_credential_materialization``) is the only other writer of
``FleetAgentTeamCredentialEligibility`` and it is all-or-nothing across the
CURRENT Pulse TeamRoster: one roster member without a FleetAgent row aborts
the entire transaction, which is why the projection has been empty since
bootstrap. This command is the bounded writer: it touches only the seats
named on the command line, never reads or mints secrets, never mutates
HiveCredential grants, and never mutates roster membership.

Trust model (unchanged, per the model docstring): Pulse TeamRoster membership
and an eligibility row are independent gates — neither alone grants anything.
This command writes ONLY the opt-in half for exact seats; team-scoped
credential materialization still requires an active team-scoped HiveCredential
row plus live roster membership at read time
(``fleet.views._credential_materialization_contract``).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fleet.models import (
    FleetAgent,
    FleetAgentTeamCredentialEligibility,
    FleetAgentTeamCredentialEligibilityAudit,
)


def _canon(value: str) -> str:
    return str(value or '').strip().lower().replace('_', '-').replace(' ', '-')


class Command(BaseCommand):
    help = (
        'Grant or revoke exact-team credential eligibility for named FleetAgent '
        'seats (bounded bootstrap writer; does not touch credentials, secrets, '
        'or roster membership).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--agent-username', action='append', dest='agent_usernames', default=[],
            help='FleetAgent agent_username to grant/revoke (repeatable).',
        )
        parser.add_argument('--team-slug', required=True)
        parser.add_argument('--organization-slug', default='shizuha')
        parser.add_argument('--actor', required=True,
                            help='Audit attribution, e.g. "origin-ci:PLAT-10906" or an operator id.')
        parser.add_argument('--reason', required=True)
        parser.add_argument('--revoke', action='store_true',
                            help='Revoke (deactivate) instead of grant.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report the planned changes without writing.')

    def handle(self, *args, **options):
        usernames = sorted({
            str(value or '').strip().lower()
            for value in (options['agent_usernames'] or [])
            if str(value or '').strip()
        })
        if not usernames:
            raise CommandError('at least one --agent-username is required')
        organization_slug = _canon(options['organization_slug'])
        team_slug = _canon(options['team_slug'])
        actor = str(options['actor']).strip()
        reason = str(options['reason']).strip()
        revoke = bool(options['revoke'])
        dry_run = bool(options['dry_run'])
        if not organization_slug or not team_slug or not actor or not reason:
            raise CommandError('team, organization, actor, and reason must be non-empty')
        desired_active = not revoke

        with transaction.atomic():
            agents = {
                row.agent_username: row
                for row in FleetAgent.objects.select_for_update().filter(
                    agent_username__in=usernames,
                )
            }
            missing = sorted(set(usernames) - set(agents))
            if missing:
                raise CommandError(
                    'No FleetAgent row(s) for: ' + ','.join(missing)
                )

            changes = 0
            unchanged = 0
            for username in usernames:
                agent = agents[username]
                row = (
                    FleetAgentTeamCredentialEligibility.objects
                    .select_for_update()
                    .select_related('agent')
                    .filter(
                        agent=agent,
                        organization_slug=organization_slug,
                        team_slug=team_slug,
                    )
                    .first()
                )
                if row is None:
                    if not desired_active:
                        # Nothing to revoke; do not mint a tombstone row —
                        # matches the whole-roster reconcile's semantics
                        # (it never creates inactive rows for absent pairs).
                        self.stdout.write(f'{username}: no row; revoke is a no-op')
                        unchanged += 1
                        continue
                    self.stdout.write(
                        f'{username}: {"GRANT" if not dry_run else "would GRANT"} '
                        f'{organization_slug}/{team_slug}'
                    )
                    if dry_run:
                        changes += 1
                        continue
                    row = FleetAgentTeamCredentialEligibility.objects.create(
                        agent=agent,
                        organization_slug=organization_slug,
                        team_slug=team_slug,
                        is_active=True,
                        actor_user_id=actor,
                        reason=reason,
                    )
                    FleetAgentTeamCredentialEligibilityAudit.objects.create(
                        eligibility=row,
                        action=FleetAgentTeamCredentialEligibilityAudit.Action.GRANT,
                        actor_user_id=actor,
                        agent_username=username,
                        organization_slug=organization_slug,
                        team_slug=team_slug,
                        redacted_before={'active': False},
                        redacted_after={'active': True},
                        message=reason,
                    )
                    changes += 1
                    continue
                if row.is_active == desired_active:
                    self.stdout.write(f'{username}: already active={row.is_active}; unchanged')
                    unchanged += 1
                    continue
                self.stdout.write(
                    f'{username}: {"REVOKE" if revoke else "GRANT"}'
                    f'{"" if dry_run else " (applied)"} {organization_slug}/{team_slug}'
                )
                if dry_run:
                    changes += 1
                    continue
                before = row.is_active
                row.is_active = desired_active
                row.actor_user_id = actor
                row.reason = reason
                row.save(update_fields=['is_active', 'actor_user_id', 'reason', 'updated_at'])
                FleetAgentTeamCredentialEligibilityAudit.objects.create(
                    eligibility=row,
                    action=(
                        FleetAgentTeamCredentialEligibilityAudit.Action.REVOKE
                        if revoke
                        else FleetAgentTeamCredentialEligibilityAudit.Action.GRANT
                    ),
                    actor_user_id=actor,
                    agent_username=username,
                    organization_slug=organization_slug,
                    team_slug=team_slug,
                    redacted_before={'active': before},
                    redacted_after={'active': desired_active},
                    message=reason,
                )
                changes += 1

        self.stdout.write(
            f'done: {changes} change(s), {unchanged} unchanged, '
            f'org={organization_slug} team={team_slug} '
            f'mode={"revoke" if revoke else "grant"}'
        )
