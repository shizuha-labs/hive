"""Re-export shim (ADR-0080 / HIVE-2021).

The canonical implementation moved to hive_platform.token_coordinator. This
shim keeps historical data migrations (e.g. claws/0027, 0039) and any legacy
importers/tests working; new code should import from hive_platform directly.
"""
from hive_platform import token_coordinator as _tc
from hive_platform.token_coordinator import *  # noqa: F401,F403
globals().update({k: v for k, v in vars(_tc).items() if k.startswith('_') and not k.startswith('__')})
