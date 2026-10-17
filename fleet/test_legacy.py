"""
Tests for the fleet provisioner (HLD PLAT-147 v0.2 / EPIC PLAT-146, PLAT-148).

The provisioner builds k8s objects (Namespace/Secret/PVC/Deployment/Service)
from a ``FleetAgent`` and hands them to the (mocked) k8s API — we inspect those
objects; no cluster or DB is touched (``SimpleTestCase`` + ``MagicMock``).

These pin the HLD §3-§4 invariants that make the k3s-native fleet safe:
  * automountServiceAccountToken=false (Q1a) — no in-cluster kube API path,
  * AGENT_PASSWORD only in the identity Secret / broker sidecar, never the agent
    (non-broker seats); broker-sidecar QA seats get it on the agent container
    sourced from the identity Secret (HIVE-2058/CON-363),
  * single-active-instance (replicas=1 + Recreate),
  * arm64 + Longhorn placement, hostname anti-affinity, grace=30,
  * shared ns for ordinary agents, own ns for isolated (DevOps) agents,
  * Longhorn RWO workspace PVC.
"""
import base64
import requests
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kubernetes.client.rest import ApiException
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.response import Response

from fleet import provisioner
from fleet.models import FleetAgent, dns1123_validator, _default_platform_url
from fleet.provider_resolution import (
    REASON_NO_PROVIDER_CONFIGURED, Resolution,
)
from fleet.serializers import FleetAgentSerializer
from fleet.views import (
    FleetAgentListView,
    FleetPulseSnapshotView,
    FleetAgentTemplateDetailView,
    FleetAgentTemplateListView,
    FleetAgentUnarchiveView,
    FleetRuntimeOptionsView,
)


def _agent(**overrides):
    defaults = dict(
        agent_username='testagent',
        agent_id='agent-uuid-123',
        display_name='Test Agent',
        platform_url='http://platform',
    )
    defaults.update(overrides)
    agent = FleetAgent(**defaults)
    agent._reviewed_runtime_image = getattr(settings, 'FLEET_AGENT_IMAGE', '')
    return agent


def _build_deployment(agent=None):
    agent = agent or _agent()
    apps_v1 = MagicMock()
    reviewed_image = getattr(settings, 'FLEET_AGENT_IMAGE', '')
    core = MagicMock()
    core.read_namespaced_secret.side_effect = ApiException(status=404)
    with patch('fleet.provisioner._live_fleet_image', return_value=reviewed_image), \
            patch('fleet.provisioner.get_k8s_client', return_value=(core, apps_v1, MagicMock())):
        provisioner._create_deployment(apps_v1, 'shizuha-fleet', agent)
    apps_v1.create_namespaced_deployment.assert_called_once()
    _, deployment = apps_v1.create_namespaced_deployment.call_args.args
    return deployment


def _container(deployment, name):
    return next(c for c in deployment.spec.template.spec.containers if c.name == name)


class FleetDeploymentInvariantTests(SimpleTestCase):
    def test_single_active_instance(self):
        dep = _build_deployment()
        self.assertEqual(dep.spec.replicas, 1)
        self.assertEqual(dep.spec.strategy.type, 'Recreate')

    def test_automount_sa_token_disabled(self):
        # HLD Q1a — removes the in-cluster kube API path from a fleet pod.
        spec = _build_deployment().spec.template.spec
        self.assertFalse(spec.automount_service_account_token)

    def test_native_runtime_ownership_and_metrics_discovery_are_explicit(self):
        deployment = _build_deployment()
        self.assertEqual(
            deployment.metadata.labels[provisioner.NATIVE_RUNTIME_LABEL],
            provisioner.NATIVE_RUNTIME_LABEL_VALUE,
        )
        self.assertEqual(
            deployment.metadata.annotations[provisioner.NATIVE_RUNTIME_OWNER_ANNOTATION],
            provisioner.NATIVE_RUNTIME_OWNER,
        )
        self.assertEqual(
            deployment.spec.template.metadata.labels[provisioner.NATIVE_RUNTIME_LABEL],
            provisioner.NATIVE_RUNTIME_LABEL_VALUE,
        )
        ports = {
            (port.name, port.container_port)
            for port in (_container(deployment, 'agent').ports or [])
        }
        self.assertIn(('metrics', provisioner.NATIVE_RUNTIME_METRICS_PORT), ports)

    def test_managed_runtime_repository_is_not_reported_as_operator_pinned(self):
        agent = _agent(
            image='localhost:30500/shizuha-agent-runtime:harness-202608011904-9c4fbd4',
        )

        self.assertEqual(
            FleetAgentSerializer().get_image_source(agent),
            'fleet_desired',
        )

    def test_arm64_longhorn_node_selector(self):
        spec = _build_deployment().spec.template.spec
        self.assertEqual(spec.node_selector, {
            'kubernetes.io/arch': 'arm64',
            'storage.longhorn.io/ready': 'true',
        })

    def test_s1_node_pin_and_workstation_toleration(self):
        agent = _agent(
            arch='amd64',
            requires_longhorn=False,
            node_name='s1',
            tolerations=[{
                'key': 'node.shizuha/workstation',
                'operator': 'Equal',
                'value': 'true',
                'effect': 'NoSchedule',
            }],
        )
        spec = _build_deployment(agent).spec.template.spec
        self.assertEqual(spec.node_selector, {
            'kubernetes.io/arch': 'amd64',
            'kubernetes.io/hostname': 's1',
        })
        self.assertEqual(spec.tolerations[0].key, 'node.shizuha/workstation')
        self.assertEqual(spec.tolerations[0].value, 'true')


