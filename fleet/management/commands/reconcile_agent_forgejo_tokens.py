"""Every fleet agent must hold its own Origin/Forgejo token — mint the missing ones.

WHAT WENT WRONG (2026-09-01 / PLAT-7406)
``ensure_agent_cortex_key`` create-if-missing persists a Secret that contains
ONLY ``CORTEX_API_KEY``. The agent container references ``FORGEJO_TOKEN`` with
``optional=True``, so a missing key is not an error — Kubernetes resolves it to
an empty string. After the 2026-09-01 agent-creds recreation, 61 of 69 Secrets
had no Origin token. Merge/write seats (ryo, haru, kei, ni, ichi, jun, …)
started Ready and could not push, review, or merge. Token-holding seats that
predated the wipe (kumo, riku, …) kept working.

WHY A TOKEN AUTH HEADER CANNOT MINT
Forgejo rejects ``Authorization: token`` on ``POST /users/{username}/tokens``
(``auth method not allowed``). Minting requires HTTP basic auth as an Origin
admin (``origin/forgejo-admin-creds``), matching the documented
``forgejo admin user generate-access-token`` repair path.

``provisioner.ensure_agent_image`` calls ``ensure_agent_forgejo_token`` on the
lifecycle path so new gaps heal on start/wake; this command sweeps seats that
are already Running and would otherwise wait for a roll.

Dry-run by default. ``--apply`` mints and patches. Existing tokens are left
alone (repair, not rotation).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from fleet.models import FleetAgent
from fleet.provisioner import ensure_agent_forgejo_token, get_k8s_client, _live_ns


SKIP_STATUSES = frozenset({
    FleetAgent.Status.DELETED,
    FleetAgent.Status.BLOCKED,
})


def _has_token(core_v1, agent: FleetAgent) -> bool | None:
    """True/False if we could read the Secret, None if we could not."""
    from kubernetes.client.rest import ApiException
    from fleet.provisioner import _forgejo_secret
    try:
        _name, secret = _forgejo_secret(agent, core_v1, _live_ns(agent))
    except ApiException:
        return None
    value = (getattr(secret, 'data', None) or {}).get('FORGEJO_TOKEN') if secret else None
    return bool(value.strip()) if isinstance(value, str) else False


class Command(BaseCommand):
    help = "Mint each fleet agent's own Origin/Forgejo token where it is missing."

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Mint and patch. Without this the command only reports.',
        )
        parser.add_argument(
            '--agent', default='',
            help='Limit to a single agent_username (for targeted repair).',
        )
        parser.add_argument(
            '--rescope-drift', action='store_true',
            help='PLAT-10143: re-mint seats whose minted token scopes have '
                 'drifted from their CURRENT Pulse team grant (stamped at '
                 'mint via the shizuha.io/token-scopes Secret annotation). '
                 'A seat whose grant is UNKNOWN (roster fetch failure) or '
                 'whose Secret predates scope stamping is reported loud, '
                 'never guessed at.',
        )

    def handle(self, *args, **options):
        apply = bool(options['apply'])
        rescope_drift = bool(options['rescope_drift'])
        only = (options['agent'] or '').strip()

        agents = FleetAgent.objects.exclude(status__in=SKIP_STATUSES).order_by('agent_username')
        if only:
            agents = agents.filter(agent_username=only)

        core_v1, _apps, _n = get_k8s_client()

        if rescope_drift:
            self._handle_rescope_drift(core_v1, agents, apply)
            return

        missing: list[str] = []
        unreadable: list[str] = []
        healed: list[str] = []
        failed: list[str] = []

        for agent in agents:
            present = _has_token(core_v1, agent)
            if present is None:
                unreadable.append(agent.agent_username)
                continue
            if present:
                continue
            missing.append(agent.agent_username)
            if not apply:
                continue
            if ensure_agent_forgejo_token(agent):
                healed.append(agent.agent_username)
            else:
                failed.append(agent.agent_username)

        total = agents.count()
        self.stdout.write(f'agents examined: {total}')
        self.stdout.write(
            f'missing FORGEJO_TOKEN: {len(missing)}'
            + (f' -> {", ".join(missing)}' if missing else '')
        )
        if unreadable:
            self.stdout.write(self.style.WARNING(
                f'could not read Origin credentials for {len(unreadable)}: {", ".join(unreadable)} '
                '(status unknown — NOT counted as healthy)'
            ))
        if not apply:
            self.stdout.write(self.style.WARNING('dry run; re-run with --apply to mint'))
            return
        if healed:
            self.stdout.write(self.style.SUCCESS(f'minted: {", ".join(healed)}'))
        if failed:
            self.stdout.write(self.style.ERROR(
                f'MINT FAILED for {len(failed)}: {", ".join(failed)} — these agents '
                'have no Origin identity until repaired'
            ))

    def _handle_rescope_drift(self, core_v1, agents, apply):
        """PLAT-10143: close the scope-drift class at the repair layer.

        The purpose-scoped Secret is externally provisioned and never
        overwritten by Hive — which is exactly why a read-scoped mint could
        survive forever on a writer seat (hiro: 10h andon). Every mint now
        stamps its granted scopes; this sweep re-mints when the CURRENT grant
        diverges from the stamp. Unknown-grant and un-stamped seats are
        reported loud — they are the residue this mode exists to surface, not
        seats to guess about.
        """
        from kubernetes.client.rest import ApiException
        from fleet.provisioner import (
            _forgejo_grant_checked, _forgejo_secret, _forgejo_token_scopes,
            _live_ns,
        )

        drifted: list[str] = []
        provisional: list[str] = []
        unstamped: list[str] = []
        unknown_grant: list[str] = []
        healthy: list[str] = []
        failed: list[str] = []

        for agent in agents:
            teams, grant_known = _forgejo_grant_checked(agent)
            if not grant_known:
                unknown_grant.append(agent.agent_username)
                continue
            expected = _forgejo_token_scopes(agent)
            ns = _live_ns(agent)
            try:
                secret_name, secret = _forgejo_secret(agent, core_v1, ns)
            except ApiException:
                unknown_grant.append(agent.agent_username)
                continue
            if secret is None:
                continue  # not provisioned yet; the missing-token path owns it
            annotations = dict(
                getattr(getattr(secret, 'metadata', None), 'annotations', None) or {}
            )
            stamped = (annotations.get('shizuha.io/token-scopes') or '').strip()
            current = (dict(getattr(secret, 'data', None) or {}).get('FORGEJO_TOKEN') or '').strip()
            if not current:
                continue  # missing-token path owns empty grants
            if not stamped:
                unstamped.append(agent.agent_username)
                continue
            expected_csv = ','.join(expected)
            if stamped == expected_csv and not annotations.get('shizuha.io/scope-provisional'):
                healthy.append(agent.agent_username)
                continue
            if annotations.get('shizuha.io/scope-provisional') and stamped == expected_csv:
                healthy.append(agent.agent_username)
                continue
            drifted.append(agent.agent_username)
            if not apply:
                continue
            if ensure_agent_forgejo_token(agent, rescope_to=expected):
                provisional.append(agent.agent_username)
            else:
                failed.append(agent.agent_username)

        self.stdout.write(f'agents examined (rescope-drift): {len(drifted) + len(healthy) + len(unstamped) + len(unknown_grant) + len(provisional) + len(failed)}')
        self.stdout.write(
            f'scope drift (re-mint due): {len(drifted)}'
            + (f' -> {", ".join(drifted)}' if drifted else '')
        )
        if unknown_grant:
            self.stdout.write(self.style.WARNING(
                f'grant UNKNOWN (roster unavailable or credential unreadable) for '
                f'{len(unknown_grant)}: {", ".join(unknown_grant)} — NOT acted on; '
                're-run after the roster resolves'
            ))
        if unstamped:
            self.stdout.write(self.style.WARNING(
                f'Secret predates scope stamping for {len(unstamped)}: '
                f'{", ".join(unstamped)} — actual token scopes cannot be inferred; '
                'these need a one-time external re-mint (hiro-class residue)'
            ))
        if not apply:
            self.stdout.write(self.style.WARNING('dry run; re-run with --apply --rescope-drift to re-mint'))
            return
        if provisional:
            self.stdout.write(self.style.SUCCESS(f're-minted at current grant: {", ".join(provisional)}'))
        if failed:
            self.stdout.write(self.style.ERROR(
                f'RE-SCOPE FAILED for {len(failed)}: {", ".join(failed)} — '
                'drifted grants remain; re-run after repair'
            ))
