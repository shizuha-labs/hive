from django.apps import AppConfig


class HivePlatformConfig(AppConfig):
    """ADR-0080 (HIVE-2018): non-Claw home for shared platform infra.

    Hosts the token coordinator, billing, and metering components that
    HIVE-2010's disposition list confirmed must leave `claws/` before any
    `rm -rf hive/claws/`. Models are moved here with `db_table` preserved so
    existing rows (token leases, usage events, subscriptions) survive intact.

    Named `hive_platform` (not `platform`) to avoid shadowing the Python
    standard-library `platform` module (breaks `uuid`/`os` imports).
    """

    name = 'hive_platform'
    verbose_name = 'Hive Platform (shared infra)'