class FleetAgentTemplateCatalogTests(SimpleTestCase):
    def _write_template(self, root, slug='security-auditor'):
        template_dir = Path(root) / slug
        template_dir.mkdir(parents=True, exist_ok=True)
        (template_dir / 'TEMPLATE.md').write_text(
            """---
name: Security Auditor
description: Reviews code and infra for security risks.
tags: [security, audit]
category: review
role: Security Engineer
model: gpt-5.5
executionMethod: codex_app_server
skills: [security-review, qa-verification]
---

Follow the security review workflow.
""",
            encoding='utf-8',
        )

    def _staff_request(self, path):
        request = APIRequestFactory().get(path)
        force_authenticate(
            request,
            user=SimpleNamespace(is_authenticated=True, is_staff=True, id=1),
        )
        return request

    def test_agent_template_list_searches_configured_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_template(tmp)
            with override_settings(HIVE_AGENT_TEMPLATE_DIRS=[tmp]):
                response = FleetAgentTemplateListView.as_view()(
                    self._staff_request('/v1/fleet/agent-templates/?query=security')
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        template = response.data['templates'][0]
        self.assertEqual(template['slug'], 'security-auditor')
        self.assertEqual(template['name'], 'Security Auditor')
        self.assertEqual(template['skills'], ['security-review', 'qa-verification'])

    def test_agent_template_detail_returns_full_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_template(tmp)
            with override_settings(HIVE_AGENT_TEMPLATE_DIRS=[tmp]):
                response = FleetAgentTemplateDetailView.as_view()(
                    self._staff_request('/v1/fleet/agent-templates/security-auditor/'),
                    template_slug='security-auditor',
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['template']['name'], 'Security Auditor')
        self.assertIn('Follow the security review workflow.', response.data['template']['content'])

    def test_node_pool_selector(self):
        spec = _build_deployment(_agent(
            arch='amd64',
            requires_longhorn=False,
            node_pool='s1',
        )).spec.template.spec
        self.assertEqual(spec.node_selector, {
            'kubernetes.io/arch': 'amd64',
            'shizuha.io/node-pool': 's1',
        })

    @override_settings(FLEET_TOLERATIONS=[{
        'key': 'node.shizuha/workstation',
        'operator': 'Equal',
        'value': 'true',
        'effect': 'NoSchedule',
    }])
    def test_global_fleet_tolerations_are_included(self):
        spec = _build_deployment(_agent(arch='amd64', requires_longhorn=False)).spec.template.spec
        self.assertEqual(spec.tolerations[0].key, 'node.shizuha/workstation')

    def test_hostname_pod_anti_affinity_is_fleet_wide(self):
        # Review #4: must select the fleet-wide label with Exists (NOT
        # shizuha.io/agent=<this>, which is a no-op for replicas=1) so different
        # agents actually spread across nodes.
        spec = _build_deployment().spec.template.spec
        term = spec.affinity.pod_anti_affinity.preferred_during_scheduling_ignored_during_execution[0]
        self.assertEqual(term.pod_affinity_term.topology_key, 'kubernetes.io/hostname')
        sel = term.pod_affinity_term.label_selector
        self.assertFalse(sel.match_labels)
        [req] = sel.match_expressions
        self.assertEqual(req.key, 'shizuha.io/agent')
        self.assertEqual(req.operator, 'Exists')
        # round-2 P2: scoped to fleet namespaces, not just the pod's own ns
        self.assertEqual(
            term.pod_affinity_term.namespace_selector.match_labels,
            {'shizuha.io/fleet': 'true'},
        )

    def test_control_plane_nodes_are_hard_excluded(self):
        # Operator hard-constraint #1 (HIVE-45 / PLAT-146): agents must NEVER
        # schedule onto a control-plane/etcd node. Taints alone don't protect
        # this because they drift over time — a REQUIRED node-affinity excludes
        # the standard control-plane role label for every agent.
        spec = _build_deployment().spec.template.spec
        terms = (spec.affinity.node_affinity
                 .required_during_scheduling_ignored_during_execution
                 .node_selector_terms)
        [req] = terms[0].match_expressions
        self.assertEqual(req.key, 'node-role.kubernetes.io/control-plane')
        self.assertEqual(req.operator, 'DoesNotExist')

    def test_control_plane_exclusion_applies_even_with_node_pin(self):
        # A pin to a non-control-plane node (s1) still carries the exclusion —
        # workers stay off etcd. A pin *to* a control-plane hostname is the
        # storage-locality exception (see test below).
        spec = _build_deployment(_agent(arch='amd64', requires_longhorn=False,
                                        node_name='s1')).spec.template.spec
        [req] = (spec.affinity.node_affinity
                 .required_during_scheduling_ignored_during_execution
                 .node_selector_terms[0].match_expressions)
        self.assertEqual(req.operator, 'DoesNotExist')

    def test_control_plane_exclusion_skipped_when_workspace_pin_is_control_plane(self):
        # CoS 2026-09-05: local-path PVC nodeAffinity hostname=gx10-15 (etcd)
        # + required DoesNotExist control-plane → 0 schedulable nodes, Pending
        # 134m. Storage-locality wins; skip the exclusion so the pod can land
        # on the volume's node. Memory limits + earlyoom keep etcd safe.
        with patch('fleet.provisioner._hostname_is_control_plane', return_value=True), \
             patch('fleet.provisioner._workspace_volume_hostname', return_value='gx10-15'):
            spec = _build_deployment(
                _agent(node_name='gx10-15', requires_longhorn=False),
            ).spec.template.spec
        na = spec.affinity.node_affinity
        required = (
            None if na is None
            else na.required_during_scheduling_ignored_during_execution
        )
        self.assertIsNone(required)
        self.assertTrue(
            spec.affinity.pod_anti_affinity
            .preferred_during_scheduling_ignored_during_execution
        )

    def test_termination_grace_period(self):
        self.assertEqual(_build_deployment().spec.template.spec.termination_grace_period_seconds, 30)

    def test_agent_container_has_no_password_and_only_scoped_secret_refs(self):
        # A NON-broker agent container must never carry AGENT_PASSWORD (HLD §5);
        # the identity password is isolated to the broker sidecar. Broker-sidecar
        # seats DO get AGENT_PASSWORD on the agent container (HIVE-2058/CON-363) —
        # covered by test_broker_sidecar_agent_renders_agent_password.
        agent_c = _container(_build_deployment(_agent(sidecar_set=[])), 'agent')
        names = [e.name for e in (agent_c.env or [])]
        self.assertNotIn('AGENT_PASSWORD', names)
        # Only scoped provider/repository credentials reach the agent container;
        # the identity password remains isolated in the broker sidecar.
        for e in (agent_c.env or []):
            ref = getattr(e, 'value_from', None)
            if ref is not None:
                skr = getattr(ref, 'secret_key_ref', None)
                self.assertIsNotNone(skr)
                self.assertIn(skr.name, {'cortex-fleet-key', 'testagent-agent-creds'})
        self.assertIsNone(agent_c.env_from)

    def test_broker_sidecar_agent_renders_agent_password(self):
        # HIVE-2058 (CON-363): broker-sidecar QA seats need AGENT_PASSWORD on the
        # agent container (browser-e2e QA logs in via /id/api/auth/login/), not
        # just in the broker. It must be sourced from the SAME identity Secret the
        # broker envFrom uses (agent-<username>-identity) so a daemon re-render
        # keeps it — never a stale <name>-agent-creds and never an inline literal.
        dep = _build_deployment()  # default sidecar_set=['broker']
        agent_c = _container(dep, 'agent')
        by_name = {e.name: e for e in (agent_c.env or [])}
        self.assertIn('AGENT_PASSWORD', by_name)
        self.assertIsNone(by_name['AGENT_PASSWORD'].value)  # never an inline literal
        skr = by_name['AGENT_PASSWORD'].value_from.secret_key_ref
        self.assertEqual(skr.name, 'agent-testagent-identity')
        self.assertEqual(skr.key, 'AGENT_PASSWORD')
        self.assertFalse(skr.optional)  # fail closed, like the broker's envFrom
        # Broker still gets it via envFrom from the same identity Secret.
        broker = _container(dep, 'broker')
        srcs = [s.secret_ref.name for s in broker.env_from if s.secret_ref]
        self.assertIn('agent-testagent-identity', srcs)

    def test_cortex_api_key_injected_from_secret(self):
        # Per-agent Cortex key (2026-07-28): OPENAI/VLLM/CORTEX_API_KEY come from
        # ``<username>-agent-creds`` CORTEX_API_KEY — never the shared
        # agents-fleet-own-fleet / cortex-fleet-key principal.
        agent_c = _container(_build_deployment(), 'agent')
        by_name = {e.name: e for e in (agent_c.env or [])}
        for name in ('OPENAI_API_KEY', 'VLLM_API_KEY', 'CORTEX_API_KEY'):
            self.assertIn(name, by_name)
            self.assertIsNone(by_name[name].value)  # never an inline literal
            skr = by_name[name].value_from.secret_key_ref
            self.assertEqual(skr.name, 'testagent-agent-creds')
            self.assertEqual(skr.key, 'CORTEX_API_KEY')
            # 2026-08-05: this assertion used to require optional=True, which
            # made a MISSING credential resolve to an empty string instead of
            # failing Pod admission. rei ran five weeks that way — Running,
            # Ready, Alive, heartbeating, holding a lease — returning
            # `vLLM error 401` on every single turn, surfaced only as a
            # detail-less "Runtime unhealthy" chip. A pod that refuses to start
            # names the missing credential; a pod that starts without one does
            # not. See fleet/test_agent_cortex_key.py.
            self.assertFalse(skr.optional)

    def test_forgejo_tokens_injected_from_per_agent_secret(self):
        agent_c = _container(_build_deployment(), 'agent')
        by_name = {e.name: e for e in (agent_c.env or [])}
        for name in ('FORGEJO_TOKEN', 'ORIGIN_TOKEN'):
            self.assertIn(name, by_name)
            self.assertIsNone(by_name[name].value)
            skr = by_name[name].value_from.secret_key_ref
            self.assertEqual(skr.name, 'testagent-agent-creds')
            self.assertEqual(skr.key, 'FORGEJO_TOKEN')
            self.assertTrue(skr.optional)

    def test_forgejo_literal_overrides_are_replaced_by_one_secret_ref_per_alias(self):
        agent = _agent(env={
            'FORGEJO_TOKEN': 'must-not-enter-the-manifest',
            'ORIGIN_TOKEN': 'must-not-enter-the-manifest',
        })
        agent_c = _container(_build_deployment(agent), 'agent')

        for name in ('FORGEJO_TOKEN', 'ORIGIN_TOKEN'):
            [entry] = [item for item in agent_c.env if item.name == name]
            self.assertIsNone(entry.value)
            ref = entry.value_from.secret_key_ref
            self.assertEqual(ref.name, 'testagent-agent-creds')
            self.assertEqual(ref.key, 'FORGEJO_TOKEN')
            self.assertTrue(ref.optional)

    def test_workspace_mounted_from_longhorn_pvc(self):
        dep = _build_deployment()
        agent_c = _container(dep, 'agent')
        mounts = {m.name: m for m in agent_c.volume_mounts}
        self.assertEqual(mounts['workspace'].mount_path, '/home/agent/.shizuha')
        vol = next(v for v in dep.spec.template.spec.volumes if v.name == 'workspace')
        self.assertEqual(vol.persistent_volume_claim.claim_name, 'agent-testagent-workspace')


    def test_default_fleet_images_are_immutable_manifest_list_tags(self):
        dep = _build_deployment()
        agent_c = _container(dep, 'agent')
        broker_c = _container(dep, 'broker')
        self.assertEqual(
            agent_c.image,
            'localhost:30500/shizuha-agent-runtime:src-20260618-mcp-authfix',
        )
        self.assertEqual(
            broker_c.image,
            settings.FLEET_BROKER_IMAGE,
        )
        self.assertEqual(agent_c.image_pull_policy, 'IfNotPresent')
        self.assertEqual(broker_c.image_pull_policy, 'IfNotPresent')

    @override_settings(FLEET_AGENT_IMAGE='gx10-1:30500/shizuha-agent-runtime:test')
    def test_image_is_registry_qualified_default(self):
        agent_c = _container(_build_deployment(), 'agent')
        self.assertEqual(agent_c.image, 'gx10-1:30500/shizuha-agent-runtime:test')
        self.assertEqual(agent_c.image_pull_policy, 'IfNotPresent')

    @override_settings(FLEET_AGENT_IMAGE='gx10-1:30500/shizuha-agent-runtime:stale')
    def test_agent_created_uses_reviewed_projection_not_legacy_cache(self):
        reviewed = 'localhost:30500/shizuha-agent-runtime:harness-reviewed'
        self.addCleanup(cache.delete, 'fleet:desired-runtime-image')
        cache.set(
            'fleet:desired-runtime-image',
            'localhost:30500/shizuha-agent-runtime:harness-unreviewed-cache',
            None,
        )
        apps_v1 = MagicMock()
        agent = _agent()
        agent._reviewed_runtime_image = reviewed
        with patch('fleet.provisioner.get_k8s_client', return_value=(MagicMock(), apps_v1, MagicMock())):
            provisioner._create_deployment(apps_v1, 'shizuha-fleet', agent)
        _, deployment = apps_v1.create_namespaced_deployment.call_args.args

        agent_c = _container(deployment, 'agent')

        self.assertEqual(agent_c.image, reviewed)


class FleetBrokerSidecarTests(SimpleTestCase):
    def test_broker_sidecar_present_by_default_with_secret_envfrom(self):
        dep = _build_deployment()
        broker = _container(dep, 'broker')  # raises if absent
        sources = [s.secret_ref.name for s in broker.env_from if s.secret_ref]
        self.assertIn('agent-testagent-identity', sources)

    def test_no_sidecar_when_set_empty(self):
        dep = _build_deployment(_agent(sidecar_set=[]))
        names = [c.name for c in dep.spec.template.spec.containers]
        self.assertEqual(names, ['agent'])


class FleetSecretTests(SimpleTestCase):
    def test_password_lives_only_in_identity_secret(self):
        core_v1 = MagicMock()
        provisioner._create_identity_secret(core_v1, 'shizuha-fleet', _agent(), 'sup3r-secret', '')
        _, secret = core_v1.create_namespaced_secret.call_args.args
        self.assertEqual(secret.string_data['AGENT_PASSWORD'], 'sup3r-secret')
        self.assertEqual(secret.metadata.name, 'agent-testagent-identity')

    def test_no_password_key_when_not_supplied(self):
        core_v1 = MagicMock()
        provisioner._create_identity_secret(core_v1, 'shizuha-fleet', _agent(), '', '')
        _, secret = core_v1.create_namespaced_secret.call_args.args
        self.assertNotIn('AGENT_PASSWORD', secret.string_data)


def _resp(ok, status, data):
    r = MagicMock()
    r.ok = ok
    r.status_code = status
    r.json.return_value = data
    r.text = str(data)
    return r


@override_settings(HIVE_ID_FLEET_PROVISIONER_TOKEN='scoped-token')
class FleetIdentityProvisionTests(SimpleTestCase):
    """HIVE-75: fleet provisioning uses a scoped id-side endpoint, not admin creds."""

    @override_settings(SHIZUHA_ID_INTERNAL_URL='http://id.internal')
    def test_scoped_endpoint_called_with_agent_namespace_and_password(self):
        calls = []

        def fake_post(url, **kw):
            calls.append(('POST', url, kw))
            return _resp(True, 200, {
                'action': 'created', 'user_id': 42, 'profile_id': 142,
                'owner_user_id': None, 'owner_profile_id': None,
                'ownership_class': '', 'username': 'testagent',
            })

        with patch.object(provisioner, 'requests') as rq:
            rq.post.side_effect = fake_post
            ok = provisioner._provision_platform_identity(_agent(), 'genpw!A1', 'http://platform')

        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        method, url, kwargs = calls[0]
        self.assertEqual(method, 'POST')
        # e7e76f9: provisioner calls shizuha-id's in-cluster service directly
        # (nginx ingress-guard blocks /id/api/internal/*), so no /id prefix.
        self.assertEqual(url, 'http://id.internal/api/internal/fleet/provision-agent/')
        self.assertNotIn('http://platform', url)
        self.assertEqual(kwargs['headers']['X-Fleet-Provisioner-Token'], 'scoped-token')
        self.assertEqual(kwargs['json']['username'], 'testagent')
        # PLAT-4220: agents provision on the canonical @shizuha.com domain (the
        # id fleet-provision endpoint accepts @shizuha.com canonical +
        # @agents.shizuha.io legacy).
        self.assertEqual(kwargs['json']['email'], 'testagent@shizuha.com')
        self.assertEqual(kwargs['json']['password'], 'genpw!A1')
        # 2026-08-05: the key is now OMITTED rather than sent as None.
        # `owner_user_id` declares a personal-default identity, and shizuha-id
        # then requires username == `shizuha-<owner_id>`; sending it for an
        # ordinary agent made every identity re-provision 400 and turned any
        # credential drift into a permanent DRIFT_HOLD (rei). Absent and None
        # are equivalent to the receiver (`if owner_user_id not in (None, '')`).
        self.assertNotIn('owner_user_id', kwargs['json'])

    @override_settings(HIVE_ID_FLEET_PROVISIONER_TOKEN='')
    def test_missing_scoped_token_fails_closed(self):
        with patch.object(provisioner, 'requests') as rq:
            ok = provisioner._provision_platform_identity(_agent(), 'genpw!A1', 'http://platform')

        self.assertFalse(ok)
        rq.post.assert_not_called()

    def test_no_agent_claims_a_personal_default_identity(self):
        """The personal-default concept is retired (operator 2026-08-05).

        These two tests used to assert the opposite: that a `shizuha-<owner_id>`
        agent SENDS `owner_user_id`, and that a mismatched personal-owner
        binding is rejected. Both encoded an assumption that split the fleet in
        two — and shizuha-id rejects the claim with 400 for any agent whose name
        is not the canonical owner-derived one, which turned a credential drift
        into a permanent DRIFT_HOLD for 42 of 58 agents (rei).

        Every agent now provisions its identity the same way, so the claim is
        never made and there is no binding to mismatch.
        """
        with patch.object(provisioner, 'requests') as rq:
            rq.post.return_value = _resp(True, 201, {
                'action': 'created', 'user_id': 142, 'profile_id': 242,
            })
            result = provisioner._provision_platform_identity_details(
                _agent(agent_username='shizuha-42', owner_id=42),
                'genpw!A1',
                'http://platform',
            )

        self.assertIsNotNone(result)
        self.assertNotIn('owner_user_id', rq.post.call_args.kwargs['json'])

    def test_an_unrequested_owner_binding_in_the_response_is_not_fatal(self):
        """Nothing is claimed, so nothing can be mismatched.

        Rejecting here is what put rei back into DRIFT_HOLD after its identity
        had already reconnected successfully (user_id=106).
        """
        with patch.object(provisioner, 'requests') as rq:
            rq.post.return_value = _resp(True, 200, {
                'action': 'reconnected', 'user_id': 142, 'profile_id': 242,
                'owner_user_id': 99, 'owner_profile_id': 399,
                'ownership_class': 'personal_default_member',
            })
            result = provisioner._provision_platform_identity_details(
                _agent(agent_username='shizuha-42', owner_id=42),
                'genpw!A1',
                'http://platform',
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.user_id, 142)

    def test_identity_response_without_exact_profile_ids_fails_closed(self):
        with patch.object(provisioner, 'requests') as rq:
            rq.post.return_value = _resp(True, 200, {'action': 'created', 'user_id': 42})
            result = provisioner._provision_platform_identity_details(
                _agent(), 'genpw!A1', 'http://platform',
            )

        self.assertIsNone(result)

    def test_scoped_endpoint_failure_fails_closed(self):
        with patch.object(provisioner, 'requests') as rq:
            rq.post.return_value = _resp(False, 403, {'error': 'Forbidden'})
            ok = provisioner._provision_platform_identity(_agent(), 'genpw!A1', 'http://platform')

        self.assertFalse(ok)


class FleetPvcTests(SimpleTestCase):
    def test_workspace_pvc_uses_interim_s1_default_storage_class(self):
        core_v1 = MagicMock()
        provisioner._create_workspace_pvc(core_v1, 'shizuha-fleet', _agent(pvc_size='15Gi'))
        _, pvc = core_v1.create_namespaced_persistent_volume_claim.call_args.args
        self.assertEqual(pvc.spec.access_modes, ['ReadWriteOnce'])
        self.assertEqual(pvc.spec.storage_class_name, 'local-path')
        self.assertEqual(pvc.spec.resources.requests['storage'], '15Gi')
        self.assertEqual(pvc.metadata.name, 'agent-testagent-workspace')

    def test_workspace_pvc_uses_per_agent_storage_class(self):
        core_v1 = MagicMock()
        provisioner._create_workspace_pvc(
            core_v1,
            'shizuha-fleet',
            _agent(pvc_size='15Gi', storage_class='local-path'),
        )
        _, pvc = core_v1.create_namespaced_persistent_volume_claim.call_args.args
        self.assertEqual(pvc.spec.storage_class_name, 'local-path')

    def test_heal_expands_bound_legacy_claim_on_enospc(self):
        agent = _agent(agent_username='sara', pvc_size='10Gi')
        core = MagicMock()
        apps = MagicMock()
        vol = MagicMock()
        vol.persistent_volume_claim.claim_name = 'sara-workspace'
        apps.read_namespaced_deployment.return_value.spec.template.spec.volumes = [vol]
        pvc = MagicMock()
        pvc.spec.resources.requests = {'storage': '10Gi'}
        pvc.spec.storage_class_name = 'longhorn'
        core.read_namespaced_persistent_volume_claim.return_value = pvc
        pod = MagicMock()
        pod.metadata.name = 'agent-sara-x'
        core.list_namespaced_pod.return_value.items = [pod]
        core.read_namespaced_pod_log.return_value = (
            '[base-instructions] write failed: ENOSPC: no space left on device, write'
        )
        with patch.object(provisioner, 'get_k8s_client', return_value=(core, apps, None)):
            ok = provisioner.heal_workspace_disk(agent)
        self.assertTrue(ok)
        core.patch_namespaced_persistent_volume_claim.assert_called_once()
        args = core.patch_namespaced_persistent_volume_claim.call_args.args
        self.assertEqual(args[0], 'sara-workspace')
        self.assertEqual(args[2]['spec']['resources']['requests']['storage'], '50Gi')

    def test_heal_is_noop_without_enospc(self):
        agent = _agent(agent_username='sara', pvc_size='10Gi')
        core = MagicMock()
        apps = MagicMock()
        pod = MagicMock()
        pod.metadata.name = 'agent-sara-x'
        core.list_namespaced_pod.return_value.items = [pod]
        core.read_namespaced_pod_log.return_value = 'listening on :8080'
        with patch.object(provisioner, 'get_k8s_client', return_value=(core, apps, None)):
            ok = provisioner.heal_workspace_disk(agent)
        self.assertFalse(ok)
        core.patch_namespaced_persistent_volume_claim.assert_not_called()


class FleetNamespaceModelTests(SimpleTestCase):
    def test_ordinary_agent_uses_shared_namespace(self):
        self.assertEqual(_agent().resolve_namespace('shizuha-fleet'), 'shizuha-fleet')

    def test_isolated_agent_gets_own_namespace(self):
        self.assertEqual(_agent(isolated=True).resolve_namespace('shizuha-fleet'), 'fleet-testagent')

    def test_derived_resource_names(self):
        a = _agent(agent_username='ichi')
        self.assertEqual(a.deployment_name, 'agent-ichi')
        self.assertEqual(a.identity_secret_name, 'agent-ichi-identity')
        self.assertEqual(a.workspace_pvc_name, 'agent-ichi-workspace')


class FleetServiceTests(SimpleTestCase):
    def test_service_only_built_when_exposed(self):
        # Agents are egress-only (§7); a Service is built only for expose_http.
        core_v1 = MagicMock()
        provisioner._create_service(core_v1, 'shizuha-fleet', _agent(http_port=8080))
        _, svc = core_v1.create_namespaced_service.call_args.args
        [port] = svc.spec.ports
        self.assertEqual(port.port, 8080)
        self.assertEqual(svc.spec.selector, {'shizuha.io/agent': 'testagent'})


class FleetScaleGuardTests(SimpleTestCase):
    def test_scale_rejects_multi_replica(self):
        # Single-active-instance: only 0 or 1 allowed (HLD §8).
        with self.assertRaises(ValueError):
            provisioner.scale_agent(_agent(), 2)


class FleetNativeResourceContractTests(SimpleTestCase):
    def test_codex_bridge_runtime_has_8gi_floor(self):
        agent = _agent(image='registry/shizuha-agent-runtime:current', model='gpt-5.6-sol')
        resources = provisioner._resolve_resources(agent)
        self.assertEqual(resources.limits['memory'], '8Gi')

    def test_gateway_runtime_has_6gi_floor(self):
        agent = _agent(image='registry/shizuha-agent-runtime:current', model='cortex/grok-4.5')
        resources = provisioner._resolve_resources(agent)
        self.assertEqual(resources.limits['memory'], '6Gi')

    def test_explicit_resource_override_remains_authoritative(self):
        agent = _agent(
            image='registry/shizuha-agent-runtime:current', model='gpt-5.6-sol',
            resource_overrides={
                'requests': {'cpu': '100m', 'memory': '256Mi'},
                'limits': {'cpu': '1', 'memory': '10Gi'},
            },
        )
        resources = provisioner._resolve_resources(agent)
        self.assertEqual(resources.limits['memory'], '10Gi')


class FleetBuildSnapshotTests(SimpleTestCase):
    def test_release_builds_avoid_full_filesystem_run_snapshots(self):
        root = Path(__file__).resolve().parents[1]
        for workflow_name in ('deploy.yml', 'deploy-frontend.yml', 'deploy-hive-mcp.yml'):
            workflow = (root / '.forgejo' / 'workflows' / workflow_name).read_text()
            self.assertIn('--snapshot-mode=redo', workflow)
            self.assertIn('--use-new-run', workflow)
            self.assertIn('superseded active Hive', workflow)


# ── Review fixes (aoi PLAT-148 PR #9 changes-requested) ──────────────────────


class _NoByoProviderGateMixin:
    """Legacy provisioner-flow tests: the HIVE-2161 S3-lite provider gate
    consults the DB (roster adjacency + org provider references) inside
    _lease_start_decision, which SimpleTestCase forbids. These flows do not
    exercise BYO resolution — pin the gate boundary to the non-BYO outcome so
    the flows proceed exactly as they did before the gate landed (gate passes
    through to the ordinary lease decision)."""

    def setUp(self):
        super().setUp()
        gate_patch = patch(
            'fleet.provider_resolution.resolve_agent_org_provider',
            return_value=Resolution(
                ok=False, reason=REASON_NO_PROVIDER_CONFIGURED,
            ),
        )
        gate_patch.start()
        self.addCleanup(gate_patch.stop)


class FleetSandboxGateTests(_NoByoProviderGateMixin, SimpleTestCase):
    """Review #1: sandbox_required agents must NOT be provisioned as a plain
    native pod (no exec substitute) — gate to BLOCKED."""

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_sandbox_required_blocked_not_provisioned(self, mock_k8s, _log):
        agent = _agent(sandbox_required=True)
        agent.save = MagicMock()
        ok = provisioner.provision_agent(agent, agent_password='x')
        self.assertFalse(ok)
        self.assertEqual(agent.status, FleetAgent.Status.BLOCKED)
        mock_k8s.assert_not_called()  # never touched k8s

    @override_settings(FLEET_SANDBOX_RUNTIME_CLASS='gvisor')
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_sandbox_required_proceeds_when_substitute_configured(self, mock_k8s, _log):
        mock_k8s.return_value = (MagicMock(), MagicMock(), MagicMock())
        agent = _agent(sandbox_required=True)
        agent.save = MagicMock()
        ok = provisioner.provision_agent(agent, agent_password='x')
        self.assertTrue(ok)
        self.assertEqual(agent.status, FleetAgent.Status.RUNNING)
        mock_k8s.assert_called()  # passed the gate


class FleetSecretPreserveTests(SimpleTestCase):
    """Review #2: a spec-only reprovision must not wipe the stored AGENT_PASSWORD."""

    def test_reprovision_without_password_preserves_existing_keys(self):
        core_v1 = MagicMock()
        core_v1.create_namespaced_secret.side_effect = ApiException(status=409, reason='exists')
        core_v1.read_namespaced_secret.return_value = SimpleNamespace(
            data={
                'AGENT_PASSWORD': base64.b64encode(b'old-pw').decode(),
                'AGENT_ID': base64.b64encode(b'old-id').decode(),
            },
            metadata=SimpleNamespace(resource_version='rv-9'),
        )
        provisioner._create_identity_secret(core_v1, 'shizuha-fleet', _agent(), '', '')
        core_v1.replace_namespaced_secret.assert_called_once()
        _, _, merged = core_v1.replace_namespaced_secret.call_args.args
        # old AGENT_PASSWORD preserved (as base64 data), NOT in string_data overwrite
        self.assertIn('AGENT_PASSWORD', merged.data)
        self.assertEqual(merged.data['AGENT_PASSWORD'], base64.b64encode(b'old-pw').decode())
        self.assertNotIn('AGENT_PASSWORD', merged.string_data)
        self.assertEqual(merged.string_data['AGENT_USERNAME'], 'testagent')
        self.assertEqual(merged.metadata.resource_version, 'rv-9')

    def test_reprovision_with_password_updates_it(self):
        core_v1 = MagicMock()
        core_v1.create_namespaced_secret.side_effect = ApiException(status=409, reason='exists')
        core_v1.read_namespaced_secret.return_value = SimpleNamespace(
            data={'AGENT_PASSWORD': base64.b64encode(b'old').decode()},
            metadata=SimpleNamespace(resource_version='rv-1'),
        )
        provisioner._create_identity_secret(core_v1, 'shizuha-fleet', _agent(), 'new-pw', '')
        _, _, merged = core_v1.replace_namespaced_secret.call_args.args
        self.assertEqual(merged.string_data['AGENT_PASSWORD'], 'new-pw')  # string_data wins


class FleetPersistedNsTests(_NoByoProviderGateMixin, SimpleTestCase):
    """Review #3: lifecycle ops target the ns persisted at provision time."""

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_stop_uses_persisted_namespace(self, mock_k8s, _log):
        apps = MagicMock()
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())
        agent = _agent(namespace='fleet-ns-AT-PROVISION')  # differs from resolver default
        agent.save = MagicMock()
        provisioner.stop_agent(agent)
        ns = apps.patch_namespaced_deployment_scale.call_args.kwargs['namespace']
        self.assertEqual(ns, 'fleet-ns-AT-PROVISION')

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_start_stages_persisted_desired_image_before_scaling(self, mock_k8s, _log):
        apps = MagicMock()
        container = SimpleNamespace(name='agent', image='registry/shizuha-agent-runtime:old', image_pull_policy='IfNotPresent')
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container]))),
        )
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())
        agent = _agent(
            namespace='fleet-ns-AT-PROVISION',
            image='registry/shizuha-agent-runtime:new',
            status=FleetAgent.Status.STOPPED,
        )
        agent._reviewed_runtime_image = agent.image
        agent.save = MagicMock()

        self.assertTrue(provisioner.start_agent(agent))
        self.assertEqual(container.image, 'registry/shizuha-agent-runtime:new')
        apps.replace_namespaced_deployment.assert_called_once()
        apps.patch_namespaced_deployment_scale.assert_called_once()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_image_convergence_repairs_legacy_native_resource_limit(self, mock_k8s, _log):
        apps = MagicMock()
        container = SimpleNamespace(
            name='agent', image='registry/shizuha-agent-runtime:current',
            image_pull_policy='IfNotPresent',
            resources=SimpleNamespace(
                requests={'cpu': '250m', 'memory': '512Mi'},
                limits={'cpu': '2', 'memory': '2Gi'},
            ),
        )
        deployment = SimpleNamespace(
            spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container]))),
        )
        apps.read_namespaced_deployment.return_value = deployment
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())
        agent = _agent(
            image='registry/shizuha-agent-runtime:current', model='gpt-5.6-sol',
        )

        self.assertTrue(provisioner.ensure_agent_image(agent))

        self.assertEqual(container.resources.limits['memory'], '8Gi')
        self.assertEqual(
            deployment.metadata.annotations[provisioner.NATIVE_RUNTIME_OWNER_ANNOTATION],
            provisioner.NATIVE_RUNTIME_OWNER,
        )
        self.assertEqual(
            deployment.spec.template.metadata.labels[provisioner.NATIVE_RUNTIME_LABEL],
            provisioner.NATIVE_RUNTIME_LABEL_VALUE,
        )
        self.assertIn(
            ('metrics', provisioner.NATIVE_RUNTIME_METRICS_PORT),
            {(item.name, item.container_port) for item in container.ports},
        )
        apps.replace_namespaced_deployment.assert_called_once()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_wake_convergence_stages_hive_desired_model_in_native_template(
        self, mock_k8s, _log,
    ):
        apps = MagicMock()
        container = SimpleNamespace(
            name='agent',
            image='registry/shizuha-agent-runtime:current',
            image_pull_policy='IfNotPresent',
            env=[
                provisioner.client.V1EnvVar(
                    name='MODEL',
                    value='cortex/GLM-5.2-NVFP4-AQLM-380K',
                ),
                provisioner.client.V1EnvVar(name='UNRELATED_ENV', value='preserve-me'),
            ],
            resources=SimpleNamespace(requests={}, limits={}),
        )
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            spec=SimpleNamespace(
                template=SimpleNamespace(spec=SimpleNamespace(containers=[container])),
            ),
        )
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())
        agent = _agent(
            image='registry/shizuha-agent-runtime:current',
            model='cortex/DeepSeek-V4-Flash',
        )

        self.assertTrue(provisioner.ensure_agent_image(agent))

        by_name = {item.name: item for item in container.env}
        self.assertEqual(by_name['MODEL'].value, 'cortex/DeepSeek-V4-Flash')
        self.assertEqual(by_name['UNRELATED_ENV'].value, 'preserve-me')
        apps.replace_namespaced_deployment.assert_called_once()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_wake_convergence_drops_retired_qwen_primary_model_env(
        self, mock_k8s, _log,
    ):
        apps = MagicMock()
        container = SimpleNamespace(
            name='agent',
            image='registry/shizuha-agent-runtime:current',
            image_pull_policy='IfNotPresent',
            env=[
                provisioner.client.V1EnvVar(
                    name='MODEL',
                    value='cortex/Qwen3.8-27B-Q2',
                ),
                provisioner.client.V1EnvVar(
                    name='SHIZUHA_K8S_PRIMARY_MODEL',
                    value='cortex/Qwen3.8-27B-Q2',
                ),
                provisioner.client.V1EnvVar(name='UNRELATED_ENV', value='preserve-me'),
            ],
            resources=SimpleNamespace(requests={}, limits={}),
        )
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            spec=SimpleNamespace(
                template=SimpleNamespace(spec=SimpleNamespace(containers=[container])),
            ),
        )
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())
        agent = _agent(
            image='registry/shizuha-agent-runtime:current',
            model='cortex/Qwen3.8-27B-Q2',
        )

        self.assertTrue(provisioner.ensure_agent_image(agent))

        by_name = {item.name: item for item in container.env}
        self.assertEqual(by_name['MODEL'].value, 'cortex/GLM-5.3-Flash')
        self.assertNotIn('SHIZUHA_K8S_PRIMARY_MODEL', by_name)
        self.assertEqual(by_name['UNRELATED_ENV'].value, 'preserve-me')
        apps.replace_namespaced_deployment.assert_called_once()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_wake_convergence_drops_sidecar_qwen_fallback_env(
        self, mock_k8s, _log,
    ):
        apps = MagicMock()
        agent_container = SimpleNamespace(
            name='agent',
            image='registry/shizuha-agent-runtime:current',
            image_pull_policy='IfNotPresent',
            env=[
                provisioner.client.V1EnvVar(
                    name='MODEL', value='cortex/DeepSeek-V4-Flash',
                ),
            ],
            resources=SimpleNamespace(requests={}, limits={}),
        )
        broker = SimpleNamespace(
            name='broker',
            image='registry/mcp-auth-proxy:current',
            env=[
                provisioner.client.V1EnvVar(
                    name='SHIZUHA_MODEL_FALLBACKS',
                    value='[{"model":"cortex/Qwen3.6-27B-NVFP4"}]',
                ),
                provisioner.client.V1EnvVar(name='KEEP_ME', value='1'),
            ],
        )
        dind = SimpleNamespace(
            name='dind',
            image='docker:dind',
            env=[
                provisioner.client.V1EnvVar(
                    name='SHIZUHA_MODEL_FALLBACKS',
                    value='[{"model":"cortex/Qwen3.6-27B-NVFP4"}]',
                ),
            ],
        )
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            spec=SimpleNamespace(
                template=SimpleNamespace(
                    spec=SimpleNamespace(containers=[agent_container, broker, dind]),
                ),
            ),
        )
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())
        agent = _agent(
            image='registry/shizuha-agent-runtime:current',
            model='cortex/DeepSeek-V4-Flash',
        )

        self.assertTrue(provisioner.ensure_agent_image(agent))

        self.assertEqual(
            [item.to_dict() for item in broker.env],
            [provisioner.client.V1EnvVar(name='KEEP_ME', value='1').to_dict()]
            + [item.to_dict() for item in provisioner._broker_sidecar(agent).env],
        )
        self.assertEqual(dind.env, [])
        apps.replace_namespaced_deployment.assert_called_once()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_harness_roll_reconciles_model_and_bridge_command_in_both_directions(
        self, mock_k8s, _log,
    ):
        """Mirror the production harness/model-lane sequence from HIVE-1674."""
        apps = MagicMock()
        old_agent = _agent(
            image='localhost:30500/shizuha-agent-runtime:harness-old',
            model='cortex/DeepSeek-V4-Flash',
            mcp_config={'execution_method': 'shizuha'},
        )
        container = SimpleNamespace(
            name='agent',
            image='localhost:30500/shizuha-agent-runtime:harness-old',
            image_pull_policy='IfNotPresent',
            command=provisioner._agent_container(
                old_agent, has_broker=True,
            ).command,
            env=[
                provisioner.client.V1EnvVar(
                    name='MODEL', value='cortex/DeepSeek-V4-Flash',
                ),
                provisioner.client.V1EnvVar(
                    name='UNRELATED_ENV', value='preserve-me',
                ),
            ],
            resources=SimpleNamespace(requests={}, limits={}),
        )
        broker = SimpleNamespace(name='broker', image='registry/mcp-auth-proxy:current')
        deployment = SimpleNamespace(
            spec=SimpleNamespace(
                replicas=0,
                template=SimpleNamespace(
                    spec=SimpleNamespace(containers=[container, broker]),
                ),
            ),
        )
        apps.read_namespaced_deployment.return_value = deployment
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())
        agent = _agent(
            image='localhost:30500/shizuha-agent-runtime:harness-new',
            model='gpt-5.6-sol',
            mcp_config={'execution_method': 'codex_app_server'},
        )
        agent._reviewed_runtime_image = agent.image

        # Production order: persist the new harness/model lane, then reconcile
        # the retained Deployment before it is woken.
        self.assertTrue(provisioner.ensure_agent_image(agent))

        self.assertEqual(
            container.image,
            'localhost:30500/shizuha-agent-runtime:harness-new',
        )
        self.assertIn('codex-bridge', ' '.join(container.command))
        self.assertNotIn(' gateway ', f" {' '.join(container.command)} ")
        self.assertEqual(
            {item.name: item for item in container.env}['MODEL'].value,
            'gpt-5.6-sol',
        )
        self.assertEqual(
            {item.name: item for item in container.env}['UNRELATED_ENV'].value,
            'preserve-me',
        )
        self.assertEqual(deployment.spec.replicas, 0)

        # A later fallback-family edit must not retain codex-bridge either.
        agent.model = 'cortex/DeepSeek-V4-Flash'
        agent.mcp_config = {'execution_method': 'shizuha'}
        self.assertTrue(provisioner.ensure_agent_image(agent))
        self.assertIn(' gateway ', f" {' '.join(container.command)} ")
        self.assertNotIn('codex-bridge', ' '.join(container.command))
        self.assertEqual(deployment.spec.replicas, 0)
        self.assertEqual(apps.replace_namespaced_deployment.call_count, 2)

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_runtime_lane_reconcile_preserves_custom_image_command(
        self, mock_k8s, _log,
    ):
        apps = MagicMock()
        custom_command = ['/opt/custom/bin/agent', '--serve']
        container = SimpleNamespace(
            name='agent',
            image='localhost:30500/reviewer-runtime:stable',
            image_pull_policy='IfNotPresent',
            command=list(custom_command),
            env=[],
            resources=SimpleNamespace(requests={}, limits={}),
        )
        deployment = SimpleNamespace(
            spec=SimpleNamespace(
                replicas=1,
                template=SimpleNamespace(
                    spec=SimpleNamespace(containers=[container]),
                ),
            ),
        )
        apps.read_namespaced_deployment.return_value = deployment
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())
        agent = _agent(
            image='localhost:30500/reviewer-runtime:stable',
            model='gpt-5.6-sol',
            mcp_config={'execution_method': 'codex_app_server'},
        )

        self.assertTrue(provisioner.ensure_agent_image(agent))

        self.assertEqual(container.command, custom_command)
        self.assertEqual(deployment.spec.replicas, 1)
        apps.replace_namespaced_deployment.assert_called_once()

    @override_settings(FLEET_BROKER_IMAGE='registry/mcp-auth-proxy:verified')
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_image_convergence_repairs_native_broker_sidecar(self, mock_k8s, _log):
        apps = MagicMock()
        agent_container = SimpleNamespace(
            name='agent', image='registry/shizuha-agent-runtime:current',
            image_pull_policy='IfNotPresent',
            resources=SimpleNamespace(requests={}, limits={}),
        )
        broker = SimpleNamespace(name='broker', image='registry/mcp-auth-proxy:stale')
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            spec=SimpleNamespace(
                template=SimpleNamespace(spec=SimpleNamespace(containers=[agent_container, broker])),
            ),
        )
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())
        agent = _agent(image='registry/shizuha-agent-runtime:current')

        self.assertTrue(provisioner.ensure_agent_image(agent))

        self.assertEqual(broker.image, 'registry/mcp-auth-proxy:verified')
        apps.replace_namespaced_deployment.assert_called_once()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_wake_convergence_repairs_legacy_forgejo_refs(self, mock_k8s, _log):
        apps = MagicMock()
        container = SimpleNamespace(
            name='agent', image='registry/shizuha-agent-runtime:current',
            image_pull_policy='IfNotPresent', env=[],
            resources=SimpleNamespace(requests={}, limits={}),
        )
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container]))),
        )
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())
        agent = _agent(image='registry/shizuha-agent-runtime:current')

        self.assertTrue(provisioner.ensure_agent_image(agent))

        by_name = {item.name: item for item in container.env}
        for name in ('FORGEJO_TOKEN', 'ORIGIN_TOKEN'):
            ref = by_name[name].value_from.secret_key_ref
            self.assertEqual(ref.name, 'testagent-forgejo-token')
            self.assertEqual(ref.key, 'FORGEJO_TOKEN')
            self.assertTrue(ref.optional)
        apps.replace_namespaced_deployment.assert_called_once()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_image_convergence_adds_missing_broker_sidecar(self, mock_k8s, _log):
        """Native seats with sidecar_set broker must grow the sidecar.

        2026-09-02: 10 GLM holders had sidecar_set=[] so Pulse preflight
        401'd and fail-opened into 200k prefix heartbeats.
        """
        apps = MagicMock()
        container = SimpleNamespace(
            name='agent', image='registry/shizuha-agent-runtime:current',
            image_pull_policy='IfNotPresent', env=[],
            resources=SimpleNamespace(requests={}, limits={}),
        )
        spec = SimpleNamespace(containers=[container], volumes=[])
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            spec=SimpleNamespace(template=SimpleNamespace(spec=spec)),
        )
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())
        agent = _agent(
            image='registry/shizuha-agent-runtime:current',
            sidecar_set=['broker'],
        )

        self.assertTrue(provisioner.ensure_agent_image(agent))

        names = [c.name for c in spec.containers]
        self.assertIn('agent', names)
        self.assertIn('broker', names)
        self.assertIn('broker-sock', [getattr(v, 'name', '') for v in spec.volumes])
        apps.replace_namespaced_deployment.assert_called_once()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_wake_convergence_canonicalizes_duplicate_literal_forgejo_aliases(
        self, mock_k8s, _log,
    ):
        apps = MagicMock()
        agent = _agent(image='registry/shizuha-agent-runtime:current')
        desired = provisioner._forgejo_credential_env(agent)
        legacy_env = []
        for entry in desired:
            legacy_env.extend([
                provisioner.client.V1EnvVar(
                    name=entry.name,
                    value='must-not-survive-reconciliation',
                ),
                entry,
            ])
        container = SimpleNamespace(
            name='agent', image=agent.image,
            image_pull_policy='IfNotPresent', env=legacy_env,
            resources=SimpleNamespace(requests={}, limits={}),
        )
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container]))),
        )
        mock_k8s.return_value = (MagicMock(), apps, MagicMock())

        self.assertTrue(provisioner.ensure_agent_image(agent))

        for name in ('FORGEJO_TOKEN', 'ORIGIN_TOKEN'):
            [entry] = [item for item in container.env if item.name == name]
            self.assertIsNone(entry.value)
            ref = entry.value_from.secret_key_ref
            self.assertEqual(ref.name, 'testagent-forgejo-token')
            self.assertEqual(ref.key, 'FORGEJO_TOKEN')
            self.assertTrue(ref.optional)
        apps.replace_namespaced_deployment.assert_called_once()


