"""
Fleet harness-drift roll (SCLI-6xx, operator 2026-09-21).

The harness-roll actuator (`_converge_running_native_release` /
`converge_running_lifecycle_agent`) was bolted to lease transitions. With a
full lease pool, transitions are rare, so live seats stayed on month-old
harness builds (pia/sato: Aug 28 while gen 50 was current) — every harness fix
sprint left a straggler tail that silently missed the fixes.

This command is the missing periodic actuator: converge every RUNNING native
seat whose runtime or broker contract lags the desired source. The roll is
drain-v1-fenced inside `ensure_agent_image` (fail-closed on busy turns), and
the eternal session lives on the PVC, so a roll is near-zero cost: the seat
resumes with its context intact (operator: "we just resume the earlier
session always").

Hibernated seats are deliberately skipped: their deployment template is
converged on wake by `start_agent -> ensure_agent_image`, so they self-heal.
Rate-limited per pass to avoid rolling the whole fleet at once.
"""
from django.core.management.base import BaseCommand

from fleet.lifecycle_fleet import converge_running_lifecycle_agent
from fleet.provisioner import _live_ns, get_k8s_client, native_agent_runtime_contract_drift


class Command(BaseCommand):
    help = 'Converge drifted native runtime and broker contracts (drain-v1 fenced).'

    def add_arguments(self, parser):
        parser.add_argument('--max-seats', type=int, default=6,
                            help='Max seats to roll this pass (storm guard).')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        max_seats = options['max_seats']
        dry_run = options['dry_run']

        # Ghost-holder reconciliation first (operator 2026-09-22): dormant
        # seats must never hold Cortex leases or KV homes — each ghost wastes
        # an admission slot on the pool.
        if not dry_run:
            try:
                from fleet.model_lease import reconcile_ghost_lease_holders
                ghost = reconcile_ghost_lease_holders()
                self.stdout.write(
                    f'harness-roll: ghost reconciliation checked={ghost.get("checked", 0)} '
                    f'released={len(ghost.get("released", []))}')
                for r in ghost.get('released', []):
                    self.stdout.write(f'  released: {r}')
            except Exception as exc:  # noqa: BLE001 — reconcile must not block the roll
                self.stdout.write(self.style.WARNING(f'  ghost reconciliation error: {exc}'))

        agents = self._running_native_agents()
        if not agents:
            self.stdout.write('harness-roll: no running native seats')
            return

        # Desired image comes from the RELEASE AUTHORITY (the promoted
        # generation), never from a majority heuristic — a majority cannot
        # bootstrap a newly-promoted generation (gen 50 had 0 seats).
        from fleet.native_runtime_release import get_native_runtime_release
        authority = get_native_runtime_release()
        desired_image = authority.release.deployment_image
        self.stdout.write(f'harness-roll: desired gen {authority.release.generation} '
                          f'{desired_image[:40]}')

        _core, apps, _net = get_k8s_client()
        drifted = []
        for lifecycle_agent, uname in agents:
            try:
                dep = apps.read_namespaced_deployment(
                    f'agent-{uname}', _live_ns(lifecycle_agent), _request_timeout=(3, 5))
            except Exception:
                continue
            image = ''
            for c in (dep.spec.template.spec.containers or []):
                if c.name == 'agent':
                    image = c.image or ''
                    break
            # The agent image can already match while its broker or signed
            # projection wiring remains old. Select the managed contract, then
            # let ordinary convergence enforce the existing idle-turn fence.
            if image and (image != desired_image or
                          native_agent_runtime_contract_drift(lifecycle_agent, desired_image)):
                drifted.append((uname, lifecycle_agent))

        if not drifted:
            self.stdout.write('harness-roll: running runtime and broker contracts converged — no drift')
            return

        self.stdout.write(f'harness-roll: {len(drifted)} drifted seats; rolling up to {max_seats}')
        rolled = 0
        for uname, la in drifted[:max_seats]:
            if dry_run:
                self.stdout.write(f'  DRY-RUN: would converge {uname}')
                continue
            try:
                ok, detail = converge_running_lifecycle_agent(la)
                converged = bool(isinstance(detail, dict) and detail.get('converged'))
                self.stdout.write(f'  {uname}: ok={bool(ok)} converged={converged}')
                rolled += 1
            except Exception as exc:  # noqa: BLE001 — one seat must not stop the sweep
                self.stdout.write(self.style.WARNING(f'  {uname}: converge error {exc}'))
        self.stdout.write(f'harness-roll: rolled {rolled}/{len(drifted[:max_seats])} drifted seats')

    def _running_native_agents(self):
        from fleet.models import FleetAgent
        out = []
        for a in FleetAgent.objects.exclude(
                status__in=['stopped', 'deleted', 'archived', 'hibernated']):
            if getattr(a, 'status', '') != 'running':
                continue
            out.append((a, a.agent_username))
        return out
