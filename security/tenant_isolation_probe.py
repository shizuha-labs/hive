"""HIVE-327 cross-org tenant-isolation probe harness.

The probe is intentionally service-agnostic: each case names an actor token,
optional organization header, request, and expected isolation behavior.  It can
run in CI against mock endpoints, or as a scheduled live check with two
non-production orgs/users and marker resources.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

DENY_STATUSES = {401, 403, 404}
PUBLIC_EXPECTED = "public"


@dataclass(frozen=True)
class ProbeCase:
    service: str
    name: str
    method: str
    url: str
    actor: str
    expect: str = "deny"  # allow | deny | public
    organization_id: str | None = None
    json_body: dict[str, Any] | None = None
    forbidden_markers: tuple[str, ...] = field(default_factory=tuple)
    allow_statuses: tuple[int, ...] = (200, 201, 202, 204)
    deny_statuses: tuple[int, ...] = tuple(sorted(DENY_STATUSES))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProbeCase":
        return cls(
            service=str(raw["service"]),
            name=str(raw["name"]),
            method=str(raw.get("method", "GET")).upper(),
            url=str(raw["url"]),
            actor=str(raw.get("actor", "a")),
            expect=str(raw.get("expect", "deny")),
            organization_id=(None if raw.get("organization_id") is None else str(raw.get("organization_id"))),
            json_body=raw.get("json"),
            forbidden_markers=tuple(str(v) for v in raw.get("forbidden_markers", []) if str(v)),
            allow_statuses=tuple(int(v) for v in raw.get("allow_statuses", (200, 201, 202, 204))),
            deny_statuses=tuple(int(v) for v in raw.get("deny_statuses", sorted(DENY_STATUSES))),
        )


def _join(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def build_default_cases() -> list[ProbeCase]:
    """Build live probe cases from env, skipping resources not configured.

    Required common env: TENANT_PROBE_ORG_A_ID, TENANT_PROBE_ORG_B_ID,
    TENANT_PROBE_TOKEN_A, TENANT_PROBE_TOKEN_B.  Per-service marker IDs/terms
    opt cases in; missing markers are skipped rather than guessed.
    """
    org_a = _env("TENANT_PROBE_ORG_A_ID")
    org_b = _env("TENANT_PROBE_ORG_B_ID")
    bases = {
        "pulse": _env("PULSE_BASE_URL", "https://pulse.shizuha.com"),
        "wiki": _env("WIKI_BASE_URL", "https://wiki.shizuha.com"),
        "connect": _env("CONNECT_BASE_URL", "https://connect.shizuha.com"),
        "drive": _env("DRIVE_BASE_URL", "https://drive.shizuha.com"),
        "books": _env("BOOKS_BASE_URL", "https://books.shizuha.com"),
    }
    if not org_a or not org_b:
        return []

    cases: list[ProbeCase] = []

    def add(service: str, name: str, path: str, marker_env: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> None:
        marker = _env(marker_env)
        if not marker:
            return
        # Actor A explicitly asks for org B: must be denied, or at least must not
        # return the org-B marker on aggregate/list/search endpoints.
        cases.append(ProbeCase(
            service=service,
            name=f"{name}: actor-a wrong explicit org-b",
            method=method,
            url=_join(bases[service], path),
            actor="a",
            expect="deny",
            organization_id=org_b,
            json_body=body,
            forbidden_markers=(marker,),
        ))
        # Actor A omits org: allowed aggregators are acceptable, but they must be
        # scoped to actor A's memberships and never include org-B private markers.
        cases.append(ProbeCase(
            service=service,
            name=f"{name}: actor-a omitted-org aggregation excludes org-b",
            method="GET",
            url=_join(bases[service], path),
            actor="a",
            expect="public",
            organization_id=None,
            forbidden_markers=(marker,),
        ))

    add("pulse", "Pulse task search/list", "/api/tasks/?search=tenant-probe", "TENANT_PROBE_PULSE_ORG_B_MARKER")
    add("wiki", "Wiki search/list", "/api/search/?q=tenant-probe", "TENANT_PROBE_WIKI_ORG_B_MARKER")
    add("connect", "Connect conversation lookup", "/api/messaging/conversations/", "TENANT_PROBE_CONNECT_ORG_B_MARKER")
    add("drive", "Drive file list", "/api/files/?search=tenant-probe", "TENANT_PROBE_DRIVE_ORG_B_MARKER")
    add("books", "Books company/list", "/api/companies/?search=tenant-probe", "TENANT_PROBE_BOOKS_ORG_B_MARKER")
    return cases


def load_cases(path: str | None) -> list[ProbeCase]:
    if not path:
        return build_default_cases()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    raw_cases = data["cases"] if isinstance(data, dict) else data
    return [ProbeCase.from_dict(raw) for raw in raw_cases]


def _token_for(actor: str) -> str:
    return _env(f"TENANT_PROBE_TOKEN_{actor.upper()}")


def http_request(case: ProbeCase) -> tuple[int, str]:
    headers = {"Accept": "application/json"}
    token = _token_for(case.actor)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if case.organization_id is not None:
        headers["X-Organization-ID"] = case.organization_id
    data = None
    if case.json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(case.json_body).encode("utf-8")
    req = urllib.request.Request(case.url, data=data, method=case.method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=float(_env("TENANT_PROBE_TIMEOUT", "20"))) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def evaluate(case: ProbeCase, requester: Callable[[ProbeCase], tuple[int, str]] = http_request) -> tuple[bool, str]:
    status, body = requester(case)
    leaked = [marker for marker in case.forbidden_markers if marker and marker in body]
    label = f"{case.service}/{case.name}: status={status}"

    if case.expect == "allow":
        if status not in case.allow_statuses:
            return False, f"{label}; expected allow status in {case.allow_statuses}"
        if leaked:
            return False, f"{label}; allow response leaked forbidden marker(s) {leaked}"
        return True, label

    if case.expect == PUBLIC_EXPECTED:
        if status in case.deny_statuses:
            return True, f"{label}; denied public/aggregate surface"
        if status not in case.allow_statuses:
            return False, f"{label}; unexpected public status"
        if leaked:
            return False, f"{label}; aggregate/public surface leaked forbidden marker(s) {leaked}"
        return True, f"{label}; aggregate/public surface did not include forbidden markers"

    # expect == "deny" (default): strict cross-org rejection. An explicit wrong-org
    # object-by-ID read, write, or list MUST be refused with a deny status. A 2xx here
    # means the cross-org request succeeded even when the body happens not to echo the
    # marker — e.g. object-by-ID GET 200 or a write POST 201 that created under org B —
    # so a non-leaking 2xx is an isolation FAILURE, not a pass. Endpoints where a scoped
    # 2xx is legitimately acceptable (aggregate/list/search that must simply not leak the
    # marker) must opt in via expect="public".
    if leaked:
        return False, f"{label}; leaked forbidden marker(s) {leaked}"
    if status in case.deny_statuses:
        return True, f"{label}; denied as expected"
    return False, (
        f"{label}; expected deny status in {case.deny_statuses}, got {status} — "
        "cross-org request was not refused (use expect=\"public\" for marker-only aggregate surfaces)"
    )


def run_cases(cases: Iterable[ProbeCase], requester: Callable[[ProbeCase], tuple[int, str]] = http_request) -> int:
    failures = 0
    for case in cases:
        ok, message = evaluate(case, requester=requester)
        print(("PASS" if ok else "FAIL") + ": " + message)
        failures += 0 if ok else 1
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run HIVE-327 cross-org tenant-isolation probes")
    parser.add_argument("--cases", help="JSON case file; omitted means build cases from TENANT_PROBE_* env")
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)
    if not cases:
        print("SKIP: no tenant-isolation probe cases configured", file=sys.stderr)
        return 0
    return 1 if run_cases(cases) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
