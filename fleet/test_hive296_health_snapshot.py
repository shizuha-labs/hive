"""HIVE-296 P1 — health snapshot classifier unit tests.

Two required fixtures:
  1. Banto alias drift: alias resolved → healthy; alias unresolved → issue
  2. Capability generation drift: applied N-1 → applied_generation_stale + observed_config_drift

All classifier tests are pure (no DB) — Django not required.
DB-persistence tests use TestCase and run against the test database.
"""
from __future__ import annotations

import uuid
from django.test import TestCase
from django.utils import timezone

from .health_snapshot import classify_snapshot


# ── Pure classifier fixtures ───────────────────────────────────────────────────

class BantoAliasFixture(TestCase):
    """Fixture: banto@shizuha.com (runtime/roster) ↔ banto@agents.shizuha.io (canonical)."""

    BANTO_SNAP_BASE = {
        "runtime_username": "banto",
        "runtime_email": "banto@shizuha.com",       # roster/runtime alias
        "canonical_email": "banto@agents.shizuha.io",  # Shizuha-ID canonical
        "platform_user_id": "42",
        "account_type": "agent",
        "account_active": True,
    }

    def test_resolved_alias_is_healthy(self):
        """Same platform user proven → no identity_alias_drift issue."""
        snap = {
            **self.BANTO_SNAP_BASE,
            "alias_resolution_evidence": {"same_platform_user": True, "platform_user_id": "42"},
        }
        issues = classify_snapshot(snap)
        alias_drift = [i for i in issues if i["issue_type"] == "identity_alias_drift"]
        self.assertEqual(alias_drift, [], "Resolved alias must not raise identity_alias_drift")

    def test_unresolved_alias_raises_drift_issue(self):
        """Alias unresolved → identity_alias_drift issue with expected/actual set."""
        snap = {
            **self.BANTO_SNAP_BASE,
            "alias_resolution_evidence": {},  # no proof
        }
        issues = classify_snapshot(snap)
        drift = [i for i in issues if i["issue_type"] == "identity_alias_drift"]
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["service"], "id")
        self.assertIn("banto@shizuha.com", str(drift[0]["actual"]))
        self.assertIn("banto@agents.shizuha.io", str(drift[0]["expected"]))

    def test_same_email_no_drift(self):
        """When runtime_email == canonical_email, no alias drift even without evidence."""
        snap = {
            **self.BANTO_SNAP_BASE,
            "runtime_email": "banto@agents.shizuha.io",
            "canonical_email": "banto@agents.shizuha.io",
        }
        issues = classify_snapshot(snap)
        self.assertFalse(any(i["issue_type"] == "identity_alias_drift" for i in issues))

    def test_unresolved_identity_fires_when_no_canonical(self):
        """When canonical_email and platform_user_id are both absent → identity_unresolved."""
        snap = {"runtime_email": "ghost@shizuha.com"}
        issues = classify_snapshot(snap)
        types = [i["issue_type"] for i in issues]
        self.assertIn("identity_unresolved", types)

    def test_inactive_account_fires_issue(self):
        snap = {**self.BANTO_SNAP_BASE, "account_active": False}
        types = [i["issue_type"] for i in classify_snapshot(snap)]
        self.assertIn("inactive_or_wrong_account_type", types)

    def test_wrong_account_type_fires_issue(self):
        snap = {**self.BANTO_SNAP_BASE, "account_type": "human"}
        types = [i["issue_type"] for i in classify_snapshot(snap)]
        self.assertIn("inactive_or_wrong_account_type", types)


