"""Proof issuance must never invent shared signing material when misconfigured."""
import base64
import hashlib
import os
import secrets
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import SimpleTestCase

from . import metering_reservations as subject


class AdmissionSigningConfigurationTests(SimpleTestCase):
    def setUp(self):
        subject._admission_signing_material.cache_clear()
        self.addCleanup(subject._admission_signing_material.cache_clear)
        self.environment = mock.patch.dict(os.environ, {
            'METERING_ADMISSION_SIGNING_KEY': '', 'METERING_ADMISSION_SIGNING_KID': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def issue(self):
        return subject._issue_proof(profile='configuration-test', payload={'iat': 123456})

    def test_missing_or_invalid_secret_refuses_actual_proof_issuance_before_write(self):
        missing = object()
        for value in (missing, None, '', ' \t\n ', False, 0):
            settings = SimpleNamespace() if value is missing else SimpleNamespace(SECRET_KEY=value)
            with self.subTest(value_type=type(value).__name__), \
                    mock.patch.object(subject, 'settings', settings), \
                    mock.patch.object(subject.AdmissionProofReceipt.objects, 'create') as create:
                with self.assertRaises(subject.ReservationError) as raised:
                    self.issue()
                self.assertEqual(raised.exception.code, 'invalid_signing_key')
                self.assertEqual(raised.exception.http_status, 500)
                create.assert_not_called()

    def test_explicit_secret_preserves_existing_derivation_bytes_and_real_signature(self):
        secret = '  ' + secrets.token_hex(32) + '  '
        seed = hashlib.sha256(f'metering-admission-eddsa-v1:{secret}'.encode()).digest()
        public = Ed25519PrivateKey.from_private_bytes(seed).public_key()
        with mock.patch.object(subject, 'settings', SimpleNamespace(SECRET_KEY=secret)), \
                mock.patch.object(subject.AdmissionProofReceipt.objects, 'create',
                                  side_effect=lambda **fields: SimpleNamespace(**fields)):
            result = self.issue()
        verified = subject.verify_proof_envelope(
            jws_compact=result.jws_compact, expected_profile='configuration-test', public_key=public)
        self.assertEqual(verified['iat'], 123456)
        self.assertEqual(result.algorithm, 'EdDSA')

    def test_explicit_managed_signing_key_does_not_require_derivation_secret(self):
        seed = secrets.token_bytes(32)
        with mock.patch.dict(os.environ, {'METERING_ADMISSION_SIGNING_KEY': base64.urlsafe_b64encode(seed).decode()}), \
                mock.patch.object(subject, 'settings', SimpleNamespace()), \
                mock.patch.object(subject.AdmissionProofReceipt.objects, 'create',
                                  side_effect=lambda **fields: SimpleNamespace(**fields)):
            result = self.issue()
        public = Ed25519PrivateKey.from_private_bytes(seed).public_key()
        verified = subject.verify_proof_envelope(
            jws_compact=result.jws_compact, expected_profile='configuration-test', public_key=public)
        self.assertEqual(verified['profile'], 'configuration-test')

    def test_cached_valid_material_cannot_mask_configuration_removal(self):
        settings = SimpleNamespace(SECRET_KEY=secrets.token_hex(32))
        with mock.patch.object(subject, 'settings', settings), \
                mock.patch.object(subject.AdmissionProofReceipt.objects, 'create',
                                  side_effect=lambda **fields: SimpleNamespace(**fields)) as create:
            self.issue()
            self.assertEqual(create.call_count, 1)
            settings.SECRET_KEY = ''
            with self.assertRaises(subject.ReservationError):
                self.issue()
            self.assertEqual(create.call_count, 1)

    def test_cached_string_secret_cannot_mask_later_invalid_type(self):
        for value in (0, False, None):
            settings = SimpleNamespace(SECRET_KEY=str(value))
            with self.subTest(value_type=type(value).__name__), \
                    mock.patch.object(subject, 'settings', settings), \
                    mock.patch.object(subject.AdmissionProofReceipt.objects, 'create',
                                      side_effect=lambda **fields: SimpleNamespace(**fields)) as create:
                self.issue()
                self.assertEqual(create.call_count, 1)
                settings.SECRET_KEY = value
                with self.assertRaises(subject.ReservationError):
                    self.issue()
                self.assertEqual(create.call_count, 1)

    def test_delimiter_in_kid_or_secret_cannot_reuse_other_configuration(self):
        secret = secrets.token_hex(32)
        settings = SimpleNamespace(SECRET_KEY=secret)
        with mock.patch.object(subject, 'settings', settings), \
                mock.patch.object(subject.AdmissionProofReceipt.objects, 'create',
                                  side_effect=lambda **fields: SimpleNamespace(**fields)):
            with mock.patch.dict(os.environ, {'METERING_ADMISSION_SIGNING_KID': 'fixture|part'}):
                first = self.issue()
            settings.SECRET_KEY = 'part|' + secret
            with mock.patch.dict(os.environ, {'METERING_ADMISSION_SIGNING_KID': 'fixture'}):
                second = self.issue()
        self.assertEqual(first.kid, 'fixture|part')
        self.assertEqual(second.kid, 'fixture')
        seed = hashlib.sha256(f'metering-admission-eddsa-v1:{settings.SECRET_KEY}'.encode()).digest()
        verified = subject.verify_proof_envelope(
            jws_compact=second.jws_compact, expected_profile='configuration-test',
            expected_kid='fixture', public_key=Ed25519PrivateKey.from_private_bytes(seed).public_key())
        self.assertEqual(verified['iat'], 123456)
