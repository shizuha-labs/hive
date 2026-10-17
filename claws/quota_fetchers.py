"""Re-export shim (ADR-0080 / HIVE-2021).

The canonical implementation moved to hive_platform.quota_fetchers. This shim keeps
historical data migrations and legacy importers working; new code should
import from hive_platform directly.
"""
from hive_platform.quota_fetchers import *  # noqa: F401,F403
