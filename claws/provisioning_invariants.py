"""HIVE-354: tenant-provisioning isolation invariant gate.

Surfaced by the HIVE-353 retro STRIDE-lite review of HIVE-320. The provisioning
saga (``tenant_provisioning``) safely stops non-dry-run runs at ``manual_review``
until concrete side-effecting adapters exist. This module is the gate those
future adapters MUST pass, wired now so a falsely-ready or cross-org-leaking
tenant is impossible-by-construction rather than depending on each adapter to
remember the checks:

  - A real side-effectful step may be marked DONE only via ``verify_step_result``,
    which records a VERIFIED resource ref (``verified: True`` + a real external id)
    or fails closed.
  - A non-dry-run run may reach READY only if ``assert_ready_invariants`` passes:
    every required resource ref is verified-real, same-org scoped, and satisfies
    the HIVE-318/HIVE-327 tenant-isolation invariants.
  - Any violation raises ``TenantIsolationInvariantError`` (fail-loud) and records
    a security-sensitive audit signal; the run is never silently marked ready.

Planned/preview refs synthesized by the saga for dry-run (``_planned_refs``) are
intentionally NOT ``verified`` — so a non-dry-run run can never reach READY on
preview data alone.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Credential/identity scope markers that must never appear on a tenant resource
# ref — they signal a shared/internal/privileged grant crossing the tenant
# boundary (STRIDE elevation-of-privilege / information-disclosure).
FORBIDDEN_SCOPE_MARKERS = frozenset({
    'shared', 'internal', 'platform', 'admin', 'root', 'cluster-admin',
    'superuser', 'global', 'cross-org', 'system',
})

# Required verified resource refs per side-effectful step. Each entry: the
# resource_refs key the step must populate, and the field on that ref that must
# carry a real external id.
# Required verified resource refs per side-effectful step. Each step maps to one
# or more (resource_key, id_field) pairs — the resource_refs keys the step must
# populate, and the field on that ref that must carry a real external id. The
# connect_drive step provisions TWO isolated resources (a Connect workspace AND a
# Drive root), so both must be verified same-org — a tenant must not reach READY
# with an unverified/absent Drive root (HIVE-354 review P1).
REQUIRED_STEP_REFS: dict[str, tuple[tuple[str, str], ...]] = {
    'id_org': (('organization', 'external_org_id'),),
    'pulse': (('pulse', 'project_id'),),
    'wiki': (('wiki', 'space_id'),),
    'connect_drive': (('connect', 'workspace_id'), ('drive', 'root_id')),
    'tenant_namespace': (('kubernetes', 'namespace'),),
    'agents': (('fleet', 'agent_identities'),),
    'cortex_key': (('cortex', 'tenant_key_id'),),
    'seed_task': (('seed_task', 'task_id'),),
}


class TenantIsolationInvariantError(Exception):
    """Fail-loud, security-sensitive: a provisioning invariant was violated.

    Carries the structured ``violations`` so callers surface the exact failed
    invariant instead of a generic error.
    """

    def __init__(self, message: str, violations: list[str] | None = None):
        super().__init__(message)
        self.violations = violations or []


def _ref_org(ref: dict) -> str | None:
    """The org this ref claims to belong to (accepting a few common key names)."""
    for key in ('org_slug', 'org', 'organization_slug', 'organization'):
        val = ref.get(key)
        if isinstance(val, dict):
            val = val.get('slug') or val.get('org_slug')
        if val:
            return str(val)
    return None


def _forbidden_markers_in(value) -> set[str]:
    """Any forbidden scope marker appearing as a whole token in the ref values."""
    found: set[str] = set()

    def scan(v):
        if isinstance(v, str):
            low = v.lower()
            # Two tokenizations, so BOTH single-word component markers and
            # hyphenated multi-word markers match (HIVE-354 review): `tight` keeps
            # hyphens intact so `cross-org` / `cluster-admin` match; `loose` also
            # splits hyphens so `shared` is caught inside `platform-shared`.
            tight = {t.strip() for t in low.replace(':', ' ').replace('/', ' ').replace(',', ' ').split()}
            loose = {t.strip() for t in low.replace(':', ' ').replace('/', ' ').replace(',', ' ').replace('-', ' ').split()}
            found.update((tight | loose) & FORBIDDEN_SCOPE_MARKERS)
        elif isinstance(v, dict):
            for sub in v.values():
                scan(sub)
        elif isinstance(v, (list, tuple)):
            for sub in v:
                scan(sub)

    scan(value)
    return found


def verify_resource_ref(run, resource_key: str, id_field: str, ref) -> list[str]:
    """Return a list of invariant violations for a single resource ref (empty = ok)."""
    violations: list[str] = []
    label = f'{resource_key}'

    if not isinstance(ref, dict):
        return [f'{label}: resource ref is missing or not an object']

    # 1. Must be a VERIFIED real resource, not a synthesized preview.
    if ref.get('verified') is not True:
        violations.append(f'{label}: ref is not verified (adapter must confirm the real resource exists)')

    # 2. Must carry a real external id.
    ext_id = ref.get(id_field)
    if not ext_id or (isinstance(ext_id, str) and not ext_id.strip()):
        violations.append(f'{label}: missing real {id_field}')
    if isinstance(ext_id, (list, tuple)) and not any(str(x).strip() for x in ext_id):
        violations.append(f'{label}: {id_field} has no real entries')

    # 3. Same-org scoping: the ref must belong to THIS run's org.
    ref_org = _ref_org(ref)
    if ref_org is None:
        violations.append(f'{label}: ref does not declare an owning org (cannot prove same-org scope)')
    elif ref_org != run.org_slug:
        violations.append(f'{label}: cross-org ref — belongs to "{ref_org}", expected "{run.org_slug}"')

    # 4. No shared/internal/privileged scope markers crossing the tenant boundary.
    bad = _forbidden_markers_in(ref)
    if bad:
        violations.append(f'{label}: forbidden shared/internal scope marker(s) {sorted(bad)}')

    # 5. Namespace isolation for the tenant_namespace step.
    if resource_key == 'kubernetes':
        expected_ns = f'tenant-{run.org_slug}'
        if ref.get('namespace') != expected_ns:
            violations.append(
                f'{label}: namespace "{ref.get("namespace")}" is not the isolated tenant namespace "{expected_ns}"'
            )
        if ref.get('network_policy_isolated') is not True:
            violations.append(f'{label}: tenant namespace is not network-policy isolated')

    return violations


def assert_ready_invariants(run) -> None:
    """Fail closed unless a non-dry-run run may safely be marked READY.

    Dry-run previews are exempt (they never produce real resources). For a real
    run, EVERY required step ref must be verified-real, same-org scoped, and pass
    the isolation checks. Raises TenantIsolationInvariantError (and records a
    fail-loud audit signal) otherwise.
    """
    if run.dry_run:
        return

    refs = run.resource_refs or {}
    violations: list[str] = []
    for step_slug, specs in REQUIRED_STEP_REFS.items():
        for resource_key, id_field in specs:
            ref = refs.get(resource_key)
            if ref is None:
                violations.append(f'{resource_key}: no resource ref recorded for required step "{step_slug}"')
                continue
            violations.extend(verify_resource_ref(run, resource_key, id_field, ref))

    if violations:
        emit_fail_loud(run, 'ready_invariant_violation', violations)
        raise TenantIsolationInvariantError(
            f'Refusing to mark tenant "{run.org_slug}" READY: {len(violations)} isolation invariant violation(s).',
            violations,
        )


def verify_step_result(run, step_slug: str, ref: dict) -> dict:
    """Validate + normalize an adapter's real result before a step is marked DONE.

    Returns the ref (stamped ``verified=True``) to store on ``run.resource_refs``.
    Raises TenantIsolationInvariantError (fail-loud) on any isolation violation so
    a bad adapter result can never silently advance the saga.
    """
    specs = REQUIRED_STEP_REFS.get(step_slug)
    if not specs:
        # Non-side-effectful / non-resource steps (validate_request, ready) need no ref.
        return ref
    if len(specs) == 1:
        # Single-resource step: `ref` is that resource's ref directly.
        resource_key, id_field = specs[0]
        candidate = dict(ref or {})
        candidate.setdefault('verified', True)
        violations = verify_resource_ref(run, resource_key, id_field, candidate)
        result: dict = candidate
    else:
        # Multi-resource step (connect_drive): `ref` maps each resource_key to its
        # own ref, e.g. {'connect': {...}, 'drive': {...}}. All must pass.
        violations = []
        result = {}
        for resource_key, id_field in specs:
            candidate = dict((ref or {}).get(resource_key) or {})
            candidate.setdefault('verified', True)
            violations.extend(verify_resource_ref(run, resource_key, id_field, candidate))
            result[resource_key] = candidate
    if violations:
        emit_fail_loud(run, 'step_invariant_violation', violations, step=step_slug)
        raise TenantIsolationInvariantError(
            f'Adapter result for step "{step_slug}" failed tenant-isolation invariants.', violations,
        )
    return result


def emit_fail_loud(run, event: str, violations: list[str], **extra) -> None:
    """Record a security-sensitive fail-loud audit signal on the run.

    This is the seam a Pulse/Connect security alert hooks into; keeping it here
    (rather than a bare log line) guarantees every invariant failure is durable
    and visible in the run's audit trail (HIVE-353 fail-loud requirement).
    """
    from django.utils import timezone

    events = list(run.audit_events or [])
    payload = {
        'at': timezone.now().isoformat(),
        'event': event,
        'security_sensitive': True,
        'message': f'Tenant-isolation invariant violation for org "{run.org_slug}".',
        'violations': violations,
    }
    payload.update(extra)
    events.append(payload)
    run.audit_events = events
    # HIVE-354 review P2: persist the fail-loud signal DURABLY, not just in memory.
    # verify_step_result() raises right after this, so without a save an adapter
    # that lets the exception propagate would lose the security-sensitive audit
    # event. Save only the audit column (a real saved run has a pk); in-memory
    # stubs / unsaved runs are left to the caller's own save (e.g. advance_preview).
    if getattr(run, 'pk', None) is not None and hasattr(run, 'save'):
        try:
            run.save(update_fields=['audit_events', 'updated_at'])
        except Exception:  # pragma: no cover - never let auditing mask the violation
            logger.exception('mechanism-liveness: failed to persist fail-loud audit for run %s', getattr(run, 'pk', '?'))
