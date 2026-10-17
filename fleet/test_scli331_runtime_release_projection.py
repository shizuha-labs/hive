"""SCLI-331: Hive is a read-only projection of rt-fleet release authority."""
from datetime import timedelta
from types import SimpleNamespace
from kubernetes.client.rest import ApiException
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from fleet import provisioner
from fleet.management.commands.provision_user_daemon import Command as ProvisionUserDaemonCommand
from fleet.models import DaemonRegistry, FleetAgent
from fleet.runtime_release import RuntimeReleaseProjectionUnavailable
from fleet.serializers import FleetAgentCreateSerializer, SwitchBackendSerializer
from fleet.ui_views import FleetAgentSwitchBackendUIView


class FleetRuntimeReleaseProjectionTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = '/api/v1/fleet/runtime-image'
        self.addCleanup(cache.delete, 'fleet:desired-runtime-image')
        self.release = {
            'generation': 7,
            'image_digest': 'sha256:' + ('a' * 64),
            'display_tag': 'localhost:30500/shizuha-agent-runtime:harness-reviewed',
            'source_commit': 'b' * 40,
            'intent': 'promote',
            'rollback_of_generation': None,
            'approved_at': '2026-07-12T22:00:00Z',
        }

    def _daemon(
        self,
        release=None,
        status=DaemonRegistry.Status.CONNECTED,
        daemon_id='rt-fleet-s1',
        fleet_id='shizuha-fleet',
        last_seen=None,
    ):
        return DaemonRegistry.objects.create(
            daemon_id=daemon_id,
            fleet_id=fleet_id,
            runtime='k8s',
            status=status,
            last_seen=last_seen or timezone.now(),
            agent_runtime_image='localhost:30500/shizuha-agent-runtime:stale-peer',
            harness_report={'desired_runtime_release': release} if release is not None else {},
        )

    def test_get_projects_the_reviewed_daemon_record_not_legacy_image_or_cache(self):
        cache.set(
            'fleet:desired-runtime-image',
            'localhost:30500/shizuha-agent-runtime:harness-unreviewed-cache',
            None,
        )
        self._daemon(self.release)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'image': self.release['display_tag'],
            'image_digest': self.release['image_digest'],
            'generation': 7,
            'broker_image': settings.FLEET_BROKER_IMAGE,
            'intent': 'promote',
            'source_commit': self.release['source_commit'],
            'approved_at': self.release['approved_at'],
        })

    def test_missing_or_invalid_reviewed_record_fails_closed(self):
        self._daemon(None)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertIn('reviewed DesiredRuntimeRelease', response.json()['error'])

    def test_structurally_invalid_reviewed_record_fails_closed(self):
        invalid_records = (
            {**self.release, 'image_digest': 'sha256:short'},
            {**self.release, 'source_commit': 'not-a-commit'},
            {**self.release, 'approved_at': 'not-a-time'},
            {**self.release, 'intent': 'promote', 'rollback_of_generation': 2},
            {**self.release, 'intent': 'rollback', 'rollback_of_generation': 7},
            {**self.release, 'display_tag': 'localhost:30500/not-the-runtime:tag'},
        )
        for index, release in enumerate(invalid_records):
            with self.subTest(release=release):
                DaemonRegistry.objects.all().delete()
                self._daemon(release, daemon_id=f'ignored-{index}')
                self._daemon(release)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 503)
                self.assertIn('invalid DesiredRuntimeRelease', response.json()['error'])

    def test_disconnected_authoritative_daemon_fails_closed(self):
        self._daemon(self.release, status=DaemonRegistry.Status.DISCONNECTED)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertIn('disconnected, stale, or unavailable', response.json()['error'])

    def test_stale_authoritative_daemon_fails_closed(self):
        self._daemon(self.release, last_seen=timezone.now() - timedelta(seconds=31))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertIn('disconnected, stale, or unavailable', response.json()['error'])

    def test_other_connected_daemons_never_substitute_for_authoritative_rt_fleet(self):
        newer = {**self.release, 'generation': 99, 'image_digest': 'sha256:' + ('9' * 64)}
        self._daemon(newer, daemon_id='personal-hritik', fleet_id='personal-fleet')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)

        authoritative = self._daemon(self.release)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['generation'], 7)
        self.assertEqual(response.json()['image_digest'], self.release['image_digest'])
        self.assertNotEqual(authoritative.daemon_id, 'personal-hritik')

    def test_staff_post_cannot_reintroduce_mutable_hive_authority(self):
        user = get_user_model().objects.create_user(
            username='release-admin', email='release-admin@shizuha.com', password='pw', is_staff=True,
        )
        self.client.force_authenticate(user=user)
        response = self.client.post(self.url, {'image': 'localhost:30500/shizuha-agent-runtime:older'}, format='json')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'runtime_release_source_only')


