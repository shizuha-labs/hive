"""Regression coverage for native + rt-fleet automatic lifecycle control."""
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from .lifecycle_fleet import (
    hibernate_lifecycle_agent,
    list_lifecycle_agents,
    notify_pulse_agent_lifecycle,
    set_lifecycle_agent_enabled,
    stamp_native_runtime_ready,
)
from .models import FleetAgent


class StampRuntimeReadyTests(TestCase):
    def test_stamps_runtime_plane_unready_pod(self):
        """rt-fleet mio 2/3 (broker 401) must not count as a living DeepSeek holder."""
        FleetAgent.objects.create(
            agent_username='mio',
            status=FleetAgent.Status.RUNNING,
        )
        agents = [{
            'agent_username': 'mio',
            'username': 'mio',
            'status': 'running',
            'enabled': True,
            'lifecycle_plane': 'runtime',
        }]
        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.deployment_runtime_ready',
            return_value=False,
        ), mock.patch(
            'fleet.lifecycle_fleet.provisioner.heal_workspace_disk',
        ):
            stamp_native_runtime_ready(agents)
        self.assertIs(agents[0]['runtime_ready'], False)

    def test_hibernated_zero_replica_is_not_cannot_turn(self):
        FleetAgent.objects.create(
            agent_username='nami',
            status=FleetAgent.Status.HIBERNATED,
        )
        agents = [{
            'agent_username': 'nami',
            'username': 'nami',
            'status': 'hibernated',
            'enabled': False,
            'lifecycle_plane': 'native',
        }]
        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.deployment_runtime_ready',
            return_value=None,
        ):
            stamp_native_runtime_ready(agents)
        self.assertNotIn('runtime_ready', agents[0])

    def test_ghost_running_zero_replica_is_unready(self):
        FleetAgent.objects.create(
            agent_username='sato',
            status=FleetAgent.Status.RUNNING,
        )
        agents = [{
            'agent_username': 'sato',
            'username': 'sato',
            'status': 'running',
            'enabled': True,
            'lifecycle_plane': 'native',
        }]
        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.deployment_runtime_ready',
            return_value=None,
        ), mock.patch(
            'fleet.lifecycle_fleet.provisioner.heal_workspace_disk',
        ):
            stamp_native_runtime_ready(agents)
        self.assertIs(agents[0]['runtime_ready'], False)


class LifecycleInventoryTests(TestCase):
    def test_native_agent_is_included_in_policy_inventory(self):
        native = FleetAgent.objects.create(
            agent_username='sato',
            status=FleetAgent.Status.RUNNING,
            last_active_at=timezone.now(),
        )

        with mock.patch('fleet.lifecycle_fleet.list_runtime_agents', return_value=[]):
            agents = list_lifecycle_agents(include_disabled=True)

        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]['id'], str(native.id))
        self.assertEqual(agents[0]['email'], 'sato@shizuha.com')
        self.assertTrue(agents[0]['enabled'])
        self.assertEqual(agents[0]['lifecycle_plane'], 'native')

    def test_personal_native_agent_email_uses_agents_domain(self):
        native = FleetAgent.objects.create(
            agent_username='shizuha-741',
            owner_id=741,
            status=FleetAgent.Status.HIBERNATED,
        )

        with mock.patch('fleet.lifecycle_fleet.list_runtime_agents', return_value=[]):
            agents = list_lifecycle_agents(include_disabled=True)

        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]['email'], 'shizuha-741@agents.shizuha.io')

    def test_runtime_agent_wins_over_same_username_native_row(self):
        FleetAgent.objects.create(
            agent_username='sato',
            status=FleetAgent.Status.RUNNING,
        )
        runtime = {
            'id': 'runtime-sato',
            'agent_username': 'sato',
            'email': 'sato@shizuha.com',
            'enabled': True,
            'status': 'running',
        }

        with mock.patch('fleet.lifecycle_fleet.list_runtime_agents', return_value=[runtime]):
            agents = list_lifecycle_agents(include_disabled=True)

        self.assertEqual([agent['id'] for agent in agents], ['runtime-sato'])
        self.assertEqual(agents[0]['lifecycle_plane'], 'runtime')


