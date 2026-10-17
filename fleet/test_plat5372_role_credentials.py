"""Role-scoped credential projection (PLAT-5372) must survive reprovision.

PLAT-5371 delivered the accounting-portal credentials to the accounting seat by
projecting the material into ``banto-agent-creds`` — a per-seat Secret the
renderer owns. That survives routine re-renders (3-way merge preserves keys the
renderer does not name) but NOT a deprovision/reprovision: the provisioner
deletes the seat Secret on teardown and the renderer recreates it with only
AGENT_PASSWORD/GITHUB_TOKEN/CONTEXT_PROMPT. Nothing re-injects the projection.

These tests pin the reconciler that closes the gap: ``ensure_agent_role_credentials``
patches the scope's keys from a single shared fleet-namespace Secret into the
seat's ``<user>-agent-creds`` — team-resolved, least-privilege, patch-not-replace.
"""
import base64
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from .models import FleetAgent
from . import provisioner


def _agent(**kw):
    defaults = dict(
        agent_username='banto',
        agent_id='agent-banto',
        display_name='Banto',
        identity_user_id=4242,
    )
    defaults.update(kw)
    return FleetAgent(**defaults)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class EnsureAgentRoleCredentialsTests(TestCase):
    def setUp(self):
        self.core = MagicMock()
        patcher = patch.object(
            provisioner, 'get_k8s_client',
            return_value=(self.core, MagicMock(), MagicMock()),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = patch.object(provisioner, '_log_event')
        patcher2.start()
        self.addCleanup(patcher2.stop)
        # Default: the agent belongs to the accounting team. Pulse roster team
        # keys are org-qualified with the '::' separator (TEAM_SCOPE_SEPARATOR);
        # team_slug_from_key strips the org qualifier to the unqualified slug.
        self.email_to_teams = {'banto@shizuha.com': {'shizuha::accounting'}}

    def _shared(self, data):
        secret = MagicMock()
        secret.data = data
        return secret

    def test_accounting_seat_gets_the_scope_projected(self):
        self.core.read_namespaced_secret.return_value = self._shared({
            'accounting-portal-catalog.json': _b64('{"catalog":1}'),
            'accounting-portal-flat.json': _b64('{"flat":1}'),
            'accounting-portal-index.json': _b64('{"index":1}'),
        })
        projected = provisioner.ensure_agent_role_credentials(
            _agent(), email_to_teams=self.email_to_teams,
        )
        self.assertEqual(projected, ['accounting-portal'])
        self.core.patch_namespaced_secret.assert_called_once()
        _name, _ns, body = self.core.patch_namespaced_secret.call_args[0]
        self.assertEqual(
            set(body['data']),
            {'accounting-portal-catalog.json', 'accounting-portal-flat.json', 'accounting-portal-index.json'},
        )

    def test_non_accounting_seat_gets_nothing(self):
        self.core.read_namespaced_secret.return_value = self._shared({
            'accounting-portal-catalog.json': _b64('{"catalog":1}'),
        })
        projected = provisioner.ensure_agent_role_credentials(
            _agent(), email_to_teams={'nami@shizuha.com': {'shizuha::engineering'}},
        )
        self.assertEqual(projected, [])
        self.core.patch_namespaced_secret.assert_not_called()

    def test_patch_never_clobbers_other_credentials(self):
        """A full-object replace would drop AGENT_PASSWORD/FORGEJO_TOKEN."""
        self.core.read_namespaced_secret.return_value = self._shared({
            'accounting-portal-catalog.json': _b64('{"catalog":1}'),
            'accounting-portal-flat.json': _b64('{"flat":1}'),
            'accounting-portal-index.json': _b64('{"index":1}'),
        })
        provisioner.ensure_agent_role_credentials(_agent(), email_to_teams=self.email_to_teams)
        _name, _ns, body = self.core.patch_namespaced_secret.call_args[0]
        self.assertTrue(
            all(k.startswith('accounting-portal-') for k in body['data']),
            'patch only the scope keys; anything else risks deleting a credential '
            'this reconciler does not own',
        )

    def test_missing_shared_secret_defers_without_crashing(self):
        from kubernetes.client.rest import ApiException
        self.core.read_namespaced_secret.side_effect = ApiException(status=404)
        projected = provisioner.ensure_agent_role_credentials(
            _agent(), email_to_teams=self.email_to_teams,
        )
        self.assertEqual(projected, [])
        self.core.patch_namespaced_secret.assert_not_called()

    def test_keys_absent_from_shared_secret_are_skipped_not_fabricated(self):
        """Partial projection: only keys the shared Secret actually carries are
        copied; absent keys are skipped, never fabricated."""
        self.core.read_namespaced_secret.return_value = self._shared({
            'accounting-portal-catalog.json': _b64('{"catalog":1}'),
        })
        projected = provisioner.ensure_agent_role_credentials(
            _agent(), email_to_teams=self.email_to_teams,
        )
        self.assertEqual(projected, ['accounting-portal'])
        self.core.patch_namespaced_secret.assert_called_once()
        _name, _ns, body = self.core.patch_namespaced_secret.call_args[0]
        self.assertEqual(
            set(body['data']), {'accounting-portal-catalog.json'},
            'only present keys are projected; absent keys must not be fabricated',
        )

    def test_shared_secret_with_none_of_the_keys_defers(self):
        """When the shared Secret carries none of the scope's keys, nothing is
        projected (no fabricated values) and the seat is left untouched."""
        self.core.read_namespaced_secret.return_value = self._shared({
            'unrelated-key.json': _b64('{"x":1}'),
        })
        projected = provisioner.ensure_agent_role_credentials(
            _agent(), email_to_teams=self.email_to_teams,
        )
        self.assertEqual(projected, [])
        self.core.patch_namespaced_secret.assert_not_called()

    def test_unknown_team_never_matches_a_scope(self):
        self.core.read_namespaced_secret.return_value = self._shared({
            'accounting-portal-catalog.json': _b64('{"catalog":1}'),
        })
        projected = provisioner.ensure_agent_role_credentials(
            _agent(), email_to_teams={},
        )
        self.assertEqual(projected, [])


class RoleCredentialScopeConfigTests(SimpleTestCase):
    def test_accounting_scope_is_team_resolved_not_per_agent(self):
        """The grant must come from the team, never a hand-maintained list."""
        spec = provisioner.ROLE_CREDENTIAL_SCOPES['accounting-portal']
        self.assertIn('accounting', spec['teams'])
        self.assertEqual(spec['shared_secret'], 'accounting-portal-credentials')
        self.assertIn('accounting-portal-catalog.json', spec['keys'])
        self.assertIn('accounting-portal-flat.json', spec['keys'])
        self.assertIn('accounting-portal-index.json', spec['keys'])
