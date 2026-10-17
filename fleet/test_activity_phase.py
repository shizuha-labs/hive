from django.test import SimpleTestCase

from fleet.activity_phase import (
    annotate_activity,
    is_lease_idle_waste,
    is_productive_activity,
    resolve_activity_phase,
)


class ActivityPhaseTests(SimpleTestCase):
    def test_prefers_fine_phase(self):
        self.assertEqual(
            resolve_activity_phase({'activity_state': 'working', 'activity_phase': 'thinking'}),
            'thinking',
        )
        self.assertEqual(
            resolve_activity_phase({'activity_phase': 'waiting_first_model_event'}),
            'thinking',
        )
        self.assertEqual(resolve_activity_phase({'activity_state': 'idle'}), 'idle')
        self.assertEqual(resolve_activity_phase({'activity_state': 'working'}), 'working')

    def test_productive_includes_new_phases(self):
        self.assertTrue(is_productive_activity({'activity_phase': 'thinking'}))
        self.assertTrue(is_productive_activity({'activity_phase': 'responding'}))
        self.assertTrue(is_productive_activity({'activity_phase': 'tool'}))
        self.assertTrue(is_productive_activity({'activity_state': 'working'}))
        self.assertFalse(is_productive_activity({'activity_state': 'idle'}))

    def test_lease_idle_waste(self):
        holder = {
            'status': 'running',
            'activity_state': 'idle',
            'activity_phase': 'idle',
            'activity_phase_for_ms': 45_000,
            'model_lease': {'state': 'holder', 'managed': True},
        }
        self.assertTrue(is_lease_idle_waste(holder))
        self.assertFalse(is_lease_idle_waste({**holder, 'activity_phase_for_ms': 5_000}))
        self.assertFalse(is_lease_idle_waste({**holder, 'model_lease': {'state': 'waiting'}}))
        self.assertFalse(is_lease_idle_waste({
            **holder,
            'activity_state': 'working',
            'activity_phase': 'thinking',
        }))

    def test_annotate_stamps_normalized_fields(self):
        agent = {
            'status': 'running',
            'activity_state': 'idle',
            'activity_phase_for_ms': 80_000,
            'model_lease': {'state': 'holder'},
        }
        annotate_activity(agent)
        self.assertEqual(agent['activity_phase'], 'idle')
        self.assertTrue(agent['lease_idle_waste'])
