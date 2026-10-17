"""
Django settings for Shizuha Hive — Claw Controller.
"""
import json
import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes'}


# Never mint sessions or sign application state with a source-known default.
# An explicitly empty higher-priority key is a configuration error, not a
# request to silently select a different signer.
SECRET_KEY = os.environ.get('JWT_SECRET_KEY', os.environ.get('SECRET_KEY', ''))
if not SECRET_KEY.strip():
    raise ImproperlyConfigured('Configure JWT_SECRET_KEY or SECRET_KEY before starting the service')

DEBUG = _env_bool('DEBUG', True)

ALLOWED_HOSTS = ['*']

APPEND_SLASH = False

# Hive stores lifecycle clocks as naive UTC (see fleet/timeutils.py) and every
# consumer/reader assumes USE_TZ=False. Django 5.0 changed the global default
# to True, so pin the documented convention explicitly to keep the upgrade
# behavior-identical (PLAT-6904).
USE_TZ = False
TIME_ZONE = 'America/Chicago'

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'channels',
    'claws',
    'hive_platform',
    'fleet',
    'waitlist',
    'analytics',
    'credentials',
]

MIDDLEWARE = [
    # HIVE-576: reject ingress-borne calls to /v1/internal/ before anything else runs.
    'claws.middleware.InternalApiIngressGuardMiddleware',
    'django.middleware.security.SecurityMiddleware',
    # The high-frequency Agents grid uses a non-secret card projection.  Keep
    # compression isolated to that endpoint; full config/detail responses stay
    # uncompressed and out of the BREACH attack surface.
    'claws.middleware.AgentCardGZipMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'claws.middleware.ServiceAccessMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]

# TLS terminates at the trusted platform nginx proxy, which overwrites
# X-Forwarded-Proto before forwarding to Hive.  Django must see the original
# HTTPS scheme so CSRF origin checks compare https://<host> with https://<host>
# instead of rejecting every cookie-authenticated unsafe request as http/https
# cross-origin.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CSRF_COOKIE_SECURE = not DEBUG
# The public edge may preserve the browser origin while routing the backend
# through Hive's internal ingress host.  Keep that topology explicit instead
# of weakening CSRF globally: only Shizuha's HTTPS origin and subdomains are
# trusted, with an environment override for a future domain cutover.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        'CSRF_TRUSTED_ORIGINS',
        'https://shizuha.com,https://*.shizuha.com',
    ).split(',')
    if origin.strip()
]

ROOT_URLCONF = 'hive_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'hive_project.wsgi.application'
ASGI_APPLICATION = 'hive_project.asgi.application'

# HIVE-235: Redis channel layer for daemon-link WS routing (stickiness via shared cursor).
# DB 5 is reserved for Hive; DB 2 = Pulse, DB 4 = Mail.
CHANNEL_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/5")
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            # channels_redis uses a 5s blocking pop internally. On redis-py 5,
            # letting the socket read timeout line up with that interval can
            # raise redis.exceptions.TimeoutError during normal idle waits,
            # which tears down long-lived daemon-link WebSockets. Keep the
            # socket timeout comfortably above the blocking-pop timeout.
            "hosts": [{
                "address": CHANNEL_REDIS_URL,
                "socket_timeout": float(os.environ.get("CHANNEL_REDIS_SOCKET_TIMEOUT", "20")),
                "socket_connect_timeout": float(os.environ.get("CHANNEL_REDIS_CONNECT_TIMEOUT", "5")),
                "health_check_interval": int(os.environ.get("CHANNEL_REDIS_HEALTH_CHECK_INTERVAL", "30")),
            }],
        },
    },
}


def _derive_cache_redis_url():
    """PLAT-1063: cache backend URL. Prefer CACHE_REDIS_URL; else reuse the Hive
    Redis host but on DB 6 (DB 5 = Hive channel layer) so cache keys never
    collide with channel-layer keys."""
    explicit = os.environ.get("CACHE_REDIS_URL")
    if explicit:
        return explicit
    base = os.environ.get("REDIS_URL", "redis://localhost:6379/5")
    host, _, tail = base.rpartition("/")
    # tail is the db number (e.g. "5"); swap to 6 only when it's a plain int db.
    if host and tail.isdigit():
        return f"{host}/6"
    return base  # unusual URL shape -> leave as-is rather than corrupt it