class FleetUsernameValidationTests(SimpleTestCase):
    """Review #5: DNS-1123 username enforced at persist."""

    def test_validator_rejects_invalid(self):
        for bad in ('Ichi', 'dev_agent', '-x', 'a..b', 'UPPER'):
            with self.assertRaises(ValidationError):
                dns1123_validator(bad)

    def test_validator_accepts_valid(self):
        for good in ('ichi', 'dev-agent', 'agent1'):
            dns1123_validator(good)  # no raise

    def test_save_rejects_invalid_username_before_db(self):
        with self.assertRaises(ValidationError):
            FleetAgent(agent_username='dev_agent').save()


class FleetPlatformDefaultTests(SimpleTestCase):
    """Review #6: platform_url default tracks settings, not a hard-coded IP."""

    @override_settings(SHIZUHA_PLATFORM_URL='http://platform.svc.example')
    def test_platform_url_defaults_from_settings(self):
        self.assertEqual(_default_platform_url(), 'http://platform.svc.example')


# ── aoi round-2 (P1 blast-radius + P2s) ──────────────────────────────────────


class FleetDeprovisionBlastRadiusTests(SimpleTestCase):
    """P1: deprovision must NEVER delete the shared fleet ns, even if `isolated`
    was flipped True after provisioning in the shared ns."""

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_flipped_isolated_does_not_delete_shared_ns(self, mock_k8s, _log):
        core, apps = MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, MagicMock())
        # provisioned in the SHARED ns, then isolated flipped True afterwards
        agent = _agent(namespace='shizuha-fleet', isolated=True)
        agent.save = MagicMock()
        provisioner.deprovision_agent(agent)
        core.delete_namespace.assert_not_called()           # shared ns spared
        # Private browser worker is deleted first (shizuha-browser), then agent
        # Deployment in the shared fleet ns — never the shared namespace itself.
        deploy_calls = apps.delete_namespaced_deployment.call_args_list
        self.assertGreaterEqual(len(deploy_calls), 1)
        self.assertTrue(
            any(
                c.kwargs.get('name') == 'agent-testagent'
                or (c.args and c.args[0] == 'agent-testagent')
                or c.kwargs.get('name') == agent.deployment_name
                for c in deploy_calls
            )
            or any('agent-' in str(c) for c in deploy_calls)
        )
        core.delete_namespaced_secret.assert_called_once()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_genuine_isolated_ns_is_deleted(self, mock_k8s, _log):
        core, apps = MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, MagicMock())
        agent = _agent(agent_username='ichi', namespace='fleet-ichi', isolated=True)
        agent.save = MagicMock()
        provisioner.deprovision_agent(agent)
        core.delete_namespace.assert_called_once()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_daemon_agent_cleans_actual_k3s_objects_when_projected_namespace_is_stale(self, mock_k8s, _log):
        core, apps = MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, MagicMock())
        agent = _agent(agent_username='shizuha-3', namespace='rt-fleet')
        agent.managing_daemon_id = 1
        agent.save = MagicMock()

        provisioner.deprovision_agent(agent)

        apps.delete_namespaced_deployment.assert_any_call(
            name='agent-shizuha-3', namespace='shizuha-fleet',
        )
        core.delete_namespaced_persistent_volume_claim.assert_any_call(
            name='shizuha-3-workspace', namespace='shizuha-fleet',
        )
        core.delete_namespaced_secret.assert_any_call(
            name='shizuha-3-agent-creds', namespace='shizuha-fleet',
        )

    def test_delete_namespace_refuses_shared_ns(self):
        with self.assertRaises(ValueError):
            provisioner._delete_namespace(MagicMock(), provisioner.fleet_namespace())


