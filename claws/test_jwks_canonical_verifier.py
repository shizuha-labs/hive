"""Canonical JWKS / RS256 verifier vectors for hive (PLAT-986 / PLAT-675).

Mirrors the canonical suite first shipped on pulse (shizuha-tasks
test_jwks_canonical_verifier.py), adapted to hive's
``FederatedJWTAuthentication`` (BaseAuthentication + PyJWKClient). Asserts the
hardened contract on ``authenticate``:

  1. Valid HS256 token accepted          (zero regression / dual-accept)
  2. ``alg=none`` rejected
  3. Unsupported alg (RS384) rejected     (alg-allowlist)
  4. HS256/RS256 key-confusion rejected   (RS256 payload HS256-signed w/ pubkey)
  5. Valid RS256 token (kid → JWKS) accepted
  6. Fail-closed: kid-less RS256, unknown-kid all rejected

Network is never touched — ``_jwks_client`` is monkeypatched.
"""
import base64
import hashlib
import hmac
import json
import time

import jwt
from unittest import mock

from django.test import SimpleTestCase
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from rest_framework import exceptions
from rest_framework_simplejwt.tokens import AccessToken

from claws.authentication import FederatedJWTAuthentication

KID = "test-rs256-kid"


def _make_rsa():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub


class _MockRequest:
    def __init__(self, token):
        self.META = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        self.COOKIES = {}


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


def _patch_jwks(pubkey=None, raise_exc=None):
    """Patch FederatedJWTAuthentication._jwks_client to a fake (no network)."""
    class _FakeClient:
        def get_signing_key_from_jwt(self, token):
            if raise_exc is not None:
                raise raise_exc
            return _FakeSigningKey(pubkey)
    return mock.patch.object(
        FederatedJWTAuthentication, "_jwks_client", classmethod(lambda cls: _FakeClient())
    )