class CapabilityGenerationDriftFixture(TestCase):
    """Fixture: applied gen N-1 → applied_generation_stale / observed_config_drift."""

    DESIRED = {
        "desired_generation": 5,
        "desired_config_hash": "abc123",
        "effective_capabilities": ["backend", "testing"],
    }

    def _healthy_snap(self) -> dict:
        return {
            "canonical_email": "kei@agents.shizuha.io",
            "platform_user_id": "99",
            "account_type": "agent",
            "account_active": True,
            "desired_generation_seen": 5,
            "applied_generation": 5,
            "observed_generation": 5,
            "observed_config_hash": "abc123",
            "effective_capabilities": ["backend", "testing"],
        }

    def test_healthy_snap_no_generation_issues(self):
        """All generations aligned → no generation/drift issues."""
        issues = classify_snapshot(self._healthy_snap(), self.DESIRED)
        gen_issues = [i for i in issues if "generation" in i["issue_type"] or "drift" in i["issue_type"]]
        self.assertEqual(gen_issues, [])

    def test_applied_generation_stale(self):
        """applied_generation = N-1 while desired = N → applied_generation_stale."""
        snap = {**self._healthy_snap(), "applied_generation": 4}
        issues = classify_snapshot(snap, self.DESIRED)
        types = [i["issue_type"] for i in issues]
        self.assertIn("applied_generation_stale", types)
        issue = next(i for i in issues if i["issue_type"] == "applied_generation_stale")
        self.assertEqual(issue["expected"], 5)
        self.assertEqual(issue["actual"], 4)

    def test_desired_generation_unseen(self):
        """daemon seen gen N-2 while Hive desired N → desired_generation_unseen."""
        snap = {**self._healthy_snap(), "desired_generation_seen": 3, "applied_generation": 3}
        issues = classify_snapshot(snap, self.DESIRED)
        types = [i["issue_type"] for i in issues]
        self.assertIn("desired_generation_unseen", types)
        self.assertIn("applied_generation_stale", types)

    def test_observed_config_drift(self):
        """applied gen matches but hash differs → observed_config_drift."""
        snap = {**self._healthy_snap(), "observed_config_hash": "deadbeef"}
        issues = classify_snapshot(snap, self.DESIRED)
        types = [i["issue_type"] for i in issues]
        self.assertIn("observed_config_drift", types)

    def test_no_generation_check_without_desired_state(self):
        """When no desired_state provided, generation issues must not fire."""
        snap = {**self._healthy_snap(), "applied_generation": 1}
        issues = classify_snapshot(snap, desired_state=None)
        gen = [i for i in issues if "generation" in i["issue_type"]]
        self.assertEqual(gen, [])

    def test_capability_grant_missing(self):
        """Desired cap present but not in observed → capability_grant_missing."""
        snap = {**self._healthy_snap(), "effective_capabilities": ["backend"]}  # missing 'testing'
        issues = classify_snapshot(snap, self.DESIRED)
        types = [i["issue_type"] for i in issues]
        self.assertIn("capability_grant_missing", types)
        missing_issues = [i for i in issues if i["issue_type"] == "capability_grant_missing"]
        self.assertTrue(any("testing" in str(i["expected"]) for i in missing_issues))

    def test_unexpected_capability_grant(self):
        """Observed cap not in desired → unexpected_capability_grant."""
        snap = {**self._healthy_snap(), "effective_capabilities": ["backend", "testing", "devops"]}
        issues = classify_snapshot(snap, self.DESIRED)
        types = [i["issue_type"] for i in issues]
        self.assertIn("unexpected_capability_grant", types)


class ProviderAndRoutingFixture(TestCase):
    def test_provider_unavailable_fires(self):
        snap = {
            "canonical_email": "agent@agents.shizuha.io",
            "platform_user_id": "1",
            "account_type": "agent",
            "account_active": True,
            "provider_health": {"available": False, "quota_ok": True, "in_backoff": False},
        }
        types = [i["issue_type"] for i in classify_snapshot(snap)]
        self.assertIn("provider_unavailable_or_quota", types)

    def test_provider_backoff_fires(self):
        snap = {
            "canonical_email": "agent@agents.shizuha.io",
            "platform_user_id": "1",
            "account_type": "agent",
            "account_active": True,
            "provider_health": {"available": True, "quota_ok": True, "in_backoff": True},
        }
        types = [i["issue_type"] for i in classify_snapshot(snap)]
        self.assertIn("provider_unavailable_or_quota", types)

    def test_healthy_provider_no_issue(self):
        snap = {
            "canonical_email": "agent@agents.shizuha.io",
            "platform_user_id": "1",
            "account_type": "agent",
            "account_active": True,
            "provider_health": {"available": True, "quota_ok": True, "in_backoff": False},
        }
        types = [i["issue_type"] for i in classify_snapshot(snap)]
        self.assertNotIn("provider_unavailable_or_quota", types)

    def test_roster_unassignable_fires(self):
        snap = {
            "canonical_email": "agent@agents.shizuha.io",
            "platform_user_id": "1",
            "account_type": "agent",
            "account_active": True,
            "pulse_assignable": False,
            "roster_assignable_reason": "TeamRoster email not registered with Pulse resolver",
        }
        issues = classify_snapshot(snap)
        types = [i["issue_type"] for i in issues]
        self.assertIn("roster_unassignable", types)

    def test_missing_credential_grant_fires(self):
        snap = {
            "canonical_email": "agent@agents.shizuha.io",
            "platform_user_id": "1",
            "account_type": "agent",
            "account_active": True,
            "missing_grants": ["github", "kubeconfig"],
        }
        issues = classify_snapshot(snap)
        types = [i["issue_type"] for i in issues]
        self.assertEqual(types.count("credential_grant_missing"), 2)


# ── DB-level persistence smoke test ───────────────────────────────────────────

