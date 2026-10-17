"""Exercise the lifecycle caller sequence across access-token expiration."""
from unittest.mock import Mock, patch

import requests
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from fleet import pulse_auth
from fleet.management.commands.reconcile_on_demand_agents import Command


def response(status=200, payload=None):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


def credential(token, ttl=900):
    return response(payload={
        'access_token': token, 'expires_in': ttl, 'token_type': 'Bearer',
        'scope': pulse_auth.SCOPE,
    })


@override_settings(
    HIVE_PULSE_API_URL='http://pulse/api',
    HIVE_PULSE_OAUTH_CLIENT_ID=pulse_auth.CLIENT_ID,
    HIVE_PULSE_OAUTH_CLIENT_SECRET='test-secret',
    HIVE_PULSE_OAUTH_TOKEN_URL='http://id/api/oauth/token',
    HIVE_PULSE_API_TOKEN='expired-personal-token',
)
class PulseFleetAuthTests(SimpleTestCase):
    def setUp(self):
        pulse_auth._TOKEN_CACHE.update(key=None, token='', expires_at=0.0)
        self.addCleanup(pulse_auth._TOKEN_CACHE.update, key=None, token='', expires_at=0.0)

    @patch('fleet.pulse_auth.requests.get')
    @patch('fleet.pulse_auth.requests.post')
    @patch('fleet.pulse_auth.time.monotonic')
    def test_real_reconcile_reads_renew_before_expiry_on_next_tick(self, clock, post, get):
        clock.return_value = 100
        post.side_effect = [credential('first'), credential('renewed')]
        get.side_effect = [
            response(payload=[{'team_name': 'engineering'}]),
            response(payload={'team_workload': {'engineering': {'open': 5}}}),
            response(payload=[{'team_name': 'engineering'}]),
            response(payload={'team_workload': {'engineering': {'open': 4}}}),
        ]
        command = Command()
        self.assertTrue(command._fetch_pulse_rosters())
        self.assertTrue(command._fetch_pulse_snapshot())
        self.assertEqual(post.call_count, 1)
        clock.return_value = 971
        self.assertTrue(command._fetch_pulse_rosters())
        self.assertTrue(command._fetch_pulse_snapshot())
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            [c.kwargs['headers']['Authorization'] for c in get.call_args_list],
            ['Bearer first', 'Bearer first', 'Bearer renewed', 'Bearer renewed'],
        )
        self.assertEqual(post.call_args.kwargs['data']['grant_type'], 'client_credentials')
        self.assertEqual(post.call_args.kwargs['data']['scope'], pulse_auth.SCOPE)

    @patch('fleet.pulse_auth.requests.get')
    @patch('fleet.pulse_auth.requests.post')
    def test_rejected_cached_access_refreshes_once_and_next_read_uses_new_token(self, post, get):
        post.side_effect = [credential('rejected'), credential('fresh')]
        get.side_effect = [response(401), response(payload=[{'id': 1}]), response(payload={'ok': True})]
        self.assertEqual(Command()._fetch_pulse_rosters(), [{'id': 1}])
        self.assertEqual(Command()._fetch_pulse_snapshot(), {'ok': True})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            [c.kwargs['headers']['Authorization'] for c in get.call_args_list],
            ['Bearer rejected', 'Bearer fresh', 'Bearer fresh'],
        )

    @patch('fleet.pulse_auth.requests.get', return_value=response(401))
    @patch('fleet.pulse_auth.requests.post')
    def test_second_401_is_fatal_not_empty_state_or_unbounded_refresh(self, post, get):
        post.side_effect = [credential('first'), credential('second')]
        with self.assertRaisesMessage(CommandError, 'HTTP 401'):
            Command()._fetch_pulse_rosters()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(pulse_auth._TOKEN_CACHE['token'], '')

    @patch('fleet.pulse_auth.requests.get', return_value=response(401))
    @patch('fleet.pulse_auth.requests.post')
    def test_failed_refresh_invalidates_cache_and_does_not_use_personal_token(self, post, get):
        post.side_effect = [credential('first'), response(401), response(401)]
        for _ in range(2):
            with self.assertRaisesMessage(CommandError, 'OAuth exchange failed: HTTP 401'):
                Command()._fetch_pulse_rosters()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(pulse_auth._TOKEN_CACHE['token'], '')

    @patch('fleet.pulse_auth.requests.post')
    def test_bad_exchange_payloads_do_not_cache_or_disclose_credentials(self, post):
        for payload in [None, [], {}, {'access_token': 'secret'}, {
            'access_token': 'secret', 'expires_in': 'bad', 'token_type': 'Bearer', 'scope': pulse_auth.SCOPE,
        }, {
            'access_token': 'secret', 'expires_in': 0, 'token_type': 'Bearer', 'scope': pulse_auth.SCOPE,
        }, {
            'access_token': 'secret', 'expires_in': 900, 'token_type': 'Bearer', 'scope': '*',
        }]:
            with self.subTest(payload=payload):
                post.return_value = response(payload=payload)
                with self.assertRaisesMessage(CommandError, 'invalid credentials') as caught:
                    pulse_auth.pulse_fleet_headers()
                self.assertNotIn('secret', str(caught.exception))
                self.assertEqual(pulse_auth._TOKEN_CACHE['token'], '')

    @patch('fleet.pulse_auth.requests.post', side_effect=requests.Timeout('test-secret'))
    def test_network_error_does_not_disclose_exchange_parameters(self, post):
        with self.assertRaisesMessage(CommandError, 'exchange unavailable') as caught:
            pulse_auth.pulse_fleet_headers()
        self.assertNotIn('test-secret', str(caught.exception))

    @override_settings(HIVE_PULSE_OAUTH_CLIENT_SECRET='')
    @patch('fleet.pulse_auth.requests.post')
    def test_missing_service_config_never_falls_back_to_expired_user(self, post):
        with self.assertRaisesMessage(CommandError, 'not configured'):
            Command()._fetch_pulse_rosters()
        post.assert_not_called()

    @patch('fleet.pulse_auth.requests.post')
    def test_credential_reader_cannot_be_used_for_other_endpoints(self, post):
        with self.assertRaisesMessage(CommandError, 'does not allow'):
            pulse_auth.pulse_fleet_get('/admin/users/')
        post.assert_not_called()

    @patch('fleet.pulse_auth.requests.get', return_value=response(payload=[]))
    @patch('fleet.pulse_auth.requests.post', return_value=credential('fresh'))
    def test_all_roster_consumers_share_renewable_service_identity(self, post, get):
        from fleet.management.commands.reconcile_agent_github_identity import Command as Github
        from fleet.management.commands.reconcile_agent_role_credentials import Command as Roles
        self.assertEqual(Github()._fetch_pulse_rosters(), [])
        self.assertEqual(Roles()._fetch_pulse_rosters(), [])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 2)
