"""RBAC bound-surface drift audit (PLAT-10277 fix-contract item 3, PLAT-11410).

Pages when a CI identity's bound RBAC surface SHRINKS: any RoleBinding /
ClusterRoleBinding removal, or any verb/resource narrowing on a still-bound
role, relative to the last-known-good baseline.

The 2026-09-29 incident — a bare, unlabeled `hive-ci-ns-reader`
ClusterRoleBinding applied with no intended Role wired — was invisible to
every reviewed source because it left no registry entry. A bound-surface diff
per lane ServiceAccount (namespace, role kind/name, verbs, resources,
resourceNames) would have caught it within one audit cycle.

Usage:
    python -m security.rbac_drift_audit --baseline k8s/rbac-drift-baseline.json
    python -m security.rbac_drift_audit --capture k8s/rbac-drift-baseline.json

Exit codes:
    0  surface stable (no shrinkage) or capture succeeded
    2  SHRINKAGE DETECTED — this is the page signal
    3  cluster access failure (cannot collect live surface)
    4  malformed baseline file

The audit is strictly read-only: it only ever runs `kubectl get` verbs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

# Default lane identities to audit. Extend via --identity (repeatable).
# Each entry is "serviceaccount-namespace:name".
DEFAULT_IDENTITIES: Tuple[str, ...] = (
    "build:hive-ci-deployer",
    "build:origin-ci-deployer",
)


def run_kubectl(args: List[str]) -> str:
    """Run a read-only kubectl command and return stdout. Raises RuntimeError
    with stderr on failure."""
    proc = subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "kubectl {} failed (rc={}): {}".format(
                " ".join(args), proc.returncode, (proc.stderr or "").strip()
            )
        )
    return proc.stdout


def parse_identity(spec: str) -> Tuple[str, str]:
    """Parse 'ns:name' into (ns, name)."""
    if ":" not in spec:
        raise ValueError("identity must be 'namespace:name', got {!r}".format(spec))
    ns, name = spec.split(":", 1)
    if not ns or not name:
        raise ValueError("identity must be 'namespace:name', got {!r}".format(spec))
    return ns, name


def _subject_matches(subject: Dict[str, Any], ns: str, name: str) -> bool:
    return (
        subject.get("kind") == "ServiceAccount"
        and subject.get("name") == name
        and subject.get("namespace") == ns
    )


def _rule_entry(rule: Dict[str, Any]) -> Dict[str, List[str]]:
    return {
        "verbs": sorted(set(rule.get("verbs") or [])),
        "resources": sorted(set(rule.get("resources") or [])),
        "resourceNames": sorted(set(rule.get("resourceNames") or [])),
    }


def _flatten_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, List[str]]]:
    return [_rule_entry(r) for r in (rules or [])]


def collect_identity_surface(
    ns: str, name: str, rolebindings_json: str, clusterrolebindings_json: str
) -> List[Dict[str, Any]]:
    """Collect the bound surface of one ServiceAccount from pre-fetched
    RoleBinding / ClusterRoleBinding lists (cluster-wide). Returns a sorted
    list of entry dicts:

        {
          "binding": "RoleBinding" | "ClusterRoleBinding",
          "bindingNamespace": "<ns or ''>",
          "bindingName": "<name>",
          "roleKind": "Role" | "ClusterRole",
          "roleName": "<name>",
          "rules": [ {verbs, resources, resourceNames}, ... ],
        }
    """
    entries: List[Dict[str, Any]] = []
    for rb in json.loads(rolebindings_json).get("items", []):
        subjects = rb.get("subjects") or []
        if not any(_subject_matches(s, ns, name) for s in subjects):
            continue
        ref = rb.get("roleRef") or {}
        entries.append(
            {
                "binding": "RoleBinding",
                "bindingNamespace": rb["metadata"]["namespace"],
                "bindingName": rb["metadata"]["name"],
                "roleKind": ref.get("kind", ""),
                "roleName": ref.get("name", ""),
                "rules": [],  # resolved later by resolve_rules()
            }
        )
    for crb in json.loads(clusterrolebindings_json).get("items", []):
        subjects = crb.get("subjects") or []
        if not any(_subject_matches(s, ns, name) for s in subjects):
            continue
        ref = crb.get("roleRef") or {}
        entries.append(
            {
                "binding": "ClusterRoleBinding",
                "bindingNamespace": "",
                "bindingName": crb["metadata"]["name"],
                "roleKind": ref.get("kind", ""),
                "roleName": ref.get("name", ""),
                "rules": [],
            }
        )
    return sorted(entries, key=lambda e: (e["binding"], e["bindingNamespace"], e["bindingName"], e["roleKind"], e["roleName"]))


def resolve_rules(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fill in the rules for each entry by fetching the referenced Role or
    ClusterRole from the live cluster."""
    resolved = []
    cache: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for e in entries:
        key = (e["roleKind"], e["bindingNamespace"], e["roleName"])
        if key not in cache:
            if e["roleKind"] == "ClusterRole":
                raw = run_kubectl(["get", "clusterrole", e["roleName"], "-o", "json"])
            else:
                raw = run_kubectl(
                    ["get", "role", e["roleName"], "-n", e["bindingNamespace"], "-o", "json"]
                )
            cache[key] = json.loads(raw).get("rules") or []
        e = dict(e, rules=[_rule_entry(r) for r in cache[key]])
        resolved.append(e)
    return resolved


