from types import SimpleNamespace

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import Claw
from .serializers import ClawSerializer


def user(user_id, username, email=None, *, is_staff=False):
    return SimpleNamespace(
        id=user_id,
        username=username,
        email=email or f'{username}@example.com',
        is_staff=is_staff,
        is_authenticated=True,
    )


class Hive286ConnectAgentsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.harshita = user(101, 'harshita', 'harshita@example.com')
        self.hritik = user(202, 'hritik', 'hritik@example.com', is_staff=True)
        self.claw = Claw.objects.create(
            owner_id=self.harshita.id,
            owner_username=self.harshita.username,
            owner_email=self.harshita.email,
            name='Harshita Claw',
            tier_id='free',
            agent_type=Claw.AgentType.CLAW,
            model='',
            status=Claw.Status.RUNNING,
            pod_name='claw-pod',
        )
        self.hermes = Claw.objects.create(
            owner_id=self.harshita.id,
            owner_username=self.harshita.username,
            owner_email=self.harshita.email,
            name='Hermes',
            tier_id='pro',
            agent_type=Claw.AgentType.HERMES,
            model='DeepSeek-V4-Flash',
            status=Claw.Status.STOPPED,
        )
        Claw.objects.create(
            owner_id=self.harshita.id,
            owner_username=self.harshita.username,
            owner_email=self.harshita.email,
            name='SCLI already handled elsewhere',
            tier_id='scli',
            agent_type=Claw.AgentType.SCLI,
            model='DeepSeek-V4-Flash',
            status=Claw.Status.RUNNING,
        )
        Claw.objects.create(
            owner_id=self.hritik.id,
            owner_username=self.hritik.username,
            owner_email=self.hritik.email,
            name='Hritik Claw',
            tier_id='free',
            agent_type=Claw.AgentType.CLAW,
            model='DeepSeek-V4-Flash',
            status=Claw.Status.RUNNING,
        )
        Claw.objects.create(
            owner_id=self.harshita.id,
            owner_username=self.harshita.username,
            owner_email=self.harshita.email,
            name='Deleted Claw',
            tier_id='deleted',
            agent_type=Claw.AgentType.CLAW,
            model='DeepSeek-V4-Flash',
            status=Claw.Status.DELETED,
        )

    @override_settings(HIVE_CORTEX_DEFAULT_MODEL='DeepSeek-V4-Flash')
    def test_owner_sees_hermes_claw_and_scli_connect_agents(self):
        self.client.force_authenticate(user=self.harshita)

        response = self.client.get('/api/v1/connect/agents/')

        self.assertEqual(response.status_code, 200)
        # HIVE-1957: SCLI claws are Connect-ready now (WS ConnectClient auth
        # wired at provision time) — they appear in discovery alongside
        # OpenClaw/Hermes so owners can DM their personal SCLI assistant.
        self.assertEqual(response.data['count'], 3)
        results = response.data['results']
        stable_ids = {item['stable_id'] for item in results}
        scli = Claw.objects.get(owner_id=self.harshita.id, agent_type=Claw.AgentType.SCLI)
        self.assertEqual(stable_ids, {
            f'hive:claw:{self.claw.id}',
            f'hive:hermes:{self.hermes.id}',
            f'hive:scli:{scli.id}',
        })
        by_type = {item['agent_type']: item for item in results}
        self.assertEqual(by_type['claw']['runtime_kind'], 'openclaw')
        self.assertEqual(by_type['hermes']['runtime_kind'], 'hermes')
        self.assertEqual(by_type['scli']['runtime_kind'], 'scli')
        self.assertEqual(by_type['claw']['effective_model'], 'DeepSeek-V4-Flash')
        self.assertEqual(by_type['claw']['model_provider'], 'Cortex')
        self.assertEqual(by_type['claw']['runtime_backend'], 'cortex')
        self.assertEqual(by_type['claw']['owner_id'], self.harshita.id)
        self.assertEqual(by_type['claw']['liveness']['status'], Claw.Status.RUNNING)
        self.assertEqual(by_type['claw']['liveness']['pod_name'], 'claw-pod')
        self.assertIn('hive%3Aclaw%3A', by_type['claw']['open_in_connect_path'])
        self.assertTrue(by_type['claw']['connect_username'].startswith('hive-claw-'))
        self.assertTrue(by_type['scli']['connect_username'].startswith('hive-scli-'))
        self.assertEqual(by_type['scli']['runtime_kind'], 'scli')
        self.assertEqual(by_type['scli']['effective_model'], 'DeepSeek-V4-Flash')
        self.assertEqual(by_type['scli']['model_provider'], 'Cortex')

    def test_staff_user_does_not_receive_other_owners_personal_agents(self):
        self.client.force_authenticate(user=self.hritik)

        response = self.client.get('/api/v1/connect/agents/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['owner_id'], self.hritik.id)
        self.assertNotIn('harshita', {item['owner_username'] for item in response.data['results']})

    def test_claw_serializer_exposes_open_in_connect_profile_for_hive_agents_only(self):
        payload = ClawSerializer(self.claw).data

        self.assertEqual(payload['connect_profile']['stable_id'], f'hive:claw:{self.claw.id}')
        self.assertIn('/connect/?agent=', payload['connect_profile']['open_in_connect_path'])

        scli = Claw.objects.get(agent_type=Claw.AgentType.SCLI)
        scli_profile = ClawSerializer(scli).data['connect_profile']
        self.assertEqual(scli_profile['stable_id'], f'hive:scli:{scli.id}')
        self.assertIn('/connect/?agent=', scli_profile['open_in_connect_path'])
