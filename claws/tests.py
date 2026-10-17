"""Tests for claw provisioning.

PLAT-64: claw pods must pull the OpenClaw image from the in-cluster registry
rather than relying on a node-local ``ctr import``. HIVE-10 landed the
``IfNotPresent`` pull policy on the Claw deployment, but left the default image
ref bare (``shizuha-openclaw:latest``) — under ``IfNotPresent`` that resolves
against docker.io on a fresh node and fails. These tests pin the two invariants
that make a claw pod schedulable on any node without a manual import:

  1. the claw container image default is registry-qualified, and
  2. the pull policy is ``IfNotPresent`` (not ``Never``).

The provisioner builds a ``V1Deployment`` and hands it to the (mocked) k8s
``AppsV1Api``; we inspect that object — no cluster or DB is touched.
"""
import uuid
from io import StringIO
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kubernetes.client.rest import ApiException
from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from claws import provisioner
from claws import views
from claws.authentication import FederatedJWTAuthentication
from claws.models import Claw


class FederatedJWTAuthenticationTests(SimpleTestCase):
    def test_accepts_rs256_shizuha_id_token_via_jwks(self):
        from rest_framework.test import APIRequestFactory

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        payload = {
            'token_type': 'access',
            'user_id': 321,
            'email': 'agent@example.com',
            'username': 'agent-one',
            'is_staff': True,
            'is_superuser': False,
            'organization_memberships': {'shizuha': {'role': 'engineer'}},
            'enabled_services': ['hive'],
            'exp': datetime.now(tz=timezone.utc) + timedelta(minutes=5),
        }
        token = jwt.encode(payload, private_pem, algorithm='RS256', headers={'kid': 'test-key'})

        class _FakeSigningKey:
            key = public_key

        class _FakeJWKClient:
            urls = []

            def __init__(self, url):
                self.urls.append(url)

            def get_signing_key_from_jwt(self, raw_token):
                self.raw_token = raw_token
                return _FakeSigningKey()

        request = APIRequestFactory().get('/', HTTP_AUTHORIZATION=f'Bearer {token}')
        with override_settings(SHIZUHA_JWKS_URL='http://id.example/.well-known/jwks.json'):
            with patch('claws.authentication.jwt.PyJWKClient', _FakeJWKClient):
                user, validated = FederatedJWTAuthentication().authenticate(request)

        self.assertEqual(_FakeJWKClient.urls, ['http://id.example/.well-known/jwks.json'])
        self.assertEqual(user.id, 321)
        self.assertEqual(user.email, 'agent@example.com')
        self.assertEqual(user.username, 'agent-one')
        self.assertTrue(user.is_staff)
        self.assertEqual(validated['enabled_services'], ['hive'])
        self.assertEqual(request._hive_bearer_token, token)

    def test_hs256_simplejwt_path_does_not_fetch_jwks(self):
        from rest_framework.test import APIRequestFactory
        from rest_framework_simplejwt.tokens import AccessToken

        token = AccessToken()
        token['user_id'] = 42
        token['email'] = 'human@example.com'
        token['username'] = 'human'
        raw = str(token)
        request = APIRequestFactory().get('/', HTTP_AUTHORIZATION=f'Bearer {raw}')
        with patch('claws.authentication.jwt.PyJWKClient') as jwks_client:
            user, validated = FederatedJWTAuthentication().authenticate(request)

        jwks_client.assert_not_called()
        self.assertEqual(user.id, 42)
        self.assertEqual(validated['username'], 'human')
        self.assertEqual(request._hive_bearer_token, raw)

    def test_numeric_string_user_id_is_normalised_to_int(self):
        from rest_framework.test import APIRequestFactory
        from rest_framework_simplejwt.tokens import AccessToken

        token = AccessToken()
        token['user_id'] = '100'
        token['email'] = 'owner@example.com'
        token['username'] = 'owner'
        raw = str(token)
        request = APIRequestFactory().get('/', HTTP_AUTHORIZATION=f'Bearer {raw}')

        user, _ = FederatedJWTAuthentication().authenticate(request)

        self.assertEqual(user.id, 100)
        self.assertIsInstance(user.id, int)


def _fake_tier():
    return SimpleNamespace(
        cpu_request="250m", cpu_limit="2",
        mem_request="512Mi", mem_limit="2Gi",
        pvc_size="10Gi",
        llm_default_model="",
    )


def _build_deployment(agent_type="claw", model="", cortex_base="", cortex_key="", cortex_model=""):
    apps_v1 = MagicMock()
    claw = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=123,
        owner_username="owner",
        owner_email="owner@example.com",
        name="My Assistant",
        platform_url="http://platform",
        agent_type=agent_type,
        model=model,
    )
    provisioner._create_claw_deployment(
        apps_v1, "claw-123-abc", claw, _fake_tier(),
        cortex_base=cortex_base, cortex_key=cortex_key, cortex_model=cortex_model,
    )
    apps_v1.create_namespaced_deployment.assert_called_once()
    # _create_claw_deployment calls create_namespaced_deployment(ns, deployment)
    _, deployment = apps_v1.create_namespaced_deployment.call_args.args
    return deployment



def _build_service():
    core_v1 = MagicMock()
    claw = SimpleNamespace(id=uuid.uuid4(), owner_id=123)
    provisioner._create_claw_service(core_v1, "claw-123-abc", claw)
    core_v1.create_namespaced_service.assert_called_once()
    _, service = core_v1.create_namespaced_service.call_args.args
    return service


def _build_secret(cortex_base="", cortex_key="", cortex_model=""):
    core_v1 = MagicMock()
    claw = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=123,
        owner_username="owner",
        owner_email="owner@example.com",
        platform_url="http://platform",
        namespace="claw-123-abc",
    )
    token_override = (
        override_settings(HIVE_INTERNAL_AUTH_TOKEN="unit-test-internal-secret")
        if not getattr(settings, "HIVE_INTERNAL_AUTH_TOKEN", "")
        else None
    )
    if token_override:
        token_override.enable()
    try:
        provisioner._create_claw_secret(
            core_v1, "claw-123-abc", claw, "user-token",
            cortex_base=cortex_base, cortex_key=cortex_key, cortex_model=cortex_model,
        )
    finally:
        if token_override:
            token_override.disable()
    core_v1.create_namespaced_secret.assert_called_once()
    _, secret = core_v1.create_namespaced_secret.call_args.args
    return claw, secret


def _claw_container(deployment):
    containers = deployment.spec.template.spec.containers
    return next(c for c in containers if c.name == "claw")


class ClawPodTokenBoundaryTests(SimpleTestCase):
    """HIVE-127 §4 — the claw pod must not get a kube-API token (per-owner boundary)."""

    def test_claw_pod_disables_sa_token_automount(self):
        spec = _build_deployment().spec.template.spec
        self.assertIs(spec.automount_service_account_token, False)

    def test_ensure_namespace_hardens_default_sa(self):
        core_v1 = MagicMock()
        # namespace already exists -> read_namespace returns, no create
        provisioner._harden_default_service_account(core_v1, "claw-123-abc")
        core_v1.patch_namespaced_service_account.assert_called_once()
        kwargs = core_v1.patch_namespaced_service_account.call_args.kwargs
        self.assertEqual(kwargs["name"], "default")
        self.assertEqual(kwargs["namespace"], "claw-123-abc")
        self.assertEqual(kwargs["body"], {"automountServiceAccountToken": False})

    def test_harden_default_sa_tolerates_missing_sa(self):
        core_v1 = MagicMock()
        core_v1.patch_namespaced_service_account.side_effect = ApiException(status=404)
        # 404 (SA not yet created) must NOT raise — pod-spec automount=false still applies.
        provisioner._harden_default_service_account(core_v1, "claw-123-abc")


class ClawDeploymentImageTests(SimpleTestCase):
    def test_pull_policy_is_if_not_present(self):
        container = _claw_container(_build_deployment())
        self.assertEqual(container.image_pull_policy, "IfNotPresent")

    @override_settings(HIVE_CLAW_IMAGE="gx10-1:30500/openclaw:2026.6.11-multiarch")
    def test_image_is_registry_qualified(self):
        container = _claw_container(_build_deployment())
        self.assertEqual(container.image, "gx10-1:30500/openclaw:2026.6.11-multiarch")
        self.assertIn(":30500/", container.image)

    def test_default_claw_image_is_current_openclaw(self):
        from django.conf import settings

        self.assertEqual(
            settings.HIVE_CLAW_IMAGE,
            "gx10-1:30500/openclaw@sha256:b55a1a003f002ed273c7977abf5d30c7ca21008a11357ad065ac08a0eb927cf2",
        )

    def test_default_claw_image_is_registry_qualified(self):
        # The settings default (PLAT-64) must carry a registry host:port and a
        # path component — i.e. NOT a bare ``shizuha-openclaw:latest`` that only
        # resolves from a node-local import / docker.io under IfNotPresent.
        from django.conf import settings

        image = settings.HIVE_CLAW_IMAGE
        registry, sep, _ = image.partition("/")
        self.assertTrue(sep, f"claw image is not registry-qualified: {image!r}")
        self.assertIn(":", registry, f"registry host has no port: {registry!r}")

    @override_settings(
        HIVE_CLAW_IMAGE="gx10-1:30500/openclaw:2026.6.11-multiarch",
        HIVE_HERMES_IMAGE="",
        HIVE_HERMES_OPENCLAW_COMPAT=False,
    )
    def test_hermes_without_runtime_image_or_compat_flag_is_unavailable(self):
        claw = SimpleNamespace(agent_type=Claw.AgentType.HERMES)

        with self.assertRaises(provisioner.HermesRuntimeUnavailable):
            provisioner._resolve_claw_image(claw)

    @override_settings(
        HIVE_CLAW_IMAGE="gx10-1:30500/openclaw:2026.6.11-multiarch",
        HIVE_HERMES_IMAGE="gx10-1:30500/openclaw:2026.6.11-multiarch",
        HIVE_HERMES_OPENCLAW_COMPAT=False,
    )
    def test_hermes_image_equal_to_openclaw_without_flag_is_unavailable(self):
        claw = SimpleNamespace(agent_type=Claw.AgentType.HERMES)

        with self.assertRaises(provisioner.HermesRuntimeUnavailable):
            provisioner.resolve_claw_runtime(claw)

    @override_settings(
        HIVE_CLAW_IMAGE="gx10-1:30500/openclaw:2026.6.11-multiarch",
        HIVE_HERMES_IMAGE="gx10-1:30500/openclaw:2026.6.11-multiarch",
        HIVE_HERMES_OPENCLAW_COMPAT=True,
    )
    def test_hermes_openclaw_compat_requires_explicit_flag(self):
        claw = SimpleNamespace(agent_type=Claw.AgentType.HERMES)

        runtime = provisioner.resolve_claw_runtime(claw)

        self.assertEqual(runtime.image, "gx10-1:30500/openclaw:2026.6.11-multiarch")
        self.assertTrue(runtime.compat_mode)

    @override_settings(
        HIVE_CLAW_IMAGE="gx10-1:30500/openclaw:2026.6.11-multiarch",
        HIVE_HERMES_IMAGE="gx10-1:30500/hermes:2026.7.2-multiarch",
        HIVE_HERMES_OPENCLAW_COMPAT=False,
    )
    def test_hermes_dedicated_image_is_not_compat_mode(self):
        claw = SimpleNamespace(agent_type=Claw.AgentType.HERMES)

        runtime = provisioner.resolve_claw_runtime(claw)

        self.assertEqual(runtime.image, "gx10-1:30500/hermes:2026.7.2-multiarch")
        self.assertFalse(runtime.compat_mode)

    def test_openclaw_data_home_is_mounted_on_the_pvc(self):
        container = _claw_container(_build_deployment())
        mounts = {mount.name: mount for mount in container.volume_mounts}

        self.assertEqual(mounts["claw-data"].mount_path, "/home/node/.openclaw")

    def test_deployment_targets_arm64_longhorn_nodes(self):
        deployment = _build_deployment()

        self.assertEqual(deployment.spec.template.spec.node_selector, {
            "kubernetes.io/arch": "arm64",
            "storage.longhorn.io/ready": "true",
        })

    def test_claw_pvc_uses_customer_ha_storage_class_not_runtime_local_path(self):
        with override_settings(RUNTIME_STORAGE_CLASS="local-path", HIVE_CLAW_STORAGE_CLASS="longhorn-3x"):
            core_v1 = MagicMock()
            claw = SimpleNamespace(id=uuid.uuid4(), owner_id=123)
            provisioner._create_claw_pvc(core_v1, "claw-123-abc", claw, _fake_tier())

        _, pvc = core_v1.create_namespaced_persistent_volume_claim.call_args.args
        self.assertEqual(pvc.spec.storage_class_name, "longhorn-3x")

    def test_init_container_seeds_gateway_mode_local(self):
        # HIVE-31: the multi-arch OpenClaw gateway exits 78 unless gateway.mode
        # is set; every claw must boot with an initContainer that seeds
        # gateway.mode=local into the PVC's openclaw.json (idempotent merge —
        # an explicitly configured mode is left untouched).
        deployment = _build_deployment()
        pod_spec = deployment.spec.template.spec

        [init] = pod_spec.init_containers
        self.assertEqual(init.name, "init-gateway-config")
        # Same image as the claw container — multi-arch, already pulled; the
        # seed script runs via `node -e` so no shell/extra image is needed.
        claw = _claw_container(deployment)
        self.assertEqual(init.image, claw.image)
        self.assertEqual(init.image_pull_policy, claw.image_pull_policy)
        self.assertEqual(init.command[0], "node")
        script = init.command[-1]
        self.assertIn("openclaw.json", script)
        self.assertIn('"local"', script)
        self.assertIn("gateway", script)
        self.assertIn("gateway.controlUi.allowedOrigins", script)
        self.assertIn("https://shizuha.com", script)
        # Never clobber an existing-but-unparseable config (fail-open guard).
        self.assertIn("unparseable", script)
        # Parseable-but-invalid contents (null / string / array / non-object
        # gateway) must also fail open — mutating them would throw and put the
        # initContainer in CrashLoopBackOff (Codex P2 on PR #14).
        self.assertIn("not a valid config object", script)
        self.assertIn('typeof c!=="object"', script)
        self.assertIn("Array.isArray(c)", script)

    def test_init_container_seeds_whatsapp_watchdog_hook(self):
        deployment = _build_deployment()
        [init] = deployment.spec.template.spec.init_containers
        script = init.command[-1]

        self.assertIn('hooks/whatsapp-link-watchdog', script)
        self.assertIn('handler.ts', script)
        self.assertIn('HOOK.md', script)
        self.assertIn('hooks.internal.enabled', script)
        self.assertIn('whatsapp-link-watchdog', script)
        self.assertIn('HIVE_INTERNAL_AUTH_TOKEN', script)

        hook_source = provisioner._wa_watchdog_hook_source()
        self.assertIn("event?.type === 'gateway' && event?.action === 'startup'", hook_source)
        self.assertIn("event?.type === 'message' && event?.action === 'sent'", hook_source)
        self.assertIn('No active WhatsApp Web listener', hook_source)
        self.assertIn('channel_health', hook_source)

    def test_init_container_writes_into_the_claw_data_pvc(self):
        deployment = _build_deployment()
        [init] = deployment.spec.template.spec.init_containers
        mounts = {mount.name: mount for mount in init.volume_mounts}

        self.assertEqual(mounts["claw-data"].mount_path, "/home/node/.openclaw")

    def test_pod_fsgroup_lets_node_user_write_a_fresh_pvc(self):
        # A fresh Longhorn PVC mounts root:root 755; without fsGroup=1000 the
        # non-root node user cannot write openclaw.json on a throwaway claw
        # (mirrors PLAT-146's fleet fsGroup fix).
        deployment = _build_deployment()

        self.assertEqual(deployment.spec.template.spec.security_context.fs_group, 1000)

    def test_openclaw_service_exposes_gateway_port(self):
        service = _build_service()
        [port] = service.spec.ports

        self.assertEqual(port.port, 18789)
        self.assertEqual(port.target_port, 18789)

    def test_existing_openclaw_service_is_replaced_on_reprovision(self):
        core_v1 = MagicMock()
        core_v1.create_namespaced_service.side_effect = ApiException(status=409, reason="Already exists")
        existing = SimpleNamespace(
            metadata=SimpleNamespace(
                name='claw',
                namespace='claw-123-abc',
                labels={'app.kubernetes.io/managed-by': 'legacy'},
                resource_version='rv-77',
                uid='uid-abc',
            ),
            spec=SimpleNamespace(
                selector={'app': 'legacy'},
                ports=[
                    SimpleNamespace(name='http', port=8016, target_port=8016),
                ],
                cluster_ip='10.43.0.10',
                cluster_ips=['10.43.0.10'],
            ),
        )
        core_v1.read_namespaced_service.return_value = existing
        claw = SimpleNamespace(id=uuid.uuid4(), owner_id=123)

        provisioner._create_claw_service(core_v1, "claw-123-abc", claw)

        core_v1.read_namespaced_service.assert_called_once_with(name='claw', namespace='claw-123-abc')
        core_v1.replace_namespaced_service.assert_called_once()
        name, namespace, service = core_v1.replace_namespaced_service.call_args.args
        [port] = service.spec.ports
        self.assertEqual(name, "claw")
        self.assertEqual(namespace, "claw-123-abc")
        self.assertEqual(port.port, 18789)
        self.assertEqual(port.target_port, 18789)
        self.assertEqual(service.metadata.labels['app.kubernetes.io/managed-by'], 'shizuha-hive')
        self.assertEqual(service.spec.selector, {'app': 'claw'})
        self.assertEqual(service.metadata.resource_version, 'rv-77')
        self.assertEqual(service.spec.cluster_ip, '10.43.0.10')
        self.assertEqual(service.spec.cluster_ips, ['10.43.0.10'])
        self.assertEqual(service.metadata.uid, 'uid-abc')

    def test_openclaw_port_env_matches_service_port(self):
        container = _claw_container(_build_deployment())
        env = {entry.name: entry for entry in container.env}

        self.assertEqual(env["PORT"].value, "18789")

    def test_internal_auth_token_is_injected_from_claw_secret(self):
        container = _claw_container(_build_deployment())
        env = {entry.name: entry for entry in container.env}

        token_env = env["HIVE_INTERNAL_AUTH_TOKEN"]
        self.assertEqual(token_env.value_from.secret_key_ref.name, "claw-config")
        self.assertEqual(token_env.value_from.secret_key_ref.key, "internal-auth-token")

        gateway_env = env["OPENCLAW_GATEWAY_TOKEN"]
        self.assertEqual(gateway_env.value_from.secret_key_ref.name, "claw-config")
        self.assertEqual(gateway_env.value_from.secret_key_ref.key, "gateway-auth-token")

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-root-secret")
    def test_claw_secret_contains_per_claw_internal_auth_token(self):
        from claws import lifecycle

        claw, secret = _build_secret()

        self.assertEqual(secret.string_data["internal-auth-token"], lifecycle.internal_auth_token(claw))
        self.assertNotEqual(secret.string_data["internal-auth-token"], "internal-root-secret")
        self.assertEqual(secret.string_data["gateway-auth-token"], lifecycle.gateway_auth_token(claw))
        self.assertNotEqual(secret.string_data["gateway-auth-token"], secret.string_data["internal-auth-token"])

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="")
    def test_provisioning_fails_fast_when_signing_secret_unset(self):
        # HIVE-165 regression: with HIVE_INTERNAL_AUTH_TOKEN unset, internal_auth_token()
        # fails closed to ''. Provisioning must NOT silently create a claw-config with an
        # empty internal-auth-token (which makes the OpenClaw gateway refuse to bind →
        # CrashLoopBackOff). It must raise so the claw is marked FAILED, never a broken pod.
        core_v1 = MagicMock()
        claw = SimpleNamespace(
            id=uuid.uuid4(),
            owner_id=123,
            owner_username="owner",
            platform_url="http://platform",
            namespace="claw-123-abc",
        )
        with self.assertRaises(RuntimeError):
            provisioner._create_claw_secret(core_v1, "claw-123-abc", claw, "user-token")
        core_v1.create_namespaced_secret.assert_not_called()

