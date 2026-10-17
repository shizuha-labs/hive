"""Step-up verification seam for HIVE-4/HIVE-7 owner-destructive actions.

HIVE-4 §9.1 allows engineering to proceed with a stub consumer while HIVE-5
lands the Shizuha ID `/verify-step-up` contract. Production must use the
`shizuha-id` backend; the stub is intentionally limited to DEBUG/dev.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests
from django.conf import settings


@dataclass(frozen=True)
class StepUpScope:
    owner_id: int
    claw_id: str
    action: str


class StepUpVerificationError(Exception):
    """Raised when a step-up token cannot authorize the requested action."""


def verify_step_up_token(token: str, *, owner_id: int, claw_id: str, action: str) -> bool:
    """Verify an action-scoped step-up token.

    The dev/CI `stub` backend accepts any non-empty token so owner export and
    hard-delete flows can be shape-tested before HIVE-5 is live. It refuses to
    run when DEBUG is false, preserving the HIVE-4 invariant that production
    never accepts stub step-up tokens.
    """
    backend = getattr(settings, 'HIVE_STEPUP_BACKEND', 'stub')
    if backend == 'stub':
        if not getattr(settings, 'DEBUG', False):
            raise StepUpVerificationError('stub step-up backend is not allowed outside DEBUG')
        return bool((token or '').strip())
    if backend == 'shizuha-id':
        id_base = getattr(
            settings,
            'SHIZUHA_ID_INTERNAL_URL',
            'http://shizuha-id.shizuha.svc.cluster.local:8001',
        ).rstrip('/')
        verify_url = (
            getattr(settings, 'SHIZUHA_ID_STEPUP_VERIFY_URL', '')
            or f'{id_base}/api/internal/step-up/verify/'
        )
        try:
            resp = requests.post(
                verify_url,
                json={
                    'token': token,
                    'expected_scope': {
                        'owner_id': int(owner_id),
                        'resource_id': str(claw_id),
                        'action': action,
                    },
                },
                timeout=getattr(settings, 'SHIZUHA_ID_STEPUP_VERIFY_TIMEOUT_SECONDS', 5),
            )
        except (TypeError, ValueError) as exc:
            raise StepUpVerificationError(f'invalid step-up scope: {exc}') from exc
        except requests.RequestException as exc:
            raise StepUpVerificationError(f'shizuha-id step-up verifier unavailable: {exc}') from exc
        if resp.status_code >= 500:
            raise StepUpVerificationError(f'shizuha-id step-up verifier error: HTTP {resp.status_code}')
        try:
            body = resp.json() or {}
        except ValueError as exc:
            raise StepUpVerificationError('shizuha-id step-up verifier returned non-JSON') from exc
        if resp.status_code >= 400:
            raise StepUpVerificationError(
                body.get('error')
                or f'shizuha-id step-up verifier rejected request: HTTP {resp.status_code}'
            )
        return bool(body.get('valid'))
    raise StepUpVerificationError(f'unknown step-up backend: {backend}')
