"""PLAT-8597 leg 1 — link-only seat provisioning.

The PLAT-8592 forensics pinned the 403 wedge as a provisioning gap: local
seats poll the daemon gate with ``os.hostname()`` as the lookup key, but only
``rt-fleet-s1`` ever existed. ``provision_user_daemon --link-only
--daemon-id <hostname>`` mints exactly the DaemonCredential + link Secret a
local seat needs — and must NOT touch the per-user daemon legs (agent
namespace, RBAC, netpol, roster seed, Deployment).
"""
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from fleet.management.commands.provision_user_daemon import (
    _valid_seat_key,
)
from fleet.models import DaemonCredential


class ValidSeatKeyTests(TestCase):
    def test_dns_safe_keys_pass_with_case_preserved(self):
        self.assertTrue(_valid_seat_key('agent-mio-75cb96c575-gj52t'))
        self.assertTrue(_valid_seat_key('Mio-Laptop.local'))
        self.assertTrue(_valid_seat_key('a' * 253))

    def test_empty_too_long_or_shell_key_fails_closed(self):
        self.assertFalse(_valid_seat_key(''))
        self.assertFalse(_valid_seat_key('a' * 254))
        self.assertFalse(_valid_seat_key('bad key;rm -rf'))
        self.assertFalse(_valid_seat_key('bad/key'))


class LinkOnlySeatProvisioningTests(TestCase):
    seat_hostname = 'mio-laptop.local'

    def _apply(self, *extra, core=None):
        if core is None:
            core = mock.MagicMock()
        with (
            mock.patch(
                'fleet.provisioner.get_k8s_client',
                return_value=(core, mock.MagicMock(), mock.MagicMock()),
            ),
            mock.patch(
                'fleet.runtime_release.get_authoritative_runtime_release',
            ) as release,
        ):
            release.return_value.release.deployment_image = 'img:reviewed'
            call_command(
                'provision_user_daemon',
                '--user-id', '42',
                '--daemon-id', self.seat_hostname,
                '--link-only',
                *extra,
            )
        return core

    def test_dry_run_prints_plan_and_never_writes(self):
        out = self._apply()
        self.assertEqual(DaemonCredential.objects.count(), 0)
        out.create_namespaced_secret.assert_not_called()

    def test_apply_mints_overridden_credential_and_secret_only(self):
        out = self._apply('--apply')

        row = DaemonCredential.objects.get(daemon_id=self.seat_hostname)
        self.assertFalse(row.revoked)
        secret = out.create_namespaced_secret.call_args.args[1]
        self.assertEqual(secret.metadata.namespace, 'rt-fleet')
        self.assertIn('token', secret.data)

        # Idempotent re-run without --rotate-token reuses the row and re-applies
        # the SAME token (create-or-replace, identical content) — the same
        # idempotent pattern as the full provisioning path's step 3.
        captured = out.create_namespaced_secret.call_args.args[1].data['token']
        out2 = mock.MagicMock()
        out2.read_namespaced_secret.return_value = mock.MagicMock(
            data={'token': captured},
        )
        self._apply('--apply', core=out2)
        self.assertEqual(DaemonCredential.objects.count(), 1)
        reapplied = out2.create_namespaced_secret.call_args.args[1]
        self.assertEqual(reapplied.data['token'], captured)

    def test_apply_never_touches_per_user_daemon_legs(self):
        out = self._apply('--apply')
        out.create_namespace.assert_not_called()
        out.create_namespaced_role.assert_not_called()
        out.create_namespaced_network_policy.assert_not_called()
        out.create_namespaced_deployment.assert_not_called()

    def test_invalid_seat_key_fails_before_any_mutation(self):
        with self.assertRaises(CommandError):
            call_command(
                'provision_user_daemon',
                '--user-id', '42',
                '--daemon-id', 'bad key;rm -rf',
                '--link-only', '--apply',
            )
        self.assertEqual(DaemonCredential.objects.count(), 0)
