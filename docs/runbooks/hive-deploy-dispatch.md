# Hive deploy lane: dispatch hygiene (PLAT-7489 / PLAT-7404)

`deploy.yml` supports `workflow_dispatch` with a `ring` input that selects
which production rings execute. The rings are gated by their own conditions:

| ring input        | deploy-canary | deploy-fleet | verify-production-deploy |
|-------------------|---------------|--------------|--------------------------|
| *(push to main)*  | runs          | runs (after canary success) | runs after fleet success |
| `canary`          | runs          | **skipped**  | **skipped**              |
| `fleet` (+ `sha`) | **skipped**   | runs         | runs after fleet success — accepts the by-design staging/canary skips (PLAT-7411); the receipt still mints only from the fenced fleet executor |
| anything else     | **skipped**   | **skipped**  | **skipped**              |

`verify-production-deploy` gates on `needs.deploy-fleet.result == 'success'`
(PLAT-7489): it executes only when the fenced fleet ring actually completed,
and skips — without red-ing the lane — when the rings were intentionally
skipped by the dispatch parameters. The production DeploymentEvent receipt is
minted only by the fenced fleet executor; a skipped or green-without-fleet run
never implies a production deploy.

## Sanctioned dispatch sequence (canary → fleet)

1. **Canary first** — validate the build on the canary ring:

   ```bash
   # ring=canary dispatch; sha defaults to the dispatch ref
   curl -X POST "$ORIGIN_API/repos/shizuha-labs/hive/actions/workflows/deploy.yml/dispatches" \
     -H "Authorization: token $TOKEN" -H "Content-Type: application/json" \
     -d '{"ref":"main","inputs":{"ring":"canary"}}'
   ```

2. **Wait for canary green**, then **fleet with the exact SHA** — the fleet
   gate requires `inputs.sha == github.sha`, so pass the same commit the
   canary validated:

   ```bash
   curl -X POST "$ORIGIN_API/repos/shizuha-labs/hive/actions/workflows/deploy.yml/dispatches" \
     -H "Authorization: token $TOKEN" -H "Content-Type: application/json" \
     -d "{\"ref\":\"main\",\"inputs\":{\"ring\":\"fleet\",\"sha\":\"$SHA\"}}"
   ```

## Common failure modes

- **Ring input omitted or misspelled** → both rings skip by their own gate
  conditions. Before PLAT-7489 this also red-ed the lane at
  `verify-production-deploy` (runs 2510–2512); now it skips cleanly. Re-dispatch
  with an explicit `ring`.
- **`sha` mismatch on ring=fleet** → `deploy-fleet` skips its gate
  (`inputs.sha == github.sha`); pass the exact full SHA the canary run
  validated.
- **Re-dispatching a failed run** re-executes at the ORIGINAL head SHA — it
  does not pick up later fixes on the branch. Dispatch fresh at the current
  head instead.
