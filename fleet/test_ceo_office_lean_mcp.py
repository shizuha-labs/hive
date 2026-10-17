"""CEO Office resident talk-seat MCP + heartbeat floor."""

from django.test import SimpleTestCase, TestCase

from fleet import provisioner
from fleet.effective_capabilities import compute_effective_capability_grant
from fleet.models import (
    BASE_PLATFORM_MCP_SERVERS,
    LEAN_CONVERSATIONAL_MCP_SERVERS,
    DEFAULT_TEAM_CAPABILITY_SPECS,
    FleetAgent,
    ensure_team_required_capability,
    platform_mcp_floor_for_teams,
    trim_lean_conversational_mcp,
    _team_mcp_server_refs,
)


class CeoOfficeLeanMcpTests(TestCase):
    def test_floor_is_platform_plus_hive_for_exclusive_ceo_office(self):
        self.assertEqual(
            platform_mcp_floor_for_teams(['ceo-office']),
            LEAN_CONVERSATIONAL_MCP_SERVERS,
        )
        self.assertEqual(
            platform_mcp_floor_for_teams(['engineering']),
            BASE_PLATFORM_MCP_SERVERS,
        )
        self.assertEqual(
            platform_mcp_floor_for_teams(['ceo-office', 'engineering']),
            BASE_PLATFORM_MCP_SERVERS,
        )

    def test_spec_includes_platform_base_and_hive(self):
        spec = DEFAULT_TEAM_CAPABILITY_SPECS['ceo-office']
        refs = _team_mcp_server_refs(spec, 'ceo-office')
        self.assertEqual(refs, list(LEAN_CONVERSATIONAL_MCP_SERVERS))
        self.assertIn('admin', refs)
        self.assertIn('id', refs)
        self.assertIn('hive', refs)

    def test_trim_keeps_operator_extras_and_platform_floor(self):
        self.assertEqual(
            trim_lean_conversational_mcp(['pulse', 'admin', 'id', 'scs', 'mail']),
            ['pulse', 'connect', 'wiki', 'admin', 'id', 'hive', 'scs', 'mail'],
        )

    def test_ensure_unions_platform_floor_and_hive(self):
        cap, _req = ensure_team_required_capability('ceo-office', actor='test')
        cap.mcp_server_refs = ['pulse', 'connect', 'wiki', 'admin', 'id']
        cap.save(update_fields=['mcp_server_refs'])
        cap, _req = ensure_team_required_capability('ceo-office', actor='test')
        self.assertEqual(cap.mcp_server_refs, list(LEAN_CONVERSATIONAL_MCP_SERVERS))

    def test_effective_grant_includes_admin_id_hive(self):
        ensure_team_required_capability('ceo-office', actor='test')
        grant = compute_effective_capability_grant(['ceo-office'])
        self.assertEqual(grant['enabled_mcp_servers'], sorted(LEAN_CONVERSATIONAL_MCP_SERVERS))
        self.assertEqual(grant['base_mcp_missing_pre_floor'], [])
        self.assertIn('admin', grant['enabled_mcp_servers'])
        self.assertIn('id', grant['enabled_mcp_servers'])
        self.assertIn('hive', grant['enabled_mcp_servers'])
        self.assertEqual(
            grant['eager_skills'],
            ['heartbeat-protocol'],
        )
        self.assertIn('heartbeat-protocol', grant['skills'])


