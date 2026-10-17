"""PLAT-4729/PLAT-6041: label already-provisioned host-plane SSH seats.

Usage:
    python manage.py backfill_hostplane_ssh_label [--agent USERNAME] [--dry-run]

``_base_labels`` writes ``shizuha.io/agent-role=devops-ssh`` for agents with
``hostplane_ssh=True``, but it is only consulted by ``_create_deployment``, which
runs at provision time (``provision_agent``) and nowhere else — there is no
periodic reconcile of pod-template labels. So flipping ``hostplane_ssh`` on an
existing seat changes the DB row and nothing in the cluster: the shared-namespace
policy ``fleet-allow-hostplane-ssh-egress-v2`` keeps selecting zero pods and the
seat still has no tailnet reach. This command closes that gap, the same way
``backfill_isolation_policies`` did for the per-agent policy set.

It patches the pod template only — no NetworkPolicy is created here. The policy
is declarative in ``k8s/fleet/30-fleet-isolation-netpol.yaml`` and applied by
``.forgejo/workflows/deploy-fleet-networkpolicy.yml``; an agent seat must never
hand-apply the object that grants its own reach.

Patching a pod template rolls the Deployment, so a RUNNING seat restarts. That is
the intended effect (the label has to be on a live pod to matter) but it is not
free — use ``--agent`` to scope the roll to one seat. For STOPPED/HIBERNATED
seats (replicas=0) the label simply takes effect on next start.
"""
from django.core.management.base import BaseCommand

from fleet import provisioner
from fleet.models import FleetAgent


class Command(BaseCommand):
    help = 'Patch shizuha.io/agent-role=devops-ssh onto provisioned hostplane_ssh seats (PLAT-4729)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--agent', default='',
            help='Restrict to a single agent_username (keeps the rollout blast radius to one seat)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print which agents would be patched without applying changes',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        agents = FleetAgent.objects.filter(
            hostplane_ssh=True,
            status__in=[
                FleetAgent.Status.RUNNING,
                FleetAgent.Status.HIBERNATED,
                FleetAgent.Status.STOPPED,
            ],
        ).exclude(namespace='')
        username = (options['agent'] or '').strip()
        if username:
            agents = agents.filter(agent_username=username)

        if not agents.exists():
            self.stdout.write(
                'No provisioned hostplane_ssh agents found — nothing to backfill. '
                '(Designation is a data decision: set hostplane_ssh=True on the '
                'DevOps seats first.)'
            )
            return

        _, apps_v1, _ = provisioner.get_k8s_client()
        patched = 0
        failed = 0
        for agent in agents:
            self.stdout.write(
                f'{"[dry-run] " if dry_run else ""}Labelling {agent.agent_username} '
                f'(ns={agent.namespace}, status={agent.status}) ...'
            )
            if dry_run:
                continue
            try:
                provisioner.apply_hostplane_ssh(agent)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.stdout.write(self.style.WARNING(
                    f'  ⚠ hostplane SSH apply failed for {agent.agent_username}: {exc}'
                ))
                continue
            patched += 1
            self.stdout.write(self.style.SUCCESS(
                f'  ✓ secret + devops-ssh label + ~/.ssh mount applied to '
                f'{agent.namespace}/{agent.deployment_name}'
            ))

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'Dry run: {agents.count()} agent(s) would be patched.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Done: {patched} patched, {failed} failed.'
            ))
