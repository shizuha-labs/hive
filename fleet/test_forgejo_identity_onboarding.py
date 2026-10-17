import base64
from copy import deepcopy
from unittest import mock

import requests
from django.test import SimpleTestCase

from fleet import provisioner
from fleet.models import FleetAgent


class OriginFixture:
    def __init__(self, *, existing=False, member=False):
        self.user = self.identity() if existing else None
        self.member = member
        self.calls = []
        self.overrides = {}
        self.team = {
            'id': 6, 'name': 'agents-readers', 'permission': 'read',
            'can_create_org_repo': False, 'units_map': {'repo.code': 'read', 'repo.pulls': 'read'},
        }

    @staticmethod
    def identity(**overrides):
        return {'id': 91, 'login': 'hina', 'email': 'hina@shizuha.com',
                'is_admin': False, 'active': True, 'prohibit_login': False,
                'restricted': True, 'visibility': 'private', **overrides}

    @staticmethod
    def response(status, payload=None):
        response = mock.Mock(status_code=status)
        response.json.return_value = deepcopy(payload)
        response.text = ''
        return response

    def request(self, method, url, **kwargs):
        path = url.removeprefix('http://origin/api/v1')
        self.calls.append((method, path, deepcopy(kwargs)))
        if (method, path) in self.overrides:
            result = self.overrides[method, path]
            if isinstance(result, Exception):
                raise result
            return result() if callable(result) else result
        if method == 'get' and path == '/users/hina':
            return self.response(200, self.user) if self.user else self.response(404)
        if method == 'get' and path == '/orgs/shizuha-labs/members/hina':
            return self.response(204 if self.member else 404)
        if method == 'get' and path == '/orgs/shizuha-labs/teams':
            return self.response(200, [self.team])
        if method == 'get' and path == '/teams/6':
            return self.response(200, self.team)
        if method == 'get' and path == '/teams/6/members/hina':
            return self.response(200, self.user) if self.member else self.response(404)
        if method == 'post' and path == '/admin/users':
            if self.user is not None:
                raise AssertionError('attempt to reset or recreate an existing account')
            self.user = self.identity()
            return self.response(201, self.user)
        if method == 'put' and path == '/teams/6/members/hina':
            if self.user is None:
                raise AssertionError('membership before user creation')
            self.member = True
            return self.response(204)
        if method == 'post' and path == '/users/hina/tokens':
            if self.user is None or not self.member:
                raise AssertionError('token mint before verified onboarding')
            return self.response(201, {'sha1': 'own-hina-origin-token'})
        raise AssertionError((method, path))


