"""HIVE-320 tenant-organization provisioning saga spine.

This module is intentionally side-effect-light for the first implementation
slice: it creates the durable run record, initializes every HLD step, supports
idempotent re-entry, and advances safe preview steps. Real service adapters can
replace the manual-review markers step-by-step without changing the persisted
contract.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from .models import TenantProvisioningRun
from .org_templates import TemplatePlanContext, resolve_org_template_plan
from .provisioning_invariants import (
    REQUIRED_STEP_REFS,
    TenantIsolationInvariantError,
    assert_ready_invariants,
    verify_step_result,
)


class ProvisioningIdempotencyConflict(ValueError):
    """Raised when a caller reuses an idempotency key for a different request."""


@dataclass(frozen=True)
class ProvisioningStep:
    slug: str
    title: str
    side_effectful: bool = True


PROVISIONING_STEPS: tuple[ProvisioningStep, ...] = (
    ProvisioningStep('validate_request', 'Validate requester, template, plan, and slug', side_effectful=False),
    ProvisioningStep('id_org', 'Create/attach Shizuha ID organization and owner membership'),
    ProvisioningStep('pulse', 'Create Pulse project, workflow scheme, teams, and roster'),
    ProvisioningStep('wiki', 'Create Wiki space and starter pages'),
    ProvisioningStep('connect_drive', 'Create Connect workspace and Drive root'),
    ProvisioningStep('tenant_namespace', 'Allocate tenant namespace and quota profile'),
    ProvisioningStep('agents', 'Provision template agents through the fleet daemon'),
    ProvisioningStep('cortex_key', 'Mint Cortex tenant key and quota binding'),
    ProvisioningStep('seed_task', 'Seed first task and observe agent completion'),
    ProvisioningStep('ready', 'Mark org ready and emit audit trail', side_effectful=False),
)


def default_idempotency_key(*, requester_user_id: int, org_slug: str, template_slug: str, plan_slug: str, dry_run: bool) -> str:
    """Return a stable, non-secret idempotency key for caller-less command usage."""

    payload = {
        'requester_user_id': requester_user_id,
        'org_slug': org_slug,
        'template_slug': template_slug,
        'plan_slug': plan_slug or '',
        'dry_run': bool(dry_run),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    return f'hive320:{org_slug}:{digest}'


def initial_step_state() -> dict:
    return {
        step.slug: {
            'status': TenantProvisioningRun.StepStatus.PENDING,
            'title': step.title,
            'side_effectful': step.side_effectful,
        }
        for step in PROVISIONING_STEPS
    }


def create_or_get_run(
    *,
    requester_user_id: int,
    requester_email: str = '',
    org_name: str,
    org_slug: str,
    template_slug: str,
    plan_slug: str = '',
    dry_run: bool = False,
    idempotency_key: str | None = None,
) -> tuple[TenantProvisioningRun, bool]:
    """Create the saga row once; repeated calls return the existing run."""

    plan_slug = plan_slug or ''
    idempotency_key = idempotency_key or default_idempotency_key(
        requester_user_id=requester_user_id,
        org_slug=org_slug,
        template_slug=template_slug,
        plan_slug=plan_slug,
        dry_run=dry_run,
    )
    with transaction.atomic():
        run, created = TenantProvisioningRun.objects.select_for_update().get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                'requester_user_id': requester_user_id,
                'requester_email': requester_email,
                'org_name': org_name,
                'org_slug': org_slug,
                'template_slug': template_slug,
                'plan_slug': plan_slug,
                'dry_run': dry_run,
                'step_state': initial_step_state(),
                'current_step': PROVISIONING_STEPS[0].slug,
                'audit_events': [
                    {
                        'at': timezone.now().isoformat(),
                        'event': 'run_created',
                        'message': 'Tenant provisioning saga initialized.',
                    }
                ],
            },
        )
        if not created:
            immutable_fields_match = (
                run.requester_user_id == requester_user_id
                and run.org_name == org_name
                and run.org_slug == org_slug
                and run.template_slug == template_slug
                and (run.plan_slug or '') == plan_slug
                and run.dry_run == dry_run
            )
            if not immutable_fields_match:
                raise ProvisioningIdempotencyConflict(
                    'Idempotency key has already been used for a different provisioning request.'
                )
    return run, created


def _append_event(run: TenantProvisioningRun, event: str, message: str, **extra) -> None:
    events = list(run.audit_events or [])
    payload = {'at': timezone.now().isoformat(), 'event': event, 'message': message}
    payload.update(extra)
    events.append(payload)
    run.audit_events = events


def _set_step(run: TenantProvisioningRun, step_slug: str, status: str, message: str = '') -> None:
    state = dict(run.step_state or initial_step_state())
    step = dict(state.get(step_slug) or {})
    step['status'] = status
    if message:
        step['message'] = message
    step['updated_at'] = timezone.now().isoformat()
    state[step_slug] = step
    run.step_state = state
    run.current_step = step_slug


def _planned_refs(run: TenantProvisioningRun) -> dict:
    namespace = f'tenant-{run.org_slug}'
    template_plan = resolve_org_template_plan(
        run.template_slug,
        TemplatePlanContext(
            org_name=run.org_name,
            org_slug=run.org_slug,
            plan_slug=run.plan_slug or 'template-default',
            tenant_kind='customer',
        ),
    )
    return {
        'template_plan': template_plan,
        'organization': {'slug': run.org_slug, 'display_name': run.org_name},
        'pulse': {'project_key': run.org_slug.upper()[:10], 'template_slug': run.template_slug},
        'wiki': {'space_slug': run.org_slug},
        'connect': {'workspace_slug': run.org_slug},
        'drive': {'root_slug': run.org_slug},
        'kubernetes': {'namespace': namespace, 'quota_profile': run.plan_slug or 'template-default'},
        'fleet': {'agent_namespace': namespace, 'template_slug': run.template_slug},
        'cortex': {'tenant_key_alias': f'{run.org_slug}:{run.plan_slug or "default"}'},
    }


def advance_preview(run: TenantProvisioningRun, *, allow_side_effects: bool = False) -> TenantProvisioningRun:
    """Advance the run through safe steps.

    Without concrete service adapters, every non-dry-run side-effectful step
    stops in ``manual_review`` instead of pretending the tenant was provisioned.
    Dry-run completes every step as a preview and records deterministic planned
    refs. ``allow_side_effects`` is reserved for the future adapter-backed path;
    it does not mark steps DONE by itself.
    """

    with transaction.atomic():
        run = TenantProvisioningRun.objects.select_for_update().get(pk=run.pk)
        if run.terminal:
            return run

        run.status = TenantProvisioningRun.Status.RUNNING
        if not run.step_state:
            run.step_state = initial_step_state()

        for step in PROVISIONING_STEPS:
            _set_step(run, step.slug, TenantProvisioningRun.StepStatus.RUNNING)

            if step.slug == 'validate_request':
                if not run.org_slug or not run.template_slug:
                    _set_step(run, step.slug, TenantProvisioningRun.StepStatus.FAILED, 'org_slug and template_slug are required')
                    run.status = TenantProvisioningRun.Status.FAILED
                    run.status_message = 'Invalid provisioning request.'
                    run.completed_at = timezone.now()
                    _append_event(run, 'run_failed', run.status_message, step=step.slug)
                    break
                _set_step(run, step.slug, TenantProvisioningRun.StepStatus.DONE, 'Request is syntactically valid.')
                continue

            if step.side_effectful and not run.dry_run:
                from .provisioning_adapters import SAGA_ADAPTERS, SagaAdapterError

                adapter = SAGA_ADAPTERS.get(step.slug)
                if adapter is None:
                    reason = 'Adapter not enabled yet; stopping before external side effects.'
                    if allow_side_effects:
                        reason = 'allow_side_effects requested, but no concrete adapter is implemented for this step.'
                    _set_step(
                        run,
                        step.slug,
                        TenantProvisioningRun.StepStatus.MANUAL_REVIEW,
                        reason,
                    )
                    run.status = TenantProvisioningRun.Status.MANUAL_REVIEW
                    run.status_message = f'Waiting for adapter implementation before step {step.slug}.'
                    _append_event(run, 'manual_review', run.status_message, step=step.slug)
                    break

                # HIVE-2715: real adapter path. Never fake success (the module
                # contract): the step is DONE only when the adapter's real side
                # effect completed AND its resource ref passes verify_step_result
                # (fail-closed verified stamping). Adapter failure is fail-loud —
                # FAILED, never the expected-pending marker, so it cannot ride
                # the golden-path honesty bridge.
                _append_event(
                    run,
                    'adapter_started',
                    f'Real adapter dispatch for step {step.slug}.',
                    step=step.slug,
                )
                try:
                    ref = adapter(run)
                    verified = verify_step_result(run, step.slug, ref)
                except SagaAdapterError as exc:
                    _set_step(run, step.slug, TenantProvisioningRun.StepStatus.FAILED, str(exc))
                    run.status = TenantProvisioningRun.Status.FAILED
                    run.status_message = f'Adapter for step {step.slug} failed: {exc}'
                    run.completed_at = timezone.now()
                    _append_event(run, 'adapter_failed', run.status_message, step=step.slug)
                    break
                except TenantIsolationInvariantError as exc:
                    # verify_step_result already durably persisted the fail-loud
                    # signal; never silently advance the saga.
                    _set_step(run, step.slug, TenantProvisioningRun.StepStatus.FAILED, str(exc))
                    run.status = TenantProvisioningRun.Status.FAILED
                    run.status_message = str(exc)
                    run.completed_at = timezone.now()
                    _append_event(run, 'adapter_failed', run.status_message, step=step.slug)
                    break
                refs = dict(run.resource_refs or {})
                specs = REQUIRED_STEP_REFS.get(step.slug) or ()
                if len(specs) == 1:
                    refs[specs[0][0]] = verified
                else:
                    refs.update(verified)
                run.resource_refs = refs
                _set_step(
                    run,
                    step.slug,
                    TenantProvisioningRun.StepStatus.DONE,
                    'Real adapter completed; resource ref verified.',
                )
                continue

            refs = dict(run.resource_refs or {})
            refs.update(_planned_refs(run))
            run.resource_refs = refs
            _set_step(run, step.slug, TenantProvisioningRun.StepStatus.DONE, 'Previewed planned resource reference.')
        else:
            # HIVE-354 gate: a real (non-dry-run) tenant may reach READY only if
            # every required resource ref is verified-real, same-org scoped, and
            # passes the tenant-isolation invariants. Fail closed to MANUAL_REVIEW
            # with a fail-loud audit signal rather than falsely marking it ready.
            try:
                assert_ready_invariants(run)
            except TenantIsolationInvariantError as exc:
                _set_step(run, 'ready', TenantProvisioningRun.StepStatus.MANUAL_REVIEW, str(exc))
                run.status = TenantProvisioningRun.Status.MANUAL_REVIEW
                run.status_message = str(exc)
                run.save(update_fields=[
                    'status', 'current_step', 'step_state', 'resource_refs',
                    'audit_events', 'status_message', 'completed_at', 'updated_at',
                ])
                return run
            run.status = TenantProvisioningRun.Status.READY
            run.status_message = 'Dry-run preview complete.' if run.dry_run else 'Provisioning complete.'
            run.completed_at = timezone.now()
            _append_event(run, 'run_ready', run.status_message)

        run.save(update_fields=[
            'status',
            'current_step',
            'step_state',
            'resource_refs',
            'audit_events',
            'status_message',
            'completed_at',
            'updated_at',
        ])
        return run


def summarize_steps(run: TenantProvisioningRun) -> Iterable[str]:
    state = run.step_state or {}
    for step in PROVISIONING_STEPS:
        info = state.get(step.slug, {})
        status = info.get('status', TenantProvisioningRun.StepStatus.PENDING)
        message = info.get('message', '')
        suffix = f' — {message}' if message else ''
        yield f'{step.slug}: {status}{suffix}'


class ProvisioningRunNotDeprovisionable(ValueError):
    """Raised when a run is not eligible for the saga-parked deprovision path."""


def deprovision_parked_run(run: TenantProvisioningRun) -> TenantProvisioningRun:
    """HIVE-2697 ruling 2: terminal cleanup path for a saga-parked run.

    The golden-path validator's stage-6 teardown must end every run with zero
    qafixture litter, but a ``manual_review``-parked saga previously had no
    deprovision path at all — the ID test-tenant endpoint does not know about
    the saga and the run row itself was never closed out.

    Best-effort cleanup of created resources up to the park point: the
    inventory is derived from the step state — every side-effectful step that
    reached DONE would have created real resources and needs a compensating
    action. Today the only park is pre-side-effect (the id_org adapter is the
    HIVE-418 epic's unimplemented leg), so the inventory is empty and the
    cleanup is the terminal close itself. As adapters land, each one must
    register its compensating action here so this path stays complete.

    Idempotent: an already-torn-down run is returned unchanged.
    """

    with transaction.atomic():
        run = TenantProvisioningRun.objects.select_for_update().get(pk=run.pk)
        if run.status == TenantProvisioningRun.Status.TORN_DOWN:
            return run
        if run.status != TenantProvisioningRun.Status.MANUAL_REVIEW:
            raise ProvisioningRunNotDeprovisionable(
                f'Only manual_review-parked runs can be deprovisioned via this path '
                f'(run status is {run.status}).'
            )

        parked_step = run.current_step
        created_side_effectful = [
            slug
            for slug, info in (run.step_state or {}).items()
            if info.get('status') == TenantProvisioningRun.StepStatus.DONE
            and info.get('side_effectful')
        ]

        run.status = TenantProvisioningRun.Status.TEARING_DOWN
        _append_event(
            run,
            'run_deprovisioning',
            'Saga-parked run deprovision started (HIVE-2697 ruling 2).',
            step=parked_step,
        )
        # Best-effort compensating actions go here as adapters land, keyed on
        # the created side-effectful inventory. HIVE-2715: the id_org adapter
        # mints a real admin Organization — its registered compensating action
        # runs ONLY for PROVABLY qafixture-scoped runs (refuse-by-default,
        # HIVE-2697 cleanup contract extended to the organization resource
        # class); a refusal is recorded fail-loud, never faked as cleanup.
        compensating_outcomes: list[dict] = []
        organization_ref = (run.resource_refs or {}).get('organization') or {}
        if organization_ref.get('verified') and organization_ref.get('external_org_id'):
            from .provisioning_adapters import is_qa_fixture_run, teardown_id_org

            if is_qa_fixture_run(run):
                outcome = teardown_id_org(run, organization_ref)
                compensating_outcomes.append(outcome)
                _append_event(
                    run,
                    'deprovision_id_org',
                    f'Admin organization teardown outcome: {outcome.get("status")}.',
                    step=parked_step,
                    outcome=outcome,
                )
            else:
                refusal = {
                    'status': 'refused',
                    'organization_id': organization_ref.get('external_org_id'),
                    'reason': 'non_qafixture_requester',
                }
                compensating_outcomes.append(refusal)
                _append_event(
                    run,
                    'deprovision_refused',
                    'Refusing admin organization teardown: requester is not a QA fixture account '
                    '(refuse-by-default; deprovisionable resources are fixture-scoped).',
                    step=parked_step,
                    outcome=refusal,
                )
        run.status = TenantProvisioningRun.Status.TORN_DOWN
        # Honest close: the message states the inventory and the REAL
        # compensating outcomes — a failed/refused external teardown is never
        # worded as a rollback (the never-fake-success contract).
        run.status_message = (
            f'Deprovisioned after saga park at step {parked_step}; '
            f'created side-effectful inventory: {created_side_effectful or "none"}; '
            f'compensating outcomes: '
            f'{json.dumps(compensating_outcomes, sort_keys=True) if compensating_outcomes else "pre-side-effect park (nothing external to roll back)"}.'
        )
        run.completed_at = timezone.now()
        _append_event(run, 'run_deprovisioned', run.status_message, step=parked_step)
        run.save(update_fields=[
            'status',
            'current_step',
            'step_state',
            'resource_refs',
            'audit_events',
            'status_message',
            'completed_at',
            'updated_at',
        ])
        return run


# ────────────────────────────────────────────────────────────────────────────
# VEN-178 S2 (HIVE-777): dry-run deploy plan for a function-pod template into
# an EXISTING org — planner-only, zero writes, deterministic preview hash.
# ────────────────────────────────────────────────────────────────────────────


class DryRunPlanRejected(ValueError):
    """Raised when a dry-run plan must fail closed (authz, visibility, or a
    sensitive-access capability targeting a customer tenant)."""


def _org_membership_role(user, org_slug: str) -> str | None:
    """Return the requester's role on the target org, or None if not a member.

    ``organization_memberships`` arrives in two shapes from Shizuha-ID:
    ``{org_id: 'role'}`` or ``{org_slug: {'role': ...}}``. Match either key
    form against the target org slug or its numeric id.
    """
    memberships = getattr(user, 'organization_memberships', None) or {}
    if not isinstance(memberships, dict):
        return None
    for key, value in memberships.items():
        if isinstance(value, dict):
            role = value.get('role')
        else:
            role = value
        key_s = str(key)
        if key_s == str(org_slug):
            return role
    return None


def _is_org_admin_or_owner(user, org_slug: str) -> bool:
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return True
    return _org_membership_role(user, org_slug) in ('admin', 'owner')


def _capability_risk_deltas(template_plan: dict) -> list[dict]:
    """Surface explicit capability/risk deltas the pod would receive.

    Each delta names the team, the capability, any sensitive access flags or
    credential grant scopes, and whether they were stripped (customer tenant)
    or carried (internal tenant).
    """
    deltas: list[dict] = []
    for team in template_plan.get('teams', []):
        for cap in team.get('capabilities', []):
            deltas.append({
                'team_slug': team.get('slug'),
                'capability_slug': cap.get('slug'),
                'sensitive_access_flags': sorted(set(cap.get('sensitive_access_flags') or [])),
                'credential_grant_scopes': sorted(set(cap.get('credential_grant_scopes') or [])),
                'stripped_credential_grant_scopes': sorted(set(cap.get('stripped_credential_grant_scopes') or [])),
                'agent_count': team.get('agent_count'),
                'model_tier': team.get('agent_model_tier'),
            })
    return deltas


def _template_has_sensitive_flags(template) -> list[dict]:
    """Scan the RAW template definition for sensitive-access capabilities.

    HIVE-777 requires a fail-closed REJECT when a template targeting a customer
    tenant carries sensitive_access_flags / credential_grant_scopes — before
    the HIVE-446 resolution strips them. Check both custom capabilities and the
    builtin capability specs referenced by the template's teams.
    """
    from .org_templates import DEFAULT_TEAM_CAPABILITY_SPECS

    custom_by_slug = {
        str(cap.get('slug') or '').strip().lower().replace('_', '-').replace(' ', '-'): cap
        for cap in (template.custom_capabilities or [])
        if isinstance(cap, dict) and cap.get('slug')
    }
    hits: list[dict] = []
    for team in template.teams or []:
        team_slug = str(team.get('slug') or '').strip().lower().replace('_', '-').replace(' ', '-')
        capabilities = team.get('capabilities') or team.get('capability') or team_slug
        if isinstance(capabilities, str):
            capabilities = [capabilities]
        for cap_slug in capabilities:
            cap_slug = str(cap_slug or '').strip().lower().replace('_', '-').replace(' ', '-')
            cap = custom_by_slug.get(cap_slug) or DEFAULT_TEAM_CAPABILITY_SPECS.get(cap_slug) or {}
            flags = list(cap.get('sensitive_access_flags') or [])
            scopes = list(cap.get('default_credential_grant_scopes') or [])
            if flags or scopes:
                hits.append({
                    'team_slug': team_slug,
                    'capability_slug': cap_slug,
                    'sensitive_access_flags': sorted(set(flags)),
                    'credential_grant_scopes': sorted(set(scopes)),
                })
    return hits


def _compute_preview_hash(plan: dict) -> str:
    canonical = json.dumps(plan, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_dry_run_plan(
    *,
    requester,
    org_slug: str,
    template_slug: str,
    plan_slug: str = '',
    inputs: dict | None = None,
    tenant_kind: str = 'customer',
) -> dict:
    """VEN-178 S2: build a deterministic dry-run deploy plan for a function-pod
    template into an EXISTING org, with zero side effects.

    Planner-only: this function performs no writes and no provisioning calls.
    It validates entitlement + risk before planning:
      1. template must exist and be active (visibility),
      2. requester must be an admin/owner of the target org (authz),
      3. a sensitive-access capability targeting a customer tenant REJECTS
         (fail closed) instead of silently stripping (HIVE-777 / VEN-178 §5).
    The returned plan carries an explicit capability/risk delta list and a
    deterministic preview hash for later audit binding of preview → deploy.
    """
    from .org_templates import load_org_template

    template = load_org_template(template_slug)
    if template.status != template.Status.ACTIVE:
        raise DryRunPlanRejected(
            f'Template {template_slug!r} is not active (status={template.status}); '
            'only active templates are visible for planning.'
        )

    if not _is_org_admin_or_owner(requester, org_slug):
        raise DryRunPlanRejected(
            f'Requester is not an admin/owner of target org {org_slug!r}; '
            'planning into an existing org requires org admin/owner authorization.'
        )

    tenant_kind = _normalize_tenant_kind(tenant_kind)
    context = TemplatePlanContext(
        org_name=str(org_slug),
        org_slug=org_slug,
        plan_slug=plan_slug or 'function-pod',
        tenant_kind=tenant_kind,
    )
    template_plan = resolve_org_template_plan(template, context)

    # Fail closed: a sensitive-access capability targeting a customer tenant
    # must REJECT the plan, not silently strip (HIVE-777). Scan the RAW
    # template before HIVE-446 resolution strips the flags.
    if tenant_kind != 'internal':
        sensitive_hits = _template_has_sensitive_flags(template)
        if sensitive_hits:
            raise DryRunPlanRejected(
                'Plan rejected (fail closed): template targets a customer tenant '
                'but carries sensitive-access capabilities. '
                + json.dumps(sensitive_hits, sort_keys=True)
            )

    # Function-pod into an EXISTING org: drop the create_organization call —
    # the org already exists; only pod/team/capability provisioning is planned.
    create_calls = [
        call
        for call in template_plan.get('create_calls', [])
        if not (call.get('service') == 'admin' and call.get('action') == 'create_organization')
    ]

    capability_deltas = _capability_risk_deltas(template_plan)
    plan = {
        'plan_type': 'function_pod_into_existing_org',
        'target_org': {'slug': org_slug, 'tenant_kind': tenant_kind},
        'plan_slug': plan_slug or 'function-pod',
        'template': template_plan.get('template'),
        'teams': template_plan.get('teams', []),
        'capability_deltas': capability_deltas,
        'risk_deltas': {
            'sensitive_access_flags': sorted({
                flag
                for delta in capability_deltas
                for flag in delta.get('sensitive_access_flags', [])
            }),
            'credential_grant_scopes': sorted({
                scope
                for delta in capability_deltas
                for scope in delta.get('credential_grant_scopes', [])
            }),
            'stripped_credential_grant_scopes': sorted({
                scope
                for delta in capability_deltas
                for scope in delta.get('stripped_credential_grant_scopes', [])
            }),
        },
        'workflows': template_plan.get('workflows', []),
        'projects': template_plan.get('projects', []),
        'starter_tasks': template_plan.get('starter_tasks', []),
        'managers': template_plan.get('managers', {}),
        'create_calls': create_calls,
        'zero_side_effects': True,
        'deterministic': True,
        'inputs': dict(inputs or {}),
    }
    plan['preview_hash'] = _compute_preview_hash(plan)
    return plan


def _normalize_tenant_kind(value: str) -> str:
    value = (value or 'customer').strip().lower()
    return 'internal' if value == 'internal' else 'customer'