class LifecycleActuationTests(TestCase):
    def test_native_automatic_scale_down_uses_hibernation(self):
        native = FleetAgent.objects.create(
            agent_username='nagi',
            status=FleetAgent.Status.RUNNING,
        )

        def mark_hibernated(agent, *, reason):
            agent.status = FleetAgent.Status.HIBERNATED
            agent.hibernation_reason = reason
            agent.save(update_fields=['status', 'hibernation_reason'])
            return True

        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.hibernate_agent',
            side_effect=mark_hibernated,
        ) as hibernate, mock.patch(
            'fleet.lifecycle_fleet.set_runtime_agent_enabled',
        ) as runtime_toggle, mock.patch(
            'fleet.lifecycle_fleet.agent_index_cache.invalidate',
        ) as invalidate, mock.patch(
            'fleet.lifecycle_fleet._release_runtime_lease',
        ) as release:
            ok, detail = hibernate_lifecycle_agent(
                str(native.id),
                reason='yielding DeepSeek lease',
            )

        self.assertTrue(ok)
        self.assertEqual(detail['status'], FleetAgent.Status.HIBERNATED)
        hibernate.assert_called_once_with(native, reason='yielding DeepSeek lease')
        runtime_toggle.assert_not_called()
        invalidate.assert_called_once()
        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs['username'], 'nagi')
        self.assertIn('native hibernate nagi', release.call_args.kwargs['reason'])

    def test_hibernate_notifies_pulse_so_ready_work_can_drain(self):
        native = FleetAgent.objects.create(
            agent_username='shion',
            status=FleetAgent.Status.RUNNING,
        )

        def mark_hibernated(agent, *, reason):
            agent.status = FleetAgent.Status.HIBERNATED
            agent.hibernation_reason = reason
            agent.save(update_fields=['status', 'hibernation_reason'])
            return True

        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.hibernate_agent',
            side_effect=mark_hibernated,
        ), mock.patch(
            'fleet.lifecycle_fleet.agent_index_cache.invalidate',
        ), mock.patch(
            'fleet.lifecycle_fleet.notify_pulse_agent_lifecycle',
        ) as notify:
            ok, detail = hibernate_lifecycle_agent(
                str(native.id),
                reason='does not hold the model lease',
            )

        self.assertTrue(ok)
        notify.assert_called_once()
        kwargs = notify.call_args.kwargs
        self.assertEqual(kwargs['username'], 'shion')
        self.assertEqual(kwargs['status'], FleetAgent.Status.HIBERNATED)
        self.assertEqual(kwargs['reason'], 'does not hold the model lease')

    @override_settings(HIVE_PULSE_API_URL='http://pulse.test/api')
    def test_pulse_lifecycle_notify_posts_when_token_present(self):
        with mock.patch.dict('os.environ', {
            'HIVE_PULSE_API_TOKEN': 'svc-token',
        }, clear=False), mock.patch('requests.post') as post:
            notify_pulse_agent_lifecycle(
                username='shion',
                email='shion@shizuha.com',
                status='hibernated',
                reason='no lease',
            )
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'http://pulse.test/api/internal/agent-lifecycle/')
        self.assertEqual(kwargs['json']['email'], 'shion@shizuha.com')
        self.assertEqual(kwargs['json']['status'], 'hibernated')

    def test_enable_ghost_running_starts_zero_replica_deploy(self):
        native = FleetAgent.objects.create(
            agent_username='nami',
            status=FleetAgent.Status.RUNNING,
        )

        def mark_started(agent):
            agent.status = FleetAgent.Status.RUNNING
            agent.save(update_fields=['status'])
            return True

        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.current_replicas',
            return_value=0,
        ), mock.patch(
            'fleet.lifecycle_fleet.provisioner.start_agent',
            side_effect=mark_started,
        ) as start, mock.patch(
            'fleet.lifecycle_fleet.agent_index_cache.invalidate',
        ):
            ok, detail = set_lifecycle_agent_enabled(str(native.id), True)

        self.assertTrue(ok)
        self.assertFalse(detail.get('unchanged'))
        start.assert_called_once()

    def test_enable_running_with_live_replicas_is_unchanged(self):
        native = FleetAgent.objects.create(
            agent_username='nagi',
            status=FleetAgent.Status.RUNNING,
        )

        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.current_replicas',
            return_value=1,
        ) as replicas, mock.patch(
            'fleet.lifecycle_fleet.provisioner.start_agent',
        ) as start, mock.patch(
            'fleet.lifecycle_fleet.provisioner.ensure_agent_image',
            return_value=True,
        ) as ensure:
            ok, detail = set_lifecycle_agent_enabled(str(native.id), True)

        self.assertTrue(ok)
        self.assertTrue(detail['unchanged'])
        replicas.assert_called_once()
        start.assert_not_called()
        ensure.assert_called_once_with(native)

    def test_converge_running_heals_stale_heartbeat_env(self):
        from fleet.lifecycle_fleet import converge_running_lifecycle_agent

        native = FleetAgent.objects.create(
            agent_username='nami-hb',
            status=FleetAgent.Status.RUNNING,
            env={'SHIZUHA_IDLE_HEARTBEAT_MS': '900000'},
        )
        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.ensure_agent_image',
            return_value=True,
        ) as ensure:
            ok, detail = converge_running_lifecycle_agent(native)

        self.assertTrue(ok)
        self.assertTrue(detail['converged'])
        ensure.assert_called_once_with(native)
        native.refresh_from_db()
        self.assertEqual(native.env['SHIZUHA_IDLE_HEARTBEAT_MS'], '90000')
        self.assertEqual(native.env['SHIZUHA_FIRST_HEARTBEAT_MS'], '8000')

    def test_converge_running_heals_daemon_linked_k3s_holder(self):
        from fleet.lifecycle_fleet import converge_running_lifecycle_agent
        from fleet.models import DaemonRegistry

        daemon = DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1', fleet_id='fleet-s1',
            status=DaemonRegistry.Status.CONNECTED,
        )
        holder = FleetAgent.objects.create(
            agent_username='hiro-hb',
            agent_id='3c3361ec-77dd-520e-8245-0cf00b27adac',
            managing_daemon=daemon,
            status=FleetAgent.Status.RUNNING,
            env={'SHIZUHA_IDLE_HEARTBEAT_MS': '900000'},
        )
        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.ensure_agent_image',
            return_value=True,
        ) as ensure:
            ok, detail = converge_running_lifecycle_agent(holder.agent_id)

        self.assertTrue(ok)
        self.assertTrue(detail['converged'])
        ensure.assert_called_once()
        holder.refresh_from_db()
        self.assertEqual(holder.env['SHIZUHA_IDLE_HEARTBEAT_MS'], '90000')
        self.assertEqual(holder.env['SHIZUHA_FIRST_HEARTBEAT_MS'], '8000')

    def test_apply_k3s_lane_after_patch_skips_when_no_deployment(self):
        from fleet.lifecycle_fleet import apply_k3s_lane_after_patch

        native = FleetAgent.objects.create(
            agent_username='nagi-patch',
            status=FleetAgent.Status.RUNNING,
        )
        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.current_replicas',
            return_value=None,
        ) as replicas, mock.patch(
            'fleet.lifecycle_fleet.provisioner.ensure_agent_image',
        ) as ensure:
            result = apply_k3s_lane_after_patch(native)
        self.assertEqual(result['reason'], 'no_k3s_deployment')
        self.assertFalse(result['applied'])
        replicas.assert_called_once_with(native)
        ensure.assert_not_called()

    def test_apply_k3s_lane_after_patch_converges_live_deployment(self):
        from fleet.lifecycle_fleet import apply_k3s_lane_after_patch

        native = FleetAgent.objects.create(
            agent_username='kai-patch',
            status=FleetAgent.Status.RUNNING,
            model='cortex/grok-4.6',
        )
        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.current_replicas',
            return_value=1,
        ), mock.patch(
            'fleet.lifecycle_fleet.provisioner.ensure_agent_image',
            return_value=True,
        ) as ensure, mock.patch(
            'fleet.lifecycle_fleet.provisioner.heal_workspace_disk',
        ):
            result = apply_k3s_lane_after_patch(native)
        self.assertTrue(result['applied'])
        self.assertEqual(result['replicas'], 1)
        ensure.assert_called_once_with(native)

    def test_native_agent_uses_k3s_provisioner(self):
        native = FleetAgent.objects.create(
            agent_username='sato',
            status=FleetAgent.Status.RUNNING,
        )

        def mark_stopped(agent):
            agent.status = FleetAgent.Status.STOPPED
            agent.save(update_fields=['status'])
            return True

        with mock.patch('fleet.lifecycle_fleet.provisioner.stop_agent', side_effect=mark_stopped) as stop, \
             mock.patch('fleet.lifecycle_fleet.set_runtime_agent_enabled') as runtime_toggle, \
             mock.patch('fleet.lifecycle_fleet.agent_index_cache.invalidate') as invalidate:
            ok, detail = set_lifecycle_agent_enabled(str(native.id), False)

        self.assertTrue(ok)
        self.assertEqual(detail['plane'], 'native')
        stop.assert_called_once()
        runtime_toggle.assert_not_called()
        invalidate.assert_called_once()

    def test_idempotent_native_stop_clears_hibernation_metadata(self):
        native = FleetAgent.objects.create(
            agent_username='nami',
            status=FleetAgent.Status.STOPPED,
            hibernation_reason='stale automatic state',
            hibernated_at=timezone.now(),
        )

        with mock.patch(
            'fleet.model_lease.release_leases_for_agent',
            return_value={'released': [], 'errors': []},
        ):
            ok, detail = set_lifecycle_agent_enabled(str(native.id), False)

        self.assertTrue(ok)
        self.assertTrue(detail['unchanged'])
        native.refresh_from_db()
        self.assertEqual(native.status, FleetAgent.Status.STOPPED)
        self.assertEqual(native.hibernation_reason, '')
        self.assertIsNone(native.hibernated_at)

    def test_rt_fleet_agent_uses_daemon_control(self):
        with mock.patch(
            'fleet.lifecycle_fleet.set_runtime_agent_enabled',
            return_value=(True, {'status': 'disabled'}),
        ) as runtime_toggle:
            result = set_lifecycle_agent_enabled('runtime-sato', False)

        self.assertEqual(result, (True, {'status': 'disabled'}))
        runtime_toggle.assert_called_once_with('runtime-sato', False)

    def test_rt_fleet_missing_agent_falls_back_to_native_start(self):
        """2026-09-01: GLM lease holders START 500 Agent not found on rt-fleet."""
        from fleet.models import DaemonRegistry

        daemon = DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1-missing', fleet_id='fleet-s1',
            status=DaemonRegistry.Status.CONNECTED,
        )
        row = FleetAgent.objects.create(
            agent_username='mio-missing',
            agent_id='runtime-mio-missing',
            managing_daemon=daemon,
            status=FleetAgent.Status.HIBERNATED,
            hibernation_reason='does not hold the model lease',
        )

        def mark_started(agent):
            agent.status = FleetAgent.Status.RUNNING
            agent.hibernation_reason = ''
            agent.save(update_fields=['status', 'hibernation_reason'])
            return True

        with mock.patch(
            'fleet.lifecycle_fleet.set_runtime_agent_enabled',
            return_value=(False, 'control enable returned 500: {"error":"Agent not found"}'),
        ), mock.patch(
            'fleet.lifecycle_fleet.provisioner.start_agent',
            side_effect=mark_started,
        ) as start, mock.patch(
            'fleet.lifecycle_fleet.agent_index_cache.invalidate',
        ):
            ok, detail = set_lifecycle_agent_enabled(str(row.id), True)

        self.assertTrue(ok)
        self.assertEqual(detail['plane'], 'native-fallback')
        start.assert_called_once()
        row.refresh_from_db()
        self.assertEqual(row.status, FleetAgent.Status.RUNNING)

    def test_rt_fleet_missing_agent_hibernate_is_local_success(self):
        """2026-09-01: SYNC_HIBERNATION jun 500 Agent not found failed CronJob."""
        from fleet.models import DaemonRegistry

        daemon = DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1-missing-hib', fleet_id='fleet-s1',
            status=DaemonRegistry.Status.CONNECTED,
        )
        row = FleetAgent.objects.create(
            agent_username='jun-missing',
            agent_id='runtime-jun-missing',
            managing_daemon=daemon,
            status=FleetAgent.Status.RUNNING,
        )
        with mock.patch(
            'fleet.lifecycle_fleet._k8s_lifecycle_agent',
            return_value=None,
        ), mock.patch(
            'fleet.lifecycle_fleet.set_runtime_agent_enabled',
            return_value=(False, 'control disable returned 500: {"error":"Agent not found"}'),
        ), mock.patch(
            'fleet.lifecycle_fleet._release_runtime_lease',
        ) as release, mock.patch(
            'fleet.lifecycle_fleet.agent_index_cache.invalidate',
        ):
            ok, detail = hibernate_lifecycle_agent(
                str(row.id),
                reason='reassert Hive hibernation intent to runtime exporter',
            )

        self.assertTrue(ok)
        self.assertEqual(detail['plane'], 'runtime-missing')
        release.assert_called_once()
        row.refresh_from_db()
        self.assertEqual(row.status, FleetAgent.Status.HIBERNATED)
        self.assertEqual(row.runtime_lifecycle_state, 'hibernated')

    def test_rt_fleet_automatic_hibernation_preserves_wakeable_intent(self):
        row = FleetAgent.objects.create(
            agent_username='sora',
            agent_id='runtime-sora',
            status=FleetAgent.Status.RUNNING,
        )
        with mock.patch(
            'fleet.lifecycle_fleet._find_native_lifecycle_agent',
            return_value=None,
        ), mock.patch(
            'fleet.lifecycle_fleet._find_k3s_deploy_agent',
            return_value=None,
        ), mock.patch(
            'fleet.lifecycle_fleet._find_runtime_lifecycle_agent',
            return_value=row,
        ), mock.patch(
            'fleet.lifecycle_fleet.set_runtime_agent_enabled',
            return_value=(True, {
                'status': 'disabled',
                'lifecycle_state': 'hibernated',
            }),
        ) as runtime_toggle, mock.patch(
            'fleet.lifecycle_fleet._release_runtime_lease',
        ):
            ok, detail = hibernate_lifecycle_agent(
                'runtime-sora',
                reason='no actionable work',
            )

        self.assertTrue(ok)
        self.assertEqual(detail['status'], FleetAgent.Status.HIBERNATED)
        runtime_toggle.assert_called_once_with(
            'runtime-sora',
            False,
            lifecycle_state='hibernated',
        )
        row.refresh_from_db()
        self.assertEqual(row.status, FleetAgent.Status.HIBERNATED)
        self.assertEqual(row.hibernation_reason, 'no actionable work')
        self.assertEqual(row.runtime_lifecycle_state, 'hibernated')

    def test_idempotent_hibernate_scales_ghost_living_replicas(self):
        """Hibernated status with replicas=1 is still living — scale to zero.

        ryo 2026-08-28: FleetAgent.status=hibernated while agent-ryo stayed
        Ready and hit Cortex. The idempotent skip trusted the status flag.
        """
        native = FleetAgent.objects.create(
            agent_username='ryo',
            status=FleetAgent.Status.HIBERNATED,
            hibernation_reason='does not hold the model lease',
        )

        def mark_hibernated(agent, *, reason):
            agent.status = FleetAgent.Status.HIBERNATED
            agent.hibernation_reason = reason
            agent.save(update_fields=['status', 'hibernation_reason'])
            return True

        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.current_replicas',
            return_value=1,
        ), mock.patch(
            'fleet.lifecycle_fleet.provisioner.hibernate_agent',
            side_effect=mark_hibernated,
        ) as hibernate, mock.patch(
            'fleet.lifecycle_fleet.set_runtime_agent_enabled',
        ) as runtime_toggle, mock.patch(
            'fleet.lifecycle_fleet.agent_index_cache.invalidate',
        ), mock.patch(
            'fleet.lifecycle_fleet._release_runtime_lease',
        ):
            ok, detail = hibernate_lifecycle_agent(
                str(native.id),
                reason='does not hold the model lease',
            )

        self.assertTrue(ok)
        self.assertFalse(detail.get('unchanged'))
        hibernate.assert_called_once_with(
            native, reason='does not hold the model lease',
        )
        runtime_toggle.assert_not_called()

    def test_idempotent_hibernate_skips_when_already_at_zero(self):
        native = FleetAgent.objects.create(
            agent_username='ryo-zero',
            status=FleetAgent.Status.HIBERNATED,
        )
        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.current_replicas',
            return_value=0,
        ), mock.patch(
            'fleet.lifecycle_fleet.provisioner.hibernate_agent',
        ) as hibernate, mock.patch(
            'fleet.lifecycle_fleet._release_runtime_lease',
        ):
            ok, detail = hibernate_lifecycle_agent(
                str(native.id),
                reason='does not hold the model lease',
            )
        self.assertTrue(ok)
        self.assertTrue(detail['unchanged'])
        hibernate.assert_not_called()

    def test_hibernate_daemon_linked_k3s_deploy_scales_native(self):
        """Daemon-linked DeepSeek seats still live as k3s Deployments.

        STOP via rt-fleet-control left ryo/rei Ready when the control proxy
        refused connections (2026-08-28). Hibernate must scale the Deployment.
        """
        from fleet.models import DaemonRegistry

        daemon = DaemonRegistry.objects.create(
            daemon_id='rt-fleet-s1-ryo', fleet_id='fleet-s1',
            status=DaemonRegistry.Status.CONNECTED,
        )
        row = FleetAgent.objects.create(
            agent_username='ryo-linked',
            agent_id='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0001',
            managing_daemon=daemon,
            status=FleetAgent.Status.RUNNING,
        )

        def mark_hibernated(agent, *, reason):
            agent.status = FleetAgent.Status.HIBERNATED
            agent.hibernation_reason = reason
            agent.save(update_fields=['status', 'hibernation_reason'])
            return True

        with mock.patch(
            'fleet.lifecycle_fleet.provisioner.current_replicas',
            return_value=1,
        ), mock.patch(
            'fleet.lifecycle_fleet.provisioner.hibernate_agent',
            side_effect=mark_hibernated,
        ) as hibernate, mock.patch(
            'fleet.lifecycle_fleet.set_runtime_agent_enabled',
            return_value=(True, {'status': 'disabled'}),
        ) as runtime_toggle, mock.patch(
            'fleet.lifecycle_fleet.agent_index_cache.invalidate',
        ), mock.patch(
            'fleet.lifecycle_fleet._release_runtime_lease',
        ):
            ok, detail = hibernate_lifecycle_agent(
                str(row.id),
                reason='does not hold the model lease',
            )

        self.assertTrue(ok)
        self.assertEqual(detail['plane'], 'native')
        hibernate.assert_called_once_with(
            row, reason='does not hold the model lease',
        )
        runtime_toggle.assert_called_once()
        _args, kwargs = runtime_toggle.call_args
        self.assertEqual(kwargs.get('lifecycle_state'), 'hibernated')


