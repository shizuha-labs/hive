"""HIVE-2233 — Hive-side Claw↔Cortex key reconcile/cutover saga.

Implements the missing Hive half of HIVE-1400 (cross-service cutover): an
idempotent ``reconcile_claw_cortex_key(claw_id)`` service that converges a
Claw's bound Cortex API key onto the owner-attributed desired tier using the
Cortex trusted internal endpoints (cortex#69):

  POST /v1/internal/api-keys/inspect          exact {owner, name, sha256} inspect
  POST /v1/internal/api-keys                  trusted mint (accepts operation_id)
  GET  /v1/internal/api-keys/operations/<id>  durable orphan-recovery status
  POST /v1/internal/api-keys/revoke           idempotent exact fenced revoke

Saga (HIVE-1397 table), crash-resumable at every boundary:

  prepared  -> bound   : mint new compliant key (or resume operation), then
                         CAS-patch ``claw-config`` Secret by resourceVersion and
                         stamp the Deployment pod template.
  bound     -> verifying: wait rollout (observedGeneration == generation).
  verifying -> active  : tiny attributed Cortex proof with the new key, then
                         revoke the exact old key (idempotent, fail-closed on
                         mismatch). Revoke failure -> durable ``retire_pending``.
  retire_pending -> active: retry the exact revoke on the next reconcile.

Invariants (HIVE-2233 §4): never select by name/prefix/latest alone; never
manually patch a live Secret outside the CAS fence; never revoke before the
new-runtime proof; raw key material is envelope-encrypted with the existing
token-coordinator primitive and is never logged or returned.
"""
import logging
import uuid

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from credentials.models import CredentialGeneration
from hive_platform.token_coordinator import (
    decrypt_token,
    encrypt_token,
    token_fingerprint,
)

from .models import Claw, ClawCortexKeyBinding
from .provisioner import (
    DEPLOYMENT_NAME,
    cortex_service_tier_for_claw,
    get_k8s_client,
    mint_owner_cortex_key,
)

logger = logging.getLogger(__name__)

# Cortex key name format — MUST match mint_owner_cortex_key / the legacy
# backfill so exact-match inspect/revoke fence on the same identity.
def cortex_key_name(claw: Claw) -> str:
    agent_type = getattr(claw, 'agent_type', Claw.AgentType.CLAW) or Claw.AgentType.CLAW
    return f'hive-{agent_type}-{claw.id.hex}'


def _cortex_base() -> str:
    base = (
        getattr(settings, 'CORTEX_BASE_URL', '')
        or getattr(settings, 'VLLM_BASE_URL', '')
    ).rstrip('/')
    return base


def _service_token() -> str:
    """Trusted Cortex service credential (same source as mint_owner_cortex_key)."""
    service_key = (
        (getattr(settings, 'HIVE_CORTEX_LEASE_API_KEY', '') or '').strip()
        or (getattr(settings, 'CORTEX_API_KEY', '') or '').strip()
    )
    if not service_key:
        return ''
    return service_key if service_key.lower().startswith('bearer ') else f'Bearer {service_key}'


def _auth_headers() -> dict:
    return {'Authorization': _service_token(), 'Accept': 'application/json'}


