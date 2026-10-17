from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0045_fleetagent_team_credential_eligible'),
    ]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='hostplane_ssh',
            field=models.BooleanField(default=False, help_text='Designated host-plane SSH seat (ichi/ni class): adds the shizuha.io/agent-role=devops-ssh pod label selected by the tailnet /32 TCP-22 egress policy. Reach only — Tailscale ACL/SSH-cert auth is a separate gate.'),
        ),
    ]
