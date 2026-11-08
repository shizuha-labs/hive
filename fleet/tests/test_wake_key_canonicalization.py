"""PLAT-11902/PLAT-12264: one deferred-wake registry key per seat.

Dual-spelling wake requests (``user@shizuha.com`` vs
``user@agents.shizuha.io`` — both valid per `lifecycle_agent_emails`)
previously split one seat's starvation into TWO independent registry
entries with independent clocks: a seat waking through one spelling left
the other spelling's entry aging until the 24h registry-rot bound, paging
a principal that holds no runtime records (observed 2026-11-08: banto
paged 21h13m on the @shizuha.com spelling while the pod was turning).
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from fleet import wake_defers
from fleet.on_demand import canonical_wake_key, lifecycle_agent_emails
from fleet.wake_defers import outstanding_wake_defers, reconcile_deferred_wakes

NOW = 1_000_000.0


def _agent(username='kai', email='kai@shizuha.com', enabled=False, eligible=True):
    return {
        'id': username, 'agent_username': username, 'username': username,
        'email': email, 'enabled': enabled,
        'eligible_to_start': eligible,
    }


class CanonicalWakeKeyTests(TestCase):
    def test_primary_email_is_canonical_regardless_of_request_spelling(self):
        agent = _agent('kai', 'kai@shizuha.com')
        self.assertEqual(canonical_wake_key(agent), 'kai@shizuha.com')

    def test_both_spellings_match_the_same_seat(self):
        agent = _agent('kai', 'kai@shizuha.com')
        emails = lifecycle_agent_emails(agent)
        self.assertIn('kai@shizuha.com', emails)
        self.assertIn('kai@agents.shizuha.io', emails)

    def test_missing_email_falls_back_to_derived_primary(self):
        # agent_email derives username@shizuha.com when the field is absent —
        # the canonical key stays deterministic per seat either way.
        agent = {'agent_username': 'kai', 'username': 'kai', 'enabled': False}
        self.assertEqual(canonical_wake_key(agent), 'kai@shizuha.com')

    def test_empty_agent_yields_empty_key(self):
        self.assertEqual(canonical_wake_key({}), '')


class DualSpellingConvergenceTests(TestCase):
    """A seat paged under one spelling must be cleared under BOTH spellings
    once it is observed running (or archived / operator-stopped), and the
    canonical key is the only key new deferrals are recorded under."""

    def setUp(self):
        wake_defers._save({'defers': {}})

    def tearDown(self):
        wake_defers._save({'defers': {}})

    def test_running_seat_clears_both_spelling_entries(self):
        # Pre-canonicalization state: two entries, one per spelling.
        wake_defers.record_wake_defer('kai@shizuha.com', now=NOW - 600)
        wake_defers.record_wake_defer('kai@agents.shizuha.io', now=NOW - 300)
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=[_agent('kai', 'kai@shizuha.com', enabled=True)]), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(True, 'ok')):
            result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(result['skipped']['kai@shizuha.com'], 'already_running')
        self.assertEqual(result['skipped']['kai@agents.shizuha.io'], 'already_running')
        self.assertEqual(outstanding_wake_defers(now=NOW), {})

    def test_archived_seat_clears_both_spelling_entries(self):
        wake_defers.record_wake_defer('kai@shizuha.com', now=NOW - 600)
        wake_defers.record_wake_defer('kai@agents.shizuha.io', now=NOW - 600)
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=[_agent('kai', 'kai@shizuha.com')]), \
             mock.patch('fleet.views._is_archived_username',
                        side_effect=lambda u: u == 'kai'), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(True, 'ok')):
            result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(result['skipped']['kai@shizuha.com'], 'archived')
        self.assertEqual(outstanding_wake_defers(now=NOW), {})

    def test_lease_unavailable_keeps_entries_and_clock(self):
        wake_defers.record_wake_defer('kai@agents.shizuha.io', now=NOW - 600)
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=[_agent('kai', 'kai@shizuha.com')]), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(False, 'lease_pool_full')):
            result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(result['granted'], [])
        self.assertIn('lease unavailable', result['skipped']['kai@agents.shizuha.io'])
        # The starvation clock started at the first defer and survives.
        self.assertEqual(outstanding_wake_defers(now=NOW),
                         {'kai@agents.shizuha.io': NOW - 600})

    def test_grant_reports_recorded_key_and_clears_both(self):
        wake_defers.record_wake_defer('kai@agents.shizuha.io', now=NOW - 600)
        with mock.patch('fleet.lifecycle_fleet.list_lifecycle_agents',
                               return_value=[_agent('kai', 'kai@shizuha.com')]), \
             mock.patch('fleet.lifecycle_fleet.set_lifecycle_agent_enabled',
                               return_value=(True, 'started')), \
             mock.patch('fleet.model_lease.lease_wake_available',
                        return_value=(True, 'ok')):
            result = reconcile_deferred_wakes(now=NOW)
        self.assertEqual(result['granted'], ['kai@agents.shizuha.io'])
        self.assertEqual(outstanding_wake_defers(now=NOW), {})
