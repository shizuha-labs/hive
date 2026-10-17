from django.apps import AppConfig


class FleetConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'fleet'
    verbose_name = 'Hive Fleet (k3s-native agent runtime)'
