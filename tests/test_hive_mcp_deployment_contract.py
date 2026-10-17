from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / '.forgejo/scripts/render-hive-mcp-deployment.py'
SPEC = importlib.util.spec_from_file_location('hive_mcp_renderer', SCRIPT)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


def declared_deployment():
    return {
        'kind': 'Deployment',
        'metadata': {'name': 'shizuha-hive-mcp', 'namespace': 'shizuha'},
        'spec': {'template': {'spec': {'containers': [{
            'name': 'hive-mcp', 'image': 'bootstrap', 'env': [
                {'name': 'SHIZUHA_MCP_OAUTH_ENFORCE', 'value': 'true'},
                {'name': 'SHIZUHA_MCP_RESOURCE_URL', 'value': 'https://shizuha.com/mcp/hive'},
                {'name': 'SHIZUHA_OAUTH_ISSUER', 'value': 'https://shizuha.com/id'},
                {'name': 'SHIZUHA_PUBLIC_HOST', 'value': 'shizuha.com'},
                {'name': 'SHIZUHA_PUBLIC_SCHEME', 'value': 'https'},
            ],
        }]}}},
    }


def live_deployment(image='localhost:30500/shizuha-hive-mcp:mcp-aaaaaaa'):
    current = declared_deployment()
    current['metadata'].update(uid='live-mcp-uid', resourceVersion='27')
    current['spec']['template']['spec']['containers'][0].update(image=image, env=[
        {'name': 'SHIZUHA_HIVE_JWT_TOKEN', 'valueFrom': {'secretKeyRef': {'name': 'existing', 'key': 'token'}}},
        {'name': 'CUSTOM_UNRELATED', 'value': 'preserved'},
        {'name': 'SHIZUHA_MCP_OAUTH_ENFORCE', 'value': 'false'},
    ])
    return current


class HiveMcpDeploymentContractTests(unittest.TestCase):
    target = 'localhost:30500/shizuha-hive-mcp:mcp-bbbbbbb'

    def test_candidate_image_and_declared_auth_are_one_identity_guarded_patch(self):
        current = live_deployment()
        original = deepcopy(current)
        patch = renderer.render_patch({'kind': 'List', 'items': [declared_deployment()]}, current, self.target)
        self.assertEqual(patch[:2], [
            {'op': 'test', 'path': '/metadata/uid', 'value': 'live-mcp-uid'},
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '27'},
        ])
        self.assertEqual(patch[2]['value'], self.target)
        environment = {entry['name']: entry for entry in patch[3]['value']}
        self.assertEqual(environment['SHIZUHA_MCP_OAUTH_ENFORCE']['value'], 'true')
        self.assertEqual(environment['CUSTOM_UNRELATED']['value'], 'preserved')
        self.assertEqual(environment['SHIZUHA_HIVE_JWT_TOKEN'], current['spec']['template']['spec']['containers'][0]['env'][0])
        self.assertEqual(current, original)

    def test_missing_or_disabled_security_declaration_fails_closed(self):
        for invalid in ('false', '', None):
            declaration = declared_deployment()
            declaration['spec']['template']['spec']['containers'][0]['env'][0]['value'] = invalid
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                renderer.render_patch(declaration, live_deployment(), self.target)

    def test_missing_identity_or_wrong_target_cannot_generate_patch(self):
        for field in ('uid', 'resourceVersion', 'name', 'namespace'):
            current = live_deployment()
            current['metadata'].pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                renderer.render_patch(declared_deployment(), current, self.target)
        with self.assertRaises(ValueError):
            renderer.render_patch(declared_deployment(), live_deployment(), 'local:temporary')

    def test_cli_consumes_manifest_and_live_state_without_printing_secrets_to_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'declared.json').write_text(
                json.dumps(declared_deployment()) + '\n' + json.dumps({'kind': 'Service'})
            )
            (root / 'current.json').write_text(json.dumps(live_deployment()))
            result = subprocess.run([
                'python3', str(SCRIPT), str(root / 'declared.json'), str(root / 'current.json'), self.target,
            ], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[2]['value'], self.target)
        self.assertEqual(result.stderr, '')

    def test_workflow_consumes_exact_manifest_and_runs_real_auth_regression(self):
        workflow = (ROOT / '.forgejo/workflows/deploy-hive-mcp.yml').read_text()
        self.assertIn('git -C "$SOURCE_DIR" rev-parse FETCH_HEAD', workflow)
        self.assertIn('${DEPLOY_SHA}:k3s/manifests/shizuha-hive-mcp.yaml', workflow)
        self.assertIn('HIVE_MCP_MANIFEST_JSON=', workflow)
        self.assertIn('command: ["python", "test_oauth_defaults.py", "-v"]', workflow)
        self.assertIn('for credential in absent invalid', workflow)
        self.assertIn('[ "\\${CODE}" = "401" ]', workflow)
        self.assertNotIn('[ "\\${CODE}" != "000" ]', workflow)


if __name__ == '__main__':
    unittest.main()
