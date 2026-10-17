"""Unit tests for CodexBar-compatible quota fetchers (no network)."""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from . import quota_fetchers as qf
from .quota_projection_v1 import project_quota_snapshot


class ClaudeOAuthUsageTests(SimpleTestCase):
    @patch('hive_platform.quota_fetchers.requests.get')
    def test_parses_utilization(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                'five_hour': {'utilization': 0.42, 'resets_at': '2026-07-09T12:00:00Z'},
                'seven_day': {'utilization': 0.61, 'resets_at': '2026-07-12T00:00:00Z'},
            },
        )
        out = qf.fetch_claude_oauth_usage('sk-ant-oat01-test')
        self.assertEqual(out['confidence'], 'exact')
        self.assertEqual(len(out['windows']), 2)
        self.assertAlmostEqual(out['windows'][0]['used_percent'], 42.0)
        self.assertAlmostEqual(out['windows'][0]['left_percent'], 58.0)
        self.assertNotIn('raw', out)

    @patch('hive_platform.quota_fetchers.requests.get')
    def test_unknown_nested_provider_fields_never_reach_snapshot(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                'five_hour': {'utilization': 0.42},
                'accessToken': 'alternate-secret',
                'nested': {'token': 'nested-secret'},
            },
        )

        projected = project_quota_snapshot(qf.fetch_claude_oauth_usage('test'))

        self.assertEqual(
            set(projected),
            {
                'quota_schema_version', 'source', 'fetched_at',
                'confidence', 'plan', 'windows', 'detail',
            },
        )
        self.assertNotIn('alternate-secret', str(projected))
        self.assertNotIn('nested-secret', str(projected))

    @patch('hive_platform.quota_fetchers.requests.get')
    def test_auth_failed(self, mock_get):
        mock_get.return_value = MagicMock(status_code=401, text='nope')
        out = qf.fetch_claude_oauth_usage('bad')
        self.assertEqual(out['detail'], 'auth_failed')
        self.assertEqual(out['confidence'], 'error')


class CodexWhamUsageTests(SimpleTestCase):
    @patch('hive_platform.quota_fetchers.requests.get')
    def test_parses_primary_secondary(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                'rate_limit': {
                    'primary_window': {'used_percent': 30, 'resets_at': '2026-07-09T10:00:00Z'},
                    'secondary_window': {'usedPercent': 55, 'resetsAt': '2026-07-14T00:00:00Z'},
                },
                'plan_type': 'plus',
            },
        )
        out = qf.fetch_codex_wham_usage('eyJ')
        self.assertEqual(out['confidence'], 'exact')
        self.assertEqual(out['schema_version'], qf.CODEX_QUOTA_SCHEMA_VERSION)
        self.assertEqual(out['plan'], 'plus')
        self.assertEqual(len(out['windows']), 2)
        self.assertAlmostEqual(out['windows'][0]['left_percent'], 70.0)
        self.assertNotIn('raw', out)

    @patch('hive_platform.quota_fetchers.requests.get')
    def test_primary_only_seven_day_window_is_labeled_weekly(self, mock_get):
        """Current token-based plans can return weekly as the only main lane."""
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                'rate_limit': {
                    'primary_window': {
                        'used_percent': 61,
                        'limit_window_seconds': 604800,
                        'reset_at': 1784780155,
                    },
                    'secondary_window': None,
                },
                'plan_type': 'pro',
            },
        )
        out = qf.fetch_codex_wham_usage('eyJ')
        self.assertEqual(len(out['windows']), 1)
        self.assertEqual(out['windows'][0]['id'], 'weekly')
        self.assertEqual(out['windows'][0]['label'], 'Weekly')
        self.assertAlmostEqual(out['windows'][0]['left_percent'], 39.0)


