"""CTX-1055 — operator-only, time-boxed per-seat lease pin."""
from __future__ import annotations

from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .lease_operator_pin import (
    OPERATOR_PIN_AUDIT_CACHE_KEY,
    OPERATOR_PIN_CACHE_KEY,
    OPERATOR_PIN_MAX_TTL_S,
    active_operator_pin,
    audit_log,
    clear_operator_pin,
    operator_pinned_usernames,
    set_operator_pin,
)
from .models import LeaseCriticalTeamPolicy
from .model_lease import (
    CRITICAL_TEAM_POLICY_CACHE_KEY,
    HISTORY_CACHE_KEY,
    HOLDINGS_CACHE_KEY,
    PREEMPT_RETRY_CACHE_KEY,
    _reconcile_one_pool,
)
from .test_model_lease_critical_floor import FloorClient, _holder

KEYS = (HISTORY_CACHE_KEY, HOLDINGS_CACHE_KEY, PREEMPT_RETRY_CACHE_KEY,
        CRITICAL_TEAM_POLICY_CACHE_KEY, OPERATOR_PIN_CACHE_KEY,
        OPERATOR_PIN_AUDIT_CACHE_KEY)


class PinStoreTests(TestCase):
    def setUp(self):
        for k in KEYS:
            cache.delete(k)

    tearDown = setUp

    def test_default_off(self):
        self.assertIsNone(active_operator_pin())
        self.assertEqual(operator_pinned_usernames(), set())

    def test_set_requires_reason_actor_and_bounded_ttl(self):
        with self.assertRaises(ValueError):
            set_operator_pin('sora', ttl_seconds=600, reason='', actor='hritik')
        with self.assertRaises(ValueError):
            set_operator_pin('sora', ttl_seconds=600, reason='r', actor='')
        with self.assertRaises(ValueError):
            set_operator_pin('sora', ttl_seconds=OPERATOR_PIN_MAX_TTL_S + 1,
                             reason='r', actor='hritik')
        with self.assertRaises(ValueError):
            set_operator_pin('sora', ttl_seconds=10, reason='r', actor='hritik')

    def test_set_normalizes_and_expires(self):
        set_operator_pin('agent-sora', ttl_seconds=600, reason='review', actor='hritik', now=1000)
        self.assertEqual(operator_pinned_usernames(now=1500), {'sora'})
        self.assertEqual(operator_pinned_usernames(now=1600), set())
        events = [e['event'] for e in audit_log()]
        self.assertEqual(events, ['set', 'expire'])

    def test_single_pin_replace_and_clear_audited(self):
        set_operator_pin('sora', ttl_seconds=600, reason='a', actor='hritik')
        set_operator_pin('aoi', ttl_seconds=600, reason='b', actor='hritik')
        self.assertEqual(operator_pinned_usernames(), {'aoi'})
        removed = clear_operator_pin(actor='hritik', reason='done')
        self.assertEqual(removed['username'], 'aoi')
        self.assertEqual(operator_pinned_usernames(), set())
        self.assertEqual([e['event'] for e in audit_log()], ['set', 'replace', 'set', 'clear'])

    def test_corrupt_cache_reads_as_no_pin(self):
        cache.set(OPERATOR_PIN_CACHE_KEY, 'garbage', 60)
        self.assertIsNone(active_operator_pin())
        cache.set(OPERATOR_PIN_CACHE_KEY, {'username': 'sora', 'expires_at': 'x'}, 60)
        self.assertIsNone(active_operator_pin())

    def test_management_command(self):
        out = StringIO()
        call_command('lease_pin', 'set', 'sora', '--minutes', '30', '--reason', 'r',
                     '--actor', 'hritik', stdout=out)
        self.assertIn('PIN SET', out.getvalue())
        out = StringIO()
        call_command('lease_pin', 'show', stdout=out)
        self.assertIn('"username": "sora"', out.getvalue())
        with self.assertRaises(CommandError):
            call_command('lease_pin', 'set', 'sora', '--minutes', '500', '--reason', 'r',
                         '--actor', 'hritik', stdout=StringIO())
        call_command('lease_pin', 'clear', '--actor', 'hritik', stdout=StringIO())
        self.assertIsNone(active_operator_pin())


