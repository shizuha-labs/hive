"""
K8s Claw Provisioner.

Creates and manages per-tenant claw workloads on the k3s cluster. Each claw is a
single-replica OpenClaw Deployment (Charter D4: ``Recreate`` strategy so the
WhatsApp-session container never double-locks the session dir) backed by a
Longhorn PVC (Charter D3), living in its own namespace ``claw-<owner>-<claw>``.

Charter D1: hive talks to k3s directly (in-cluster ServiceAccount), and the claw
is *near-native OpenClaw* — there is NO agent-runtime account minting here.

HIVE-9 workload binding:
The near-native OpenClaw image is the HIVE-21 multi-arch artifact and OpenClaw's
stateful home is mounted at ``/home/node/.openclaw`` so the s1 source data
(``$CLAW_SOURCE_DATA_DIR`` plus the nested workspace) can be copied into a
Longhorn PVC without remapping the OAuth/WhatsApp/Baileys session paths.
"""
import base64
import dataclasses
import json
import logging
import os
import threading
import time
from datetime import timedelta

import requests
import yaml
from django.conf import settings
from django.utils import timezone
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from . import lifecycle
from .models import Claw, ClawEvent

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class TierSpec:
    pvc_size: str
    cpu_request: str
    cpu_limit: str
    mem_request: str
    mem_limit: str
    idle_ttl_minutes: int
    daily_msg_limit: int
    llm_service: str
    llm_default_model: str
    max_claws_per_owner: int


_FREE_TIER_DEFAULT = TierSpec(
    pvc_size='5Gi',
    cpu_request='100m',
    cpu_limit='1',
    mem_request='256Mi',
    mem_limit='2Gi',
    idle_ttl_minutes=30,
    daily_msg_limit=100,
    llm_service='hive-llm',
    llm_default_model='DeepSeek-V4-Flash',
    max_claws_per_owner=1,
)

_TIER_CACHE: dict = {}
_TIER_CACHE_LOCK = threading.Lock()
_TIER_CACHE_TTL = 300  # seconds


def resolve_tier(tier_id: str, core_v1=None) -> TierSpec:
    """Read tier spec from the hive-tiers ConfigMap (shizuha-hive namespace).

    Falls back to the built-in free-tier default if the ConfigMap is absent,
    the requested tier key is missing, or any field is malformed — so a bad
    ConfigMap can never crash provisioning.  Results are cached for 5 minutes.
    """
    tier_id = (tier_id or 'free').strip().lower()

    now = time.monotonic()
    with _TIER_CACHE_LOCK:
        cached = _TIER_CACHE.get(tier_id)
        if cached and now - cached['ts'] < _TIER_CACHE_TTL:
            return cached['spec']

    try:
        if core_v1 is None:
            core_v1, _ = get_k8s_client()
        hive_ns = getattr(settings, 'HIVE_NAMESPACE', 'shizuha-hive')
        cm = core_v1.read_namespaced_config_map(name='hive-tiers', namespace=hive_ns)
        raw = yaml.safe_load(cm.data.get('tiers.yaml', '{}')) or {}
        tiers = raw.get('tiers', {})
        # Fall back to 'free' when the requested tier key is absent.
        entry = tiers.get(tier_id) or tiers.get('free') or {}
        res = entry.get('resources', {})
        req = res.get('requests', {})
        lim = res.get('limits', {})
        sto = entry.get('scale_to_zero', {})
        llm = entry.get('llm', {})
        spec = TierSpec(
            pvc_size=str(entry.get('pvc_size') or _FREE_TIER_DEFAULT.pvc_size),
            cpu_request=str(req.get('cpu') or _FREE_TIER_DEFAULT.cpu_request),
            cpu_limit=str(lim.get('cpu') or _FREE_TIER_DEFAULT.cpu_limit),
            mem_request=str(req.get('memory') or _FREE_TIER_DEFAULT.mem_request),
            mem_limit=str(lim.get('memory') or _FREE_TIER_DEFAULT.mem_limit),
            idle_ttl_minutes=int(sto.get('idle_ttl_minutes') or _FREE_TIER_DEFAULT.idle_ttl_minutes),
            daily_msg_limit=int(entry.get('daily_msg_limit') or _FREE_TIER_DEFAULT.daily_msg_limit),
            llm_service=str(llm.get('service') or _FREE_TIER_DEFAULT.llm_service),
            llm_default_model=str(llm.get('default_model') or _FREE_TIER_DEFAULT.llm_default_model),
            max_claws_per_owner=int(entry.get('max_claws_per_owner') or _FREE_TIER_DEFAULT.max_claws_per_owner),
        )
    except Exception:
        logger.warning(
            'resolve_tier(%r): failed to read hive-tiers ConfigMap; using built-in free fallback',
            tier_id, exc_info=True,
        )
        spec = _FREE_TIER_DEFAULT

    with _TIER_CACHE_LOCK:
        _TIER_CACHE[tier_id] = {'spec': spec, 'ts': time.monotonic()}

    return spec

DEPLOYMENT_NAME = 'claw'
APP_LABEL = 'claw'
REQUIRED_CLAW_NETWORK_POLICIES = (
    'default-deny-all',
    'dns-egress',
    'platform-egress',
    'cortex-egress',
    'wan-egress',
    'dashboard-ingress',
    'owner-isolation',
)
REQUIRED_CLAW_RESOURCE_QUOTA = 'claw-resource-boundaries'
REQUIRED_CLAW_LIMIT_RANGE = 'claw-limit-range'


class HermesRuntimeUnavailable(RuntimeError):
    """Raised when Hermes is requested but no honest runtime mode is configured."""


class CortexModelUnavailable(RuntimeError):
    """Raised when a claw credential cannot use its selected Cortex model."""


@dataclasses.dataclass(frozen=True)
class RuntimeResolution:
    image: str
    pull_policy: str
    compat_mode: bool = False


@dataclasses.dataclass(frozen=True)
class CortexModelAccess:
    status: str
    reason: str = ''
    model_ids: tuple[str, ...] = ()


def _runtime_image_equals(lhs: str, rhs: str) -> bool:
    return (lhs or '').strip() == (rhs or '').strip()

# HIVE-201 E2 / HIVE-188 M4: the notify_owner MCP server (proactive owner push).
# Option A (aoi 2026-06-25): provision-time install — the init-gateway-config
# initContainer seeds the server into the claw PVC and registers it in
# openclaw.json under mcp.servers.notify_owner. No multiarch image rebuild.
# Bump _NOTIFY_OWNER_VERSION whenever notify_owner_server.mjs changes so a
# re-provision/pod-restart re-seeds the updated file.
_NOTIFY_OWNER_VERSION = '1.0.0'
_NOTIFY_OWNER_DIR = 'extensions/notify-owner'
_WA_WATCHDOG_VERSION = '1.0.0'
_WA_WATCHDOG_DIR = 'hooks/whatsapp-link-watchdog'

def _notify_owner_server_source() -> str:
    """Read the pinned notify_owner MCP server JS shipped in the hive image."""
    path = os.path.join(os.path.dirname(__file__), 'openclaw', 'notify_owner_server.mjs')
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


def _wa_watchdog_hook_source() -> str:
    """Read the pinned OpenClaw hook that pages owners on WhatsApp link drops."""
    path = os.path.join(os.path.dirname(__file__), 'openclaw', 'whatsapp_link_watchdog_hook.ts')
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


def _wa_watchdog_hook_md() -> str:
    return '''---
name: whatsapp-link-watchdog
description: "Notify the claw owner when WhatsApp link health degrades or outbound delivery fails"
metadata:
  openclaw:
    emoji: "📲"
    events: ["gateway:startup", "message:sent"]
    always: true
---
# WhatsApp link watchdog

Runs inside each Hive-managed OpenClaw gateway. It probes WhatsApp channel health on startup/interval and watches outbound `message:sent` failures. On link drop, missing web listener, or repeated failed probes it calls Hive's owner notification endpoint with a dashboard deep-link so the owner can re-pair.
'''.strip() + '\n'


