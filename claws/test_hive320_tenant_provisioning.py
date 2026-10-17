from django.core.management import call_command
from django.test import TestCase, override_settings
from unittest import mock

from claws import provisioning_adapters as adapters
from claws.models import TenantProvisioningRun
from claws.tenant_provisioning import (
    PROVISIONING_STEPS,
    advance_preview,
    create_or_get_run,
    deprovision_parked_run,
    default_idempotency_key,
)


class TenantProvisioningRunTests(TestCase):
    def setUp(self):
        # PLAT-9671: the trust edge is pinned HERE, not in the environment.
        # The adapter's config contract is correct (fail loud when the
        # provisioning trust edge is unconfigured), but the protected-master
        # pipeline does not export HIVE_SERVICE_SIGNING_KEY, so env-dependent
        # fixtures rode the fail-loud short-circuit BEFORE the POST seam
        # (admin_post/teardown_post counts 0≠1, ride-class and message-class
        # divergences). With the key set, the fleet signing helper computes
        # locally and the POST-seam fixtures exercise the real HTTP path they
        # were written for — env-independent on both premerge and main.
        trust_edge = override_settings(
            HIVE_SERVICE_SIGNING_KEY='hive320-test-signing-key',
        )
        trust_edge.enable()
        self.addCleanup(trust_edge.disable)

    def test_create_or_get_run_is_idempotent_and_initializes_all_steps(self):
        kwargs = {
            'requester_user_id': 42,
            'requester_email': 'owner@example.com',
            'org_name': 'Acme AI Ops',
            'org_slug': 'acme-ai-ops',
            'template_slug': 'design-partner-starter',
            'plan_slug': 'pilot',
            'dry_run': True,
        }

        run, created = create_or_get_run(**kwargs)
        again, created_again = create_or_get_run(**kwargs)

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(run.id, again.id)
        self.assertEqual(run.current_step, PROVISIONING_STEPS[0].slug)
        self.assertEqual(set(run.step_state.keys()), {step.slug for step in PROVISIONING_STEPS})
        self.assertEqual(
            run.idempotency_key,
            default_idempotency_key(
                requester_user_id=42,
                org_slug='acme-ai-ops',
                template_slug='design-partner-starter',
                plan_slug='pilot',
                dry_run=True,
            ),
        )

    def test_dry_run_advances_all_steps_and_records_planned_refs(self):
        run, _ = create_or_get_run(
            requester_user_id=7,
            org_name='Pilot Org',
            org_slug='pilot-org',
            template_slug='ops-starter',
            plan_slug='pilot',
            dry_run=True,
        )

        advanced = advance_preview(run)

        self.assertEqual(advanced.status, TenantProvisioningRun.Status.READY)
        self.assertEqual(advanced.current_step, 'ready')
        self.assertEqual(advanced.resource_refs['kubernetes']['namespace'], 'tenant-pilot-org')
        self.assertTrue(all(step['status'] == TenantProvisioningRun.StepStatus.DONE for step in advanced.step_state.values()))

    def test_non_dry_run_completes_id_org_via_real_adapter_then_parks_at_pulse(self):
        """HIVE-2715: the id_org leg is adapter-backed, so a real run completes
        id_org FOR REAL (verified resource ref) and the honesty bridge now
        parks at the registry-derived next un-implemented leg (`pulse`)."""
        run, _ = create_or_get_run(
            requester_user_id=7,
            requester_email='gp_owner@qafixture.shizuha.com',
            org_name='Pilot Org',
            org_slug='pilot-prod',
            template_slug='ops-starter',
            plan_slug='pilot',
        )

        class _AdminResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        with mock.patch.object(
            adapters.requests, 'post',
            return_value=_AdminResponse(201, {'id': 901, 'created': True, 'slug': 'pilot-prod'}),
        ) as admin_post:
            advanced = advance_preview(run)

        self.assertEqual(admin_post.call_count, 1)
        self.assertEqual(advanced.status, TenantProvisioningRun.Status.MANUAL_REVIEW)
        self.assertEqual(advanced.current_step, 'pulse')
        self.assertEqual(
            advanced.step_state['validate_request']['status'],
            TenantProvisioningRun.StepStatus.DONE,
        )
        self.assertEqual(
            advanced.step_state['id_org']['status'],
            TenantProvisioningRun.StepStatus.DONE,
        )
        organization_ref = advanced.resource_refs['organization']
        self.assertTrue(organization_ref['verified'])
        self.assertEqual(organization_ref['external_org_id'], '901')
        self.assertEqual(organization_ref['org_slug'], 'pilot-prod')
        self.assertEqual(
            advanced.step_state['pulse']['status'],
            TenantProvisioningRun.StepStatus.MANUAL_REVIEW,
        )

    def test_adapter_failure_is_fail_loud_failed_not_manual_review(self):
        """Shrink constraint: a park at an ADAPTERED step cannot ride the
        honesty bridge — an adapter error is fail-loud FAILED, never the
        expected-pending marker."""
        run, _ = create_or_get_run(
            requester_user_id=9,
            org_name='Real Org',
            org_slug='real-org',
            template_slug='ops-starter',
            plan_slug='pilot',
        )

        class _AdminResponse:
            status_code = 503

            def json(self):
                return {'error': 'upstream unavailable'}

        with mock.patch.object(adapters.requests, 'post', return_value=_AdminResponse()):
            advanced = advance_preview(run)

        self.assertEqual(advanced.status, TenantProvisioningRun.Status.FAILED)
        self.assertEqual(advanced.current_step, 'id_org')
        self.assertEqual(
            advanced.step_state['id_org']['status'],
            TenantProvisioningRun.StepStatus.FAILED,
        )
        # The seam prefix is stable across adapter failure details (upstream
        # 503, trust-edge misconfiguration); the 503 datum is the mocked
        # upstream under test here.
        self.assertIn('Adapter for step id_org failed', advanced.status_message)
        self.assertIn('http 503', advanced.status_message)
        audit_events = advanced.audit_events or []
        self.assertTrue(any(event['event'] == 'adapter_failed' for event in audit_events))
        self.assertFalse(any(event['event'] == 'manual_review' for event in audit_events))

    def test_adapter_bad_result_fails_closed_on_verify_step_result(self):
        """A bad adapter result can never silently advance: verify_step_result
        raises fail-loud and the saga marks the step FAILED."""
        run, _ = create_or_get_run(
            requester_user_id=10,
            org_name='Cross Org',
            org_slug='cross-org',
            template_slug='ops-starter',
            plan_slug='pilot',
        )

        def _bad_adapter(_run):
            return {
                'verified': True,
                'external_org_id': '555',
                'org_slug': 'some-other-org',  # cross-org ref must fail closed
            }

        with mock.patch.dict(adapters.SAGA_ADAPTERS, {'id_org': _bad_adapter}):
            advanced = advance_preview(run)

        self.assertEqual(advanced.status, TenantProvisioningRun.Status.FAILED)
        audit_events = advanced.audit_events or []
        self.assertTrue(any(
            event['event'] == 'step_invariant_violation' and event.get('security_sensitive')
            for event in audit_events
        ))

    def test_adapter_replay_after_crash_yields_same_org_not_duplicate(self):
        """Crash-mid-step resumability: the side effect landed (org created)
        but the ref was never recorded; the replay hits the SAME slug +
        operation_id and the endpoint returns the EXISTING org (200 exists) —
        the ref records the SAME org id, never a duplicate."""
        run, _ = create_or_get_run(
            requester_user_id=11,
            requester_email='gp_owner@qafixture.shizuha.com',
            org_name='Crash Org',
            org_slug='crash-org',
            template_slug='ops-starter',
            plan_slug='pilot',
        )

        class _AdminResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        responses = iter([
            _AdminResponse(201, {'id': 777, 'created': True, 'slug': 'crash-org'}),
            _AdminResponse(200, {'id': 777, 'created': False, 'slug': 'crash-org'}),
        ])
        with mock.patch.object(adapters.requests, 'post', side_effect=lambda *a, **k: next(responses)) as admin_post:
            first = adapters.provision_id_org(run)
            # simulate the crash: the side effect landed, the ref recording did not
            replayed = adapters.provision_id_org(run)

        self.assertEqual(admin_post.call_count, 2)
        self.assertEqual(first['external_org_id'], '777')
        self.assertEqual(replayed['external_org_id'], '777')
        self.assertEqual(first['operation_id'], replayed['operation_id'])

    def test_deprovision_qafixture_run_tears_down_minted_org(self):
        """HIVE-2697 cleanup contract extended to the minted org: a QA
        fixture-scoped run's teardown calls the registered compensating
        action and records the real outcome; the run still closes torn_down."""
        run, _ = create_or_get_run(
            requester_user_id=12,
            requester_email='gp_owner@qafixture.shizuha.com',
            org_name='Litter Org',
            org_slug='litter-org',
            template_slug='ops-starter',
            plan_slug='pilot',
        )
        run.resource_refs = {
            'organization': {
                'verified': True,
                'external_org_id': '901',
                'org_slug': 'litter-org',
            },
        }
        run.step_state = {
            **run.step_state,
            'id_org': {'status': TenantProvisioningRun.StepStatus.DONE, 'side_effectful': True},
        }
        run.status = TenantProvisioningRun.Status.MANUAL_REVIEW
        run.current_step = 'pulse'
        run.save()

        class _AdminResponse:
            status_code = 200

            def json(self):
                return {'status': 'deleted', 'organization_id': 901}

        with mock.patch.object(adapters.requests, 'post', return_value=_AdminResponse()) as teardown_post:
            torn = deprovision_parked_run(run)

        self.assertEqual(teardown_post.call_count, 1)
        self.assertEqual(torn.status, TenantProvisioningRun.Status.TORN_DOWN)
        self.assertIn('deleted', torn.status_message)
        audit_events = torn.audit_events or []
        self.assertTrue(any(event['event'] == 'deprovision_id_org' for event in audit_events))

    def test_deprovision_refuses_non_qafixture_run_teardown(self):
        """Refuse-by-default: a NON-fixture requester's minted org is never
        torn down by the saga path; the refusal is recorded and the run still
        closes torn_down (honest wording, no fake cleanup)."""
        run, _ = create_or_get_run(
            requester_user_id=13,
            requester_email='real.human@example.com',
            org_name='Human Org',
            org_slug='human-org',
            template_slug='ops-starter',
            plan_slug='pilot',
        )
        run.resource_refs = {
            'organization': {
                'verified': True,
                'external_org_id': '902',
                'org_slug': 'human-org',
            },
        }
        run.step_state = {
            **run.step_state,
            'id_org': {'status': TenantProvisioningRun.StepStatus.DONE, 'side_effectful': True},
        }
        run.status = TenantProvisioningRun.Status.MANUAL_REVIEW
        run.current_step = 'pulse'
        run.save()

        with mock.patch.object(adapters.requests, 'post') as teardown_post:
            torn = deprovision_parked_run(run)

        self.assertEqual(teardown_post.call_count, 0)
        self.assertEqual(torn.status, TenantProvisioningRun.Status.TORN_DOWN)
        audit_events = torn.audit_events or []
        self.assertTrue(any(event['event'] == 'deprovision_refused' for event in audit_events))
        self.assertIn('902', torn.status_message)  # honest inventory wording

    def test_allow_side_effects_does_not_mark_real_run_ready_without_adapters(self):
        run, _ = create_or_get_run(
            requester_user_id=8,
            org_name='Real Org',
            org_slug='real-org',
            template_slug='ops-starter',
            plan_slug='pilot',
            dry_run=False,
        )

        class _AdminResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        with mock.patch.object(
            adapters.requests, 'post',
            return_value=_AdminResponse(201, {'id': 911, 'created': True, 'slug': 'real-org'}),
        ):
            advanced = advance_preview(run, allow_side_effects=True)

        self.assertEqual(advanced.status, TenantProvisioningRun.Status.MANUAL_REVIEW)
        self.assertEqual(advanced.current_step, 'pulse')
        self.assertIn('no concrete adapter', advanced.step_state['pulse']['message'])
        self.assertEqual(
            advanced.step_state['id_org']['status'],
            TenantProvisioningRun.StepStatus.DONE,
        )
        self.assertTrue(advanced.resource_refs.get('organization', {}).get('verified'))

    def test_dry_run_does_not_reserve_org_slug_for_later_real_run(self):
        dry_run, _ = create_or_get_run(
            requester_user_id=7,
            org_name='Reusable Slug',
            org_slug='reusable-slug',
            template_slug='ops-starter',
            plan_slug='pilot',
            dry_run=True,
        )
        advance_preview(dry_run)

        real_run, created = create_or_get_run(
            requester_user_id=7,
            org_name='Reusable Slug',
            org_slug='reusable-slug',
            template_slug='ops-starter',
            plan_slug='pilot',
            dry_run=False,
        )

        self.assertTrue(created)
        self.assertNotEqual(real_run.id, dry_run.id)
        self.assertFalse(real_run.dry_run)

    def test_management_command_creates_json_summary(self):
        from io import StringIO

        out = StringIO()
        call_command(
            'bootstrap_tenant_org',
            '--requester-user-id=9',
            '--org-name=CLI Org',
            '--org-slug=cli-org',
            '--template-slug=ops-starter',
            '--dry-run',
            '--json',
            stdout=out,
        )

        run = TenantProvisioningRun.objects.get(org_slug='cli-org')
        self.assertEqual(run.status, TenantProvisioningRun.Status.READY)
        self.assertIn('"status": "ready"', out.getvalue())
