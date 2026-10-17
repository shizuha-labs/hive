from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .models import FleetAgent
from .personal_agents import PersonalShizuhaProvisionError, ensure_personal_shizuha_agent
from . import provisioner
from .provisioner import FleetIdentityBinding
from .views import FleetAgentListView


def _binding(owner_id, *, identity_user_id=None, identity_profile_id=None, owner_profile_id=None):
    return FleetIdentityBinding(
        user_id=identity_user_id or (10_000 + owner_id),
        profile_id=identity_profile_id or (20_000 + owner_id),
        owner_user_id=owner_id,
        owner_profile_id=owner_profile_id or (30_000 + owner_id),
        ownership_class='personal_default_member',
    )


@override_settings(SHIZUHA_PLATFORM_URL='https://shizuha.test')
class PersonalShizuhaOwnerTests(TestCase):
    def test_legacy_fleet_agent_owner_defaults_null(self):
        agent = FleetAgent.objects.create(agent_username='legacy')
        self.assertIsNone(agent.owner_id)
        self.assertEqual(agent.owner_username, '')

    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    @patch('fleet.personal_agents.provisioner.provision_agent', return_value=True)
    @patch('fleet.personal_agents._generate_agent_password', return_value='generated!A1')
    @patch('fleet.personal_agents._provision_platform_identity_details', return_value=_binding(42))
    def test_ensure_creates_one_isolated_hibernated_agent_and_identity(self, provision, _password, runtime, wire):
        first = ensure_personal_shizuha_agent(owner_id=42, owner_username='alice')
        second = ensure_personal_shizuha_agent(owner_id=42, owner_username='changed-handle')

        self.assertTrue(first.created)
        self.assertTrue(first.identity_created)
        self.assertFalse(second.created)
        self.assertFalse(second.identity_created)
        self.assertEqual(FleetAgent.objects.filter(owner_id=42).count(), 1)
        agent = second.agent
        self.assertEqual(agent.agent_username, 'shizuha-42')
        self.assertEqual(agent.display_name, 'Shizuha')
        self.assertTrue(agent.isolated)
        self.assertFalse(agent.sandbox_required)
        self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)
        self.assertEqual(agent.hibernation_reason, 'default_until_first_use')
        self.assertTrue(str(agent.agent_id or '').strip())
        self.assertIsNotNone(agent.identity_provisioned_at)
        self.assertEqual(agent.owner_profile_id, 30042)
        self.assertEqual(agent.identity_user_id, 10042)
        self.assertEqual(agent.identity_profile_id, 20042)
        self.assertIn('grok-voice', agent.model)
        self.assertEqual((agent.mcp_config or {}).get('execution_method'), 'shizuha')
        skills = (agent.skill_config or {}).get('skills') or []
        eager = (agent.skill_config or {}).get('eager_skills') or []
        self.assertIn('heartbeat-protocol', skills)
        self.assertIn('heartbeat-protocol', eager)
        runtime.assert_not_called()
        wire.assert_called()

    @override_settings(PERSONAL_AGENT_MODEL='cortex/grok-voice-think-fast-2.0')
    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    def test_ensure_aligns_unused_factory_codex_seat_without_starting(self, _wire):
        agent = FleetAgent.objects.create(
            agent_username='shizuha-279', owner_id=279, isolated=True,
            display_name='Shizuha', model='gpt-5.5',
            model_overrides={'codex_app_server': 'gpt-5.5'},
            mcp_config={'execution_method': 'codex_app_server', 'runtime_environment': 'k8s'},
            owner_profile_id=300279, identity_user_id=752, identity_profile_id=747,
            identity_provisioned_at=timezone.now(),
            status=FleetAgent.Status.HIBERNATED,
            hibernation_reason='default_until_first_use',
        )
        with patch('fleet.personal_agents.provisioner.provision_agent') as runtime, \
             patch('fleet.personal_agents.provisioner.start_agent') as start:
            result = ensure_personal_shizuha_agent(owner_id=279, owner_username='mihirgates')
        result.agent.refresh_from_db()
        self.assertEqual(result.agent.pk, agent.pk)
        self.assertEqual(result.agent.model, 'cortex/grok-voice-think-fast-2.0')
        self.assertEqual((result.agent.mcp_config or {}).get('execution_method'), 'shizuha')
        self.assertEqual(result.agent.model_overrides, {})
        self.assertEqual(result.agent.status, FleetAgent.Status.HIBERNATED)
        self.assertEqual(result.agent.hibernation_reason, 'default_until_first_use')
        runtime.assert_not_called()
        start.assert_not_called()

    @override_settings(PERSONAL_AGENT_MODEL='cortex/grok-voice-think-fast-2.0')
    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    def test_ensure_does_not_overwrite_a_custom_used_personal_model(self, _wire):
        agent = FleetAgent.objects.create(
            agent_username='shizuha-280', owner_id=280, isolated=True,
            display_name='Shizuha', model='cortex/grok-4.6',
            mcp_config={'execution_method': 'shizuha', 'runtime_environment': 'k8s'},
            owner_profile_id=300280, identity_user_id=753, identity_profile_id=748,
            identity_provisioned_at=timezone.now(),
            status=FleetAgent.Status.STOPPED,
            last_active_at=timezone.now(),
        )
        result = ensure_personal_shizuha_agent(owner_id=280, owner_username='custom')
        result.agent.refresh_from_db()
        self.assertEqual(result.agent.pk, agent.pk)
        self.assertEqual(result.agent.model, 'cortex/grok-4.7')

    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    def test_ensure_floors_heartbeat_protocol_on_existing_personal_seed(self, _wire):
        agent = FleetAgent.objects.create(
            agent_username='shizuha-281', owner_id=281, isolated=True,
            display_name='Shizuha', model='cortex/grok-voice-think-fast-2.0',
            mcp_config={'execution_method': 'shizuha', 'runtime_environment': 'k8s'},
            skill_config={'skills': ['connect-messaging', 'personal-assistant'], 'eager_skills': []},
            owner_profile_id=300281, identity_user_id=754, identity_profile_id=749,
            identity_provisioned_at=timezone.now(),
            status=FleetAgent.Status.HIBERNATED,
            hibernation_reason='default_until_first_use',
        )
        result = ensure_personal_shizuha_agent(owner_id=281, owner_username='seeded')
        result.agent.refresh_from_db()
        self.assertEqual(result.agent.pk, agent.pk)
        skills = (result.agent.skill_config or {}).get('skills') or []
        eager = (result.agent.skill_config or {}).get('eager_skills') or []
        self.assertIn('heartbeat-protocol', skills)
        self.assertIn('heartbeat-protocol', eager)
        self.assertIn('personal-assistant', skills)

    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    @patch('fleet.agent_index_cache.invalidate')
    @patch('fleet.personal_agents.provisioner.provision_agent', return_value=True)
    @patch('fleet.personal_agents._generate_agent_password', return_value='generated!A1')
    @patch('fleet.personal_agents._provision_platform_identity_details', return_value=_binding(44))
    def test_ensure_invalidates_agents_index_so_owner_list_sees_the_row(
        self, _identity, _password, _runtime, invalidate, _wire,
    ):
        ensure_personal_shizuha_agent(owner_id=44, owner_username='owner44')
        invalidate.assert_called()

    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    @patch('fleet.personal_agents._provision_platform_identity_details')
    def test_two_owners_never_share_row_or_username(self, provision, _wire):
        provision.side_effect = lambda agent, *_args: _binding(agent.owner_id)
        a = ensure_personal_shizuha_agent(owner_id=7, owner_username='a').agent
        b = ensure_personal_shizuha_agent(owner_id=8, owner_username='b').agent
        self.assertNotEqual(a.pk, b.pk)
        self.assertEqual({a.agent_username, b.agent_username}, {'shizuha-7', 'shizuha-8'})

    def test_owner_is_immutable_after_identity_provisioning(self):
        agent = FleetAgent.objects.create(
            agent_username='shizuha-9', owner_id=9, isolated=True,
            owner_profile_id=30009, identity_user_id=10009, identity_profile_id=20009,
            identity_provisioned_at=timezone.now(), status=FleetAgent.Status.STOPPED,
        )
        agent.owner_id = 10
        with self.assertRaisesRegex(ValidationError, 'owner_id is immutable'):
            agent.save()

    @patch('fleet.personal_agents._provision_platform_identity_details')
    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    def test_ensure_does_not_rebind_when_owner_profile_id_missing(self, _wire, identity):
        """ID sometimes omits owner_profile_id. That must not remint a password
        on every Hive page load (the completeness gate used to require it)."""
        agent = FleetAgent.objects.create(
            agent_username='shizuha-90', owner_id=90, isolated=True,
            owner_profile_id=None, identity_user_id=10090, identity_profile_id=20090,
            identity_provisioned_at=timezone.now(),
            status=FleetAgent.Status.HIBERNATED,
            hibernation_reason='default_until_first_use',
        )
        result = ensure_personal_shizuha_agent(owner_id=90, owner_username='owner90')
        self.assertEqual(result.agent.pk, agent.pk)
        identity.assert_not_called()

    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    def test_ensure_mints_missing_agent_id_on_existing_row(self, _wire):
        agent = FleetAgent.objects.create(
            agent_username='shizuha-88', owner_id=88, isolated=True, agent_id='',
            owner_profile_id=30088, identity_user_id=10088, identity_profile_id=20088,
            identity_provisioned_at=timezone.now(),
            status=FleetAgent.Status.HIBERNATED,
            hibernation_reason='default_until_first_use',
        )
        result = ensure_personal_shizuha_agent(owner_id=88, owner_username='owner88')
        result.agent.refresh_from_db()
        self.assertEqual(result.agent.pk, agent.pk)
        self.assertTrue(str(result.agent.agent_id or '').strip())

    def test_owner_who_already_has_agents_does_not_get_a_default(self):
        FleetAgent.objects.create(
            agent_username='hina', owner_id=3, status=FleetAgent.Status.RUNNING)
        with self.assertRaises(PersonalShizuhaProvisionError):
            ensure_personal_shizuha_agent(owner_id=3, owner_username='hritik')
        self.assertFalse(
            FleetAgent.objects.filter(agent_username='shizuha-3').exists())

    @patch('fleet.personal_agents.provisioner.hibernate_agent', return_value=True)
    def test_park_scales_existing_namespace_to_zero(self, hibernate):
        from fleet.personal_agents import _park_default_hibernated
        agent = FleetAgent.objects.create(
            agent_username='shizuha-115', owner_id=115, isolated=True,
            namespace='agent-shizuha-115', status=FleetAgent.Status.RUNNING,
            hibernation_reason='default_until_first_use',
        )
        hibernate.side_effect = lambda a, reason='': (
            FleetAgent.objects.filter(pk=a.pk).update(
                status=FleetAgent.Status.HIBERNATED,
                hibernation_reason=reason,
            ) or True
        )
        _park_default_hibernated(agent)
        hibernate.assert_called_once()
        self.assertEqual(hibernate.call_args.kwargs['reason'], 'default_until_first_use')
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)

    @patch('fleet.personal_agents._actuate_personal_runtime')
    def test_deleted_personal_agent_is_a_durable_tombstone(self, actuate):
        agent = FleetAgent.objects.create(
            agent_username='shizuha-43', owner_id=43, isolated=False,
            owner_profile_id=30043, identity_user_id=10043, identity_profile_id=20043,
            identity_provisioned_at=timezone.now(), status=FleetAgent.Status.DELETED,
        )

        result = ensure_personal_shizuha_agent(owner_id=43, owner_username='alice')

        agent.refresh_from_db()
        self.assertEqual(result.agent.pk, agent.pk)
        self.assertEqual(agent.status, FleetAgent.Status.DELETED)
        actuate.assert_not_called()

    def test_exact_profile_binding_is_immutable_after_identity_provisioning(self):
        agent = FleetAgent.objects.create(
            agent_username='shizuha-10', owner_id=10, isolated=True,
            owner_profile_id=30010, identity_user_id=10010, identity_profile_id=20010,
            identity_provisioned_at=timezone.now(), status=FleetAgent.Status.STOPPED,
        )
        for field, value in (
            ('owner_profile_id', 999),
            ('identity_user_id', 998),
            ('identity_profile_id', 997),
        ):
            row = FleetAgent.objects.get(pk=agent.pk)
            setattr(row, field, value)
            with self.assertRaisesRegex(ValidationError, f'{field} is immutable'):
                row.save()

    def test_owner_profile_first_bind_null_to_value_is_allowed(self):
        # PLAT-6199: persist_identity_binding queryset-updates owner_profile_id
        # and mutates the in-memory instance, but refresh_from_db() does not
        # reset __orig_owner_profile_id — so a NULL→value first bind on a
        # provisioned agent must be allowed, or every later save() (e.g.
        # start_agent's status write in the enable path) raises a false
        # immutability error. Same first-bind class as identity_user_id.
        agent = FleetAgent.objects.create(
            agent_username='shizuha-6199', owner_id=6199, isolated=True,
            owner_profile_id=None, identity_user_id=10099, identity_profile_id=20099,
            identity_provisioned_at=timezone.now(), status=FleetAgent.Status.STOPPED,
        )
        agent.owner_profile_id = 30099  # first bind NULL→value
        agent.save()  # must not raise
        agent.refresh_from_db()
        self.assertEqual(agent.owner_profile_id, 30099)
        # A later change is still rejected (immutable once bound).
        row = FleetAgent.objects.get(pk=agent.pk)
        row.owner_profile_id = 999
        with self.assertRaisesRegex(ValidationError, 'owner_profile_id is immutable'):
            row.save()

    def test_owner_may_own_a_fleet_personal_resolved_by_username(self):
        # PLAT-4235 ("users own fleets", operator 2026-07-12): the former
        # one-personal-agent-per-owner unique constraint is dropped (migration
        # 0030). An owner's per-user daemon manages their whole fleet, so an
        # owner may own MANY agents; the canonical personal Shizuha is resolved
        # by its deterministic username, not by owner_id uniqueness.
        from .personal_agents import get_personal_agent
        FleetAgent.objects.create(agent_username='shizuha-11', owner_id=11)
        # A second agent for the same owner is now allowed (a fleet, not a singleton).
        FleetAgent.objects.create(agent_username='other-11', owner_id=11)
        self.assertEqual(FleetAgent.objects.filter(owner_id=11).count(), 2)
        personal = get_personal_agent(11)
        self.assertIsNotNone(personal)
        self.assertEqual(personal.agent_username, 'shizuha-11')

    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    @patch('fleet.personal_agents.provisioner.provision_agent', return_value=True)
    @patch('fleet.personal_agents._provision_platform_identity_details', return_value=None)
    def test_identity_failure_stays_non_running_and_retryable(self, failed, runtime, _wire):
        with self.assertRaises(PersonalShizuhaProvisionError):
            ensure_personal_shizuha_agent(owner_id=12, owner_username='owner')
        agent = FleetAgent.objects.get(owner_id=12)
        self.assertEqual(agent.status, FleetAgent.Status.FAILED)
        self.assertIsNone(agent.identity_provisioned_at)
        runtime.assert_not_called()  # no runtime without a confirmed identity

        failed.return_value = _binding(12)
        retry = ensure_personal_shizuha_agent(owner_id=12, owner_username='owner')
        self.assertIsNotNone(retry.agent.identity_provisioned_at)
        self.assertEqual(retry.agent.status, FleetAgent.Status.HIBERNATED)
        runtime.assert_not_called()

    def test_owned_agent_may_run_in_shared_org_fleet(self):
        # Operator 2026-09-01: no org-vs-personal class. Shared fleet
        # (daemon.owner_subject NULL) is the org workspace; owned agents join
        # orgs their owner belongs to.
        from fleet.models import DaemonRegistry
        daemon = DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1-test', fleet_id='org',
            owner_subject=None, status=DaemonRegistry.Status.CONNECTED,
        )
        for username, owner_id in (('ichi', 3), ('shizuha-13', 13)):
            agent = FleetAgent.objects.create(
                agent_username=username, owner_id=owner_id, isolated=False,
                namespace='shizuha-fleet', managing_daemon=daemon,
                status=FleetAgent.Status.HIBERNATED,
            )
            self.assertFalse(provisioner._personal_runtime_isolation_gate(agent))
            agent.refresh_from_db()
            self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)
            self.assertNotIn('isolated namespace', agent.status_message or '')

    def test_isolated_agent_passes_the_gate(self):
        agent = FleetAgent.objects.create(
            agent_username='shizuha-15', owner_id=15, isolated=True,
            status=FleetAgent.Status.STOPPED,
        )
        self.assertFalse(provisioner._personal_runtime_isolation_gate(agent))
        agent.refresh_from_db()
        self.assertNotEqual(agent.status, FleetAgent.Status.BLOCKED)

    def test_stale_personal_isolation_block_is_cleared_to_hibernate(self):
        from fleet.models import DaemonRegistry
        daemon = DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1-stale', fleet_id='org',
            owner_subject=None, status=DaemonRegistry.Status.CONNECTED,
        )
        agent = FleetAgent.objects.create(
            agent_username='ichi', owner_id=3, isolated=False,
            namespace='shizuha-fleet', managing_daemon=daemon,
            status=FleetAgent.Status.BLOCKED,
            status_message=(
                'Personal agent must run in its own isolated namespace '
                '(isolated=True); refusing to actuate a personal agent into '
                'the shared fleet namespace.'
            ),
            hibernation_reason='does not hold the model lease',
        )
        n = provisioner.clear_stale_personal_isolation_blocks()
        self.assertEqual(n, 1)
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)
        self.assertEqual(agent.status_message, '')
        self.assertEqual(agent.hibernation_reason, 'does not hold the model lease')

    def test_cross_tenant_block_is_not_cleared_as_stale(self):
        from fleet.models import DaemonRegistry
        daemon = DaemonRegistry.objects.create(
            daemon_id='rt-fleet-other-stale', fleet_id='other',
            owner_subject=99, status=DaemonRegistry.Status.CONNECTED,
        )
        agent = FleetAgent.objects.create(
            agent_username='ichi', owner_id=3, isolated=False,
            namespace='shizuha-fleet', managing_daemon=daemon,
            status=FleetAgent.Status.BLOCKED,
            status_message=(
                'Personal agent must run in its own isolated namespace '
                '(isolated=True); refusing to actuate a personal agent into '
                'the shared fleet namespace.'
            ),
        )
        n = provisioner.clear_stale_personal_isolation_blocks()
        self.assertEqual(n, 0)
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.BLOCKED)

    def test_cross_tenant_daemon_placement_is_refused(self):
        from fleet.models import DaemonRegistry
        daemon = DaemonRegistry.objects.create(
            daemon_id='rt-fleet-other', fleet_id='other',
            owner_subject=99, status=DaemonRegistry.Status.CONNECTED,
        )
        agent = FleetAgent.objects.create(
            agent_username='ichi', owner_id=3, isolated=False,
            namespace='shizuha-fleet', managing_daemon=daemon,
            status=FleetAgent.Status.HIBERNATED,
        )
        self.assertTrue(provisioner._personal_runtime_isolation_gate(agent))
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.BLOCKED)
        self.assertIn('different owner', agent.status_message)

    @patch('fleet.views._attach_pulse_team_memberships', side_effect=lambda _request, agents, org_slug='': agents)
    @patch('fleet.views._owner_claw_agents', return_value=[])
    @patch('fleet.views.list_external_agents', return_value=[])
    def test_owner_inventory_contains_only_own_native_personal_agent(self, _external, _claws, _attach_teams):
        mine = FleetAgent.objects.create(
            agent_username='shizuha-21', owner_id=21, display_name='Shizuha',
            isolated=True, status=FleetAgent.Status.STOPPED,
        )
        FleetAgent.objects.create(
            agent_username='shizuha-22', owner_id=22, display_name='Shizuha',
            isolated=True, status=FleetAgent.Status.STOPPED,
        )
        request = SimpleNamespace(user=SimpleNamespace(id=21, is_staff=False))
        agents = FleetAgentListView()._build_agents_index(request, include_external=True)
        self.assertEqual([item['id'] for item in agents], [str(mine.id)])
        self.assertEqual(agents[0]['owner_id'], 21)

    @patch('fleet.views._attach_health_summaries', side_effect=lambda agents: agents)
    @patch('fleet.views._attach_identity_routing_health', side_effect=lambda agents: agents)
    @patch('fleet.views._attach_pulse_team_memberships')
    @patch('fleet.views._owner_claw_agents', return_value=[])
    @patch('fleet.views.list_external_agents', return_value=[])
    def test_owner_inventory_attaches_authoritative_pulse_team_placement(
        self, _external, _claws, attach_teams, _routing_health, _health,
    ):
        mine = FleetAgent.objects.create(
            agent_username='shizuha-23', owner_id=23, display_name='Shizuha',
            isolated=True, status=FleetAgent.Status.RUNNING,
        )
        attach_teams.side_effect = lambda _request, agents, org_slug='': [{
            **agents[0],
            'team_memberships': [{
                'team_slug': 'admin-ops',
                'organization_slug': 'shizuha',
            }],
            'team_names': ['admin-ops'],
        }]
        request = SimpleNamespace(user=SimpleNamespace(id=23, is_staff=False))

        agents = FleetAgentListView()._build_agents_index(
            request, include_external=True, org_slug='shizuha',
        )

        self.assertEqual([item['id'] for item in agents], [str(mine.id)])
        self.assertEqual(agents[0]['team_names'], ['admin-ops'])
        self.assertEqual(agents[0]['team_memberships'][0]['organization_slug'], 'shizuha')
        attach_teams.assert_called_once()
        self.assertEqual(attach_teams.call_args.kwargs['org_slug'], 'shizuha')


