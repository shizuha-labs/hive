"""Re-export shim (ADR-0080 / HIVE-2021).

The canonical implementation moved to hive_platform.coordinator. This shim keeps
historical data migrations (e.g. claws/0027, 0039) and any legacy importers
working; new code should import from hive_platform directly.
"""
from hive_platform.coordinator import *  # noqa: F401,F403