class CeoOfficeMcpAllowListEnvTests(SimpleTestCase):
    def _env(self, container):
        return {item.name: item.value for item in (container.env or []) if item.value is not None}

    def test_hina_pod_gets_full_effective_mcp_allow_list(self):
        agent = FleetAgent(
            agent_username='hina',
            agent_id='hina-id',
            display_name='Hina',
            role='Executive Assistant',
            mcp_config={
                'enabled_mcp_server_ids': ['pulse', 'connect', 'wiki'],
                'mcp_servers': [{'name': 'pulse', 'slug': 'pulse'}],
            },
            env={'AGENT_TEAM': 'ceo-office'},
        )
        env = self._env(provisioner._agent_container(agent))
        self.assertEqual(
            env['AGENT_EFFECTIVE_MCP_SERVICES'],
            'pulse,connect,wiki,admin,id,hive',
        )
        self.assertEqual(env['SHIZUHA_MCP_SERVICES'], 'pulse,connect,wiki,admin,id,hive')
        self.assertEqual(env['SHIZUHA_LEAN_MCP'], '1')
        self.assertEqual(env['SHIZUHA_TALK_MINIMAL_PROMPT'], '0')
        self.assertIn('AGENT_EFFECTIVE_MCP_SERVICES', provisioner._RUNTIME_LANE_ENV_NAMES)
        self.assertIn('SHIZUHA_MCP_SERVICES', provisioner._RUNTIME_LANE_ENV_NAMES)

    def test_lean_username_unions_declared_mcp_with_floor(self):
        agent = FleetAgent(
            agent_username='aya',
            agent_id='aya-id',
            display_name='Aya',
            mcp_config={'enabled_mcp_server_ids': ['pulse', 'admin', 'id', 'scs']},
        )
        slugs = provisioner._agent_effective_mcp_services(agent)
        self.assertEqual(slugs, ['pulse', 'admin', 'id', 'scs', 'connect', 'wiki', 'hive'])
        self.assertIn('admin', slugs)
        self.assertIn('hive', slugs)

    def test_yuna_scli_seat_is_resident_by_username(self):
        agent = FleetAgent(
            agent_username='yuna',
            agent_id='yuna-id',
            display_name='Yuna',
            mcp_config={'enabled_mcp_server_ids': ['pulse', 'admin', 'id', 'scs']},
        )
        slugs = provisioner._agent_effective_mcp_services(agent)
        self.assertEqual(slugs, ['pulse', 'admin', 'id', 'scs', 'connect', 'wiki', 'hive'])
        env = self._env(provisioner._agent_container(agent))
        self.assertEqual(env['SHIZUHA_LEAN_MCP'], '1')
        self.assertEqual(env['AGENT_TEAM'], 'ceo-office')
        self.assertEqual(env['SHIZUHA_FIRST_HEARTBEAT_MS'], '8000')
        self.assertEqual(env['SHIZUHA_TALK_MINIMAL_PROMPT'], '0')
        self.assertEqual(env['SHIZUHA_CONNECT_AUTOREPLY'], '0')
        self.assertEqual(env['VLLM_STREAM_STALL_MS'], '20000')

    def test_ena_scli_grok_seat_is_resident_by_username(self):
        agent = FleetAgent(
            agent_username='ena',
            agent_id='ena-id',
            display_name='Ena',
            mcp_config={'enabled_mcp_server_ids': ['pulse', 'admin', 'id', 'scs']},
        )
        slugs = provisioner._agent_effective_mcp_services(agent)
        self.assertEqual(slugs, ['pulse', 'admin', 'id', 'scs', 'connect', 'wiki', 'hive'])
        env = self._env(provisioner._agent_container(agent))
        self.assertEqual(env['SHIZUHA_LEAN_MCP'], '1')
        self.assertEqual(env['AGENT_TEAM'], 'ceo-office')
        self.assertEqual(env['SHIZUHA_FIRST_HEARTBEAT_MS'], '8000')
        self.assertEqual(env['SHIZUHA_TALK_MINIMAL_PROMPT'], '0')
        self.assertEqual(env['SHIZUHA_CONNECT_AUTOREPLY'], '0')

    def test_stale_talk_minimal_prompt_is_rewritten_off(self):
        agent = FleetAgent(
            agent_username='ena',
            agent_id='ena-id',
            display_name='Ena',
            mcp_config={'enabled_mcp_server_ids': list(LEAN_CONVERSATIONAL_MCP_SERVERS)},
            env={'AGENT_TEAM': 'ceo-office', 'SHIZUHA_TALK_MINIMAL_PROMPT': '1'},
        )
        env = self._env(provisioner._agent_container(agent))
        self.assertEqual(env['SHIZUHA_TALK_MINIMAL_PROMPT'], '0')

    def test_hina_voice_seat_enables_connect_autoreply(self):
        agent = FleetAgent(
            agent_username='hina',
            agent_id='hina-id',
            display_name='Hina',
            model='cortex/grok-voice-think-fast-2.0',
            mcp_config={'enabled_mcp_server_ids': list(LEAN_CONVERSATIONAL_MCP_SERVERS)},
        )
        env = self._env(provisioner._agent_container(agent))
        self.assertEqual(env['SHIZUHA_CONNECT_AUTOREPLY'], '1')

    def test_stale_connect_autoreply_is_rewritten_off(self):
        agent = FleetAgent(
            agent_username='ena',
            agent_id='ena-id',
            display_name='Ena',
            mcp_config={'enabled_mcp_server_ids': list(LEAN_CONVERSATIONAL_MCP_SERVERS)},
            env={'AGENT_TEAM': 'ceo-office', 'SHIZUHA_CONNECT_AUTOREPLY': '1'},
        )
        env = self._env(provisioner._agent_container(agent))
        self.assertEqual(env['SHIZUHA_CONNECT_AUTOREPLY'], '0')

    def test_non_lean_agent_keeps_declared_mcp_config(self):
        agent = FleetAgent(
            agent_username='jun',
            agent_id='jun-id',
            display_name='Jun',
            mcp_config={'enabled_mcp_server_ids': ['pulse', 'connect', 'wiki', 'admin']},
        )
        self.assertEqual(
            provisioner._agent_effective_mcp_services(agent),
            ['pulse', 'connect', 'wiki', 'admin'],
        )

    def test_work_agent_rewrites_stale_15m_heartbeat_floor(self):
        agent = FleetAgent(
            agent_username='nami',
            agent_id='nami-id',
            display_name='Nami',
            model='cortex/DeepSeek-V4-Flash',
            mcp_config={'enabled_mcp_server_ids': ['pulse', 'connect', 'wiki']},
            env={'SHIZUHA_IDLE_HEARTBEAT_MS': '900000'},
        )
        env = self._env(provisioner._agent_container(agent))
        self.assertEqual(env['SHIZUHA_IDLE_HEARTBEAT_MS'], '90000')
        self.assertEqual(env['SHIZUHA_HEARTBEAT_DEBOUNCE_MS'], '90000')
        self.assertEqual(env['SHIZUHA_FIRST_HEARTBEAT_MS'], '8000')

    def test_work_agent_missing_heartbeat_env_gets_floor(self):
        agent = FleetAgent(
            agent_username='kai',
            agent_id='kai-id',
            display_name='Kai',
            model='cortex/DeepSeek-V4-Flash',
        )
        env = self._env(provisioner._agent_container(agent))
        self.assertEqual(env['SHIZUHA_IDLE_HEARTBEAT_MS'], '90000')
        self.assertEqual(env['SHIZUHA_FIRST_HEARTBEAT_MS'], '8000')
        self.assertEqual(env['SHIZUHA_HEARTBEAT_DEBOUNCE_MS'], '90000')

    def test_lean_seat_keeps_30m_idle_when_work_floor_is_applied(self):
        agent = FleetAgent(
            agent_username='yuna',
            agent_id='yuna-id',
            display_name='Yuna',
            mcp_config={'enabled_mcp_server_ids': list(LEAN_CONVERSATIONAL_MCP_SERVERS)},
        )
        env = self._env(provisioner._agent_container(agent))
        # Lean/voice seats keep the 30m long-idle fallback by design.
        self.assertEqual(env['SHIZUHA_IDLE_HEARTBEAT_MS'], '1800000')
        self.assertEqual(env['SHIZUHA_HEARTBEAT_DEBOUNCE_MS'], '1800000')
        self.assertEqual(env['SHIZUHA_FIRST_HEARTBEAT_MS'], '8000')
        self.assertEqual(env['SHIZUHA_LEAN_MCP'], '1')


class WorkAgentHeartbeatHealTests(TestCase):
    def test_heal_rewrites_stale_fleet_env_on_the_row(self):
        agent = FleetAgent.objects.create(
            agent_username='sara-hb',
            agent_id='sara-hb-id',
            display_name='Sara',
            model='cortex/DeepSeek-V4-Flash',
            env={'SHIZUHA_IDLE_HEARTBEAT_MS': '900000'},
        )
        self.assertTrue(provisioner.heal_stale_work_agent_heartbeat_env(agent))
        self.assertEqual(agent.env['SHIZUHA_IDLE_HEARTBEAT_MS'], '90000')
        self.assertEqual(agent.env['SHIZUHA_FIRST_HEARTBEAT_MS'], '8000')
        self.assertEqual(agent.env['SHIZUHA_HEARTBEAT_DEBOUNCE_MS'], '90000')
        agent.save(update_fields=['env', 'updated_at'])
        agent.refresh_from_db()
        self.assertEqual(agent.env['SHIZUHA_IDLE_HEARTBEAT_MS'], '90000')
        self.assertFalse(provisioner.heal_stale_work_agent_heartbeat_env(agent))
