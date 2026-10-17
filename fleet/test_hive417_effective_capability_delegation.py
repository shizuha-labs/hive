"""
HIVE-417: views._effective_capability_set delegates to the single source of truth
compute_effective_capability_grant, while keeping the HTTP payload byte-for-byte
stable.

Regression guard for the security-sensitive capability path: it pins the exact
historical response shape (no harness-only additive keys leak into the HTTP
payload) AND proves the view's computed fields equal the pure engine's output for
the same (team_slugs, identifiers).
"""
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from hive_platform.models import BillingPlan, TenantSubscription
from fleet.effective_capabilities import compute_effective_capability_grant
from fleet.models import (
    CapabilityDefinition,
    FleetAgent,
    FleetAgentTeamCredentialEligibility,
    HiveCredential,
    TeamMembershipRole,
    TeamRequiredCapability,
    ensure_team_required_capability,
)
from fleet.runtime_fleet import _RUNTIME_CACHE, normalize_runtime_agent
from fleet.views import (
    _OrganizationMembershipSet,
    _UNKNOWN_CUSTOMER_TENANT_SCOPE,
    _effective_agent_identity,
    _effective_capability_set,
)


# The exact set of top-level keys this endpoint has always returned, plus the
# HIVE-2695 listing-mint token-scope parity stamp (scope_gap/scope_gap_detail):
# the stamp is part of the minted listing by design, so EVERY serve surface —
# including this signed daemon payload — carries the explicit flag (see the
# HIVE-2695 stamp block in fleet.views._effective_capability_set). The harness
# module compute_effective_capability_grant additionally surfaces 'team_slugs'
# and a top-level 'sensitive_access_flags'; those must NOT leak into the HTTP payload.
HISTORICAL_PAYLOAD_KEYS = {
    'agent', 'source_teams', 'display_title', 'roles_by_team', 'capabilities',
    'agent_overrides', 'skills', 'eager_skills', 'enabled_mcp_servers',
    'base_mcp_missing_pre_floor', 'inline_profiles', 'runtime_flags',
    'credential_grant_scopes', 'diagnostics', 'computed_at', 'catalog_version',
    'definition_hashes', 'team_credential_eligible_teams', 'credential_materializations',
    'source_team_memberships', 'team_credential_eligible_memberships',
    # HIVE-2695 token-scope parity stamp (additive, deterministic shape).
    'scope_gap', 'scope_gap_detail',
}
HISTORICAL_CAPABILITY_KEYS = {
    'slug', 'display_name', 'risk_tier', 'version', 'definition_hash', 'sources',
}


