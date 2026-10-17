"""Team-based GitHub credential injection (PLAT-4238) — identity-pointer reconciler.

Operator 2026-07-11/12: review + non-write teams use the sara2574 (read-only)
GitHub identity; write teams (merge/devops/platform-ops/engineering + org-scoped
engineering) use kai2574 (write). Separation-of-duties holds by construction
(single-team mandate: no agent is both).

HOW IT WORKS (durable — with the daemon render change, cli 867add57):
Hive assigns each agent a NON-SECRET ``GITHUB_IDENTITY`` env pointer (e.g.
``sara2574``), team-resolved from the Pulse TeamRoster. The daemon's k8s render
turns that pointer into ``GITHUB_TOKEN valueFrom secretKeyRef
{<identity>-github-token/GITHUB_TOKEN}`` — the pod references the shared
team-identity Secret DIRECTLY. No token bytes flow through Hive config, the
roster, or ``<user>-agent-creds``; rotating an identity = updating one Secret.

History of wrong layers (kept so nobody re-treads them):
- Patching ``<user>-agent-creds`` — daemon re-renders it from the broker grant →
  reverted on every reconcile.
- Writing the raw token into ``FleetAgent.env['GITHUB_TOKEN']`` — the render's
  broker-grant precedence beat it AND it leaked plaintext onto the config surface.
The identity POINTER has neither problem: it is not a credential, and the render
consumes it explicitly.

Dry-run by default; ``--apply`` persists ``FleetAgent.env['GITHUB_IDENTITY']`` and
live-pushes via ``configure_runtime_agent`` (the daemon reseed also replays env).
Agents converge on their next restart/re-render (identity is in the config-drift
hash, so the daemon re-renders changed agents itself). Kill-switch
``HIVE_GITHUB_IDENTITY_RECONCILE_ENABLED=0``.
"""
from __future__ import annotations

import base64
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fleet.on_demand import memberships_by_email, team_slug_from_key


# Teams whose agents legitimately push/merge to git repos → write identity.
# Everyone else gets the read-only identity. Keep in sync with Pulse
# WorkflowService._WRITE_GITHUB_TEAMS (a2b42fb).
WRITE_TEAMS = frozenset({
    'merge', 'devops', 'platform-ops', 'engineering',
    'securities-engineering', 'trading-engineering',
})

READONLY_IDENTITY = 'sara2574'
WRITE_IDENTITY = 'kai2574'

# ORIG-18 source-control cutover: once Origin (Forgejo) is primary, GitHub is a
# read-only mirror for EVERY agent — write flows use per-agent Forgejo tokens
# via the credential broker (ORIG-15). Setting this env collapses the mapping
# to sara2574 for all teams so kai2574's write PATs can be revoked without the
# GITHUB_TOKEN health invariant (k8s-backend gh-api-user probe) going red.
import os
GITHUB_READONLY_CUTOVER = os.environ.get(
    'HIVE_GITHUB_READONLY_CUTOVER', ''
).strip().lower() in ('1', 'true', 'yes')


def identity_secret_name(identity: str) -> str:
    return f'{identity}-github-token'


class Command(BaseCommand):
    help = (
        'Assign each agent its team GitHub identity pointer (PLAT-4238): '
        'write teams -> kai2574, everyone else -> sara2574. Dry-run unless --apply.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Persist env + live-push (default: dry-run).')
        parser.add_argument('--only', default='', help='Comma-separated agent usernames to limit to.')
        parser.add_argument('--namespace', default=getattr(settings, 'SHIZUHA_FLEET_NAMESPACE', 'shizuha-fleet'))

    def handle(self, *args, **opts):
        if os.environ.get('HIVE_GITHUB_IDENTITY_RECONCILE_ENABLED', '1') != '1':
            self.stdout.write('HIVE_GITHUB_IDENTITY_RECONCILE_ENABLED=0 — skipping')
            return
        apply = bool(opts['apply'])
        ns = opts['namespace']
        only = {u.strip().lower() for u in (opts['only'] or '').split(',') if u.strip()}

        from fleet.models import FleetAgent
        from fleet.provisioner import get_k8s_client
        from fleet.runtime_fleet import configure_runtime_agent

        # Preflight (fail closed): both identity Secrets must exist with a
        # non-empty GITHUB_TOKEN — assigning a pointer to a missing Secret would
        # leave agents with an unresolvable optional ref (empty GITHUB_TOKEN).
        core_v1, _, _ = get_k8s_client()
        for identity in (READONLY_IDENTITY, WRITE_IDENTITY):
            if not self._secret_has_token(core_v1, ns, identity_secret_name(identity)):
                raise CommandError(
                    f'identity Secret {identity_secret_name(identity)}/GITHUB_TOKEN missing or empty in {ns}')

        rosters = self._fetch_pulse_rosters()
        if not rosters:
            raise CommandError('No Pulse rosters available; refusing to reconcile github identities.')
        email_to_teams = memberships_by_email(rosters)

        changed = same = 0
        for agent in FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED).order_by('agent_username'):
            uname = (agent.agent_username or '').strip().lower()
            if not uname or (only and uname not in only):
                continue
            # Lifecycle membership identities are organization-qualified so
            # same-named rosters cannot overwrite one another. GitHub policy is
            # intentionally team-slug based, so discard only the org qualifier.
            teams = {
                team_slug_from_key(t)
                for t in email_to_teams.get(f'{uname}@shizuha.com', set())
            }
            is_write = bool(teams & WRITE_TEAMS) and not GITHUB_READONLY_CUTOVER
            want = WRITE_IDENTITY if is_write else READONLY_IDENTITY

            env = dict(agent.env or {})
            if env.get('GITHUB_IDENTITY') == want:
                same += 1
                continue

            changed += 1
            line = f'{uname} teams={sorted(teams) or "-"} -> {want} ({"write" if is_write else "readonly"})'
            if not apply:
                self.stdout.write(f'[dry-run] {line}')
                continue
            env['GITHUB_IDENTITY'] = want
            agent.env = env
            agent.save(update_fields=['env'])
            # Best-effort live push; the daemon reseed replays env regardless.
            try:
                configure_runtime_agent(str(agent.id), {'env': env})
            except Exception as exc:  # noqa: BLE001 — DB env is the durable SoT
                self.stdout.write(self.style.WARNING(f'  {uname}: live push failed ({exc}); reseed will apply'))
            self.stdout.write(self.style.SUCCESS(f'{line}: set'))

        mode = 'APPLY' if apply else 'DRY-RUN'
        self.stdout.write(f'{mode}: changed={changed} unchanged={same} (env GITHUB_IDENTITY pointer)')

    # ── helpers ──────────────────────────────────────────────────────────
    def _secret_has_token(self, core_v1, ns, name):
        from kubernetes.client.exceptions import ApiException
        try:
            sec = core_v1.read_namespaced_secret(name, ns)
        except ApiException as e:
            if e.status == 404:
                return False
            raise
        raw = (sec.data or {}).get('GITHUB_TOKEN')
        if not raw:
            return False
        try:
            return bool(base64.b64decode(raw).decode('utf-8').strip())
        except Exception:
            return False

    def _fetch_pulse_rosters(self):
        from fleet.pulse_auth import pulse_fleet_rosters
        return pulse_fleet_rosters()
