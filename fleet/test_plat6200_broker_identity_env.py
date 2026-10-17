"""PLAT-6200: broker env converges to the per-agent identity Secret (identity SoT).

Sara's broker stayed unready after a Hive enable/model bounce because two
identity secrets drifted: ``agent-sara-identity`` (what PLAT-4884 reads/writes,
33-char) vs ``sara-agent-creds`` (what the live Deployment injected as broker
``AGENT_PASSWORD``, 32-char). ``ensure_agent_image`` converged image/labels but
never the broker env wiring, so the stale template never healed.

``_converge_broker_identity_env`` rewrites a broker container to source identity
env (AGENT_PASSWORD / AGENT_USERNAME / AGENT_ID / ACCESS_TOKEN) from the
per-agent identity Secret and drops explicit refs to any other Secret.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase

from fleet.provisioner import (
    BROKER_IDENTITY_ENV_NAMES,
    _converge_broker_identity_env,
)


def _agent(username='sara'):
    return SimpleNamespace(agent_username=username, identity_secret_name='agent-sara-identity')


def _legacy_identity_env_var(name, secret='sara-agent-creds'):
    return SimpleNamespace(
        name=name,
        value_from=SimpleNamespace(secret_key_ref=SimpleNamespace(
            name=secret, key=name, optional=False,
        )),
    )


def _identity_env_var(name, secret='agent-sara-identity'):
    return SimpleNamespace(
        name=name,
        value_from=SimpleNamespace(secret_key_ref=SimpleNamespace(
            name=secret, key=name, optional=False,
        )),
    )


def _container(env=None, env_from=None):
    return SimpleNamespace(env=list(env or []), env_from=list(env_from or []))


class BrokerIdentityEnvConvergeTests(SimpleTestCase):
    def test_stale_creds_ref_is_rewritten_to_identity_secret(self):
        """Legacy explicit secretKeyRef on sara-agent-creds is dropped + envFrom added."""
        other = SimpleNamespace(name='MCP_AUTH_PROXY_ID_BASE_URL', value='https://id', value_from=None)
        container = _container(
            env=[other] + [_legacy_identity_env_var(n) for n in BROKER_IDENTITY_ENV_NAMES],
        )
        self.assertTrue(_converge_broker_identity_env(container, _agent()))
        # Stale identity env refs removed; unrelated env preserved.
        names = [e.name for e in container.env]
        self.assertIn('MCP_AUTH_PROXY_ID_BASE_URL', names)
        for n in BROKER_IDENTITY_ENV_NAMES:
            self.assertNotIn(n, names)
        # envFrom now references the identity Secret (SoT).
        self.assertTrue(any(
            getattr(getattr(s, 'secret_ref', None), 'name', None) == 'agent-sara-identity'
            for s in container.env_from
        ))

    def test_identity_secret_env_is_not_drift(self):
        """Explicit refs on the identity Secret + envFrom present → no change."""
        container = _container(
            env=[_identity_env_var(n) for n in BROKER_IDENTITY_ENV_NAMES],
            env_from=[SimpleNamespace(secret_ref=SimpleNamespace(name='agent-sara-identity'))],
        )
        self.assertFalse(_converge_broker_identity_env(container, _agent()))

    def test_missing_envfrom_adds_identity_secret(self):
        """Correct explicit refs but no envFrom → envFrom is added (still converges)."""
        container = _container(
            env=[_identity_env_var(n) for n in BROKER_IDENTITY_ENV_NAMES],
        )
        self.assertTrue(_converge_broker_identity_env(container, _agent()))
        self.assertTrue(any(
            getattr(getattr(s, 'secret_ref', None), 'name', None) == 'agent-sara-identity'
            for s in container.env_from
        ))

    def test_empty_env_adds_envfrom(self):
        """Broker with no env at all still gets the identity envFrom (SoT)."""
        container = _container(env=[])
        self.assertTrue(_converge_broker_identity_env(container, _agent()))
        self.assertTrue(any(
            getattr(getattr(s, 'secret_ref', None), 'name', None) == 'agent-sara-identity'
            for s in container.env_from
        ))

    def test_non_identity_env_is_preserved(self):
        """Unrelated broker env (coordinator URL etc.) survives convergence."""
        other = SimpleNamespace(name='MCP_AUTH_PROXY_COORDINATOR_URL', value='https://coord', value_from=None)
        container = _container(
            env=[other, _legacy_identity_env_var('AGENT_PASSWORD')],
        )
        self.assertTrue(_converge_broker_identity_env(container, _agent()))
        names = [e.name for e in container.env]
        self.assertIn('MCP_AUTH_PROXY_COORDINATOR_URL', names)
        self.assertNotIn('AGENT_PASSWORD', names)