class PinPlannerTests(TestCase):
    def setUp(self):
        LeaseCriticalTeamPolicy.objects.all().delete()
        for k in KEYS:
            cache.delete(k)
        self.cfg = {
            'enabled': True, 'model_id': 'GLM-5.3-Flash',
            'members': ['kei', 'ryo', 'jun', 'sora'],
            'min_hold_seconds': 900, 'max_hold_seconds': 2700,
            'idle_release_seconds': 180, 'max_holders': 2,
        }
        self.agents = [
            {'username': u, 'email': f'{u}@shizuha.com', 'status': 'running', 'enabled': True}
            for u in ('kei', 'ryo', 'jun', 'sora')
        ]
        self.teams = {
            'kei@shizuha.com': {'engineering'}, 'ryo@shizuha.com': {'engineering'},
            'jun@shizuha.com': {'engineering'}, 'sora@shizuha.com': {'architecture'},
        }

    def tearDown(self):
        for k in KEYS:
            cache.delete(k)

    def _tick(self, client, *, now=1000, agents=None, workload=None):
        return _reconcile_one_pool(
            cfg=self.cfg, agents=agents or self.agents, email_to_teams=self.teams,
            workload_by_team={}, workload_by_email=workload or {},
            unread_by_email={}, alerts_by_email={},
            actionable_buckets=('ready', 'open', 'todo'),
            client=client, now=now, apply=True,
        )

    def test_no_pin_unworthy_seat_not_granted(self):
        client = FloorClient(holders=[_holder('agent-kei', held_for_s=300, idle_for_s=0)],
                             governed=['agent-kei', 'agent-ryo', 'agent-jun', 'agent-sora'])
        self._tick(client, workload={'kei@shizuha.com': {'ready': 1},
                                     'jun@shizuha.com': {'ready': 1}})
        self.assertNotIn('agent-sora', client.grants)
        self.assertIn('agent-jun', client.grants)

    def test_pin_takes_free_slot_first(self):
        set_operator_pin('sora', ttl_seconds=1800, reason='review', actor='hritik')
        client = FloorClient(holders=[_holder('agent-kei', held_for_s=300, idle_for_s=0)],
                             governed=['agent-kei', 'agent-ryo', 'agent-jun', 'agent-sora'])
        self._tick(client, workload={'kei@shizuha.com': {'ready': 1},
                                     'jun@shizuha.com': {'ready': 1},
                                     'ryo@shizuha.com': {'ready': 1}})
        # sora has NO work signal of its own; only the pin makes it worthy,
        # and it wins the single free slot over worthy jun/ryo.
        self.assertEqual(client.grants[0], 'agent-sora')

    def test_pin_preempts_only_a_quiet_holder(self):
        set_operator_pin('sora', ttl_seconds=1800, reason='review', actor='hritik')
        client = FloorClient(
            holders=[_holder('agent-kei', held_for_s=300, idle_for_s=290),
                     _holder('agent-ryo', held_for_s=300, idle_for_s=290)],
            governed=['agent-kei', 'agent-ryo', 'agent-jun', 'agent-sora'])
        agents = [{**a, 'activity_state': 'working', 'telemetry_ts': 1000}
                  if a['username'] == 'kei' else a for a in self.agents]
        self._tick(client, agents=agents, workload={'kei@shizuha.com': {'ready': 1},
                                                    'ryo@shizuha.com': {'ready': 1}})
        self.assertEqual(client.revokes, ['agent-ryo'])
        self.assertIn('agent-sora', client.grants)

    def test_pin_never_evicts_active_work(self):
        set_operator_pin('sora', ttl_seconds=1800, reason='review', actor='hritik')
        client = FloorClient(
            holders=[_holder('agent-kei', held_for_s=300, idle_for_s=0),
                     _holder('agent-ryo', held_for_s=300, idle_for_s=0)],
            governed=['agent-kei', 'agent-ryo', 'agent-jun', 'agent-sora'])
        agents = [{**a, 'activity_state': 'working', 'telemetry_ts': 1000}
                  if a['username'] in ('kei', 'ryo') else a for a in self.agents]
        self._tick(client, agents=agents, workload={'kei@shizuha.com': {'ready': 1},
                                                    'ryo@shizuha.com': {'ready': 1}})
        self.assertEqual(client.revokes, [])
        self.assertNotIn('agent-sora', client.grants)

    def test_pin_ignored_for_operator_stopped_seat(self):
        set_operator_pin('sora', ttl_seconds=1800, reason='review', actor='hritik')
        agents = [{**a, 'enabled': False, 'status': 'stopped'}
                  if a['username'] == 'sora' else a for a in self.agents]
        client = FloorClient(holders=[_holder('agent-kei', held_for_s=300, idle_for_s=0)],
                             governed=['agent-kei', 'agent-ryo', 'agent-jun', 'agent-sora'])
        self._tick(client, agents=agents, workload={'kei@shizuha.com': {'ready': 1}})
        self.assertNotIn('agent-sora', client.grants)

    def test_expired_pin_has_no_effect(self):
        set_operator_pin('sora', ttl_seconds=60, reason='review', actor='hritik', now=0)
        client = FloorClient(holders=[_holder('agent-kei', held_for_s=300, idle_for_s=0)],
                             governed=['agent-kei', 'agent-ryo', 'agent-jun', 'agent-sora'])
        self._tick(client, now=1000, workload={'kei@shizuha.com': {'ready': 1}})
        self.assertNotIn('agent-sora', client.grants)
