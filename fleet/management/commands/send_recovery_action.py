"""Dispatch trigger for HIVE-332 recovery actions (PLAT-7631).

HIVE-332's recovery-action pipe was complete end-to-end but had no dispatch
trigger: ``fleet.recovery.create_recovery_action`` + ``send_recovery_action``
had zero callers, so no frame could ever reach a daemon and the PLAT-5467
acceptance ("authenticated ``recovery_result`` for every accepted
``recovery_action``") was unexercisable live. This command is the faucet.

Safety contract (v1, PLAT-5467/HIVE-332 — enforced by
``AgentRecoveryAction.clean()`` via ``save() → full_clean()``):

- mode defaults to ``observe`` (the model's ``dry_run``): nothing is applied,
  the daemon evaluates and reports ``would_apply``.
- ``apply`` mode is denied unless the default-off P4 policy
  (``HIVE_RECOVERY_AUTO_APPLY_ENABLED`` + per-action allowlist) allows it.
- GATED action types (cross_org_identity_change, delete_user, grant_*,
  restart_fleet, expand_privileges, …) are rejected at creation — hard
  code-level gate, defense in depth with the daemon's independent deny.
- Unknown action types are default-denied.
- Secret material in ``--params`` is rejected (never stored, never echoed).
- ``expires_at`` is honored: ``now + --ttl-seconds`` (default
  ``DEFAULT_ACTION_TTL``).

Audit: the created action's id + correlation_id are printed for the operator
trail; ``--reason`` is recorded in ``before_summary['reason']`` (append-only
audit JSON) and echoed in the output.
"""
from __future__ import annotations

import json
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from fleet.models import AgentRecoveryAction, DaemonRegistry, FleetAgent
from fleet.recovery import (
    DEFAULT_ACTION_TTL,
    create_recovery_action,
    send_recovery_action,
)

# The task contract names the safe mode "observe"; the model calls it
# "dry_run". Accept both spellings, store the model's value.
_MODE_ALIASES = {
    'observe': AgentRecoveryAction.Mode.DRY_RUN,
    'dry_run': AgentRecoveryAction.Mode.DRY_RUN,
    'dry-run': AgentRecoveryAction.Mode.DRY_RUN,
    'apply': AgentRecoveryAction.Mode.APPLY,
}


class Command(BaseCommand):
    help = (
        'Create and dispatch a HIVE-332 recovery action to a daemon over its '
        'daemon-link channel. Observe (dry_run) mode by default; apply mode '
        'and gated/unknown action types are denied by the v1 safety contract.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--agent', required=True,
            help='Target agent_username (FleetAgent.agent_username).',
        )
        parser.add_argument(
            '--action-type', required=True,
            help='Recovery action type (SAFE set only; gated/unknown are denied).',
        )
        parser.add_argument(
            '--daemon', default='',
            help='Target daemon_id or owner_username. Defaults to the agent\'s '
                 'managing_daemon; required if the agent has none.',
        )
        parser.add_argument(
            '--mode', default='observe',
            help='observe (default, alias of dry_run) or apply. Apply is '
                 'denied unless the P4 auto-apply policy allows it.',
        )
        parser.add_argument(
            '--params', default='{}',
            help='Action parameters as a JSON object (default {}). Secret '
                 'material is rejected.',
        )
        parser.add_argument(
            '--reason', default='',
            help='Audit reason; stored in before_summary["reason"].',
        )
        parser.add_argument(
            '--ttl-seconds', type=int, default=None,
            help='Expiry window in seconds (default: DEFAULT_ACTION_TTL).',
        )
        parser.add_argument(
            '--actor', default='',
            help='Actor stamp for the audit trail (defaults to the invoking user).',
        )
        parser.add_argument(
            '--no-send', action='store_true',
            help='Create the action but do not dispatch it (stays PENDING).',
        )

    def _resolve_mode(self, raw: str) -> str:
        mode = _MODE_ALIASES.get((raw or '').strip().lower())
        if mode is None:
            raise CommandError(
                f'unknown mode {raw!r} — use observe (default) or apply'
            )
        return mode

    def _resolve_params(self, raw: str) -> dict:
        try:
            params = json.loads(raw or '{}')
        except json.JSONDecodeError as exc:
            raise CommandError(f'--params is not valid JSON: {exc}') from exc
        if not isinstance(params, dict):
            raise CommandError('--params must be a JSON object')
        return params

    def _resolve_agent(self, username: str) -> FleetAgent:
        agent = FleetAgent.objects.filter(
            agent_username=(username or '').strip(),
        ).first()
        if agent is None:
            raise CommandError(f'no FleetAgent with agent_username {username!r}')
        return agent

    def _resolve_daemon(self, agent: FleetAgent, raw: str) -> DaemonRegistry | None:
        ref = (raw or '').strip()
        if ref:
            daemon = DaemonRegistry.objects.filter(daemon_id=ref).first()
            if daemon is None:
                daemon = (
                    DaemonRegistry.objects.filter(owner_username=ref)
                    .order_by('-last_seen')
                    .first()
                )
            if daemon is None:
                raise CommandError(
                    f'no DaemonRegistry matches {ref!r} by daemon_id or owner_username'
                )
            return daemon
        return agent.managing_daemon

    def handle(self, *args, **options):
        agent = self._resolve_agent(options['agent'])
        mode = self._resolve_mode(options['mode'])
        params = self._resolve_params(options['params'])
        daemon = self._resolve_daemon(agent, options['daemon'])

        if daemon is None and not options['no_send']:
            raise CommandError(
                f'agent {agent.agent_username!r} has no managing_daemon and no '
                '--daemon was given — dispatch target unresolved'
            )

        ttl = (
            timedelta(seconds=options['ttl_seconds'])
            if options['ttl_seconds'] is not None
            else DEFAULT_ACTION_TTL
        )
        reason = (options['reason'] or '').strip()

        try:
            action = create_recovery_action(
                agent,
                options['action_type'],
                mode=mode,
                params=params,
                daemon=daemon,
                ttl=ttl,
                actor=(options['actor'] or '').strip(),
                source='hive_management_command',
            )
        except ValidationError as exc:
            raise CommandError(self._format_validation_error(exc)) from exc

        if reason:
            action.before_summary = {'reason': reason}
            action.save(update_fields=['before_summary'])

        if options['no_send']:
            self.stdout.write(self.style.WARNING(
                '--no-send: action created but not dispatched (status '
                f'{action.status})'
            ))
        else:
            try:
                frame = async_to_sync(send_recovery_action)(action, daemon)
            except Exception as exc:
                raise CommandError(
                    f'action {action.id} created (correlation_id '
                    f'{action.correlation_id}) but dispatch failed: {exc}'
                ) from exc
            self.stdout.write(f'dispatched frame to daemon-link-{daemon.daemon_id}')

        self.stdout.write(self.style.SUCCESS(
            f'action_id={action.id} correlation_id={action.correlation_id} '
            f'status={action.status} mode={action.mode} '
            f'action_type={action.action_type} '
            f'agent={agent.agent_username} '
            f'daemon={daemon.daemon_id if daemon else "-"} '
            f'expires_at={action.expires_at.isoformat() if action.expires_at else "-"}'
        ))
        if reason:
            self.stdout.write(f'reason: {reason}')

    @staticmethod
    def _format_validation_error(exc) -> str:
        if hasattr(exc, 'message_dict'):
            parts = [
                f'{field}: {"; ".join(messages)}'
                for field, messages in exc.message_dict.items()
            ]
            return 'recovery action rejected by the v1 safety contract — ' + ' | '.join(parts)
        return f'recovery action rejected by the v1 safety contract — {exc}'
