from django.test import SimpleTestCase

from .views import _parse_efficiency_metrics


class EfficiencyMetricsParserTests(SimpleTestCase):
    def test_accepts_current_agent_label_and_legacy_agent_email_label(self):
        metrics = """
# HELP agent_tasks_completed_24h Completed tasks.
agent_tasks_completed_24h{agent="Kai@Shizuha.com",team="engineering"} 2
agent_avg_lead_time_seconds{agent="Kai@Shizuha.com",team="engineering"} 123.5
agent_tasks_completed_24h{agent_email="jun@shizuha.com"} 4
"""

        self.assertEqual(
            _parse_efficiency_metrics(metrics),
            {
                "kai@shizuha.com": {
                    "tasks_completed_24h": 2.0,
                    "avg_lead_time_seconds": 123.5,
                },
                "jun@shizuha.com": {
                    "tasks_completed_24h": 4.0,
                },
            },
        )

    def test_prefers_canonical_legacy_label_when_both_are_present(self):
        metrics = (
            'agent_wip_depth{agent_email="aoi@shizuha.com",'
            'agent="wrong@shizuha.com"} 3\n'
        )

        self.assertEqual(
            _parse_efficiency_metrics(metrics),
            {"aoi@shizuha.com": {"wip_depth": 3.0}},
        )

    def test_parses_full_exporter_metric_set_without_inventing_missing_agents(self):
        metrics = """
agent_tasks_completed_24h{agent="sara@shizuha.com"} 7
agent_avg_lead_time_seconds{agent="sara@shizuha.com"} 10
agent_wip_depth{agent="sara@shizuha.com"} 2
agent_backlog_depth{agent="sara@shizuha.com"} 5
agent_blocked_depth{agent="sara@shizuha.com"} 244
agent_overdue_tasks{agent="sara@shizuha.com"} 1
agent_oldest_open_item_age_seconds{agent="sara@shizuha.com"} 3600
agent_tasks_completed_24h{team="engineering"} 99
unrelated_metric{agent="sara@shizuha.com"} 100
"""

        self.assertEqual(
            _parse_efficiency_metrics(metrics),
            {
                "sara@shizuha.com": {
                    "tasks_completed_24h": 7.0,
                    "avg_lead_time_seconds": 10.0,
                    "wip_depth": 2.0,
                    "backlog_depth": 5.0,
                    "blocked_depth": 244.0,
                    "overdue_tasks": 1.0,
                    "oldest_open_item_age_seconds": 3600.0,
                },
            },
        )