class OriginOnboardingCallerTests(SimpleTestCase):
    def setUp(self):
        self.agent = FleetAgent(agent_username='hina', agent_id='hive-hina',
                                identity_user_id=698, display_name='Hina', status='running')
        self.core = mock.Mock()
        self.secret = mock.Mock(data={'CORTEX_API_KEY': base64.b64encode(b'cortex-key').decode()})
        def read_secret(name, namespace):
            from kubernetes.client.rest import ApiException
            if name.endswith('-forgejo-token'):
                raise ApiException(status=404)
            return self.secret
        self.core.read_namespaced_secret.side_effect = read_secret
        self.core.patch_namespaced_secret.side_effect = self.persist_secret
        for name, value in (
            ('get_k8s_client', (self.core, mock.Mock(), mock.Mock())),
            ('_forgejo_admin_basic_auth', ('admin', 'private-admin-password')),
            ('_hive_origin_api_url', 'http://origin'),
            ('_agent_teams_from_roster', {'ceo-office'}),
            ('_log_event', None),
        ):
            patcher = mock.patch.object(provisioner, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.origin = OriginFixture()
        for method in ('get', 'post', 'put', 'patch', 'delete'):
            patcher = mock.patch('requests.' + method, side_effect=lambda url, method=method, **kwargs: self.origin.request(method, url, **kwargs))
            patcher.start()
            self.addCleanup(patcher.stop)

    def persist_secret(self, name, namespace, payload):
        self.secret.data.update(payload['data'])

    def ensure(self):
        return provisioner.ensure_agent_forgejo_token(self.agent)

    def mutations(self):
        return [(method, path) for method, path, _ in self.origin.calls if method != 'get']

    def assert_no_token_or_secret_write(self):
        self.assertFalse(any(path.endswith('/tokens') for _, path, _ in self.origin.calls))
        self.core.patch_namespaced_secret.assert_not_called()
        self.core.create_namespaced_secret.assert_not_called()

    def test_missing_identity_joins_only_readers_then_mints_once(self):
        fingerprint = self.ensure()
        self.assertTrue(fingerprint)
        self.assertEqual(self.mutations(), [
            ('post', '/admin/users'), ('put', '/teams/6/members/hina'),
            ('post', '/users/hina/tokens'),
        ])
        create = next(kwargs['json'] for method, path, kwargs in self.origin.calls if path == '/admin/users')
        self.assertEqual(create['username'], 'hina')
        self.assertEqual(create['email'], 'hina@shizuha.com')
        self.assertTrue(create['restricted'])
        self.assertEqual(create['visibility'], 'private')
        self.assertFalse(create['send_notify'])
        self.assertFalse(create['must_change_password'])
        self.assertGreater(len(create['password']), 48)
        token = next(kwargs['json'] for method, path, kwargs in self.origin.calls if path.endswith('/tokens'))
        self.assertEqual(token['scopes'], list(provisioner._FORGEJO_READ_SCOPES))
        self.assertEqual(base64.b64decode(self.secret.data['FORGEJO_TOKEN']), b'own-hina-origin-token')
        self.assertEqual(self.secret.data['FORGEJO_TOKEN'], self.secret.data['ORIGIN_TOKEN'])
        self.assertEqual(base64.b64decode(self.secret.data['CORTEX_API_KEY']), b'cortex-key')
        count = len(self.origin.calls)
        self.assertEqual(self.ensure(), fingerprint)
        self.assertEqual(len(self.origin.calls), count)
        self.core.patch_namespaced_secret.assert_called_once()

    def test_existing_writer_member_preserves_roles_and_account(self):
        self.agent.owner_id = 3
        self.origin = OriginFixture(existing=True, member=True)
        self.origin.user.update(restricted=False, visibility='public', email='legacy-alias@example.test')
        self.origin.team['permission'] = 'admin'
        # PLAT-10143 boundary follow-up: scope derivation reads the roster
        # through _forgejo_grant_checked -> _role_credential_roster_cache;
        # mocking the old _agent_teams_from_roster boundary no longer
        # intercepts it, so the unmocked live fetch rides the real path and
        # a degraded run classifies the seat unknown-grant (read-scoped).
        # Mock the boundary production actually consumes.
        with mock.patch.object(
            provisioner, '_role_credential_roster_cache',
            return_value={'hina@shizuha.com': {'engineering'}},
        ):
            self.assertTrue(self.ensure())
        self.assertEqual(self.mutations(), [('post', '/users/hina/tokens')])
        self.assertFalse(any('/teams' in path for _, path, _ in self.origin.calls))
        payload = self.origin.calls[-1][2]['json']
        self.assertEqual(payload['scopes'], list(provisioner._FORGEJO_WRITE_SCOPES))

    def test_cos_alias_uses_existing_agent_identity_never_origin_admin(self):
        self.agent.agent_username = 'shizuha'
        self.origin = OriginFixture(existing=True, member=True)
        self.origin.user.update(login='shizuha-agent', email='shizuha-agent@shizuha.com')
        urls = []

        def call(method, url, **kwargs):
            urls.append(url)
            return self.origin.request(method, url.replace('shizuha-agent', 'hina'), **kwargs)

        with mock.patch('requests.get', side_effect=lambda url, **kwargs: call('get', url, **kwargs)), \
                mock.patch('requests.post', side_effect=lambda url, **kwargs: call('post', url, **kwargs)):
            self.assertTrue(self.ensure())
        self.assertTrue(urls[-1].endswith('/users/shizuha-agent/tokens'))
        self.assertFalse(any('/users/shizuha/' in url or url.endswith('/admin/users') for url in urls))

    def test_forbidden_membership_is_not_absent_onboarding(self):
        self.origin = OriginFixture(existing=True)
        self.origin.overrides['get', '/orgs/shizuha-labs/members/hina'] = self.origin.response(403)
        self.assertEqual(self.ensure(), '')
        self.assertEqual(self.mutations(), [])
        self.assert_no_token_or_secret_write()

    def test_unconfirmed_membership_does_not_mint_after_successful_put(self):
        self.origin.overrides['get', '/teams/6/members/hina'] = self.origin.response(404)
        self.assertEqual(self.ensure(), '')
        self.assert_no_token_or_secret_write()

    def test_non_json_identity_response_never_creates_or_mints(self):
        response = self.origin.response(200)
        response.json.side_effect = ValueError('malformed JSON')
        self.origin.overrides['get', '/users/hina'] = response
        self.assertEqual(self.ensure(), '')
        self.assertEqual(self.mutations(), [])
        self.assert_no_token_or_secret_write()

    def test_interrupted_account_creation_resumes_membership_without_reset(self):
        self.origin.overrides['put', '/teams/6/members/hina'] = self.origin.response(503)
        self.assertEqual(self.ensure(), '')
        self.assertIsNotNone(self.origin.user)
        self.assert_no_token_or_secret_write()
        del self.origin.overrides['put', '/teams/6/members/hina']
        self.assertTrue(self.ensure())
        self.assertEqual(self.mutations().count(('post', '/admin/users')), 1)

    def test_forbidden_and_transport_errors_are_not_missing_identity(self):
        for result in (self.origin.response(401), self.origin.response(403), self.origin.response(500), requests.Timeout('unavailable')):
            with self.subTest(result=result):
                self.origin.calls.clear()
                self.origin.overrides['get', '/users/hina'] = result
                self.assertEqual(self.ensure(), '')
                self.assertEqual(self.mutations(), [])
                self.assert_no_token_or_secret_write()

    def test_mismatched_admin_disabled_and_prohibited_accounts_fail_closed(self):
        for invalid in ({'login': 'another'},
                        {'is_admin': True}, {'active': False}, {'prohibit_login': True}):
            with self.subTest(invalid=invalid):
                self.origin = OriginFixture(existing=True, member=True)
                self.origin.user.update(invalid)
                self.assertEqual(self.ensure(), '')
                self.assertEqual(self.mutations(), [])
                self.assert_no_token_or_secret_write()

    def test_nonmember_with_conflicting_email_cannot_gain_reader_membership(self):
        self.origin = OriginFixture(existing=True)
        self.origin.user['email'] = 'someone-else@shizuha.com'
        self.assertEqual(self.ensure(), '')
        self.assertEqual(self.mutations(), [])
        self.assert_no_token_or_secret_write()

    def test_personal_unbound_and_admin_principals_cannot_gain_org_access(self):
        # Operator 2026-09-23: owner-scoped (personal) agents now get the same
        # least-privilege onboarding as system agents (restricted account +
        # agents-readers team) so shizuha-115/741-class seats can mint Origin
        # tokens. The refusal set keeps unbound identities and deleted/blocked
        # agents only.
        for changes in ({'identity_user_id': None}, {'status': 'deleted'}, {'status': 'blocked'}):
            with self.subTest(changes=changes):
                agent = deepcopy(self.agent)
                for name, value in changes.items():
                    setattr(self.agent, name, value)
                self.assertEqual(self.ensure(), '')
                self.assertEqual(self.mutations(), [])
                self.assert_no_token_or_secret_write()
                self.agent = agent
                self.origin.calls.clear()
        with mock.patch.object(provisioner, '_forgejo_identity_username', return_value='shizuha'):
            self.assertEqual(self.ensure(), '')
        self.assertEqual(self.origin.calls, [])

    def test_personal_agent_gains_least_privilege_org_membership(self):
        # shizuha-115/741 class: a bound, active personal agent completes the
        # restricted-account + agents-readers onboarding like a system agent.
        self.agent.owner_id = 116
        result = self.ensure()
        self.assertNotEqual(result, '')
        joins = [c for c in self.origin.calls if c[0] == 'put' and '/members/' in c[1]]
        self.assertTrue(joins, f'expected a team-membership write, got {self.origin.calls}')

    def test_unsafe_reader_team_refuses_before_account_creation(self):
        for change in ({'permission': 'write'}, {'can_create_org_repo': True},
                       {'units_map': {'repo.code': 'read', 'repo.actions': 'write'}},
                       {'units_map': {}}, {'name': 'agents-writers'}):
            with self.subTest(change=change):
                self.origin = OriginFixture()
                self.origin.team.update(change)
                self.assertEqual(self.ensure(), '')
                self.assertEqual(self.mutations(), [])
                self.assert_no_token_or_secret_write()

    def test_team_change_before_grant_refuses_without_token_or_secret(self):
        self.origin.overrides['get', '/teams/6'] = self.origin.response(200, {**self.origin.team, 'permission': 'write'})
        self.assertEqual(self.ensure(), '')
        self.assertEqual(self.mutations(), [('post', '/admin/users')])
        self.assert_no_token_or_secret_write()

    def test_membership_readback_cannot_substitute_other_user(self):
        self.origin.overrides['get', '/teams/6/members/hina'] = self.origin.response(200, self.origin.identity(id=92))
        self.assertEqual(self.ensure(), '')
        self.assert_no_token_or_secret_write()

    def test_racing_create_rereads_identity_without_password_reset(self):
        def racing_create():
            self.origin.user = self.origin.identity()
            return self.origin.response(422)
        self.origin.overrides['post', '/admin/users'] = racing_create
        self.assertTrue(self.ensure())
        self.assertEqual(self.mutations(), [
            ('post', '/admin/users'), ('put', '/teams/6/members/hina'),
            ('post', '/users/hina/tokens'),
        ])

    def test_create_collision_with_another_email_never_mints(self):
        def racing_create():
            self.origin.user = self.origin.identity(email='someone-else@shizuha.com')
            return self.origin.response(422)
        self.origin.overrides['post', '/admin/users'] = racing_create
        self.assertEqual(self.ensure(), '')
        self.assert_no_token_or_secret_write()
