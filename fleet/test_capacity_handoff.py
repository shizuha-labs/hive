from copy import deepcopy
from unittest.mock import Mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .model_lease import _reconcile_one_pool


@override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    'LOCATION': 'capacity-handoff-caller',
}})
class CapacityHandoffCallerTests(SimpleTestCase):
    class Client:
        def __init__(self):
            self.data = {
                'max_holders': 3,
                'admission_capacity': 4,
                'holders': [
                    {'principal': 'agent-mio', 'lease_id': 'mio-1', 'idle_for_s': 90, 'held_for_s': 100},
                    {'principal': 'agent-ren', 'lease_id': 'ren-1', 'idle_for_s': 79, 'held_for_s': 100},
                    {'principal': 'agent-jun', 'lease_id': 'jun-1', 'idle_for_s': 77, 'held_for_s': 100},
                ],
                'residency': {'grantable': 0, 'full': False},
                'governed_principals': ['agent-mio', 'agent-ren', 'agent-jun', 'agent-ni'],
                'eligible_principals': ['agent-mio', 'agent-ren', 'agent-jun', 'agent-ni'],
            }
            self.revokes = []
            self.grants = []
            self.yields = []
            self.fail_revoke = set()

        def board(self, model):
            return deepcopy(self.data)

        def set_governed(self, model, principals):
            self.data['governed_principals'] = sorted(principals)
            return True

        def set_eligible(self, model, principals):
            self.data['eligible_principals'] = sorted(principals)
            return True

        def revoke(self, model, *, holder, **kwargs):
            self.revokes.append((holder, kwargs))
            current = next((entry for entry in self.data['holders'] if entry['principal'] == holder), None)
            if holder in self.fail_revoke or not current:
                return False
            if kwargs.get('expected_lease_id') and kwargs['expected_lease_id'] != current['lease_id']:
                return False
            self.data['holders'] = [entry for entry in self.data['holders'] if entry['principal'] != holder]
            return True

        def grant(self, model, holder, **kwargs):
            self.grants.append((holder, kwargs))
            if len(self.data['holders']) >= kwargs['max_holders']:
                return False
            self.data['holders'].append({'principal': holder, 'lease_id': holder + '-new'})
            return True

        def yield_homes(self, model, principal, **kwargs):
            self.yields.append(principal)
            return True

    def setUp(self):
        cache.clear()
        self.client = self.Client()
        self.cfg = {
            'enabled': True, 'model_id': 'GLM-5.3-Flash',
            'members': ['mio', 'ren', 'jun', 'ni'], 'max_holders': 3,
            'min_hold_seconds': 900, 'max_hold_seconds': 2700, 'idle_release_seconds': 180,
        }
        self.agents = [
            {'username': name, 'email': f'{name}@shizuha.com',
             'status': 'running' if name != 'ni' else 'hibernated', 'enabled': True,
             'busy': name != 'ni'} for name in self.cfg['members']
        ]

    def tearDown(self):
        cache.clear()

    def tick(self, can_handoff, now=10000, apply=True):
        return _reconcile_one_pool(
            cfg=self.cfg, agents=self.agents, email_to_teams={}, workload_by_team={},
            workload_by_email={f'{name}@shizuha.com': {'ready': 1} for name in self.cfg['members']},
            actionable_buckets=('ready',), client=self.client, now=now, apply=apply,
            can_handoff=can_handoff,
        )

    def test_active_keep_survives_capacity_shrink_and_rearms_after_idle(self):
        busy = Mock(return_value=(False, 'runtime reports working at apply time'))
        first = self.tick(busy)
        self.assertIn('KEEP holder=agent-mio reason=holder runtime is actively working', ' '.join(first['log']))
        self.client.data['admission_capacity'] = 3
        second = self.tick(busy, now=10001)
        self.assertEqual(self.client.revokes, [])
        self.assertEqual(self.client.grants, [])
        self.assertEqual(set(second['holders']), {'agent-mio', 'agent-ren', 'agent-jun'})
        self.assertTrue(second['scale']['mio'])
        self.assertIn('DEFER REVOKE holder=agent-mio', ' '.join(second['log']))
        third = self.tick(lambda name: (name == 'mio', 'fresh idle' if name == 'mio' else 'busy'), now=10002)
        self.assertEqual([holder for holder, _kwargs in self.client.revokes], ['agent-mio'])
        self.assertEqual(self.client.revokes[0][1]['force'], False)
        self.assertEqual(self.client.revokes[0][1]['expected_lease_id'], 'mio-1')
        self.assertNotIn('agent-mio', third['holders'])
        self.assertEqual(self.client.grants, [])

    def test_busy_first_candidate_does_not_hide_idle_alternative(self):
        self.client.data['admission_capacity'] = 3
        checker = Mock(side_effect=lambda name: (name == 'jun', 'fresh idle' if name == 'jun' else 'busy'))
        result = self.tick(checker)
        self.assertEqual([holder for holder, _kwargs in self.client.revokes], ['agent-jun'])
        self.assertEqual([call.args[0] for call in checker.call_args_list], ['mio', 'ren', 'jun'])
        self.assertEqual(set(result['holders']), {'agent-mio', 'agent-ren'})
        self.assertEqual([principal for principal in self.client.yields if principal != 'agent-ni'], ['agent-jun'])
        self.assertEqual(self.client.grants, [])

    def test_unknown_or_failed_idle_confirmation_never_revokes(self):
        self.client.data['admission_capacity'] = 3
        for checker in (None, Mock(return_value=(False, 'unavailable')),
                        Mock(side_effect=RuntimeError('transport unavailable')),
                        Mock(return_value=('idle', 'malformed bool'))):
            with self.subTest(checker=checker):
                result = self.tick(checker)
                self.assertEqual(self.client.revokes, [])
                self.assertEqual(self.client.grants, [])
                self.assertTrue(result['scale']['mio'])

    def test_replaced_lease_after_idle_check_not_revoked(self):
        self.client.data['admission_capacity'] = 3

        def checked(name):
            if name == 'mio':
                self.client.data['holders'][0]['lease_id'] = 'mio-2'
                return True, 'idle at observation'
            return False, 'busy'

        result = self.tick(checked)
        self.assertIn('agent-mio', result['holders'])
        self.assertEqual(self.client.data['holders'][0]['lease_id'], 'mio-2')
        self.assertNotIn('agent-mio', self.client.yields)
        self.assertEqual(self.client.grants, [])

    def test_missing_or_empty_lease_identity_never_revokes(self):
        for lease_id in (None, '', '   '):
            with self.subTest(lease_id=lease_id):
                self.client = self.Client()
                self.client.data['admission_capacity'] = 3
                for holder in self.client.data['holders']:
                    if lease_id is None:
                        holder.pop('lease_id')
                    else:
                        holder['lease_id'] = lease_id
                result = self.tick(lambda name: (True, 'fresh idle'))
                self.assertEqual(self.client.revokes, [])
                self.assertEqual(self.client.grants, [])
                self.assertEqual(set(result['holders']), {'agent-mio', 'agent-ren', 'agent-jun'})
                self.assertIn('observed lease identity unavailable', ' '.join(result['log']))

    def test_missing_lease_identity_does_not_hide_exact_idle_alternative(self):
        self.client.data['admission_capacity'] = 3
        self.client.data['holders'][0].pop('lease_id')
        result = self.tick(lambda name: (True, 'fresh idle'))
        self.assertEqual([holder for holder, _kwargs in self.client.revokes], ['agent-ren'])
        self.assertEqual(self.client.revokes[0][1]['expected_lease_id'], 'ren-1')
        self.assertIn('agent-mio', result['holders'])
        self.assertNotIn('agent-ren', result['holders'])
        self.assertEqual(self.client.grants, [])

    def test_expired_nonholder_hint_cannot_override_busy_current_holder(self):
        cache.set('fleet:model-lease-nonholder-active:glm-5.3-flash', {'mio': 1}, 3600)
        self.client.data['admission_capacity'] = 3
        result = self.tick(lambda name: (False, 'fresh busy'))
        self.assertIn('agent-mio', result['holders'])
        self.assertTrue(result['scale']['mio'])
        self.assertEqual(self.client.revokes, [])

    def test_current_holders_never_accrue_nonholder_grace(self):
        key = 'fleet:model-lease-nonholder-active:glm-5.3-flash'
        cache.set(key, {'mio': 1, 'ren': 2, 'jun': 3}, 3600)
        result = self.tick(lambda name: (False, 'busy'))
        self.assertEqual(cache.get(key), {})
        self.assertFalse(result['interrupt_ok'])
        self.assertNotIn('active-fence grace expired', ' '.join(result['log']))

    def test_nonholder_grace_starts_after_loss_not_while_holding(self):
        key = 'fleet:model-lease-nonholder-active:glm-5.3-flash'
        self.tick(lambda name: (False, 'busy'), now=10000)
        self.assertEqual(cache.get(key), {})
        self.client.data['holders'] = self.client.data['holders'][1:]
        self.client.data['admission_capacity'] = 3
        second = self.tick(lambda name: (False, 'busy'), now=10001)
        self.assertEqual(cache.get(key), {'mio': 10001})
        self.assertNotIn('mio', second['interrupt_ok'])
        third = self.tick(lambda name: (False, 'busy'), now=10902)
        self.assertIn('mio', third['interrupt_ok'])
        self.assertNotIn('ren', third['interrupt_ok'])
        self.assertNotIn('jun', third['interrupt_ok'])

    def test_unknown_board_does_not_age_or_clear_grace(self):
        key = 'fleet:model-lease-nonholder-active:glm-5.3-flash'
        cache.set(key, {'mio': 1}, 3600)
        self.client.board = Mock(return_value=None)
        result = self.tick(lambda name: (False, 'busy'))
        self.assertEqual(cache.get(key), {'mio': 1})
        self.assertEqual(result['scale'], {})

    def test_failed_revoke_keeps_owner_and_tries_another_idle_candidate(self):
        self.client.data['admission_capacity'] = 3
        self.client.fail_revoke.add('agent-mio')
        result = self.tick(lambda name: (True, 'fresh idle'))
        self.assertEqual([holder for holder, _kwargs in self.client.revokes], ['agent-mio', 'agent-ren'])
        self.assertIn('agent-mio', result['holders'])
        self.assertNotIn('agent-ren', result['holders'])
        self.assertNotIn('agent-mio', self.client.yields)
        self.assertEqual(self.client.grants, [])

    def test_reserved_waiter_does_not_bypass_deferred_capacity_trim(self):
        self.client.data['admission_capacity'] = 3
        self.agents[-1]['warm_pool_size'] = 1
        result = self.tick(lambda name: (False, 'busy'))
        self.assertEqual(self.client.revokes, [])
        self.assertEqual(self.client.grants, [])
        self.assertNotIn('agent-ni', result['holders'])
        self.assertIn('capacity trim awaiting safe handoff', ' '.join(result['log']))

    def test_dry_run_does_not_plan_revoking_known_active_holders(self):
        self.client.data['admission_capacity'] = 3
        checker = Mock(side_effect=AssertionError('dry-run must not call idle probe'))
        result = self.tick(checker, apply=False)
        self.assertEqual(self.client.revokes, [])
        self.assertEqual(self.client.grants, [])
        self.assertEqual(set(result['holders']), {'agent-mio', 'agent-ren', 'agent-jun'})
        checker.assert_not_called()

    def test_new_grant_clears_nonholder_clock_in_same_tick(self):
        key = 'fleet:model-lease-nonholder-active:glm-5.3-flash'
        self.client.data['holders'] = self.client.data['holders'][1:]
        self.client.data['residency']['grantable'] = 1
        cache.set(key, {'mio': 9999}, 3600)
        result = self.tick(lambda name: (False, 'busy'))
        self.assertIn('agent-mio', result['holders'])
        self.assertNotIn('mio', cache.get(key))
        self.assertNotIn('mio', result['interrupt_ok'])
