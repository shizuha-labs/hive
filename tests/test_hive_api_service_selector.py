"""Static regression gate for the Hive API Service endpoint boundary."""
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
SERVICES = {'hive', 'hive-direct', 'hive-tail'}


class HiveApiServiceSelectorTests(unittest.TestCase):
    def test_connect_delivery_service_and_policy_require_backend_component(self):
        documents = list(yaml.safe_load_all(
            (ROOT / 'k8s' / 'hive-connect-delivery-boundary.yaml').read_text(),
        ))
        service = next(doc for doc in documents if doc.get('kind') == 'Service')
        policy = next(doc for doc in documents if doc.get('kind') == 'NetworkPolicy')
        backend_selector = {
            'app': 'hive',
            'app.kubernetes.io/component': 'backend',
        }

        self.assertEqual(service['spec']['selector'], backend_selector)
        self.assertEqual(policy['spec']['podSelector']['matchLabels'], backend_selector)

        worker = {
            'app': 'hive',
            'app.kubernetes.io/component': 'usage-outbox-dispatcher',
        }
        self.assertFalse(all(
            worker.get(key) == value
            for key, value in service['spec']['selector'].items()
        ))

    def test_every_api_service_requires_the_backend_component(self):
        documents = list(yaml.safe_load_all(
            (ROOT / 'k8s' / 'hive-api-services.yaml').read_text(),
        ))
        services = {
            doc['metadata']['name']: doc
            for doc in documents
            if doc and doc.get('kind') == 'Service'
        }
        self.assertEqual(set(services), SERVICES)

        for name, service in services.items():
            self.assertEqual(service['spec']['selector'], {
                'app': 'hive',
                'app.kubernetes.io/component': 'backend',
            }, name)

        backend = {'app': 'hive', 'app.kubernetes.io/component': 'backend'}
        worker = {
            'app': 'hive',
            'app.kubernetes.io/component': 'usage-outbox-dispatcher',
        }
        selector = services['hive']['spec']['selector']
        self.assertTrue(all(backend.get(k) == v for k, v in selector.items()))
        self.assertFalse(all(worker.get(k) == v for k, v in selector.items()))

    def test_fleet_promotion_labels_backend_before_applying_services(self):
        workflow = (ROOT / '.forgejo' / 'scripts' / 'ring-release-fleet.sh').read_text()
        label_marker = '"app.kubernetes.io/component": "backend"'
        service_marker = 'kubectl apply -f k8s/hive-api-services.yaml'

        self.assertIn(label_marker, workflow)
        self.assertIn(service_marker, workflow)
        self.assertLess(workflow.index(label_marker), workflow.index(service_marker))
        self.assertIn(
            r"-o jsonpath='{.spec.selector.app\.kubernetes\.io/component}'",
            workflow,
        )
        self.assertNotIn(
            r"-o jsonpath='{.spec.selector.app\\.kubernetes\\.io/component}'",
            workflow,
        )
        self.assertIn(
            'HIVE-725 dedicated listener Service is not backend-scoped',
            workflow,
        )


if __name__ == '__main__':
    unittest.main()
