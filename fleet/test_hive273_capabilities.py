"""HIVE-273 capability catalog/team requirement/effective-set tests."""
import hashlib
import time
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from fleet.models import (
    CapabilityAuditEvent,
    CapabilityDefinition,
    DaemonCredential,
    DEFAULT_TEAM_MCP_SERVER_REFS,
    FleetAgent,
    TeamMembershipRole,
    TeamRequiredCapability,
    ensure_team_required_capability,
)
from fleet.views import (
    AgentEffectiveCapabilitiesView,
    CapabilityActionView,
    CapabilityDetailView,
    CapabilityListView,
    TeamCapabilitiesView,
)


class _OpenCapabilityListView(CapabilityListView):
    permission_classes = []
    authentication_classes = []


class _OpenCapabilityDetailView(CapabilityDetailView):
    permission_classes = []
    authentication_classes = []


class _OpenCapabilityActionView(CapabilityActionView):
    permission_classes = []
    authentication_classes = []


class _OpenTeamCapabilitiesView(TeamCapabilitiesView):
    permission_classes = []
    authentication_classes = []


class _OpenEffectiveCapabilitiesView(AgentEffectiveCapabilitiesView):
    permission_classes = []
    authentication_classes = []


class HiveCapabilitiesTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        # PLAT-1251: the fail-loud guard fires a background Pulse notification when
        # a capability union misses base MCP servers (deliberately common in these
        # fixtures) — keep tests hermetic.
        notify_patcher = patch('fleet.views._notify_base_mcp_floor_violation_async')
        self.notify_mock = notify_patcher.start()
        self.addCleanup(notify_patcher.stop)

    def test_team_capability_baselines_include_core_platform_mcp_contract(self):
        required = {'pulse', 'connect', 'wiki', 'admin', 'id'}
        self.assertTrue(required.issubset(set(DEFAULT_TEAM_MCP_SERVER_REFS)))

        for team in ['admin-ops', 'governance', 'program-management']:
            cap, req = ensure_team_required_capability(
                team,
                actor='test:PLAT-1251',
                reason='PLAT-1251 baseline MCP contract regression guard.',
            )
            self.assertTrue(req.enabled)
            self.assertTrue(required.issubset(set(cap.mcp_server_refs)))

        agent = FleetAgent.objects.create(agent_username='shizuha', agent_id='shizuha-id')
        req = self.factory.get(f'/v1/fleet/agents/{agent.id}/effective-capabilities/')
        with patch('fleet.views._require_staff', return_value=None):
            with patch(
                'fleet.views._pulse_roster_memberships',
                return_value=(
                    {'admin-ops', 'governance', 'program-management'},
                    {'admin-ops', 'governance', 'program-management'},
                    None,
                ),
            ):
                resp = _OpenEffectiveCapabilitiesView.as_view()(req, agent_id=str(agent.id))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(required.issubset(set(resp.data['enabled_mcp_servers'])))
        self.assertIn('connect', resp.data['enabled_mcp_servers'])

    def test_effective_capabilities_accepts_valid_daemon_link_credential(self):
        token = 'daemon-capability-read-token'
        DaemonCredential.objects.create(
            daemon_id='rt-fleet-test',
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
        )
        agent = FleetAgent.objects.create(agent_username='nagi', agent_id='nagi-runtime')
        req = self.factory.get(
            f'/v1/fleet/agents/{agent.id}/effective-capabilities/',
            HTTP_X_HIVE_DAEMON_ID='rt-fleet-test',
            HTTP_X_HIVE_DAEMON_TOKEN=token,
        )

        roster = {
            'nagi@shizuha.com': [{'team_slug': 'engineering', 'team_name': 'Engineering'}],
        }
        with patch(
            'fleet.views._roster_process_read',
            return_value=(
                roster,
                timezone.now().timestamp(),
            ),
        ), patch('fleet.views._roster_cache_read') as redis_roster, \
                patch('fleet.views._pulse_roster_memberships') as live_roster:
            started = time.monotonic()
            resp = _OpenEffectiveCapabilitiesView.as_view()(req, agent_id=str(agent.id))
            elapsed = time.monotonic() - started

        self.assertEqual(resp.status_code, 200)
        # Process-cache path must not fall through to Redis/Pulse. 0.1s was
        # CI-flaky under load (0.144s on hive-5cc6cf6 redispatch 2026-09-02
        # and wedged deploy-fleet; 0.17s on a busy runner per HIVE-2132).
        # 1.0s still fails a network round-trip without flaking.
        self.assertLess(elapsed, 1.0)
        self.assertIn('enabled_mcp_servers', resp.data)
        self.assertIn(
            {'level': 'info', 'message': 'daemon capability roster snapshot',
             'source': 'process', 'age_seconds': 0},
            resp.data['diagnostics'],
        )
        redis_roster.assert_not_called()
        live_roster.assert_not_called()

    def test_effective_capabilities_cold_daemon_read_uses_bounded_redis_snapshot(self):
        token = 'daemon-capability-cold-token'
        DaemonCredential.objects.create(
            daemon_id='rt-fleet-cold',
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
        )
        agent = FleetAgent.objects.create(agent_username='cold', agent_id='cold-runtime')
        req = self.factory.get(
            f'/v1/fleet/agents/{agent.id}/effective-capabilities/',
            HTTP_X_HIVE_DAEMON_ID='rt-fleet-cold',
            HTTP_X_HIVE_DAEMON_TOKEN=token,
        )
        roster = {'cold@shizuha.com': [{'team_slug': 'engineering'}]}
        with patch('fleet.views._roster_process_read', return_value=(None, 0.0)), \
                patch('fleet.views._roster_membership_cache_read', return_value=(None, 0.0)), \
                patch('fleet.views._roster_cache_read', return_value=(roster, time.time())):
            resp = _OpenEffectiveCapabilitiesView.as_view()(req, agent_id=str(agent.id))

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(
            item.get('source') == 'redis' and item.get('age_seconds') == 0
            for item in resp.data['diagnostics']
        ))

    def test_effective_capabilities_cold_daemon_read_fails_soft_without_snapshot(self):
        token = 'daemon-capability-empty-token'
        DaemonCredential.objects.create(
            daemon_id='rt-fleet-empty',
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
        )
        agent = FleetAgent.objects.create(agent_username='empty', agent_id='empty-runtime')
        req = self.factory.get(
            f'/v1/fleet/agents/{agent.id}/effective-capabilities/',
            HTTP_X_HIVE_DAEMON_ID='rt-fleet-empty',
            HTTP_X_HIVE_DAEMON_TOKEN=token,
        )
        with patch('fleet.views._roster_process_read', return_value=(None, 0.0)), \
                patch('fleet.views._roster_membership_cache_read', return_value=(None, 0.0)), \
                patch('fleet.views._roster_cache_read', return_value=(None, 0.0)):
            resp = _OpenEffectiveCapabilitiesView.as_view()(req, agent_id=str(agent.id))

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data['error'], 'Pulse roster cache unavailable for daemon capability read')

    @patch('fleet.views.list_runtime_agents', return_value=[{'mcp_servers': [{'slug': 'pulse'}]}])
    @patch('fleet.views.get_runtime_skills', return_value=['engineering', 'devops'])
    def test_catalog_create_rejects_unknown_refs(self, _skills, _agents):
        req = self.factory.post(
            '/v1/fleet/capabilities/',
            {
                'slug': 'bad-capability',
                'display_name': 'Bad Capability',
                'activation_status': 'active',
                'skill_refs': ['missing-skill'],
            },
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenCapabilityListView.as_view()(req)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('skill_refs', resp.data)

    @patch('fleet.views.list_runtime_agents', return_value=[{'mcp_servers': [{'slug': 'pulse'}]}])
    @patch('fleet.views.get_runtime_skills', return_value=['engineering', 'devops'])
    def test_generic_create_cannot_activate_sensitive_capability_or_forge_review_metadata(self, _skills, _agents):
        forged_at = timezone.now().isoformat()
        req = self.factory.post(
            '/v1/fleet/capabilities/',
            {
                'slug': 'privileged-grant',
                'display_name': 'Privileged Grant',
                'risk_tier': CapabilityDefinition.RiskTier.PRIVILEGED,
                'activation_status': CapabilityDefinition.ActivationStatus.ACTIVE,
                'skill_refs': ['engineering'],
                'mcp_server_refs': ['pulse'],
                'approval_task_key': 'HIVE-999',
                'approval_wiki_page_id': 'forged-page',
                'approved_by': 'attacker@example.com',
                'approved_at': forged_at,
                'security_reviewed_by': 'attacker@example.com',
                'security_reviewed_at': forged_at,
            },
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenCapabilityListView.as_view()(req)
        self.assertEqual(resp.status_code, 201)

        cap = CapabilityDefinition.objects.get(slug='privileged-grant')
        self.assertEqual(cap.activation_status, CapabilityDefinition.ActivationStatus.DRAFT)
        self.assertEqual(cap.approval_task_key, '')
        self.assertEqual(cap.approval_wiki_page_id, '')
        self.assertEqual(cap.approved_by, '')
        self.assertIsNone(cap.approved_at)
        self.assertEqual(cap.security_reviewed_by, '')
        self.assertIsNone(cap.security_reviewed_at)
        self.assertTrue(cap.security_review_required)
        self.assertEqual(resp.data['activation_status'], CapabilityDefinition.ActivationStatus.DRAFT)
        self.assertEqual(resp.data['approved_by'], '')
        self.assertIsNone(resp.data['approved_at'])
        self.assertEqual(resp.data['security_reviewed_by'], '')
        self.assertIsNone(resp.data['security_reviewed_at'])

    @patch('fleet.views.list_runtime_agents', return_value=[{'mcp_servers': [{'slug': 'pulse'}]}])
    @patch('fleet.views.get_runtime_skills', return_value=['engineering', 'devops'])
    def test_generic_patch_cannot_activate_sensitive_capability_or_forge_review_metadata(self, _skills, _agents):
        cap = CapabilityDefinition.objects.create(
            slug='sensitive-access',
            display_name='Sensitive Access',
            risk_tier=CapabilityDefinition.RiskTier.SENSITIVE,
            skill_refs=['engineering'],
            mcp_server_refs=['pulse'],
            approval_task_key='HIVE-273',
            approval_wiki_page_id='accepted-hld',
            approved_by='akira@shizuha.com',
            approved_at=timezone.now(),
        )
        original_approved_at = cap.approved_at
        req = self.factory.patch(
            '/v1/fleet/capabilities/sensitive-access/',
            {
                'activation_status': CapabilityDefinition.ActivationStatus.ACTIVE,
                'approval_task_key': 'HIVE-999',
                'approval_wiki_page_id': 'forged-page',
                'approved_by': 'attacker@example.com',
                'approved_at': timezone.now().isoformat(),
                'security_reviewed_by': 'attacker@example.com',
                'security_reviewed_at': timezone.now().isoformat(),
                'description': 'Legitimate editable metadata still updates.',
            },
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenCapabilityDetailView.as_view()(req, slug='sensitive-access')
        self.assertEqual(resp.status_code, 200)

        cap.refresh_from_db()
        self.assertEqual(cap.activation_status, CapabilityDefinition.ActivationStatus.DRAFT)
        self.assertEqual(cap.approval_task_key, 'HIVE-273')
        self.assertEqual(cap.approval_wiki_page_id, 'accepted-hld')
        self.assertEqual(cap.approved_by, 'akira@shizuha.com')
        self.assertEqual(cap.approved_at, original_approved_at)
        self.assertEqual(cap.security_reviewed_by, '')
        self.assertIsNone(cap.security_reviewed_at)
        self.assertEqual(cap.description, 'Legitimate editable metadata still updates.')
        self.assertEqual(resp.data['activation_status'], CapabilityDefinition.ActivationStatus.DRAFT)
        self.assertEqual(resp.data['approved_by'], 'akira@shizuha.com')
        self.assertEqual(resp.data['security_reviewed_by'], '')

    @patch('fleet.views.list_runtime_agents', return_value=[{'mcp_servers': [{'slug': 'pulse'}]}])
    @patch('fleet.views.get_runtime_skills', return_value=['engineering', 'devops'])
    def test_standard_capability_create_activate_assign_flow(self, _skills, _agents):
        req = self.factory.post(
            '/v1/fleet/capabilities/',
            {
                'slug': 'standard-tooling',
                'display_name': 'Standard Tooling',
                'risk_tier': CapabilityDefinition.RiskTier.STANDARD,
                'skill_refs': ['engineering'],
                'mcp_server_refs': ['pulse'],
            },
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenCapabilityListView.as_view()(req)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['activation_status'], CapabilityDefinition.ActivationStatus.DRAFT)

        req = self.factory.post(
            '/v1/fleet/capabilities/standard-tooling/activate/',
            {'reason': 'UI activation flow'},
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenCapabilityActionView.as_view()(req, slug='standard-tooling', action='activate')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['activation_status'], CapabilityDefinition.ActivationStatus.ACTIVE)

        req = self.factory.put(
            '/v1/fleet/teams/engineering/capabilities/',
            {'capabilities': [{'capability_slug': 'standard-tooling', 'reason': 'baseline'}]},
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenTeamCapabilitiesView.as_view()(req, team_name='engineering')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['capabilities'][0]['capability_slug'], 'standard-tooling')

    @patch('fleet.views.list_runtime_agents', return_value=[{'mcp_servers': [{'slug': 'pulse'}]}])
    @patch('fleet.views.get_runtime_skills', return_value=['engineering', 'devops'])
    def test_team_assignment_and_effective_capability_union(self, _skills, _agents):
        cap = CapabilityDefinition.objects.create(
            slug='engineering-effective-capability',
            display_name='Engineering Effective Capability',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            skill_refs=['engineering'],
            mcp_server_refs=['pulse'],
            runtime_flags={'can_code': True},
        )
        self.assertEqual(cap.version, 1)

        req = self.factory.put(
            '/v1/fleet/teams/engineering/capabilities/',
            {'capabilities': [{'capability_slug': 'engineering-effective-capability', 'reason': 'baseline'}]},
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenTeamCapabilitiesView.as_view()(req, team_name='engineering')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['team_slug'], 'engineering')
        self.assertEqual(resp.data['capabilities'][0]['capability_slug'], 'engineering-effective-capability')
        self.assertTrue(TeamRequiredCapability.objects.filter(team_slug='engineering', capability=cap).exists())

        agent = FleetAgent.objects.create(agent_username='nagi', agent_id='nagi-id', role='Engineer')
        TeamMembershipRole.objects.create(
            agent_identifier='nagi@shizuha.com',
            team_slug='engineering',
            role_title='Backend Lead',
            membership_role=TeamMembershipRole.MembershipRole.LEAD,
        )
        req = self.factory.get(f'/v1/fleet/agents/{agent.id}/effective-capabilities/')
        with patch('fleet.views._require_staff', return_value=None):
            with patch('fleet.views._pulse_roster_memberships', return_value=({'engineering'}, {'engineering'}, None)):
                resp = _OpenEffectiveCapabilitiesView.as_view()(req, agent_id=str(agent.id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['source_teams'], ['engineering'])
        self.assertEqual(resp.data['capabilities'][0]['slug'], 'engineering-effective-capability')
        self.assertEqual(resp.data['skills'], ['engineering', 'heartbeat-protocol'])
        self.assertEqual(resp.data['eager_skills'], ['heartbeat-protocol'])
        # PLAT-1251: the base platform MCP floor tops up the pulse-only union.
        self.assertEqual(resp.data['enabled_mcp_servers'], ['admin', 'connect', 'id', 'pulse', 'wiki'])
        self.assertEqual(resp.data['base_mcp_missing_pre_floor'], ['admin', 'connect', 'id', 'wiki'])
        self.assertEqual(resp.data['runtime_flags'], {'can_code': True})
        self.assertEqual(resp.data['display_title'], 'Engineer')
        self.assertEqual(resp.data['roles_by_team'][0]['role_title'], 'Backend Lead')
        self.assertEqual(resp.data['roles_by_team'][0]['membership_role'], 'lead')

    def test_team_assignment_rejects_inactive_capability(self):
        CapabilityDefinition.objects.create(
            slug='draft-capability',
            display_name='Draft Capability',
            activation_status=CapabilityDefinition.ActivationStatus.DRAFT,
        )
        req = self.factory.put(
            '/v1/fleet/teams/engineering/capabilities/',
            {'capabilities': ['draft-capability']},
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenTeamCapabilitiesView.as_view()(req, team_name='engineering')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not active', resp.data['error'])


    def test_review_team_baseline_carries_github_review_grant_scope(self):
        cap, _ = ensure_team_required_capability('review', actor='test', reason='PLAT-1282')
        self.assertEqual(cap.default_credential_grant_scopes, ['github:review'])

    def test_review_team_effective_capabilities_include_github_review_scope(self):
        ensure_team_required_capability('review', actor='test', reason='PLAT-1282')
        agent = {'id': 'rt-reika', 'email': 'reika@shizuha.com', 'agent_username': 'reika'}
        req = self.factory.get('/v1/fleet/agents/rt-reika/effective-capabilities/')
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.get_runtime_agent', return_value=agent), \
                patch('fleet.views._pulse_roster_memberships', return_value=({'review'}, {'review'}, None)):
            resp = _OpenEffectiveCapabilitiesView.as_view()(req, agent_id='rt-reika')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('github:review', resp.data['credential_grant_scopes'])

    def test_capability_patch_reconciles_agents_receiving_capability(self):
        cap = CapabilityDefinition.objects.create(
            slug='engineering-runtime',
            display_name='Engineering Runtime',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            skill_refs=['old-skill'],
            mcp_server_refs=['old-mcp'],
            inline_profile_refs=['standard'],
            runtime_flags={'supports_parallel': True},
            default_credential_grant_scopes=['kubeconfig'],
        )
        TeamRequiredCapability.objects.create(team_slug='sandbox-team', capability=cap, enabled=True, required=True)
        agent = {
            'id': 'rt-nagi',
            'agent_username': 'nagi',
            'email': 'nagi@shizuha.com',
            'skills': ['old-skill'],
            'eager_skills': [],
            'enabled_mcp_server_ids': ['old-mcp'],
            # HIVE-331: validation now always has a non-empty universe (platform
            # registry), so 'new-mcp' must be runtime-known for the PATCH to pass.
            'mcp_servers': [{'slug': 'old-mcp'}, {'slug': 'new-mcp'}],
        }
        req = self.factory.patch(
            '/v1/fleet/capabilities/engineering-runtime/',
            {'skill_refs': ['new-skill'], 'mcp_server_refs': ['new-mcp'], 'reason': 'mapping changed'},
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.get_runtime_skills', return_value=['new-skill']), \
                patch('fleet.views.list_runtime_agents', return_value=[agent]), \
                patch('fleet.views.get_runtime_agent', return_value=agent), \
                patch('fleet.views._pulse_roster_team_index', return_value={'nagi@shizuha.com': [{'team_slug': 'sandbox-team'}]}), \
                patch('fleet.views._pulse_roster_memberships', return_value=({'sandbox-team'}, {'sandbox-team'}, None)), \
                patch('fleet.views.configure_runtime_agent', return_value=(True, '')) as configure:
            resp = _OpenCapabilityDetailView.as_view()(req, slug='engineering-runtime')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['runtime_reconcile']['matched_agents'], 1)
        self.assertEqual(resp.data['runtime_reconcile']['updated_agents'], 1)
        self.assertEqual(resp.data['runtime_reconcile']['agents'][0]['effective_inline_profiles'], ['standard'])
        self.assertEqual(resp.data['runtime_reconcile']['agents'][0]['effective_runtime_flags'], {'supports_parallel': True})
        self.assertEqual(resp.data['runtime_reconcile']['agents'][0]['effective_credential_grant_scopes'], ['kubeconfig'])
        self.assertEqual(
            resp.data['runtime_reconcile']['agents'][0]['daemon_update_fields'],
            ['skills', 'eager_skills', 'enabled_mcp_server_ids'],
        )
        configure.assert_called_once_with(
            'rt-nagi',
            {
                'skills': ['heartbeat-protocol', 'new-skill'],
                'eager_skills': ['heartbeat-protocol'],
                # PLAT-1251: the base platform MCP floor tops up the capability union.
                'enabled_mcp_server_ids': ['admin', 'connect', 'id', 'new-mcp', 'pulse', 'wiki'],
            },
        )
        audit = CapabilityAuditEvent.objects.get(target_key='engineering-runtime', action='capability.update')
        self.assertEqual(audit.runtime_reconcile_outcome, 'runtime-reconciled')
        self.assertIn('rt-nagi', audit.diagnostic)

    # PLAT-5591/PLAT-5584: Kai cluster-plane kubeconfig via dedicated capability +
    # explicit authorizing seat override (HIVE-272 §4.2). Regression for the
    # "annotation claims privileged but caps deny" divergence.
    def _cluster_plane_capability(self):
        # update_or_create: migration 0050 already creates this capability on the
        # test DB (keepdb), so a plain create() would hit the unique slug. The
        # model's clean() also requires security-review metadata for a
        # sensitive/privileged ACTIVE capability.
        cap, _ = CapabilityDefinition.objects.update_or_create(
            slug='engineering-cluster-plane',
            defaults={
                'display_name': 'Engineering Cluster-Plane',
                'activation_status': CapabilityDefinition.ActivationStatus.ACTIVE,
                'risk_tier': CapabilityDefinition.RiskTier.SENSITIVE,
                'sensitive_access_flags': ['kubeconfig'],
                'default_credential_grant_scopes': ['kubeconfig'],
                'security_reviewed_by': 'san@shizuha.com',
                'security_reviewed_at': timezone.now(),
                'approved_by': 'san@shizuha.com',
                'approved_at': timezone.now(),
            },
        )
        return cap

    def test_engineering_cluster_plane_capability_confers_kubeconfig_only(self):
        cap = self._cluster_plane_capability()
        self.assertIn('kubeconfig', cap.sensitive_access_flags)
        self.assertIn('kubeconfig', cap.default_credential_grant_scopes)
        # Plane separation: NO host_exec / credential_broker on the dedicated cap.
        self.assertNotIn('host_exec', cap.sensitive_access_flags)
        self.assertNotIn('credential_broker', cap.sensitive_access_flags)

    def test_engineering_seat_with_cluster_plane_override_gets_kubeconfig(self):
        cap = self._cluster_plane_capability()
        # Kai-shaped fixture: engineering team (no kubeconfig) + explicit seat override.
        # update_or_create: migration 0050 already creates this override on the
        # test DB (keepdb), so a plain create() would hit the unique
        # (agent_identifier, capability) constraint.
        from fleet.models import AgentCapabilityOverride
        AgentCapabilityOverride.objects.update_or_create(
            agent_identifier='kai@shizuha.com',
            capability=cap,
            defaults={
                'action': AgentCapabilityOverride.Action.ADD,
                'reason': 'PLAT-5591 restore Kai cluster-plane kubeconfig',
                'approved_by': 'san@shizuha.com',
                'approved_at': timezone.now(),
                'created_by': 'san@shizuha.com',
                'updated_by': 'san@shizuha.com',
            },
        )
        agent = {'id': 'rt-kai', 'email': 'kai@shizuha.com', 'agent_username': 'kai'}
        req = self.factory.get('/v1/fleet/agents/rt-kai/effective-capabilities/')
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.get_runtime_agent', return_value=agent), \
                patch('fleet.views._pulse_roster_memberships', return_value=({'engineering'}, {'engineering'}, None)):
            resp = _OpenEffectiveCapabilitiesView.as_view()(req, agent_id='rt-kai')
        self.assertEqual(resp.status_code, 200)
        # The override must surface kubeconfig in the signed effective evidence so
        # the daemon's isPrivilegedK8sAgent gate selects the privileged variant.
        # NOTE: the view deliberately drops top-level + per-capability
        # sensitive_access_flags from the response (byte-for-byte stable for
        # existing consumers), so kubeconfig is asserted via credential_grant_scopes
        # and the capability slug instead.
        self.assertIn('kubeconfig', resp.data['credential_grant_scopes'])
        self.assertIn('engineering-cluster-plane', [c['slug'] for c in resp.data['capabilities']])

    def test_capability_patch_reconciles_agents_receiving_capability_through_requires(self):
        base = CapabilityDefinition.objects.create(
            slug='base-runtime',
            display_name='Base Runtime',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            skill_refs=['old-base'],
        )
        parent = CapabilityDefinition.objects.create(
            slug='engineering-runtime',
            display_name='Engineering Runtime',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            skill_refs=['parent-skill'],
            requires=['base-runtime'],
        )
        TeamRequiredCapability.objects.create(team_slug='sandbox-team', capability=parent, enabled=True, required=True)
        agent = {
            'id': 'rt-nagi',
            'agent_username': 'nagi',
            'email': 'nagi@shizuha.com',
            'skills': ['old-base', 'parent-skill'],
            'eager_skills': [],
            'enabled_mcp_server_ids': [],
        }
        req = self.factory.patch(
            '/v1/fleet/capabilities/base-runtime/',
            {'skill_refs': ['new-base'], 'reason': 'transitive dependency mapping changed'},
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.get_runtime_skills', return_value=['new-base', 'parent-skill']), \
                patch('fleet.views.list_runtime_agents', return_value=[agent]), \
                patch('fleet.views.get_runtime_agent', return_value=agent), \
                patch('fleet.views._pulse_roster_team_index', return_value={'nagi@shizuha.com': [{'team_slug': 'sandbox-team'}]}), \
                patch('fleet.views._pulse_roster_memberships', return_value=({'sandbox-team'}, {'sandbox-team'}, None)), \
                patch('fleet.views.configure_runtime_agent', return_value=(True, '')) as configure:
            resp = _OpenCapabilityDetailView.as_view()(req, slug=base.slug)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['runtime_reconcile']['team_slugs'], ['sandbox-team'])
        self.assertEqual(resp.data['runtime_reconcile']['matched_agents'], 1)
        configure.assert_called_once_with(
            'rt-nagi',
            {
                'skills': ['heartbeat-protocol', 'new-base', 'parent-skill'],
                'eager_skills': ['heartbeat-protocol'],
                'enabled_mcp_server_ids': ['admin', 'connect', 'id', 'pulse', 'wiki'],
            },
        )

    def test_capability_disable_reconciles_agents_receiving_capability_through_requires(self):
        base = CapabilityDefinition.objects.create(
            slug='base-runtime',
            display_name='Base Runtime',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            skill_refs=['old-base'],
        )
        parent = CapabilityDefinition.objects.create(
            slug='engineering-runtime',
            display_name='Engineering Runtime',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            skill_refs=['parent-skill'],
            requires=['base-runtime'],
        )
        TeamRequiredCapability.objects.create(team_slug='sandbox-team', capability=parent, enabled=True, required=True)
        agent = {
            'id': 'rt-nagi',
            'agent_username': 'nagi',
            'email': 'nagi@shizuha.com',
            'skills': ['old-base', 'parent-skill'],
            'eager_skills': [],
            'enabled_mcp_server_ids': [],
        }
        req = self.factory.post('/v1/fleet/capabilities/base-runtime/disable/', {'reason': 'disable dependency'}, format='json')
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.list_runtime_agents', return_value=[agent]), \
                patch('fleet.views.get_runtime_agent', return_value=agent), \
                patch('fleet.views._pulse_roster_team_index', return_value={'nagi@shizuha.com': [{'team_slug': 'sandbox-team'}]}), \
                patch('fleet.views._pulse_roster_memberships', return_value=({'sandbox-team'}, {'sandbox-team'}, None)), \
                patch('fleet.views.configure_runtime_agent', return_value=(True, '')) as configure:
            resp = _OpenCapabilityActionView.as_view()(req, slug=base.slug, action='disable')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['runtime_reconcile']['team_slugs'], ['sandbox-team'])
        self.assertEqual(resp.data['runtime_reconcile']['matched_agents'], 1)
        configure.assert_called_once_with(
            'rt-nagi',
            {
                'skills': ['heartbeat-protocol', 'parent-skill'],
                'eager_skills': ['heartbeat-protocol'],
                'enabled_mcp_server_ids': ['admin', 'connect', 'id', 'pulse', 'wiki'],
            },
        )

    def test_capability_disable_removes_runtime_grants_from_affected_agents(self):
        cap = CapabilityDefinition.objects.create(
            slug='removable-runtime',
            display_name='Removable Runtime',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            skill_refs=['temporary-skill'],
            mcp_server_refs=['temporary-mcp'],
        )
        TeamRequiredCapability.objects.create(team_slug='sandbox-team', capability=cap, enabled=True, required=True)
        agent = {
            'id': 'rt-nagi',
            'agent_username': 'nagi',
            'email': 'nagi@shizuha.com',
            'skills': ['temporary-skill'],
            'eager_skills': [],
            'enabled_mcp_server_ids': ['temporary-mcp'],
        }
        req = self.factory.post('/v1/fleet/capabilities/removable-runtime/disable/', {'reason': 'disable test'}, format='json')
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.list_runtime_agents', return_value=[agent]), \
                patch('fleet.views.get_runtime_agent', return_value=agent), \
                patch('fleet.views._pulse_roster_team_index', return_value={'nagi@shizuha.com': [{'team_slug': 'sandbox-team'}]}), \
                patch('fleet.views._pulse_roster_memberships', return_value=({'sandbox-team'}, {'sandbox-team'}, None)), \
                patch('fleet.views.configure_runtime_agent', return_value=(True, '')) as configure:
            resp = _OpenCapabilityActionView.as_view()(req, slug='removable-runtime', action='disable')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['activation_status'], CapabilityDefinition.ActivationStatus.DISABLED)
        configure.assert_called_once_with(
            'rt-nagi',
            {
                'skills': ['heartbeat-protocol'],
                'eager_skills': ['heartbeat-protocol'],
                'enabled_mcp_server_ids': ['admin', 'connect', 'id', 'pulse', 'wiki'],
            },
        )

    def test_capability_reconcile_deduplicates_multi_team_agent_union(self):
        cap = CapabilityDefinition.objects.create(
            slug='shared-runtime',
            display_name='Shared Runtime',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            skill_refs=['shared-skill'],
        )
        TeamRequiredCapability.objects.create(team_slug='sandbox-team', capability=cap, enabled=True, required=True)
        TeamRequiredCapability.objects.create(team_slug='sandbox-alt', capability=cap, enabled=True, required=True)
        agent = {
            'id': 'rt-nagi',
            'agent_username': 'nagi',
            'email': 'nagi@shizuha.com',
            'skills': [],
            'eager_skills': [],
            'enabled_mcp_server_ids': [],
        }
        req = self.factory.post('/v1/fleet/capabilities/shared-runtime/reconcile/', {'reason': 'manual'}, format='json')
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.list_runtime_agents', return_value=[agent]), \
                patch('fleet.views.get_runtime_agent', return_value=agent), \
                patch('fleet.views._pulse_roster_team_index', return_value={'nagi@shizuha.com': [{'team_slug': 'sandbox-team'}, {'team_slug': 'sandbox-alt'}]}), \
                patch('fleet.views._pulse_roster_memberships', return_value=({'sandbox-team', 'sandbox-alt'}, {'sandbox-team', 'sandbox-alt'}, None)), \
                patch('fleet.views.configure_runtime_agent', return_value=(True, '')) as configure:
            resp = _OpenCapabilityActionView.as_view()(req, slug='shared-runtime', action='reconcile')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['runtime_reconcile']['team_slugs'], ['sandbox-alt', 'sandbox-team'])
        self.assertEqual(resp.data['runtime_reconcile']['matched_agents'], 1)
        configure.assert_called_once_with(
            'rt-nagi',
            {
                'skills': ['heartbeat-protocol', 'shared-skill'],
                'eager_skills': ['heartbeat-protocol'],
                'enabled_mcp_server_ids': ['admin', 'connect', 'id', 'pulse', 'wiki'],
            },
        )

    def test_manual_reconcile_surfaces_daemon_failure_diagnostics(self):
        cap = CapabilityDefinition.objects.create(
            slug='failing-runtime',
            display_name='Failing Runtime',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            skill_refs=['wanted-skill'],
        )
        TeamRequiredCapability.objects.create(team_slug='sandbox-team', capability=cap, enabled=True, required=True)
        agent = {
            'id': 'rt-nagi',
            'agent_username': 'nagi',
            'email': 'nagi@shizuha.com',
            'skills': [],
            'eager_skills': [],
            'enabled_mcp_server_ids': [],
        }
        req = self.factory.post('/v1/fleet/capabilities/failing-runtime/reconcile/', {'reason': 'manual'}, format='json')
        with patch('fleet.views._require_staff', return_value=None), \
                patch('fleet.views.list_runtime_agents', return_value=[agent]), \
                patch('fleet.views.get_runtime_agent', return_value=agent), \
                patch('fleet.views._pulse_roster_team_index', return_value={'nagi@shizuha.com': [{'team_slug': 'sandbox-team'}]}), \
                patch('fleet.views._pulse_roster_memberships', return_value=({'sandbox-team'}, {'sandbox-team'}, None)), \
                patch('fleet.views.configure_runtime_agent', return_value=(False, 'daemon unavailable')):
            resp = _OpenCapabilityActionView.as_view()(req, slug='failing-runtime', action='reconcile')
        self.assertEqual(resp.status_code, 207)
        self.assertEqual(resp.data['runtime_reconcile']['errors'][0]['error'], 'daemon unavailable')
        audit = CapabilityAuditEvent.objects.get(target_key='failing-runtime', action='capability.reconcile')
        self.assertEqual(audit.runtime_reconcile_outcome, 'runtime-reconcile-partial')
        self.assertIn('daemon unavailable', audit.diagnostic)

    @patch('fleet.views.list_runtime_agents', return_value=[{'mcp_servers': [{'slug': 'pulse'}, {'slug': 'wiki'}]}])
    @patch('fleet.views.get_runtime_skills', return_value=['engineering', 'devops'])
    def test_catalog_patch_edits_mapping_fields_and_audits_reason(self, _skills, _agents):
        CapabilityDefinition.objects.create(
            slug='bundle-editor',
            display_name='Bundle Editor',
            risk_tier=CapabilityDefinition.RiskTier.STANDARD,
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
        )
        CapabilityDefinition.objects.create(
            slug='base-capability',
            display_name='Base Capability',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
        )
        req = self.factory.patch(
            '/v1/fleet/capabilities/bundle-editor/',
            {
                'display_name': 'Updated Bundle Editor',
                'description': 'Edited through the Hive Teams capability drawer.',
                'skill_refs': ['engineering'],
                'eager_skill_refs': ['devops'],
                'mcp_server_refs': ['pulse'],
                'runtime_flags': {'can_reconcile': True},
                'default_credential_grant_scopes': ['pulse:read'],
                'requires': ['base-capability'],
                'reason': 'HIVE-306 mapping edit test',
            },
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenCapabilityDetailView.as_view()(req, slug='bundle-editor')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['skill_refs'], ['engineering'])
        self.assertEqual(resp.data['eager_skill_refs'], ['devops'])
        self.assertEqual(resp.data['mcp_server_refs'], ['pulse'])
        self.assertEqual(resp.data['runtime_flags'], {'can_reconcile': True})
        self.assertEqual(resp.data['default_credential_grant_scopes'], ['pulse:read'])
        self.assertEqual(resp.data['requires'], ['base-capability'])

        cap = CapabilityDefinition.objects.get(slug='bundle-editor')
        self.assertEqual(cap.version, 2)
        from fleet.models import CapabilityAuditEvent
        audit = CapabilityAuditEvent.objects.get(target_key='bundle-editor', action='capability.update')
        self.assertEqual(audit.reason, 'HIVE-306 mapping edit test')
        self.assertEqual(audit.after['mcp_server_refs'], ['pulse'])

    @patch('fleet.views.list_runtime_agents', return_value=[{'mcp_servers': [{'slug': 'pulse'}, {'slug': 'wiki'}]}])
    @patch('fleet.views.get_runtime_skills', return_value=['engineering', 'devops'])
    def test_catalog_patch_demotes_active_sensitive_mapping_change_for_review(self, _skills, _agents):
        reviewed_at = timezone.now()
        CapabilityDefinition.objects.create(
            slug='sensitive-bundle-editor',
            display_name='Sensitive Bundle Editor',
            risk_tier=CapabilityDefinition.RiskTier.SENSITIVE,
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            skill_refs=['engineering'],
            mcp_server_refs=['pulse'],
            approved_by='akira@shizuha.com',
            approved_at=reviewed_at,
            security_reviewed_by='akira@shizuha.com',
            security_reviewed_at=reviewed_at,
        )
        req = self.factory.patch(
            '/v1/fleet/capabilities/sensitive-bundle-editor/',
            {
                'skill_refs': ['engineering', 'devops'],
                'mcp_server_refs': ['pulse', 'wiki'],
                'reason': 'HIVE-306 sensitive mapping edit requires fresh review',
            },
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenCapabilityDetailView.as_view()(req, slug='sensitive-bundle-editor')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['activation_status'], CapabilityDefinition.ActivationStatus.PENDING_SECURITY)
        self.assertEqual(resp.data['approved_by'], '')
        self.assertIsNone(resp.data['approved_at'])
        self.assertEqual(resp.data['security_reviewed_by'], '')
        self.assertIsNone(resp.data['security_reviewed_at'])
        self.assertEqual(resp.data['skill_refs'], ['engineering', 'devops'])
        self.assertEqual(resp.data['mcp_server_refs'], ['pulse', 'wiki'])

        cap = CapabilityDefinition.objects.get(slug='sensitive-bundle-editor')
        self.assertEqual(cap.activation_status, CapabilityDefinition.ActivationStatus.PENDING_SECURITY)
        self.assertEqual(cap.approved_by, '')
        self.assertIsNone(cap.approved_at)
        self.assertEqual(cap.security_reviewed_by, '')
        self.assertIsNone(cap.security_reviewed_at)
        from fleet.models import CapabilityAuditEvent
        audit = CapabilityAuditEvent.objects.get(target_key='sensitive-bundle-editor', action='capability.update')
        self.assertEqual(audit.runtime_reconcile_outcome, 'security_review_required')

    @patch('fleet.views.list_runtime_agents', return_value=[{'mcp_servers': [{'slug': 'pulse'}]}])
    @patch('fleet.views.get_runtime_skills', return_value=['engineering'])
    def test_catalog_patch_rejects_unknown_mapping_refs(self, _skills, _agents):
        CapabilityDefinition.objects.create(
            slug='invalid-ref-target',
            display_name='Invalid Ref Target',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
        )
        req = self.factory.patch(
            '/v1/fleet/capabilities/invalid-ref-target/',
            {
                'skill_refs': ['missing-skill'],
                'mcp_server_refs': ['missing-mcp'],
            },
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenCapabilityDetailView.as_view()(req, slug='invalid-ref-target')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('skill_refs', resp.data)
        self.assertIn('mcp_server_refs', resp.data)

    @patch('fleet.views.list_runtime_agents', return_value=[{'mcp_servers': [{'slug': 'pulse'}]}])
    @patch('fleet.views.get_runtime_skills', return_value=['engineering'])
    def test_effective_set_applies_base_platform_mcp_floor_with_diagnostic(self, _skills, _agents):
        """PLAT-1251 point 1: a capability row stripped to a partial MCP list can
        never produce an effective set below the base platform contract."""
        cap = CapabilityDefinition.objects.create(
            slug='pulse-only-capability',
            display_name='Pulse Only Capability',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            mcp_server_refs=['pulse'],
        )
        TeamRequiredCapability.objects.create(team_slug='sandbox-floor', capability=cap)
        agent = FleetAgent.objects.create(agent_username='ryo', agent_id='ryo-id')
        req = self.factory.get(f'/v1/fleet/agents/{agent.id}/effective-capabilities/')
        with patch('fleet.views._require_staff', return_value=None):
            with patch('fleet.views._pulse_roster_memberships', return_value=({'sandbox-floor'}, {'sandbox-floor'}, None)):
                resp = _OpenEffectiveCapabilitiesView.as_view()(req, agent_id=str(agent.id))
        self.assertEqual(resp.status_code, 200)
        base = {'pulse', 'connect', 'wiki', 'admin', 'id'}
        self.assertTrue(base.issubset(set(resp.data['enabled_mcp_servers'])))
        self.assertEqual(resp.data['base_mcp_missing_pre_floor'], ['admin', 'connect', 'id', 'wiki'])
        self.assertTrue(
            any('base-MCP floor' in d.get('message', '') and d.get('level') == 'error'
                for d in resp.data['diagnostics']),
            resp.data['diagnostics'],
        )

    def test_reconcile_never_pushes_mcp_missing_base_and_notifies_devops(self):
        """PLAT-1251 point 2: the reconcile inherits the floor (never pushes a set
        missing base servers) and fail-loud-notifies on the violating data."""
        from fleet.views import _runtime_capability_reconcile_for_agent_emails

        cap = CapabilityDefinition.objects.create(
            slug='pulse-only-reconcile-capability',
            display_name='Pulse Only Reconcile Capability',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            mcp_server_refs=['pulse'],
        )
        TeamRequiredCapability.objects.create(team_slug='sandbox-floor', capability=cap)
        agent = FleetAgent.objects.create(agent_username='ryo', agent_id='ryo-id')
        runtime_agent = {
            'id': str(agent.id),
            'agent_username': 'ryo',
            'enabled_mcp_server_ids': ['pulse'],
            'skills': [],
            'eager_skills': [],
        }
        pushed = {}

        def fake_configure(agent_id, updates):
            pushed.update(updates)
            return True, ''

        req = self.factory.get('/v1/fleet/reconcile-test/')
        with patch('fleet.views.list_runtime_agents', return_value=[runtime_agent]), \
                patch('fleet.views.configure_runtime_agent', side_effect=fake_configure), \
                patch('fleet.views._pulse_roster_memberships', return_value=({'sandbox-floor'}, {'sandbox-floor'}, None)):
            result = _runtime_capability_reconcile_for_agent_emails(req, ['ryo@shizuha.com'])

        base = {'pulse', 'connect', 'wiki', 'admin', 'id'}
        self.assertIn('enabled_mcp_server_ids', pushed)
        self.assertTrue(base.issubset(set(pushed['enabled_mcp_server_ids'])), pushed)
        self.assertEqual(result['matched_agents'], 1)
        self.assertEqual(result['updated_agents'], 1)
        self.assertEqual(len(result['base_mcp_floor_violations']), 1)
        violation = result['base_mcp_floor_violations'][0]
        self.assertEqual(violation['email'], 'ryo@shizuha.com')
        self.assertEqual(violation['missing'], ['admin', 'connect', 'id', 'wiki'])
        self.notify_mock.assert_called_once_with('ryo@shizuha.com', ['admin', 'connect', 'id', 'wiki'])
