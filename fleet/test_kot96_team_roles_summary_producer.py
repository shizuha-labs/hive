# KOT-96 — producer-only regression for the `team_roles_summary` wire contract.
#
# No server behaviour change. This pins the exact v0.4 Architecture contract
# (handed off on KOT-96 by sora/Architecture):
#   * `team_roles_summary`: JSON array, sorted by `team_slug`; every element has
#     exactly six string keys: team_slug, team_name, role_title,
#     membership_role, tier, source.
#   * `has_multiple_team_roles`: true iff more than one distinct
#     (team_slug, role_title, membership_role) tuple exists.
#   * Staff aggregate emits `[]` / `false` when there are no team memberships.
#
# The Android consumer (HiveFleetModels.kt) was crashing the APK-290 build with
# `Expected a string but was BEGIN_ARRAY` because it modelled this field as a
# String. This file is the producer-side half of the contract lock; the Kotlin
# decoder/rendering half is the companion PR.

from django.test import TestCase

from fleet.models import TeamMembershipRole
from fleet.views import _attach_team_role_summaries


def _agent(**overrides):
    agent = {
        "id": "agent-1",
        "agent_username": "ryo",
        "email": "ryo@shizuha.io",
        "role": "engineer",
    }
    agent.update(overrides)
    return agent


def _summary_agent(*memberships, **overrides):
    return _agent(team_memberships=list(memberships), **overrides)


def _m(team_slug, **kw):
    row = {"team_slug": team_slug}
    row.update(kw)
    return row


class TeamRoleSummaryProducerContractTest(TestCase):
    """Pins the `team_roles_summary` / `has_multiple_team_roles` wire contract."""

    def _attach(self, agents):
        return _attach_team_role_summaries(agents, org_slug="shizuha")

    def _role_edge(self, agent_id, team_slug, *, role_title="", membership_role="member",
                   tier="", source="hive", enabled=True):
        return TeamMembershipRole.objects.create(
            agent_identifier=str(agent_id).strip().lower(),
            team_slug=team_slug,
            organization_slug="shizuha",
            role_title=role_title,
            membership_role=membership_role,
            tier=tier,
            enabled=enabled,
            source=source,
        )

    # ── Emptiness / shape ────────────────────────────────────────────────────

    def test_staff_aggregate_empty_is_empty_array_and_false(self):
        self.assertEqual(self._attach([]), [])

    def test_single_membership_emits_six_string_keys(self):
        agents = [
            _summary_agent(
                _m("engineering", team_name="Engineering", role_title="Lead Engineer",
                   membership_role="lead", tier=1)
            )
        ]
        result = self._attach(agents)
        self.assertEqual(len(result), 1)
        summary = result[0]["team_roles_summary"]
        self.assertEqual(len(summary), 1)
        row = summary[0]
        self.assertEqual(
            set(row.keys()),
            {"team_slug", "team_name", "role_title", "membership_role", "tier", "source"},
        )
        for key in ("team_slug", "team_name", "role_title", "membership_role", "tier", "source"):
            self.assertIsInstance(row[key], str, f"{key} must be a string")
        self.assertEqual(row["team_slug"], "engineering")
        self.assertEqual(row["team_name"], "Engineering")
        self.assertEqual(row["role_title"], "Lead Engineer")
        self.assertEqual(row["membership_role"], "lead")
        self.assertEqual(row["tier"], "1")
        self.assertIsInstance(row["source"], str)
        self.assertIs(result[0]["has_multiple_team_roles"], False)

    def test_edge_row_source_and_role_title_flow_through(self):
        # With a real enabled edge row present, the payload must use the edge's
        # role_title / membership_role / source (the 'hive' path), not fallbacks.
        self._role_edge(
            "ryo@shizuha.io", "engineering",
            role_title="Staff Engineer", membership_role="member", source="hive",
        )
        agents = [
            _summary_agent(
                _m("engineering", team_name="Engineering", role_title="Overridden", tier=1)
            )
        ]
        result = self._attach(agents)
        row = result[0]["team_roles_summary"][0]
        self.assertEqual(row["role_title"], "Staff Engineer")
        self.assertEqual(row["membership_role"], "member")
        self.assertEqual(row["source"], "hive")

    # ── Ordering / distinctness ──────────────────────────────────────────────

    def test_sorted_by_team_slug(self):
        agents = [
            _summary_agent(
                _m("zeta", team_name="Zeta", membership_role="member"),
                _m("alpha", team_name="Alpha", membership_role="lead"),
                _m("mid", team_name="Mid", membership_role="member"),
            )
        ]
        result = self._attach(agents)
        slugs = [row["team_slug"] for row in result[0]["team_roles_summary"]]
        self.assertEqual(slugs, sorted(slugs))
        self.assertEqual(slugs, ["alpha", "mid", "zeta"])

    def test_distinct_roles_across_teams_is_multiple(self):
        agents = [
            _summary_agent(
                _m("eng", team_name="Eng", role_title="Lead", membership_role="lead"),
                _m("ops", team_name="Ops", role_title="Lead", membership_role="lead"),
            )
        ]
        result = self._attach(agents)
        # Distinct tuple includes team_slug — different teams => distinct roles.
        self.assertIs(result[0]["has_multiple_team_roles"], True)

    def test_single_role_not_multiple(self):
        # One membership => exactly one distinct tuple => false.
        agents = [
            _summary_agent(
                _m("eng", team_name="Eng", role_title="Reviewer", membership_role="member")
            )
        ]
        result = self._attach(agents)
        self.assertIs(result[0]["has_multiple_team_roles"], False)

    def test_missing_optional_keys_fall_back_to_defaults(self):
        # Minimal memberships must still produce six string keys with sane
        # fallbacks and never crash the producer.
        agents = [_summary_agent(_m("eng"))]
        result = self._attach(agents)
        summary = result[0]["team_roles_summary"]
        self.assertEqual(len(summary), 1)
        row = summary[0]
        self.assertEqual(
            set(row.keys()),
            {"team_slug", "team_name", "role_title", "membership_role", "tier", "source"},
        )
        for key in ("team_slug", "team_name", "role_title", "membership_role", "tier", "source"):
            self.assertIsInstance(row[key], str)
        self.assertEqual(row["team_slug"], "eng")
        self.assertEqual(row["membership_role"], "member")
        self.assertIs(result[0]["has_multiple_team_roles"], False)
