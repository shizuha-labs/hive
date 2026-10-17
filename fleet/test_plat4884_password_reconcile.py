"""PLAT-4884: start/enable password drift reconcile — typed probe + one reconcile."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from fleet import provisioner
from fleet.models import FleetAgent
from fleet.provider_resolution import (
    REASON_NO_PROVIDER_CONFIGURED, Resolution,
)


class ClassifyLoginResponseTests(SimpleTestCase):
    def test_200_is_ok(self):
        resp = MagicMock(status_code=200)
        self.assertEqual(provisioner._classify_id_login_response(resp), 'ok')

    def test_401_invalid_credentials_is_drift(self):
        resp = MagicMock(status_code=401)
        resp.json.return_value = {'error': 'Invalid credentials'}
        self.assertEqual(
            provisioner._classify_id_login_response(resp), 'invalid_credentials',
        )

    def test_401_other_body_is_indeterminate(self):
        resp = MagicMock(status_code=401)
        resp.json.return_value = {'detail': 'token expired'}
        self.assertEqual(
            provisioner._classify_id_login_response(resp), 'indeterminate',
        )

    def test_429_is_indeterminate(self):
        resp = MagicMock(status_code=429)
        self.assertEqual(
            provisioner._classify_id_login_response(resp), 'indeterminate',
        )

    def test_403_is_indeterminate(self):
        resp = MagicMock(status_code=403)
        self.assertEqual(
            provisioner._classify_id_login_response(resp), 'indeterminate',
        )

    def test_500_is_indeterminate(self):
        resp = MagicMock(status_code=500)
        self.assertEqual(
            provisioner._classify_id_login_response(resp), 'indeterminate',
        )


class _NoByoProviderGateMixin:
    """The HIVE-2161 S3-lite provider gate consults the DB (roster adjacency +
    org provider references) inside _lease_start_decision, which SimpleTestCase
    forbids. These flows do not exercise BYO resolution — pin the gate boundary
    to the non-BYO outcome so the flows proceed exactly as they did before the
    gate landed."""

    def setUp(self):
        super().setUp()
        gate_patch = patch(
            'fleet.provider_resolution.resolve_agent_org_provider',
            return_value=Resolution(
                ok=False, reason=REASON_NO_PROVIDER_CONFIGURED,
            ),
        )
        gate_patch.start()
        self.addCleanup(gate_patch.stop)


@override_settings(SHIZUHA_PLATFORM_URL='https://shizuha.com')
class EnsureAlignedTests(_NoByoProviderGateMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()  # starts the _NoByoProviderGateMixin gate patch
        provisioner._drift_hold_emitted.clear()
        # Use real model defaults for the lifecycle caller. Password tests
        # reach their own gate only after the model lease has been confirmed.
        self.agent = FleetAgent(
            agent_username='ren',
            display_name='Ren',
            owner_id=None,
            agent_id='ren-id',
            namespace='ns-ren',
            image='',
            model='cortex/GLM-5.3-Flash',
        )
        self.agent._reviewed_runtime_image = 'registry/shizuha-agent-runtime:test-reviewed'
        self.agent.save = MagicMock()
        self.enterContext(patch('fleet.model_lease.all_pool_configs', return_value=[{
            'model_id': 'GLM-5.3-Flash', 'enabled': True, 'max_holders': 1,
        }]))
        self.board = self.enterContext(patch('fleet.model_lease.CortexLeaseClient.board', return_value={
            'model': 'GLM-5.3-Flash', 'leased': True, 'max_holders': 1,
            'holders': [{'principal': 'agent-ren', 'lease_id': 'confirmed-test-lease'}],
        }))

    def _secret(self, password=b'secret-pw!A1', rv='42'):
        import base64
        snap = MagicMock()
        snap.data = {
            'AGENT_PASSWORD': base64.b64encode(password).decode(),
        }
        snap.metadata.resource_version = rv
        return snap

    @patch('fleet.provisioner._provision_platform_identity')
    @patch('fleet.provisioner._probe_id_login', return_value='ok')
    @patch('fleet.provisioner._secret_data_text', return_value='secret-pw!A1')
    @patch('fleet.provisioner._read_identity_secret_snapshot')
    @patch('fleet.provisioner.get_k8s_client')
    def test_ok_when_login_succeeds(self, k8s, read_sec, _text, probe, provision):
        k8s.return_value = (MagicMock(), MagicMock(), MagicMock())
        read_sec.return_value = self._secret()
        self.assertEqual(provisioner.ensure_agent_id_password_aligned(self.agent), 'ok')
        provision.assert_not_called()
        self.assertEqual(probe.call_count, 1)

    @patch('fleet.provisioner._provision_platform_identity', return_value=True)
    @patch('fleet.provisioner._probe_id_login')
    @patch('fleet.provisioner._secret_data_text', return_value='secret-pw!A1')
    @patch('fleet.provisioner._read_identity_secret_snapshot')
    @patch('fleet.provisioner.get_k8s_client')
    def test_reconcile_on_exact_401_then_verify_ok(
        self, k8s, read_sec, _text, probe, provision,
    ):
        k8s.return_value = (MagicMock(), MagicMock(), MagicMock())
        read_sec.return_value = self._secret()
        probe.side_effect = ['invalid_credentials', 'ok']
        self.assertEqual(
            provisioner.ensure_agent_id_password_aligned(self.agent), 'reconciled',
        )
        provision.assert_called_once()
        self.assertEqual(probe.call_count, 2)

    @patch('fleet.provisioner._provision_platform_identity')
    @patch('fleet.provisioner._probe_id_login', return_value='indeterminate')
    @patch('fleet.provisioner._secret_data_text', return_value='secret-pw!A1')
    @patch('fleet.provisioner._read_identity_secret_snapshot')
    @patch('fleet.provisioner.get_k8s_client')
    def test_timeout_or_5xx_zero_mutation(self, k8s, read_sec, _text, probe, provision):
        k8s.return_value = (MagicMock(), MagicMock(), MagicMock())
        read_sec.return_value = self._secret()
        self.assertEqual(
            provisioner.ensure_agent_id_password_aligned(self.agent), 'skipped',
        )
        provision.assert_not_called()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner._scale', return_value=True)
    @patch('fleet.provisioner.ensure_agent_image', return_value=True)
    @patch('fleet.provisioner.ensure_agent_id_password_aligned', return_value='hold')
    @patch('fleet.provisioner._personal_runtime_isolation_gate', return_value=False)
    def test_start_agent_refuses_on_drift_hold(
        self, _gate, align, _img, scale, _log,
    ):
        agent = self.agent
        agent.status = 'stopped'
        ok = provisioner.start_agent(agent)
        self.assertFalse(ok)
        self.board.assert_called_once_with('GLM-5.3-Flash')
        align.assert_called_once_with(agent)
        _img.assert_not_called()
        scale.assert_not_called()
        self.assertIn('PLAT-4884', agent.status_message)

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner._scale', return_value=True)
    @patch('fleet.provisioner.ensure_agent_image', return_value=True)
    @patch('fleet.provisioner.ensure_agent_id_password_aligned', return_value='reconciled')
    @patch('fleet.provisioner._personal_runtime_isolation_gate', return_value=False)
    def test_start_agent_scales_after_reconcile(
        self, _gate, align, _img, scale, _log,
    ):
        agent = self.agent
        agent.status = 'stopped'
        ok = provisioner.start_agent(agent)
        self.assertTrue(ok)
        self.board.assert_called_once_with('GLM-5.3-Flash')
        align.assert_called_once_with(agent)
        _img.assert_called_once_with(agent)
        scale.assert_called_once_with(agent, 1)

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner._scale', return_value=True)
    @patch('fleet.provisioner.ensure_agent_image', return_value=True)
    @patch('fleet.provisioner.ensure_agent_id_password_aligned', return_value='reconciled')
    @patch('fleet.provisioner._personal_runtime_isolation_gate', return_value=False)
    def test_start_agent_requires_lease_before_password_reconcile(
        self, _gate, align, image, scale, _log,
    ):
        self.board.return_value['holders'] = []
        self.agent.status = 'stopped'
        self.assertFalse(provisioner.start_agent(self.agent))
        self.board.assert_called_once_with('GLM-5.3-Flash')
        align.assert_not_called()
        image.assert_not_called()
        scale.assert_not_called()
        self.assertEqual(provisioner.lease_start_pending_reason(self.agent), 'await_reconcile_grant')
