"""PLAT-13883: the reconcile hook lives at the release primitive.

The event-path reconcile hooks (stop/hibernate call sites) miss slot-frees
that happen through other release callers — notably the periodic ghost-lease
reaper (``reconcile_ghost_lease_holders``) and any future release caller.
Hooking ``reconcile_deferred_wakes`` inside ``release_leases_for_agent``
itself makes "a slot freed" and "deferred wakes reconsidered" the same
event, on every path.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from fleet import model_lease, wake_defers


class PostReleaseReconcileHookTests(TestCase):
    def setUp(self):
        wake_defers._save({'defers': {}})

    def tearDown(self):
        wake_defers._save({'defers': {}})

    def _release_with_success(self):
        """Drive release_leases_for_agent through one successful revoke."""
        board = {'holders': [{'principal': 'agent-kai'}]}
        with mock.patch.object(model_lease, 'all_pool_configs',
                               return_value=[{'model_id': 'glm-5.3'}]), \
             mock.patch.object(model_lease, 'CortexLeaseClient') as client_cls, \
             mock.patch.object(model_lease, '_record_holding'), \
             mock.patch('fleet.wake_defers.reconcile_deferred_wakes') as recon:
            client = client_cls.return_value
            client.yield_homes.return_value = False
            client.board.return_value = board
            client.revoke.return_value = True
            result = model_lease.release_leases_for_agent(
                'kai', client=client, reason='test')
            return result, recon

    def test_successful_release_fires_reconcile(self):
        result, recon = self._release_with_success()
        self.assertEqual(result['released'], ['glm-5.3'])
        recon.assert_called_once_with()

    def test_noop_release_does_not_fire_reconcile(self):
        """Nothing freed (no holdings, healthy path) — no reconcile work."""
        with mock.patch.object(model_lease, 'all_pool_configs',
                               return_value=[{'model_id': 'glm-5.3'}]), \
             mock.patch.object(model_lease, 'CortexLeaseClient') as client_cls, \
             mock.patch('fleet.wake_defers.reconcile_deferred_wakes') as recon:
            client = client_cls.return_value
            client.yield_homes.return_value = False
            # Board has no holders for this principal -> pure no-op.
            client.board.return_value = {'holders': [{'principal': 'agent-x'}]}
            model_lease.release_leases_for_agent('kai', client=client)
        recon.assert_not_called()

    def test_reconcile_fault_never_breaks_release(self):
        """A reconcile fault is logged, not raised — release must never fail."""
        board = {'holders': [{'principal': 'agent-kai'}]}
        with mock.patch.object(model_lease, 'all_pool_configs',
                               return_value=[{'model_id': 'glm-5.3'}]), \
             mock.patch.object(model_lease, 'CortexLeaseClient') as client_cls, \
             mock.patch('fleet.wake_defers.reconcile_deferred_wakes',
                        side_effect=RuntimeError('boom')):
            client = client_cls.return_value
            client.yield_homes.return_value = False
            client.board.return_value = board
            client.revoke.return_value = True
            result = model_lease.release_leases_for_agent(
                'kai', client=client, reason='test')
        self.assertEqual(result['released'], ['glm-5.3'])
