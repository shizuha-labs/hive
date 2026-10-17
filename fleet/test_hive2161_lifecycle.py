"""HIVE-2161 (S3 lifecycle): org Settings > Providers — write path.

Covers the HIVE-2161 acceptance surface (slice 1 — backend core):
  - provision orchestrates the LIVE Cortex listing lifecycle (draft → submit
    with the one-time key → validate) and persists the thin mirror row; the
    secret key never appears in any response field
  - provision failure states are recorded honestly (invalid + redacted
    outcome code), org-scoped
  - rotate re-runs submit+validate and refreshes the mirror
  - disable is org-scoped (owner/admin only; cross-org 403)
  - the cross-org override invariant: per_agent_overrides referencing another
    organization's credential is rejected at write time (403); unknown
    references at 400
"""
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from fleet.models import HiveOrgProviderReference
from fleet.org_provider_views import (
    OrgProviderReferenceDetailView,
    OrgProviderReferenceDisableView,
    OrgProviderReferenceListView,
    OrgProviderReferenceProvisionView,
    OrgProviderReferenceRotateView,
)

VIEWS = 'fleet.org_provider_views'


def _user(is_staff=False, org_ids=(), roles=None):
    roles = roles or {}
    return SimpleNamespace(
        is_authenticated=True,
        is_staff=is_staff,
        organization_memberships={str(o): roles.get(str(o), 'member') for o in org_ids},
    )


