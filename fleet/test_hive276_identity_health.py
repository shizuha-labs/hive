from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import textwrap
from unittest.mock import MagicMock, patch

import yaml

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from fleet.identity_health import annotate_agents, identity_health_for_agent, routing_health_for_agent
from fleet.models import DaemonRegistry, FleetAgent, FleetAgentEvent
from fleet.views import (
    _compute_agent_health_summary,
    _id_agent_records_for_health,
    _normalise_team_members_payload,
)


BANTO_ID = {
    'user_id': 112,
    'username': 'banto',
    'email': 'banto@agents.shizuha.io',
    'account_type': 'agent',
    'is_active': True,
}


class Hive276IdentityRoutingHealthTests(SimpleTestCase):
    def test_banto_alias_runtime_and_roster_are_healthy_when_id_resolves_by_platform_user(self):
        agents = [{
            'agent_username': 'banto',
            'email': 'banto@shizuha.com',
            'agent_id': 'runtime-agent-uuid',
            'platform_user_id': '112',
            'status': 'running',
            'team_memberships': [{
                'team_name': 'Accounting',
                'team_slug': 'accounting',
                'email': 'banto@shizuha.com',
                'assignable': True,
            }],
        }]

        annotate_agents(agents, [BANTO_ID])

        self.assertTrue(agents[0]['identity_health']['ok'])
        self.assertTrue(agents[0]['routing_health']['ok'])
        self.assertTrue(agents[0]['health_summary']['ok'])
        self.assertEqual(agents[0]['identity_health']['canonical_email'], 'banto@agents.shizuha.io')
        self.assertEqual(agents[0]['identity_health']['runtime_email'], 'banto@shizuha.com')

    def test_runtime_agent_uuid_does_not_count_as_platform_user_id(self):
        health = identity_health_for_agent({
            'agent_username': 'banto',
            'email': 'banto@agents.shizuha.io',
            'agent_id': '01e7f043-e4d2-5084-8714-d97bf6709201',
        }, [BANTO_ID])

        self.assertTrue(health['ok'])
        self.assertFalse(any(
            warning['issue_type'] == 'platform_user_mismatch'
            for warning in health['warnings']
        ))

    def test_banto_runtime_missing_id_record_reports_id_blocker(self):
        health = identity_health_for_agent({
            'agent_username': 'banto',
            'email': 'banto@shizuha.com',
            'platform_user_id': '112',
        }, [])

        self.assertFalse(health['ok'])
        self.assertEqual(health['blocking_services'], ['id'])
        self.assertEqual(health['warnings'][0]['issue_type'], 'identity_unresolved')
        self.assertIn('platform_user_id', health['warnings'][0]['actual'])

    def test_stopped_never_provisioned_row_is_distinct_gated_cleanup_orphan(self):
        agent = {
            'id': '07d95876-80b0-4e9e-82bb-3b13bf667144',
            'agent_username': 'haruka',
            'status': 'stopped',
            'provisioned_at': None,
            'lifecycle_managed': True,
        }
        identity = identity_health_for_agent(agent, [])

        self.assertFalse(identity['ok'])
        self.assertTrue(identity['orphan_cleanup_required'])
        issue = identity['warnings'][0]
        self.assertEqual(issue['issue_type'], 'identity_orphan_cleanup')
        self.assertNotEqual(issue['issue_type'], 'identity_unresolved')
        self.assertEqual(issue['cleanup_action']['kind'], 'delete_agent')
        self.assertTrue(issue['cleanup_action']['staff_only'])
        self.assertEqual(
            issue['cleanup_action']['url'],
            '/v1/fleet/agents/07d95876-80b0-4e9e-82bb-3b13bf667144/',
        )

        summary = _compute_agent_health_summary({'health_summary': {
            'warnings': identity['warnings'],
        }})
        self.assertEqual(summary['issues'][0]['label'], 'Incomplete identity — retiring')
        self.assertEqual(summary['issues'][0]['cleanup_action']['method'], 'DELETE')

    def test_running_or_previously_provisioned_missing_identity_stays_drift_not_cleanup(self):
        for agent in (
            {'id': '1', 'agent_username': 'haruka', 'status': 'running', 'provisioned_at': None},
            {'id': '2', 'agent_username': 'haruka', 'status': 'stopped', 'provisioned_at': '2026-01-01T00:00:00Z'},
        ):
            health = identity_health_for_agent(agent, [])
            self.assertEqual(health['warnings'][0]['issue_type'], 'identity_unresolved')
            self.assertNotIn('cleanup_action', health['warnings'][0])

    def test_missing_agent_inventory_entry_is_probed_before_orphan_classification(self):
        def id_get(path, params=None):
            if path.endswith('/list/'):
                return {'agents': []}
            return {
                'exists': True,
                'user_id': 111,
                'username': 'fumi',
                'email': 'fumi@agents.shizuha.io',
                'account_type': 'human',
                'is_active': True,
            }

        with patch('fleet.external_agents._id_get', side_effect=id_get):
            records = _id_agent_records_for_health([{
                'id': 'fumi-row',
                'agent_username': 'fumi',
                'status': 'stopped',
                'provisioned_at': None,
                'lifecycle_managed': True,
            }])
        health = identity_health_for_agent({'agent_username': 'fumi'}, records)
        self.assertEqual(health['warnings'][0]['issue_type'], 'inactive_or_wrong_account_type')
        self.assertNotEqual(health['warnings'][0]['issue_type'], 'identity_orphan_cleanup')

    def test_banto_roster_member_unassignable_reports_pulse_blocker(self):
        health = routing_health_for_agent({
            'agent_username': 'banto',
            'email': 'banto@shizuha.com',
            'team_memberships': [{
                'team_name': 'Accounting',
                'email': 'banto@shizuha.com',
                'assignable': False,
                'routing_error': 'user_service.get_user_by_email returned None',
            }],
        })

        self.assertFalse(health['ok'])
        self.assertEqual(health['blocking_services'], ['pulse'])
        self.assertEqual(health['warnings'][0]['issue_type'], 'roster_unassignable')
        self.assertIn('user_service.get_user_by_email', health['warnings'][0]['message'])

    def test_team_members_payload_surfaces_unassignable_member_health(self):
        payload = {
            'team_name': 'accounting',
            'memberships': [{
                'email': 'banto@shizuha.com',
                'assignable': False,
                'routing_error': 'Pulse alias lookup failed',
            }],
        }

        out = _normalise_team_members_payload(payload, 'accounting')

        member = out['memberships'][0]
        self.assertFalse(member['routing_health']['ok'])
        self.assertEqual(member['routing_health']['blocking_services'], ['pulse'])
        self.assertEqual(member['routing_health']['warnings'][0]['field'], 'pulse.TeamRoster.member')


