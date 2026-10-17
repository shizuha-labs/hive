from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from django.test import TestCase
from claws.models import OrgTemplate, TenantProvisioningRun
from claws.org_templates import (
    TemplatePlanContext,
    builtin_template_definitions,
    resolve_org_template_plan,
    seed_builtin_org_templates,
)
from claws.tenant_provisioning import advance_preview, create_or_get_run


class OrgTemplateTests(TestCase):
    def test_seed_builtin_library_creates_versioned_active_templates(self):
        seeded = seed_builtin_org_templates(actor='test-suite')

        self.assertEqual(len(seeded), 4)
        self.assertTrue(OrgTemplate.objects.filter(slug='tech-company', version=1, status=OrgTemplate.Status.ACTIVE).exists())
        tech = OrgTemplate.objects.get(slug='tech-company', version=1)
        self.assertGreaterEqual(len(tech.teams), 8)
        self.assertTrue(tech.definition_hash)
        self.assertTrue(tech.scrubbed_by)
        self.assertIsNotNone(tech.scrubbed_at)

    def test_active_template_requires_scrub_gate(self):
        template = OrgTemplate(
            slug='bad-template',
            version=1,
            name='Bad Template',
            status=OrgTemplate.Status.ACTIVE,
            teams=[{'slug': 'engineering', 'capabilities': ['engineering'], 'agent_count': 1}],
        )

        with self.assertRaisesMessage(Exception, 'scrub gate'):
            template.full_clean()


    def test_active_template_scrub_gate_scans_custom_capabilities_and_full_payload(self):
        internal_markers = [
            'kubeconfig',
            'credential_broker',
            'host_exec',
            'repo_write',
            'github:merge',
            'github:review',
        ]
        for marker in internal_markers:
            with self.subTest(marker=marker):
                template = OrgTemplate(
                    slug=f'bad-{marker.replace(":", "-").replace("_", "-")}',
                    version=1,
                    name='Bad Template',
                    status=OrgTemplate.Status.ACTIVE,
                    teams=[{'slug': 'engineering', 'capabilities': ['custom-safe'], 'agent_count': 1}],
                    custom_capabilities=[{
                        'slug': 'custom-safe',
                        'skill_refs': [],
                        'mcp_refs': [],
                        'default_credential_grant_scopes': [marker],
                    }],
                    scrubbed_by='test-suite',
                    scrubbed_at=timezone.now(),
                )

                with self.assertRaisesMessage(Exception, 'internal-only marker'):
                    template.full_clean()

        template = OrgTemplate(
            slug='bad-manager',
            version=1,
            name='Bad Manager Template',
            status=OrgTemplate.Status.ACTIVE,
            teams=[{'slug': 'engineering', 'capabilities': ['engineering'], 'agent_count': 1}],
            managers={'andon_routes': {'infra-ops': 'credential_broker'}},
            scrubbed_by='test-suite',
            scrubbed_at=timezone.now(),
        )

        with self.assertRaisesMessage(Exception, 'internal-only marker'):
            template.full_clean()

    def test_template_plan_resolves_exact_create_calls_and_scrubs_sensitive_flags(self):
        tech = seed_builtin_org_templates(actor='test-suite')[0]

        plan = resolve_org_template_plan(
            tech,
            TemplatePlanContext(org_name='Acme AI', org_slug='acme-ai', plan_slug='team'),
        )

        self.assertEqual(plan['template']['slug'], 'tech-company')
        self.assertEqual(plan['context']['org_slug'], 'acme-ai')
        actions = [(call['service'], call['action']) for call in plan['create_calls']]
        self.assertIn(('admin', 'create_organization'), actions)
        self.assertIn(('pulse', 'create_project'), actions)
        self.assertIn(('pulse', 'create_task'), actions)
        self.assertIn(('hive', 'configure_manager_andon_routes'), actions)
        devops = next(team for team in plan['teams'] if team['slug'] == 'devops')
        merge = next(team for team in plan['teams'] if team['slug'] == 'merge')
        review = next(team for team in plan['teams'] if team['slug'] == 'review')
        self.assertEqual(devops['sensitive_access_flags'], [])
        self.assertIn('kubeconfig', devops['stripped_sensitive_access_flags'])
        self.assertIn('kubeconfig', devops['stripped_credential_grant_scopes'])
        for team in (devops, merge, review):
            exposed_scopes = [
                scope
                for cap in team['capabilities']
                for scope in cap.get('credential_grant_scopes', [])
            ]
            self.assertEqual(exposed_scopes, [])
        self.assertIn('github:merge', merge['stripped_credential_grant_scopes'])
        self.assertIn('github:review', review['stripped_credential_grant_scopes'])


    def test_template_plan_scrubs_for_unknown_tenant_kind_fail_closed(self):
        tech = seed_builtin_org_templates(actor='test-suite')[0]

        plan = resolve_org_template_plan(
            tech,
            TemplatePlanContext(org_name='Acme AI', org_slug='acme-ai', plan_slug='team', tenant_kind='external'),
        )

        devops = next(team for team in plan['teams'] if team['slug'] == 'devops')
        self.assertEqual(plan['context']['tenant_kind'], 'external')
        self.assertEqual(devops['sensitive_access_flags'], [])
        self.assertIn('kubeconfig', devops['stripped_credential_grant_scopes'])
        for cap in devops['capabilities']:
            self.assertEqual(cap.get('credential_grant_scopes'), [])

    def test_template_plan_normalizes_customer_tenant_kind_before_scrubbing(self):
        tech = seed_builtin_org_templates(actor='test-suite')[0]

        plan = resolve_org_template_plan(
            tech,
            TemplatePlanContext(org_name='Acme AI', org_slug='acme-ai', plan_slug='team', tenant_kind='Customer'),
        )

        devops = next(team for team in plan['teams'] if team['slug'] == 'devops')
        self.assertEqual(plan['context']['tenant_kind'], 'customer')
        self.assertEqual(devops['sensitive_access_flags'], [])
        self.assertIn('kubeconfig', devops['stripped_credential_grant_scopes'])

    def test_bootstrap_dry_run_embeds_template_plan(self):
        seed_builtin_org_templates(actor='test-suite')
        run, _ = create_or_get_run(
            requester_user_id=99,
            org_name='Dry Run Org',
            org_slug='dry-run-org',
            template_slug='tech-company',
            plan_slug='team',
            dry_run=True,
        )

        advanced = advance_preview(run)

        self.assertEqual(advanced.status, TenantProvisioningRun.Status.READY)
        self.assertEqual(advanced.resource_refs['template_plan']['template']['slug'], 'tech-company')
        self.assertTrue(advanced.resource_refs['template_plan']['create_calls'])

    def test_management_command_outputs_seed_summary_json(self):
        out = StringIO()
        call_command('seed_org_templates', '--json', stdout=out)

        self.assertIn('tech-company', out.getvalue())
        self.assertEqual(OrgTemplate.objects.count(), len(builtin_template_definitions()))
