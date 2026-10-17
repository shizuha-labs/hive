#!/usr/bin/env bash
# HIVE-665: regression test for the approval-only mode of migration-compat-gate.sh.
# Pure git + grep + file checks — no cluster/images needed, so it runs anywhere
# (locally and in CI). Proves the early `test`-job gate fails fast on a flagged
# migration missing its PLAT-187 approval doc, passes when the doc exists, and is
# scoped to the unreleased delta (anchor...HEAD) so grandfathered migrations are
# never re-litigated.
set -uo pipefail

GATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/migration-compat-gate.sh"
[[ -x "$GATE" || -f "$GATE" ]] || { echo "gate script not found at $GATE"; exit 2; }

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT; cd "$work"
git init -q .; git config user.email t@ci; git config user.name ci
mkdir -p claws/migrations .forgejo/migration-approvals
echo app > README; git add -A; git commit -qm base
BASE="$(git rev-parse HEAD)"

pass=0; fail=0
check() { # desc  want-exit
  local desc="$1" want="$2"
  MIGRATION_APPROVAL_ONLY=1 bash "$GATE" "$BASE" >/tmp/mag_out 2>&1
  local got=$?
  if [[ "$got" == "$want" ]]; then echo "  PASS[$got] $desc"; pass=$((pass+1))
  else echo "  FAIL want=$want got=$got: $desc"; sed 's/^/     /' /tmp/mag_out; fail=$((fail+1)); fi
}
reset() { git reset -q --hard "$BASE"; mkdir -p claws/migrations .forgejo/migration-approvals; }

echo x >> README; git add -A; git commit -qm nochange
check "no migration changed -> pass" 0
reset

printf 'operations = [migrations.AddField()]\n' > claws/migrations/0100_add.py
git add -A; git commit -qm additive
check "additive (non-flagged) migration -> pass" 0
reset

printf 'operations = [migrations.AlterField()]\n' > claws/migrations/0101_alter.py
git add -A; git commit -qm flagged-noapproval
check "flagged AlterField without approval doc -> FAIL" 1
echo 'approved: reversible plan' > .forgejo/migration-approvals/claws__0101_alter.md
git add -A; git commit -qm add-approval
check "flagged AlterField with approval doc -> pass" 0
reset

printf 'operations = [migrations.RemoveField()]\n' > claws/migrations/0099_grandfathered.py
git add -A; git commit -qm pre-anchor-flagged
GRAND_BASE="$(git rev-parse HEAD)"
echo y >> README; git add -A; git commit -qm after-anchor
BASE="$GRAND_BASE" check "grandfathered flagged migration before anchor -> pass (outside delta)" 0
BASE="$(git rev-parse "$BASE")"; reset

printf 'operations = [migrations.RunPython(fwd)]\n' > claws/migrations/0102_data.py
git add -A; git commit -qm runpython-noapproval
check "flagged RunPython without approval doc -> FAIL" 1

echo "approval-only gate: $pass passed, $fail failed"
[[ "$fail" == 0 ]]
