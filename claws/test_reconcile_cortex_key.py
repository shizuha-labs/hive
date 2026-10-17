"""HIVE-2233 — regression fixtures for the Claw↔Cortex key reconcile saga.

Covers ≥2 top-level reconcile attempts plus every reset/re-arm boundary
(prepared / bound / verifying / retire_pending) and the fail-closed rules:
CAS drift, never-revoke-before-proof, exact-fence revoke failure -> retire_pending,
and idempotent noop on an already-compliant live key.
"""
import base64
import uuid
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from claws.models import Claw, ClawCortexKeyBinding
from claws.reconcile_cortex_key import cortex_mint_key, reconcile_claw_cortex_key

KEK = {'HIVE_TOKEN_COORDINATOR_KEK': 'unit-test-kek', 'HIVE_TOKEN_COORDINATOR_KEK_ID': 'unit-v1'}


def _make_claw(**overrides) -> Claw:
    fields = {
        'owner_id': 275,
        'owner_username': 'owner',
        'owner_email': 'owner@example.com',
        'name': 'Reconcile Claw',
        'namespace': 'claw-275-reconcile',
        'status': Claw.Status.RUNNING,
        'agent_type': Claw.AgentType.SCLI,
    }
    fields.update(overrides)
    return Claw.objects.create(**fields)


def _secret_obj(version='rv-1', raw_key='sk-cortex-old'):
    data = {'cortex-api-key': base64.b64encode(raw_key.encode()).decode()}
    return MagicMock(metadata=MagicMock(resource_version=version), data=data)


def _deploy_obj(generation=1, observed=1, available=1, replicas=1):
    deploy = MagicMock()
    deploy.metadata.generation = generation
    deploy.metadata.annotations = {}
    deploy.spec.replicas = replicas
    deploy.spec.template.metadata.annotations = {}
    deploy.status.observed_generation = observed
    deploy.status.available_replicas = available
    return deploy


