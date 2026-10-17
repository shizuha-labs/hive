"""Reconcile exact-team credential eligibility and handle-only grants.

Pulse TeamRoster is the membership source of truth. This command deliberately
accepts no seat allow-list: CI names a team and credential handles, while Hive
resolves the current roster, writes exact-team eligibility, and appends redacted
audit events in one transaction.
"""

from __future__ import annotations

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from fleet.models import (
    FleetAgent,
    FleetAgentTeamCredentialEligibility,
    FleetAgentTeamCredentialEligibilityAudit,
    HiveCredential,
    HiveCredentialAudit,
)


def _canon(value: str) -> str:
    return str(value or '').strip().lower().replace('_', '-').replace(' ', '-')


def _member_email(member) -> str:
    if isinstance(member, dict):
        return str(member.get('email') or '').strip().lower()
    return str(member or '').strip().lower()


class Command(BaseCommand):
    help = 'Reconcile audited exact-team credential eligibility from Pulse TeamRoster.'

    def add_arguments(self, parser):
        parser.add_argument('--mode', choices=('provision', 'rollback'), default='provision')
        parser.add_argument('--team-slug', required=True)
        parser.add_argument('--organization-slug', default='shizuha')
        parser.add_argument('--secret-namespace', required=True)
        parser.add_argument('--secret-name', required=True)
        parser.add_argument('--purpose', action='append', dest='purposes', required=True)
        parser.add_argument('--actor', required=True)
        parser.add_argument('--reason', required=True)

    def _fetch_team_usernames(self, *, team_slug: str, organization_slug: str) -> set[str]:
        base = getattr(settings, 'HIVE_PULSE_API_URL', '').rstrip('/')
        if not base:
            raise CommandError('HIVE_PULSE_API_URL is not configured')
        try:
            response = requests.get(f'{base}/internal/team-roster/', timeout=15)
            response.raise_for_status()
            payload = response.json() or {}
        except (requests.RequestException, ValueError) as exc:
            raise CommandError(f'Pulse TeamRoster read failed: {exc}') from exc

        matches = []
        for team in payload.get('teams') or []:
            if _canon(team.get('team_name') or team.get('team_slug')) != team_slug:
                continue
            row_org = _canon(team.get('organization_slug'))
            if row_org and row_org != organization_slug:
                continue
            matches.append(team)
        if len(matches) != 1:
            raise CommandError(
                f'Expected exactly one Pulse TeamRoster for {organization_slug}/{team_slug}; '
                f'found {len(matches)}'
            )

        emails = []
        for tier in matches[0].get('tiers') or []:
            emails.extend(_member_email(member) for member in (tier or []))
        emails.extend(_member_email(member) for member in (matches[0].get('junior_emails') or []))
        emails = sorted({email for email in emails if email})
        if not emails:
            raise CommandError(f'Pulse TeamRoster {organization_slug}/{team_slug} has no members')

        usernames = set()
        for email in emails:
            local, separator, domain = email.partition('@')
            if not separator or domain != 'shizuha.com' or not local:
                raise CommandError(
                    f'Pulse TeamRoster {organization_slug}/{team_slug} contains a non-seat identity: {email}'
                )
            usernames.add(local)
        return usernames

    @staticmethod
    def _audit_eligibility(eligibility, *, action, actor, before, after, message):
        FleetAgentTeamCredentialEligibilityAudit.objects.create(
            eligibility=eligibility,
            action=action,
            actor_user_id=actor,
            agent_username=eligibility.agent.agent_username,
            organization_slug=eligibility.organization_slug,
            team_slug=eligibility.team_slug,
            redacted_before={'active': before},
            redacted_after={'active': after},
            message=message,
        )

    def handle(self, *args, **options):
        mode = options['mode']
        team_slug = _canon(options['team_slug'])
        organization_slug = _canon(options['organization_slug'])
        purposes = sorted({str(value).strip() for value in options['purposes'] if str(value).strip()})
        actor = str(options['actor']).strip()
        reason = str(options['reason']).strip()
        if not team_slug or not organization_slug or not purposes or not actor or not reason:
            raise CommandError('team, organization, purpose, actor, and reason must be non-empty')

        roster_usernames = self._fetch_team_usernames(
            team_slug=team_slug,
            organization_slug=organization_slug,
        )
        desired_active = mode == 'provision'
        now = timezone.now()

        with transaction.atomic():
            agents = {
                row.agent_username: row
                for row in FleetAgent.objects.select_for_update().filter(
                    agent_username__in=roster_usernames,
                )
            }
            missing = sorted(roster_usernames - set(agents))
            if missing:
                raise CommandError(
                    'Pulse TeamRoster members lack FleetAgent rows: ' + ','.join(missing)
                )

            existing = {
                row.agent.agent_username: row
                for row in FleetAgentTeamCredentialEligibility.objects.select_for_update()
                .select_related('agent')
                .filter(organization_slug=organization_slug, team_slug=team_slug)
            }
            eligibility_changes = 0
            for username, agent in sorted(agents.items()):
                row = existing.get(username)
                if row is None:
                    row = FleetAgentTeamCredentialEligibility.objects.create(
                        agent=agent,
                        organization_slug=organization_slug,
                        team_slug=team_slug,
                        is_active=desired_active,
                        actor_user_id=actor,
                        reason=reason,
                    )
                    if desired_active:
                        self._audit_eligibility(
                            row,
                            action=FleetAgentTeamCredentialEligibilityAudit.Action.GRANT,
                            actor=actor,
                            before=False,
                            after=True,
                            message=reason,
                        )
                        eligibility_changes += 1
                    continue
                if row.is_active != desired_active:
                    before = row.is_active
                    row.is_active = desired_active
                    row.actor_user_id = actor
                    row.reason = reason
                    row.save(update_fields=['is_active', 'actor_user_id', 'reason', 'updated_at'])
                    self._audit_eligibility(
                        row,
                        action=(
                            FleetAgentTeamCredentialEligibilityAudit.Action.GRANT
                            if desired_active
                            else FleetAgentTeamCredentialEligibilityAudit.Action.REVOKE
                        ),
                        actor=actor,
                        before=before,
                        after=desired_active,
                        message=reason,
                    )
                    eligibility_changes += 1

            # Seats that left this exact roster lose only this team's eligibility.
            for username, row in sorted(existing.items()):
                if username in roster_usernames or not row.is_active:
                    continue
                row.is_active = False
                row.actor_user_id = actor
                row.reason = f'{reason}; no longer in authoritative roster'
                row.save(update_fields=['is_active', 'actor_user_id', 'reason', 'updated_at'])
                self._audit_eligibility(
                    row,
                    action=FleetAgentTeamCredentialEligibilityAudit.Action.REVOKE,
                    actor=actor,
                    before=True,
                    after=False,
                    message=row.reason,
                )
                eligibility_changes += 1

            grant_changes = 0
            grants = HiveCredential.objects.select_for_update().filter(
                scope=HiveCredential.Scope.TEAM,
                organization_slug=organization_slug,
                team_slug=team_slug,
                provider=HiveCredential.Provider.GENERIC_ENV,
                purpose__in=purposes,
                is_active=True,
            )
            if mode == 'rollback':
                for grant in grants:
                    grant.is_active = False
                    grant.revoked_at = now
                    grant.updated_by_user_id = actor
                    grant.save(update_fields=['is_active', 'revoked_at', 'updated_by_user_id', 'updated_at'])
                    HiveCredentialAudit.objects.create(
                        credential=grant,
                        actor_user_id=actor,
                        action=HiveCredentialAudit.Action.REVOKE,
                        target_scope='team',
                        target_team=f'{organization_slug}/{team_slug}',
                        redacted_before={'active': True, 'has_secret_ref': True},
                        redacted_after={'active': False, 'has_secret_ref': True},
                        message=reason,
                    )
                    grant_changes += 1
            else:
                active_by_purpose = {grant.purpose: grant for grant in grants}
                for purpose in purposes:
                    secret_ref = (
                        f'k8s-secret://{options["secret_namespace"]}/'
                        f'{options["secret_name"]}#{purpose}'
                    )
                    grant = active_by_purpose.get(purpose)
                    created = grant is None
                    before = {}
                    if created:
                        grant = HiveCredential.objects.create(
                            scope=HiveCredential.Scope.TEAM,
                            organization_slug=organization_slug,
                            team_slug=team_slug,
                            provider=HiveCredential.Provider.GENERIC_ENV,
                            purpose=purpose,
                            secret_ref=secret_ref,
                            requires_team_credential_eligible=True,
                            created_by_user_id=actor,
                            updated_by_user_id=actor,
                        )
                    else:
                        before = {
                            'active': grant.is_active,
                            'has_secret_ref': bool(grant.secret_ref),
                            'purpose': grant.purpose,
                        }
                        changed = (
                            grant.secret_ref != secret_ref
                            or not grant.requires_team_credential_eligible
                            or grant.revoked_at is not None
                        )
                        if not changed:
                            continue
                        grant.secret_ref = secret_ref
                        grant.requires_team_credential_eligible = True
                        grant.revoked_at = None
                        grant.updated_by_user_id = actor
                        grant.save(update_fields=[
                            'secret_ref', 'requires_team_credential_eligible',
                            'revoked_at', 'updated_by_user_id', 'updated_at',
                        ])
                    HiveCredentialAudit.objects.create(
                        credential=grant,
                        actor_user_id=actor,
                        action=(
                            HiveCredentialAudit.Action.CREATE
                            if created
                            else HiveCredentialAudit.Action.UPDATE
                        ),
                        target_scope='team',
                        target_team=f'{organization_slug}/{team_slug}',
                        redacted_before=before,
                        redacted_after={
                            'active': True,
                            'has_secret_ref': True,
                            'purpose': purpose,
                        },
                        message=reason,
                    )
                    grant_changes += 1

        self.stdout.write(
            f'team_credential_materialization mode={mode} '
            f'team={organization_slug}/{team_slug} '
            f'roster_seats={len(roster_usernames)} eligibility_changes={eligibility_changes} '
            f'grant_changes={grant_changes} credentials_exposed=0'
        )
