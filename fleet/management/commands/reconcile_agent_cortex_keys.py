"""Every fleet agent must hold its own Cortex API key — mint the missing ones.

WHAT WENT WRONG (2026-08-05)
The agent container references its Cortex credential as
``secretKeyRef {<user>-agent-creds, key: CORTEX_API_KEY, optional: true}``.
``optional: true`` means a MISSING key is not an error — it resolves to an empty
string. The agent starts, passes its readiness probe, registers, heartbeats on
schedule, and reports Running/Alive, while every single model call returns::

    vLLM error 401: {"detail":"Authentication credentials were not provided."}

rei was provisioned 2026-06-27, before per-agent keys existed, and had not
completed one turn in the five weeks since. The only signal anywhere was a
detail-less "Runtime unhealthy" chip on the Agents page. A sweep found 10 of 51
``*-agent-creds`` Secrets with no ``CORTEX_API_KEY`` at all.

WHY IT IS SAFE TO PATCH THE SECRET HERE
``reconcile_agent_github_identity`` records that patching ``<user>-agent-creds``
gets reverted, because the daemon's k8s render OWNS ``GITHUB_TOKEN`` — it emits
that key in the Secret manifest, so every apply overwrites it. ``CORTEX_API_KEY``
is deliberately NOT in that manifest (``cli/src/daemon/k8s-backend.ts``, the
``stringData`` block carries only AGENT_PASSWORD/GITHUB_TOKEN/CONTEXT_PROMPT), so
apply's three-way merge preserves an out-of-band key. That is why nova's key,
minted 2026-07-04, is still there. The invariant is load-bearing and is pinned by
a guard test in the CLI repo: if anyone adds CORTEX_API_KEY to the daemon's
Secret render, it starts clobbering minted keys on every reconcile.

Operator 2026-08-05: "hive manages its agents underneath .. and each agent uses
their own key .. so you shouldn't have to do it .. let hive/fleet daemon/agent
handle this". Hence a reconciler, not a runbook. ``provisioner.ensure_agent_image``
calls the same ``ensure_agent_cortex_key`` on the reconcile path so new gaps heal
on their own; this command exists to sweep the backlog and to answer "does every
agent actually have one?" on demand.

Dry-run by default. ``--apply`` mints and patches.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from fleet.models import FleetAgent
from fleet.provisioner import ensure_agent_cortex_key, get_k8s_client, _live_ns


# Agents in these states have no pod and no reason to hold a credential.
SKIP_STATUSES = frozenset({
    FleetAgent.Status.DELETED,
    FleetAgent.Status.BLOCKED,
})


def _has_key(core_v1, agent: FleetAgent) -> bool | None:
    """True/False if we could read the Secret, None if we could not."""
    from kubernetes.client.rest import ApiException
    try:
        secret = core_v1.read_namespaced_secret(
            f'{agent.agent_username}-agent-creds', _live_ns(agent),
        )
    except ApiException as exc:
        return False if getattr(exc, 'status', None) == 404 else None
    return bool((dict(getattr(secret, 'data', None) or {}).get('CORTEX_API_KEY') or '').strip())


class Command(BaseCommand):
    help = "Mint each fleet agent's own Cortex API key where it is missing."

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Mint and patch. Without this the command only reports.',
        )
        parser.add_argument(
            '--agent', default='',
            help='Limit to a single agent_username (for targeted repair).',
        )

    def handle(self, *args, **options):
        apply = bool(options['apply'])
        only = (options['agent'] or '').strip()

        agents = FleetAgent.objects.exclude(status__in=SKIP_STATUSES).order_by('agent_username')
        if only:
            agents = agents.filter(agent_username=only)

        core_v1, _apps, _n = get_k8s_client()
        missing: list[str] = []
        unreadable: list[str] = []
        healed: list[str] = []
        failed: list[str] = []

        for agent in agents:
            present = _has_key(core_v1, agent)
            if present is None:
                # Never report an unreadable Secret as "has a key" — that is the
                # exact flattering-reading failure this whole class came from.
                unreadable.append(agent.agent_username)
                continue
            if present:
                continue
            missing.append(agent.agent_username)
            if not apply:
                continue
            if ensure_agent_cortex_key(agent):
                healed.append(agent.agent_username)
            else:
                failed.append(agent.agent_username)

        total = agents.count()
        self.stdout.write(f'agents examined: {total}')
        self.stdout.write(f'missing CORTEX_API_KEY: {len(missing)}' + (f' -> {", ".join(missing)}' if missing else ''))
        if unreadable:
            self.stdout.write(self.style.WARNING(
                f'could not read agent-creds for {len(unreadable)}: {", ".join(unreadable)} '
                '(status unknown — NOT counted as healthy)'
            ))
        if not apply:
            self.stdout.write(self.style.WARNING('dry run; re-run with --apply to mint'))
            return
        if healed:
            self.stdout.write(self.style.SUCCESS(f'minted: {", ".join(healed)}'))
        if failed:
            # Loud on purpose: with the mandatory secretKeyRef, an agent left
            # without a key cannot start, which is the intended outcome — but it
            # must never be a silent one.
            self.stdout.write(self.style.ERROR(
                f'MINT FAILED for {len(failed)}: {", ".join(failed)} — these agents '
                'cannot authenticate to Cortex'
            ))
