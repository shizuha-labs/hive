import hashlib

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import DaemonCredential, FleetAgent


class DaemonIdentityRosterViewTests(TestCase):
    url = '/hive/api/v1/fleet/daemon/identity-roster/'

    def setUp(self):
        self.client = APIClient()
        self.token = 'daemon-roster-test-token'
        DaemonCredential.objects.create(
            daemon_id='runtime-fleet',
            token_hash=hashlib.sha256(self.token.encode()).hexdigest(),
        )
        FleetAgent.objects.create(
            agent_id='jun-runtime-id',
            agent_username='jun',
            identity_user_id=42,
            status=FleetAgent.Status.RUNNING,
        )
        FleetAgent.objects.create(
            agent_id='sora-runtime-id',
            agent_username='sora',
            status=FleetAgent.Status.STOPPED,
        )
        FleetAgent.objects.create(
            agent_id='deleted-runtime-id',
            agent_username='deleted-agent',
            status=FleetAgent.Status.DELETED,
        )
        FleetAgent.objects.create(
            agent_id='',
            agent_username='not-yet-identified',
            status=FleetAgent.Status.PENDING,
        )

    def test_valid_daemon_credential_gets_only_narrow_identity_fields(self):
        response = self.client.get(self.url, **{
            'HTTP_X_HIVE_DAEMON_ID': 'runtime-fleet',
            'HTTP_X_HIVE_DAEMON_TOKEN': self.token,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'count': 2,
            'agents': [
                {'id': 'jun-runtime-id', 'username': 'jun', 'status': 'running', 'user_id': 42},
                {'id': 'sora-runtime-id', 'username': 'sora', 'status': 'stopped', 'user_id': None},
            ],
        })

    def test_invalid_daemon_credential_is_rejected(self):
        response = self.client.get(self.url, **{
            'HTTP_X_HIVE_DAEMON_ID': 'runtime-fleet',
            'HTTP_X_HIVE_DAEMON_TOKEN': 'wrong-token',
        })
        self.assertEqual(response.status_code, 403)

    def test_staff_jwt_session_is_also_authorized(self):
        user = get_user_model().objects.create_user(
            username='roster-admin', password='test-password', is_staff=True,
        )
        self.client.force_authenticate(user=user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['count'], 2)

    def test_duplicate_stable_agent_ids_fail_closed(self):
        FleetAgent.objects.all().delete()
        FleetAgent.objects.create(
            agent_id='duplicate-runtime-id',
            agent_username='first-agent',
            status=FleetAgent.Status.RUNNING,
        )
        FleetAgent.objects.create(
            agent_id='duplicate-runtime-id',
            agent_username='second-agent',
            status=FleetAgent.Status.RUNNING,
        )

        response = self.client.get(self.url, **{
            'HTTP_X_HIVE_DAEMON_ID': 'runtime-fleet',
            'HTTP_X_HIVE_DAEMON_TOKEN': self.token,
        })
        self.assertEqual(response.status_code, 503)
