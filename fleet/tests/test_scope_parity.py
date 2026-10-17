"""HIVE-2695: listing-mint token-scope parity validator.

Verb-subset check + flag path per the PLAT-9430 design (sara comment 826977).
Pure-function tests — no DB, no HTTP: the validator is a pure module so the
serve point (AgentEffectiveCapabilitiesView) and the mint point
(ensure_agent_forgejo_token) can never silently diverge from this contract.
"""
from django.test import SimpleTestCase

from fleet.scope_parity import (
    GAP_CLASS_EXERCISED,
    GAP_CLASS_NON_ORIGIN,
    GAP_CLASS_UNMAPPED,
    check_scope_parity,
    scope_satisfied,
)


def _grant(*tags):
    return {'credential_grant_scopes': sorted(tags)}


class ScopeSatisfiedTests(SimpleTestCase):
    """The Forgejo write-implies-read verb-subset semantics."""

    def test_exact_read_scope_satisfies_read(self):
        self.assertTrue(scope_satisfied({'read:repository'}, 'read:repository'))

    def test_write_scope_implies_read_on_same_resource(self):
        self.assertTrue(scope_satisfied({'write:repository'}, 'read:repository'))

    def test_read_scope_does_not_imply_write(self):
        self.assertFalse(scope_satisfied({'read:repository'}, 'write:repository'))

    def test_other_resource_scopes_never_satisfy(self):
        self.assertFalse(scope_satisfied({'write:issue', 'read:user'}, 'read:repository'))

    def test_empty_token_scopes_satisfy_nothing(self):
        self.assertFalse(scope_satisfied(set(), 'read:repository'))
        self.assertFalse(scope_satisfied(None, 'write:repository'))

    def test_empty_requirement_is_trivially_satisfied(self):
        self.assertTrue(scope_satisfied(set(), ''))


class CheckScopeParityTests(SimpleTestCase):
    """The flag path: advertised-but-unexercisable must be impossible to mint
    silently."""

    def test_flag_path_merge_tag_on_read_scoped_token(self):
        # The PLAT-9426 fixture: whitelisted-merger listing on a read-scoped
        # ORIGIN_TOKEN.
        result = check_scope_parity(
            agent=None,
            grant=_grant('github:merge'),
            origin_token_scopes=('read:repository', 'read:issue', 'read:user', 'read:organization'),
        )
        self.assertTrue(result.scope_gap)
        by_tag = {g.tag: g for g in result.gaps}
        self.assertIn('github:merge', by_tag)
        gap = by_tag['github:merge']
        self.assertEqual(gap.gap_class, GAP_CLASS_EXERCISED)
        self.assertEqual(gap.required_origin_scopes, ('write:repository',))
        self.assertEqual(gap.missing_scopes, ('write:repository',))

    def test_flag_path_origin_blind_no_token_at_all(self):
        # The kai origin-blind fixture: listing advertised, no backing token.
        result = check_scope_parity(agent=None, grant=_grant('github:merge'), origin_token_scopes=[])
        self.assertTrue(result.scope_gap)
        self.assertEqual(
            {g.tag: g.gap_class for g in result.gaps}['github:merge'],
            GAP_CLASS_EXERCISED,
        )

    def test_satisfied_path_write_scoped_token_advertising_merge(self):
        result = check_scope_parity(
            agent=None,
            grant=_grant('github:merge'),
            origin_token_scopes=('write:repository', 'write:issue', 'read:user', 'read:organization'),
        )
        self.assertFalse(result.scope_gap)
        self.assertEqual(result.gaps, ())

    def test_review_tag_needs_only_read(self):
        result = check_scope_parity(
            agent=None,
            grant=_grant('github:review'),
            origin_token_scopes=('read:repository', 'read:issue', 'read:user', 'read:organization'),
        )
        self.assertFalse(result.scope_gap)

    def test_review_tag_flagged_when_even_read_is_missing(self):
        result = check_scope_parity(
            agent=None,
            grant=_grant('github:review'),
            origin_token_scopes=('read:user', 'read:organization'),
        )
        self.assertTrue(result.scope_gap)
        self.assertEqual(
            {g.tag: g.gap_class for g in result.gaps}['github:review'],
            GAP_CLASS_EXERCISED,
        )

    def test_kubeconfig_is_non_origin_and_never_sets_scope_gap(self):
        # The bearer-wall nuance: kubeconfig is a non-Origin broker provider —
        # recorded in the detail for the entitled lane, never an Origin flag.
        result = check_scope_parity(
            agent=None,
            grant=_grant('kubeconfig', 'github:review'),
            origin_token_scopes=('read:repository',),
        )
        self.assertFalse(result.scope_gap)
        by_tag = {g.tag: g for g in result.gaps}
        self.assertIn('kubeconfig', by_tag)
        self.assertEqual(by_tag['kubeconfig'].gap_class, GAP_CLASS_NON_ORIGIN)

    def test_unmapped_github_tag_fails_closed(self):
        # A new verb-tag type must not silently skip the check: unmapped tags
        # set scope_gap until they are added to ORIGIN_VERB_TAG_REQUIREMENTS.
        result = check_scope_parity(
            agent=None,
            grant=_grant('github:rotate'),
            origin_token_scopes=('write:repository',),
        )
        self.assertTrue(result.scope_gap)
        self.assertEqual(
            {g.tag: g.gap_class for g in result.gaps}['github:rotate'],
            GAP_CLASS_UNMAPPED,
        )

    def test_mixed_listing_reports_every_class(self):
        result = check_scope_parity(
            agent=None,
            grant=_grant('github:merge', 'github:review', 'kubeconfig'),
            origin_token_scopes=('read:repository',),
        )
        self.assertTrue(result.scope_gap)
        classes = {g.tag: g.gap_class for g in result.gaps}
        self.assertEqual(classes['github:merge'], GAP_CLASS_EXERCISED)
        self.assertNotIn('github:review', classes)  # satisfied, no gap row
        self.assertEqual(classes['kubeconfig'], GAP_CLASS_NON_ORIGIN)
        self.assertEqual(sorted(result.checked_tags), ['github:merge', 'github:review', 'kubeconfig'])

    def test_no_advertised_tags_is_never_a_gap(self):
        result = check_scope_parity(agent=None, grant=_grant(), origin_token_scopes=[])
        self.assertFalse(result.scope_gap)
        self.assertEqual(result.gaps, ())

    def test_as_dict_shape_for_payload_stamping(self):
        result = check_scope_parity(
            agent=None,
            grant=_grant('github:merge', 'kubeconfig'),
            origin_token_scopes=('read:repository',),
        )
        d = result.as_dict()
        self.assertTrue(d['scope_gap'])
        self.assertTrue(any(g['tag'] == 'github:merge' for g in d['scope_gap_detail']))
        self.assertEqual(d['checked_tags'], ['github:merge', 'kubeconfig'])