class HealthSnapshotPersistenceTest(TestCase):
    """Smoke test: AgentRuntimeDesiredState and AgentHealthIssue persist correctly."""

    def setUp(self):
        from .models import (
            DaemonCredential, DaemonRegistry, FleetAgent,
            AgentRuntimeDesiredState,
        )
        self.agent = FleetAgent.objects.create(
            agent_username='hive296test',
            display_name='HIVE-296 Test Agent',
            role='Engineer',
        )
        DaemonCredential.objects.create(daemon_id='hive296-daemon', token_hash='hive296-token-hash')
        self.daemon = DaemonRegistry.objects.create(
            daemon_id='hive296-daemon',
            fleet_id='hive296-fleet',
            status=DaemonRegistry.Status.CONNECTED,
        )
        self.desired = AgentRuntimeDesiredState.objects.create(
            agent=self.agent,
            desired_generation=3,
            desired_config_hash='abc999',
            effective_capabilities=['backend', 'testing'],
            canonical_email='hive296test@agents.shizuha.io',
            source=AgentRuntimeDesiredState.Source.TEAM_ROSTER_SYNC,
        )

    def test_desired_state_created(self):
        from .models import AgentRuntimeDesiredState
        ds = AgentRuntimeDesiredState.objects.get(agent=self.agent)
        self.assertEqual(ds.desired_generation, 3)
        self.assertEqual(ds.effective_capabilities, ['backend', 'testing'])

    def test_snapshot_and_issues_persisted(self):
        from .models import AgentHealthSnapshot, AgentHealthIssue
        now = timezone.now()
        snap = AgentHealthSnapshot.objects.create(
            agent=self.agent,
            daemon=self.daemon,
            runtime_username='hive296test',
            runtime_email='hive296test@shizuha.com',
            canonical_email='hive296test@agents.shizuha.io',
            platform_user_id='99',
            account_type='agent',
            account_active=True,
            desired_generation_seen=2,
            applied_generation=2,
            observed_config_hash='stale999',
            snapshot_type=AgentHealthSnapshot.SnapshotType.FULL,
        )
        # Persist issues for applied_generation_stale
        AgentHealthIssue.objects.create(
            agent=self.agent,
            snapshot=snap,
            issue_type=AgentHealthIssue.IssueType.APPLIED_GENERATION_STALE,
            severity=AgentHealthIssue.Severity.MEDIUM,
            service='fleet',
            field_path='runtime.applied_generation',
            expected=3,
            actual=2,
            detail='Daemon applied generation 2 is behind desired 3.',
            status=AgentHealthIssue.Status.OPEN,
            first_seen=now,
            last_seen=now,
        )
        open_issues = AgentHealthIssue.objects.filter(
            agent=self.agent, status=AgentHealthIssue.Status.OPEN,
        )
        self.assertEqual(open_issues.count(), 1)
        self.assertEqual(open_issues.first().issue_type, 'applied_generation_stale')

    def test_resolved_issues_excluded_from_open(self):
        from .models import AgentHealthSnapshot, AgentHealthIssue
        now = timezone.now()
        snap = AgentHealthSnapshot.objects.create(
            agent=self.agent,
            daemon=self.daemon,
            snapshot_type=AgentHealthSnapshot.SnapshotType.FULL,
        )
        issue = AgentHealthIssue.objects.create(
            agent=self.agent,
            snapshot=snap,
            issue_type=AgentHealthIssue.IssueType.APPLIED_GENERATION_STALE,
            severity='medium',
            service='fleet',
            field_path='runtime.applied_generation',
            status=AgentHealthIssue.Status.OPEN,
            first_seen=now,
            last_seen=now,
        )
        issue.status = AgentHealthIssue.Status.RESOLVED
        issue.resolved_at = now
        issue.save()

        self.assertEqual(
            AgentHealthIssue.objects.filter(
                agent=self.agent, status=AgentHealthIssue.Status.OPEN
            ).count(),
            0,
        )


