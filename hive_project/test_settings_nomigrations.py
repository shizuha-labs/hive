"""SQLite settings with data migrations disabled (tables built from models).

hive_project.test_settings is broken for migrations >=0050: they store
timezone-aware ISO strings (e.g. "2026-08-20T00:00:00Z") while the project
deliberately runs USE_TZ=False, so create_test_db fails on SQLite. This
variant disables migrations for the project apps so lightweight unit tests
run against tables built straight from the current models.
"""
from .test_settings import *  # noqa: F401,F403

MIGRATION_MODULES = {
    'claws': None,
    'hive_platform': None,
    'fleet': None,
    'waitlist': None,
    'analytics': None,
    'credentials': None,
}
