"""PLAT-174 P2: backfill isolation NetworkPolicies for already-provisioned isolated agents.

Usage:
    python manage.py backfill_isolation_policies [--dry-run]

Run once after deploying PLAT-174 to install the full policy set (default-deny + baseline
egress + SSH) into existing fleet-<username> namespaces that were provisioned before this
feature shipped. Safe to re-run: the provisioner uses create→replace-on-409.
"""
from django.core.management.base import BaseCommand
from fleet.models import FleetAgent
from fleet import provisioner


class Command(BaseCommand):
    help = 'Backfill isolation NetworkPolicies for existing isolated fleet agents (PLAT-174)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print which agents would be updated without applying changes',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        # P2: include STOPPED agents — they retain their namespace/Deployment and
        # will restart without isolation policies if not backfilled here.
        agents = FleetAgent.objects.filter(
            isolated=True,
            status__in=[
                FleetAgent.Status.RUNNING,
                FleetAgent.Status.HIBERNATED,
                FleetAgent.Status.STOPPED,
            ],
            namespace__startswith='fleet-',
        )
        if not agents.exists():
            self.stdout.write('No isolated agents found — nothing to backfill.')
            return

        _, apps_v1, networking_v1 = provisioner.get_k8s_client()
        for agent in agents:
            self.stdout.write(f'{"[dry-run] " if dry_run else ""}Backfilling {agent.agent_username} '
                              f'(ns={agent.namespace}, status={agent.status}) ...')
            if not dry_run:
                provisioner._create_ssh_egress_network_policies(networking_v1, agent.namespace, agent)
                # P1: patch the Deployment's pod-template labels to add the selector
                # label that the SSH NetworkPolicies use.  Existing pods were created
                # before _base_labels started writing shizuha.io/agent-role=devops-ssh,
                # so the SSH policies would select zero pods without this patch.
                # For STOPPED agents (replicas=0) the label takes effect on next start.
                try:
                    apps_v1.patch_namespaced_deployment(
                        name=agent.deployment_name,
                        namespace=agent.namespace,
                        body={'spec': {'template': {'metadata': {'labels': {
                            'shizuha.io/agent-role': 'devops-ssh',
                        }}}}},
                    )
                    self.stdout.write(self.style.SUCCESS(
                        f'  ✓ policies + deployment label applied to {agent.namespace}'
                    ))
                except Exception as exc:  # noqa: BLE001
                    self.stdout.write(self.style.WARNING(
                        f'  ⚠ policies applied but deployment patch failed: {exc}'
                    ))

        if dry_run:
            self.stdout.write(self.style.WARNING(f'Dry run: {agents.count()} agent(s) would be updated.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Done: {agents.count()} agent(s) updated.'))
