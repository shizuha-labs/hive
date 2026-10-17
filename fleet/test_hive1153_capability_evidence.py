"""HIVE-1153: exact-seat Ed25519 capability-evidence producer regressions."""
import base64
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from fleet import capability_evidence as ce
from fleet.models import (
    CapabilityEvidenceProducerGeneration,
    FleetAgent,
    ensure_team_required_capability,
)
from fleet.views import _roster_process_membership_write


PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
PRIVATE_PEM = PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
KID = 'hive-cap-ed25519-test-v1'


def _request(**overrides):
    value = {
        'schema_version': ce.REQUEST_SCHEMA,
        'request_id': '11111111-1111-1111-1111-111111111111',
        'email': 'san@shizuha.com',
        'organization_id': 1,
        'team_slug': 'devops',
        'requirement_key': 'autonomous-dev::in_progress',
        'required_capabilities': ['bench-runner', 'host-exec', 'kubeconfig'],
        'item_id': 20626,
        'assignment_generation': 7,
    }
    value.update(overrides)
    return value


@override_settings(
    HIVE_INTERNAL_AUTH_TOKEN='broader-hive-internal-token',
    HIVE_CAPABILITY_EVIDENCE_AUTH_TOKEN='hive1153-service-token',
    HIVE_CAPABILITY_EVIDENCE_PRIVATE_KEY_PEM=PRIVATE_PEM,
    HIVE_CAPABILITY_EVIDENCE_KID=KID,
    HIVE_CAPABILITY_EVIDENCE_SERVICE_ACCOUNT='hive-api',
    HIVE_CAPABILITY_EVIDENCE_ROSTER_MAX_AGE_SECONDS=90,
)
class CapabilityEvidenceProducerTests(TestCase):
    def setUp(self):
        self.agent = FleetAgent.objects.create(
            agent_username='san', agent_id='san-platform-id', role='DevOps',
            status=FleetAgent.Status.RUNNING,
        )
        ensure_team_required_capability('devops', actor='hive1153-test')
        self.memberships = [{
            'email': 'san@shizuha.com',
            'organization_id': 1,
            'organization_slug': 'shizuha',
            'team_slug': 'devops',
        }]
        _roster_process_membership_write('san@shizuha.com', self.memberships, time.time())
        signing = ce.load_signing_identity()
        self.generation = CapabilityEvidenceProducerGeneration.objects.create(
            generation_id='hive-' + 'a' * 40,
            source_sha='a' * 40,
            source_tree='b' * 40,
            image_index_digest='sha256:' + 'c' * 64,
            schema_digest=ce.schema_digest(),
            materializer_version=ce.MATERIALIZER_VERSION,
            materializer_digest=ce.materializer_digest(),
            config_digest=ce.config_digest(),
            service_account='hive-api',
            signing_kid=signing.kid,
            signing_key_thumbprint=signing.thumbprint,
            activated_at=timezone.now(),
        )

    def _post(self, request=None, **headers):
        raw = ce.canonical_json_bytes(request or _request())
        return self.client.post(
            '/internal/v1/fleet/capability-evidence',
            data=raw,
            content_type='application/json',
            HTTP_X_HIVE_INTERNAL_TOKEN='hive1153-service-token',
            **headers,
        )

    def test_exact_seat_returns_only_compact_jws(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {'evidence'})
        token = response.json()['evidence']
        header, claims = ce.decode_and_verify_for_test(token, PRIVATE_KEY.public_key())
        self.assertEqual(header, {'alg': 'EdDSA', 'kid': KID, 'typ': ce.EVIDENCE_TYP})
        self.assertEqual(claims['results'], {
            'bench-runner': 'ABSENT',
            'host-exec': 'PRESENT',
            'kubeconfig': 'PRESENT',
        })
        self.assertEqual(claims['request_sha256'], ce.request_sha256(_request()))
        self.assertEqual(claims['producer_generation_id'], self.generation.generation_id)
        self.assertEqual(claims['producer_generation_digest'], self.generation.generation_digest)
        self.assertTrue(claims['materialization_generation'].startswith('mat-sha256:'))
        self.assertEqual(claims['exp'] - claims['iat'], 5)

    def test_pulse_transport_contract_reaches_real_view_and_binds_exact_body(self):
        """Mirror Pulse's requests.post(data=..., headers=...) at the real view.

        The sibling consumer regression owns the requests transport assertion;
        this complementary producer regression proves those exact bytes and the
        shared service credential cross the Django/DRF boundary unchanged.
        """
        request_doc = _request()
        transmitted = ce.canonical_json_bytes(request_doc)
        response = self.client.post(
            '/internal/v1/fleet/capability-evidence',
            data=transmitted,
            content_type='application/json',
            HTTP_X_HIVE_INTERNAL_TOKEN='hive1153-service-token',
        )

        self.assertEqual(response.status_code, 200)
        _header, claims = ce.decode_and_verify_for_test(
            response.json()['evidence'], PRIVATE_KEY.public_key(),
        )
        self.assertEqual(
            claims['request_sha256'],
            'sha256:' + hashlib.sha256(transmitted).hexdigest(),
        )

    def test_service_auth_and_ingress_guard_fail_closed(self):
        raw = ce.canonical_json_bytes(_request())
        unauthenticated = self.client.post(
            '/internal/v1/fleet/capability-evidence', data=raw,
            content_type='application/json',
        )
        self.assertEqual(unauthenticated.status_code, 403)
        broader_internal_token = self.client.post(
            '/internal/v1/fleet/capability-evidence', data=raw,
            content_type='application/json',
            HTTP_X_HIVE_INTERNAL_TOKEN='broader-hive-internal-token',
        )
        self.assertEqual(broader_internal_token.status_code, 403)
        ingress = self._post(HTTP_X_FORWARDED_FOR='203.0.113.10')
        self.assertEqual(ingress.status_code, 404)

    def test_caller_org_team_are_assertions_not_grant_selectors(self):
        wrong_team = self._post(_request(team_slug='engineering'))
        self.assertEqual(wrong_team.status_code, 409)
        self.assertEqual(wrong_team.json()['error'], 'HIVE_EVIDENCE_EXACT_SEAT_MISMATCH')
        wrong_org = self._post(_request(organization_id=99))
        self.assertEqual(wrong_org.status_code, 409)

        # A capability from a second real roster seat must not leak into the
        # exact devops-seat statement merely because the subject holds both.
        ensure_team_required_capability('bench-runner', actor='hive1153-test')
        multi_seat = self.memberships + [{
            'email': 'san@shizuha.com',
            'organization_id': 1,
            'organization_slug': 'shizuha',
            'team_slug': 'bench-runner',
        }]
        _roster_process_membership_write('san@shizuha.com', multi_seat, time.time())
        response = self._post()
        self.assertEqual(response.status_code, 200)
        _header, claims = ce.decode_and_verify_for_test(
            response.json()['evidence'], PRIVATE_KEY.public_key(),
        )
        self.assertEqual(claims['results']['bench-runner'], 'ABSENT')

    def test_stale_or_incomplete_roster_never_mints(self):
        _roster_process_membership_write(
            'san@shizuha.com', self.memberships, time.time() - 91,
        )
        with patch('fleet.views._roster_membership_cache_read', return_value=(None, 0)), patch(
            'fleet.views._roster_cache_read', return_value=(None, 0),
        ), patch('fleet.views._service_pulse_roster_index', return_value={}):
            response = self._post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error'], 'HIVE_MATERIALIZATION_ROSTER_STALE')

    def test_fresh_replica_uses_authoritative_shared_full_roster_snapshot(self):
        with patch('fleet.views._roster_process_entry_read', return_value=(None, 0)), patch(
            'fleet.views._roster_membership_cache_read', return_value=(None, 0),
        ), patch(
            'fleet.views._roster_cache_read',
            return_value=({'san@shizuha.com': self.memberships}, time.time()),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200)

    def test_stale_shared_full_roster_snapshot_never_mints(self):
        with patch('fleet.views._roster_process_entry_read', return_value=(None, 0)), patch(
            'fleet.views._roster_membership_cache_read', return_value=(None, 0),
        ), patch(
            'fleet.views._roster_cache_read',
            return_value=({'san@shizuha.com': self.memberships}, time.time() - 91),
        ), patch('fleet.views._service_pulse_roster_index', return_value={}):
            response = self._post()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error'], 'HIVE_MATERIALIZATION_ROSTER_STALE')

    def test_cold_replica_self_heals_from_network_secured_service_roster(self):
        """PLAT-8592: zero staff fleet reads leave all three sources cold; the
        producer must self-heal by projecting the subject from Pulse's
        network-secured service roster instead of starving ingestion."""
        with patch('fleet.views._roster_process_entry_read', return_value=(None, 0)), patch(
            'fleet.views._roster_membership_cache_read', return_value=(None, 0),
        ), patch(
            'fleet.views._roster_cache_read', return_value=(None, 0),
        ), patch(
            'fleet.views._service_pulse_roster_index',
            return_value={'san@shizuha.com': self.memberships},
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200)

    def test_service_roster_absence_keeps_failing_closed(self):
        """PLAT-8592: absence of the subject in the service roster is a valid
        authoritative answer — never authorize from absence."""
        with patch('fleet.views._roster_process_entry_read', return_value=(None, 0)), patch(
            'fleet.views._roster_membership_cache_read', return_value=(None, 0),
        ), patch(
            'fleet.views._roster_cache_read', return_value=(None, 0),
        ), patch(
            'fleet.views._service_pulse_roster_index', return_value={},
        ):
            response = self._post()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error'], 'HIVE_MATERIALIZATION_ROSTER_STALE')

    def test_non_current_generation_or_wrong_key_never_mints(self):
        self.generation.signing_kid = 'wrong-kid'
        CapabilityEvidenceProducerGeneration.objects.filter(pk=self.generation.pk).update(
            signing_kid='wrong-kid',
        )
        response = self._post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error'], 'HIVE_PRODUCER_GENERATION_NOT_CURRENT')

    def test_request_shape_canonicalization_and_duplicates_fail_closed(self):
        extra = _request(extra='not-allowed')
        response = self._post(extra)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'HIVE_EVIDENCE_REQUEST_FIELDS_MISMATCH')

        noncanonical = json.dumps(_request(), sort_keys=False).encode()
        response = self.client.post(
            '/internal/v1/fleet/capability-evidence', data=noncanonical,
            content_type='application/json',
            HTTP_X_HIVE_INTERNAL_TOKEN='hive1153-service-token',
        )
        self.assertEqual(response.json()['error'], 'HIVE_EVIDENCE_REQUEST_NOT_CANONICAL')

        duplicate = ce.canonical_json_bytes(_request()).decode().replace(
            '"item_id":20626', '"item_id":20626,"item_id":20627',
        ).encode()
        response = self.client.post(
            '/internal/v1/fleet/capability-evidence', data=duplicate,
            content_type='application/json',
            HTTP_X_HIVE_INTERNAL_TOKEN='hive1153-service-token',
        )
        self.assertEqual(response.json()['error'], 'HIVE_EVIDENCE_DUPLICATE_JSON_KEY')

    def test_generation_bound_fields_and_history_are_immutable(self):
        self.generation.source_sha = 'd' * 40
        with self.assertRaises(ValidationError):
            self.generation.save()
        with self.assertRaises(ValidationError):
            self.generation.delete()

    def test_activation_command_rotates_once_and_is_idempotent(self):
        args = [
            '--generation-id', 'hive-' + 'd' * 40,
            '--source-sha', 'd' * 40,
            '--source-tree', 'e' * 40,
            '--image-index-digest', 'sha256:' + 'f' * 64,
        ]
        call_command('activate_capability_evidence_generation', *args)
        successor = CapabilityEvidenceProducerGeneration.objects.get(pk='hive-' + 'd' * 40)
        self.assertTrue(successor.active)
        self.generation.refresh_from_db()
        self.assertFalse(self.generation.active)
        call_command('activate_capability_evidence_generation', *args)
        self.assertEqual(CapabilityEvidenceProducerGeneration.objects.filter(active=True).count(), 1)


