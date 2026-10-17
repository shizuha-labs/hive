"""HIVE-360: fail-loud liveness + invariant observability for the HIVE-355 Connect
personal-agent delivery path.

Proves each inbound records exactly ONE terminal outcome; a persistent async reply
failure increments the stage-labelled ``reply_failed`` counter (the fail-loud
signal the PrometheusRule alerts on) and logs level:error; the metrics endpoint
renders the counters; and a retry after failure does not double-reply.

HIVE-613: All requests must carry an HMAC-SHA256 signature. Tests use
``HIVE_CONNECT_HMAC_SECRET=test-secret``.
"""
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from claws.models import Claw
from claws import views
from claws import connect_delivery_metrics as metrics

TEST_HMAC_SECRET = 'test-secret'


def _hmac_headers_from_body(body: str) -> dict:
    """Build HMAC-SHA256 signature headers for a pre-serialized JSON body."""
    timestamp = str(int(time.time()))
    signature = hmac.new(
        TEST_HMAC_SECRET.encode('utf-8'),
        f'{timestamp}||{body}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return {
        'HTTP_X_HIVE_SIGNATURE_256': signature,
        'HTTP_X_HIVE_TIMESTAMP': timestamp,
    }


class _SyncThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


class _FailingThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError('thread unavailable')


