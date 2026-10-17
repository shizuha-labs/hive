export function runtimeReconcileSummary(data, fallbackSuccess) {
  const diag = data?.runtime_reconcile
  if (!diag) {
    return { level: 'success', message: fallbackSuccess }
  }

  const errors = Array.isArray(diag.errors) ? diag.errors.length : Number(diag.errors || 0)
  const matched = Number(diag.matched_agents || 0)
  const updated = Number(diag.updated_agents || 0)
  const base = `${fallbackSuccess}; runtime reconcile ${updated}/${matched} agent${matched === 1 ? '' : 's'}`

  if (errors > 0) {
    return {
      level: 'error',
      message: `${base}; ${errors} error${errors === 1 ? '' : 's'}`,
    }
  }

  return { level: 'success', message: base }
}

export function toastRuntimeReconcile(toast, data, fallbackSuccess) {
  const summary = runtimeReconcileSummary(data, fallbackSuccess)
  toast[summary.level](summary.message)
  return summary
}
