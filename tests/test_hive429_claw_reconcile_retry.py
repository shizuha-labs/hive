"""Guards for the production claw reconciliation retry contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".forgejo/workflows/deploy.yml").read_text()


def _claw_reconcile_block() -> str:
    return WORKFLOW.split(
        'echo "Installing scheduled claw-isolation reconciliation CronJob"',
        1,
    )[1].split(
        'echo "Installing harness auto-upgrade CronJob"',
        1,
    )[0]


def test_all_claw_reconcilers_run_and_any_failure_reaches_the_job_controller():
    block = _claw_reconcile_block()
    assert '"status=0; "' in block
    for command in (
        "finalize_claw_deletions",
        "reconcile_claw_availability",
        "reconcile_claw_isolation",
    ):
        assert f"python manage.py {command} || status=$?" in block
    assert 'exit \\"$status\\"' in block


def test_transient_dependency_failure_retries_before_next_cron_tick():
    block = _claw_reconcile_block()
    assert '"backoffLimit": 6' in block
    assert '"activeDeadlineSeconds": 1200' in block
