"""Per-user host-dist paths are managed inputs, never an operator home."""
import os
from unittest import mock
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from fleet.management.commands.provision_user_daemon import Command


class PortableDaemonPathsTests(SimpleTestCase):
    def render(self, command=Command):
        apps = mock.MagicMock()
        command()._apply_deployment(apps, 'daemon-u2','did-u2','agents-u2','secret-u2',2,'owner', 'registry.example.invalid/runtime@sha256:'+'a'*64)
        return apps.create_namespaced_deployment.call_args.args[1]

    @mock.patch.dict(os.environ, {'HIVE_RUNTIME_BASE_IMAGE': 'registry.example.test/base@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'HIVE_RUNTIME_BROKER_IMAGE': 'registry.example.test/broker@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'HIVE_RUNTIME_AGENT_NODE_SELECTOR': 'kubernetes.io/hostname=agent-node', 'HIVE_RUNTIME_DAEMON_NODE_SELECTOR': 'kubernetes.io/hostname=daemon-node', 'HIVE_RUNTIME_HOST_CLI': '/srv/runtime-checkout'})
    def test_actual_render_uses_configured_host_directory_and_stable_container_layout(self):
        deployment = self.render()
        spec = deployment.spec.template.spec
        host = next(v for v in spec.volumes if v.name == 'host-cli')
        self.assertEqual(host.host_path.path, '/srv/runtime-checkout')
        daemon = spec.containers[0]
        mount = next(v for v in daemon.volume_mounts if v.name == 'host-cli')
        self.assertEqual(mount.mount_path, '/opt/shizuha-runtime')
        self.assertTrue(mount.read_only)
        self.assertIn('exec node /opt/shizuha-runtime/dist/shizuha.js', daemon.args[0])
        self.assertNotIn('/srv/runtime-checkout', daemon.args[0])

    def test_absent_or_ambiguous_source_refuses_before_deployment_api(self):
        for value in ('', 'relative/checkout', '/', '/srv/../private', '/srv//checkout', '/srv/checkout\nother'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {'HIVE_RUNTIME_HOST_CLI':value}):
                apps = mock.MagicMock()
                with self.assertRaises(CommandError):
                    Command()._apply_deployment(apps,'daemon','did','ns','secret',2,'owner','image')
                apps.create_namespaced_deployment.assert_not_called()
                apps.patch_namespaced_deployment.assert_not_called()

    @mock.patch.dict(os.environ, {'HIVE_RUNTIME_HOST_CLI':''})
    def test_apply_refuses_missing_path_before_credentials_or_cluster_access(self):
        with mock.patch('fleet.provisioner.get_k8s_client') as cluster:
            with self.assertRaises(CommandError):
                Command().handle(user_id=2, username='owner', apply=True, daemon_id='', link_only=False)
        cluster.assert_not_called()

    @mock.patch.dict(os.environ, {'HIVE_RUNTIME_HOST_CLI':'/srv/runtime-checkout',
        'HIVE_RUNTIME_BASE_IMAGE':'registry.example.test/base@sha256:'+'b'*64,
        'HIVE_RUNTIME_BROKER_IMAGE':'registry.example.test/broker@sha256:'+'c'*64,
        'HIVE_RUNTIME_AGENT_NODE_SELECTOR':'kubernetes.io/hostname=agent-node',
        'HIVE_RUNTIME_DAEMON_NODE_SELECTOR':'kubernetes.io/hostname=daemon-node'})
    def test_render_uses_explicit_images_and_each_distinct_placement(self):
        deployment=self.render()
        spec=deployment.spec.template.spec
        self.assertTrue(spec.node_selector == {'kubernetes.io/hostname':'daemon-node'}, 'explicit daemon placement must own render')
        self.assertTrue(spec.containers[0].image.startswith('registry.example.test/base@'), 'explicit base image must own render')
        env={item.name:item.value for item in spec.containers[0].env}
        self.assertTrue(env['SHIZUHA_BROKER_IMAGE'].startswith('registry.example.test/broker@'), 'explicit broker image must own render')
        self.assertTrue(env['SHIZUHA_FLEET_NODE_SELECTOR'] == 'kubernetes.io/hostname=agent-node', 'explicit agent placement must own render')

    @mock.patch.dict(os.environ, {'HIVE_RUNTIME_HOST_CLI':'/srv/runtime-checkout',
        'HIVE_RUNTIME_BASE_IMAGE':'','HIVE_RUNTIME_BROKER_IMAGE':'',
        'HIVE_RUNTIME_AGENT_NODE_SELECTOR':'','HIVE_RUNTIME_DAEMON_NODE_SELECTOR':''})
    def test_absent_infrastructure_refuses_before_credentials_or_cluster_access(self):
        with mock.patch('fleet.provisioner.get_k8s_client') as cluster:
            with self.assertRaises(CommandError):
                Command().handle(user_id=2,username='owner',apply=True,daemon_id='',link_only=False)
        cluster.assert_not_called()

    def test_malformed_or_missing_field_refuses_before_deployment_api(self):
        configured = {'HIVE_RUNTIME_BASE_IMAGE': 'registry.example.test/base@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'HIVE_RUNTIME_BROKER_IMAGE': 'registry.example.test/broker@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'HIVE_RUNTIME_AGENT_NODE_SELECTOR': 'kubernetes.io/hostname=agent-node', 'HIVE_RUNTIME_DAEMON_NODE_SELECTOR': 'kubernetes.io/hostname=daemon-node', 'HIVE_RUNTIME_HOST_CLI': '/srv/runtime-checkout'}
        bad = {'HIVE_RUNTIME_BASE_IMAGE':['','bad image'], 'HIVE_RUNTIME_BROKER_IMAGE':['','https://bad image'],
               'HIVE_RUNTIME_AGENT_NODE_SELECTOR':['','missing-equals','key=value,key=other'],
               'HIVE_RUNTIME_DAEMON_NODE_SELECTOR':['','key=value\nother','key=']}
        for name,values in bad.items():
            for value in values:
                with self.subTest(field=name,value=value), mock.patch.dict(os.environ,{**configured,name:value}):
                    apps=mock.MagicMock()
                    with self.assertRaises(CommandError):
                        Command()._apply_deployment(apps,'daemon','did','ns','secret',2,'owner','image')
                    apps.create_namespaced_deployment.assert_not_called()
                    apps.patch_namespaced_deployment.assert_not_called()
