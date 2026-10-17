from pathlib import Path
import textwrap
from unittest.mock import MagicMock

import yaml

from django.test import SimpleTestCase

from fleet.management.commands.provision_user_daemon import Command


class Scli349FleetNetworkPolicyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        documents = list(yaml.safe_load_all(
            Path('k8s/fleet/30-fleet-isolation-netpol.yaml').read_text()
        ))
        cls.policy = next(
            item for item in documents
            if item['metadata']['name'] == 'fleet-agent-allow-egress'
        )

    def _rule(self, namespace, pod_labels=None, ip_block=None):
        for rule in self.policy['spec']['egress']:
            for peer in rule.get('to', []):
                namespace_name = (
                    peer.get('namespaceSelector', {})
                    .get('matchLabels', {})
                    .get('kubernetes.io/metadata.name')
                )
                labels = peer.get('podSelector', {}).get('matchLabels')
                cidr = peer.get('ipBlock', {}).get('cidr')
                if namespace_name == namespace and labels == pod_labels and cidr == ip_block:
                    return rule
        self.fail(
            f'missing egress rule namespace={namespace!r} '
            f'pod_labels={pod_labels!r} ip_block={ip_block!r}'
        )

    @staticmethod
    def _ports(rule):
        return {int(item['port']) for item in rule['ports']}

    def test_direct_id_account_and_mcp_ports_are_allowed(self):
        rule = self._rule('shizuha', {
            'app.kubernetes.io/name': 'shizuha-id',
            'app.kubernetes.io/component': 'backend',
        })
        self.assertEqual(self._ports(rule), {8001, 18102})

    def test_origin_git_ssh_and_ci_logs_target_ports_are_allowed(self):
        rule = self._rule('origin', {'app': 'forgejo'})
        self.assertEqual(self._ports(rule), {22, 3000, 8080})

    def test_dockerhub_pull_through_cache_is_allowed_without_broad_registry_access(self):
        labels = {
            'app.kubernetes.io/name': 'shizuha-registry',
            'app.kubernetes.io/component': 'mirror-dockerhub',
        }
        rule = self._rule('registry', labels)
        self.assertEqual(self._ports(rule), {5000})

    def test_internal_image_registry_is_allowed_without_broad_registry_access(self):
        labels = {
            'app.kubernetes.io/name': 'shizuha-registry',
            'app.kubernetes.io/component': 'registry',
        }
        rule = self._rule('registry', labels)
        self.assertEqual(self._ports(rule), {5000})

    def test_python_package_cache_is_allowed_without_broad_registry_access(self):
        rule = self._rule('registry', {'app': 'pip-cache'})
        self.assertEqual(self._ports(rule), {80})

    def test_apt_package_cache_is_allowed_without_broad_registry_access(self):
        rule = self._rule('registry', {'app': 'apt-cache'})
        self.assertEqual(self._ports(rule), {3142})

    def test_npm_package_cache_is_allowed_without_broad_registry_access(self):
        # PLAT-5022: the only package cache that was never allowlisted.
        rule = self._rule('registry', {'app': 'npm-cache'})
        self.assertEqual(self._ports(rule), {4873})

    def test_alpine_and_git_raw_caches_are_allowed_without_broad_registry_access(self):
        self.assertEqual(self._ports(self._rule('registry', {'app': 'apk-cache'})), {80})
        self.assertEqual(self._ports(self._rule('registry', {'app': 'git-raw-cache'})), {80})

    def test_search_egress_names_the_service_target_port(self):
        # PLAT-6007: NetworkPolicy is evaluated AFTER kube-proxy DNAT, and
        # shizuha-search is Service :80 -> targetPort 8090. A port-80-only rule
        # therefore matched no real packet: every admitted agent's
        # SEARCH_BASE_URL got an instant ECONNREFUSED that reads exactly like the
        # search backend being down. provision_user_daemon.py already carried
        # both ports; only this manifest half shipped. Keep 80 so the two
        # sources stay in lockstep.
        self.assertEqual(self._ports(self._rule('search')), {80, 8090})

    def test_only_the_exact_origin_private_vip_is_allowed_for_public_tls(self):
        rule = self._rule(None, ip_block='192.168.0.250/32')
        self.assertEqual(self._ports(rule), {443})

    def test_public_tls_edge_post_dnat_endpoint_is_allowed(self):
        rule = self._rule('shizuha', {
            'app.kubernetes.io/name': 'shizuha-edge',
        })
        self.assertEqual(self._ports(rule), {443})

    def test_origin_policy_has_a_ci_reconciliation_workflow(self):
        workflow = Path('.forgejo/workflows/deploy-fleet-networkpolicy.yml').read_text()
        self.assertIn('kubectl apply --dry-run=server -f "$MANIFEST"', workflow)
        self.assertIn('kubectl apply -f "$MANIFEST"', workflow)
        self.assertIn('registry/app=apt-cache TCP 3142', workflow)
        self.assertIn('registry/app=npm-cache TCP 4873', workflow)
        self.assertIn('registry/app=apk-cache TCP 80', workflow)
        self.assertIn('registry/app=git-raw-cache TCP 80', workflow)
        # PLAT-5022: the internal registry :5000 egress rule and its deploy guard
        # are mandatory (approved but previously never merged).
        self.assertIn('registry/shizuha-registry component=registry TCP 5000', workflow)
        self.assertIn('exact search TCP 80+8090 rule (PLAT-6007)', workflow)
        self.assertIn('("apt-dns", "apt-cache.registry.svc.cluster.local", 3142)', workflow)
        self.assertIn('("apk-dns", "apk-cache.registry.svc.cluster.local", 80)', workflow)
        self.assertIn('("git-raw-dns", "git-raw-cache.registry.svc.cluster.local", 80)', workflow)

        # PLAT-5598 / ADR-0067: CI proves the policy with a disposable,
        # non-privileged Job. It must never regain interactive access to a real
        # fleet agent merely because exec made the old verifier convenient.
        self.assertNotIn('kubectl -n shizuha-fleet exec', workflow)
        self.assertNotIn('docker run --rm', workflow)
        self.assertIn("require_yes 'create bounded Job' create jobs.batch", workflow)
        self.assertIn("require_no 'fleet Pod exec must stay forbidden'", workflow)
        self.assertIn('for verb in get list; do', workflow)
        self.assertIn('persistentvolumeclaims -n shizuha-fleet', workflow)
        self.assertIn('for verb in patch delete; do', workflow)
        self.assertNotIn("require_no 'fleet PVC reads must stay forbidden'", workflow)
        self.assertIn('CROSS_NODE_UNAVAILABLE', workflow)
        self.assertIn('FLEET_NETPOL_CANARY=passed', workflow)

        # A failed init probe must remain diagnosable. Kubernetes rejects a
        # combined log request while the regular container is PodInitializing,
        # so the workflow captures outer first and gates nested on containerID.
        self.assertNotIn('--all-containers=true', workflow)
        self.assertIn('collect_canary_logs()', workflow)
        self.assertIn('logs "$POD" -c outer', workflow)
        self.assertIn('containerStatuses[?(@.name=="nested")].containerID', workflow)
        self.assertIn('logs "$POD" -c nested', workflow)
        self.assertIn('nested logs unavailable: container never started', workflow)

        heredoc = workflow.split('cat >"$JOB_MANIFEST" <<YAML\n', 1)[1]
        heredoc = heredoc.split('\n          YAML\n', 1)[0]
        rendered = textwrap.dedent(heredoc)
        for variable, value in {
            '${JOB}': 'fleet-netpol-canary',
            '${RUN_ID}': '12345',
            '${CANARY_NODE}': 'gx10-13',
            '${APT_ENDPOINT}': '10.42.13.9',
            '${APT_CLUSTER_IP}': '10.43.13.9',
            '${S1_IP}': '100.64.0.3',
            '${APT_NODE_PORT}': '30142',
        }.items():
            rendered = rendered.replace(variable, value)
        job = yaml.safe_load(rendered)

        self.assertEqual(job['kind'], 'Job')
        self.assertEqual(job['metadata']['name'], 'fleet-netpol-canary')
        self.assertEqual(job['metadata']['namespace'], 'shizuha-fleet')
        self.assertEqual(job['spec']['backoffLimit'], 0)
        self.assertEqual(job['spec']['activeDeadlineSeconds'], 90)
        self.assertEqual(job['spec']['ttlSecondsAfterFinished'], 15)

        template = job['spec']['template']
        self.assertEqual(
            template['metadata']['labels']['shizuha.io/agent'],
            'fleet-netpol-canary',
        )
        pod = template['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertFalse(pod['enableServiceLinks'])
        self.assertEqual(pod['restartPolicy'], 'Never')
        self.assertEqual(pod['securityContext']['seccompProfile']['type'], 'RuntimeDefault')

        clients = pod['initContainers'] + pod['containers']
        self.assertEqual([client['name'] for client in clients], ['outer', 'nested'])
        for expected_mode, client in zip(('outer', 'nested'), clients):
            self.assertRegex(client['image'], r'@sha256:[0-9a-f]{64}$')
            self.assertEqual(client['imagePullPolicy'], 'IfNotPresent')
            self.assertFalse(client['securityContext']['allowPrivilegeEscalation'])
            self.assertTrue(client['securityContext']['readOnlyRootFilesystem'])
            self.assertTrue(client['securityContext']['runAsNonRoot'])
            self.assertEqual(client['securityContext']['capabilities']['drop'], ['ALL'])
            env = {item['name']: item['value'] for item in client['env']}
            self.assertEqual(env['PROBE_MODE'], expected_mode)
        self.assertIn(
            'registry/shizuha-registry component=registry TCP 5000',
            workflow,
        )

        full_deploy = Path('.forgejo/workflows/deploy.yml').read_text()
        self.assertIn('Reconcile fleet agent connectivity policy', full_deploy)
        self.assertIn('fleet-allow-hostplane-ssh-egress-v2', full_deploy)
        self.assertIn('-n shizuha-fleet', full_deploy)
        self.assertNotIn(
            'for verb in get list watch create update patch delete; do',
            full_deploy,
        )
        self.assertIn('for verb in create delete; do', full_deploy)

    def test_per_user_daemon_policy_keeps_the_same_identity_and_origin_paths(self):
        networking = MagicMock()
        Command()._ensure_netpol(networking, 'user-110')
        policies = {
            call.args[1].metadata.name: call.args[1]
            for call in networking.create_namespaced_network_policy.call_args_list
        }
        allow = policies['fleet-agent-allow-egress']

        def matching_ports(namespace, labels=None, cidr=None):
            for rule in allow.spec.egress:
                for peer in rule.to or []:
                    ns_labels = getattr(peer.namespace_selector, 'match_labels', None)
                    ns = (ns_labels or {}).get('kubernetes.io/metadata.name')
                    pod_labels = getattr(peer.pod_selector, 'match_labels', None)
                    block = getattr(peer.ip_block, 'cidr', None)
                    if ns == namespace and pod_labels == labels and block == cidr:
                        return {int(item.port) for item in rule.ports}
            self.fail(
                f'missing generated rule namespace={namespace!r} '
                f'labels={labels!r} cidr={cidr!r}'
            )

        self.assertEqual(matching_ports('origin', {'app': 'forgejo'}), {22, 3000, 8080})
        self.assertEqual(matching_ports('registry', {
            'app.kubernetes.io/name': 'shizuha-registry',
            'app.kubernetes.io/component': 'mirror-dockerhub',
        }), {5000})
        self.assertEqual(matching_ports('registry', {
            'app.kubernetes.io/name': 'shizuha-registry',
            'app.kubernetes.io/component': 'registry',
        }), {5000})
        self.assertEqual(matching_ports('registry', {'app': 'pip-cache'}), {80})
        self.assertEqual(matching_ports('registry', {'app': 'apt-cache'}), {3142})
        self.assertEqual(matching_ports('registry', {'app': 'npm-cache'}), {4873})
        self.assertEqual(matching_ports('registry', {'app': 'apk-cache'}), {80})
        self.assertEqual(matching_ports('registry', {'app': 'git-raw-cache'}), {80})
        self.assertEqual(matching_ports('shizuha', {
            'app.kubernetes.io/name': 'shizuha-id',
            'app.kubernetes.io/component': 'backend',
        }), {8001, 18102})
        self.assertEqual(matching_ports('shizuha', {
            'app.kubernetes.io/name': 'shizuha-edge',
        }), {443})
        self.assertEqual(matching_ports(None, cidr='192.168.0.250/32'), {443})

        # PLAT-6007: the search allowance is spread over more than one generated
        # rule here and folded into one rule in the manifest, so compare the
        # union rather than the first match. The two sources drifted apart once
        # already — this pins them together at the port set that actually
        # survives kube-proxy DNAT (Service :80 -> targetPort 8090).
        def union_ports(namespace):
            ports = set()
            for rule in allow.spec.egress:
                for peer in rule.to or []:
                    ns_labels = getattr(peer.namespace_selector, 'match_labels', None)
                    if (ns_labels or {}).get('kubernetes.io/metadata.name') == namespace:
                        ports |= {int(item.port) for item in rule.ports or []}
            return ports

        self.assertEqual(union_ports('search'), {80, 8090})
        self.assertEqual(union_ports('search'), self._ports(self._rule('search')))
