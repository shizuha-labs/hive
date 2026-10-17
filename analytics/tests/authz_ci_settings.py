"""HIVE-393: minimal settings to run the analytics ingest isolation suite in
isolation (no live k8s/redis/heavy hive apps). sqlite + disabled migrations."""
SECRET_KEY = 'hive393-analytics-ci-secret-key-0123456789'
DEBUG = False
INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'rest_framework',
    'analytics',
]
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
ROOT_URLCONF = 'analytics.urls'
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'UNAUTHENTICATED_USER': None,
    'DEFAULT_THROTTLE_RATES': {
        'analytics-ingest': '120/min',
        'discovery-feedback-ingest': '30/min',
    },
}


class _DisableMigrations:
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = _DisableMigrations()
