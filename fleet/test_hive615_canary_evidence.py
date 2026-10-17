"""HIVE-615: the canary bar — a post-switch NON-EMPTY turn at a NEWER sequence.

revi's P1 #4 set this bar, and it is deliberately harder than "the agent is
running". Each test below is a distinct way a naive `total > pre_sequence`
check returns VERIFIED on evidence that does not show the new image doing any
work:

  * the sequence advances but every new event is empty — a crash-loop or a bare
    lifecycle record bumps the counter without the agent producing anything;
  * the pre-switch baseline is missing, so "newer" is unprovable. Defaulting it
    to 0 is the dangerous reading: it makes activity recorded BEFORE the switch
    satisfy a post-switch check;
  * the activity read failed. `get_runtime_agent_activity` returns {} on any
    transport error, which is byte-identical to a genuinely idle agent, so it
    can prove neither success nor failure.

The probe is read-only by construction — it never mutates fleet state and never
touches the lease — which is what lets it run as the ruling's "read-only async
probe" while the owning command stays the sole writer.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from fleet import harness_versions as hv


class CanaryTurnEvidenceTests(SimpleTestCase):
    def _activity(self, total, events):
        return {'total': total, 'events': events, 'sessionFile': '/s.jsonl'}

    def test_non_empty_turn_at_newer_sequence_verifies(self):
        v = hv.canary_turn_evidence('a1', 5, activity=self._activity(
            6, [{'text': 'did the work'}]))
        self.assertEqual(v['status'], hv.CANARY_VERIFIED)
        self.assertEqual(v['post_sequence'], 6)
        self.assertEqual(v['content_events'], 1)

    def test_sequence_advanced_but_all_new_events_empty_is_not_verified(self):
        v = hv.canary_turn_evidence('a1', 5, activity=self._activity(
            7, [{'text': ''}, {'content': []}]))
        self.assertEqual(v['status'], hv.CANARY_PENDING)
        self.assertIn('no new event carries content', v['reason'])

    def test_absent_pre_sequence_is_unproven_never_treated_as_zero(self):
        for missing in (None, '', 'unknown', -1, 3.5):
            with self.subTest(pre=missing):
                v = hv.canary_turn_evidence('a1', missing, activity=self._activity(
                    99, [{'text': 'activity recorded BEFORE the switch'}]))
                self.assertEqual(v['status'], hv.CANARY_UNPROVEN)
                self.assertIn('unprovable', v['reason'])

    def test_failed_activity_read_is_unproven_not_pass_and_not_fail(self):
        for payload in ({}, None, [], 'nope', {'events': []}):
            with self.subTest(payload=payload):
                v = hv.canary_turn_evidence('a1', 5, activity=payload)
                self.assertEqual(v['status'], hv.CANARY_UNPROVEN)

    def test_probe_never_raises_into_the_orchestrator(self):
        def boom(_agent_id):
            raise RuntimeError('control plane down')

        v = hv.canary_turn_evidence('a1', 5, fetch=boom)
        self.assertEqual(v['status'], hv.CANARY_UNPROVEN)
        self.assertIn('activity read failed', v['reason'])

    def test_unadvanced_sequence_is_pending(self):
        v = hv.canary_turn_evidence('a1', 9, activity=self._activity(
            9, [{'text': 'old turn'}]))
        self.assertEqual(v['status'], hv.CANARY_PENDING)

    def test_only_events_newer_than_the_baseline_can_verify(self):
        """Pre-existing content must not verify a newly advanced sequence."""
        v = hv.canary_turn_evidence('a1', 3, activity=self._activity(
            4, [{'text': 'old'}, {'text': 'old'}, {'text': 'old'}, {'text': ''}]))
        self.assertEqual(v['new_events'], 1)
        self.assertEqual(v['status'], hv.CANARY_PENDING)

    def test_verified_requires_content_in_a_NEW_event_specifically(self):
        v = hv.canary_turn_evidence('a1', 3, activity=self._activity(
            5, [{'text': 'old'}, {'text': 'old'}, {'text': 'old'},
                {'text': ''}, {'result': {'ok': True}}]))
        self.assertEqual(v['status'], hv.CANARY_VERIFIED)
        self.assertEqual(v['new_events'], 2)
        self.assertEqual(v['content_events'], 1)

    def test_probe_does_not_mutate_the_run_record_or_touch_the_lease(self):
        """Read-only by construction: the ruling allows an async probe only if
        it cannot become a second writer."""
        from django.core.cache import cache
        cache.delete(hv.HARNESS_UPGRADE_RUN_KEY)
        run = hv.new_run('img:new', run_id='r1', lease_token='t1', last_good='img:old')
        hv.save_run(run)
        before = cache.get(hv.HARNESS_UPGRADE_RUN_KEY)
        hv.canary_turn_evidence('a1', 5, activity=self._activity(6, [{'text': 'x'}]))
        self.assertEqual(cache.get(hv.HARNESS_UPGRADE_RUN_KEY), before)
        cache.delete(hv.HARNESS_UPGRADE_RUN_KEY)
