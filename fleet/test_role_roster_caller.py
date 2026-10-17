"""Credential consumers must resolve real roster reads, including cache renewal."""
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from fleet import provisioner, pulse_auth
from fleet.management.commands.reconcile_agent_github_identity import Command as GithubCommand
from fleet.management.commands.reconcile_agent_role_credentials import Command as RoleCommand
from fleet.models import FleetAgent


def response(status=200, payload=None):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


def credential(token):
    return response(payload={
        'access_token': token, 'expires_in': 900, 'token_type': 'Bearer',
        'scope': pulse_auth.SCOPE,
    })


def roster(team='accounting'):
    return {
        'team_name': team, 'organization_slug': 'shizuha', 'organization_id': 1,
        'is_active': True, 'tiers': [['banto@shizuha.com']],
    }


@override_settings(
    HIVE_PULSE_API_URL='http://pulse/api',
    HIVE_PULSE_OAUTH_CLIENT_ID=pulse_auth.CLIENT_ID,
    HIVE_PULSE_OAUTH_CLIENT_SECRET='test-service-secret',
    HIVE_PULSE_OAUTH_TOKEN_URL='http://id/api/oauth/token',
    SHIZUHA_FLEET_NAMESPACE='configured-fleet',
)
class RoleRosterCallerTests(SimpleTestCase):
    def setUp(self):
        # Patch the cache contents, not the readers: restore both shared caches
        # after each test so TTL/credentials cannot leak into another caller.
        for cache, values in (
            (pulse_auth._TOKEN_CACHE, {'key': None, 'token': '', 'expires_at': 0.0}),
            (provisioner._ROLE_CRED_ROSTER_CACHE, {'at': 0.0, 'map': None}),
        ):
            patcher = patch.dict(cache, values, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agent = FleetAgent(
            agent_username='banto', agent_id='agent-banto',
            display_name='Banto', identity_user_id=4242,
        )

    def test_github_command_parser_uses_configured_namespace(self):
        parser = GithubCommand().create_parser('manage.py', 'reconcile_agent_github_identity')
        self.assertEqual(parser.parse_args([]).namespace, 'configured-fleet')
        self.assertEqual(parser.parse_args(['--namespace', 'explicit-fleet']).namespace, 'explicit-fleet')

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    @patch('fleet.pulse_auth.requests.get')
    @patch('fleet.pulse_auth.requests.post')
    @patch('fleet.pulse_auth.time.monotonic')
    def test_lazy_caller_projects_scopes_and_refreshes_roster_and_access_token(self, clock, post, get, k8s, event):
        clock.return_value = 100.0
        post.side_effect = [credential('first'), credential('renewed')]
        get.side_effect = [
            response(payload={'teams': [roster()]}),
            response(payload={'teams': [roster('engineering')]}),
            response(payload={'teams': [roster()]}),
        ]
        core = Mock()
        core.read_namespaced_secret.return_value = Mock(data={
            'accounting-portal-catalog.json': 'e30=',
        })
        k8s.return_value = (core, Mock(), Mock())

        self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), {'accounting'})
        self.assertEqual(provisioner.ensure_agent_role_credentials(self.agent), ['accounting-portal'])
        core.patch_namespaced_secret.assert_called_with(
            'banto-agent-creds', 'configured-fleet',
            {'data': {'accounting-portal-catalog.json': 'e30='}},
        )
        clock.return_value = 399.0
        self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), {'accounting'})
        self.assertEqual(get.call_count, 1)

        # The existing 300s roster TTL changes the grant while the access
        # token remains valid. No helper or membership result is mocked away.
        clock.return_value = 401.0
        self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), {'engineering'})
        self.assertEqual(provisioner.ensure_agent_role_credentials(self.agent), [])
        self.assertIn('write:repository', provisioner._forgejo_token_scopes(self.agent))
        self.assertEqual(post.call_count, 1)

        # Both caches expire independently; the next provision read renews
        # access and restores accounting's real role projection.
        clock.return_value = 971.0
        self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), {'accounting'})
        self.assertEqual(provisioner.ensure_agent_role_credentials(self.agent), ['accounting-portal'])
        self.assertEqual(core.patch_namespaced_secret.call_count, 2)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(
            [call.kwargs['headers']['Authorization'] for call in get.call_args_list],
            ['Bearer first', 'Bearer first', 'Bearer renewed'],
        )
        self.assertTrue(all(call.args == ('http://pulse/api/admin/team-roster/',) for call in get.call_args_list))
        self.assertEqual(post.call_args.kwargs['data']['grant_type'], 'client_credentials')
        self.assertEqual(post.call_args.kwargs['data']['scope'], pulse_auth.SCOPE)

    @patch('fleet.pulse_auth.requests.get', return_value=response(payload={'teams': [roster()]}))
    @patch('fleet.pulse_auth.requests.post')
    @patch('fleet.pulse_auth.time.monotonic', return_value=100.0)
    def test_auth_failure_is_not_cached_as_no_membership(self, clock, post, get):
        post.side_effect = [response(401), credential('recovered')]
        with self.assertLogs('fleet.provisioner', level='WARNING'):
            self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), set())
        self.assertIsNone(provisioner._ROLE_CRED_ROSTER_CACHE['map'])
        # No TTL or clock advance: a repaired credential works on the very
        # next caller rather than retaining a fabricated empty roster.
        self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), {'accounting'})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(get.call_count, 1)

    @patch('fleet.pulse_auth.requests.get')
    @patch('fleet.pulse_auth.requests.post')
    @patch('fleet.pulse_auth.time.monotonic')
    def test_expired_roster_failure_does_not_reuse_stale_grants_or_delay_recovery(self, clock, post, get):
        post.side_effect = [credential('first'), credential('rejected'), credential('recovered')]
        get.side_effect = [
            response(payload={'teams': [roster()]}), response(401), response(401),
            response(payload={'teams': [roster('engineering')]}),
        ]
        clock.return_value = 100.0
        self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), {'accounting'})
        clock.return_value = 401.0
        with self.assertLogs('fleet.provisioner', level='WARNING'):
            self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), set())
        self.assertIsNone(provisioner._ROLE_CRED_ROSTER_CACHE['map'])
        self.assertEqual(pulse_auth._TOKEN_CACHE['token'], '')
        clock.return_value = 402.0
        self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), {'engineering'})
        self.assertEqual(get.call_count, 4)
        self.assertEqual(post.call_count, 3)

    @patch('fleet.pulse_auth.requests.get')
    @patch('fleet.pulse_auth.requests.post', return_value=credential('fresh'))
    def test_invalid_payload_is_not_cached_as_successful_empty_roster(self, post, get):
        for invalid in (None, {}, 'bad payload', {'teams': 'not a list'}):
            with self.subTest(payload=invalid):
                provisioner._ROLE_CRED_ROSTER_CACHE.update(at=0.0, map=None)
                get.side_effect = [response(payload=invalid), response(payload={'teams': [roster()]})]
                with self.assertLogs('fleet.provisioner', level='WARNING'):
                    self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), set())
                self.assertIsNone(provisioner._ROLE_CRED_ROSTER_CACHE['map'])
                self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), {'accounting'})

    @patch('fleet.pulse_auth.requests.get')
    @patch('fleet.pulse_auth.requests.post', return_value=credential('fresh'))
    @patch('fleet.pulse_auth.time.monotonic')
    def test_successful_empty_roster_is_cached_until_normal_ttl(self, clock, post, get):
        get.side_effect = [response(payload={'teams': []}), response(payload={'teams': [roster()]})]
        clock.return_value = 100.0
        self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), set())
        self.assertEqual(provisioner._ROLE_CRED_ROSTER_CACHE['map'], {})
        clock.return_value = 399.0
        self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), set())
        self.assertEqual(get.call_count, 1)
        clock.return_value = 401.0
        self.assertEqual(provisioner._agent_teams_from_roster(self.agent, None), {'accounting'})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(post.call_count, 1)

    @patch('fleet.pulse_auth.requests.get')
    @patch('fleet.pulse_auth.requests.post', return_value=credential('fresh'))
    def test_github_and_role_commands_share_public_reader_envelope_support(self, post, get):
        for payload in ([roster()], *({key: [roster()]} for key in ('teams', 'rosters', 'results', 'items'))):
            with self.subTest(payload=payload):
                get.return_value = response(payload=payload)
                self.assertEqual(GithubCommand()._fetch_pulse_rosters(), [roster()])
                self.assertEqual(RoleCommand()._fetch_pulse_rosters(), [roster()])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 10)