class ClawDuplicateCleanupTests(TestCase):
    def _ns(self, name, owner_id, claw_id):
        return SimpleNamespace(metadata=SimpleNamespace(
            name=name,
            labels={
                'app.kubernetes.io/managed-by': 'shizuha-hive',
                'shizuha.io/owner-id': str(owner_id),
                'shizuha.io/claw-id': claw_id,
            },
        ))

    def test_cleanup_stale_duplicate_namespaces_keeps_active_claws_and_deletes_stale(self):
        active = Claw.objects.create(
            owner_id=252,
            owner_username='owner',
            owner_email='owner@example.com',
            name='Owner Claw',
            status=Claw.Status.RUNNING,
        )
        stale_id = uuid.uuid4().hex
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        core_v1.list_namespace.return_value.items = [
            self._ns(active.namespace, 252, active.id.hex),
            self._ns(f'claw-252-{stale_id}', 252, stale_id),
        ]

        summary = provisioner.cleanup_stale_duplicate_claw_namespaces(core_v1, apps_v1, 252, dry_run=False)

        self.assertEqual(summary['kept'], [{'namespace': active.namespace, 'claw_id': active.id.hex}])
        self.assertEqual(summary['stale'], [{'namespace': f'claw-252-{stale_id}', 'claw_id': stale_id}])
        self.assertEqual(summary['deleted'], [{'namespace': f'claw-252-{stale_id}', 'claw_id': stale_id}])
        apps_v1.patch_namespaced_deployment_scale.assert_called_once_with(
            provisioner.DEPLOYMENT_NAME,
            f'claw-252-{stale_id}',
            {'spec': {'replicas': 0}},
        )
        core_v1.delete_namespace.assert_called_once_with(name=f'claw-252-{stale_id}')

    def test_cleanup_stale_duplicate_namespaces_dry_run_does_not_delete(self):
        stale_id = uuid.uuid4().hex
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        core_v1.list_namespace.return_value.items = [self._ns(f'claw-116-{stale_id}', 116, stale_id)]

        summary = provisioner.cleanup_stale_duplicate_claw_namespaces(core_v1, apps_v1, 116, dry_run=True)

        self.assertEqual(len(summary['stale']), 1)
        self.assertEqual(summary['deleted'], [])
        apps_v1.patch_namespaced_deployment_scale.assert_not_called()
        core_v1.delete_namespace.assert_not_called()


