"""Health chips must distinguish a broken runtime from stuck work, and say why.

Operator 2026-08-05, reading the Agents page: "why a lot of deep seek agents
show runtime unhealthy or needs help etc even though some of them are working
fine".

Two separate defects made a healthy fleet look broken:

* `runtime_unhealthy` was raised with detail=None, so an agent failing auth
  (`vLLM error 401: Authentication credentials were not provided` x4 on rei) and
  one merely failing a prewarm optimisation (`soft_drain_rehome` on nami) showed
  the identical unactionable chip.
* `heartbeat_needs_help` was severity `error`, rendered in the same red as a
  broken runtime — while health_ok was TRUE and the agent was serving normally.
  banto/shion were "2 ready task(s) with no progress for 59 heartbeat(s)": real,
  worth acting on, but not a runtime fault.
"""
from django.test import SimpleTestCase

from .views import (
    _HEALTH_ATTENTION_TYPES,
    _HEALTH_ERROR_TYPES,
    _clear_empty_queue_conversational_needs_help,
    _compute_agent_health_summary,
)


def _agent(**over):
    base = {
        'health_ok': True,
        'provider_unavailable': False,
        'runtime_config_drift': False,
        'needs_help': False,
        'recent_errors': [],
    }
    base.update(over)
    return base


def _issue(summary, kind):
    for i in summary.get('issues') or []:
        if i.get('type') == kind:
            return i
    return None


class RuntimeUnhealthyDetailTests(SimpleTestCase):
    def test_carries_the_actual_error(self):
        summary = _compute_agent_health_summary(_agent(
            health_ok=False,
            health_consecutive_errors=4,
            recent_errors=[
                'vLLM error 401: {"detail":"Authentication credentials were not provided."}',
            ],
        ))
        issue = _issue(summary, 'runtime_unhealthy')
        self.assertIsNotNone(issue)
        self.assertIn('401', issue['detail'])
        self.assertIn('4 consecutive error turn(s)', issue['detail'])

    def test_two_different_faults_produce_different_details(self):
        auth = _compute_agent_health_summary(_agent(
            health_ok=False, health_consecutive_errors=4,
            recent_errors=['vLLM error 401: credentials were not provided'],
        ))
        prewarm = _compute_agent_health_summary(_agent(
            health_ok=False, health_consecutive_errors=2,
            recent_errors=['cortex_prefix_prewarm_failed reason=soft_drain_rehome'],
        ))
        self.assertNotEqual(
            _issue(auth, 'runtime_unhealthy')['detail'],
            _issue(prewarm, 'runtime_unhealthy')['detail'],
            'an auth failure and a prewarm miss must not read identically',
        )

    def test_detail_is_none_rather_than_invented_when_nothing_is_known(self):
        summary = _compute_agent_health_summary(_agent(health_ok=False))
        self.assertIsNone(_issue(summary, 'runtime_unhealthy')['detail'])

    def test_detail_is_bounded(self):
        summary = _compute_agent_health_summary(_agent(
            health_ok=False, recent_errors=['x' * 5000],
        ))
        self.assertLessEqual(len(_issue(summary, 'runtime_unhealthy')['detail']), 260)

    def test_residency_full_503_is_provider_not_runtime(self):
        # 2026-09-02 misaki: pod 2/2 Running, last turn 503 residency_full.
        # That is Cortex capacity, not a dead runtime.
        summary = _compute_agent_health_summary(_agent(
            health_ok=False,
            health_consecutive_errors=3,
            recent_errors=[
                'residency_full: GLM-5.3-Flash residency is full (12 homes still inside their cache_control TTL)',
            ],
        ))
        self.assertIsNone(_issue(summary, 'runtime_unhealthy'))
        issue = _issue(summary, 'provider_unavailable')
        self.assertIsNotNone(issue)
        self.assertEqual(issue['severity'], 'warning')
        self.assertIn('residency_full', issue['detail'])

    def test_occupies_the_slot_503_is_provider_not_runtime(self):
        summary = _compute_agent_health_summary(_agent(
            health_ok=False,
            health_consecutive_errors=1,
            recent_errors=[
                'Cortex stream error: live session occupies the slot; retry as a continuation or wait',
            ],
        ))
        self.assertIsNone(_issue(summary, 'runtime_unhealthy'))
        self.assertIsNotNone(_issue(summary, 'provider_unavailable'))

    def test_realtime_mint_503_is_provider_not_runtime(self):
        summary = _compute_agent_health_summary(_agent(
            health_ok=False,
            health_consecutive_errors=1,
            recent_errors=[
                'Cortex realtime session mint failed (503): Grok Voice realtime mint failed (503).',
            ],
        ))
        self.assertIsNone(_issue(summary, 'runtime_unhealthy'))
        self.assertIsNotNone(_issue(summary, 'provider_unavailable'))


