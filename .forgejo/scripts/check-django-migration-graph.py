#!/usr/bin/env python3
"""Fail CI when Django apps have multiple migration graph leaves.

Run after dependencies are installed, from the repository root:

    python manage.py makemigrations --check --dry-run
    python .forgejo/scripts/check-django-migration-graph.py

The first command catches model changes without migrations. This script catches a
second deploy-breaking class: a PR branch adding a migration from an older base
after main has already landed a newer leaf or merge migration. Git can merge that
shape cleanly, but Django aborts at graph load until the branch is rebased and
renumbered or a merge migration is added.
Rollout (ORIG-21): invoked from .forgejo/workflows/deploy.yml in hive, id, cortex, connect, and books.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ensure_settings_module(root: Path) -> None:
    if os.environ.get("DJANGO_SETTINGS_MODULE"):
        return
    manage_py = root / "manage.py"
    text = manage_py.read_text(encoding="utf-8")
    match = re.search(
        r"DJANGO_SETTINGS_MODULE['\"]\s*,\s*['\"]([^'\"]+)['\"]",
        text,
    )
    if not match:
        raise SystemExit(
            "DJANGO_SETTINGS_MODULE is not set and manage.py default could not be parsed"
        )
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", match.group(1))


def main() -> int:
    root = _repo_root()
    sys.path.insert(0, str(root))
    # Migration graph loading should be a read-only CI check. Some services start
    # background reconcilers from app startup; keep those off for this static gate.
    os.environ.setdefault("CORTEX_RECONCILE_ENABLED", "0")
    os.environ.setdefault("CORTEX_AUTOSCALE_ENABLED", "0")
    _ensure_settings_module(root)

    import django
    from django.db.migrations.loader import MigrationLoader

    django.setup()
    loader = MigrationLoader(None, ignore_no_migrations=True)
    conflicts = loader.detect_conflicts()
    checked_apps = {app_label for app_label, _ in loader.graph.leaf_nodes()}
    if not conflicts:
        print(
            "Django migration graph OK: one leaf per app "
            f"({len(checked_apps)} migrated app(s) checked)."
        )
        return 0

    print("::error::Django migration graph has multiple leaf nodes", file=sys.stderr)
    for app_label, leaves in conflicts.items():
        print(
            f"::error::app {app_label!r} has conflicting migration leaves: "
            f"{', '.join(leaves)}",
            file=sys.stderr,
        )
    print(
        "Fix: rebase onto the target branch and renumber the migration, or add a "
        "Django merge migration (for example `python manage.py makemigrations "
        "--merge`) before review/merge.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