@override_settings(SHIZUHA_PLATFORM_URL='https://shizuha.test')
class PersonalShizuhaConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    @patch('fleet.personal_agents._provision_platform_identity_details')
    @patch('fleet.personal_agents.provisioner.provision_agent', return_value=True)
    def test_concurrent_first_use_converges_on_one_row(self, provision, identity, _wire):
        identity.side_effect = lambda agent, *_args: _binding(agent.owner_id)
        ready = Barrier(2)

        def ensure():
            close_old_connections()
            try:
                ready.wait(timeout=5)
                result = ensure_personal_shizuha_agent(owner_id=31, owner_username='owner')
                return result.agent.pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            agent_ids = list(pool.map(lambda _index: ensure(), range(2)))

        self.assertEqual(len(set(agent_ids)), 1)
        self.assertEqual(FleetAgent.objects.filter(owner_id=31).count(), 1)
        agent = FleetAgent.objects.get(owner_id=31)
        self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)
        provision.assert_not_called()


@override_settings(SHIZUHA_PLATFORM_URL='https://shizuha.test')
class UnifiedDaemonPathTests(TestCase):
    """Operator 2026-07-13 ("every agent should be literally the same"): a
    personal Shizuha is an ORDINARY fleet agent. When the owner has a CONNECTED
    fleet daemon, ensure() provisions through it (HIVE-247 top-down, shared
    fleet namespace, isolated=False) — the k3s isolated-namespace provisioner is
    only the fallback for owners without a daemon."""

    def _daemon(self, owner):
        from .models import DaemonRegistry
        return DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1', fleet_id='fleet-test', owner_subject=owner,
            status=DaemonRegistry.Status.CONNECTED)

    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    @patch('fleet.runtime_fleet.provision_runtime_agent_via_daemon', return_value=(True, ''))
    @patch('fleet.personal_agents.provisioner.provision_agent', return_value=True)
    @patch('fleet.personal_agents._provision_platform_identity_details')
    def test_owner_daemon_keeps_row_hibernated_in_owner_fleet(self, identity, k3s, via_daemon, _wire):
        identity.side_effect = lambda agent, *_a: _binding(agent.owner_id)
        self._daemon(owner=61)
        result = ensure_personal_shizuha_agent(owner_id=61, owner_username='owner61')
        self.assertFalse(result.agent.isolated)  # owner's own fleet namespace
        self.assertEqual(result.agent.status, FleetAgent.Status.HIBERNATED)
        via_daemon.assert_not_called()
        k3s.assert_not_called()

    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    @patch('fleet.runtime_fleet.provision_runtime_agent_via_daemon')
    @patch('fleet.personal_agents.provisioner.provision_agent', return_value=True)
    @patch('fleet.personal_agents._provision_platform_identity_details')
    def test_no_daemon_parks_isolated_hibernated(self, identity, k3s, via_daemon, _wire):
        identity.side_effect = lambda agent, *_a: _binding(agent.owner_id)
        result = ensure_personal_shizuha_agent(owner_id=62, owner_username='owner62')
        self.assertTrue(result.agent.isolated)
        self.assertEqual(result.agent.status, FleetAgent.Status.HIBERNATED)
        k3s.assert_not_called()
        via_daemon.assert_not_called()


