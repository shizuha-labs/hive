"""PLAT-4000: HMAC-sign the Hive effective-capabilities payload.

The fleet daemon (`shizuha-beta src/platform/effective-capabilities.ts`) fails
CLOSED on unsigned effective-capabilities payloads: unless the payload's HMAC
verifies, it withholds every `credentialGrantScopes` entry, all custom
credential-grant services, and sensitive MCP servers (e.g. `cron`). Hive is the
emitter, so Hive must sign the payload it returns from
`/v1/fleet/agents/<id>/effective-capabilities/`.

This module is a **byte-for-byte** port of the daemon's signer so the HMACs match
across the TS <-> Python trust boundary:

    daemon:  createHmac('sha256', secret).update(stableJson(withoutSignature(payload))).digest('hex')
    stableJson(v):
      Array  -> '[' + v.map(stableJson).join(',') + ']'          (order preserved)
      Object -> '{' + sortedKeys.map(k => JSON.stringify(k)+':'+stableJson(v[k])).join(',') + '}'
      scalar -> JSON.stringify(v)

The daemon verifies the RAW received JSON (it deletes only the `signature` key
before canonicalizing; camelCase/snake_case key-mapping happens *after*
verification), so Hive signs its own snake_case dict and the daemon matches.

Constraint (documented): the payload must contain only JSON-safe scalars --
strings, bools, null, and **integer** numbers. Floats are disallowed because
JS `JSON.stringify(1.0) === "1"` while Python `json.dumps(1.0) === "1.0"` would
diverge across the round-trip; capability payloads carry no floats. `sign_*`
raises if a float is encountered so a violation fails loud in tests, never
silently produces a non-verifying signature in prod.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

HMAC_SECRET_ENV = 'SHIZUHA_HIVE_CAPABILITY_HMAC_SECRET'
SIGNATURE_PREFIX = 'hmac_sha256:'


def _js_string(value: str) -> str:
    """Match JS `JSON.stringify(str)` byte-for-byte (used for scalars + object keys).

    Python `json.dumps(s, ensure_ascii=False)` matches JS `JSON.stringify` for
    strings: same short escapes, same `\\u00XX` for other control chars, non-ASCII
    left as UTF-8, `/` unescaped.
    """
    return json.dumps(value, ensure_ascii=False)


def stable_json(value: Any) -> str:
    """Port of the daemon's `stableJson` canonicalization."""
    # bool is an int subclass in Python -- must be checked before int/number.
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return 'null'
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(stable_json(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ','.join(
            _js_string(str(key)) + ':' + stable_json(value[key])
            for key in sorted(value.keys())
        ) + '}'
    if isinstance(value, float):
        raise ValueError(
            'capability_signing.stable_json: float values are not permitted in a '
            'signed effective-capabilities payload (JS/Python number stringify '
            f'diverges); got {value!r}. Use int or str.'
        )
    if isinstance(value, str):
        return _js_string(value)
    if isinstance(value, int):
        # JS JSON.stringify(int) == Python json.dumps(int) == decimal string.
        return json.dumps(value)
    raise ValueError(
        f'capability_signing.stable_json: unsupported value type {type(value).__name__} '
        f'({value!r}); serialize it to a JSON-safe scalar before signing.'
    )


def _without_signature(payload: dict) -> dict:
    """Shallow copy with the `signature` key removed (matches daemon `withoutSignature`)."""
    return {k: v for k, v in payload.items() if k != 'signature'}


def sign_effective_capabilities_payload(payload: dict, secret: str) -> str:
    """Return the HMAC-SHA256 hex digest over `stableJson(withoutSignature(payload))`.

    Byte-identical to the daemon's `signEffectiveCapabilitiesPayload`.
    """
    canonical = stable_json(_without_signature(payload)).encode('utf-8')
    return hmac.new(secret.encode('utf-8'), canonical, hashlib.sha256).hexdigest()


def resolve_hmac_secret(secret: str | None = None) -> str:
    """The shared HMAC secret: explicit arg, else the env var the daemon also reads."""
    if secret is not None:
        return secret
    return os.environ.get(HMAC_SECRET_ENV, '') or ''


def attach_effective_capabilities_signature(payload: dict, secret: str | None = None) -> dict:
    """Return the payload with a `signature: 'hmac_sha256:<hex>'` field attached.

    When no secret is configured (DevOps has not provisioned it to Hive yet), the
    payload is returned UNCHANGED (unsigned) -- preserving today's behavior so a
    missing secret degrades gracefully to the existing fail-closed withholding
    rather than breaking the endpoint. Once the shared secret is provisioned to
    both Hive and the rt-fleet daemon (same value), the payload is signed and the
    daemon stops withholding credential scopes / sensitive MCP.
    """
    resolved = resolve_hmac_secret(secret)
    if not resolved:
        return payload
    digest = sign_effective_capabilities_payload(payload, resolved)
    signed = dict(payload)
    signed['signature'] = f'{SIGNATURE_PREFIX}{digest}'
    return signed
