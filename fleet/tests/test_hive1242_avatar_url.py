"""HIVE-1242 / HIVE-1263: agent portrait `avatar_url` contract.

The API emits a resolvable `avatar_url` for EVERY valid identity so no agent
renders as bare initials or 404s:
- curated named portrait (the published 42) → `/avatars/<name>.png`
- every other identity → deterministic stock pick `/avatars/stock/<seed%N>.png`
- only empty/None input yields None (frontend then falls back to initials).

These tests pin the helper + serializer contract; the zero-404 render behavior
is covered by frontend/src/components/AgentAvatar.test.jsx and live browser
verification.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from hive_project.avatars import (
    AVATAR_STOCK_SIZE,
    AVATAR_USERNAMES,
    avatar_seed,
    avatar_url_for,
)
from fleet.serializers import FleetAgentSerializer


class AvatarUrlHelperTests(SimpleTestCase):
    def test_known_agent_resolves_full_url(self):
        self.assertEqual(avatar_url_for('sara'), 'https://shizuha.com/avatars/sara.png')

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(avatar_url_for('SARA'), 'https://shizuha.com/avatars/sara.png')
        self.assertEqual(avatar_url_for('  Kai '), 'https://shizuha.com/avatars/kai.png')

    def test_every_valid_username_resolves_to_a_stock_avatar(self):
        # HIVE-1263: previously unknown identities were None (initials-only);
        # now every valid username gets a deterministically-assigned stock avatar.
        url = avatar_url_for('no-such-agent-xyz')
        self.assertTrue(url.startswith('https://shizuha.com/avatars/stock/'))
        self.assertTrue(url.endswith('.png'))

    def test_stock_avatar_is_stable_and_bounded(self):
        a = avatar_url_for('goro')
        b = avatar_url_for('GORO')
        self.assertEqual(a, b)  # case-insensitive + deterministic → stable identity avatar
        idx = int(a.rsplit('/', 1)[-1].split('.')[0])
        self.assertGreaterEqual(idx, 0)
        self.assertLess(idx, AVATAR_STOCK_SIZE)

    def test_distinct_usernames_are_not_all_collapsed(self):
        # The hash is broad enough that a handful of names spread across the pool.
        seen = {avatar_url_for(n) for n in ['goro', 'ada', 'neo', 'axel', 'vera', 'ogi']}
        self.assertGreater(len(seen), 1)

    def test_empty_input_is_none(self):
        self.assertIsNone(avatar_url_for(''))
        self.assertIsNone(avatar_url_for(None))

    def test_roster_is_the_published_42(self):
        self.assertEqual(len(AVATAR_USERNAMES), 42)
        self.assertTrue(AVATAR_USERNAMES.issuperset({'sara', 'kai', 'ryo', 'aoi'}))

    def test_avatar_seed_is_stable(self):
        self.assertEqual(avatar_seed('goro'), avatar_seed('GORO'))
        self.assertEqual(avatar_seed('goro'), avatar_seed('  goro '))

    @override_settings(AGENT_AVATAR_BASE_URL='https://cdn.example.com/a/')
    def test_base_url_is_overridable_and_trailing_slash_safe(self):
        self.assertEqual(avatar_url_for('sara'), 'https://cdn.example.com/a/sara.png')
        self.assertTrue(avatar_url_for('goro').startswith('https://cdn.example.com/a/stock/'))

    @override_settings(AGENT_AVATAR_BASE_URL='')
    def test_empty_base_url_falls_back_to_none(self):
        self.assertIsNone(avatar_url_for('sara'))
        self.assertIsNone(avatar_url_for('goro'))


class FleetSerializerAvatarTests(SimpleTestCase):
    def test_get_avatar_url_known(self):
        s = FleetAgentSerializer()
        self.assertEqual(
            s.get_avatar_url(SimpleNamespace(agent_username='ryo')),
            'https://shizuha.com/avatars/ryo.png',
        )

    def test_get_avatar_url_unknown_is_stock_not_none(self):
        s = FleetAgentSerializer()
        self.assertTrue(
            s.get_avatar_url(SimpleNamespace(agent_username='goro'))
            .startswith('https://shizuha.com/avatars/stock/'),
        )

    def test_avatar_url_declared_on_serializer(self):
        self.assertIn('avatar_url', FleetAgentSerializer().fields)
