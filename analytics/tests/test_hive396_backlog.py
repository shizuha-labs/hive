from django.test import TestCase
from django.utils import timezone

from analytics.backlog import PulseTaskResult, materialize_discovery_backlog, payload_for_theme, priority_for_score, rank_score, urgent_alert_for_theme
from analytics.models import Theme


class DiscoveryBacklogMaterializerTests(TestCase):
    def _theme(self, **overrides):
        defaults = {
            'scope': 'platform',
            'org_id': None,
            'surface': 'hive',
            'label': 'login/authentication friction',
            'summary': '3 feedback signal(s) clustered as login/authentication friction',
            'member_count': 3,
            'sentiment_trend': 'negative',
            'velocity': 3.0,
            'severity': 'medium',
            'first_seen': timezone.now(),
            'last_seen': timezone.now(),
            'status': 'new',
            'anonymized_exemplars': ['Anonymized exemplar: login/authentication friction on hive'],
        }
        defaults.update(overrides)
        return Theme.objects.create(**defaults)

    def test_payload_cites_concrete_theme_evidence_and_valid_routing(self):
        theme = self._theme(id=101)

        payload = payload_for_theme(theme, project_id=42, parent_item_id='HIVE-396')

        self.assertEqual(payload['workflow'], 'feature-request-rfc')
        self.assertEqual(payload['project_id'], 42)
        self.assertEqual(payload['parent_item_id'], 'HIVE-396')
        self.assertEqual(payload['assignment_group'], 'product')
        self.assertIn('hive-396', payload['labels'])
        self.assertIn('Theme `101`', payload['description'])
        self.assertIn('Member count:** 3', payload['description'])
        self.assertIn('Anonymized exemplar: login/authentication friction on hive', payload['description'])
        self.assertIn('Backlink contract', payload['description'])
        self.assertNotIn('raw phrase', payload['description'])

    def test_unknown_surface_routes_to_product_with_owner_label_not_fake_group(self):
        theme = self._theme(surface='', label='general product feedback', scope='org', org_id=77)

        payload = payload_for_theme(theme)

        self.assertEqual(payload['assignment_group'], 'product')
        self.assertIn('needs-surface-owner', payload['labels'])
        self.assertIn('[Discovery][unknown] general product feedback', payload['title'])

    def test_materializer_creates_ranked_active_items_and_backlinks_themes(self):
        low = self._theme(surface='home', label='navigation or onboarding friction', member_count=1, severity='normal', velocity=1.0)
        high = self._theme(surface='hive', label='login/authentication friction', member_count=8, severity='high', velocity=8.0)
        normal = self._theme(surface='home', label='billing or pricing confusion', member_count=3, severity='medium', velocity=3.0)
        seen = []

        def fake_create(payload):
            seen.append(payload)
            return PulseTaskResult(item_key=f'HIVE-DISC-{len(seen)}')

        result = materialize_discovery_backlog(create_task=fake_create, urgent_notifier=None, project_id=42, parent_item_id='HIVE-396')

        self.assertEqual(result['created'], 3)
        self.assertEqual([payload['title'] for payload in seen], [
            '[Discovery][hive] login/authentication friction',
            '[Discovery][home] billing or pricing confusion',
            '[Discovery][home] navigation or onboarding friction',
        ])
        self.assertEqual(Theme.objects.get(id=high.id).backlog_ref, 'HIVE-DISC-1')
        self.assertEqual(Theme.objects.get(id=normal.id).backlog_ref, 'HIVE-DISC-2')
        self.assertEqual(Theme.objects.get(id=low.id).backlog_ref, 'HIVE-DISC-3')
        self.assertTrue(all(payload['workflow'] == 'feature-request-rfc' for payload in seen))

    def test_materializer_skips_deferred_resolved_and_already_backlinked_themes(self):
        self._theme(label='login/authentication friction')
        self._theme(label='billing or pricing confusion', backlog_ref='HIVE-999')
        self._theme(label='navigation or onboarding friction', status='resolved')
        seen = []

        result = materialize_discovery_backlog(create_task=lambda payload: seen.append(payload) or PulseTaskResult('HIVE-DISC-1'), urgent_notifier=None)

        self.assertEqual(result['created'], 1)
        self.assertEqual(len(seen), 1)


    def test_limit_is_applied_after_rank_sorting(self):
        urgent_low_count = self._theme(
            surface='hive', label='login/authentication friction', member_count=1,
            severity='high', velocity=10.0, sentiment_trend='negative',
        )
        self._theme(
            surface='home', label='navigation or onboarding friction', member_count=20,
            severity='normal', velocity=1.0, sentiment_trend='neutral',
        )
        seen = []

        result = materialize_discovery_backlog(
            create_task=lambda payload: seen.append(payload) or PulseTaskResult('HIVE-URGENT-1'),
            urgent_notifier=lambda _alert: None,
            limit=1,
        )

        self.assertEqual(result['created'], 1)
        self.assertEqual(result['items'][0]['theme_id'], urgent_low_count.id)
        self.assertEqual(seen[0]['priority'], 'urgent')
        self.assertEqual(Theme.objects.get(id=urgent_low_count.id).backlog_ref, 'HIVE-URGENT-1')

    def test_urgent_materialization_notifies_surface_pm(self):
        urgent = self._theme(
            surface='hive', label='login/authentication friction', member_count=20,
            severity='high', velocity=10.0, sentiment_trend='negative',
        )
        alerts = []

        result = materialize_discovery_backlog(
            create_task=lambda _payload: PulseTaskResult('HIVE-URGENT-2'),
            urgent_notifier=alerts.append,
        )

        self.assertEqual(result['created'], 1)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].theme_id, urgent.id)
        self.assertEqual(alerts[0].item_key, 'HIVE-URGENT-2')
        self.assertGreaterEqual(alerts[0].rank_score, 90)
        self.assertEqual(alerts[0].recipient_username, 'nao')
        self.assertIn('not raw feedback text', alerts[0].message)


    def test_urgent_notifier_failure_preserves_backlink_and_prevents_duplicate_retry(self):
        urgent = self._theme(
            surface='hive', label='login/authentication friction', member_count=20,
            severity='high', velocity=10.0, sentiment_trend='negative',
        )
        created = []

        def fake_create(_payload):
            created.append('created')
            return PulseTaskResult('HIVE-URGENT-3')

        def failing_notifier(_alert):
            raise RuntimeError('connect down')

        with self.assertRaisesRegex(RuntimeError, 'connect down'):
            materialize_discovery_backlog(create_task=fake_create, urgent_notifier=failing_notifier)

        self.assertEqual(Theme.objects.get(id=urgent.id).backlog_ref, 'HIVE-URGENT-3')
        retry = materialize_discovery_backlog(create_task=fake_create, urgent_notifier=None)
        self.assertEqual(retry['created'], 0)
        self.assertEqual(len(created), 1)

    def test_urgent_alert_payload_contains_only_theme_metadata(self):
        theme = self._theme(
            member_count=20, severity='high', velocity=10,
            summary='20 feedback signals clustered as login/authentication friction',
            anonymized_exemplars=['Anonymized exemplar only'],
        )

        alert = urgent_alert_for_theme(theme, 'HIVE-500', rank_score(theme))

        self.assertIn('HIVE-500', alert.message)
        self.assertIn(f'Theme: {theme.id}', alert.message)
        self.assertNotIn('sk-', alert.message)
        self.assertNotIn('raw phrase', alert.message)

    def test_rank_score_priority_bands_match_hive396_contract(self):
        self.assertEqual(priority_for_score(90), 'urgent')
        self.assertEqual(priority_for_score(70), 'high')
        self.assertEqual(priority_for_score(40), 'normal')
        self.assertEqual(priority_for_score(39), 'low')
        theme = self._theme(member_count=20, severity='high', velocity=10, sentiment_trend='negative')
        self.assertGreaterEqual(rank_score(theme), 90)