class NeedsHelpIsNotARuntimeFaultTests(SimpleTestCase):
    def test_needs_help_is_attention_not_error(self):
        summary = _compute_agent_health_summary(_agent(
            health_ok=True,
            needs_help=True,
            needs_help_reason='2 ready task(s) with no progress for 59 heartbeat(s)',
        ))
        issue = _issue(summary, 'heartbeat_needs_help')
        self.assertIsNotNone(issue, 'the signal must still be surfaced')
        self.assertEqual(issue['severity'], 'attention')
        self.assertNotEqual(
            issue['severity'], 'error',
            'the runtime is healthy — do not render stuck work as a fault',
        )

    def test_reason_is_preserved(self):
        summary = _compute_agent_health_summary(_agent(
            health_ok=True, needs_help=True,
            needs_help_reason='2 ready task(s) with no progress for 59 heartbeat(s)',
        ))
        self.assertIn('59 heartbeat', _issue(summary, 'heartbeat_needs_help')['detail'])

    def test_tier_membership_is_explicit(self):
        self.assertIn('heartbeat_needs_help', _HEALTH_ATTENTION_TYPES)
        self.assertNotIn('heartbeat_needs_help', _HEALTH_ERROR_TYPES)
        # A genuinely broken runtime stays an error.
        self.assertIn('runtime_unhealthy', _HEALTH_ERROR_TYPES)
        self.assertIn('runtime_config_drift', _HEALTH_ERROR_TYPES)


