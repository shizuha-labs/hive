"""Regression tests for HIVE-160 context-budget estimates."""
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from fleet.context_budget import (
    attach_context_budget,
    budgets_to_prometheus,
    context_budget_for_agent,
    estimate_text_tokens,
)
from fleet.runtime_fleet import normalize_runtime_agent


if not settings.configured:
    settings.configure(
        DEFAULT_CHARSET='utf-8',
        HIVE_DEFAULT_CONTEXT_WINDOW_TOKENS=128000,
    )


class ContextBudgetTests(SimpleTestCase):
    def test_budget_has_required_components_and_no_raw_prompt(self):
        agent = {
            'id': 'a1',
            'agent_username': 'kai',
            'display_name': 'Kai',
            'role': 'Engineer',
            'model': 'gpt-4o',
            'execution_method': 'codex_app_server',
            'context_prompt': 'SECRET PROMPT CONTENT THAT MUST NOT BE RETURNED',
            'skill_config': {'skills': ['pulse-workflows', 'connect-messaging']},
            'mcp_config': {'tools': ['pulse_get_my_tasks']},
            'runtime_source': 'rt-fleet',
        }

        budget = context_budget_for_agent(agent)

        self.assertEqual(budget['agent'], 'kai')
        self.assertGreater(budget['total_tokens'], 0)
        components = {c['component']: c for c in budget['components']}
        for name in ('agents_md', 'agent_config', 'skills', 'mcp_tools', 'runtime_wrapper', 'total', 'headroom'):
            self.assertIn(name, components)
        self.assertNotIn('SECRET PROMPT CONTENT', str(budget))

    @override_settings(HIVE_CONTEXT_BUDGET_WARNING_UTILIZATION=0.01)
    def test_outlier_threshold_flags_high_utilization(self):
        budget = context_budget_for_agent({
            'agent_username': 'bloated',
            'model': 'tiny-model',
            'skill_config': {'skills': ['a', 'b', 'c']},
        })

        self.assertTrue(budget['outlier'])
        self.assertIn(budget['severity'], {'warning', 'critical'})

    def test_prometheus_exposition_contains_expected_labels(self):
        budget = context_budget_for_agent({'agent_username': 'kai', 'model': 'gpt-4o'})
        text = budgets_to_prometheus([budget])

        self.assertIn('# TYPE shizuha_agent_context_tokens gauge', text)
        self.assertIn('shizuha_agent_context_tokens{agent="kai",component="total"', text)
        self.assertIn('model="gpt-4o"', text)

    def test_runtime_normalizer_preserves_safe_context_metadata(self):
        normalized = normalize_runtime_agent({
            'id': 'agent-1',
            'username': 'jun',
            'enabled': True,
            'executionMethod': 'codex_app_server',
            'contextPrompt': 'private prompt',
            'personalityTraits': {'style': 'direct'},
            'modelFallbacks': [{'model': 'gpt-4o', 'method': 'codex_app_server'}],
            'skills': ['pulse-workflows'],
        })

        self.assertEqual(normalized['context_prompt'], 'private prompt')
        self.assertEqual(normalized['personality_traits'], {'style': 'direct'})
        self.assertEqual(normalized['skill_config']['skills'], ['pulse-workflows'])

    def test_chars_over_four_estimator_fallback_is_nonzero(self):
        estimate = estimate_text_tokens('abcd')
        self.assertGreaterEqual(estimate.tokens, 1)
        self.assertIn(estimate.status, {'exact', 'estimated'})

    def test_gpt6_astra_name_window_is_catalog_max_until_telemetry(self):
        budget = context_budget_for_agent({
            'agent_username': 'nami',
            'model': 'gpt-6-astra',
        })
        self.assertEqual(budget['context_window_tokens'], 872_000)
        measured = context_budget_for_agent({
            'agent_username': 'nami',
            'model': 'gpt-6-astra',
            'context_max_tokens': 272000,
            'context_used_tokens': 104537,
        })
        self.assertEqual(measured['context_window_tokens'], 272000)

    def test_glm_flash_window_is_500k_not_128k_default(self):
        budget = context_budget_for_agent({
            'agent_username': 'kei',
            'model': 'cortex/GLM-5.3-Flash',
        })
        self.assertEqual(budget['context_window_tokens'], 500_000)
        components = {c['component']: c for c in budget['components']}
        self.assertEqual(components['conversation_history']['status'], 'deferred')
        self.assertEqual(components['conversation_history']['tokens'], 0)

    def test_telemetry_used_tokens_fill_conversation_history(self):
        budget = context_budget_for_agent({
            'agent_username': 'kei',
            'model': 'cortex/GLM-5.3-Flash',
            'context_used_tokens': 350230,
            'context_max_tokens': 500000,
            'skill_config': {'skills': ['heartbeat-protocol', 'pulse-merge']},
        })
        components = {c['component']: c for c in budget['components']}
        self.assertEqual(components['conversation_history']['status'], 'measured')
        self.assertGreater(components['conversation_history']['tokens'], 300000)
        self.assertEqual(budget['total_tokens'], 350230)
        self.assertEqual(budget['context_window_tokens'], 500000)
        self.assertGreaterEqual(budget['utilization_percent'], 50)
        self.assertEqual(budget['severity'], 'warning')
        self.assertTrue(budget['outlier'])

    def test_list_sequence_recomputes_budget_after_telemetry_overlay(self):
        """Production order: attach_context_budget, then telemetry_fields update,
        then refresh context_budget. A pre-telemetry budget must not stick."""
        agent = attach_context_budget({
            'agent_username': 'kei',
            'model': 'cortex/GLM-5.3-Flash',
            'skill_config': {'skills': ['pulse-merge']},
        })
        history = {c['component']: c for c in agent['context_budget']['components']}
        self.assertEqual(history['conversation_history']['status'], 'deferred')
        self.assertLess(agent['context_budget']['total_tokens'], 20_000)

        agent['context_used_tokens'] = 350230
        agent['context_max_tokens'] = 500000
        agent['context_budget'] = context_budget_for_agent(agent)

        self.assertEqual(agent['context_budget']['total_tokens'], 350230)
        self.assertEqual(
            {c['component']: c['status'] for c in agent['context_budget']['components']}['conversation_history'],
            'measured',
        )

if __name__ == '__main__':
    import unittest
    unittest.main()
