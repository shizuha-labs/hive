"""Re-export shim (ADR-0080 / HIVE-2021).

The canonical implementation moved to hive_platform.coordinator_views. This
shim keeps legacy importers/tests working; new code should import from
hive_platform directly.
"""
from hive_platform import coordinator_views as _cv
from hive_platform.coordinator_views import *  # noqa: F401,F403
globals().update({k: v for k, v in vars(_cv).items() if k.startswith('_') and not k.startswith('__')})
