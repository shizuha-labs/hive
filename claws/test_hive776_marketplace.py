from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from .marketplace import transition_marketplace_template
from .models import MarketplaceTemplate, MarketplaceTemplateAuditEvent, OrgTemplate
from .views import MarketplaceTemplateDetailView, MarketplaceTemplateListView


class MarketplaceTemplateTests(TestCase):
    def setUp(self):
        self.base = OrgTemplate.objects.create(slug='support-pod', version=1, name='Support Pod')
        self.entry = MarketplaceTemplate.objects.create(
            slug='support-pod', version='1.0.0', base_template=self.base,
            publisher='publisher@shizuha.com', visibility=MarketplaceTemplate.Visibility.PUBLIC,
        )
        self.factory = APIRequestFactory()

    def test_lifecycle_is_enforced_and_audited(self):
        transition_marketplace_template(self.entry.id, target='reviewed', actor='reviewer@shizuha.com')
        transition_marketplace_template(self.entry.id, target='published', actor='publisher@shizuha.com')
        transition_marketplace_template(self.entry.id, target='deprecated', actor='publisher@shizuha.com')
        self.assertEqual(
            list(MarketplaceTemplateAuditEvent.objects.values_list('event_type', flat=True)),
            ['reviewed', 'published', 'deprecated'],
        )
        with self.assertRaises(ValidationError):
            transition_marketplace_template(self.entry.id, target='published', actor='publisher@shizuha.com')

    def test_sensitive_access_flag_cannot_publish(self):
        self.entry.risk_flags = ['sensitive_access_flags:kubeconfig']
        self.entry.save(update_fields=['risk_flags'])
        transition_marketplace_template(self.entry.id, target='reviewed', actor='reviewer@shizuha.com')
        with self.assertRaisesMessage(ValidationError, 'cannot publish'):
            transition_marketplace_template(self.entry.id, target='published', actor='publisher@shizuha.com')
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, MarketplaceTemplate.Status.REVIEWED)
        self.assertEqual(self.entry.audit_events.count(), 1)

    def test_catalog_visibility_is_server_side_and_deprecated_is_hidden(self):
        transition_marketplace_template(self.entry.id, target='reviewed', actor='reviewer@shizuha.com')
        transition_marketplace_template(self.entry.id, target='published', actor='publisher@shizuha.com')
        internal_base = OrgTemplate.objects.create(slug='internal-pod', version=1, name='Internal')
        internal = MarketplaceTemplate.objects.create(
            slug='internal-pod', version='1.0.0', base_template=internal_base,
            publisher='publisher@shizuha.com', visibility=MarketplaceTemplate.Visibility.INTERNAL,
        )
        transition_marketplace_template(internal.id, target='reviewed', actor='reviewer@shizuha.com')
        transition_marketplace_template(internal.id, target='published', actor='publisher@shizuha.com')

        user = SimpleNamespace(is_authenticated=True, is_staff=False, entitlements=[])
        request = self.factory.get('/api/v1/marketplace/templates')
        force_authenticate(request, user=user)
        response = MarketplaceTemplateListView.as_view()(request)
        self.assertEqual([row['slug'] for row in response.data], ['support-pod'])

        request = self.factory.get('/api/v1/marketplace/templates/internal-pod')
        force_authenticate(request, user=user)
        self.assertEqual(MarketplaceTemplateDetailView.as_view()(request, template_slug='internal-pod').status_code, 404)

        transition_marketplace_template(self.entry.id, target='deprecated', actor='publisher@shizuha.com')
        request = self.factory.get('/api/v1/marketplace/templates')
        force_authenticate(request, user=user)
        self.assertEqual(MarketplaceTemplateListView.as_view()(request).data, [])

    def test_published_version_is_protected_by_wrapper_and_base_relationship(self):
        transition_marketplace_template(self.entry.id, target='reviewed', actor='reviewer@shizuha.com')
        transition_marketplace_template(self.entry.id, target='published', actor='publisher@shizuha.com')
        self.entry.refresh_from_db()
        self.entry.required_inputs = ['mutated']
        with self.assertRaisesMessage(ValidationError, 'published versions are immutable'):
            self.entry.save()
        with self.assertRaises(Exception):
            OrgTemplate.objects.filter(id=self.base.id).delete()