# PLAT-1063: Django cache for the /v1/fleet/agents bounded-TTL read model. The
# cache is an optimization only — fleet.agent_index_cache guards every op, so an
# unreachable Redis degrades to the live-aggregate path instead of erroring.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": _derive_cache_redis_url(),
        "KEY_PREFIX": "hive",
        "TIMEOUT": 30,
        # HIVE-619: cache Redis is an optimization, never a request-liveness
        # dependency.  Keep both the TCP connect and command read comfortably
        # below rt-fleet's 5s caller deadline (and Daphne's 10s disconnect
        # cleanup) so a blackholed Redis socket cannot strand a sync worker.
        "OPTIONS": {
            "socket_connect_timeout": float(os.environ.get("CACHE_REDIS_CONNECT_TIMEOUT", "0.25")),
            "socket_timeout": float(os.environ.get("CACHE_REDIS_READ_TIMEOUT", "0.25")),
            "retry_on_timeout": False,
        },
    },
}

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'shizuha_hive'),
        'USER': os.environ.get('DB_USER', 'postgres'),
        'PASSWORD': os.environ.get('DB_PASSWORD', ''),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '5432'),
    }
}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'claws.authentication.FederatedJWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
    # HIVE-68 §Security: per-owner rate-limit for the credential-minting
    # channel-token endpoint (applied via ChannelTokenRateThrottle on the view).
    # A channel open is interactive, not bursty — 6/min/owner caps repeat-mint
    # abuse (start_claw scale thrash + jti-cache growth) without hurting UX.
    # HIVE-127 §SRE-hardening: guard provision + start control-plane actions to
    # avoid bursty destructive operations and protect shared k8s API capacity.
    'DEFAULT_THROTTLE_RATES': {
        'channel-token': '6/min',
        'claw-provision': '2/min',
        'claw-start': '3/min',
        # HIVE-100/HIVE-212: public waitlist beacons + JSON signup adapter.
        # Kept low enough to blunt spam while allowing normal form retries.
        'waitlist': '20/min',
        # HIVE-399 §6 DoS/abuse: cap authenticated analytics ingest per user+org
        # so a looping tenant client cannot flood its own analytics partition.
        'analytics-ingest': '120/min',
        # HIVE-394 §6 DoS/abuse: cap per-user feedback ingest so a looping client
        # can't flood feedback_event or storm the downstream clustering job.
        'discovery-feedback-ingest': '30/min',
    },
}

# HIVE-72: Platform JWT verification key — must match the signing key used by
# Shizuha ID. In production, set SHIZUHA_JWT_SIGNING_KEY to the same value as
# shizuha-id's JWT_SECRET_KEY / SECRET_KEY. Defaults to HIVE's own SECRET_KEY
# so dev/CI works without extra config; production MUST override this.
SHIZUHA_JWT_SIGNING_KEY = os.environ.get('SHIZUHA_JWT_SIGNING_KEY', SECRET_KEY)
# RS256 tokens (agent/OAuth-style Shizuha ID tokens) are verified against ID's
# JWKS. Defaults to the platform URL used elsewhere by Hive; override for local
# dev or split-network deployments.
_DEFAULT_SHIZUHA_PLATFORM_URL = os.environ.get(
    'SHIZUHA_PLATFORM_URL',
    'http://192.168.0.123',
).rstrip('/')
SHIZUHA_JWKS_URL = os.environ.get(
    'SHIZUHA_JWKS_URL',
    f'{_DEFAULT_SHIZUHA_PLATFORM_URL}/shizuha-id/.well-known/jwks.json',
)

SIMPLE_JWT = {
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SHIZUHA_JWT_SIGNING_KEY,
    'VERIFYING_KEY': SHIZUHA_JWT_SIGNING_KEY,
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'TOKEN_TYPE_CLAIM': 'token_type',
}

STATIC_URL = '/hive/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/hive/login'

# ── Hive-specific settings ──

# HIVE-291: public Free Claw waitlist launch gate. Default fail-closed until
# DevOps explicitly enables it after Security/QA sign-off.
HIVE_FREE_CLAW_WAITLIST_ENABLED = _env_bool('HIVE_FREE_CLAW_WAITLIST_ENABLED', False)