class ClawAgentTypeCortexTests(SimpleTestCase):
    """HIVE per-owner Cortex: agent_type image selection + cortex wiring.

    'claw' (default) → OpenClaw image, cortex provider merged into openclaw.json;
    'scli' → shizuha-runtime image, cortex injected as env, no init container.
    """

    # ── image selection ──
    @override_settings(
        HIVE_CLAW_IMAGE="gx10-1:30500/openclaw:tag",
        HIVE_SCLI_IMAGE="localhost:30500/shizuha-runtime:latest",
    )
    def test_claw_type_uses_openclaw_image(self):
        container = _claw_container(_build_deployment(agent_type="claw"))
        self.assertEqual(container.image, "gx10-1:30500/openclaw:tag")

    @override_settings(
        HIVE_CLAW_IMAGE="gx10-1:30500/openclaw:tag",
        HIVE_SCLI_IMAGE="localhost:30500/shizuha-runtime:latest",
    )
    def test_scli_type_uses_runtime_image(self):
        container = _claw_container(_build_deployment(agent_type="scli"))
        self.assertEqual(container.image, "localhost:30500/shizuha-runtime:latest")

    def test_default_scli_image_setting(self):
        from django.conf import settings
        self.assertEqual(settings.HIVE_SCLI_IMAGE, "localhost:30500/shizuha-runtime:latest")

    # ── claw: cortex provider merged into openclaw.json init script ──
    def test_claw_init_script_merges_cortex_provider_when_key_present(self):
        deployment = _build_deployment(
            agent_type="claw",
            cortex_base="https://cortex.shizuha.com/v1",
            cortex_key="sk-cortex-xyz",
            cortex_model="Qwen3.6-27B",
        )
        [init] = deployment.spec.template.spec.init_containers
        script = init.command[-1]
        self.assertIn("c.models.providers.cortex", script)
        self.assertIn("openai-completions", script)
        self.assertIn("process.env.CORTEX_API_KEY", script)
        self.assertNotIn("sk-cortex-xyz", script)
        self.assertIn("https://cortex.shizuha.com/v1", script)
        self.assertIn("cortex/Qwen3.6-27B", script)
        self.assertIn("c.agents.defaults.model", script)
        self.assertIn('directPolicy:"block"', script)
        init_env = {e.name: e for e in init.env}
        self.assertEqual(init_env["CORTEX_API_KEY"].value_from.secret_key_ref.name, "claw-config")
        self.assertEqual(init_env["CORTEX_API_KEY"].value_from.secret_key_ref.key, "cortex-api-key")
        self.assertEqual(init_env["CORTEX_BASE_URL"].value_from.secret_key_ref.key, "cortex-base-url")
        self.assertEqual(init_env["CORTEX_MODEL"].value_from.secret_key_ref.key, "cortex-model")
        # gateway.mode seeding is preserved
        self.assertIn('"local"', script)

    def test_claw_init_script_generates_deepseek_v4_flash_metadata(self):
        deployment = _build_deployment(
            agent_type="claw",
            cortex_base="http://cortex.shizuha-cortex.svc.cluster.local:8040/v1",
            cortex_key="sk-cortex-xyz",
            cortex_model="DeepSeek-V4-Flash",
        )
        [init] = deployment.spec.template.spec.init_containers
        script = init.command[-1]

        self.assertIn('const isDeepSeekV4Flash=/^DeepSeek-V4-Flash$/i.test(cortexModel);', script)
        # provisioner seeds non-V4-Flash models at 131072 (not 128000) — keep in sync
        self.assertIn('const cortexContext=isDeepSeekV4Flash?262144:131072;', script)
        self.assertIn('contextWindow:cortexContext', script)
        self.assertIn('contextTokens:cortexContext', script)
        self.assertIn('maxTokens:cortexContext', script)
        self.assertIn('compat:{supportsTools:true,supportsUsageInStreaming:true,supportsDeveloperRole:false}', script)

    def test_claw_init_script_omits_cortex_provider_without_key(self):
        deployment = _build_deployment(
            agent_type="claw",
            cortex_base="https://cortex.shizuha.com/v1",
            cortex_key="",  # no key minted
            cortex_model="Qwen3.6-27B",
        )
        [init] = deployment.spec.template.spec.init_containers
        script = init.command[-1]
        self.assertNotIn("c.models.providers.cortex", script)
        init_env = {e.name for e in init.env}
        self.assertNotIn("CORTEX_API_KEY", init_env)
        # still seeds gateway.mode
        self.assertIn('"local"', script)

    def test_claw_does_not_get_cortex_env(self):
        container = _claw_container(_build_deployment(
            agent_type="claw", cortex_base="https://cortex.shizuha.com/v1",
            cortex_key="sk-cortex-xyz", cortex_model="Qwen3.6-27B",
        ))
        env = {e.name for e in container.env}
        self.assertNotIn("CORTEX_BASE_URL", env)
        self.assertNotIn("CORTEX_API_KEY", env)

    # ── scli: cortex env injected, no init container, key via secretKeyRef ──
    def test_scli_injects_cortex_env(self):
        container = _claw_container(_build_deployment(
            agent_type="scli", cortex_base="https://cortex.shizuha.com/v1",
            cortex_key="sk-cortex-xyz", cortex_model="Qwen3.6-27B",
        ))
        env = {e.name: e for e in container.env}
        self.assertEqual(env["CORTEX_BASE_URL"].value, "https://cortex.shizuha.com/v1")
        self.assertEqual(env["MODEL"].value, "Qwen3.6-27B")
        # the API key is injected from the claw-config Secret, never a literal
        self.assertIsNone(env["CORTEX_API_KEY"].value)
        self.assertEqual(env["CORTEX_API_KEY"].value_from.secret_key_ref.name, "claw-config")
        self.assertEqual(env["CORTEX_API_KEY"].value_from.secret_key_ref.key, "cortex-api-key")

    def test_scli_has_no_init_container(self):
        deployment = _build_deployment(agent_type="scli", cortex_key="sk-cortex-xyz", cortex_model="m")
        self.assertIsNone(deployment.spec.template.spec.init_containers)

    def test_scli_without_key_still_sets_base_and_model(self):
        container = _claw_container(_build_deployment(
            agent_type="scli", cortex_base="https://cortex.shizuha.com/v1",
            cortex_key="", cortex_model="Qwen3.6-27B",
        ))
        env = {e.name: e for e in container.env}
        self.assertEqual(env["CORTEX_BASE_URL"].value, "https://cortex.shizuha.com/v1")
        self.assertEqual(env["MODEL"].value, "Qwen3.6-27B")
        self.assertNotIn("CORTEX_API_KEY", env)

    # ── HIVE-1957: SCLI claws get Connect auth so the runtime's WebSocket
    #    ConnectClient can connect to Connect's AgentChatConsumer and receive
    #    DMs directly (SCLI claws have no /v1/connect/messages HTTP bridge). ──
    def test_scli_injects_connect_auth_env(self):
        container = _claw_container(_build_deployment(agent_type="scli"))
        env = {e.name: e for e in container.env}
        self.assertEqual(env["AGENT_ID"].value, env["HIVE_CLAW_ID"].value)
        self.assertEqual(env["AGENT_USERNAME"].value, "owner")
        self.assertEqual(env["AGENT_NAME"].value, "My Assistant")
        self.assertEqual(env["SHIZUHA_CONNECT_AUTOREPLY"].value, "1")
        self.assertEqual(env["SHIZUHA_CONNECT_REPLY_EMAIL"].value, "owner@example.com")
        # the Connect identity token comes from the claw-config Secret, never a literal
        self.assertIsNone(env["AGENT_ACCESS_TOKEN"].value)
        self.assertEqual(env["AGENT_ACCESS_TOKEN"].value_from.secret_key_ref.name, "claw-config")
        self.assertEqual(env["AGENT_ACCESS_TOKEN"].value_from.secret_key_ref.key, "connect-access-token")
        # HIVE-1957: dial Connect's AgentChatConsumer WS via svc DNS (in-cluster),
        # not the public platform URL (Tailscale edge excluded by netpols).
        self.assertEqual(
            env["CONNECT_WS_URL"].value,
            "ws://shizuha-connect.shizuha.svc.cluster.local:8019/connect/ws/connect/agent/",
        )

    def test_openclaw_claw_does_not_get_connect_auth_env(self):
        container = _claw_container(_build_deployment(agent_type="claw"))
        env = {e.name for e in container.env}
        self.assertNotIn("AGENT_ACCESS_TOKEN", env)
        self.assertNotIn("AGENT_USERNAME", env)
        self.assertNotIn("SHIZUHA_CONNECT_AUTOREPLY", env)

    def test_secret_stores_connect_access_token(self):
        _, secret = _build_secret()
        self.assertEqual(secret.string_data["connect-access-token"], "user-token")

    # ── secret stores the minted owner key ──
    def test_secret_stores_cortex_key_when_present(self):
        _, secret = _build_secret(cortex_key="sk-cortex-xyz")
        self.assertEqual(secret.string_data["cortex-api-key"], "sk-cortex-xyz")

    def test_secret_omits_cortex_key_when_absent(self):
        _, secret = _build_secret(cortex_key="")
        self.assertNotIn("cortex-api-key", secret.string_data)

    # ── model resolution: request > tier > built-in default ──
    @override_settings(HIVE_CORTEX_DEFAULT_MODEL="Qwen3.6-27B")
    def test_resolve_cortex_model_request_wins(self):
        claw = SimpleNamespace(model="Custom-Model")
        tier = SimpleNamespace(llm_default_model="Tier-Model")
        self.assertEqual(provisioner.resolve_cortex_model(claw, tier), "Custom-Model")

    @override_settings(HIVE_CORTEX_DEFAULT_MODEL="Qwen3.6-27B")
    def test_resolve_cortex_model_tier_fallback(self):
        claw = SimpleNamespace(model="")
        tier = SimpleNamespace(llm_default_model="Tier-Model")
        self.assertEqual(provisioner.resolve_cortex_model(claw, tier), "Tier-Model")

    @override_settings(HIVE_CORTEX_DEFAULT_MODEL="Qwen3.6-27B")
    def test_resolve_cortex_model_builtin_default(self):
        claw = SimpleNamespace(model="")
        tier = SimpleNamespace(llm_default_model="")
        self.assertEqual(provisioner.resolve_cortex_model(claw, tier), "Qwen3.6-27B")

    # ── owner cortex key minting ──
    def test_org_id_from_owner_jwt_memberships(self):
        import base64, json as _json
        payload = base64.urlsafe_b64encode(_json.dumps({
            'user_id': '3',
            'organization_memberships': {'7': 'admin', '1': 'owner'},
        }).encode()).decode().rstrip('=')
        token = f'aaa.{payload}.sig'
        self.assertEqual(provisioner._org_id_from_user_token(token), 1)
        self.assertEqual(provisioner._org_id_from_user_token('not-a-jwt'), 0)

    @override_settings(
        CORTEX_BASE_URL="http://cortex:8040",
        HIVE_CORTEX_LEASE_API_KEY="sk-trusted-service",
        CORTEX_API_KEY="sk-customer-tier",
    )
    def test_mint_sends_org_id_from_owner_jwt(self):
        import base64, json as _json
        from unittest.mock import patch, MagicMock as MM
        payload = base64.urlsafe_b64encode(_json.dumps({
            'organization_memberships': {'1': 'owner'},
        }).encode()).decode().rstrip('=')
        claw = SimpleNamespace(id=uuid.uuid4(), owner_id=42, agent_type="scli")
        resp = MM(status_code=201)
        resp.json.return_value = {"key": "sk-cortex-minted"}
        with patch("claws.provisioner.requests.post", return_value=resp) as post:
            provisioner.mint_owner_cortex_key(claw, f'aaa.{payload}.sig')
        self.assertEqual(post.call_args.kwargs["json"]["org_id"], 1)

    @override_settings(
        CORTEX_BASE_URL="http://cortex:8040",
        HIVE_CORTEX_LEASE_API_KEY="sk-trusted-service",
        CORTEX_API_KEY="sk-customer-tier",
    )
    def test_mint_prefers_lease_key_not_customer_tier_cortex_key(self):
        """HIVE-1951: the customer-tier CORTEX_API_KEY 403s on /v1/internal/api-keys."""
        from unittest.mock import patch, MagicMock as MM
        claw = SimpleNamespace(id=uuid.uuid4(), owner_id=42, agent_type="scli")
        resp = MM(status_code=201)
        resp.json.return_value = {"key": "sk-cortex-minted"}
        with patch("claws.provisioner.requests.post", return_value=resp) as post:
            key = provisioner.mint_owner_cortex_key(claw, "owner-jwt-ignored")
        self.assertEqual(key, "sk-cortex-minted")
        auth = post.call_args.kwargs["headers"]["Authorization"]
        self.assertEqual(auth, "Bearer sk-trusted-service")
        self.assertNotIn("sk-customer-tier", auth)

    @override_settings(CORTEX_BASE_URL="http://cortex:8040", CORTEX_API_KEY="fleet-secret")
    def test_mint_owner_cortex_key_returns_plaintext(self):
        from unittest.mock import patch, MagicMock as MM
        claw = SimpleNamespace(id=uuid.uuid4(), owner_id=42, agent_type="hermes")
        resp = MM(status_code=201)
        resp.json.return_value = {"key": "sk-cortex-minted"}
        with patch("claws.provisioner.requests.post", return_value=resp) as post:
            key = provisioner.mint_owner_cortex_key(claw, "owner-jwt-ignored")
        self.assertEqual(key, "sk-cortex-minted")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://cortex:8040/v1/internal/api-keys")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fleet-secret")
        self.assertEqual(kwargs["json"], {
            "name": f"hive-hermes-{claw.id.hex}",
            "user_id": 42,
            "service_tier": "claw-longctx",
            # 2026-08-05: the mint must declare the traffic class. Omitting it
            # made Cortex apply its RESOLD_CUSTOMER default to every claw,
            # which its internal-model gate refuses outright.
            "traffic_class": "resold_customer",
            "billing_user_id": 42,
        })
        self.assertNotIn("rate_limit_tpm", kwargs["json"])

    @override_settings(CORTEX_BASE_URL="http://cortex:8040", CORTEX_API_KEY="fleet-secret")
    def test_mint_owner_cortex_key_failopen_on_error(self):
        from unittest.mock import patch, MagicMock as MM
        claw = SimpleNamespace(id=uuid.uuid4(), owner_id=42, agent_type="claw")
        resp = MM(status_code=403, text="forbidden")
        with patch("claws.provisioner.requests.post", return_value=resp):
            self.assertEqual(provisioner.mint_owner_cortex_key(claw, "owner-jwt"), "")

    @override_settings(CORTEX_API_KEY="")
    def test_mint_owner_cortex_key_no_service_credential(self):
        claw = SimpleNamespace(id=uuid.uuid4(), owner_id=42, agent_type="claw")
        self.assertEqual(provisioner.mint_owner_cortex_key(claw, "owner-jwt"), "")

    @override_settings(CORTEX_BASE_URL="http://cortex:8040", CORTEX_API_KEY="fleet-secret")
    def test_mint_owner_cortex_key_keeps_scli_standard_tier(self):
        from unittest.mock import patch, MagicMock as MM
        claw = SimpleNamespace(id=uuid.uuid4(), owner_id=42, agent_type="scli")
        resp = MM(status_code=201)
        resp.json.return_value = {"key": "sk-cortex-minted"}
        with patch("claws.provisioner.requests.post", return_value=resp) as post:
            self.assertEqual(provisioner.mint_owner_cortex_key(claw, "owner-jwt-ignored"), "sk-cortex-minted")
        self.assertEqual(post.call_args.kwargs["json"]["service_tier"], "standard")

    @override_settings(CORTEX_PUBLIC_BASE_URL="http://cortex:8040/v1")
    def test_cortex_model_probe_uses_exact_credential_catalog(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": [{"id": "DeepSeek-V4-Flash"}]}
        with patch("claws.provisioner.requests.get", return_value=response) as get:
            access = provisioner.probe_cortex_model_access(
                "sk-owner", "cortex/DeepSeek-V4-Flash",
            )
        self.assertEqual(access.status, "available")
        self.assertEqual(get.call_args.args[0], "http://cortex:8040/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer sk-owner")

    @override_settings(CORTEX_PUBLIC_BASE_URL="http://cortex:8040/v1")
    def test_cortex_model_probe_reports_policy_denial_without_fallback(self):
        response = MagicMock(status_code=403)
        with patch("claws.provisioner.requests.get", return_value=response):
            access = provisioner.probe_cortex_model_access(
                "sk-resold", "cortex/DeepSeek-V4-Flash",
            )
        self.assertEqual(access.status, "unavailable")
        self.assertIn("HTTP 403", access.reason)


class ClawLongctxBackfillCommandTests(TestCase):
    def _create_claw(self, *, agent_type=Claw.AgentType.CLAW, status=Claw.Status.RUNNING, namespace='claw-42-test'):
        return Claw.objects.create(
            owner_id=42,
            owner_username='owner',
            owner_email='owner@example.com',
            agent_type=agent_type,
            status=status,
            namespace=namespace,
            tier_id='free',
            platform_url='https://s1.tail.shizuha.com',
        )

    def test_backfill_command_mints_secret_and_restarts_claw_hermes_only(self):
        claw = self._create_claw(agent_type=Claw.AgentType.CLAW, namespace='claw-42-claw')
        hermes = self._create_claw(agent_type=Claw.AgentType.HERMES, namespace='claw-42-hermes')
        self._create_claw(agent_type=Claw.AgentType.SCLI, namespace='claw-42-scli')
        self._create_claw(agent_type=Claw.AgentType.CLAW, status=Claw.Status.DELETED, namespace='claw-42-deleted')

        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        with patch('claws.management.commands.backfill_claw_longctx_cortex_keys.get_k8s_client', return_value=(core_v1, apps_v1)), \
             patch('claws.management.commands.backfill_claw_longctx_cortex_keys.mint_owner_cortex_key', return_value='sk-longctx') as mint:
            out = StringIO()
            call_command('backfill_claw_longctx_cortex_keys', stdout=out)

        self.assertEqual([call.args[0] for call in mint.call_args_list], [claw, hermes])
        self.assertEqual(core_v1.patch_namespaced_secret.call_count, 2)
        core_v1.patch_namespaced_secret.assert_any_call(
            name='claw-config',
            namespace='claw-42-claw',
            body={'stringData': {'cortex-api-key': 'sk-longctx'}},
        )
        self.assertEqual(apps_v1.patch_namespaced_deployment.call_count, 2)
        first_body = apps_v1.patch_namespaced_deployment.call_args_list[0].kwargs['body']
        self.assertIn(
            'hive.shizuha.com/claw-longctx-backfill-at',
            first_body['spec']['template']['metadata']['annotations'],
        )
        self.assertIn('scanned=2 minted=2 patched=2 restarted=2 skipped=0', out.getvalue())

    def test_backfill_command_dry_run_does_not_mint_or_patch(self):
        self._create_claw(agent_type=Claw.AgentType.CLAW, namespace='claw-42-claw')
        with patch('claws.management.commands.backfill_claw_longctx_cortex_keys.get_k8s_client') as get_k8s, \
             patch('claws.management.commands.backfill_claw_longctx_cortex_keys.mint_owner_cortex_key') as mint:
            out = StringIO()
            call_command('backfill_claw_longctx_cortex_keys', '--dry-run', stdout=out)

        get_k8s.assert_not_called()
        mint.assert_not_called()
        self.assertIn('scanned=1 minted=0 patched=0 restarted=0 skipped=0 (dry-run)', out.getvalue())


class ClawProvisionerPolicyTests(SimpleTestCase):
    def setUp(self):
        self.claw = SimpleNamespace(
            id=uuid.uuid4(),
            owner_id=123,
            namespace='claw-123-abc',
        )
        self.tier = SimpleNamespace(
            cpu_request='250m',
            cpu_limit='2',
            mem_request='512Mi',
            mem_limit='2Gi',
            pvc_size='10Gi',
        )

    def test_ensure_network_policies_creates_fail_closed_bundle(self):
        net_v1 = MagicMock()
        expected_policy_names = [
            'default-deny-all',
            'dns-egress',
            'platform-egress',
            'cortex-egress',
            'wan-egress',
            'dashboard-ingress',
            'owner-isolation',
        ]

        with patch("claws.provisioner.client.NetworkingV1Api", return_value=net_v1):
            provisioner._ensure_network_policies(MagicMock(), self.claw.namespace, self.claw, self.tier)

        self.assertEqual(len(net_v1.create_namespaced_network_policy.call_args_list), 7)
        self.assertEqual(
            [call.args[1].metadata.name for call in net_v1.create_namespaced_network_policy.call_args_list],
            expected_policy_names,
        )
        default_policy = next(
            call.args[1] for call in net_v1.create_namespaced_network_policy.call_args_list
            if call.args[1].metadata.name == 'default-deny-all'
        )
        self.assertEqual(default_policy.spec.policy_types, ['Ingress', 'Egress'])
        dns_policy = next(
            call.args[1] for call in net_v1.create_namespaced_network_policy.call_args_list
            if call.args[1].metadata.name == 'dns-egress'
        )
        dns_ports = {int(item.port) for item in dns_policy.spec.egress[0].ports}
        self.assertIn(53, dns_ports)
        self.assertEqual(dns_policy.spec.egress[0].to[0].namespace_selector.match_labels['kubernetes.io/metadata.name'], 'kube-system')
        cortex_policy = next(
            call.args[1] for call in net_v1.create_namespaced_network_policy.call_args_list
            if call.args[1].metadata.name == 'cortex-egress'
        )
        cortex_ports = {int(item.port) for item in cortex_policy.spec.egress[0].ports}
        self.assertIn(8040, cortex_ports)
        self.assertEqual(
            cortex_policy.spec.egress[0].to[0].namespace_selector.match_labels['kubernetes.io/metadata.name'],
            'shizuha-cortex',
        )
        dashboard_policy = next(
            call.args[1] for call in net_v1.create_namespaced_network_policy.call_args_list
            if call.args[1].metadata.name == 'dashboard-ingress'
        )
        dashboard_peer = dashboard_policy.spec.ingress[0]._from[0]
        self.assertEqual(
            dashboard_peer.namespace_selector.match_labels['kubernetes.io/metadata.name'],
            'shizuha',
        )
        self.assertEqual(
            dashboard_peer.pod_selector.match_labels['app.kubernetes.io/name'],
            'shizuha-nginx',
        )
        dashboard_ports = {int(item.port) for item in dashboard_policy.spec.ingress[0].ports}
        self.assertIn(18789, dashboard_ports)
        # HIVE-366: wan-egress opens public internet (80/443) but the ipBlock
        # except-list must carve out every private/cluster range so WAN egress
        # cannot reach in-cluster or private-network services (AC4).
        wan_policy = next(
            call.args[1] for call in net_v1.create_namespaced_network_policy.call_args_list
            if call.args[1].metadata.name == 'wan-egress'
        )
        self.assertEqual(wan_policy.spec.policy_types, ['Egress'])
        wan_ports = {int(item.port) for item in wan_policy.spec.egress[0].ports}
        self.assertEqual(wan_ports, {80, 443})
        wan_ipblock = wan_policy.spec.egress[0].to[0].ip_block
        self.assertEqual(wan_ipblock.cidr, '0.0.0.0/0')
        for private_cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                             '169.254.0.0/16', '100.64.0.0/10'):
            self.assertIn(private_cidr, wan_ipblock._except,
                          f'wan-egress must exclude private range {private_cidr} (AC4)')
        # It must NOT target an in-cluster namespace (that would be private-svc reach).
        self.assertIsNone(wan_policy.spec.egress[0].to[0].namespace_selector)
        # PLAT-8489: the second wan-egress rule is the dedicated s1 bridge
        # carve-out (ori MCP :9500, google-drive MCP :9600, fleet-control
        # :8016) — /32 node-set CIDRs only, so the tailnet/LAN stay dark for
        # every other destination and port.
        self.assertEqual(len(wan_policy.spec.egress), 2)
        s1_rule = wan_policy.spec.egress[1]
        s1_cidrs = {peer.ip_block.cidr for peer in s1_rule.to}
        self.assertEqual(
            s1_cidrs,
            {'100.64.0.3/32', '192.168.0.136/32', '192.168.88.136/32'},
        )
        s1_ports = {int(item.port) for item in s1_rule.ports}
        self.assertEqual(s1_ports, {9500, 9600, 8016})

        owner_policy = next(
            call.args[1] for call in net_v1.create_namespaced_network_policy.call_args_list
            if call.args[1].metadata.name == 'owner-isolation'
        )
        owner_ns = owner_policy.spec.ingress[0]._from[0].namespace_selector.match_labels['kubernetes.io/metadata.name']
        self.assertEqual(owner_ns, self.claw.namespace)

    def test_scli_claw_gets_connect_egress_network_policy(self):
        # HIVE-1957: SCLI claws dial Connect's AgentChatConsumer WS in-cluster
        # (no /v1/connect/messages HTTP bridge) — the bundle must include a
        # connect-egress rule opening the shizuha namespace on the Connect port.
        net_v1 = MagicMock()
        scli_claw = SimpleNamespace(
            id=uuid.uuid4(),
            owner_id=123,
            namespace='claw-123-abc',
            agent_type=Claw.AgentType.SCLI,
        )

        with patch("claws.provisioner.client.NetworkingV1Api", return_value=net_v1):
            provisioner._ensure_network_policies(MagicMock(), scli_claw.namespace, scli_claw, self.tier)

        names = [call.args[1].metadata.name for call in net_v1.create_namespaced_network_policy.call_args_list]
        self.assertIn('connect-egress', names)
        connect_policy = next(
            call.args[1] for call in net_v1.create_namespaced_network_policy.call_args_list
            if call.args[1].metadata.name == 'connect-egress'
        )
        self.assertEqual(connect_policy.spec.policy_types, ['Egress'])
        self.assertEqual(
            connect_policy.spec.egress[0].to[0].namespace_selector.match_labels['kubernetes.io/metadata.name'],
            'shizuha',
        )
        connect_ports = {int(item.port) for item in connect_policy.spec.egress[0].ports}
        self.assertIn(8019, connect_ports)

    def test_openclaw_claw_does_not_get_connect_egress_network_policy(self):
        net_v1 = MagicMock()
        with patch("claws.provisioner.client.NetworkingV1Api", return_value=net_v1):
            provisioner._ensure_network_policies(MagicMock(), self.claw.namespace, self.claw, self.tier)
        names = [call.args[1].metadata.name for call in net_v1.create_namespaced_network_policy.call_args_list]
        self.assertNotIn('connect-egress', names)

    def test_ensure_network_policies_replaces_existing_on_conflict(self):
        net_v1 = MagicMock()
        net_v1.create_namespaced_network_policy.side_effect = [ApiException(status=409)] * 7

        with patch("claws.provisioner.client.NetworkingV1Api", return_value=net_v1):
            provisioner._ensure_network_policies(MagicMock(), self.claw.namespace, self.claw, self.tier)

        self.assertEqual(net_v1.replace_namespaced_network_policy.call_count, 7)
        self.assertEqual(
            [call.args[0] for call in net_v1.replace_namespaced_network_policy.call_args_list],
            [
                'default-deny-all',
                'dns-egress',
                'platform-egress',
                'cortex-egress',
                'wan-egress',
                'dashboard-ingress',
                'owner-isolation',
            ],
        )

    def test_ensure_resource_guards_creates_quota_and_limits(self):
        core_v1 = MagicMock()

        provisioner._ensure_resource_guards(core_v1, self.claw.namespace, self.claw, self.tier)

        quota = core_v1.create_namespaced_resource_quota.call_args.args[1]
        limit_range = core_v1.create_namespaced_limit_range.call_args.args[1]
        self.assertEqual(quota.metadata.name, 'claw-resource-boundaries')
        self.assertEqual(quota.spec.hard['requests.storage'], '10Gi')
        self.assertEqual(limit_range.metadata.name, 'claw-limit-range')
        self.assertEqual(limit_range.spec.limits[0].default_request['memory'], '512Mi')

    def test_ensure_resource_guards_replaces_existing_on_conflict(self):
        core_v1 = MagicMock()
        core_v1.create_namespaced_resource_quota.side_effect = ApiException(status=409)
        core_v1.create_namespaced_limit_range.side_effect = ApiException(status=409)

        provisioner._ensure_resource_guards(core_v1, self.claw.namespace, self.claw, self.tier)

        core_v1.replace_namespaced_resource_quota.assert_called_once_with(
            'claw-resource-boundaries',
            self.claw.namespace,
            core_v1.create_namespaced_resource_quota.call_args.args[1],
        )
        core_v1.replace_namespaced_limit_range.assert_called_once_with(
            'claw-limit-range',
            self.claw.namespace,
            core_v1.create_namespaced_limit_range.call_args.args[1],
        )


    def test_audit_claw_namespace_isolation_reports_missing_bundle(self):
        core_v1 = MagicMock()
        core_v1.read_namespace.return_value = MagicMock()
        core_v1.read_namespaced_resource_quota.side_effect = ApiException(status=404)
        core_v1.read_namespaced_limit_range.side_effect = ApiException(status=404)
        core_v1.read_namespaced_service_account.return_value = SimpleNamespace(
            automount_service_account_token=True,
        )
        net_v1 = MagicMock()
        net_v1.list_namespaced_network_policy.return_value = SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name='default-deny-all'))],
        )

        with patch("claws.provisioner.client.NetworkingV1Api", return_value=net_v1):
            missing = provisioner.audit_claw_namespace_isolation(core_v1, self.claw.namespace)

        self.assertIn('networkpolicy/dns-egress', missing)
        self.assertIn('networkpolicy/platform-egress', missing)
        self.assertIn('networkpolicy/cortex-egress', missing)
        self.assertIn('networkpolicy/wan-egress', missing)
        self.assertIn('networkpolicy/dashboard-ingress', missing)
        self.assertIn('networkpolicy/owner-isolation', missing)
        self.assertIn('resourcequota/claw-resource-boundaries', missing)
        self.assertIn('limitrange/claw-limit-range', missing)
        self.assertIn('serviceaccount/default/automountServiceAccountToken=false', missing)

    def test_audit_claw_namespace_isolation_passes_when_required_controls_exist(self):
        core_v1 = MagicMock()
        core_v1.read_namespace.return_value = MagicMock()
        core_v1.read_namespaced_resource_quota.return_value = MagicMock()
        core_v1.read_namespaced_limit_range.return_value = MagicMock()
        core_v1.read_namespaced_service_account.return_value = SimpleNamespace(
            automount_service_account_token=False,
        )
        net_v1 = MagicMock()
        net_v1.list_namespaced_network_policy.return_value = SimpleNamespace(
            items=[
                SimpleNamespace(metadata=SimpleNamespace(name=name))
                for name in provisioner.REQUIRED_CLAW_NETWORK_POLICIES
            ],
        )

        with patch("claws.provisioner.client.NetworkingV1Api", return_value=net_v1):
            missing = provisioner.audit_claw_namespace_isolation(core_v1, self.claw.namespace)

        self.assertEqual(missing, [])


class ClawNamespaceIsolationReconcileTests(TestCase):
    def test_reconcile_claw_namespace_isolation_backfills_drift(self):
        claw = Claw.objects.create(
            owner_id=116,
            owner_username='owner',
            owner_email='owner@example.com',
            name='Existing Claw',
            namespace='claw-116-existing',
            status=Claw.Status.RUNNING,
            tier_id='free',
        )
        core_v1 = MagicMock()
        with patch('claws.provisioner.get_k8s_client', return_value=(core_v1, MagicMock())), \
             patch(
                 'claws.provisioner.audit_claw_namespace_isolation',
                 side_effect=[['networkpolicy/dns-egress'], []],
             ) as audit, \
             patch('claws.provisioner.resolve_tier', return_value=SimpleNamespace()) as resolve_tier, \
             patch('claws.provisioner._ensure_namespace') as ensure_namespace, \
             patch('claws.provisioner._ensure_network_policies') as ensure_policies, \
             patch('claws.provisioner._ensure_resource_guards') as ensure_guards, \
             patch('claws.provisioner._harden_default_service_account') as harden_sa:
            summary = provisioner.reconcile_claw_namespace_isolation()

        self.assertEqual(summary['checked'], 1)
        self.assertEqual(summary['drifted'], 1)
        self.assertEqual(summary['reconciled'], 1)
        self.assertEqual(summary['still_missing'], 0)
        self.assertEqual(audit.call_count, 2)
        resolve_tier.assert_called_once_with('free', core_v1)
        ensure_namespace.assert_called_once_with(core_v1, claw.namespace, claw)
        ensure_policies.assert_called_once()
        ensure_guards.assert_called_once()
        harden_sa.assert_called_once_with(core_v1, claw.namespace)

    def test_reconcile_claw_namespace_isolation_dry_run_does_not_mutate(self):
        Claw.objects.create(
            owner_id=116,
            owner_username='owner',
            owner_email='owner@example.com',
            name='Existing Claw',
            namespace='claw-116-existing',
            status=Claw.Status.RUNNING,
            tier_id='free',
        )
        core_v1 = MagicMock()
        with patch('claws.provisioner.get_k8s_client', return_value=(core_v1, MagicMock())), \
             patch(
                 'claws.provisioner.audit_claw_namespace_isolation',
                 return_value=['networkpolicy/dns-egress'],
             ), \
             patch('claws.provisioner._ensure_namespace') as ensure_namespace:
            summary = provisioner.reconcile_claw_namespace_isolation(dry_run=True)

        self.assertEqual(summary['checked'], 1)
        self.assertEqual(summary['drifted'], 1)
        self.assertEqual(summary['reconciled'], 0)
        self.assertEqual(summary['still_missing'], 1)
        ensure_namespace.assert_not_called()


