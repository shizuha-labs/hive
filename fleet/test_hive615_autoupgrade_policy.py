"""HIVE-615: the auto-upgrade policy dial.

The operator asked for "auto for patch bumps, notify-only for majors" after the
codex 0.136->0.144 bump poisoned sessions fleet-wide. These tests pin the two
properties that make the dial trustworthy:

  1. it can never classify a DOWNGRADE as a bump (the defect revi found in the
     abandoned Home implementation, where `_bump_allowed('1.2.3','1.2.2')` was
     True at `patch` and could roll the fleet backward);
  2. withholding an upgrade suppresses DISPATCH only — detection still reports
     it, so a withheld upgrade never becomes invisible.
"""

from __future__ import annotations

import os
from unittest import mock

from django.test import SimpleTestCase

from fleet import harness_versions as hv


class BumpLevelTests(SimpleTestCase):
    def test_classifies_each_level(self):
        self.assertEqual(hv.bump_level('1.2.3', '1.2.4'), 'patch')
        self.assertEqual(hv.bump_level('1.2.3', '1.3.0'), 'minor')
        self.assertEqual(hv.bump_level('1.2.3', '2.0.0'), 'major')

    def test_downgrade_is_never_a_bump(self):
        """The exact failure revi reproduced: a downgrade passing the gate."""
        self.assertIsNone(hv.bump_level('1.2.3', '1.2.2'))
        self.assertIsNone(hv.bump_level('2.0.0', '1.9.9'))
        self.assertIsNone(hv.bump_level('1.3.0', '1.2.9'))

    def test_equal_is_not_a_bump(self):
        self.assertIsNone(hv.bump_level('1.2.3', '1.2.3'))

    def test_missing_or_unparseable_is_not_a_bump(self):
        for cur, new in (('', '1.0.0'), ('1.0.0', ''), (None, '1.0.0'),
                         ('1.0.0', None), ('abc', 'def')):
            self.assertIsNone(hv.bump_level(cur, new), (cur, new))

    def test_short_versions_zero_pad_rather_than_misclassify(self):
        """1.2 -> 1.2.1 is a patch, not a fallthrough."""
        self.assertEqual(hv.bump_level('1.2', '1.2.1'), 'patch')
        self.assertEqual(hv.bump_level('1', '1.1'), 'minor')
        self.assertEqual(hv.bump_level('1', '2'), 'major')

    def test_matches_upgrade_available(self):
        """bump_level must never disagree with the proven gate."""
        for cur, new in (('1.2.3', '1.2.4'), ('1.2.3', '1.2.2'), ('1.2.3', '1.2.3'),
                         ('1.9.0', '2.0.0'), ('', '1.0.0')):
            self.assertEqual(
                hv.bump_level(cur, new) is not None,
                hv.upgrade_available(cur, new),
                (cur, new),
            )


class PolicyResolutionTests(SimpleTestCase):
    def test_default_is_conservative_and_stops_majors(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HARNESS_AUTOUPGRADE', None)
            self.assertEqual(hv.autoupgrade_policy(), hv.DEFAULT_AUTOUPGRADE_POLICY)
            self.assertEqual(hv.DEFAULT_AUTOUPGRADE_POLICY, 'minor')
        # The point of the default: majors stop auto-rolling.
        self.assertFalse(hv.autoupgrade_allowed('1.2.3', '2.0.0', 'minor'))

    def test_each_policy_is_honoured(self):
        cases = [
            ('off',   '1.2.3', '1.2.4', False),
            ('off',   '1.2.3', '1.3.0', False),
            ('off',   '1.2.3', '2.0.0', False),
            ('patch', '1.2.3', '1.2.4', True),
            ('patch', '1.2.3', '1.3.0', False),
            ('patch', '1.2.3', '2.0.0', False),
            ('minor', '1.2.3', '1.2.4', True),
            ('minor', '1.2.3', '1.3.0', True),
            ('minor', '1.2.3', '2.0.0', False),
        ]
        for policy, cur, new, expected in cases:
            self.assertEqual(hv.autoupgrade_allowed(cur, new, policy), expected,
                             (policy, cur, new))

    def test_unrecognised_value_falls_back_and_does_not_become_permissive(self):
        """A typo must not silently mean 'promote anything'."""
        # NB: 'PATCH ' is deliberately NOT in this list. autoupgrade_policy()
        # normalises with .strip().lower(), so it resolves to the valid, MORE
        # conservative 'patch' -- and test_policy_is_case_insensitive below
        # requires exactly that normalisation. Treating it as a typo would
        # demand the variant fall back to the MORE permissive 'minor',
        # inverting this test's own intent.
        for bogus in ('all', 'major', 'true', 'yes', '1', ''):
            with mock.patch.dict(os.environ, {'HARNESS_AUTOUPGRADE': bogus}):
                self.assertEqual(hv.autoupgrade_policy(), hv.DEFAULT_AUTOUPGRADE_POLICY, bogus)
                # Whatever the typo, a major never auto-promotes.
                self.assertFalse(hv.autoupgrade_allowed('1.2.3', '2.0.0'), bogus)

    def test_policy_is_case_insensitive(self):
        with mock.patch.dict(os.environ, {'HARNESS_AUTOUPGRADE': 'PATCH'}):
            self.assertEqual(hv.autoupgrade_policy(), 'patch')

    def test_no_policy_ever_permits_a_downgrade(self):
        """The safety property that must hold across the whole dial."""
        for policy in hv.AUTOUPGRADE_POLICIES:
            self.assertFalse(hv.autoupgrade_allowed('1.2.3', '1.2.2', policy), policy)
            self.assertFalse(hv.autoupgrade_allowed('2.0.0', '1.0.0', policy), policy)
            self.assertFalse(hv.autoupgrade_allowed('1.2.3', '1.2.3', policy), policy)
