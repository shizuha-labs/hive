"""HIVE-355: Connect DM → personal-agent delivery endpoint.

Connect hands a persisted user DM to Hive at
``POST /api/v1/internal/connect/messages/`` (HIVE_CONNECT_DELIVERY_URL) with
``{stable_id, conversation_id, message_id, sender_id, content, client_message_id}``
and expects a fast 2xx ack; Hive resolves the target Claw, acks, and posts the
assistant reply back to Connect asynchronously with idempotency. Before this
route existed the handoff 404'd and no assistant response was produced.

HIVE-613: All requests must carry an HMAC-SHA256 signature (``X-Hive-Signature-256``
+ ``X-Hive-Timestamp`` headers) using the shared secret. Tests use
``HIVE_CONNECT_HMAC_SECRET=test-secret``.
"""
import hashlib
import hmac
import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from claws.models import Claw
from claws import views

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
    """Runs the target inline so the async reply path is deterministic in tests."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@override_settings(HIVE_CONNECT_HMAC_SECRET=TEST_HMAC_SECRET)
class ConnectDeliveryTests(TestCase):
    URL = '/api/v1/internal/connect/messages/'

    def setUp(self):
        cache.clear()  # dedupe keys live in the process cache, not the DB rollback
        self.org_patcher = patch(
            'claws.views._owner_personal_organization_id', return_value=7,
        )
        self.org_patcher.start()
        self.addCleanup(self.org_patcher.stop)
        self.client = APIClient()
        self.claw = Claw.objects.create(
            owner_id=101, owner_username='owner', owner_email='owner@example.com',
            name='Fixture Claw', tier_id='free',
            agent_type=Claw.AgentType.CLAW, model='',
            status=Claw.Status.RUNNING, pod_name='claw-pod')
        self.stopped = Claw.objects.create(
            owner_id=101, owner_username='owner', owner_email='owner@example.com',
            name='Stopped', tier_id='free',
            agent_type=Claw.AgentType.HERMES, model='',
            status=Claw.Status.STOPPED, pod_name='')
        self.stable_id = f'hive:{self.claw.agent_type}:{self.claw.id}'

    def _post(self, payload, *, client=None):
        """POST with HMAC-signed headers and the test secret.

        Serializes the payload to JSON first, then signs the serialized body
        so the HMAC covers the exact bytes the server receives.
        """
        import json as _json
        body = _json.dumps(payload, separators=(',', ':'), sort_keys=True)
        headers = _hmac_headers_from_body(body)
        return (client or self.client).post(
            self.URL, body, content_type='application/json', **headers)

    def _payload(self, **over):
        p = {
            'stable_id': self.stable_id,
            'conversation_id': 'c-123',
            'message_id': 'm-1',
            'sender_id': 15,
            'content': 'hello agent',
            'client_message_id': 'cm-1',
            'owner_id': '101',
            'organization_id': 7,
            'agent_participant_id': str(views._hive_agent_user_id(self.stable_id)),
        }
        p.update(over)
        return p

    # ── HMAC authentication ──
    def test_missing_hmac_returns_401(self):
        r = self.client.post(self.URL, self._payload(), format='json')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['status'], 'auth_failed')

    def test_wrong_hmac_signature_returns_401(self):
        payload = self._payload()
        import json as _json
        body = _json.dumps(payload, separators=(',', ':'), sort_keys=True)
        headers = _hmac_headers_from_body(body)
        # Tamper the signature
        headers['HTTP_X_HIVE_SIGNATURE_256'] = 'deadbeef' + headers['HTTP_X_HIVE_SIGNATURE_256'][8:]
        r = self.client.post(self.URL, body, content_type='application/json', **headers)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['status'], 'auth_failed')

    def test_stale_hmac_timestamp_returns_401(self):
        payload = self._payload()
        import json as _json
        body = _json.dumps(payload, separators=(',', ':'), sort_keys=True)
        old_ts = str(int(time.time()) - 60)
        signature = hmac.new(
            TEST_HMAC_SECRET.encode('utf-8'),
            f'{old_ts}||{body}'.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        r = self.client.post(self.URL, body, content_type='application/json',
                             HTTP_X_HIVE_SIGNATURE_256=signature,
                             HTTP_X_HIVE_TIMESTAMP=old_ts)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['status'], 'auth_failed')

    # ── owner binding ──
    def test_owner_mismatch_returns_403(self):
        payload = self._payload(owner_id='999')
        with self.assertLogs('claws.views', level='INFO') as logs:
            r = self._post(payload)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['status'], 'owner_mismatch')
        self.assertTrue(any('outcome=owner_mismatch' in line for line in logs.output))

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views.threading, 'Thread')
    def test_correct_owner_passes(self, thread, _mock_deliv):
        payload = self._payload(owner_id='101')
        r = self._post(payload)
        # Should reach the running agent and accept
        self.assertEqual(r.status_code, 202)
        thread.assert_called_once()

    def test_missing_owner_id_returns_400(self):
        """Missing owner_id is rejected — owner binding is mandatory."""
        payload = self._payload()
        # Remove owner_id from the payload
        payload.pop('owner_id', None)
        r = self._post(payload)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['status'], 'owner_required')

    def test_missing_organization_scope_returns_400(self):
        payload = self._payload()
        payload.pop('organization_id')
        r = self._post(payload)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['status'], 'organization_scope_required')

    def test_invalid_organization_scope_returns_400(self):
        r = self._post(self._payload(organization_id='not-an-org'))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['status'], 'organization_scope_required')

    def test_forged_agent_type_is_non_enumerating_404(self):
        payload = self._payload(
            stable_id=f'hive:{Claw.AgentType.HERMES}:{self.claw.id}')
        payload['agent_participant_id'] = str(
            views._hive_agent_user_id(payload['stable_id']))
        r = self._post(payload)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()['status'], 'unknown_agent')

    def test_agent_participant_binding_is_required(self):
        with self.assertLogs('claws.views', level='INFO') as logs:
            r = self._post(self._payload(agent_participant_id='1500000000'))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['status'], 'participant_mismatch')
        self.assertTrue(any('outcome=participant_mismatch' in line for line in logs.output))

    # ── validation / resolution ──
    def test_missing_fields_returns_400(self):
        r = self._post(self._payload(stable_id=''))
        self.assertEqual(r.status_code, 400)

    def test_unknown_agent_returns_404(self):
        r = self._post(
            self._payload(stable_id='hive:openclaw:00000000-0000-0000-0000-000000000000'))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()['status'], 'unknown_agent')

    def test_malformed_stable_id_returns_404(self):
        r = self._post(self._payload(stable_id='not-a-hive-id'))
        self.assertEqual(r.status_code, 404)

    def test_empty_content_ignored_200(self):
        r = self._post(self._payload(content='   '))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'ignored')

    def test_non_running_agent_reports_status_not_silent(self):
        sid = f'hive:{self.stopped.agent_type}:{self.stopped.id}'
        r = self._post(self._payload(
            stable_id=sid,
            agent_participant_id=str(views._hive_agent_user_id(sid))))
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()['status'], 'agent_not_running')

    # ── happy path: accept + async reply posted back ──
    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views, '_claw_delivery_token', return_value='runtime-token')
    @patch.object(views.requests, 'post')
    @patch.object(views.threading, 'Thread', _SyncThread)
    def test_running_agent_accepts_and_forwards_to_runtime(self, mock_post, _mock_token, _mock_deliv):
        mock_post.return_value = MagicMock(status_code=202, text='accepted')
        r = self._post(self._payload())
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()['status'], 'accepted')
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs['json']['conversation_id'], 'c-123')
        self.assertEqual(kwargs['json']['message_id'], 'm-1')
        self.assertEqual(kwargs['headers']['X-Connect-Runtime-Secret'], 'runtime-token')

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views, '_claw_delivery_token', return_value='runtime-token')
    @patch.object(views.requests, 'post')
    @patch.object(views.threading, 'Thread', _SyncThread)
    def test_replay_same_message_id_is_deduped(self, mock_post, _mock_token, _mock_deliv):
        mock_post.return_value = MagicMock(status_code=202, text='accepted')
        first = self._post(self._payload())
        self.assertEqual(first.status_code, 202)
        with self.assertLogs('claws.views', level='INFO') as logs:
            second = self._post(self._payload())
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['status'], 'duplicate')
        # reply generated + posted exactly once despite the replay.
        self.assertEqual(mock_post.call_count, 1)
        self.assertTrue(any('outcome=duplicate' in line for line in logs.output))

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views.threading, 'Thread')
    def test_same_message_id_in_different_conversation_is_not_deduped(self, _thread, _mock_deliv):
        first = self._post(self._payload())
        second = self._post(self._payload(conversation_id='c-456'))
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)

    @patch.object(views.threading, 'Thread')
    def test_forged_organization_scope_is_rejected(self, _thread):
        response = self._post(self._payload(organization_id=999))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['status'], 'organization_mismatch')

    def test_oversize_content_is_rejected(self):
        response = self._post(self._payload(
            content='x' * (views._CONNECT_DELIVERY_MAX_CONTENT_CHARS + 1),
        ))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()['status'], 'content_too_large')

    def test_oversize_body_is_rejected_before_parsing(self):
        response = self._post(self._payload(
            padding='x' * views._CONNECT_DELIVERY_MAX_BODY_BYTES,
        ))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()['status'], 'body_too_large')

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views, '_CONNECT_DELIVERY_RATE_LIMIT', 1)
    @patch.object(views.threading, 'Thread')
    def test_tuple_rate_limit_rejects_second_distinct_message(self, _thread, *_):
        first = self._post(self._payload(message_id='rate-1'))
        second = self._post(self._payload(message_id='rate-2'))
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()['status'], 'rate_limited')

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views, '_CONNECT_DELIVERY_RATE_LIMIT', 3)
    @patch.object(views.threading, 'Thread')
    def test_accepted_retry_bypasses_exhausted_new_work_rate_limit(self, thread, *_):
        """Mirror Connect's accept -> distinct traffic -> retry caller order."""
        original = self._payload(message_id='retry-after-limit')
        self.assertEqual(self._post(original).status_code, 202)
        self.assertEqual(
            self._post(self._payload(message_id='rate-fill-1')).status_code,
            202,
        )
        self.assertEqual(
            self._post(self._payload(message_id='rate-fill-2')).status_code,
            202,
        )
        rejected = self._post(self._payload(message_id='rate-over-limit'))
        self.assertEqual(rejected.status_code, 429)

        retry = self._post(original)

        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()['status'], 'duplicate')
        # Three distinct tuples dispatch; the accepted tuple's retry does not.
        self.assertEqual(thread.call_count, 3)

    @patch.object(views, '_CONNECT_DELIVERY_RATE_LIMIT', 1)
    @override_settings(CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': os.environ.get(
                'CACHE_REDIS_URL', 'redis://127.0.0.1:6379/6'),
            'KEY_PREFIX': 'hive',
        },
    })
    def test_overlapping_signed_posts_use_redis_reservation_at_rate_boundary(self):
        """Run the production-order overlap through the signed HTTP view.

        Request A is paused after the production Redis Lua transition has
        atomically admitted and claimed its tuple, but before the view can
        dispatch.  The same signed POST then retries as request B while the
        owner+claw budget is exhausted.  B must observe A's reservation as a
        duplicate rather than create a rate-linked terminal outcome.
        """
        from claws import connect_delivery_metrics as metrics

        original_reserve = views._reserve_connect_delivery
        original_thread = threading.Thread
        admitted = threading.Event()
        release = threading.Event()
        responses = {}
        worker_errors = []
        dispatch_threads = []

        def reserve_then_pause(*args, **kwargs):
            outcome = original_reserve(*args, **kwargs)
            if outcome == 'reserved' and not admitted.is_set():
                admitted.set()
                if not release.wait(timeout=5):
                    raise RuntimeError('timed out waiting to overlap signed retry')
            return outcome

        def capture_dispatch_thread(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            dispatch_threads.append(thread)
            return thread

        def request_a():
            try:
                responses['a'] = self._post(
                    self._payload(message_id='concurrent-boundary'),
                    client=APIClient(),
                )
            except Exception as exc:  # propagate request-thread failures
                worker_errors.append(exc)

        runtime_response = MagicMock(status_code=202, text='accepted')
        worker = original_thread(target=request_a)
        try:
            cache.clear()
            with patch.object(
                    views, '_reserve_connect_delivery',
                    side_effect=reserve_then_pause), \
                 patch.object(
                    views.threading, 'Thread',
                    side_effect=capture_dispatch_thread), \
                 patch.object(
                    views, '_claw_from_stable_id', return_value=self.claw), \
                 patch.object(
                    views, '_claw_runtime_deliverable',
                    return_value=(True, 'ready')), \
                 patch.object(
                    views, '_claw_delivery_token', return_value='runtime-token'), \
                 patch.object(
                    views.requests, 'post', return_value=runtime_response) as runtime_post:
                worker.start()
                self.assertTrue(admitted.wait(timeout=5))
                responses['b'] = self._post(
                    self._payload(message_id='concurrent-boundary'),
                    client=APIClient(),
                )
                release.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker_errors, [])

                for dispatch_thread in dispatch_threads:
                    dispatch_thread.join(timeout=5)
                    self.assertFalse(dispatch_thread.is_alive())

                self.assertEqual(responses['a'].status_code, 202)
                self.assertEqual(responses['a'].json()['status'], 'accepted')
                self.assertEqual(responses['b'].status_code, 200)
                self.assertEqual(responses['b'].json()['status'], 'duplicate')
                self.assertEqual(len(dispatch_threads), 1)
                runtime_post.assert_called_once()

                snapshot = metrics.snapshot()
                self.assertEqual(snapshot['accepted'], 1)
                self.assertEqual(snapshot['duplicate'], 1)
                self.assertEqual(snapshot['rate_limited'], 0)
                self.assertEqual(snapshot['bridge_accepted'], 1)
                self.assertEqual(snapshot['reply_posted'], 0)
                self.assertEqual(snapshot['reply_failed'], 0)
                self.assertEqual(snapshot['pending']['count'], 0)
        finally:
            release.set()
            if worker.is_alive():
                worker.join(timeout=5)
            cache.clear()

    @override_settings(CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': os.environ.get(
                'CACHE_REDIS_URL', 'redis://127.0.0.1:6379/6'),
            'KEY_PREFIX': 'hive',
        },
    })
    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views.threading, 'Thread')
    def test_redis_reservation_failure_is_503_and_exported_fail_loud(self, thread, *_):
        """Exercise Redis persistence failure through the signed HTTP caller.

        The production Redis Lua reservation is the admission boundary.  If it
        cannot persist, the request must fail closed before dispatch and its
        reservation_store failure must reach the stage-labelled Prometheus
        series consumed by the owner-routed alert.
        """
        from claws import connect_delivery_metrics as metrics

        with patch(
                'redis.client.Redis.eval',
                side_effect=RuntimeError('redis unavailable')):
            response = self._post(self._payload(message_id='reservation-failure'))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'unavailable')
        thread.assert_not_called()
        self.assertIn(
            'hive_connect_delivery_reply_failed_total'
            '{stage="reservation_store"} 1',
            metrics.to_prometheus(),
        )

    @patch.object(views, '_claw_runtime_deliverable', return_value=(True, 'ready'))
    @patch.object(views.threading, 'Thread')
    def test_accepted_request_emits_safe_audit_fields(self, _thread, *_):
        with self.assertLogs('claws.views', level='INFO') as logs:
            response = self._post(self._payload())
        self.assertEqual(response.status_code, 202)
        audit = next(line for line in logs.output if 'outcome=accepted' in line)
        self.assertIn('service=shizuha-connect', audit)
        self.assertIn('owner_id=101', audit)
        self.assertIn('organization_id=7', audit)

    @patch.object(views, '_claw_runtime_deliverable',
                  return_value=(False, 'ready_replicas=0'))
    @patch.object(views.threading, 'Thread')
    def test_running_but_undeliverable_is_agent_not_running(self, mock_thread, *_):
        """HIVE-1276 D1: status=running with 0 ready replicas is a visible terminal."""
        r = self._post(self._payload(message_id='undeliverable-running'))
        self.assertEqual(r.status_code, 202)
        body = r.json()
        self.assertEqual(body['status'], 'agent_not_running')
        self.assertEqual(body.get('reason'), 'ready_replicas=0')
        mock_thread.assert_not_called()

    # ── helpers ──

    @override_settings(SHIZUHA_CONNECT_INTERNAL_URL='http://platform.internal')
    def test_connect_internal_base_honors_configured_platform_route(self):
        self.assertEqual(views._connect_internal_base_url(), 'http://platform.internal')

    @override_settings(SHIZUHA_CONNECT_INTERNAL_URL='', SHIZUHA_PLATFORM_URL='http://platform-from-settings')
    def test_connect_internal_base_falls_back_to_platform_not_raw_connect_service(self):
        self.assertEqual(views._connect_internal_base_url(), 'http://platform-from-settings')

    def test_hive_agent_user_id_matches_connect_formula(self):
        sid = self.stable_id
        digest = hashlib.sha256(sid.encode('utf-8')).hexdigest()
        expected = 1_500_000_000 + (int(digest[:8], 16) % 500_000_000)
        self.assertEqual(views._hive_agent_user_id(sid), expected)
        self.assertTrue(1_500_000_000 <= views._hive_agent_user_id(sid) < 2_000_000_000)

    def test_claw_from_stable_id_resolves_and_rejects(self):
        self.assertEqual(views._claw_from_stable_id(self.stable_id).id, self.claw.id)
        self.assertIsNone(views._claw_from_stable_id('hive:openclaw:not-a-uuid'))
        self.assertIsNone(views._claw_from_stable_id(
            f'hive:{Claw.AgentType.HERMES}:{self.claw.id}'))
        self.assertIsNone(views._claw_from_stable_id('garbage'))
        self.assertIsNone(views._claw_from_stable_id(''))


    def test_process_delivery_forwards_to_runtime_bridge(self):
        with patch.object(views, '_claw_delivery_token', return_value='runtime-token'), \
             patch.object(views.requests, 'post') as p:
            p.return_value = MagicMock(status_code=202, text='accepted')
            views._process_connect_delivery(
                self.claw, self.stable_id, 'c-9', 'm-9', 'question',
                'hive:con355:delivered:test-key')
            p.assert_called_once()
            _, kwargs = p.call_args
            self.assertEqual(kwargs['json'], {
                'stable_id': self.stable_id,
                'conversation_id': 'c-9',
                'message_id': 'm-9',
                'content': 'question',
            })

    @patch.object(views.requests, 'post')
    @patch.object(views, '_claw_delivery_token', return_value='test-token')
    @patch.object(views, '_claw_runtime_bridge_url', return_value='http://claw.ns:18789/v1/connect/messages')
    def test_process_delivery_records_bridge_rejection_without_raising(
            self, mock_url, mock_token, mock_post):
        # Bridge rejects, then the HIVE-1276 terminal-failure post succeeds.
        bridge_resp = MagicMock(status_code=403, text='Forbidden')
        terminal_resp = MagicMock(status_code=201, text='created')
        mock_post.side_effect = [bridge_resp, terminal_resp]
        cache.set('hive:con355:delivered:m-9', 1, 60)

        with self.assertLogs('claws.views', level='ERROR') as log:
            views._process_connect_delivery(
                self.claw, self.stable_id, 'c-9', 'm-9', 'question')

        mock_url.assert_called_once_with(self.claw)
        mock_token.assert_called_once_with(self.claw)
        self.assertEqual(mock_post.call_count, 2)
        # Terminal failure was posted, so retain dedupe and never double-fail.
        self.assertEqual(cache.get('hive:con355:delivered:m-9'), 1)
        terminal_call = mock_post.call_args_list[1]
        self.assertIn('/agent-message/', terminal_call.args[0])
        self.assertEqual(
            terminal_call.kwargs['json']['client_message_id'],
            'hive-con1276-fail-m-9',
        )
        self.assertTrue(
            str(terminal_call.kwargs['json']['content']).startswith(
                'Delivery failed:')
        )
        self.assertTrue(any(
            'runtime bridge rejected delivery: HTTP 403 Forbidden' in message
            for message in log.output
        ))

    @patch.object(views.requests, 'post')
    @patch.object(views, '_claw_delivery_token', return_value='test-token')
    @patch.object(views, '_claw_runtime_bridge_url', return_value='http://claw.ns:18789/v1/connect/messages')
    def test_process_delivery_clears_dedupe_when_terminal_post_also_fails(
            self, mock_url, mock_token, mock_post):
        # Both bridge and terminal-failure post fail, so clear dedupe for retry.
        mock_post.side_effect = [
            MagicMock(status_code=503, text='bridge down'),
            MagicMock(status_code=503, text='connect down'),
        ]
        cache.set('hive:con355:delivered:m-10', 1, 60)

        with self.assertLogs('claws.views', level='ERROR'):
            views._process_connect_delivery(
                self.claw, self.stable_id, 'c-10', 'm-10', 'question')

        self.assertEqual(mock_post.call_count, 2)
        self.assertIsNone(cache.get('hive:con355:delivered:m-10'))

    def test_connect_delivery_failure_text_is_user_visible(self):
        text = views._connect_delivery_failure_text(
            self.stable_id, 'connection refused')
        self.assertTrue(text.startswith('Delivery failed:'))
        self.assertIn(self.stable_id, text)
        self.assertIn('connection refused', text)

    # ── HIVE-1276 D1: _claw_runtime_deliverable unit cases ──

    def test_runtime_deliverable_no_namespace(self):
        claw = Claw(owner_id=1, name='x', tier_id='free', namespace='')
        ok, reason = views._claw_runtime_deliverable(claw)
        self.assertFalse(ok)
        self.assertEqual(reason, 'no_namespace')

    def test_runtime_deliverable_ready_ignores_empty_pod_name(self):
        """Empty pod_name is NOT undeliverable — only Deployment ready_replicas."""
        deploy = MagicMock()
        deploy.status.ready_replicas = 1
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment.return_value = deploy
        self.claw.pod_name = ''
        with patch('claws.provisioner.get_k8s_client', return_value=(None, apps_v1)):
            ok, reason = views._claw_runtime_deliverable(self.claw)
        self.assertTrue(ok)
        self.assertEqual(reason, 'ready')
        apps_v1.read_namespaced_deployment.assert_called_once()

    def test_runtime_deliverable_zero_ready_replicas(self):
        deploy = MagicMock()
        deploy.status.ready_replicas = 0
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment.return_value = deploy
        with patch('claws.provisioner.get_k8s_client', return_value=(None, apps_v1)):
            ok, reason = views._claw_runtime_deliverable(self.claw)
        self.assertFalse(ok)
        self.assertEqual(reason, 'ready_replicas=0')

    def test_runtime_deliverable_deployment_missing_404(self):
        from kubernetes.client.rest import ApiException

        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
        with patch('claws.provisioner.get_k8s_client', return_value=(None, apps_v1)):
            ok, reason = views._claw_runtime_deliverable(self.claw)
        self.assertFalse(ok)
        self.assertEqual(reason, 'deployment_missing')

    def test_runtime_deliverable_api_error_fail_open(self):
        from kubernetes.client.rest import ApiException

        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment.side_effect = ApiException(status=500)
        with patch('claws.provisioner.get_k8s_client', return_value=(None, apps_v1)):
            ok, reason = views._claw_runtime_deliverable(self.claw)
        self.assertTrue(ok)
        self.assertEqual(reason, 'api_error_fail_open:500')

    def test_runtime_deliverable_probe_unavailable_fail_open(self):
        with patch(
                'claws.provisioner.get_k8s_client',
                side_effect=RuntimeError('no kubeconfig')):
            ok, reason = views._claw_runtime_deliverable(self.claw)
        self.assertTrue(ok)
        self.assertTrue(reason.startswith('probe_unavailable_fail_open:'))