class NativeHibernateClearsOperatorStopTests(TestCase):
    """mika 2026-08-31: native hibernate left runtime_lifecycle_state=
    operator_stopped, so QA could not auto-wake onto a GLM lease."""

    def test_hibernate_agent_sets_lifecycle_hibernated(self):
        from fleet import provisioner

        agent = FleetAgent.objects.create(
            agent_username='mika-lease',
            status=FleetAgent.Status.STOPPED,
            runtime_lifecycle_state='operator_stopped',
        )
        with mock.patch('fleet.provisioner._scale', return_value=True), mock.patch(
            'fleet.provisioner.ensure_agent_image', return_value=True,
        ), mock.patch(
            'fleet.model_lease.release_leases_for_agent',
            return_value={'released': [], 'errors': []},
        ), mock.patch(
            'fleet.activity_snapshot.capture_native_activity_snapshot',
        ) as capture:
            ok = provisioner.hibernate_agent(
                agent, reason='clear operator_stop so QA can auto-wake',
            )
        capture.assert_called_once_with(agent)
        self.assertTrue(ok)
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)
        self.assertEqual(agent.runtime_lifecycle_state, 'hibernated')

    def test_stop_agent_sets_operator_stopped(self):
        from fleet import provisioner

        agent = FleetAgent.objects.create(
            agent_username='shion-stop',
            status=FleetAgent.Status.RUNNING,
            runtime_lifecycle_state='enabled',
        )
        with mock.patch('fleet.provisioner._scale', return_value=True), mock.patch(
            'fleet.provisioner.ensure_agent_image', return_value=True,
        ), mock.patch(
            'fleet.model_lease.release_leases_for_agent',
            return_value={'released': [], 'errors': []},
        ), mock.patch(
            'fleet.activity_snapshot.capture_native_activity_snapshot',
        ) as capture:
            ok = provisioner.stop_agent(agent)
        capture.assert_called_once_with(agent)
        self.assertTrue(ok)
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.STOPPED)
        self.assertEqual(agent.runtime_lifecycle_state, 'operator_stopped')


