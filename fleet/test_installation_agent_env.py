"""Installation inheritance at actual native and daemon output boundaries."""
import os
from unittest.mock import patch, MagicMock, AsyncMock
from django.test import SimpleTestCase, TestCase
from .installation_config import effective_agent_env, effective_runtime_config, PULSE_HUMAN_EMAILS as KEY
from .models import FleetAgent, DaemonRegistry
from . import runtime_fleet

DEFAULT = '{"7":"operator@example.test"}'


class EffectiveAgentEnvTests(SimpleTestCase):
    def test_precedence_copy_reset_and_single_key_scope(self):
        original = {'OTHER': 'explicit'}
        with patch.dict(os.environ, {KEY:DEFAULT, 'UNRELATED_INSTALLATION':'not-inherited'}):
            self.assertEqual(effective_agent_env(original), {**original, KEY:DEFAULT})
            self.assertEqual(original, {'OTHER':'explicit'})
            for value in ('', '{}', '{"8":"other@example.test"}'):
                self.assertEqual(effective_agent_env({KEY:value}), {KEY:value})
            os.environ[KEY] = '{}'
            self.assertEqual(effective_agent_env({}), {KEY:'{}'})
            del os.environ[KEY]
            self.assertEqual(effective_agent_env({}), {})
        config={'env':original, 'model':'fixed'}
        with patch.dict(os.environ, {KEY:DEFAULT}):
            self.assertEqual(effective_runtime_config(config)['env'], {**original,KEY:DEFAULT})
            self.assertEqual(config['env'], original)
            self.assertEqual(effective_runtime_config({'model':'fixed'}), {'model':'fixed'})

    def test_legacy_confirmation_requires_current_effective_value(self):
        from .consumers import _pending_key_confirmed
        pending={'OTHER':'explicit'}
        with patch.dict(os.environ,{KEY:DEFAULT}):
            self.assertTrue(_pending_key_confirmed('env',pending,{'env':{**pending,KEY:DEFAULT}},{}))
            self.assertFalse(_pending_key_confirmed('env',pending,{'env':pending},{}))
            os.environ[KEY]='{}'
            self.assertFalse(_pending_key_confirmed('env',pending,{'env':{**pending,KEY:DEFAULT}},{}))
            self.assertTrue(_pending_key_confirmed('env',{KEY:''},{'env':{KEY:''}},{}))
        self.assertEqual(pending,{'OTHER':'explicit'})

    def test_lane_transport_copy_binds_effective_fingerprint_without_changing_outbox(self):
        from .installation_config import effective_config_frame
        from .config_fingerprints import config_fingerprints
        raw={'type':'config_update','desired_generation':7,'config':{'env':{'OTHER':'explicit'},'model':'fixed'}}
        with patch.dict(os.environ,{KEY:DEFAULT}):
            outgoing=effective_config_frame(raw)
            self.assertEqual(outgoing['config']['env'],{'OTHER':'explicit',KEY:DEFAULT})
            self.assertEqual(outgoing['config_fingerprints'],config_fingerprints(outgoing['config']))
            self.assertEqual(outgoing['desired_generation'],7)
            self.assertNotIn(KEY,raw['config']['env'])
            self.assertEqual(effective_config_frame({'config':{'model':'fixed'}}),{'config':{'model':'fixed'}})