# Platform URL (where Shizuha ID, Connect, etc. live)
# Platform URL — use LAN IP for k8s pods (can't resolve Tailscale hostnames)
SHIZUHA_PLATFORM_URL = os.environ.get('SHIZUHA_PLATFORM_URL', 'http://192.168.0.123')
# Internal Connect base used for posting Hive-agent replies back through the
# platform route. The reply code appends /connect/api/..., so default to the
# platform/nginx base (not the raw shizuha-connect service, whose path is /api).
SHIZUHA_CONNECT_INTERNAL_URL = os.environ.get('SHIZUHA_CONNECT_INTERNAL_URL', SHIZUHA_PLATFORM_URL)
# Public browser/WebView URL returned to mobile/web clients. Keep separate from
# SHIZUHA_PLATFORM_URL because Hive pods may need the internal cluster URL for
# service calls, while Android WebView must launch over public HTTPS.
HIVE_PUBLIC_PLATFORM_URL = os.environ.get('HIVE_PUBLIC_PLATFORM_URL', 'https://shizuha.com')

# Shizuha runtime Docker image (legacy; retained as fallback for HIVE_CLAW_IMAGE)
SHIZUHA_RUNTIME_IMAGE = os.environ.get('SHIZUHA_RUNTIME_IMAGE', 'shizuha-runtime:latest')

# Container registry for runtime/claw images. Used to registry-qualify the
# default claw image ref below so an IfNotPresent pull resolves against the
# in-cluster registry instead of docker.io (PLAT-64 durable rt-* pull path).
RUNTIME_REGISTRY = os.environ.get('RUNTIME_REGISTRY', 'gx10-1:30500')

# Near-native OpenClaw container image for claw workloads (Charter D1 / HIVE-9).
# HIVE OpenClaw image promotion history:
# - HIVE-35 (2026-06-10): 2026.6.5-arm64, required because
#   clawhub:@openclaw/whatsapp enforces plugin API >=2026.6.5.
# - 2026-07-02: promoted to 2026.6.11-multiarch after the control UI
#   self-update path correctly skipped inside Kubernetes with
#   managed-service-handoff-unavailable. Hive-managed claws update via image
#   rollout, not in-place npm mutation.
#   tag:    gx10-1:30500/openclaw:2026.6.11-multiarch
#   digest: sha256:b55a1a003f002ed273c7977abf5d30c7ca21008a11357ad065ac08a0eb927cf2
# Explicit HIVE_CLAW_IMAGE overrides remain verbatim for rollback.
HIVE_CLAW_IMAGE = os.environ.get(
    'HIVE_CLAW_IMAGE',
    os.environ.get('SHIZUHA_RUNTIME_IMAGE') or f'{RUNTIME_REGISTRY}/openclaw@sha256:b55a1a003f002ed273c7977abf5d30c7ca21008a11357ad065ac08a0eb927cf2',
)
# Runtime-backed personal assistants. Hermes is schema/API-visible, but must
# never silently run OpenClaw. A dedicated Hermes image is used only when
# explicitly configured; OpenClaw compatibility mode requires an explicit flag
# so API/UI surfaces can label it honestly.
HIVE_SCLI_IMAGE = os.environ.get('HIVE_SCLI_IMAGE', 'localhost:30500/shizuha-runtime:latest')
HIVE_HERMES_IMAGE = os.environ.get('HIVE_HERMES_IMAGE', '')
HIVE_HERMES_OPENCLAW_COMPAT = _env_bool('HIVE_HERMES_OPENCLAW_COMPAT', False)
HIVE_CLAW_IMAGE_PULL_POLICY = os.environ.get('HIVE_CLAW_IMAGE_PULL_POLICY', 'IfNotPresent')

# OpenClaw's gateway image stores OAuth, WhatsApp/Baileys session state, and
# workspace under /home/node/.openclaw and serves the LAN gateway on 18789.
HIVE_CLAW_DATA_MOUNT_PATH = os.environ.get('HIVE_CLAW_DATA_MOUNT_PATH', '/home/node/.openclaw')
HIVE_CLAW_HTTP_PORT = int(os.environ.get('HIVE_CLAW_HTTP_PORT', '18789'))

# HIVE-1957: in-cluster Connect AgentChatConsumer WS URL for SCLI claw pods.
# The SCLI runtime's ConnectClient dials this directly (svc DNS) instead of the
# public platform URL (s1.tail.shizuha.com → Tailscale 100.64.0.3, which claw
# network policies exclude). Mirrors the fleet's SHIZUHA_CONNECT_WS_BASE
# (PLAT-1161) — svc-direct removes the edge 502/ECONNREFUSED failure mode.
HIVE_SCLI_CONNECT_WS_URL = os.environ.get(
    'HIVE_SCLI_CONNECT_WS_URL',
    'ws://shizuha-connect.shizuha.svc.cluster.local:8019/connect/ws/connect/agent/',
)

