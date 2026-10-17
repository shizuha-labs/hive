"""A claw owned by one of our own agents is own-fleet traffic, not a customer.

Operator 2026-08-05, from the Cortex usage log:

    hive-claw-16-1d17606444994ee6a1075c6e03a3fb35 · internal · DeepSeek-V4-Flash
    · model access denied

    i'd like even claw based agents in hive to be able to lease our deepseek
    model .. just like other agents do

`mint_owner_cortex_key` sent `name`, `user_id` and `service_tier` — but no
`traffic_class`. Cortex's internal mint endpoint defaults that to
RESOLD_CUSTOMER, and `principal_can_access_internal_models` refuses resold
traffic FIRST and unconditionally (deliberately: "RESOLD traffic is NEVER
internal, no exceptions"). So every claw was classified as a paying outside
customer:

    hive-claw-16 (mika@shizuha.com)   tc=resold_customer  -> denied
    hive-claw-15 (zen@shizuha.com)    tc=resold_customer  -> denied
    hive-claw-3  (hritik)             tc=own_fleet        -> worked

The gate is right. The classification was wrong. These tests pin the
classification, and specifically that it FAILS CLOSED — a claw we cannot
positively identify as internal must stay resold, or this becomes a way to hand
internal model access to a customer.
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from claws import provisioner


class _Claw:
    def __init__(self, owner_id=16):
        self.id = MagicMock()
        self.id.hex = 'deadbeef'
        self.owner_id = owner_id


def _classify(payload=None, *, status_code=200, raises=None, settings_ok=True):
    resp = MagicMock(status_code=status_code)
    if payload is None:
        resp.json.side_effect = ValueError('not json')
    else:
        resp.json.return_value = payload
    with patch.object(provisioner, 'settings') as st, \
            patch.object(provisioner.requests, 'get',
                         side_effect=raises, return_value=resp):
        st.SHIZUHA_ID_INTERNAL_URL = 'http://id:8000' if settings_ok else ''
        st.HIVE_ID_FLEET_PROVISIONER_TOKEN = 'tok' if settings_ok else ''
        return provisioner.claw_traffic_class(_Claw())


class InternalPrincipalsGetOwnFleetTests(SimpleTestCase):
    def test_an_agent_owner_is_own_fleet(self):
        """mika/zen own claws and are agents — the reported case."""
        self.assertEqual(
            _classify({'id': 16, 'account_type': 'agent'}), 'own_fleet',
        )

    def test_an_agent_owner_via_nested_profile_is_own_fleet(self):
        self.assertEqual(
            _classify({'id': 16, 'profile': {'account_type': 'agent'}}), 'own_fleet',
        )

    def test_staff_is_own_fleet(self):
        self.assertEqual(_classify({'id': 3, 'is_staff': True}), 'own_fleet')

    def test_a_superuser_is_own_fleet(self):
        self.assertEqual(_classify({'id': 3, 'is_superuser': True}), 'own_fleet')


class EverythingElseStaysResoldTests(SimpleTestCase):
    """Fail closed: this must not become a path to internal models for customers."""

    def test_an_ordinary_human_is_resold(self):
        self.assertEqual(
            _classify({'id': 500, 'account_type': 'human', 'is_staff': False}),
            'resold_customer',
        )

    def test_a_shizuha_com_email_alone_does_not_qualify(self):
        # Cortex's own gate warns customer accounts can live on @shizuha.com,
        # so the domain is never the signal.
        self.assertEqual(
            _classify({'id': 500, 'email': 'someone@shizuha.com', 'is_staff': False}),
            'resold_customer',
        )

    def test_an_unreachable_id_service_is_resold(self):
        import requests as rq
        self.assertEqual(
            _classify({'id': 16, 'account_type': 'agent'},
                      raises=rq.RequestException('down')),
            'resold_customer',
        )

    def test_a_non_200_is_resold(self):
        self.assertEqual(
            _classify({'id': 16, 'account_type': 'agent'}, status_code=503),
            'resold_customer',
        )

    def test_a_non_json_body_is_resold(self):
        self.assertEqual(_classify(None), 'resold_customer')

    def test_unconfigured_id_service_is_resold(self):
        self.assertEqual(
            _classify({'id': 16, 'account_type': 'agent'}, settings_ok=False),
            'resold_customer',
        )

    def test_a_claw_with_no_owner_is_resold(self):
        claw = _Claw(owner_id=None)
        with patch.object(provisioner, 'settings') as st:
            st.SHIZUHA_ID_INTERNAL_URL = 'http://id:8000'
            st.HIVE_ID_FLEET_PROVISIONER_TOKEN = 'tok'
            self.assertEqual(provisioner.claw_traffic_class(claw), 'resold_customer')


class MintSendsTheClassificationTests(SimpleTestCase):
    def test_the_mint_body_carries_traffic_class(self):
        """Computing it is useless if the request omits it — that was the bug."""
        from claws.models import Claw
        claw = MagicMock(spec=Claw)
        claw.id = MagicMock(); claw.id.hex = 'abc'
        claw.owner_id = 16
        claw.agent_type = 'claw'
        resp = MagicMock(status_code=201)
        resp.json.return_value = {'key': 'sk-cortex-x'}
        with patch.object(provisioner, 'settings') as st, \
                patch.object(provisioner, 'claw_traffic_class', return_value='own_fleet'), \
                patch.object(provisioner, 'cortex_service_tier_for_claw', return_value='internal'), \
                patch.object(provisioner.requests, 'post', return_value=resp) as post:
            st.CORTEX_API_KEY = 'svc'
            st.CORTEX_BASE_URL = 'http://cortex:8040'
            provisioner.mint_owner_cortex_key(claw, 'tok')
        body = post.call_args.kwargs['json']
        self.assertEqual(body['traffic_class'], 'own_fleet')
        self.assertNotIn('billing_user_id', body)
