"""HIVE-356 (VEN-61) — design-partner pilot-prep glue.

Builds the audited handoff from HIVE-324/Connect lead intake into a HIVE-320
``TenantProvisioningRun`` **preview** record, and fail-closes the external
launch/apply path while the RFC §3 launch gates are red.

Scope (VEN-70 scoped/conditional sign-off): INTERNAL pilot-prep only —
- internal test leads/org pairs, admin-assisted preview/handoff, audit records,
  explicit gate state;
- NO external customer data, real customer user invites, real tenant apply,
  payment collection, or customer-agent credential grants.

The pilot handoff is ALWAYS a dry-run preview: it never performs a real external
apply. External apply is guarded by :func:`assert_external_apply_allowed`, which
fails closed until every launch gate is green — and no internal admin/devops/
credential-broker/billing grant may be attached to a customer-agent handoff
(:func:`assert_no_forbidden_grants`).

RFC: wiki ``c1af3d6e-ada6-46f4-9e71-2ab40740780d`` (VEN-61 / HIVE-316).
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import TenantProvisioningRun
from .tenant_provisioning import _append_event, advance_preview, create_or_get_run

# RFC §2.1 lead-intake fields the handoff must capture.
INTAKE_FIELDS = (
    'company', 'country', 'workflow', 'sop_link',
    'sponsor', 'sponsor_availability', 'sensitivity', 'fit_risk',
)

# RFC §3 minimum launch gates. Every gate must be green before the external
# launch/apply path may run for a chosen pilot org pair. Ordered as in the RFC.
PILOT_LAUNCH_GATES = (
    ('hive_327_isolation_probes',
     'HIVE-327 tenant-isolation probes green live/CI for the pilot org pair '
     '(Pulse/Wiki/Connect/Drive/Books cross-org negatives)'),
    ('con_129_135_connect_dm',
     'CON-129/CON-135 prod verification: same-org DM succeeds, zero-shared-org '
     'DM 403s, alert/fail-loud DM paths not silently dropped'),
    ('hive_326_341_342_compute_isolation',
     'HIVE-326/HIVE-341/HIVE-342 compute-isolation drift-guard verification'),
    ('final_security_org_pair_signoff',
     'Final external customer/security/org-pair sign-off for the chosen pilot pair'),
)

# Internal grants that must NEVER be issued to a customer-agent pilot handoff
# (RFC §2.2 / §4 Elevation-of-Privilege mitigation). Matched case-insensitively
# as substrings so e.g. 'admin-mcp', 'credential_broker', 'books-write' are caught.
FORBIDDEN_PILOT_GRANTS = (
    'admin', 'devops', 'credential-broker', 'credential_broker', 'broker',
    'billing', 'books', 'payment', 'root', 'cluster', 'kubectl', 'host-ssh',
)

PILOT_MODE = 'internal_pilot_prep'


class PilotGateError(Exception):
    """Raised when an external launch/apply is attempted while gates are red."""


class PilotGrantError(Exception):
    """Raised when a forbidden internal grant is attached to a pilot handoff."""


def default_gate_state() -> dict:
    """All launch gates start RED — fail-closed until explicitly verified green."""
    return {
        key: {'status': 'red', 'label': label, 'evidence': ''}
        for key, label in PILOT_LAUNCH_GATES
    }


def _normalize_gate_state(gate_state) -> dict:
    """Merge caller-supplied gate statuses over the RFC default, dropping unknown
    keys so a caller can never invent a gate or omit one to widen access."""
    state = default_gate_state()
    if isinstance(gate_state, dict):
        for key, val in gate_state.items():
            if key not in state:
                continue
            status = (val.get('status') if isinstance(val, dict) else val) or 'red'
            evidence = val.get('evidence', '') if isinstance(val, dict) else ''
            state[key]['status'] = 'green' if str(status).lower() == 'green' else 'red'
            state[key]['evidence'] = evidence
    return state


def assert_no_forbidden_grants(tool_grants) -> None:
    """Fail closed if any requested grant is an internal admin/devops/credential/
    billing grant. Customer-agent grants must be template-scoped only (RFC §2.2)."""
    for grant in (tool_grants or []):
        g = str(grant).strip().lower()
        for bad in FORBIDDEN_PILOT_GRANTS:
            if bad in g:
                raise PilotGrantError(
                    f'Forbidden internal grant for a pilot handoff: {grant!r} '
                    f'(matches {bad!r}). Pilot customer agents get template-scoped '
                    'grants only — no internal admin/devops/credential-broker/billing.')


def all_gates_green(run: TenantProvisioningRun) -> bool:
    gates = (run.pilot_prep or {}).get('gates') or {}
    return bool(gates) and all(
        (g or {}).get('status') == 'green' for g in gates.values())


def pilot_gate_blockers(run: TenantProvisioningRun) -> list:
    """Keys of launch gates that are not yet green."""
    gates = (run.pilot_prep or {}).get('gates') or {}
    return [key for key, g in gates.items() if (g or {}).get('status') != 'green']


def assert_external_apply_allowed(run: TenantProvisioningRun) -> None:
    """FAIL-CLOSED gate for the external launch/apply path.

    Blocks a real external apply unless EVERY RFC §3 launch gate is green. Internal
    dry-run pilot-prep previews never reach this guard (they do not apply). Any
    apply path (now or future) must call this before creating real external
    resources for a customer pilot."""
    if not (run.pilot_prep or {}).get('gates'):
        raise PilotGateError(
            'External apply blocked: no pilot launch-gate state recorded on this run.')
    blockers = pilot_gate_blockers(run)
    if blockers:
        raise PilotGateError(
            'External apply blocked — pilot launch gates not green: '
            + ', '.join(blockers)
            + '. Internal dry-run pilot-prep previews are allowed; a real external '
            'apply requires all RFC §3 gates green + final Security sign-off.')


def create_pilot_handoff(
    *,
    requester_user_id: int,
    requester_email: str = '',
    intake: dict,
    org_name: str,
    org_slug: str,
    template_slug: str,
    plan_slug: str = '',
    tool_grants=None,
    gate_state=None,
) -> tuple[TenantProvisioningRun, bool]:
    """Capture a design-partner lead intake and produce an audited, dry-run
    provisioning PREVIEW/handoff (never a real external apply).

    - Forces ``dry_run=True`` — internal pilot-prep is preview-only.
    - Stamps the RFC §2.1 intake fields, RFC §3 launch-gate state (default all
      red / fail-closed), pilot mode, and template-scoped tool grants onto the
      run's ``pilot_prep`` record + an audit event.
    - Rejects forbidden internal grants before anything is written.
    """
    assert_no_forbidden_grants(tool_grants)
    captured = {field: (intake or {}).get(field, '') for field in INTAKE_FIELDS}
    gates = _normalize_gate_state(gate_state)

    # Internal pilot-prep NEVER applies externally: force a dry-run preview.
    run, created = create_or_get_run(
        requester_user_id=requester_user_id,
        requester_email=requester_email,
        org_name=org_name,
        org_slug=org_slug,
        template_slug=template_slug,
        plan_slug=plan_slug,
        dry_run=True,
    )

    with transaction.atomic():
        run.refresh_from_db()
        pilot = dict(run.pilot_prep or {})
        pilot['mode'] = PILOT_MODE
        pilot['intake'] = captured
        pilot['gates'] = gates
        pilot['tool_grants'] = list(tool_grants or [])
        pilot['external_apply_allowed'] = all(
            g.get('status') == 'green' for g in gates.values())
        pilot['captured_at'] = timezone.now().isoformat()
        run.pilot_prep = pilot
        _append_event(
            run, 'pilot_intake_captured',
            'Design-partner lead intake captured for internal pilot-prep handoff '
            '(preview only; external apply fail-closed until launch gates are green).',
            mode=PILOT_MODE, company=captured.get('company', ''),
            sensitivity=captured.get('sensitivity', ''),
            open_gate_blockers=[k for k, g in gates.items() if g.get('status') != 'green'],
        )
        run.save(update_fields=['pilot_prep', 'audit_events', 'updated_at'])

    # Advance the dry-run preview (plans org slug/template/plan/teams/quotas;
    # side-effectful steps are previewed, not applied).
    run = advance_preview(run)
    run.refresh_from_db()
    return run, created
