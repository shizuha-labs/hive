"""Re-export shim (ADR-0080 / HIVE-2021).

The canonical implementation moved to hive_platform.token_revocation. This
shim keeps legacy importers/tests working; new code should import from
hive_platform directly.
"""
from hive_platform import token_revocation as _tr
from hive_platform.token_revocation import *  # noqa: F401,F403
globals().update({k: v for k, v in vars(_tr).items() if k.startswith('_') and not k.startswith('__')})
