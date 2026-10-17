# PLAT-1109 Runtime parallelism contract

Hive must not treat an agent's configured `maxConcurrentTasks` as proof that the
runtime can safely execute multiple Pulse tasks at once. Parallel execution is
reported as ready only when the runtime also proves the isolated-task contract.

## Safe default

All agents are `single_task` by default. This includes:

- k3s-native `FleetAgent` rows without `SHIZUHA_PARALLEL_EXECUTION_CONTRACT=isolated-task-v1`.
- external rt-fleet agents that set `allowParallelExecution`/`maxConcurrentTasks`
  but do not report runtime isolation evidence.
- any runtime that reports a missing/unknown contract version or incomplete
  isolation flags.

## Contract: `isolated-task-v1`

A parallel-capable runtime must provide a per-task boundary for every item below:

- session state
- workspace/filesystem writes
- MCP client sessions
- credential broker/grant access
- activity log attribution

External runtime agents advertise this to Hive with `runtimeParallelism`:

```json
{
  "supported": true,
  "contractVersion": "isolated-task-v1",
  "maxTasks": 3,
  "isolation": {
    "session": true,
    "workspace": true,
    "mcp_client": true,
    "credential_broker": true,
    "activity_log": true
  }
}
```

Hive reports `effective_max_tasks = min(configured maxConcurrentTasks, runtime maxTasks)`.
If any required flag is absent/false, Hive reports `single_task` with an explicit
reason.

k3s-native FleetAgent rows can opt into the same contract through env only after
the runtime image actually enforces isolated task execution:

```text
SHIZUHA_PARALLEL_EXECUTION_CONTRACT=isolated-task-v1
SHIZUHA_MAX_PARALLEL_TASKS=2
```

Do not set these env vars as a UI workaround. They are runtime evidence: setting
them without isolated sessions/workspaces/MCP/credentials/activity logs would make
Hive diagnostics lie.

## Scheduler/rollback behavior

A runtime that starts multiple tasks must keep failures scoped to the task slot:
failed task slots release their lease and leave sibling slots untouched. If the
runtime cannot preserve isolation or attribution, it must omit the contract and
run one task at a time.
