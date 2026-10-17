"""Park one isolated hibernated default agent for each eligible ID human."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from fleet.models import FleetAgent
from fleet.personal_agents import (
    PersonalShizuhaProvisionError,
    eligible_for_default_personal_agent,
    ensure_personal_shizuha_agent,
    fetch_id_owner_record,
    get_personal_agent,
    iter_id_human_directory,
    owner_record,
)


class Command(BaseCommand):
    help = 'Ensure each eligible human has an isolated hibernated default agent.'

    def add_arguments(self, parser):
        parser.add_argument('--owner-id', type=int, default=0,
                            help='Only this owner (0 = every eligible human).')
        parser.add_argument('--username', default='',
                            help='Optional username when --owner-id is set.')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        owner_id = int(options['owner_id'] or 0)
        dry = bool(options['dry_run'])
        username_override = str(options.get('username') or '').strip()
        created = 0
        skipped = 0
        failed = 0
        for user in self._iter_owners(owner_id, username_override):
            existing = get_personal_agent(user.id)
            if existing is None and not eligible_for_default_personal_agent(user):
                skipped += 1
                continue
            if dry:
                action = 'align' if existing is not None else 'create'
                self.stdout.write(
                    f'dry-run would {action} shizuha-{user.id} for {user.username}')
                created += 1
                continue
            try:
                result = ensure_personal_shizuha_agent(
                    owner_id=int(user.id),
                    owner_username=str(user.username or ''),
                )
                if result.created:
                    created += 1
                    self.stdout.write(
                        f'created {result.agent.agent_username} '
                        f'status={result.agent.status} isolated={result.agent.isolated}'
                    )
                else:
                    skipped += 1
            except PersonalShizuhaProvisionError as exc:
                failed += 1
                self.stderr.write(f'refused {user.id} {user.username}: {exc}')
            except Exception as exc:
                failed += 1
                self.stderr.write(f'failed {user.id} {user.username}: {exc}')
        self.stdout.write(
            f'done created={created} skipped={skipped} failed={failed} '
            f'fleet={FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED).count()}'
        )

    def _iter_owners(self, owner_id, username_override):
        if owner_id:
            record = fetch_id_owner_record(owner_id)
            if username_override:
                record = owner_record(
                    owner_id=owner_id,
                    username=username_override,
                    email=getattr(record, 'email', ''),
                    is_staff=getattr(record, 'is_staff', False),
                    is_superuser=getattr(record, 'is_superuser', False),
                    is_agent=getattr(record, 'is_agent', False),
                    account_type=getattr(getattr(record, 'profile', None), 'account_type', 'human'),
                )
            if record is None:
                return []
            return [record]
        try:
            humans = list(iter_id_human_directory())
        except Exception as exc:
            self.stderr.write(f'ID human directory unavailable ({exc}); falling back to Hive users')
            humans = []
        if humans:
            return humans
        User = get_user_model()
        return [
            owner_record(
                owner_id=user.id,
                username=user.username or '',
                email=getattr(user, 'email', '') or '',
                is_staff=bool(getattr(user, 'is_staff', False)),
                is_superuser=bool(getattr(user, 'is_superuser', False)),
                is_agent=bool(getattr(user, 'is_agent', False)),
                account_type=getattr(getattr(user, 'profile', None), 'account_type', 'human'),
            )
            for user in User.objects.filter(is_active=True).iterator()
        ]
