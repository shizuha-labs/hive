"""PLAT-13818: signed active-generation endpoint contract tests.

Mirrors jun's consumer verification exactly: canonical JSON of the five
fields (sort_keys=True, separators=(',', ':'), UTF-8, ensure_ascii=False),
Ed25519 detached signature, base64url unpadded, signature field excluded
from the signing input and verified against the signing identity's public
JWK (the JWK Pulse pins via PULSE_HIVE_EVIDENCE_PUBLIC_JWKS).
"""
import base64
import datetime
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from fleet.capability_evidence import (
    MATERIALIZER_VERSION,
    _sha256,
    canonical_json_bytes,
    config_digest,
    load_signing_identity,
    materializer_digest,
    schema_digest,
)
from fleet.models import CapabilityEvidenceProducerGeneration

URL = '/internal/v1/fleet/capability-evidence/active-generation'


def _identity():
    pem = Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return load_signing_identity(pem, 'hive-cap-ed25519-v1')


def _generation_row(signing, sha='a' * 40):
    return CapabilityEvidenceProducerGeneration.objects.create(
        generation_id='hive-' + sha[:36],
        generation_digest=_sha256(canonical_json_bytes({'source': sha})),
        source_sha=sha,
        source_tree=sha,
        image_index_digest=_sha256(canonical_json_bytes({'img': sha})),
        schema_digest=schema_digest(),
        materializer_version=MATERIALIZER_VERSION,
        materializer_digest=materializer_digest(),
        config_digest=config_digest(),
        service_account='hive-api',
        signing_kid=signing.kid,
        signing_key_thumbprint=signing.thumbprint,
        activated_at=datetime.datetime(2026, 11, 16, 6, 32, 3, 580112, tzinfo=datetime.timezone.utc),
    )


class ActiveGenerationDocumentTests(TestCase):
    def test_valid_document_verifies_against_pinned_jwk(self):
        signing = _identity()
        row = _generation_row(signing)
        client = APIClient()
        response = client.get(URL)
        self.assertEqual(response.status_code, 200, response.content)
        document = response.json()
        self.assertEqual(
            sorted(document),
            ['activated_at', 'kid', 'producer_generation_digest',
             'producer_generation_id', 'signature', 'source_sha'],
        )
        self.assertEqual(document['kid'], signing.kid)
        self.assertEqual(document['producer_generation_id'], row.generation_id)
        self.assertEqual(document['producer_generation_digest'], row.generation_digest)
        self.assertEqual(document['activated_at'], row.activated_at.isoformat())
        self.assertEqual(document['source_sha'], row.source_sha)
        # jun's verification ladder: canonical form of the five fields,
        # signature excluded, Ed25519 verify against the public JWK.
        signed = {k: v for k, v in document.items() if k != 'signature'}
        public_raw = base64.urlsafe_b64decode(signing.public_jwk['x'] + '==')
        public_key = Ed25519PublicKey.from_public_bytes(public_raw)
        public_key.verify(
            base64.urlsafe_b64decode(document['signature'] + '=='),
            canonical_json_bytes(signed),
        )

    def test_tampered_field_fails_verification(self):
        signing = _identity()
        _generation_row(signing)
        document = APIClient().get(URL).json()
        signed = {k: v for k, v in document.items() if k != 'signature'}
        signed['producer_generation_id'] = 'hive-forged'
        public_raw = base64.urlsafe_b64decode(signing.public_jwk['x'] + '==')
        with self.assertRaises(Exception):
            Ed25519PublicKey.from_public_bytes(public_raw).verify(
                base64.urlsafe_b64decode(document['signature'] + '=='),
                canonical_json_bytes(signed),
            )

    def test_fails_closed_when_signer_unconfigured(self):
        _generation_row(_identity())
        with override_settings(HIVE_CAPABILITY_EVIDENCE_PRIVATE_KEY_PEM='', HIVE_CAPABILITY_EVIDENCE_KID=''):
            response = APIClient().get(URL)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'HIVE_EVIDENCE_SIGNER_NOT_CONFIGURED')

    def test_fails_closed_when_generation_not_current(self):
        signing = _identity()
        row = _generation_row(signing)
        # a second active row makes current_generation() reject (len != 1)
        CapabilityEvidenceProducerGeneration.objects.create(
            generation_id='hive-second',
            generation_digest=_sha256(canonical_json_bytes({'x': 2})),
            source_sha='b' * 40, source_tree='b' * 40,
            image_index_digest=_sha256(canonical_json_bytes({'y': 2})),
            schema_digest=schema_digest(),
            materializer_version=MATERIALIZER_VERSION,
            materializer_digest=materializer_digest(),
            config_digest=config_digest(),
            service_account='hive-api',
            signing_kid=signing.kid,
            signing_key_thumbprint=signing.thumbprint,
            activated_at=datetime.datetime(2026, 11, 16, 7, 0, 0, tzinfo=datetime.timezone.utc),
        )
        response = APIClient().get(URL)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'HIVE_PRODUCER_GENERATION_NOT_CURRENT')
