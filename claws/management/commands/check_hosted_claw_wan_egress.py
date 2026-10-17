from django.core.management.base import BaseCommand, CommandError

from security.hosted_claw_wan_egress_probe import WanEgressProbeConfig, get_k8s_core_api, run_probe


class Command(BaseCommand):
    help = "Run the HIVE-450 hosted-claw WAN-egress live regression smoke"

    def add_arguments(self, parser):
        parser.add_argument('--namespace', help='internal hosted-claw namespace to probe')
        parser.add_argument('--pod', help='hosted-claw pod to exec')
        parser.add_argument('--namespace-selector', help='selector for the internal smoke namespace')
        parser.add_argument('--pod-selector', help='selector for the claw pod in the namespace')
        parser.add_argument('--public-url', help='public HTTPS URL that must be reachable')
        parser.add_argument('--blocked-target', action='append', help='name=ip:port target that must NOT connect')

    def handle(self, *args, **options):
        cfg = WanEgressProbeConfig.from_env()
        cfg = WanEgressProbeConfig(
            namespace=options.get('namespace') or cfg.namespace,
            pod=options.get('pod') or cfg.pod,
            namespace_label_selector=options.get('namespace_selector') or cfg.namespace_label_selector,
            pod_label_selector=options.get('pod_selector') or cfg.pod_label_selector,
            public_url=options.get('public_url') or cfg.public_url,
            public_statuses=cfg.public_statuses,
            blocked_targets=tuple(options.get('blocked_target') or cfg.blocked_targets),
            timeout_ms=cfg.timeout_ms,
        )
        try:
            ok, payload = run_probe(get_k8s_core_api(), cfg)
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(str(payload))
        if not ok:
            raise CommandError('hosted-claw WAN-egress regression smoke failed')
