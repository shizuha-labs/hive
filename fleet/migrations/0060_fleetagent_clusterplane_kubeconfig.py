from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0059_remap_retired_metal_to_glm'),
    ]

    operations = [
        migrations.AddField(
            model_name='fleetagent',
            name='clusterplane_kubeconfig',
            field=models.BooleanField(
                default=False,
                help_text='Cluster-plane read seat: projects the SA token (kubelet-rotated) and init-materializes ~/.kube/config referencing the tokenFile. Fail-closed default; RBAC is a separate declarative grant.',
            ),
        ),
    ]