class ClawHealthViewIsolationTests(SimpleTestCase):
    DRIFT = {
        'ok': False,
        'checked': 1,
        'drifted': 1,
        'missing': 1,
        'issues': [{'namespace': 'claw-116-existing', 'missing_after': ['networkpolicy/dns-egress']}],
    }

    def setUp(self):
        from django.core.cache import cache
        cache.delete(views.IsolationHealthView.CACHE_KEY)

    def test_kubelet_health_view_never_runs_the_isolation_audit(self):
        # HIVE-342: kubelet readiness/liveness point at /api/health/ with a 1s
        # timeout — it must stay O(1) and unaffected by tenant drift.
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        with patch('claws.views.provisioner.summarize_claw_namespace_isolation_health') as summarize:
            response = views.HealthView.as_view()(factory.get('/hive/api/v1/health'))

        summarize.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'ok')
        self.assertNotIn('claw_namespace_isolation', response.data)

    def test_isolation_health_view_reports_degraded_when_drift_exists(self):
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        with patch('claws.views.provisioner.summarize_claw_namespace_isolation_health',
                   return_value=dict(self.DRIFT)):
            response = views.IsolationHealthView.as_view()(
                factory.get('/hive/api/v1/health/isolation'))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['status'], 'degraded')
        self.assertFalse(response.data['claw_namespace_isolation']['ok'])

    def test_isolation_health_view_caches_the_audit(self):
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        with patch('claws.views.provisioner.summarize_claw_namespace_isolation_health',
                   return_value={'ok': True, 'checked': 1, 'drifted': 0, 'missing': 0, 'issues': []}) as summarize:
            first = views.IsolationHealthView.as_view()(factory.get('/hive/api/v1/health/isolation'))
            second = views.IsolationHealthView.as_view()(factory.get('/hive/api/v1/health/isolation'))

        self.assertEqual(summarize.call_count, 1)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)


