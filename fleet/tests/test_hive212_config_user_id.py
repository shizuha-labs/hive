"""Regression tests for HIVE-212.

Hive's top-down config push (`configure_agent_via_connect`) keyed the Connect
internal config POST by `username`. BYO/daemon-registered agents cannot be
resolved by username on Connect's side, so the POST returned 400 and the config
never reached the agent. The fix resolves the numeric platform `agent_user_id`
and sends that (preferred), falling back to `username` only when it cannot be
resolved. Tests mock the id lookup, connect base URL, and requests.post.
"""
import unittest
from unittest import mock

from fleet import external_agents


class ConfigPushAgentUserIdTest(unittest.TestCase):
    def _run(self, id_get_return, username='Scout', updates=None):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured['url'] = url
            captured['json'] = json
            return mock.Mock(status_code=200, text='')

        with mock.patch.object(external_agents, '_id_get', return_value=id_get_return), \
                mock.patch.object(external_agents, '_connect_internal_base',
                                  return_value='http://connect'), \
                mock.patch.object(external_agents.requests, 'post', side_effect=fake_post):
            ok, err = external_agents.configure_agent_via_connect(
                username, {'model': 'x'} if updates is None else updates)
        return ok, err, captured

    def test_byo_agent_sends_numeric_agent_user_id(self):
        ok, err, cap = self._run({'exists': True, 'account_type': 'agent', 'user_id': 74})
        self.assertTrue(ok, err)
        self.assertEqual(cap['json'].get('agent_user_id'), 74)
        self.assertNotIn('username', cap['json'])
        self.assertEqual(cap['json'].get('config'), {'model': 'x'})

    def test_user_id_coerced_to_int(self):
        ok, err, cap = self._run({'user_id': '74'})
        self.assertTrue(ok, err)
        self.assertEqual(cap['json'].get('agent_user_id'), 74)

    def test_fallback_to_username_when_id_unresolved(self):
        ok, err, cap = self._run(None)
        self.assertTrue(ok, err)
        self.assertEqual(cap['json'].get('username'), 'scout')
        self.assertNotIn('agent_user_id', cap['json'])

    def test_empty_updates_short_circuits_without_network(self):
        with mock.patch.object(external_agents, '_id_get') as id_get, \
                mock.patch.object(external_agents.requests, 'post') as post:
            ok, err = external_agents.configure_agent_via_connect('scout', {})
        self.assertTrue(ok)
        id_get.assert_not_called()
        post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