class PersonalOrganizationAuthorityTests(TestCase):
    def setUp(self):
        cache.clear()

    @staticmethod
    def _response(organizations):
        response = MagicMock(status_code=200)
        response.json.return_value = {'organizations': organizations}
        return response

    @patch.object(views.requests, 'get')
    def test_requires_exactly_one_personal_org_owned_by_user(self, get):
        get.return_value = self._response([
            {'id': 7, 'slug': 'personal-101', 'is_personal': True, 'owner_id': 101},
        ])
        self.assertEqual(views._owner_personal_organization_id(101), 7)

    @patch.object(views.requests, 'get')
    def test_slug_only_or_wrong_owner_is_rejected(self, get):
        get.return_value = self._response([
            {'id': 7, 'slug': 'personal-101', 'is_personal': False, 'owner_id': 101},
            {'id': 8, 'slug': 'workspace', 'is_personal': True, 'owner_id': 202},
        ])
        self.assertIsNone(views._owner_personal_organization_id(101))

    @patch.object(views.requests, 'get')
    def test_multiple_personal_orgs_fail_closed(self, get):
        get.return_value = self._response([
            {'id': 7, 'is_personal': True, 'owner_id': 101},
            {'id': 8, 'is_personal': True, 'owner_id': 101},
        ])
        self.assertIsNone(views._owner_personal_organization_id(101))
