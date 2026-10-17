"""Operator 2026-07-16: Hive renders and writes one Research team."""
from django.test import SimpleTestCase

from claws.org_templates import builtin_template_definitions
from fleet.views import (
    _canon_capability_slug,
    _canon_team_name,
    _display_team_name,
    _normalise_team_collection_payload,
)


class ResearchTeamCanonicalizationTests(SimpleTestCase):
    def test_legacy_team_labels_share_one_slug(self):
        for label in ('research', 'analytics', 'Research Analytics', 'research-analytics'):
            self.assertEqual(_canon_team_name(label), 'research-analytics')

    def test_ceo_decisions_is_not_human_ops(self):
        self.assertEqual(_canon_team_name('ceo-decisions'), 'ceo-decisions')
        self.assertEqual(_canon_team_name('CEO Decisions'), 'ceo-decisions')
        self.assertEqual(_canon_team_name('human-ops'), 'human-ops')

    def test_consolidated_team_has_short_display_name(self):
        self.assertEqual(_display_team_name('research-analytics'), 'Research')

    def test_ceo_decisions_display_name(self):
        self.assertEqual(_display_team_name('ceo-decisions'), 'CEO Decisions')

    def test_capability_tags_are_not_rewritten_as_team_aliases(self):
        self.assertEqual(_canon_capability_slug('research'), 'research')
        self.assertEqual(_canon_capability_slug('analytics'), 'analytics')

    def test_team_list_hides_inactive_alias_tombstones(self):
        payload = _normalise_team_collection_payload([
            {
                'team_name': 'analytics',
                'organization_slug': 'shizuha',
                'is_active': False,
                'tiers': [[]],
            },
            {
                'team_name': 'research',
                'organization_slug': 'shizuha',
                'is_active': False,
                'tiers': [['hana@shizuha.com']],
            },
            {
                'team_name': 'research-analytics',
                'organization_slug': 'shizuha',
                'is_active': True,
                'tiers': [['tomo@shizuha.com', 'scout@shizuha.com', 'hana@shizuha.com']],
            },
        ])

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]['team_name'], 'research-analytics')
        self.assertEqual(payload[0]['display_name'], 'Research')
        self.assertTrue(payload[0]['is_active'])
        self.assertEqual(
            payload[0]['tiers'],
            [['tomo@shizuha.com', 'scout@shizuha.com', 'hana@shizuha.com']],
        )

    def test_same_team_slug_in_different_orgs_is_not_coalesced(self):
        payload = _normalise_team_collection_payload([
            {'team_name': 'research', 'organization_slug': 'shizuha', 'is_active': True},
            {'team_name': 'research', 'organization_slug': 'acme', 'is_active': True},
        ])
        self.assertEqual(len(payload), 2)

    def test_content_template_provisions_only_the_consolidated_team(self):
        template = next(
            row for row in builtin_template_definitions()
            if row['slug'] == 'content-marketing-org'
        )
        research = [
            team for team in template['teams']
            if _canon_team_name(team['slug']) == 'research-analytics'
        ]
        self.assertEqual(
            [(team['slug'], team['agent_count']) for team in research],
            [('research-analytics', 2)],
        )
        routed = {
            task['assignment_group']
            for task in template['starter_tasks']
            if 'Research' in task['title'] or 'report' in task['title']
        }
        self.assertEqual(routed, {'research-analytics'})
