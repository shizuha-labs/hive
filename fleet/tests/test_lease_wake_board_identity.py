"""Exercise the real wake client and command against case-sensitive boards."""

from contextlib import ExitStack
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from fleet.management.commands.reconcile_on_demand_agents import ConnectLifecycleSnapshot
from fleet.model_lease import CortexLeaseClient, lease_wake_available


@override_settings(CORTEX_BASE_URL='https://cortex.invalid', HIVE_CORTEX_LEASE_API_KEY='test-only')
class LeaseWakeBoardIdentityTests(SimpleTestCase):
    models = ('GLM-5.3-Flash', 'DeepSeek-V4-Flash', 'Qwen3.8-27B-Q2')

    def config(self, model):
        return [{'model_id': model, 'enabled': True, 'members': ['sara'], 'max_holders': 3}]

    def agent(self, model):
        return {
            'id': 'id-sara', 'agent_username': 'sara',
            'email': 'sara@shizuha.com', 'model': model, 'effective_model': model,
            'enabled': False, 'status': 'hibernated', 'lifecycle_state': 'hibernated',
        }

    def board(self, model, holders):
        return {'model': model, 'leased': True, 'max_holders': 3,
                'holders': [{'principal': 'agent-' + name} for name in holders]}

    def response(self, board):
        return Mock(status_code=200, json=Mock(return_value=board))

    def test_join_normalization_never_changes_the_board_resource_id(self):
        for model in self.models:
            for spelling in (model, 'cortex/' + model, 'cortex/' + model.lower()):
                with self.subTest(model=model, spelling=spelling):
                    expected = 'https://cortex.invalid/v1/admin/model-leases/' + model + '/'
                    requests_seen = []

                    def request(method, url, **kwargs):
                        requests_seen.append((method, url))
                        # Production returns a plausible empty board for an unknown
                        # spelling; a constant fake board concealed the caller bug.
                        value = (self.board(model, ['ichi', 'kumo', 'sato'])
                                 if url == expected else
                                 {'model': model.lower(), 'leased': False,
                                  'max_holders': 1, 'holders': [], 'admission_capacity': 0})
                        return self.response(value)

                    with patch('requests.request', side_effect=request):
                        result = lease_wake_available(
                            agent=self.agent(spelling), configs=self.config(model),
                            client=CortexLeaseClient(),
                        )
                    self.assertEqual(requests_seen, [('GET', expected)])
                    self.assertEqual(result, (False, 'lease_pool_full'))

    def test_explicitly_unleased_board_cannot_authorize_a_governed_wake(self):
        model = self.models[0]
        with patch('requests.request', return_value=self.response({
            'model': model, 'leased': False, 'max_holders': 1, 'holders': [],
        })):
            result = lease_wake_available(
                agent=self.agent(model), configs=self.config(model), client=CortexLeaseClient(),
            )
        self.assertEqual(result, (False, 'board_not_governed'))

    def test_reconcile_timeout_fallback_observes_each_new_board_without_start_churn(self):
        model = self.models[0]
        prefix = 'fleet.management.commands.reconcile_on_demand_agents.'
        board_now = self.board(model, ['ichi', 'kumo', 'sato'])
        urls = []

        def request(method, url, **kwargs):
            self.assertEqual(method, 'GET')
            urls.append(url)
            if url != 'https://cortex.invalid/v1/admin/model-leases/' + model + '/':
                return self.response({'leased': False, 'max_holders': 1, 'holders': []})
            return self.response(board_now)

        replacements = {
            prefix + 'list_lifecycle_agents': [self.agent('cortex/' + model)],
            prefix + 'Command._fetch_pulse_rosters': [{
                'team_name': 'engineering', 'is_active': True,
                'tiers': [['sara@shizuha.com']],
            }],
            prefix + 'Command._fetch_pulse_snapshot': {
                'workload': {'sara@shizuha.com': {'in_progress': 1}},
                'team_workload': {'engineering': {'in_progress': 1}},
            },
            prefix + 'Command._fetch_connect_state': ConnectLifecycleSnapshot(
                available=True, unread_by_email={}, last_dm_at_by_email={},
            ),
            prefix + 'Command._apply_health_signals': None,
            'fleet.models.archived_usernames': set(),
            'fleet.external_agents.fresh_agent_telemetry': {},
            'fleet.model_serviceability.unserviceable_cortex_models': frozenset(),
            'fleet.model_lease.all_pool_configs': self.config(model),
        }
        with ExitStack() as stack:
            for target, value in replacements.items():
                stack.enter_context(patch(target, return_value=value))
            stack.enter_context(patch(prefix + 'reconcile_model_lease', side_effect=TimeoutError('lease cache unavailable')))
            stack.enter_context(patch('requests.request', side_effect=request))
            start = stack.enter_context(patch(prefix + 'set_lifecycle_agent_enabled', return_value=(True, 'started')))
            stop = stack.enter_context(patch(prefix + 'hibernate_lifecycle_agent'))
            # No sticky positive or negative result may survive the next board.
            for holders, should_start in [
                (['ichi', 'kumo', 'sato'], False),
                (['sara', 'kumo', 'sato'], True),
                (['ichi', 'kumo', 'sato'], False),
                (['ichi'], False),
                (['ichi', 'kumo', 'sato'], False),
            ]:
                board_now = self.board(model, holders)
                start.reset_mock()
                output = StringIO()
                call_command('reconcile_on_demand_agents', '--apply', stdout=output)
                if should_start:
                    start.assert_called_once_with('id-sara', True)
                else:
                    start.assert_not_called()
                    self.assertIn('DEFER START sara', output.getvalue())
            stop.assert_not_called()
        self.assertEqual(len(urls), 5)