class LifecycleTestBase(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.provider_id = uuid4()
        self.listing_id = uuid4()
        self.credential_provider_id = uuid4()
        self.ref = HiveOrgProviderReference.objects.create(
            org_id=1,
            cortex_provider_id=self.provider_id,
            cortex_key_ref='cred-abc',
            display_name='Existing provider',
            status=HiveOrgProviderReference.Status.ACTIVE,
        )
        self.other_ref = HiveOrgProviderReference.objects.create(
            org_id=2,
            cortex_provider_id=self.credential_provider_id,
            cortex_key_ref='cred-other',
            display_name='Other org provider',
        )

    def _provision(self, user, data):
        req = self.factory.post('/v1/org-provider-refs/provision/', data, format='json')
        force_authenticate(req, user=user)
        return OrgProviderReferenceProvisionView.as_view()(req)

    def _rotate(self, user, ref_id, data):
        req = self.factory.post(f'/v1/org-provider-refs/{ref_id}/rotate/', data, format='json')
        force_authenticate(req, user=user)
        return OrgProviderReferenceRotateView.as_view()(req, ref_id=ref_id)

    def _disable(self, user, ref_id):
        req = self.factory.post(f'/v1/org-provider-refs/{ref_id}/disable/')
        force_authenticate(req, user=user)
        return OrgProviderReferenceDisableView.as_view()(req, ref_id=ref_id)

    def _remove(self, user, ref_id):
        req = self.factory.delete(f'/v1/org-provider-refs/{ref_id}/')
        force_authenticate(req, user=user)
        return OrgProviderReferenceDetailView.as_view()(req, ref_id=ref_id)

    def _upsert(self, user, data):
        req = self.factory.post('/v1/org-provider-refs/', data, format='json')
        force_authenticate(req, user=user)
        return OrgProviderReferenceListView.as_view()(req)

    @staticmethod
    def _scripted_post(responses):
        """Fake _cortex_post consuming a list of (payload, failure) tuples."""
        calls = []

        def fake(url, headers, body, timeout=20):
            calls.append({'url': url, 'body': body})
            return responses.pop(0)

        return fake, calls

    def _owner(self):
        return _user(org_ids=[1], roles={'1': 'owner'})


class ProvisionTests(LifecycleTestBase):
    def test_provision_happy_path_persists_mirror_and_never_leaks_key(self):
        secret = 'sk-super-secret-one-time-key'
        listing_payload = {'id': str(self.listing_id), 'display_name': 'Acme BYO',
                           'status': 'unlisted'}
        submit_payload = {'id': str(self.listing_id),
                          'validation_state': {'provider_id': str(self.credential_provider_id)},
                          'status': 'verifying'}
        validate_payload = {'id': str(self.listing_id), 'status': 'probation',
                            'models': [{'model': 'gpt-5.6'}]}
        fake, calls = self._scripted_post([
            (listing_payload, None), (submit_payload, None), (validate_payload, None),
        ])
        with patch(f'{VIEWS}._cortex_post', fake), \
             patch(f'{VIEWS}._cortex_base', return_value='http://cortex'), \
             patch(f'{VIEWS}._cortex_headers', return_value={}):
            resp = self._provision(self._owner(), {
                'org_id': 1, 'provider_type': 'openai_compatible',
                'display_name': 'Acme BYO', 'base_url': 'https://api.acme.example/v1',
                'models': [{'model': 'gpt-5.6'}], 'secret_key': secret,
            })
        self.assertEqual(resp.status_code, 201)
        ref = HiveOrgProviderReference.objects.get(id=resp.data['ref']['id'])
        self.assertEqual(str(ref.cortex_provider_id), str(self.listing_id))
        self.assertEqual(ref.cortex_key_ref, str(self.credential_provider_id))
        self.assertEqual(ref.status, HiveOrgProviderReference.Status.PROBATION)
        self.assertEqual(ref.last_validation_result, 'ok')
        self.assertEqual(ref.provider_type, 'openai_compatible')
        self.assertEqual(ref.created_by_principal, '')
        # The lifecycle hit the three named Cortex endpoints in order.
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[0]['url'].endswith('/v1/provider-listings'))
        self.assertTrue(calls[1]['url'].endswith('/submit'))
        self.assertTrue(calls[2]['url'].endswith('/validate'))
        # The key was relayed to Cortex exactly twice (submit + validate)…
        self.assertEqual(calls[1]['body']['api_key'], secret)
        self.assertEqual(calls[2]['body']['api_key'], secret)
        # …and appears NOWHERE in the response payload.
        self.assertNotIn(secret, json.dumps(resp.data))

    def test_provision_submit_failure_records_invalid_auth_failed(self):
        secret = 'sk-another-secret'
        fake, calls = self._scripted_post([
            ({'id': str(self.listing_id)}, None),
            (None, {'error': 'Cortex provider request failed.',
                    'detail': {'detail': 'bad key'}, 'status': 401}),
        ])
        with patch(f'{VIEWS}._cortex_post', fake), \
             patch(f'{VIEWS}._cortex_base', return_value='http://cortex'), \
             patch(f'{VIEWS}._cortex_headers', return_value={}):
            resp = self._provision(self._owner(), {
                'org_id': 1, 'display_name': 'Acme BYO',
                'base_url': 'https://api.acme.example/v1',
                'models': ['gpt-5.6'], 'secret_key': secret,
            })
        self.assertEqual(resp.status_code, 502)
        ref = HiveOrgProviderReference.objects.get(id=resp.data['ref']['id'])
        self.assertEqual(ref.status, HiveOrgProviderReference.Status.INVALID)
        self.assertEqual(ref.last_validation_result, 'auth_failed')
        self.assertNotIn(secret, json.dumps(resp.data))

    def test_provision_validate_failure_records_rate_limited(self):
        fake, calls = self._scripted_post([
            ({'id': str(self.listing_id)}, None),
            ({'validation_state': {'provider_id': str(self.credential_provider_id)}}, None),
            (None, {'error': 'Cortex provider request failed.',
                    'detail': {'detail': 'slow down'}, 'status': 429}),
        ])
        with patch(f'{VIEWS}._cortex_post', fake), \
             patch(f'{VIEWS}._cortex_base', return_value='http://cortex'), \
             patch(f'{VIEWS}._cortex_headers', return_value={}):
            resp = self._provision(self._owner(), {
                'org_id': 1, 'display_name': 'Acme BYO',
                'base_url': 'https://api.acme.example/v1',
                'models': ['gpt-5.6'], 'secret_key': 'sk-x',
            })
        self.assertEqual(resp.status_code, 502)
        ref = HiveOrgProviderReference.objects.get(id=resp.data['ref']['id'])
        self.assertEqual(ref.status, HiveOrgProviderReference.Status.VERIFYING)
        self.assertEqual(ref.last_validation_result, 'rate_limited')

    def test_provision_denies_non_owner_member(self):
        with patch(f'{VIEWS}._cortex_base', return_value='http://cortex'):
            resp = self._provision(_user(org_ids=[1]), {
                'org_id': 1, 'display_name': 'Acme BYO',
                'base_url': 'https://api.acme.example/v1',
                'models': ['gpt-5.6'], 'secret_key': 'sk-x',
            })
        self.assertEqual(resp.status_code, 403)

    def test_provision_requires_base_url_for_openai_compatible(self):
        resp = self._provision(self._owner(), {
            'org_id': 1, 'provider_type': 'openai_compatible',
            'display_name': 'Acme BYO', 'models': ['gpt-5.6'], 'secret_key': 'sk-x',
        })
        self.assertEqual(resp.status_code, 400)

    def test_provision_requires_secret_key(self):
        resp = self._provision(self._owner(), {
            'org_id': 1, 'display_name': 'Acme BYO',
            'base_url': 'https://api.acme.example/v1', 'models': ['gpt-5.6'],
        })
        self.assertEqual(resp.status_code, 400)


