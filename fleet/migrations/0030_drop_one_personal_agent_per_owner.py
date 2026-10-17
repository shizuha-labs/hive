from django.db import migrations, models


class Migration(migrations.Migration):
    """PLAT-4235 (operator 2026-07-12, "users OWN fleets").

    Drop the single-personal-agent-per-owner unique constraint (added in 0027 for
    a superseded model) so a user's per-user daemon can own/manage their whole
    FLEET of agents. The canonical personal Shizuha is now keyed by its
    deterministic username `shizuha-<owner_id>` (fleet.personal_agents), not by
    owner_id uniqueness. owner_id keeps a plain index for owner-scoped queries.
    """

    dependencies = [
        ('fleet', '0029_daemonregistry_owner_subject'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='fleetagent',
            name='fleet_one_personal_agent_per_owner',
        ),
        migrations.AddIndex(
            model_name='fleetagent',
            index=models.Index(fields=['owner_id'], name='fleet_agent_owner_idx'),
        ),
    ]
