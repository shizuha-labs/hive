"""HIVE-583: subscription OAuth token refresh loop (HA background worker).

Periodically refreshes active OpenAI, xAI, and Anthropic credentials ahead of
access expiry. Uses a PostgreSQL session advisory lock for leader election so
multiple Hive pods can run without duplicating work.

Run once (e.g. from a CronJob):
    manage.py codex_refresh_loop

Run as a long-lived loop (e.g. a sidecar/Deployment):
    manage.py codex_refresh_loop --loop --interval 300
"""
import time

from django.core.management.base import BaseCommand

from hive_platform.token_coordinator import run_codex_refresh_loop


class Command(BaseCommand):
    help = 'Refresh expiring subscription OAuth credentials before expiry (HIVE-583).'

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true', help='Run forever on an interval instead of once.')
        parser.add_argument('--interval', type=int, default=300, help='Loop interval seconds (default 300).')
        parser.add_argument('--dry-run', action='store_true', help='Check which tokens need refresh without actually refreshing.')
        parser.add_argument(
            '--skip-quota-refresh',
            action='store_true',
            help='Only refresh OAuth access tokens; do not update subscription quota snapshots.',
        )
        parser.add_argument(
            '--quota-stale-after',
            type=int,
            default=1800,
            help='Refresh subscription quota snapshots older than this many seconds (default 1800).',
        )

    def handle(self, *args, **opts):
        interval = max(opts['interval'], 5)
        dry_run = opts['dry_run']

        if not opts['loop']:
            self._run_once(dry_run, not opts['skip_quota_refresh'], opts['quota_stale_after'])
            return

        refresh_quotas = not opts['skip_quota_refresh']
        quota_stale_after = opts['quota_stale_after']
        self.stdout.write(
            f'codex_refresh_loop every {interval}s '
            f'(dry_run={dry_run}, refresh_quotas={refresh_quotas}, '
            f'quota_stale_after={quota_stale_after})'
        )
        while True:
            self._run_once(dry_run, refresh_quotas, quota_stale_after)
            time.sleep(interval)

    def _run_once(self, dry_run, refresh_quotas, quota_stale_after):
        summary = run_codex_refresh_loop(
            dry_run=dry_run,
            refresh_quotas=refresh_quotas,
            quota_stale_after_seconds=quota_stale_after,
        )
        if not summary['leader_held']:
            return  # another pod is the leader — silent
        self.stdout.write(
            'codex-refresh: leader_held=True checked={checked} '
            'refreshed={refreshed} skipped_fresh={skipped_fresh} '
            'failed={failed} errors={errors} '
            'quota_checked={quota_checked} quota_updated={quota_updated} '
            'quota_skipped_fresh={quota_skipped_fresh} quota_failed={quota_failed} '
            'xai_cooldowns_checked={xai_cooldowns_checked} '
            'xai_cooldowns_reasserted={xai_cooldowns_reasserted} '
            'xai_refresh_checked={xai_refresh_checked} '
            'xai_refreshed={xai_refreshed} '
            'xai_refresh_failed={xai_refresh_failed}'.format(**summary)
        )
