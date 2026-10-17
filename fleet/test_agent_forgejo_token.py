"""A missing Origin token must not look like a working code-shipping seat.

PLAT-7406: ``ensure_agent_cortex_key`` create-if-missing persisted Secrets
with only CORTEX_API_KEY. FORGEJO_TOKEN refs are optional, so pods started
Ready with no Origin identity and every merge/write path froze.

Guards:
  1. missing FORGEJO_TOKEN is minted and patched (both aliases, no replace)
  2. an existing token is left alone (repair, not rotation)
  3. mint uses admin basic auth, never ``Authorization: token``
  4. the Origin admin login is never projected into a fleet pod
"""
import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from .models import FleetAgent
from . import provisioner


def _agent(**kw):
    defaults = dict(
        agent_username='ryo',
        agent_id='agent-ryo',
        display_name='Ryo',
        identity_user_id=35,
    )
    defaults.update(kw)
    return FleetAgent(**defaults)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class ForgejoIdentityTests(SimpleTestCase):
    def test_shizuha_fleet_seat_uses_shizuha_agent_not_admin(self):
        self.assertEqual(
            provisioner._forgejo_identity_username(_agent(agent_username='shizuha')),
            'shizuha-agent',
        )

    def test_ordinary_seat_keeps_its_username(self):
        self.assertEqual(provisioner._forgejo_identity_username(_agent()), 'ryo')


class ForgejoScopeTests(SimpleTestCase):
    """PLAT-10143 note: scope mapping is tested against _forgejo_grant_checked
    (the grant + known source); roster plumbing has its own seam."""

    def _grant(self, teams):
        return patch.object(
            provisioner, '_forgejo_grant_checked', return_value=(teams, True),
        )

    def test_write_team_gets_write_repository_scope(self):
        with self._grant({'merge'}):
            scopes = provisioner._forgejo_token_scopes(_agent())
        self.assertIn('write:repository', scopes)

    def test_review_team_gets_write_repository_scope(self):
        with self._grant({'review'}):
            scopes = provisioner._forgejo_token_scopes(_agent(agent_username='revi'))
        self.assertIn('write:repository', scopes)

    def test_reader_team_stays_read_scoped(self):
        with self._grant({'architecture'}):
            scopes = provisioner._forgejo_token_scopes(_agent(agent_username='kumo'))
        self.assertNotIn('write:repository', scopes)
        self.assertIn('read:repository', scopes)

    def test_unknown_grant_maps_to_read_not_write(self):
        """PLAT-10143: a failed roster fetch must never mint write scopes on
        a guess — unknown maps to read, and the mint path marks it."""
        with patch.object(
            provisioner, '_forgejo_grant_checked', return_value=(set(), False),
        ):
            scopes = provisioner._forgejo_token_scopes(_agent())
        self.assertNotIn('write:repository', scopes)


class MintUsesBasicAuthTests(SimpleTestCase):
    def test_mint_sends_basic_auth_not_a_bearer_token(self):
        resp = MagicMock(status_code=201)
        resp.json.return_value = {'sha1': 'minted-token-bytes', 'id': 9}
        with patch.object(provisioner, '_forgejo_admin_basic_auth', return_value=('shizuha', 'pw')), \
                patch.object(provisioner, '_hive_origin_api_url', return_value='http://origin'), \
                patch('fleet.forgejo_identity.ensure_origin_identity', return_value=True), \
                patch('requests.post', return_value=resp) as post:
            token = provisioner._mint_agent_forgejo_token(_agent(), provisioner._FORGEJO_WRITE_SCOPES)
        self.assertEqual(token, 'minted-token-bytes')
        self.assertEqual(post.call_args.kwargs['auth'], ('shizuha', 'pw'))
        self.assertNotIn('headers', post.call_args.kwargs)
        body = post.call_args.kwargs['json']
        self.assertEqual(body['scopes'], list(provisioner._FORGEJO_WRITE_SCOPES))
        self.assertTrue(str(post.call_args.args[0]).endswith('/api/v1/users/ryo/tokens'))

    def test_refuses_to_mint_the_admin_identity(self):
        with patch.object(provisioner, '_forgejo_identity_username', return_value='shizuha'), \
                patch('requests.post') as post:
            self.assertEqual(
                provisioner._mint_agent_forgejo_token(_agent(), provisioner._FORGEJO_READ_SCOPES),
                '',
            )
        post.assert_not_called()


