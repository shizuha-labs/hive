"""KURA-160: wire the kura api-client seat label via a settings allowlist.

The kura-server-api-ingress netpol admits programmatic API clients ONLY via
the pod label ``kura.shizuha.io/api-client="true"`` — the designed path was
never wired to the seat template, so every seat's KURA-4 daily assurance
probe was netpol-refused for ~20d while kura-server itself stayed healthy
(pod 1/1 Running 0 restarts, endpoint populated, live 200s to
kubelet/blackbox/heartbeats). Membership is a Hive data decision (settings
allowlist), never a hand-applied kubectl label — same doctrine as
devops-ssh. Seat-side self-labeling is RBAC-blocked (PLAT-9018).
"""
from django.test import TestCase, override_settings

from fleet.provisioner import _base_labels
from fleet.test_legacy import _agent


class KuraApiClientLabelTests(TestCase):
    def _labels_for(self, username):
        return _base_labels(_agent(agent_username=username))

    @override_settings(FLEET_KURA_API_CLIENT_USERNAMES='san,zen')
    def test_allowlisted_seat_carries_the_label(self):
        self.assertEqual(
            self._labels_for('san').get('kura.shizuha.io/api-client'), 'true')
        self.assertEqual(
            self._labels_for('ZEN').get('kura.shizuha.io/api-client'), 'true')

    @override_settings(FLEET_KURA_API_CLIENT_USERNAMES='san,zen')
    def test_non_allowlisted_seat_does_not(self):
        self.assertNotIn('kura.shizuha.io/api-client', self._labels_for('mio'))

    def test_default_off_without_setting(self):
        self.assertNotIn('kura.shizuha.io/api-client', self._labels_for('san'))

    @override_settings(FLEET_KURA_API_CLIENT_USERNAMES='  , san ,')
    def test_blank_entries_are_ignored(self):
        self.assertEqual(
            self._labels_for('san').get('kura.shizuha.io/api-client'), 'true')
        self.assertNotIn('kura.shizuha.io/api-client', self._labels_for('zen'))
