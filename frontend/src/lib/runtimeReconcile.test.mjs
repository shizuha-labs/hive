import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'vitest'

import { runtimeReconcileSummary } from './runtimeReconcile.js'

test('summarizes runtime reconcile outcomes and verifies Teams toasts errors', async () => {
  assert.deepEqual(
    runtimeReconcileSummary({}, 'Capability updated'),
    { level: 'success', message: 'Capability updated' },
  )

  assert.deepEqual(
    runtimeReconcileSummary(
      { runtime_reconcile: { matched_agents: 2, updated_agents: 2, errors: [] } },
      'Capability activated',
    ),
    { level: 'success', message: 'Capability activated; runtime reconcile 2/2 agents' },
  )

  assert.deepEqual(
    runtimeReconcileSummary(
      { runtime_reconcile: { matched_agents: 3, updated_agents: 1, errors: ['daemon unavailable'] } },
      'Capability created and activated',
    ),
    { level: 'error', message: 'Capability created and activated; runtime reconcile 1/3 agents; 1 error' },
  )

  assert.deepEqual(
    runtimeReconcileSummary(
      { runtime_reconcile: { matched_agents: 1, updated_agents: 0, errors: ['one', 'two'] } },
      'Capability reconciled',
    ),
    { level: 'error', message: 'Capability reconciled; runtime reconcile 0/1 agent; 2 errors' },
  )

  assert.deepEqual(
    runtimeReconcileSummary(
      { runtime_reconcile: { matched_agents: 4, updated_agents: 2, errors: ['agent patch failed'] } },
      'Capability mappings updated',
    ),
    { level: 'error', message: 'Capability mappings updated; runtime reconcile 2/4 agents; 1 error' },
  )

  assert.deepEqual(
    runtimeReconcileSummary(
      { runtime_reconcile: { matched_agents: 5, updated_agents: 3, errors: ['runtime patch failed'] } },
      'Team capabilities updated',
    ),
    { level: 'error', message: 'Team capabilities updated; runtime reconcile 3/5 agents; 1 error' },
  )

  const teamsPage = await readFile('src/pages/Teams.jsx', 'utf8')
  assert.match(teamsPage, /toastRuntimeReconcile\(toast, data, 'Capability mappings updated'\)/)
  assert.match(teamsPage, /toastRuntimeReconcile\(toast, data, 'Team capabilities updated'\)/)
  assert.match(teamsPage, /summary\.level === 'error'/)
})
