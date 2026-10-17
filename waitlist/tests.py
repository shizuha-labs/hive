"""
HIVE-100 — Free Claw waitlist tests.

Live verification is the preferred proof on the running stack, but these unit
tests pin the launch-gate, funnel-event, and dedup contracts so a regression
fails CI rather than silently breaking the demand-capture funnel.
"""
import json
import os
import subprocess
import sys
from unittest.mock import patch

from hive_project.settings import _env_bool

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .models import Channel, WaitlistEntry, WaitlistEvent


class WaitlistLaunchFlagSettingsTests(SimpleTestCase):
    def _setting_from_fresh_process(self, env_value=None):
        env = os.environ.copy()
        if env_value is None:
            env.pop('HIVE_FREE_CLAW_WAITLIST_ENABLED', None)
        else:
            env['HIVE_FREE_CLAW_WAITLIST_ENABLED'] = env_value
        result = subprocess.run(
            [
                sys.executable,
                '-c',
                (
                    'import json; '
                    'from hive_project import settings; '
                    'print(json.dumps(settings.HIVE_FREE_CLAW_WAITLIST_ENABLED))'
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return json.loads(result.stdout)

    def test_env_bool_accepts_launch_truthy_values(self):
        with patch.dict('os.environ', {'HIVE_FREE_CLAW_WAITLIST_ENABLED': 'true'}):
            self.assertTrue(_env_bool('HIVE_FREE_CLAW_WAITLIST_ENABLED'))
        with patch.dict('os.environ', {'HIVE_FREE_CLAW_WAITLIST_ENABLED': '1'}):
            self.assertTrue(_env_bool('HIVE_FREE_CLAW_WAITLIST_ENABLED'))
        with patch.dict('os.environ', {'HIVE_FREE_CLAW_WAITLIST_ENABLED': 'yes'}):
            self.assertTrue(_env_bool('HIVE_FREE_CLAW_WAITLIST_ENABLED'))

    def test_env_bool_defaults_waitlist_launch_gate_closed(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(_env_bool('HIVE_FREE_CLAW_WAITLIST_ENABLED', False))
        with patch.dict('os.environ', {'HIVE_FREE_CLAW_WAITLIST_ENABLED': 'false'}):
            self.assertFalse(_env_bool('HIVE_FREE_CLAW_WAITLIST_ENABLED'))

    def test_env_var_wires_into_waitlist_launch_setting(self):
        self.assertTrue(self._setting_from_fresh_process('true'))
        self.assertTrue(self._setting_from_fresh_process('1'))

    def test_waitlist_launch_setting_defaults_fail_closed(self):
        self.assertFalse(self._setting_from_fresh_process(None))
        self.assertFalse(self._setting_from_fresh_process('false'))


@override_settings(HIVE_FREE_CLAW_WAITLIST_ENABLED=False)
class LaunchGateTests(TestCase):
    def test_landing_404_when_disabled(self):
        self.assertEqual(self.client.get('/hive/free-claw').status_code, 404)

    def test_beacon_404_when_disabled(self):
        resp = self.client.post('/hive/api/v1/waitlist/event',
                                {'event_type': 'start'}, content_type='application/json')
        self.assertEqual(resp.status_code, 404)

    def test_json_signup_404_when_disabled(self):
        resp = self.client.post('/hive/api/v1/waitlist/',
                                {'email': 'blocked@example.com'}, content_type='application/json')
        self.assertEqual(resp.status_code, 404)

    @patch('rest_framework.throttling.ScopedRateThrottle.allow_request', side_effect=RuntimeError('cache down'))
    def test_json_signup_disabled_options_404s_without_throttle(self, _throttle):
        resp = self.client.options('/hive/api/v1/waitlist/')
        self.assertEqual(resp.status_code, 404)
        _throttle.assert_not_called()

    @patch('rest_framework.throttling.ScopedRateThrottle.allow_request', side_effect=RuntimeError('cache down'))
    def test_json_signup_disabled_post_404s_without_throttle(self, _throttle):
        resp = self.client.post('/hive/api/v1/waitlist/',
                                {'email': 'blocked@example.com'}, content_type='application/json')
        self.assertEqual(resp.status_code, 404)
        _throttle.assert_not_called()


@override_settings(HIVE_FREE_CLAW_WAITLIST_ENABLED=True)
class LandingTests(TestCase):
    def test_visit_renders_and_emits_event(self):
        resp = self.client.get('/hive/free-claw')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Join the waitlist', resp.content)
        self.assertEqual(WaitlistEvent.objects.filter(event_type='visit').count(), 1)

    def test_submit_creates_entry_and_submit_event(self):
        resp = self.client.post('/hive/free-claw', {
            'email': 'User@Example.com',
            'preferred_channel': 'whatsapp',
            'top_use_case': 'Customer support / inbox triage',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"on the list", resp.content)
        entry = WaitlistEntry.objects.get()
        self.assertEqual(entry.email, 'user@example.com')  # normalized
        self.assertEqual(entry.preferred_channel, Channel.WHATSAPP)
        self.assertTrue(WaitlistEvent.objects.filter(event_type='submit', entry=entry).exists())

    def test_resubmit_dedupes_by_email(self):
        for ch in ('web', 'app'):
            self.client.post('/hive/free-claw', {'email': 'dup@example.com', 'preferred_channel': ch})
        self.assertEqual(WaitlistEntry.objects.filter(email='dup@example.com').count(), 1)
        self.assertEqual(WaitlistEntry.objects.get(email='dup@example.com').preferred_channel, 'app')
        # both submits still recorded in the audit funnel
        self.assertEqual(WaitlistEvent.objects.filter(event_type='submit').count(), 2)

    def test_invalid_email_rejected(self):
        resp = self.client.post('/hive/free-claw', {'email': 'not-an-email'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'valid email', resp.content)
        self.assertEqual(WaitlistEntry.objects.count(), 0)

    def test_beacon_records_start_event(self):
        resp = self.client.post('/hive/api/v1/waitlist/event',
                                {'event_type': 'start'}, content_type='application/json')
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(WaitlistEvent.objects.filter(event_type='start').count(), 1)

    def test_beacon_rejects_unknown_event(self):
        resp = self.client.post('/hive/api/v1/waitlist/event',
                                {'event_type': 'submit'}, content_type='application/json')
        self.assertEqual(resp.status_code, 400)


class JsonSignupTests(TestCase):
    @override_settings(HIVE_FREE_CLAW_WAITLIST_ENABLED=True)
    def test_json_signup_creates_entry_and_submit_event(self):
        resp = self.client.post('/hive/api/v1/waitlist/', {
            'email': 'Lead@Example.com',
            'preferred_channel': 'whatsapp',
            'top_use_case': 'WhatsApp business assistant',
        }, content_type='application/json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['status'], 'joined')
        entry = WaitlistEntry.objects.get(email='lead@example.com')
        self.assertEqual(entry.preferred_channel, Channel.WHATSAPP)
        self.assertEqual(entry.top_use_case, 'WhatsApp business assistant')
        self.assertEqual(entry.source, 'hive_public_landing')
        event = WaitlistEvent.objects.get(event_type='submit', entry=entry)
        self.assertEqual(event.metadata['source'], 'hive_public_landing')
        self.assertTrue(event.metadata['created'])

    @override_settings(HIVE_FREE_CLAW_WAITLIST_ENABLED=True)
    def test_json_signup_is_idempotent_for_existing_email(self):
        first = self.client.post('/hive/api/v1/waitlist/', {
            'email': 'dup-json@example.com',
        }, content_type='application/json')
        second = self.client.post('/hive/api/v1/waitlist/', {
            'email': 'dup-json@example.com',
            'preferred_channel': 'app',
        }, content_type='application/json')
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['status'], 'already_joined')
        self.assertEqual(WaitlistEntry.objects.filter(email='dup-json@example.com').count(), 1)
        self.assertEqual(WaitlistEntry.objects.get(email='dup-json@example.com').preferred_channel, Channel.APP)
        self.assertEqual(WaitlistEvent.objects.filter(event_type='submit').count(), 2)

    @override_settings(HIVE_FREE_CLAW_WAITLIST_ENABLED=True)
    def test_json_signup_rejects_invalid_email(self):
        resp = self.client.post('/hive/api/v1/waitlist/', {
            'email': 'not-an-email',
        }, content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(WaitlistEntry.objects.count(), 0)