class EmptyQueueConversationalNeedsHelpTests(SimpleTestCase):
    """Idle talkable seats with no assigned Pulse work are not Needs help.

    Operator 2026-08-15, CEO Office Hina/Aya: cards painted
    "2 consecutive heartbeat(s) exposed no Pulse queue snapshot" while
    workload was 0/0/0. Lean seats do not declare pulse_get_my_tasks, so
    every heartbeat is queue-blind by construction.
    """

    def test_hina_empty_snapshot_is_not_a_health_chip(self):
        agent = _agent(
            agent_username='hina',
            needs_help=True,
            needs_help_reason='2 consecutive heartbeat(s) exposed no Pulse queue snapshot',
            heartbeat_outcome='needs_help',
            heartbeat_ready_task_count=0,
            env={'SHIZUHA_LEAN_MCP': '1', 'AGENT_TEAM': 'ceo-office'},
            team_memberships=[{'team_slug': 'ceo-office'}],
        )
        summary = _compute_agent_health_summary(agent)
        self.assertFalse(agent['needs_help'])
        self.assertEqual(agent['heartbeat_outcome'], 'queue_empty')
        self.assertIsNone(_issue(summary, 'heartbeat_needs_help'))
        self.assertEqual(summary['highest_severity'], 'ok')

    def test_ceo_office_membership_alone_is_enough(self):
        agent = _agent(
            agent_username='aya',
            needs_help=True,
            needs_help_reason='2 consecutive heartbeat(s) exposed no Pulse queue snapshot',
            heartbeat_outcome='needs_help',
            team_memberships=[{'team_slug': 'ceo-office'}],
        )
        _clear_empty_queue_conversational_needs_help(agent)
        self.assertFalse(agent['needs_help'])
        self.assertEqual(agent['needs_help_reason'], '')

    def test_pulse_worker_empty_snapshot_still_surfaces(self):
        # saki / Pulse-driven: queue-blind after restart is still Needs help.
        summary = _compute_agent_health_summary(_agent(
            agent_username='saki',
            needs_help=True,
            needs_help_reason='2 consecutive heartbeat(s) exposed no Pulse queue snapshot',
            heartbeat_outcome='needs_help',
            heartbeat_ready_task_count=0,
            env={'AGENT_TEAM': 'engineering'},
        ))
        self.assertIsNotNone(_issue(summary, 'heartbeat_needs_help'))

    def test_thinking_queue_blind_is_not_needs_help(self):
        """Aoi 2026-09-16: thinking tenure painted queue-blind Needs help."""
        agent = _agent(
            agent_username='aoi',
            needs_help=True,
            needs_help_reason='5 consecutive heartbeat(s) exposed no Pulse queue snapshot',
            heartbeat_outcome='needs_help',
            heartbeat_ready_task_count=0,
            activity_phase='thinking',
            env={'AGENT_TEAM': 'merge'},
        )
        summary = _compute_agent_health_summary(agent)
        self.assertFalse(agent['needs_help'])
        self.assertIsNone(_issue(summary, 'heartbeat_needs_help'))

    def test_ichi_working_retracts_stale_ready_no_progress_chip(self):
        """Heartbeat said 5 ready/no progress while live tools were Pulse transitions."""
        agent = _agent(
            agent_username='ichi',
            needs_help=True,
            needs_help_reason='5 ready task(s) with no progress for 3 heartbeat(s)',
            heartbeat_outcome='needs_help',
            heartbeat_ready_task_count=5,
            activity_phase='tool',
            activity_tool_name='mcp__shizuha-pulse__pulse_execute_transition',
            activity_tool_count=1,
            env={'AGENT_TEAM': 'devops'},
        )
        summary = _compute_agent_health_summary(agent)
        self.assertFalse(agent['needs_help'])
        self.assertEqual(agent['heartbeat_outcome'], 'worked_task')
        self.assertIsNone(_issue(summary, 'heartbeat_needs_help'))

    def test_hibernated_ready_no_progress_does_not_paint_needs_help(self):
        """Sora 2026-09-16: scaled-to-zero still showed 4 ready / no progress."""
        agent = _agent(
            agent_username='sora',
            status='hibernated',
            lifecycle_state='hibernated',
            hibernation_reason='does not hold the model lease',
            needs_help=True,
            needs_help_reason='4 ready task(s) with no progress for 4 heartbeat(s)',
            heartbeat_outcome='needs_help',
            heartbeat_ready_task_count=4,
            env={'AGENT_TEAM': 'architecture'},
        )
        summary = _compute_agent_health_summary(agent)
        self.assertFalse(agent['needs_help'])
        self.assertEqual(agent['needs_help_reason'], '')
        self.assertIsNone(_issue(summary, 'heartbeat_needs_help'))

    def test_hibernated_empty_snapshot_does_not_paint_needs_help(self):
        """Sana 2026-09-16: hibernated + queue-blind chip on the same card."""
        agent = _agent(
            agent_username='sana',
            status='hibernated',
            lifecycle_state='hibernated',
            hibernation_reason='does not hold the model lease',
            needs_help=True,
            needs_help_reason='3 consecutive heartbeat(s) exposed no Pulse queue snapshot',
            heartbeat_outcome='needs_help',
            heartbeat_ready_task_count=0,
            env={'AGENT_TEAM': 'sales'},
        )
        summary = _compute_agent_health_summary(agent)
        self.assertFalse(agent['needs_help'])
        self.assertIsNone(_issue(summary, 'heartbeat_needs_help'))

    def test_lean_seat_with_ready_work_still_surfaces(self):
        summary = _compute_agent_health_summary(_agent(
            agent_username='hina',
            needs_help=True,
            needs_help_reason='1 ready task(s) with no progress for 2 heartbeat(s)',
            heartbeat_outcome='needs_help',
            heartbeat_ready_task_count=1,
            env={'SHIZUHA_LEAN_MCP': '1', 'AGENT_TEAM': 'ceo-office'},
        ))
        self.assertIsNotNone(_issue(summary, 'heartbeat_needs_help'))
