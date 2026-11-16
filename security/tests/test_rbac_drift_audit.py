"""Unit tests for the RBAC bound-surface drift audit (PLAT-11410 acceptance).

Acceptance contract (from PLAT-11410):
  * a deliberately-shrunk binding on a test identity fires the alert
    (diff_surfaces reports it) within one audit cycle;
  * no alert on a stable surface;
  * verb narrowing on a still-bound role also fires.

All fixtures are in-memory; no cluster access is required.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from security import rbac_drift_audit as audit  # noqa: E402


def rb(namespace, name, role_kind="ClusterRole", role_name="hive-ci-admin"):
    return {
        "binding": "RoleBinding",
        "bindingNamespace": namespace,
        "bindingName": name,
        "roleKind": role_kind,
        "roleName": role_name,
        "rules": [
            {
                "verbs": ["get", "list", "watch"],
                "resources": ["deployments", "jobs", "pods"],
                "resourceNames": [],
            }
        ],
    }


def crb(name, role_name="hive-ci-ns-reader"):
    return {
        "binding": "ClusterRoleBinding",
        "bindingNamespace": "",
        "bindingName": name,
        "roleKind": "ClusterRole",
        "roleName": role_name,
        "rules": [
            {
                "verbs": ["get", "list", "watch"],
                "resources": ["namespaces"],
                "resourceNames": [],
            }
        ],
    }


class TestDiffSurfaces(unittest.TestCase):
    def test_removed_binding_fires(self):
        """Acceptance: a deliberately-shrunk (removed) binding fires the alert."""
        baseline = [rb("build", "hive-ci-admin"), crb("hive-ci-ns-reader")]
        current = [rb("build", "hive-ci-admin")]
        result = audit.diff_surfaces(baseline, current)
        self.assertEqual(len(result["removed"]), 1)
        self.assertEqual(result["removed"][0]["bindingName"], "hive-ci-ns-reader")
        # A shrinkage in either class is the page signal.
        self.assertTrue(result["removed"] or result["narrowed"])

    def test_stable_surface_no_alert(self):
        """Acceptance: no alert on a stable surface."""
        baseline = [rb("build", "hive-ci-admin"), crb("hive-ci-ns-reader")]
        current = [rb("build", "hive-ci-admin"), crb("hive-ci-ns-reader")]
        result = audit.diff_surfaces(baseline, current)
        self.assertEqual(result, {"removed": [], "narrowed": []})

    def test_verb_narrowing_fires(self):
        """Acceptance: verb narrowing on a still-bound role fires the alert."""
        baseline = [rb("build", "hive-ci-admin")]
        shrunk = json.loads(json.dumps(baseline[0]))
        shrunk["rules"][0]["verbs"] = ["get"]  # list/watch dropped
        result = audit.diff_surfaces(baseline, [shrunk])
        self.assertEqual(result["removed"], [])
        self.assertEqual(len(result["narrowed"]), 1)
        self.assertEqual(
            result["narrowed"][0]["entry"]["rules"][0]["verbs"], ["get"]
        )

    def test_resource_narrowing_fires(self):
        """Dropping a resource class (e.g. jobs) is also a page."""
        baseline = [rb("build", "hive-ci-admin")]
        shrunk = json.loads(json.dumps(baseline[0]))
        shrunk["rules"][0]["resources"] = ["deployments", "pods"]  # jobs dropped
        result = audit.diff_surfaces(baseline, [shrunk])
        self.assertEqual(len(result["narrowed"]), 1)

    def test_growth_is_not_a_page(self):
        """A grown surface (more verbs/resources) is not a shrinkage."""
        baseline = [rb("build", "hura-read")]
        grown = json.loads(json.dumps(baseline[0]))
        grown["rules"][0]["verbs"] = ["get", "list", "watch", "create"]
        grown["rules"][0]["resources"] = ["deployments", "jobs", "pods", "configmaps"]
        result = audit.diff_surfaces(baseline, [grown])
        self.assertEqual(result, {"removed": [], "narrowed": []})

    def test_nonempty_surface_never_counts_as_narrowed_from_nothing(self):
        """An identity that had NO grants and gains one cannot be 'narrowed'."""
        baseline = []
        current = [rb("build", "new-binding")]
        result = audit.diff_surfaces(baseline, current)
        self.assertEqual(result, {"removed": [], "narrowed": []})


class TestBaselineIO(unittest.TestCase):
    def test_load_baseline_roundtrip(self):
        data = {
            "schema": "shizuha.rbac-drift-baseline.v1",
            "identities": {"build:hive-ci-deployer": [rb("build", "hive-ci-admin")]},
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(data, fh)
            path = fh.name
        try:
            loaded = audit.load_baseline(path)
            self.assertEqual(loaded["identities"]["build:hive-ci-deployer"], data["identities"]["build:hive-ci-deployer"])
        finally:
            os.unlink(path)

    def test_load_baseline_rejects_malformed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"not": "the right shape"}')
            path = fh.name
        try:
            with self.assertRaises(ValueError):
                audit.load_baseline(path)
        finally:
            os.unlink(path)

    def test_entry_key_distinguishes_bindings(self):
        self.assertNotEqual(audit.entry_key(rb("build", "a")), audit.entry_key(rb("build", "b")))
        self.assertNotEqual(audit.entry_key(rb("build", "a")), audit.entry_key(crb("a")))


class TestCollectSurface(unittest.TestCase):
    """collect_identity_surface filters cluster-wide binding lists by subject."""

    def _fixtures(self):
        rb_list = {
            "items": [
                {
                    "metadata": {"namespace": "build", "name": "hive-ci-admin"},
                    "subjects": [
                        {"kind": "ServiceAccount", "name": "hive-ci-deployer", "namespace": "build"}
                    ],
                    "roleRef": {"kind": "ClusterRole", "name": "hive-ci-admin"},
                },
                {
                    "metadata": {"namespace": "staging", "name": "other"},
                    "subjects": [
                        {"kind": "ServiceAccount", "name": "someone-else", "namespace": "staging"}
                    ],
                    "roleRef": {"kind": "Role", "name": "other"},
                },
            ]
        }
        crb_list = {
            "items": [
                {
                    "metadata": {"name": "hive-ci-ns-reader"},
                    "subjects": [
                        {"kind": "ServiceAccount", "name": "hive-ci-deployer", "namespace": "build"}
                    ],
                    "roleRef": {"kind": "ClusterRole", "name": "hive-ci-ns-reader"},
                }
            ]
        }
        return json.dumps(rb_list), json.dumps(crb_list)

    def test_collect_identity_surface(self):
        rb_json, crb_json = self._fixtures()
        entries = audit.collect_identity_surface("build", "hive-ci-deployer", rb_json, crb_json)
        self.assertEqual(len(entries), 2)
        kinds = {(e["binding"], e["roleName"]) for e in entries}
        self.assertEqual(kinds, {("RoleBinding", "hive-ci-admin"), ("ClusterRoleBinding", "hive-ci-ns-reader")})

    def test_collect_ignores_other_identities(self):
        rb_json, crb_json = self._fixtures()
        entries = audit.collect_identity_surface("staging", "hive-ci-deployer", rb_json, crb_json)
        self.assertEqual(entries, [])


class TestParseIdentity(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(audit.parse_identity("build:hive-ci-deployer"), ("build", "hive-ci-deployer"))

    def test_invalid(self):
        for bad in ("no-colon", ":name", "ns:", ""):
            with self.assertRaises(ValueError):
                audit.parse_identity(bad)


class TestMainBaselineGuard(unittest.TestCase):
    def test_missing_baseline_exits_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = audit.main(["--baseline", os.path.join(tmp, "missing.json")])
        self.assertEqual(rc, 4)



class TestCommittedBaselineArtifact(unittest.TestCase):
    """Pins the committed k8s/rbac-drift-baseline.json to the intended posture.

    Context: 2026-11-14 origin-outage remediation — the operator intentionally
    REMOVED the `origin-ci-plat6638-netpol-create` RoleBinding in the `origin`
    namespace (origin-ci-deployer is now limited to get/list netpols there).
    The daily audit paged on the stale baseline (run 4175, 2026-11-16); this
    artifact was renewed surgically because the fail-loud payload named the
    full delta (removed=that entry, narrowed=[]). These assertions are
    fail-loud against accidental re-introduction or wider deletions.
    """

    ARTIFACT = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "k8s", "rbac-drift-baseline.json",
    )

    def load(self):
        return audit.load_baseline(self.ARTIFACT)

    def test_artifact_parses(self):
        d = self.load()
        self.assertEqual(d["schema"], "shizuha.rbac-drift-baseline.v1")
        self.assertEqual(set(d["identities"]), {"build:hive-ci-deployer", "build:origin-ci-deployer"})

    def test_origin_netpol_create_not_reintroduced(self):
        d = self.load()
        entries = d["identities"]["build:hive-ci-deployer"]
        self.assertEqual(
            [e for e in entries
             if e["bindingName"] == "origin-ci-plat6638-netpol-create"
             and e["bindingNamespace"] == "origin"],
            [],
            "origin-ns origin-ci-plat6638-netpol-create was intentionally removed "
            "by the operator (2026-11-14 origin outage remediation) — do not "
            "blind-restore it",
        )

    def test_remaining_netpol_create_grants_intact(self):
        d = self.load()
        entries = d["identities"]["build:hive-ci-deployer"]
        namespaced = sorted(
            e["bindingNamespace"] for e in entries
            if e["bindingName"] == "origin-ci-plat6638-netpol-create"
            and e["bindingNamespace"] != ""
        )
        self.assertEqual(
            namespaced,
            ["ai-models", "kura", "mail-edge", "monitoring", "staging"],
            "exactly the five still-intended netpol-create lane grants must "
            "remain in the baseline",
        )


if __name__ == "__main__":
    unittest.main()
