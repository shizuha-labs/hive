import base64
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import TestCase
from django.utils import timezone

from .mcp_projection import (
    AUTHORITY_CURRENT,
    AUTHORITY_UNKNOWN,
    PROTOCOL_PREFIX,
    canonical_projection_envelope,
    fence_projection_authority_leases,
    projection_fingerprint,
    renew_projection_authority_leases,
)
from .models import AgentMcpProjectionState, FleetAgent


def _nonce():
    return base64.urlsafe_b64encode(b'n' * 32).decode().rstrip('=')


def _payload(services):
    return {'enabled_mcp_servers': services, 'catalog_version': 7}


class McpProjectionTests(TestCase):
    def setUp(self):
        self.agent = FleetAgent.objects.create(
            agent_username='plat5275', agent_id='runtime-agent-1',
        )
        self.private_key = Ed25519PrivateKey.generate()
        self.signing = SimpleNamespace(kid='test-ed25519-v1', private_key=self.private_key)

    def _state(self, services=('pulse',)):
        return AgentMcpProjectionState.objects.create(
            agent=self.agent,
            fingerprint=projection_fingerprint(list(services), 7),
            generation=1,
            current_high_water=1,
            authority_state=AUTHORITY_CURRENT,
            lease_deadline=timezone.now() + timedelta(seconds=4),
        )

    def test_canonical_nonce_bound_envelope_verifies(self):
        state = self._state()
        with patch('fleet.mcp_projection.load_projection_signing_identity', return_value=self.signing):
            raw = canonical_projection_envelope(
                _payload(['pulse']), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce(),
            )
        self.assertEqual(raw, json.dumps(json.loads(raw), sort_keys=True, separators=(',', ':')))
        envelope = json.loads(raw)
        self.assertEqual(envelope['projection']['generation'], state.generation)
        self.assertEqual(envelope['projection']['agent_audience'], self.agent.agent_id)
        claims = json.dumps(envelope['projection'], sort_keys=True, separators=(',', ':')).encode()
        preimage = PROTOCOL_PREFIX + b'\0' + (b'n' * 32) + b'\0' + claims
        self.private_key.public_key().verify(base64.urlsafe_b64decode(envelope['signature'] + '=='), preimage)

    def test_generation_is_stable_then_advances_on_scope_removal(self):
        self._state(('mail', 'pulse'))
        with patch('fleet.mcp_projection.load_projection_signing_identity', return_value=self.signing):
            first = json.loads(canonical_projection_envelope(
                _payload(['mail', 'pulse']), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce(),
            ))
            second = json.loads(canonical_projection_envelope(
                _payload(['pulse', 'mail']), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce(),
            ))
        self.assertEqual(first['projection']['generation'], second['projection']['generation'])
        with self.assertRaisesRegex(RuntimeError, 'lease unavailable'):
            canonical_projection_envelope(
                _payload(['pulse']), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce(),
            )
        state = AgentMcpProjectionState.objects.get(agent=self.agent)
        self.assertEqual(state.generation, 2)
        self.assertEqual(state.authority_state, AUTHORITY_UNKNOWN)

    def test_only_authenticated_lease_writer_reenables_current_generation(self):
        state = self._state()
        fence_projection_authority_leases([self.agent.id])
        with self.assertRaisesRegex(RuntimeError, 'lease unavailable'):
            canonical_projection_envelope(
                _payload(['pulse']), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce(),
            )
        self.assertEqual(renew_projection_authority_leases([self.agent.id]), 1)
        state.refresh_from_db()
        self.assertEqual(state.authority_state, AUTHORITY_CURRENT)
        self.assertEqual(state.current_high_water, state.generation)

    def test_silent_stall_lease_expires_without_projection_reads_renewing_it(self):
        state = self._state()
        original_deadline = state.lease_deadline
        with patch('fleet.mcp_projection.load_projection_signing_identity', return_value=self.signing):
            canonical_projection_envelope(
                _payload(['pulse']), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce(),
            )
        state.refresh_from_db()
        self.assertEqual(state.lease_deadline, original_deadline)
        self.assertGreater(state.lease_deadline, timezone.now())  # non-zero allowed control
        state.lease_deadline = timezone.now() - timedelta(milliseconds=1)
        state.save(update_fields=['lease_deadline'])
        with self.assertRaisesRegex(RuntimeError, 'lease unavailable'):
            canonical_projection_envelope(
                _payload(['pulse']), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce(),
            )

    def test_cold_replay_of_removed_scope_stays_fenced_until_current_lease_renews(self):
        self._state(('mail', 'pulse'))
        with self.assertRaisesRegex(RuntimeError, 'lease unavailable'):
            canonical_projection_envelope(
                _payload(['pulse']), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce(),
            )
        state = AgentMcpProjectionState.objects.get(agent=self.agent)
        self.assertEqual(state.generation, 2)
        self.assertEqual(state.authority_state, AUTHORITY_UNKNOWN)
        self.assertEqual(renew_projection_authority_leases([self.agent.id]), 1)
        with patch('fleet.mcp_projection.load_projection_signing_identity', return_value=self.signing):
            envelope = json.loads(canonical_projection_envelope(
                _payload(['pulse']), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce(),
            ))
        self.assertEqual(envelope['projection']['generation'], 2)
        self.assertEqual(envelope['projection']['mcp_services'], ['pulse'])
        self.assertNotIn('mail', envelope['projection']['mcp_services'])

    def test_expired_or_mismatched_lease_refuses_signature(self):
        state = self._state()
        state.lease_deadline = timezone.now() - timedelta(milliseconds=1)
        state.save(update_fields=['lease_deadline'])
        with self.assertRaisesRegex(RuntimeError, 'lease unavailable'):
            canonical_projection_envelope(
                _payload(['pulse']), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce(),
            )
        state.lease_deadline = timezone.now() + timedelta(seconds=4)
        state.current_high_water = 2
        state.save(update_fields=['lease_deadline', 'current_high_water'])
        with self.assertRaisesRegex(RuntimeError, 'lease unavailable'):
            canonical_projection_envelope(
                _payload(['pulse']), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce(),
            )


    def _mint_qualified_after_authority_witness(self, refs):
        # The producer must materialize the exact tuple before the established
        # authenticated currentness writer can renew it; never self-renew.
        with self.assertRaisesRegex(RuntimeError, 'lease unavailable'):
            canonical_projection_envelope(_payload(refs), agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce())
        self.assertEqual(renew_projection_authority_leases([self.agent.id]), 1)
        with patch('fleet.mcp_projection.load_projection_signing_identity', return_value=self.signing):
            return json.loads(canonical_projection_envelope(_payload(refs),
                agent_id=str(self.agent.id), agent_audience=self.agent.agent_id, nonce=_nonce()))

    def test_tenant_runtime_refs_emit_v1_logical_service_ids_without_changing_grants(self):
        refs = ['wiki:shizuha-digital', 'pulse', 'pulse:shizuha-digital']
        envelope = self._mint_qualified_after_authority_witness(refs)
        claims = envelope['projection']
        self.assertEqual(claims['mcp_services'], ['pulse', 'wiki'])
        self.assertIsInstance(claims['catalog_version'], str)
        self.assertLessEqual(len(claims['catalog_version']), 128)
        self.assertEqual(refs, ['wiki:shizuha-digital', 'pulse', 'pulse:shizuha-digital'])
        preimage = PROTOCOL_PREFIX+b'\0'+b'n'*32+b'\0'+json.dumps(claims, sort_keys=True, separators=(',', ':')).encode()
        self.private_key.public_key().verify(base64.urlsafe_b64decode(envelope['signature']+'=='), preimage)

    def test_tenant_only_removal_fences_unchanged_logical_services(self):
        first = self._mint_qualified_after_authority_witness(['pulse', 'pulse:customer-a', 'pulse:customer-b'])
        second = self._mint_qualified_after_authority_witness(['pulse', 'pulse:customer-a'])
        self.assertEqual(first['projection']['mcp_services'], second['projection']['mcp_services'])
        self.assertNotEqual(first['projection']['fingerprint'], second['projection']['fingerprint'])
        self.assertNotEqual(first['projection']['catalog_version'], second['projection']['catalog_version'])
        self.assertEqual(second['projection']['generation'], first['projection']['generation']+1)
        third = self._mint_qualified_after_authority_witness(['pulse'])
        self.assertNotEqual(third['projection']['fingerprint'], second['projection']['fingerprint'])
        self.assertEqual(third['projection']['generation'], second['projection']['generation']+1)

    def test_tenant_ref_order_and_duplicates_do_not_churn_authority(self):
        refs = ['pulse', 'wiki:customer-a', 'pulse:customer-a']
        first = self._mint_qualified_after_authority_witness(refs)
        with patch('fleet.mcp_projection.load_projection_signing_identity', return_value=self.signing):
            second = json.loads(canonical_projection_envelope(_payload(list(reversed(refs))+[refs[0]]),
                agent_id=str(self.agent.id), agent_audience=self.agent.agent_id, nonce=_nonce()))
        self.assertEqual(first['projection']['fingerprint'], second['projection']['fingerprint'])
        self.assertEqual(first['projection']['generation'], second['projection']['generation'])

    def test_invalid_or_unknown_runtime_refs_fail_before_authority_or_signature(self):
        for ref in ['pulse:', ':customer-a', 'pulse:a:b', 'pulse/customer-a',
                    'pulse:../customer-a', 'pulse:Customer-A', ' pulse', 'pulse:customer_a',
                    'not-a-catalog-service', 'not-a-catalog-service:customer-a', 1, None]:
            with self.subTest(ref=ref), patch('fleet.mcp_projection._projection_state') as state:
                with self.assertRaises(ValueError):
                    canonical_projection_envelope(_payload([ref]), agent_id=str(self.agent.id),
                        agent_audience=self.agent.agent_id, nonce=_nonce())
                state.assert_not_called()


    def test_hive_catalog_service_is_qualified_without_restoring_retired_routes(self):
        envelope = self._mint_qualified_after_authority_witness(['hive:customer-a'])
        self.assertEqual(envelope['projection']['mcp_services'], ['hive'])

    def test_qualified_catalog_revision_change_also_fences_authority(self):
        first = self._mint_qualified_after_authority_witness(['pulse:customer-a'])
        payload = {**_payload(['pulse:customer-a']), 'catalog_version': 8}
        with self.assertRaisesRegex(RuntimeError, 'lease unavailable'):
            canonical_projection_envelope(payload, agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce())
        self.assertEqual(renew_projection_authority_leases([self.agent.id]), 1)
        with patch('fleet.mcp_projection.load_projection_signing_identity', return_value=self.signing):
            second = json.loads(canonical_projection_envelope(payload, agent_id=str(self.agent.id),
                agent_audience=self.agent.agent_id, nonce=_nonce()))
        self.assertNotEqual(first['projection']['catalog_version'], second['projection']['catalog_version'])
        self.assertEqual(first['projection']['generation']+1, second['projection']['generation'])

    def test_catalog_revision_obeys_existing_v1_consumer_types_and_bounds(self):
        for value in [True, False, -1, 1.5, 9007199254740992, '', 'x'*129, [], None]:
            with self.subTest(value=value), patch('fleet.mcp_projection._projection_state') as state:
                with self.assertRaises(ValueError):
                    canonical_projection_envelope({**_payload(['pulse:customer-a']), 'catalog_version': value},
                        agent_id=str(self.agent.id), agent_audience=self.agent.agent_id, nonce=_nonce())
                state.assert_not_called()