class RotateTests(LifecycleTestBase):
    def test_rotate_happy_path_refreshes_mirror(self):
        fake, calls = self._scripted_post([
            ({'validation_state': {'provider_id': 'cred-abc'}}, None),
            ({'status': 'active'}, None),
        ])
        with patch(f'{VIEWS}._cortex_post', fake), \
             patch(f'{VIEWS}._cortex_base', return_value='http://cortex'), \
             patch(f'{VIEWS}._cortex_headers', return_value={}):
            resp = self._rotate(self._owner(), self.ref.id, {'secret_key': 'sk-new'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['rotated'])
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.status, HiveOrgProviderReference.Status.ACTIVE)
        self.assertEqual(self.ref.last_validation_result, 'ok')
        self.assertEqual(calls[0]['body']['api_key'], 'sk-new')

    def test_rotate_failure_marks_invalid(self):
        fake, calls = self._scripted_post([
            (None, {'error': 'Cortex provider request failed.',
                    'detail': {'detail': 'revoked'}, 'status': 401}),
        ])
        with patch(f'{VIEWS}._cortex_post', fake), \
             patch(f'{VIEWS}._cortex_base', return_value='http://cortex'), \
             patch(f'{VIEWS}._cortex_headers', return_value={}):
            resp = self._rotate(self._owner(), self.ref.id, {'secret_key': 'sk-bad'})
        self.assertEqual(resp.status_code, 502)
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.status, HiveOrgProviderReference.Status.INVALID)
        self.assertEqual(self.ref.last_validation_result, 'auth_failed')

    def test_rotate_denies_other_org_member(self):
        with patch(f'{VIEWS}._cortex_base', return_value='http://cortex'):
            resp = self._rotate(_user(org_ids=[2], roles={'2': 'owner'}),
                                self.ref.id, {'secret_key': 'sk-x'})
        self.assertEqual(resp.status_code, 403)


class DisableTests(LifecycleTestBase):
    def test_disable_sets_disabled_status(self):
        resp = self._disable(self._owner(), self.ref.id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['disabled'])
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.status, HiveOrgProviderReference.Status.DISABLED)

    def test_disable_denies_other_org_member(self):
        resp = self._disable(_user(org_ids=[2], roles={'2': 'owner'}), self.ref.id)
        self.assertEqual(resp.status_code, 403)


