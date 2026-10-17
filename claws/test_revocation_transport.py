"""Exercise real HTTP revocation transport through the JWT authentication caller."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework.exceptions import AuthenticationFailed as InvalidToken

from claws.authentication import FederatedJWTAuthentication
from hive_platform import token_revocation as revocation


class RevocationTransportTests(SimpleTestCase):
    def setUp(self):
        self.responses = []
        self.calls = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.calls.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                status, body = owner.responses.pop(0)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/api/oauth/revocation-status'
        self.env = patch.dict(os.environ, {
            'SHIZUHA_REVOCATION_STATUS_URL': self.url,
            'SHIZUHA_EXPECTED_ISSUER': 'https://identity.example/id',
        })
        self.env.start()
        revocation._REVOCATION_CACHE.clear()
        self.raw = str(AccessToken())
        self.auth = FederatedJWTAuthentication()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        revocation._REVOCATION_CACHE.clear()

    def test_unavailable_lookup_denies_but_next_request_recovers(self):
        self.responses.extend([(503, {}), (200, {'active': True})])
        with self.assertRaises(InvalidToken):
            self.auth.validate_token(self.raw)
        self.assertNotIn(revocation._cache_key(self.raw), revocation._REVOCATION_CACHE)
        self.auth.validate_token(self.raw)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0], ('/api/oauth/revocation-status', {'token': self.raw}))
        self.assertEqual(os.environ['SHIZUHA_EXPECTED_ISSUER'], 'https://identity.example/id')

    def test_real_revocation_still_denies_and_is_cached(self):
        self.responses.extend([(200, {'active': True}), (200, {'active': False})])
        self.auth.validate_token(self.raw)
        revocation._REVOCATION_CACHE[revocation._cache_key(self.raw)] = (time.monotonic()-1, False)
        for _ in range(2):
            with self.assertRaises(InvalidToken):
                self.auth.validate_token(self.raw)
        self.assertEqual(len(self.calls), 2)

    def test_malformed_active_never_authorizes_or_poisons_cache(self):
        self.responses.extend([(200, []), (200, {'active': 'false'}), (200, {'active': True})])
        for _ in range(2):
            with self.assertRaises(InvalidToken):
                self.auth.validate_token(self.raw)
            self.assertFalse(revocation._REVOCATION_CACHE)
        self.auth.validate_token(self.raw)

    def test_legacy_issuer_url_remains_compatible_when_unconfigured(self):
        with patch.dict(os.environ, {'SHIZUHA_REVOCATION_STATUS_URL': ''}):
            self.assertEqual(revocation._revocation_status_url(), 'https://identity.example/id/api/oauth/revocation-status')