class ProvisionViaDaemonTests(TestCase):
    """runtime_fleet.provision_runtime_agent_via_daemon contract."""

    def _daemon(self, daemon_id='rt-fleet-s1', owner=63):
        from .models import DaemonRegistry
        return DaemonRegistry.objects.create(
            daemon_id=daemon_id, fleet_id='fleet-test', owner_subject=owner,
            status=DaemonRegistry.Status.CONNECTED)

    def test_provision_binds_row_and_enables(self):
        from fleet import runtime_fleet
        d = self._daemon()
        agent = FleetAgent.objects.create(agent_username='shizuha-63', owner_id=63, model='m1')
        with patch.object(runtime_fleet, 'runtime_control_enabled', return_value=True), \
             patch.object(runtime_fleet, '_post_control',
                          return_value=(True, {'admitted': True, 'agent_id': 'local-shizuha-63-abc'})) as post, \
             patch.object(runtime_fleet, 'set_runtime_agent_enabled', return_value=(True, {})) as enable, \
             patch.object(runtime_fleet, 'configure_runtime_agent', return_value=(True, '')):
            ok, detail = runtime_fleet.provision_runtime_agent_via_daemon(agent, d)
        self.assertTrue(ok, detail)
        agent.refresh_from_db()
        self.assertEqual(agent.agent_id, 'local-shizuha-63-abc')
        self.assertEqual(agent.managing_daemon_id, d.id)
        self.assertEqual(agent.status, FleetAgent.Status.PROVISIONING)
        enable.assert_called_once_with('local-shizuha-63-abc', True)
        body = post.call_args[0][1]
        self.assertEqual(body['username'], 'shizuha-63')
        self.assertEqual(body['runtimeEnvironment'], 'k8s')
        self.assertEqual(body['op_id'], f'hive-provision-{agent.pk}')

    def test_refuses_daemon_without_control_endpoint(self):
        # The control proxy fronts ONE daemon; a per-user daemon without its own
        # control endpoint must be refused (never cross-provision), caller falls back.
        from fleet import runtime_fleet
        d = self._daemon(daemon_id='fleet-daemon-u99', owner=99)
        agent = FleetAgent.objects.create(agent_username='shizuha-99', owner_id=99)
        with patch.object(runtime_fleet, 'runtime_control_enabled', return_value=True):
            ok, detail = runtime_fleet.provision_runtime_agent_via_daemon(agent, d)
        self.assertFalse(ok)
        self.assertIn('no control endpoint', detail)

    def test_refuses_rematerialize_of_bound_row(self):
        # A row already daemon-materialized must never be provisioned again — the
        # daemon would create a duplicate roster entry for the same username.
        from fleet import runtime_fleet
        d = self._daemon(owner=64)
        agent = FleetAgent.objects.create(
            agent_username='shizuha-64', owner_id=64,
            agent_id='local-shizuha-64-x', managing_daemon=d)
        with patch.object(runtime_fleet, 'runtime_control_enabled', return_value=True):
            ok, detail = runtime_fleet.provision_runtime_agent_via_daemon(agent, d)
        self.assertFalse(ok)
        self.assertIn('already materialized', detail)


