from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from fleet.models import (
    FleetAgent,
    FleetAgentTeamCredentialEligibility,
    FleetAgentTeamCredentialEligibilityAudit,
    HiveCredential,
)


@override_settings(HIVE_PULSE_API_URL='http://pulse/api')
class TeamCredentialReconcileTests(TestCase):
    roster = ['agy', 'mika', 'nami', 'nova', 'zen']

    def setUp(self):
        for username in [*self.roster, 'reika']:
            FleetAgent.objects.create(
                agent_username=username,
                agent_id=f'{username}-id',
                role='QA' if username in self.roster else 'Reviewer',
            )

    @staticmethod
    def _response(usernames):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'teams': [{
                'team_name': 'qa',
                'organization_slug': 'shizuha',
                'tiers': [[{'email': f'{username}@shizuha.com'} for username in usernames]],
                'junior_emails': [],
            }],
        }
        return response

    def _run(self, mode='provision'):
        stdout = StringIO()
        with patch(
            'fleet.management.commands.reconcile_team_credential_materialization.requests.get',
            return_value=self._response(self.roster),
        ):
            call_command(
                'reconcile_team_credential_materialization',
                '--purpose=QA_ISOLATION_A_USERNAME',
                '--purpose=QA_ISOLATION_A_PASSWORD',
                mode=mode,
                team_slug='qa',
                organization_slug='shizuha',
                secret_namespace='shizuha-fleet',
                secret_name='qa-isolation-fixtures',
                actor='origin-ci:PLAT-5699',
                reason='PLAT-5699 test fixture',
                stdout=stdout,
            )
        self.assertIn('credentials_exposed=0', stdout.getvalue())

    def test_current_roster_only_gets_exact_qa_eligibility_and_rollback_is_scoped(self):
        reika = FleetAgent.objects.get(agent_username='reika')
        review_row = FleetAgentTeamCredentialEligibility.objects.create(
            agent=reika,
            organization_slug='shizuha',
            team_slug='review',
            is_active=True,
            actor_user_id='test',
        )
        stale_qa = FleetAgentTeamCredentialEligibility.objects.create(
            agent=reika,
            organization_slug='shizuha',
            team_slug='qa',
            is_active=True,
            actor_user_id='stale-test',
        )
        customer_qa = FleetAgentTeamCredentialEligibility.objects.create(
            agent=reika,
            organization_slug='customer-x',
            team_slug='qa',
            is_active=True,
            actor_user_id='customer-test',
        )
        customer_grant = HiveCredential.objects.create(
            scope=HiveCredential.Scope.TEAM,
            organization_slug='customer-x',
            team_slug='qa',
            provider=HiveCredential.Provider.GENERIC_ENV,
            purpose='QA_ISOLATION_A_USERNAME',
            secret_ref='k8s-secret://customer-x/customer-qa#QA_ISOLATION_A_USERNAME',
            created_by_user_id='customer-test',
        )

        self._run('provision')
        active_qa = set(
            FleetAgentTeamCredentialEligibility.objects.filter(
                organization_slug='shizuha', team_slug='qa', is_active=True,
            ).values_list('agent__agent_username', flat=True)
        )
        self.assertEqual(active_qa, set(self.roster))
        stale_qa.refresh_from_db()
        self.assertFalse(stale_qa.is_active)
        review_row.refresh_from_db()
        self.assertTrue(review_row.is_active)
        customer_qa.refresh_from_db()
        self.assertTrue(customer_qa.is_active)
        customer_grant.refresh_from_db()
        self.assertTrue(customer_grant.is_active)
        self.assertEqual(
            set(HiveCredential.objects.filter(
                organization_slug='shizuha', is_active=True,
            ).values_list('purpose', flat=True)),
            {'QA_ISOLATION_A_USERNAME', 'QA_ISOLATION_A_PASSWORD'},
        )

        self._run('rollback')
        self.assertFalse(
            FleetAgentTeamCredentialEligibility.objects.filter(
                organization_slug='shizuha', team_slug='qa', is_active=True,
            ).exists()
        )
        review_row.refresh_from_db()
        self.assertTrue(review_row.is_active)
        customer_qa.refresh_from_db()
        self.assertTrue(customer_qa.is_active)
        customer_grant.refresh_from_db()
        self.assertTrue(customer_grant.is_active)
        self.assertFalse(HiveCredential.objects.filter(
            organization_slug='shizuha', is_active=True,
        ).exists())
        self.assertGreaterEqual(FleetAgentTeamCredentialEligibilityAudit.objects.count(), 11)

    def test_unknown_roster_member_fails_without_partial_writes(self):
        with patch(
            'fleet.management.commands.reconcile_team_credential_materialization.requests.get',
            return_value=self._response([*self.roster, 'missing-seat']),
        ):
            with self.assertRaisesMessage(CommandError, 'missing-seat'):
                call_command(
                    'reconcile_team_credential_materialization',
                    '--purpose=QA_ISOLATION_A_USERNAME',
                    mode='provision',
                    team_slug='qa',
                    organization_slug='shizuha',
                    secret_namespace='shizuha-fleet',
                    secret_name='qa-isolation-fixtures',
                    actor='origin-ci:PLAT-5699',
                    reason='PLAT-5699 test fixture',
                )
        self.assertFalse(FleetAgentTeamCredentialEligibility.objects.exists())
        self.assertFalse(HiveCredential.objects.exists())
