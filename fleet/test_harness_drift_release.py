import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock
from io import StringIO

from django.test import SimpleTestCase
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / '.forgejo' / 'scripts'


class HarnessDriftReleaseTests(SimpleTestCase):
    def run_same_image_contract_selection(self, drift):
        from fleet.management.commands import roll_harness_drift as roller
        image = 'localhost:30500/shizuha-agent-runtime@sha256:' + 'a' * 64
        agents = [SimpleNamespace(agent_username=name) for name in ('jun', 'ni', 'sara')]
        command = roller.Command(stdout=StringIO())
        deployment = SimpleNamespace(spec=SimpleNamespace(template=SimpleNamespace(
            spec=SimpleNamespace(containers=[SimpleNamespace(name='agent', image=image)]))))
        apps = mock.Mock()
        apps.read_namespaced_deployment.return_value = deployment
        with mock.patch.object(command, '_running_native_agents', return_value=[(a, a.agent_username) for a in agents]), \
             mock.patch.object(roller, '_live_ns', return_value='shizuha-fleet'), \
             mock.patch.object(roller, 'get_k8s_client', return_value=(None, apps, None)), \
             mock.patch.object(roller, 'native_agent_runtime_contract_drift', side_effect=drift, create=True) as probe, \
             mock.patch.object(roller, 'converge_running_lifecycle_agent', return_value=(True, {'converged': True})) as converge, \
             mock.patch('fleet.model_lease.reconcile_ghost_lease_holders', return_value={'checked': 0, 'released': []}), \
             mock.patch('fleet.native_runtime_release.get_native_runtime_release', return_value=SimpleNamespace(
                 release=SimpleNamespace(generation=53, deployment_image=image))):
            command.handle(max_seats=1, dry_run=False)
        return agents, converge, probe, image

    def test_same_runtime_image_broker_contract_drift_reaches_bounded_converger(self):
        agents, converge, probe, image = self.run_same_image_contract_selection([True, False, True])
        converge.assert_called_once_with(agents[0])
        self.assertEqual(probe.call_args_list, [mock.call(a, image) for a in agents])

    def test_same_runtime_image_and_healthy_contract_does_not_roll(self):
        _, converge, _, _ = self.run_same_image_contract_selection([False, False, False])
        converge.assert_not_called()

    def deployment(self):
        return {'spec': {'template': {'spec': {
            'serviceAccountName': 'hive-controller',
            'volumes': [{'name': 'credentials', 'secret': {'secretName': 'hive-secret'}}],
            'securityContext': {'runAsNonRoot': True},
            'nodeSelector': {'kubernetes.io/arch': 'amd64'},
            'containers': [{
                'name': 'hive', 'image': 'localhost:30500/shizuha-hive:hive-qualified',
                'command': ['serve'], 'args': ['--web'], 'ports': [{'containerPort': 8030}],
                'readinessProbe': {'httpGet': {'path': '/health', 'port': 8030}},
                'livenessProbe': {'httpGet': {'path': '/health', 'port': 8030}},
                'env': [
                    {'name': 'HIVE_NATIVE_RUNTIME_RELEASE_ENABLED', 'value': 'true'},
                    {'name': 'HIVE_NATIVE_RUNTIME_RELEASE_NAMESPACE', 'value': 'shizuha-hive'},
                    {'name': 'DB_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'database', 'key': 'password'}}},
                ],
                'envFrom': [{'secretRef': {'name': 'hive-config'}}],
                'volumeMounts': [{'name': 'credentials', 'mountPath': '/run/credentials', 'readOnly': True}],
                'resources': {'requests': {'cpu': '200m', 'memory': '512Mi'}, 'limits': {'cpu': '1', 'memory': '1Gi'}},
            }, {'name': 'sidecar', 'image': 'must-not-be-inherited'}],
        }}}}

    def run_release_block(self, deployment=None, corruption=''):
        source = (SCRIPTS / 'ring-release-fleet.sh').read_text()
        block = source.split('echo "Installing fleet harness-drift roll CronJob"', 1)[1].split(
            'echo "Installing harness auto-upgrade CronJob"', 1,
        )[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.forgejo' / 'scripts').mkdir(parents=True)
            (root / 'hive-deploy-post-rollout.json').write_text(json.dumps(deployment or self.deployment()))
            facade = root / '.forgejo' / 'scripts' / 'kubectl'
            facade.write_text(f'#!{sys.executable}\n' + '''import json, os, pathlib, sys
root = pathlib.Path.cwd()
arguments = sys.argv[1:]
with (root / 'calls').open('a') as stream:
    stream.write(json.dumps(arguments) + '\\n')
if arguments[:2] == ['apply', '-f']:
    desired = json.loads(pathlib.Path(arguments[2]).read_text())
    (root / 'applied.json').write_text(json.dumps(desired))
elif arguments == ['-n', 'shizuha-hive', 'get', 'cronjob', 'fleet-harness-drift-roll', '-o', 'json']:
    desired = json.loads((root / 'applied.json').read_text())
    container = desired['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]
    corruption = os.environ.get('DRIFT_TEST_CORRUPTION')
    if corruption == 'command':
        container.pop('command')
    elif corruption == 'environment':
        container['env'] = []
    elif corruption == 'image':
        container['image'] = 'localhost:30500/shizuha-hive:hive-stale'
    elif corruption == 'arguments':
        container['args'] = ['--web']
    elif corruption == 'suspended':
        desired['spec']['suspend'] = True
    elif corruption == 'catch-up':
        desired['spec'].pop('startingDeadlineSeconds')
    elif corruption == 'probe':
        container['readinessProbe'] = {'exec': {'command': ['true']}}
    if not container.get('args'):
        container.pop('args', None)
    desired['metadata']['resourceVersion'] = '123'
    print(json.dumps(desired))
else:
    raise SystemExit('unexpected kubectl operation: ' + str(arguments))
''')
            facade.chmod(0o755)
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', block.replace('/tmp/', directory + '/')],
                                    cwd=root, env={**os.environ, 'DRIFT_TEST_CORRUPTION': corruption}, text=True, capture_output=True)
            calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()] if (root / 'calls').exists() else []
            manifest = json.loads((root / 'applied.json').read_text()) if (root / 'applied.json').exists() else None
            return result, manifest, calls

    def test_actual_release_caller_inherits_qualified_hive_template(self):
        deployment = self.deployment()
        result, manifest, calls = self.run_release_block(deployment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 2)
        pod = manifest['spec']['jobTemplate']['spec']['template']['spec']
        container = pod['containers'][0]
        base = deployment['spec']['template']['spec']['containers'][0]
        for field in ('image', 'env', 'envFrom', 'volumeMounts', 'resources'):
            self.assertEqual(container[field], base[field])
        self.assertEqual(container['command'], ['python', 'manage.py', 'roll_harness_drift', '--max-seats', '6'])
        self.assertEqual(container['args'], [])
        for field in ('ports', 'readinessProbe', 'livenessProbe', 'startupProbe', 'lifecycle'):
            self.assertNotIn(field, container)
        for field in ('serviceAccountName', 'volumes', 'securityContext', 'nodeSelector'):
            self.assertEqual(pod[field], deployment['spec']['template']['spec'][field])
        self.assertEqual(len(pod['containers']), 1)
        self.assertEqual(manifest['spec']['schedule'], '*/15 * * * *')
        self.assertEqual(manifest['spec']['startingDeadlineSeconds'], 900)
        self.assertEqual(manifest['spec']['concurrencyPolicy'], 'Forbid')
        self.assertFalse(manifest['spec']['suspend'])
        self.assertEqual(manifest['spec']['jobTemplate']['spec']['ttlSecondsAfterFinished'], 3600)

    def test_release_verification_rejects_stale_or_incomplete_template(self):
        for corruption in ('command', 'environment', 'image', 'arguments', 'suspended', 'probe', 'catch-up'):
            with self.subTest(corruption=corruption):
                result, _, calls = self.run_release_block(corruption=corruption)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(calls), 2)
                self.assertIn('harness-drift roll', result.stderr)

    def test_missing_native_activation_refuses_before_apply(self):
        deployment = self.deployment()
        deployment['spec']['template']['spec']['containers'][0]['env'][0]['value'] = 'false'
        result, manifest, calls = self.run_release_block(deployment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not enable native runtime authority', result.stderr)
        self.assertIsNone(manifest)
        self.assertEqual(calls, [])

    def test_recovery_grant_is_exact_name_read_and_patch_only(self):
        documents = list(yaml.safe_load_all((ROOT / 'k8s' / 'hive-ring-recover-rbac.yaml').read_text()))
        role, binding = [document for document in documents
                         if document['metadata']['name'] == 'hive-ring-recover-harness-drift']
        self.assertEqual(role['kind'], 'Role')
        self.assertEqual(role['metadata']['namespace'], 'shizuha-hive')
        self.assertEqual(role['rules'], [{'apiGroups': ['batch'], 'resources': ['cronjobs'],
                                        'resourceNames': ['fleet-harness-drift-roll'], 'verbs': ['get', 'patch']}])
        self.assertEqual(binding['kind'], 'RoleBinding')
        self.assertEqual(binding['metadata']['namespace'], 'shizuha-hive')
        self.assertEqual(binding['subjects'], [{'kind': 'ServiceAccount', 'name': 'hive-ring-recover', 'namespace': 'origin'}])
        self.assertEqual(binding['roleRef'], {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role',
                                            'name': role['metadata']['name']})

    def test_exact_legacy_target_can_be_cas_claimed_but_not_foreign_owner(self):
        sys.path.insert(0, str(SCRIPTS))
        try:
            import ring_executor
            import ring_fence
        finally:
            sys.path.remove(str(SCRIPTS))
        holder = ring_fence.Holder(repo='shizuha-labs/hive', workflow='deploy.yml', run_id='100',
                                   run_number='100', job='recover', ring='fleet', release_sha='a' * 40, restored_sha='b' * 40)
        target = ('shizuha-hive', 'cronjob', 'fleet-harness-drift-roll')
        self.assertIn(target, ring_executor.protected_targets(holder))
        self.assertFalse(ring_executor.target_is_protected(holder, *target[:2], 'other-drift-roll'))
        body = {'metadata': {'name': target[2], 'uid': 'legacy-uid', 'resourceVersion': '44',
                             'labels': {'app.kubernetes.io/part-of': 'shizuha-platform'}, 'annotations': {}},
                'spec': {'jobTemplate': {'spec': {'template': {'spec': {
                    'serviceAccountName': 'hive-controller',
                    'containers': [{'name': 'roll', 'image': 'localhost:30500/shizuha-hive:hive-legacy'}],
                }}}}}}
        self.assertTrue(ring_executor.is_recovery_bootstrap_target(*target, body))
        for change in ('label', 'service-account', 'container'):
            invalid = copy.deepcopy(body)
            if change == 'label':
                invalid['metadata']['labels'] = {}
            elif change == 'service-account':
                invalid['spec']['jobTemplate']['spec']['template']['spec']['serviceAccountName'] = 'foreign'
            else:
                invalid['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['name'] = 'foreign'
            self.assertFalse(ring_executor.is_recovery_bootstrap_target(*target, invalid))
        def read(arguments, **kwargs):
            if '-l' in arguments:
                return subprocess.CompletedProcess(arguments, 0, json.dumps({'items': []}), '')
            if arguments[arguments.index('get') + 1:arguments.index('get') + 3] == list(target[1:]):
                return subprocess.CompletedProcess(arguments, 0, json.dumps(body), '')
            return subprocess.CompletedProcess(arguments, 1, '', 'NotFound')
        api = SimpleNamespace(run=read, json_patch=mock.Mock(return_value={'metadata': {'resourceVersion': '45'}}))
        fence = SimpleNamespace(holder=holder, token=8, api=api, validate=mock.Mock())
        ring_executor.claim_existing_targets(fence, allow_absent=True, expected_token_ceiling=7)
        api.json_patch.assert_called_once()
        operations = api.json_patch.call_args.args[-1]
        self.assertIn({'op': 'test', 'path': '/metadata/resourceVersion', 'value': '44'}, operations)
        body['metadata']['annotations'] = {'shizuha.io/ring-generation': 'foreign', 'shizuha.io/fence-token': '9'}
        with self.assertRaises(ring_fence.Conflict):
            ring_executor.claim_existing_targets(fence, allow_absent=True, expected_token_ceiling=7)
