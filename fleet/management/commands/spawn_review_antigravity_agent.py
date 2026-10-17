"""Management command to configure/spawn an Antigravity agent in the Review team."""

from django.core.management.base import BaseCommand
from fleet.models import FleetAgent, TeamMembershipRole


class Command(BaseCommand):
    help = 'Spawn/configure Antigravity agent in the Review team with claude-opus-4.6 thinking'

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            default='reika',
            help='Username for the review agent (default: reika)',
        )
        parser.add_argument(
            '--model',
            default='claude-opus-4.6',
            help='LLM model (default: claude-opus-4.6)',
        )
        parser.add_argument(
            '--reasoning-effort',
            default='high',
            help='Reasoning effort (default: high)',
        )

    def handle(self, *args, **options):
        username = options['username']
        model = options['model']
        effort = options['reasoning_effort']

        agent, created = FleetAgent.objects.get_or_create(
            agent_username=username,
            defaults={
                'display_name': 'Reika',
                'role': 'Junior Code Reviewer',
                'model': model,
                'reasoning_effort': effort,
                'mcp_config': {'execution_method': 'antigravity_server'},
                'status': FleetAgent.Status.RUNNING,
            },
        )
        if not created:
            agent.model = model
            agent.reasoning_effort = effort
            mcp = dict(agent.mcp_config or {})
            mcp['execution_method'] = 'antigravity_server'
            agent.mcp_config = mcp
            agent.save()

        # Wire into Review team membership
        email = f'{username}@shizuha.com'
        TeamMembershipRole.objects.update_or_create(
            agent_identifier=email,
            team_slug='review',
            defaults={
                'role_title': agent.role or 'Code Reviewer',
                'membership_role': TeamMembershipRole.MembershipRole.REVIEWER,
                'enabled': True,
                'organization_slug': 'shizuha-global',
            },
        )

        action_str = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f"{action_str} agent @{username} ({agent.display_name}) with "
            f"Antigravity runtime (antigravity_server) + {model} (effort: {effort}) in Review team."
        ))
