from unittest.mock import Mock

from django.test import TestCase
from django.utils import timezone

from fleet.capability_reconcile import (
    apply_capability_reconcile,
    plan_capability_reconcile,
)
from fleet.effective_capabilities import (
    compute_effective_capability_grant_for_memberships,
)
from fleet.models import AgentCapabilityOverride, ensure_team_required_capability
from fleet.models import FleetAgent


class OrganizationScopedCapabilityGrantTests(TestCase):
    def setUp(self):
        ensure_team_required_capability('accounting', actor='test')
        ensure_team_required_capability('devops', actor='test')

    def test_accounting_grant_namespaces_books_and_drive_to_digital_org(self):
        grant = compute_effective_capability_grant_for_memberships([
            {
                'team_slug': 'accounting',
                'organization_slug': 'shizuha-digital',
            },
        ], {'kane', 'kane@shizuha.com'})

        self.assertIn('books:shizuha-digital', grant['enabled_mcp_servers'])
        self.assertIn('drive:shizuha-digital', grant['enabled_mcp_servers'])
        self.assertNotIn('books', grant['enabled_mcp_servers'])
        self.assertEqual(grant['organization_scopes'], ['shizuha-digital'])

    def test_accounting_grant_namespaces_books_and_drive_to_personal_org(self):
        grant = compute_effective_capability_grant_for_memberships([
            {
                'team_slug': 'accounting',
                'organization_slug': 'personal-3',
            },
        ], {'sato', 'sato@shizuha.com'})

        self.assertIn('books:personal-3', grant['enabled_mcp_servers'])
        self.assertIn('drive:personal-3', grant['enabled_mcp_servers'])
        self.assertNotIn('books:shizuha-digital', grant['enabled_mcp_servers'])

    def test_explicit_org_read_excludes_capabilities_held_in_another_org(self):
        memberships = [
            {'team_slug': 'devops', 'organization_slug': 'shizuha'},
            {'team_slug': 'accounting', 'organization_slug': 'personal-3'},
        ]

        personal = compute_effective_capability_grant_for_memberships(
            memberships,
            {'sato', 'sato@shizuha.com'},
            organization_scope='personal-3',
        )

        self.assertEqual([cap['slug'] for cap in personal['capabilities']], ['accounting'])
        self.assertIn('books:personal-3', personal['enabled_mcp_servers'])
        self.assertNotIn('company-os:shizuha', personal['skills'])
        self.assertNotIn('devops', personal['skills'])

    def test_aggregate_config_preserves_independent_multi_org_grants(self):
        memberships = [
            {'team_slug': 'accounting', 'organization_slug': 'shizuha-digital'},
            {'team_slug': 'accounting', 'organization_slug': 'personal-3'},
        ]

        grant = compute_effective_capability_grant_for_memberships(
            memberships,
            {'multi-accountant', 'multi-accountant@shizuha.com'},
        )

        self.assertIn('books:shizuha-digital', grant['enabled_mcp_servers'])
        self.assertIn('books:personal-3', grant['enabled_mcp_servers'])
        self.assertIn('drive:shizuha-digital', grant['enabled_mcp_servers'])
        self.assertIn('drive:personal-3', grant['enabled_mcp_servers'])

    def test_platform_global_agent_override_does_not_cross_into_tenant_org(self):
        devops, _ = ensure_team_required_capability('devops', actor='test')
        AgentCapabilityOverride.objects.create(
            agent_identifier='sato@shizuha.com',
            capability=devops,
            action=AgentCapabilityOverride.Action.ADD,
            reason='platform-only exception',
            approved_by='test',
            approved_at=timezone.now(),
        )

        personal = compute_effective_capability_grant_for_memberships([
            {'team_slug': 'accounting', 'organization_slug': 'personal-3'},
        ], {'sato@shizuha.com'})
        internal = compute_effective_capability_grant_for_memberships([
            {'team_slug': 'accounting', 'organization_slug': 'shizuha'},
        ], {'sato@shizuha.com'})

        self.assertEqual([cap['slug'] for cap in personal['capabilities']], ['accounting'])
        self.assertIn('devops', [cap['slug'] for cap in internal['capabilities']])


