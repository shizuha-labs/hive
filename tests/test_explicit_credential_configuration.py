"""Real settings imports must not invent signing or database credentials."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "hive_project/settings.py"
PASSWORD_ENV = "DB_PASSWORD"
CHILD = """import hashlib,json,os,runpy
s=runpy.run_path(os.environ['CONFIG_FILE'])
print(json.dumps({'key_digest':hashlib.sha256(s['SECRET_KEY'].encode()).hexdigest(),
                  'db_empty':s['DATABASES']['default']['PASSWORD']==''}))
"""


class ExplicitCredentialConfigurationTests(unittest.TestCase):
    def settings(self, **values):
        env = dict(os.environ)
        for key in ('JWT_SECRET_KEY', 'SECRET_KEY', PASSWORD_ENV):
            env.pop(key, None)
        env.update(values)
        env.update(CONFIG_FILE=str(SETTINGS), OTEL_ENABLED='false')
        return subprocess.run([sys.executable, '-c', CHILD], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=30)

    def test_missing_signing_keys_refuses_actual_settings_import(self):
        result = self.settings()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('JWT_SECRET_KEY or SECRET_KEY', result.stderr)

    def test_explicit_empty_or_whitespace_keys_refuse(self):
        for values in ({'JWT_SECRET_KEY': '', 'SECRET_KEY': ''},
                       {'JWT_SECRET_KEY': '   ', 'SECRET_KEY': ''}):
            with self.subTest(values_present=tuple(values)):
                self.assertNotEqual(self.settings(**values).returncode, 0)

    def test_configured_secret_key_and_empty_database_password(self):
        import hashlib
        key = secrets.token_urlsafe(48)
        result = self.settings(SECRET_KEY=key)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'key_digest': hashlib.sha256(key.encode()).hexdigest(), 'db_empty': True})

    def test_jwt_key_retains_precedence(self):
        import hashlib
        key = secrets.token_urlsafe(48)
        result = self.settings(JWT_SECRET_KEY=key, SECRET_KEY=secrets.token_urlsafe(48))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['key_digest'], hashlib.sha256(key.encode()).hexdigest())

    def test_explicit_empty_jwt_key_cannot_silently_switch_signers(self):
        self.assertNotEqual(self.settings(JWT_SECRET_KEY='', SECRET_KEY=secrets.token_urlsafe(48)).returncode, 0)


if __name__ == '__main__':
    unittest.main()