# k8s namespace prefix for claws: namespace = claw-<owner_id>-<claw_id> (Charter D2).
CLAW_NAMESPACE_PREFIX = os.environ.get('CLAW_NAMESPACE_PREFIX', 'claw-')

# Default storage class for runtime/fleet PVCs. PLAT-497 makes the s1
# cutover turnkey while Longhorn is ARM-node-only, so the durable interim
# default is local-path unless an environment override pins a different class.
RUNTIME_STORAGE_CLASS = os.environ.get('RUNTIME_STORAGE_CLASS', 'local-path')

# Customer OpenClaw/Hermes data must not inherit the fleet/runtime local-path
# fallback: user-facing sessions need HA storage so a cordoned/rebooted worker
# cannot make dashboards unavailable (PLAT-1229).
HIVE_CLAW_STORAGE_CLASS = os.environ.get('HIVE_CLAW_STORAGE_CLASS', 'longhorn-3x')

# Default PVC size for user data
RUNTIME_PVC_SIZE = os.environ.get('RUNTIME_PVC_SIZE', '10Gi')

try:
    FLEET_TOLERATIONS = json.loads(os.environ.get('FLEET_TOLERATIONS_JSON', '')) or []
except json.JSONDecodeError:
    FLEET_TOLERATIONS = []

# HIVE-3 §5.1 tier source. In production, the `hive-tiers` ConfigMap is mounted
# as HIVE_TIERS_JSON; the default keeps dev/CI and the UI shell usable.
DEFAULT_HIVE_TIERS = [
    {
        'id': 'small', 'name': 'Small', 'cpu': '500m', 'memory': '1Gi', 'pvc': '5Gi',
        'llm_quota': {'cluster_local_tok_per_day': 100000, 'remote_tok_per_day': 5000},
        'price': 100,
    },
    {
        'id': 'med', 'name': 'Medium', 'cpu': '1000m', 'memory': '2Gi', 'pvc': '20Gi',
        'llm_quota': {'cluster_local_tok_per_day': 500000, 'remote_tok_per_day': 50000},
        'price': 400,
    },
]
try:
    HIVE_TIERS = json.loads(os.environ.get('HIVE_TIERS_JSON', '')) or DEFAULT_HIVE_TIERS
except json.JSONDecodeError:
    HIVE_TIERS = DEFAULT_HIVE_TIERS

# Sysbox runtime class name (for Docker-in-Docker support)
# Set to empty string to disable (sysbox not installed yet)
RUNTIME_CLASS_NAME = os.environ.get('RUNTIME_CLASS_NAME', '') or None

# ── Fleet (k3s-native agent runtime) — HLD PLAT-147 v0.2 / EPIC PLAT-146 ──
# Shared namespace for ordinary fleet agents (Q1). Isolated agents (DevOps
# ichi/ni) get their own ns `fleet-<username>` (Q1b) — resolved per-agent.
SHIZUHA_FLEET_NAMESPACE = os.environ.get('SHIZUHA_FLEET_NAMESPACE', 'shizuha-fleet')
# CI bootstraps the explicit native generation-27 authority before activating
# these consumers. Once enabled, missing authority is an error, never fallback.
HIVE_NATIVE_RUNTIME_RELEASE_ENABLED = os.environ.get('HIVE_NATIVE_RUNTIME_RELEASE_ENABLED', 'false').lower() == 'true'
HIVE_NATIVE_RUNTIME_RELEASE_NAMESPACE = os.environ.get('HIVE_NATIVE_RUNTIME_RELEASE_NAMESPACE', 'shizuha-hive')

