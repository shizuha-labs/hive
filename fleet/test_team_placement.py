from types import SimpleNamespace
import time
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from fleet.models import TeamMembershipRole
from fleet.views import (
    FleetAgentTeamPlacementView,
    FleetPulseSnapshotView,
    FleetTeamRosterView,
    _ROSTER_INDEX_STALE_LOCK,
    _ROSTER_LAST_SERVE,
    _ROSTER_PROCESS_RECHECK_AGE_S,
    _cached_pulse_roster_memberships_for_daemon,
    _pulse_roster_team_index,
    _roster_membership_cache_key,
    _roster_membership_cache_write,
    _roster_process_write,
)


def _response(status_code, payload=None):
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = payload if payload is not None else {}
    resp.text = ''
    return resp


def _assert_roster_command(test, body, expected, *, expected_version=0):
    """Assert a PLS-745 roster command payload: exact caller fields plus a
    well-formed UUID command_id and the CAS expected_version."""
    import uuid as _uuid
    body = dict(body)
    _uuid.UUID(body.pop('command_id'))
    test.assertEqual(body.pop('expected_version'), expected_version)
    test.assertEqual(body, expected)


def _wait_revalidate_done(key, timeout=3.0):
    """PLAT-4169: block until read_model_cache's background revalidate thread for
    `key` has finished, so the failure marker is deterministically set/cleared
    before a follow-up view request asserts degraded/not-degraded (the view reads
    the marker synchronously; get_or_revalidate returns before the thread runs)."""
    import time as _t
    from fleet import read_model_cache as _rmc
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        with _rmc._inflight_lock:
            running = key in _rmc._inflight
        if not running:
            return
        _t.sleep(0.02)
    raise AssertionError(f'revalidate for {key} did not finish within {timeout}s')


# Deterministic in-process cache for the pulse-snapshot read-model tests — no
# live Redis needed; the read_model_cache contract is backend-agnostic.
_SNAPSHOT_LOCMEM = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'pulse-snapshot-test',
    }
}




class FleetRosterOrgContextTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.staff = SimpleNamespace(
            id=3,
            email='test-owner@example.com',
            username='hritik',
            is_staff=True,
            is_authenticated=True,
        )
        with _ROSTER_INDEX_STALE_LOCK:
            _ROSTER_LAST_SERVE.update({'degraded': False, 'built_at': 0.0})

    def _request(self):
        req = self.factory.get('/v1/fleet/agents/', HTTP_AUTHORIZATION='Bearer staff-token')
        force_authenticate(req, user=self.staff, token={'email': self.staff.email, 'is_staff': True})
        return req

    @override_settings(HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views.requests.request')
    def test_pulse_roster_index_preserves_org_identity_on_memberships(self, request_mock):
        def fake_request(method, url, **kwargs):
            if method == 'GET' and url == 'http://pulse/api/admin/team-roster/members/':
                return _response(200, {
                    'rosters': [{
                        'team_name': 'Human Ops',
                        'organization_slug': 'shizuha',
                        'organization_name': 'Shizuha Global Pvt. Ltd',
                        'tiers': [[{'email': 'kai@shizuha.com'}]],
                    }],
                })
            raise AssertionError(f'unexpected roster call {method} {url}')

        request_mock.side_effect = fake_request

        index = _pulse_roster_team_index(self._request())

        membership = index['kai@shizuha.com'][0]
        self.assertEqual(membership['team_slug'], 'human-ops')
        self.assertEqual(membership['organization_slug'], 'shizuha')
        self.assertEqual(membership['organization_name'], 'Shizuha Global Pvt. Ltd')

    @override_settings(HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views.requests.request')
    def test_pulse_roster_index_does_not_infer_membership_from_lead_metadata(self, request_mock):
        """Directory lead metadata must never become a second placement source."""
        def fake_request(method, url, **kwargs):
            if method == 'GET' and url == 'http://pulse/api/admin/team-roster/members/':
                return _response(200, {
                    'rosters': [{
                        'team_name': 'Engineering',
                        'organization_slug': 'shizuha',
                        'organization_name': 'Shizuha Global Pvt. Ltd',
                        'lead_email': 'ryo@shizuha.com',
                        # Lead is intentionally absent from authoritative tiers.
                        'tiers': [[{'email': 'jun@shizuha.com'}]],
                    }],
                })
            raise AssertionError(f'unexpected roster call {method} {url}')

        request_mock.side_effect = fake_request
        index = _pulse_roster_team_index(self._request())

        self.assertNotIn('ryo@shizuha.com', index)
        self.assertEqual(len(index['jun@shizuha.com']), 1)

    @override_settings(HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._roster_cache_write')
    @mock.patch('fleet.views.requests.request')
    def test_authoritative_empty_roster_replaces_cached_placement(self, request_mock, cache_write):
        request_mock.return_value = _response(200, {'rosters': [], 'memberships': []})

        self.assertEqual(_pulse_roster_team_index(self._request()), {})
        cache_write.assert_called_once_with({})


class FleetAgentTeamPlacementViewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = FleetAgentTeamPlacementView.as_view()
        org_membership_patcher = mock.patch(
            'fleet.views.ensure_roster_org_membership',
            return_value=SimpleNamespace(ok=True, status='exists', detail=''),
        )
        self.org_membership_sync = org_membership_patcher.start()
        self.addCleanup(org_membership_patcher.stop)
        self.staff = SimpleNamespace(
            id=3,
            email='test-owner@example.com',
            username='hritik',
            is_staff=True,
            is_authenticated=True,
        )

    def _request(self, method='get', data=None):
        factory = self.factory.get if method == 'get' else self.factory.post
        req = factory(
            '/v1/fleet/agents/rin-id/team-placement/',
            data or {},
            format='json',
            HTTP_AUTHORIZATION='Bearer staff-token',
        )
        force_authenticate(req, user=self.staff, token={'email': self.staff.email, 'is_staff': True})
        return req

    @override_settings(HIVE_ADMIN_API_URL='http://admin/api', HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'rin-id',
        'agent_username': 'rin',
        'role': 'Engineer',
        'email': 'rin@shizuha.com',
    })
    @mock.patch('fleet.views.requests.request')
    def test_get_lists_pulse_roster_placement_for_runtime_agent(self, request_mock, _agent):
        TeamMembershipRole.objects.create(
            agent_identifier='rin@shizuha.com',
            team_slug='marketing',
            organization_slug='shizuha',
            role_title='Campaign Lead',
            membership_role=TeamMembershipRole.MembershipRole.LEAD,
            tier='primary',
        )

        def fake_request(method, url, **kwargs):
            if url == 'http://pulse/api/admin/team-roster/members/':
                return _response(200, {
                    'memberships': [{'team_name': 'marketing', 'organization_slug': 'shizuha'}],
                    'rosters': [{'team_name': 'marketing', 'organization_slug': 'shizuha'}],
                })
            raise AssertionError(f'unexpected roster call {method} {url}')

        request_mock.side_effect = fake_request

        resp = self.view(self._request(), agent_id='rin-id')

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['agent']['email'], 'rin@shizuha.com')
        self.assertEqual(resp.data['teams'][0]['id'], 'shizuha::marketing')
        self.assertEqual(resp.data['teams'][0]['name'], 'Marketing')
        self.assertTrue(resp.data['teams'][0]['selected'])
        self.assertEqual(resp.data['teams'][0]['membership_role'], 'lead')
        self.assertEqual(resp.data['teams'][0]['role_title'], 'Campaign Lead')
        self.assertEqual(resp.data['teams'][0]['tier'], 'primary')
        self.assertEqual(resp.data['teams'][0]['membership_source'], 'pulse_roster')
        self.assertEqual(request_mock.call_args_list[0].kwargs['headers']['Authorization'], 'Bearer staff-token')

    @override_settings(HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'rin-id', 'agent_username': 'rin', 'email': 'rin@shizuha.com',
    })
    @mock.patch('fleet.views.requests.request')
    def test_global_write_rejects_same_team_slug_without_qualified_id(self, request_mock, _agent):
        request_mock.return_value = _response(200, {
            'memberships': [],
            'rosters': [
                {'team_name': 'accounting', 'organization_slug': 'alpha'},
                {'team_name': 'accounting', 'organization_slug': 'beta'},
            ],
        })

        resp = self.view(
            self._request('post', {'action': 'add', 'team_id': 'accounting'}),
            agent_id='rin-id',
        )

        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertIn('ambiguous', resp.data['error'])
        self.assertEqual(request_mock.call_count, 1)

    @override_settings(HIVE_ADMIN_API_URL='http://admin/api', HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'rin-id',
        'agent_username': 'rin',
        'role': 'Engineer',
        'email': 'rin@shizuha.com',
    })
    @mock.patch('fleet.views.agent_index_cache.invalidate')
    @mock.patch('fleet.views.requests.request')
    def test_update_role_persists_membership_edge_without_touching_runtime_grants(self, request_mock, _invalidate, _agent):
        def fake_request(method, url, **kwargs):
            if url == 'http://pulse/api/admin/team-roster/members/':
                self.assertEqual(method, 'GET')
                return _response(200, {
                    'memberships': [{'team_name': 'marketing', 'organization_slug': 'shizuha'}],
                    'rosters': [{'team_name': 'marketing', 'organization_slug': 'shizuha'}],
                })
            raise AssertionError(f'unexpected roster call {method} {url}')

        request_mock.side_effect = fake_request

        resp = self.view(
            self._request('post', {
                'action': 'update_role',
                'team_id': 'marketing',
                'role_title': 'Launch Coordinator',
                'membership_role': 'reviewer',
                'tier': 'fallback',
            }),
            agent_id='rin-id',
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        row = TeamMembershipRole.objects.get(agent_identifier='rin@shizuha.com', team_slug='marketing')
        self.assertEqual(row.role_title, 'Launch Coordinator')
        self.assertEqual(row.membership_role, TeamMembershipRole.MembershipRole.REVIEWER)
        self.assertEqual(row.tier, 'fallback')
        self.assertEqual(resp.data['teams'][0]['role_title'], 'Launch Coordinator')
        self.assertEqual(resp.data['teams'][0]['membership_role'], 'reviewer')

    @override_settings(HIVE_ADMIN_API_URL='http://admin/api', HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'rin-id',
        'agent_username': 'rin',
        'email': 'rin@shizuha.com',
    })
    @mock.patch('fleet.views.requests.request')
    def test_get_uses_pulse_rosters_for_dropdown(self, request_mock, _agent):
        def fake_request(method, url, **kwargs):
            if url == 'http://pulse/api/admin/team-roster/members/':
                return _response(200, {
                    'memberships': [],
                    'rosters': [{'team_name': 'social-media'}, {'team_name': 'accounting'}],
                })
            raise AssertionError(f'unexpected roster call {method} {url}')

        request_mock.side_effect = fake_request

        resp = self.view(self._request(), agent_id='rin-id')

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual([team['name'] for team in resp.data['teams']], ['Accounting', 'Social Media'])

    @override_settings(HIVE_ADMIN_API_URL='http://admin/api', HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'rin-id',
        'agent_username': 'rin',
        'email': 'rin@shizuha.com',
    })
    @mock.patch('fleet.views.requests.request')
    def test_get_org_scoped_placement_uses_pulse_roster_only(self, request_mock, _agent):
        def fake_request(method, url, **kwargs):
            if method == 'GET' and url == 'http://pulse/api/admin/team-roster/members/':
                return _response(200, {
                    'memberships': [{'team_name': 'marketing', 'organization_slug': 'acme'}],
                    'rosters': [{'team_name': 'marketing', 'organization_slug': 'acme'}],
                })
            raise AssertionError(f'unexpected org roster call {method} {url}')

        request_mock.side_effect = fake_request

        resp = self.view(self._request('get', {'org_slug': 'acme'}), agent_id='rin-id')

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['organization_slug'], 'acme')
        self.assertEqual(resp.data['teams'][0]['membership_source'], 'pulse_roster')
        self.assertTrue(resp.data['teams'][0]['selected'])
        self.assertEqual(resp.data['teams'][0]['team_name'], 'marketing')

    @override_settings(HIVE_ADMIN_API_URL='http://admin/api', HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'rin-id',
        'agent_username': 'rin',
        'email': 'rin@shizuha.com',
    })
    @mock.patch('fleet.views.agent_index_cache.invalidate')
    @mock.patch('fleet.views.requests.request')
    def test_org_scoped_add_posts_pulse_membership(self, request_mock, _invalidate, _agent):
        added = {'done': False}

        def fake_request(method, url, **kwargs):
            if method == 'GET' and url == 'http://pulse/api/admin/team-roster/members/':
                memberships = ([{'team_name': 'marketing', 'organization_slug': 'acme'}]
                               if added['done'] else [])
                return _response(200, {
                    'memberships': memberships,
                    'rosters': [{'team_name': 'marketing', 'organization_slug': 'acme'}],
                })
            if method == 'POST' and url == 'http://pulse/api/admin/team-roster/members/':
                _assert_roster_command(self, kwargs['json'], {
                    'action': 'add', 'team_name': 'marketing',
                    'email': 'rin@shizuha.com', 'organization_slug': 'acme',
                })
                added['done'] = True
                return _response(200, {'changed': True})
            raise AssertionError(f'unexpected org roster call {method} {url}')

        request_mock.side_effect = fake_request

        resp = self.view(
            self._request('post', {'org_slug': 'acme', 'action': 'add', 'team_id': 'marketing'}),
            agent_id='rin-id',
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(added['done'])
        self.org_membership_sync.assert_called_once_with(
            email='rin@shizuha.com',
            organization_slug='acme',
            display_name='rin',
        )
        self.assertTrue(resp.data['teams'][0]['selected'])
        row = TeamMembershipRole.objects.get(
            agent_identifier='rin@shizuha.com',
            team_slug='marketing',
        )
        self.assertEqual(row.organization_slug, 'acme')

    @override_settings(HIVE_ADMIN_API_URL='http://admin/api', HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'rin-id',
        'agent_username': 'rin',
        'email': 'rin@shizuha.com',
    })
    @mock.patch('fleet.views.requests.request')
    def test_add_fails_closed_before_roster_write_when_identity_is_missing(self, request_mock, _agent):
        request_mock.return_value = _response(200, {
            'memberships': [],
            'rosters': [{'team_name': 'marketing', 'organization_slug': 'shizuha'}],
        })
        self.org_membership_sync.return_value = SimpleNamespace(
            ok=False,
            status='identity_not_found',
            detail='Shizuha ID identity does not exist',
        )

        resp = self.view(
            self._request('post', {'action': 'add', 'team_id': 'marketing'}),
            agent_id='rin-id',
        )

        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(resp.data['reason'], 'identity_not_found')
        self.assertEqual(request_mock.call_count, 1)
        self.assertEqual(request_mock.call_args.args[0], 'GET')

    @override_settings(HIVE_ADMIN_API_URL='http://admin/api', HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'rin-id',
        'agent_username': 'rin',
        'email': 'rin@shizuha.com',
    })
    @mock.patch('fleet.views.agent_index_cache.invalidate')
    @mock.patch('fleet.views.requests.request')
    def test_org_scoped_remove_preserves_other_org_role_overlay(self, request_mock, _invalidate, _agent):
        TeamMembershipRole.objects.create(
            agent_identifier='rin@shizuha.com',
            team_slug='accounting',
            organization_slug='acme',
            role_title='Acme analyst',
            membership_role=TeamMembershipRole.MembershipRole.MEMBER,
        )
        TeamMembershipRole.objects.create(
            agent_identifier='rin@shizuha.com',
            team_slug='accounting',
            organization_slug='beta',
            role_title='Beta approver',
            membership_role=TeamMembershipRole.MembershipRole.LEAD,
        )
        removed = {'done': False}

        def fake_request(method, url, **kwargs):
            if method == 'GET' and url == 'http://pulse/api/admin/team-roster/members/':
                memberships = [] if removed['done'] else [{'team_name': 'accounting', 'organization_slug': 'acme'}]
                return _response(200, {
                    'memberships': memberships,
                    'rosters': [{'team_name': 'accounting', 'organization_slug': 'acme'}],
                })
            if method == 'POST' and url == 'http://pulse/api/admin/team-roster/members/':
                self.assertEqual(kwargs['json']['action'], 'remove')
                self.assertEqual(kwargs['json']['organization_slug'], 'acme')
                removed['done'] = True
                return _response(200, {'changed': True})
            raise AssertionError(f'unexpected org roster call {method} {url}')

        request_mock.side_effect = fake_request

        resp = self.view(
            self._request('post', {'org_slug': 'acme', 'action': 'remove', 'team_id': 'accounting'}),
            agent_id='rin-id',
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(removed['done'])
        self.assertFalse(
            TeamMembershipRole.objects.get(
                agent_identifier='rin@shizuha.com',
                team_slug='accounting',
                organization_slug='acme',
            ).enabled
        )
        beta_role = TeamMembershipRole.objects.get(
            agent_identifier='rin@shizuha.com',
            team_slug='accounting',
            organization_slug='beta',
        )
        self.assertTrue(beta_role.enabled)
        self.assertEqual(beta_role.role_title, 'Beta approver')

    @override_settings(HIVE_ADMIN_API_URL='http://admin/api', HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'rin-id',
        'agent_username': 'rin',
        'email': 'rin@shizuha.com',
    })
    @mock.patch('fleet.views.requests.request')
    def test_add_posts_team_membership_when_missing(self, request_mock, _agent):
        posted = {'done': False}

        def fake_request(method, url, **kwargs):
            if method == 'POST' and url == 'http://pulse/api/admin/team-roster/members/':
                _assert_roster_command(self, kwargs['json'], {
                    'action': 'add', 'team_name': 'marketing',
                    'email': 'rin@shizuha.com', 'organization_slug': 'shizuha',
                })
                posted['done'] = True
                return _response(200, {'changed': True, 'team_name': 'marketing'})
            if method == 'GET' and url == 'http://pulse/api/admin/team-roster/members/':
                memberships = ([{'team_name': 'marketing', 'organization_slug': 'shizuha'}]
                               if posted['done'] else [])
                return _response(200, {
                    'memberships': memberships,
                    'rosters': [{'team_name': 'marketing', 'organization_slug': 'shizuha'}],
                })
            raise AssertionError(f'unexpected roster call {method} {url}')

        request_mock.side_effect = fake_request

        with mock.patch('fleet.views._runtime_capability_reconcile_for_agent_emails') as reconcile:
            resp = self.view(
                self._request('post', {'action': 'add', 'team_id': 'marketing'}),
                agent_id='rin-id',
            )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(posted['done'])
        self.assertTrue(resp.data['teams'][0]['selected'])
        reconcile.assert_called_once()
        self.assertEqual(reconcile.call_args.args[1], {'rin@shizuha.com'})
        self.assertEqual(reconcile.call_args.kwargs['context']['reason'], 'team-placement:add')

    @override_settings(HIVE_ADMIN_API_URL='http://admin/api', HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'rin-id',
        'agent_username': 'rin',
        'email': 'rin@shizuha.com',
    })
    @mock.patch('fleet.views.requests.request')
    def test_missed_push_converges_old_replica_snapshot_after_roster_mutation(
            self, request_mock, _agent):
        cache.clear()
        old_index = {
            'rin@shizuha.com': [
                {'team_slug': 'engineering', 'team_name': 'Engineering'},
            ],
        }
        old_built_at = time.time() - _ROSTER_PROCESS_RECHECK_AGE_S - 1
        _roster_process_write(old_index, old_built_at)
        posted = {'done': False}

        def fake_request(method, url, **kwargs):
            if method == 'POST' and url == 'http://pulse/api/admin/team-roster/members/':
                posted['done'] = True
                return _response(200, {'changed': True, 'team_name': 'marketing'})
            if method == 'GET' and url == 'http://pulse/api/admin/team-roster/members/':
                memberships = [{'team_name': 'engineering'}]
                if posted['done']:
                    memberships.append({'team_name': 'marketing'})
                return _response(200, {
                    'memberships': memberships,
                    'rosters': [{'team_name': 'engineering'}, {'team_name': 'marketing'}],
                })
            raise AssertionError(f'unexpected roster call {method} {url}')

        request_mock.side_effect = fake_request
        with mock.patch('fleet.views._runtime_capability_reconcile_for_agent_emails') as reconcile:
            reconcile.return_value = {'errors': [{'error': 'missed daemon push'}]}
            resp = self.view(
                self._request('post', {'action': 'add', 'team_id': 'marketing'}),
                agent_id='rin-id',
            )
        self.assertEqual(resp.status_code, 200, resp.data)

        # Simulate the other Daphne replica retaining its pre-mutation process
        # copy. The shared per-email snapshot must repair its later daemon read.
        _roster_process_write(old_index, old_built_at)
        with mock.patch('fleet.views._pulse_roster_memberships') as live_roster:
            memberships, diagnostic, error = _cached_pulse_roster_memberships_for_daemon(
                'rin@shizuha.com',
            )

        self.assertIsNone(error)
        self.assertEqual(memberships, {'engineering', 'marketing'})
        self.assertEqual(diagnostic['source'], 'redis')
        live_roster.assert_not_called()

    def test_recovered_old_shared_snapshot_cannot_roll_back_newer_process_mutation(self):
        cache.clear()
        email = 'rin@shizuha.com'
        now = time.time()
        old_built_at = now - _ROSTER_PROCESS_RECHECK_AGE_S - 30
        old_rows = [{'team_slug': 'engineering', 'team_name': 'Engineering'}]
        new_rows = old_rows + [{'team_slug': 'marketing', 'team_name': 'Marketing'}]
        cache.set(
            _roster_membership_cache_key(email),
            {'rows': old_rows, 'built_at': old_built_at},
            300,
        )
        _roster_process_write({email: old_rows}, old_built_at)

        # The roster mutation succeeds locally while its Redis publish fails,
        # leaving the old shared value behind during the outage.
        with mock.patch('fleet.views.time.time', return_value=now), \
                mock.patch('fleet.views._django_cache.set', side_effect=TimeoutError('redis down')):
            _roster_membership_cache_write(email, new_rows)

        # Redis later recovers with that older value. Once the normal recheck
        # interval elapses, the daemon read must preserve the newer local grant.
        with mock.patch('fleet.views.time.time', return_value=now + _ROSTER_PROCESS_RECHECK_AGE_S + 1):
            memberships, diagnostic, error = _cached_pulse_roster_memberships_for_daemon(email)

        self.assertIsNone(error)
        self.assertEqual(memberships, {'engineering', 'marketing'})
        self.assertEqual(diagnostic['source'], 'process')

    @override_settings(HIVE_ADMIN_API_URL='http://admin/api', HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views._nondb_agent', return_value={
        'id': 'rin-id',
        'agent_username': 'rin',
        'email': 'rin@shizuha.com',
    })
    @mock.patch('fleet.views.requests.request')
    def test_remove_deletes_existing_team_membership(self, request_mock, _agent):
        deleted = {'done': False}

        def fake_request(method, url, **kwargs):
            if method == 'POST' and url == 'http://pulse/api/admin/team-roster/members/':
                _assert_roster_command(self, kwargs['json'], {
                    'action': 'remove', 'team_name': 'marketing',
                    'email': 'rin@shizuha.com', 'organization_slug': 'shizuha',
                })
                deleted['done'] = True
                return _response(200, {'changed': True, 'team_name': 'marketing'})
            if method == 'GET' and url == 'http://pulse/api/admin/team-roster/members/':
                memberships = ([] if deleted['done'] else [
                    {'team_name': 'marketing', 'organization_slug': 'shizuha'},
                ])
                return _response(200, {
                    'memberships': memberships,
                    'rosters': [{'team_name': 'marketing', 'organization_slug': 'shizuha'}],
                })
            raise AssertionError(f'unexpected roster call {method} {url}')

        request_mock.side_effect = fake_request

        resp = self.view(self._request('post', {'action': 'remove', 'team_id': 'marketing'}), agent_id='rin-id')

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(deleted['done'])
        self.assertFalse(resp.data['teams'][0]['selected'])

    @override_settings(HIVE_PULSE_API_URL='http://pulse/api')
    @mock.patch('fleet.views.ensure_team_required_capability')
    @mock.patch('fleet.views.requests.request')
    def test_create_team_posts_empty_roster_to_pulse(self, request_mock, _ensure_capability):
        request_mock.return_value = _response(200, {'team_name': 'social-media', 'tiers': []})
        view = FleetTeamRosterView.as_view()

        resp = view(self._request('post', {'team_name': 'Social Media'}))

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['team']['id'], 'social-media')
        _assert_roster_command(self, request_mock.call_args.kwargs['json'], {
            'action': 'create',
            'team_name': 'social-media',
        })

    @override_settings(HIVE_PULSE_API_URL='http://pulse/api')
    def test_roster_write_sends_current_version_and_retries_stale_cas(self):
        """PLS-745 contract: the write carries the roster's live version; a
        stale-CAS 409 is retried ONCE with the authoritative version from the
        conflict detail and a fresh command_id."""
        from fleet.views import _write_pulse_roster_membership
        seen_posts = []

        def fake_request(method, url, **kwargs):
            if method == 'GET':
                return _response(200, {
                    'memberships': [],
                    'rosters': [{
                        'team_name': 'marketing',
                        'organization_slug': 'shizuha',
                        'version': 4,
                    }],
                })
            seen_posts.append(kwargs['json'])
            if len(seen_posts) == 1:
                return _response(409, {
                    'error': 'expected_version_conflict',
                    'detail': 'Roster version is 6, not 4.',
                })
            return _response(200, {'changed': True, 'version': 7})

        request = self._request('post', {})
        with mock.patch('fleet.views.requests.request', side_effect=fake_request):
            result, error = _write_pulse_roster_membership(
                request,
                action='add',
                team_name='marketing',
                email='rin@shizuha.com',
                org_slug='shizuha',
            )

        self.assertIsNone(error)
        self.assertEqual(result.get('version'), 7)
        self.assertEqual(len(seen_posts), 2)
        self.assertEqual(seen_posts[0]['expected_version'], 4)
        self.assertEqual(seen_posts[1]['expected_version'], 6)
        self.assertNotEqual(seen_posts[0]['command_id'], seen_posts[1]['command_id'])


    @override_settings(HIVE_ADMIN_API_URL='http://admin/api', HIVE_PULSE_API_URL='http://pulse/api', CACHES=_SNAPSHOT_LOCMEM)
    @mock.patch('fleet.views.requests.request')
    def test_pulse_snapshot_selected_org_returns_pulse_authority_directly(self, request_mock):
        from django.core.cache import cache as djcache
        djcache.clear()

        def fake_request(method, url, **kwargs):
            if method == 'GET' and str(url).endswith('/admin/org-chart/snapshot/'):
                # HIVE-609: workload comes from Pulse's own org-scoped snapshot
                # aggregation (full DB visibility) — never from caller-membership-
                # filtered /projects/ + /items/ listings. Pulse's global teams must
                # still not leak into the selected-org team list.
                params = kwargs.get('params') or {}
                if params.get('organization_slug') == 'acme':
                    return _response(200, {
                        'teams': [{
                            'id': 'accounting', 'slug': 'accounting', 'name': 'Accounting',
                            'organization_slug': 'acme',
                        }],
                        'workload': {'acct@shizuha.com': {'open': 1, 'completed': 1}},
                        'team_workload': {
                            'accounting': {'open': 1, 'completed': 1},
                        },
                        'workload_by_assignee_team': {
                            'acct@shizuha.com::accounting': {'open': 1, 'completed': 1},
                        },
                        'availability': {'acct@shizuha.com': 'available'},
                    })
            raise AssertionError(f'unexpected snapshot call {method} {url}')

        request_mock.side_effect = fake_request
        view = FleetPulseSnapshotView.as_view()
        resp = view(self._request('get', {'org_slug': 'acme'}))

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['organization_slug'], 'acme')
        self.assertEqual([team['slug'] for team in resp.data['teams']], ['accounting'])
        self.assertEqual(resp.data['teams'][0]['organization_slug'], 'acme')
        accounting_id = resp.data['teams'][0]['id']
        self.assertEqual(resp.data['team_workload'][accounting_id]['open'], 1)
        self.assertEqual(resp.data['team_workload'][accounting_id]['completed'], 1)
        self.assertEqual(resp.data['workload']['acct@shizuha.com']['open'], 1)



    @override_settings(HIVE_PULSE_API_URL='http://pulse/api', CACHES=_SNAPSHOT_LOCMEM)
    @mock.patch('fleet.views.requests.request')
    def test_pulse_snapshot_serves_stale_on_pulse_error(self, request_mock):
        # Stale-while-revalidate contract: after the fresh window lapses, a
        # request is answered instantly from last-good even while Pulse is
        # erroring (the failing rebuild happens off-path in the background).
        from django.core.cache import cache as djcache
        from fleet import views
        djcache.clear()
        views._PULSE_SNAPSHOT_STALE.clear()
        request_mock.side_effect = lambda method, url, **kwargs: (
            _response(200, {
                'teams': [{'id': 'engineering', 'name': 'Engineering'}],
                'nodes': [{'email': 'ryo@shizuha.com'}],
                'workload': {'ryo@shizuha.com': {'open': 3}},
            }) if request_mock.call_count == 1 else _response(503, {'error': 'pulse unavailable'})
        )
        view = FleetPulseSnapshotView.as_view()

        first = view(self._request('get'))
        djcache.delete('rmc:fresh:pulse-snapshot:global')  # lapse the fresh window
        second = view(self._request('get'))

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second['X-Hive-Cache'], 'stale-revalidating')
        self.assertEqual(second.data['workload']['ryo@shizuha.com']['open'], 3)
        self.assertEqual(second.data['teams'][0]['id'], 'engineering')

    @override_settings(HIVE_PULSE_API_URL='http://pulse/api', CACHES=_SNAPSHOT_LOCMEM)
    @mock.patch('fleet.views.requests.request')
    def test_pulse_snapshot_marks_old_lastgood_degraded(self, request_mock):
        # PLAT-4169: degraded now requires a real FAILED revalidate, not age
        # alone. The first request only serves stale + spawns the failing
        # rebuild; we wait for it (marker set), then a second request must
        # deterministically annotate degraded. (Was racy: a single request
        # assumed the child thread had already set the marker.)
        import time as _time
        from django.core.cache import cache as djcache
        from fleet import views, read_model_cache
        djcache.clear()
        views._PULSE_SNAPSHOT_STALE.clear()
        djcache.delete('rmc:revalfail:pulse-snapshot:global')
        djcache.set('rmc:lastgood:pulse-snapshot:global', {
            'payload': {'teams': [{'id': 'engineering'}], 'nodes': [{'email': 'ryo@shizuha.com'}],
                        'workload': {'ryo@shizuha.com': {'open': 1}}},
            'built_at': _time.time() - (read_model_cache.DEGRADED_AGE_S + 60),
        }, 600)
        request_mock.return_value = _response(503, {'error': 'pulse unavailable'})
        view = FleetPulseSnapshotView.as_view()

        first = view(self._request('get'))  # serves stale, spawns failing revalidate
        self.assertEqual(first.status_code, 200, first.data)
        _wait_revalidate_done('pulse-snapshot:global')
        djcache.delete('rmc:fresh:pulse-snapshot:global')
        second = view(self._request('get'))

        self.assertEqual(second.status_code, 200, second.data)
        self.assertTrue(second.data['pulse_degraded'])
        self.assertEqual(second.data['pulse_degraded_source'], 'read-model-cache')
        self.assertGreaterEqual(second.data['pulse_stale_age_seconds'], read_model_cache.DEGRADED_AGE_S)
        self.assertEqual(second.data['teams'][0]['id'], 'engineering')

    @override_settings(HIVE_PULSE_API_URL='http://pulse/api', CACHES=_SNAPSHOT_LOCMEM)
    @mock.patch('fleet.views.requests.request')
    def test_pulse_snapshot_old_lastgood_healthy_revalidate_not_degraded(self, request_mock):
        # PLAT-4169 (the reported bug, at view level): an old idle last-good with a
        # HEALTHY background revalidate must NOT render the degraded banner.
        import time as _time
        from django.core.cache import cache as djcache
        from fleet import views, read_model_cache
        djcache.clear()
        views._PULSE_SNAPSHOT_STALE.clear()
        djcache.delete('rmc:revalfail:pulse-snapshot:global')
        djcache.set('rmc:lastgood:pulse-snapshot:global', {
            'payload': {'teams': [{'id': 'engineering'}], 'nodes': [{'email': 'ryo@shizuha.com'}],
                        'workload': {'ryo@shizuha.com': {'open': 1}}},
            'built_at': _time.time() - (read_model_cache.DEGRADED_AGE_S + 60),
        }, 600)
        # Healthy Pulse -> revalidate succeeds with a complete snapshot.
        request_mock.return_value = _response(200, {
            'teams': [{'id': 'engineering'}], 'nodes': [{'email': 'ryo@shizuha.com'}],
            'workload': {'ryo@shizuha.com': {'open': 2}},
        })
        view = FleetPulseSnapshotView.as_view()

        first = view(self._request('get'))  # serves old stale, spawns healthy revalidate
        self.assertEqual(first.status_code, 200, first.data)
        # First hit is stale-and-idle but the fetcher is healthy -> no banner.
        self.assertFalse(first.data.get('pulse_degraded', False))
        _wait_revalidate_done('pulse-snapshot:global')
        self.assertFalse(read_model_cache.revalidate_failing('pulse-snapshot:global'))

    @override_settings(HIVE_PULSE_API_URL='http://pulse/api', CACHES=_SNAPSHOT_LOCMEM)
    @mock.patch('fleet.views.requests.request')
    def test_pulse_snapshot_empty_workload_build_is_failed_not_cached(self, request_mock):
        # PLAT-4169 (operator): a build with nodes but empty workload/team_workload
        # is a failed sub-aggregation — it must not be cached as fresh or remembered
        # as last-good; the prior good last-good keeps serving, flagged degraded.
        import time as _time
        from django.core.cache import cache as djcache
        from fleet import views, read_model_cache
        djcache.clear()
        views._PULSE_SNAPSHOT_STALE.clear()
        djcache.delete('rmc:revalfail:pulse-snapshot:global')
        good = {'teams': [{'id': 'engineering'}], 'nodes': [{'email': 'ryo@shizuha.com'}],
                'workload': {'ryo@shizuha.com': {'open': 5}}}
        djcache.set('rmc:lastgood:pulse-snapshot:global', {
            'payload': good, 'built_at': _time.time() - (read_model_cache.DEGRADED_AGE_S + 60),
        }, 600)
        # Pulse returns 200 but with an EMPTY workload while nodes are present.
        request_mock.return_value = _response(200, {'nodes': [{'email': 'ryo@shizuha.com'}], 'workload': {}})
        view = FleetPulseSnapshotView.as_view()

        first = view(self._request('get'))  # serves good stale, spawns the empty (failed) build
        self.assertEqual(first.status_code, 200, first.data)
        _wait_revalidate_done('pulse-snapshot:global')
        # The empty build must have been treated as a failure...
        self.assertTrue(read_model_cache.revalidate_failing('pulse-snapshot:global'))
        # ...and NOT cached as fresh (no rmc:fresh key with the empty payload).
        self.assertIsNone(djcache.get('rmc:fresh:pulse-snapshot:global'))
        djcache.delete('rmc:fresh:pulse-snapshot:global')
        second = view(self._request('get'))
        # Still serving the GOOD last-good (workload intact), now flagged degraded.
        self.assertEqual(second.data['workload']['ryo@shizuha.com']['open'], 5)
        self.assertTrue(second.data['pulse_degraded'])
        # rei P2 hygiene: the second request spawned another (failing) revalidate;
        # drain it before the cache override / request mock are torn down so the
        # background thread cannot leak into a later test.
        _wait_revalidate_done('pulse-snapshot:global')

    @override_settings(HIVE_PULSE_API_URL='http://pulse/api', CACHES=_SNAPSHOT_LOCMEM)
    @mock.patch('fleet.views.requests.request')
    def test_pulse_snapshot_cold_start_incomplete_build_is_flagged_not_silent(self, request_mock):
        # PLAT-4169 (rei P1): with NO usable last-good, an incomplete live build
        # (nodes + empty workload) must be flagged degraded on the cold-start
        # fallback too — never returned as a clean empty-workload success. Two
        # consecutive incomplete upstream responses stay flagged, not silently fresh.
        from django.core.cache import cache as djcache
        from fleet import views
        djcache.clear()
        views._PULSE_SNAPSHOT_STALE.clear()
        djcache.delete('rmc:revalfail:pulse-snapshot:global')
        request_mock.return_value = _response(200, {'nodes': [{'email': 'ryo@shizuha.com'}], 'workload': {}})
        view = FleetPulseSnapshotView.as_view()

        first = view(self._request('get'))   # cold miss: inline build raises -> except fallback
        second = view(self._request('get'))  # still cold (incomplete never cached)

        for label, resp in (('first', first), ('second', second)):
            self.assertEqual(resp.status_code, 200, resp.data)
            self.assertTrue(resp.data.get('pulse_degraded'),
                            f'{label} incomplete cold build must be flagged, not silently fresh')
            self.assertEqual(resp.data.get('pulse_degraded_source'), 'incomplete-build')
            self.assertFalse(resp.data.get('workload'))  # empty workload, but explicitly flagged

    def test_pulse_snapshot_completeness_guard_unit(self):
        from fleet.views import _snapshot_is_complete
        self.assertTrue(_snapshot_is_complete({'nodes': [], 'workload': {}}))  # no nodes -> n/a
        self.assertTrue(_snapshot_is_complete({'nodes': [1], 'workload': {'a': {}}}))
        self.assertTrue(_snapshot_is_complete({'nodes': [1], 'team_workload': {'t': {}}}))
        self.assertFalse(_snapshot_is_complete({'nodes': [1], 'workload': {}, 'team_workload': {}}))
        self.assertFalse(_snapshot_is_complete(None))

    @override_settings(HIVE_PULSE_API_URL='http://pulse/api', CACHES=_SNAPSHOT_LOCMEM)
    @mock.patch('fleet.views.requests.request')
    def test_pulse_snapshot_is_proxied_same_origin(self, request_mock):
        from django.core.cache import cache as djcache
        djcache.clear()
        request_mock.return_value = _response(200, {'teams': [{'id': 'analytics'}], 'nodes': []})
        view = FleetPulseSnapshotView.as_view()

        resp = view(self._request('get'))

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['teams'][0]['id'], 'analytics')
        self.assertEqual(request_mock.call_args.args[:2], ('GET', 'http://pulse/api/admin/org-chart/snapshot/'))
