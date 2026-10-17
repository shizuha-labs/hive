"""Consume the claw deletion/reclaim pool (the missing HIVE-7 hard-delete worker).

Until this command existed, nothing ever advanced a claw past ``deleting``:
``begin_delete_claw``/``mark_reclaim_now`` only stamp DB state, and
``destroy_claw`` had no production caller — so every deleted claw sat in the
Deleting state forever (operator-reported 2026-07-09, "My Assistant" stuck).

Run inside the hive-api pod (wired into the claw reconcile CronJob):

    # Promote expired 60s undo windows + destroy reclaim-ready claws
    python manage.py finalize_claw_deletions

    # Report what would happen without touching Kubernetes
    python manage.py finalize_claw_deletions --dry-run
"""

from django.core.management.base import BaseCommand, CommandError

from claws import idle_recycle_metrics, lifecycle
from claws.models import Claw, ClawEvent
from claws.provisioner import destroy_claw, stop_claw


class Command(BaseCommand):
    help = (
        'Promote expired delete windows into the reclaim pool and hard-delete '
        'reclaim-ready claws (60s undo -> 24h retention -> teardown). Also '
        'scales idle Running OpenClaw tenants to zero (ADR-0080/HIVE-2011) and '
        'fails loudly when an idle scale-to-zero cannot complete.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Only report candidates; do not modify state or Kubernetes.',
        )

    def _stop_idle(self, claw, *, dry_run):
        """Stop one idle Running claw; returns True on success/False on failure.

        Missing Deployment is still idle — mark STOPPED so the row cannot sit
        RUNNING with no workload (HIVE-1690 ghosts).
        """
        if dry_run:
            self.stdout.write(f'would stop idle {claw.namespace or claw.id}')
            return True
        if stop_claw(claw, ok_if_missing=True):
            idle_recycle_metrics.record_stopped()
            try:
                ClawEvent.objects.create(
                    claw=claw,
                    event_type='idle_stopped',
                    message='Scaled to 0 by the idle-recycle worker (idle TTL elapsed)',
                )
            except Exception:  # noqa: BLE001 — observability must not break the stop
                pass
            self.stdout.write(f'stopped idle {claw.namespace or claw.id}')
            return True
        idle_recycle_metrics.record_stop_failure()
        self.stderr.write(f'idle stop FAILED for {claw.namespace or claw.id}')
        return False

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if dry_run:
            promoted = (
                'skipped (dry-run)'
            )
        else:
            promoted = lifecycle.promote_expired_delete_windows()

        idle_stopped = 0
        idle_failed = 0
        idle_candidates = list(lifecycle.idle_stop_candidates())
        failed_ids = []
        for claw in idle_candidates:
            if self._stop_idle(claw, dry_run=dry_run):
                idle_stopped += 1
            else:
                idle_failed += 1
                failed_ids.append(claw.id)

        # Retry transient k8s scale failures at the tail (the runtime control
        # endpoint can be briefly busy applying a large reconcile batch). A
        # persistent failure must NOT report a green CronJob — that is exactly
        # the silent scale-to-zero failure ADR-0080 called out. The retry keeps
        # one transient blip from failing the whole idempotent chain.
        if not dry_run and failed_ids:
            for claw_id in list(failed_ids):
                claw = Claw.objects.filter(pk=claw_id).first()
                if claw is None:
                    failed_ids.remove(claw_id)
                    continue
                self.stderr.write(f'RETRY idle stop for {claw.namespace or claw.id}')
                if self._stop_idle(claw, dry_run=False):
                    idle_stopped += 1
                    idle_failed -= 1
                    failed_ids.remove(claw_id)

        # Stale-Running gauge: every Running claw still past its idle TTL after
        # the reconcile attempt (i.e. stop failed) is the exact ADR-0080 failure
        # mode and must be visible to the HiveClawIdleRunning alert.
        if not dry_run:
            idle_recycle_metrics.set_idle_running_set(
                [
                    (str(c.id), c.namespace, 1)
                    for c in Claw.objects.filter(
                        id__in=failed_ids,
                        status=Claw.Status.RUNNING,
                    )
                ]
                or [('', '', 0)]
            )

        destroyed = 0
        failed = 0
        skipped = 0
        candidates = list(lifecycle.reclaim_candidates())
        for candidate in candidates:
            claw = Claw.objects.filter(pk=candidate.claw_id).first()
            if claw is None:
                continue
            # HARD GUARD: only claws the delete flow actually parked in DELETING
            # may be torn down. `recycle_after` is ALSO stamped at provision time
            # (created+30min) on running claws, so reclaim_candidates() alone
            # would select live assistants — destroying those is data loss.
            if claw.status != Claw.Status.DELETING:
                skipped += 1
                continue
            if dry_run:
                self.stdout.write(
                    f'would destroy {candidate.namespace} ({candidate.reason})'
                )
                continue
            if destroy_claw(claw):
                destroyed += 1
                self.stdout.write(f'destroyed {candidate.namespace} ({candidate.reason})')
            else:
                # destroy_claw already flipped the row to FAILED with the k8s
                # error; keep going so one bad namespace can't wedge the pool.
                failed += 1
                self.stderr.write(f'destroy FAILED for {candidate.namespace}')

        self.stdout.write(
            f'claw-finalize: promoted={promoted} idle_due={len(idle_candidates)} '
            f'idle_stopped={idle_stopped} idle_failed={idle_failed} '
            f'candidates={len(candidates)} destroyed={destroyed} '
            f'skipped_not_deleting={skipped} failed={failed} dry_run={dry_run}'
        )

        # Fail-loud guard (HIVE-2011 / ADR-0080): an idle scale-to-zero that
        # cannot complete after retry must fail the CronJob so the existing
        # HiveClawIsolationReconcileFailed alert fires. This is the exact
        # "scale-to-zero silently failed for months" failure mode — never
        # report a green reconcile while an idle Running claw is stuck.
        if not dry_run and idle_failed > 0:
            names = ', '.join(
                str(c.namespace or c.id)
                for c in Claw.objects.filter(id__in=failed_ids)
            )
            raise CommandError(
                f'Idle scale-to-zero incomplete after retry for {idle_failed} '
                f'Running claw(s): {names or "unknown"}; PVC retained, next sweep '
                're-attempts. Check the HiveClawIdleRunning alert for the stale '
                'Running gauge.'
            )
