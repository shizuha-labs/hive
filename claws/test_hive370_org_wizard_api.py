from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from unittest import mock

from claws import provisioning_adapters as adapters
from claws.models import TenantProvisioningRun


class Hive370OrgWizardApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username='owner', email='owner@example.com')
        self.other = User.objects.create_user(username='other', email='other@example.com')
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        # Bridge determinism (HIVE-2715): these cases pin the PRE-2715 bridge
        # semantics — a real run parks at the first un-adaptered side-effectful
        # step (id_org). The saga's own adapter coverage lives in
        # test_hive320_tenant_provisioning; clearing the registry here keeps
        # these cases independent of admin reachability and signing config.
        self._registry_patcher = mock.patch.dict(adapters.SAGA_ADAPTERS, {}, clear=True)
        self._registry_patcher.start()
        self.addCleanup(self._registry_patcher.stop)

    def test_template_catalog_lists_builtin_cards_without_seed_command(self):
        response = self.client.get('/hive/api/v1/org-templates/')

        self.assertEqual(response.status_code, 200)
        slugs = {item['slug'] for item in response.data['results']}
        self.assertIn('tech-company', slugs)
        tech = next(item for item in response.data['results'] if item['slug'] == 'tech-company')
        self.assertGreaterEqual(tech['team_count'], 8)
        self.assertGreaterEqual(tech['total_agent_count'], 10)
        self.assertTrue(tech['what_you_get'])
        self.assertTrue(tech['teams_preview'])

    def test_template_detail_resolves_customer_safe_plan_preview(self):
        response = self.client.get(
            '/hive/api/v1/org-templates/tech-company/',
            {'org_name': 'Acme AI', 'org_slug': 'acme-ai', 'plan_slug': 'team'},
        )

        self.assertEqual(response.status_code, 200)
        plan = response.data['template_plan']
        self.assertEqual(plan['context']['org_slug'], 'acme-ai')
        devops = next(team for team in plan['teams'] if team['slug'] == 'devops')
        self.assertEqual(devops['sensitive_access_flags'], [])
        self.assertIn('kubeconfig', devops['stripped_credential_grant_scopes'])
        actions = {(call['service'], call['action']) for call in plan['create_calls']}
        self.assertIn(('admin', 'create_organization'), actions)
        self.assertIn(('pulse', 'create_task'), actions)

    def test_create_dry_run_provisioning_run_uses_authenticated_owner_and_advances_preview(self):
        response = self.client.post(
            '/hive/api/v1/org-provisioning-runs/',
            {
                'org_name': 'Acme AI Ops',
                'template_slug': 'tech-company',
                'plan_slug': 'team',
                'dry_run': True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['org_slug'], 'acme-ai-ops')
        self.assertEqual(response.data['status'], TenantProvisioningRun.Status.READY)
        self.assertTrue(response.data['is_preview'])
        self.assertEqual(response.data['progress']['percent'], 100)
        self.assertTrue(response.data['dashboard_path'].endswith('org=acme-ai-ops'))
        self.assertEqual(response.data['resource_refs']['template_plan']['template']['slug'], 'tech-company')
        run = TenantProvisioningRun.objects.get(id=response.data['id'])
        self.assertEqual(run.requester_user_id, self.owner.id)
        self.assertEqual(run.requester_email, 'owner@example.com')

    def test_ready_run_exposes_hive372_onboarding_contract(self):
        response = self.client.post(
            '/hive/api/v1/org-provisioning-runs/',
            {
                'org_name': 'Acme AI Ops',
                'template_slug': 'tech-company',
                'plan_slug': 'team',
                'dry_run': True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        onboarding = response.data['onboarding']
        self.assertEqual(onboarding['version'], 'hive-372-v1')
        self.assertEqual(onboarding['template']['slug'], 'tech-company')
        self.assertGreaterEqual(onboarding['summary']['team_count'], 8)
        self.assertGreaterEqual(onboarding['summary']['agent_count'], 10)
        self.assertGreaterEqual(onboarding['summary']['starter_task_count'], 3)
        self.assertEqual(
            {surface['id'] for surface in onboarding['surfaces']},
            {'dashboard', 'hive', 'pulse', 'admin'},
        )
        first_task = onboarding['first_task_flow']
        self.assertIn('Define the first customer-visible product increment', first_task['title'])
        self.assertEqual(first_task['assignment_group'], 'product')
        self.assertEqual(
            first_task['href'],
            '/pulse/tasks?org=acme-ai-ops&onboarding=first-task'
            '&title=Define%20the%20first%20customer-visible%20product%20increment',
        )
        pulse_surface = next(surface for surface in onboarding['surfaces'] if surface['id'] == 'pulse')
        self.assertEqual(
            pulse_surface['href'],
            '/pulse/tasks?org=acme-ai-ops&onboarding=first-task',
        )
        self.assertEqual(len(first_task['steps']), 3)

    def test_real_run_stops_at_manual_review_until_side_effect_adapters_exist(self):
        response = self.client.post(
            '/hive/api/v1/org-provisioning-runs/',
            {
                'org_name': 'Real Tenant',
                'org_slug': 'real-tenant',
                'template_slug': 'solo-founder',
                'plan_slug': 'pilot',
                'dry_run': False,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], TenantProvisioningRun.Status.MANUAL_REVIEW)
        self.assertEqual(response.data['current_step'], 'id_org')
        id_step = next(step for step in response.data['steps'] if step['slug'] == 'id_org')
        self.assertEqual(id_step['status'], TenantProvisioningRun.StepStatus.MANUAL_REVIEW)
        self.assertIsNone(response.data['dashboard_path'])

    def test_saga_parked_run_deprovision_closes_the_run_terminal(self):
        """HIVE-2697 ruling 2: a manual_review-parked saga has a terminal
        cleanup path so golden-path teardown ends with zero qafixture litter."""
        created = self.client.post(
            '/hive/api/v1/org-provisioning-runs/',
            {
                'org_name': 'Parked Tenant',
                'org_slug': 'parked-tenant',
                'template_slug': 'solo-founder',
                'dry_run': False,
            },
            format='json',
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data['status'], TenantProvisioningRun.Status.MANUAL_REVIEW)

        deprovisioned = self.client.post(
            f"/hive/api/v1/org-provisioning-runs/{created.data['id']}/deprovision/",
        )

        self.assertEqual(deprovisioned.status_code, 200)
        self.assertEqual(deprovisioned.data['status'], TenantProvisioningRun.Status.TORN_DOWN)
        self.assertIn('run_deprovisioned', str(deprovisioned.data['audit_events']))
        # Idempotent: a second deprovision of the torn-down run is a no-op 200.
        replay = self.client.post(
            f"/hive/api/v1/org-provisioning-runs/{created.data['id']}/deprovision/",
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.data['status'], TenantProvisioningRun.Status.TORN_DOWN)

    def test_saga_parked_run_deprovision_is_owner_scoped(self):
        created = self.client.post(
            '/hive/api/v1/org-provisioning-runs/',
            {
                'org_name': 'Scoped Tenant',
                'org_slug': 'scoped-tenant',
                'template_slug': 'solo-founder',
                'dry_run': False,
            },
            format='json',
        )
        self.assertEqual(created.status_code, 201)

        other_client = APIClient()
        other_client.force_authenticate(user=self.other)
        denied = other_client.post(
            f"/hive/api/v1/org-provisioning-runs/{created.data['id']}/deprovision/",
        )

        self.assertEqual(denied.status_code, 404)
        run = TenantProvisioningRun.objects.get(id=created.data['id'])
        self.assertEqual(run.status, TenantProvisioningRun.Status.MANUAL_REVIEW)

    def test_deprovision_refuses_runs_that_are_not_saga_parked(self):
        """The path is for manual_review-parked sagas only — a ready (real)
        tenant must never be torn down through it."""
        created = self.client.post(
            '/hive/api/v1/org-provisioning-runs/',
            {
                'org_name': 'Preview Tenant',
                'org_slug': 'preview-tenant',
                'template_slug': 'solo-founder',
                'dry_run': True,
            },
            format='json',
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data['status'], TenantProvisioningRun.Status.READY)

        refused = self.client.post(
            f"/hive/api/v1/org-provisioning-runs/{created.data['id']}/deprovision/",
        )

        self.assertEqual(refused.status_code, 409)
        run = TenantProvisioningRun.objects.get(id=created.data['id'])
        self.assertEqual(run.status, TenantProvisioningRun.Status.READY)

    def test_provisioning_run_detail_is_owner_scoped(self):
        created = self.client.post(
            '/hive/api/v1/org-provisioning-runs/',
            {
                'org_name': 'Private Org',
                'template_slug': 'solo-founder',
                'dry_run': True,
            },
            format='json',
        )
        self.assertEqual(created.status_code, 201)

        other_client = APIClient()
        other_client.force_authenticate(user=self.other)
        denied = other_client.get(f"/hive/api/v1/org-provisioning-runs/{created.data['id']}/")

        self.assertEqual(denied.status_code, 404)

    def test_user_supplied_idempotency_key_replays_only_same_owner_and_same_request(self):
        payload = {
            'org_name': 'Replay Org',
            'template_slug': 'tech-company',
            'plan_slug': 'team',
            'dry_run': True,
            'idempotency_key': 'owner-safe-replay-key',
        }

        created = self.client.post('/hive/api/v1/org-provisioning-runs/', payload, format='json')
        replay = self.client.post('/hive/api/v1/org-provisioning-runs/', payload, format='json')

        self.assertEqual(created.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.data['id'], created.data['id'])
        self.assertEqual(TenantProvisioningRun.objects.filter(idempotency_key='owner-safe-replay-key').count(), 1)

    def test_idempotency_key_reuse_by_another_owner_is_rejected_without_leaking_run(self):
        created = self.client.post(
            '/hive/api/v1/org-provisioning-runs/',
            {
                'org_name': 'Alice Secret Org',
                'template_slug': 'tech-company',
                'plan_slug': 'team',
                'dry_run': True,
                'idempotency_key': 'shared-owner-leak-key',
            },
            format='json',
        )
        self.assertEqual(created.status_code, 201)

        other_client = APIClient()
        other_client.force_authenticate(user=self.other)
        denied = other_client.post(
            '/hive/api/v1/org-provisioning-runs/',
            {
                'org_name': 'Bob Org',
                'org_slug': 'bob-org',
                'template_slug': 'solo-founder',
                'plan_slug': 'pilot',
                'dry_run': True,
                'idempotency_key': 'shared-owner-leak-key',
            },
            format='json',
        )

        self.assertEqual(denied.status_code, 409)
        self.assertNotIn('id', denied.data)
        self.assertNotIn('org_slug', denied.data)
        self.assertNotIn('alice-secret-org', str(denied.data).lower())

    def test_same_owner_idempotency_key_reuse_with_different_params_is_rejected(self):
        created = self.client.post(
            '/hive/api/v1/org-provisioning-runs/',
            {
                'org_name': 'Immutable Org',
                'template_slug': 'tech-company',
                'plan_slug': 'team',
                'dry_run': True,
                'idempotency_key': 'same-owner-mismatch-key',
            },
            format='json',
        )
        self.assertEqual(created.status_code, 201)

        denied = self.client.post(
            '/hive/api/v1/org-provisioning-runs/',
            {
                'org_name': 'Different Org',
                'template_slug': 'solo-founder',
                'plan_slug': 'pilot',
                'dry_run': True,
                'idempotency_key': 'same-owner-mismatch-key',
            },
            format='json',
        )

        self.assertEqual(denied.status_code, 409)
        self.assertNotIn('org_slug', denied.data)
        self.assertEqual(TenantProvisioningRun.objects.filter(idempotency_key='same-owner-mismatch-key').count(), 1)
