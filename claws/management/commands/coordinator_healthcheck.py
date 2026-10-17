"""HIVE-124: Token Coordinator health-check loop.

Periodically probes every active token with a cheap ~8-token provider ping and
auto-cools/kills dead tokens BEFORE any agent picks them — this is what prevents
the baked-expired-token incident (a token whose auth has died is marked dead so
``pick_token`` never hands it out).

Run once (e.g. from a CronJob):
    manage.py coordinator_healthcheck

Run as a long-lived loop (e.g. a sidecar/Deployment):
    manage.py coordinator_healthcheck --loop --interval 300
"""
import time

from django.core.management.base import BaseCommand

from hive_platform.token_coordinator import run_health_check


class Command(BaseCommand):
    help = ('Probe active Coordinator tokens, cool/kill dead ones (HIVE-124), and '
            'auto-recover auth-killed tokens whose backoff lapsed (PLAT-881), and clear stale cooldowns (PLAT-924).')

    def add_arguments(self, parser):
        parser.add_argument('--provider', default=None, help='Limit to one provider (anthropic/openai).')
        parser.add_argument('--loop', action='store_true', help='Run forever on an interval instead of once.')
        parser.add_argument('--interval', type=int, default=300, help='Loop interval seconds (default 300).')

    def handle(self, *args, **opts):
        provider = opts['provider']
        if not opts['loop']:
            self._run_once(provider)
            return
        interval = max(opts['interval'], 5)
        self.stdout.write(f'coordinator_healthcheck loop every {interval}s (provider={provider or "all"})')
        while True:
            self._run_once(provider)
            time.sleep(interval)

    def _run_once(self, provider):
        summary = run_health_check(provider=provider)
        self.stdout.write(
            'health-check: checked={checked} ok={ok} rate_limited={rate_limited} '
            'dead={dead} errored={errored} recovered={recovered} '
            'still_dead={still_dead} cooldown_cleared={cooldown_cleared}'.format(**summary)
        )
