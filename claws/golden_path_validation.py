"""HIVE-420 golden-path validation harness.

The harness intentionally runs the six-stage HIVE-419 recipe as data: every
stage returns a structured verdict instead of throwing away partial progress.
That lets the recurring driver prove the live journey when it is buildable and,
until then, produce precise gap records for HIVE-418/HIVE-422.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests


class StageStatus:
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    # HIVE-2697 ruling 1: the saga parked at a step whose adapter is
    # deliberately not implemented yet (the HIVE-418 epic's real scope). This
    # is the by-design bridge, not a defect — the scorecard reports it loudly
    # but the run exits 0 instead of red-paging KubeJobFailed.
    BLOCKED_SAGA_WIP = "blocked-saga-wip"


# HIVE-2697 ruling 1 (honesty guard) reworked by HIVE-2715: only THIS park is
# expected — and the expected step is DERIVED from the landed adapter registry
# (claws/provisioning_adapters.expected_first_adapterless_step): the FIRST
# side-effectful step lacking a concrete adapter. As adapters register, the
# bridge allowlist SHRINKS automatically (once id_org registered, a park there
# REDs again; the bridge may only label the next un-implemented leg — today
# `pulse`). A park at any adaptered step, or with any other reason class,
# must keep failing the job. The bridge closes entirely when every
# side-effectful step has an adapter. The HIVE-2697 static frozenset
# ({"id_org"}) is retired by the same change per its own docstring contract.
EXPECTED_SAGA_WIP_STEPS = frozenset()  # deprecated: see expected_first_adapterless_step()


def expected_first_adapterless_step() -> str | None:
    """Registry-derived expected park step (thin seam for tests to stub)."""
    from claws.provisioning_adapters import expected_first_adapterless_step as _derived
    return _derived()

# The reason strings the saga itself writes when it parks for lack of an
# adapter (claws/tenant_provisioning.py, advance_preview). Matched
# case-insensitively against the parked step's message.
SAGA_WIP_REASON_MARKERS = (
    "adapter not enabled yet",
    "no concrete adapter is implemented",
)


BRIEFS: dict[str, dict[str, Any]] = {
    "f0-coffee-shop": {
        "tier": "F0",
        "title": "Coffee-shop landing page",
        "brief": "Build a one-page landing site for my coffee shop with a menu and a contact form.",
        "done_definition": [
            "URL serves HTTP 200 text/html",
            "hero/header section has non-empty copy",
            "menu section lists at least three items",
            "contact form has at least two inputs and a submit control",
            "page is coherent; no template-error/placeholder-only body",
        ],
    },
    "f1-snake": {
        "tier": "F1",
        "title": "Playable Snake game",
        "brief": "Build me a playable Snake game I can open in a browser.",
        "done_definition": [
            "URL serves HTTP 200 and renders a canvas or game DOM",
            "arrow keys move the snake",
            "eating food increments a visible score",
            "collision produces a game-over state",
            "game is restartable after game-over",
        ],
    },
    "f2-todo": {
        "tier": "F2",
        "title": "Simple to-do app with persistence",
        "brief": "Build a simple to-do web app where I can add tasks, mark them done, and delete them.",
        "done_definition": [
            "URL serves HTTP 200 and renders input + list UI",
            "adding a task displays it",
            "task persists across reload",
            "complete toggles a visible done state",
            "delete removes the task",
        ],
    },
}


@dataclass
class HarnessConfig:
    run_id: str
    id_base_url: str = ""
    # In-cluster internal Shizuha-ID base URL (e.g.
    # http://shizuha-id.shizuha.svc.cluster.local:8001). The teardown's
    # /api/internal/* deprovision call is network-gated OFF the public
    # ingress (it 404s there), so it must target the internal service URL.
    # Falls back to id_base_url when unset (non-cluster harness runs).
    id_internal_base_url: str = ""
    hive_base_url: str = ""
    validation_runtime_token: str = ""
    template_slug: str = "tech-company"
    plan_slug: str = "team"
    brief_slug: str = "f0-coffee-shop"
    timeout_seconds: float = 15.0
    allow_live_side_effects: bool = False
    # HIVE-423: must equal shizuha-id QA_FIXTURE_SIGNUP_DOMAIN (qafixture.shizuha.com)
    # so is_qa_fixture_email() bypasses the Turnstile captcha check — the harness
    # mints no captcha token, so any other domain deterministically yields
    # signup_denied reason=missing_token (root cause: ni 09-09, run c42d08a).
    generated_email_domain: str = "qafixture.shizuha.com"
    brief_submit_url: str = ""
    execution_status_url: str = ""
    artifact_url: str = ""


@dataclass
class StageResult:
    stage: int
    slug: str
    title: str
    status: str
    elapsed_ms: int
    evidence: dict[str, Any] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)


class GoldenPathHarness:
    def __init__(self, config: HarnessConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.context: dict[str, Any] = {
            "run_id": config.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "brief": BRIEFS.get(config.brief_slug, BRIEFS["f0-coffee-shop"]),
        }
        self.results: list[StageResult] = []

    def run(self) -> dict[str, Any]:
        for stage in (
            self.signup,
            self.create_org_from_template,
            self.submit_brief,
            self.monitor_execution,
            self.verify_deliverable,
            self.teardown,
        ):
            self.results.append(stage())
        return self.report()

    def report(self) -> dict[str, Any]:
        stages = [result.__dict__ for result in self.results]
        # HIVE-2697 ruling 1: the by-design adapter-pending saga park is not a
        # failure — and neither are the stages blocked purely BECAUSE of it
        # (stages 3-5 are unreachable while the saga parks at id_org). A
        # blocked stage inherits the expected classification only when its
        # ENTIRE blocked_by chain is already expected; a blocked stage with an
        # independent reason (missing config, upstream real failure) stays red.
        expected_slugs = {s["slug"] for s in stages if s["status"] == StageStatus.BLOCKED_SAGA_WIP}
        changed = True
        while changed:
            changed = False
            for s in stages:
                if (
                    s["status"] == StageStatus.BLOCKED
                    and s["slug"] not in expected_slugs
                    and s["blocked_by"]
                    and set(s["blocked_by"]) <= expected_slugs
                ):
                    expected_slugs.add(s["slug"])
                    changed = True
        red = [
            s for s in stages
            if s["status"] in {StageStatus.FAIL, StageStatus.BLOCKED}
            and s["slug"] not in expected_slugs
        ]
        saga_wip = [s for s in stages if s["slug"] in expected_slugs]
        if red:
            outcome = "red"
        elif saga_wip:
            # Explicit, loud, and exit-0 — the bridge is visible in the
            # outcome string itself, not silently folded into "green".
            outcome = "expected-blocked-saga-wip"
        else:
            outcome = "green"
        return {
            "schema": "hive-420-golden-path-run.v1",
            "run_id": self.config.run_id,
            "created_at": self.context["created_at"],
            "recipe_source": "HIVE-419 v1",
            "brief": self.context["brief"],
            "outcome": outcome,
            "green": not red,
            "stages": stages,
            "metrics": self._metrics(stages),
        }

    def _metrics(self, stages: list[dict[str, Any]]) -> dict[str, Any]:
        stage1 = next((s for s in stages if s["stage"] == 1), {})
        stage4 = next((s for s in stages if s["stage"] == 4), {})
        return {
            "stage_pass_count": sum(1 for s in stages if s["status"] == StageStatus.PASS),
            "stage_fail_count": sum(1 for s in stages if s["status"] == StageStatus.FAIL),
            "stage_blocked_count": sum(1 for s in stages if s["status"] == StageStatus.BLOCKED),
            "stage_blocked_saga_wip_count": sum(1 for s in stages if s["status"] == StageStatus.BLOCKED_SAGA_WIP),
            "manual_nudges": sum(int((s.get("evidence") or {}).get("manual_nudges") or 0) for s in stages),
            "time_to_first_value_ms": stage1.get("evidence", {}).get("time_to_first_value_ms"),
            "time_to_deliver_ms": stage4.get("evidence", {}).get("time_to_deliver_ms"),
        }

    def _stage(self, number: int, slug: str, title: str, fn) -> StageResult:
        started = time.monotonic()
        try:
            status, evidence, gaps, blocked_by = fn()
        except Exception as exc:  # fail loud but keep the run record useful
            status = StageStatus.FAIL
            evidence = {"error_type": type(exc).__name__, "error": str(exc)}
            gaps = [f"{slug}_harness_exception"]
            blocked_by = []
        elapsed = int((time.monotonic() - started) * 1000)
        return StageResult(number, slug, title, status, elapsed, evidence, gaps, blocked_by)

    def _require_live(self, stage_slug: str):
        if self.config.allow_live_side_effects:
            return None
        return (
            StageStatus.BLOCKED,
            {"allow_live_side_effects": False},
            [f"{stage_slug}_live_side_effects_disabled"],
            [],
        )

    def _url(self, base: str, path: str) -> str:
        if not base:
            return ""
        return urljoin(base.rstrip("/") + "/", path.lstrip("/"))

    def _request_json(self, method: str, url: str, **kwargs) -> tuple[int, dict[str, Any] | list[Any] | str]:
        response = self.session.request(method, url, timeout=self.config.timeout_seconds, **kwargs)
        try:
            payload = response.json()
        except ValueError:
            payload = response.text[:1000]
        return response.status_code, payload

    def signup(self) -> StageResult:
        return self._stage(1, "signup", "Fresh signup", self._signup)

    def _signup(self):
        blocked = self._require_live("signup")
        if blocked:
            return blocked
        if not self.config.id_base_url:
            return StageStatus.BLOCKED, {}, ["signup_missing_id_base_url"], []

        # The register API only accepts [A-Za-z0-9_] in usernames. run_id
        # contains hyphens (gp-YYYYMMDDHHMMSS), so translate every disallowed
        # character to an underscore instead of emitting it verbatim (the old
        # code even converted underscores to hyphens, guaranteeing a 400).
        raw_suffix = f"{self.config.run_id}-{secrets.token_hex(3)}".lower()
        suffix = re.sub(r"[^a-z0-9_]", "_", raw_suffix)
        username = f"gp_{suffix}"[:80]
        email = f"{username}@{self.config.generated_email_domain}"
        password = "Gp!" + secrets.token_urlsafe(24) + "9a"
        self.context.update({"signup_username": username, "signup_email": email})
        url = self._url(self.config.id_base_url, "/api/auth/register/")
        status, payload = self._request_json(
            "POST",
            url,
            json={
                "username": username,
                "email": email,
                "password": password,
                "password2": password,
                "first_name": "GoldenPath",
                "last_name": "Validation",
            },
        )
        evidence = {"http_status": status, "email": email, "response": _redact(payload)}
        user = payload.get("user") if isinstance(payload, dict) else None
        if isinstance(user, dict):
            self.context["user_id"] = user.get("id")
            evidence["user_id"] = user.get("id")
        tokens = payload.get("tokens") if isinstance(payload, dict) else None
        access = (tokens or {}).get("access") if isinstance(tokens, dict) else None
        access = access or (payload.get("access") if isinstance(payload, dict) else None)
        if status == 201 and access:
            self.context["access_token"] = access
            return StageStatus.PASS, evidence | {"jwt_issued": True, "manual_nudges": 0}, [], []
        if status == 201 and isinstance(payload, dict) and payload.get("pending_approval"):
            return (
                StageStatus.FAIL,
                evidence | {"jwt_issued": False, "manual_nudges": 1},
                ["signup_pending_approval_no_jwt"],
                [],
            )
        return StageStatus.FAIL, evidence | {"jwt_issued": False}, ["signup_no_auth_token"], []

    def create_org_from_template(self) -> StageResult:
        return self._stage(2, "create_org", "Create org from template", self._create_org_from_template)

    def _create_org_from_template(self):
        if not self.context.get("access_token"):
            return StageStatus.BLOCKED, {}, ["create_org_requires_signup_jwt"], ["signup"]
        if not self.config.hive_base_url:
            return StageStatus.BLOCKED, {}, ["create_org_missing_hive_base_url"], []
        url = self._url(self.config.hive_base_url, "/hive/api/v1/org-provisioning-runs/")
        org_slug = f"gp-{self.config.run_id}".lower().replace("_", "-")[:63]
        self.context["org_slug"] = org_slug
        status, payload = self._request_json(
            "POST",
            url,
            headers={"Authorization": f"Bearer {self.context['access_token']}"},
            json={
                "org_name": f"Golden Path {self.config.run_id}",
                "org_slug": org_slug,
                "template_slug": self.config.template_slug,
                "plan_slug": self.config.plan_slug,
                "dry_run": False,
                "idempotency_key": f"hive-420:{self.config.run_id}",
            },
        )
        evidence = {"http_status": status, "response": _redact(payload)}
        if isinstance(payload, dict):
            evidence.update({
                "provisioning_status": payload.get("status"),
                "current_step": payload.get("current_step"),
                "progress": payload.get("progress"),
                "dashboard_path": payload.get("dashboard_path"),
            })
            self.context["provisioning_run_id"] = payload.get("id")
        if status in {200, 201} and isinstance(payload, dict) and payload.get("status") == "ready" and not payload.get("is_preview"):
            self.context["tenant_ready"] = True
            return StageStatus.PASS, evidence, [], []
        if status in {200, 201} and isinstance(payload, dict) and payload.get("status") == "manual_review":
            park = classify_saga_park(payload)
            if park:
                # HIVE-2697 ruling 1: the by-design adapter-pending park. Not a
                # failure — but the bridge stays loudly visible in the stage
                # status, the evidence, and the run outcome.
                self.context["saga_parked"] = True
                return (
                    StageStatus.BLOCKED_SAGA_WIP,
                    evidence | {"saga_park": park},
                    [],
                    [],
                )
            # Honesty guard: a manual_review park that is NOT the known
            # adapter-pending class (unexpected reason, or a park at a step
            # that should have an adapter) is a real failure.
            return StageStatus.FAIL, evidence, ["org_provisioning_stops_at_manual_review_before_side_effects"], []
        return StageStatus.FAIL, evidence, ["org_provisioning_not_ready"], []

    def submit_brief(self) -> StageResult:
        return self._stage(3, "submit_brief", "Submit dream-project brief", self._submit_brief)

    def _submit_brief(self):
        if not self.context.get("tenant_ready"):
            return StageStatus.BLOCKED, {}, ["submit_brief_requires_ready_tenant"], ["create_org"]
        if not self.config.brief_submit_url:
            return StageStatus.BLOCKED, {"brief": self.context["brief"]}, ["brief_intake_url_not_configured"], []
        status, payload = self._request_json(
            "POST",
            self.config.brief_submit_url,
            headers={"Authorization": f"Bearer {self.context['access_token']}"},
            json={"brief": self.context["brief"], "org_slug": self.context.get("org_slug")},
        )
        evidence = {"http_status": status, "response": _redact(payload)}
        if status in {200, 201, 202} and isinstance(payload, dict) and (payload.get("task_id") or payload.get("project_id") or payload.get("work_item")):
            self.context["work_item"] = payload.get("task_id") or payload.get("project_id") or payload.get("work_item")
            return StageStatus.PASS, evidence, [], []
        return StageStatus.FAIL, evidence, ["brief_not_materialized_as_work_item"], []

    def monitor_execution(self) -> StageResult:
        return self._stage(4, "execute", "Monitor autonomous execution", self._monitor_execution)

    def _monitor_execution(self):
        if not self.context.get("work_item"):
            return StageStatus.BLOCKED, {}, ["execution_requires_brief_work_item"], ["submit_brief"]
        if not self.config.execution_status_url:
            return StageStatus.BLOCKED, {"work_item": self.context.get("work_item")}, ["execution_status_url_not_configured"], []
        status, payload = self._request_json("GET", self.config.execution_status_url, headers={"Authorization": f"Bearer {self.context['access_token']}"})
        evidence = {"http_status": status, "response": _redact(payload)}
        if status == 200 and isinstance(payload, dict) and payload.get("artifact_url"):
            self.context["artifact_url"] = payload["artifact_url"]
            return StageStatus.PASS, evidence | {"manual_nudges": payload.get("manual_nudges", 0)}, [], []
        return StageStatus.FAIL, evidence, ["execution_no_deployed_artifact"], []

    def verify_deliverable(self) -> StageResult:
        return self._stage(5, "verify_deliverable", "Verify deliverable against done-definition", self._verify_deliverable)

    def _verify_deliverable(self):
        artifact_url = self.context.get("artifact_url") or self.config.artifact_url
        if not artifact_url:
            return StageStatus.BLOCKED, {"brief": self.context["brief"]}, ["verify_requires_artifact_url"], ["execute"]
        status, body = self._request_json("GET", artifact_url)
        evidence = {"http_status": status, "artifact_url": artifact_url}
        if status != 200 or not isinstance(body, str):
            return StageStatus.FAIL, evidence | {"response": _redact(body)}, ["artifact_not_serving_html"], []
        lowered = body.lower()
        if self.config.brief_slug == "f0-coffee-shop":
            checks = {
                "has_menu": "menu" in lowered,
                "has_contact_form": "<form" in lowered and ("textarea" in lowered or "input" in lowered),
                "has_submit": "submit" in lowered or "button" in lowered,
            }
            if all(checks.values()):
                return StageStatus.PASS, evidence | {"degraded_dom_checks": checks}, [], []
            return StageStatus.FAIL, evidence | {"degraded_dom_checks": checks}, ["artifact_failed_f0_dom_done_definition"], []
        return StageStatus.BLOCKED, evidence, ["full_browser_verification_requires_BRW_26_or_browser_runtime"], []

    def teardown(self) -> StageResult:
        return self._stage(6, "teardown", "Teardown throwaway tenant", self._teardown)

    def _teardown(self):
        user_id = self.context.get("user_id")
        if not user_id:
            return StageStatus.BLOCKED, {}, ["teardown_requires_signup_user_id"], ["signup"]
        # HIVE-2697: the /api/internal/* deprovision surface is network-gated
        # off the public ingress (it 404s there), so prefer the in-cluster
        # internal ID URL when configured.
        id_base = self.config.id_internal_base_url or self.config.id_base_url
        if not id_base:
            return StageStatus.BLOCKED, {"user_id": user_id}, ["teardown_missing_id_base_url"], []
        if not self.config.validation_runtime_token:
            return StageStatus.BLOCKED, {"user_id": user_id}, ["teardown_missing_validation_runtime_token"], []
        if not self.config.allow_live_side_effects:
            return StageStatus.BLOCKED, {"user_id": user_id}, ["teardown_live_side_effects_disabled"], []

        evidence: dict[str, Any] = {"user_id": user_id}
        gaps: list[str] = []

        # HIVE-2697 ruling 2: a saga-parked run has no ID-side org to tear
        # down, but the saga row itself must be closed out hive-side so no
        # qafixture litter survives the run. Best-effort: attempt both
        # cleanups, fail the stage if either leaves residue (contract clause
        # 4 — strict teardown, zero-residue assertion).
        if self.context.get("saga_parked") and self.context.get("provisioning_run_id"):
            saga_url = self._url(
                self.config.hive_base_url,
                f"/hive/api/v1/org-provisioning-runs/{self.context['provisioning_run_id']}/deprovision/",
            )
            saga_status, saga_payload = self._request_json(
                "POST",
                saga_url,
                headers={"Authorization": f"Bearer {self.context.get('access_token', '')}"},
            )
            evidence["saga_deprovision"] = {"http_status": saga_status, "response": _redact(saga_payload)}
            if not (200 <= saga_status < 300):
                gaps.append("teardown_saga_deprovision_failed")

        url = self._url(id_base, f"/api/internal/test-tenants/{user_id}/deprovision/")
        status, payload = self._request_json("DELETE", url, headers={"X-Validation-Runtime-Token": self.config.validation_runtime_token})
        evidence["id_deprovision"] = {"http_status": status, "response": _redact(payload)}
        if not (200 <= status < 300):
            gaps.append("teardown_deprovision_failed")

        if gaps:
            return StageStatus.FAIL, evidence, gaps, []
        return StageStatus.PASS, evidence, [], []


def classify_saga_park(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the saga-park classification when the park is the KNOWN
    adapter-pending WIP class, else None.

    Keys on the specific manual_review reason class written by the saga
    (advance_preview) AND on the parked step being the one expected to lack an
    adapter (registry-derived expected_first_adapterless_step()). Anything
    else — stage-1 failures, unexpected exception shapes, parks at
    adapter-backed steps, parks at later legs while an EARLIER leg lacks an
    adapter — classifies as None and keeps failing the job (HIVE-2697
    honesty guard, HIVE-2715 shrink constraint).
    """
    if not isinstance(payload, dict) or payload.get("status") != "manual_review":
        return None
    parked_step = str(payload.get("current_step") or "")
    if parked_step != (expected_first_adapterless_step() or ""):
        return None
    reason = ""
    for step in payload.get("steps") or []:
        if isinstance(step, dict) and step.get("slug") == parked_step:
            reason = str(step.get("message") or "")
            break
    reason = reason or str(payload.get("status_message") or "")
    lowered = reason.lower()
    if not any(marker in lowered for marker in SAGA_WIP_REASON_MARKERS):
        return None
    return {"step": parked_step, "reason_class": "adapter_pending", "reason": reason}


def _redact(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted = {}
        for key, value in payload.items():
            if key.lower() in {"access", "refresh", "token", "password", "password2", "secret"}:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact(value)
        return redacted
    if isinstance(payload, list):
        return [_redact(item) for item in payload]
    return payload


def default_run_id() -> str:
    return "gp-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def dump_report(report: dict[str, Any], *, indent: int | None = 2) -> str:
    return json.dumps(report, indent=indent, sort_keys=True)