def _owner_id(claw: Claw) -> int:
    try:
        return int(getattr(claw, 'owner_id') or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Cortex trusted client (thin, exact-match only)
# ---------------------------------------------------------------------------

def cortex_inspect_key(claw: Claw, raw_key: str) -> dict | None:
    """Exact-match trusted inspect. Returns secret-free metadata or None (miss)."""
    base = _cortex_base()
    token = _service_token()
    if not base or not token or not raw_key:
        return None
    try:
        resp = requests.post(
            f'{base}/v1/internal/api-keys/inspect',
            headers=_auth_headers(),
            json={
                'owner_user_id': _owner_id(claw),
                'key_name': cortex_key_name(claw),
                'key_hash': token_fingerprint(raw_key),
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.warning('claw %s: cortex inspect failed: %s', claw.id, exc)
        return None
    if resp.status_code != 200:
        logger.warning(
            'claw %s: cortex inspect returned %s: %s',
            claw.id, resp.status_code, resp.text[:200],
        )
        return None
    try:
        return resp.json() or {}
    except ValueError:
        return None


def cortex_mint_key(claw: Claw, operation_id: uuid.UUID) -> dict | None:
    """Trusted mint at the desired tier with a client operation_id.

    Returns the mint response (includes the one-time raw ``key``) or None.
    A replay of a succeeded operation_id returns the durable status without
    re-emitting the raw key (orphan recovery handled by the caller).
    """
    base = _cortex_base()
    token = _service_token()
    if not base or not token:
        return None
    traffic_class = _claw_traffic_class(claw)
    body = {
        'name': cortex_key_name(claw),
        'user_id': _owner_id(claw),
        'service_tier': cortex_service_tier_for_claw(claw),
        'traffic_class': traffic_class,
        'operation_id': str(operation_id),
    }
    if traffic_class != 'own_fleet':
        body['billing_user_id'] = _owner_id(claw)
    # HIVE-2233 fix: Cortex requires org_id on STANDARD (customer) key mints
    # ("org_id is required when minting customer API keys"). The reconcile
    # path has no owner JWT (service-token only), so the org must come from
    # the Claw's persisted org_id (populated at claw creation / repair).
    org_id = getattr(claw, 'org_id', None) or 0
    if org_id > 0:
        body['org_id'] = org_id
    try:
        resp = requests.post(
            f'{base}/v1/internal/api-keys',
            headers=_auth_headers(),
            json=body,
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.warning('claw %s: cortex mint failed: %s', claw.id, exc)
        return None
    if resp.status_code not in (200, 201):
        logger.warning(
            'claw %s: cortex mint returned %s: %s',
            claw.id, resp.status_code, resp.text[:200],
        )
        return None
    try:
        return resp.json() or {}
    except ValueError:
        return None


def cortex_operation_status(operation_id: uuid.UUID) -> dict | None:
    """Durable orphan-recovery status for a mint operation (no raw key)."""
    base = _cortex_base()
    token = _service_token()
    if not base or not token:
        return None
    try:
        resp = requests.get(
            f'{base}/v1/internal/api-keys/operations/{operation_id}',
            headers=_auth_headers(),
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.warning('cortex operation %s status failed: %s', operation_id, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json() or {}
    except ValueError:
        return None


def cortex_revoke_key(claw: Claw, key_id: str, fingerprint: str) -> tuple[bool, str]:
    """Idempotent exact revoke. Returns (ok, message); fail-closed on mismatch."""
    base = _cortex_base()
    token = _service_token()
    if not base or not token or not key_id:
        return False, 'revoke skipped: no key_id or service credential'
    try:
        resp = requests.post(
            f'{base}/v1/internal/api-keys/revoke',
            headers=_auth_headers(),
            json={
                'key_id': str(key_id),
                'owner_user_id': _owner_id(claw),
                'key_name': cortex_key_name(claw),
                'key_fingerprint': fingerprint or None,
                'reason': 'HIVE-2233 reconcile cutover',
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.warning('claw %s: cortex revoke failed: %s', claw.id, exc)
        return False, f'revoke transport error: {type(exc).__name__}'
    if resp.status_code == 200:
        return True, 'revoked'
    if resp.status_code == 409:
        logger.warning('claw %s: cortex revoke fence failed: %s', claw.id, resp.text[:200])
        return False, f'revoke fence failed: {resp.text[:120]}'
    logger.warning('claw %s: cortex revoke returned %s: %s', claw.id, resp.status_code, resp.text[:200])
    return False, f'revoke HTTP {resp.status_code}'


def _claw_traffic_class(claw: Claw) -> str:
    from .provisioner import claw_traffic_class
    return claw_traffic_class(claw)


# ---------------------------------------------------------------------------
# Kubernetes helpers (CAS Secret patch + Deployment stamp/rollout)
# ---------------------------------------------------------------------------

def _read_secret_version(core_v1, claw: Claw) -> tuple[dict | None, str]:
    """Read claw-config Secret + its resourceVersion (CAS fence)."""
    ns = (claw.namespace or '').strip()
    if not ns:
        return None, ''
    try:
        secret = core_v1.read_namespaced_secret(name='claw-config', namespace=ns)
    except Exception as exc:
        logger.warning('claw %s: read claw-config failed: %s', claw.id, exc)
        return None, ''
    return secret, (secret.metadata.resource_version or '')


def _read_secret_raw_key(core_v1, claw: Claw) -> str:
    """Read the live cortex-api-key value from claw-config (raw, for inspect)."""
    import base64
    ns = (claw.namespace or '').strip()
    if not ns:
        return ''
    try:
        secret = core_v1.read_namespaced_secret(name='claw-config', namespace=ns)
    except Exception as exc:
        logger.warning('claw %s: read claw-config for key failed: %s', claw.id, exc)
        return ''
    data = secret.data or {}
    raw = data.get('cortex-api-key') or ''
    if not raw:
        return ''
    try:
        return base64.b64decode(raw).decode()
    except Exception:
        return ''


def _cas_patch_secret(core_v1, claw: Claw, new_raw_key: str, expected_version: str) -> bool:
    """CAS-patch claw-config Secret by resourceVersion; fail closed on drift."""
    ns = (claw.namespace or '').strip()
    if not ns:
        return False
    try:
        secret = core_v1.read_namespaced_secret(name='claw-config', namespace=ns)
    except Exception as exc:
        logger.warning('claw %s: CAS read failed: %s', claw.id, exc)
        return False
    current_version = secret.metadata.resource_version or ''
    if expected_version and current_version != expected_version:
        logger.warning(
            'claw %s: CAS drift — expected rv %s, got %s', claw.id, expected_version, current_version,
        )
        return False
    secret.data = secret.data or {}
    import base64
    secret.data['cortex-api-key'] = base64.b64encode(new_raw_key.encode()).decode()
    try:
        core_v1.replace_namespaced_secret(name='claw-config', namespace=ns, body=secret)
    except Exception as exc:
        logger.warning('claw %s: CAS secret replace failed: %s', claw.id, exc)
        return False
    return True


def _stamp_deployment(apps_v1, claw: Claw, generation: str) -> bool:
    """Stamp the Deployment pod template with the new key generation."""
    ns = (claw.namespace or '').strip()
    if not ns:
        return False
    try:
        deploy = apps_v1.read_namespaced_deployment(name=DEPLOYMENT_NAME, namespace=ns)
    except Exception as exc:
        logger.warning('claw %s: read deployment failed: %s', claw.id, exc)
        return False
    deploy.spec.template.metadata.annotations = deploy.spec.template.metadata.annotations or {}
    deploy.spec.template.metadata.annotations['hive.shizuha.com/cortex-key-generation'] = generation
    try:
        apps_v1.replace_namespaced_deployment(name=DEPLOYMENT_NAME, namespace=ns, body=deploy)
    except Exception as exc:
        logger.warning('claw %s: deployment stamp failed: %s', claw.id, exc)
        return False
    return True


def _wait_rollout(apps_v1, claw: Claw, timeout: int = 180) -> bool:
    """Wait until observedGeneration == generation and available replicas match."""
    ns = (claw.namespace or '').strip()
    if not ns:
        return False
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            deploy = apps_v1.read_namespaced_deployment(name=DEPLOYMENT_NAME, namespace=ns)
        except Exception:
            time.sleep(5)
            continue
        # HIVE-2233 fix: a 0-replica (idle) claw has no pods to wait for —
        # observedGeneration == generation is sufficient. Treating 0 as 1
        # made idle claws never satisfy available_replicas == 1 and time out.
        desired_replicas = deploy.spec.replicas or 0
        if (
            deploy.metadata.generation
            and deploy.status.observed_generation == deploy.metadata.generation
            and (
                desired_replicas == 0
                or deploy.status.available_replicas == desired_replicas
            )
        ):
            return True
        time.sleep(5)
    logger.warning('claw %s: rollout wait timed out', claw.id)
    return False


def _cortex_proof(claw: Claw, raw_key: str) -> bool:
    """Tiny attributed Cortex call proof with the new key (never revoke first)."""
    from .provisioner import probe_cortex_model_access, resolve_cortex_model
    try:
        model = resolve_cortex_model(claw, None)
    except Exception:
        model = getattr(settings, 'HIVE_CORTEX_DEFAULT_MODEL', 'DeepSeek-V4-Flash')
    result = probe_cortex_model_access(raw_key, model)
    return result.status == 'available'


# ---------------------------------------------------------------------------
# Saga
# ---------------------------------------------------------------------------

def _new_generation(claw: Claw) -> CredentialGeneration:
    """Create the next immutable credential-generation node for this entry."""
    entry_id = f'claw-cortex:{claw.id.hex}'
    last = (
        CredentialGeneration.objects.filter(entry_id=entry_id)
        .order_by('-generation_ordinal')
        .first()
    )
    ordinal = (last.generation_ordinal + 1) if last else 1
    parent = last.generation_id if last else None
    chain = f'{entry_id}:{ordinal}:{parent or ""}'
    import hashlib
    return CredentialGeneration.objects.create(
        entry_id=entry_id,
        generation_ordinal=ordinal,
        parent_generation_id=parent,
        generation_chain_digest=hashlib.sha256(chain.encode()).hexdigest(),
    )


def _artifact(raw_key: str) -> dict:
    """Envelope-encrypt a raw key artifact (never plaintext)."""
    return encrypt_token(raw_key)


def _decrypt_artifact(artifact: dict) -> str:
    if not artifact:
        return ''
    try:
        return decrypt_token(artifact['ciphertext'], artifact['wrapped_dek'])
    except (KeyError, ValueError):
        return ''


@transaction.atomic
def reconcile_claw_cortex_key(claw_id, *, user_token: str = '', dry_run: bool = False) -> tuple[ClawCortexKeyBinding, str]:
    """Idempotently converge a Claw's Cortex key onto the desired tier.

    Returns (binding, outcome) where outcome is a short status string:
    ``noop`` (already compliant/active), ``converged`` (cutover completed),
    ``prepared``/``bound``/``verifying`` (crash-resumed, more work pending),
    or ``error`` (see binding.last_error).

    Wrapped in ``transaction.atomic`` (HIVE-2233 fix): the saga uses
    ``select_for_update`` row locks, which crash with
    ``TransactionManagementError`` outside an atomic block.
    """
    claw = Claw.objects.select_for_update().get(id=claw_id)
    binding, _created = ClawCortexKeyBinding.objects.select_for_update().get_or_create(claw=claw)
    ns = (claw.namespace or '').strip()
    if not ns:
        binding.last_error = 'claw has no namespace'
        binding.save(update_fields=['last_error', 'updated_at'])
        return binding, 'error'

    core_v1, apps_v1 = get_k8s_client() if not dry_run else (None, None)
    desired_tier = cortex_service_tier_for_claw(claw)
    binding.desired_tier = desired_tier

    # ── Resume from crash points (HIVE-1397 table) ────────────────────────
    if binding.state in (ClawCortexKeyBinding.State.ACTIVE, ClawCortexKeyBinding.State.REVOKED):
        return binding, 'noop'

    # ── Step 0: capture the live secret key + compliance check ────────────
    live_raw = ''
    if not dry_run:
        live_raw = _read_secret_raw_key(core_v1, claw)
    current_raw = _decrypt_artifact(binding.current_artifact)

    # If the live key is already compliant at the desired tier, nothing to do.
    if binding.state == ClawCortexKeyBinding.State.PREPARED and not current_raw:
        if live_raw:
            meta = cortex_inspect_key(claw, live_raw)
            if meta and str(meta.get('service_tier') or '') == desired_tier:
                binding.state = ClawCortexKeyBinding.State.ACTIVE
                binding.current_artifact = _artifact(live_raw)
                binding.cortex_key_uuid = _as_uuid(meta.get('id'))
                binding.key_fingerprint = token_fingerprint(live_raw)
                binding.last_reconcile_at = timezone.now()
                binding.last_error = ''
                binding.save(update_fields=[
                    'current_artifact', 'cortex_key_uuid', 'key_fingerprint',
                    'state', 'desired_tier', 'last_error', 'last_reconcile_at', 'updated_at',
                ])
                return binding, 'noop'
            # Non-compliant live key becomes the prior (to-be-revoked) artifact.
            binding.prior_artifact = _artifact(live_raw)
            binding.prior_key_id = _as_uuid(meta.get('id')) if meta else None

    # ── Step 1: ensure a prepared compliant generation exists ─────────────
    if binding.state == ClawCortexKeyBinding.State.PREPARED and not current_raw:
        if dry_run:
            return binding, 'prepared'
        # Mint a new compliant key (idempotent via operation_id).
        operation_id = binding.operation_id or uuid.uuid4()
        binding.operation_id = operation_id
        mint = cortex_mint_key(claw, operation_id)
        if mint is None:
            # Orphan recovery: the mint may have succeeded but the response was
            # lost. Query the durable operation status (never re-emits raw key).
            status = cortex_operation_status(operation_id)
            if not status or str(status.get('status', '')).lower() not in ('succeeded', 'active'):
                binding.last_error = 'cortex mint unavailable'
                binding.save(update_fields=['last_error', 'operation_id', 'updated_at'])
                return binding, 'error'
            binding.last_error = 'mint response lost; operation durable — resume on next reconcile'
            binding.save(update_fields=['last_error', 'operation_id', 'updated_at'])
            return binding, 'prepared'
        raw_key = str(mint.get('key') or '')
        if not raw_key:
            binding.last_error = 'cortex mint returned no plaintext key'
            binding.save(update_fields=['last_error', 'operation_id', 'updated_at'])
            return binding, 'error'
        binding.current_artifact = _artifact(raw_key)
        binding.cortex_key_uuid = _as_uuid(mint.get('id'))
        binding.key_fingerprint = token_fingerprint(raw_key)
        binding.generation = _new_generation(claw)
        binding.state = ClawCortexKeyBinding.State.PREPARED
        binding.last_error = ''
        binding.save(update_fields=[
            'current_artifact', 'cortex_key_uuid', 'key_fingerprint', 'generation',
            'operation_id', 'state', 'desired_tier', 'prior_artifact', 'prior_key_id',
            'last_error', 'updated_at',
        ])
        current_raw = raw_key

    # ── Step 2: CAS-patch Secret + stamp Deployment (prepared -> bound) ───
    if binding.state == ClawCortexKeyBinding.State.PREPARED:
        if dry_run:
            return binding, 'prepared'
        secret, version = _read_secret_version(core_v1, claw)
        if not _cas_patch_secret(core_v1, claw, current_raw, version):
            binding.last_error = 'CAS secret patch failed (drift or transport)'
            binding.save(update_fields=['last_error', 'updated_at'])
            return binding, 'error'
        binding.secret_resource_version = version
        generation_stamp = f'{binding.generation_id.hex if binding.generation_id else binding.id.hex}:{binding.key_fingerprint[:12]}'
        if not _stamp_deployment(apps_v1, claw, generation_stamp):
            binding.last_error = 'deployment stamp failed'
            binding.save(update_fields=['last_error', 'updated_at'])
            return binding, 'error'
        binding.state = ClawCortexKeyBinding.State.BOUND
        binding.last_error = ''
        binding.save(update_fields=['secret_resource_version', 'state', 'last_error', 'updated_at'])

    # ── Step 3: wait rollout (bound -> verifying) ─────────────────────────
    if binding.state == ClawCortexKeyBinding.State.BOUND:
        if dry_run:
            return binding, 'bound'
        if not _wait_rollout(apps_v1, claw):
            binding.last_error = 'rollout wait timed out'
            binding.save(update_fields=['last_error', 'updated_at'])
            return binding, 'error'
        binding.state = ClawCortexKeyBinding.State.VERIFYING
        binding.last_error = ''
        binding.save(update_fields=['state', 'last_error', 'updated_at'])

    # ── Step 4: proof, then revoke exact old key (verifying -> active) ────
    if binding.state == ClawCortexKeyBinding.State.VERIFYING:
        if dry_run:
            return binding, 'verifying'
        if not _cortex_proof(claw, current_raw):
            binding.last_error = 'new-runtime Cortex proof failed; not revoking old key'
            binding.save(update_fields=['last_error', 'updated_at'])
            return binding, 'error'
        # Revoke the exact old key (prior artifact) if one exists.
        prior_raw = _decrypt_artifact(binding.prior_artifact)
        if prior_raw:
            prior_fp = token_fingerprint(prior_raw)
            ok, msg = cortex_revoke_key(claw, binding.prior_key_id or '', prior_fp)
            if not ok:
                binding.state = ClawCortexKeyBinding.State.RETIRE_PENDING
                binding.last_error = f'old-key revoke pending: {msg}'
                binding.save(update_fields=['state', 'last_error', 'updated_at'])
                return binding, 'retire_pending'
        binding.state = ClawCortexKeyBinding.State.ACTIVE
        binding.last_error = ''
        binding.last_reconcile_at = timezone.now()
        binding.save(update_fields=['state', 'last_error', 'last_reconcile_at', 'updated_at'])
        return binding, 'converged'

    # ── Step 5: retire_pending retry ──────────────────────────────────────
    if binding.state == ClawCortexKeyBinding.State.RETIRE_PENDING:
        if dry_run:
            return binding, 'retire_pending'
        prior_raw = _decrypt_artifact(binding.prior_artifact)
        if prior_raw:
            ok, msg = cortex_revoke_key(claw, binding.prior_key_id or '', token_fingerprint(prior_raw))
            if not ok:
                binding.last_error = f'old-key revoke still pending: {msg}'
                binding.save(update_fields=['last_error', 'updated_at'])
                return binding, 'retire_pending'
        binding.state = ClawCortexKeyBinding.State.ACTIVE
        binding.last_error = ''
        binding.last_reconcile_at = timezone.now()
        binding.save(update_fields=['state', 'last_error', 'last_reconcile_at', 'updated_at'])
        return binding, 'converged'

    return binding, 'noop'


def _as_uuid(value) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None
