import hashlib

from django.test import TestCase
from rest_framework.test import APIClient

from .models import DaemonCredential, FleetAgent


class AgentRuntimeLaneViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.token = 'plat4112-daemon-token'
        DaemonCredential.objects.create(
            daemon_id='runtime-fleet',
            token_hash=hashlib.sha256(self.token.encode()).hexdigest(),
        )
        self.agent = FleetAgent.objects.create(
            agent_id='plat4112-runtime-id',
            agent_username='plat4112-agent',
            model='gpt-5.6-sol',
            mcp_config={'execution_method': 'codex_app_server'},
            model_overrides={'codex_app_server': 'gpt-5.6-sol'},
            reasoning_effort='high',
        )
        # FleetAgent.save() intentionally clears legacy fallbacks on a visible
        # primary-model edit. Daemon-link snapshots write the complete runtime
        # lane directly, so mirror that persisted shape for this read contract.
        FleetAgent.objects.filter(pk=self.agent.pk).update(model_fallbacks=[
            {'method': 'codex_app_server', 'model': 'gpt-5.6-sol'},
            {'method': 'shizuha', 'model': 'cortex/GLM-5.2'},
        ])
        self.url = f'/hive/api/v1/fleet/agents/{self.agent.agent_id}/runtime-lane/'

    def test_daemon_credential_reads_only_runtime_lane_fields(self):
        response = self.client.get(self.url, **{
            'HTTP_X_HIVE_DAEMON_ID': 'runtime-fleet',
            'HTTP_X_HIVE_DAEMON_TOKEN': self.token,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'model': 'gpt-5.6-sol',
            'execution_method': 'codex_app_server',
            'model_fallbacks': [
                {'method': 'codex_app_server', 'model': 'gpt-5.6-sol'},
                {'method': 'shizuha', 'model': 'cortex/GLM-5.2'},
            ],
            'model_overrides': {'codex_app_server': 'gpt-5.6-sol'},
            'reasoning_effort': 'high',
        })

    def test_invalid_daemon_credential_is_rejected(self):
        response = self.client.get(self.url, **{
            'HTTP_X_HIVE_DAEMON_ID': 'runtime-fleet',
            'HTTP_X_HIVE_DAEMON_TOKEN': 'wrong-token',
        })

        self.assertEqual(response.status_code, 403)
