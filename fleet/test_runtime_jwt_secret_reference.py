import os
from unittest.mock import MagicMock, patch
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from fleet.management.commands.provision_user_daemon import Command, runtime_jwt_secret_ref


class RuntimeJWTSecretReferenceTests(SimpleTestCase):
    def setUp(self):
        self.infrastructure = {'HIVE_RUNTIME_HOST_CLI': '/srv/runtime-checkout', 'HIVE_RUNTIME_BASE_IMAGE': 'registry.example.test/base@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'HIVE_RUNTIME_BROKER_IMAGE': 'registry.example.test/broker@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'HIVE_RUNTIME_AGENT_NODE_SELECTOR': 'kubernetes.io/hostname=agent-node', 'HIVE_RUNTIME_DAEMON_NODE_SELECTOR': 'kubernetes.io/hostname=daemon-node'}
        environment = patch.dict(os.environ,self.infrastructure)
        environment.start()
        self.addCleanup(environment.stop)

    def test_generated_daemon_has_required_secret_reference_without_literal(self):
        apps = MagicMock()
        with patch.dict(os.environ, self.infrastructure, clear=True):
            Command()._apply_deployment(apps, 'fleet-daemon-u9', 'rt-fleet-u9',
                'fleet-u9', 'user-daemon-u9-link', 9, 'demo', 'runtime:reviewed')
        deployment = apps.create_namespaced_deployment.call_args.args[1]
        variables = deployment.spec.template.spec.containers[0].env
        jwt = next(v for v in variables if v.name == 'JWT_SECRET_KEY')
        self.assertIsNone(jwt.value)
        self.assertEqual(jwt.value_from.secret_key_ref.name, 'runtime-platform-jwt-v2')
        self.assertEqual(jwt.value_from.secret_key_ref.key, 'jwt-secret')
        self.assertIs(jwt.value_from.secret_key_ref.optional, False)

    def test_custom_deployment_can_select_its_secret(self):
        with patch.dict(os.environ, {'USER_DAEMON_JWT_SECRET_NAME': 'external-runtime-auth',
                                     'USER_DAEMON_JWT_SECRET_KEY': 'signing-key'}):
            ref = runtime_jwt_secret_ref()
        self.assertEqual((ref.name, ref.key, ref.optional), ('external-runtime-auth', 'signing-key', False))

    def test_empty_or_invalid_config_fails_before_deployment_write(self):
        for name, key in [('', 'jwt-secret'), ('runtime-platform-jwt', ''), ('../secret', 'key')]:
            apps = MagicMock()
            with self.subTest(name=name, key=key), patch.dict(os.environ, {
                'USER_DAEMON_JWT_SECRET_NAME': name, 'USER_DAEMON_JWT_SECRET_KEY': key,
            }):
                with self.assertRaisesRegex(CommandError, "USER_DAEMON_JWT_SECRET"):
                    Command()._apply_deployment(apps, 'fleet-daemon-u9', 'rt-fleet-u9',
                        'fleet-u9', 'user-daemon-u9-link', 9, '', 'runtime:reviewed')
            apps.create_namespaced_deployment.assert_not_called()
            apps.patch_namespaced_deployment.assert_not_called()