class FleetServiceCleanupTests(_NoByoProviderGateMixin, SimpleTestCase):
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_expose_http_off_removes_stale_service(self, mock_k8s, _log):
        core, apps = MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, MagicMock())
        agent = _agent(expose_http=False)
        agent.save = MagicMock()
        provisioner.provision_agent(agent, agent_password='x')
        core.delete_namespaced_service.assert_called_once()  # stale Service removed
        core.create_namespaced_service.assert_not_called()

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_grok_voice_keeps_scli_service_without_expose_http(self, mock_k8s, _log):
        core, apps = MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, MagicMock())
        agent = _agent(expose_http=False, model='cortex/grok-voice-think-fast-2.0')
        agent.save = MagicMock()
        provisioner.provision_agent(agent, agent_password='x')
        core.create_namespaced_service.assert_called_once()
        core.delete_namespaced_service.assert_not_called()

    def test_voice_s2s_label_only_on_grok_voice_seats(self):
        self.assertTrue(provisioner.wants_voice_s2s(_agent(model='grok-voice-think-fast-2.0')))
        self.assertIn('shizuha.io/voice-s2s', provisioner._base_labels(_agent(model='cortex/grok-voice-latest')))
        self.assertNotIn('shizuha.io/voice-s2s', provisioner._base_labels(_agent(model='grok-4.6')))


class FleetImmutabilityTests(SimpleTestCase):
    def test_username_immutable_after_provision(self):
        import datetime
        a = _agent()
        a.provisioned_at = datetime.datetime(2026, 6, 8)
        a.agent_username = 'renamed'
        with self.assertRaises(ValidationError):
            a.save()

    def test_isolated_immutable_after_provision(self):
        import datetime
        a = _agent()
        a.provisioned_at = datetime.datetime(2026, 6, 8)
        a.isolated = True
        with self.assertRaises(ValidationError):
            a.save()

    def test_username_length_capped(self):
        with self.assertRaises(ValidationError):
            _agent(agent_username='a' * 48).save()


class FleetSandboxRuntimeClassTests(SimpleTestCase):
    @override_settings(FLEET_SANDBOX_RUNTIME_CLASS='gvisor')
    def test_runtime_class_set_for_sandbox_agent(self):
        dep = _build_deployment(_agent(sandbox_required=True))
        self.assertEqual(dep.spec.template.spec.runtime_class_name, 'gvisor')

    def test_no_runtime_class_for_normal_agent(self):
        dep = _build_deployment()
        self.assertIsNone(dep.spec.template.spec.runtime_class_name)


class FleetPvcSizeChangeTests(SimpleTestCase):
    def test_quantity_bytes(self):
        self.assertEqual(provisioner._quantity_bytes('1Gi'), 1024**3)
        self.assertGreater(provisioner._quantity_bytes('15Gi'), provisioner._quantity_bytes('10Gi'))

    def test_grow_patches_existing_pvc(self):
        core = MagicMock()
        core.create_namespaced_persistent_volume_claim.side_effect = ApiException(status=409)
        core.read_namespaced_persistent_volume_claim.return_value = SimpleNamespace(
            spec=SimpleNamespace(resources=SimpleNamespace(requests={'storage': '10Gi'})),
        )
        provisioner._create_workspace_pvc(core, 'shizuha-fleet', _agent(pvc_size='20Gi'))
        core.patch_namespaced_persistent_volume_claim.assert_called_once()


class FleetRuntimeOptionsTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    @override_settings(CORTEX_BASE_URL='http://cortex:8040', CORTEX_API_KEY='test-key')
    @patch('fleet.views.requests.get')
    def test_cortex_models_are_loaded_as_runtime_options(self, get):
        resp = MagicMock()
        resp.json.return_value = {
            'data': [
                {'id': 'DeepSeek-V4-Flash'},
                {'id': 'Qwen3.6-27B-4bit-MLX'},
            ]
        }
        get.return_value = resp

        models, error = FleetRuntimeOptionsView()._cortex_models()

        self.assertEqual(error, '')
        get.assert_called_once_with(
            'http://cortex:8040/v1/models',
            headers={'Authorization': 'Bearer test-key'},
            timeout=2,
        )
        ids = [item['id'] for item in models]
        self.assertIn('cortex/DeepSeek-V4-Flash', ids)
        self.assertIn('cortex/Qwen3.6-27B-4bit-MLX', ids)
        # Managed fleet Grok is always listed (even if catalog key is resold).
        self.assertIn('cortex/grok-4.5', ids)
        self.assertIn('cortex/grok-4.7', ids)
        self.assertNotIn('cortex/grok-4.6', ids)

    @override_settings(CORTEX_BASE_URL='http://cortex:8040', CORTEX_API_KEY='test-key')
    @patch('fleet.views.requests.get')
    def test_cortex_models_surface_availability_and_provider_fields(self, get):
        """HIVE-1863 (S2): the catalog consumer surfaces available/status and
        x_cortex.{providers, auth_classes, quality_tier} so the agent-editor
        dropdown can show provider/auth_class + availability."""
        resp = MagicMock()
        resp.json.return_value = {
            'data': [
                {
                    'id': 'DeepSeek-V4-Flash',
                    'available': True,
                    'status': 'available',
                    'x_cortex': {
                        'providers': ['cortex-self'],
                        'auth_classes': ['api_key'],
                        'quality_tier': 'self_hosted',
                    },
                },
                {
                    'id': 'Qwen3.6-27B-4bit-MLX',
                    'available': False,
                    'status': 'maintenance',
                    'maintenance_message': 'rolling',
                    'x_cortex': {
                        'providers': ['cortex-self'],
                        'auth_classes': ['api_key'],
                        'quality_tier': 'self_hosted',
                    },
                },
            ]
        }
        get.return_value = resp

        models, error = FleetRuntimeOptionsView()._cortex_models()

        self.assertEqual(error, '')
        by_id = {m['id']: m for m in models}
        ds = by_id['cortex/DeepSeek-V4-Flash']
        self.assertTrue(ds['available'])
        self.assertEqual(ds['status'], 'available')
        self.assertEqual(ds['providers'], ['cortex-self'])
        self.assertEqual(ds['auth_classes'], ['api_key'])
        self.assertEqual(ds['quality_tier'], 'self_hosted')
        qw = by_id['cortex/Qwen3.6-27B-4bit-MLX']
        self.assertFalse(qw['available'])
        self.assertEqual(qw['status'], 'maintenance')
        # Managed fleet Grok entries carry default availability/provider fields.
        grok = by_id['cortex/grok-4.7']
        self.assertTrue(grok['available'])
        self.assertEqual(grok['providers'], ['xai-grok'])
        self.assertEqual(grok['auth_classes'], ['oauth_subscription'])

    @override_settings(CORTEX_BASE_URL='http://cortex:8040', CORTEX_API_KEY='resold-key')
    @patch('fleet.views.requests.get')
    def test_managed_grok_seeded_when_catalog_omits_internal_offers(self, get):
        resp = MagicMock()
        # Live resold key only sees self-hosted backends — no grok-4.5.
        resp.json.return_value = {'data': [{'id': 'DeepSeek-V4-Flash'}]}
        get.return_value = resp

        models, error = FleetRuntimeOptionsView()._cortex_models()

        self.assertEqual(error, '')
        ids = [item['id'] for item in models]
        self.assertIn('cortex/grok-4.5', ids)
        self.assertIn('cortex/grok-4.7', ids)
        self.assertNotIn('cortex/grok-4.6', ids)
        self.assertTrue(any(m['provider_id'] == 'grok-4.7' for m in models))
        self.assertFalse(any(m['provider_id'] == 'grok-4.6' for m in models))
        self.assertTrue(any(m['provider_id'] == 'grok-4.5' for m in models))

    @override_settings(CORTEX_BASE_URL='http://cortex:8040', CORTEX_API_KEY='test-key')
    @patch('fleet.views.get_runtime_skills', return_value=[])
    @patch('fleet.views.list_runtime_agents', return_value=[])
    @patch('fleet.views.requests.get')
    def test_runtime_options_include_method_model_catalogs(self, get, _agents, _skills):
        resp = MagicMock()
        resp.json.return_value = {'data': [{'id': 'DeepSeek-V4-Flash'}]}
        get.return_value = resp

        req = self.factory.get('/v1/fleet/runtime-options/')
        force_authenticate(req, user=SimpleNamespace(is_staff=True, is_authenticated=True))
        response = FleetRuntimeOptionsView.as_view()(req)

        self.assertEqual(response.status_code, 200)
        self.assertIn('runtime_methods', response.data)
        self.assertIn('method_models', response.data)
        codex_models = response.data['method_models']['codex_app_server']
        self.assertIn('gpt-5.5', [item['id'] for item in codex_models])
        self.assertIn('gpt-5.3-codex-spark', [item['id'] for item in codex_models])
        shizuha_ids = [item['id'] for item in response.data['method_models']['shizuha']]
        self.assertIn('cortex/DeepSeek-V4-Flash', shizuha_ids)
        self.assertIn('cortex/grok-4.5', shizuha_ids)
        self.assertIn('cortex/grok-4.7', shizuha_ids)
        self.assertNotIn('cortex/grok-4.6', shizuha_ids)
        self.assertIn('cortex/grok-voice-think-fast-2.0', shizuha_ids)
        method_ids = [m['id'] for m in response.data['runtime_methods']]
        self.assertIn('grok_build', method_ids)
        grok_models = response.data['method_models']['grok_build']
        grok_ids = [item['id'] for item in grok_models]
        self.assertIn('cortex/grok-4.7', grok_ids)
        self.assertNotIn('cortex/grok-4.6', grok_ids)
        self.assertIn('cortex/grok-4.5', grok_ids)
        self.assertNotIn('cortex/grok-voice-think-fast-2.0', grok_ids)


class FleetWorkspacePermissionTests(SimpleTestCase):
    """PLAT-146 proof finding: the agent (uid 1000) must be able to write the
    Longhorn workspace PVC — fsGroup makes the mounted volume group-1000."""

    def test_pod_has_fsgroup_1000(self):
        spec = _build_deployment().spec.template.spec
        self.assertEqual(spec.security_context.fs_group, 1000)

    def test_pod_fsgroup_change_policy_on_root_mismatch(self):
        """PLAT-1173 / ShizuhaMcpAuthProxyUnready (09-20): the default Always
        policy recursively chowns the workspace PVC on every mount; after a
        Longhorn attach migration that walk raced a settling remount, failed
        lstat ENOENT deep inside .venv, aborted the pod's volume-ownership
        phase, and left the broker-sock emptyDir root:root 755 — the broker
        could not bind proxy.sock (not_owner) and crash-looped the seat
        unready. OnRootMismatch skips the recursion when the root already
        matches, removing both the walk and the race."""
        spec = _build_deployment().spec.template.spec
        self.assertEqual(spec.security_context.fs_group_change_policy, 'OnRootMismatch')