def get_k8s_client():
    """Get k8s API client. Uses in-cluster config when running in k8s."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api()


# Qwen GB10 serving retired 2026-07-10 (operator DeepSeek-only directive) — any
# tier still defaulting to a Qwen model falls through to HIVE_CORTEX_DEFAULT_MODEL.
_STALE_TIER_MODEL_DEFAULTS = {
    'GLM-5.1', 'GLM-5.2', 'Qwen3.6-27B', 'Qwen3.6-27B-4bit-MLX',
    'Qwen3.6-27B-NVFP4', 'Qwen3.6-27B-FP8', 'Qwen3.6-35B-A3B', 'Qwen3.6-35B-A3B-NVFP4',
}


def _cortex_public_base() -> str:
    """Cortex base URL, including /v1, used by personal assistant pods."""
    public = getattr(settings, 'CORTEX_PUBLIC_BASE_URL', '') or ''
    if public:
        return public.rstrip('/')
    base = (
        getattr(settings, 'CORTEX_BASE_URL', '')
        or getattr(settings, 'VLLM_BASE_URL', '')
        or 'http://cortex.shizuha-cortex.svc.cluster.local:8040'
    ).rstrip('/')
    return base if base.endswith('/v1') else f'{base}/v1'


def resolve_cortex_model(claw: Claw, tier: TierSpec) -> str:
    """Resolve Cortex model for a personal assistant: request > tier > service default."""
    requested = (getattr(claw, 'model', '') or '').strip()
    if requested:
        return requested
    tier_model = (getattr(tier, 'llm_default_model', '') or '').strip()
    if tier_model and tier_model not in _STALE_TIER_MODEL_DEFAULTS:
        return tier_model
    return getattr(settings, 'HIVE_CORTEX_DEFAULT_MODEL', 'DeepSeek-V4-Flash')


def _cortex_model_id(value: str) -> str:
    model = str(value or '').strip()
    return model.split('/', 1)[1] if model.lower().startswith('cortex/') else model


def probe_cortex_model_access(api_key: str, model: str) -> CortexModelAccess:
    """Verify the exact key/model pair without weakening Cortex policy.

    A successful model catalog is authoritative.  Authentication/authorization
    denial is a stable unavailable result; transport and 5xx failures remain
    unknown so a transient Cortex outage is not misreported as entitlement.
    """
    key = str(api_key or '').strip()
    target = _cortex_model_id(model)
    if not key:
        return CortexModelAccess('unavailable', 'cortex credential missing')
    token = key if key.lower().startswith('bearer ') else f'Bearer {key}'
    try:
        response = requests.get(
            f'{_cortex_public_base()}/models',
            headers={'Authorization': token, 'Accept': 'application/json'},
            timeout=10,
        )
    except requests.RequestException as exc:
        return CortexModelAccess('unknown', f'catalog transport {type(exc).__name__}')
    if response.status_code in (401, 403):
        return CortexModelAccess('unavailable', f'catalog authorization HTTP {response.status_code}')
    if response.status_code != 200:
        return CortexModelAccess('unknown', f'catalog HTTP {response.status_code}')
    try:
        payload = response.json() or {}
        model_ids = tuple(
            str(item.get('id') or '').strip()
            for item in (payload.get('data') or [])
            if isinstance(item, dict) and str(item.get('id') or '').strip()
        )
    except (TypeError, ValueError):
        return CortexModelAccess('unknown', 'catalog response malformed')
    if target.lower() in {_cortex_model_id(item).lower() for item in model_ids}:
        return CortexModelAccess('available', model_ids=model_ids)
    return CortexModelAccess(
        'unavailable',
        f'model {_cortex_model_id(model)} absent from credential catalog',
        model_ids,
    )


CLAW_LONGCTX_SERVICE_TIER = 'claw-longctx'
STANDARD_SERVICE_TIER = 'standard'


def cortex_service_tier_for_claw(claw: Claw) -> str:
    agent_type = getattr(claw, 'agent_type', Claw.AgentType.CLAW) or Claw.AgentType.CLAW
    if agent_type in (Claw.AgentType.CLAW, Claw.AgentType.HERMES):
        return CLAW_LONGCTX_SERVICE_TIER
    return STANDARD_SERVICE_TIER


def claw_traffic_class(claw: Claw) -> str:
    """Whether this claw's key is own-fleet traffic or a resold customer's.

    Operator 2026-08-05, on `hive-claw-16` (mika) getting `model access denied`
    for DeepSeek-V4-Flash:

        i'd like even claw based agents in hive to be able to lease our deepseek
        model .. just like other agents do

    The mint omitted `traffic_class` entirely, so Cortex applied its
    `RESOLD_CUSTOMER` default — and `principal_can_access_internal_models`
    refuses resold traffic FIRST and unconditionally, by design. So every claw
    owned by one of our own agents was classified as a paying outside customer
    and locked out of internal models, while fleet agents (own_fleet) were fine.
    `hive-claw-3` worked only because it happens to carry own_fleet.

    The fix belongs HERE, in classification — not in Cortex's gate. Resold
    traffic must never reach internal models; the error was calling our own
    agents resold. Fails CLOSED: anything we cannot positively identify as an
    internal principal stays resold.
    """
    owner_id = getattr(claw, 'owner_id', None)
    if not owner_id:
        return 'resold_customer'
    base = (getattr(settings, 'SHIZUHA_ID_INTERNAL_URL', '') or '').rstrip('/')
    token = (getattr(settings, 'HIVE_ID_FLEET_PROVISIONER_TOKEN', '') or '').strip()
    if not base or not token:
        logger.warning(
            'claw %s: cannot classify owner %s without the id service; '
            'defaulting to resold_customer', claw.id, owner_id,
        )
        return 'resold_customer'
    try:
        resp = requests.get(
            f'{base}/api/internal/users/{int(owner_id)}/',
            headers={'X-Fleet-Provisioner-Token': token, 'Accept': 'application/json'},
            timeout=8,
        )
    except (requests.RequestException, TypeError, ValueError) as exc:
        logger.warning('claw %s: owner classification lookup failed: %s', claw.id, exc)
        return 'resold_customer'
    if resp.status_code != 200:
        logger.warning(
            'claw %s: owner classification returned HTTP %s', claw.id, resp.status_code,
        )
        return 'resold_customer'
    try:
        data = resp.json() or {}
    except ValueError:
        return 'resold_customer'
    account_type = str(
        (data.get('profile') or {}).get('account_type') or data.get('account_type') or ''
    ).strip().lower()
    # An agent IS the fleet, and staff/superusers are the operators of it.
    # Everyone else is a customer until proven otherwise.
    if account_type == 'agent' or data.get('is_staff') or data.get('is_superuser'):
        return 'own_fleet'
    return 'resold_customer'


def _org_id_from_user_token(user_token: str) -> int:
    """Best-effort tenant org from the already-authenticated owner JWT.

    Cortex requires org_id on STANDARD (customer) key mints. The owner token
    carries organization_memberships; pick the lowest id as a stable default.
    """
    raw = str(user_token or '').strip()
    if raw.lower().startswith('bearer '):
        raw = raw[7:].strip()
    parts = raw.split('.')
    if len(parts) < 2:
        return 0
    payload = parts[1] + '=' * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode('ascii')))
    except (ValueError, json.JSONDecodeError):
        return 0
    memberships = data.get('organization_memberships') or {}
    if isinstance(memberships, dict) and memberships:
        try:
            return min(int(k) for k in memberships if str(k).isdigit())
        except ValueError:
            return 0
    for key in ('org_id', 'organization_id', 'tenant_org_id'):
        try:
            value = int(data.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def mint_owner_cortex_key(claw: Claw, user_token: str) -> str:
    """Mint an owner-scoped Cortex key for a Claw/Hermes/SCLI pod.

    HIVE-350: elevated long-context quotas are authorized only through Cortex's
    trusted internal mint endpoint. The owner JWT path stays STANDARD-only, so
    do not send service_tier or raw TPM values to /v1/api-keys.
    """
    # Same trap as fleet/_mint_agent_cortex_key: Cortex grants trusted-service
    # identity on /v1/internal/api-keys only to HIVE_CORTEX_LEASE_API_KEY
    # (Cortex's own service key). Hive's CORTEX_API_KEY is a customer-tier
    # inference credential — sending it 403s "Only a Cortex-trusted service
    # credential may mint elevated-tier keys" and the claw comes up with no
    # key (HIVE-1951). Prefer the lease key; fall back only if unset.
    service_key = (
        (getattr(settings, 'HIVE_CORTEX_LEASE_API_KEY', '') or '').strip()
        or (getattr(settings, 'CORTEX_API_KEY', '') or '').strip()
    )
    if not service_key:
        logger.warning('claw %s: trusted Cortex mint credential unavailable; skipping Cortex key mint', claw.id)
        return ''
    base = (
        getattr(settings, 'CORTEX_BASE_URL', '')
        or getattr(settings, 'VLLM_BASE_URL', '')
    ).rstrip('/')
    if not base:
        logger.warning('claw %s: CORTEX_BASE_URL not configured; skipping Cortex key mint', claw.id)
        return ''
    token = service_key if service_key.lower().startswith('bearer ') else f'Bearer {service_key}'
    agent_type = getattr(claw, 'agent_type', Claw.AgentType.CLAW) or Claw.AgentType.CLAW
    try:
        traffic_class = claw_traffic_class(claw)
        body = {
            'name': f'hive-{agent_type}-{claw.id.hex}',
            'user_id': int(getattr(claw, 'owner_id')),
            'service_tier': cortex_service_tier_for_claw(claw),
            # Without this Cortex defaults to RESOLD_CUSTOMER, which its
            # internal-model gate refuses outright — every claw owned by one
            # of our own agents got `model access denied` on DeepSeek.
            'traffic_class': traffic_class,
        }
        # Customer claws bill the human owner. Own-fleet claws are platform
        # self-use — do not put an agent identity on the Hane purse.
        if traffic_class != 'own_fleet':
            body['billing_user_id'] = int(getattr(claw, 'owner_id'))
        org_id = _org_id_from_user_token(user_token)
        if org_id > 0:
            body['org_id'] = org_id
        resp = requests.post(
            f'{base}/v1/internal/api-keys',
            headers={'Authorization': token, 'Accept': 'application/json'},
            json=body,
            timeout=15,
        )
    except (TypeError, ValueError) as exc:
        logger.warning('claw %s: invalid owner_id for Cortex key mint: %s', claw.id, exc)
        return ''
    except requests.RequestException as exc:
        logger.warning('claw %s: Cortex key mint request failed: %s', claw.id, exc)
        return ''
    if resp.status_code not in (200, 201):
        logger.warning('claw %s: Cortex key mint returned %s: %s', claw.id, resp.status_code, resp.text[:300])
        return ''
    try:
        key = (resp.json() or {}).get('key', '')
    except ValueError:
        logger.warning('claw %s: Cortex key mint returned non-JSON body', claw.id)
        return ''
    if not key:
        logger.warning('claw %s: Cortex key mint response had no plaintext key', claw.id)
    return key or ''


def resolve_claw_runtime(claw: Claw) -> RuntimeResolution:
    """Resolve the runtime image without silently aliasing Hermes to OpenClaw."""
    pull_policy = getattr(settings, 'HIVE_CLAW_IMAGE_PULL_POLICY', 'IfNotPresent')
    agent_type = getattr(claw, 'agent_type', Claw.AgentType.CLAW)
    if agent_type == Claw.AgentType.SCLI:
        return RuntimeResolution(
            getattr(settings, 'HIVE_SCLI_IMAGE', 'localhost:30500/shizuha-runtime:latest'),
            pull_policy,
        )
    if agent_type == Claw.AgentType.HERMES:
        openclaw_image = getattr(settings, 'HIVE_CLAW_IMAGE')
        hermes_image = (getattr(settings, 'HIVE_HERMES_IMAGE', '') or '').strip()
        if hermes_image and not _runtime_image_equals(hermes_image, openclaw_image):
            return RuntimeResolution(hermes_image, pull_policy, compat_mode=False)
        if getattr(settings, 'HIVE_HERMES_OPENCLAW_COMPAT', False):
            return RuntimeResolution(openclaw_image, pull_policy, compat_mode=True)
        raise HermesRuntimeUnavailable(
            'Hermes runtime is unavailable: configure a dedicated HIVE_HERMES_IMAGE '
            'or explicitly enable HIVE_HERMES_OPENCLAW_COMPAT=true to run the '
            'OpenClaw-compatible gateway with visible compatibility labeling.'
        )
    return RuntimeResolution(
        getattr(
            settings, 'HIVE_CLAW_IMAGE',
            getattr(settings, 'SHIZUHA_RUNTIME_IMAGE', 'shizuha-openclaw:latest'),
        ),
        pull_policy,
    )


def _resolve_claw_image(claw: Claw):
    runtime = resolve_claw_runtime(claw)
    return runtime.image, runtime.pull_policy


def provision_claw(claw: Claw, user_token: str = '') -> bool:
    """
    Provision a full claw on k8s.

    Creates: namespace, secret (platform creds + claw config), PVC (Longhorn),
    deployment (single-replica Recreate), service.
    """
    runtime = resolve_claw_runtime(claw)
    core_v1, apps_v1 = get_k8s_client()
    ns = claw.namespace
    tier = resolve_tier(claw.tier_id or 'free', core_v1)
    cortex_model = resolve_cortex_model(claw, tier)
    cortex_base = _cortex_public_base()
    cortex_key = mint_owner_cortex_key(claw, user_token)

    try:
        access = probe_cortex_model_access(cortex_key, cortex_model)
        if access.status != 'available':
            raise CortexModelUnavailable(
                f'Cortex model {_cortex_model_id(cortex_model)} unavailable for this claw credential '
                f'({access.reason or access.status})'
            )
        claw.status = Claw.Status.PROVISIONING
        # Snapshot the resolved pvc_size so the DB record reflects what was
        # actually provisioned (audit trail); does not change the PV after creation.
        claw.pvc_size = tier.pvc_size
        claw.runtime_image = runtime.image
        claw.runtime_compat_mode = runtime.compat_mode
        claw.save(update_fields=['status', 'pvc_size', 'runtime_image', 'runtime_compat_mode'])
        _log_event(
            claw,
            'provisioning',
            f'Starting provisioning in namespace {ns} '
            f'(tier={claw.tier_id or "free"}, agent_type={claw.agent_type}, '
            f'cortex_model={cortex_model}, cortex_key={"set" if cortex_key else "none"}, '
            f'runtime_image={runtime.image}, compat_mode={runtime.compat_mode})',
        )
        if runtime.compat_mode:
            _log_event(
                claw,
                'runtime_compat',
                'Hermes is running through the OpenClaw-compatible gateway image; '
                'API/UI surfaces must label this as compatibility mode.',
            )

        cleanup = cleanup_stale_duplicate_claw_namespaces(core_v1, apps_v1, claw.owner_id, dry_run=False)
        if cleanup.get('deleted') or cleanup.get('errors'):
            _log_event(
                claw,
                'stale_duplicate_cleanup',
                f"stale duplicate namespace cleanup: deleted={len(cleanup.get('deleted') or [])} errors={len(cleanup.get('errors') or [])}",
            )
        _ensure_namespace(core_v1, ns, claw)
        _ensure_network_policies(core_v1, ns, claw, tier)
        _ensure_resource_guards(core_v1, ns, claw, tier)
        _create_claw_secret(
            core_v1, ns, claw, user_token,
            cortex_base=cortex_base, cortex_key=cortex_key, cortex_model=cortex_model,
        )
        _create_claw_pvc(core_v1, ns, claw, tier)
        _create_claw_deployment(
            apps_v1, ns, claw, tier,
            cortex_base=cortex_base, cortex_key=cortex_key, cortex_model=cortex_model,
            runtime=runtime,
        )
        _create_claw_service(core_v1, ns, claw)

        claw.status = Claw.Status.RUNNING
        claw.provisioned_at = timezone.now()
        claw.last_active_at = claw.provisioned_at
        # HIVE-598: initial idle-recycle deadline from tier goes to the
        # DEDICATED idle field. recycle_after is delete-retention ONLY —
        # stamping it here once made running claws reclaim candidates.
        claw.idle_recycle_after = claw.provisioned_at + timedelta(minutes=tier.idle_ttl_minutes)
        claw.save(update_fields=['status', 'provisioned_at', 'last_active_at', 'idle_recycle_after'])
        _log_event(claw, 'provisioned', 'Claw provisioned successfully')
        return True

    except Exception as e:
        logger.error(f'Provisioning failed for claw {claw.id} (owner {claw.owner_id}): {e}')
        claw.status = Claw.Status.FAILED
        claw.status_message = str(e)
        claw.save(update_fields=['status', 'status_message'])
        _log_event(claw, 'error', f'Provisioning failed: {e}')
        return False


def destroy_claw(claw: Claw) -> bool:
    """Delete all k8s resources for a claw (hard teardown)."""
    core_v1, _ = get_k8s_client()
    ns = claw.namespace
    try:
        core_v1.delete_namespace(name=ns)
        claw.status = Claw.Status.DELETED
        claw.save(update_fields=['status'])
        _log_event(claw, 'destroyed', 'Claw destroyed')
        return True
    except ApiException as e:
        if e.status == 404:
            claw.status = Claw.Status.DELETED
            claw.save(update_fields=['status'])
            return True
        logger.error(f'Destroy failed for {ns}: {e}')
        claw.status = Claw.Status.FAILED
        claw.status_message = str(e)
        claw.save(update_fields=['status', 'status_message'])
        return False


def begin_delete_claw(claw: Claw) -> bool:
    """
    Phase 1 of the HLD §4.2 two-phase delete: scale to 0, then — only if the scale
    succeeded — mark ``deleting`` and stamp ``delete_requested_at`` so the 60s undo
    window and the (follow-up) 24h Longhorn reclaim cron can act. The namespace is
    NOT torn down here.

    If the k8s scale-down fails (transient API/RBAC error), we do NOT enter the
    DELETING state and return False — otherwise the two-phase contract would
    report a claw as deleting while its workload is still running. A *missing*
    Deployment (404) is the exception: a partially-provisioned/failed claw has no
    running workload, so scale-to-0 is a no-op success and the delete proceeds.
    """
    if not _scale(claw, 0, ok_if_missing=True):
        claw.status_message = 'Delete aborted: failed to scale claw to 0'
        claw.save(update_fields=['status_message'])
        _log_event(claw, 'error', 'Delete requested but scale-to-0 failed; not entering undo window')
        return False
    claw.status = Claw.Status.DELETING
    claw.delete_requested_at = timezone.now()
    claw.save(update_fields=['status', 'delete_requested_at'])
    _log_event(claw, 'deleting', 'Delete requested; entered 60s undo window')
    return True


def undo_delete_claw(claw: Claw) -> bool:
    """Undo a pending delete within the 60s window (HLD §4.2) — back to ``stopped``."""
    claw.status = Claw.Status.STOPPED
    claw.delete_requested_at = None
    # Hard-delete uses the same DELETING/delete_requested_at shell while also
    # setting reclaim metadata. If callers hit the older undo-delete route for
    # that state, clear the reclaim intent too so the API cannot report a
    # successful undo while the backstop still hard-deletes the claw.
    claw.reclaim_now = False
    claw.reclaim_previous_status = ''
    claw.recycle_after = None
    claw.recycle_after_frozen_at = None
    claw.save(update_fields=[
        'status', 'delete_requested_at', 'reclaim_now',
        'reclaim_previous_status', 'recycle_after', 'recycle_after_frozen_at',
    ])
    _log_event(claw, 'undo_delete', 'Delete undone; claw stopped')
    return True


def start_claw(claw: Claw) -> bool:
    """Scale claw to 1 replica (HLD §5 start)."""
    if not _scale(claw, 1):
        return False
    now = timezone.now()
    claw.status = Claw.Status.RUNNING
    claw.last_active_at = now
    try:
        ttl = max(1, int(resolve_tier(claw.tier_id or 'free').idle_ttl_minutes))
    except Exception:
        ttl = _FREE_TIER_DEFAULT.idle_ttl_minutes
    claw.idle_recycle_after = now + timedelta(minutes=ttl)
    claw.save(update_fields=['status', 'last_active_at', 'idle_recycle_after'])
    _log_event(claw, 'started', 'Scaled to 1')
    return True


def stop_claw(claw: Claw, *, ok_if_missing: bool = False) -> bool:
    """Scale claw to 0 replicas; PVC retained (HLD §5 stop)."""
    if not _scale(claw, 0, ok_if_missing=ok_if_missing):
        return False
    claw.status = Claw.Status.STOPPED
    claw.save(update_fields=['status'])
    _log_event(claw, 'stopped', 'Scaled to 0')
    return True


def stop_claw_for_reclaim(claw: Claw) -> bool:
    """Scale a claw workload down before queuing hard-delete/reclaim.

    Unlike ``stop_claw()``, this does not persist STOPPED state: the caller will
    immediately persist DELETING/reclaim metadata after the scale-to-zero
    succeeds. A missing Deployment is treated as already stopped, matching the
    ordinary delete path.
    """
    if not _scale(claw, 0, ok_if_missing=True):
        claw.status_message = 'Hard delete aborted: failed to scale claw to 0'
        claw.save(update_fields=['status_message'])
        _log_event(claw, 'error', 'Hard delete requested but scale-to-0 failed; reclaim not queued')
        return False
    _log_event(claw, 'reclaim_stopped', 'Scaled to 0 before queuing hard delete')
    return True


def get_claw_status(claw: Claw) -> dict:
    """Get live status of a claw from k8s."""
    core_v1, _ = get_k8s_client()
    ns = claw.namespace
    result = {
        'status': claw.status,
        'namespace': ns,
        'pod_status': 'unknown',
        'pod_ip': None,
        'containers': 0,
    }
    try:
        pods = core_v1.list_namespaced_pod(ns, label_selector=f'app={APP_LABEL}')
        if pods.items:
            pod = pods.items[0]
            result['pod_status'] = pod.status.phase
            result['pod_ip'] = pod.status.pod_ip
            result['pod_name'] = pod.metadata.name
            if pod.status.container_statuses:
                result['containers'] = sum(1 for c in pod.status.container_statuses if c.ready)
    except ApiException:
        pass
    return result


# ── Internal helpers ──


def _scale(claw: Claw, replicas: int, ok_if_missing: bool = False) -> bool:
    _, apps_v1 = get_k8s_client()
    try:
        apps_v1.patch_namespaced_deployment_scale(
            name=DEPLOYMENT_NAME,
            namespace=claw.namespace,
            body={'spec': {'replicas': replicas}},
        )
        return True
    except ApiException as e:
        # A missing Deployment (404) means there is no workload to scale. On the
        # delete path (scale-to-0) that's a success — nothing is running, so a
        # partially-provisioned/failed claw can still be torn down. Scale-up keeps
        # treating a missing Deployment as a failure.
        if e.status == 404 and ok_if_missing:
            logger.warning(
                f'Scale to {replicas} for {claw.namespace}: deployment missing (404), treating as no-op'
            )
            return True
        logger.error(f'Scale to {replicas} failed for {claw.namespace}: {e}')
        return False


def _ensure_namespace(core_v1, ns: str, claw: Claw):
    try:
        core_v1.read_namespace(name=ns)
    except ApiException as e:
        if e.status == 404:
            core_v1.create_namespace(
                body=client.V1Namespace(
                    metadata=client.V1ObjectMeta(
                        name=ns,
                        labels={
                            'app.kubernetes.io/managed-by': 'shizuha-hive',
                            'shizuha.io/owner-id': str(claw.owner_id),
                            'shizuha.io/claw-id': claw.id.hex,
                        },
                    )
                )
            )
            logger.info(f'Created namespace {ns}')
        else:
            raise
    _harden_default_service_account(core_v1, ns)


def _harden_default_service_account(core_v1, ns: str):
    """Disable token automount on the namespace's default ServiceAccount (HIVE-127 §4).

    Defence-in-depth alongside the pod-spec ``automountServiceAccountToken:false``:
    even a pod that omitted the pod-level flag would not get a projected kube-API
    token in a claw namespace. Best-effort — the default SA is created
    asynchronously by the control plane, so a 404 here is tolerated (the pod-spec
    flag is the load-bearing control); any other API error is non-fatal to
    provisioning and only logged.
    """
    try:
        core_v1.patch_namespaced_service_account(
            name='default',
            namespace=ns,
            body={'automountServiceAccountToken': False},
        )
        logger.info(f'Disabled default SA token automount in {ns}')
    except ApiException as e:
        if e.status == 404:
            logger.info(f'Default SA not yet present in {ns}; pod-spec automount=false still enforces the boundary')
        else:
            logger.warning(f'Could not harden default SA in {ns}: {e.status} (pod-spec automount=false still applies)')


def build_claw_default_deny_network_policy(ns: str):
    """Fail-closed baseline network guard for a claw namespace."""
    return client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name='default-deny-all',
            namespace=ns,
            labels={'app.kubernetes.io/managed-by': 'shizuha-hive'},
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(),  # all pods
            policy_types=['Ingress', 'Egress'],
        ),
    )


def build_claw_dns_egress_network_policy(ns: str, claw: Claw):
    """Allow DNS for k8s resolution from claw pods."""
    return client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name='dns-egress',
            namespace=ns,
            labels={
                'app.kubernetes.io/managed-by': 'shizuha-hive',
                'shizuha.io/owner-id': str(claw.owner_id),
                'shizuha.io/claw-id': claw.id.hex,
            },
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(),  # all pods
            policy_types=['Egress'],
            egress=[
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(
                        namespace_selector=client.V1LabelSelector(
                            match_labels={'kubernetes.io/metadata.name': 'kube-system'},
                        ),
                        pod_selector=client.V1LabelSelector(
                            match_labels={'k8s-app': 'kube-dns'},
                        ),
                    )],
                    ports=[
                        client.V1NetworkPolicyPort(protocol='UDP', port=53),
                        client.V1NetworkPolicyPort(protocol='TCP', port=53),
                    ],
                ),
            ],
        ),
    )


def build_claw_platform_egress_network_policy(ns: str, claw: Claw):
    """Allow outbound calls to in-cluster control-plane services."""
    return client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name='platform-egress',
            namespace=ns,
            labels={
                'app.kubernetes.io/managed-by': 'shizuha-hive',
                'shizuha.io/owner-id': str(claw.owner_id),
                'shizuha.io/claw-id': claw.id.hex,
            },
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(),  # all pods
            policy_types=['Egress'],
            egress=[
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(
                        namespace_selector=client.V1LabelSelector(
                            match_labels={'kubernetes.io/metadata.name': 'shizuha-hive'},
                        ),
                    )],
                    ports=[
                        client.V1NetworkPolicyPort(protocol='TCP', port=8030),
                        client.V1NetworkPolicyPort(protocol='TCP', port=80),
                        client.V1NetworkPolicyPort(protocol='TCP', port=443),
                    ],
                ),
            ],
        ),
    )


def build_claw_connect_egress_network_policy(ns: str, claw: Claw):
    """Allow SCLI claw pods to reach Connect's AgentChatConsumer WS in-cluster.

    HIVE-1957: SCLI claws receive DMs via the runtime's WebSocket ConnectClient
    (no /v1/connect/messages HTTP bridge). The client dials the in-cluster
    Connect service (shizuha-connect.shizuha.svc.cluster.local:8019) directly —
    the public platform URL resolves to a Tailscale IP (100.64.0.3) that the
    wan-egress policy excludes. This rule opens egress to the shizuha namespace
    on the Connect WS port for SCLI claws only (OpenClaw/Hermes use the HTTP
    bridge and need no Connect egress).
    """
    connect_port = int(getattr(settings, 'HIVE_SCLI_CONNECT_WS_PORT', 8019))
    return client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name='connect-egress',
            namespace=ns,
            labels={
                'app.kubernetes.io/managed-by': 'shizuha-hive',
                'shizuha.io/owner-id': str(claw.owner_id),
                'shizuha.io/claw-id': claw.id.hex,
            },
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(),  # all pods
            policy_types=['Egress'],
            egress=[
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(
                        namespace_selector=client.V1LabelSelector(
                            match_labels={'kubernetes.io/metadata.name': 'shizuha'},
                        ),
                    )],
                    ports=[
                        client.V1NetworkPolicyPort(protocol='TCP', port=connect_port),
                    ],
                ),
            ],
        ),
    )


def build_claw_cortex_egress_network_policy(ns: str, claw: Claw):
    """Allow hosted assistants to reach the in-cluster Cortex inference API."""
    return client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name='cortex-egress',
            namespace=ns,
            labels={
                'app.kubernetes.io/managed-by': 'shizuha-hive',
                'shizuha.io/owner-id': str(claw.owner_id),
                'shizuha.io/claw-id': claw.id.hex,
            },
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(),  # all pods
            policy_types=['Egress'],
            egress=[
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(
                        namespace_selector=client.V1LabelSelector(
                            match_labels={'kubernetes.io/metadata.name': 'shizuha-cortex'},
                        ),
                    )],
                    ports=[
                        client.V1NetworkPolicyPort(protocol='TCP', port=8040),
                    ],
                ),
            ],
        ),
    )


def build_claw_wan_egress_network_policy(ns: str, claw: Claw):
    """Allow hosted claw pods outbound access to the PUBLIC internet (HIVE-366).

    Customers must be able to hand a hosted agent a public repo/URL (GitHub HTTPS,
    pip, npm, etc.) and get help without pasting code by hand. Egress is opened to
    ``0.0.0.0/0`` on 80/443, but the ipBlock ``except`` list carves out every
    private/cluster/link-local/CGNAT range, so public-WAN reachability does NOT
    imply reachability of in-cluster or private-network services (AC4). Tenant
    isolation stays enforced by ``default-deny-all`` + ``owner-isolation`` (cross-
    owner ingress deny) and the DNS/platform/cortex egress rules are unchanged.
    Privileged fleet agents run in ``rt-fleet``/``rt-8``/``shizuha-runtime`` (never
    ``claw-*``), so this policy does not alter their access (AC5). Default-on for
    hosted claws; a future per-claw/tier opt-out can gate inclusion in the bundle.

    PLAT-8489: a second, dedicated rule permits the s1 bridge services (ori MCP
    :9500, google-drive MCP :9600, fleet-control socat :8016) — hostNetwork pods
    on s1, reached via the tailnet address (the WAN rule above excludes
    100.64.0.0/10) or the node LAN IP. Scoped to /32 node-set CIDRs and exactly
    the three bridge ports so the carve-out cannot widen into general tailnet or
    LAN reach. The hosted-claw WAN-egress smoke asserts these as MUST-CONNECT
    (HIVE-402 AC6 extension) — without this rule the smoke fails closed.
    """
    return client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name='wan-egress',
            namespace=ns,
            labels={
                'app.kubernetes.io/managed-by': 'shizuha-hive',
                'shizuha.io/owner-id': str(claw.owner_id),
                'shizuha.io/claw-id': claw.id.hex,
            },
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(),  # all pods
            policy_types=['Egress'],
            egress=[
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(
                        ip_block=client.V1IPBlock(
                            cidr='0.0.0.0/0',
                            # Carve out private/cluster ranges so WAN egress can
                            # never reach in-cluster or private-network services.
                            _except=[
                                '10.0.0.0/8',      # RFC1918 + k3s pod/svc CIDRs (10.42/10.43)
                                '172.16.0.0/12',   # RFC1918
                                '192.168.0.0/16',  # RFC1918
                                '169.254.0.0/16',  # link-local / cloud metadata
                                '100.64.0.0/10',   # CGNAT / Tailscale tailnet
                            ],
                        ),
                    )],
                    ports=[
                        client.V1NetworkPolicyPort(protocol='TCP', port=80),
                        client.V1NetworkPolicyPort(protocol='TCP', port=443),
                    ],
                ),
                client.V1NetworkPolicyEgressRule(  # s1 bridge services (PLAT-8489)
                    to=[
                        client.V1NetworkPolicyPeer(ip_block=client.V1IPBlock(cidr='100.64.0.3/32')),
                        client.V1NetworkPolicyPeer(ip_block=client.V1IPBlock(cidr='192.168.0.136/32')),
                        client.V1NetworkPolicyPeer(ip_block=client.V1IPBlock(cidr='192.168.88.136/32')),
                    ],
                    ports=[
                        client.V1NetworkPolicyPort(protocol='TCP', port=9500),
                        client.V1NetworkPolicyPort(protocol='TCP', port=9600),
                        client.V1NetworkPolicyPort(protocol='TCP', port=8016),
                    ],
                ),
            ],
        ),
    )


def build_claw_dashboard_ingress_network_policy(ns: str, claw: Claw):
    """Allow the shared Hive nginx proxy to reach the claw dashboard gateway."""
    http_port = int(getattr(settings, 'HIVE_CLAW_HTTP_PORT', 18789))
    return client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name='dashboard-ingress',
            namespace=ns,
            labels={
                'app.kubernetes.io/managed-by': 'shizuha-hive',
                'shizuha.io/owner-id': str(claw.owner_id),
                'shizuha.io/claw-id': claw.id.hex,
            },
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(),  # all pods in this claw ns
            policy_types=['Ingress'],
            ingress=[
                client.V1NetworkPolicyIngressRule(
                    _from=[
                        client.V1NetworkPolicyPeer(
                            namespace_selector=client.V1LabelSelector(
                                match_labels={'kubernetes.io/metadata.name': 'shizuha'},
                            ),
                            pod_selector=client.V1LabelSelector(
                                match_labels={'app.kubernetes.io/name': 'shizuha-nginx'},
                            ),
                        ),
                    ],
                    ports=[client.V1NetworkPolicyPort(protocol='TCP', port=http_port)],
                ),
            ],
        ),
    )


def build_owner_isolation_network_policy(ns: str, claw: Claw):
    """Per-owner namespace boundary for a claw (HIVE-127 §4).

    Default-deny ingress: NO pod in another owner's namespace can reach this
    claw's pods, so cross-owner namespace traffic is denied by default. Only
    same-namespace ingress (the claw's own broker sidecar <-> agent path) is
    allowed. This is the k8s layer of the tenancy property — isolation is enforced
    below the app layer too, mirroring the ``claw-<owner>`` model. Egress is left
    to the existing platform/LLM allow rules; the load-bearing rule here is the
    cross-owner ingress deny.
    """
    return client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name='owner-isolation',
            namespace=ns,
            labels={
                'app.kubernetes.io/managed-by': 'shizuha-hive',
                'shizuha.io/owner-id': str(claw.owner_id),
            },
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(),  # all pods in this claw ns
            policy_types=['Ingress'],
            # Single ingress rule with an empty namespaceSelector matching ONLY this
            # namespace's own pods — any other owner's namespace is denied.
            ingress=[
                client.V1NetworkPolicyIngressRule(
                    _from=[
                        client.V1NetworkPolicyPeer(
                            namespace_selector=client.V1LabelSelector(
                                match_labels={'kubernetes.io/metadata.name': ns},
                            ),
                        ),
                    ],
                ),
            ],
        ),
    )


def build_claw_resource_quota(ns: str, claw: Claw, tier: TierSpec):
    """Namespace-level resource boundaries for a claw namespace."""
    return client.V1ResourceQuota(
        metadata=client.V1ObjectMeta(
            name='claw-resource-boundaries',
            namespace=ns,
            labels={
                'app.kubernetes.io/managed-by': 'shizuha-hive',
                'shizuha.io/owner-id': str(claw.owner_id),
                'shizuha.io/claw-id': claw.id.hex,
            },
        ),
        spec=client.V1ResourceQuotaSpec(
            hard={
                'pods': '2',
                'services': '2',
                'persistentvolumeclaims': '2',
                'requests.cpu': tier.cpu_request,
                'requests.memory': tier.mem_request,
                'limits.cpu': tier.cpu_limit,
                'limits.memory': tier.mem_limit,
                'requests.storage': tier.pvc_size,
            },
        ),
    )


def build_claw_limit_range(ns: str, claw: Claw, tier: TierSpec):
    """Per-namespace container resource defaults for claw pods."""
    return client.V1LimitRange(
        metadata=client.V1ObjectMeta(
            name='claw-limit-range',
            namespace=ns,
            labels={
                'app.kubernetes.io/managed-by': 'shizuha-hive',
                'shizuha.io/owner-id': str(claw.owner_id),
                'shizuha.io/claw-id': claw.id.hex,
            },
        ),
        spec=client.V1LimitRangeSpec(
            limits=[
                client.V1LimitRangeItem(
                    type='Container',
                    min={'cpu': '10m', 'memory': '16Mi'},
                    max={
                        'cpu': tier.cpu_limit,
                        'memory': tier.mem_limit,
                    },
                    default_request={
                        'cpu': tier.cpu_request,
                        'memory': tier.mem_request,
                    },
                    default={
                        'cpu': tier.cpu_limit,
                        'memory': tier.mem_limit,
                    },
                ),
            ],
        ),
    )


def build_required_claw_network_policies(ns: str, claw: Claw, tier: TierSpec):
    """Return the canonical fail-closed NetworkPolicy bundle for a claw namespace."""
    policies = [
        build_claw_default_deny_network_policy(ns),
        build_claw_dns_egress_network_policy(ns, claw),
        build_claw_platform_egress_network_policy(ns, claw),
        build_claw_cortex_egress_network_policy(ns, claw),
        build_claw_wan_egress_network_policy(ns, claw),
        build_claw_dashboard_ingress_network_policy(ns, claw),
        build_owner_isolation_network_policy(ns, claw),
    ]
    # HIVE-1957: SCLI claws dial Connect's AgentChatConsumer WS in-cluster
    # (no /v1/connect/messages HTTP bridge) — grant Connect egress for them.
    if getattr(claw, 'agent_type', Claw.AgentType.CLAW) == Claw.AgentType.SCLI:
        policies.append(build_claw_connect_egress_network_policy(ns, claw))
    return tuple(policies)


def _ensure_network_policies(core_v1, ns: str, claw: Claw, tier: TierSpec):
    """Apply claw namespace network policies (fail-closed)."""
    policies = build_required_claw_network_policies(ns, claw, tier)
    net_v1 = client.NetworkingV1Api()
    for policy in policies:
        try:
            net_v1.create_namespaced_network_policy(ns, policy)
            logger.info(f'Applied network policy {policy.metadata.name} in {ns}')
        except ApiException as e:
            if e.status == 409:
                existing = net_v1.read_namespaced_network_policy(policy.metadata.name, ns)
                policy.metadata.resource_version = existing.metadata.resource_version
                net_v1.replace_namespaced_network_policy(policy.metadata.name, ns, policy)
            else:
                raise


def _ensure_resource_guards(core_v1, ns: str, claw: Claw, tier: TierSpec):
    """Create/update per-namespace ResourceQuota + LimitRange objects."""
    quota = build_claw_resource_quota(ns, claw, tier)
    limit_range = build_claw_limit_range(ns, claw, tier)
    try:
        core_v1.create_namespaced_resource_quota(ns, quota)
    except ApiException as e:
        if e.status == 409:
            existing = core_v1.read_namespaced_resource_quota(quota.metadata.name, ns)
            quota.metadata.resource_version = existing.metadata.resource_version
            core_v1.replace_namespaced_resource_quota('claw-resource-boundaries', ns, quota)
        else:
            raise
    try:
        core_v1.create_namespaced_limit_range(ns, limit_range)
    except ApiException as e:
        if e.status == 409:
            existing = core_v1.read_namespaced_limit_range(limit_range.metadata.name, ns)
            limit_range.metadata.resource_version = existing.metadata.resource_version
            core_v1.replace_namespaced_limit_range('claw-limit-range', ns, limit_range)
        else:
            raise


def active_claws_queryset():
    """Claw rows whose namespaces must keep tenant-isolation controls present."""
    return Claw.objects.exclude(status=Claw.Status.DELETED).order_by('owner_id', 'created_at')


def cleanup_stale_duplicate_claw_namespaces(core_v1, apps_v1, owner_id: int, *, dry_run=True) -> dict:
    """Audit/delete Hive-managed claw namespaces no active DB claw owns.

    CON-161: duplicate stale OpenClaw Deployments can race the primary
    WhatsApp Web session if cloned credentials are present. A namespace is safe
    to clean when it is labelled as Hive-managed for this owner but its
    ``shizuha.io/claw-id`` is absent from the non-deleted Claw table. Intentional
    multi-claw owners remain untouched because each active claw id is retained.
    """
    owner = str(owner_id)
    active_ids = set(
        Claw.objects.exclude(status=Claw.Status.DELETED)
        .filter(owner_id=owner_id)
        .values_list('id', flat=True)
    )
    active_hex = {str(claw_id).replace('-', '') for claw_id in active_ids}
    selector = f'app.kubernetes.io/managed-by=shizuha-hive,shizuha.io/owner-id={owner}'
    items = core_v1.list_namespace(label_selector=selector).items
    stale = []
    kept = []
    errors = []
    for ns_obj in items:
        meta = ns_obj.metadata
        labels = meta.labels or {}
        ns = meta.name
        claw_id = str(labels.get('shizuha.io/claw-id') or '').replace('-', '')
        row = {'namespace': ns, 'claw_id': claw_id or None}
        if claw_id and claw_id in active_hex:
            kept.append(row)
            continue
        stale.append(row)
        if dry_run:
            continue
        try:
            try:
                apps_v1.patch_namespaced_deployment_scale(DEPLOYMENT_NAME, ns, {'spec': {'replicas': 0}})
            except ApiException as exc:
                if exc.status != 404:
                    raise
            core_v1.delete_namespace(name=ns)
        except Exception as exc:  # pragma: no cover - exact k8s client errors vary
            errors.append({'namespace': ns, 'error': str(exc)})
    errored = {e['namespace'] for e in errors}
    return {
        'owner_id': owner_id,
        'checked': len(items),
        'kept': kept,
        'stale': stale,
        'deleted': [] if dry_run else [row for row in stale if row['namespace'] not in errored],
        'errors': errors,
        'dry_run': dry_run,
    }


def _read_required_network_policy_names(ns: str) -> set[str]:
    net_v1 = client.NetworkingV1Api()
    return {item.metadata.name for item in net_v1.list_namespaced_network_policy(ns).items}


def audit_claw_namespace_isolation(core_v1, ns: str) -> list[str]:
    """Return missing tenant-boundary controls for an existing claw namespace.

    Empty list means the namespace has the required fail-closed NetworkPolicy
    bundle, quota, limit range, and default ServiceAccount token hardening.
    """
    missing = []
    try:
        core_v1.read_namespace(name=ns)
    except ApiException as exc:
        if exc.status == 404:
            return ['namespace']
        raise

    policy_names = _read_required_network_policy_names(ns)
    for name in REQUIRED_CLAW_NETWORK_POLICIES:
        if name not in policy_names:
            missing.append(f'networkpolicy/{name}')

    try:
        core_v1.read_namespaced_resource_quota(REQUIRED_CLAW_RESOURCE_QUOTA, ns)
    except ApiException as exc:
        if exc.status == 404:
            missing.append(f'resourcequota/{REQUIRED_CLAW_RESOURCE_QUOTA}')
        else:
            raise

    try:
        core_v1.read_namespaced_limit_range(REQUIRED_CLAW_LIMIT_RANGE, ns)
    except ApiException as exc:
        if exc.status == 404:
            missing.append(f'limitrange/{REQUIRED_CLAW_LIMIT_RANGE}')
        else:
            raise

    try:
        default_sa = core_v1.read_namespaced_service_account('default', ns)
    except ApiException as exc:
        if exc.status == 404:
            missing.append('serviceaccount/default')
        else:
            raise
    else:
        if getattr(default_sa, 'automount_service_account_token', None) is not False:
            missing.append('serviceaccount/default/automountServiceAccountToken=false')

    return missing


def reconcile_claw_namespace_isolation(*, dry_run=False, owner_id=None, claw_id=None) -> dict:
    """Audit and optionally backfill tenant-boundary controls for active claws.

    Used by the HIVE-326 management command and health signal so namespaces
    created before newer hardening cannot silently drift forever.
    """
    core_v1, _ = get_k8s_client()
    qs = active_claws_queryset()
    if owner_id is not None:
        qs = qs.filter(owner_id=owner_id)
    if claw_id is not None:
        qs = qs.filter(id=claw_id)

    summary = {
        'checked': 0,
        'drifted': 0,
        'reconciled': 0,
        'still_missing': 0,
        'dry_run': bool(dry_run),
        'issues': [],
    }
    for claw in qs:
        summary['checked'] += 1
        ns = claw.namespace
        before = audit_claw_namespace_isolation(core_v1, ns)
        if before:
            summary['drifted'] += 1
        after = before
        if before and not dry_run:
            tier = resolve_tier(claw.tier_id or 'free', core_v1)
            _ensure_namespace(core_v1, ns, claw)
            _ensure_network_policies(core_v1, ns, claw, tier)
            _ensure_resource_guards(core_v1, ns, claw, tier)
            _harden_default_service_account(core_v1, ns)
            after = audit_claw_namespace_isolation(core_v1, ns)
            if not after:
                summary['reconciled'] += 1

        if after:
            summary['still_missing'] += 1
            summary['issues'].append({
                'claw_id': str(claw.id),
                'owner_id': claw.owner_id,
                'namespace': ns,
                'missing_before': before,
                'missing_after': after,
            })
        elif before:
            summary['issues'].append({
                'claw_id': str(claw.id),
                'owner_id': claw.owner_id,
                'namespace': ns,
                'missing_before': before,
                'missing_after': [],
            })
    return summary


def summarize_claw_namespace_isolation_health(limit=10) -> dict:
    """Health endpoint payload for claw namespace isolation drift."""
    try:
        summary = reconcile_claw_namespace_isolation(dry_run=True)
    except Exception as exc:
        logger.exception('claw namespace isolation health check failed')
        return {'ok': False, 'error': str(exc), 'checked': 0, 'issues': []}
    ok = summary['still_missing'] == 0
    issues = summary['issues'][:limit]
    return {
        'ok': ok,
        'checked': summary['checked'],
        'drifted': summary['drifted'],
        'missing': summary['still_missing'],
        'issues': issues,
    }


def get_claw_secret(claw: Claw, key: str) -> str:
    """Read a key from the claw-config secret in the claw's namespace."""
    core_v1, _ = get_k8s_client()
    ns = getattr(claw, 'namespace', '')
    if not ns:
        raise RuntimeError(f'claw {claw.id} has no namespace')
    try:
        secret = core_v1.read_namespaced_secret(name='claw-config', namespace=ns)
    except ApiException as e:
        raise RuntimeError(f'failed to read claw-config secret for claw {claw.id}: {e}')
    data = secret.data or {}
    raw = data.get(key, '')
    if not raw:
        raise RuntimeError(f'key {key} not found in claw-config secret for claw {claw.id}')
    import base64
    return base64.b64decode(raw).decode('utf-8')


def _create_claw_secret(
    core_v1, ns: str, claw: Claw, user_token: str,
    cortex_base: str = '', cortex_key: str = '', cortex_model: str = '',
):
    """Platform credentials + claw config (near-native OpenClaw, no agent account)."""
    daemon_config = json.dumps({'platformUrl': claw.platform_url})
    auth_config = json.dumps({
        'username': claw.owner_username,
        'accessToken': user_token,
        'idApiBaseUrl': claw.platform_url,
    })

    internal_auth_token = lifecycle.internal_auth_token(claw)
    gateway_auth_token = lifecycle.gateway_auth_token(claw)
    if not internal_auth_token:
        raise RuntimeError('HIVE_INTERNAL_AUTH_TOKEN is required to provision claw internal auth')
    if not gateway_auth_token:
        raise RuntimeError('gateway auth token is required to provision claw gateway auth')

    string_data = {
        'daemon.json': daemon_config,
        'auth.json': auth_config,
        # Per-claw service credential for OpenClaw -> hive-api handshakes
        # such as /v1/internal/claws/<id>/auth-state. It is derived from
        # the hive-api signing secret plus this claw's id/namespace, so a
        # tenant pod cannot replay its mounted value against another claw.
        'internal-auth-token': internal_auth_token,
        # PLAT-905: per-claw OpenClaw gateway bind token. Newer gateways
        # refuse to bind to the LAN without auth (exit 78 -> CrashLoopBackOff);
        # injected as OPENCLAW_GATEWAY_TOKEN into the claw container below.
        'gateway-auth-token': gateway_auth_token,
        # HIVE-1957: owner-scoped Connect identity token for the SCLI runtime's
        # WebSocket ConnectClient. Same value as auth.json's accessToken (the
        # owner's shizuha-id JWT passed at provision time). Injected into SCLI
        # claw pods as AGENT_ACCESS_TOKEN so the runtime can connect to Connect's
        # AgentChatConsumer WS and receive DMs directly (SCLI claws have no
        # /v1/connect/messages HTTP bridge — they route via the WS ConnectClient).
        'connect-access-token': user_token,
    }
    if cortex_key:
        string_data['cortex-api-key'] = cortex_key
    if cortex_base:
        string_data['cortex-base-url'] = cortex_base
    if cortex_model:
        string_data['cortex-model'] = cortex_model

    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name='claw-config',
            namespace=ns,
            labels={'app.kubernetes.io/managed-by': 'shizuha-hive'},
        ),
        string_data=string_data,
    )
    try:
        core_v1.create_namespaced_secret(ns, secret)
    except ApiException as e:
        if e.status == 409:
            core_v1.replace_namespaced_secret('claw-config', ns, secret)
        else:
            raise


