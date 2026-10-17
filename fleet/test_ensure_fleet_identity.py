"""Identity must be bindable even when rt-fleet image projection is down."""
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from fleet import provisioner
from fleet.models import FleetAgent
from fleet.provisioner import FleetIdentityBinding


def _binding(user_id=77, profile_id=88):
    return FleetIdentityBinding(
        user_id=user_id,
        profile_id=profile_id,
        owner_user_id=None,
        owner_profile_id=None,
        ownership_class='',
    )


class EnsureFleetIdentityTests(TestCase):
    def setUp(self):
        self.agent = FleetAgent.objects.create(
            agent_username='hina',
            agent_id='hina-runtime-id',
            display_name='Hina',
            status=FleetAgent.Status.PENDING,
        )

    def test_ensure_persists_id_user_without_k8s(self):
        with mock.patch.object(
            provisioner, '_provision_platform_identity_details', return_value=_binding(),
        ), mock.patch('fleet.personal_agents.resolve_agent_user_id', return_value=None):
            ok = provisioner.ensure_fleet_identity(self.agent)
        self.assertTrue(ok)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.identity_user_id, 77)
        self.assertEqual(self.agent.identity_profile_id, 88)
        self.assertIsNotNone(self.agent.identity_provisioned_at)

    def test_enable_pending_never_provisioned_calls_provision(self):
        with mock.patch.object(provisioner, 'ensure_fleet_identity', return_value=True), \
                mock.patch.object(provisioner, 'provision_agent', return_value=True) as provision, \
                mock.patch.object(provisioner, 'start_agent') as start:
            ok = provisioner.enable_or_provision_agent(self.agent)
        self.assertTrue(ok)
        provision.assert_called_once_with(self.agent)
        start.assert_not_called()

    def test_enable_stopped_agent_starts_without_reprovision(self):
        from fleet.timeutils import naive_utc_now
        self.agent.status = FleetAgent.Status.STOPPED
        self.agent.identity_user_id = 9
        self.agent.provisioned_at = naive_utc_now()
        self.agent.save(update_fields=['status', 'identity_user_id', 'provisioned_at'])
        with mock.patch.object(provisioner, 'provision_agent') as provision, \
                mock.patch.object(provisioner, 'start_agent', return_value=True) as start:
            ok = provisioner.enable_or_provision_agent(self.agent)
        self.assertTrue(ok)
        provision.assert_not_called()
        start.assert_called_once_with(self.agent)

    def test_enable_hibernated_never_provisioned_calls_provision(self):
        """Default personal agents park hibernated with no Deployment. First
        enable/connect-wake must provision, not scale a missing object."""
        self.agent.status = FleetAgent.Status.HIBERNATED
        self.agent.hibernation_reason = 'default_until_first_use'
        self.agent.identity_user_id = 9
        self.agent.provisioned_at = None
        self.agent.save(update_fields=[
            'status', 'hibernation_reason', 'identity_user_id', 'provisioned_at',
        ])
        with mock.patch.object(provisioner, 'provision_agent', return_value=True) as provision, \
                mock.patch.object(provisioner, 'start_agent') as start:
            ok = provisioner.enable_or_provision_agent(self.agent)
        self.assertTrue(ok)
        provision.assert_called_once_with(self.agent)
        start.assert_not_called()

    def test_enable_mints_missing_agent_id_before_provision(self):
        self.agent.agent_id = ''
        self.agent.identity_user_id = 9
        self.agent.save(update_fields=['agent_id', 'identity_user_id'])
        with mock.patch.object(provisioner, 'provision_agent', return_value=True) as provision:
            ok = provisioner.enable_or_provision_agent(self.agent)
        self.assertTrue(ok)
        provision.assert_called_once()
        self.agent.refresh_from_db()
        self.assertTrue(str(self.agent.agent_id or '').strip())

    def test_bind_existing_does_not_mint_a_password(self):
        with mock.patch('fleet.personal_agents.resolve_agent_user_id', return_value=55), \
                mock.patch.object(provisioner, '_generate_agent_password') as gen, \
                mock.patch.object(provisioner, '_provision_platform_identity_details') as details:
            ok = provisioner.bind_existing_identity_user(self.agent)
        self.assertTrue(ok)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.identity_user_id, 55)
        gen.assert_not_called()
        details.assert_not_called()

    def test_persist_refuses_to_overwrite_a_different_user_id(self):
        self.agent.identity_user_id = 11
        self.agent.save(update_fields=['identity_user_id'])
        ok = provisioner.persist_identity_binding(self.agent, _binding(user_id=99))
        self.assertFalse(ok)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.identity_user_id, 11)