# Multi-arch shizuha-agent-runtime image (PLAT-150/PLAT-497), pinned per
# PLAT-50 tag discipline. IMPORTANT: fleet pods schedule ACROSS the gx10 nodes
# and the s1 amd64 pool, so the ref MUST be a manifest-list TAG, not an
# arch-specific digest. It also MUST use each node's trusted loopback registry
# `localhost:30500` (NodePort to the in-cluster registry) — NOT `gx10-1:30500`,
# which only resolves on gx10-1 and fails to pull on every other node (verified
# PLAT-150). It is the same registry/backing store the s1 `gx10-1:30500` push
# went to.
FLEET_AGENT_IMAGE = os.environ.get(
    'FLEET_AGENT_IMAGE',
    'localhost:30500/shizuha-agent-runtime:src-20260618-mcp-authfix',
)
FLEET_AGENT_MODEL = os.environ.get('FLEET_AGENT_MODEL', 'GLM-5.1')
FLEET_COORDINATOR_MODEL_TOKEN_URL = os.environ.get('FLEET_COORDINATOR_MODEL_TOKEN_URL', '')
FLEET_COORDINATOR_TOKEN_SECRET_NAME = os.environ.get('FLEET_COORDINATOR_TOKEN_SECRET_NAME', '')
FLEET_COORDINATOR_TOKEN_SECRET_KEY = os.environ.get('FLEET_COORDINATOR_TOKEN_SECRET_KEY', 'token')
# Native ID client_credentials; broker renews short-lived tokens in memory.
# Presence selects OAuth exclusively; a failed grant never uses the old JWT.
FLEET_COORDINATOR_OAUTH_SECRET_NAME = os.environ.get('FLEET_COORDINATOR_OAUTH_SECRET_NAME', '')
RUNTIME_FLEET_API_URL = os.environ.get(
    'RUNTIME_FLEET_API_URL',
    'http://shizuha-runtime-fleet.rt-fleet.svc.cluster.local:8016',
)
RUNTIME_FLEET_API_TIMEOUT = float(os.environ.get('RUNTIME_FLEET_API_TIMEOUT', '3'))
# HIVE-171: shared secret for rt-fleet lifecycle control (enable/disable/restart);
# must match the proxy's FLEET_CONTROL_SECRET (same k8s Secret, env-from-secret).
RUNTIME_FLEET_CONTROL_SECRET = os.environ.get('RUNTIME_FLEET_CONTROL_SECRET', '') or os.environ.get('FLEET_CONTROL_SECRET', '')
# ADR-PLAT-002 / PLAT-149 broker sidecar = mcp-auth-proxy (pinned). Same
# loopback-registry rule (fleet pods pull via each node's localhost:30500;
# s1 needs the amd64 manifest).
FLEET_BROKER_IMAGE = os.environ.get(
    'FLEET_BROKER_IMAGE',
    'localhost:30500/mcp-auth-proxy:src-3a0a1b6a159c',
)

# Per-agent private browser-worker image (PLAT-536 / BRW-12). Must be a
# fully-qualified internal-registry reference: the unqualified
# 'shizuha-browser:latest' fallback in provisioner.py resolves to
# docker.io/library/shizuha-browser:latest (not found) and the worker pod
# lands in ErrImagePull, taking the agent's browser MCP down with it. The
# shared shizuha-browser Deployment uses this same pinned tag.
FLEET_BROWSER_IMAGE = os.environ.get(
    'FLEET_BROWSER_IMAGE',
    'localhost:30500/shizuha-browser:s260606x-b9669f4',
)

# Named resource profiles → requests/limits (HLD §3, capacity R2). Override via
# FLEET_RESOURCE_PROFILES_JSON without a code change.
# PLAT-6397: heavy must be 8Gi, aligned with the provisioner's
# DEFAULT_RESOURCE_PROFILES['heavy'] and the daemon's gateway spec — the old
# 4Gi here made the provisioner's runtime floor raise heavy gateway agents to
# only 6Gi, which OOM-killed agent-nagi (300K-token sessions spike to 4-6GB).
DEFAULT_FLEET_RESOURCE_PROFILES = {
    'small': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '1', 'memory': '1Gi'}},
    'standard': {'requests': {'cpu': '250m', 'memory': '512Mi'}, 'limits': {'cpu': '2', 'memory': '2Gi'}},
    'heavy': {'requests': {'cpu': '500m', 'memory': '1Gi'}, 'limits': {'cpu': '4', 'memory': '8Gi'}},
}
try:
    FLEET_RESOURCE_PROFILES = json.loads(
        os.environ.get('FLEET_RESOURCE_PROFILES_JSON', '')
    ) or DEFAULT_FLEET_RESOURCE_PROFILES
except json.JSONDecodeError:
    FLEET_RESOURCE_PROFILES = DEFAULT_FLEET_RESOURCE_PROFILES

# Shizuha Inference — Cortex endpoint for agent LLM calls
# No /v1 suffix — the shizuha vLLM provider appends it
VLLM_BASE_URL = os.environ.get('VLLM_BASE_URL', 'http://cortex.shizuha-cortex.svc.cluster.local:8040')

# HIVE-76: Cortex root used by the user-self API-key proxy (claws/cortex_keys.py).
# Defaults to the inference URL above; override only if the key API splits off.
CORTEX_BASE_URL = os.environ.get('CORTEX_BASE_URL', VLLM_BASE_URL)
CORTEX_PUBLIC_BASE_URL = os.environ.get('CORTEX_PUBLIC_BASE_URL', f'{CORTEX_BASE_URL.rstrip("/")}/v1')
CORTEX_API_KEY = os.environ.get('CORTEX_API_KEY', '')
HIVE_CORTEX_DEFAULT_MODEL = os.environ.get('HIVE_CORTEX_DEFAULT_MODEL', 'DeepSeek-V4-Flash')