class CodexWhamSparkAndResetCreditsTests(SimpleTestCase):
    """2026-07-14 (operator): show per-model buckets (GPT-5.3-Codex-Spark) and
    'usage resets available' on /hive/subscriptions. Payload shape captured live."""

    LIVE_SHAPE = {
        'plan_type': 'pro',
        'rate_limit': {
            'allowed': False,
            'limit_reached': True,
            'primary_window': {
                'used_percent': 100,
                'limit_window_seconds': 18000,
                'reset_after_seconds': 1234,
                'reset_at': '2026-07-20T00:31:11Z',
            },
        },
        'additional_rate_limits': [{
            'limit_name': 'GPT-5.3-Codex-Spark',
            'metered_feature': 'codex_bengalfox',
            'rate_limit': {
                'allowed': True,
                'limit_reached': False,
                'primary_window': {
                    'used_percent': 0,
                    'limit_window_seconds': 604800,
                    'reset_after_seconds': 604800,
                },
            },
        }],
        'rate_limit_reset_credits': {'available_count': 1},
    }

    @patch('hive_platform.quota_fetchers.requests.get')
    def test_parses_spark_bucket_and_reset_credits(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: self.LIVE_SHAPE)
        out = qf.fetch_codex_wham_usage('eyJ')
        self.assertEqual(out['confidence'], 'exact')
        labels = [w['label'] for w in out['windows']]
        self.assertIn('Session (5h)', labels)
        self.assertIn('GPT-5.3-Codex-Spark', labels)
        spark = next(w for w in out['windows'] if w['label'] == 'GPT-5.3-Codex-Spark')
        self.assertAlmostEqual(spark['used_percent'], 0.0)
        self.assertEqual(out['reset_credits'], {'available': 1})

    @patch('hive_platform.quota_fetchers.requests.get')
    def test_legacy_flat_extra_shape_still_parses(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {
            'rate_limit': {
                'primary_window': {'used_percent': 10},
                'additional_rate_limits': [
                    {'name': 'mini', 'used_percent': 20},
                ],
            },
        })
        out = qf.fetch_codex_wham_usage('eyJ')
        labels = [w['label'] for w in out['windows']]
        self.assertIn('mini', labels)
        self.assertIsNone(out['reset_credits'])

    @patch('hive_platform.quota_fetchers.requests.get')
    def test_reset_credits_detail_endpoint(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {
            'available_count': 1,
            'total_earned_count': 0,
            'credits': [{
                'id': 'x', 'reset_type': 'codex_rate_limits', 'status': 'available',
                'granted_at': '2026-07-01T00:00:00Z', 'expires_at': '2026-08-12T17:40:45Z',
                'profile_image_url': 'REDACT-ME', 'profile_user_id': 'REDACT-ME',
            }],
        })
        out = qf.fetch_codex_reset_credits('eyJ', account_id='acc')
        self.assertEqual(out['available'], 1)
        self.assertEqual(out['total_earned'], 0)
        self.assertEqual(out['credits'][0]['reset_type'], 'codex_rate_limits')
        # profile fields must NOT leak into the persisted snapshot
        self.assertNotIn('profile_image_url', out['credits'][0])

    @patch('hive_platform.quota_fetchers.requests.get')
    def test_reset_credits_failure_returns_none(self, mock_get):
        mock_get.return_value = MagicMock(status_code=500, json=lambda: {})
        self.assertIsNone(qf.fetch_codex_reset_credits('eyJ'))
        self.assertIsNone(qf.fetch_codex_reset_credits(''))

    @patch('hive_platform.quota_fetchers.requests.get')
    def test_reset_credits_keep_id_for_redeem(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {
            'available_count': 1,
            'credits': [{'id': 'RateLimitResetCredit_abc', 'status': 'available',
                         'reset_type': 'codex_rate_limits'}],
        })
        out = qf.fetch_codex_reset_credits('eyJ')
        self.assertEqual(out['credits'][0]['id'], 'RateLimitResetCredit_abc')

    @patch('hive_platform.quota_fetchers.requests.post')
    def test_redeem_reset_credit_ok(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {
            'code': 'reset',
            'credit': {'id': 'RateLimitResetCredit_abc', 'status': 'redeemed',
                       'redeemed_at': '2026-07-14T00:00:00Z'},
            'windows_reset': 1,
        })
        out = qf.redeem_codex_reset_credit('eyJ', account_id='acc', credit_id='RateLimitResetCredit_abc')
        self.assertTrue(out['ok'])
        self.assertEqual(out['windows_reset'], 1)
        self.assertEqual(out['status'], 'redeemed')
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs['json']['credit_id'], 'RateLimitResetCredit_abc')
        self.assertIn('redeem_request_id', kwargs['json'])
        self.assertEqual(kwargs['headers']['ChatGPT-Account-ID'], 'acc')

    def test_redeem_requires_token_but_allows_missing_credit_id(self):
        self.assertEqual(qf.redeem_codex_reset_credit('', credit_id='x')['detail'], 'missing_access_token')

        with patch('hive_platform.quota_fetchers.requests.post') as mock_post:
            mock_post.return_value = MagicMock(status_code=200, json=lambda: {
                'code': 'reset', 'windows_reset': 2,
            })
            out = qf.redeem_codex_reset_credit('eyJ', credit_id='')

        self.assertTrue(out['ok'])
        self.assertEqual(out['windows_reset'], 2)
        self.assertNotIn('credit_id', mock_post.call_args.kwargs['json'])

    @patch('hive_platform.quota_fetchers.requests.post')
    def test_redeem_200_no_credit_is_not_success(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {
            'code': 'no_credit', 'windows_reset': 0,
        })
        out = qf.redeem_codex_reset_credit('eyJ')
        self.assertFalse(out['ok'])
        self.assertEqual(out['detail'], 'no_credit')

    @patch('hive_platform.quota_fetchers.requests.post')
    def test_redeem_http_error_not_ok(self, mock_post):
        mock_post.return_value = MagicMock(status_code=409, text='already redeemed')
        out = qf.redeem_codex_reset_credit('eyJ', credit_id='x')
        self.assertFalse(out['ok'])
        self.assertTrue(out['detail'].startswith('http_409'))