def _create_claw_pvc(core_v1, ns: str, claw: Claw, tier: TierSpec):
    storage_class = getattr(settings, 'HIVE_CLAW_STORAGE_CLASS', 'longhorn-3x')
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name='claw-data',
            namespace=ns,
            labels={'app.kubernetes.io/managed-by': 'shizuha-hive'},
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=['ReadWriteOnce'],
            storage_class_name=storage_class,
            resources=client.V1VolumeResourceRequirements(
                requests={'storage': tier.pvc_size},
            ),
        ),
    )
    try:
        core_v1.create_namespaced_persistent_volume_claim(ns, pvc)
    except ApiException as e:
        if e.status != 409:
            raise


def _build_cortex_provider_seed_js(cortex_base: str, cortex_key: str, cortex_model: str) -> str:
    """Return JS that merges a Cortex provider into OpenClaw config."""
    if not (cortex_key and cortex_model):
        return ''
    primary = f'cortex/{cortex_model}'
    return (
        'const cortexKey=process.env.CORTEX_API_KEY||"";'
        'const cortexBase=process.env.CORTEX_BASE_URL||'
        f'{json.dumps(cortex_base)};'
        'const cortexModel=process.env.CORTEX_MODEL||'
        f'{json.dumps(cortex_model)};'
        'if(!cortexKey){console.error("CORTEX_API_KEY missing; skipped Cortex provider seed");}'
        'else{'
        'delete c.heartbeat;'
        'if(typeof c.models!=="object"||c.models===null||Array.isArray(c.models)){c.models={};}'
        'c.models.mode="merge";'
        'if(typeof c.models.providers!=="object"||c.models.providers===null||Array.isArray(c.models.providers)){c.models.providers={};}'
        'const isDeepSeekV4Flash=/^DeepSeek-V4-Flash$/i.test(cortexModel);'
        'const cortexContext=isDeepSeekV4Flash?262144:131072;'
        'const cortexModelDef={id:cortexModel,name:cortexModel,'
        'reasoning:isDeepSeekV4Flash||/r1|reasoning|think|reason/i.test(cortexModel),'
        'input:["text"],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},'
        'contextWindow:cortexContext,contextTokens:cortexContext,maxTokens:cortexContext,'
        'compat:{supportsTools:true,supportsUsageInStreaming:true,supportsDeveloperRole:false}};'
        'c.models.providers.cortex={baseUrl:cortexBase,apiKey:cortexKey,api:"openai-completions",'
        'request:{allowPrivateNetwork:true},'
        'models:[cortexModelDef]};'
        'if(typeof c.agents!=="object"||c.agents===null||Array.isArray(c.agents)){c.agents={};}'
        'if(typeof c.agents.defaults!=="object"||c.agents.defaults===null||Array.isArray(c.agents.defaults)){c.agents.defaults={};}'
        'c.agents.defaults.model={primary:"cortex/"+cortexModel};'
        'c.agents.defaults.heartbeat=Object.assign({},c.agents.defaults.heartbeat||{},'
        '{every:"0m",directPolicy:"block"});'
        'dirty=true;'
        f'console.log("merged cortex provider primary={primary}");'
        '}'
    )


