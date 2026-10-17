"""HIVE-331: MCP ref validation must not be circular.

The capability API used the union of mcp_servers across currently-running
agents as its validity universe, so a service no agent had attached could
never be (re-)added — the chicken-and-egg lockout that broke PLAT-1274
(books/drive stripped from accounting, un-restorable via API).
"""
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory

from fleet.models import (
    CapabilityDefinition,
    ensure_team_required_capability,
)
from fleet.views import CapabilityDetailView, CapabilityListView


class _OpenCapabilityListView(CapabilityListView):
    authentication_classes = []
    permission_classes = []


class _OpenCapabilityDetailView(CapabilityDetailView):
    authentication_classes = []
    permission_classes = []


@patch('fleet.views.list_runtime_agents', return_value=[{'mcp_servers': [{'slug': 'pulse'}]}])
@patch('fleet.views.get_runtime_skills', return_value=['engineering', 'devops'])
class Hive331McpRefValidationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def test_platform_registry_service_accepted_when_no_agent_has_it(self, _skills, _agents):
        """PLAT-1274 scenario: 'books'/'drive' attached to no agent must still validate."""
        cap, _ = CapabilityDefinition.objects.update_or_create(
            slug='accounting',
            defaults={
                'display_name': 'Accounting',
                'activation_status': CapabilityDefinition.ActivationStatus.ACTIVE,
                'mcp_server_refs': ['pulse'],
                'skill_refs': [],
            },
        )
        req = self.factory.patch(
            f'/v1/fleet/capabilities/{cap.slug}',
            {'mcp_server_refs': ['pulse', 'books', 'drive']},
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenCapabilityDetailView.as_view()(req, slug=cap.slug)
        self.assertEqual(resp.status_code, 200, resp.data)
        cap.refresh_from_db()
        self.assertEqual(cap.mcp_server_refs, ['pulse', 'books', 'drive'])

    def test_genuinely_unknown_mcp_ref_still_rejected(self, _skills, _agents):
        req = self.factory.post(
            '/v1/fleet/capabilities/',
            {
                'slug': 'bad-mcp-capability',
                'display_name': 'Bad MCP Capability',
                'mcp_server_refs': ['not-a-real-service'],
            },
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenCapabilityListView.as_view()(req)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('mcp_server_refs', resp.data)

    def test_ref_already_stored_on_a_capability_row_stays_valid(self, _skills, _agents):
        """Lockout guard: a slug present on any existing row (e.g. restored via
        Django shell) is re-addable even if absent from registry + attachments."""
        CapabilityDefinition.objects.create(
            slug='legacy-holder',
            display_name='Legacy Holder',
            mcp_server_refs=['legacy-mcp'],
        )
        req = self.factory.post(
            '/v1/fleet/capabilities/',
            {
                'slug': 'legacy-consumer',
                'display_name': 'Legacy Consumer',
                'mcp_server_refs': ['legacy-mcp'],
            },
            format='json',
        )
        with patch('fleet.views._require_staff', return_value=None):
            resp = _OpenCapabilityListView.as_view()(req)
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_ensure_team_required_capability_does_not_drop_existing_refs(self, _skills, _agents):
        """Re-running ensure() (e.g. team re-created) must not clobber a
        hand-tuned bundle back to the spec/default."""
        CapabilityDefinition.objects.update_or_create(
            slug='analytics',
            defaults={
                'display_name': 'Analytics',
                'activation_status': CapabilityDefinition.ActivationStatus.ACTIVE,
                'mcp_server_refs': ['pulse', 'books', 'drive'],
            },
        )
        ensure_team_required_capability('analytics')
        cap = CapabilityDefinition.objects.get(slug='analytics')
        for ref in ('pulse', 'books', 'drive'):
            self.assertIn(ref, cap.mcp_server_refs)

    def test_accounting_spec_carries_books_and_drive(self, _skills, _agents):
        ensure_team_required_capability('accounting')
        cap = CapabilityDefinition.objects.get(slug='accounting')
        for ref in ('pulse', 'connect', 'wiki', 'admin', 'id', 'books', 'drive'):
            self.assertIn(ref, cap.mcp_server_refs)