class DeleteTests(LifecycleTestBase):
    """DELETE /v1/org-provider-refs/<id> — mirror-only remove, org-owner gated.

    The destructive endpoint must hold the same isolation invariants as
    disable: only org owners/admins of the row's own org may remove it, and
    the removal is mirror-only (Cortex state is untouched).
    """

    def test_delete_happy_path_removes_mirror_row(self):
        resp = self._remove(self._owner(), self.ref.id)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(
            HiveOrgProviderReference.objects.filter(id=self.ref.id).exists()
        )

    def test_delete_denies_non_owner_member(self):
        resp = self._remove(_user(org_ids=[1], roles={'1': 'member'}), self.ref.id)
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            HiveOrgProviderReference.objects.filter(id=self.ref.id).exists()
        )

    def test_delete_denies_other_org_member(self):
        resp = self._remove(_user(org_ids=[2], roles={'2': 'owner'}), self.ref.id)
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            HiveOrgProviderReference.objects.filter(id=self.ref.id).exists()
        )

    def test_delete_missing_ref_returns_404(self):
        missing = uuid4()
        resp = self._remove(self._owner(), missing)
        self.assertEqual(resp.status_code, 404)


class OverrideInvariantTests(LifecycleTestBase):
    def test_cross_org_override_rejected_at_write_time(self):
        resp = self._upsert(self._owner(), {
            'org_id': 1, 'cortex_provider_id': str(self.provider_id),
            'per_agent_overrides': {
                'agent-7': {'provider_credential_id': str(self.credential_provider_id)},
            },
        })
        self.assertEqual(resp.status_code, 403)
        self.assertIn('cross-org', str(resp.data['error']))

    def test_unknown_override_reference_rejected(self):
        resp = self._upsert(self._owner(), {
            'org_id': 1, 'cortex_provider_id': str(self.provider_id),
            'per_agent_overrides': {
                'agent-7': {'provider_credential_id': str(uuid4())},
            },
        })
        self.assertEqual(resp.status_code, 400)

    def test_same_org_override_accepted(self):
        resp = self._upsert(self._owner(), {
            'org_id': 1, 'cortex_provider_id': str(self.provider_id),
            'per_agent_overrides': {
                'agent-7': {'provider_credential_id': str(self.provider_id),
                            'model_id': 'gpt-5.6'},
            },
        })
        self.assertEqual(resp.status_code, 200)
        self.ref.refresh_from_db()
        self.assertEqual(
            self.ref.per_agent_overrides['agent-7']['provider_credential_id'],
            str(self.provider_id),
        )


