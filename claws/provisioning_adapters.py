"""HIVE-2715: concrete saga adapters for side-effectful provisioning steps.

The saga spine (``claws/tenant_provisioning.advance_preview``) fail-closes
every real side-effectful step at ``manual_review`` until a concrete adapter
is registered here. ``advance_preview`` dispatches to ``SAGA_ADAPTERS`` for
real (non-dry-run) runs and marks a step DONE only through
``provisioning_invariants.verify_step_result`` — the fail-closed seam that
stamps a VERIFIED resource ref or fails loud.

Honesty bridge semantics (``claws/golden_path_validation.py``): the
golden-path harness rides the ``blocked-saga-wip`` bridge ONLY when the saga
parks at the FIRST side-effectful step that lacks an adapter — derived from
this registry, so the bridge allowlist shrinks automatically as adapters land
(HIVE-2697 ruling 1 + the HIVE-2715 shrink constraint: once ``id_org`` has an
adapter, a park there REDs again). A park at any adaptered step is always a
real failure.

Service credentials least-privilege: adapters reuse the fleet membership
trust edge (``fleet/org_membership_sync`` helpers) — the Hive-signed internal
envelope bound to Admin's Hive-only verifier, scoped to the provisioning
surface only. No tenant JWT is ever accepted as service proof.
"""

from __future__ import annotations

import logging

import requests

from django.conf import settings

logger = logging.getLogger(__name__)

PROVISIONING_TIMEOUT_SECONDS = 10

# Ported from shizuha-id's validation-runtime refuse-by-default contract
# (HIVE-444 / HIVE-423 / HIVE-2697 ruling 2): the QA fixture signup domain set
# defines which runs keep their real resources deprovisionable.
QA_TEST_TENANT_DOMAIN = 'shizuha-test.local'


class SagaAdapterError(RuntimeError):
    """An adapter failed to complete its real side effect; the saga must not advance.

    Fail-loud by contract: the saga marks the step FAILED (never manual_review
    with the expected-pending marker, which would ride the honesty bridge) and
    never fakes success (the saga docstring contract — the failure mode the
    manual_review park exists to prevent).
    """


# --- least-privilege service client (single source of truth: fleet sync) ---

def _admin_base() -> str:
    from fleet.org_membership_sync import _admin_base
    return _admin_base()


def _signed_membership_request(url: str, payload: dict) -> tuple[bytes, dict[str, str]]:
    from fleet.org_membership_sync import _signed_membership_request as _signed
    return _signed(url, payload)


def _json_payload(response) -> dict:
    try:
        payload = response.json() or {}
    except ValueError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _redact(payload: dict) -> dict:
    redacted = {}
    for key, value in (payload or {}).items():
        if any(marker in key.lower() for marker in ('token', 'secret', 'signature', 'key')):
            redacted[key] = '<redacted>'
        else:
            redacted[key] = value
    return redacted


# --- QA-fixture refuse-by-default predicate (new resource class) ---

def _qa_test_tenant_domains() -> set[str]:
    domains = {QA_TEST_TENANT_DOMAIN}
    qa_domain = str(
        getattr(settings, 'QA_FIXTURE_SIGNUP_DOMAIN', '') or 'qafixture.shizuha.com'
    ).strip().lower()
    if qa_domain:
        domains.add(qa_domain)
    return domains


def is_qa_fixture_run(run) -> bool:
    """True only when the run's requester is PROVABLY a QA fixture account.

    Refuse-by-default: the saga's deprovision path may delete a real admin
    Organization only for these runs (HIVE-2697 cleanup contract extended to
    the organization resource class minted by the id_org adapter).
    """
    email = str(getattr(run, 'requester_email', '') or '').strip().lower()
    domain = email.rpartition('@')[2]
    return domain in _qa_test_tenant_domains()


# --- id_org adapter: the saga step the golden path actually parks at ---

