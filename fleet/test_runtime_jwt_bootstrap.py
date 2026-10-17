import base64
import copy
import importlib.util
from pathlib import Path
import secrets
import unittest
from unittest.mock import patch

module_path = Path(__file__).resolve().parents[1] / '.forgejo/scripts/ensure-runtime-platform-jwt.py'
spec = importlib.util.spec_from_file_location('runtime_jwt_bootstrap', module_path)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class RuntimeJWTBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.value = secrets.token_urlsafe(32)
        self.encoded = base64.b64encode(self.value.encode()).decode()
        self.entry = {'name': 'JWT_SECRET_KEY', 'valueFrom': {'secretKeyRef': {
            'name': bootstrap.SECRET, 'key': bootstrap.KEY, 'optional': False}}}
        self.source = {'metadata': {'resourceVersion': '5'}, 'spec': {'template': {'spec': {
            'containers': [{'env': [self.entry]}]}}}}
        self.target = {'metadata': {'resourceVersion': '9'}, 'immutable': True,
                       'data': {bootstrap.KEY: self.encoded}}
        self.calls = []

    def call(self, *args, data=None):
        self.calls.append(args)
        self.assertEqual(args[0], 'get', 'verification must never mutate Kubernetes')
        if args[:2] == ('get', 'deployment'):
            return copy.deepcopy(self.source)
        if args[:2] == ('get', 'secret'):
            self.assertEqual(args[2], 'runtime-platform-jwt-v2')
            return copy.deepcopy(self.target)
        self.fail('unexpected Kubernetes operation')

    def test_replacement_secret_reference_verified_without_mutation_or_disclosure(self):
        result = bootstrap.ensure(self.call)
        self.assertNotIn(self.value, result)
        self.assertNotIn(self.encoded, result)
        self.assertEqual(len(self.calls), 3)

    def test_legacy_literal_can_never_be_projected_into_replacement(self):
        self.entry.clear()
        self.entry.update(name='JWT_SECRET_KEY', value=self.value)
        with self.assertRaisesRegex(bootstrap.BootstrapError, 'must require'):
            bootstrap.ensure(self.call)

    def test_old_secret_optional_or_wrong_key_reference_rejected(self):
        for update in ({'name': 'runtime-platform-jwt'}, {'optional': True}, {'key': 'other'}):
            with self.subTest(update=update):
                original = self.entry['valueFrom']['secretKeyRef'].copy()
                self.entry['valueFrom']['secretKeyRef'].update(update)
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.ensure(self.call)
                self.entry['valueFrom']['secretKeyRef'] = original

    def test_absent_or_duplicate_configuration_rejected(self):
        for entries in ([], [self.entry, self.entry]):
            self.source['spec']['template']['spec']['containers'][0]['env'] = entries
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.ensure(self.call)

    def test_missing_short_invalid_or_mutable_secret_rejected(self):
        for target in ({}, {'immutable': False, 'data': {bootstrap.KEY: self.encoded}},
                       {'immutable': True, 'data': {}},
                       {'immutable': True, 'data': {bootstrap.KEY: '%%%'}},
                       {'immutable': True, 'data': {bootstrap.KEY: base64.b64encode(b'x').decode()}}):
            self.target = target
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.ensure(self.call)

    def test_changed_source_version_fails_readonly(self):
        count = 0
        def call(*args, **kwargs):
            nonlocal count
            if args[:2] == ('get', 'deployment'):
                count += 1
                if count > 1:
                    self.source['metadata']['resourceVersion'] = '6'
            return self.call(*args, **kwargs)
        with self.assertRaisesRegex(bootstrap.BootstrapError, 'changed'):
            bootstrap.ensure(call)

    def test_cli_subprocess_error_does_not_disclose_secret(self):
        import subprocess
        response = subprocess.CompletedProcess([], 1, self.value, self.encoded)
        with patch.object(bootstrap.subprocess, 'run', return_value=response):
            with self.assertRaises(bootstrap.BootstrapError) as caught:
                bootstrap.kubectl('get', 'secret', bootstrap.SECRET)
        self.assertNotIn(self.value, str(caught.exception))
        self.assertNotIn(self.encoded, str(caught.exception))

    def test_rbac_allows_only_named_read_without_secret_placeholder(self):
        import yaml
        docs = list(yaml.safe_load_all((module_path.parents[2] / 'k8s/runtime-jwt-bootstrap-rbac.yaml').read_text()))
        self.assertNotIn('Secret', [d['kind'] for d in docs])
        role = next(d for d in docs if d['kind'] == 'Role')
        rule = next(r for r in role['rules'] if r['resources'] == ['secrets'])
        self.assertEqual(rule['resourceNames'], [bootstrap.SECRET])
        self.assertEqual(rule['verbs'], ['get'])
