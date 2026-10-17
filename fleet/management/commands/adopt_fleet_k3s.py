"""
HIVE-253 P4: Adopt existing k3s FleetAgent rows under fleet-daemon-k8s supervision.

Usage:
    python manage.py adopt_fleet_k3s [--daemon-id DAEMON_ID] [--dry-run] [--skip-id-api]

What it does:
  1. Discovers running k3s pods in the fleet namespace (label selector).
  2. For each pod, finds the FleetAgent row and validates identity (INV-ID, §4.3):
     - Calls Shizuha-ID API to check account_type=='agent' and is_active==True
       (unless --skip-id-api; pass that flag when the ID API is unreachable)
     - account_type != 'agent' (human/other masquerading) → REJECT
     - inactive (is_active == False) → REJECT
     - Missing agent_id → ADMIT with warning (identity repair recommended)
     - Otherwise → ADMIT
  3. Sets FleetAgent.managing_daemon for admitted agents.
  4. Scales down REJECTED pods (--dry-run skips the actual scale).
  5. Emits a structured summary.

The concrete junior-agent rename cleanup specific to HIVE-233 §4.4 is complete.
This command now validates identity records only; it does not impose naming
pattern rules.
"""
import logging
import re

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


def _lookup_shizuha_id(username, platform_url):
    """Fetch account_type and is_active for `username` from the Shizuha-ID API.

    Endpoint: GET {platform_url}/id/api/internal/agents/?username=<username>
    Returns dict with exists/account_type/is_active/user_id, or None on error.
    None means unknown; callers treat unknown as lenient (no rejection).
    """
    token = getattr(settings, 'HIVE_ID_FLEET_PROVISIONER_TOKEN', '') or ''
    url = f'{platform_url.rstrip("/")}/id/api/internal/agents/'
    try:
        resp = requests.get(
            url,
            params={'username': username},
            headers={'X-Fleet-Provisioner-Token': token} if token else {},
            timeout=10,
        )
        if not resp.ok:
            return None
        return resp.json()
    except Exception as exc:
        logger.warning('adopt_fleet_k3s: ID API lookup failed for %s: %s', username, exc)
        return None