@override_settings(**KEK)
class ReconcileCortexKeyTests(TestCase):
    def setUp(self):
        self.claw = _make_claw()

    def _mock_k8s(self, secret=None, deploy=None):
        core = MagicMock()
        core.read_namespaced_secret.return_value = secret or _secret_obj()
        core.replace_namespaced_secret.return_value = MagicMock()
        apps = MagicMock()
        apps.read_namespaced_deployment.return_value = deploy or _deploy_obj()
        apps.replace_namespaced_deployment.return_value = MagicMock()
        return core, apps

    def test_already_compliant_live_key_is_noop(self):
        """A live key already at the desired tier must not mint or mutate."""
        core, apps = self._mock_k8s(secret=_secret_obj(raw_key='sk-cortex-compliant'))
        with patch('claws.reconcile_cortex_key.get_k8s_client', return_value=(core, apps)), \
             patch('claws.reconcile_cortex_key.cortex_inspect_key', return_value={
                 'id': str(uuid.uuid4()), 'service_tier': 'standard',
             }) as inspect, \
             patch('claws.reconcile_cortex_key.cortex_mint_key') as mint:
            binding, outcome = reconcile_claw_cortex_key(self.claw.id)
        self.assertEqual(outcome, 'noop')
        self.assertEqual(binding.state, ClawCortexKeyBinding.State.ACTIVE)
        inspect.assert_called_once()
        mint.assert_not_called()

    def test_non_compliant_cutover_converges_and_revokes_old(self):
        """internal 0/0 -> standard: mint, CAS patch, rollout, proof, revoke old."""
        old_key = 'sk-cortex-internal-0000'
        core, apps = self._mock_k8s(secret=_secret_obj(raw_key=old_key))
        new_key = 'sk-cortex-standard-new'
        new_id = str(uuid.uuid4())
        with patch('claws.reconcile_cortex_key.get_k8s_client', return_value=(core, apps)), \
             patch('claws.reconcile_cortex_key.cortex_inspect_key', return_value={
                 'id': str(uuid.uuid4()), 'service_tier': 'internal',
             }), \
             patch('claws.reconcile_cortex_key.cortex_mint_key', return_value={
                 'id': new_id, 'key': new_key, 'service_tier': 'standard',
             }), \
             patch('claws.reconcile_cortex_key._cortex_proof', return_value=True) as proof, \
             patch('claws.reconcile_cortex_key.cortex_revoke_key', return_value=(True, 'revoked')) as revoke:
            binding, outcome = reconcile_claw_cortex_key(self.claw.id)
        self.assertEqual(outcome, 'converged')
        self.assertEqual(binding.state, ClawCortexKeyBinding.State.ACTIVE)
        self.assertEqual(binding.desired_tier, 'standard')
        proof.assert_called_once()
        revoke.assert_called_once()
        # The old key was captured as the prior artifact and revoked.
        self.assertTrue(binding.prior_artifact)
        self.assertEqual(binding.cortex_key_uuid, uuid.UUID(new_id))

    def test_cas_drift_fails_closed_before_stamp(self):
        """A resourceVersion mismatch must abort the cutover (no stamp/revoke)."""
        core, apps = self._mock_k8s(secret=_secret_obj(version='rv-1', raw_key='sk-old'))
        # Simulate drift: the CAS re-read sees a different version.
        drifted = _secret_obj(version='rv-2', raw_key='sk-old')
        core.read_namespaced_secret.side_effect = [_secret_obj(version='rv-1', raw_key='sk-old'), drifted]
        with patch('claws.reconcile_cortex_key.get_k8s_client', return_value=(core, apps)), \
             patch('claws.reconcile_cortex_key.cortex_inspect_key', return_value={
                 'id': str(uuid.uuid4()), 'service_tier': 'internal',
             }), \
             patch('claws.reconcile_cortex_key.cortex_mint_key', return_value={
                 'id': str(uuid.uuid4()), 'key': 'sk-new', 'service_tier': 'standard',
             }), \
             patch('claws.reconcile_cortex_key._cortex_proof') as proof, \
             patch('claws.reconcile_cortex_key.cortex_revoke_key') as revoke:
            binding, outcome = reconcile_claw_cortex_key(self.claw.id)
        self.assertEqual(outcome, 'error')
        self.assertIn('CAS', binding.last_error)
        proof.assert_not_called()
        revoke.assert_not_called()
        core.replace_namespaced_secret.assert_not_called()

    def test_proof_failure_never_revokes_old(self):
        """A failed new-runtime proof must abort before revoke (fail-closed)."""
        core, apps = self._mock_k8s(secret=_secret_obj(raw_key='sk-old'))
        with patch('claws.reconcile_cortex_key.get_k8s_client', return_value=(core, apps)), \
             patch('claws.reconcile_cortex_key.cortex_inspect_key', return_value={
                 'id': str(uuid.uuid4()), 'service_tier': 'internal',
             }), \
             patch('claws.reconcile_cortex_key.cortex_mint_key', return_value={
                 'id': str(uuid.uuid4()), 'key': 'sk-new', 'service_tier': 'standard',
             }), \
             patch('claws.reconcile_cortex_key._cortex_proof', return_value=False), \
             patch('claws.reconcile_cortex_key.cortex_revoke_key') as revoke:
            binding, outcome = reconcile_claw_cortex_key(self.claw.id)
        self.assertEqual(outcome, 'error')
        self.assertIn('proof', binding.last_error)
        revoke.assert_not_called()

    def test_revoke_failure_is_durable_retire_pending(self):
        """A failed old-key revoke must land in retire_pending, not lose the key."""
        core, apps = self._mock_k8s(secret=_secret_obj(raw_key='sk-old'))
        with patch('claws.reconcile_cortex_key.get_k8s_client', return_value=(core, apps)), \
             patch('claws.reconcile_cortex_key.cortex_inspect_key', return_value={
                 'id': str(uuid.uuid4()), 'service_tier': 'internal',
             }), \
             patch('claws.reconcile_cortex_key.cortex_mint_key', return_value={
                 'id': str(uuid.uuid4()), 'key': 'sk-new', 'service_tier': 'standard',
             }), \
             patch('claws.reconcile_cortex_key._cortex_proof', return_value=True), \
             patch('claws.reconcile_cortex_key.cortex_revoke_key', return_value=(False, 'fence failed')):
            binding, outcome = reconcile_claw_cortex_key(self.claw.id)
        self.assertEqual(outcome, 'retire_pending')
        self.assertEqual(binding.state, ClawCortexKeyBinding.State.RETIRE_PENDING)
        self.assertIn('revoke', binding.last_error)

    def test_retire_pending_retry_converges(self):
        """A later reconcile retries the exact revoke and reaches active."""
        core, apps = self._mock_k8s(secret=_secret_obj(raw_key='sk-old'))
        # First run: revoke fails -> retire_pending.
        with patch('claws.reconcile_cortex_key.get_k8s_client', return_value=(core, apps)), \
             patch('claws.reconcile_cortex_key.cortex_inspect_key', return_value={
                 'id': str(uuid.uuid4()), 'service_tier': 'internal',
             }), \
             patch('claws.reconcile_cortex_key.cortex_mint_key', return_value={
                 'id': str(uuid.uuid4()), 'key': 'sk-new', 'service_tier': 'standard',
             }), \
             patch('claws.reconcile_cortex_key._cortex_proof', return_value=True), \
             patch('claws.reconcile_cortex_key.cortex_revoke_key', return_value=(False, 'fence failed')):
            reconcile_claw_cortex_key(self.claw.id)
        binding = ClawCortexKeyBinding.objects.get(claw=self.claw)
        self.assertEqual(binding.state, ClawCortexKeyBinding.State.RETIRE_PENDING)
        # Second run: revoke succeeds -> active.
        with patch('claws.reconcile_cortex_key.get_k8s_client', return_value=(core, apps)), \
             patch('claws.reconcile_cortex_key.cortex_revoke_key', return_value=(True, 'revoked')):
            _, outcome = reconcile_claw_cortex_key(self.claw.id)
        binding.refresh_from_db()
        self.assertEqual(outcome, 'converged')
        self.assertEqual(binding.state, ClawCortexKeyBinding.State.ACTIVE)

    def test_idempotent_noop_after_active(self):
        """A second reconcile on an active binding is a noop."""
        core, apps = self._mock_k8s(secret=_secret_obj(raw_key='sk-compliant'))
        with patch('claws.reconcile_cortex_key.get_k8s_client', return_value=(core, apps)), \
             patch('claws.reconcile_cortex_key.cortex_inspect_key', return_value={
                 'id': str(uuid.uuid4()), 'service_tier': 'standard',
             }):
            reconcile_claw_cortex_key(self.claw.id)
        with patch('claws.reconcile_cortex_key.get_k8s_client', return_value=(core, apps)), \
             patch('claws.reconcile_cortex_key.cortex_mint_key') as mint:
            binding, outcome = reconcile_claw_cortex_key(self.claw.id)
        self.assertEqual(outcome, 'noop')
        self.assertEqual(binding.state, ClawCortexKeyBinding.State.ACTIVE)
        mint.assert_not_called()

    def test_second_top_level_reconcile_resumes_from_prepared(self):
        """Crash after mint (prepared, no CAS yet) resumes on the next call."""
        core, apps = self._mock_k8s(secret=_secret_obj(raw_key='sk-old'))
        with patch('claws.reconcile_cortex_key.get_k8s_client', return_value=(core, apps)), \
             patch('claws.reconcile_cortex_key.cortex_inspect_key', return_value={
                 'id': str(uuid.uuid4()), 'service_tier': 'internal',
             }), \
             patch('claws.reconcile_cortex_key.cortex_mint_key', return_value={
                 'id': str(uuid.uuid4()), 'key': 'sk-new', 'service_tier': 'standard',
             }), \
             patch('claws.reconcile_cortex_key._cortex_proof', return_value=True), \
             patch('claws.reconcile_cortex_key.cortex_revoke_key', return_value=(True, 'revoked')):
            # First call: force a crash after mint by making the CAS patch fail.
            with patch('claws.reconcile_cortex_key._cas_patch_secret', return_value=False):
                binding, outcome = reconcile_claw_cortex_key(self.claw.id)
            self.assertEqual(outcome, 'error')
            self.assertIn('CAS', binding.last_error)
            # Second call resumes from prepared (current artifact already set).
            binding, outcome = reconcile_claw_cortex_key(self.claw.id)
        self.assertEqual(outcome, 'converged')
        self.assertEqual(binding.state, ClawCortexKeyBinding.State.ACTIVE)

    def test_cortex_mint_key_sends_org_id_when_persisted(self):
        """HIVE-2233 fix: mint body must include org_id for STANDARD mints."""
        claw = self.claw  # reuse setUp claw (unique active (owner, tier, type, model))
        claw.org_id = 147
        claw.save()
        captured = {}

        def _fake_post(url, headers=None, json=None, timeout=None):
            captured['body'] = json
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {'id': str(uuid.uuid4()), 'key': 'sk-new', 'service_tier': 'standard'}
            return resp

        with override_settings(
            CORTEX_BASE_URL='https://cortex.test',
            HIVE_CORTEX_LEASE_API_KEY='Bearer svc-key',
        ), patch('claws.reconcile_cortex_key.requests.post', side_effect=_fake_post):
            result = cortex_mint_key(claw, uuid.uuid4())

        self.assertIsNotNone(result)
        self.assertEqual(captured['body']['org_id'], 147)
        self.assertEqual(captured['body']['user_id'], 275)

    def test_cortex_mint_key_omits_org_id_when_unset(self):
        """Legacy claws without org_id must not send a bogus org_id."""
        claw = self.claw  # setUp claw, org_id unset
        captured = {}

        def _fake_post(url, headers=None, json=None, timeout=None):
            captured['body'] = json
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {'id': str(uuid.uuid4()), 'key': 'sk-new', 'service_tier': 'standard'}
            return resp

        with override_settings(
            CORTEX_BASE_URL='https://cortex.test',
            HIVE_CORTEX_LEASE_API_KEY='Bearer svc-key',
        ), patch('claws.reconcile_cortex_key.requests.post', side_effect=_fake_post):
            result = cortex_mint_key(claw, uuid.uuid4())

        self.assertIsNotNone(result)
        self.assertNotIn('org_id', captured['body'])

    def test_wait_rollout_succeeds_on_zero_replica_idle_claw(self):
        """HIVE-2233 fix: 0-replica (idle) claw rollout completes on observedGeneration match."""
        from claws.reconcile_cortex_key import _wait_rollout
        apps = MagicMock()
        # spec.replicas = 0, observed == generation, available = 0
        apps.read_namespaced_deployment.return_value = _deploy_obj(
            generation=9, observed=9, available=0, replicas=0,
        )
        self.assertTrue(_wait_rollout(apps, self.claw, timeout=5))