class FleetBrokerWiringTests(SimpleTestCase):
    """PLAT-168: real mcp-auth-proxy broker sidecar wiring + shared UDS."""

    @override_settings(
        FLEET_BROKER_IMAGE='localhost:30500/mcp-auth-proxy:v2026.06.05-6f375d8',
        SHIZUHA_PLATFORM_URL='http://platform',
    )
    def test_broker_image_and_mandatory_envs(self):
        dep = _build_deployment()
        broker = _container(dep, 'broker')
        self.assertEqual(broker.image, 'localhost:30500/mcp-auth-proxy:v2026.06.05-6f375d8')
        env = {e.name: e.value for e in broker.env}
        self.assertEqual(env['MCP_AUTH_PROXY_EXPECTED_AGENT_UID'], '1000')  # fail-closed mandatory
        self.assertEqual(env['MCP_AUTH_PROXY_ID_BASE_URL'], 'http://platform')
        self.assertEqual(env['MCP_AUTH_PROXY_SOCKET'], '/run/shizuha/mcp-auth-proxy/proxy.sock')
        # AGENT_PASSWORD reaches the broker ONLY via the identity Secret envFrom
        srcs = [s.secret_ref.name for s in broker.env_from if s.secret_ref]
        self.assertIn('agent-testagent-identity', srcs)

    @override_settings(
        FLEET_COORDINATOR_MODEL_TOKEN_URL='http://hive.shizuha-hive.svc.cluster.local:8030/hive/api/v1/coordinator/model-token',
        FLEET_COORDINATOR_TOKEN_SECRET_NAME='hive-coordinator-broker-token',
        FLEET_COORDINATOR_TOKEN_SECRET_KEY='token',
    )
    def test_coordinator_model_token_wires_broker_and_fail_closed_agent(self):
        dep = _build_deployment()

        agent = _container(dep, 'agent')
        agent_env = {e.name: e.value for e in agent.env}
        self.assertEqual(agent_env['CLAUDE_BRIDGE_REQUIRE_BROKER_MODEL_TOKEN'], '1')
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', agent_env)

        broker = _container(dep, 'broker')
        broker_env = {e.name: e for e in broker.env}
        self.assertEqual(
            broker_env['MCP_AUTH_PROXY_COORDINATOR_URL'].value,
            'http://hive.shizuha-hive.svc.cluster.local:8030/hive/api/v1/coordinator/model-token',
        )
        token_ref = broker_env['MCP_AUTH_PROXY_COORDINATOR_TOKEN'].value_from.secret_key_ref
        self.assertEqual(token_ref.name, 'hive-coordinator-broker-token')
        self.assertEqual(token_ref.key, 'token')

    @override_settings(
        FLEET_COORDINATOR_MODEL_TOKEN_URL='http://hive.shizuha-hive.svc.cluster.local:8030/hive/api/v1/coordinator/model-token',
        FLEET_COORDINATOR_TOKEN_SECRET_NAME='',
    )
    def test_coordinator_model_token_requires_secret_before_fail_closed(self):
        dep = _build_deployment()
        agent_env = {e.name for e in _container(dep, 'agent').env}
        broker_env = {e.name for e in _container(dep, 'broker').env}

        self.assertNotIn('CLAUDE_BRIDGE_REQUIRE_BROKER_MODEL_TOKEN', agent_env)
        self.assertNotIn('MCP_AUTH_PROXY_COORDINATOR_URL', broker_env)
        self.assertNotIn('MCP_AUTH_PROXY_COORDINATOR_TOKEN', broker_env)

    def test_shared_uds_mounted_in_both_containers(self):
        dep = _build_deployment()
        spec = dep.spec.template.spec
        for cname in ('agent', 'broker'):
            mounts = {m.name: m.mount_path for m in _container(dep, cname).volume_mounts}
            self.assertEqual(mounts['broker-sock'], '/run/shizuha/mcp-auth-proxy')
        vol = next(v for v in spec.volumes if v.name == 'broker-sock')
        self.assertIsNotNone(vol.empty_dir)

    def test_agent_readiness_gated_on_broker_readyz(self):
        agent_c = _container(_build_deployment(), 'agent')
        probe = agent_c.readiness_probe
        self.assertIsNotNone(probe)
        cmd = ' '.join(probe._exec.command)
        self.assertIn('/readyz', cmd)
        self.assertIn('/run/shizuha/mcp-auth-proxy/proxy.sock', cmd)
        env = {e.name: e.value for e in agent_c.env}
        self.assertEqual(env['MCP_AUTH_PROXY_SOCKET'], '/run/shizuha/mcp-auth-proxy/proxy.sock')

    def test_no_broker_no_socket_no_probe(self):
        dep = _build_deployment(_agent(sidecar_set=[]))
        spec = dep.spec.template.spec
        self.assertEqual([c.name for c in spec.containers], ['agent'])
        self.assertNotIn('broker-sock', [v.name for v in spec.volumes])
        self.assertIsNone(_container(dep, 'agent').readiness_probe)

    def test_dind_sidecar_is_declarative_and_wires_agent_docker_cli(self):
        dep = _build_deployment(_agent(sidecar_set=['broker', 'dind']))
        spec = dep.spec.template.spec
        agent_c = _container(dep, 'agent')
        dind_c = _container(dep, 'dind')

        self.assertIn('docker-cli', [c.name for c in spec.init_containers])
        self.assertTrue(dind_c.security_context.privileged)
        self.assertEqual(dind_c.command, ['dockerd'])
        self.assertIn('--host=unix:///var/run/docker.sock', dind_c.args)

        agent_env = {e.name: e.value for e in agent_c.env}
        self.assertEqual(agent_env['DOCKER_HOST'], 'unix:///var/run/docker.sock')
        mounts = {m.name: m.mount_path for m in agent_c.volume_mounts}
        self.assertEqual(mounts['docker-run'], '/var/run')
        self.assertEqual(mounts['docker-bin'], '/usr/local/bin/docker')
        self.assertIn('docker-graph', [v.name for v in spec.volumes])
        self.assertIn('docker-run', [v.name for v in spec.volumes])
        self.assertIn('docker-bin', [v.name for v in spec.volumes])
        # The pod-level uid is relaxed only for the DinD sidecar; agent/broker
        # still carry their own non-root container security contexts.
        self.assertIsNone(spec.security_context.run_as_user)
        self.assertEqual(agent_c.security_context.run_as_user, 1000)

    def test_without_dind_no_privileged_container_or_docker_wiring(self):
        dep = _build_deployment()
        spec = dep.spec.template.spec
        self.assertIsNone(spec.init_containers)
        self.assertNotIn('dind', [c.name for c in spec.containers])
        self.assertNotIn('docker-run', [v.name for v in spec.volumes])
        agent_c = _container(dep, 'agent')
        self.assertNotIn('DOCKER_HOST', {e.name for e in agent_c.env})
        self.assertTrue(spec.security_context.run_as_non_root)


class FleetBrokerStartGateTests(SimpleTestCase):
    """PLAT-146 e2e finding: agent must wait for broker /readyz before boot
    (avoids the /token startup race that disables Connect)."""

    def test_agent_waits_for_broker_readyz_then_execs_entrypoint(self):
        agent_c = _container(_build_deployment(), 'agent')
        cmd = ' '.join(agent_c.command)
        self.assertIn('/readyz', cmd)
        self.assertIn('/run/shizuha/mcp-auth-proxy/proxy.sock', cmd)
        self.assertIn('agent-runtime-entrypoint.sh', cmd)

    def test_no_broker_still_pins_cwd_to_workspace_pvc(self):
        """Scout 2026-09-01: no-broker native image used overlay cwd and
        created a new eternal session after hibernate (93K → 12K, 0% cache).
        """
        agent_c = _container(_build_deployment(_agent(sidecar_set=[])), 'agent')
        cmd = ' '.join(agent_c.command or [])
        self.assertIn('--cwd /home/agent/.shizuha', cmd)
        self.assertIn('agent-runtime-entrypoint.sh', cmd)
        self.assertNotIn('/readyz', cmd)


class FleetCustomImageStartGateScopingTests(SimpleTestCase):
    """Review P2: the start-gate (curl /readyz probe + hardcoded entrypoint
    command) assumes the DEFAULT runtime image — must NOT be applied to a custom
    image, but the generic broker wiring (UDS mount + socket env) still should."""

    def test_custom_image_keeps_broker_wiring_but_no_gate(self):
        dep = _build_deployment(_agent(image='localhost:30500/claw-special:1'))
        agent_c = _container(dep, 'agent')
        # generic broker wiring present:
        self.assertEqual(agent_c.image, 'localhost:30500/claw-special:1')
        mounts = {m.name for m in agent_c.volume_mounts}
        self.assertIn('broker-sock', mounts)
        env = {e.name for e in agent_c.env}
        self.assertIn('MCP_AUTH_PROXY_SOCKET', env)
        # default-image-specific gate NOT applied:
        self.assertIsNone(agent_c.command)
        self.assertIsNone(agent_c.readiness_probe)

    def test_default_image_gets_the_gate(self):
        agent_c = _container(_build_deployment(), 'agent')  # _agent() has no custom image
        self.assertIsNotNone(agent_c.command)
        self.assertIn('agent-runtime-entrypoint.sh', ' '.join(agent_c.command))
        self.assertIsNotNone(agent_c.readiness_probe)


class FleetIsolationNetworkPolicyTests(SimpleTestCase):
    """PLAT-174 Codex P1-A: isolated agent must receive the full baseline policy
    set (default-deny + DNS + platform + LLM + Cortex) alongside SSH policies.
    Emitting only SSH policies makes the pod egress-isolated and drops DNS/platform."""

    def _call_create_policies(self):
        networking_v1 = MagicMock()
        provisioner._create_ssh_egress_network_policies(
            networking_v1, 'fleet-ichi', _agent(agent_username='ichi', isolated=True)
        )
        return networking_v1

    def _created_policy_names(self):
        networking_v1 = self._call_create_policies()
        return [
            call.args[1].metadata.name
            for call in networking_v1.create_namespaced_network_policy.call_args_list
        ]

    def test_emits_all_seven_policies(self):
        names = self._created_policy_names()
        expected = {
            'default-deny-all',
            'allow-dns-egress',
            'allow-platform-egress',
            'allow-llm-https-egress',
            'allow-cortex-llm-egress',
            'allow-hostplane-ssh-egress',
            'allow-hostplane-ssh-bootstrap-lan',
        }
        self.assertEqual(set(names), expected, f'Missing policies: {expected - set(names)}')

    def test_default_deny_selects_all_pods(self):
        networking_v1 = self._call_create_policies()
        calls = {
            c.args[1].metadata.name: c.args[1]
            for c in networking_v1.create_namespaced_network_policy.call_args_list
        }
        deny = calls['default-deny-all']
        self.assertEqual(deny.spec.pod_selector.match_labels, None)  # {} selects all
        self.assertIn('Ingress', deny.spec.policy_types)
        self.assertIn('Egress', deny.spec.policy_types)

    def test_platform_egress_reaches_hive_coordinator_and_id(self):
        networking_v1 = self._call_create_policies()
        calls = {
            c.args[1].metadata.name: c.args[1]
            for c in networking_v1.create_namespaced_network_policy.call_args_list
        }
        policy = calls['allow-platform-egress']
        port_sets = []
        namespaces = []
        for rule in policy.spec.egress:
            port_sets.append({p.port for p in (rule.ports or [])})
            for peer in rule.to or []:
                labels = (peer.namespace_selector.match_labels or {})
                namespaces.append(labels.get('kubernetes.io/metadata.name'))
        self.assertIn('shizuha', namespaces)
        self.assertIn('shizuha-hive', namespaces)
        merged = set().union(*port_sets)
        for required in (80, 8001, 8019, 8030):
            self.assertIn(required, merged)

    def test_ssh_bootstrap_lan_has_retire_label(self):
        networking_v1 = self._call_create_policies()
        calls = {
            c.args[1].metadata.name: c.args[1]
            for c in networking_v1.create_namespaced_network_policy.call_args_list
        }
        bootstrap = calls['allow-hostplane-ssh-bootstrap-lan']
        self.assertEqual(bootstrap.metadata.labels.get('shizuha.io/plat178-retire'), 'true')

    def test_idempotent_on_409(self):
        from kubernetes.client.exceptions import ApiException
        networking_v1 = MagicMock()
        networking_v1.create_namespaced_network_policy.side_effect = ApiException(status=409)
        # expose_http=False (default) → 7 base policies via create→replace, plus
        # a delete attempt for the stale ingress allow (returns 404 → ignored).
        networking_v1.delete_namespaced_network_policy.side_effect = ApiException(status=404)
        provisioner._create_ssh_egress_network_policies(
            networking_v1, 'fleet-ichi', _agent(agent_username='ichi', isolated=True)
        )
        self.assertEqual(
            networking_v1.replace_namespaced_network_policy.call_count,
            7,  # one replace per base policy
        )

    def test_expose_http_true_emits_ingress_allow(self):
        networking_v1 = MagicMock()
        provisioner._create_ssh_egress_network_policies(
            networking_v1, 'fleet-ichi',
            _agent(agent_username='ichi', isolated=True, expose_http=True, http_port=8080),
        )
        created_names = [
            call.args[1].metadata.name
            for call in networking_v1.create_namespaced_network_policy.call_args_list
        ]
        self.assertIn('allow-http-ingress', created_names)
        # Total: 7 base + 1 ingress allow
        self.assertEqual(len(created_names), 8)
        ingress_policy = next(
            call.args[1]
            for call in networking_v1.create_namespaced_network_policy.call_args_list
            if call.args[1].metadata.name == 'allow-http-ingress'
        )
        self.assertEqual(ingress_policy.spec.policy_types, ['Ingress'])
        [rule] = ingress_policy.spec.ingress
        [port] = rule.ports
        self.assertEqual(port.port, 8080)

    def test_expose_http_false_removes_stale_ingress_allow(self):
        networking_v1 = MagicMock()
        provisioner._create_ssh_egress_network_policies(
            networking_v1, 'fleet-ichi',
            _agent(agent_username='ichi', isolated=True, expose_http=False),
        )
        # delete must be called (stale ingress policy cleanup)
        networking_v1.delete_namespaced_network_policy.assert_called_once_with(
            'allow-http-ingress', 'fleet-ichi'
        )
        # create must NOT include the ingress allow
        created_names = [
            c.args[1].metadata.name
            for c in networking_v1.create_namespaced_network_policy.call_args_list
        ]
        self.assertNotIn('allow-http-ingress', created_names)


class BrowserWorkerPodSpecTests(SimpleTestCase):
    def test_amd64_browser_worker_tolerates_s1_workstation_taint(self):
        spec = provisioner._browser_worker_pod_spec(
            _agent(agent_username='plat536-smoke', arch='amd64'),
            'localhost:30500/shizuha-browser:test',
        )

        self.assertEqual(spec.node_selector, {'kubernetes.io/arch': 'amd64'})
        self.assertIsNotNone(spec.tolerations)
        by_key = {t.key: t for t in spec.tolerations}
        # s1 workstation taint (PLAT-536).
        self.assertIn('node.shizuha/workstation', by_key)
        tol = by_key['node.shizuha/workstation']
        self.assertEqual(tol.operator, 'Equal')
        self.assertEqual(tol.value, 'true')
        self.assertEqual(tol.effect, 'NoSchedule')
        # s1 also carries the operator=unschedulable maintenance taint
        # (PLAT-5936); browser-worker PVCs are local-path pinned to s1, so the
        # worker must tolerate it or it stays Unschedulable and the agent's
        # browser MCP never comes up (ShizuhaAgentMcpReconnectWedge).
        self.assertIn('operator', by_key)
        self.assertEqual(by_key['operator'].value, 'unschedulable')
        self.assertEqual(by_key['operator'].effect, 'NoSchedule')

    def test_arm64_agent_still_gets_amd64_browser_worker(self):
        # Browser image is amd64-only; private workers always pin amd64 +
        # workstation taint even when the owning fleet agent is arm64.
        spec = provisioner._browser_worker_pod_spec(
            _agent(agent_username='arm-browser', arch='arm64'),
            'localhost:30500/shizuha-browser:test',
        )

        self.assertEqual(spec.node_selector, {'kubernetes.io/arch': 'amd64'})
        self.assertIsNotNone(spec.tolerations)
        keys = {t.key for t in spec.tolerations}
        self.assertIn('node.shizuha/workstation', keys)
        self.assertIn('operator', keys)