class CapabilityEvidenceGoldenVectorTests(TestCase):
    fixture_path = Path(__file__).with_name('fixtures') / 'hive_capability_evidence_v2_golden.json'

    def test_checked_vector_sha_signature_and_producer_bytes(self):
        vector = json.loads(self.fixture_path.read_text())
        declared_sha = vector.pop('vector_sha256')
        self.assertEqual(
            declared_sha,
            'sha256:' + hashlib.sha256(ce.canonical_json_bytes(vector)).hexdigest(),
        )
        self.assertEqual(declared_sha, 'sha256:15fc74daa8fea53ce4a5539eeccd3016e6b3eb7bffe9a64bb4b16d655a2fe2ab')

        signing = ce.load_signing_identity(PRIVATE_PEM, vector['public_jwk']['kid'])
        header, claims = ce.decode_and_verify_for_test(
            vector['compact_jws'], signing.private_key.public_key(),
        )
        self.assertEqual(header, {
            'alg': 'EdDSA', 'kid': vector['public_jwk']['kid'], 'typ': ce.EVIDENCE_TYP,
        })
        self.assertEqual(claims, vector['claims'])
        self.assertEqual(signing.public_jwk, vector['public_jwk'])

        generation = SimpleNamespace(
            generation_id=vector['producer_generation']['producer_generation_id'],
            generation_digest=vector['producer_generation']['producer_generation_digest'],
        )
        materialized = (
            vector['claims']['results'],
            vector['claims']['materialization_generation'],
            0,
        )
        request_raw = ce.canonical_json_bytes(vector['request'])
        with patch('fleet.capability_evidence.current_generation', return_value=(generation, signing)), patch(
            'fleet.capability_evidence._materialize', return_value=materialized,
        ):
            compact, produced_claims = ce.produce_evidence(
                request_raw,
                now=vector['claims']['iat'],
                jti=vector['claims']['jti'],
            )
        self.assertEqual(produced_claims, vector['claims'])
        self.assertEqual(compact, vector['compact_jws'])

    def test_unsigned_alg_substitution_unknown_kid_and_tamper_reject_signature(self):
        vector = json.loads(self.fixture_path.read_text())
        token = vector['compact_jws']
        header, payload, signature = token.split('.')
        pad = lambda value: value + '=' * (-len(value) % 4)
        header_doc = json.loads(base64.urlsafe_b64decode(pad(header)))
        signing = ce.load_signing_identity(PRIVATE_PEM, vector['public_jwk']['kid'])

        for mutated_header in (
            {**header_doc, 'alg': 'none'},
            {**header_doc, 'alg': 'HS256'},
            {**header_doc, 'kid': 'unknown'},
        ):
            encoded = base64.urlsafe_b64encode(ce.canonical_json_bytes(mutated_header)).rstrip(b'=').decode()
            with self.assertRaises(Exception):
                ce.decode_and_verify_for_test(
                    f'{encoded}.{payload}.{signature}', signing.private_key.public_key(),
                )
        with self.assertRaises(Exception):
            ce.decode_and_verify_for_test(
                f'{header}.{payload}.', signing.private_key.public_key(),
            )
