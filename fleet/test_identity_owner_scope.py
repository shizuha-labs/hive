"""An ordinary fleet agent is not a personal-default identity.

Live, 2026-08-05: rei was disabled to pick up a freshly minted Cortex key and
then could not be enabled again:

    PLAT-4884: confirmed credential drift for rei gen=72036087 — one reconcile
    HIVE-75: scoped fleet provision failed for rei (400):
      {'error': 'Personal-default identity must use its canonical owner-derived
                 username/email'}
    PLAT-4884 DRIFT_HOLD agent=rei reason=reconcile_failed
    Internal Server Error: /api/v1/fleet/agents/<id>/enable/

`owner_user_id` is shizuha-id's declaration that a request provisions THE
PERSONAL-DEFAULT identity for a human, and `identity/internal_urls.py` enforces
the matching contract: username and email must be the canonical
`shizuha-<owner_id>` pair. Hive sent it for every agent that merely HAD an
owner, so any ordinary agent's identity re-provision was rejected — turning a
recoverable credential drift into a permanent, unenable-able DRIFT_HOLD.

Measured on the live fleet: 45 of 58 agents carry an owner_id, and only THREE
(shizuha-1, shizuha-116, shizuha-128) are genuinely personal-default. The other
42 were one credential drift away from being unstartable.
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from .models import FleetAgent
from . import provisioner


def _agent(username, owner_id):
    return FleetAgent(
        agent_username=username,
        agent_id=f'agent-{username}',
        display_name=username.title(),
        owner_id=owner_id,
    )


def _provision(agent):
    """Call the identity provisioner and return the JSON body it posted."""
    resp = MagicMock(ok=True)
    resp.json.return_value = {'user_id': 1, 'profile_id': 2}
    with patch.object(provisioner, '_fleet_provisioner_token', return_value='tok'), \
            patch.object(provisioner, 'settings') as st, \
            patch('requests.post', return_value=resp) as post:
        st.SHIZUHA_ID_INTERNAL_URL = 'http://id:8000'
        provisioner._provision_platform_identity_details(agent, 'a-password-value', 'http://platform')
    return post.call_args.kwargs['json']


class OrdinaryAgentsDoNotClaimPersonalDefaultTests(SimpleTestCase):
    def test_an_ordinary_agent_omits_owner_user_id(self):
        body = _provision(_agent('rei', 3))
        self.assertNotIn(
            'owner_user_id', body,
            "sending it declares a personal-default identity, and shizuha-id "
            "then rejects any username that is not shizuha-<owner_id> — which "
            "made rei permanently unenable-able",
        )
        self.assertEqual(body['username'], 'rei')

    def test_even_a_personal_shaped_agent_omits_it(self):
        """The personal-default concept is retired (operator 2026-08-05).

        `shizuha-<owner_id>` is now just a username. Nothing claims
        personal-default semantics, so nothing can be rejected for failing to
        match them.
        """
        body = _provision(_agent('shizuha-128', 128))
        self.assertNotIn('owner_user_id', body)

    def test_a_lookalike_username_does_not_claim_another_owner(self):
        """`shizuha-128` owned by 3 is NOT owner 128's personal default."""
        body = _provision(_agent('shizuha-128', 3))
        self.assertNotIn('owner_user_id', body)

    def test_an_agent_with_no_owner_omits_it(self):
        body = _provision(_agent('scout', None))
        self.assertNotIn('owner_user_id', body)

    def test_the_rest_of_the_payload_is_unchanged(self):
        body = _provision(_agent('rei', 3))
        self.assertEqual(body['email'], 'rei@agents.shizuha.io')
        self.assertEqual(body['display_name'], 'Rei')
        self.assertEqual(body['runtime_id'], 'agent-rei')
        self.assertEqual(body['password'], 'a-password-value')


class BindingValidationIsScopedTooTests(SimpleTestCase):
    """The response check must not demand personal-default semantics either.

    After the REQUEST stopped over-claiming, rei's identity reconnected cleanly
    (`HIVE-75: reconnected platform identity for agent rei (user_id=106)`) and
    was then rejected by the response validation:

        HIVE-620: ID returned mismatched personal-owner binding for rei
        PLAT-4884 DRIFT_HOLD agent=rei reason=reconcile_failed

    Two guards, one wrong assumption. Fixing only the request left the agent
    exactly as unstartable.
    """

    def _provision_with_response(self, agent, payload):
        resp = MagicMock(ok=True)
        resp.json.return_value = payload
        with patch.object(provisioner, '_fleet_provisioner_token', return_value='tok'), \
                patch.object(provisioner, 'settings') as st, \
                patch('requests.post', return_value=resp):
            st.SHIZUHA_ID_INTERNAL_URL = 'http://id:8000'
            return provisioner._provision_platform_identity_details(
                agent, 'a-password-value', 'http://platform',
            )

    def test_an_ordinary_agent_accepts_a_non_personal_binding(self):
        binding = self._provision_with_response(
            _agent('rei', 3),
            {'user_id': 106, 'profile_id': 9},
        )
        self.assertIsNotNone(
            binding,
            'rejecting this is what put rei back into DRIFT_HOLD after the '
            'identity had already reconnected successfully',
        )
        self.assertEqual(binding.user_id, 106)

    def test_a_personal_shaped_agent_is_accepted_without_an_owner_binding(self):
        """No personal-default binding is requested, so none is required."""
        binding = self._provision_with_response(
            _agent('shizuha-128', 128),
            {'user_id': 489, 'profile_id': 9},
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding.user_id, 489)

    def test_an_ordinary_agent_is_accepted_too(self):
        binding = self._provision_with_response(
            _agent('rei', 3), {'user_id': 106, 'profile_id': 9},
        )
        self.assertIsNotNone(
            binding,
            'rejecting this is what put rei back into DRIFT_HOLD after the '
            'identity had already reconnected successfully',
        )
