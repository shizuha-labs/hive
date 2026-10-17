from django.test import SimpleTestCase

from fleet.views import _fleet_team_coverage


class Hive467TeamCoverageTests(SimpleTestCase):
    def test_live_codex_k3s_agent_covers_team(self):
        teams = [{'team_name': 'engineering'}, {'team_name': 'review'}]
        agents = [
            {
                'agent_username': 'sara',
                'email': 'sara@shizuha.com',
                'status': 'running',
                'enabled': True,
                'runtime_environment': 'k8s',
                'execution_method': 'codex_app_server',
                'effective_model': 'gpt-5.5',
                'team_names': ['engineering'],
            },
        ]

        result = _fleet_team_coverage(teams, agents)

        self.assertFalse(result['ok'])
        self.assertEqual(result['covered_team_count'], 1)
        self.assertEqual(result['uncovered_team_slugs'], ['review'])
        engineering = next(row for row in result['teams'] if row['team_slug'] == 'engineering')
        self.assertTrue(engineering['covered'])
        self.assertEqual(engineering['live_codex_k3s_agents'][0]['agent_username'], 'sara')


    def test_non_running_k3s_codex_agents_do_not_satisfy_coverage(self):
        teams = [{'team_name': 'devops'}]

        for not_live_status in ('pending', 'provisioning', 'starting'):
            with self.subTest(status=not_live_status):
                agents = [
                    {
                        'agent_username': 'ichi',
                        'email': 'ichi@shizuha.com',
                        'status': not_live_status,
                        'enabled': True,
                        'runtime_environment': 'k8s',
                        'execution_method': 'codex_app_server',
                        'effective_model': 'gpt-5.5',
                        'team_names': ['devops'],
                    },
                ]

                result = _fleet_team_coverage(teams, agents)

                self.assertFalse(result['ok'])
                self.assertEqual(result['covered_team_count'], 0)
                self.assertEqual(result['uncovered_team_slugs'], ['devops'])
                self.assertFalse(result['teams'][0]['covered'])

    def test_host_docker_or_non_codex_agents_do_not_satisfy_coverage(self):
        teams = [{'team_name': 'devops'}]
        agents = [
            {
                'agent_username': 'ichi',
                'email': 'ichi@shizuha.com',
                'status': 'running',
                'enabled': True,
                'runtime_environment': 'container',
                'runtime_source': 'rt-fleet',
                'execution_method': 'codex_app_server',
                'team_names': ['devops'],
            },
            {
                'agent_username': 'legacy',
                'email': 'legacy@shizuha.com',
                'status': 'running',
                'enabled': True,
                'runtime_environment': 'k8s',
                'execution_method': 'claude_code_server',
                'model': 'claude-sonnet-4-6',
                'team_names': ['devops'],
            },
        ]

        result = _fleet_team_coverage(teams, agents)

        self.assertFalse(result['ok'])
        self.assertEqual(result['uncovered_team_slugs'], ['devops'])
        self.assertFalse(result['teams'][0]['covered'])

    def test_membership_rows_are_used_but_inactive_teams_are_ignored(self):
        teams = [{'team_name': 'qa'}, {'team_name': 'retired', 'is_active': False}]
        agents = [
            {
                'agent_username': 'zen',
                'email': 'zen@shizuha.com',
                'status': 'running',
                'enabled': True,
                'runtime_source': 'k3s-native',
                'runtime_harness': 'Codex',
                'team_memberships': [{'team_slug': 'qa'}],
            },
        ]

        result = _fleet_team_coverage(teams, agents)

        self.assertTrue(result['ok'])
        self.assertEqual(result['active_team_count'], 1)
        self.assertEqual(result['teams'][0]['team_slug'], 'qa')

    def test_reports_configured_and_effective_coverage_for_target_providers(self):
        teams = [{'team_name': 'engineering'}]
        agents = [
            {
                'agent_username': 'haru',
                'email': 'haru@shizuha.com',
                'status': 'stopped',
                'enabled': False,
                'runtime_environment': 'k8s',
                'execution_method': 'claude_code_server',
                'effective_model': 'claude-fable-5',
                'team_names': ['engineering'],
            },
            {
                'agent_username': 'jun',
                'email': 'jun@shizuha.com',
                'status': 'running',
                'enabled': True,
                'runtime_environment': 'k8s',
                'execution_method': 'codex_app_server',
                'effective_model': 'gpt-5.6-sol',
                'team_names': ['engineering'],
            },
            {
                'agent_username': 'nagi',
                'email': 'nagi@shizuha.com',
                'status': 'running',
                'enabled': True,
                'runtime_environment': 'k8s',
                'execution_method': 'grok_build',
                'effective_model': 'cortex/grok-4.5',
                'provider_unavailable': True,
                'team_names': ['engineering'],
            },
        ]

        result = _fleet_team_coverage(teams, agents)

        engineering = result['teams'][0]
        coverage = engineering['provider_coverage']
        self.assertTrue(coverage['claude_code']['configured'])
        self.assertFalse(coverage['claude_code']['effective'])
        self.assertTrue(coverage['codex']['configured'])
        self.assertTrue(coverage['codex']['effective'])
        self.assertTrue(coverage['grok_build']['configured'])
        self.assertFalse(coverage['grok_build']['effective'])
        self.assertTrue(engineering['configured_all_target_providers'])
        self.assertFalse(engineering['effective_all_target_providers'])
        self.assertEqual(
            engineering['missing_effective_providers'],
            ['claude_code', 'grok_build'],
        )
        self.assertEqual(result['configured_all_provider_team_count'], 1)
        self.assertEqual(result['effective_all_provider_team_count'], 0)
        self.assertTrue(result['configured_coverage_ok'])
        self.assertFalse(result['effective_coverage_ok'])

        # HIVE-467 compatibility fields remain effective-Codex projections.
        self.assertTrue(engineering['covered'])
        self.assertEqual(
            engineering['live_codex_k3s_agents'][0]['agent_username'],
            'jun',
        )

    def test_effective_coverage_excludes_capacity_limited_health(self):
        teams = [{'team_name': 'qa'}]
        capacity_signals = [
            {'availability_reason': 'capacity_limited'},
            {'availability_status': 'quota_exhausted'},
            {'health': {'token_pool_unavailable': True}},
            {
                'health_summary': {
                    'issues': [{'type': 'provider_unavailable_or_quota'}],
                },
            },
        ]

        for signal in capacity_signals:
            with self.subTest(signal=signal):
                agent = {
                    'agent_username': 'mika',
                    'email': 'mika@shizuha.com',
                    'status': 'running',
                    'enabled': True,
                    'runtime_environment': 'k8s',
                    'execution_method': 'grok_build',
                    'effective_model': 'cortex/grok-4.5',
                    'team_names': ['qa'],
                    **signal,
                }

                result = _fleet_team_coverage(teams, [agent])

                grok = result['teams'][0]['provider_coverage']['grok_build']
                self.assertTrue(grok['configured'])
                self.assertFalse(grok['effective'])
                self.assertEqual(grok['configured_agents'][0]['agent_username'], 'mika')
                self.assertEqual(grok['effective_agents'], [])

    def test_provider_aggregate_reports_per_family_team_gaps(self):
        teams = [{'team_name': 'engineering'}, {'team_name': 'review'}]
        agents = [
            {
                'agent_username': 'sara',
                'status': 'running',
                'enabled': True,
                'runtime_source': 'k3s-native',
                'runtime_harness': 'Claude Code',
                'model': 'claude-opus-4-8',
                'team_names': ['engineering'],
            },
            {
                'agent_username': 'jun',
                'status': 'running',
                'enabled': True,
                'runtime_source': 'k3s-native',
                'runtime_harness': 'Codex',
                'model': 'gpt-5.6-sol',
                'team_names': ['engineering', 'review'],
            },
        ]

        result = _fleet_team_coverage(teams, agents)

        self.assertEqual(result['provider_coverage']['claude_code'], {
            'label': 'Claude Code',
            'configured_team_count': 1,
            'effective_team_count': 1,
            'configured_gap_team_slugs': ['review'],
            'effective_gap_team_slugs': ['review'],
        })
        self.assertEqual(result['provider_coverage']['codex']['effective_team_count'], 2)
        self.assertEqual(
            result['provider_coverage']['grok_build']['configured_gap_team_slugs'],
            ['engineering', 'review'],
        )
