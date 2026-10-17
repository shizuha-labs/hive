import uuid
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from claws.models import Claw, WhatsAppLink
from claws.views import (
    _normalize_notify_event_type,
    ClawDashboardUrlView,
    ClawWhatsAppLinkView,
    ClawWhatsAppLinksView,
    ClawWhatsAppLinkDetailView,
    ClawWhatsAppPairingView,
)


class WhatsAppDashboardManagementTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        User = get_user_model()
        self.owner = User.objects.create_user(username='owner', email='owner@example.com')
        self.other = User.objects.create_user(username='other', email='other@example.com')
        self.claw = Claw.objects.create(
            owner_id=self.owner.id,
            owner_username='owner',
            owner_email='owner@example.com',
            name='Owner Claw',
            namespace='claw-owner',
            status=Claw.Status.RUNNING,
        )
        self.link = WhatsAppLink.objects.create(
            code='abc123',
            claw=self.claw,
            owner_id=self.owner.id,
            wa_phone_jid='919876543210@s.whatsapp.net',
            status=WhatsAppLink.Status.LINKED,
            expires_at=timezone.now() + timedelta(minutes=5),
            linked_at=timezone.now(),
        )

    def _request(self, method, url, user, data=None):
        req = getattr(self.factory, method)(url, data or {}, format='json')
        force_authenticate(req, user=user)
        return req


    def test_channel_health_notify_event_type_is_preserved(self):
        self.assertEqual(_normalize_notify_event_type('channel_health'), 'channel_health')

    def test_list_returns_masked_owner_links_only(self):
        req = self._request('get', '/', self.owner)
        resp = ClawWhatsAppLinksView.as_view()(req, claw_id=self.claw.id)
        self.assertEqual(resp.status_code, 200)
        row = resp.data['results'][0]
        self.assertEqual(row['status'], 'active')
        self.assertNotIn('9876543210', row['masked_number'])
        self.assertTrue(row['masked_number'].endswith('3210'))

    def test_non_owner_gets_404(self):
        req = self._request('get', '/', self.other)
        resp = ClawWhatsAppLinksView.as_view()(req, claw_id=self.claw.id)
        self.assertEqual(resp.status_code, 404)

    def test_pairing_accepts_numeric_string_owner_id(self):
        user = type('User', (), {
            'id': str(self.owner.id),
            'pk': str(self.owner.id),
            'username': self.owner.username,
            'email': self.owner.email,
            'is_staff': False,
            'is_authenticated': True,
        })()
        req = self._request('post', '/', user)

        resp = ClawWhatsAppPairingView.as_view()(req, claw_id=self.claw.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['mode'], 'claw_native_pairing')
        self.assertEqual(resp.data['dashboard_url'], f'/hive/assistants/{self.claw.id}/dash')
        self.assertEqual(resp.data['dashboard_path'], f'/hive/assistants/{self.claw.id}/dash')
        self.assertEqual(resp.data['dashboard_api_path'], f'/hive/api/v1/claws/{self.claw.id}/dashboard-url?redirect=1')
        self.assertNotIn('/hive/openclaw/', resp.data['dashboard_url'])

    def test_frontend_pairing_link_prefers_authenticated_dashboard_api_path(self):
        drawer = Path('frontend/src/components/AgentDrawer.jsx').read_text()
        self.assertIn(
            'href={waPairing.dashboard_api_path || waPairing.dashboard_url || waPairing.dashboard_path}',
            drawer,
        )

    def test_frontend_exposes_direct_dashboard_launcher(self):
        drawer = Path('frontend/src/components/AgentDrawer.jsx').read_text()
        self.assertIn("const isOpenClawAgent = claw?.agent_type === 'claw'", drawer)
        self.assertIn('const dashboardHref = isOpenClawAgent && claw?.id ? `/hive/assistants/${claw.id}/dash` : \'\'', drawer)
        self.assertIn('Open dashboard <ExternalLink', drawer)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN='dashboard-test-secret')
    @patch('claws.views.lifecycle.auth_state', return_value={'authorized': True})
    def test_dashboard_redirect_targets_owner_routed_openclaw_path(self, _auth_state):
        req = self._request('get', '/?redirect=1', self.owner)

        resp = ClawDashboardUrlView.as_view()(req, claw_id=self.claw.id)

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith(f'/hive/openclaw/{self.owner.id}/{self.claw.id}/?'))
        self.assertIn('hive_dashboard_token=', resp['Location'])
        self.assertIn('#token=', resp['Location'])

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN='dashboard-test-secret')
    def test_hermes_dashboard_url_does_not_mint_openclaw_link(self):
        hermes = Claw.objects.create(
            owner_id=self.owner.id,
            owner_username='owner',
            owner_email='owner@example.com',
            name='Owner Hermes',
            namespace='hermes-owner',
            agent_type=Claw.AgentType.HERMES,
            status=Claw.Status.RUNNING,
        )
        req = self._request('get', '/?redirect=1', self.owner)

        resp = ClawDashboardUrlView.as_view()(req, claw_id=hermes.id)

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['agent_type'], Claw.AgentType.HERMES)

    def test_hermes_whatsapp_pairing_does_not_return_openclaw_payload(self):
        hermes = Claw.objects.create(
            owner_id=self.owner.id,
            owner_username='owner',
            owner_email='owner@example.com',
            name='Owner Hermes',
            namespace='hermes-owner',
            agent_type=Claw.AgentType.HERMES,
            status=Claw.Status.RUNNING,
        )
        req = self._request('post', '/', self.owner)

        resp = ClawWhatsAppPairingView.as_view()(req, claw_id=hermes.id)

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['agent_type'], Claw.AgentType.HERMES)

    def test_compat_whatsapp_link_ignores_newer_non_openclaw_assistants(self):
        Claw.objects.create(
            owner_id=self.owner.id,
            owner_username='owner',
            owner_email='owner@example.com',
            name='Owner Hermes',
            namespace='hermes-owner',
            agent_type=Claw.AgentType.HERMES,
            status=Claw.Status.RUNNING,
        )
        req = self._request('post', '/', self.owner)

        resp = ClawWhatsAppLinkView.as_view()(req)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['claw_id'], str(self.claw.id))
        self.assertEqual(resp.data['mode'], 'claw_native_pairing')

    def test_pause_and_resume_link(self):
        view = ClawWhatsAppLinkDetailView.as_view()
        req = self._request('patch', '/', self.owner, {'status': 'paused'})
        resp = view(req, claw_id=self.claw.id, code=self.link.code)
        self.assertEqual(resp.status_code, 200)
        self.link.refresh_from_db()
        self.assertEqual(self.link.status, WhatsAppLink.Status.PAUSED)
        self.assertEqual(resp.data['status'], 'paused')

        req = self._request('patch', '/', self.owner, {'status': 'active'})
        resp = view(req, claw_id=self.claw.id, code=self.link.code)
        self.assertEqual(resp.status_code, 200)
        self.link.refresh_from_db()
        self.assertEqual(self.link.status, WhatsAppLink.Status.LINKED)

    @override_settings(HIVE_WA_SEND_URL='')
    def test_unlink_deletes_and_attempts_confirmation(self):
        with patch('claws.views._send_wa_unlink_confirmation', return_value=True) as send:
            req = self._request('delete', '/', self.owner)
            resp = ClawWhatsAppLinkDetailView.as_view()(req, claw_id=self.claw.id, code=self.link.code)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(WhatsAppLink.objects.filter(code=self.link.code).exists())
        send.assert_called_once_with('919876543210@s.whatsapp.net')