@override_settings(
    HIVE_CONNECT_HMAC_SECRET=TEST_HMAC_SECRET,
    HIVE_INTERNAL_AUTH_TOKEN='metrics-test-secret',
)
class ConnectDeliveryMetricsTests(TestCase):
    URL = '/api/v1/internal/connect/messages/'
    METRICS_URL = '/api/v1/internal/connect/delivery/metrics/'

    def setUp(self):
        cache.clear()
        self.org_patcher = patch.object(
            views, '_owner_personal_organization_id', return_value=7,
        )
        self.org_patcher.start()
        self.addCleanup(self.org_patcher.stop)
        self.client = APIClient()
        self.claw = Claw.objects.create(
            owner_id=1, owner_username='o', owner_email='o@x.com', name='C',
            tier_id='free', agent_type=Claw.AgentType.CLAW, model='',
            status=Claw.Status.RUNNING, pod_name='p')
        self.stopped = Claw.objects.create(
            owner_id=1, owner_username='o', owner_email='o@x.com', name='S',
            tier_id='free', agent_type=Claw.AgentType.HERMES, model='',
            status=Claw.Status.STOPPED, pod_name='')
        self.sid = f'hive:{self.claw.agent_type}:{self.claw.id}'

    def _post(self, payload):
        """POST with HMAC-signed headers and the test secret.

        Serializes the payload to JSON first, then signs the serialized body
        so the HMAC covers the exact bytes the server receives.
        """
        import json as _json
        body = _json.dumps(payload, separators=(',', ':'), sort_keys=True)
        headers = _hmac_headers_from_body(body)
        return self.client.post(self.URL, body, content_type='application/json', **headers)

    def _payload(self, **o):
        p = {'stable_id': self.sid, 'conversation_id': 'c1', 'message_id': 'm1',
             'sender_id': 9, 'content': 'hi', 'client_message_id': 'cm1',
             'owner_id': '1',
             'organization_id': 7,
             'agent_participant_id': str(views._hive_agent_user_id(self.sid))}
        p.update(o)
        return p

    # ── terminal-outcome recording (invariant: exactly one per inbound) ──
    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views, '_claw_delivery_token', return_value='runtime-token')
    @patch.object(views.requests, 'post')
    @patch.object(views.threading, 'Thread', _SyncThread)
    def test_success_records_bridge_accepted_not_reply_posted(self, mock_post, *_):
        # HIVE-1276: bridge HTTP 202 is Hive handoff, not a user-visible reply.
        mock_post.return_value.status_code = 202
        self._post(self._payload())
        snap = metrics.snapshot()
        self.assertEqual(snap['accepted'], 1)
        self.assertEqual(snap['bridge_accepted'], 1)
        self.assertEqual(snap['reply_posted'], 0)
        self.assertEqual(snap['reply_failed'], 0)
        self.assertEqual(snap['pending']['count'], 0)

    # HIVE-1957: SCLI claws have no /v1/connect/messages HTTP bridge — delivery
    # routes via the runtime's WebSocket ConnectClient, so hive must NOT post to
    # the bridge (which would 404/refuse) and must still record the terminal
    # outcome so the conversation is not double-delivered.
    @patch.object(views.requests, 'post')
    @patch.object(views.threading, 'Thread', _SyncThread)
    def test_scli_delivery_routes_via_ws_skips_http_bridge(self, mock_post, *_):
        scli = Claw.objects.create(
            owner_id=1, owner_username='o', owner_email='o@x.com', name='S',
            tier_id='free', agent_type=Claw.AgentType.SCLI, model='',
            status=Claw.Status.RUNNING, pod_name='p')
        sid = f'hive:{scli.agent_type}:{scli.id}'
        self._post(self._payload(
            stable_id=sid,
            agent_participant_id=str(views._hive_agent_user_id(sid)),
        ))
        mock_post.assert_not_called()
        snap = metrics.snapshot()
        self.assertEqual(snap['accepted'], 1)
        self.assertEqual(snap['reply_posted'], 1)
        self.assertEqual(snap['reply_failed'], 0)
        self.assertEqual(snap['pending']['count'], 0)

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views, '_claw_delivery_token', return_value='runtime-token')
    @patch.object(views.threading, 'Thread', _SyncThread)
    def test_bridge_failure_records_reply_failed_stage_runtime_bridge(self, *_):
        with patch.object(views.requests, 'post') as mock_post:
            # Bridge reject + successful HIVE-1276 terminal-failure post.
            mock_post.side_effect = [
                MagicMock(status_code=403, text='Forbidden'),
                MagicMock(status_code=201, text='created'),
            ]
            with self.assertLogs('claws.views', level='ERROR') as log:
                self._post(self._payload())
        snap = metrics.snapshot()
        self.assertEqual(snap['reply_failed'], 1)
        self.assertEqual(snap['reply_failed_stages'].get('runtime_bridge'), 1)
        self.assertEqual(snap['pending']['count'], 0)
        self.assertTrue(any('connect reply FAILED stage=runtime_bridge' in m for m in log.output))
        # Terminal failure posted to Connect agent-message endpoint.
        self.assertEqual(mock_post.call_count, 2)
        self.assertIn('/agent-message/', mock_post.call_args_list[1].args[0])

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views, '_claw_delivery_token', return_value='runtime-token')
    @patch.object(views.requests, 'post')
    @patch.object(views.threading, 'Thread', _SyncThread)
    def test_runtime_rejection_records_stage_runtime_bridge(self, mock_post, *_):
        mock_post.return_value.status_code = 500
        mock_post.return_value.text = 'runtime failed'
        self._post(self._payload())
        snap = metrics.snapshot()
        self.assertEqual(snap['reply_failed_stages'].get('runtime_bridge'), 1)

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views, '_claw_delivery_token', return_value='runtime-token')
    @patch.object(views.requests, 'post')
    @patch.object(views.threading, 'Thread', _SyncThread)
    def test_runtime_failure_clears_tuple_dedupe_key_for_retry(self, mock_post, *_):
        # Both the bridge and terminal post fail on each attempt. The failed
        # terminal post clears the tuple key, so the second top-level request
        # retries both network legs instead of returning duplicate.
        mock_post.return_value.status_code = 500
        mock_post.return_value.text = 'runtime failed'
        first = self._post(self._payload())
        second = self._post(self._payload())
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(mock_post.call_count, 4)

    def test_unknown_agent_and_invalid_and_ignored_recorded(self):
        self._post(self._payload(
            stable_id='hive:openclaw:00000000-0000-0000-0000-000000000000'))
        self._post(self._payload(stable_id=''))       # invalid
        self._post(self._payload(content='  ', message_id='m2'))  # ignored
        snap = metrics.snapshot()
        self.assertEqual(snap['unknown_agent'], 1)
        self.assertEqual(snap['binding_required'], 1)
        self.assertEqual(snap['ignored'], 1)

    def test_non_running_agent_recorded(self):
        sid = f'hive:{self.stopped.agent_type}:{self.stopped.id}'
        self._post(self._payload(
            stable_id=sid,
            agent_participant_id=str(views._hive_agent_user_id(sid))))
        self.assertEqual(metrics.snapshot()['agent_not_running'], 1)

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views, '_claw_delivery_token', return_value='runtime-token')
    @patch.object(views.requests, 'post')
    @patch.object(views.threading, 'Thread', _SyncThread)
    def test_replay_records_duplicate_not_second_reply(self, mock_post, *_):
        mock_post.return_value.status_code = 202
        self._post(self._payload())
        self._post(self._payload())  # replay same message_id
        snap = metrics.snapshot()
        self.assertEqual(snap['duplicate'], 1)
        self.assertEqual(snap['bridge_accepted'], 1)  # exactly one handoff despite replay
        self.assertEqual(snap['reply_posted'], 0)
        self.assertEqual(mock_post.call_count, 1)

    @patch.object(views, '_claw_runtime_deliverable', return_value=(False, 'ready_replicas=0'))
    @patch.object(views.threading, 'Thread')
    def test_undeliverable_running_records_agent_not_running(self, mock_thread, *_):
        """HIVE-1276 D1: running-but-no-ready-replicas → agent_not_running, no worker."""
        r = self._post(self._payload())
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()['status'], 'agent_not_running')
        self.assertEqual(r.json().get('reason'), 'ready_replicas=0')
        mock_thread.assert_not_called()
        snap = metrics.snapshot()
        self.assertEqual(snap['agent_not_running'], 1)
        self.assertEqual(snap['accepted'], 0)
        self.assertEqual(snap['pending']['count'], 0)

    # ── liveness endpoint ──
    def test_metrics_endpoint_renders_prometheus(self):
        metrics.record('reply_posted')
        metrics.record('reply_failed', stage='cortex')
        metrics.mark_pending('stalled-message', started_at=time.time() - 600)
        resp = self.client.get(
            self.METRICS_URL,
            HTTP_AUTHORIZATION='Bearer metrics-test-secret',
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('hive_connect_delivery_total{outcome="reply_posted", claw_owner="", probe=""} 1', body)
        self.assertIn('hive_connect_delivery_reply_failed_total{stage="cortex"} 1', body)
        self.assertIn('hive_connect_delivery_pending 1', body)
        oldest = next(
            line for line in body.splitlines()
            if line.startswith('hive_connect_delivery_oldest_pending_age_seconds ')
        )
        self.assertGreaterEqual(float(oldest.split()[-1]), 599)

    def test_metrics_endpoint_fails_closed_without_internal_token(self):
        self.assertEqual(self.client.get(self.METRICS_URL).status_code, 403)
        self.assertEqual(
            self.client.get(
                self.METRICS_URL,
                HTTP_AUTHORIZATION='Bearer wrong-secret',
            ).status_code,
            403,
        )

    def test_pending_state_clears_only_after_terminal_outcome(self):
        started = time.time() - (8 * 24 * 60 * 60)
        metrics.mark_pending('m-pending', started_at=started)
        snapshot = metrics.pending_snapshot(now=time.time())
        self.assertEqual(snapshot['count'], 1)
        self.assertGreaterEqual(snapshot['oldest_age_seconds'], 8 * 24 * 60 * 60)
        metrics.finish_pending('m-pending')
        self.assertEqual(metrics.pending_snapshot()['count'], 0)

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(metrics, 'mark_pending', return_value=False)
    def test_delivery_fails_closed_when_pending_state_cannot_be_stored(self, *_):
        resp = self._post(self._payload())
        self.assertEqual(resp.status_code, 503)
        snap = metrics.snapshot()
        self.assertEqual(snap['accepted'], 0)
        self.assertEqual(snap['reply_failed_stages'].get('pending_store'), 1)

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views.threading, 'Thread', _FailingThread)
    def test_thread_start_failure_clears_pending_and_allows_retry(self, *_):
        resp = self._post(self._payload())
        self.assertEqual(resp.status_code, 503)
        snap = metrics.snapshot()
        self.assertEqual(snap['accepted'], 0)
        self.assertEqual(snap['pending']['count'], 0)
        self.assertEqual(snap['reply_failed_stages'].get('thread_start'), 1)
        # Dedupe was rolled back because the message was never accepted.
        retry = self._post(self._payload())
        self.assertEqual(retry.status_code, 503)

    def test_record_is_exhaustive_and_isolated(self):
        for o in metrics.OUTCOMES:
            metrics.record(o)
        snap = metrics.snapshot()
        for o in metrics.OUTCOMES:
            self.assertEqual(snap[o], 1)

    def test_mismatch_records_per_owner_breakdown(self):
        """HIVE-1766: tenant-boundary mismatch outcomes carry a per-owner label
        so the alert rule can allowlist expected QA-fixture negative-test
        denials (claw_owner=252) without silencing real cross-tenant attempts.
        """
        cache.clear()
        metrics.record('owner_mismatch', claw_owner='252')
        metrics.record('owner_mismatch', claw_owner='252')
        metrics.record('owner_mismatch', claw_owner='777')
        metrics.record('organization_mismatch', claw_owner='252')
        metrics.record('participant_mismatch', claw_owner='999')
        snap = metrics.snapshot()
        # Aggregate totals unchanged (backward compat).
        self.assertEqual(snap['owner_mismatch'], 3)
        self.assertEqual(snap['organization_mismatch'], 1)
        self.assertEqual(snap['participant_mismatch'], 1)
        # Per-owner breakdown present.
        self.assertEqual(snap['owners']['owner_mismatch'], {'252': 2, '777': 1})
        self.assertEqual(snap['owners']['organization_mismatch'], {'252': 1})
        self.assertEqual(snap['owners']['participant_mismatch'], {'999': 1})
        # Prometheus output carries the claw_owner label for mismatch outcomes.
        out = metrics.to_prometheus()
        self.assertIn('outcome="owner_mismatch", claw_owner="252", probe=""} 2', out)
        self.assertIn('outcome="owner_mismatch", claw_owner="777", probe=""} 1', out)
        self.assertIn('outcome="participant_mismatch", claw_owner="999", probe=""} 1', out)
        # Non-mismatch outcomes carry an empty claw_owner label.
        metrics.record('accepted')
        out = metrics.to_prometheus()
        self.assertIn('outcome="accepted", claw_owner="", probe=""} 1', out)

    def test_probe_attribution_excludes_harness_probes_from_alerting_series(self):
        """PLAT-7972: HIVE-725 harness negative probes land in dedicated probe
        series and never count as real delivery denials."""
        from claws import connect_delivery_metrics as metrics
        # Simulate the harness: unsigned release probe (401) + tenant-mismatched
        # signed release probe (403) + cutover probe.
        metrics.record('auth_failed', probe='release')
        metrics.record('auth_failed', probe='release')
        metrics.record('owner_mismatch', probe='release')
        metrics.record('auth_failed', probe='cutover')
        snap = metrics.snapshot()
        # Base (alerting) outcomes unchanged — probes do not count as denials.
        self.assertEqual(snap['auth_failed'], 0)
        self.assertEqual(snap['owner_mismatch'], 0)
        # Probe attribution recorded per marker.
        self.assertEqual(snap['probes']['auth_failed'], {'release': 2, 'cutover': 1})
        self.assertEqual(snap['probes']['owner_mismatch'], {'release': 1})
        out = metrics.to_prometheus()
        # Probe series visible in metrics...
        self.assertIn('outcome="auth_failed", claw_owner="", probe="release"} 2', out)
        self.assertIn('outcome="auth_failed", claw_owner="", probe="cutover"} 1', out)
        self.assertIn('outcome="owner_mismatch", claw_owner="", probe="release"} 1', out)
        # ...but the base (probe="") series the security alerts scrape stays
        # at zero — no page.
        self.assertIn('outcome="auth_failed", claw_owner="", probe=""} 0', out)
        # Unknown marker strings are NOT attributed (fail-closed: they count
        # as real traffic).
        metrics.record('auth_failed', probe='bogus')
        self.assertEqual(metrics.snapshot()['auth_failed'], 1)

    def test_real_unsigned_delivery_still_alerts_alongside_probes(self):
        """PLAT-7972 guard: a REAL unsigned (non-probe) request still lands in
        the probe="" alerting series even when probe traffic is present."""
        from claws import connect_delivery_metrics as metrics
        metrics.record('auth_failed', probe='release')  # harness probe
        metrics.record('auth_failed')                   # real unsigned traffic
        snap = metrics.snapshot()
        self.assertEqual(snap['auth_failed'], 1)
        out = metrics.to_prometheus()
        self.assertIn('outcome="auth_failed", claw_owner="", probe=""} 1', out)
        self.assertIn('outcome="auth_failed", claw_owner="", probe="release"} 1', out)