class CapabilityDesiredConfigReconcileTests(TestCase):
    def setUp(self):
        ensure_team_required_capability('accounting', actor='test')
        self.agents = [
            {
                'id': 'kane-id',
                'agent_username': 'kane',
                'email': 'kane@shizuha.com',
                'skills': ['company-os'],
                'enabled_mcp_server_ids': ['admin', 'connect', 'id', 'pulse', 'wiki'],
            },
            {
                'id': 'sato-id',
                'agent_username': 'sato',
                'email': 'sato@shizuha.com',
                'skills': ['company-os'],
                'enabled_mcp_server_ids': ['admin', 'connect', 'id', 'pulse', 'wiki'],
            },
        ]
        self.rosters = [
            {
                'team_name': 'accounting',
                'organization_slug': 'shizuha-digital',
                'tiers': [[{'email': 'kane@shizuha.com'}]],
            },
            {
                'team_name': 'accounting',
                'organization_slug': 'personal-3',
                'tiers': [[{'email': 'sato@shizuha.com'}]],
            },
        ]

    def test_plan_repairs_stored_base_only_mcp_config_per_org(self):
        plans = {
            plan['agent_username']: plan
            for plan in plan_capability_reconcile(self.agents, self.rosters)
        }

        self.assertIn(
            'books:shizuha-digital',
            plans['kane']['updates']['enabled_mcp_server_ids'],
        )
        self.assertIn(
            'drive:shizuha-digital',
            plans['kane']['updates']['enabled_mcp_server_ids'],
        )
        self.assertNotIn(
            'books:personal-3',
            plans['kane']['updates']['enabled_mcp_server_ids'],
        )
        self.assertIn(
            'books:personal-3',
            plans['sato']['updates']['enabled_mcp_server_ids'],
        )
        self.assertNotIn(
            'books:shizuha-digital',
            plans['sato']['updates']['enabled_mcp_server_ids'],
        )

    def test_apply_uses_hive_runtime_config_actuator(self):
        plans = plan_capability_reconcile(self.agents, self.rosters)
        configure = Mock(return_value=(True, ''))

        result = apply_capability_reconcile(plans, configure)

        self.assertEqual(result['checked_agents'], 2)
        self.assertEqual(result['drifted_agents'], 2)
        self.assertEqual(result['updated_agents'], 2)
        self.assertEqual(configure.call_count, 2)
        calls = {call.args[0]: call.args[1] for call in configure.call_args_list}
        self.assertIn('books:shizuha-digital', calls['kane-id']['enabled_mcp_server_ids'])
        self.assertIn('books:personal-3', calls['sato-id']['enabled_mcp_server_ids'])

    def test_periodic_reconcile_does_not_clobber_unrostered_personal_agent_config(self):
        agents = self.agents + [{
            'id': 'personal-assistant-id',
            'agent_username': 'personal-assistant',
            'email': 'personal-assistant@agents.shizuha.io',
            'enabled_mcp_server_ids': ['drive', 'mail', 'notes'],
        }]

        plans = plan_capability_reconcile(agents, self.rosters)

        self.assertEqual(
            {plan['agent_username'] for plan in plans},
            {'kane', 'sato'},
        )

    def test_periodic_reconcile_includes_native_agents(self):
        agents = self.agents + [{
            'id': 'native-id',
            'agent_username': 'native-accountant',
            'email': 'native-accountant@shizuha.com',
            'lifecycle_plane': 'native',
        }]
        rosters = self.rosters + [{
            'team_name': 'accounting',
            'organization_slug': 'shizuha',
            'tiers': [[{'email': 'native-accountant@shizuha.com'}]],
        }]

        plans = plan_capability_reconcile(agents, rosters)

        self.assertEqual(
            {plan['agent_username'] for plan in plans},
            {'kane', 'sato', 'native-accountant'},
        )
        native = next(
            plan for plan in plans
            if plan['agent_username'] == 'native-accountant'
        )
        self.assertEqual(native['lifecycle_plane'], 'native')


class NativeCapabilityConfigReconcileTests(TestCase):
    def test_native_projection_and_actuator_converge_without_waking_agent(self):
        from fleet.lifecycle_fleet import (
            configure_lifecycle_agent,
            list_lifecycle_agents,
        )

        agent = FleetAgent.objects.create(
            agent_username='native-accountant',
            agent_id='native-accountant',
            status=FleetAgent.Status.STOPPED,
            skill_config={
                'skills': ['company-os'],
                'inline_profiles': ['preserve-me'],
            },
            mcp_config={
                'enabled_mcp_server_ids': ['pulse'],
                'runtime_flags': {'preserve': True},
            },
        )

        with self.settings(RUNTIME_FLEET_API_URL=''):
            projected = next(
                row for row in list_lifecycle_agents()
                if row['agent_username'] == 'native-accountant'
            )
        self.assertEqual(projected['skills'], ['company-os'])
        self.assertEqual(projected['enabled_mcp_server_ids'], ['pulse'])

        ok, detail = configure_lifecycle_agent(str(agent.id), {
            'skills': ['accounting'],
            'eager_skills': ['books'],
            'enabled_mcp_server_ids': ['books:personal-3', 'drive:personal-3'],
        })

        self.assertTrue(ok, detail)
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.STOPPED)
        self.assertEqual(agent.skill_config['skills'], ['accounting'])
        self.assertEqual(agent.skill_config['eager_skills'], ['books'])
        self.assertEqual(agent.skill_config['inline_profiles'], ['preserve-me'])
        self.assertEqual(
            agent.mcp_config['enabled_mcp_server_ids'],
            ['books:personal-3', 'drive:personal-3'],
        )
        self.assertEqual(agent.mcp_config['runtime_flags'], {'preserve': True})

        with self.settings(RUNTIME_FLEET_API_URL=''):
            converged = next(
                row for row in list_lifecycle_agents()
                if row['agent_username'] == 'native-accountant'
            )
        self.assertEqual(converged['skills'], ['accounting'])
        self.assertEqual(
            converged['enabled_mcp_server_ids'],
            ['books:personal-3', 'drive:personal-3'],
        )
