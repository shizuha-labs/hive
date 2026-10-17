"""HIVE-395 clustering/theme invariants.

Slice 3 turns feedback_event rows into org-scoped themes and platform-wide
aggregate themes. The non-waivable §6 invariant: platform themes expose counts +
anonymized exemplars only — never raw feedback text from one tenant to another.
"""
from django.core.cache import cache
from django.test import TestCase

from analytics.clustering import (
    CLUSTERING_WATERMARK_KEY,
    cluster_unassigned_feedback,
    process_feedback_watermark,
)
from analytics.feedback import record_feedback_event
from analytics.models import FeedbackEvent, Theme

ORG_A, ORG_B = 9201, 9202


class FeedbackClusteringTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_feedback_ingest_marks_event_driven_watermark(self):
        record_feedback_event(
            org_id=ORG_A, source='feedback_widget', source_ref='a1',
            text='login is broken', surface='hive', user_id=1,
        )
        self.assertEqual(cache.get(CLUSTERING_WATERMARK_KEY), 1)

    def test_watermark_job_clusters_feedback_and_alerts_on_platform_spike(self):
        record_feedback_event(
            org_id=ORG_A, source='feedback_widget', source_ref='a1',
            text='login is broken for my org secret-alpha', surface='hive', user_id=1,
        )
        record_feedback_event(
            org_id=ORG_B, source='support_ticket', source_ref='b1',
            text='login failed for my org secret-beta', surface='hive', user_id=2,
        )
        alerts = []

        result = process_feedback_watermark(alert_callback=alerts.append)

        self.assertEqual(result['processed'], 2)
        self.assertEqual(result['org_themes'], 2)
        self.assertEqual(result['platform_themes'], 1)
        self.assertEqual(result['alerts'], 1)
        self.assertEqual(FeedbackEvent.objects.filter(theme_id__isnull=False).count(), 2)
        self.assertEqual(set(FeedbackEvent.objects.values_list('sentiment', flat=True)), {'negative'})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].label, 'login/authentication friction')
        self.assertEqual(alerts[0].surface, 'hive')

    def test_platform_theme_never_contains_raw_cross_tenant_feedback(self):
        raw_a = 'login broke and exposed tenant A raw phrase alpha-secret'
        raw_b = 'login failed and exposed tenant B raw phrase beta-secret'
        record_feedback_event(org_id=ORG_A, source='feedback_widget', source_ref='a2', text=raw_a, surface='hive')
        record_feedback_event(org_id=ORG_B, source='feedback_widget', source_ref='b2', text=raw_b, surface='hive')

        cluster_unassigned_feedback()

        platform_theme = Theme.objects.get(scope='platform')
        self.assertIsNone(platform_theme.org_id)
        exposed = f'{platform_theme.summary} {platform_theme.anonymized_exemplars}'
        self.assertIn('login/authentication friction', exposed)
        self.assertNotIn('alpha-secret', exposed)
        self.assertNotIn('beta-secret', exposed)
        self.assertNotIn(raw_a, exposed)
        self.assertNotIn(raw_b, exposed)

    def test_platform_theme_created_when_matching_org_feedback_arrives_in_separate_watermark_runs(self):
        record_feedback_event(
            org_id=ORG_A, source='feedback_widget', source_ref='seq-a',
            text='login is broken for alpha', surface='hive',
        )
        first_alerts = []

        first = process_feedback_watermark(alert_callback=first_alerts.append)

        self.assertEqual(first['platform_themes'], 0)
        self.assertEqual(first_alerts, [])

        record_feedback_event(
            org_id=ORG_B, source='feedback_widget', source_ref='seq-b',
            text='login failed for beta', surface='hive',
        )
        second_alerts = []

        second = process_feedback_watermark(alert_callback=second_alerts.append)

        self.assertEqual(second['processed'], 1)
        self.assertEqual(second['platform_themes'], 1)
        platform_theme = Theme.objects.get(scope='platform')
        self.assertEqual(platform_theme.member_count, 2)
        self.assertEqual(platform_theme.label, 'login/authentication friction')
        self.assertEqual({e.org_id for e in FeedbackEvent.objects.filter(theme_id__isnull=False)}, {ORG_A, ORG_B})
        self.assertEqual(len(second_alerts), 1)


    def test_watermark_job_drains_more_rows_than_one_clustering_page(self):
        for idx in range(5):
            record_feedback_event(
                org_id=ORG_A, source='feedback_widget', source_ref=f'page-{idx}',
                text='login is broken for alpha', surface=['home', 'hive', 'pulse', 'admin', 'home'][idx],
            )

        first = process_feedback_watermark(limit=2)
        second = process_feedback_watermark(limit=2)

        self.assertEqual(first['processed'], 5)
        self.assertEqual(FeedbackEvent.objects.filter(theme_id__isnull=True).count(), 0)
        self.assertEqual(second['processed'], 0)

    def test_single_org_feedback_only_creates_org_theme(self):
        record_feedback_event(org_id=ORG_A, source='feedback_widget', source_ref='a3', text='pricing is confusing', surface='home')

        result = cluster_unassigned_feedback()

        self.assertEqual(result['org_themes'], 1)
        self.assertEqual(result['platform_themes'], 0)
        self.assertTrue(Theme.objects.filter(scope='org', org_id=ORG_A).exists())
        self.assertFalse(Theme.objects.filter(scope='platform').exists())
