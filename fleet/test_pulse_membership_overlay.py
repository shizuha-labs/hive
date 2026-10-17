"""Serve-time Pulse placement overlay — last-good must not hide a roster write."""
from unittest import mock

from django.test import SimpleTestCase

from fleet.views import _overlay_live_pulse_memberships


class OverlayLivePulseMembershipsTests(SimpleTestCase):
    @mock.patch('fleet.views._service_pulse_roster_index')
    def test_fills_empty_memberships_from_live_roster(self, index):
        index.return_value = {
            'hina@shizuha.com': [{
                'email': 'hina@shizuha.com',
                'team_slug': 'ceo-decisions',
                'team_name': 'CEO Decisions',
                'organization_slug': 'shizuha',
            }],
        }
        cached = {'agent_username': 'hina', 'team_memberships': [], 'team_names': []}
        out = _overlay_live_pulse_memberships([cached])
        self.assertEqual(out[0]['team_names'], ['ceo-decisions'])
        self.assertEqual(out[0]['team_memberships'][0]['team_slug'], 'ceo-decisions')
        self.assertEqual(cached['team_names'], [])

    @mock.patch('fleet.views._service_pulse_roster_index')
    def test_rewrites_stale_human_ops_alias_to_ceo_decisions(self, index):
        index.return_value = {
            'hina@shizuha.com': [{
                'email': 'hina@shizuha.com',
                'team_slug': 'ceo-decisions',
                'team_name': 'CEO Decisions',
                'organization_slug': 'shizuha',
            }],
        }
        out = _overlay_live_pulse_memberships([{
            'agent_username': 'hina',
            'team_memberships': [{'team_slug': 'human-ops', 'team_name': 'Human Ops'}],
            'team_names': ['human-ops'],
        }])
        self.assertEqual(out[0]['team_names'], ['ceo-decisions'])

    @mock.patch('fleet.views._service_pulse_roster_index', return_value={})
    def test_leaves_payload_alone_when_live_index_is_empty(self, _index):
        cached = {'agent_username': 'hina', 'team_memberships': [], 'team_names': []}
        out = _overlay_live_pulse_memberships([cached])
        self.assertIs(out[0], cached)