class HiveDisableIsHibernateTests(TestCase):
    """Operator 2026-09-01: Hive disable hibernates; only superuser /stop/."""

    def _request(self, *, is_superuser=False):
        from types import SimpleNamespace

        from rest_framework.test import APIRequestFactory, force_authenticate

        user = SimpleNamespace(
            id=1,
            email='staff@shizuha.com',
            username='staff',
            is_authenticated=True,
            is_staff=True,
            is_superuser=is_superuser,
        )
        request = APIRequestFactory().post('/v1/fleet/agents/x/disable/')
        force_authenticate(request, user=user)
        return request

    def test_disable_hibernates_native_agent(self):
        from fleet.views import FleetAgentDisableView

        agent = FleetAgent.objects.create(
            agent_username='cora-disable',
            status=FleetAgent.Status.RUNNING,
            runtime_lifecycle_state='enabled',
        )

        def mark_hibernated(agent_id, *, reason):
            row = FleetAgent.objects.get(id=agent_id)
            row.status = FleetAgent.Status.HIBERNATED
            row.hibernation_reason = reason
            row.runtime_lifecycle_state = 'hibernated'
            row.save(update_fields=[
                'status', 'hibernation_reason', 'runtime_lifecycle_state',
            ])
            return True, {'plane': 'native', 'status': 'hibernated'}

        with mock.patch(
            'fleet.views.hibernate_lifecycle_agent',
            side_effect=mark_hibernated,
        ) as hibernate, mock.patch(
            'fleet.views.provisioner.stop_agent',
        ) as stop:
            response = FleetAgentDisableView.as_view()(
                self._request(), agent_id=str(agent.id),
            )

        self.assertEqual(response.status_code, 200)
        hibernate.assert_called_once()
        stop.assert_not_called()
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)

    def test_disable_converts_operator_stopped_to_hibernated(self):
        from fleet.views import FleetAgentDisableView

        agent = FleetAgent.objects.create(
            agent_username='misaki-recover',
            status=FleetAgent.Status.STOPPED,
            runtime_lifecycle_state='operator_stopped',
        )

        def mark_hibernated(agent_id, *, reason):
            row = FleetAgent.objects.get(id=agent_id)
            row.status = FleetAgent.Status.HIBERNATED
            row.hibernation_reason = reason
            row.runtime_lifecycle_state = 'hibernated'
            row.save(update_fields=[
                'status', 'hibernation_reason', 'runtime_lifecycle_state',
            ])
            return True, {'plane': 'native', 'status': 'hibernated'}

        with mock.patch(
            'fleet.views.hibernate_lifecycle_agent',
            side_effect=mark_hibernated,
        ) as hibernate, mock.patch(
            'fleet.views.provisioner.stop_agent',
        ) as stop:
            response = FleetAgentDisableView.as_view()(
                self._request(), agent_id=str(agent.id),
            )

        self.assertEqual(response.status_code, 200, getattr(response, 'data', None))
        hibernate.assert_called_once()
        stop.assert_not_called()
        agent.refresh_from_db()
        self.assertEqual(agent.status, FleetAgent.Status.HIBERNATED)
        self.assertEqual(agent.runtime_lifecycle_state, 'hibernated')

    def test_stop_forbidden_for_non_superuser(self):
        from fleet.views import FleetAgentStopView

        agent = FleetAgent.objects.create(
            agent_username='nori-stop',
            status=FleetAgent.Status.RUNNING,
        )
        with mock.patch(
            'fleet.views.set_lifecycle_agent_enabled',
        ) as hard_stop:
            response = FleetAgentStopView.as_view()(
                self._request(is_superuser=False), agent_id=str(agent.id),
            )
        self.assertEqual(response.status_code, 403)
        hard_stop.assert_not_called()

    def test_stop_allowed_for_superuser(self):
        from fleet.views import FleetAgentStopView

        agent = FleetAgent.objects.create(
            agent_username='nori-op-stop',
            status=FleetAgent.Status.RUNNING,
        )
        with mock.patch(
            'fleet.views.set_lifecycle_agent_enabled',
            return_value=(True, {'status': 'stopped'}),
        ) as hard_stop:
            response = FleetAgentStopView.as_view()(
                self._request(is_superuser=True), agent_id=str(agent.id),
            )
        self.assertEqual(response.status_code, 200)
        hard_stop.assert_called_once_with(str(agent.id), False)