class PersonalShizuhaFleetAgentGuardTests(TestCase):
    """A FLEET AGENT calling the first-session hook must NOT spawn its own personal
    Shizuha. Agent platform accounts are non-staff @shizuha.com users, so without
    this guard a fleet agent (e.g. lina/owner=273) spuriously gets shizuha-273."""

    @patch('fleet.views._admin_organizations', return_value=([], None))
    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    @patch('fleet.personal_agents.ensure_personal_shizuha_agent')
    def test_fleet_agent_caller_skips_personal_shizuha(self, ensure, wire, _orgs):
        from fleet.views import FleetOrganizationListView
        FleetAgent.objects.create(agent_username='lina', owner_id=3, status=FleetAgent.Status.RUNNING)
        request = SimpleNamespace(user=SimpleNamespace(
            id=273, username='lina', is_staff=False, is_authenticated=True))
        FleetOrganizationListView().get(request)
        ensure.assert_not_called()  # the agent must not get a personal assistant
        wire.assert_not_called()

    @patch('fleet.views._admin_organizations', return_value=([], None))
    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    @patch('fleet.personal_agents.ensure_personal_shizuha_agent')
    def test_is_agent_claim_skips_personal_shizuha(self, ensure, wire, _orgs):
        # Authoritative signal: the Shizuha-ID is_agent claim, independent of the
        # local roster (no matching FleetAgent row needed).
        from fleet.views import FleetOrganizationListView
        request = SimpleNamespace(user=SimpleNamespace(
            id=555, username='some-agent-acct', is_staff=False,
            is_authenticated=True, is_agent=True))
        FleetOrganizationListView().get(request)
        ensure.assert_not_called()

    @patch('fleet.views._admin_organizations', return_value=([], None))
    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    @patch('fleet.personal_agents.ensure_personal_shizuha_agent')
    def test_org_list_does_not_mint_personal_shizuha(self, ensure, wire, _orgs):
        from fleet.views import FleetOrganizationListView
        request = SimpleNamespace(user=SimpleNamespace(
            id=901, username='real-human', email='navin@gmail.com',
            is_staff=False, is_superuser=False, is_agent=False,
            is_authenticated=True, profile=SimpleNamespace(account_type='human')))
        FleetOrganizationListView().get(request)
        ensure.assert_not_called()

    @patch('fleet.views._admin_organizations', return_value=([], None))
    @patch('fleet.personal_agents.wire_personal_agent_connectivity')
    @patch('fleet.personal_agents.ensure_personal_shizuha_agent')
    def test_existing_fleet_owner_skips_default(self, ensure, wire, _orgs):
        from fleet.views import FleetOrganizationListView
        FleetAgent.objects.create(agent_username='hina', owner_id=3, status=FleetAgent.Status.RUNNING)
        request = SimpleNamespace(user=SimpleNamespace(
            id=3, username='hritik', email='test-owner@example.com',
            is_staff=False, is_superuser=False, is_agent=False,
            is_authenticated=True, profile=SimpleNamespace(account_type='human')))
        FleetOrganizationListView().get(request)
        ensure.assert_not_called()