def provision_id_org(run) -> dict:
    """Create/attach the Shizuha-ID organization + owner membership (real side effect).

    Acts on Admin via the Hive-scoped internal provisioning surface
    (``POST /api/internal/organizations/``), fail-closed signed envelope:
    ``X-Internal-Service: shizuha-hive`` + token + X-Hive-* HMAC + durable
    nonce + operation_id bound into the signed body.

    Idempotent + saga-resumable: the endpoint is atomic create-or-attach keyed
    on the unique slug and the operation_id binds this exact saga step, so a
    crash after the side effect but before the resource ref is recorded
    replays to the SAME org (the endpoint returns the existing row for the
    same immutable fields) — never a duplicate or an orphan.

    Returns the resource ref for ``verify_step_result``:
    ``{'verified': True, 'external_org_id': <real admin org id>, 'org_slug': ...}``.
    """
    operation_id = f'hive320:{run.idempotency_key}:id_org'
    url = f'{_admin_base()}/internal/organizations/'
    payload = {
        'operation_id': operation_id,
        'name': run.org_name,
        'slug': run.org_slug,
        'owner_id': int(run.requester_user_id),
        'owner_email': str(getattr(run, 'requester_email', '') or ''),
        'plan': 'free',
    }
    try:
        body, headers = _signed_membership_request(url, payload)
    except ValueError as exc:
        # Config-contract failure (e.g. unconfigured HIVE_SERVICE_SIGNING_KEY):
        # fail loud as an adapter error so advance_preview marks the step
        # FAILED — never leak a bare ValueError out of the API seam.
        raise SagaAdapterError(
            f'Provisioning trust edge is not configured: {exc}'
        ) from exc
    try:
        response = requests.post(
            url, data=body, headers=headers, timeout=PROVISIONING_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise SagaAdapterError(
            f'ID org provisioning unreachable: {exc.__class__.__name__}: {exc}'
        ) from exc

    org_payload = _json_payload(response)
    if response.status_code in (200, 201) and org_payload.get('id'):
        return {
            'verified': True,
            'external_org_id': str(org_payload['id']),
            'org_slug': run.org_slug,
            'display_name': run.org_name,
            # No 'service' provenance key here on purpose (CI lap-4 datum,
            # run 3135): the HIVE-354 marker scanner tokenizes ref VALUES and
            # the loose split flags 'admin' inside 'shizuha-admin' — a scope
            # marker false-positive that fail-closes an honest ref. Service
            # provenance is durable server-side (the admin provisioning
            # marker); the ref carries only the verified resource identity.
            'created': bool(org_payload.get('created')),
            'operation_id': operation_id,
        }
    raise SagaAdapterError(
        f'ID org provisioning failed: http {response.status_code} {_redact(org_payload)}'
    )


def teardown_id_org(run, organization_ref: dict) -> dict:
    """Registered compensating action for the id_org adapter (saga teardown).

    Tears down the saga-minted admin Organization via the signed internal
    surface (``POST /api/internal/organizations/<id>/delete/``). The server
    refuses any org that lacks THIS surface's provenance marker or has gained
    active members beyond the minted owner row — refuse-by-default on both
    ends. Best-effort: outcome is recorded in the run's audit trail either way.
    """
    org_id = str(organization_ref.get('external_org_id') or '').strip()
    operation_id = f'hive320:{run.idempotency_key}:id_org:teardown'
    url = f'{_admin_base()}/internal/organizations/{org_id}/delete/'
    payload = {'operation_id': operation_id}
    try:
        body, headers = _signed_membership_request(url, payload)
    except ValueError as exc:
        # Best-effort compensating action: an unconfigured trust edge is an
        # honest failed outcome for the audit trail, never a crash out of
        # deprovision_parked_run.
        return {
            'status': 'unconfigured',
            'detail': f'provisioning trust edge: {exc}',
            'organization_id': org_id,
        }
    try:
        response = requests.post(
            url, data=body, headers=headers, timeout=PROVISIONING_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return {
            'status': 'unreachable',
            'detail': f'{exc.__class__.__name__}: {exc}',
            'organization_id': org_id,
        }
    org_payload = _json_payload(response)
    if response.status_code == 200 and org_payload.get('status') == 'deleted':
        return {'status': 'deleted', 'organization_id': org_id, 'operation_id': operation_id}
    return {
        'status': f'failed_http_{response.status_code}',
        'detail': _redact(org_payload),
        'organization_id': org_id,
    }


# --- the discoverable registry (HIVE-2715 scope: full step registry lives in
# claws/tenant_provisioning.PROVISIONING_STEPS; subsequent side-effectful legs
# — pulse, wiki, connect_drive, tenant_namespace, agents, cortex_key,
# seed_task — register here as their predecessors land). ---

SAGA_ADAPTERS = {
    'id_org': provision_id_org,
}


def expected_first_adapterless_step() -> str | None:
    """The only step whose adapter-pending park may ride the honesty bridge.

    The FIRST side-effectful step in PROVISIONING_STEPS order lacking a
    concrete adapter — registry-derived, so the bridge shrinks automatically
    as adapters land. None when every side-effectful step has an adapter
    (the bridge closes entirely).
    """
    from claws.tenant_provisioning import PROVISIONING_STEPS
    for step in PROVISIONING_STEPS:
        if step.side_effectful and step.slug not in SAGA_ADAPTERS:
            return step.slug
    return None