class GrokBillingTests(SimpleTestCase):
    # Captured live from grok.com GetGrokCreditsConfig on 2026-07-11 (org-xai
    # pool token, credits fully consumed, window resets 2026-07-16T08:54:07Z).
    # gRPC-web DATA frame + trailer frame; no secrets — floats and timestamps.
    PAYLOAD = (
        b'\x00\x00\x00\x00_\n]\r\x00\x00\xc8B\x12\x00\x1a\x00'
        b'"\x0c\x08\xaf\xc3\xbd\xd2\x06\x10\xf0\xf7\xb8\xfb\x01'
        b'*\x0c\x08\xaf\xb8\xe2\xd2\x06\x10\xf0\xf7\xb8\xfb\x01'
        b':\x07\x08\x01\x15\x00\x00\xb0B:\x07\x08\x02\x15\x00\x00@A'
        b'B\x1e\x08\x02\x12\x0c\x08\xaf\xc3\xbd\xd2\x06\x10\xf0\xf7\xb8\xfb\x01'
        b'\x1a\x0c\x08\xaf\xb8\xe2\xd2\x06\x10\xf0\xf7\xb8\xfb\x01'
        b'X\x01b\x00h\x02'
        b'\x80\x00\x00\x00\x0fgrpc-status:0\r\n'
    )

    @patch('hive_platform.quota_fetchers.fetch_grok_reset_credits', return_value=None)
    @patch('hive_platform.quota_fetchers.requests.post')
    def test_parses_live_billing_payload(self, mock_post, _reset):
        mock_post.return_value = MagicMock(
            status_code=200, content=self.PAYLOAD, headers={'grpc-status': '0'},
        )
        # The captured reset is fixed in time. Freeze the parser clock before
        # that boundary so this fixture remains a parser test after 2026-07-16.
        with patch('time.time', return_value=1784188800):
            out = qf.fetch_grok_usage('tok')
        self.assertEqual(out['confidence'], 'exact')
        self.assertEqual(out['detail'], 'ok_via_grok_billing')
        self.assertEqual(len(out['windows']), 1)
        w = out['windows'][0]
        self.assertEqual(w['label'], 'Credits')
        self.assertAlmostEqual(w['used_percent'], 100.0)
        self.assertAlmostEqual(w['left_percent'], 0.0)
        self.assertIn('2026-07-16T08:54:07', w['resets_at'])
        self.assertNotIn('reset_credits', out)

    @patch('hive_platform.quota_fetchers.requests.get')
    @patch('hive_platform.quota_fetchers.requests.post')
    def test_oauth_rejected_falls_back_to_unsupported(self, mock_post, mock_get):
        mock_post.return_value = MagicMock(status_code=403, content=b'', headers={})
        mock_get.return_value = MagicMock(status_code=404, text='')
        out = qf.fetch_grok_usage('tok')
        self.assertEqual(out['confidence'], 'unsupported')
        self.assertEqual(out['detail'], 'usage_api_unavailable')

    @patch('hive_platform.quota_fetchers.fetch_grok_reset_credits', return_value=None)
    @patch('hive_platform.quota_fetchers.requests.post')
    def test_no_usage_yet_reports_zero_used(self, mock_post, _reset):
        # Percent fields absent but a future reset present -> 0% used (mirrors
        # CodexBar's noUsageYet rule for fresh accounts).
        frame = b'\n\x10*\x0c\x08\xaf\xb8\xe2\xd2\x06\x10\xf0\xf7\xb8\xfb\x01h\x02'
        payload = b'\x00' + len(frame).to_bytes(4, 'big') + frame
        mock_post.return_value = MagicMock(
            status_code=200, content=payload, headers={'grpc-status': '0'},
        )
        with patch('time.time', return_value=1784188800):
            out = qf.fetch_grok_usage('tok')
        self.assertEqual(out['confidence'], 'exact')
        self.assertAlmostEqual(out['windows'][0]['used_percent'], 0.0)