class FleetHarnessVersionsContractTest(TestCase):
    """HIVE-1891: one malformed optional daemon map must not crash the API."""

    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username='harness-admin',
            email='harness-admin@shizuha.com',
            password='pw',
            is_staff=True,
        )
        self.client.force_authenticate(user=self.user)
        self.url = '/api/v1/fleet/harness-versions'
        self.release = {
            'generation': 7,
            'image_digest': 'sha256:' + ('a' * 64),
            'display_tag': 'localhost:30500/shizuha-agent-runtime:harness-reviewed',
            'source_commit': 'b' * 40,
            'intent': 'promote',
            'rollback_of_generation': None,
            'approved_at': '2026-08-12T22:00:00Z',
        }

    def _daemon(self, **report):
        return DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1',
            fleet_id='shizuha-fleet',
            runtime='k8s',
            status=DaemonRegistry.Status.CONNECTED,
            last_seen=timezone.now(),
            agent_runtime_image='localhost:30500/shizuha-agent-runtime:harness-reviewed',
            harness_report={
                'desired_runtime_release': self.release,
                **report,
            },
        )

    @mock.patch('fleet.views.FleetHarnessVersionsView._fleet_agents', return_value=[])
    @mock.patch('fleet.harness_versions.npm_latest', return_value='1.1.0')
    @mock.patch(
        'fleet.harness_versions.image_harness_labels',
        return_value={'codex': '1.0.0'},
    )
    def test_malformed_optional_maps_degrade_to_registry_labels(
        self, image_labels, _npm_latest, _fleet_agents,
    ):
        self._daemon(
            image_harness=['malformed', 'instead-of-a-map'],
            daemon_cli='malformed-instead-of-a-map',
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['harnesses'])
        codex = next(
            item for item in payload['harnesses']
            if item['harness'] == 'codex'
        )
        self.assertEqual(codex['current'], '1.0.0')
        self.assertEqual(codex['latest'], '1.1.0')
        self.assertTrue(codex['upgrade_available'])
        image_labels.assert_called_once_with(self.release['display_tag'])

    def test_unavailable_authoritative_projection_is_structured_503(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)
        self.assertIn('error', response.json())

    @mock.patch('fleet.views.FleetHarnessVersionsView._fleet_agents', return_value=[])
    @mock.patch('fleet.harness_versions.npm_latest', return_value='1.0.0')
    @mock.patch(
        'fleet.harness_versions.image_harness_labels',
        return_value={'codex': '1.0.0'},
    )
    def test_digest_form_of_reviewed_image_is_not_a_rollout(
        self, _image_labels, _npm_latest, _fleet_agents,
    ):
        """Tag vs @digest of the SAME release is convergence, not rolling."""
        self._daemon()
        DaemonRegistry.objects.filter(daemon_id='rt-fleet-s1').update(
            agent_runtime_image=(
                'localhost:30500/shizuha-agent-runtime@' + self.release['image_digest']
            ),
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            payload['desired_runtime_image'], self.release['display_tag'],
        )
        self.assertTrue(payload['agent_runtime_image'].endswith(self.release['image_digest']))
        self.assertFalse(payload['rollout_in_progress'])

    @mock.patch('fleet.views.FleetHarnessVersionsView._fleet_agents', return_value=[])
    @mock.patch('fleet.harness_versions.npm_latest', return_value='1.0.0')
    @mock.patch(
        'fleet.harness_versions.image_harness_labels',
        return_value={'codex': '1.0.0'},
    )
    def test_different_digest_still_counts_as_rolling(
        self, _image_labels, _npm_latest, _fleet_agents,
    ):
        self._daemon()
        DaemonRegistry.objects.filter(daemon_id='rt-fleet-s1').update(
            agent_runtime_image=(
                'localhost:30500/shizuha-agent-runtime@sha256:' + ('f' * 64)
            ),
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['rollout_in_progress'])