@override_settings(SHIZUHA_ID_INTERNAL_URL='http://id.internal')
class Hive599IdentityReconcileCommandTests(TestCase):
    def setUp(self):
        self.agent = FleetAgent.objects.create(
            agent_username='fumi',
            status=FleetAgent.Status.STOPPED,
        )

    @staticmethod
    def _mistagged_id_get(path, params=None):
        if path.endswith('/list/'):
            return {'agents': []}
        return {
            'exists': True,
            'user_id': 111,
            'username': 'fumi',
            'email': 'fumi@agents.shizuha.io',
            'account_type': 'human',
            'is_active': True,
        }

    def test_safe_mistagged_reconcile_patches_only_account_type_and_audits(self):
        response = MagicMock(status_code=200, content=b'{"changed":true}', text='')
        response.json.return_value = {'changed': True}
        output = StringIO()
        with patch(
            'fleet.management.commands.reconcile_agent_identities._id_get',
            side_effect=self._mistagged_id_get,
        ), patch(
            'fleet.management.commands.reconcile_agent_identities.requests.patch',
            return_value=response,
        ) as patch_id:
            call_command('reconcile_agent_identities', stdout=output)

        patch_id.assert_called_once_with(
            'http://id.internal/api/internal/agents/',
            json={'username': 'fumi', 'action': 'reconcile_account_type'},
            headers={},
            timeout=8,
        )
        event = FleetAgentEvent.objects.get(agent=self.agent)
        self.assertEqual(event.event_type, 'identity_reconciled')
        self.assertIn('changed=True', event.message)
        self.assertIn("reconciled=['fumi']", output.getvalue())

    def test_dry_run_is_non_mutating_and_writes_no_audit(self):
        with patch(
            'fleet.management.commands.reconcile_agent_identities._id_get',
            side_effect=self._mistagged_id_get,
        ), patch(
            'fleet.management.commands.reconcile_agent_identities.requests.patch'
        ) as patch_id:
            call_command('reconcile_agent_identities', '--dry-run')
        patch_id.assert_not_called()
        self.assertFalse(FleetAgentEvent.objects.filter(agent=self.agent).exists())

    def test_true_orphan_is_reported_but_never_created_or_patched(self):
        def orphan_id_get(path, params=None):
            return {'agents': []} if path.endswith('/list/') else {'exists': False}

        output = StringIO()
        with patch(
            'fleet.management.commands.reconcile_agent_identities._id_get',
            side_effect=orphan_id_get,
        ), patch(
            'fleet.management.commands.reconcile_agent_identities.requests.patch'
        ) as patch_id:
            call_command('reconcile_agent_identities', stdout=output)
        patch_id.assert_not_called()
        self.assertFalse(FleetAgentEvent.objects.filter(agent=self.agent).exists())
        self.assertIn('ORPHANS', output.getvalue())
        self.assertIn('fumi', output.getvalue())

    def test_stale_daemon_orphan_is_retired_through_durable_tombstone(self):
        daemon = DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1',
            fleet_id='fleet-s1',
            status=DaemonRegistry.Status.CONNECTED,
        )
        self.agent.managing_daemon = daemon
        self.agent.save(update_fields=['managing_daemon'])
        FleetAgent.objects.filter(pk=self.agent.pk).update(
            created_at=timezone.now() - timedelta(days=2),
        )

        def orphan_id_get(path, params=None):
            return {'agents': []} if path.endswith('/list/') else {'exists': False}

        observed_statuses = []

        def runtime_delete(row):
            row.refresh_from_db()
            observed_statuses.append(row.status)
            return True, 'applied'

        with patch(
            'fleet.management.commands.reconcile_agent_identities._id_get',
            side_effect=orphan_id_get,
        ), patch(
            'fleet.lifecycle.delete_runtime_agent',
            side_effect=runtime_delete,
        ) as delete_runtime, patch(
            'fleet.lifecycle.provisioner.deprovision_agent',
            return_value=True,
        ), patch('fleet.lifecycle.agent_index_cache.invalidate'):
            call_command(
                'reconcile_agent_identities',
                '--reap-stale-orphans',
                '--orphan-grace-hours=24',
            )

        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, FleetAgent.Status.DELETED)
        self.assertEqual(observed_statuses, [FleetAgent.Status.DELETED])
        delete_runtime.assert_called_once()
        self.assertTrue(FleetAgentEvent.objects.filter(
            agent=self.agent,
            event_type='deleted',
        ).exists())

    def test_fresh_unprovisioned_orphan_survives_grace_period(self):
        def orphan_id_get(path, params=None):
            return {'agents': []} if path.endswith('/list/') else {'exists': False}

        with patch(
            'fleet.management.commands.reconcile_agent_identities._id_get',
            side_effect=orphan_id_get,
        ), patch('fleet.lifecycle.retire_fleet_agent') as retire:
            call_command(
                'reconcile_agent_identities',
                '--reap-stale-orphans',
                '--orphan-grace-hours=24',
            )
        retire.assert_not_called()
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, FleetAgent.Status.STOPPED)

    def test_inactive_identity_is_never_auto_retired(self):
        FleetAgent.objects.filter(pk=self.agent.pk).update(
            created_at=timezone.now() - timedelta(days=2),
        )

        def inactive_id_get(path, params=None):
            if path.endswith('/list/'):
                return {'agents': []}
            return {
                'exists': True,
                'username': 'fumi',
                'is_active': False,
            }

        output = StringIO()
        with patch(
            'fleet.management.commands.reconcile_agent_identities._id_get',
            side_effect=inactive_id_get,
        ), patch('fleet.lifecycle.retire_fleet_agent') as retire, patch(
            'fleet.management.commands.reconcile_agent_identities.requests.patch',
        ) as patch_id:
            call_command(
                'reconcile_agent_identities',
                '--reap-stale-orphans',
                stdout=output,
            )

        retire.assert_not_called()
        patch_id.assert_not_called()
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, FleetAgent.Status.STOPPED)
        self.assertIn('INACTIVE IDENTITIES', output.getvalue())