def _proto_varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _proto_ld(field: int, payload: bytes) -> bytes:
    return _proto_varint((field << 3) | 2) + _proto_varint(len(payload)) + payload


def _proto_var(field: int, value: int) -> bytes:
    return _proto_varint((field << 3) | 0) + _proto_varint(value)


def _grpcweb_frame(payload: bytes) -> bytes:
    return b'\x00' + len(payload).to_bytes(4, 'big') + payload + (
        b'\x80\x00\x00\x00\x0fgrpc-status:0\r\n'
    )


class GrokResetCreditsTests(SimpleTestCase):
    """prod_mc_billing.ConsumerUiSvc/GetRemainingResets (gRPC-web).

    Shape captured live 2026-08-17: repeated ConsumerResetToken field 10 with
    token_id=10, validity_start=20, validity_end=30 (google.protobuf.Timestamp
    seconds at field 1). Hive SuperGrok OAuth bearer is accepted.
    """

    START = 1786560540  # 2026-08-12T18:49:00Z
    END = 1789238940    # 2026-09-12T18:49:00Z (grok.com UI "Sep 13" in IST)

    def _token_msg(self, token_id='restok_test1', start=START, end=END):
        return (
            _proto_ld(10, token_id.encode())
            + _proto_ld(20, _proto_var(1, start))
            + _proto_ld(30, _proto_var(1, end))
        )

    def _resp(self, payload: bytes, status=200, grpc='0'):
        return MagicMock(status_code=status, content=payload, headers={'grpc-status': grpc})

    @patch('hive_platform.quota_fetchers.requests.post')
    def test_parses_one_pending_reset(self, mock_post):
        mock_post.return_value = self._resp(_grpcweb_frame(_proto_ld(10, self._token_msg())))
        with patch('time.time', return_value=self.START + 3600):
            out = qf.fetch_grok_reset_credits('tok')
        self.assertEqual(out['available'], 1)
        credit = out['credits'][0]
        self.assertEqual(credit['id'], 'restok_test1')
        self.assertEqual(credit['reset_type'], 'grok_usage_limit')
        self.assertEqual(credit['status'], 'available')
        self.assertEqual(credit['expires_at'], '2026-09-12T18:49:00+00:00')
        url = mock_post.call_args.args[0]
        self.assertIn('prod_mc_billing.ConsumerUiSvc/GetRemainingResets', url)

    @patch('hive_platform.quota_fetchers.requests.post')
    def test_empty_tokens_is_zero_not_failure(self, mock_post):
        mock_post.return_value = self._resp(_grpcweb_frame(b''))
        out = qf.fetch_grok_reset_credits('tok')
        self.assertEqual(out, {'available': 0})

    @patch('hive_platform.quota_fetchers.requests.post')
    def test_expired_token_not_counted_as_available(self, mock_post):
        mock_post.return_value = self._resp(_grpcweb_frame(_proto_ld(10, self._token_msg())))
        with patch('time.time', return_value=self.END + 60):
            out = qf.fetch_grok_reset_credits('tok')
        self.assertEqual(out['available'], 0)
        self.assertEqual(out['credits'][0]['status'], 'expired')

    @patch('hive_platform.quota_fetchers.requests.post')
    def test_http_or_grpc_failure_returns_none(self, mock_post):
        mock_post.return_value = self._resp(b'', status=500)
        self.assertIsNone(qf.fetch_grok_reset_credits('tok'))
        mock_post.return_value = self._resp(b'', grpc='12')
        self.assertIsNone(qf.fetch_grok_reset_credits('tok'))
        self.assertIsNone(qf.fetch_grok_reset_credits(''))

    @patch('hive_platform.quota_fetchers.fetch_grok_reset_credits')
    @patch('hive_platform.quota_fetchers.requests.post')
    def test_usage_snapshot_attaches_reset_credits(self, mock_post, mock_reset):
        mock_post.return_value = MagicMock(
            status_code=200, content=GrokBillingTests.PAYLOAD, headers={'grpc-status': '0'},
        )
        mock_reset.return_value = {
            'available': 1,
            'credits': [{'id': 'restok_test1', 'reset_type': 'grok_usage_limit',
                         'status': 'available'}],
        }
        with patch('time.time', return_value=1784188800):
            out = qf.fetch_grok_usage('tok')
        self.assertEqual(out['confidence'], 'exact')
        self.assertEqual(out['reset_credits']['available'], 1)
        mock_reset.assert_called_once_with('tok')

    @patch('hive_platform.quota_fetchers.fetch_grok_reset_credits', return_value=None)
    @patch('hive_platform.quota_fetchers.requests.post')
    def test_reset_fetch_failure_does_not_break_usage(self, mock_post, _reset):
        mock_post.return_value = MagicMock(
            status_code=200, content=GrokBillingTests.PAYLOAD, headers={'grpc-status': '0'},
        )
        with patch('time.time', return_value=1784188800):
            out = qf.fetch_grok_usage('tok')
        self.assertEqual(out['confidence'], 'exact')
        self.assertNotIn('reset_credits', out)

    def test_projection_keeps_grok_reset_credits(self):
        projected = project_quota_snapshot({
            'source': 'grok_xai_usage',
            'confidence': 'exact',
            'detail': 'ok_via_grok_billing',
            'reset_credits': {
                'available': 1,
                'credits': [{
                    'id': 'restok_test1',
                    'reset_type': 'grok_usage_limit',
                    'status': 'available',
                    'expires_at': '2026-09-12T18:49:00+00:00',
                }],
            },
        })
        self.assertEqual(projected['reset_credits']['available'], 1)
        self.assertEqual(projected['reset_credits']['credits'][0]['id'], 'restok_test1')
        self.assertEqual(
            projected['reset_credits']['credits'][0]['reset_type'],
            'grok_usage_limit',
        )


class GoogleAntigravityUsageTests(SimpleTestCase):
    """Antigravity quota must not invent full bars when scrape fails."""

    @patch('socket.socket')
    @patch('hive_platform.quota_fetchers.os.path.exists', return_value=False)
    @patch('hive_platform.quota_fetchers.requests.post')
    def test_loadcodeassist_without_windows_stays_partial_empty(self, mock_post, _exists, mock_sock):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {'currentTier': {'name': 'Antigravity', 'id': 'free-tier'}},
        )
        mock_sock.return_value.connect_ex.return_value = 1  # no open local ports
        out = qf.fetch_google_usage('tok')
        self.assertEqual(out['confidence'], 'partial')
        self.assertEqual(out['windows'], [])
        self.assertIn('quota_windows_unavailable', out['detail'])
        # Never paint green-full when we did not scrape remainingFraction
        self.assertFalse(any(
            (w.get('left_percent') == 100.0) for w in out.get('windows') or []
        ))