# Agent→model exclusivity leasing (fleet/model_lease.py). Seeds the pool on a cold
# cache; the live pool is edited through the `fleet:model-lease-pool` cache key so
# membership can change without a redeploy. Empty model = leasing off, and every
# code path short-circuits, so unset is a hard no-op rather than a silent policy.
HIVE_MODEL_LEASE_MODEL = os.environ.get('HIVE_MODEL_LEASE_MODEL', '')
HIVE_MODEL_LEASE_MEMBERS = [
    m.strip() for m in os.environ.get('HIVE_MODEL_LEASE_MEMBERS', '').split(',') if m.strip()
]
# Hive-governed Cortex listings. Vision-Metal / Qwen3.8-Q2 / DeepSeek-V4-Flash
# are retired serving ids (remap_retired_fleet_model); leftover KV homes on
# those ids are drained by lease reconcile, not by overlaying a live pool.
# Agents on an omitted *live* model get lease_wake_available=no_lease_pool.
HIVE_CORTEX_LEASED_MODELS = os.environ.get(
    'HIVE_CORTEX_LEASED_MODELS',
    'GLM-5.3-Flash',
)
# Must authenticate to Cortex as fleet-service or staff to reach the admin lease
# API. CORTEX_API_KEY is often a resold_customer key (see fleet/views.py) which
# would 403, so this is a separate knob rather than a reuse.
HIVE_CORTEX_LEASE_API_KEY = os.environ.get('HIVE_CORTEX_LEASE_API_KEY', '')

# Scoped credential for agent account provisioning on Shizuha ID.
# Empty means automatic platform-identity provisioning fails closed.
HIVE_ID_FLEET_PROVISIONER_TOKEN = os.environ.get('HIVE_ID_FLEET_PROVISIONER_TOKEN', '')

# Admin API used by legacy staff/account/profile controls. Team roster
# membership CRUD is intentionally handled through Pulse TeamRoster below.
HIVE_ADMIN_API_URL = os.environ.get(
    'HIVE_ADMIN_API_URL',
    'http://shizuha-admin.shizuha.svc.cluster.local:8003/api',
).rstrip('/')

# Pulse API used for canonical team routing/display roster membership CRUD.
HIVE_PULSE_API_URL = os.environ.get(
    'HIVE_PULSE_API_URL',
    'http://shizuha-pulse.shizuha.svc.cluster.local:8002/api',
).rstrip('/')

# ID client_credentials for lifecycle roster/workload reads. This service
# identity has no staff privileges and is renewed inside its short access TTL.
HIVE_PULSE_OAUTH_CLIENT_ID = os.environ.get('HIVE_PULSE_OAUTH_CLIENT_ID', '')
HIVE_PULSE_OAUTH_CLIENT_SECRET = os.environ.get('HIVE_PULSE_OAUTH_CLIENT_SECRET', '')
HIVE_PULSE_OAUTH_TOKEN_URL = os.environ.get('HIVE_PULSE_OAUTH_TOKEN_URL', '')

# PLAT-397: create a Pulse onboarding task when a new fleet agent is provisioned.
# Set HIVE_PULSE_ONBOARDING_ENABLED=false to disable (e.g. in test environments).
HIVE_PULSE_ONBOARDING_ENABLED = os.environ.get('HIVE_PULSE_ONBOARDING_ENABLED', 'true').lower() not in ('false', '0', 'no')

# HIVE-277 P4: bounded recovery auto-apply remains default-off and is gated
# by a per-action allowlist. Empty allowlist means observe/dry-run only.
HIVE_RECOVERY_AUTO_APPLY_ENABLED = _env_bool('HIVE_RECOVERY_AUTO_APPLY_ENABLED', False)
# Per-action allowlist for HIVE-277 P4 auto-apply. Wildcards are not
# supported; name each action explicitly.
HIVE_RECOVERY_AUTO_APPLY_ACTIONS = tuple(
    item.strip() for item in os.environ.get('HIVE_RECOVERY_AUTO_APPLY_ACTIONS', '').split(',')
    if item.strip()
)
HIVE_RECOVERY_AUTO_APPLY_DENY_ACTIONS = tuple(
    item.strip() for item in os.environ.get('HIVE_RECOVERY_AUTO_APPLY_DENY_ACTIONS', '').split(',')
    if item.strip()
)