class InstallationAgentProducerTests(TestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,{KEY:DEFAULT})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.daemon=DaemonRegistry.objects.create(daemon_id='rt-fleet-s1',fleet_id='test',status='connected')
        self.agent=FleetAgent.objects.create(agent_username='installation-agent', agent_id='installation-id',
            model='test-model', managing_daemon=self.daemon, env={'OTHER':'explicit'})

    def test_native_container_and_projection_use_effective_copy(self):
        from .provisioner import _agent_container
        for explicit in ({}, {KEY:''}, {KEY:'{}'}):
            with self.subTest(explicit=explicit):
                self.agent.env=explicit
                container=_agent_container(self.agent)
                actual={item.name:item.value for item in container.env}
                self.assertEqual(actual[KEY], explicit.get(KEY,DEFAULT))
                projected=runtime_fleet._fleet_agent_row_to_runtime(self.agent)
                self.assertEqual(projected['env'][KEY],explicit.get(KEY,DEFAULT))
                self.assertEqual(self.agent.env,explicit)

    def test_per_user_bootstrap_roster_uses_same_default_without_persistence(self):
        from .management.commands.provision_user_daemon import Command
        self.agent.owner_id=73
        self.agent.save(update_fields=['owner_id'])
        roster=Command()._build_roster(73)
        # The command returns the roster and its separately enabled identity list.
        agents=roster[0] if isinstance(roster,tuple) else roster
        self.assertEqual(agents[0]['env'], {'OTHER':'explicit',KEY:DEFAULT})
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.env, {'OTHER':'explicit'})

    def test_new_daemon_provision_requests_raw_env_edit_even_for_empty_agent_env(self):
        self.agent.agent_id=''
        self.agent.managing_daemon=None
        self.agent.env={}
        self.agent.save()
        with patch.object(runtime_fleet,'runtime_control_enabled',return_value=True), \
             patch.object(runtime_fleet,'_post_control',return_value=(True,{'agent_id':'new-id'})), \
             patch.object(runtime_fleet,'set_runtime_agent_enabled',return_value=(True,{})), \
             patch.object(runtime_fleet,'configure_runtime_agent',return_value=(True,'')) as configure:
            ok,detail=runtime_fleet.provision_runtime_agent_via_daemon(self.agent,self.daemon)
        self.assertTrue(ok,detail)
        self.assertEqual(configure.call_args.args[1]['env'],{})
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.env,{})

    def test_partial_push_retry_ack_preserve_raw_desired_and_current_effective_fence(self):
        from .consumers import DaemonLinkConsumer
        from .config_fingerprints import config_fingerprints
        layer=MagicMock()
        layer.group_send=AsyncMock()
        updates={'env':{'OTHER':'changed'}}
        with patch('channels.layers.get_channel_layer',return_value=layer):
            ok,_=runtime_fleet._configure_runtime_agent_via_daemon_link(self.agent.agent_id,updates)
        self.assertTrue(ok)
        sent=layer.group_send.call_args.args[1]['frame']
        self.assertEqual(sent['config']['env'], {'OTHER':'changed',KEY:DEFAULT})
        self.assertEqual(sent['config_fingerprints'],config_fingerprints(sent['config']))
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.env,updates['env'])
        self.assertEqual(self.agent.pending_config,updates)
        consumer=DaemonLinkConsumer()
        consumer._registry_pk=self.daemon.pk
        consumer._daemon_id=self.daemon.daemon_id
        retry=DaemonLinkConsumer.__dict__['_db_pending_config_frames'].func(consumer)
        self.assertEqual(retry[0]['config'],sent['config'])
        os.environ[KEY]='{}'
        old_ack={'agent_id':self.agent.agent_id,'config_fingerprints':sent['config_fingerprints']}
        result=DaemonLinkConsumer.__dict__['_db_confirm_config_result'].func(consumer,old_ack)
        self.assertEqual(result['confirmed'],[])
        fresh=DaemonLinkConsumer.__dict__['_db_pending_config_frames'].func(consumer)[0]
        result=DaemonLinkConsumer.__dict__['_db_confirm_config_result'].func(consumer,{
            'agent_id':self.agent.agent_id,'config_fingerprints':fresh['config_fingerprints']})
        self.assertEqual(result['confirmed'],['env'])
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.env,updates['env'])
        self.assertEqual(self.agent.pending_config,{})
        self.assertEqual(updates,{'env':{'OTHER':'changed'}})

    def test_current_reconnect_applies_default_and_respects_explicit_empty(self):
        from .consumers import DaemonLinkConsumer
        consumer=DaemonLinkConsumer()
        consumer._registry_pk=self.daemon.pk
        frames=DaemonLinkConsumer.__dict__['_db_current_desired_config_frames'].func(consumer)
        self.assertEqual(frames[0]['config']['env'],{'OTHER':'explicit',KEY:DEFAULT})
        self.agent.env={KEY:''}
        self.agent.save(update_fields=['env'])
        frames=DaemonLinkConsumer.__dict__['_db_current_desired_config_frames'].func(consumer)
        self.assertEqual(frames[0]['config']['env'],{KEY:''})

    def test_legacy_http_partial_env_keeps_raw_cache_and_non_env_updates_unchanged(self):
        with patch.object(runtime_fleet,'_configure_runtime_agent_via_daemon_link',return_value=(False,'offline')), \
             patch.object(runtime_fleet,'_runtime_control_url',return_value='http://control.example.test'), \
             patch.object(runtime_fleet,'_control_secret',return_value='synthetic-test'), \
             patch.object(runtime_fleet.requests,'patch',return_value=MagicMock(status_code=200)) as request:
            ok,detail=runtime_fleet.configure_runtime_agent(self.agent.agent_id,{'env':{KEY:''}})
            self.assertTrue(ok,detail)
            self.assertEqual(request.call_args.kwargs['json']['env'],{KEY:''})
            ok,detail=runtime_fleet.configure_runtime_agent(self.agent.agent_id,{'env':{'OTHER':'new'}})
            self.assertTrue(ok,detail)
            self.assertEqual(request.call_args.kwargs['json']['env'],{'OTHER':'new',KEY:DEFAULT})
            self.agent.refresh_from_db()
            self.assertEqual(self.agent.env,{'OTHER':'new'})
            runtime_fleet.configure_runtime_agent(self.agent.agent_id,{'model':'new-model'})
            self.assertNotIn('env',request.call_args.kwargs['json'])
            self.assertEqual(request.call_args.kwargs['json']['model'],'new-model')
