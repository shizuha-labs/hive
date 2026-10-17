"""Native live-activity snapshots survive hibernate/restart on the Hive GET."""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from fleet.activity_snapshot import (
    capture_native_activity_snapshot,
    ingest_native_activity_raw,
    merge_activity_with_snapshot,
    persist_activity_snapshot,
)
from fleet.models import FleetAgent, FleetAgentActivitySnapshot
from fleet.views import FleetAgentActivityView


LIVE_EVENT = {
    'id': 'call-1',
    'ts': '2026-09-15T15:35:00Z',
    'type': 'tool_call',
    'tool': 'mcp__pulse__get',
    'input': 'PLAT-8846',
}
OLDER_EVENT = {
    'id': 'call-0',
    'ts': '2026-09-15T14:00:00Z',
    'type': 'turn_summary',
    'text': 'LLM turn completed: GLM-5.3-Flash',
}


class ActivitySnapshotMergeTests(TestCase):
    def setUp(self):
        self.agent = FleetAgent.objects.create(
            agent_username='sara-activity',
            status=FleetAgent.Status.RUNNING,
        )

    def test_live_read_persists_and_keeps_older_history(self):
        persist_activity_snapshot(self.agent, {
            'events': [OLDER_EVENT],
            'source_state': 'live',
            'source': 'k8s-native-audit+telemetry',
            'pod_name': 'agent-sara-old',
        })
        live = {
            'source_state': 'live',
            'source': 'k8s-native-audit+telemetry',
            'events': [LIVE_EVENT],
            'history_events': [],
            'total': 1,
            'pod_name': 'agent-sara-new',
        }
        merged = merge_activity_with_snapshot(self.agent, live, limit=50)
        self.assertEqual(merged['source_state'], 'live')
        self.assertEqual(merged['events'], [LIVE_EVENT])
        self.assertEqual(merged['history_events'], [OLDER_EVENT])
        snap = FleetAgentActivitySnapshot.objects.get(agent=self.agent)
        self.assertEqual(len(snap.events), 2)

    def test_unavailable_pod_serves_persisted_history_not_blank(self):
        persist_activity_snapshot(self.agent, {
            'events': [LIVE_EVENT, OLDER_EVENT],
            'source_state': 'live',
            'pod_name': 'agent-sara-vkdgr',
            'resource_uid': 'pod-uid-1',
        })
        empty = {
            'source_state': 'unavailable',
            'source': 'k8s-native-audit',
            'events': [],
            'history_events': [],
            'total': 0,
        }
        merged = merge_activity_with_snapshot(self.agent, empty, limit=50)
        self.assertEqual(merged['source_state'], 'persisted')
        self.assertEqual(merged['live_source_state'], 'unavailable')
        self.assertEqual(merged['events'][0]['id'], 'call-1')
        self.assertEqual(merged['total'], 2)
        self.assertEqual(merged['pod_name'], 'agent-sara-vkdgr')
        self.assertTrue(merged.get('persisted_at'))

    def test_empty_degraded_tail_does_not_wipe_snapshot(self):
        persist_activity_snapshot(self.agent, {
            'events': [LIVE_EVENT],
            'source_state': 'live',
        })
        degraded = {
            'source_state': 'degraded',
            'events': [],
            'history_events': [],
            'total': 0,
        }
        merged = merge_activity_with_snapshot(self.agent, degraded, limit=50)
        self.assertEqual(merged['source_state'], 'persisted')
        self.assertEqual(merged['events'], [LIVE_EVENT])
        self.assertEqual(
            FleetAgentActivitySnapshot.objects.get(agent=self.agent).events,
            [LIVE_EVENT],
        )

    def test_live_empty_reset_keeps_history_without_relabelling_persisted(self):
        persist_activity_snapshot(self.agent, {
            'events': [LIVE_EVENT],
            'source_state': 'live',
        })
        empty_live = {
            'source_state': 'live',
            'events': [],
            'history_events': [],
            'total': 0,
            'pod_name': 'agent-sara-reset',
        }
        merged = merge_activity_with_snapshot(self.agent, empty_live, limit=50)
        self.assertEqual(merged['source_state'], 'live')
        self.assertEqual(merged['events'], [])
        self.assertEqual(merged['history_events'], [LIVE_EVENT])
        self.assertTrue(merged.get('persisted_at'))