# HIVE-277 §10 v16 consumer-first rollout. Dark support may be deployed before
# any daemon is allowed to emit/execute v2. Enforcement stays fail-closed and
# default-off until the PLAT-4163 migration plus accepted canary gates complete.
HIVE_RUNTIME_PROJECTION_V2_DARK = _env_bool('HIVE_RUNTIME_PROJECTION_V2_DARK', True)
HIVE_RUNTIME_PROJECTION_V2_ENFORCE = _env_bool('HIVE_RUNTIME_PROJECTION_V2_ENFORCE', False)

# Shared bearer for in-cluster OpenClaw -> hive-api internal handshakes.
# Must be set in k8s via Secret; empty means internal endpoints fail closed.
HIVE_INTERNAL_AUTH_TOKEN = os.environ.get('HIVE_INTERNAL_AUTH_TOKEN', '')

# HIVE-1153: dedicated asymmetric signer for HiveCapabilityEvidenceV2.  The
# PKCS#8 private key is mounted only into Hive; Pulse receives the public JWK
# and current producer tuple through its own declarative deployment config.
HIVE_CAPABILITY_EVIDENCE_PRIVATE_KEY_PEM = os.environ.get(
    'HIVE_CAPABILITY_EVIDENCE_PRIVATE_KEY_PEM', '',
)
HIVE_CAPABILITY_EVIDENCE_KID = os.environ.get('HIVE_CAPABILITY_EVIDENCE_KID', '')
# Dedicated Pulse -> Hive service credential. Keep this separate from the
# broader OpenClaw internal token so evidence minting has its own blast radius.
HIVE_CAPABILITY_EVIDENCE_AUTH_TOKEN = os.environ.get(
    'HIVE_CAPABILITY_EVIDENCE_AUTH_TOKEN', '',
)
HIVE_CAPABILITY_EVIDENCE_SERVICE_ACCOUNT = os.environ.get(
    'HIVE_CAPABILITY_EVIDENCE_SERVICE_ACCOUNT', 'hive-api',
)
HIVE_CAPABILITY_EVIDENCE_ROSTER_MAX_AGE_SECONDS = int(
    os.environ.get('HIVE_CAPABILITY_EVIDENCE_ROSTER_MAX_AGE_SECONDS', '90')
)

# PLAT-4482 neutral Metering Core. During the Hive bootstrap the dedicated
# service token may intentionally fall back to HIVE_INTERNAL_AUTH_TOKEN; an empty
# value still fails closed. Producers can override the endpoint when Metering is
# extracted from Hive without changing the CloudEvents/outbox adapter.
METERING_CORE_SERVICE_TOKEN = os.environ.get('METERING_CORE_SERVICE_TOKEN', '')
METERING_CORE_EVENTS_URL = os.environ.get('METERING_CORE_EVENTS_URL', '')

# HIVE-620/624: token for admin's internal-service endpoints, used to add a
# user's PERSONAL AGENT to the owner's personal org so owner<->own-agent DMs pass
# the shared-org gate (no tenancy exemption). Sent as X-Internal-Service-Token
# with X-Internal-Service: shizuha-hive. Empty → the membership-wiring step is
# skipped and logged (the agent still provisions; DM connectivity self-heals once
# the token is configured). Fast personal-assistant model, overridable.
ADMIN_INTERNAL_SERVICE_TOKEN = os.environ.get('ADMIN_INTERNAL_SERVICE_TOKEN', '')
# Dedicated Hive-only key for Admin's signed membership-mutation contract.
# Read compatibility credentials are not authority to create organization edges.
HIVE_SERVICE_SIGNING_KEY = os.environ.get('HIVE_SERVICE_SIGNING_KEY', '')
# Default personal talk seat (HIVE-2131): Grok Voice so Home Live is native
# speech-to-speech. Seats stay hibernated until first Connect DM / connect-wake;
# Voice can talk via Cortex realtime while the pod is still parked.
PERSONAL_AGENT_MODEL = os.environ.get(
    'PERSONAL_AGENT_MODEL', 'cortex/grok-voice-think-fast-2.0')

# HIVE-123 Token Coordinator envelope-encryption KEK. This must be injected
# from a k8s Secret (or the PLAT-555 service-account credential path); empty
# means token encryption/decryption fails closed rather than using a committed
# fallback key. Tests must set an explicit override.
HIVE_TOKEN_COORDINATOR_KEK = os.environ.get('HIVE_TOKEN_COORDINATOR_KEK', '')
HIVE_TOKEN_COORDINATOR_KEK_ID = os.environ.get('HIVE_TOKEN_COORDINATOR_KEK_ID', 'k8s-secret-v1')