class EnsureAgentForgejoTokenTests(SimpleTestCase):
    def setUp(self):
        self.core = MagicMock()
        # These fixtures exercise the pre-migration legacy grant. The purpose
        # Secret is independently absent, not an alias of this same object.
        from kubernetes.client.rest import ApiException
        def read_secret(name, namespace):
            if name.endswith('-forgejo-token'):
                raise ApiException(status=404)
            return self.core.read_namespaced_secret.return_value
        self.core.read_namespaced_secret.side_effect = read_secret
        patcher = patch.object(
            provisioner, 'get_k8s_client',
            return_value=(self.core, MagicMock(), MagicMock()),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = patch.object(provisioner, '_log_event')
        patcher2.start()
        self.addCleanup(patcher2.stop)
        patcher3 = patch.object(
            provisioner, '_agent_teams_from_roster', return_value={'merge'},
        )
        patcher3.start()
        self.addCleanup(patcher3.stop)

    def _secret(self, data):
        secret = MagicMock()
        secret.data = data
        return secret

    def test_existing_token_is_left_alone(self):
        self.core.read_namespaced_secret.return_value = self._secret(
            {'FORGEJO_TOKEN': _b64('already-there'), 'CORTEX_API_KEY': _b64('cx')},
        )
        with patch.object(provisioner, '_mint_agent_forgejo_token') as mint:
            fp = provisioner.ensure_agent_forgejo_token(_agent())
        self.assertTrue(fp)
        mint.assert_not_called()
        self.core.patch_namespaced_secret.assert_not_called()

    def test_fingerprint_is_stable_and_does_not_leak_the_token(self):
        encoded = _b64('already-there')
        self.core.read_namespaced_secret.return_value = self._secret(
            {'FORGEJO_TOKEN': encoded},
        )
        first = provisioner.ensure_agent_forgejo_token(_agent())
        second = provisioner.ensure_agent_forgejo_token(_agent())
        self.assertEqual(first, second)
        self.assertNotIn('already-there', first)
        self.assertEqual(len(first), 16)

    def test_missing_token_is_minted_and_patched_as_both_aliases(self):
        self.core.read_namespaced_secret.return_value = self._secret(
            {'CORTEX_API_KEY': _b64('cx')},
        )
        with patch.object(provisioner, '_mint_agent_forgejo_token', return_value='new-origin-token') as mint:
            fp = provisioner.ensure_agent_forgejo_token(_agent())
        self.assertTrue(fp)
        mint.assert_called_once()
        _name, _ns, body = self.core.patch_namespaced_secret.call_args[0]
        self.assertEqual(set(body['data']), {'FORGEJO_TOKEN', 'ORIGIN_TOKEN'})
        decoded = base64.b64decode(body['data']['FORGEJO_TOKEN']).decode()
        self.assertEqual(decoded, 'new-origin-token')
        self.assertEqual(body['data']['FORGEJO_TOKEN'], body['data']['ORIGIN_TOKEN'])

    def test_patch_does_not_clobber_cortex_or_password_keys(self):
        self.core.read_namespaced_secret.return_value = self._secret({
            'CORTEX_API_KEY': _b64('cx'),
            'AGENT_PASSWORD': _b64('pw'),
        })
        with patch.object(provisioner, '_mint_agent_forgejo_token', return_value='new-origin-token'):
            provisioner.ensure_agent_forgejo_token(_agent())
        _name, _ns, body = self.core.patch_namespaced_secret.call_args[0]
        self.assertEqual(set(body['data']), {'FORGEJO_TOKEN', 'ORIGIN_TOKEN'})

    def test_absent_secret_does_not_create_a_forgejo_only_secret(self):
        from kubernetes.client.rest import ApiException
        self.core.read_namespaced_secret.side_effect = ApiException(status=404)
        with patch.object(provisioner, '_mint_agent_forgejo_token') as mint:
            self.assertEqual(provisioner.ensure_agent_forgejo_token(_agent()), '')
        mint.assert_not_called()
        self.core.create_namespaced_secret.assert_not_called()
        self.core.patch_namespaced_secret.assert_not_called()

    def test_mint_failure_reports_no_token_rather_than_a_false_ok(self):
        self.core.read_namespaced_secret.return_value = self._secret({'CORTEX_API_KEY': _b64('cx')})
        with patch.object(provisioner, '_mint_agent_forgejo_token', return_value=''):
            self.assertEqual(provisioner.ensure_agent_forgejo_token(_agent()), '')
        self.core.patch_namespaced_secret.assert_not_called()

    def test_unreadable_secret_is_not_treated_as_missing(self):
        from kubernetes.client.rest import ApiException
        self.core.read_namespaced_secret.side_effect = ApiException(status=500)
        with patch.object(provisioner, '_mint_agent_forgejo_token') as mint:
            self.assertEqual(provisioner.ensure_agent_forgejo_token(_agent()), '')
        mint.assert_not_called()


class CortexKeyCreateDoesNotDropForgejoContractTests(SimpleTestCase):
    def test_reconciler_cronjob_also_mints_forgejo_tokens(self):
        text = (Path(__file__).resolve().parents[1] / 'k8s' / 'fleet-agent-cortex-key-reconciler.yaml').read_text()
        self.assertIn('reconcile_agent_cortex_keys --apply', text)
        self.assertIn('reconcile_agent_forgejo_tokens --apply', text)

    def test_ring_release_installs_forgejo_token_reconciler(self):
        text = (
            Path(__file__).resolve().parents[1]
            / '.forgejo' / 'scripts' / 'ring-release-fleet.sh'
        ).read_text()
        self.assertIn('hive-forgejo-token-reconciler', text)
        self.assertIn('reconcile_agent_forgejo_tokens', text)


class ScopeDriftGuardTests(SimpleTestCase):
    """PLAT-10143: a roster outage must never silently mint a writer seat
    read-scoped, and a drifted grant must be re-mintable at the sweep."""

    def setUp(self):
        self.core = MagicMock()
        patcher = patch.object(
            provisioner, 'get_k8s_client',
            return_value=(self.core, MagicMock(), MagicMock()),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('_log_event', '_agent_teams_from_roster'):
            p = patch.object(provisioner, name, return_value=None if name == '_log_event' else set())
            p.start()
            self.addCleanup(p.stop)

    def _purpose_secret(self, token_b64, annotations=None):
        secret = MagicMock()
        secret.data = {'FORGEJO_TOKEN': token_b64}
        secret.metadata.annotations = annotations or {}
        return secret

    def test_roster_failure_mints_read_loud_and_marked(self):
        legacy = MagicMock()
        legacy.data = {'CORTEX_API_KEY': _b64('cx')}
        with patch.object(provisioner, '_forgejo_secret',
                          return_value=('ryo-agent-creds', legacy)), \
                patch.object(provisioner, '_forgejo_grant_checked',
                             return_value=(set(), False)), \
                patch.object(provisioner, '_mint_agent_forgejo_token',
                             return_value='provisional-token') as mint:
            fp = provisioner.ensure_agent_forgejo_token(_agent())
        self.assertTrue(fp)
        self.assertEqual(mint.call_args[0][1], provisioner._FORGEJO_READ_SCOPES)
        _name, _ns, body = self.core.patch_namespaced_secret.call_args[0]
        self.assertEqual(
            body['metadata']['annotations']['shizuha.io/scope-provisional'],
            'roster-unavailable',
        )
        self.assertEqual(
            body['metadata']['annotations']['shizuha.io/token-scopes'],
            ','.join(provisioner._FORGEJO_READ_SCOPES),
        )
        events = [c.args for c in provisioner._log_event.call_args_list]
        self.assertTrue(any(
            len(a) > 1 and a[1] == 'forgejo_token_scope_provisional' for a in events
        ), f'expected provisional mint event, got {events}')

    def test_known_writer_grant_stamps_write_scopes_without_provisional(self):
        legacy = MagicMock()
        legacy.data = {'CORTEX_API_KEY': _b64('cx')}
        with patch.object(provisioner, '_forgejo_secret',
                          return_value=('ryo-agent-creds', legacy)), \
                patch.object(provisioner, '_forgejo_grant_checked',
                             return_value=({'merge'}, True)), \
                patch.object(provisioner, '_mint_agent_forgejo_token',
                             return_value='write-token') as mint:
            provisioner.ensure_agent_forgejo_token(_agent())
        self.assertEqual(mint.call_args[0][1], provisioner._FORGEJO_WRITE_SCOPES)
        _name, _ns, body = self.core.patch_namespaced_secret.call_args[0]
        annotations = body['metadata']['annotations']
        self.assertEqual(
            annotations['shizuha.io/token-scopes'],
            ','.join(provisioner._FORGEJO_WRITE_SCOPES),
        )
        self.assertNotIn('shizuha.io/scope-provisional', annotations)

    def test_rescope_overwrites_drifted_purpose_secret(self):
        purpose = self._purpose_secret(_b64('drifted-read-token'))
        with patch.object(provisioner, '_forgejo_secret',
                          return_value=('ryo-forgejo-token', purpose)), \
                patch.object(provisioner, '_forgejo_grant_checked',
                             return_value=({'merge'}, True)), \
                patch.object(provisioner, '_mint_agent_forgejo_token',
                             return_value='fresh-write-token') as mint:
            fp = provisioner.ensure_agent_forgejo_token(
                _agent(), rescope_to=provisioner._FORGEJO_WRITE_SCOPES,
            )
        self.assertTrue(fp)
        self.assertEqual(mint.call_args[0][1], provisioner._FORGEJO_WRITE_SCOPES)
        _name, _ns, body = self.core.patch_namespaced_secret.call_args[0]
        decoded = base64.b64decode(body['data']['FORGEJO_TOKEN']).decode()
        self.assertEqual(decoded, 'fresh-write-token')
        self.assertEqual(
            body['metadata']['annotations']['shizuha.io/token-scopes'],
            ','.join(provisioner._FORGEJO_WRITE_SCOPES),
        )

    def test_no_rescope_still_leaves_existing_token_alone(self):
        purpose = self._purpose_secret(_b64('drifted-read-token'))
        with patch.object(provisioner, '_forgejo_secret',
                          return_value=('ryo-forgejo-token', purpose)), \
                patch.object(provisioner, '_mint_agent_forgejo_token') as mint:
            fp = provisioner.ensure_agent_forgejo_token(_agent())
        mint.assert_not_called()
        self.core.patch_namespaced_secret.assert_not_called()
        self.assertTrue(fp)


class RescopeDriftSweepTests(SimpleTestCase):
    """The --rescope-drift sweep: drifted -> re-mint; unknown -> loud skip;
    unstamped -> loud residue report; healthy -> untouched."""

    def _run_sweep(self, seats, apply):
        import io
        from unittest.mock import MagicMock, patch as mp
        from fleet.management.commands.reconcile_agent_forgejo_tokens import Command
        cmd = Command()
        cmd.stdout = io.StringIO()
        agents = [MagicMock(agent_username=name) for name, _ in seats]
        secrets = {
            name: MagicMock(
                data={'FORGEJO_TOKEN': _b64('tok')},
                metadata=MagicMock(annotations=anns),
            )
            for name, (_, anns) in seats
        }
        import fleet.management.commands.reconcile_agent_forgejo_tokens as recon_mod
        with mp.object(provisioner, '_forgejo_grant_checked',
                       side_effect=lambda a: next(g for n, (g, _) in seats if n == a.agent_username)), \
                mp.object(provisioner, '_forgejo_secret',
                          side_effect=lambda a, c, ns: (f'{a.agent_username}-forgejo-token', secrets[a.agent_username])), \
                mp.object(provisioner, '_forgejo_token_scopes',
                          side_effect=lambda a: provisioner._FORGEJO_READ_SCOPES if a.agent_username == 'kumo' else provisioner._FORGEJO_WRITE_SCOPES), \
                mp.object(recon_mod, 'ensure_agent_forgejo_token',
                          return_value='fp') as ensure:
            cmd._handle_rescope_drift(MagicMock(), agents, apply)
        return cmd.stdout.getvalue(), ensure

    def test_drifted_seat_is_re_minted_only_on_apply(self):
        seats = [('hiro', (({'merge'}, True), {'shizuha.io/token-scopes': 'read:repository'}))]
        out, ensure = self._run_sweep(seats, apply=False)
        self.assertIn('hiro', out)
        self.assertIn('scope drift (re-mint due): 1', out)
        ensure.assert_not_called()
        out, ensure = self._run_sweep(seats, apply=True)
        ensure.assert_called_once()
        self.assertEqual(
            ensure.call_args.kwargs['rescope_to'],
            provisioner._FORGEJO_WRITE_SCOPES,
        )

    def test_healthy_and_unstamped_and_unknown_are_not_touched(self):
        seats = [
            ('kumo', ((set(), True), {'shizuha.io/token-scopes': ','.join(provisioner._FORGEJO_READ_SCOPES)})),
            ('jun', (({'merge'}, True), {})),  # unstamped residue
            ('sato', ((set(), False), {'shizuha.io/token-scopes': 'read:repository'})),  # unknown grant
        ]
        out, ensure = self._run_sweep(seats, apply=True)
        ensure.assert_not_called()
        self.assertIn('jun', out)
        self.assertIn('hiro-class residue', out)
        self.assertIn('NOT acted on', out)