class Hive599IdentityReconcilerDeployTests(SimpleTestCase):
    def test_checked_in_cronjob_remains_safe_and_hourly(self):
        manifest = yaml.safe_load(Path('k8s/hive-identity-reconciler.yaml').read_text())
        self.assertEqual(manifest['kind'], 'CronJob')
        self.assertEqual(manifest['metadata']['name'], 'hive-identity-reconciler')
        self.assertEqual(manifest['spec']['schedule'], '17 * * * *')
        pod_spec = manifest['spec']['jobTemplate']['spec']['template']['spec']
        self.assertEqual(pod_spec['serviceAccountName'], 'hive-controller')
        reconciler = pod_spec['containers'][0]
        self.assertEqual(
            reconciler['command'],
            [
                'python', 'manage.py', 'reconcile_agent_identities',
                '--reap-archived-tombstones', '--reap-stale-orphans',
            ],
        )
        self.assertNotIn('create', ' '.join(reconciler['command']))
        self.assertNotIn('delete', ' '.join(reconciler['command']))
        env_names = {item['name'] for item in reconciler['env']}
        self.assertIn('DB_PASSWORD', env_names)
        self.assertIn('JWT_SECRET_KEY', env_names)

    def test_deploy_workflow_renders_exact_rollout_image_and_checks_it(self):
        workflow = (Path('.forgejo/scripts/ring-release-fleet.sh').read_text())
        marker = (
            "python3 - /tmp/hive-deploy-post-rollout.json <<'PY' > "
            "/tmp/hive-identity-reconciler-cronjob.json"
        )
        renderer = textwrap.dedent(
            workflow.split(marker, 1)[1].split('\nPY', 1)[0]
        )
        source_deploy = {
            'spec': {'template': {'spec': {
                'serviceAccountName': 'hive-controller',
                'affinity': {'nodeAffinity': {'required': 'preserved'}},
                'tolerations': [{'key': 'node.shizuha/workstation'}],
                'volumes': [{'name': 'runtime-secrets'}],
                'containers': [{
                    'name': 'hive',
                    'image': 'localhost:30500/shizuha-hive:hive-reviewsha',
                    'imagePullPolicy': 'Always',
                    'command': ['gunicorn'],
                    'ports': [{'containerPort': 8000}],
                    'env': [{
                        'name': 'DB_PASSWORD',
                        'valueFrom': {'secretKeyRef': {
                            'name': 'shizuha-infra-postgresql',
                            'key': 'postgres-password',
                        }},
                    }],
                    'volumeMounts': [{'name': 'runtime-secrets', 'mountPath': '/run/secrets'}],
                }],
            }}},
        }
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.json') as source:
            json.dump(source_deploy, source)
            source.flush()
            rendered = StringIO()
            with patch.object(sys, 'argv', ['renderer', source.name]), redirect_stdout(rendered):
                exec(compile(renderer, '<identity-reconciler-renderer>', 'exec'), {})
        cronjob = json.loads(rendered.getvalue())
        pod_spec = cronjob['spec']['jobTemplate']['spec']['template']['spec']
        reconciler = pod_spec['containers'][0]
        self.assertEqual(reconciler['image'], 'localhost:30500/shizuha-hive:hive-reviewsha')
        self.assertEqual(
            reconciler['command'],
            [
                'python', 'manage.py', 'reconcile_agent_identities',
                '--reap-archived-tombstones', '--reap-stale-orphans',
            ],
        )
        self.assertEqual(pod_spec['serviceAccountName'], 'hive-controller')
        self.assertEqual(pod_spec['affinity'], source_deploy['spec']['template']['spec']['affinity'])
        self.assertEqual(pod_spec['volumes'], source_deploy['spec']['template']['spec']['volumes'])
        self.assertEqual(reconciler['env'], source_deploy['spec']['template']['spec']['containers'][0]['env'])
        self.assertNotIn('ports', reconciler)
        self.assertEqual(cronjob['spec']['schedule'], '17 * * * *')

        self.assertIn('hive-identity-reconciler-cronjob.json', workflow)
        self.assertIn('container["name"] = "reconciler"', workflow)
        self.assertIn(
            '"--reap-archived-tombstones", "--reap-stale-orphans"',
            workflow,
        )
        self.assertIn('"schedule": "17 * * * *"', workflow)
        self.assertIn('"serviceAccountName"', workflow)
        self.assertIn('kubectl apply -f /tmp/hive-identity-reconciler-cronjob.json', workflow)
        self.assertIn(
            '--from=cronjob/hive-identity-reconciler',
            workflow,
        )
        self.assertIn(
            'job/${IDENTITY_RECONCILE_JOB}',
            workflow,
        )
        self.assertIn('Identity reconciler image: ${IDENTITY_RECONCILER_IMAGE}', workflow)
        self.assertIn('identity reconciler image mismatch', workflow)
        self.assertIn('identity reconciler command missing', workflow)
