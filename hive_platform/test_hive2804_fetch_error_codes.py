import unittest

from hive_platform.quota_alert_conditions import is_fetch_error_detail


class FetchErrorCodeTests(unittest.TestCase):
    def test_projected_failure_codes_count(self):
        for code in ('request_failed', 'http_500', 'quota_refresh_failed', 'timeout'):
            self.assertTrue(is_fetch_error_detail(code), code)

    def test_ok_and_auth_do_not_count_as_fetch_error(self):
        self.assertFalse(is_fetch_error_detail('ok'))
        self.assertFalse(is_fetch_error_detail('auth_failed'))
        self.assertFalse(is_fetch_error_detail('http_429'))
