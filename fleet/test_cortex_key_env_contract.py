"""Per-agent Cortex credential env contract convergence (operator 2026-08-06).

    whoever is using cortex must use their own cortex account .. a proper
    shizuha id user just like the other ones

Ten pre-fix Deployments still wired CORTEX_API_KEY from the shared
``cortex-fleet-key`` secret (optional: true — the pre-rei shape). Image rolls
converged image/resources but never env refs, so the moment kumo/kane woke
they billed 4.6M grok tokens onto the shared ``agents-fleet-own-fleet``
principal. ensure_agent_image now converges the credential contract, and the
legacy ref counts as runtime-contract drift so the roller heals it.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase

from fleet.provisioner import (
    CORTEX_KEY_ENV_NAMES,
    _converge_cortex_key_env,
    _cortex_key_env_ref_drifted,
    _per_agent_cortex_key_env,
)


def _agent(username='kumo'):
    return SimpleNamespace(agent_username=username)


def _legacy_env_var(name):
    return SimpleNamespace(
        name=name,
        value_from=SimpleNamespace(secret_key_ref=SimpleNamespace(
            name='cortex-fleet-key', key='OPENAI_API_KEY', optional=True,
        )),
    )


def _contract_env_var(name, username='kumo'):
    return SimpleNamespace(
        name=name,
        value_from=SimpleNamespace(secret_key_ref=SimpleNamespace(
            name=f'{username}-agent-creds', key='CORTEX_API_KEY', optional=False,
        )),
    )


class CortexKeyEnvContractTests(SimpleTestCase):
    def test_legacy_shared_key_ref_is_drift(self):
        container = SimpleNamespace(env=[_legacy_env_var(n) for n in CORTEX_KEY_ENV_NAMES])
        self.assertTrue(_cortex_key_env_ref_drifted(container, _agent()))

    def test_missing_refs_are_drift(self):
        self.assertTrue(_cortex_key_env_ref_drifted(SimpleNamespace(env=[]), _agent()))

    def test_optional_true_on_the_right_secret_is_still_drift(self):
        """optional: true is the rei failure shape — empty string, 401 forever."""
        var = _contract_env_var('CORTEX_API_KEY')
        var.value_from.secret_key_ref.optional = True
        container = SimpleNamespace(
            env=[_contract_env_var(n) for n in ('OPENAI_API_KEY', 'VLLM_API_KEY')] + [var],
        )
        self.assertTrue(_cortex_key_env_ref_drifted(container, _agent()))

    def test_contract_env_is_not_drift(self):
        container = SimpleNamespace(env=[_contract_env_var(n) for n in CORTEX_KEY_ENV_NAMES])
        self.assertFalse(_cortex_key_env_ref_drifted(container, _agent()))

    def test_converge_rewrites_legacy_refs_and_keeps_other_env(self):
        other = SimpleNamespace(name='MODEL', value='cortex/grok-4.5', value_from=None)
        container = SimpleNamespace(
            env=[other] + [_legacy_env_var(n) for n in CORTEX_KEY_ENV_NAMES],
        )
        self.assertTrue(_converge_cortex_key_env(container, _agent()))
        self.assertFalse(_cortex_key_env_ref_drifted(container, _agent()))
        names = [e.name for e in container.env]
        self.assertIn('MODEL', names)
        refs = {
            e.name: e.value_from.secret_key_ref
            for e in container.env if e.name in CORTEX_KEY_ENV_NAMES
        }
        for name in CORTEX_KEY_ENV_NAMES:
            self.assertEqual(refs[name].name, 'kumo-agent-creds')
            self.assertEqual(refs[name].key, 'CORTEX_API_KEY')
            self.assertFalse(refs[name].optional)

    def test_converge_is_idempotent(self):
        container = SimpleNamespace(env=[_contract_env_var(n) for n in CORTEX_KEY_ENV_NAMES])
        self.assertFalse(_converge_cortex_key_env(container, _agent()))

    def test_full_render_uses_the_same_contract(self):
        for var in _per_agent_cortex_key_env(_agent('rei')):
            ref = var.value_from.secret_key_ref
            self.assertEqual(ref.name, 'rei-agent-creds')
            self.assertEqual(ref.key, 'CORTEX_API_KEY')
            self.assertFalse(ref.optional)


class LaneEnvDriftTests(SimpleTestCase):
    """Hive's lane env must be obeyed EXACTLY — including absence.

    Operator 2026-08-06: "hive should sync those changes to the agent asap ...
    permanently ensuring that hive's truth is obeyed". A retired
    MODEL_FALLBACKS chain survived in kumo's live template because the
    strategic-merge patch could not delete it AND nothing classified the
    leftover as drift, so no reconcile pass ever revisited it.
    """

    def _container(self, env_pairs):
        return SimpleNamespace(env=[
            SimpleNamespace(name=k, value=v, value_from=None) for k, v in env_pairs
        ])

    def _agent_with_lane(self, lane_env):
        """Stub the render so the test pins the COMPARISON, not the renderer."""
        from unittest import mock
        from fleet import provisioner as P
        desired = SimpleNamespace(env=[
            SimpleNamespace(name=k, value=v, value_from=None) for k, v in lane_env
        ])
        return mock.patch.object(P, '_agent_container', return_value=desired)

    def test_leftover_retired_key_is_drift(self):
        from fleet.provisioner import _lane_env_drifted
        agent = SimpleNamespace(agent_username='kumo', sidecar_set=['broker', 'dind'])
        container = self._container([
            ('MODEL', 'cortex/grok-4.5'),
            ('MODEL_FALLBACKS', '[{"model":"cortex/DeepSeek-V4-Flash"}]'),
        ])
        with self._agent_with_lane([('MODEL', 'cortex/grok-4.5')]):
            self.assertTrue(_lane_env_drifted(container, agent))

    def test_changed_model_is_drift(self):
        from fleet.provisioner import _lane_env_drifted
        agent = SimpleNamespace(agent_username='kumo', sidecar_set=[])
        container = self._container([('MODEL', 'cortex/old-model')])
        with self._agent_with_lane([('MODEL', 'cortex/grok-4.5')]):
            self.assertTrue(_lane_env_drifted(container, agent))

    def test_matching_lane_env_is_not_drift(self):
        from fleet.provisioner import _lane_env_drifted
        agent = SimpleNamespace(agent_username='kumo', sidecar_set=[])
        container = self._container([
            ('MODEL', 'cortex/grok-4.5'), ('REASONING_EFFORT', 'high'),
            ('UNRELATED', 'keep-me'),
        ])
        with self._agent_with_lane([
            ('MODEL', 'cortex/grok-4.5'), ('REASONING_EFFORT', 'high'),
        ]):
            self.assertFalse(_lane_env_drifted(container, agent))

    def test_render_failure_is_treated_as_drift(self):
        """Never assert health from a failed render."""
        from unittest import mock
        from fleet import provisioner as P
        agent = SimpleNamespace(agent_username='kumo', sidecar_set=[])
        with mock.patch.object(P, '_agent_container', side_effect=RuntimeError('boom')):
            self.assertTrue(P._lane_env_drifted(self._container([]), agent))