class FleetProvisionOrderingTests(_NoByoProviderGateMixin, SimpleTestCase):
    """Codex P1 (2026-06-11): NetworkPolicies must be applied BEFORE the Deployment
    so the pod never starts in a window without isolation (default-deny is live
    before the scheduler can place the container)."""

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_policies_created_before_deployment_for_isolated_agent(self, mock_k8s, _log):
        core, apps, networking = MagicMock(), MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, networking)

        call_order = []
        orig_policies = provisioner._create_ssh_egress_network_policies
        orig_deploy = provisioner._create_deployment

        def track_policies(nw, ns, agent):
            call_order.append('policies')
            orig_policies(nw, ns, agent)

        def track_deploy(ap, ns, agent):
            call_order.append('deployment')
            orig_deploy(ap, ns, agent)

        agent = _agent(agent_username='ichi', isolated=True)
        agent.save = MagicMock()

        with patch.object(provisioner, '_create_ssh_egress_network_policies', track_policies), \
             patch.object(provisioner, '_create_deployment', track_deploy):
            provisioner.provision_agent(agent, agent_password='x')

        self.assertIn('policies', call_order)
        self.assertIn('deployment', call_order)
        self.assertLess(
            call_order.index('policies'),
            call_order.index('deployment'),
            'NetworkPolicies must be applied before Deployment creation',
        )

    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_non_isolated_agent_skips_policies(self, mock_k8s, _log):
        core, apps, networking = MagicMock(), MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, networking)

        agent = _agent(isolated=False)
        agent.save = MagicMock()
        provisioner.provision_agent(agent, agent_password='x')

        networking.create_namespaced_network_policy.assert_not_called()

    @override_settings(SHIZUHA_PLATFORM_URL='http://platform', HIVE_ID_FLEET_PROVISIONER_TOKEN='scoped')
    @patch('fleet.provisioner._trigger_onboarding_async')
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_auto_password_writes_secret_before_identity(self, mock_k8s, _log, _onboard):
        core, apps, networking = MagicMock(), MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, networking)
        agent = _agent()
        agent.save = MagicMock()
        call_order = []

        with patch.object(provisioner, '_generate_agent_password', return_value='generated!A1'), \
             patch.object(provisioner, '_read_identity_secret_snapshot', return_value=None), \
             patch.object(provisioner, '_create_identity_secret', side_effect=lambda *a: call_order.append('secret')), \
             patch.object(provisioner, '_provision_platform_identity', side_effect=lambda *a: call_order.append('id') or True):
            ok = provisioner.provision_agent(agent)

        self.assertTrue(ok)
        self.assertLess(call_order.index('secret'), call_order.index('id'))

    @override_settings(SHIZUHA_PLATFORM_URL='http://platform', HIVE_ID_FLEET_PROVISIONER_TOKEN='scoped')
    @patch('fleet.provisioner._create_deployment')
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_identity_failure_restores_first_time_secret_and_stops(self, mock_k8s, _log, mock_deploy):
        core, apps, networking = MagicMock(), MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, networking)
        agent = _agent()
        agent.save = MagicMock()

        with patch.object(provisioner, '_generate_agent_password', return_value='new-generated!A1'), \
             patch.object(provisioner, '_read_identity_secret_snapshot', return_value=None) as read_snapshot, \
             patch.object(provisioner, '_create_identity_secret') as write_secret, \
             patch.object(provisioner, '_provision_platform_identity', return_value=False), \
             patch.object(provisioner, '_restore_identity_secret') as restore_secret:
            ok = provisioner.provision_agent(agent)

        self.assertFalse(ok)
        read_snapshot.assert_called_once_with(core, 'shizuha-fleet', agent)
        write_secret.assert_called_once_with(core, 'shizuha-fleet', agent, 'new-generated!A1', '')
        restore_secret.assert_called_once_with(core, 'shizuha-fleet', agent, None)
        mock_deploy.assert_not_called()
        self.assertEqual(agent.status, FleetAgent.Status.FAILED)

    @override_settings(SHIZUHA_PLATFORM_URL='http://platform', HIVE_ID_FLEET_PROVISIONER_TOKEN='scoped')
    @patch('fleet.provisioner._trigger_onboarding_async')
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_existing_secret_auto_reprovision_reuses_password_without_secret_write(self, mock_k8s, _log, _onboard):
        core, apps, networking = MagicMock(), MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, networking)
        snapshot = SimpleNamespace(
            data={'AGENT_PASSWORD': base64.b64encode(b'existing-pw!A1').decode()},
            metadata=SimpleNamespace(labels={'existing': 'true'}),
            type='Opaque',
        )
        agent = _agent()
        agent.save = MagicMock()

        with patch.object(provisioner, '_generate_agent_password') as generate_password, \
             patch.object(provisioner, '_read_identity_secret_snapshot', return_value=snapshot) as read_snapshot, \
             patch.object(provisioner, '_create_identity_secret') as write_secret, \
             patch.object(provisioner, '_provision_platform_identity', return_value=True) as provision_identity:
            ok = provisioner.provision_agent(agent)

        self.assertTrue(ok)
        read_snapshot.assert_called_once_with(core, 'shizuha-fleet', agent)
        generate_password.assert_not_called()
        write_secret.assert_not_called()
        provision_identity.assert_called_once_with(agent, 'existing-pw!A1', 'http://platform')

    @override_settings(SHIZUHA_PLATFORM_URL='http://platform', HIVE_ID_FLEET_PROVISIONER_TOKEN='scoped')
    @patch('fleet.provisioner._create_deployment')
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_existing_secret_identity_failure_leaves_secret_unchanged_and_stops(self, mock_k8s, _log, mock_deploy):
        core, apps, networking = MagicMock(), MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, networking)
        snapshot = SimpleNamespace(
            data={'AGENT_PASSWORD': base64.b64encode(b'existing-pw!A1').decode()},
            metadata=SimpleNamespace(labels={'existing': 'true'}),
            type='Opaque',
        )
        agent = _agent()
        agent.save = MagicMock()

        with patch.object(provisioner, '_generate_agent_password') as generate_password, \
             patch.object(provisioner, '_read_identity_secret_snapshot', return_value=snapshot), \
             patch.object(provisioner, '_create_identity_secret') as write_secret, \
             patch.object(provisioner, '_provision_platform_identity', return_value=False) as provision_identity, \
             patch.object(provisioner, '_restore_identity_secret') as restore_secret:
            ok = provisioner.provision_agent(agent)

        self.assertFalse(ok)
        generate_password.assert_not_called()
        write_secret.assert_not_called()
        provision_identity.assert_called_once_with(agent, 'existing-pw!A1', 'http://platform')
        restore_secret.assert_not_called()
        mock_deploy.assert_not_called()
        self.assertEqual(agent.status, FleetAgent.Status.FAILED)

    @override_settings(SHIZUHA_PLATFORM_URL='http://platform', HIVE_ID_FLEET_PROVISIONER_TOKEN='scoped')
    @patch('fleet.provisioner._provision_platform_identity')
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_secret_write_failure_never_updates_identity(self, mock_k8s, _log, mock_identity):
        core, apps, networking = MagicMock(), MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, networking)
        agent = _agent()
        agent.save = MagicMock()

        with patch.object(provisioner, '_generate_agent_password', return_value='generated!A1'), \
             patch.object(provisioner, '_read_identity_secret_snapshot', return_value=None), \
             patch.object(provisioner, '_create_identity_secret', side_effect=ApiException(status=500, reason='boom')):
            ok = provisioner.provision_agent(agent)

        self.assertFalse(ok)
        mock_identity.assert_not_called()


# ── PLAT-153 / HLD §11 R1: gVisor-Job sandbox substitute ─────────────────────
from fleet import sandbox  # noqa: E402


def _sandbox_clients():
    """(core, batch, rbac, net) MagicMocks — matches sandbox.get_sandbox_clients()."""
    return MagicMock(), MagicMock(), MagicMock(), MagicMock()


class FleetSandboxNamespaceTests(SimpleTestCase):
    """The per-agent sandbox ns IS the security boundary — assert it's tight."""

    @patch('fleet.sandbox.get_sandbox_clients')
    def test_ensure_creates_full_boundary(self, mock_clients):
        core, batch, rbac, net = _sandbox_clients()
        mock_clients.return_value = (core, batch, rbac, net)
        ns = sandbox.ensure_sandbox_namespace(_agent())
        self.assertEqual(ns, 'sbx-testagent')
        core.create_namespace.assert_called_once()
        core.create_namespaced_service_account.assert_called_once()
        rbac.create_namespaced_role.assert_called_once()
        rbac.create_namespaced_role_binding.assert_called_once()
        net.create_namespaced_network_policy.assert_called_once()
        core.create_namespaced_resource_quota.assert_called_once()
        core.create_namespaced_limit_range.assert_called_once()

    @patch('fleet.sandbox.get_sandbox_clients')
    def test_runner_sa_token_not_mounted(self, mock_clients):
        core, batch, rbac, net = _sandbox_clients()
        mock_clients.return_value = (core, batch, rbac, net)
        sandbox.ensure_sandbox_namespace(_agent())
        _ns, sa = core.create_namespaced_service_account.call_args.args
        self.assertFalse(sa.automount_service_account_token)

    @patch('fleet.sandbox.get_sandbox_clients')
    def test_role_is_namespaced_jobs_only_no_secrets(self, mock_clients):
        core, batch, rbac, net = _sandbox_clients()
        mock_clients.return_value = (core, batch, rbac, net)
        sandbox.ensure_sandbox_namespace(_agent())
        # create_namespaced_role (not cluster_role) → the grant is ns-scoped.
        _ns, role = rbac.create_namespaced_role.call_args.args
        resources = {r for rule in role.rules for r in rule.resources}
        self.assertEqual(resources, {'jobs', 'pods', 'pods/log'})
        self.assertNotIn('secrets', resources)
        rbac.create_cluster_role.assert_not_called()

    @patch('fleet.sandbox.get_sandbox_clients')
    def test_default_deny_network_policy(self, mock_clients):
        core, batch, rbac, net = _sandbox_clients()
        mock_clients.return_value = (core, batch, rbac, net)
        sandbox.ensure_sandbox_namespace(_agent())
        _ns, np = net.create_namespaced_network_policy.call_args.args
        self.assertEqual(sorted(np.spec.policy_types), ['Egress', 'Ingress'])
        self.assertIsNone(np.spec.egress)   # no egress rules = deny all egress
        self.assertIsNone(np.spec.ingress)

    @patch('fleet.sandbox.get_sandbox_clients')
    def test_ensure_is_idempotent_on_409(self, mock_clients):
        core, batch, rbac, net = _sandbox_clients()
        conflict = ApiException(status=409)
        core.create_namespace.side_effect = conflict
        core.create_namespaced_service_account.side_effect = conflict
        rbac.create_namespaced_role.side_effect = conflict
        rbac.create_namespaced_role_binding.side_effect = conflict
        net.create_namespaced_network_policy.side_effect = conflict
        core.create_namespaced_resource_quota.side_effect = conflict
        core.create_namespaced_limit_range.side_effect = conflict
        mock_clients.return_value = (core, batch, rbac, net)
        # Must not raise — 409 AlreadyExists is swallowed.
        self.assertEqual(sandbox.ensure_sandbox_namespace(_agent()), 'sbx-testagent')


class FleetSandboxJobTests(SimpleTestCase):
    @override_settings(FLEET_SANDBOX_RUNTIME_CLASS='gvisor')
    def test_job_security_posture(self):
        job = sandbox.build_exec_job(
            _agent(), name='exec-1', image='reg/exec:1', command=['python', '-c', 'print(1)'])
        ps = job.spec.template.spec
        self.assertEqual(ps.runtime_class_name, 'gvisor')
        self.assertEqual(ps.restart_policy, 'Never')
        self.assertFalse(ps.automount_service_account_token)
        self.assertEqual(ps.service_account_name, 'sandbox-runner')
        self.assertEqual(ps.node_selector, {'node.shizuha/sandbox': 'true'})
        self.assertEqual(ps.tolerations[0].key, 'node.shizuha/sandbox')
        self.assertTrue(ps.security_context.run_as_non_root)
        # Job-level runaway/self-reap guards
        self.assertEqual(job.spec.backoff_limit, 0)
        self.assertEqual(job.spec.active_deadline_seconds, 900)
        self.assertTrue(job.spec.ttl_seconds_after_finished)
        # Container hardening
        c = ps.containers[0]
        self.assertFalse(c.security_context.allow_privilege_escalation)
        self.assertTrue(c.security_context.read_only_root_filesystem)
        self.assertEqual(c.security_context.capabilities.drop, ['ALL'])
        # Ephemeral storage only (emptyDir, no PVC)
        self.assertTrue(all(v.empty_dir is not None for v in ps.volumes))

    @override_settings(FLEET_SANDBOX_RUNTIME_CLASS='gvisor')
    @patch('fleet.sandbox.get_sandbox_clients')
    def test_dispatch_creates_job_and_egress_policy(self, mock_clients):
        core, batch, rbac, net = _sandbox_clients()
        mock_clients.return_value = (core, batch, rbac, net)
        name = sandbox.dispatch_exec_job(
            _agent(sandbox_required=True), name='exec-2', image='reg/exec:1',
            command=['echo', 'hi'], egress_cidrs=['10.0.0.0/8'])
        self.assertEqual(name, 'exec-2')
        batch.create_namespaced_job.assert_called_once()
        # egress allowlist NetworkPolicy created (DNS + the CIDR)
        egress_calls = [c for c in net.create_namespaced_network_policy.call_args_list]
        self.assertTrue(any(
            getattr(call.args[1].metadata, 'name', '') == 'egress-exec-2'
            for call in egress_calls))

    @override_settings(FLEET_SANDBOX_RUNTIME_CLASS='gvisor')
    @patch('fleet.sandbox.get_sandbox_clients')
    def test_dispatch_no_network_by_default(self, mock_clients):
        core, batch, rbac, net = _sandbox_clients()
        mock_clients.return_value = (core, batch, rbac, net)
        sandbox.dispatch_exec_job(
            _agent(sandbox_required=True), name='exec-3', image='reg/exec:1')
        # Only the ns default-deny policy — no per-Job egress allowlist.
        names = [call.args[1].metadata.name
                 for call in net.create_namespaced_network_policy.call_args_list]
        self.assertNotIn('egress-exec-3', names)


class FleetSandboxSecurityHardeningTests(SimpleTestCase):
    """PLAT-153 review round: 2 P1s (DNS egress, ephemeral storage) + P2s."""

    def test_job_bounds_ephemeral_storage_and_emptydir(self):
        job = sandbox.build_exec_job(_agent(), name='exec-x', image='img:1')
        ps = job.spec.template.spec
        self.assertIn('ephemeral-storage', ps.containers[0].resources.limits)  # P1: disk cap
        self.assertTrue(all(v.empty_dir.size_limit for v in ps.volumes))       # P1: scratch cap

    def test_egress_dns_restricted_to_kube_dns(self):
        np = sandbox._egress_policy(_agent(), 'exec-y', ['10.0.0.0/8'])
        dns_rule = np.spec.egress[0]
        self.assertTrue(dns_rule.to, "DNS egress rule must scope `to:` kube-dns (P1)")
        peer = dns_rule.to[0]
        self.assertEqual(peer.namespace_selector.match_labels.get('kubernetes.io/metadata.name'),
                         'kube-system')
        self.assertEqual(peer.pod_selector.match_labels.get('k8s-app'), 'kube-dns')

    def test_job_label_truncated_to_63(self):
        job = sandbox.build_exec_job(_agent(), name='exec-' + 'a' * 80, image='img:1')
        self.assertLessEqual(len(job.spec.template.metadata.labels['shizuha.io/sandbox-job']), 63)

    @patch('fleet.sandbox.get_sandbox_clients')
    def test_reap_deletes_egress_policy(self, mock_clients):
        core, batch, rbac, net = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        mock_clients.return_value = (core, batch, rbac, net)
        sandbox.reap_job(_agent(), 'exec-z')
        batch.delete_namespaced_job.assert_called_once()
        net.delete_namespaced_network_policy.assert_called_once()  # P2: stale egress cleaned

    @patch('fleet.sandbox.get_sandbox_clients')
    def test_ensure_reconciles_on_409(self, mock_clients):
        core, batch, rbac, net = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        rbac.create_namespaced_role.side_effect = ApiException(status=409)
        mock_clients.return_value = (core, batch, rbac, net)
        sandbox.ensure_sandbox_namespace(_agent())
        rbac.patch_namespaced_role.assert_called_once()  # P2: reconciled, not ignored


# ── PLAT-397: onboarding trigger ─────────────────────────────────────────────