class Command(BaseCommand):
    help = 'Adopt existing k3s FleetAgent rows under fleet-daemon-k8s (HIVE-253 P4)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--daemon-id',
            default='fleet-daemon-k8s',
            help='DaemonRegistry daemon_id to assign as managing_daemon (default: fleet-daemon-k8s)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Validate identity and report — do NOT scale down rejected pods or set managing_daemon',
        )
        parser.add_argument(
            '--skip-id-api',
            action='store_true',
            help='Skip Shizuha-ID API checks (account_type, is_active) — use when ID API is unreachable',
        )
        parser.add_argument(
            '--namespace',
            default=getattr(settings, 'SHIZUHA_FLEET_NAMESPACE', 'shizuha-fleet'),
            help='Fleet k8s namespace (default: SHIZUHA_FLEET_NAMESPACE setting)',
        )

    def handle(self, *args, **options):
        from fleet.models import FleetAgent, DaemonRegistry
        from fleet.provisioner import get_k8s_client

        daemon_id = options['daemon_id']
        dry_run = options['dry_run']
        skip_id_api = options['skip_id_api']
        namespace = options['namespace']

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be made'))
        if skip_id_api:
            self.stdout.write(self.style.WARNING(
                '--skip-id-api: Shizuha-ID account_type/is_active checks are skipped. '
                'Run without this flag once the ID API is reachable for full INV-ID validation.'
            ))

        platform_url = getattr(settings, 'SHIZUHA_PLATFORM_URL', '').rstrip('/')

        # ------------------------------------------------------------------
        # 1. Resolve daemon registry entry
        # ------------------------------------------------------------------
        try:
            daemon = DaemonRegistry.objects.get(daemon_id=daemon_id)
        except DaemonRegistry.DoesNotExist:
            self.stdout.write(self.style.WARNING(
                f"DaemonRegistry entry for '{daemon_id}' not found — "
                "adoption will validate only; managing_daemon will not be set."
            ))
            daemon = None

        # ------------------------------------------------------------------
        # 2. Enumerate running k3s pods in the fleet namespace
        # ------------------------------------------------------------------
        try:
            core_v1, apps_v1, _ = get_k8s_client()
        except Exception as e:
            raise CommandError(f'Cannot get k8s client: {e}')

        try:
            pods = core_v1.list_namespaced_pod(
                namespace,
                label_selector='app.kubernetes.io/part-of=shizuha-fleet',
            )
        except Exception as e:
            logger.warning('adopt_fleet_k3s: label-selector list failed (%s); falling back to all pods', e)
            try:
                pods = core_v1.list_namespaced_pod(namespace)
            except Exception as e2:
                raise CommandError(f'Cannot list pods in namespace {namespace}: {e2}')

        self.stdout.write(f'Found {len(pods.items)} pods in namespace {namespace}')

        # ------------------------------------------------------------------
        # 3. Build set of running canonical usernames for duplicate check
        # ------------------------------------------------------------------
        running_usernames = set()
        for pod in pods.items:
            if pod.status.phase not in ('Running', 'Pending'):
                continue
            match = re.match(r'^agent-([a-z0-9][a-z0-9-]*)(?:-[a-f0-9]{8,10}-[a-z0-9]{5})?$',
                             pod.metadata.name)
            if match:
                running_usernames.add(match.group(1))

        self.stdout.write(f'Running agent usernames: {sorted(running_usernames)}')

        # ------------------------------------------------------------------
        # 4. Validate identity for each agent pod
        # ------------------------------------------------------------------
        admitted = []
        rejected = []
        not_found = []

        for pod in pods.items:
            if pod.status.phase not in ('Running', 'Pending'):
                continue
            pod_name = pod.metadata.name
            match = re.match(r'^agent-([a-z0-9][a-z0-9-]*)(?:-[a-f0-9]{8,10}-[a-z0-9]{5})?$',
                             pod_name)
            if not match:
                continue
            username = match.group(1)

            try:
                agent = FleetAgent.objects.get(agent_username=username)
            except FleetAgent.DoesNotExist:
                not_found.append(username)
                self.stdout.write(self.style.WARNING(
                    f'  SKIP {username}: no FleetAgent row'
                ))
                continue

            id_data = None
            if not skip_id_api and platform_url:
                id_data = _lookup_shizuha_id(username, platform_url)

            verdict, reason = self._validate_identity(username, agent, running_usernames, id_data)

            if verdict == 'admit':
                admitted.append((username, agent, reason))
                self.stdout.write(self.style.SUCCESS(f'  ADMIT  {username}: {reason}'))
            else:
                rejected.append((username, agent, reason))
                self.stdout.write(self.style.ERROR(f'  REJECT {username}: {reason}'))

        # ------------------------------------------------------------------
        # 5. Apply changes
        # ------------------------------------------------------------------
        if not dry_run:
            for username, agent, _ in admitted:
                if daemon is not None:
                    agent.managing_daemon = daemon
                    agent.save(update_fields=['managing_daemon'])
                    self.stdout.write(f'  SET managing_daemon={daemon_id} for {username}')

            for username, agent, reason in rejected:
                deployment_name = f'agent-{username}'
                self.stdout.write(f'  SCALING DOWN {deployment_name} (reason: {reason})')
                try:
                    apps_v1.patch_namespaced_deployment_scale(
                        deployment_name,
                        namespace,
                        {'spec': {'replicas': 0}},
                    )
                    self.stdout.write(self.style.SUCCESS(f'  Scaled down {deployment_name} → 0 replicas'))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'  Failed to scale {deployment_name}: {e}'))
        else:
            self.stdout.write('\n[DRY RUN] Would apply:')
            for username, _, _ in admitted:
                self.stdout.write(f'  SET managing_daemon={daemon_id} for {username}')
            for username, _, reason in rejected:
                self.stdout.write(f'  SCALE DOWN agent-{username} (reason: {reason})')

        # ------------------------------------------------------------------
        # 6. Summary
        # ------------------------------------------------------------------
        self.stdout.write('\n=== Adoption Summary ===')
        self.stdout.write(f'Admitted : {[u for u, _, _ in admitted]}')
        self.stdout.write(f'Rejected : {[u for u, _, _ in rejected]}')
        if not_found:
            self.stdout.write(f'No DB row: {not_found}')
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN complete — no changes applied'))
        else:
            self.stdout.write(self.style.SUCCESS('Adoption complete'))

    # ------------------------------------------------------------------
    # Identity validation (INV-ID, §4.3)
    # ------------------------------------------------------------------
    def _validate_identity(self, username, agent, running_usernames, id_data):
        """Return (verdict, reason). verdict is 'admit' or 'reject'.

        Checks applied (§4.3):
        1. account_type != 'agent' (from ID API, if available) → REJECT
        2. is_active == False (from ID API, if available) → REJECT
        3. Missing agent_id → ADMIT with warning
        4. Otherwise → ADMIT

        id_data is the parsed Shizuha-ID API response dict, or None when
        --skip-id-api was passed or the API was unreachable. None = lenient.
        """
        # Checks (1)/(2): Shizuha-ID API validation
        if id_data is not None:
            if not id_data.get('exists', False):
                return 'reject', f'no Shizuha-ID record found for {username}'
            account_type = id_data.get('account_type', 'unknown')
            if account_type != 'agent':
                return 'reject', f'account_type={account_type!r} (must be "agent", INV-ID §4.3)'
            is_active = id_data.get('is_active', None)
            if is_active is False:
                return 'reject', f'Shizuha-ID account is inactive (is_active=False, INV-ID §4.3)'

        # Check (3): missing agent_id
        if not agent.agent_id:
            return 'admit', 'no agent_id set — admitting with warning (identity repair needed)'

        return 'admit', 'identity checks passed'