class ReviewFixTests(LifecycleTestBase):
    """kumo's slice-1 review (Origin review 4399): must-fix + advisories."""

    def test_canonical_provider_maps_wellknown_endpoint_not_display_name(self):
        """MUST-FIX: openai/anthropic without base_url map to the canonical
        endpoint — never the display name (Cortex would reject it or
        materialize a bogus endpoint). Also asserts the models pass-through
        (advisory: verify the models schema at build time)."""
        fake, calls = self._scripted_post([
            ({'id': str(self.listing_id)}, None),
            ({'validation_state': {'provider_id': 'cred-x'}}, None),
            ({'status': 'probation'}, None),
        ])
        with patch(f'{VIEWS}._cortex_post', fake), \
             patch(f'{VIEWS}._cortex_base', return_value='http://cortex'), \
             patch(f'{VIEWS}._cortex_headers', return_value={}):
            resp = self._provision(self._owner(), {
                'org_id': 1, 'provider_type': 'openai',
                'display_name': 'Acme OpenAI', 'models': ['gpt-5.6'],
                'secret_key': 'sk-x',
            })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(calls[0]['body']['endpoint_url'], 'https://api.openai.com/v1')
        self.assertEqual(calls[0]['body']['models'], ['gpt-5.6'])

    def test_failure_detail_echoing_secret_is_scrubbed(self):
        """Advisory: self-contained redaction — a provider error payload that
        echoes the request body must not leak the relayed secret."""
        secret = 'sk-echoed-in-error'
        fake, calls = self._scripted_post([
            ({'id': str(self.listing_id)}, None),
            (None, {'error': 'Cortex provider request failed.',
                    'detail': {'detail': f'invalid key {secret} provided',
                               'request': {'api_key': secret}},
                    'status': 401}),
        ])
        with patch(f'{VIEWS}._cortex_post', fake), \
             patch(f'{VIEWS}._cortex_base', return_value='http://cortex'), \
             patch(f'{VIEWS}._cortex_headers', return_value={}):
            resp = self._provision(self._owner(), {
                'org_id': 1, 'display_name': 'Acme BYO',
                'base_url': 'https://api.acme.example/v1',
                'models': ['gpt-5.6'], 'secret_key': secret,
            })
        self.assertEqual(resp.status_code, 502)
        self.assertNotIn(secret, json.dumps(resp.data))
        self.assertIn('[redacted]', json.dumps(resp.data))

    def test_rotate_preserves_disabled_status(self):
        """Advisory: rotation replaces the key but must not silently
        re-activate a disabled credential."""
        self.ref.status = HiveOrgProviderReference.Status.DISABLED
        self.ref.save(update_fields=['status'])
        fake, calls = self._scripted_post([
            ({'validation_state': {'provider_id': 'cred-abc'}}, None),
            ({'status': 'active'}, None),
        ])
        with patch(f'{VIEWS}._cortex_post', fake), \
             patch(f'{VIEWS}._cortex_base', return_value='http://cortex'), \
             patch(f'{VIEWS}._cortex_headers', return_value={}):
            resp = self._rotate(self._owner(), self.ref.id, {'secret_key': 'sk-new'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['rotated'])
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.status, HiveOrgProviderReference.Status.DISABLED)
        self.assertEqual(self.ref.last_validation_result, 'ok')