def _rs256(priv, *, kid=KID, alg="RS256"):
    headers = {} if kid is None else {"kid": kid}
    payload = {
        "user_id": 1, "email": "a@b.c", "username": "u",
        "token_type": "access", "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, priv, algorithm=alg, headers=headers)


def _rs256_with_version(priv, version, *, kid=KID, alg="RS256"):
    """RS256 token carrying a ``token_version`` claim (PLAT-5996)."""
    headers = {} if kid is None else {"kid": kid}
    payload = {
        "user_id": 1, "email": "a@b.c", "username": "u",
        "token_type": "access", "token_version": version,
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, priv, algorithm=alg, headers=headers)


def _fake_response(body):
    """Minimal stand-in for requests.Response (JSON body only)."""
    class _FakeResp:
        status_code = 200
        def json(self):
            return body
    return _FakeResp()


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")


# ── PLAT-1023: token revocation enforced on BOTH verifier paths ──────────────
# A validly-signed but REVOKED OAuth token (has a client_id) must be rejected;
# non-OAuth tokens (no client_id) skip the check; lookup errors fail CLOSED.
from claws import authentication as _auth_mod          # noqa: E402
from claws import token_revocation as _revmod          # noqa: E402


def _rsa_pubkey(pub):
    return jwt.algorithms.RSAAlgorithm(
        jwt.algorithms.RSAAlgorithm.SHA256
    ).prepare_key(pub.decode())


def _rs256_oauth(priv, *, kid=KID):
    """RS256 token carrying a client_id → an OAuth token subject to revocation."""
    payload = {
        "user_id": 1, "email": "a@b.c", "username": "u", "client_id": "cli-1",
        "token_type": "access", "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid})


# ── Vector 1: valid HS256 accepted (dual-accept / zero regression) ──────────
class JWKSCanonicalVerifierTests(SimpleTestCase):
    def setUp(self):
        _revmod._REVOCATION_CACHE.clear()
        # SCLI-376: the revocation consult now applies to EVERY token type.
        # Default the revocation endpoint to active=True so the acceptance
        # vectors prove signature/alg/key routing, and revocation-specific
        # vectors override per-test.
        self._post = mock.patch.object(
            _revmod.requests, "post", lambda *a, **k: _fake_response({"active": True})
        )
        self._post.start()

    def tearDown(self):
        self._post.stop()
        _revmod._REVOCATION_CACHE.clear()

    def test_valid_hs256_accepted(self):
        raw = str(AccessToken())  # simplejwt mints an HS256 token w/ the configured key
        user, validated = FederatedJWTAuthentication().authenticate(_MockRequest(raw))
        self.assertTrue(user.is_authenticated)

    # ── Vector 2: alg=none rejected ─────────────────────────────────────────
    def test_alg_none_rejected(self):
        raw = jwt.encode({"sub": "1"}, key=None, algorithm="none")
        with self.assertRaises(exceptions.AuthenticationFailed):
            FederatedJWTAuthentication().authenticate(_MockRequest(raw))

    # ── Vector 3: unsupported alg (RS384) rejected by allowlist ─────────────
    def test_unsupported_alg_rejected(self):
        priv, _ = _make_rsa()
        raw = _rs256(priv, alg="RS384")
        with self.assertRaises(exceptions.AuthenticationFailed):
            FederatedJWTAuthentication().authenticate(_MockRequest(raw))

    # ── Vector 4: HS256/RS256 key-confusion rejected ───────────────────────
    def test_key_confusion_rejected(self):
        """alg=HS256 header, HMAC-signed with the RSA public-key bytes. Strict routing
        sends it to the HS256/simplejwt path which verifies against the shared
        SIGNING_KEY (not the RSA key), so the forged signature fails."""
        _, pub = _make_rsa()
        head = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
        body = _b64(json.dumps({"user_id": 1, "token_type": "access",
                                "exp": int(time.time()) + 3600}).encode())
        sig = _b64(hmac.new(pub, head + b"." + body, hashlib.sha256).digest())
        forged = (head + b"." + body + b"." + sig).decode()
        with self.assertRaises(exceptions.AuthenticationFailed):
            FederatedJWTAuthentication().authenticate(_MockRequest(forged))

    # ── Vector 5: valid RS256 (kid → JWKS) accepted ─────────────────────────
    def test_valid_rs256_accepted(self):
        priv, pub = _make_rsa()
        pubkey = jwt.algorithms.RSAAlgorithm(
            jwt.algorithms.RSAAlgorithm.SHA256
        ).prepare_key(pub.decode())
        with _patch_jwks(pubkey=pubkey):
            raw = _rs256(priv)
            user, validated = FederatedJWTAuthentication().authenticate(_MockRequest(raw))
        self.assertEqual(user.id, 1)
        self.assertEqual(validated.get("token_type"), "access")

    # ── Vector 6: fail-closed — kid-less, unknown-kid ───────────────────────
    def test_rs256_missing_kid_rejected(self):
        priv, pub = _make_rsa()
        pubkey = jwt.algorithms.RSAAlgorithm(
            jwt.algorithms.RSAAlgorithm.SHA256
        ).prepare_key(pub.decode())
        with _patch_jwks(pubkey=pubkey):
            raw = _rs256(priv, kid=None)
            with self.assertRaises(exceptions.AuthenticationFailed):
                FederatedJWTAuthentication().authenticate(_MockRequest(raw))

    def test_rs256_unknown_kid_failclosed(self):
        priv, _ = _make_rsa()
        # JWKS lookup raises (kid not found / JWKS down) → must fail closed
        with _patch_jwks(raise_exc=jwt.exceptions.PyJWKClientError("no key")):
            raw = _rs256(priv, kid="some-other-kid")
            with self.assertRaises(exceptions.AuthenticationFailed):
                FederatedJWTAuthentication().authenticate(_MockRequest(raw))

    def test_revoked_rs256_oauth_token_rejected(self):
        priv, pub = _make_rsa()
        with _patch_jwks(pubkey=_rsa_pubkey(pub)), \
             mock.patch.object(_auth_mod, "is_token_revoked", lambda claims, raw: True):
            with self.assertRaises(exceptions.AuthenticationFailed):
                FederatedJWTAuthentication().authenticate(_MockRequest(_rs256_oauth(priv)))

    def test_active_rs256_oauth_token_accepted(self):
        priv, pub = _make_rsa()
        with _patch_jwks(pubkey=_rsa_pubkey(pub)), \
             mock.patch.object(_auth_mod, "is_token_revoked", lambda claims, raw: False):
            user, validated = FederatedJWTAuthentication().authenticate(
                _MockRequest(_rs256_oauth(priv)))
        self.assertEqual(user.id, 1)

    def test_rs256_revocation_fail_closed(self):
        """Real is_token_revoked + revocation endpoint raising → fail closed → reject."""
        priv, pub = _make_rsa()

        def _raise(*a, **k):
            raise Exception("revocation endpoint down")
        with _patch_jwks(pubkey=_rsa_pubkey(pub)), \
             mock.patch.object(_revmod.requests, "post", _raise):
            with self.assertRaises(exceptions.AuthenticationFailed):
                FederatedJWTAuthentication().authenticate(_MockRequest(_rs256_oauth(priv)))

    def test_non_oauth_rs256_revocation_consulted_active(self):
        """SCLI-376: no client_id → revocation IS consulted; accepted iff active."""
        priv, pub = _make_rsa()
        with _patch_jwks(pubkey=_rsa_pubkey(pub)), \
             mock.patch.object(_revmod.requests, "post",
                               lambda *a, **k: _fake_response({"active": True})):
            user, validated = FederatedJWTAuthentication().authenticate(
                _MockRequest(_rs256(priv)))  # _rs256 carries no client_id
        self.assertEqual(user.id, 1)

    def test_non_oauth_rs256_revoked_rejected(self):
        """SCLI-376: broker/agent JWT (no client_id) revoked in ID → rejected."""
        priv, pub = _make_rsa()
        with _patch_jwks(pubkey=_rsa_pubkey(pub)), \
             mock.patch.object(_revmod.requests, "post",
                               lambda *a, **k: _fake_response({"active": False})):
            with self.assertRaises(exceptions.AuthenticationFailed):
                FederatedJWTAuthentication().authenticate(_MockRequest(_rs256(priv)))

    def test_stale_token_version_rejected_through_real_caller(self):
        """PLAT-5996: a replayed pre-revocation token (token_version < current)
        is rejected through the real FederatedJWTAuthentication caller even when
        the OAuth-store row is gone (revive-by-replay closure)."""
        priv, pub = _make_rsa()
        with _patch_jwks(pubkey=_rsa_pubkey(pub)), \
             mock.patch.object(_revmod.requests, "post",
                               lambda *a, **k: _fake_response(
                                   {"active": True, "token_version": 3})):
            with self.assertRaises(exceptions.AuthenticationFailed):
                FederatedJWTAuthentication().authenticate(
                    _MockRequest(_rs256_with_version(priv, 1)))

    def test_current_token_version_accepted_through_real_caller(self):
        """PLAT-5996: a token carrying the CURRENT version is accepted through
        the real caller (zero regression on the happy path)."""
        priv, pub = _make_rsa()
        with _patch_jwks(pubkey=_rsa_pubkey(pub)), \
             mock.patch.object(_revmod.requests, "post",
                               lambda *a, **k: _fake_response(
                                   {"active": True, "token_version": 3})):
            user, validated = FederatedJWTAuthentication().authenticate(
                _MockRequest(_rs256_with_version(priv, 3)))
        self.assertEqual(user.id, 1)

    def test_no_token_version_claim_accepted_during_rollout(self):
        """PLAT-5996 coordinated rollout: a token WITHOUT the token_version
        claim (old ID) is evaluated by the active check alone — accepted when
        active, no version comparison."""
        priv, pub = _make_rsa()
        with _patch_jwks(pubkey=_rsa_pubkey(pub)), \
             mock.patch.object(_revmod.requests, "post",
                               lambda *a, **k: _fake_response(
                                   {"active": True, "token_version": 3})):
            user, validated = FederatedJWTAuthentication().authenticate(
                _MockRequest(_rs256(priv)))
        self.assertEqual(user.id, 1)

    def test_revoked_hs256_oauth_token_rejected(self):
        """The HS256 path also enforces revocation (check sits after path convergence)."""
        with mock.patch.object(_auth_mod, "is_token_revoked", lambda claims, raw: True):
            with self.assertRaises(exceptions.AuthenticationFailed):
                FederatedJWTAuthentication().authenticate(_MockRequest(str(AccessToken())))
