"""An agent without its own Cortex key must never look healthy.

Operator 2026-08-05, on rei showing "Runtime unhealthy" with no detail:

    Re-mint rei's CORTEX_API_KEY -> this should be automatic. hive manages its
    agents underneath .. and each agent uses their own key .. so you shouldn't
    have to do it .. let hive/fleet daemon/agent handle this .. and yea sweep
    the class

rei was provisioned 2026-06-27, before per-agent Cortex keys existed. Its
``rei-agent-creds`` Secret carried AGENT_PASSWORD, CONTEXT_PROMPT, FORGEJO_TOKEN,
GITHUB_TOKEN, ORIGIN_TOKEN — and no CORTEX_API_KEY. Because the container
referenced that key with ``optional=True``, Kubernetes resolved it to an EMPTY
STRING rather than refusing the Pod. The agent therefore ran for five weeks:
Running, Ready, Alive, heartbeating every 900s, holding a model lease, and
returning ``vLLM error 401`` on every turn.

Two defects, two guards below:
  1. the credential was not guaranteed to exist  -> ensure_agent_cortex_key mints
  2. its absence was not an error                -> the secretKeyRef is mandatory
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from .models import FleetAgent
from . import provisioner


def _agent(**kw):
    defaults = dict(
        agent_username='testagent',
        agent_id='agent-test',
        display_name='Test',
        identity_user_id=4242,
    )
    defaults.update(kw)
    return FleetAgent(**defaults)


class MandatoryCortexKeyRefTests(SimpleTestCase):
    """The rendered container must FAIL to start without the key, not run blind."""

    def test_cortex_key_env_is_not_optional(self):
        container = provisioner._agent_container(_agent())
        refs = [
            e.value_from.secret_key_ref
            for e in (container.env or [])
            if e.name in ('CORTEX_API_KEY', 'OPENAI_API_KEY', 'VLLM_API_KEY')
            and getattr(e, 'value_from', None) is not None
            and getattr(e.value_from, 'secret_key_ref', None) is not None
        ]
        self.assertEqual(len(refs), 3, 'all three Cortex aliases must be secret-backed')
        for ref in refs:
            self.assertEqual(ref.key, 'CORTEX_API_KEY')
            self.assertFalse(
                bool(ref.optional),
                'optional=True turns a missing credential into an empty string; '
                'the agent then runs indefinitely and 401s on every turn (rei, '
                '5 weeks). A Pod that refuses to start names the problem.',
            )


class EnsureAgentCortexKeyTests(TestCase):
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

    def _secret(self, data):
        secret = MagicMock()
        secret.data = data
        return secret

    def test_existing_key_is_left_alone(self):
        import base64
        self.core.read_namespaced_secret.return_value = self._secret(
            {'CORTEX_API_KEY': base64.b64encode(b'sk-cortex-live').decode()},
        )
        with patch.object(provisioner, '_mint_agent_cortex_key') as mint:
            fp = provisioner.ensure_agent_cortex_key(_agent())
        self.assertTrue(fp)
        mint.assert_not_called()
        self.core.patch_namespaced_secret.assert_not_called()

    def test_fingerprint_is_stable_so_reconciles_do_not_churn_pods(self):
        """The annotation must not change while the key does not."""
        import base64
        self.core.read_namespaced_secret.return_value = self._secret(
            {'CORTEX_API_KEY': base64.b64encode(b'sk-cortex-live').decode()},
        )
        first = provisioner.ensure_agent_cortex_key(_agent())
        second = provisioner.ensure_agent_cortex_key(_agent())
        self.assertEqual(first, second)

    def test_fingerprint_never_leaks_the_key(self):
        import base64
        self.core.read_namespaced_secret.return_value = self._secret(
            {'CORTEX_API_KEY': base64.b64encode(b'sk-cortex-supersecret').decode()},
        )
        fp = provisioner.ensure_agent_cortex_key(_agent())
        self.assertNotIn('sk-cortex', fp)
        self.assertNotIn('supersecret', fp)

    def test_missing_key_is_minted_and_patched_in(self):
        self.core.read_namespaced_secret.return_value = self._secret({'AGENT_PASSWORD': 'eA=='})
        with patch.object(provisioner, '_mint_agent_cortex_key', return_value='sk-cortex-new'):
            fp = provisioner.ensure_agent_cortex_key(_agent())
        self.assertTrue(fp)
        self.core.patch_namespaced_secret.assert_called_once()
        _name, _ns, body = self.core.patch_namespaced_secret.call_args[0]
        self.assertIn('CORTEX_API_KEY', body['data'])

    def test_patch_does_not_clobber_the_other_credentials(self):
        """A full-object replace would drop AGENT_PASSWORD and strand the broker."""
        self.core.read_namespaced_secret.return_value = self._secret({
            'AGENT_PASSWORD': 'eA==', 'FORGEJO_TOKEN': 'eQ==', 'CONTEXT_PROMPT': 'ej==',
        })
        with patch.object(provisioner, '_mint_agent_cortex_key', return_value='sk-cortex-new'):
            provisioner.ensure_agent_cortex_key(_agent())
        _name, _ns, body = self.core.patch_namespaced_secret.call_args[0]
        self.assertEqual(
            set(body['data']), {'CORTEX_API_KEY'},
            'patch only our own key; anything else risks deleting a credential '
            'this reconciler does not own',
        )

    def test_absent_secret_is_created(self):
        from kubernetes.client.rest import ApiException
        self.core.read_namespaced_secret.side_effect = ApiException(status=404)
        with patch.object(provisioner, '_mint_agent_cortex_key', return_value='sk-cortex-new'):
            fp = provisioner.ensure_agent_cortex_key(_agent())
        self.assertTrue(fp)
        self.core.create_namespaced_secret.assert_called_once()

    def test_mint_failure_reports_no_key_rather_than_a_false_ok(self):
        self.core.read_namespaced_secret.return_value = self._secret({})
        with patch.object(provisioner, '_mint_agent_cortex_key', return_value=''):
            self.assertEqual(provisioner.ensure_agent_cortex_key(_agent()), '')
        self.core.patch_namespaced_secret.assert_not_called()

    def test_unreadable_secret_is_not_treated_as_missing(self):
        """A 403/500 must not trigger a mint — that would churn keys on an outage."""
        from kubernetes.client.rest import ApiException
        self.core.read_namespaced_secret.side_effect = ApiException(status=500)
        with patch.object(provisioner, '_mint_agent_cortex_key') as mint:
            self.assertEqual(provisioner.ensure_agent_cortex_key(_agent()), '')
        mint.assert_not_called()


class MintAttributionTests(SimpleTestCase):
    def test_mint_is_attributed_to_the_agents_own_identity(self):
        agent = _agent(identity_user_id=4242)
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'key': 'sk-cortex-minted'}
        with patch.object(provisioner, 'settings') as st, \
                patch('requests.post', return_value=resp) as post:
            st.HIVE_CORTEX_LEASE_API_KEY = 'sk-trusted-service'
            st.CORTEX_API_KEY = 'sk-customer-tier'
            st.CORTEX_BASE_URL = 'http://cortex:8040'
            key = provisioner._mint_agent_cortex_key(agent)
        self.assertEqual(key, 'sk-cortex-minted')
        body = post.call_args.kwargs['json']
        self.assertEqual(body['user_id'], 4242)
        self.assertEqual(body['name'], 'agent-testagent')
        # Matches every existing per-agent key (agent-nova, agent-rui, ...).
        # A STANDARD-tier key would be metered as resold-customer traffic.
        self.assertEqual(body['service_tier'], 'internal')
        self.assertEqual(body['traffic_class'], 'own_fleet')
        # No personal owner on this fixture → no Hane payer (platform self-use).
        self.assertEqual(body['billing_user_id'], 0)

    def test_mint_bills_the_human_owner_not_the_agent_identity(self):
        agent = _agent(identity_user_id=740, owner_id=3)
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'key': 'sk-cortex-minted'}
        with patch.object(provisioner, 'settings') as st, \
                patch('requests.post', return_value=resp) as post:
            st.HIVE_CORTEX_LEASE_API_KEY = 'sk-trusted-service'
            st.CORTEX_API_KEY = 'sk-customer-tier'
            st.CORTEX_BASE_URL = 'http://cortex:8040'
            provisioner._mint_agent_cortex_key(agent)
        body = post.call_args.kwargs['json']
        self.assertEqual(body['user_id'], 740)
        self.assertEqual(body['billing_user_id'], 3)

    def test_mint_presents_the_trusted_service_credential_not_the_inference_key(self):
        """Cortex grants trusted-service identity ONLY to a token equal to its own
        CORTEX_API_KEY. Hive's CORTEX_API_KEY is a different, customer-tier key:
        sending it returns 403 'Only a Cortex-trusted service credential may mint
        elevated-tier keys', which the caller swallows as a warning. That is
        precisely how this whole class stayed invisible — the mint automation
        existed and had never once succeeded."""
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'key': 'sk-cortex-minted'}
        with patch.object(provisioner, 'settings') as st, \
                patch('requests.post', return_value=resp) as post:
            st.HIVE_CORTEX_LEASE_API_KEY = 'sk-trusted-service'
            st.CORTEX_API_KEY = 'sk-customer-tier'
            st.CORTEX_BASE_URL = 'http://cortex:8040'
            provisioner._mint_agent_cortex_key(_agent())
        auth = post.call_args.kwargs['headers']['Authorization']
        self.assertEqual(auth, 'Bearer sk-trusted-service')
        self.assertNotIn('sk-customer-tier', auth)

    def test_no_identity_means_no_key_rather_than_a_misattributed_one(self):
        agent = _agent(identity_user_id=None)
        with patch.object(provisioner, 'settings') as st, patch('requests.post') as post:
            st.HIVE_CORTEX_LEASE_API_KEY = 'sk-trusted-service'
            st.CORTEX_BASE_URL = 'http://cortex:8040'
            self.assertEqual(provisioner._mint_agent_cortex_key(agent), '')
        post.assert_not_called()

    def test_non_2xx_is_not_read_as_a_key(self):
        agent = _agent()
        resp = MagicMock(status_code=403, text='forbidden')
        with patch.object(provisioner, 'settings') as st, patch('requests.post', return_value=resp):
            st.HIVE_CORTEX_LEASE_API_KEY = 'sk-trusted-service'
            st.CORTEX_BASE_URL = 'http://cortex:8040'
            self.assertEqual(provisioner._mint_agent_cortex_key(agent), '')

    def test_network_failure_is_swallowed_not_raised_into_the_reconciler(self):
        import requests
        agent = _agent()
        with patch.object(provisioner, 'settings') as st, \
                patch('requests.post', side_effect=requests.RequestException('boom')):
            st.HIVE_CORTEX_LEASE_API_KEY = 'sk-trusted-service'
            st.CORTEX_BASE_URL = 'http://cortex:8040'
            self.assertEqual(provisioner._mint_agent_cortex_key(agent), '')


class IdentityResolutionTests(TestCase):
    """Most of the fleet predates `identity_user_id`; refusing there strands it.

    The first live run of the reconciler minted 1 of 18 keys — the other 17 all
    failed with "no identity_user_id". That field is only populated for agents
    provisioned through the identity flow, so a strict refusal would have left
    every pre-existing agent permanently uncredentialed, which is the exact
    situation this whole change exists to end.
    """

    def setUp(self):
        self.core = MagicMock()
        p1 = patch.object(provisioner, 'get_k8s_client',
                          return_value=(self.core, MagicMock(), MagicMock()))
        p1.start(); self.addCleanup(p1.stop)

    def test_a_missing_identity_is_resolved_by_username(self):
        agent = _agent(identity_user_id=None)
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'key': 'sk-cortex-minted'}
        with patch.object(provisioner, 'settings') as st, \
                patch('fleet.personal_agents.resolve_agent_user_id', return_value=77), \
                patch('requests.post', return_value=resp) as post:
            st.HIVE_CORTEX_LEASE_API_KEY = 'sk-trusted-service'
            st.CORTEX_BASE_URL = 'http://cortex:8040'
            key = provisioner._mint_agent_cortex_key(agent)
        self.assertEqual(key, 'sk-cortex-minted')
        self.assertEqual(post.call_args.kwargs['json']['user_id'], 77)

    def test_an_unresolvable_identity_still_refuses_to_mint(self):
        """agent-nami already exists in Cortex with user_id=0 from guessing."""
        agent = _agent(identity_user_id=None)
        with patch.object(provisioner, 'settings') as st, \
                patch('fleet.personal_agents.resolve_agent_user_id', return_value=None), \
                patch('requests.post') as post:
            st.HIVE_CORTEX_LEASE_API_KEY = 'sk-trusted-service'
            st.CORTEX_BASE_URL = 'http://cortex:8040'
            self.assertEqual(provisioner._mint_agent_cortex_key(agent), '')
        post.assert_not_called()

    def test_a_failing_identity_lookup_does_not_break_the_reconcile(self):
        agent = _agent(identity_user_id=None)
        with patch.object(provisioner, 'settings') as st, \
                patch('fleet.personal_agents.resolve_agent_user_id',
                      side_effect=RuntimeError('id service down')), \
                patch('requests.post') as post:
            st.HIVE_CORTEX_LEASE_API_KEY = 'sk-trusted-service'
            st.CORTEX_BASE_URL = 'http://cortex:8040'
            self.assertEqual(provisioner._mint_agent_cortex_key(agent), '')
        post.assert_not_called()


class MissingNamespaceIsSkippedTests(TestCase):
    """`fleet-shizuha-128` does not exist — an agent with no namespace has no pod."""

    def test_no_namespace_anywhere_returns_empty_without_minting(self):
        from kubernetes.client.rest import ApiException
        core = MagicMock()
        core.read_namespaced_secret.side_effect = ApiException(status=404)
        core.create_namespaced_secret.side_effect = ApiException(
            status=404, reason='Not Found',
        )
        core.create_namespaced_secret.side_effect.body = (
            '{"message":"namespaces \\"fleet-shizuha-128\\" not found"}'
        )
        with patch.object(provisioner, 'get_k8s_client',
                          return_value=(core, MagicMock(), MagicMock())), \
                patch.object(provisioner, '_mint_agent_cortex_key', return_value='sk-new'), \
                patch.object(provisioner, '_log_event'):
            self.assertEqual(provisioner.ensure_agent_cortex_key(_agent()), '')
