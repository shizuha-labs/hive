"""Opaque acknowledgements for Hive-authored daemon configuration.

Values may contain credentials or merely look secret enough for the runtime's
redaction policy.  The daemon therefore echoes only these per-key fingerprints
after its authoritative state store has accepted a config update.  Hive clears
a pending key only when the echoed fingerprint still matches the latest desired
value, so a late acknowledgement cannot clear a newer edit.
"""

import hashlib
import json


def config_value_fingerprint(value) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def config_fingerprints(config: dict) -> dict[str, str]:
    return {
        str(key): config_value_fingerprint(value)
        for key, value in (config or {}).items()
    }