def _create_claw_deployment(
    apps_v1, ns: str, claw: Claw, tier: TierSpec,
    cortex_base: str = '', cortex_key: str = '', cortex_model: str = '',
    runtime: RuntimeResolution | None = None,
):
    """
    Single-replica ``Recreate`` Deployment (Charter D4). The container image is
    configurable via ``HIVE_CLAW_IMAGE`` and defaults to the HIVE-21
    multi-arch OpenClaw artifact. The PVC is mounted at OpenClaw's real home
    path so migrated WhatsApp/OAuth state is read in-place.
    """
    is_scli = getattr(claw, 'agent_type', Claw.AgentType.CLAW) == Claw.AgentType.SCLI
    runtime = runtime or resolve_claw_runtime(claw)
    claw_image, image_pull_policy = runtime.image, runtime.pull_policy
    data_mount_path = getattr(settings, 'HIVE_CLAW_DATA_MOUNT_PATH', '/home/node/.openclaw')
    http_port = int(getattr(settings, 'HIVE_CLAW_HTTP_PORT', 18789))

    # HIVE-31 (backport of the HIVE-9 §4.5 live fix): the multi-arch OpenClaw
    # gateway exits 78 ("Missing config / set gateway.mode=local") unless
    # gateway.mode is set, so every claw must boot with it. Seed it into the
    # PVC's openclaw.json from an initContainer, idempotently:
    #   - file absent            -> create {"gateway": {"mode": "local"}}
    #   - file valid, mode unset -> merge gateway.mode=local, keep the rest
    #   - file valid, mode set   -> leave untouched (operator override wins)
    #   - file present, invalid  -> DO NOT clobber; warn and let the claw
    #     container surface its own config error (fail-open, never destroy).
    # Runs `node -e` from the claw image itself — no extra image, multi-arch.
    config_path = f'{data_mount_path}/openclaw.json'
    # HIVE-201 E2: also seed the notify_owner MCP server (pinned file + a
    # mcp.servers entry) in the SAME idempotent initContainer. Env (claw id,
    # backend url, per-claw token) is written into the server's env block from
    # the initContainer's own env — robust regardless of whether OpenClaw's MCP
    # host passes parent env to stdio children, and consistent with the existing
    # secrets-in-openclaw.json pattern (the cortex apiKey already lives there).
    notify_dir = f'{data_mount_path}/{_NOTIFY_OWNER_DIR}'
    notify_path = f'{notify_dir}/server.mjs'
    notify_b64 = base64.b64encode(_notify_owner_server_source().encode('utf-8')).decode('ascii')
    wa_hook_b64 = base64.b64encode(_wa_watchdog_hook_source().encode('utf-8')).decode('ascii')
    wa_hook_md_b64 = base64.b64encode(_wa_watchdog_hook_md().encode('utf-8')).decode('ascii')
    cortex_seed_js = '' if is_scli else _build_cortex_provider_seed_js(cortex_base, cortex_key, cortex_model)
    init_gateway_script = (
        'const fs=require("fs");'
        f'const p={json.dumps(config_path)};'
        'let c={},dirty=false;'
        'if(fs.existsSync(p)){'
        'try{c=JSON.parse(fs.readFileSync(p,"utf8"))}'
        'catch(e){console.error("openclaw.json unparseable; leaving untouched: "+e);process.exit(0)}'
        '}'
        # Fail-open guard also covers PARSEABLE-but-invalid contents (null, a
        # string, an array, or a non-object gateway): mutating those would
        # throw and crashloop the initContainer. Same policy as unparseable —
        # leave the file untouched and let the claw surface its config error.
        'if(c===null||typeof c!=="object"||Array.isArray(c)||'
        '(c.gateway!==undefined&&(typeof c.gateway!=="object"||c.gateway===null||Array.isArray(c.gateway)))){'
        'console.error("openclaw.json exists but is not a valid config object; leaving untouched");process.exit(0)}'
        'c.gateway=c.gateway||{};'
        'if(!c.gateway.mode){c.gateway.mode="local";dirty=true;'
        'console.log("seeded gateway.mode=local");}'
        'if(c.gateway.controlUi===undefined||(c.gateway.controlUi!==null&&typeof c.gateway.controlUi==="object"&&!Array.isArray(c.gateway.controlUi))){'
        'c.gateway.controlUi=c.gateway.controlUi||{};'
        'const origins=Array.isArray(c.gateway.controlUi.allowedOrigins)?c.gateway.controlUi.allowedOrigins.slice():[];'
        'const platform=(process.env.SHIZUHA_PLATFORM_URL||"").replace(/\\/$/,"");'
        'for(const origin of [platform,"https://shizuha.com"]){'
        'if(origin&&!origins.includes(origin)){origins.push(origin);dirty=true;console.log("allowed OpenClaw Control UI origin "+origin);}}'
        'c.gateway.controlUi.allowedOrigins=origins;'
        'if(c.gateway.controlUi.dangerouslyDisableDeviceAuth!==true){'
        'c.gateway.controlUi.dangerouslyDisableDeviceAuth=true;dirty=true;'
        'console.log("disabled OpenClaw Control UI device-pairing for Hive owner-routed dashboard");}'
        '}else{console.error("gateway.controlUi is not an object; skipped allowedOrigins seed");}'
        + cortex_seed_js +
        # ── HIVE-201 E2: notify_owner MCP server (pinned file, idempotent) ──
        f'const ND={json.dumps(notify_dir)},NP={json.dumps(notify_path)},NV={json.dumps(_NOTIFY_OWNER_VERSION)};'
        'try{fs.mkdirSync(ND,{recursive:true});'
        'let cur=null;try{cur=fs.readFileSync(ND+"/.version","utf8").trim()}catch(e){}'
        'if(cur!==NV){'
        f'fs.writeFileSync(NP,Buffer.from({json.dumps(notify_b64)},"base64").toString("utf8"));'
        'fs.writeFileSync(ND+"/.version",NV+"\\n");'
        'console.log("seeded notify_owner server.mjs v"+NV);}'
        '}catch(e){console.error("notify_owner file seed failed (non-fatal): "+e)}'
        # Register mcp.servers.notify_owner — touch ONLY our key (no-clobber of
        # other servers); skip cleanly if mcp / mcp.servers is a non-object.
        'if(c.mcp===undefined||(c.mcp!==null&&typeof c.mcp==="object"&&!Array.isArray(c.mcp))){'
        'c.mcp=c.mcp||{};'
        'if(c.mcp.servers===undefined||(c.mcp.servers!==null&&typeof c.mcp.servers==="object"&&!Array.isArray(c.mcp.servers))){'
        'c.mcp.servers=c.mcp.servers||{};'
        'const want={command:"node",args:[NP],env:{'
        'HIVE_CLAW_ID:process.env.HIVE_CLAW_ID||"",'
        'BACKEND_URL:process.env.BACKEND_URL||process.env.SHIZUHA_PLATFORM_URL||"",'
        'HIVE_INTERNAL_AUTH_TOKEN:process.env.HIVE_INTERNAL_AUTH_TOKEN||""}};'
        'if(JSON.stringify(c.mcp.servers.notify_owner)!==JSON.stringify(want)){'
        'c.mcp.servers.notify_owner=want;dirty=true;console.log("registered mcp.servers.notify_owner");}'
        '}else{console.error("mcp.servers is not an object; skipped notify_owner registration");}'
        '}else{console.error("mcp is not an object; skipped notify_owner registration");}'
        # ── CON-161: WhatsApp link/drop watchdog hook (owner notification + probe keep-alive) ──
        f'const WHD={json.dumps(f"{data_mount_path}/{_WA_WATCHDOG_DIR}")},WHV={json.dumps(_WA_WATCHDOG_VERSION)};'
        'try{fs.mkdirSync(WHD,{recursive:true});'
        'let wv=null;try{wv=fs.readFileSync(WHD+"/.version","utf8").trim()}catch(e){}'
        'if(wv!==WHV){'
        f'fs.writeFileSync(WHD+"/handler.ts",Buffer.from({json.dumps(wa_hook_b64)},"base64").toString("utf8"));'
        f'fs.writeFileSync(WHD+"/HOOK.md",Buffer.from({json.dumps(wa_hook_md_b64)},"base64").toString("utf8"));'
        'fs.writeFileSync(WHD+"/.version",WHV+"\\n");console.log("seeded whatsapp-link-watchdog hook v"+WHV);}'
        '}catch(e){console.error("whatsapp watchdog hook seed failed (non-fatal): "+e)}'
        'if(c.hooks===undefined||(c.hooks!==null&&typeof c.hooks==="object"&&!Array.isArray(c.hooks))){'
        'c.hooks=c.hooks||{};'
        'if(c.hooks.internal===undefined||(c.hooks.internal!==null&&typeof c.hooks.internal==="object"&&!Array.isArray(c.hooks.internal))){'
        'c.hooks.internal=c.hooks.internal||{};'
        'if(c.hooks.internal.enabled!==true){c.hooks.internal.enabled=true;dirty=true;console.log("enabled internal hooks for whatsapp watchdog");}'
        'c.hooks.internal.entries=c.hooks.internal.entries||{};'
        'const hookWant={enabled:true,env:{HIVE_CLAW_ID:process.env.HIVE_CLAW_ID||"",BACKEND_URL:process.env.BACKEND_URL||process.env.SHIZUHA_PLATFORM_URL||"",HIVE_INTERNAL_AUTH_TOKEN:process.env.HIVE_INTERNAL_AUTH_TOKEN||""}};'
        'if(JSON.stringify(c.hooks.internal.entries["whatsapp-link-watchdog"])!==JSON.stringify(hookWant)){'
        'c.hooks.internal.entries["whatsapp-link-watchdog"]=hookWant;dirty=true;console.log("enabled whatsapp-link-watchdog hook");}'
        '}else{console.error("hooks.internal is not an object; skipped whatsapp watchdog registration");}'
        '}else{console.error("hooks is not an object; skipped whatsapp watchdog registration");}'
        'if(dirty){fs.writeFileSync(p,JSON.stringify(c,null,2)+"\\n");console.log("openclaw.json updated -> "+p);}'
        'else{console.log("openclaw.json already current; no-op");}'
    )

    labels = {
        'app': APP_LABEL,
        'app.kubernetes.io/managed-by': 'shizuha-hive',
        'shizuha.io/owner-id': str(claw.owner_id),
        'shizuha.io/claw-id': claw.id.hex,
    }

    init_env = [
        client.V1EnvVar(name='HIVE_CLAW_ID', value=claw.id.hex),
        client.V1EnvVar(name='BACKEND_URL', value=claw.platform_url),
        client.V1EnvVar(
            name='HIVE_INTERNAL_AUTH_TOKEN',
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name='claw-config',
                    key='internal-auth-token',
                )
            ),
        ),
    ]
    if cortex_key:
        init_env.extend([
            client.V1EnvVar(
                name='CORTEX_API_KEY',
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name='claw-config',
                        key='cortex-api-key',
                    )
                ),
            ),
            client.V1EnvVar(
                name='CORTEX_BASE_URL',
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name='claw-config',
                        key='cortex-base-url',
                    )
                ),
            ),
            client.V1EnvVar(
                name='CORTEX_MODEL',
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name='claw-config',
                        key='cortex-model',
                    )
                ),
            ),
        ])

    init_containers = None if is_scli else [
        client.V1Container(
            name='init-gateway-config',
            image=claw_image,
            image_pull_policy=image_pull_policy,
            command=['node', '-e', init_gateway_script],
            # HIVE-201 E2: the init script writes these into the
            # notify_owner MCP server's env block in openclaw.json.
            env=init_env,
            volume_mounts=[
                client.V1VolumeMount(name='claw-data', mount_path=data_mount_path),
            ],
            resources=client.V1ResourceRequirements(
                requests={'memory': '32Mi', 'cpu': '10m'},
                limits={'memory': '128Mi', 'cpu': '250m'},
            ),
        ),
    ]

    container_env = [
        client.V1EnvVar(name='SHIZUHA_PLATFORM_URL', value=claw.platform_url),
        client.V1EnvVar(name='BACKEND_URL', value=claw.platform_url),
        client.V1EnvVar(name='PORT', value=str(http_port)),
        # PLAT-905: OpenClaw gateway refuses to bind to the LAN without auth.
        client.V1EnvVar(
            name='OPENCLAW_GATEWAY_TOKEN',
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name='claw-config',
                    key='gateway-auth-token',
                )
            ),
        ),
        client.V1EnvVar(name='HIVE_CLAW_ID', value=claw.id.hex),
        client.V1EnvVar(
            name='HIVE_INTERNAL_AUTH_TOKEN',
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name='claw-config',
                    key='internal-auth-token',
                )
            ),
        ),
        # HIVE-610: the runtime bridge uses this token to authenticate Connect
        # DM delivery requests from Hive. Same value as HIVE_INTERNAL_AUTH_TOKEN
        # (stored in the claw-config secret) — the bridge validates it via
        # validateConnectRuntimeAuth() which checks X-Connect-Runtime-Secret.
        client.V1EnvVar(
            name='CONNECT_RUNTIME_DELIVERY_TOKEN',
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name='claw-config',
                    key='internal-auth-token',
                )
            ),
        ),
    ]
    if is_scli:
        if cortex_base:
            container_env.append(client.V1EnvVar(name='CORTEX_BASE_URL', value=cortex_base))
        if cortex_model:
            container_env.append(client.V1EnvVar(name='MODEL', value=cortex_model))
        if cortex_key:
            container_env.append(client.V1EnvVar(
                name='CORTEX_API_KEY',
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name='claw-config',
                        key='cortex-api-key',
                    )
                ),
            ))
        # HIVE-1957: wire the SCLI runtime's WebSocket ConnectClient so the claw
        # connects to Connect's AgentChatConsumer and receives DMs directly.
        # SCLI claws have no /v1/connect/messages HTTP bridge — delivery routes
        # via the WS ConnectClient path. The owner-scoped Connect identity token
        # lives in the claw-config secret (connect-access-token, same value as
        # auth.json's accessToken) and is injected as AGENT_ACCESS_TOKEN.
        # SHIZUHA_CONNECT_AUTOREPLY=1 + SHIZUHA_CONNECT_REPLY_EMAIL deliver the
        # agent's turn output back to the owner's Connect conversation.
        container_env.append(client.V1EnvVar(name='AGENT_ID', value=claw.id.hex))
        container_env.append(client.V1EnvVar(
            name='AGENT_USERNAME', value=getattr(claw, 'owner_username', '') or '',
        ))
        container_env.append(client.V1EnvVar(
            name='AGENT_NAME', value=getattr(claw, 'name', '') or '',
        ))
        container_env.append(client.V1EnvVar(
            name='AGENT_ACCESS_TOKEN',
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name='claw-config',
                    key='connect-access-token',
                )
            ),
        ))
        container_env.append(client.V1EnvVar(name='SHIZUHA_CONNECT_AUTOREPLY', value='1'))
        container_env.append(client.V1EnvVar(
            name='SHIZUHA_CONNECT_REPLY_EMAIL',
            value=getattr(claw, 'owner_email', '') or '',
        ))
        # HIVE-1957: dial Connect's AgentChatConsumer WS via svc DNS (in-cluster)
        # instead of the public platform URL — the claw network policies exclude
        # the Tailscale edge (s1.tail.shizuha.com → 100.64.0.3). Same svc-direct
        # pattern as the fleet's SHIZUHA_CONNECT_WS_BASE (PLAT-1161).
        container_env.append(client.V1EnvVar(
            name='CONNECT_WS_URL',
            value=getattr(settings, 'HIVE_SCLI_CONNECT_WS_URL', ''),
        ))

    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(name=DEPLOYMENT_NAME, namespace=ns, labels=labels),
        spec=client.V1DeploymentSpec(
            replicas=1,
            # Charter D4 — never run two pods against the same WhatsApp session dir.
            strategy=client.V1DeploymentStrategy(type='Recreate'),
            selector=client.V1LabelSelector(match_labels={'app': APP_LABEL}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    # HIVE-127 §4: no kube-API path from a tenant claw pod. The claw
                    # never calls the Kubernetes API, so deny the projected SA token
                    # — defence-in-depth for the per-owner boundary (mirrors the
                    # fleet provisioner). Paired with the default-SA patch in
                    # _ensure_namespace for belt-and-braces.
                    automount_service_account_token=False,
                    node_selector={
                        'kubernetes.io/arch': 'arm64',
                        'storage.longhorn.io/ready': 'true',
                    },
                    # HIVE-74: ARM64 nodes carry a NoSchedule taint
                    # shizuha.com/inference-paused. Without this toleration CLAWS
                    # pods remain Pending indefinitely (sibling of HIVE-69 fleet fix).
                    tolerations=[
                        client.V1Toleration(
                            key='shizuha.com/inference-paused',
                            operator='Exists',
                            effect='NoSchedule',
                        ),
                    ],
                    # fsGroup=1000 (the openclaw image's `node` user) so a FRESH
                    # Longhorn PVC — root:root 755 at first mount — is writable by
                    # the non-root initContainer/claw (mirrors PLAT-146's fleet
                    # fsGroup fix). Without this, seeding openclaw.json only works
                    # on migrated PVCs whose data already carries node ownership.
                    security_context=client.V1PodSecurityContext(fs_group=1000),
                    init_containers=init_containers,
                    containers=[
                        client.V1Container(
                            name='claw',
                            image=claw_image,
                            image_pull_policy=image_pull_policy,
                            env=container_env,
                            volume_mounts=[
                                client.V1VolumeMount(name='claw-data', mount_path=data_mount_path),
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={'memory': tier.mem_request, 'cpu': tier.cpu_request},
                                limits={'memory': tier.mem_limit, 'cpu': tier.cpu_limit},
                            ),
                        ),
                    ],
                    volumes=[
                        client.V1Volume(
                            name='claw-data',
                            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                                claim_name='claw-data',
                            ),
                        ),
                    ],
                ),
            ),
        ),
    )
    try:
        apps_v1.create_namespaced_deployment(ns, deployment)
    except ApiException as e:
        if e.status == 409:
            apps_v1.replace_namespaced_deployment(DEPLOYMENT_NAME, ns, deployment)
        else:
            raise


