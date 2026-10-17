import json
import os
import tempfile
import unittest
from unittest.mock import patch

from security.tenant_isolation_probe import ProbeCase, build_default_cases, evaluate, load_cases, run_cases


class TenantIsolationProbeTests(unittest.TestCase):
    def test_deny_status_passes_negative_probe(self):
        case = ProbeCase(service="wiki", name="wrong org", method="GET", url="https://wiki/api", actor="a")
        ok, message = evaluate(case, requester=lambda _: (403, "forbidden"))
        self.assertTrue(ok, message)

    def test_marker_leak_fails_even_on_200(self):
        case = ProbeCase(
            service="pulse",
            name="aggregate leak",
            method="GET",
            url="https://pulse/api",
            actor="a",
            expect="public",
            forbidden_markers=("ORG_B_PRIVATE_TASK",),
        )
        ok, message = evaluate(case, requester=lambda _: (200, '{"title":"ORG_B_PRIVATE_TASK"}'))
        self.assertFalse(ok)
        self.assertIn("leaked", message)

    def test_non_leaking_2xx_passes_when_endpoint_aggregates(self):
        case = ProbeCase(
            service="drive",
            name="aggregate no leak",
            method="GET",
            url="https://drive/api",
            actor="a",
            expect="public",
            forbidden_markers=("org-b-file-id",),
        )
        ok, message = evaluate(case, requester=lambda _: (200, "[]"))
        self.assertTrue(ok, message)

    def test_deny_wrong_org_object_by_id_200_fails(self):
        # A cross-org GET /object-b-id that returns 200 without echoing the marker
        # must FAIL: the object was served across the org boundary regardless of body.
        case = ProbeCase(
            service="pulse",
            name="wrong-org object-by-id",
            method="GET",
            url="https://pulse/api/tasks/ORG-B-TASK-ID/",
            actor="a",
            organization_id="200",
            forbidden_markers=("org-b-task",),
        )
        ok, message = evaluate(case, requester=lambda _: (200, '{"id":"ORG-B-TASK-ID","status":"open"}'))
        self.assertFalse(ok, message)
        self.assertIn("not refused", message)

    def test_deny_wrong_org_write_201_fails(self):
        # A cross-org POST that returns 201 without echoing the marker must FAIL:
        # the write was accepted under org B even though the response body is clean.
        case = ProbeCase(
            service="books",
            name="wrong-org write",
            method="POST",
            url="https://books/api/companies/",
            actor="a",
            organization_id="200",
            json_body={"name": "should-not-create"},
            forbidden_markers=("org-b-company",),
        )
        ok, message = evaluate(case, requester=lambda _: (201, '{"id":"new-id","name":"should-not-create"}'))
        self.assertFalse(ok, message)
        self.assertIn("not refused", message)

    def test_deny_marker_leak_on_2xx_reports_leak(self):
        # When a cross-org 2xx also echoes the marker, the leak is the reported reason.
        case = ProbeCase(
            service="wiki",
            name="wrong-org read leak",
            method="GET",
            url="https://wiki/api/pages/ORG-B-PAGE/",
            actor="a",
            organization_id="200",
            forbidden_markers=("org-b-page",),
        )
        ok, message = evaluate(case, requester=lambda _: (200, '{"slug":"org-b-page"}'))
        self.assertFalse(ok, message)
        self.assertIn("leaked", message)

    def test_case_file_loads_explicit_wrong_org_and_write_probe(self):
        payload = {
            "cases": [{
                "service": "books",
                "name": "wrong-org write",
                "method": "POST",
                "url": "https://books/api/companies/",
                "actor": "a",
                "organization_id": 200,
                "json": {"name": "should-not-create"},
                "forbidden_markers": ["org-b-company"],
            }]
        }
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            json.dump(payload, fh)
            path = fh.name
        try:
            cases = load_cases(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].method, "POST")
        self.assertEqual(cases[0].organization_id, "200")
        self.assertEqual(cases[0].json_body, {"name": "should-not-create"})

    def test_default_cases_cover_required_services_when_markers_are_configured(self):
        env = {
            "TENANT_PROBE_ORG_A_ID": "100",
            "TENANT_PROBE_ORG_B_ID": "200",
            "TENANT_PROBE_PULSE_ORG_B_MARKER": "pulse-b",
            "TENANT_PROBE_WIKI_ORG_B_MARKER": "wiki-b",
            "TENANT_PROBE_CONNECT_ORG_B_MARKER": "connect-b",
            "TENANT_PROBE_DRIVE_ORG_B_MARKER": "drive-b",
            "TENANT_PROBE_BOOKS_ORG_B_MARKER": "books-b",
        }
        with patch.dict(os.environ, env, clear=True):
            cases = build_default_cases()
        self.assertEqual({c.service for c in cases}, {"pulse", "wiki", "connect", "drive", "books"})
        self.assertTrue(all(c.forbidden_markers for c in cases))
        self.assertIn("actor-a wrong explicit org-b", {c.name.split(": ", 1)[1] for c in cases})

    def test_run_cases_returns_failure_count(self):
        cases = [ProbeCase(service="connect", name="leak", method="GET", url="x", actor="a", forbidden_markers=("b",))]
        self.assertEqual(run_cases(cases, requester=lambda _: (200, "contains b")), 1)


if __name__ == "__main__":
    unittest.main()
