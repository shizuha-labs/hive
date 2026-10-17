"""Running a Hive-issued fallback is obedience, not drift.

Operator 2026-08-04, on the Agents page showing nagi as
`Grok Build / cortex/grok-4.5 pinned` with a red "Runtime config drift":

    "hive is source of truth .. if hive says it should be grok 4.5 then the
     underlying fleet agent should sync this properly and obey it without any
     drifts"

nagi WAS obeying. Hive's own record carries
`model_fallbacks=[cortex/grok-4.5, cortex/DeepSeek-V4-Flash]` — the sanctioned
Grok Build chain from `managed_model_fallbacks` (operator exception
2026-08-01) — and the pod env carried the same chain. That chain was removed on
2026-08-04 ("an agent is pinned to one backend"), so in production the
sanctioned set is now just the primary and this union logic reduces to the
strict original check. The tests below keep a chain in the FIXTURE on purpose:
the drift rule must stay correct for any chain that is ever configured, rather
than silently depending on there being none. With grok out of quota
(157 "no healthy token" + 34 upstream 403s in 24h) nagi ran step 2, exactly as
instructed, and was reported `runtime_config_drift` / `degraded` /
health_ok=False for it, because the check compared only against the primary.
"""
import time

from django.test import SimpleTestCase

from .external_agents import telemetry_fields


NAGI_CHAIN = [
    {'model': 'cortex/grok-4.5', 'method': 'grok_build'},
    {'model': 'cortex/DeepSeek-V4-Flash', 'method': 'shizuha'},
]


def _snap(model):
    # live model lives under `runtime.model` — telemetry_fields reads
    # rt = snap['runtime']; live_model = rt.get('model').
    return {'received_at': int(time.time()), 'runtime': {'model': model}}


def _configured(model, fallbacks):
    return {
        'model': model,
        'effective_model': model,
        'execution_method': 'grok_build',
        'model_fallbacks': fallbacks,
    }


class DriftRespectsHiveChainTests(SimpleTestCase):
    def test_running_the_hive_issued_fallback_is_not_drift(self):
        fields = telemetry_fields(
            _snap('cortex/DeepSeek-V4-Flash'),
            configured=_configured('cortex/grok-4.5', NAGI_CHAIN),
        )
        self.assertFalse(
            fields.get('runtime_config_drift'),
            'step 2 of the chain Hive itself issued is compliance',
        )
        self.assertNotEqual(fields.get('health_reason'), 'runtime_config_drift')
        self.assertNotEqual(fields.get('availability_status'), 'degraded')

    def test_running_the_primary_is_not_drift(self):
        fields = telemetry_fields(
            _snap('cortex/grok-4.5'),
            configured=_configured('cortex/grok-4.5', NAGI_CHAIN),
        )
        self.assertFalse(fields.get('runtime_config_drift'))

    def test_unsanctioned_model_is_still_drift(self):
        """The original purpose of the check must survive."""
        fields = telemetry_fields(
            _snap('claude-opus-5'),
            configured=_configured('cortex/grok-4.5', NAGI_CHAIN),
        )
        self.assertTrue(
            fields.get('runtime_config_drift'),
            'a model absent from Hive\'s chain is a real violation',
        )
        self.assertFalse(fields.get('health_ok'))
        self.assertEqual(fields.get('health_reason'), 'runtime_config_drift')
        self.assertEqual(fields.get('availability_status'), 'degraded')

    def test_single_model_lane_still_drifts_on_mismatch(self):
        """Lanes without a chain keep the strict single-model contract."""
        fields = telemetry_fields(
            _snap('cortex/DeepSeek-V4-Flash'),
            configured=_configured('gpt-5.6-sol', []),
        )
        self.assertTrue(fields.get('runtime_config_drift'))

    def test_cortex_prefix_is_ignored_when_matching(self):
        fields = telemetry_fields(
            _snap('DeepSeek-V4-Flash'),
            configured=_configured('cortex/grok-4.5', NAGI_CHAIN),
        )
        self.assertFalse(
            fields.get('runtime_config_drift'),
            'the bare and cortex/-prefixed spellings are the same lane',
        )

    def test_malformed_chain_entries_do_not_widen_the_allow_set(self):
        fields = telemetry_fields(
            _snap('claude-opus-5'),
            configured=_configured('cortex/grok-4.5', [None, {}, {'model': ''}, 'junk']),
        )
        self.assertTrue(fields.get('runtime_config_drift'))