class CarriedSlice2NotesTests(LifecycleTestBase):
    """The two non-blocking notes carried from the slice-2 review (kumo),
    landing in this slice's backend suite per the gate decision."""

    def test_rotate_on_disabled_is_never_served_end_to_end(self):
        """E2E assert: rotation replaces the key but the credential stays
        resolution-ineligible end-to-end — the runtime-honoring chain refuses
        a disabled credential (no_provider_configured), never serves it."""
        from fleet import provider_resolution as pr

        self.ref.status = HiveOrgProviderReference.Status.DISABLED
        self.ref.save(update_fields=['status'])
        fake, calls = self._scripted_post([
            ({'validation_state': {'provider_id': 'cred-abc'}}, None),
            ({'status': 'active'}, None),
        ])
        with patch(f'{VIEWS}._cortex_post', fake), \
             patch(f'{VIEWS}._cortex_base', return_value='http://cortex'), \
             patch(f'{VIEWS}._cortex_headers', return_value={}):
            resp = self._rotate(self._owner(), self.ref.id, {'secret_key': 'sk-new'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['rotated'])
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.status, HiveOrgProviderReference.Status.DISABLED)

        # The runtime-honoring leg: the rotated-but-disabled credential is
        # typed unavailable with its OWN reason (F1: provider_disabled, never
        # silently no_provider_configured) — never ok, never a wiring hold.
        resolution = pr.resolve_model(
            organization_id=self.ref.org_id, runtime_class='claude-bridge',
            reference=self.ref,
        )
        self.assertFalse(resolution.ok)
        self.assertEqual(resolution.reason, pr.REASON_PROVIDER_DISABLED)
        self.assertNotEqual(resolution.reason, pr.REASON_NO_PROVIDER_CONFIGURED)
        self.assertFalse(resolution.lease_wiring_pending)
        self.assertEqual(resolution.model_id, '')

        # True end-to-end leg (N2, kumo review 2026-09-12): the WAKE gate
        # holds for the rotated-but-disabled credential — the agent never
        # starts, on the org's provider or on Shizuha-owned keys. The wake
        # gate resolves adjacency from the roster edge and bridges slug→org
        # via the registry hook (unwired in tests — patched here the same way
        # the registry slice will wire it in production).
        from unittest.mock import patch as _patch
        from fleet.models import FleetAgent, TeamMembershipRole
        from fleet import provisioner
        TeamMembershipRole.objects.create(
            agent_identifier='gin', team_slug='build-pipeline',
            organization_slug='acme', enabled=True,
        )
        agent = FleetAgent(agent_username='gin')
        with _patch.object(pr, 'ORG_SLUG_LOOKUP', lambda slug: self.ref.org_id):
            decision = provisioner._lease_start_decision(agent)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, pr.REASON_PROVIDER_DISABLED)

    def test_lifecycle_never_persists_or_surfaces_plaintext_keys(self):
        """Server-side redaction assertion: across the full lifecycle
        (provision → rotate → disable → delete) the plaintext keys appear
        nowhere — not in any response payload, not in any persisted mirror
        field, not in any row in the DB."""
        from fleet.models import HiveOrgProviderReference

        alpha = 'sk-live-alpha-9f2c41do-not-leak'
        beta = 'sk-live-beta-7b81e3also-secret'

        def assert_no_key_material(response):
            blob = json.dumps(getattr(response, 'data', {}), default=str)
            self.assertNotIn(alpha, blob)
            self.assertNotIn(beta, blob)

        # ── provision ──
        fake, calls = self._scripted_post([
            ({'id': str(self.listing_id), 'status': 'draft'}, None),
            ({'id': str(self.listing_id),
              'validation_state': {'provider_id': 'cred-new'},
              'status': 'verifying'}, None),
            ({'id': str(self.listing_id), 'status': 'probation',
              'models': [{'model': 'gpt-5.6'}]}, None),
        ])
        with patch(f'{VIEWS}._cortex_post', fake), \
             patch(f'{VIEWS}._cortex_base', return_value='http://cortex'), \
             patch(f'{VIEWS}._cortex_headers', return_value={}):
            resp = self._provision(self._owner(), {
                'org_id': 1, 'provider_type': 'openai_compatible',
                'display_name': 'Sweep provider',
                'base_url': 'https://api.sweep.example/v1',
                'models': [{'model': 'gpt-5.6'}],
                'secret_key': alpha,
            })
        self.assertEqual(resp.status_code, 201)
        assert_no_key_material(resp)
        sweep_ref = HiveOrgProviderReference.objects.get(
            id=resp.data['ref']['id'],
        )

        # ── rotate ──
        fake, calls = self._scripted_post([
            ({'validation_state': {'provider_id': 'cred-new'}}, None),
            ({'status': 'active'}, None),
        ])
        with patch(f'{VIEWS}._cortex_post', fake), \
             patch(f'{VIEWS}._cortex_base', return_value='http://cortex'), \
             patch(f'{VIEWS}._cortex_headers', return_value={}):
            resp = self._rotate(self._owner(), sweep_ref.id, {'secret_key': beta})
        self.assertEqual(resp.status_code, 200)
        assert_no_key_material(resp)

        # ── disable ──
        resp = self._disable(self._owner(), sweep_ref.id)
        self.assertEqual(resp.status_code, 200)
        assert_no_key_material(resp)

        # ── server-side sweep: every persisted field of every row ──
        for ref in HiveOrgProviderReference.objects.all():
            for field in HiveOrgProviderReference._meta.get_fields():
                if not getattr(field, 'concrete', False) or field.primary_key:
                    continue
                value = getattr(ref, field.name, None)
                if value is None:
                    continue
                self.assertNotIn(alpha, str(value))
                self.assertNotIn(beta, str(value))

        # ── delete ──
        resp = self._remove(self._owner(), sweep_ref.id)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(
            HiveOrgProviderReference.objects.filter(id=sweep_ref.id).exists(),
        )
