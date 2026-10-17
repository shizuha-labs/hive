"""HIVE-1750 — the identity Secret site must refuse an empty ``agent_id``.

HIVE-1502 fixed the container-render site to refuse an empty ``agent_id``, but
``_create_identity_secret()`` still coerced it with ``agent.agent_id or ''``.
The Secret site is "safe" today only because a single caller sits downstream of
the HIVE-1502 guard — a call-graph contingency, which is exactly the protection
that failed for the container path before a second caller appeared. A call-graph
fact is not an invariant; it is a fact that holds until it doesn't.

These tests assert the REFUSAL at the Secret site, both polarities:
- an empty ``agent_id`` raises and creates no Secret;
- a populated one still creates the Secret unchanged with the real value.
"""
from unittest import mock

from django.test import TestCase

from fleet import provisioner
from fleet.models import FleetAgent


def _agent(**overrides):
    defaults = dict(
        agent_username='hive1750',
        agent_id='agent-uuid-1750',
        display_name='HIVE-1750 seat',
        platform_url='http://platform',
    )
    defaults.update(overrides)
    return FleetAgent(**defaults)


class IdentitySecretRefusesEmptyAgentIdTests(TestCase):
    def test_empty_agent_id_raises_and_creates_no_secret(self):
        core_v1 = mock.MagicMock()
        with self.assertRaises(ValueError) as ctx:
            provisioner._create_identity_secret(
                core_v1, 'fleet', _agent(agent_id=''), 'pw', 'tok'
            )
        self.assertIn('agent_id is empty', str(ctx.exception))
        self.assertIn('HIVE-1502', str(ctx.exception))
        # The cluster must never be touched with a coerced empty id.
        core_v1.create_namespaced_secret.assert_not_called()
        core_v1.replace_namespaced_secret.assert_not_called()

    def test_whitespace_only_agent_id_is_refused(self):
        """A whitespace-only value is empty for every purpose that matters — it
        must not slip through a truthiness check that `'   '` satisfies."""
        core_v1 = mock.MagicMock()
        with self.assertRaises(ValueError):
            provisioner._create_identity_secret(
                core_v1, 'fleet', _agent(agent_id='   '), 'pw', 'tok'
            )
        core_v1.create_namespaced_secret.assert_not_called()

    def test_populated_agent_id_creates_secret_unchanged(self):
        """The happy path must still work — and must emit the real value, not a
        coerced empty string."""
        core_v1 = mock.MagicMock()
        provisioner._create_identity_secret(
            core_v1, 'fleet', _agent(agent_id='real-id-1750'), 'pw', 'tok'
        )
        core_v1.create_namespaced_secret.assert_called_once()
        secret = core_v1.create_namespaced_secret.call_args.args[1]
        self.assertEqual(secret.string_data['AGENT_ID'], 'real-id-1750')
        self.assertEqual(secret.string_data['AGENT_PASSWORD'], 'pw')
        self.assertEqual(secret.string_data['ACCESS_TOKEN'], 'tok')
