"""HIVE-1502 — an empty ``agent_id`` must never render an unstartable pod.

The runtime entrypoint guards ``${AGENT_ID:?AGENT_ID required}``, which fires on
**empty** as well as unset. Both render sites previously coerced the value
(``agent.agent_id or ''``), so a missing required input became a syntactically
VALID Deployment: provisioning reported success, ``kubectl apply`` succeeded, the
ReplicaSet scaled, and the only symptom was a crash loop nothing in the roll path
inspects.

Live cost of that (mio, HIVE-1502): the ``shizuha-116`` seat — one user's personal
assistant, and the fleet's only ``devops-ssh``-labelled host-plane SSH vantage —
sat unstartable for 4 days across 10 ReplicaSet revisions and 9 image rolls,
having never once produced a Ready agent container.

These tests assert the REFUSAL. A test that only exercised a populated
``agent_id`` would have passed on every one of those 4 days, which is the point:
the happy path was never broken.
"""
from unittest import mock

from django.test import TestCase

from fleet import provisioner
from fleet.models import FleetAgent


def _agent(**overrides):
    defaults = dict(
        agent_username='hive1502',
        agent_id='agent-uuid-123',
        display_name='HIVE-1502 seat',
        platform_url='http://platform',
    )
    defaults.update(overrides)
    return FleetAgent(**defaults)


class RenderRefusesEmptyAgentIdTests(TestCase):
    """The single render site is a hard invariant — no caller can bypass it."""

    def test_agent_container_refuses_empty_agent_id(self):
        with self.assertRaises(ValueError) as ctx:
            provisioner._agent_container(_agent(agent_id=''))
        self.assertIn('agent_id is empty', str(ctx.exception))
        self.assertIn('HIVE-1502', str(ctx.exception))

    def test_agent_container_refuses_whitespace_only_agent_id(self):
        """`${AGENT_ID:?}` fires on empty, and a whitespace-only value is empty
        for every purpose that matters — it must not slip through a truthiness
        check that `'   '` satisfies."""
        with self.assertRaises(ValueError):
            provisioner._agent_container(_agent(agent_id='   '))

    def test_agent_container_renders_agent_id_when_present(self):
        """The happy path must still work — and must emit the real value, not a
        coerced empty string."""
        container = provisioner._agent_container(_agent(agent_id='real-id-42'))
        env = {item.name: item.value for item in container.env}
        self.assertEqual(env['AGENT_ID'], 'real-id-42')


class ProvisionRefusesEmptyAgentIdTests(TestCase):
    """Provisioning blocks with a reason on the row, rather than rendering."""

    def test_provision_agent_blocks_and_explains(self):
        agent = FleetAgent.objects.create(
            agent_username='hive1502-provision', agent_id='',
            display_name='no id', platform_url='http://platform',
        )
        with mock.patch.object(provisioner, '_personal_runtime_isolation_gate', return_value=False), \
             mock.patch.object(provisioner, 'get_k8s_client') as k8s:
            result = provisioner.provision_agent(agent)

        self.assertFalse(result)
        # The k8s client must never be reached: refusing AFTER touching the
        # cluster would still leave a partially-provisioned seat.
        k8s.assert_not_called()

        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.BLOCKED)
        self.assertIn('agent_id is empty', agent.status_message)
        self.assertIn('HIVE-1502', agent.status_message)


class ImageRollRefusesEmptyAgentIdTests(TestCase):
    """The roll path is the one that actually recreated the broken pod."""

    def test_ensure_agent_image_refuses_and_records_reason(self):
        """`ensure_agent_image` is NOT reached through `provision_agent`; it is
        the image-roll path, and it is what re-rendered shizuha-116 nine times.
        Guarding only the provisioning entry point would have left this loop
        intact, so this test exists to prove the roll path refuses too."""
        agent = FleetAgent.objects.create(
            agent_username='hive1502-roll', agent_id='',
            display_name='no id', platform_url='http://platform',
        )
        with mock.patch.object(provisioner, 'get_k8s_client') as k8s:
            result = provisioner.ensure_agent_image(agent)

        self.assertFalse(result)
        k8s.assert_not_called()
        agent.refresh_from_db()
        self.assertIn('agent_id is empty', agent.status_message)

    def test_ensure_agent_image_proceeds_when_agent_id_present(self):
        """Negative control: the guard must not block a healthy seat. Without
        this, a guard that refused everything would pass every test above."""
        agent = FleetAgent.objects.create(
            agent_username='hive1502-ok', agent_id='present-id',
            display_name='ok', platform_url='http://platform',
        )
        agent._reviewed_runtime_image = 'registry/shizuha-agent-runtime:test-reviewed'
        with mock.patch.object(provisioner, 'get_k8s_client', side_effect=RuntimeError('reached k8s')):
            with self.assertRaisesMessage(RuntimeError, 'reached k8s'):
                provisioner.ensure_agent_image(agent)