class Hive313PersistenceRegressionTest(TestCase):
    """HIVE-313: first_seen survives re-fires; multi-instance issues merge; delta retention."""

    def setUp(self):
        from .models import (
            DaemonCredential, DaemonRegistry, FleetAgent,
            AgentRuntimeDesiredState,
        )
        self.agent = FleetAgent.objects.create(
            agent_username='hive313test',
            display_name='HIVE-313 Test Agent',
            role='Engineer',
        )
        DaemonCredential.objects.create(daemon_id='hive313-daemon', token_hash='hive313-token-hash')
        self.daemon = DaemonRegistry.objects.create(
            daemon_id='hive313-daemon',
            fleet_id='hive313-fleet',
            status=DaemonRegistry.Status.CONNECTED,
        )
        AgentRuntimeDesiredState.objects.create(
            agent=self.agent,
            desired_generation=1,
            canonical_email='hive313test@agents.shizuha.io',
            credential_grant_expectations=['github', 'kubeconfig'],
            source=AgentRuntimeDesiredState.Source.TEAM_ROSTER_SYNC,
        )

    def _persist(self, frame, snap_type='full'):
        from .consumers import DaemonLinkConsumer

        consumer = DaemonLinkConsumer()
        consumer._registry_pk = self.daemon.pk
        consumer._daemon_id = self.daemon.daemon_id
        # HIVE-365: run persistence in this TestCase's DB connection/thread.
        # The production consumer still awaits the database_sync_to_async wrapper;
        # these regression tests exercise the same sync body without asgiref's
        # worker-thread connection lifecycle, which made PostgreSQL CI close the
        # transactional test connection between assertions.
        consumer._persist_health_snapshot_sync(
            frame, frame.get('child_id', 'hive313test'), snap_type,
        )

    def _drift_frame(self):
        # desired_generation=1, daemon saw 0 -> desired_generation_unseen fires
        return {
            'child_id': 'hive313test',
            'runtime_username': 'hive313test',
            'runtime_email': 'hive313test@shizuha.com',
            'canonical_email': 'hive313test@agents.shizuha.io',
            'account_type': 'agent',
            'account_active': True,
            'desired_generation_seen': 0,
            'missing_grants': ['github', 'kubeconfig'],
            'credential_grant_health': {},
        }

    def test_first_seen_survives_refire(self):
        from .models import AgentHealthIssue
        self._persist(self._drift_frame())
        issue = AgentHealthIssue.objects.get(
            agent=self.agent,
            issue_type=AgentHealthIssue.IssueType.DESIRED_GENERATION_UNSEEN,
            status=AgentHealthIssue.Status.OPEN,
        )
        original_first_seen = issue.first_seen

        self._persist(self._drift_frame())
        issue.refresh_from_db()
        self.assertEqual(issue.first_seen, original_first_seen)
        self.assertGreater(issue.last_seen, original_first_seen)

    def test_multi_instance_issue_merges_all_instances(self):
        from .models import AgentHealthIssue
        self._persist(self._drift_frame())
        rows = AgentHealthIssue.objects.filter(
            agent=self.agent,
            issue_type=AgentHealthIssue.IssueType.CREDENTIAL_GRANT_MISSING,
            status=AgentHealthIssue.Status.OPEN,
        )
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        as_text = f'{row.expected} {row.actual} {row.field_path} {row.detail}'
        self.assertIn('github', as_text)
        self.assertIn('kubeconfig', as_text)

    def test_merge_helper_single_instance_passthrough(self):
        from .consumers import DaemonLinkConsumer
        classified = [{
            'issue_type': 'roster_unassignable', 'severity': 'medium', 'service': 'pulse',
            'field_path': 'a', 'expected': 1, 'actual': 2, 'detail': 'd',
        }]
        merged = DaemonLinkConsumer._merge_issue_instances(classified)
        self.assertEqual(merged['roster_unassignable'], classified[0])

    def test_merge_helper_keeps_worst_severity(self):
        from .consumers import DaemonLinkConsumer
        classified = [
            {'issue_type': 't', 'severity': 'low', 'service': 's',
             'field_path': 'p1', 'expected': 'e1', 'actual': 'a1', 'detail': 'd1'},
            {'issue_type': 't', 'severity': 'critical', 'service': 's',
             'field_path': 'p2', 'expected': 'e2', 'actual': 'a2', 'detail': 'd2'},
        ]
        merged = DaemonLinkConsumer._merge_issue_instances(classified)['t']
        self.assertEqual(merged['severity'], 'critical')
        self.assertEqual(merged['expected'], ['e1', 'e2'])
        self.assertEqual(merged['actual'], ['a1', 'a2'])
        self.assertEqual(merged['field_path'], 'p1,p2')
        self.assertEqual(merged['detail'], 'd1; d2')

    def test_delta_snapshots_capped(self):
        from .models import AgentHealthSnapshot
        from . import consumers as consumers_mod
        original = consumers_mod._DELTA_SNAPSHOT_RETENTION
        consumers_mod._DELTA_SNAPSHOT_RETENTION = 3
        try:
            for _ in range(5):
                self._persist(self._drift_frame(), snap_type='delta')
        finally:
            consumers_mod._DELTA_SNAPSHOT_RETENTION = original
        self.assertEqual(
            AgentHealthSnapshot.objects.filter(
                agent=self.agent, daemon=self.daemon, snapshot_type='delta',
            ).count(),
            3,
        )