class OnboardingTriggerTests(_NoByoProviderGateMixin, SimpleTestCase):
    """_trigger_onboarding creates a Pulse task for a newly provisioned agent.

    Non-blocking contract: failures in the Pulse API path must never raise —
    they log a warning and return so the k8s provisioning record is preserved.
    """

    def _agent(self, **kw):
        # HIVE-1502: agent_id is a REQUIRED provisioning input — the runtime
        # entrypoint guards `${AGENT_ID:?AGENT_ID required}`. This fixture
        # previously omitted it and still "provisioned successfully", because
        # the renderer coerced the missing value (`agent.agent_id or ''`) into a
        # syntactically valid Deployment that could never start. The fixture
        # worked by accident; provisioning now refuses, so it carries a real id.
        defaults = dict(
            agent_username='newagent', agent_id='agent-uuid-newagent',
            display_name='New Agent', role='Engineer',
        )
        defaults.update(kw)
        agent = FleetAgent(**defaults)
        agent._reviewed_runtime_image = getattr(settings, 'FLEET_AGENT_IMAGE', '')
        return agent

    @override_settings(
        SHIZUHA_PLATFORM_URL='http://platform.test',
        HIVE_ADMIN_USERNAME='admin@shizuha.com',
        HIVE_ADMIN_PASSWORD='secret',
        HIVE_PULSE_ONBOARDING_ENABLED=True,
    )
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.requests.post')
    def test_creates_pulse_task_on_success(self, mock_post, _log_event):
        # _id_admin_token call returns a token; Pulse task creation returns 201.
        login_resp = MagicMock(ok=True, status_code=200)
        login_resp.json.return_value = {'tokens': {'access': 'tok123'}}
        task_resp = MagicMock(status_code=201)
        task_resp.json.return_value = {'item_key': 'PLT-42', 'id': 99}
        mock_post.side_effect = [login_resp, task_resp]

        agent = self._agent()
        provisioner._trigger_onboarding(agent)

        self.assertEqual(mock_post.call_count, 2)
        # Second call is the Pulse task creation
        pulse_call = mock_post.call_args_list[1]
        url = pulse_call.args[0] if pulse_call.args else pulse_call.kwargs.get('url', '')
        self.assertIn('/pulse/api/tasks/', url)
        payload = pulse_call.kwargs.get('json') or pulse_call.args[1]
        self.assertIn('newagent@shizuha.com', payload['assignee_email'])  # PLAT-4220 canonical domain
        self.assertIn('Onboarding', payload['title'])
        self.assertEqual(payload['priority'], 'normal')
        self.assertEqual(payload['workflow_name'], 'simple')
        self.assertIn('direct_assignment_reason', payload)

    @override_settings(
        SHIZUHA_PLATFORM_URL='http://platform.test',
        HIVE_ADMIN_USERNAME='admin@shizuha.com',
        HIVE_ADMIN_PASSWORD='secret',
        HIVE_PULSE_ONBOARDING_ENABLED=True,
    )
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.requests.post')
    def test_no_raise_when_admin_token_missing(self, mock_post, _log_event):
        # Admin login returns a non-OK response.
        login_resp = MagicMock(ok=False, status_code=401)
        login_resp.json.return_value = {}
        mock_post.return_value = login_resp

        agent = self._agent()
        provisioner._trigger_onboarding(agent)  # must not raise

        # Only the login call should have been made; Pulse call skipped.
        self.assertEqual(mock_post.call_count, 1)

    @override_settings(
        SHIZUHA_PLATFORM_URL='http://platform.test',
        HIVE_ADMIN_USERNAME='admin@shizuha.com',
        HIVE_ADMIN_PASSWORD='secret',
        HIVE_PULSE_ONBOARDING_ENABLED=True,
    )
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.requests.post')
    def test_no_raise_when_pulse_api_fails(self, mock_post, _log_event):
        login_resp = MagicMock(ok=True, status_code=200)
        login_resp.json.return_value = {'tokens': {'access': 'tok123'}}
        error_resp = MagicMock(status_code=500, text='Internal Server Error')
        mock_post.side_effect = [login_resp, error_resp]

        agent = self._agent()
        provisioner._trigger_onboarding(agent)  # must not raise

    @override_settings(SHIZUHA_PLATFORM_URL='', HIVE_PULSE_ONBOARDING_ENABLED=True)
    @patch('fleet.provisioner.requests.post')
    def test_skips_when_platform_url_unset(self, mock_post):
        provisioner._trigger_onboarding(self._agent())
        mock_post.assert_not_called()

    @override_settings(
        SHIZUHA_PLATFORM_URL='http://platform.test',
        HIVE_PULSE_ONBOARDING_ENABLED=False,
    )
    @patch('fleet.provisioner.requests.post')
    def test_skips_when_disabled(self, mock_post):
        provisioner._trigger_onboarding(self._agent())
        mock_post.assert_not_called()

    @override_settings(
        SHIZUHA_PLATFORM_URL='http://platform.test',
        HIVE_ADMIN_USERNAME='admin@shizuha.com',
        HIVE_ADMIN_PASSWORD='secret',
        HIVE_PULSE_ONBOARDING_ENABLED=True,
    )
    @patch('fleet.provisioner.requests.post')
    def test_no_raise_on_network_error(self, mock_post):
        import requests as req_lib
        mock_post.side_effect = req_lib.ConnectionError('unreachable')
        provisioner._trigger_onboarding(self._agent())  # must not raise

    @override_settings(
        SHIZUHA_PLATFORM_URL='http://platform.test',
        HIVE_ADMIN_USERNAME='admin@shizuha.com',
        HIVE_ADMIN_PASSWORD='secret',
        HIVE_PULSE_ONBOARDING_ENABLED=True,
    )
    @patch('fleet.provisioner._trigger_onboarding_async')
    @patch('fleet.provisioner._log_event')
    @patch('fleet.provisioner.get_k8s_client')
    def test_provision_agent_calls_onboarding_on_success(self, mock_k8s, _log_event, mock_onboard):
        core, apps, net = MagicMock(), MagicMock(), MagicMock()
        mock_k8s.return_value = (core, apps, net)

        agent = self._agent()
        agent.save = MagicMock()
        agent.provisioned_at = None

        ok = provisioner.provision_agent(agent, agent_password='pwd123')

        self.assertTrue(ok)
        mock_onboard.assert_called_once_with(agent)


class RuntimeFleetControlProxySecurityTests(SimpleTestCase):
    def test_control_proxy_manifest_requires_shared_secret_before_forwarding(self):
        from pathlib import Path
        manifest = (Path(__file__).resolve().parents[1] / 'k8s' / 'rt-fleet-control-proxy.yaml').read_text()
        self.assertIn('name: FLEET_CONTROL_SECRET', manifest)
        # Live + canonical secretKeyRef (must match daemon + Hive RUNTIME_FLEET_CONTROL_SECRET)
        self.assertIn('name: runtime-fleet-control-secret', manifest)
        self.assertIn('key: secret', manifest)
        # Header name is held in SECRET_HEADER const; requireControlSecret gates upstream.
        self.assertIn("const SECRET_HEADER = 'x-fleet-control-secret'", manifest)
        self.assertIn("missing or invalid fleet control secret", manifest)
        self.assertIn("reset-session", manifest)
        self.assertLess(
            manifest.index('if (!requireControlSecret(req, res)) return;'),
            manifest.index("const upstream = http.request"),
        )

    @override_settings(
        RUNTIME_FLEET_API_URL='http://readonly-proxy',
        RUNTIME_FLEET_CONTROL_URL='http://control-proxy',
        RUNTIME_FLEET_CONTROL_SECRET='shared-secret',
        RUNTIME_FLEET_API_TIMEOUT=3,
    )
    @patch('fleet.runtime_fleet.requests.post')
    def test_lifecycle_control_uses_control_proxy_and_secret_header(self, mock_post):
        from fleet import runtime_fleet
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'status': 'restarted'}
        mock_post.return_value = resp

        ok, detail = runtime_fleet.restart_runtime_agent('agent-123')

        self.assertTrue(ok)
        self.assertEqual(detail, {'status': 'restarted'})
        # restart hits the daemon's /restart verb route WITH a JSON body — the
        # control proxy forces content-type: application/json on every POST, so a
        # bodyless request 500s ("Invalid JSON") on the daemon (HIVE-171 fix).
        mock_post.assert_called_once_with(
            'http://control-proxy/v1/agents/agent-123/restart',
            headers={'X-Fleet-Control-Secret': 'shared-secret'},
            json={},
            timeout=3,
        )

    @override_settings(
        RUNTIME_FLEET_API_URL='http://readonly-proxy',
        RUNTIME_FLEET_CONTROL_URL='http://control-proxy',
        RUNTIME_FLEET_CONTROL_SECRET='shared-secret',
        RUNTIME_FLEET_API_TIMEOUT=3,
    )
    @patch('fleet.runtime_fleet.requests.post')
    def test_reset_session_uses_control_proxy_and_secret_header(self, mock_post):
        from fleet import runtime_fleet
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'status': 'session_reset'}
        mock_post.return_value = resp

        ok, detail = runtime_fleet.reset_runtime_agent_session('agent-123')

        self.assertTrue(ok)
        self.assertEqual(detail, {'status': 'session_reset'})
        mock_post.assert_called_once_with(
            'http://control-proxy/v1/agents/agent-123/reset-session',
            headers={'X-Fleet-Control-Secret': 'shared-secret'},
            json={},
            timeout=3,
        )

    @override_settings(
        RUNTIME_FLEET_API_URL='http://readonly-proxy',
        RUNTIME_FLEET_CONTROL_URL='http://control-proxy',
        RUNTIME_FLEET_CONTROL_SECRET='shared-secret',
        RUNTIME_FLEET_API_TIMEOUT=3,
    )
    @patch('fleet.runtime_fleet.requests.post')
    def test_enable_disable_use_toggle_route_with_override_kill_switch(self, mock_post):
        from fleet import runtime_fleet
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'ok': True}
        mock_post.return_value = resp

        # Enable (start) -> daemon's POST /v1/agents/toggle, enabled=True, with
        # overrideKillSwitch so a Hive Start clears the daemon kill-switch (SCLI-110).
        # The daemon has NO per-verb /enable|/disable route — those 404 (HIVE-171 fix).
        with patch('fleet.runtime_fleet._find_runtime_agent_row', return_value=None), patch(
            'fleet.runtime_fleet.get_runtime_agent',
            return_value={'id': 'agent-123', 'agent_username': 'legacy', 'model': 'unpooled-model'},
        ), patch('fleet.model_lease.all_pool_configs', return_value=[]):
            ok, _ = runtime_fleet.set_runtime_agent_enabled('agent-123', True)
        self.assertTrue(ok)
        mock_post.assert_called_with(
            'http://control-proxy/v1/agents/toggle',
            headers={'X-Fleet-Control-Secret': 'shared-secret'},
            json={'agent_id': 'agent-123', 'enabled': True, 'overrideKillSwitch': True},
            timeout=3,
        )

        # Disable (stop) -> same route, enabled=False.
        ok, _ = runtime_fleet.set_runtime_agent_enabled('agent-123', False)
        self.assertTrue(ok)
        mock_post.assert_called_with(
            'http://control-proxy/v1/agents/toggle',
            headers={'X-Fleet-Control-Secret': 'shared-secret'},
            json={
                'agent_id': 'agent-123',
                'enabled': False,
                'overrideKillSwitch': True,
                'lifecycle_state': 'operator_stopped',
            },
            timeout=3,
        )

        # Automatic hibernation uses the same actuator but carries typed,
        # wakeable lifecycle intent across the Hive -> daemon boundary.
        ok, _ = runtime_fleet.set_runtime_agent_enabled(
            'agent-123', False, lifecycle_state='hibernated',
        )
        self.assertTrue(ok)
        mock_post.assert_called_with(
            'http://control-proxy/v1/agents/toggle',
            headers={'X-Fleet-Control-Secret': 'shared-secret'},
            json={
                'agent_id': 'agent-123',
                'enabled': False,
                'overrideKillSwitch': True,
                'lifecycle_state': 'hibernated',
            },
            timeout=3,
        )


