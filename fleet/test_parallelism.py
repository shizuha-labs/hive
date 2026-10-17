from django.test import SimpleTestCase

from .models import FleetAgent
from .parallelism import CONTRACT_VERSION, native_agent_parallelism, runtime_agent_parallelism
from .runtime_fleet import normalize_runtime_agent
from .serializers import FleetAgentSerializer


class RuntimeParallelismContractTests(SimpleTestCase):
    def test_runtime_without_isolation_evidence_is_single_task(self):
        readiness = runtime_agent_parallelism({
            'allowParallelExecution': True,
            'maxConcurrentTasks': 4,
            'runtimeParallelism': {
                'supported': True,
                'contractVersion': CONTRACT_VERSION,
                'maxTasks': 4,
                'isolation': {'session': True},
            },
        })

        self.assertFalse(readiness.supported)
        self.assertEqual(readiness.effective_max_tasks, 1)
        self.assertEqual(readiness.status, 'single_task')
        self.assertIn('missing isolation evidence', readiness.reason)

    def test_parallel_capable_runtime_reports_effective_limit(self):
        readiness = runtime_agent_parallelism({
            'allowParallelExecution': True,
            'maxConcurrentTasks': 3,
            'runtimeParallelism': {
                'supported': True,
                'contractVersion': CONTRACT_VERSION,
                'maxTasks': 5,
                'isolation': {
                    'session': True,
                    'workspace': True,
                    'mcp_client': True,
                    'credential_broker': True,
                    'activity_log': True,
                },
            },
        })

        self.assertTrue(readiness.supported)
        self.assertEqual(readiness.effective_max_tasks, 3)
        self.assertEqual(readiness.status, 'parallel_ready')

    def test_native_agent_defaults_single_task_without_contract_marker(self):
        agent = FleetAgent(agent_username='sara', env={'SHIZUHA_MAX_PARALLEL_TASKS': '4'})

        readiness = native_agent_parallelism(agent)

        self.assertFalse(readiness.supported)
        self.assertEqual(readiness.effective_max_tasks, 1)
        self.assertIn('no PLAT-1109', readiness.reason)

    def test_native_agent_with_contract_marker_is_parallel_ready(self):
        agent = FleetAgent(
            agent_username='sara',
            env={
                'SHIZUHA_PARALLEL_EXECUTION_CONTRACT': CONTRACT_VERSION,
                'SHIZUHA_MAX_PARALLEL_TASKS': '2',
            },
        )

        readiness = native_agent_parallelism(agent)

        self.assertTrue(readiness.supported)
        self.assertEqual(readiness.effective_max_tasks, 2)

    def test_hive_serializer_exposes_parallel_readiness(self):
        agent = FleetAgent(agent_username='sara')

        data = FleetAgentSerializer(agent).data

        self.assertEqual(data['parallel_execution_readiness'], 'single_task')
        self.assertEqual(data['effective_parallelism']['effective_max_tasks'], 1)

    def test_external_runtime_normalization_exposes_parallel_readiness(self):
        normalized = normalize_runtime_agent({
            'id': 'sara',
            'username': 'sara',
            'enabled': True,
            'status': 'running',
            'allowParallelExecution': False,
            'maxConcurrentTasks': 8,
        })

        self.assertFalse(normalized['parallel_execution_supported'])
        self.assertEqual(normalized['effective_parallelism']['effective_max_tasks'], 1)
