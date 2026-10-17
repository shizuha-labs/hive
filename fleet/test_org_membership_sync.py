from io import StringIO
import hashlib
import hmac
import json
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from .management.commands.reconcile_on_demand_agents import (
    Command,
    ConnectLifecycleSnapshot,
)
from .org_membership_sync import (
    _existing_org_member_emails,
    ensure_roster_org_membership,
    reconcile_roster_org_memberships,
    roster_org_edges,
)
from .personal_agents import wire_personal_agent_connectivity


def _response(status_code, payload=None):
    response = mock.Mock(status_code=status_code)
    response.json.return_value = payload if payload is not None else {}
    return response


@override_settings(
    SHIZUHA_ID_INTERNAL_URL='http://id',
    HIVE_ADMIN_API_URL='http://admin/api',
    ADMIN_INTERNAL_SERVICE_TOKEN='service-token',
    HIVE_SERVICE_SIGNING_KEY='dedicated-hive-key',
)
class OrgMembershipSyncTests(SimpleTestCase):
    @mock.patch('fleet.org_membership_sync.requests.get')
    def test_existing_members_are_loaded_once_per_organization(self, get):
        get.side_effect = [
            _response(200, {'id': 1, 'slug': 'shizuha'}),
            _response(200, {'members': [
                {'user_email': 'kumo@shizuha.com'},
                {'user_email': 'Sora@Shizuha.com'},
            ]}),
        ]

        emails, error = _existing_org_member_emails('shizuha')

        self.assertIsNone(error)
        self.assertEqual(emails, {'kumo@shizuha.com', 'sora@shizuha.com'})
        self.assertEqual(get.call_count, 2)

    @mock.patch('fleet.org_membership_sync.requests.post')
    @mock.patch('fleet.org_membership_sync.requests.get')
    def test_ensure_creates_only_parent_org_membership(self, get, post):
        get.side_effect = [
            _response(200, {'id': 440, 'username': 'kumo'}),
            _response(200, {'id': 1, 'slug': 'shizuha'}),
        ]
        post.return_value = _response(201, {'status': 'created'})

        result = ensure_roster_org_membership(
            email=' Kumo@Shizuha.com ',
            organization_slug='Shizuha',
            display_name='Kumo',
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, 'created')
        self.assertEqual(result.user_id, 440)
        self.assertEqual(result.organization_id, 1)
        post.assert_called_once()
        self.assertEqual(post.call_args.args, ('http://admin/api/internal/organizations/1/members/',))
        body = post.call_args.kwargs['data']
        self.assertIsInstance(body, bytes)
        self.assertNotIn('json', post.call_args.kwargs)
        self.assertEqual(json.loads(body), {
                'user_id': 440,
                'user_email': 'kumo@shizuha.com',
                'display_name': 'Kumo',
                'role': 'member',
                'operation_id': 'roster-org-membership:1:440',
        })
        headers = post.call_args.kwargs['headers']
        self.assertEqual(headers['X-Internal-Service-Token'], 'dedicated-hive-key')
        self.assertEqual(headers['X-Internal-Service'], 'shizuha-hive')
        self.assertEqual(headers['X-Hive-Body-Sha256'], hashlib.sha256(body).hexdigest())
        signed = '\n'.join(('POST', '/api/internal/organizations/1/members/',
                            headers['X-Hive-Timestamp'], headers['X-Hive-Nonce'],
                            headers['X-Hive-Body-Sha256']))
        self.assertEqual(headers['X-Hive-Signature'],
                         hmac.new(b'dedicated-hive-key', signed.encode(), hashlib.sha256).hexdigest())
        self.assertEqual(post.call_args.kwargs['timeout'], 8)

    @mock.patch('fleet.org_membership_sync.requests.post')
    @mock.patch('fleet.org_membership_sync.requests.get')
    def test_missing_identity_is_typed_and_never_posts_membership(self, get, post):
        get.return_value = _response(404, {'error': 'not found'})

        result = ensure_roster_org_membership(
            email='ghost@shizuha.com',
            organization_slug='shizuha',
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'identity_not_found')
        post.assert_not_called()

    @mock.patch('fleet.org_membership_sync.requests.post')
    @mock.patch('fleet.org_membership_sync.requests.get')
    def test_platform_agent_email_alias_resolves_by_username(self, get, post):
        get.side_effect = [
            _response(404, {'error': 'email not found'}),
            _response(200, {
                'exists': True,
                'user_id': 432,
                'username': 'goro',
                'email': 'goro@agents.shizuha.io',
                'account_type': 'agent',
                'is_active': True,
            }),
            _response(200, {'id': 1, 'slug': 'shizuha'}),
        ]
        post.return_value = _response(201, {'status': 'created'})

        result = ensure_roster_org_membership(
            email='goro@shizuha.com',
            organization_slug='shizuha',
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.user_id, 432)
        self.assertEqual(
            get.call_args_list[1],
            mock.call(
                'http://id/api/internal/agents/',
                params={'username': 'goro'},
                headers={
                    'Content-Type': 'application/json',
                    'X-Internal-Service': 'shizuha-hive',
                    'X-Internal-Service-Token': 'service-token',
                },
                timeout=8,
            ),
        )
        self.assertEqual(json.loads(post.call_args.kwargs['data'])['user_email'], 'goro@shizuha.com')

    @override_settings(HIVE_SERVICE_SIGNING_KEY='')
    @mock.patch('fleet.org_membership_sync.requests.post')
    @mock.patch('fleet.org_membership_sync.requests.get')
    def test_missing_signer_does_not_fall_back_to_read_service_token(self, get, post):
        get.side_effect = [_response(200, {'id': 440}), _response(200, {'id': 1})]
        result = ensure_roster_org_membership(email='kumo@shizuha.com', organization_slug='shizuha')
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'membership_signing_unavailable')
        post.assert_not_called()

    @mock.patch('fleet.org_membership_sync.requests.post')
    @mock.patch('fleet.org_membership_sync.requests.get')
    def test_repeated_edge_preserves_operation_and_renews_nonce(self, get, post):
        get.side_effect = [_response(200, {'id': 440}), _response(200, {'id': 1})] * 2
        post.return_value = _response(200, {'status': 'exists'})
        for _ in range(2):
            self.assertTrue(ensure_roster_org_membership(
                email='kumo@shizuha.com', organization_slug='shizuha').ok)
        first, second = [c.kwargs for c in post.call_args_list]
        self.assertEqual(first['data'], second['data'])
        self.assertNotEqual(first['headers']['X-Hive-Nonce'], second['headers']['X-Hive-Nonce'])
        self.assertNotEqual(first['headers']['X-Hive-Signature'], second['headers']['X-Hive-Signature'])

    @mock.patch('fleet.org_membership_sync.requests.post')
    @mock.patch('fleet.org_membership_sync.requests.get')
    def test_inactive_idempotent_receipt_is_not_reported_as_active_membership(self, get, post):
        get.side_effect = [_response(200, {'id': 440}), _response(200, {'id': 1})]
        post.return_value = _response(200, {'status': 'exists', 'already_applied': True, 'is_active': False})
        result = ensure_roster_org_membership(email='kumo@shizuha.com', organization_slug='shizuha')
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'membership_inactive')
        self.assertEqual(post.call_count, 1)

    @mock.patch('fleet.org_membership_sync.requests.post')
    @mock.patch('fleet.org_membership_sync.requests.get')
    def test_alias_fallback_rejects_non_agent_identity(self, get, post):
        get.side_effect = [
            _response(404, {'error': 'email not found'}),
            _response(200, {
                'exists': True,
                'user_id': 8,
                'username': 'human',
                'account_type': 'human',
                'is_active': True,
            }),
        ]

        result = ensure_roster_org_membership(
            email='human@shizuha.com',
            organization_slug='shizuha',
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'identity_not_found')
        post.assert_not_called()

    def test_roster_edges_support_string_and_object_members(self):
        edges = roster_org_edges([
            {
                'team_name': 'architecture',
                'organization_slug': 'shizuha',
                'tiers': [['kumo@shizuha.com', {'email': 'sora@shizuha.com'}]],
            },
            {
                'team_name': 'disabled',
                'organization_slug': 'shizuha',
                'is_active': False,
                'tiers': [['ignored@shizuha.com']],
            },
        ])

        self.assertEqual(edges, [
            ('shizuha', 'kumo@shizuha.com'),
            ('shizuha', 'sora@shizuha.com'),
        ])

    @mock.patch('fleet.org_membership_sync._existing_org_member_emails')
    @mock.patch('fleet.org_membership_sync.ensure_roster_org_membership')
    def test_reconcile_repairs_valid_edges_without_hiding_stale_identity(self, ensure, existing):
        existing.return_value = (set(), None)
        ensure.side_effect = [
            mock.Mock(ok=True, status='created', email='kumo@shizuha.com'),
            mock.Mock(ok=False, status='identity_not_found', email='stale@shizuha.com'),
        ]

        summary = reconcile_roster_org_memberships([{
            'team_name': 'architecture',
            'organization_slug': 'shizuha',
            'tiers': [['kumo@shizuha.com', 'stale@shizuha.com']],
        }])

        self.assertEqual(summary['created'], 1)
        self.assertEqual(summary['unresolved'], ['stale@shizuha.com'])
        self.assertEqual(summary['failures'], [])

    @mock.patch('fleet.org_membership_sync._existing_org_member_emails')
    @mock.patch('fleet.org_membership_sync.ensure_roster_org_membership')
    def test_reconcile_skips_already_present_edges_without_per_agent_calls(self, ensure, existing):
        existing.return_value = ({'kumo@shizuha.com', 'sora@shizuha.com'}, None)

        summary = reconcile_roster_org_memberships([{
            'team_name': 'architecture',
            'organization_slug': 'shizuha',
            'tiers': [['kumo@shizuha.com', 'sora@shizuha.com']],
        }])

        self.assertEqual(summary['checked'], 2)
        self.assertEqual(summary['existing'], 2)
        ensure.assert_not_called()

    @mock.patch('fleet.org_membership_sync.reconcile_roster_org_memberships')
    def test_command_reports_repair_summary(self, reconcile):
        reconcile.return_value = {
            'checked': 2,
            'created': 1,
            'existing': 0,
            'unresolved': ['stale@shizuha.com'],
            'failures': [],
        }

        summary = Command()._reconcile_roster_org_memberships([{'team_name': 'architecture'}])

        self.assertEqual(summary['created'], 1)

    @mock.patch('fleet.models.archived_usernames', return_value=set())
    @mock.patch(
        'fleet.management.commands.reconcile_on_demand_agents.'
        'hibernate_lifecycle_agent'
    )
    @mock.patch(
        'fleet.management.commands.reconcile_on_demand_agents.'
        'set_lifecycle_agent_enabled'
    )
    @mock.patch(
        'fleet.management.commands.reconcile_on_demand_agents.'
        'list_lifecycle_agents'
    )
    @mock.patch.object(Command, '_fetch_connect_state')
    @mock.patch.object(Command, '_fetch_pulse_snapshot')
    @mock.patch.object(Command, '_fetch_pulse_rosters')
    @mock.patch.object(Command, '_reconcile_roster_org_memberships')
    def test_org_membership_failure_does_not_skip_lifecycle_actions(
        self,
        reconcile_memberships,
        rosters,
        pulse_snapshot,
        connect_snapshot,
        lifecycle_agents,
        set_enabled,
        hibernate,
        _archived,
    ):
        """An unrelated repair outage must not strand an excess paid seat."""
        reconcile_memberships.side_effect = CommandError(
            'Organization membership reconcile failed: *:members_unavailable'
        )
        rosters.return_value = [{
            'team_name': 'devops',
            'tiers': [[
                {'email': 'ichi@shizuha.com'},
                {'email': 'ni@shizuha.com'},
                {'email': 'san@shizuha.com'},
            ]],
        }]
        pulse_snapshot.return_value = {
            'workload': {
                'ichi@shizuha.com': {'in_progress': 1},
                'ni@shizuha.com': {'open': 1},
                'san@shizuha.com': {'blocked': 3},
            },
            'team_workload': {'devops': {'open': 1, 'in_progress': 1}},
        }
        lifecycle_agents.return_value = [
            {
                'id': f'id-{username}',
                'username': username,
                'email': f'{username}@shizuha.com',
                'enabled': True,
                'status': 'running',
            }
            for username in ('ichi', 'ni', 'san')
        ]
        connect_snapshot.return_value = ConnectLifecycleSnapshot(
            available=True,
            unread_by_email={},
            last_dm_at_by_email={},
        )
        hibernate.return_value = (True, {'status': 'hibernated'})
        stdout = StringIO()

        # The lifecycle assertion is about independence from membership repair,
        # not about the stop safety fence.  Supply the affirmative live-idle
        # observation now required before any automatic hibernate.
        with mock.patch(
            'fleet.management.commands.reconcile_on_demand_agents.'
            'runtime_idle_confirmation',
            return_value=(True, 'runtime reports fresh idle at apply time'),
        ), self.assertRaisesMessage(
            CommandError,
            'PARTIAL reconcile: lifecycle decisions completed',
        ):
            call_command(
                'reconcile_on_demand_agents',
                '--apply',
                '--reconcile-org-memberships',
                stdout=stdout,
            )

        set_enabled.assert_not_called()
        hibernate.assert_called_once_with(
            'id-san',
            reason='idle running seat exceeds current actionable team demand: devops=2',
        )
        self.assertIn(
            'repair incomplete; continuing lifecycle reconcile',
            stdout.getvalue(),
        )

    def test_ci_and_bootstrap_enable_repair_backstop(self):
        root = Path(__file__).resolve().parents[1]
        workflow = "\n".join((root / path).read_text() for path in (
            '.forgejo/workflows/deploy.yml',
            '.forgejo/scripts/ring-release-fleet.sh',
        ))
        bootstrap = (root / 'k8s' / 'hive-on-demand-reconciler.yaml').read_text()

        self.assertIn('"--reconcile-org-memberships"', workflow)
        self.assertIn('- --reconcile-org-memberships', bootstrap)
        self.assertIn('"--reconcile-capabilities"', workflow)
        self.assertIn('- --reconcile-capabilities', bootstrap)


@override_settings(
    HIVE_ADMIN_API_URL='http://admin/api',
    HIVE_SERVICE_SIGNING_KEY='dedicated-hive-key',
    ADMIN_INTERNAL_SERVICE_TOKEN='service-token',
)
class PersonalAgentOrgWireTests(SimpleTestCase):
    @mock.patch('fleet.personal_agents._rq.post')
    @mock.patch('fleet.personal_agents._owner_personal_org_id', return_value=909)
    @mock.patch('fleet.personal_agents.resolve_agent_user_id', return_value=1003)
    def test_personal_wire_signs_the_org_membership(self, _uid, _org, post):
        agent = mock.Mock(agent_username='raunak-glm', display_name='GLM Flash', identity_user_id=1003)
        wire_personal_agent_connectivity(1001, agent)
        org_call = post.call_args_list[0]
        self.assertEqual(
            org_call.args[0],
            'http://admin/api/internal/organizations/909/members/',
        )
        self.assertIn('X-Hive-Signature', org_call.kwargs['headers'])
        body = json.loads(org_call.kwargs['data'])
        self.assertEqual(body['user_id'], 1003)
        self.assertEqual(body['operation_id'], 'personal-agent-org:909:1003')
        self.assertEqual(body['role'], 'member')