class ClawThrottleTests(SimpleTestCase):
    def test_claw_list_view_throttle_applies_only_to_post(self):
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.get("/")
        list_view = views.ClawListView()
        list_view.request = request
        self.assertEqual(list_view.get_throttles(), [])

        list_view.request = factory.post("/", {"name": "test"})
        throttles = list_view.get_throttles()
        self.assertEqual(len(throttles), 1)
        self.assertEqual(type(throttles[0]).scope, 'claw-provision')

    def test_shell_provision_view_throttle_applies_only_to_post(self):
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.get("/")
        shell_view = views.HiveNewAssistantShellView()
        shell_view.request = request
        self.assertEqual(shell_view.get_throttles(), [])

        shell_view.request = factory.post("/", {"name": "from-shell"}, format="json")
        throttles = shell_view.get_throttles()
        self.assertEqual(len(throttles), 1)
        self.assertEqual(type(throttles[0]).scope, 'claw-provision')

    def test_shell_action_view_uses_start_throttle(self):
        shell_view = views.HiveAssistantActionView()
        self.assertEqual(
            shell_view.throttle_classes,
            [views.ClawStartRateThrottle],
        )

    @override_settings(
        REST_FRAMEWORK={
            'DEFAULT_AUTHENTICATION_CLASSES': ['claws.authentication.FederatedJWTAuthentication'],
            'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],
            'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
            'PAGE_SIZE': 50,
            'DEFAULT_THROTTLE_RATES': {
                'claw-provision': '1/min',
                'claw-start': '1/min',
                'channel-token': '6/min',
            },
        },
    )
    def test_claw_provision_view_rate_limits_repeated_posts(self):
        from django.core.cache import cache
        from rest_framework.test import APIRequestFactory, force_authenticate

        cache.clear()
        factory = APIRequestFactory()
        user = SimpleNamespace(id=123, pk=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        req1 = factory.post("/v1/claws", {"name": "A"}, format="json")
        req2 = factory.post("/v1/claws", {"name": "B"}, format="json")
        force_authenticate(req1, user=user)
        force_authenticate(req2, user=user)
        req1.user = user
        req2.user = user

        throttle = views.ClawProvisionRateThrottle()
        throttle.rate = "1/min"
        throttle.num_requests, throttle.duration = throttle.parse_rate(throttle.rate)
        view = views.ClawListView()

        self.assertTrue(throttle.allow_request(req1, view))
        self.assertFalse(throttle.allow_request(req2, view))

    def test_claw_start_view_uses_hardened_throttle(self):
        self.assertEqual(
            views.ClawStartView.throttle_classes,
            [views.ClawStartRateThrottle],
        )
        self.assertEqual(views.ClawStartRateThrottle.scope, 'claw-start')


class _FakeClaw:
    def __init__(self):
        self.id = uuid.uuid4()
        self.owner_id = 123
        self.owner_username = "owner"
        self.owner_email = "owner@example.com"
        self.namespace = f"claw-123-{self.id.hex}"
        self.tier_id = ""
        self.agent_type = "claw"
        self.model = ""
        self.legal_hold = False
        self.legal_hold_reason = ""
        self.legal_hold_case_id = ""
        self.legal_hold_set_by = ""
        self.legal_hold_set_at = None
        self.recycle_after = None
        self.recycle_after_frozen_at = None
        self.reclaim_now = False
        self.reclaim_previous_status = ""
        self.status = "running"
        self.status_message = ""
        self.name = "Held Assistant"
        self.delete_requested_at = None
        self.saved_fields = []

    def save(self, update_fields=None):
        self.saved_fields.append(tuple(update_fields or ()))


class ClawLifecycleFoundationTests(SimpleTestCase):
    def test_legal_hold_suppresses_reclaim_and_recycle_annotations(self):
        from datetime import timedelta
        from unittest.mock import patch
        from django.utils import timezone
        from claws import lifecycle

        claw = _FakeClaw()
        claw.recycle_after = timezone.now() + timedelta(hours=24)
        claw.reclaim_now = True

        with patch("claws.lifecycle.ClawEvent.objects.create"):
            lifecycle.set_legal_hold(claw, reason="case", case_id="HIVE-LH-1", actor="admin")

        self.assertTrue(claw.legal_hold)
        self.assertEqual(claw.legal_hold_case_id, "HIVE-LH-1")
        annotations = lifecycle.annotation_state(claw)
        self.assertEqual(annotations[lifecycle.LEGAL_HOLD_ANNOTATION], "true")
        self.assertIn("hive.shizuha.io/recycle-after-frozen-at", annotations)
        self.assertLess(claw.recycle_after_frozen_at, claw.recycle_after)

    def test_reclaim_now_is_blocked_by_legal_hold(self):
        from unittest.mock import patch
        from claws import lifecycle

        claw = _FakeClaw()
        claw.legal_hold = True

        with patch("claws.lifecycle.ClawEvent.objects.create"):
            _, accepted = lifecycle.mark_reclaim_now(claw, actor="owner")

        self.assertFalse(accepted)
        self.assertFalse(claw.reclaim_now)
        self.assertEqual(claw.status, "running")

    def test_reclaim_now_sets_backstop_state(self):
        from unittest.mock import patch
        from claws import lifecycle
        from claws.models import Claw

        claw = _FakeClaw()

        with patch("claws.lifecycle.ClawEvent.objects.create"):
            _, accepted = lifecycle.mark_reclaim_now(claw, actor="owner")

        self.assertTrue(accepted)
        self.assertTrue(claw.reclaim_now)
        self.assertEqual(claw.status, Claw.Status.DELETING)
        self.assertIsNotNone(claw.delete_requested_at)
        self.assertEqual(lifecycle.annotation_state(claw), {lifecycle.RECLAIM_NOW_ANNOTATION: "true"})

    def test_delayed_reclaim_schedules_24h_pool_entry(self):
        from unittest.mock import patch
        from django.utils import timezone
        from claws import lifecycle
        from claws.models import Claw

        now = timezone.now()
        claw = _FakeClaw()

        with patch("claws.lifecycle.ClawEvent.objects.create"):
            lifecycle.schedule_delayed_reclaim(claw, now=now)

        self.assertEqual(claw.status, Claw.Status.DELETING)
        self.assertFalse(claw.reclaim_now)
        self.assertEqual(claw.recycle_after, now + lifecycle.DELAYED_RECLAIM_RETENTION)
        self.assertEqual(lifecycle.annotation_state(claw)[lifecycle.RECYCLE_AFTER_ANNOTATION], claw.recycle_after.isoformat())

    def test_auth_state_denies_suspended_claw_with_5s_cache_contract(self):
        from claws import lifecycle
        from claws.models import Claw

        claw = _FakeClaw()
        claw.owner_id = 123
        claw.status = Claw.Status.SUSPENDED_OWNER_REMOVED

        state = lifecycle.auth_state(claw)

        self.assertFalse(state["authorized"])
        self.assertEqual(state["reason"], Claw.Status.SUSPENDED_OWNER_REMOVED)
        self.assertEqual(state["cache_ttl_seconds"], 5)

    def test_clear_legal_hold_slides_expired_recycle_deadline_by_frozen_remaining_time(self):
        from datetime import timedelta
        from unittest.mock import patch
        from django.utils import timezone
        from claws import lifecycle

        frozen_at = timezone.now()
        original_deadline = frozen_at + timedelta(hours=2)
        clear_at = frozen_at + timedelta(hours=5)
        claw = _FakeClaw()
        claw.recycle_after = original_deadline

        with patch("claws.lifecycle.ClawEvent.objects.create"), patch("claws.lifecycle.timezone.now", return_value=frozen_at):
            lifecycle.set_legal_hold(claw, reason="case")

        self.assertEqual(claw.recycle_after_frozen_at, frozen_at)

        with patch("claws.lifecycle.ClawEvent.objects.create"), patch("claws.lifecycle.timezone.now", return_value=clear_at):
            lifecycle.clear_legal_hold(claw, actor="admin")

        self.assertFalse(claw.legal_hold)
        self.assertIsNone(claw.recycle_after_frozen_at)
        self.assertEqual(claw.recycle_after, clear_at + timedelta(hours=2))

    def test_owner_rename_and_delete_reject_legal_hold(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import lifecycle, views

        claw = _FakeClaw()
        claw.legal_hold = True
        factory = APIRequestFactory()
        view = views.ClawDetailView.as_view()

        rename_request = factory.patch("/", {"name": "New"}, format="json")
        delete_request = factory.delete(f"/?confirm={claw.name}")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(rename_request, user=user)
        force_authenticate(delete_request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw):
            rename_response = view(rename_request, claw_id=claw.id)
            delete_response = view(delete_request, claw_id=claw.id)

        self.assertEqual(rename_response.status_code, 409)
        self.assertEqual(delete_response.status_code, 409)
        self.assertEqual(claw.name, "Held Assistant")

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-test-token")
    def test_internal_auth_state_requires_service_token_not_owner_jwt(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import lifecycle, views

        claw = _FakeClaw()
        claw.owner_id = 123
        factory = APIRequestFactory()
        view = views.InternalAuthStateView.as_view()
        owner = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)

        owner_request = factory.get("/")
        force_authenticate(owner_request, user=owner)
        owner_response = view(owner_request, claw_id=claw.id)
        self.assertEqual(owner_response.status_code, 403)

        service_request = factory.get("/", HTTP_X_HIVE_INTERNAL_TOKEN=lifecycle.internal_auth_token(claw))
        with patch("claws.views.Claw.objects.exclude") as exclude:
            exclude.return_value.get.return_value = claw
            service_response = view(service_request, claw_id=claw.id)

        self.assertEqual(service_response.status_code, 200)
        self.assertTrue(service_response.data["authorized"])
        self.assertEqual(service_response.data["cache_ttl_seconds"], 5)

    # ── HIVE-198 E1: claw proactive-event notify endpoint ──
    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-test-token",
                       SHIZUHA_PLATFORM_URL="http://platform")
    def test_notify_requires_internal_token_and_title_body(self):
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory
        from claws import views

        claw = _FakeClaw()
        factory = APIRequestFactory()
        view = views.ClawNotifyView.as_view()

        # No internal token → 403 (service auth required)
        missing = view(factory.post("/", {"title": "t", "body": "b"}, format="json"), claw_id=claw.id)
        self.assertEqual(missing.status_code, 403)

        # Valid token but missing title/body → 400
        from claws import lifecycle
        with patch("claws.views.Claw.objects.exclude") as exclude:
            exclude.return_value.get.return_value = claw
            req = factory.post("/", {"title": "hi"}, format="json",
                               HTTP_X_HIVE_INTERNAL_TOKEN=lifecycle.internal_auth_token(claw))
            bad = view(req, claw_id=claw.id)
        self.assertEqual(bad.status_code, 400)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-test-token",
                       SHIZUHA_PLATFORM_URL="http://platform")
    def test_notify_resolves_owner_and_injects_claw_channel(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory
        from django.core.cache import cache
        from claws import lifecycle, views

        cache.clear()
        claw = _FakeClaw()
        claw.owner_id = 777
        factory = APIRequestFactory()
        view = views.ClawNotifyView.as_view()

        captured = {}
        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return SimpleNamespace(status_code=200, text="ok")

        req = factory.post(
            "/",
            # caller TRIES to spoof user_id + type — both must be overridden server-side
            {"title": "Done", "body": "Your task finished", "data": {"user_id": 1, "type": "spoof", "conv": "c1"}},
            format="json",
            HTTP_X_HIVE_INTERNAL_TOKEN=lifecycle.internal_auth_token(claw),
        )
        with patch("claws.views.Claw.objects.exclude") as exclude, \
                patch("claws.views.requests.post", side_effect=fake_post):
            exclude.return_value.get.return_value = claw
            resp = view(req, claw_id=claw.id)

        self.assertEqual(resp.status_code, 202)
        # Owner resolved server-side from the claw, never the caller's body.
        self.assertEqual(captured["json"]["user_id"], 777)
        # type:claw_channel injected (not the caller's "spoof"); channel_id set.
        self.assertEqual(captured["json"]["data"]["type"], "claw_channel")
        self.assertEqual(captured["json"]["channel_id"], "claw_channel")
        self.assertEqual(captured["json"]["data"]["conv"], "c1")  # opaque caller data preserved
        self.assertTrue(captured["url"].endswith("/shizuha-id/api/internal/push/send/"))

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-test-token",
                       SHIZUHA_PLATFORM_URL="http://platform")
    def test_notify_rate_limited_after_hourly_cap(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory
        from django.core.cache import cache
        from claws import lifecycle, views

        cache.clear()
        claw = _FakeClaw()  # fresh uuid → isolated rate-limit keys
        factory = APIRequestFactory()
        view = views.ClawNotifyView.as_view()
        token = lifecycle.internal_auth_token(claw)

        def call():
            req = factory.post("/", {"title": "t", "body": "b"}, format="json",
                               HTTP_X_HIVE_INTERNAL_TOKEN=token)
            with patch("claws.views.Claw.objects.exclude") as exclude, \
                    patch("claws.views.requests.post",
                          return_value=SimpleNamespace(status_code=200, text="ok")):
                exclude.return_value.get.return_value = claw
                return view(req, claw_id=claw.id)

        # assistant_initiated cap = 5/hour; the 6th in the window is dropped (429).
        for _ in range(5):
            self.assertEqual(call().status_code, 202)
        self.assertEqual(call().status_code, 429)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-test-token",
                       SHIZUHA_PLATFORM_URL="http://platform")
    def test_notify_unknown_event_types_share_default_rate_cap(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory
        from django.core.cache import cache
        from claws import lifecycle, views

        cache.clear()
        claw = _FakeClaw()
        factory = APIRequestFactory()
        view = views.ClawNotifyView.as_view()
        token = lifecycle.internal_auth_token(claw)

        def call(event_type):
            req = factory.post(
                "/",
                {"title": "t", "body": "b", "event_type": event_type},
                format="json",
                HTTP_X_HIVE_INTERNAL_TOKEN=token,
            )
            with patch("claws.views.Claw.objects.exclude") as exclude, \
                    patch("claws.views.requests.post",
                          return_value=SimpleNamespace(status_code=200, text="ok")):
                exclude.return_value.get.return_value = claw
                return view(req, claw_id=claw.id)

        for i in range(5):
            resp = call(f"attacker_event_{i}")
            self.assertEqual(resp.status_code, 202)
            self.assertEqual(resp.data["event_type"], "assistant_initiated")
        self.assertEqual(call("attacker_event_5").status_code, 429)


    # ── HIVE-203 E3: native WhatsApp → Claw/Cortex reply endpoint ──
    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-test-token",
                       CORTEX_BASE_URL="http://cortex:8040",
                       HIVE_CORTEX_DEFAULT_MODEL="Qwen3.6-27B")
    def test_whatsapp_message_routes_to_cortex_and_returns_reply(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory
        from django.core.cache import cache
        from claws import lifecycle, views

        cache.clear()
        claw = _FakeClaw()
        claw.name = "Owner Claw"
        claw.model = "custom-model"
        factory = APIRequestFactory()
        view = views.InternalWhatsAppMessageView.as_view()
        token = lifecycle.internal_auth_token(claw)
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers, timeout=timeout)
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"choices": [{"message": {"content": "Hello from your Claw"}}]},
                text="ok",
            )

        req = factory.post(
            "/",
            {"phone_jid": "15551234567@s.whatsapp.net", "body": "hello"},
            format="json",
            HTTP_X_HIVE_INTERNAL_TOKEN=token,
        )
        # HIVE-410: HIVE-345 added synchronous DB-writing usage metering
        # (record_agent_turn / record_cortex_token_usage -> UsageEvent, wrapped
        # in transaction.atomic()) into the whatsapp reply path, guarded by a
        # fail-loud wrapper that degrades any metering error into
        # "upstream_unavailable". This is a SimpleTestCase (no DB) using a
        # non-persisted _FakeClaw, so the metering seam AND its transaction
        # wrapper must be mocked to stay hermetic — otherwise the atomic block
        # trips the SimpleTestCase DB-access guard. DB-backed metering behavior
        # is covered separately in test_usage_metering.py (a real TestCase).
        with patch("claws.views.Claw.objects.exclude") as exclude, \
                patch("claws.views.requests.post", side_effect=fake_post), \
                patch("claws.views.transaction.atomic"), \
                patch("claws.views.record_agent_turn"), \
                patch("claws.views.record_cortex_token_usage"):
            exclude.return_value.get.return_value = claw
            resp = view(req, claw_id=claw.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "replied")
        self.assertEqual(resp.data["reply"], "Hello from your Claw")
        self.assertEqual(captured["url"], "http://cortex:8040/v1/chat/completions")
        self.assertEqual(captured["json"]["model"], "custom-model")
        self.assertEqual(captured["json"]["messages"][-1], {"role": "user", "content": "hello"})
        self.assertIn("WhatsApp", captured["json"]["messages"][0]["content"])

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-test-token")
    def test_whatsapp_message_returns_200_for_unlinked_and_old_messages(self):
        from datetime import timedelta
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory
        from django.utils import timezone
        from claws import lifecycle, views

        claw = _FakeClaw()
        factory = APIRequestFactory()
        view = views.InternalWhatsAppMessageView.as_view()
        token = lifecycle.internal_auth_token(claw)

        def call(payload):
            req = factory.post("/", payload, format="json", HTTP_X_HIVE_INTERNAL_TOKEN=token)
            with patch("claws.views.Claw.objects.exclude") as exclude:
                exclude.return_value.get.return_value = claw
                return view(req, claw_id=claw.id)

        unlinked = call({"phone_jid": "15551234567@s.whatsapp.net", "body": "hi", "paired": False})
        self.assertEqual(unlinked.status_code, 200)
        self.assertEqual(unlinked.data["status"], "not_linked")
        self.assertIn("not linked", unlinked.data["reply"])

        old_ts = int((timezone.now() - timedelta(hours=25)).timestamp())
        old = call({"phone_jid": "15551234567@s.whatsapp.net", "body": "hi", "timestamp": old_ts})
        self.assertEqual(old.status_code, 200)
        self.assertEqual(old.data["status"], "template_required")
        self.assertTrue(old.data["template_required"])

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-test-token",
                       CORTEX_BASE_URL="http://cortex:8040",
                       HIVE_WHATSAPP_DAILY_MESSAGE_LIMIT=1)
    def test_whatsapp_message_rate_limit_returns_friendly_200_and_masks_identity(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory
        from django.core.cache import cache
        from claws import lifecycle, views

        cache.clear()
        claw = _FakeClaw()
        factory = APIRequestFactory()
        view = views.InternalWhatsAppMessageView.as_view()
        token = lifecycle.internal_auth_token(claw)
        phone = "15551234567@s.whatsapp.net"

        def req():
            return factory.post(
                "/", {"phone_jid": phone, "body": "hello"}, format="json",
                HTTP_X_HIVE_INTERNAL_TOKEN=token,
            )

        cortex_response = SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": "first reply"}}]},
            text="ok",
        )
        # HIVE-410: mock the HIVE-345 metering seam (functions + transaction
        # wrapper) so the hermetic (no-DB) reply path returns "replied" instead
        # of the fail-loud "upstream_unavailable" (see test_usage_metering.py).
        with patch("claws.views.Claw.objects.exclude") as exclude, \
                patch("claws.views.requests.post", return_value=cortex_response) as post, \
                patch("claws.views.logger.info") as info_log, \
                patch("claws.views.transaction.atomic"), \
                patch("claws.views.record_agent_turn"), \
                patch("claws.views.record_cortex_token_usage"):
            exclude.return_value.get.return_value = claw
            first = view(req(), claw_id=claw.id)
            second = view(req(), claw_id=claw.id)

        self.assertEqual(first.data["status"], "replied")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["status"], "rate_limited")
        self.assertIn("limit", second.data["reply"])
        post.assert_called_once()
        logged_args = info_log.call_args.args
        self.assertIn("***4567", str(logged_args))
        self.assertNotIn("15551234567", str(logged_args))

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-test-token", CORTEX_BASE_URL="http://cortex:8040")
    def test_whatsapp_message_upstream_failure_is_friendly_200(self):
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory
        from claws import lifecycle, views

        claw = _FakeClaw()
        factory = APIRequestFactory()
        view = views.InternalWhatsAppMessageView.as_view()
        req = factory.post(
            "/",
            {"phone_jid": "15551234567@s.whatsapp.net", "body": "hello"},
            format="json",
            HTTP_X_HIVE_INTERNAL_TOKEN=lifecycle.internal_auth_token(claw),
        )
        with patch("claws.views.Claw.objects.exclude") as exclude, \
                patch("claws.views.requests.post", side_effect=RuntimeError("down")):
            exclude.return_value.get.return_value = claw
            resp = view(req, claw_id=claw.id)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "upstream_unavailable")
        self.assertIn("temporarily unavailable", resp.data["reply"])
    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-test-token")
    def test_internal_hold_state_requires_service_token_and_omits_legal_metadata(self):
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory
        from claws import lifecycle, views

        claw = _FakeClaw()
        claw.legal_hold = True
        claw.legal_hold_case_id = "CASE-123"
        factory = APIRequestFactory()
        view = views.InternalHoldStateView.as_view()

        missing_response = view(factory.get("/"), claw_id=claw.id)
        with patch("claws.views.Claw.objects.exclude") as exclude:
            exclude.return_value.get.return_value = claw
            wrong_response = view(factory.get("/", HTTP_X_HIVE_INTERNAL_TOKEN="wrong"), claw_id=claw.id)

        service_request = factory.get("/", HTTP_X_HIVE_INTERNAL_TOKEN=lifecycle.internal_auth_token(claw))
        with patch("claws.views.Claw.objects.exclude") as exclude:
            exclude.return_value.get.return_value = claw
            service_response = view(service_request, claw_id=claw.id)

        self.assertEqual(missing_response.status_code, 403)
        self.assertEqual(wrong_response.status_code, 403)
        self.assertEqual(service_response.status_code, 200)
        self.assertTrue(service_response.data["legal_hold"])
        self.assertNotIn("legal_hold_reason", service_response.data)
        self.assertNotIn("legal_hold_case_id", service_response.data)
        self.assertNotIn("legal_hold_set_by", service_response.data)
        self.assertNotIn("legal_hold_set_at", service_response.data)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-root-secret")
    def test_internal_auth_state_rejects_token_from_another_claw(self):
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory
        from claws import lifecycle, views

        own_claw = _FakeClaw()
        other_claw = _FakeClaw()
        factory = APIRequestFactory()
        view = views.InternalAuthStateView.as_view()

        cross_claw_request = factory.get("/", HTTP_X_HIVE_INTERNAL_TOKEN=lifecycle.internal_auth_token(own_claw))
        with patch("claws.views.Claw.objects.exclude") as exclude:
            exclude.return_value.get.return_value = other_claw
            response = view(cross_claw_request, claw_id=other_claw.id)

        self.assertEqual(response.status_code, 403)

    @override_settings(HIVE_INTERNAL_AUTH_TOKEN="internal-root-secret")
    def test_internal_hold_state_rejects_token_from_another_claw(self):
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory
        from claws import lifecycle, views

        own_claw = _FakeClaw()
        other_claw = _FakeClaw()
        other_claw.legal_hold = True
        other_claw.legal_hold_case_id = "CASE-OTHER"
        factory = APIRequestFactory()
        view = views.InternalHoldStateView.as_view()

        cross_claw_request = factory.get("/", HTTP_X_HIVE_INTERNAL_TOKEN=lifecycle.internal_auth_token(own_claw))
        with patch("claws.views.Claw.objects.exclude") as exclude:
            exclude.return_value.get.return_value = other_claw
            response = view(cross_claw_request, claw_id=other_claw.id)

        self.assertEqual(response.status_code, 403)

    def test_me_view_uses_enabled_services_for_entitlements(self):
        from types import SimpleNamespace
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        factory = APIRequestFactory()
        request = factory.get("/")
        user = SimpleNamespace(
            id=123,
            username="owner",
            email="owner@example.com",
            enabled_services=["hive", "books"],
            is_staff=False,
            is_superuser=False,
            is_authenticated=True,
        )
        force_authenticate(request, user=user)

        response = views.MeView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["entitlements"], ["hive", "books"])
        self.assertEqual(response.data["service_ids"], ["home", "hive"])
        self.assertFalse(response.data["is_staff"])
        self.assertFalse(response.data["is_superuser"])

    def test_me_view_exposes_server_authoritative_admin_flags(self):
        from types import SimpleNamespace
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        request = APIRequestFactory().get("/")
        user = SimpleNamespace(
            id=1,
            username="admin",
            email="admin@example.com",
            enabled_services=None,
            is_staff=True,
            is_superuser=True,
            is_authenticated=True,
        )
        force_authenticate(request, user=user)

        response = views.MeView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["is_staff"])
        self.assertTrue(response.data["is_superuser"])

    @patch('fleet.views._active_jit_grant_for_scope')
    def test_me_view_exposes_fleet_read_capability_without_staff_bit(self, grant_lookup):
        from types import SimpleNamespace
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        request = APIRequestFactory().get("/", HTTP_AUTHORIZATION='Bearer test-token')
        user = SimpleNamespace(
            id=42,
            username='mika',
            email='mika@shizuha.com',
            enabled_services=['hive'],
            is_staff=False,
            is_superuser=False,
            is_authenticated=True,
        )
        force_authenticate(request, user=user)
        grant_lookup.return_value = ({'id': 325}, None, '')

        response = views.MeView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['is_staff'])
        self.assertTrue(response.data['fleet_read_access'])
        self.assertEqual(response.data['fleet_read_access_source'], 'jit')
        self.assertEqual(grant_lookup.call_args.args[1], 'hive.fleet.read')
        self.assertEqual(grant_lookup.call_args.args[0].user.id, 42)

    @patch('fleet.views._active_jit_grant_for_scope')
    def test_me_view_distinguishes_jit_dependency_failure_from_denial(self, grant_lookup):
        from types import SimpleNamespace
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        request = APIRequestFactory().get("/")
        force_authenticate(request, user=SimpleNamespace(
            id=42, username='mika', email='mika@shizuha.com',
            enabled_services=['hive'], is_staff=False, is_superuser=False,
            is_authenticated=True,
        ))
        grant_lookup.return_value = (None, 'dependency', 'Admin unavailable')

        response = views.MeView.as_view()(request)

        self.assertFalse(response.data['fleet_read_access'])
        self.assertEqual(response.data['fleet_read_access_source'], 'unavailable')

    def test_me_services_view_filters_to_current_user_enabled_services(self):
        from types import SimpleNamespace
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        factory = APIRequestFactory()
        request = factory.get("/", HTTP_X_ORGANIZATION_ID="7")
        user = SimpleNamespace(
            id=123,
            username="owner",
            email="owner@example.com",
            enabled_services=["hive", "pulse", "books"],
            is_staff=False,
            is_superuser=False,
            is_authenticated=True,
        )
        force_authenticate(request, user=user)

        response = views.MeServicesView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user_id"], 123)
        self.assertEqual(response.data["organization_id"], "7")
        self.assertEqual(response.data["service_ids"], ["home", "hive", "pulse"])
        self.assertEqual([service["id"] for service in response.data["services"]], ["home", "hive", "pulse"])

    def test_me_services_view_expands_staff_to_all_platform_apps(self):
        from types import SimpleNamespace
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        request = APIRequestFactory().get("/")
        user = SimpleNamespace(
            id=1,
            username="admin",
            email="admin@example.com",
            enabled_services=None,
            is_staff=True,
            is_superuser=False,
            is_authenticated=True,
        )
        force_authenticate(request, user=user)

        response = views.MeServicesView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["service_ids"], ["home", "hive", "pulse", "wiki", "connect", "notes", "admin"])

    def test_cancel_delete_honors_ordinary_delete_undo_window(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.DELETING
        claw.delete_requested_at = timezone.now() - timedelta(seconds=views.UNDO_DELETE_WINDOW.total_seconds() + 1)
        factory = APIRequestFactory()
        request = factory.post("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw):
            response = views.OwnerCancelDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 410)
        self.assertEqual(claw.status, Claw.Status.DELETING)
        self.assertIsNotNone(claw.delete_requested_at)

    def test_cancel_delete_preserves_ordinary_delete_window_after_reclaim_promotion(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        existing_recycle_after = timezone.now() + timedelta(hours=24)
        claw = _FakeClaw()
        claw.status = Claw.Status.DELETING
        claw.delete_requested_at = timezone.now() - timedelta(seconds=views.UNDO_DELETE_WINDOW.total_seconds() + 1)
        claw.recycle_after = existing_recycle_after
        factory = APIRequestFactory()
        request = factory.post("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw):
            response = views.OwnerCancelDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 410)
        self.assertEqual(claw.status, Claw.Status.DELETING)
        self.assertIsNotNone(claw.delete_requested_at)
        self.assertEqual(claw.recycle_after, existing_recycle_after)

    def test_cancel_delete_allows_ordinary_delete_inside_undo_window(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.DELETING
        claw.delete_requested_at = timezone.now() - timedelta(seconds=10)
        factory = APIRequestFactory()
        request = factory.post("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw), patch("claws.lifecycle.ClawEvent.objects.create"):
            response = views.OwnerCancelDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(claw.status, Claw.Status.STOPPED)
        self.assertIsNone(claw.delete_requested_at)

    def test_cancel_delete_rejects_overlong_session_id_before_restore(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.DELETING
        claw.delete_requested_at = timezone.now() - timedelta(seconds=10)
        claw._state = SimpleNamespace(adding=False)
        factory = APIRequestFactory()
        request = factory.post("/", HTTP_X_SESSION_ID="s" * 129)
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with (
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.lifecycle.restore_from_reclaim") as restore_from_reclaim,
            patch("claws.views.ClawCancelDelete.objects.create") as create_audit,
        ):
            response = views.OwnerCancelDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 400)
        restore_from_reclaim.assert_not_called()
        create_audit.assert_not_called()
        self.assertEqual(claw.status, Claw.Status.DELETING)
        self.assertIsNotNone(claw.delete_requested_at)

    def test_legacy_undo_delete_uses_reclaim_restore_for_hard_delete_status(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.DELETING
        claw.delete_requested_at = timezone.now() - timedelta(seconds=10)
        claw.reclaim_now = True
        claw.reclaim_previous_status = Claw.Status.SUSPENDED_NO_CREDITS
        claw.recycle_after = timezone.now() + timedelta(hours=24)
        claw.recycle_after_frozen_at = timezone.now()
        factory = APIRequestFactory()
        request = factory.post("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with (
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.provisioner.undo_delete_claw") as ordinary_undo,
            patch("claws.lifecycle.ClawEvent.objects.create"),
        ):
            response = views.ClawUndoDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        ordinary_undo.assert_not_called()
        self.assertEqual(claw.status, Claw.Status.SUSPENDED_NO_CREDITS)
        self.assertFalse(claw.reclaim_now)
        self.assertEqual(claw.reclaim_previous_status, "")
        self.assertIsNone(claw.delete_requested_at)
        self.assertIsNone(claw.recycle_after)
        self.assertIsNone(claw.recycle_after_frozen_at)

    def test_delete_rejects_already_deleting_claw_after_reclaim_promotion(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        existing_delete_requested_at = timezone.now() - timedelta(minutes=10)
        existing_recycle_after = timezone.now() + timedelta(hours=24)
        claw = _FakeClaw()
        claw.status = Claw.Status.DELETING
        claw.delete_requested_at = existing_delete_requested_at
        claw.recycle_after = existing_recycle_after
        factory = APIRequestFactory()
        request = factory.delete(f"/?confirm={claw.name}")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with (
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.provisioner.begin_delete_claw") as begin_delete,
        ):
            response = views.ClawDetailView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 409)
        begin_delete.assert_not_called()
        self.assertEqual(claw.delete_requested_at, existing_delete_requested_at)
        self.assertEqual(claw.recycle_after, existing_recycle_after)

    def test_hard_delete_rejects_promoted_ordinary_delete_reclaim_pool_claw(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        existing_delete_requested_at = timezone.now() - timedelta(minutes=10)
        existing_recycle_after = timezone.now() + timedelta(hours=24)
        claw = _FakeClaw()
        claw.status = Claw.Status.DELETING
        claw.delete_requested_at = existing_delete_requested_at
        claw.recycle_after = existing_recycle_after
        claw.reclaim_now = False
        factory = APIRequestFactory()
        request = factory.post("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with (
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.provisioner.stop_claw_for_reclaim") as stop_for_reclaim,
            patch("claws.views.lifecycle.mark_reclaim_now") as mark_reclaim_now,
        ):
            response = views.OwnerHardDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 409)
        stop_for_reclaim.assert_not_called()
        mark_reclaim_now.assert_not_called()
        self.assertFalse(claw.reclaim_now)
        self.assertEqual(claw.delete_requested_at, existing_delete_requested_at)
        self.assertEqual(claw.recycle_after, existing_recycle_after)

    def test_cancel_hard_delete_restores_post_scale_stopped_status(self):
        from unittest.mock import patch
        from claws import lifecycle
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.STOPPED

        with patch("claws.lifecycle.ClawEvent.objects.create"):
            _, accepted = lifecycle.mark_reclaim_now(claw, actor="owner")
            lifecycle.restore_from_reclaim(claw, actor="owner")

        self.assertTrue(accepted)
        self.assertFalse(claw.reclaim_now)
        self.assertEqual(claw.status, Claw.Status.STOPPED)
        self.assertEqual(claw.reclaim_previous_status, "")

    def test_restore_from_reclaim_clears_frozen_recycle_timestamp_for_reschedule(self):
        from datetime import timedelta
        from unittest.mock import patch
        from django.utils import timezone
        from claws import lifecycle

        old_frozen_at = timezone.now() - timedelta(days=2)
        old_deadline = old_frozen_at + timedelta(hours=1)
        new_frozen_at = timezone.now()
        new_deadline = new_frozen_at + timedelta(hours=3)
        clear_at = new_frozen_at + timedelta(hours=10)
        claw = _FakeClaw()
        claw.legal_hold = True
        claw.recycle_after = old_deadline
        claw.recycle_after_frozen_at = old_frozen_at

        with patch("claws.lifecycle.ClawEvent.objects.create"):
            lifecycle.restore_from_reclaim(claw, actor="owner")

        self.assertIsNone(claw.recycle_after)
        self.assertIsNone(claw.recycle_after_frozen_at)

        with patch("claws.lifecycle.ClawEvent.objects.create"), patch("claws.lifecycle.timezone.now", return_value=new_frozen_at):
            lifecycle.schedule_recycle(claw, new_deadline, reason="rescheduled while held")

        self.assertEqual(claw.recycle_after_frozen_at, new_frozen_at)

        with patch("claws.lifecycle.ClawEvent.objects.create"), patch("claws.lifecycle.timezone.now", return_value=clear_at):
            lifecycle.clear_legal_hold(claw, actor="admin")

        self.assertEqual(claw.recycle_after, clear_at + timedelta(hours=3))

    def test_k8s_manifest_binds_hive_api_service_account_to_control_plane_rbac(self):
        from pathlib import Path

        manifest = Path(__file__).resolve().parents[1].joinpath("k8s/shizuha-hive.yaml").read_text()

        self.assertIn("kind: ServiceAccount", manifest)
        self.assertIn("name: hive-api", manifest)
        self.assertIn("kind: ClusterRole", manifest)
        self.assertIn('resources: ["namespaces"]', manifest)
        self.assertIn('resources: ["deployments/scale"]', manifest)
        self.assertIn('resources: ["resourcequotas", "limitranges"]', manifest)
        self.assertIn("kind: ClusterRoleBinding", manifest)
        self.assertIn("serviceAccountName: hive-api", manifest)

    def test_owner_serializer_excludes_internal_legal_hold_metadata(self):
        from django.utils import timezone
        from claws.models import Claw
        from claws.serializers import ClawSerializer

        claw = Claw(
            owner_id=123,
            owner_username="owner",
            owner_email="owner@example.com",
            name="Held Assistant",
            legal_hold=True,
            legal_hold_reason="legal case details",
            legal_hold_case_id="CASE-123",
            legal_hold_set_by="operator",
            legal_hold_set_at=timezone.now(),
            recycle_after_frozen_at=timezone.now(),
            recycle_after=timezone.now(),
            reclaim_now=True,
            reclaim_previous_status="running",
        )

        data = ClawSerializer(claw).data

        self.assertTrue(data["legal_hold"])
        self.assertNotIn("legal_hold_reason", data)
        self.assertNotIn("legal_hold_case_id", data)
        self.assertNotIn("legal_hold_set_by", data)
        self.assertNotIn("legal_hold_set_at", data)
        self.assertNotIn("recycle_after_frozen_at", data)
        self.assertNotIn("reclaim_previous_status", data)

    def test_owner_hard_delete_legal_hold_response_excludes_internal_metadata(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        claw = _FakeClaw()
        claw.legal_hold = True
        claw.legal_hold_reason = "legal case details"
        claw.legal_hold_case_id = "CASE-123"
        claw.legal_hold_set_by = "operator"
        claw.legal_hold_set_at = timezone.now()
        factory = APIRequestFactory()
        request = factory.post("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw), patch("claws.lifecycle.ClawEvent.objects.create"):
            response = views.OwnerHardDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 409)
        hold_state = response.data["hold_state"]
        self.assertTrue(hold_state["legal_hold"])
        self.assertNotIn("legal_hold_reason", hold_state)
        self.assertNotIn("legal_hold_case_id", hold_state)
        self.assertNotIn("legal_hold_set_by", hold_state)
        self.assertNotIn("legal_hold_set_at", hold_state)


    @override_settings(HIVE_STEP_UP_SIGNING_KEY="step-secret")
    def test_owner_hard_delete_requires_recent_step_up(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        claw = _FakeClaw()
        factory = APIRequestFactory()
        request = factory.post("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw), patch("claws.views.provisioner.stop_claw_for_reclaim") as stop_for_reclaim:
            response = views.OwnerHardDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 403)
        stop_for_reclaim.assert_not_called()
        self.assertFalse(claw.reclaim_now)

        expired_at = int((timezone.now() - timedelta(seconds=61)).timestamp())
        expired_proof = views.make_step_up_proof(user.id, claw.id, "hard_delete", expired_at)
        expired_request = factory.post(
            "/",
            HTTP_X_HIVE_STEP_UP_AT=str(expired_at),
            HTTP_X_HIVE_STEP_UP_PROOF=expired_proof,
        )
        force_authenticate(expired_request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw), patch("claws.views.provisioner.stop_claw_for_reclaim") as stop_for_reclaim:
            expired_response = views.OwnerHardDeleteView.as_view()(expired_request, claw_id=claw.id)

        self.assertEqual(expired_response.status_code, 403)
        stop_for_reclaim.assert_not_called()
        self.assertFalse(claw.reclaim_now)

    @override_settings(DEBUG=True, HIVE_STEPUP_BACKEND="stub")
    def test_owner_hard_delete_accepts_step_up_token_in_request_body(self):
        from types import SimpleNamespace
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        claw = _FakeClaw()
        factory = APIRequestFactory()
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        request = factory.post("/", {"step_up_token": "step-up-token"}, format="json")
        force_authenticate(request, user=user)

        with (
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.provisioner.stop_claw_for_reclaim", return_value=True) as stop_for_reclaim,
            patch("claws.views.ClawEvent.objects.create"),
            patch("claws.views.lifecycle.ClawEvent.objects.create"),
        ):
            response = views.OwnerHardDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        stop_for_reclaim.assert_called_once_with(claw)

    @override_settings(HIVE_STEP_UP_SIGNING_KEY="step-secret")
    def test_owner_hard_delete_scales_down_before_queueing_reclaim(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.RUNNING
        factory = APIRequestFactory()
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        issued_at = int(timezone.now().timestamp())
        proof = views.make_step_up_proof(user.id, claw.id, "hard_delete", issued_at)
        request = factory.post(
            "/",
            HTTP_X_HIVE_STEP_UP_AT=str(issued_at),
            HTTP_X_HIVE_STEP_UP_PROOF=proof,
        )
        force_authenticate(request, user=user)

        with (
            override_settings(DEBUG=True, HIVE_STEPUP_BACKEND="stub"),
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.provisioner.stop_claw_for_reclaim", return_value=True) as stop_for_reclaim,
            patch("claws.lifecycle.ClawEvent.objects.create"),
        ):
            response = views.OwnerHardDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        stop_for_reclaim.assert_called_once_with(claw)
        self.assertTrue(claw.reclaim_now)
        self.assertEqual(claw.status, Claw.Status.DELETING)
        self.assertEqual(claw.reclaim_previous_status, Claw.Status.STOPPED)

    @override_settings(HIVE_STEP_UP_SIGNING_KEY="step-secret")
    def test_owner_hard_delete_does_not_queue_reclaim_when_scale_down_fails(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.RUNNING
        factory = APIRequestFactory()
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        issued_at = int(timezone.now().timestamp())
        proof = views.make_step_up_proof(user.id, claw.id, "hard_delete", issued_at)
        request = factory.post(
            "/",
            HTTP_X_HIVE_STEP_UP_AT=str(issued_at),
            HTTP_X_HIVE_STEP_UP_PROOF=proof,
        )
        force_authenticate(request, user=user)

        with (
            override_settings(DEBUG=True, HIVE_STEPUP_BACKEND="stub"),
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.provisioner.stop_claw_for_reclaim", return_value=False) as stop_for_reclaim,
            patch("claws.views.ClawHardDeleteFailure.objects.create") as create_failure,
        ):
            response = views.OwnerHardDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 502)
        stop_for_reclaim.assert_called_once_with(claw)
        create_failure.assert_called_once()
        failure_kwargs = create_failure.call_args.kwargs
        self.assertEqual(failure_kwargs["claw"], claw)
        self.assertEqual(failure_kwargs["owner_id"], claw.owner_id)
        self.assertEqual(failure_kwargs["requested_by"], "owner")
        self.assertEqual(failure_kwargs["failure_class"], "scale_down_failed")
        self.assertIsNotNone(failure_kwargs["requested_at"])
        self.assertFalse(claw.reclaim_now)
        self.assertEqual(claw.status, Claw.Status.RUNNING)

    def test_stop_claw_for_reclaim_scales_to_zero_without_persisting_stopped(self):
        from unittest.mock import patch
        from claws import provisioner
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.RUNNING

        with patch("claws.provisioner._scale", return_value=True) as scale, patch("claws.provisioner._log_event"):
            accepted = provisioner.stop_claw_for_reclaim(claw)

        self.assertTrue(accepted)
        scale.assert_called_once_with(claw, 0, ok_if_missing=True)
        self.assertEqual(claw.status, Claw.Status.RUNNING)
        self.assertNotIn(("status",), claw.saved_fields)

    def test_undo_delete_clears_hard_delete_reclaim_metadata(self):
        from django.utils import timezone
        from unittest.mock import patch
        from claws import provisioner
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.DELETING
        claw.delete_requested_at = timezone.now()
        claw.reclaim_now = True
        claw.reclaim_previous_status = Claw.Status.STOPPED
        claw.recycle_after = timezone.now()
        claw.recycle_after_frozen_at = timezone.now()

        with patch("claws.provisioner._log_event"):
            accepted = provisioner.undo_delete_claw(claw)

        self.assertTrue(accepted)
        self.assertEqual(claw.status, Claw.Status.STOPPED)
        self.assertIsNone(claw.delete_requested_at)
        self.assertFalse(claw.reclaim_now)
        self.assertEqual(claw.reclaim_previous_status, "")
        self.assertIsNone(claw.recycle_after)
        self.assertIsNone(claw.recycle_after_frozen_at)

    def test_owner_data_actions_disables_cancel_after_ordinary_undo_window(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.DELETING
        claw.delete_requested_at = timezone.now() - timedelta(seconds=views.UNDO_DELETE_WINDOW.total_seconds() + 1)
        claw.recycle_after = timezone.now() + timedelta(hours=24)
        claw.reclaim_now = False
        factory = APIRequestFactory()
        request = factory.get("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw):
            response = views.OwnerDataActionsView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        cancel_delete = response.data["actions"]["cancel_delete"]
        self.assertFalse(cancel_delete["enabled"])
        self.assertEqual(cancel_delete["disabled_reason"], "undo_window_passed")

    def test_owner_data_actions_disables_hard_delete_while_deletion_pending(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.utils import timezone
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.DELETING
        claw.delete_requested_at = timezone.now() - timedelta(seconds=views.UNDO_DELETE_WINDOW.total_seconds() + 1)
        claw.recycle_after = timezone.now() + timedelta(hours=24)
        claw.reclaim_now = False
        factory = APIRequestFactory()
        request = factory.get("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw):
            response = views.OwnerDataActionsView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        hard_delete = response.data["actions"]["hard_delete"]
        self.assertFalse(hard_delete["enabled"])
        self.assertEqual(hard_delete["disabled_reason"], "pending_deletion")

    def test_owner_data_actions_reports_export_enabled_with_stepup_metadata(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.test import override_settings
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        claw = _FakeClaw()
        claw.legal_hold = True
        claw.legal_hold_reason = "legal case details"
        claw.legal_hold_case_id = "CASE-123"
        claw.legal_hold_set_by = "operator"
        factory = APIRequestFactory()
        request = factory.get("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with override_settings(DEBUG=True, HIVE_STEPUP_BACKEND="stub"), patch("claws.views._get_owned_claw", return_value=claw):
            response = views.OwnerDataActionsView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        export = response.data["actions"]["export"]
        self.assertTrue(export["enabled"])
        self.assertTrue(export["requires_step_up"])
        self.assertEqual(export["step_up_ttl_seconds"], 300)
        self.assertEqual(export["disabled_reason"], "")
        hold_state = response.data["hold_state"]
        self.assertTrue(hold_state["legal_hold"])
        self.assertNotIn("legal_hold_reason", hold_state)
        self.assertNotIn("legal_hold_case_id", hold_state)
        self.assertNotIn("legal_hold_set_by", hold_state)

    def test_owner_data_actions_disables_export_when_stepup_backend_unavailable_in_production(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.test import override_settings
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        claw = _FakeClaw()
        factory = APIRequestFactory()
        request = factory.get("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with override_settings(DEBUG=False, HIVE_STEPUP_BACKEND="stub"), patch("claws.views._get_owned_claw", return_value=claw):
            response = views.OwnerDataActionsView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        export = response.data["actions"]["export"]
        self.assertFalse(export["enabled"])
        self.assertTrue(export["requires_step_up"])
        self.assertEqual(export["step_up_ttl_seconds"], 300)
        self.assertEqual(export["disabled_reason"], "step_up_unavailable")

    def test_owner_data_actions_reports_export_available_with_shizuha_id_backend_in_production(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.test import override_settings
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        claw = _FakeClaw()
        factory = APIRequestFactory()
        request = factory.get("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with (
            override_settings(DEBUG=False, HIVE_STEPUP_BACKEND="shizuha-id"),
            patch("claws.views._get_owned_claw", return_value=claw),
        ):
            response = views.OwnerDataActionsView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        export = response.data["actions"]["export"]
        self.assertTrue(export["enabled"])
        self.assertTrue(export["requires_step_up"])
        self.assertEqual(export["step_up_ttl_seconds"], 300)
        self.assertEqual(export["disabled_reason"], "")

    def test_cancel_delete_rejects_noop_without_audit_event(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw

        claw = _FakeClaw()
        claw.status = Claw.Status.RUNNING
        claw.delete_requested_at = None
        claw.recycle_after = None
        claw.reclaim_now = False
        claw._state = SimpleNamespace(adding=False)
        factory = APIRequestFactory()
        request = factory.post("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with (
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.lifecycle.restore_from_reclaim") as restore_from_reclaim,
            patch("claws.views.ClawCancelDelete.objects.create") as create_audit,
        ):
            response = views.OwnerCancelDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 409)
        restore_from_reclaim.assert_not_called()
        create_audit.assert_not_called()


class ClawLifecycleBackstopTests(TestCase):
    def test_expired_ordinary_delete_promotes_to_delayed_reclaim_before_candidate(self):
        from datetime import timedelta
        from django.utils import timezone
        from claws import lifecycle
        from claws.models import Claw

        now = timezone.now()
        claw = Claw.objects.create(
            owner_id=123,
            owner_username="owner",
            owner_email="owner@example.com",
            name="Expired Delete",
            status=Claw.Status.DELETING,
            delete_requested_at=now - lifecycle.ORDINARY_DELETE_UNDO_WINDOW - timedelta(seconds=1),
        )

        self.assertEqual(list(lifecycle.reclaim_candidates(now=now)), [])

        promoted = lifecycle.promote_expired_delete_windows(now=now)
        claw.refresh_from_db()

        self.assertEqual(promoted, 1)
        self.assertEqual(claw.status, Claw.Status.DELETING)
        self.assertFalse(claw.reclaim_now)
        self.assertEqual(claw.recycle_after, now + lifecycle.DELAYED_RECLAIM_RETENTION)
        self.assertEqual(list(lifecycle.reclaim_candidates(now=now)), [])

        due_at = claw.recycle_after + timedelta(seconds=1)
        candidates = list(lifecycle.reclaim_candidates(now=due_at))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].claw_id, str(claw.id))
        self.assertEqual(candidates[0].reason, "recycle-after")

    def test_unexpired_ordinary_delete_is_not_reclaim_candidate(self):
        from datetime import timedelta
        from django.utils import timezone
        from claws import lifecycle
        from claws.models import Claw

        now = timezone.now()
        Claw.objects.create(
            owner_id=123,
            owner_username="owner",
            owner_email="owner@example.com",
            name="Fresh Delete",
            status=Claw.Status.DELETING,
            delete_requested_at=now - lifecycle.ORDINARY_DELETE_UNDO_WINDOW + timedelta(seconds=1),
        )

        self.assertEqual(list(lifecycle.reclaim_candidates(now=now)), [])

    def test_idle_running_claws_are_stop_candidates_including_null_deadline(self):
        from datetime import timedelta
        from django.utils import timezone
        from claws import lifecycle
        from claws.models import Claw

        now = timezone.now()
        due = Claw.objects.create(
            owner_id=1, owner_username="a", owner_email="a@example.com",
            name="Due", status=Claw.Status.RUNNING,
            idle_recycle_after=now - timedelta(minutes=1),
            namespace="claw-due",
        )
        null_deadline = Claw.objects.create(
            owner_id=2, owner_username="b", owner_email="b@example.com",
            name="Legacy", status=Claw.Status.RUNNING,
            idle_recycle_after=None,
            namespace="claw-legacy",
        )
        future = Claw.objects.create(
            owner_id=3, owner_username="c", owner_email="c@example.com",
            name="Fresh", status=Claw.Status.RUNNING,
            idle_recycle_after=now + timedelta(minutes=20),
            namespace="claw-fresh",
        )
        held = Claw.objects.create(
            owner_id=4, owner_username="d", owner_email="d@example.com",
            name="Held", status=Claw.Status.RUNNING, legal_hold=True,
            idle_recycle_after=now - timedelta(minutes=1),
            namespace="claw-held",
        )
        ids = {c.id for c in lifecycle.idle_stop_candidates(now=now)}
        self.assertEqual(ids, {due.id, null_deadline.id})
        self.assertNotIn(future.id, ids)
        self.assertNotIn(held.id, ids)

    def test_finalize_stops_idle_running_claws_without_destroying(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone
        from unittest.mock import patch
        from claws.models import Claw

        now = timezone.now()
        claw = Claw.objects.create(
            owner_id=9, owner_username="idle", owner_email="idle@example.com",
            name="Idle", status=Claw.Status.RUNNING,
            idle_recycle_after=now - timedelta(minutes=5),
            namespace="claw-idle",
        )
        with patch("claws.management.commands.finalize_claw_deletions.stop_claw", return_value=True) as stop, \
             patch("claws.management.commands.finalize_claw_deletions.destroy_claw") as destroy:
            call_command("finalize_claw_deletions")
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[0].id, claw.id)
        self.assertTrue(stop.call_args.kwargs.get("ok_if_missing"))
        destroy.assert_not_called()

    def test_finalize_fails_loud_when_idle_stop_persistently_fails(self):
        """HIVE-2011/ADR-0080: a persistent idle scale-to-zero failure must
        raise CommandError so the reconcile CronJob goes red — the exact
        'scale-to-zero silently failed for months' failure mode."""
        from django.core.management import CommandError
        from django.utils import timezone
        from claws.models import Claw

        now = timezone.now()
        claw = Claw.objects.create(
            owner_id=10, owner_username="idle-fail", owner_email="idle-fail@example.com",
            name="IdleFail", status=Claw.Status.RUNNING,
            idle_recycle_after=now - timedelta(minutes=5),
            namespace="claw-idle-fail",
        )
        with patch("claws.management.commands.finalize_claw_deletions.stop_claw", return_value=False) as stop, \
             patch("claws.management.commands.finalize_claw_deletions.destroy_claw") as destroy:
            with self.assertRaises(CommandError) as ctx:
                call_command("finalize_claw_deletions")
        # Original attempt + one retry, both failed.
        self.assertGreaterEqual(stop.call_count, 2)
        self.assertIn("Idle scale-to-zero incomplete after retry", str(ctx.exception))
        self.assertIn(str(claw.namespace), str(ctx.exception))
        destroy.assert_not_called()
        # The stale-Running gauge must surface the stuck claw for the alert.
        from claws import idle_recycle_metrics
        prom = idle_recycle_metrics.to_prometheus()
        self.assertIn(f'hive_claw_idle_running{{claw_id="{claw.id}",namespace="{claw.namespace}"}} 1', prom)

    def test_finalize_recovers_transient_idle_stop_failure(self):
        """A transient scale failure (first False, retry True) must succeed
        without raising — one k8s blip must not fail the whole idempotent
        chain."""
        from django.utils import timezone
        from claws.models import Claw

        now = timezone.now()
        claw = Claw.objects.create(
            owner_id=11, owner_username="idle-transient", owner_email="idle-transient@example.com",
            name="IdleTransient", status=Claw.Status.RUNNING,
            idle_recycle_after=now - timedelta(minutes=5),
            namespace="claw-idle-transient",
        )
        results = iter([False, True])
        with patch(
            "claws.management.commands.finalize_claw_deletions.stop_claw",
            side_effect=lambda *a, **k: next(results),
        ) as stop, \
             patch("claws.management.commands.finalize_claw_deletions.destroy_claw") as destroy:
            call_command("finalize_claw_deletions")  # must NOT raise
        self.assertEqual(stop.call_count, 2)
        destroy.assert_not_called()

    def test_idle_recycle_metrics_prometheus_format(self):
        """HIVE-2011: to_prometheus() emits valid Prometheus text with the
        expected series, and the stale-Running gauge is emitted as 0 when the
        set is empty (so absent() catches a dead exporter)."""
        from claws import idle_recycle_metrics
        from django.core.cache import cache

        cache.clear()
        idle_recycle_metrics.record_stopped()
        idle_recycle_metrics.record_stop_failure()
        idle_recycle_metrics.set_idle_running_set([("claw-1", "ns-1", 1)])
        prom = idle_recycle_metrics.to_prometheus()
        self.assertIn('# TYPE hive_claw_idle_running gauge', prom)
        self.assertIn('hive_claw_idle_running{claw_id="claw-1",namespace="ns-1"} 1', prom)
        self.assertIn('# TYPE hive_claw_idle_stopped_total counter', prom)
        self.assertIn('hive_claw_idle_stopped_total 1', prom)
        self.assertIn('# TYPE hive_claw_idle_stop_failure_total counter', prom)
        self.assertIn('hive_claw_idle_stop_failure_total 1', prom)

        # Empty set → gauge emitted as 0 (probe absent()).
        cache.clear()
        idle_recycle_metrics.set_idle_running_set([])
        prom_empty = idle_recycle_metrics.to_prometheus()
        self.assertIn('hive_claw_idle_running{claw_id="",namespace=""} 0', prom_empty)
        self.assertIn('hive_claw_idle_stopped_total 0', prom_empty)


class ClawFreeProvisioningFlowTests(TestCase):
    def _user(self, user_id=123):
        return SimpleNamespace(
            id=user_id,
            pk=user_id,
            username=f"owner{user_id}",
            email=f"owner{user_id}@example.com",
            is_staff=False,
            is_authenticated=True,
        )

    def _free_tier(self):
        return provisioner.TierSpec(
            pvc_size="5Gi",
            cpu_request="100m",
            cpu_limit="1",
            mem_request="256Mi",
            mem_limit="2Gi",
            idle_ttl_minutes=30,
            daily_msg_limit=100,
            llm_service="hive-llm",
            llm_default_model="GLM-5.2",
            max_claws_per_owner=1,
        )

    @override_settings(HIVE_TIERS=None, SHIZUHA_PLATFORM_URL="https://s1.tail.shizuha.com")
    def test_post_free_tier_is_retired_and_does_not_provision(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        request = APIRequestFactory().post("/hive/api/v1/claws/", {"name": "Free", "tier_id": "free"}, format="json")
        force_authenticate(request, user=self._user())

        with patch("claws.views.provisioner.resolve_tier", return_value=self._free_tier()) as resolve, \
             patch("claws.views.provisioner.provision_claw", return_value=True) as provision:
            response = views.ClawListView.as_view()(request)

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.data["error"], "Claw provisioning is retired")
        resolve.assert_not_called()
        provision.assert_not_called()
        self.assertFalse(Claw.objects.filter(owner_id=123).exists())

    @override_settings(
        SHIZUHA_PLATFORM_URL="https://s1.tail.shizuha.com",
        HIVE_CLAW_IMAGE="gx10-1:30500/openclaw:2026.6.11-multiarch",
        HIVE_HERMES_IMAGE="gx10-1:30500/openclaw:2026.6.11-multiarch",
        HIVE_HERMES_OPENCLAW_COMPAT=False,
    )
    def test_post_hermes_without_dedicated_runtime_or_compat_flag_503s_without_row(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        request = APIRequestFactory().post(
            "/hive/api/v1/claws/",
            {"name": "Hermes", "tier_id": "free", "agent_type": "hermes"},
            format="json",
        )
        force_authenticate(request, user=self._user())

        response = views.ClawListView.as_view()(request)

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.data["error"], "Claw provisioning is retired")
        self.assertFalse(Claw.objects.filter(owner_id=123, agent_type=Claw.AgentType.HERMES).exists())

    def test_connect_payload_reports_openclaw_compat_backing_image(self):
        from claws.serializers import ClawSerializer, connect_agent_payload

        claw = Claw.objects.create(
            owner_id=123,
            owner_username="owner123",
            owner_email="owner123@example.com",
            name="Hermes",
            tier_id="free",
            agent_type=Claw.AgentType.HERMES,
            runtime_image="gx10-1:30500/openclaw:2026.6.11-multiarch",
            runtime_compat_mode=True,
        )

        payload = connect_agent_payload(claw)
        data = ClawSerializer(claw).data

        self.assertEqual(payload["runtime_kind"], "openclaw-compat")
        self.assertEqual(payload["backing_image"], "gx10-1:30500/openclaw:2026.6.11-multiarch")
        self.assertTrue(payload["runtime_compat_mode"])
        self.assertEqual(data["runtime_image"], "gx10-1:30500/openclaw:2026.6.11-multiarch")
        self.assertTrue(data["runtime_compat_mode"])

    def test_claw_serializer_exposes_ingress_url_for_openclaw_only(self):
        # HIVE-696: HLD §5.2 — ClawSerializer carries the claw's reachable
        # ingress path for OpenClaw claws; non-OpenClaw agent types get None.
        from claws.serializers import ClawSerializer

        claw = Claw.objects.create(
            owner_id=123,
            owner_username="owner123",
            owner_email="owner123@example.com",
            name="Ingress",
            tier_id="free",
            agent_type=Claw.AgentType.CLAW,
        )
        data = ClawSerializer(claw).data
        self.assertEqual(
            data["ingress_url"],
            f"/hive/openclaw/{claw.owner_id}/{claw.id}/",
        )

        scli = Claw.objects.create(
            owner_id=123,
            owner_username="owner123",
            owner_email="owner123@example.com",
            name="Ingress SCLI",
            tier_id="free",
            agent_type=Claw.AgentType.SCLI,
        )
        self.assertIsNone(ClawSerializer(scli).data["ingress_url"])

    @override_settings(
        HIVE_CLAW_IMAGE="gx10-1:30500/openclaw:2026.6.11-multiarch",
        HIVE_HERMES_IMAGE="gx10-1:30500/openclaw:2026.6.11-multiarch",
        HIVE_HERMES_OPENCLAW_COMPAT=True,
    )
    def test_provision_hermes_compat_persists_runtime_identity_and_event(self):
        claw = Claw.objects.create(
            owner_id=124,
            owner_username="owner124",
            owner_email="owner124@example.com",
            name="Hermes",
            tier_id="free",
            agent_type=Claw.AgentType.HERMES,
        )

        with patch("claws.provisioner.get_k8s_client", return_value=(MagicMock(), MagicMock())), \
             patch("claws.provisioner.resolve_tier", return_value=self._free_tier()), \
             patch("claws.provisioner.mint_owner_cortex_key", return_value="sk-owner"), \
             patch(
                 "claws.provisioner.probe_cortex_model_access",
                 return_value=provisioner.CortexModelAccess("available"),
             ), \
             patch("claws.provisioner._ensure_namespace"), \
             patch("claws.provisioner._ensure_network_policies"), \
             patch("claws.provisioner._ensure_resource_guards"), \
             patch("claws.provisioner._create_claw_secret"), \
             patch("claws.provisioner._create_claw_pvc"), \
             patch("claws.provisioner._create_claw_deployment"), \
             patch("claws.provisioner._create_claw_service"):
            self.assertTrue(provisioner.provision_claw(claw))

        claw.refresh_from_db()
        self.assertEqual(claw.runtime_image, "gx10-1:30500/openclaw:2026.6.11-multiarch")
        self.assertTrue(claw.runtime_compat_mode)
        self.assertTrue(claw.events.filter(event_type="runtime_compat").exists())

    def test_provision_fails_before_kubernetes_mutation_when_model_is_denied(self):
        claw = Claw.objects.create(
            owner_id=125,
            owner_username="owner125",
            owner_email="owner125@example.com",
            name="Denied",
            tier_id="free",
        )

        with patch(
            "claws.provisioner.get_k8s_client",
            return_value=(MagicMock(), MagicMock()),
        ), patch(
            "claws.provisioner.resolve_tier",
            return_value=self._free_tier(),
        ), patch(
            "claws.provisioner.mint_owner_cortex_key",
            return_value="sk-resold",
        ), patch(
            "claws.provisioner.probe_cortex_model_access",
            return_value=provisioner.CortexModelAccess(
                "unavailable", "catalog authorization HTTP 403",
            ),
        ), patch("claws.provisioner._ensure_namespace") as ensure_namespace:
            self.assertFalse(provisioner.provision_claw(claw))

        claw.refresh_from_db()
        self.assertEqual(claw.status, Claw.Status.FAILED)
        self.assertIn("unavailable for this claw credential", claw.status_message)
        ensure_namespace.assert_not_called()

    @override_settings(SHIZUHA_PLATFORM_URL="https://s1.tail.shizuha.com")
    def test_mine_does_not_auto_provision_for_new_owner(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        request = APIRequestFactory().get("/hive/api/v1/claws/mine")
        force_authenticate(request, user=self._user(456))

        with patch("claws.views.provisioner.resolve_tier", return_value=self._free_tier()), \
             patch("claws.views.provisioner.provision_claw", return_value=True) as provision:
            response = views.ClawMineView.as_view()(request)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Claw.objects.filter(owner_id=456).exists())
        provision.assert_not_called()

    def test_mine_does_not_auto_start_existing_stopped_claw(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        claw = Claw.objects.create(
            owner_id=789,
            owner_username="owner789",
            owner_email="owner789@example.com",
            name="Stopped",
            tier_id="free",
            status=Claw.Status.STOPPED,
        )
        request = APIRequestFactory().get("/hive/api/v1/claws/mine")
        force_authenticate(request, user=self._user(789))

        with patch("claws.views.provisioner.start_claw") as start:
            response = views.ClawMineView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        start.assert_not_called()
        self.assertEqual(Claw.objects.filter(owner_id=789, tier_id="free").count(), 1)
        claw.refresh_from_db()
        self.assertEqual(claw.status, Claw.Status.STOPPED)
        self.assertEqual(response.data["id"], str(claw.id))

    @override_settings(
        SHIZUHA_PLATFORM_URL="http://shizuha-nginx.shizuha.svc.cluster.local",
        HIVE_PUBLIC_PLATFORM_URL="https://shizuha.com",
    )
    def test_channel_token_returns_public_https_platform_url(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        claw = Claw.objects.create(
            owner_id=654,
            owner_username="owner654",
            owner_email="owner654@example.com",
            name="Running",
            tier_id="free",
            status=Claw.Status.RUNNING,
            platform_url="http://shizuha-nginx.shizuha.svc.cluster.local",
        )
        request = APIRequestFactory().post("/hive/api/v1/claws/mine/channel-token")
        force_authenticate(request, user=self._user(654))

        response = views.ClawChannelTokenView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["platform_url"], "https://shizuha.com")
        self.assertNotIn("svc.cluster.local", response.data["platform_url"])
        self.assertNotEqual(response.data["platform_url"], claw.platform_url)

    @override_settings(
        SHIZUHA_PLATFORM_URL="http://shizuha-nginx.shizuha.svc.cluster.local",
        HIVE_PUBLIC_PLATFORM_URL="",
    )
    def test_channel_token_falls_back_to_https_request_origin(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        Claw.objects.create(
            owner_id=655,
            owner_username="owner655",
            owner_email="owner655@example.com",
            name="Running",
            tier_id="free",
            status=Claw.Status.RUNNING,
            platform_url="http://shizuha-nginx.shizuha.svc.cluster.local",
        )
        request = APIRequestFactory().post(
            "/hive/api/v1/claws/mine/channel-token",
            secure=True,
            HTTP_HOST="mobile.example.test",
        )
        force_authenticate(request, user=self._user(655))

        response = views.ClawChannelTokenView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["platform_url"], "https://mobile.example.test")

    @override_settings(SHIZUHA_PLATFORM_URL="https://s1.tail.shizuha.com")
    def test_second_active_free_claw_request_is_also_retired(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        user = self._user(321)
        factory = APIRequestFactory()
        with patch("claws.views.provisioner.resolve_tier", return_value=self._free_tier()), \
             patch("claws.views.provisioner.provision_claw", return_value=True):
            first = factory.post("/hive/api/v1/claws/", {"name": "First", "tier_id": "free"}, format="json")
            force_authenticate(first, user=user)
            self.assertEqual(views.ClawListView.as_view()(first).status_code, 410)

            second = factory.post("/hive/api/v1/claws/", {"name": "Second", "tier_id": "free"}, format="json")
            force_authenticate(second, user=user)
            response = views.ClawListView.as_view()(second)

        self.assertEqual(response.status_code, 410)
        self.assertEqual(Claw.objects.filter(owner_id=321, tier_id="free").count(), 0)


class ClawControlPlaneRouteTests(SimpleTestCase):
    def test_health_and_lifecycle_routes_resolve_under_hive_prefix(self):
        from django.urls import resolve

        claw_id = uuid.uuid4()
        routes = [
            "/hive/api/v1/me",
            "/hive/api/v1/me/services",
            "/hive/api/v1/claws/mine",
            "/api/v1/claws/mine",
            f"/hive/api/v1/claws/{claw_id}/dashboard-url",
            f"/hive/api/v1/owner/claws/{claw_id}/data-actions",
            f"/hive/api/v1/internal/claws/{claw_id}/auth-state",
            f"/hive/api/v1/owner/claws/{claw_id}/export",
            f"/hive/api/v1/owner/claws/{claw_id}/hard-delete",
            f"/hive/api/v1/owner/claws/{claw_id}/cancel-delete",
            f"/hive/api/v1/admin/claws/{claw_id}/hold/set",
            f"/hive/api/v1/admin/claws/{claw_id}/hold/clear",
            f"/hive/api/v1/admin/claws/{claw_id}/reclaim-restore",
            f"/hive/api/v1/internal/claws/{claw_id}/hold-state",
            f"/hive/assistants/{claw_id}",
            f"/hive/assistants/{claw_id}/data",
            "/hive/claw",
        ]
        for route in routes:
            with self.subTest(route=route):
                self.assertIsNotNone(resolve(route).func)

    def test_hive_shell_entry_redirects_anonymous_users_to_login_cta(self):
        from rest_framework.test import APIRequestFactory
        from claws import views

        request = APIRequestFactory().get("/hive/assistants")
        response = views.HiveAssistantsListShellView.as_view()(request)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/hive/login?next=/hive/assistants"))

    @override_settings(SHIZUHA_PLATFORM_URL="http://shizuha-nginx.shizuha.svc.cluster.local")
    def test_hive_login_uses_same_origin_shizuha_id_route(self):
        from rest_framework.test import APIRequestFactory
        from claws import views

        claw_id = uuid.uuid4()
        request = APIRequestFactory().get(f"/hive/login?next=/hive/assistants/{claw_id}/dash")
        response = views.HiveLoginView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(f'href="/id/accounts/login/?next=/hive/assistants/{claw_id}/dash"', html)
        self.assertNotIn("svc.cluster.local", html)
        self.assertNotIn("shizuha-nginx.shizuha", html)

    def test_claw_launch_shell_renders_async_mobile_wrapper(self):
        from types import SimpleNamespace
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        user = SimpleNamespace(
            id=123,
            pk=123,
            username="owner",
            email="owner@example.com",
            is_authenticated=True,
            entitlements=["hive:provision"],
        )
        request = APIRequestFactory().get("/hive/claw")
        force_authenticate(request, user=user)
        response = views.HiveClawLaunchShellView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Getting your Claw ready", html)
        self.assertIn("Ask your Claw anything", html)
        self.assertIn("/hive/api/v1/claws/mine", html)
        self.assertIn("/hive/api/v1/claws/mine/channel-token", html)
        self.assertIn("@media (max-width: 480px)", html)
        self.assertIn("Retry", html)
        self.assertIn("node.textContent = text", html)
        self.assertIn("textContent = text", html)
        self.assertNotIn("statusEl.innerHTML", html)
        self.assertIn("${claw.name", html)  # API-controlled name is routed through textContent.
        self.assertIn("function validateLaunchUrl", html)
        self.assertIn("unsupported launch URL", html)
        self.assertIn("unexpected launch host", html)

    @override_settings(HIVE_TIERS=[
        {'id': 'small', 'name': 'Small', 'cpu': '500m', 'memory': '1Gi', 'pvc': '5Gi', 'price': 100},
        {'id': 'med', 'name': 'Medium', 'cpu': '1000m', 'memory': '2Gi', 'pvc': '20Gi', 'price': 400},
    ])
    def test_browser_provisioning_uses_selected_tier_pvc(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.core.cache import cache
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        cache.clear()
        claw_id = uuid.uuid4()
        claw = SimpleNamespace(id=claw_id)
        user = SimpleNamespace(
            id=123,
            pk=123,
            username="owner",
            email="owner@example.com",
            is_staff=False,
            is_authenticated=True,
            entitlements=["hive:provision"],
        )
        request = APIRequestFactory().post(
            "/hive/assistants/new",
            {"name": "Tiered Assistant", "tier_id": "med"},
        )
        force_authenticate(request, user=user)

        with patch("claws.views.Claw.objects.create", return_value=claw) as create, \
             patch("claws.views.provisioner.provision_claw", return_value=True):
            response = views.HiveNewAssistantShellView.as_view()(request)

        self.assertEqual(response.status_code, 410)
        create.assert_not_called()
        self.assertEqual(response.data["error"], "Claw provisioning is retired")


# HIVE-577: this suite instantiates real ``ClawExportJob`` models and drives the
# owner/admin export views, whose DRF/response path touches the DB connection
# under Postgres (a no-op under sqlite). ``SimpleTestCase`` forbids any DB access
# → ``DatabaseOperationForbidden`` reds the hive deploy.yml test gate in CI.
# ``TestCase`` provides a per-test transactional DB, so the guard never fires; the
# tests themselves still rely on mocked querysets (``_FakeExportJobs``) and don't
# depend on persisted rows.
class ClawExportOffboardingFoundationTests(TestCase):
    class _ExplodingExportJobs:
        def all(self):
            return self

        def filter(self, **kwargs):
            raise AssertionError(f"malformed job IDs must be rejected before ORM filtering: {kwargs!r}")

    class _FakeExportJobs:
        def __init__(self, jobs):
            self.jobs = list(jobs)

        def all(self):
            return self

        def filter(self, **kwargs):
            jobs = self.jobs
            for key, value in kwargs.items():
                jobs = [job for job in jobs if getattr(job, key) == value]
            return type(self)(jobs)

        def first(self):
            return self.jobs[0] if self.jobs else None

        def __getitem__(self, item):
            return self.jobs[item]

    def _owner_request(self, body=None):
        from types import SimpleNamespace
        from rest_framework.test import APIRequestFactory, force_authenticate

        factory = APIRequestFactory()
        request = factory.post("/", body or {}, format="json")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)
        return request, user

    def test_owner_export_status_rejects_malformed_job_id_before_uuid_lookup(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        claw = SimpleNamespace(
            id=uuid.uuid4(),
            owner_id=123,
            export_jobs=self._ExplodingExportJobs(),
        )
        factory = APIRequestFactory()
        request = factory.get("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw):
            response = views.OwnerClawExportView.as_view()(request, claw_id=claw.id, job_id="not-a-uuid")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"], "Export job not found")

    def test_owner_export_status_list_hides_operator_jobs(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import ClawExportJob

        claw_id = uuid.uuid4()
        owner_job = ClawExportJob(
            id=uuid.uuid4(),
            claw_id=claw_id,
            owner_id=123,
            requested_by=ClawExportJob.RequestedBy.OWNER,
            requested_by_actor="owner",
            snapshot_name="hive-export-owner",
            share_url="https://example.test/owner",
        )
        operator_job = ClawExportJob(
            id=uuid.uuid4(),
            claw_id=claw_id,
            owner_id=123,
            requested_by=ClawExportJob.RequestedBy.OPERATOR,
            requested_by_actor="admin",
            snapshot_name="hive-export-operator",
            share_url="https://example.test/operator-secret",
        )
        claw = SimpleNamespace(
            id=claw_id,
            owner_id=123,
            export_jobs=self._FakeExportJobs([owner_job, operator_job]),
        )
        factory = APIRequestFactory()
        request = factory.get("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw):
            response = views.OwnerClawExportView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["job_id"] for item in response.data], [str(owner_job.id)])
        self.assertNotIn("https://example.test/operator-secret", repr(response.data))
        self.assertNotIn("admin", repr(response.data))

    def test_owner_export_status_lookup_hides_operator_jobs(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import ClawExportJob

        claw_id = uuid.uuid4()
        operator_job = ClawExportJob(
            id=uuid.uuid4(),
            claw_id=claw_id,
            owner_id=123,
            requested_by=ClawExportJob.RequestedBy.OPERATOR,
            requested_by_actor="admin",
            snapshot_name="hive-export-operator",
            share_url="https://example.test/operator-secret",
        )
        claw = SimpleNamespace(
            id=claw_id,
            owner_id=123,
            export_jobs=self._FakeExportJobs([operator_job]),
        )
        factory = APIRequestFactory()
        request = factory.get("/")
        user = SimpleNamespace(id=123, username="owner", email="owner@example.com", is_staff=False, is_authenticated=True)
        force_authenticate(request, user=user)

        with patch("claws.views._get_owned_claw", return_value=claw):
            response = views.OwnerClawExportView.as_view()(request, claw_id=claw.id, job_id=str(operator_job.id))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"], "Export job not found")
        self.assertNotIn("https://example.test/operator-secret", repr(response.data))

    def test_admin_export_status_rejects_malformed_job_id_before_uuid_lookup(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        claw = SimpleNamespace(
            id=uuid.uuid4(),
            owner_id=123,
            export_jobs=self._ExplodingExportJobs(),
        )
        factory = APIRequestFactory()
        request = factory.get("/")
        user = SimpleNamespace(id=999, username="admin", email="admin@example.com", is_staff=True, is_authenticated=True)
        force_authenticate(request, user=user)

        with patch("claws.views._get_admin_claw", return_value=claw):
            response = views.AdminClawExportView.as_view()(request, claw_id=claw.id, job_id="../not-a-uuid")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"], "Export job not found")

    def test_stub_stepup_accepts_non_empty_token_only_in_debug(self):
        from django.test import override_settings
        from claws.stepup import StepUpVerificationError, verify_step_up_token

        with override_settings(DEBUG=True, HIVE_STEPUP_BACKEND="stub"):
            self.assertTrue(verify_step_up_token("token", owner_id=123, claw_id="c", action="export"))
            self.assertFalse(verify_step_up_token("", owner_id=123, claw_id="c", action="export"))

        with override_settings(DEBUG=False, HIVE_STEPUP_BACKEND="stub"):
            with self.assertRaises(StepUpVerificationError):
                verify_step_up_token("token", owner_id=123, claw_id="c", action="export")

    def test_shizuha_id_stepup_calls_internal_verify_contract(self):
        from unittest.mock import MagicMock, patch
        from django.test import override_settings
        from claws.stepup import verify_step_up_token

        resp = MagicMock(status_code=200)
        resp.json.return_value = {'valid': True}
        with (
            override_settings(
                HIVE_STEPUP_BACKEND='shizuha-id',
                SHIZUHA_ID_INTERNAL_URL='http://id.internal',
                SHIZUHA_ID_STEPUP_VERIFY_TIMEOUT_SECONDS=2,
            ),
            patch('claws.stepup.requests.post', return_value=resp) as post,
        ):
            self.assertTrue(verify_step_up_token('step-token', owner_id=123, claw_id='c1', action='export'))

        post.assert_called_once_with(
            'http://id.internal/api/internal/step-up/verify/',
            json={
                'token': 'step-token',
                'expected_scope': {'owner_id': 123, 'resource_id': 'c1', 'action': 'export'},
            },
            timeout=2,
        )

    def test_shizuha_id_stepup_fails_closed_on_invalid_token(self):
        from unittest.mock import MagicMock, patch
        from django.test import override_settings
        from claws.stepup import verify_step_up_token

        resp = MagicMock(status_code=200)
        resp.json.return_value = {'valid': False, 'reason': 'scope_mismatch'}
        with (
            override_settings(HIVE_STEPUP_BACKEND='shizuha-id', SHIZUHA_ID_INTERNAL_URL='http://id.internal'),
            patch('claws.stepup.requests.post', return_value=resp),
        ):
            self.assertFalse(verify_step_up_token('bad-token', owner_id=123, claw_id='c1', action='export'))

    def test_owner_export_completes_manifest_job_with_stepup_and_allows_legal_hold(self):
        from unittest.mock import patch
        from django.test import override_settings
        from claws import views
        from claws.models import Claw, ClawExportJob

        claw = Claw(
            owner_id=123,
            owner_username="owner",
            owner_email="owner@example.com",
            name="Export Me",
            legal_hold=True,  # HIVE-4 allows read-only export during hold.
        )
        request, _ = self._owner_request({
            "include_session": True,
            "include_credits": False,
            "step_up_token": "fresh-step-up",
        })
        job = ClawExportJob(
            claw=claw,
            owner_id=claw.owner_id,
            requested_by=ClawExportJob.RequestedBy.OWNER,
            requested_by_actor="owner",
            include_session=True,
            include_credits=False,
            snapshot_name="hive-export-test",
        )

        from django.utils import timezone

        def fake_complete(created_job, request=None):
            created_job.status = ClawExportJob.Status.DONE
            created_job.share_url = request.build_absolute_uri(
                f"/hive/api/v1/owner/claws/{created_job.claw_id}/export/{created_job.id}"
            )
            created_job.byte_count = 123
            created_job.sha256 = "a" * 64
            created_job.completed_at = timezone.now()
            return created_job

        with (
            override_settings(DEBUG=True, HIVE_STEPUP_BACKEND="stub"),
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.ClawExportJob.objects.create", return_value=job) as create_job,
            patch("claws.views.complete_export_job", side_effect=fake_complete) as complete_job,
        ):
            response = views.OwnerClawExportView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 202)
        create_job.assert_called_once()
        complete_job.assert_called_once()
        self.assertEqual(create_job.call_args.kwargs["requested_by"], ClawExportJob.RequestedBy.OWNER)
        self.assertTrue(create_job.call_args.kwargs["include_session"])
        self.assertFalse(create_job.call_args.kwargs["include_credits"])
        self.assertEqual(response.data["status"], ClawExportJob.Status.DONE)
        self.assertTrue(response.data["share_url"].endswith(f"/hive/api/v1/owner/claws/{claw.id}/export/{job.id}"))
        self.assertGreater(response.data["byte_count"], 0)
        self.assertRegex(response.data["sha256"], r"^[0-9a-f]{64}$")
        self.assertIsNotNone(response.data["completed_at"])

    def test_complete_export_job_writes_terminal_manifest_metadata(self):
        from unittest.mock import patch
        from claws import views
        from claws.models import Claw, ClawEvent, ClawExportJob

        claw = Claw(
            owner_id=123,
            owner_username="owner",
            owner_email="owner@example.com",
            name="Export Me",
            namespace="claw-123-export",
            status=Claw.Status.RUNNING,
            tier_id="team",
        )
        job = ClawExportJob(
            claw=claw,
            owner_id=claw.owner_id,
            requested_by=ClawExportJob.RequestedBy.OWNER,
            requested_by_actor="owner",
            include_session=True,
            include_credits=True,
            snapshot_name="hive-export-test",
        )

        with (
            patch.object(job, "save") as save,
            patch.object(ClawEvent.objects, "create") as create_event,
        ):
            completed = views.complete_export_job(job)

        self.assertEqual(completed.status, ClawExportJob.Status.DONE)
        self.assertGreater(completed.byte_count, 0)
        self.assertRegex(completed.sha256, r"^[0-9a-f]{64}$")
        self.assertIsNotNone(completed.completed_at)
        self.assertEqual(completed.share_url, f"/hive/api/v1/owner/claws/{claw.id}/export/{job.id}")
        save.assert_called_once()
        create_event.assert_called_once()

    def test_repeated_owner_exports_for_same_claw_use_distinct_snapshot_names(self):
        from unittest.mock import patch
        from django.test import override_settings
        from claws import views
        from claws.models import Claw, ClawExportJob

        claw = Claw(owner_id=123, owner_username="owner", owner_email="owner@example.com", name="Export Me")

        def fake_create(**kwargs):
            return ClawExportJob(**kwargs)

        with (
            override_settings(DEBUG=True, HIVE_STEPUP_BACKEND="stub"),
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.ClawExportJob.objects.create", side_effect=fake_create) as create_job,
            # complete_export_job (74b8ede) saves the job + writes a ClawEvent;
            # with the mocked UNSAVED claw those are real FK-violating INSERTs
            # that trip TestCase's end-of-test constraint check (HIVE-577).
            patch("claws.views.complete_export_job", side_effect=lambda job, request=None: job),
        ):
            first_request, _ = self._owner_request({"include_session": True, "step_up_token": "fresh-step-up"})
            second_request, _ = self._owner_request({"include_session": True, "step_up_token": "fresh-step-up"})
            first_response = views.OwnerClawExportView.as_view()(first_request, claw_id=claw.id)
            second_response = views.OwnerClawExportView.as_view()(second_request, claw_id=claw.id)

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        snapshot_names = [call.kwargs["snapshot_name"] for call in create_job.call_args_list]
        self.assertEqual(len(snapshot_names), 2)
        self.assertNotEqual(snapshot_names[0], snapshot_names[1])
        for snapshot_name in snapshot_names:
            self.assertTrue(snapshot_name.startswith(f"hive-export-{views.uuid_safe(str(claw.id))}-"))
            self.assertLessEqual(len(snapshot_name), 128)

    def test_repeated_admin_exports_for_same_claw_use_distinct_snapshot_names(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views
        from claws.models import Claw, ClawExportJob

        claw = Claw(owner_id=123, owner_username="owner", owner_email="owner@example.com", name="Export Me")
        factory = APIRequestFactory()
        admin = SimpleNamespace(id=999, username="admin", email="admin@example.com", is_staff=True, is_authenticated=True)

        def fake_create(**kwargs):
            return ClawExportJob(**kwargs)

        with (
            patch("claws.views._get_admin_claw", return_value=claw),
            patch("claws.views.ClawExportJob.objects.create", side_effect=fake_create) as create_job,
            # See owner-variant note: complete_export_job would really INSERT
            # for the unsaved mocked claw and violate the claw FK.
            patch("claws.views.complete_export_job", side_effect=lambda job, request=None: job),
        ):
            first_request = factory.post("/", {"include_session": False, "include_credits": True}, format="json")
            second_request = factory.post("/", {"include_session": False, "include_credits": True}, format="json")
            force_authenticate(first_request, user=admin)
            force_authenticate(second_request, user=admin)
            first_response = views.AdminClawExportView.as_view()(first_request, claw_id=claw.id)
            second_response = views.AdminClawExportView.as_view()(second_request, claw_id=claw.id)

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        snapshot_names = [call.kwargs["snapshot_name"] for call in create_job.call_args_list]
        self.assertEqual(len(snapshot_names), 2)
        self.assertNotEqual(snapshot_names[0], snapshot_names[1])
        for snapshot_name in snapshot_names:
            self.assertTrue(snapshot_name.startswith(f"hive-export-{views.uuid_safe(str(claw.id))}-"))
            self.assertLessEqual(len(snapshot_name), 128)

    def test_export_job_share_url_allows_long_signed_urls(self):
        from claws.models import ClawExportJob

        self.assertGreaterEqual(ClawExportJob._meta.get_field("share_url").max_length, 2048)

    def test_owner_export_rejects_missing_stepup_before_queueing(self):
        from unittest.mock import patch
        from django.test import override_settings
        from claws import views
        from claws.models import Claw

        claw = Claw(owner_id=123, owner_username="owner", owner_email="owner@example.com", name="Export Me")
        request, _ = self._owner_request({"include_session": True})

        with (
            override_settings(DEBUG=True, HIVE_STEPUP_BACKEND="stub"),
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.ClawExportJob.objects.create") as create_job,
        ):
            response = views.OwnerClawExportView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 403)
        create_job.assert_not_called()

    def test_owner_hard_delete_requires_stepup_before_scale_down(self):
        from unittest.mock import patch
        from django.test import override_settings
        from claws import views
        from claws.models import Claw

        claw = Claw(owner_id=123, owner_username="owner", owner_email="owner@example.com", name="Delete Me")
        request, _ = self._owner_request({})

        with (
            override_settings(DEBUG=True, HIVE_STEPUP_BACKEND="stub"),
            patch("claws.views._get_owned_claw", return_value=claw),
            patch("claws.views.provisioner.stop_claw_for_reclaim") as stop_for_reclaim,
        ):
            response = views.OwnerHardDeleteView.as_view()(request, claw_id=claw.id)

        self.assertEqual(response.status_code, 403)
        stop_for_reclaim.assert_not_called()


class ClawDashboardStoppedStateTests(TestCase):
    """HIVE-1711: a non-RUNNING claw has no dashboard upstream.

    Minting a signed dashboard redirect for a STOPPED claw sent owners to a
    bare nginx 502 (no OpenClaw shell, no explanation) — the dead end Mika's
    headed-Chromium probe hit on the non-staff owner vantage. The dashboard-url
    endpoint must return the honest runtime state instead.
    """

    def _make_claw(self, *, status):
        return Claw.objects.create(
            owner_id=811,
            owner_username="owner811",
            owner_email="owner811@example.com",
            name="My Assistant",
            tier_id="free",
            status=status,
            status_message="Cortex model unavailable for this claw credential",
        )

    def _get(self, claw, *, redirect=False):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from claws import views

        user = SimpleNamespace(id=811, is_staff=False, is_authenticated=True)
        path = f"/hive/api/v1/claws/{claw.id}/dashboard-url"
        if redirect:
            path += "?redirect=1"
        request = APIRequestFactory().get(path)
        force_authenticate(request, user=user)
        return views.ClawDashboardUrlView.as_view()(request, claw_id=str(claw.id))

    def test_stopped_claw_returns_honest_409_not_a_signed_redirect(self):
        claw = self._make_claw(status=Claw.Status.STOPPED)
        response = self._get(claw)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["claw_status"], Claw.Status.STOPPED)
        self.assertIn("not running", response.data["error"])
        self.assertEqual(response.data["status_message"], claw.status_message)
        self.assertNotIn("dashboard_url", response.data)

    def test_stopped_claw_redirect_param_also_refuses(self):
        claw = self._make_claw(status=Claw.Status.STOPPED)
        response = self._get(claw, redirect=True)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("dashboard_url", response.data)

    def test_running_claw_still_mints_the_dashboard_url(self):
        claw = self._make_claw(status=Claw.Status.RUNNING)
        claw.status_message = ""
        claw.save(update_fields=["status_message"])
        with patch("claws.views.lifecycle.auth_state", return_value={"authorized": True}), \
             patch("claws.views.lifecycle.gateway_auth_token", return_value="gw-token"):
            response = self._get(claw)
        self.assertEqual(response.status_code, 200)
        self.assertIn("dashboard_url", response.data)