def entry_key(e: Dict[str, Any]) -> Tuple[Any, ...]:
    """Identity of a bound entry: the binding object and its role reference.
    Two entries are the same bound grant iff all of these match."""
    return (
        e["binding"],
        e["bindingNamespace"],
        e["bindingName"],
        e["roleKind"],
        e["roleName"],
    )


def _rule_set(entry: Dict[str, Any]) -> Set[Tuple[Any, ...]]:
    return {
        (tuple(r["verbs"]), tuple(r["resources"]), tuple(r["resourceNames"]))
        for r in entry.get("rules", [])
    }


def _coverage(entry: Dict[str, Any]) -> Set[Tuple[Any, ...]]:
    """Expanded grant coverage of one entry: every (verb, resource,
    resourceName) triple the rules allow."""
    cov: Set[Tuple[Any, ...]] = set()
    for r in entry.get("rules", []):
        verbs = r["verbs"] or [""]
        resources = r["resources"] or [""]
        names = r["resourceNames"] or [""]
        for v in verbs:
            for res in resources:
                for n in names:
                    cov.add((v, res, n))
    return cov


def _strict_subset(new_rules: Set[Tuple[Any, ...]], old_rules: Set[Tuple[Any, ...]]) -> bool:
    return bool(new_rules) and new_rules != old_rules and new_rules.issubset(old_rules)