class ActivitySnapshotViewTests(TestCase):
    def setUp(self):
        self.agent = FleetAgent.objects.create(
            agent_username='kai-activity',
            status=FleetAgent.Status.HIBERNATED,
        )
        persist_activity_snapshot(self.agent, {
            'events': [LIVE_EVENT],
            'source_state': 'live',
            'pod_name': 'agent-kai-old',
        })

    def _request(self):
        request = MagicMock()
        request.user = MagicMock(is_authenticated=True, is_staff=True)
        request.query_params = {'limit': '50'}
        return request

    def test_native_get_after_hibernate_returns_persisted_events(self):
        unavailable = {
            'source_state': 'unavailable',
            'source': 'k8s-native-audit',
            'events': [],
            'history_events': [],
            'total': 0,
        }
        with patch(
            'fleet.views._get_agent', return_value=self.agent,
        ), patch(
            'fleet.views._get_native_agent_activity', return_value=unavailable,
        ):
            response = FleetAgentActivityView().get(self._request(), str(self.agent.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['source_state'], 'persisted')
        self.assertEqual(response.data['events'][0]['tool'], 'mcp__pulse__get')
        self.assertNotEqual(response.data['events'], [])


class PersistSanitizesSurrogatesTests(TestCase):
    def test_unpaired_surrogate_in_tool_text_still_persists(self):
        agent = FleetAgent.objects.create(
            agent_username='sara-emoji',
            status=FleetAgent.Status.RUNNING,
        )
        persist_activity_snapshot(agent, {
            'events': [{
                'id': 'call-emoji',
                'ts': '2026-09-15T07:16:13Z',
                'type': 'tool_call',
                'tool': 'bash',
                'input': 'grep -E "\u274c Failure|\ud83c"',
            }],
            'source_state': 'live',
        })
        snap = FleetAgentActivitySnapshot.objects.get(agent=agent)
        self.assertEqual(snap.events[0]['tool'], 'bash')
        self.assertNotIn('\ud83c', snap.events[0]['input'])


class IngestNativeActivityRawTests(TestCase):
    def test_pvc_tail_becomes_a_snapshot(self):
        agent = FleetAgent.objects.create(
            agent_username='rui-activity',
            status=FleetAgent.Status.HIBERNATED,
        )
        raw = (
            '{"id":"call-1","timestamp":"2026-09-15T15:35:00Z","agent":"rui",'
            '"tool":"mcp__pulse__get","inputSummary":"AT-91","phase":"before"}\n'
            '__TELEMETRY__\n'
        )
        n = ingest_native_activity_raw(agent, raw, pod_name='agent-rui-old')
        self.assertEqual(n, 1)
        snap = FleetAgentActivitySnapshot.objects.get(agent=agent)
        self.assertEqual(snap.events[0]['tool'], 'mcp__pulse__get')
        merged = merge_activity_with_snapshot(agent, {
            'source_state': 'unavailable',
            'events': [],
            'history_events': [],
            'total': 0,
        }, limit=50)
        self.assertEqual(merged['source_state'], 'persisted')
        self.assertEqual(merged['events'][0]['input'], 'AT-91')


class CaptureNativeActivitySnapshotTests(TestCase):
    def test_capture_persists_live_tail_before_scale_down(self):
        agent = FleetAgent.objects.create(
            agent_username='nagi-activity',
            status=FleetAgent.Status.RUNNING,
        )
        live = {
            'source_state': 'live',
            'events': [LIVE_EVENT],
            'pod_name': 'agent-nagi-abc',
        }
        with patch('fleet.views._get_native_agent_activity', return_value=live):
            capture_native_activity_snapshot(agent)
        snap = FleetAgentActivitySnapshot.objects.get(agent=agent)
        self.assertEqual(snap.events, [LIVE_EVENT])
        self.assertEqual(snap.pod_name, 'agent-nagi-abc')
        self.assertIsNotNone(snap.newest_event_at)
        self.assertEqual(
            (snap.newest_event_at.year, snap.newest_event_at.month, snap.newest_event_at.day),
            (2026, 9, 15),
        )


class ActivitySnapshotFailSoftTests(SimpleTestCase):
    def test_merge_without_db_leaves_live_receipt_intact(self):
        agent = MagicMock()
        agent.pk = 'not-a-real-pk'
        live = {
            'source_state': 'live',
            'events': [LIVE_EVENT],
            'history_events': [],
            'total': 1,
        }
        merged = merge_activity_with_snapshot(agent, live, limit=50)
        self.assertEqual(merged['source_state'], 'live')
        self.assertEqual(merged['events'], [LIVE_EVENT])


class ActivitySnapshotDatabaseTimezoneTests(TestCase):
    """The JSON event clock is UTC; naive ORM fields use the DB connection zone."""

    def test_real_postgres_roundtrip_preserves_snapshot_instants_in_both_dst_seasons(self):
        from datetime import datetime, timezone as utc_timezone
        from django.db import connection
        from django.test import override_settings
        from fleet.activity_snapshot import load_activity_snapshot

        self.assertEqual(connection.vendor, 'postgresql')
        with override_settings(USE_TZ=False, TIME_ZONE='America/Chicago'):
            with connection.cursor() as cursor:
                cursor.execute('SHOW TimeZone')
                self.assertEqual(cursor.fetchone()[0], 'America/Chicago')
            agent = FleetAgent.objects.create(agent_username='snapshot-db-clock')
            for month, expected_local_hour in ((7, 17), (1, 16)):
                with self.subTest(month=month):
                    event = {'id': f'clock-{month}', 'ts': f'2026-{month:02d}-15T22:00:00Z',
                             'type': 'tool_call', 'tool': 'synthetic-read'}
                    FleetAgentActivitySnapshot.objects.filter(agent=agent).delete()
                    persist_activity_snapshot(agent, {'events': [event], 'source_state': 'live'})
                    stored = FleetAgentActivitySnapshot.objects.get(agent=agent)
                    self.assertIsNone(stored.newest_event_at.tzinfo)
                    self.assertEqual(stored.newest_event_at.hour, expected_local_hour)
                    with connection.cursor() as cursor:
                        cursor.execute(
                            'SELECT extract(epoch from captured_at) FROM fleet_fleetagentactivitysnapshot WHERE agent_id=%s',
                            [str(agent.pk)],
                        )
                        captured_epoch = float(cursor.fetchone()[0])
                    loaded = load_activity_snapshot(agent)
                    self.assertEqual(loaded['events'], [event])
                    self.assertEqual(loaded['newest_event_at'], event['ts'])
                    captured = datetime.fromisoformat(loaded['captured_at'].replace('Z', '+00:00'))
                    self.assertAlmostEqual(captured.timestamp(), captured_epoch, places=5)
                    merged = merge_activity_with_snapshot(agent, {
                        'source_state': 'unavailable', 'events': [], 'history_events': [], 'total': 0,
                    })
                    self.assertEqual(merged['source_state'], 'persisted')
                    self.assertEqual(merged['newest_event_at'], event['ts'])
                    self.assertEqual(merged['events'][0]['id'], event['id'])

    def test_aware_database_values_and_live_event_clock_remain_utc(self):
        from datetime import datetime, timezone as utc_timezone
        from django.test import override_settings
        from fleet.activity_snapshot import _iso, _parse_ts, _newest_ts

        with override_settings(USE_TZ=False, TIME_ZONE='America/Chicago'):
            value = datetime(2026, 7, 15, 22, tzinfo=utc_timezone.utc)
            self.assertEqual(_iso(value), '2026-07-15T22:00:00Z')
            self.assertIsNone(_iso(None))
            self.assertEqual(_parse_ts('2026-07-15T22:00:00Z'), value)
            self.assertEqual(_parse_ts('2026-07-15T22:00:00'), value)
            self.assertEqual(_newest_ts([
                {'ts': '2026-07-15T21:00:00Z'}, {'ts': '2026-07-15T22:00:00Z'},
            ]), value)

    def test_actual_postgres_both_dst_folds_keep_distinct_event_and_capture_instants(self):
        from datetime import datetime
        from django.db import connection
        from django.test import override_settings
        from fleet.activity_snapshot import load_activity_snapshot

        self.assertEqual(connection.vendor, 'postgresql')
        with override_settings(USE_TZ=False, TIME_ZONE='America/Chicago'):
            with connection.cursor() as cursor:
                cursor.execute('SHOW DateStyle')
                self.assertTrue(cursor.fetchone()[0].startswith('ISO,'))
            agent = FleetAgent.objects.create(agent_username='snapshot-fold-clock')
            for hour in (6, 7):
                with self.subTest(utc_hour=hour):
                    text = f'2026-11-01T{hour:02d}:30:00Z'
                    event = {'id': f'fold-{hour}', 'ts': text, 'type': 'tool_call'}
                    instant = datetime.fromisoformat(text.replace('Z', '+00:00'))
                    FleetAgentActivitySnapshot.objects.filter(agent=agent).delete()
                    persist_activity_snapshot(agent, {'events': [event], 'source_state': 'live'})
                    FleetAgentActivitySnapshot.objects.filter(agent=agent).update(captured_at=instant)
                    raw = FleetAgentActivitySnapshot.objects.get(agent=agent)
                    self.assertIsNone(raw.newest_event_at.tzinfo)
                    self.assertEqual(raw.newest_event_at.hour, 1)
                    self.assertEqual(raw.newest_event_at.fold, 0)
                    loaded = load_activity_snapshot(agent)
                    self.assertEqual(loaded['newest_event_at'], text)
                    self.assertEqual(loaded['captured_at'], text)
                    self.assertEqual(loaded['events'], [event])
            FleetAgentActivitySnapshot.objects.filter(agent=agent).update(newest_event_at=None)
            self.assertIsNone(load_activity_snapshot(agent)['newest_event_at'])