@override_settings(HIVE_ADMIN_API_URL='http://admin.test/api')
class FleetAgentUnarchiveJITTests(SimpleTestCase):
    """HIVE-779 request-time Admin grant authorization matrix."""

    def setUp(self):
        self.factory = APIRequestFactory()

    def _user(self, **overrides):
        values = {
            'id': 42,
            'email': 'ni@shizuha.com',
            'username': 'ni',
            'is_staff': False,
            'is_authenticated': True,
            'organization_memberships': {'1': 'member'},
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _grant(self, **overrides):
        values = {
            'id': 168,
            'organization': 1,
            'requester_user_id': 42,
            'requester_email': 'ni@shizuha.com',
            'scope': 'hive.fleet.agent.unarchive:kenta',
            'status': 'approved',
            'expires_at': '2999-01-01T00:00:00Z',
            'revoked_at': None,
            'active': True,
        }
        values.update(overrides)
        return values

    def _call(self, *, results=None, user=None, username='Kenta',
              admin_status=200, admin_json=None, admin_side_effect=None):
        request = self.factory.post(
            '/v1/fleet/agents/agent-id/unarchive/',
            {},
            format='json',
            HTTP_AUTHORIZATION='Bearer test-token',
        )
        force_authenticate(request, user=user or self._user())

        admin_response = MagicMock(status_code=admin_status)
        admin_response.json.return_value = (
            admin_json if admin_json is not None else {'count': len(results or []), 'results': results or []}
        )
        archive_qs = MagicMock()
        archive_qs.delete.return_value = (1, {})
        with patch('fleet.views._agent_username_for_unarchive', return_value=(username, None)), \
                patch('fleet.views.requests.get', return_value=admin_response,
                      side_effect=admin_side_effect) as admin_get, \
                patch('fleet.views.FleetAgentArchive.objects.filter', return_value=archive_qs) as archive_filter, \
                patch('fleet.views.agent_index_cache.invalidate'), \
                patch('fleet.views._audit_capability') as audit:
            response = FleetAgentUnarchiveView.as_view()(request, agent_id='agent-id')
        return response, admin_get, archive_filter, audit

    def test_staff_compatibility_bypasses_admin(self):
        response, admin_get, archive_filter, audit = self._call(
            user=self._user(is_staff=True),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['agent_username'], 'kenta')
        admin_get.assert_not_called()
        archive_filter.assert_called_once_with(agent_username__iexact='kenta')
        audit.assert_not_called()

    def test_exact_active_grant_allows_and_audits_immutable_identity(self):
        response, admin_get, archive_filter, audit = self._call(results=[self._grant()])
        self.assertEqual(response.status_code, 200)
        admin_get.assert_called_once_with(
            'http://admin.test/api/jit-elevations/',
            headers={'Authorization': 'Bearer test-token'},
            params={'status': 'approved'},
            timeout=5,
        )
        archive_filter.assert_called_once_with(agent_username__iexact='kenta')
        audit.assert_called_once()
        kwargs = audit.call_args.kwargs
        self.assertEqual(kwargs['approval_ref'], '168')
        self.assertEqual(kwargs['outcome'], 'allowed')
        self.assertEqual(kwargs['target_key'], 'kenta')
        self.assertEqual(kwargs['after']['caller_user_id'], 42)
        self.assertEqual(kwargs['after']['organization_id'], 1)
        self.assertNotIn('test-token', str(kwargs))

    def test_wrong_target_and_missing_grant_fail_closed(self):
        for results in ([self._grant(scope='hive.fleet.agent.unarchive:other')], []):
            with self.subTest(results=results):
                response, _admin_get, archive_filter, audit = self._call(results=results)
                self.assertEqual(response.status_code, 403)
                archive_filter.assert_not_called()
                self.assertEqual(audit.call_args.kwargs['outcome'], 'denied')

    def test_caller_and_organization_mismatch_fail_closed(self):
        cases = [
            (self._grant(requester_user_id=99), self._user()),
            (self._grant(requester_email='other@shizuha.com'), self._user()),
            (self._grant(organization=2), self._user(organization_memberships={'1': 'member'})),
        ]
        for grant, user in cases:
            with self.subTest(grant=grant):
                response, _admin_get, archive_filter, _audit = self._call(results=[grant], user=user)
                self.assertEqual(response.status_code, 403)
                archive_filter.assert_not_called()

    def test_missing_caller_email_cannot_authorize(self):
        """HIVE-779 reika P1: the id+email binding must hold even when the
        authenticated caller presents no email. Pre-fix, `if caller_email and
        grant_email != caller_email` short-circuited to False for an empty caller
        email, skipping the email binding and authorizing on id+org+scope alone;
        a caller with no resolvable email must now fail closed (403 denied)."""
        response, _admin_get, archive_filter, audit = self._call(
            results=[self._grant()], user=self._user(email=''),
        )
        self.assertEqual(response.status_code, 403)
        archive_filter.assert_not_called()
        self.assertEqual(audit.call_args.kwargs['outcome'], 'denied')

    def test_empty_grant_email_cannot_authorize(self):
        """HIVE-779 reika P1: a grant whose requester_email is empty/missing
        cannot be bound to the caller and must never authorize, even for an exact
        id/org/scope match."""
        response, _admin_get, archive_filter, _audit = self._call(
            results=[self._grant(requester_email='')],
        )
        self.assertEqual(response.status_code, 403)
        archive_filter.assert_not_called()

    def test_pending_expired_and_revoked_grants_fail_closed(self):
        cases = [
            self._grant(status='pending', active=False),
            self._grant(expires_at='2000-01-01T00:00:00Z', active=False),
            self._grant(revoked_at='2026-07-15T00:00:00Z', active=False),
        ]
        for grant in cases:
            with self.subTest(grant=grant):
                response, _admin_get, archive_filter, _audit = self._call(results=[grant])
                self.assertEqual(response.status_code, 403)
                archive_filter.assert_not_called()

    def test_admin_timeout_and_5xx_are_dependency_failures(self):
        response, _admin_get, archive_filter, audit = self._call(
            admin_side_effect=requests.Timeout('slow'),
        )
        self.assertEqual(response.status_code, 503)
        archive_filter.assert_not_called()
        self.assertEqual(audit.call_args.kwargs['outcome'], 'dependency-error')

        response, _admin_get, archive_filter, audit = self._call(admin_status=500)
        self.assertEqual(response.status_code, 503)
        archive_filter.assert_not_called()
        self.assertEqual(audit.call_args.kwargs['outcome'], 'dependency-error')

    def test_malformed_or_ambiguous_admin_response_fails_dependency_closed(self):
        cases = [
            {'unexpected': []},
            {'count': 2, 'results': [self._grant(), self._grant(id=169)]},
            {'count': 1, 'results': [self._grant(expires_at='not-a-date')]},
            {'count': 2, 'results': [self._grant()]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                response, _admin_get, archive_filter, audit = self._call(admin_json=payload)
                self.assertEqual(response.status_code, 503)
                archive_filter.assert_not_called()
                self.assertEqual(audit.call_args.kwargs['outcome'], 'dependency-error')

    def test_username_is_canonicalized_before_scope_match(self):
        response, _admin_get, _archive_filter, audit = self._call(
            username='KENTA',
            results=[self._grant(scope='hive.fleet.agent.unarchive:kenta')],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['agent_username'], 'kenta')
        self.assertEqual(audit.call_args.kwargs['after']['scope'], 'hive.fleet.agent.unarchive:kenta')

    def test_missing_agent_remains_404_and_is_audited(self):
        response, admin_get, archive_filter, audit = self._call(username='')
        self.assertEqual(response.status_code, 404)
        admin_get.assert_not_called()
        archive_filter.assert_not_called()
        self.assertEqual(audit.call_args.kwargs['outcome'], 'denied')


@override_settings(HIVE_ADMIN_API_URL='http://admin.test/api')
class FleetReadJITTests(SimpleTestCase):
    """PLAT-6163 request-time Admin grant authorization at fleet read edges."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = SimpleNamespace(
            id=42,
            email='mika@shizuha.com',
            username='mika',
            is_staff=False,
            is_authenticated=True,
            organization_memberships={'1': 'member'},
        )

    def _grant(self, **overrides):
        values = {
            'id': 325,
            'organization': 1,
            'requester_user_id': 42,
            'requester_email': 'mika@shizuha.com',
            'scope': 'hive.fleet.read',
            'status': 'approved',
            'expires_at': '2999-01-01T00:00:00Z',
            'revoked_at': None,
            'active': True,
        }
        values.update(overrides)
        return values

    def _request(self, path, *, user=None):
        request = self.factory.get(path, HTTP_AUTHORIZATION='Bearer test-token')
        force_authenticate(request, user=user or self.user)
        return request

    def _admin_response(self, *, results=None, status_code=200, payload=None):
        response = MagicMock(status_code=status_code)
        rows = list(results or [])
        response.json.return_value = payload if payload is not None else {
            'count': len(rows),
            'results': rows,
        }
        return response

    @patch('fleet.views._audit_capability')
    @patch('fleet.views.requests.get')
    def test_exact_active_grant_allows_snapshot_and_redacted_audit(self, admin_get, audit):
        admin_get.return_value = self._admin_response(results=[self._grant()])
        request = self._request('/v1/fleet/pulse-snapshot/')

        with patch.object(FleetPulseSnapshotView, '_build_live_snapshot', return_value=Response({
            'nodes': [], 'workload': {}, 'teams': [],
        })):
            response = FleetPulseSnapshotView.as_view()(request)

        self.assertEqual(response.status_code, 200, response.data)
        admin_get.assert_called_once_with(
            'http://admin.test/api/jit-elevations/',
            headers={'Authorization': 'Bearer test-token'},
            params={'status': 'approved'},
            timeout=5,
        )
        kwargs = audit.call_args.kwargs
        self.assertEqual(kwargs['action'], 'fleet.read')
        self.assertEqual(kwargs['approval_ref'], '325')
        self.assertEqual(kwargs['outcome'], 'allowed')
        self.assertNotIn('test-token', str(kwargs))

    @patch('fleet.views._audit_capability')
    @patch('fleet.views.requests.get')
    def test_absent_expired_revoked_or_mismatched_grant_denies(self, admin_get, audit):
        cases = [
            [],
            [self._grant(expires_at='2000-01-01T00:00:00Z', active=False)],
            [self._grant(revoked_at='2026-08-13T00:00:00Z', active=False)],
            [self._grant(requester_user_id=99)],
            [self._grant(requester_email='other@shizuha.com')],
            [self._grant(organization=2)],
            [self._grant(scope='hive.fleet.write')],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                admin_get.return_value = self._admin_response(results=rows)
                response = FleetPulseSnapshotView.as_view()(
                    self._request('/v1/fleet/pulse-snapshot/'),
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(audit.call_args.kwargs['outcome'], 'denied')

    @patch('fleet.views._audit_capability')
    @patch('fleet.views.requests.get')
    def test_missing_bearer_and_admin_failure_are_dependency_closed(self, admin_get, audit):
        request = self.factory.get('/v1/fleet/pulse-snapshot/')
        force_authenticate(request, user=self.user)
        response = FleetPulseSnapshotView.as_view()(request)
        self.assertEqual(response.status_code, 503)
        admin_get.assert_not_called()
        self.assertEqual(audit.call_args.kwargs['outcome'], 'dependency-error')

        for admin_response in (
            self._admin_response(status_code=500),
            self._admin_response(payload={'unexpected': []}),
            self._admin_response(results=[self._grant(), self._grant(id=326)]),
        ):
            with self.subTest(admin_response=admin_response):
                admin_get.return_value = admin_response
                response = FleetPulseSnapshotView.as_view()(
                    self._request('/v1/fleet/pulse-snapshot/'),
                )
                self.assertEqual(response.status_code, 503)
                self.assertEqual(audit.call_args.kwargs['outcome'], 'dependency-error')

    @patch('fleet.views.FleetAgentArchive.objects.values_list', return_value=[])
    @patch('fleet.views.FleetAgent.objects.filter')
    @patch('fleet.views._audit_capability')
    @patch('fleet.views.FleetAgentListView._build_agents_index', return_value=[{'agent_username': 'ren'}])
    @patch('fleet.views.requests.get')
    def test_explicit_grant_opt_in_returns_global_list_without_staff_bit(
        self, admin_get, build_index, _audit, fleet_filter, _archives,
    ):
        fleet_filter.return_value.values_list.return_value = []
        admin_get.return_value = self._admin_response(results=[self._grant()])
        response = FleetAgentListView.as_view()(
            self._request('/v1/fleet/agents/?fleet_read=1&nocache=1'),
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['results'][0]['agent_username'], 'ren')
        build_request = build_index.call_args.args[0]
        self.assertEqual(build_request._hive_fleet_read_grant['id'], 325)
        self.assertFalse(build_request.user.is_staff)

    @patch('fleet.views.FleetAgentArchive.objects.values_list', return_value=[])
    @patch('fleet.views.FleetAgent.objects.filter')
    @patch('fleet.views._audit_capability')
    @patch('fleet.views.FleetAgentListView._build_agents_index', return_value=[])
    @patch('fleet.views._overlay_live_pulse_memberships', side_effect=lambda agents, org_slug='': agents)
    @patch('fleet.views.requests.get')
    def test_ordinary_nonstaff_list_stays_owner_scoped_without_admin_lookup(
        self, admin_get, _overlay, build_index, _audit, fleet_filter, _archives,
    ):
        fleet_filter.return_value.values_list.return_value = []
        response = FleetAgentListView.as_view()(
            self._request('/v1/fleet/agents/?nocache=1'),
        )

        self.assertEqual(response.status_code, 200, response.data)
        admin_get.assert_not_called()
        build_request = build_index.call_args.args[0]
        self.assertFalse(hasattr(build_request, '_hive_fleet_read_grant'))
        self.assertFalse(build_request.user.is_staff)

    @patch('fleet.views._audit_capability')
    @patch('fleet.views.FleetAgentListView._build_agents_index')
    @patch('fleet.views.requests.get')
    def test_denied_grant_never_reaches_global_list_builder(self, admin_get, build_index, _audit):
        admin_get.return_value = self._admin_response(results=[])
        response = FleetAgentListView.as_view()(
            self._request('/v1/fleet/agents/?fleet_read=1&nocache=1'),
        )
        self.assertEqual(response.status_code, 403)
        build_index.assert_not_called()

    @patch('fleet.views.requests.get')
    def test_staff_compatibility_bypasses_admin(self, admin_get):
        staff = SimpleNamespace(
            id=1, email='staff@shizuha.com', username='staff',
            is_staff=True, is_authenticated=True,
        )
        with patch.object(FleetPulseSnapshotView, '_build_live_snapshot', return_value=Response({
            'nodes': [], 'workload': {}, 'teams': [],
        })):
            response = FleetPulseSnapshotView.as_view()(
                self._request('/v1/fleet/pulse-snapshot/', user=staff),
            )
        self.assertEqual(response.status_code, 200, response.data)
        admin_get.assert_not_called()


def _ready_runtime_roll_pod(name='agent-native-idle-abc'):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, deletion_timestamp=None),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type='Ready', status='True')],
        ),
    )


class NativeRuntimeRollFenceTests(SimpleTestCase):
    def setUp(self):
        self.agent = _agent(
            agent_username='native-idle',
            agent_id='00000000-0000-0000-0000-000000000001',
            status=FleetAgent.Status.RUNNING,
        )
        self.core = MagicMock()
        self.core.list_namespaced_pod.return_value = SimpleNamespace(
            items=[_ready_runtime_roll_pod()],
        )

    @patch('fleet.provisioner.stream')
    @patch('fleet.provisioner.get_k8s_client')
    def test_drain_v1_requires_exact_ready_fence(self, get_client, k8s_stream):
        import hashlib

        target = 'localhost:30500/shizuha-agent-runtime:harness-new'
        get_client.return_value = (self.core, MagicMock(), MagicMock())
        request_id = hashlib.sha256(
            f'{self.agent.agent_id}\0{target}'.encode(),
        ).hexdigest()[:32]
        k8s_stream.return_value = (
            '{"protocol":1,"requestId":"' + request_id
            + '","targetImage":"' + target
            + '","state":"ready","busy":false,'
              '"acceptingTurns":false,"pendingAcceptedTurns":0}'
              '\n__SHIZUHA_HTTP_STATUS__:200'
        )

        self.assertTrue(
            provisioner.native_agent_runtime_roll_ready(self.agent, target),
        )

    @patch('fleet.provisioner.stream')
    @patch('fleet.provisioner.get_k8s_client')
    def test_drain_v2_fenced_with_retained_row_is_ready(self, get_client, k8s_stream):
        """The 33h kumo wedge (2026-08-06): a bridge that fences ingress at the
        boundary RETAINS the checkpointed in-flight row for redelivery to the
        replacement pod. protocol=2 + ingressFenced + pendingAcceptedTurns=1 is
        the DESIGNED ready shape for a mid-turn drain — requiring zero pending
        rows made every busy agent permanently unrollable."""
        import hashlib

        target = 'localhost:30500/shizuha-agent-runtime:harness-new'
        get_client.return_value = (self.core, MagicMock(), MagicMock())
        request_id = hashlib.sha256(
            f'{self.agent.agent_id}\0{target}'.encode(),
        ).hexdigest()[:32]
        k8s_stream.return_value = (
            '{"protocol":2,"ingressFenced":true,"admissionVersion":7,'
            '"requestId":"' + request_id
            + '","targetImage":"' + target
            + '","state":"ready","busy":false,'
              '"acceptingTurns":false,"pendingAcceptedTurns":1}'
              '\n__SHIZUHA_HTTP_STATUS__:200'
        )

        self.assertTrue(
            provisioner.native_agent_runtime_roll_ready(self.agent, target),
        )

    @patch('fleet.provisioner.stream')
    @patch('fleet.provisioner.get_k8s_client')
    def test_drain_v2_without_fence_is_not_ready(self, get_client, k8s_stream):
        """Retained rows are only safe because the fence blocks new admissions."""
        import hashlib

        target = 'localhost:30500/shizuha-agent-runtime:harness-new'
        get_client.return_value = (self.core, MagicMock(), MagicMock())
        request_id = hashlib.sha256(
            f'{self.agent.agent_id}\0{target}'.encode(),
        ).hexdigest()[:32]
        k8s_stream.return_value = (
            '{"protocol":2,"requestId":"' + request_id
            + '","targetImage":"' + target
            + '","state":"ready","busy":false,'
              '"acceptingTurns":false,"pendingAcceptedTurns":1}'
              '\n__SHIZUHA_HTTP_STATUS__:200'
        )

        self.assertFalse(
            provisioner.native_agent_runtime_roll_ready(self.agent, target),
        )

    @patch('fleet.provisioner.stream')
    @patch('fleet.provisioner.get_k8s_client')
    def test_drain_v1_with_pending_rows_stays_deferred(self, get_client, k8s_stream):
        """Protocol 1 has no fence contract — keep the strict zero-row gate."""
        import hashlib

        target = 'localhost:30500/shizuha-agent-runtime:harness-new'
        get_client.return_value = (self.core, MagicMock(), MagicMock())
        request_id = hashlib.sha256(
            f'{self.agent.agent_id}\0{target}'.encode(),
        ).hexdigest()[:32]
        k8s_stream.return_value = (
            '{"protocol":1,"requestId":"' + request_id
            + '","targetImage":"' + target
            + '","state":"ready","busy":false,'
              '"acceptingTurns":false,"pendingAcceptedTurns":1}'
              '\n__SHIZUHA_HTTP_STATUS__:200'
        )

        self.assertFalse(
            provisioner.native_agent_runtime_roll_ready(self.agent, target),
        )

    @patch('fleet.provisioner.time.sleep')
    @patch('fleet.provisioner.stream')
    @patch('fleet.provisioner.get_k8s_client')
    def test_legacy_bridge_requires_two_quiet_observations(
        self, get_client, k8s_stream, sleep,
    ):
        get_client.return_value = (self.core, MagicMock(), MagicMock())
        k8s_stream.side_effect = [
            '{"message":"not found"}\n__SHIZUHA_HTTP_STATUS__:404',
            {'busy': False},
            {'busy': False},
        ]

        self.assertTrue(provisioner.native_agent_runtime_roll_ready(
            self.agent,
            'localhost:30500/shizuha-agent-runtime:harness-new',
        ))
        sleep.assert_called_once_with(0.2)

    @patch('fleet.provisioner.time.sleep')
    @patch('fleet.provisioner.stream')
    @patch('fleet.provisioner.get_k8s_client')
    def test_legacy_bridge_accepts_python_repr_returned_by_live_websocket_client(
        self, get_client, k8s_stream, sleep,
    ):
        get_client.return_value = (self.core, MagicMock(), MagicMock())
        # Exact live shape observed for Agy: type=str, content is a Python dict
        # representation after the Kubernetes websocket client decoded stdout.
        health = (
            "{'status': 'ok', 'bridge': 'antigravity-cli', "
            "'authenticated': True, 'providerHealthy': True, 'busy': False}"
        )
        k8s_stream.side_effect = [
            '{"message":"not found"}\n__SHIZUHA_HTTP_STATUS__:404',
            health,
            health,
        ]

        self.assertTrue(provisioner.native_agent_runtime_roll_ready(
            self.agent,
            'localhost:30500/shizuha-agent-runtime:harness-new',
        ))
        sleep.assert_called_once_with(0.2)

    @patch('fleet.provisioner.stream', side_effect=TimeoutError('exec timed out'))
    @patch('fleet.provisioner.get_k8s_client')
    def test_transport_failure_is_fail_closed(self, get_client, _k8s_stream):
        get_client.return_value = (self.core, MagicMock(), MagicMock())

        self.assertFalse(provisioner.native_agent_runtime_roll_ready(
            self.agent,
            'localhost:30500/shizuha-agent-runtime:harness-new',
        ))