# Personal-ownership model (operator 2026-07-11): credentials belong to the USER
# who added them, not the org. Fleet-internal (org-scope) broker requests draw
# from this platform owner's personal pool FIRST, then the shared org pool.
# Default '3' = the platform operator's shizuha-id user id.
HIVE_FLEET_OWNER_USER_ID = os.environ.get('HIVE_FLEET_OWNER_USER_ID', '3')

# Browser-facing OpenClaw deep-link template. Deployments should point this at
# the external OpenClaw ingress; the signed dashboard token is appended as a
# query parameter by Hive when owners hit /hive/assistants/<id>/dash.
HIVE_OPENCLAW_DASHBOARD_URL_TEMPLATE = os.environ.get(
    'HIVE_OPENCLAW_DASHBOARD_URL_TEMPLATE',
    '/hive/openclaw/{owner_id}/{claw_id}/',
)
HIVE_DASHBOARD_LINK_SIGNING_KEY = os.environ.get(
    'HIVE_DASHBOARD_LINK_SIGNING_KEY',
    SECRET_KEY,
)
HIVE_BROWSER_JWT_COOKIE = os.environ.get('HIVE_BROWSER_JWT_COOKIE', 'shizuha-access-token')

# HIVE-201 E1: Ori's linked WhatsApp number, in international digits-only format.
# When set, the dashboard wa.me deep-link targets it directly so the owner's
# LINK-<code> message lands on Ori's authenticated whatsmeow session. Empty =>
# the deep-link omits the recipient and the user picks the chat.
HIVE_WA_LINK_NUMBER = os.environ.get('HIVE_WA_LINK_NUMBER', '')

# HIVE-347: live Books draft-invoice ingestion endpoint. HIVE_BOOKS_API_URL is
# the Books origin (for example https://books.shizuha.com); the path is relative
# to that origin and carries Hive's idempotent tenant+period invoice contract.
HIVE_BOOKS_API_URL = os.environ.get('HIVE_BOOKS_API_URL', '')
HIVE_BOOKS_DRAFT_INVOICE_PATH = os.environ.get(
    'HIVE_BOOKS_DRAFT_INVOICE_PATH',
    '/shizuha-books/api/hive/invoices/drafts/',
)
HIVE_BOOKS_SERVICE_TOKEN = os.environ.get('HIVE_BOOKS_SERVICE_TOKEN', '')
HIVE_BILLING_DEFAULT_SAC_CODE = os.environ.get('HIVE_BILLING_DEFAULT_SAC_CODE', '998313')
HIVE_BILLING_SELLER_GST_STATE_CODE = os.environ.get('HIVE_BILLING_SELLER_GST_STATE_CODE', '29')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
}

# HIVE-4 §9.1 step-up verifier backend: 'stub' for dev/CI, 'shizuha-id' for prod.
HIVE_STEPUP_BACKEND = os.environ.get('HIVE_STEPUP_BACKEND') or ('stub' if DEBUG else 'shizuha-id')
SHIZUHA_ID_INTERNAL_URL = os.environ.get(
    'SHIZUHA_ID_INTERNAL_URL',
    'http://shizuha-id.shizuha.svc.cluster.local:8001',
)
SHIZUHA_ID_STEPUP_VERIFY_URL = os.environ.get('SHIZUHA_ID_STEPUP_VERIFY_URL', '')
SHIZUHA_ID_STEPUP_VERIFY_TIMEOUT_SECONDS = float(
    os.environ.get('SHIZUHA_ID_STEPUP_VERIFY_TIMEOUT_SECONDS', '5')
)

# HIVE-613: HMAC shared secret for Connect→Hive delivery authentication.
# Both services must share the same secret. If unset, HMAC verification fails
# closed (no unauthenticated requests are accepted).
HIVE_CONNECT_HMAC_SECRET = os.environ.get('HIVE_CONNECT_HMAC_SECRET', '')

# HIVE-609 temporary containment while PLAT-4159 lands the owner/epoch/resource-
# fenced authority reducer. Remove this flag when that projection ships.
HIVE609_TEMP_FRESHNESS_PRECEDENCE = os.getenv(
    'HIVE609_TEMP_FRESHNESS_PRECEDENCE', 'false'
).lower() in ('1', 'true', 'yes')
