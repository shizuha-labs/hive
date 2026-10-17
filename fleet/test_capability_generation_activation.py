import json
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

from django.test import SimpleTestCase


SCRIPTS = Path(__file__).resolve().parents[1] / '.forgejo' / 'scripts'


class CapabilityGenerationActivationCallerTests(SimpleTestCase):
    @staticmethod
    def pod(name, *, phase='Running', ready=True, container='hive'):
        return {
            'metadata': {'name': name},
            'spec': {'nodeName': 'fixture-node'},
            'status': {
                'phase': phase,
                'conditions': [{'type': 'Ready', 'status': 'True' if ready else 'False'}],
                'containerStatuses': [{'name': container, 'ready': ready}],
            },
        }

    def run_activation(self, pods, *, activation_status=0):
        source = (SCRIPTS / 'ring-release-fleet.sh').read_text()
        selector = 'h725_ready_pod() {' + source.split('h725_ready_pod() {', 1)[1].split('\n}', 1)[0] + '\n}'
        activation = source[source.index('CAPABILITY_HIVE_POD='):]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / '.forgejo' / 'scripts'
            scripts.mkdir(parents=True)
            shutil.copy2(SCRIPTS / 'select-ready-pod.py', scripts)
            (root / 'pods.json').write_text(json.dumps({'items': pods}))
            fake = scripts / 'kubectl'
            fake.write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
                import json
                import os
                import pathlib
                import sys
                arguments = sys.argv[1:]
                if arguments == ['-n', 'shizuha-hive', 'get', 'pod', '-l', 'app=hive', '-o', 'json']:
                    print(pathlib.Path('pods.json').read_text())
                elif arguments[:3] == ['-n', 'shizuha-hive', 'exec']:
                    pathlib.Path('executed.json').write_text(json.dumps(arguments))
                    sys.exit(int(os.environ['ACTIVATION_STATUS']))
                else:
                    sys.exit(23)
                '''))
            fake.chmod(0o755)
            process = subprocess.run(
                ['bash', '-c', 'set -euo pipefail\n' + selector + '\n' + activation],
                cwd=root, text=True, capture_output=True,
                env={**os.environ, 'SHA': 'a' * 40, 'SOURCE_TREE': 'b' * 40,
                     'IMAGE_INDEX_DIGEST': 'sha256:' + 'c' * 64,
                     'HIVE_POD': 'hive-stale-before-roll',
                     'ACTIVATION_STATUS': str(activation_status)},
            )
            receipt = root / 'executed.json'
            return process, json.loads(receipt.read_text()) if receipt.exists() else None

    def test_final_activation_reselects_named_ready_api_not_shared_label_job(self):
        process, command = self.run_activation([
            self.pod('hive-on-demand-old', phase='Succeeded', container='reconciler'),
            self.pod('hive-on-demand-current', container='reconciler'),
            self.pod('hive-new-ready-api'),
        ])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(command[:7], ['-n', 'shizuha-hive', 'exec', 'hive-new-ready-api', '-c', 'hive', '--'])
        self.assertEqual(command[7:10], ['python', 'manage.py', 'activate_capability_evidence_generation'])
        self.assertIn('hive-' + 'a' * 40, command)
        self.assertIn('sha256:' + 'c' * 64, command)

    def test_final_activation_fails_closed_without_ready_api(self):
        process, command = self.run_activation([
            self.pod('hive-on-demand-current', container='reconciler'),
            self.pod('hive-not-ready', ready=False),
        ])
        self.assertNotEqual(process.returncode, 0)
        self.assertIsNone(command)

    def test_final_activation_preserves_management_command_failure(self):
        process, command = self.run_activation(
            [self.pod('hive-new-ready-api')], activation_status=19,
        )
        self.assertEqual(process.returncode, 19)
        self.assertIsNotNone(command)