class Hive417DelegationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.agent = FleetAgent.objects.create(
            agent_username='hive417-eng', agent_id='hive417-eng-id', role='Engineer',
        )
        # Seed a representative multi-team, mixed-risk scenario.
        ensure_team_required_capability('engineering', actor='test')
        ensure_team_required_capability('devops', actor='test')  # privileged: sensitive flags + kubeconfig scope
        # A custom capability exercising the eager/profile/runtime-flag fields.
        custom = CapabilityDefinition.objects.create(
            slug='hive417-custom',
            display_name='HIVE417 Custom',
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
            skill_refs=['custom-skill'],
            eager_skill_refs=['custom-eager'],
            inline_profile_refs=['custom-profile'],
            mcp_server_refs=['pulse'],
            runtime_flags={'custom_flag': True},
            created_by='test', updated_by='test', approved_by='test',
        )
        TeamRequiredCapability.objects.update_or_create(
            team_slug='engineering', capability=custom,
            defaults={'required': True, 'enabled': True, 'reason': 'test', 'created_by': 'test'},
        )
        TeamMembershipRole.objects.create(
            agent_identifier='hive417-eng@shizuha.com', team_slug='engineering',
            role_title='Backend', membership_role=TeamMembershipRole.MembershipRole.LEAD,
        )
        self.team_slugs = ['devops', 'engineering']

    def _call_view(self):
        request = self.factory.get('/v1/fleet/agents/x/effective-capabilities/')
        membership_rows = getattr(self, 'membership_rows_override', None) or [
            {'organization_slug': 'shizuha', 'team_slug': team_slug}
            for team_slug in self.team_slugs
        ]
        memberships = _OrganizationMembershipSet(
            {row['team_slug'] for row in membership_rows},
            organization_rows=membership_rows,
        )
        with patch(
            'fleet.views._pulse_roster_memberships',
            return_value=(memberships, memberships, None),
        ):
            payload, error = _effective_capability_set(request, str(self.agent.id))
        self.assertIsNone(error)
        self.assertIsNotNone(payload)
        return payload

    def test_http_payload_key_shape_unchanged(self):
        """Payload exposes the historical keys plus the signed credential contract."""
        payload = self._call_view()
        self.assertEqual(set(payload.keys()), HISTORICAL_PAYLOAD_KEYS)
        # The two additive keys the pure engine adds must never reach the HTTP payload.
        self.assertNotIn('sensitive_access_flags', payload)
        self.assertNotIn('team_slugs', payload)

    def test_team_credential_requires_membership_and_exact_team_eligibility(self):
        qa_grant = HiveCredential.objects.create(
            scope=HiveCredential.Scope.TEAM,
            organization_slug='shizuha',
            team_slug='qa',
            provider=HiveCredential.Provider.GENERIC_ENV,
            purpose='QA_ISOLATION_A_USERNAME',
            secret_ref='k8s-secret://shizuha-fleet/qa-isolation-fixtures#QA_ISOLATION_A_USERNAME',
            # The signed read model enforces explicit seat eligibility for all
            # team materializations, even if a legacy grant row disabled its
            # older per-grant advisory flag.
            requires_team_credential_eligible=False,
        )
        review_grant = HiveCredential.objects.create(
            scope=HiveCredential.Scope.TEAM,
            organization_slug='shizuha',
            team_slug='review',
            provider=HiveCredential.Provider.GENERIC_ENV,
            purpose='REVIEW_DESTRUCTIVE_TOKEN',
            secret_ref='k8s-secret://shizuha-fleet/review-secrets#REVIEW_DESTRUCTIVE_TOKEN',
        )
        self.team_slugs = ['qa', 'review']

        ineligible = self._call_view()
        self.assertEqual(ineligible['team_credential_eligible_teams'], [])
        self.assertEqual(ineligible['credential_materializations'], [])

        eligibility = FleetAgentTeamCredentialEligibility.objects.create(
            agent=self.agent,
            organization_slug='shizuha',
            team_slug='qa',
            is_active=True,
            actor_user_id='test',
        )
        eligible = self._call_view()
        self.assertEqual(eligible['team_credential_eligible_teams'], ['qa'])
        self.assertEqual(eligible['team_credential_eligible_memberships'], [{
            'organization_slug': 'shizuha',
            'team_slug': 'qa',
        }])
        self.assertEqual(eligible['credential_materializations'], [{
            'grant_id': str(qa_grant.id),
            'scope': 'team',
            'organization_slug': 'shizuha',
            'team_slug': 'qa',
            'provider': 'generic-env',
            'purpose': 'QA_ISOLATION_A_USERNAME',
            'secret_ref': 'k8s-secret://shizuha-fleet/qa-isolation-fixtures#QA_ISOLATION_A_USERNAME',
            'is_active': True,
        }])
        self.assertNotIn(
            str(review_grant.id),
            {row['grant_id'] for row in eligible['credential_materializations']},
        )

        self.team_slugs = ['engineering']
        self.assertEqual(self._call_view()['credential_materializations'], [])

        self.team_slugs = ['qa', 'review']
        eligibility.is_active = False
        eligibility.save(update_fields=['is_active', 'updated_at'])
        self.assertEqual(self._call_view()['credential_materializations'], [])

        eligibility.is_active = True
        eligibility.save(update_fields=['is_active', 'updated_at'])
        qa_grant.is_active = False
        qa_grant.revoked_at = timezone.now()
        qa_grant.save(update_fields=['is_active', 'revoked_at', 'updated_at'])
        self.assertEqual(self._call_view()['credential_materializations'], [])

    def test_customer_qa_membership_cannot_satisfy_shizuha_qa_credential_gate(self):
        """Caller-boundary regression: org-local team-name collisions fail closed."""
        internal_grant = HiveCredential.objects.create(
            scope=HiveCredential.Scope.TEAM,
            organization_slug='shizuha',
            team_slug='qa',
            provider=HiveCredential.Provider.GENERIC_ENV,
            purpose='QA_ISOLATION_A_PASSWORD',
            secret_ref='k8s-secret://shizuha-fleet/qa-isolation-fixtures#QA_ISOLATION_A_PASSWORD',
        )
        FleetAgentTeamCredentialEligibility.objects.create(
            agent=self.agent,
            organization_slug='shizuha',
            team_slug='qa',
            is_active=True,
            actor_user_id='test',
        )

        self.team_slugs = ['qa']
        self.membership_rows_override = [{
            'organization_slug': 'customer-x',
            'team_slug': 'qa',
        }]
        customer_only = self._call_view()
        self.assertEqual(customer_only['source_teams'], ['qa'])
        self.assertEqual(customer_only['source_team_memberships'], [{
            'organization_slug': 'customer-x',
            'team_slug': 'qa',
        }])
        self.assertEqual(customer_only['team_credential_eligible_memberships'], [])
        self.assertEqual(customer_only['credential_materializations'], [])

        self.membership_rows_override = [{
            'organization_slug': 'shizuha',
            'team_slug': 'qa',
        }]
        internal = self._call_view()
        self.assertEqual(
            [row['grant_id'] for row in internal['credential_materializations']],
            [str(internal_grant.id)],
        )

        self.membership_rows_override = [{
            'organization_slug': 'customer-x',
            'team_slug': 'qa',
        }]
        after_internal_leave = self._call_view()
        self.assertEqual(after_internal_leave['credential_materializations'], [])

    def test_capability_entries_have_no_sensitive_flags_key(self):
        """Per-capability HTTP entries keep the historical 6 keys (no sensitive_access_flags)."""
        payload = self._call_view()
        self.assertTrue(payload['capabilities'], 'expected capabilities in the grant')
        for entry in payload['capabilities']:
            self.assertEqual(set(entry.keys()), HISTORICAL_CAPABILITY_KEYS)
            self.assertNotIn('sensitive_access_flags', entry)

    def test_view_delegates_to_pure_engine(self):
        """Every computed field equals compute_effective_capability_grant's output."""
        payload = self._call_view()
        agent, _ = _effective_agent_identity(str(self.agent.id))
        identifiers = {agent['id'], agent['username'], agent['email']}
        grant = compute_effective_capability_grant(self.team_slugs, identifiers)

        # Request-scoped shaping.
        self.assertEqual(payload['source_teams'], grant['team_slugs'])
        self.assertEqual(payload['source_teams'], self.team_slugs)

        # Delegated computation must match field-for-field (computed_at excluded — it
        # is a fresh timestamp on each call).
        for field in (
            'skills', 'eager_skills', 'enabled_mcp_servers', 'base_mcp_missing_pre_floor',
            'inline_profiles', 'runtime_flags', 'credential_grant_scopes',
            'agent_overrides', 'diagnostics', 'catalog_version', 'definition_hashes',
        ):
            self.assertEqual(payload[field], grant[field], f'field {field!r} diverged from the engine')

        # Capabilities match once the additive per-entry key is projected away.
        projected = [
            {k: c[k] for k in HISTORICAL_CAPABILITY_KEYS} for c in grant['capabilities']
        ]
        self.assertEqual(payload['capabilities'], projected)

    def test_representative_values_flow_through(self):
        """Sanity: real skills/MCP-floor/scopes/custom-fields all arrive via delegation."""
        payload = self._call_view()
        self.assertIn('engineering-core', payload['skills'])
        self.assertIn('custom-skill', payload['skills'])
        self.assertIn('heartbeat-protocol', payload['skills'])
        self.assertIn('custom-eager', payload['eager_skills'])
        self.assertIn('heartbeat-protocol', payload['eager_skills'])
        self.assertIn('custom-profile', payload['inline_profiles'])
        self.assertEqual(payload['runtime_flags'].get('custom_flag'), True)
        # devops (privileged) contributes the kubeconfig credential grant scope.
        self.assertIn('kubeconfig', payload['credential_grant_scopes'])
        # PLAT-1251 base MCP floor is always present.
        for ref in ('pulse', 'connect', 'wiki', 'admin', 'id'):
            self.assertIn(ref, payload['enabled_mcp_servers'])

    def test_runtime_normalizer_preserves_server_resolved_tenant_scope(self):
        """Daemon-provided org fields must survive normalization for HIVE-446 gating."""
        normalized = normalize_runtime_agent({
            'id': 'rt-cust',
            'username': 'cust-devops',
            'role': 'DevOps',
            'enabled': True,
            'status': 'running',
            'organization_slug': 'cust-acme',
            'tenant_org_slug': 'cust-acme',
        })

        self.assertEqual(normalized['organization_slug'], 'cust-acme')
        self.assertEqual(normalized['tenant_org_slug'], 'cust-acme')
        self.assertTrue(normalized['email_synthesized'])

    @override_settings(RUNTIME_FLEET_API_URL='http://runtime-fleet.test', RUNTIME_FLEET_API_TIMEOUT=0.1)
    def test_runtime_daemon_tenant_slug_survives_get_runtime_agent_path(self):
        """Regression for Ren's review: exercise get_runtime_agent -> normalize path."""
        tenant = 'cust-normalized'
        plan = BillingPlan.objects.create(plan_id='plan-cust-normalized', name='Customer plan')
        TenantSubscription.objects.create(
            tenant_org_id='org-cust-normalized',
            tenant_org_slug=tenant,
            plan=plan,
            status=TenantSubscription.Status.ACTIVE,
        )
        _RUNTIME_CACHE.clear()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'agents': [{
            'id': 'rt-cust-normalized',
            'username': 'cust-devops',
            'role': 'DevOps',
            'enabled': True,
            'status': 'running',
            'organization_slug': tenant,
            'tenant_org_slug': tenant,
        }]}
        request = self.factory.get('/v1/fleet/agents/rt-cust-normalized/effective-capabilities/')
        with patch('fleet.runtime_fleet.requests.get', return_value=response), patch(
            'fleet.views._pulse_roster_memberships',
            return_value=({'devops'}, {'devops'}, None),
        ):
            payload, error = _effective_capability_set(request, 'rt-cust-normalized')

        self.assertIsNone(error)
        self.assertEqual(payload['credential_grant_scopes'], [])
        self.assertTrue(payload['enabled_mcp_servers'])
        self.assertTrue(all(ref.endswith(f':{tenant}') for ref in payload['enabled_mcp_servers']))
        self.assertIn(f'pulse:{tenant}', payload['enabled_mcp_servers'])

    @override_settings(RUNTIME_FLEET_API_URL='http://runtime-fleet.test', RUNTIME_FLEET_API_TIMEOUT=0.1)
    def test_runtime_daemon_missing_tenant_with_synthesized_email_defaults_deny(self):
        """A synthesized @shizuha.com email must not make an unscoped runtime agent internal."""
        _RUNTIME_CACHE.clear()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'agents': [{
            'id': 'rt-cust-unscoped',
            'username': 'cust-devops',
            'role': 'DevOps',
            'enabled': True,
            'status': 'running',
        }]}
        request = self.factory.get('/v1/fleet/agents/rt-cust-unscoped/effective-capabilities/')
        with patch('fleet.runtime_fleet.requests.get', return_value=response), patch(
            'fleet.views._pulse_roster_memberships',
            return_value=({'devops'}, {'devops'}, None),
        ):
            payload, error = _effective_capability_set(request, 'rt-cust-unscoped')

        self.assertIsNone(error)
        self.assertEqual(payload['credential_grant_scopes'], [])
        self.assertTrue(payload['enabled_mcp_servers'])
        self.assertTrue(
            all(ref.endswith(f':{_UNKNOWN_CUSTOMER_TENANT_SCOPE}') for ref in payload['enabled_mcp_servers'])
        )

    def test_missing_query_org_uses_server_resolved_agent_tenant(self):
        """A customer runtime agent cannot bypass gating by omitting org_slug."""
        tenant = 'cust-hive417'
        plan = BillingPlan.objects.create(plan_id='plan-cust-hive417', name='Customer plan')
        TenantSubscription.objects.create(
            tenant_org_id='org-cust-hive417',
            tenant_org_slug=tenant,
            plan=plan,
            status=TenantSubscription.Status.ACTIVE,
        )
        request = self.factory.get('/v1/fleet/agents/runtime-customer/effective-capabilities/')
        agent = {
            'id': 'runtime-customer',
            'username': 'cust-devops',
            'email': 'cust-devops@example.com',
            'role': 'DevOps',
            'display_title': 'DevOps',
            'source': 'runtime',
            'organization_slug': tenant,
        }
        with patch('fleet.views._effective_agent_identity', return_value=(agent, None)), patch(
            'fleet.views._pulse_roster_memberships',
            return_value=({'devops'}, {'devops'}, None),
        ):
            payload, error = _effective_capability_set(request, 'runtime-customer')

        self.assertIsNone(error)
        self.assertEqual(payload['credential_grant_scopes'], [])
        self.assertNotIn('kubeconfig', payload['credential_grant_scopes'])
        self.assertTrue(all(ref.endswith(f':{tenant}') for ref in payload['enabled_mcp_servers']))

    def test_ambiguous_non_internal_runtime_agent_defaults_deny(self):
        """Missing tenant context for a non-internal runtime identity fails closed."""
        request = self.factory.get('/v1/fleet/agents/runtime-customer/effective-capabilities/')
        agent = {
            'id': 'runtime-customer',
            'username': 'cust-devops',
            'email': 'cust-devops@example.com',
            'role': 'DevOps',
            'display_title': 'DevOps',
            'source': 'runtime',
        }
        with patch('fleet.views._effective_agent_identity', return_value=(agent, None)), patch(
            'fleet.views._pulse_roster_memberships',
            return_value=({'devops'}, {'devops'}, None),
        ):
            payload, error = _effective_capability_set(request, 'runtime-customer')

        self.assertIsNone(error)
        self.assertEqual(payload['credential_grant_scopes'], [])
        self.assertTrue(all(ref.endswith(f':{_UNKNOWN_CUSTOMER_TENANT_SCOPE}') for ref in payload['enabled_mcp_servers']))

    def test_org_scoped_request_tenant_scopes_mcp_refs(self):
        """An org-scoped customer request gets tenant-namespaced MCP refs; default internal path stays bare."""
        tenant = 'acme-pilot'
        request = self.factory.get('/v1/fleet/agents/x/effective-capabilities/', {'org_slug': tenant})
        with patch(
            'fleet.views._pulse_roster_memberships',
            return_value=(set(self.team_slugs), set(self.team_slugs), None),
        ):
            payload, error = _effective_capability_set(request, str(self.agent.id))
        self.assertIsNone(error)
        self.assertTrue(payload['enabled_mcp_servers'])
        self.assertTrue(all(ref.endswith(f':{tenant}') for ref in payload['enabled_mcp_servers']))
        self.assertIn(f'pulse:{tenant}', payload['enabled_mcp_servers'])
        self.assertNotIn('mcp_ref_scope', payload, 'tenant scope remains encoded in refs, not a new HTTP key')
