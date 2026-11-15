"""PLAT-10906: bounded exact-seat eligibility writer + fail-loud census.

The whole-roster reconcile aborts its ENTIRE transaction when any current
roster member lacks a FleetAgent row — the bootstrap gap that left the
projection empty since inception. These tests pin the bounded writer's
contract: named seats only, idempotent re-runs, audited changes, no
credential/secret surface, and a census that goes RED exactly on the
aged-populated-empty signature.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from fleet.models import (
    FleetAgent,
    FleetAgentTeamCredentialEligibility,
    FleetAgentTeamCredentialEligibilityAudit,
)


def _make_agent(username, status='running'):
    return FleetAgent.objects.create(
        agent_username=username,
        agent_id=f'{username}-id',
        role='QA',
        status=status,
    )


class GrantTeamCredentialEligibilityTests(TestCase):
    def test_grant_creates_row_and_audit_for_named_seats_only(self):
        nova = _make_agent('nova')
        _make_agent('unrelated-seat')
        stdout = StringIO()
        call_command(
            'grant_team_credential_eligibility',
            '--agent-username=nova',
            '--team-slug=qa',
            '--actor=origin-ci:PLAT-10906',
            '--reason=PLAT-10906 sanctioned seat bootstrap',
            stdout=stdout,
        )
        rows = FleetAgentTeamCredentialEligibility.objects.all()
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.agent, nova)
        self.assertEqual(row.organization_slug, 'shizuha')
        self.assertEqual(row.team_slug, 'qa')
        self.assertTrue(row.is_active)
        audit = FleetAgentTeamCredentialEligibilityAudit.objects.get(eligibility=row)
        self.assertEqual(audit.action, 'grant')
        self.assertEqual(audit.actor_user_id, 'origin-ci:PLAT-10906')
        self.assertEqual(audit.agent_username, 'nova')
        self.assertEqual(audit.redacted_after, {'active': True})

    def test_replay_is_idempotent_without_audit_noise(self):
        _make_agent('nova')
        call_command(
            'grant_team_credential_eligibility',
            '--agent-username=nova', '--team-slug=qa',
            '--actor=origin-ci:PLAT-10906', '--reason=bootstrap',
        )
        call_command(
            'grant_team_credential_eligibility',
            '--agent-username=nova', '--team-slug=qa',
            '--actor=origin-ci:PLAT-10906', '--reason=bootstrap',
        )
        self.assertEqual(FleetAgentTeamCredentialEligibility.objects.count(), 1)
        self.assertEqual(FleetAgentTeamCredentialEligibilityAudit.objects.count(), 1)

    def test_revoke_then_regrant_audits_both_directions(self):
        _make_agent('nova')
        call_command(
            'grant_team_credential_eligibility',
            '--agent-username=nova', '--team-slug=qa',
            '--actor=origin-ci:PLAT-10906', '--reason=bootstrap',
        )
        call_command(
            'grant_team_credential_eligibility',
            '--agent-username=nova', '--team-slug=qa',
            '--actor=origin-ci:PLAT-10906', '--reason=left the team',
            '--revoke',
        )
        row = FleetAgentTeamCredentialEligibility.objects.get()
        self.assertFalse(row.is_active)
        actions = list(
            FleetAgentTeamCredentialEligibilityAudit.objects.order_by('created_at')
            .values_list('action', flat=True)
        )
        self.assertEqual(actions, ['grant', 'revoke'])
        # Rows are toggled, never deleted — the ledger stays durable.
        self.assertEqual(FleetAgentTeamCredentialEligibility.objects.count(), 1)
        call_command(
            'grant_team_credential_eligibility',
            '--agent-username=nova', '--team-slug=qa',
            '--actor=origin-ci:PLAT-10906', '--reason=re-joined',
        )
        self.assertTrue(FleetAgentTeamCredentialEligibility.objects.get().is_active)
        actions = list(
            FleetAgentTeamCredentialEligibilityAudit.objects.order_by('created_at')
            .values_list('action', flat=True)
        )
        self.assertEqual(actions, ['grant', 'revoke', 'grant'])

    def test_revoke_of_never_granted_seat_is_a_noop_without_tombstone(self):
        _make_agent('nova')
        call_command(
            'grant_team_credential_eligibility',
            '--agent-username=nova', '--team-slug=qa',
            '--actor=origin-ci:PLAT-10906', '--reason=cleanup',
            '--revoke',
        )
        self.assertEqual(FleetAgentTeamCredentialEligibility.objects.count(), 0)
        self.assertEqual(FleetAgentTeamCredentialEligibilityAudit.objects.count(), 0)

    def test_unknown_seat_fails_loud_and_writes_nothing(self):
        _make_agent('nova')
        with self.assertRaises(CommandError) as ctx:
            call_command(
                'grant_team_credential_eligibility',
                '--agent-username=nova', '--agent-username=ghost',
                '--team-slug=qa',
                '--actor=origin-ci:PLAT-10906', '--reason=bootstrap',
            )
        self.assertIn('ghost', str(ctx.exception))
        self.assertEqual(FleetAgentTeamCredentialEligibility.objects.count(), 0)
        self.assertEqual(FleetAgentTeamCredentialEligibilityAudit.objects.count(), 0)

    def test_does_not_touch_credentials_or_other_teams(self):
        nova = _make_agent('nova')
        call_command(
            'grant_team_credential_eligibility',
            '--agent-username=nova', '--team-slug=qa',
            '--actor=origin-ci:PLAT-10906', '--reason=bootstrap',
        )
        from fleet.models import HiveCredential, HiveCredentialAudit
        self.assertEqual(HiveCredential.objects.count(), 0)
        self.assertEqual(HiveCredentialAudit.objects.count(), 0)
        # Slug canonicalization: mixed-case/underscore input lands canonical.
        call_command(
            'grant_team_credential_eligibility',
            '--agent-username=nova', '--team-slug=QA_Team',
            '--organization-slug=Shizuha',
            '--actor=origin-ci:PLAT-10906', '--reason=canonicalization check',
        )
        teams = set(
            FleetAgentTeamCredentialEligibility.objects.filter(agent=nova)
            .values_list('team_slug', flat=True)
        )
        self.assertEqual(teams, {'qa', 'qa-team'})


class CensusTeamCredentialEligibilityTests(TestCase):
    def _populate_fleet(self, count, age_days=30, status='running', prefix='seat'):
        created = timezone.now() - timedelta(days=age_days)
        for index in range(count):
            agent = _make_agent(f'{prefix}-{index:03d}', status=status)
            # auto_now_add pins real now; align the census age window via update.
            FleetAgent.objects.filter(pk=agent.pk).update(created_at=created)

    def test_red_on_aged_populated_empty_projection(self):
        self._populate_fleet(12)
        with self.assertRaises(SystemExit) as ctx:
            call_command('census_team_credential_eligibility', stderr=StringIO())
        self.assertEqual(ctx.exception.code, 2)

    def test_green_once_projection_has_rows(self):
        self._populate_fleet(12)
        _make_agent('nova')
        call_command(
            'grant_team_credential_eligibility',
            '--agent-username=nova', '--team-slug=qa',
            '--actor=origin-ci:PLAT-10906', '--reason=bootstrap',
        )
        stdout = StringIO()
        call_command('census_team_credential_eligibility', stdout=stdout)
        self.assertIn('GREEN', stdout.getvalue())

    def test_skips_young_fleet(self):
        self._populate_fleet(12, age_days=1)
        stdout = StringIO()
        call_command('census_team_credential_eligibility', stdout=stdout)
        self.assertIn('skipped', stdout.getvalue())

    def test_skips_small_fleet_and_ignores_non_live_statuses(self):
        self._populate_fleet(5, prefix='live')
        self._populate_fleet(12, status='stopped', prefix='stopped')
        stdout = StringIO()
        call_command('census_team_credential_eligibility', stdout=stdout)
        self.assertIn('skipped', stdout.getvalue())
