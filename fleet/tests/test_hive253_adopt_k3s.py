"""HIVE-253 P4: tests for adopt_fleet_k3s identity validation (INV-ID §4.3)."""
import unittest


# Mirror the identity validation logic inline (avoids Django setup).
class _FakeAgent:
    def __init__(self, agent_id='agent-001'):
        self.agent_id = agent_id


def _validate_identity(username, agent, running_usernames, id_data=None):
    """Mirror of Command._validate_identity for pure-Python unit tests."""
    if id_data is not None:
        if not id_data.get('exists', False):
            return 'reject', f'no Shizuha-ID record found for {username}'
        account_type = id_data.get('account_type', 'unknown')
        if account_type != 'agent':
            return 'reject', f'account_type={account_type!r} (must be "agent", INV-ID §4.3)'
        is_active = id_data.get('is_active', None)
        if is_active is False:
            return 'reject', f'Shizuha-ID account is inactive (is_active=False, INV-ID §4.3)'

    if not agent.agent_id:
        return 'admit', 'no agent_id — admitting with warning'

    return 'admit', 'identity checks passed'


class TestAdoptIdentityValidation(unittest.TestCase):
    """Tests for INV-ID adoption checks."""

    def test_normal_agent_admitted(self):
        verdict, _ = _validate_identity('fumi', _FakeAgent(), {'fumi'})
        self.assertEqual(verdict, 'admit')

    def test_missing_agent_id_admitted_with_warning(self):
        verdict, reason = _validate_identity('banto', _FakeAgent(agent_id=''), {'banto'})
        self.assertEqual(verdict, 'admit')
        self.assertIn('agent_id', reason)

    # ------------------------------------------------------------------
    # Shizuha-ID API checks (P2-1 — INV-ID §4.3(b)/(c))
    # ------------------------------------------------------------------

    def test_non_agent_account_type_rejected(self):
        """account_type='user' must be REJECTED (INV-ID §4.3(b))."""
        id_data = {'exists': True, 'account_type': 'user', 'is_active': True}
        verdict, reason = _validate_identity('banto', _FakeAgent(), set(), id_data)
        self.assertEqual(verdict, 'reject')
        self.assertIn('account_type', reason)
        self.assertIn('user', reason)

    def test_service_account_type_rejected(self):
        """account_type='service' must be REJECTED."""
        id_data = {'exists': True, 'account_type': 'service', 'is_active': True}
        verdict, reason = _validate_identity('banto', _FakeAgent(), set(), id_data)
        self.assertEqual(verdict, 'reject')
        self.assertIn('account_type', reason)

    def test_inactive_agent_rejected(self):
        """is_active=False must be REJECTED (INV-ID §4.3(c))."""
        id_data = {'exists': True, 'account_type': 'agent', 'is_active': False}
        verdict, reason = _validate_identity('fumi', _FakeAgent(), set(), id_data)
        self.assertEqual(verdict, 'reject')
        self.assertIn('inactive', reason)

    def test_active_agent_admitted(self):
        """account_type='agent', is_active=True → ADMIT."""
        id_data = {'exists': True, 'account_type': 'agent', 'is_active': True}
        verdict, _ = _validate_identity('fumi', _FakeAgent(), set(), id_data)
        self.assertEqual(verdict, 'admit')

    def test_no_shizuha_id_record_rejected(self):
        """exists=False (no Shizuha-ID row) → REJECT."""
        id_data = {'exists': False}
        verdict, reason = _validate_identity('ghost', _FakeAgent(), set(), id_data)
        self.assertEqual(verdict, 'reject')
        self.assertIn('Shizuha-ID', reason)

    def test_id_api_unavailable_lenient(self):
        """id_data=None (--skip-id-api or API error) → no rejection on account_type/is_active."""
        verdict, _ = _validate_identity('fumi', _FakeAgent(), set(), id_data=None)
        self.assertEqual(verdict, 'admit')


if __name__ == '__main__':
    unittest.main()