def _create_claw_service(core_v1, ns: str, claw: Claw):
    http_port = int(getattr(settings, 'HIVE_CLAW_HTTP_PORT', 18789))
    service = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=DEPLOYMENT_NAME,
            namespace=ns,
            labels={'app.kubernetes.io/managed-by': 'shizuha-hive'},
        ),
        spec=client.V1ServiceSpec(
            selector={'app': APP_LABEL},
            ports=[
                client.V1ServicePort(name='http', port=http_port, target_port=http_port),
            ],
        ),
    )
    try:
        core_v1.create_namespaced_service(ns, service)
    except ApiException as e:
        if e.status == 409:
            # Reprovisioning can change the OpenClaw gateway port/image without
            # recreating the namespace. Keep the Service in lockstep with the
            # Deployment so existing claws do not keep routing to an old
            # targetPort after a failed/partial migration retry.
            # Read the live Service first so we preserve immutable / cluster
            # assigned fields (clusterIP, resourceVersion, etc.) while updating
            # only what we own.
            current = core_v1.read_namespaced_service(name=DEPLOYMENT_NAME, namespace=ns)
            current.spec.selector = {'app': APP_LABEL}
            current.spec.ports = [client.V1ServicePort(name='http', port=http_port, target_port=http_port)]
            current.metadata.labels = {'app.kubernetes.io/managed-by': 'shizuha-hive'}
            core_v1.replace_namespaced_service(DEPLOYMENT_NAME, ns, current)
        else:
            raise


def _log_event(claw: Claw, event_type: str, message: str):
    ClawEvent.objects.create(claw=claw, event_type=event_type, message=message)
    logger.info(f'[hive] claw {claw.id} (owner {claw.owner_id}): {event_type} — {message}')
