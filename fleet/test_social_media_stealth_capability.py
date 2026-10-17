"""Social-media team capability must grant stealth skills (operator 2026-07-29)."""
from django.test import SimpleTestCase, TestCase

from fleet.models import (
    DEFAULT_TEAM_CAPABILITY_SPECS,
    CapabilityDefinition,
    ensure_team_required_capability,
)
from fleet.effective_capabilities import compute_effective_capability_grant


REQUIRED_SKILLS = {
    'x-twitter',
    'connect-messaging',
    'human-browser-stealth',
    'x-account-safety',
}
REQUIRED_EAGER = {
    'human-browser-stealth',
    'x-account-safety',
    'x-twitter',
}


class SocialMediaStealthBaselineSpecTests(SimpleTestCase):
    def test_default_spec_lists_stealth_skills(self):
        for team in ('social-media', 'marketing'):
            spec = DEFAULT_TEAM_CAPABILITY_SPECS[team]
            self.assertTrue(REQUIRED_SKILLS.issubset(set(spec['skill_refs'])))
            self.assertTrue(REQUIRED_EAGER.issubset(set(spec.get('eager_skill_refs') or [])))

    def test_social_media_spec_allows_private_browser_mcp(self):
        self.assertIn('browser', DEFAULT_TEAM_CAPABILITY_SPECS['social-media']['mcp_server_refs'])


class SocialMediaStealthCapabilityGrantTests(TestCase):
    def test_ensure_team_required_capability_installs_stealth_bundle(self):
        cap, req = ensure_team_required_capability(
            'social-media',
            actor='test:social-media-stealth',
            reason='unit test baseline stealth skills',
        )
        self.assertTrue(req.enabled)
        self.assertTrue(req.required)
        self.assertEqual(cap.activation_status, CapabilityDefinition.ActivationStatus.ACTIVE)
        self.assertTrue(REQUIRED_SKILLS.issubset(set(cap.skill_refs or [])))
        self.assertTrue(REQUIRED_EAGER.issubset(set(cap.eager_skill_refs or [])))

    def test_ensure_merges_stealth_into_existing_row_without_dropping_extras(self):
        # Start from baseline (migration / ensure may already have created the row).
        ensure_team_required_capability(
            'social-media',
            actor='test:social-media-stealth',
            reason='seed',
        )
        cap = CapabilityDefinition.objects.get(slug='social-media')
        cap.skill_refs = list(cap.skill_refs or []) + ['custom-extra-skill']
        cap.eager_skill_refs = []
        cap.save(update_fields=['skill_refs', 'eager_skill_refs', 'updated_at'])
        cap, _ = ensure_team_required_capability(
            'social-media',
            actor='test:social-media-stealth',
            reason='merge stealth into existing',
        )
        self.assertIn('custom-extra-skill', cap.skill_refs)
        self.assertTrue(REQUIRED_SKILLS.issubset(set(cap.skill_refs or [])))
        self.assertTrue(REQUIRED_EAGER.issubset(set(cap.eager_skill_refs or [])))

    def test_effective_grant_for_social_media_team_includes_stealth(self):
        ensure_team_required_capability(
            'social-media',
            actor='test:social-media-stealth',
            reason='effective grant',
        )
        grant = compute_effective_capability_grant(['social-media'])
        self.assertTrue(REQUIRED_SKILLS.issubset(set(grant['skills'])))
        self.assertTrue(REQUIRED_EAGER.issubset(set(grant['eager_skills'])))
        self.assertIn('browser', grant['enabled_mcp_servers'])