def diff_surfaces(
    baseline: List[Dict[str, Any]], current: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """Compare baseline vs current surface. Returns a dict of shrinkage
    classes:

        removed:           baseline entries no longer bound
        narrowed:          entries still bound, but with strictly fewer
                           verbs/resources/resourceNames than baseline
    """
    base_by_key = {entry_key(e): e for e in baseline}
    curr_by_key = {entry_key(e): e for e in current}
    removed = []
    narrowed = []
    for key, old_entry in sorted(base_by_key.items()):
        new_entry = curr_by_key.get(key)
        if new_entry is None:
            removed.append(old_entry)
            continue
        old_set = _coverage(old_entry)
        new_set = _coverage(new_entry)
        if _strict_subset(new_set, old_set):
            narrowed.append(
                {
                    "entry": new_entry,
                    "baselineRules": [dict(r) for r in old_entry.get("rules", [])],
                }
            )
    return {"removed": removed, "narrowed": narrowed}


def load_baseline(path: str) -> Dict[str, Any]:
    """Load and validate a baseline file. Raises ValueError on malformed
    content."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "identities" not in data:
        raise ValueError("baseline must be an object with an 'identities' mapping")
    identities = data["identities"]
    if not isinstance(identities, dict):
        raise ValueError("'identities' must be a mapping")
    for spec, entries in identities.items():
        parse_identity(spec)
        if not isinstance(entries, list):
            raise ValueError("identities[{!r}] must be a list".format(spec))
        for e in entries:
            for field in ("binding", "bindingNamespace", "bindingName", "roleKind", "roleName", "rules"):
                if field not in e:
                    raise ValueError(
                        "entry missing field {!r} in identity {!r}".format(field, spec)
                    )
    return data


def run_live_audit(identity_specs: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Collect the current bound surface for each identity from the live
    cluster. Two list calls total (RBAC cluster-wide), plus per-role fetches."""
    ns_list_arg = ["get", "rolebindings", "-A", "-o", "json"]
    try:
        rb_json = run_kubectl(ns_list_arg)
        crb_json = run_kubectl(["get", "clusterrolebindings", "-o", "json"])
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        raise ClusterAccessError(str(exc)) from exc
    surfaces: Dict[str, List[Dict[str, Any]]] = {}
    for spec in identity_specs:
        ns, name = parse_identity(spec)
        entries = collect_identity_surface(ns, name, rb_json, crb_json)
        surfaces[spec] = resolve_rules(entries)
    return surfaces


class ClusterAccessError(Exception):
    pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="RBAC bound-surface drift audit")
    parser.add_argument(
        "--baseline",
        default="k8s/rbac-drift-baseline.json",
        help="path to the last-known-good baseline JSON",
    )
    parser.add_argument(
        "--capture",
        metavar="PATH",
        help="capture the current live surface into PATH and exit (no diff)",
    )
    parser.add_argument(
        "--identity",
        action="append",
        default=[],
        help="audit this identity ('ns:name'); repeatable. Default: built-in lane list",
    )
    parser.add_argument(
        "--fail-on-missing-baseline",
        action="store_true",
        default=True,
        help="(default) exit 4 when the baseline file does not exist",
    )
    args = parser.parse_args(argv)

    identity_specs = args.identity or list(DEFAULT_IDENTITIES)

    if args.capture:
        try:
            surfaces = run_live_audit(identity_specs)
        except ClusterAccessError as exc:
            print("CAPTURE FAILED — cluster access error: {}".format(exc), file=sys.stderr)
            return 3
        with open(args.capture, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "schema": "shizuha.rbac-drift-baseline.v1",
                    "capturedAt": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    "identities": surfaces,
                },
                fh,
                indent=2,
                sort_keys=True,
            )
            fh.write("\n")
        print("captured baseline for {} identity(ies) into {}".format(len(identity_specs), args.capture))
        return 0

    try:
        baseline_data = load_baseline(args.baseline)
    except FileNotFoundError:
        print(
            "BASELINE MISSING: {} — run `python -m security.rbac_drift_audit "
            "--capture <path>` on a seat with cluster access, commit it, then "
            "re-run.".format(args.baseline),
            file=sys.stderr,
        )
        return 4
    except (json.JSONDecodeError, ValueError) as exc:
        print("BASELINE MALFORMED: {}".format(exc), file=sys.stderr)
        return 4

    try:
        current = run_live_audit(identity_specs)
    except ClusterAccessError as exc:
        print("AUDIT ABORTED — cluster access error: {}".format(exc), file=sys.stderr)
        return 3

    findings = {"removed": [], "narrowed": []}
    for spec in identity_specs:
        baseline_entries = baseline_data["identities"].get(spec, [])
        result = diff_surfaces(baseline_entries, current.get(spec, []))
        findings["removed"].extend(result["removed"])
        findings["narrowed"].extend(result["narrowed"])

    if findings["removed"] or findings["narrowed"]:
        print("RBAC SHRINKAGE DETECTED for lane identity(ies):", file=sys.stderr)
        print(json.dumps(findings, indent=2, sort_keys=True), file=sys.stderr)
        print(
            "REMEDIATION: see Alert Runbooks (Operations space) — "
            "'RBAC bound-surface shrink on a CI lane identity'. "
            "Do NOT blind-apply RBAC; reconcile the intended principal first.",
            file=sys.stderr,
        )
        return 2

    print("RBAC bound surface stable for {} identity(ies).".format(len(identity_specs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
