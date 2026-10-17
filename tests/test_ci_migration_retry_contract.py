"""The fleet release must survive a bounded platform-db failover."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".forgejo/workflows/deploy.yml").read_text()


def _fleet_migration_block() -> str:
    return WORKFLOW.split(
        'MIGRATE_JOB="ci-migrate-hive-${SHORT}"',
        1,
    )[1].split(
        "kubectl -n shizuha-hive get deploy hive -o json "
        "> /tmp/hive-deploy-pre-rollout.json",
        1,
    )[0]


def test_fleet_migration_retries_during_bounded_database_failover():
    block = _fleet_migration_block()
    runner = (ROOT / ".forgejo/scripts/run-migrations-with-retry.sh").read_text()
    assert 'PYTHON="${HIVE_MIGRATION_PYTHON:-/app/.venv/bin/python}"' in runner
    assert '"$PYTHON" -c \'import django\'' in runner
    assert '"$PYTHON" manage.py migrate --noinput' in runner
    assert '"backoffLimit": 6' in block
    assert '"activeDeadlineSeconds": 900' in block
    assert '"job/${MIGRATE_JOB}" --timeout=900s' in block
    assert "Django migrations failed; refusing to roll Hive backend" in block


def test_fleet_migration_uses_venv_and_fails_fast_on_deterministic_errors():
    block = _fleet_migration_block()
    assert '"/app/.forgejo/scripts/run-migrations-with-retry.sh"' in block
    assert 'container.pop("args", None)' in block
    assert '"podFailurePolicy": {' in block
    assert '"action": "FailJob"' in block
    assert '"values": [42]' in block