class CreateSerializerMintsAgentIdTests(TestCase):
    """The creation boundary must never mint a row that HIVE-1502 then refuses.

    Nothing populates ``agent_id`` after creation, so a row created without one
    is blocked at every provision attempt forever. The MCP create tool (which
    has no ``agent_id`` input) hit exactly this on 2026-08-07: seven rows
    created, all refused with 'agent_id is empty'."""

    def test_create_serializer_mints_agent_id_when_absent(self):
        from fleet.serializers import FleetAgentCreateSerializer
        import uuid as _uuid
        ser = FleetAgentCreateSerializer(data={'agent_username': 'mintme'})
        self.assertTrue(ser.is_valid(), ser.errors)
        minted = ser.validated_data['agent_id']
        _uuid.UUID(minted)  # well-formed, non-empty

    def test_create_serializer_keeps_caller_agent_id(self):
        from fleet.serializers import FleetAgentCreateSerializer
        ser = FleetAgentCreateSerializer(
            data={'agent_username': 'keepme', 'agent_id': 'caller-chosen-id'},
        )
        self.assertTrue(ser.is_valid(), ser.errors)
        self.assertEqual(ser.validated_data['agent_id'], 'caller-chosen-id')


class IdentityFirstBindTests(TestCase):
    """identity_user_id/identity_profile_id: first bind allowed, change refused.

    The guard used to refuse NULL→value on any provisioned row, which meant the
    resolved identity could never persist — and the refused save left the
    in-memory instance poisoned so every LATER save on it raised too (the
    enable-path 500 of 2026-08-07, where start_agent's status write crashed)."""

    def _provisioned_agent(self, **overrides):
        from fleet.timeutils import naive_utc_now
        defaults = dict(
            agent_username='firstbind',
            agent_id='fb-id',
            display_name='First Bind',
            platform_url='http://platform',
            provisioned_at=naive_utc_now(),
        )
        defaults.update(overrides)
        return FleetAgent.objects.create(**defaults)

    def test_first_bind_of_identity_user_id_persists(self):
        agent = self._provisioned_agent()
        agent.identity_user_id = 42
        agent.save(update_fields=['identity_user_id'])
        agent.refresh_from_db()
        self.assertEqual(agent.identity_user_id, 42)

    def test_rebind_of_identity_user_id_is_refused(self):
        from django.core.exceptions import ValidationError
        agent = self._provisioned_agent(agent_username='firstbind2')
        agent.identity_user_id = 42
        agent.save(update_fields=['identity_user_id'])
        agent.identity_user_id = 43
        with self.assertRaises(ValidationError):
            agent.save(update_fields=['identity_user_id'])

    def test_refused_persist_does_not_poison_later_saves(self):
        """The cortex-key path restores the in-memory value on refusal so a
        subsequent unrelated save (e.g. start_agent's status write) succeeds."""
        from django.core.exceptions import ValidationError
        agent = self._provisioned_agent(agent_username='firstbind3')
        agent.identity_user_id = 42
        agent.save(update_fields=['identity_user_id'])
        agent.identity_user_id = 99
        with self.assertRaises(ValidationError):
            agent.save(update_fields=['identity_user_id'])
        agent.identity_user_id = 42  # what the restore does
        agent.status_message = 'still writable'
        agent.save(update_fields=['status_message'])
        agent.refresh_from_db()
        self.assertEqual(agent.status_message, 'still writable')
        self.assertEqual(agent.identity_user_id, 42)
