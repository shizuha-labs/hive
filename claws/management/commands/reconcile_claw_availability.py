"""PLAT-4122: claw availability + tier-envelope resource reconcile.

Two silent-failure classes from the 2026-07-09 incident (hritik's claw down
~10.5h while its DB row said running):

1. **Out-of-envelope resources** — any writer (hand kubectl-patch, ad-hoc bulk
   script doing read-modify-write) can leave the claw Deployment with
   resources exceeding the tier's LimitRange/ResourceQuota. k8s validates only
   at pod CREATION, so the violation sits dormant until the next pod churn,
   then every replacement pod is forbidden → 0 replicas forever. The tier spec
   is the ONLY authority for claw resources (the provisioner always builds
   from it); this command clamps drifted Deployments back to it.

2. **Running-but-unavailable** — Claw.status stays 'running' (desired state)
   while the Deployment serves 0 ready replicas. Emit a ClawEvent
   ('unavailable', deduped to one per window) so the UI/event lane surfaces
   the outage instead of it rotting silently.

Idempotent; wired into the hive-claw-isolation-reconcile CronJob (*/30).
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from claws.models import Claw, ClawEvent
from claws.provisioner import (
    DEPLOYMENT_NAME,
    get_k8s_client,
    probe_cortex_model_access,
    resolve_cortex_model,
    resolve_tier,
)

UNAVAILABLE_GRACE = timedelta(minutes=5)
UNAVAILABLE_EVENT_DEDUPE = timedelta(minutes=30)
MODEL_UNAVAILABLE_PREFIX = 'Cortex model unavailable for this claw credential:'


def _kubernetes_timestamp(value) -> datetime | None:
    """Normalize Kubernetes-client timestamps to aware UTC datetimes.

    Client versions differ on whether an RFC3339 ``Z`` timestamp is returned
    as aware UTC or naive UTC. Django's clock is aware when ``USE_TZ`` is on,
    so normalizing at this API boundary prevents a deterministic subtraction
    failure from aborting the entire availability reconcile.
    """
    if isinstance(value, str):
        value = parse_datetime(value)
    if value is None:
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, UTC)
    return value


def _res_dict(container) -> dict:
    res = getattr(container, 'resources', None)
    return {
        'requests': dict(getattr(res, 'requests', None) or {}),
        'limits': dict(getattr(res, 'limits', None) or {}),
    }


class Command(BaseCommand):
    help = 'Clamp claw Deployment resources to tier + flag running-but-unavailable claws (PLAT-4122).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report drift/outages without patching or writing events.')

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        try:
            core_v1, apps_v1 = get_k8s_client()[:2]
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(f'k8s client unavailable: {exc}')
            return
        from kubernetes.client.rest import ApiException

        clamped, unavailable, model_unavailable, model_reconciled, checked = [], [], [], [], 0
        for claw in Claw.objects.filter(status=Claw.Status.RUNNING):
            ns = claw.namespace
            if not ns:
                continue
            checked += 1
            try:
                deploy = apps_v1.read_namespaced_deployment(DEPLOYMENT_NAME, ns)
            except ApiException as exc:
                if exc.status == 404:
                    self._note_unavailable(claw, 'deployment missing', dry, unavailable)
                continue
            except Exception:  # noqa: BLE001
                continue

            tier = resolve_tier(claw.tier_id)
            self._check_model_access(
                core_v1,
                apps_v1,
                deploy,
                claw,
                tier,
                dry=dry,
                unavailable_sink=model_unavailable,
                reconciled_sink=model_reconciled,
            )

            want = {
                'requests': {'cpu': tier.cpu_request, 'memory': tier.mem_request},
                'limits': {'cpu': tier.cpu_limit, 'memory': tier.mem_limit},
            }
            containers = deploy.spec.template.spec.containers or []
            main = next((c for c in containers if c.name == DEPLOYMENT_NAME), None)
            if main is not None and _res_dict(main) != want:
                detail = f'{_res_dict(main)} -> {want}'
                if not dry:
                    body = {'spec': {'template': {'spec': {'containers': [{
                        'name': DEPLOYMENT_NAME, 'resources': want,
                    }]}}}}
                    try:
                        apps_v1.patch_namespaced_deployment(DEPLOYMENT_NAME, ns, body)
                        ClawEvent.objects.create(
                            claw=claw, event_type='resources_clamped',
                            message=f'PLAT-4122: deployment resources drifted from tier {claw.tier_id or "free"}; clamped {detail}',
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.stderr.write(f'{claw.id} clamp failed: {exc}')
                        continue
                clamped.append((str(claw.id), detail))

            desired = deploy.spec.replicas or 0
            ready = (deploy.status.ready_replicas or 0) if deploy.status else 0
            if desired >= 1 and ready == 0:
                # Grace: only flag when the Available condition has been False
                # long enough that this is an outage, not a rollout blip.
                stale = False
                for cond in (deploy.status.conditions or []) if deploy.status else []:
                    if cond.type == 'Available' and cond.status != 'True':
                        ts = _kubernetes_timestamp(cond.last_transition_time)
                        if ts and timezone.now() - ts > UNAVAILABLE_GRACE:
                            stale = True
                if stale:
                    self._note_unavailable(claw, f'{desired} desired, 0 ready > {UNAVAILABLE_GRACE}', dry, unavailable)

        self.stdout.write(
            f'checked={checked} clamped={len(clamped)} unavailable={len(unavailable)} '
            f'model_unavailable={len(model_unavailable)} model_reconciled={len(model_reconciled)}'
            + (' (dry-run)' if dry else '')
        )
        for cid, detail in clamped:
            self.stdout.write(f'CLAMPED {cid}: {detail}')
        for cid, detail in unavailable:
            self.stdout.write(f'UNAVAILABLE {cid}: {detail}')
        for cid, detail in model_unavailable:
            self.stdout.write(f'MODEL_UNAVAILABLE {cid}: {detail}')
        for cid, detail in model_reconciled:
            self.stdout.write(f'MODEL_RECONCILED {cid}: {detail}')

    def _check_model_access(
        self,
        core_v1,
        apps_v1,
        deployment,
        claw,
        tier,
        *,
        dry,
        unavailable_sink,
        reconciled_sink,
    ):
        """Reconcile the exact desired model without broadening entitlement.

        Old claw Secrets can retain a retired tier model after Hive's durable
        desired model changes. Probe the desired key/model pair once. Only an
        authoritative available catalog result may update the Secret and roll
        the Deployment; unavailable or unknown results are fail-closed and
        leave the running workload untouched.
        """
        try:
            secret = core_v1.read_namespaced_secret('claw-config', claw.namespace)
            data = dict(getattr(secret, 'data', None) or {})
            key = base64.b64decode(data.get('cortex-api-key') or b'').decode('utf-8')
            observed_model = base64.b64decode(data.get('cortex-model') or b'').decode('utf-8')
        except Exception:  # noqa: BLE001 - secret/API outage is unknown, not entitlement
            return
        desired_model = resolve_cortex_model(claw, tier)
        access = probe_cortex_model_access(key, desired_model)
        if access.status == 'unknown':
            return
        if access.status == 'available':
            observed_id = str(observed_model or '').removeprefix('cortex/')
            desired_id = str(desired_model or '').removeprefix('cortex/')
            template_meta = getattr(getattr(deployment.spec, 'template', None), 'metadata', None)
            annotations = dict(getattr(template_meta, 'annotations', None) or {})
            applied_id = str(annotations.get('shizuha.io/cortex-model') or '').removeprefix('cortex/')
            if observed_id != desired_id or applied_id != desired_id:
                detail = (
                    f'secret={observed_model or "unset"}, applied={annotations.get("shizuha.io/cortex-model") or "unset"} '
                    f'-> {desired_model}'
                )
                reconciled_sink.append((str(claw.id), detail))
                if dry:
                    return
                try:
                    if observed_id != desired_id:
                        encoded = base64.b64encode(desired_model.encode('utf-8')).decode('ascii')
                        core_v1.patch_namespaced_secret(
                            'claw-config', claw.namespace, {'data': {'cortex-model': encoded}},
                        )
                    apps_v1.patch_namespaced_deployment(
                        DEPLOYMENT_NAME,
                        claw.namespace,
                        {'spec': {'template': {'metadata': {'annotations': {
                            'shizuha.io/cortex-model': desired_model,
                            'shizuha.io/model-reconciled-at': timezone.now().isoformat(),
                        }}}}},
                    )
                    ClawEvent.objects.create(
                        claw=claw,
                        event_type='model_reconciled',
                        message=f'Cortex model reconciled after catalog authorization: {detail}',
                    )
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(f'{claw.id} model reconcile failed: {exc}')
                    return
            if str(claw.status_message or '').startswith(MODEL_UNAVAILABLE_PREFIX) and not dry:
                claw.status_message = ''
                claw.save(update_fields=['status_message'])
                ClawEvent.objects.create(
                    claw=claw,
                    event_type='model_available',
                    message=f'Cortex model access recovered for {desired_model}',
                )
            return

        detail = f'{desired_model or "unset"}: {access.reason}'
        unavailable_sink.append((str(claw.id), detail))
        if dry:
            return
        message = f'{MODEL_UNAVAILABLE_PREFIX} {detail}'
        if claw.status_message != message:
            claw.status_message = message
            claw.save(update_fields=['status_message'])
            ClawEvent.objects.create(
                claw=claw,
                event_type='model_unavailable',
                message=message,
            )

    def _note_unavailable(self, claw, detail, dry, sink):
        sink.append((str(claw.id), detail))
        if dry:
            return
        since = timezone.now() - UNAVAILABLE_EVENT_DEDUPE
        if ClawEvent.objects.filter(
            claw=claw, event_type='unavailable', created_at__gte=since,
        ).exists():
            return
        ClawEvent.objects.create(
            claw=claw, event_type='unavailable',
            message=f'PLAT-4122: claw is running in Hive but serving 0 ready replicas ({detail})',
        )
