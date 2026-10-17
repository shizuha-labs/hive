"""
Management command: backfill_behavioral_fields

Reads agents.json (the legacy runtime-fleet config) and populates the HIVE-133
behavioral fields on each FleetAgent row where they are currently blank.

Usage:
    python manage.py backfill_behavioral_fields [--dry-run] [--agents-json PATH]

The command is IDEMPOTENT — re-running it on an already-populated row is a no-op
because it only writes fields that are currently blank/empty.
"""
import json
import logging
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from fleet.models import FleetAgent
from fleet.serializers import _ENV_DENYLIST

logger = logging.getLogger(__name__)

AGENTS_JSON_DEFAULT = Path('/home/agent/.shizuha/agents.json')


class Command(BaseCommand):
    help = 'Backfill HIVE-133 behavioral fields on FleetAgent rows from agents.json.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--agents-json',
            default=str(AGENTS_JSON_DEFAULT),
            help=f'Path to agents.json (default: {AGENTS_JSON_DEFAULT})',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Print what would be updated without writing to the database.',
        )

    def handle(self, *args, **options):
        agents_json_path = Path(options['agents_json'])
        dry_run = options['dry_run']

        if not agents_json_path.exists():
            raise CommandError(f'agents.json not found at {agents_json_path}')

        with agents_json_path.open() as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as exc:
                raise CommandError(f'Failed to parse agents.json: {exc}') from exc

        agents_list = data if isinstance(data, list) else data.get('agents', [])
        if not agents_list:
            self.stdout.write(self.style.WARNING('No agents found in agents.json — nothing to backfill.'))
            return

        updated = 0
        skipped = 0

        for entry in agents_list:
            username = entry.get('username') or entry.get('agentUsername')
            if not username:
                logger.debug('Skipping entry with no username: %s', entry)
                continue

            try:
                agent = FleetAgent.objects.get(agent_username=username)
            except FleetAgent.DoesNotExist:
                logger.debug('No FleetAgent row for username=%s — skipping', username)
                skipped += 1
                continue

            fields_to_update = []

            def _set_if_blank(field_name, value):
                if value and not getattr(agent, field_name):
                    setattr(agent, field_name, value)
                    fields_to_update.append(field_name)

            def _set_json_if_empty(field_name, value):
                current = getattr(agent, field_name)
                if value and not current:
                    setattr(agent, field_name, value)
                    fields_to_update.append(field_name)

            _set_if_blank('context_prompt', entry.get('contextPrompt', ''))
            _set_json_if_empty('personality_traits', entry.get('personalityTraits') or {})
            _set_json_if_empty('model_fallbacks', entry.get('modelFallbacks') or [])
            _set_json_if_empty('model_overrides', entry.get('modelOverrides') or {})
            _set_if_blank('reasoning_effort', entry.get('reasoningEffort') or entry.get('thinkingLevel', ''))
            raw_env = entry.get('env') or {}
            safe_env = {k: v for k, v in raw_env.items() if k not in _ENV_DENYLIST}
            if raw_env and len(safe_env) < len(raw_env):
                dropped = sorted(set(raw_env) - set(safe_env))
                logger.warning(
                    'backfill: %s env contains reserved key(s) %s — dropping from backfill',
                    username, dropped,
                )
            _set_json_if_empty('env', safe_env)
            _set_if_blank('failover_chain_id', entry.get('failoverChainId', ''))

            if fields_to_update:
                if dry_run:
                    self.stdout.write(
                        f'[DRY-RUN] {username}: would update {", ".join(fields_to_update)}'
                    )
                else:
                    agent.save(update_fields=fields_to_update)
                    self.stdout.write(f'{username}: updated {", ".join(fields_to_update)}')
                updated += 1
            else:
                logger.debug('%s: no blank fields to fill', username)

        verb = 'Would update' if dry_run else 'Updated'
        self.stdout.write(
            self.style.SUCCESS(
                f'{verb} {updated} agent(s). Skipped {skipped} (not in DB).'
            )
        )
