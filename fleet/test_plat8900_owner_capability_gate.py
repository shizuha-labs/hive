"""PLAT-8900: owner JWT may read its own runtime lane; others stay 403."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from fleet import views


class _User:
    def __init__(self, user_id, *, authenticated=True, staff=False):
        self.id = user_id
        self.is_authenticated = authenticated
        self.is_staff = staff


class OwnerCapabilityGateTests(SimpleTestCase):
    def test_owner_matches_immutable_id(self):
        request = SimpleNamespace(user=_User(7), META={})
        agent = SimpleNamespace(owner_id=7)
        with patch('fleet.runtime_fleet._find_runtime_agent_row', return_value=agent):
            self.assertIsNone(views._require_staff_daemon_or_owner(request, 'agent-1'))

    def test_other_user_stays_staff_required(self):
        request = SimpleNamespace(user=_User(8), META={})
        agent = SimpleNamespace(owner_id=7)
        with patch('fleet.runtime_fleet._find_runtime_agent_row', return_value=agent):
            denied = views._require_staff_daemon_or_owner(request, 'agent-1')
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.data['error'], 'Staff access required')

    def test_null_owner_is_not_owned_by_anyone(self):
        request = SimpleNamespace(user=_User(7), META={})
        agent = SimpleNamespace(owner_id=None)
        with patch('fleet.runtime_fleet._find_runtime_agent_row', return_value=agent):
            denied = views._require_staff_daemon_or_owner(request, 'agent-1')
        self.assertEqual(denied.status_code, 403)

    def test_anonymous_is_denied(self):
        request = SimpleNamespace(user=_User(7, authenticated=False), META={})
        with patch('fleet.runtime_fleet._find_runtime_agent_row') as finder:
            denied = views._require_staff_daemon_or_owner(request, 'agent-1')
        finder.assert_not_called()
        self.assertEqual(denied.status_code, 403)