class ReviewedRuntimeProvisioningTest(TestCase):
    reviewed_image = 'localhost:30500/shizuha-agent-runtime:harness-reviewed'
    reviewed_digest_image = (
        'localhost:30500/shizuha-agent-runtime@sha256:' + ('c' * 64)
    )

    def setUp(self):
        self.addCleanup(cache.delete, 'fleet:desired-runtime-image')
        self.release = {
            'generation': 3,
            'image_digest': 'sha256:' + ('c' * 64),
            'display_tag': self.reviewed_image,
            'source_commit': 'd' * 40,
            'intent': 'promote',
            'rollback_of_generation': None,
            'approved_at': '2026-08-12T10:00:00Z',
        }

    def _authoritative_daemon(self, *, last_seen=None, release=None):
        return DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1',
            fleet_id='shizuha-fleet',
            runtime='k8s',
            status=DaemonRegistry.Status.CONNECTED,
            last_seen=last_seen or timezone.now(),
            agent_runtime_image='localhost:30500/shizuha-agent-runtime:harness-observed-old',
            harness_report={'desired_runtime_release': release or self.release},
        )

    def test_empty_and_old_managed_rows_use_reviewed_projection(self):
        cache.set(
            'fleet:desired-runtime-image',
            'localhost:30500/shizuha-agent-runtime:harness-unreviewed-cache',
            None,
        )
        self._authoritative_daemon()

        empty = FleetAgent(agent_username='new-native', image='')
        managed = FleetAgent(
            agent_username='old-native',
            image='localhost:30500/shizuha-agent-runtime:harness-old',
        )

        provisioner._bind_reviewed_runtime_image(empty)
        provisioner._bind_reviewed_runtime_image(managed)
        self.assertEqual(
            provisioner._resolve_image(empty), self.reviewed_digest_image,
        )
        self.assertEqual(
            provisioner._resolve_image(managed), self.reviewed_digest_image,
        )

    def test_custom_image_remains_pinned_without_release_projection(self):
        custom = FleetAgent(
            agent_username='custom-runtime',
            image='localhost:30500/team/custom-agent:stable',
        )

        self.assertEqual(provisioner._resolve_image(custom), custom.image)

    def test_stale_or_invalid_projection_blocks_native_provisioning(self):
        self._authoritative_daemon(last_seen=timezone.now() - timedelta(seconds=31))
        native = FleetAgent(agent_username='blocked-native', image='')
        with self.assertRaises(RuntimeReleaseProjectionUnavailable):
            provisioner._bind_reviewed_runtime_image(native)

        DaemonRegistry.objects.all().delete()
        self._authoritative_daemon(release={**self.release, 'source_commit': 'invalid'})
        with self.assertRaises(RuntimeReleaseProjectionUnavailable):
            provisioner._bind_reviewed_runtime_image(native)

    def test_newer_personal_daemon_never_substitutes_for_authority(self):
        DaemonRegistry.objects.create(
            daemon_id='personal-newer',
            fleet_id='personal-fleet',
            runtime='k8s',
            status=DaemonRegistry.Status.CONNECTED,
            last_seen=timezone.now(),
            harness_report={'desired_runtime_release': self.release},
        )
        native = FleetAgent(agent_username='blocked-native', image='')

        with self.assertRaises(RuntimeReleaseProjectionUnavailable):
            provisioner._bind_reviewed_runtime_image(native)

    def _runtime_deployment(self, image):
        container = SimpleNamespace(
            name='agent',
            image=image,
            image_pull_policy='IfNotPresent',
            command=[],
            env=[],
            ports=[],
            resources=SimpleNamespace(requests={}, limits={}),
        )
        deployment = SimpleNamespace(
            metadata=provisioner.client.V1ObjectMeta(),
            spec=SimpleNamespace(
                template=SimpleNamespace(
                    metadata=provisioner.client.V1ObjectMeta(),
                    spec=SimpleNamespace(containers=[container]),
                ),
            ),
        )
        return deployment, container

    def _assert_start_converges_to_reviewed_digest(self, row_image):
        self._authoritative_daemon()
        agent = FleetAgent.objects.create(
            agent_username=f'wake-{"empty" if not row_image else "stale"}',
            agent_id=f'wake-{"empty" if not row_image else "stale"}-id',
            image=row_image,
            status=FleetAgent.Status.STOPPED,
        )
        deployment, container = self._runtime_deployment(
            'localhost:30500/shizuha-agent-runtime:harness-live-old',
        )
        apps = mock.MagicMock()
        apps.read_namespaced_deployment.return_value = deployment

        def scale_after_replace(scale_agent, replicas):
            self.assertEqual(replicas, 1)
            self.assertEqual(container.image, self.reviewed_digest_image)
            self.assertTrue(apps.replace_namespaced_deployment.called)
            return True

        with (
            mock.patch('fleet.provisioner.ensure_agent_cortex_key', return_value=''),
            mock.patch('fleet.provisioner.ensure_agent_forgejo_token', return_value=''),
            mock.patch('fleet.provisioner._ensure_coordinator_broker_token', return_value=True),
            mock.patch(
                'fleet.provisioner.get_k8s_client',
                return_value=(mock.MagicMock(), apps, mock.MagicMock()),
            ),
            mock.patch('fleet.provisioner.ensure_agent_id_password_aligned', return_value='ok'),
            mock.patch('fleet.provisioner._scale', side_effect=scale_after_replace) as scale,
            mock.patch('fleet.provisioner._log_event'),
            mock.patch('fleet.model_lease.lease_wake_available', return_value=(True, 'free_slot')),
        ):
            self.assertTrue(provisioner.start_agent(agent))

        scale.assert_called_once_with(agent, 1)
        self.assertEqual(container.image, self.reviewed_digest_image)

    def test_start_agent_converges_stale_managed_row_to_reviewed_digest(self):
        self._assert_start_converges_to_reviewed_digest(
            'localhost:30500/shizuha-agent-runtime:harness-row-old',
        )

    def test_start_agent_converges_empty_managed_row_to_reviewed_digest(self):
        self._assert_start_converges_to_reviewed_digest('')

    def test_ensure_image_stale_projection_fails_before_any_mutation(self):
        self._authoritative_daemon(
            last_seen=timezone.now() - timedelta(seconds=31),
        )
        agent = FleetAgent.objects.create(
            agent_username='stale-direct-converge',
            agent_id='stale-direct-converge-id',
            image='localhost:30500/shizuha-agent-runtime:harness-old',
        )
        with (
            mock.patch('fleet.provisioner.ensure_agent_cortex_key') as cortex_key,
            mock.patch('fleet.provisioner.ensure_agent_forgejo_token') as forgejo_token,
            mock.patch('fleet.provisioner.get_k8s_client') as k8s,
            self.assertRaises(RuntimeReleaseProjectionUnavailable),
        ):
            provisioner.ensure_agent_image(agent)
        cortex_key.assert_not_called()
        forgejo_token.assert_not_called()
        k8s.assert_not_called()

    @mock.patch.dict("os.environ", {'HIVE_RUNTIME_BASE_IMAGE': 'registry.example.test/base@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'HIVE_RUNTIME_BROKER_IMAGE': 'registry.example.test/broker@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'HIVE_RUNTIME_AGENT_NODE_SELECTOR': 'kubernetes.io/hostname=agent-node', 'HIVE_RUNTIME_DAEMON_NODE_SELECTOR': 'kubernetes.io/hostname=daemon-node', 'HIVE_RUNTIME_HOST_CLI': '/srv/runtime-test-checkout'})
    def test_native_and_per_user_renderers_use_reviewed_digest_reference(self):
        self._authoritative_daemon()
        agent = FleetAgent(
            agent_username='render-reviewed',
            agent_id='render-reviewed-id',
            image='',
        )
        provisioner._bind_reviewed_runtime_image(agent)

        apps = mock.MagicMock()
        core = mock.MagicMock()
        core.read_namespaced_secret.side_effect = ApiException(status=404)
        with mock.patch.object(provisioner, 'get_k8s_client', return_value=(core, apps, mock.MagicMock())):
            provisioner._create_deployment(apps, 'test-ns', agent)
        native_deployment = apps.create_namespaced_deployment.call_args.args[1]
        native_agent = next(
            item for item in native_deployment.spec.template.spec.containers
            if item.name == 'agent'
        )
        self.assertEqual(native_agent.image, self.reviewed_digest_image)

        daemon_apps = mock.MagicMock()
        ProvisionUserDaemonCommand()._apply_deployment(
            daemon_apps,
            'personal-daemon',
            'personal-daemon-id',
            'personal-agents',
            'personal-daemon-secret',
            42,
            'owner@example.com',
            self.reviewed_digest_image,
        )
        daemon_deployment = daemon_apps.create_namespaced_deployment.call_args.args[1]
        env = {
            item.name: item.value
            for item in daemon_deployment.spec.template.spec.containers[0].env
        }
        self.assertEqual(
            env['SHIZUHA_AGENT_RUNTIME_IMAGE'], self.reviewed_digest_image,
        )

    def test_same_digest_with_different_display_tag_has_same_actuation_ref(self):
        first = self._authoritative_daemon()
        first_ref = provisioner._live_fleet_image()
        first.delete()
        retagged = {
            **self.release,
            'display_tag': 'localhost:30500/shizuha-agent-runtime:another-label',
        }
        self._authoritative_daemon(release=retagged)
        self.assertEqual(provisioner._live_fleet_image(), first_ref)


class ManagedRuntimeSwitchBoundaryTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='runtime-switch-admin',
            email='runtime-switch-admin@shizuha.com',
            password='pw',
            is_staff=True,
        )
        self.agent = FleetAgent.objects.create(
            agent_username='runtime-switch-agent',
            agent_id='runtime-switch-agent-id',
            image='localhost:30500/team/custom-agent:old',
            status=FleetAgent.Status.STOPPED,
        )

    def test_api_serializer_rejects_managed_tag_and_digest_but_allows_custom(self):
        managed = (
            'localhost:30500/shizuha-agent-runtime:harness-unreviewed',
            'localhost:30500/shizuha-agent-runtime@sha256:' + ('a' * 64),
        )
        for image in managed:
            with self.subTest(image=image):
                serializer = SwitchBackendSerializer(data={'image': image})
                self.assertFalse(serializer.is_valid())
                self.assertIn('DesiredRuntimeRelease', str(serializer.errors))

        serializer = SwitchBackendSerializer(data={
            'image': 'localhost:30500/team/custom-agent:stable',
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_agent_create_rejects_managed_ref_but_allows_custom_repo(self):
        managed = FleetAgentCreateSerializer(data={
            'agent_username': 'create-managed-ref',
            'image': 'localhost:30500/shizuha-agent-runtime:harness-unreviewed',
        })
        self.assertFalse(managed.is_valid())
        self.assertIn('DesiredRuntimeRelease', str(managed.errors))

        custom = FleetAgentCreateSerializer(data={
            'agent_username': 'create-custom-ref',
            'image': 'localhost:30500/team/custom-agent:stable',
        })
        self.assertTrue(custom.is_valid(), custom.errors)

    def test_api_rejects_managed_refs_without_persisting_or_patching(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        url = f'/api/v1/fleet/agents/{self.agent.id}/switch-backend'
        for image in (
            'localhost:30500/shizuha-agent-runtime:harness-unreviewed',
            'localhost:30500/shizuha-agent-runtime@sha256:' + ('a' * 64),
        ):
            with self.subTest(image=image):
                response = client.post(url, {'image': image}, format='json')
                self.assertEqual(response.status_code, 400)
                self.assertIn('DesiredRuntimeRelease', str(response.json()))
                self.agent.refresh_from_db()
                self.assertEqual(
                    self.agent.image,
                    'localhost:30500/team/custom-agent:old',
                )

    @mock.patch('fleet.ui_views._auth_user')
    def test_ui_rejects_managed_refs_and_allows_custom_repo(self, auth_user):
        auth_user.return_value = (self.user, None)
        factory = RequestFactory()
        url = f'/hive/fleet/agents/{self.agent.id}/switch-backend/'
        for image in (
            'localhost:30500/shizuha-agent-runtime:harness-unreviewed',
            'localhost:30500/shizuha-agent-runtime@sha256:' + ('a' * 64),
        ):
            with self.subTest(image=image):
                response = FleetAgentSwitchBackendUIView.as_view()(
                    factory.post(url, {'image': image}),
                    agent_id=self.agent.id,
                )
                self.assertEqual(response.status_code, 409)
                self.agent.refresh_from_db()
                self.assertEqual(
                    self.agent.image,
                    'localhost:30500/team/custom-agent:old',
                )

        custom = 'localhost:30500/team/custom-agent:new'
        response = FleetAgentSwitchBackendUIView.as_view()(
            factory.post(url, {'image': custom}),
            agent_id=self.agent.id,
        )
        self.assertEqual(response.status_code, 302)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.image, custom)
