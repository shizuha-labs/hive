"""Operator-only per-seat model-lease pin (CTX-1055). See fleet.lease_operator_pin.

    python manage.py lease_pin set sora --minutes 30 --reason "..." --actor hritik
    python manage.py lease_pin show
    python manage.py lease_pin clear --actor hritik --reason "review done"
"""
from __future__ import annotations

import json
import time

from django.core.management.base import BaseCommand, CommandError

from fleet.lease_operator_pin import (
    OPERATOR_PIN_MAX_TTL_S,
    active_operator_pin,
    audit_log,
    clear_operator_pin,
    set_operator_pin,
)


class Command(BaseCommand):
    help = 'Set, show or clear the single time-boxed operator lease pin (audited).'

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest='action', required=True)
        s = sub.add_parser('set')
        s.add_argument('username')
        s.add_argument('--minutes', type=int, required=True,
                       help=f'TTL in minutes (max {OPERATOR_PIN_MAX_TTL_S // 60}).')
        s.add_argument('--reason', required=True)
        s.add_argument('--actor', required=True)
        sub.add_parser('show')
        c = sub.add_parser('clear')
        c.add_argument('--actor', required=True)
        c.add_argument('--reason', default='')
        a = sub.add_parser('audit')
        a.add_argument('--limit', type=int, default=20)

    def handle(self, *args, **opts):
        action = opts['action']
        if action == 'set':
            try:
                pin = set_operator_pin(
                    opts['username'], ttl_seconds=int(opts['minutes']) * 60,
                    reason=opts['reason'], actor=opts['actor'],
                )
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write('PIN SET ' + json.dumps(pin, sort_keys=True))
        elif action == 'show':
            pin = active_operator_pin()
            if pin:
                pin = {**pin, 'remaining_s': int(pin['expires_at'] - time.time())}
            self.stdout.write('PIN ' + json.dumps(pin, sort_keys=True))
        elif action == 'clear':
            pin = clear_operator_pin(actor=opts['actor'], reason=opts['reason'])
            self.stdout.write('PIN CLEARED ' + json.dumps(pin, sort_keys=True))
        elif action == 'audit':
            for entry in audit_log()[-max(1, int(opts['limit'])):]:
                self.stdout.write('AUDIT ' + json.dumps(entry, sort_keys=True))
