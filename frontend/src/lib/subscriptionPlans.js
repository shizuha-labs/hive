// This preset is a display preference, not a replacement for model grants,
// owner leasing policy, credential health, or the provider's quota checks.
const AGENT_PLANS = {
  openai: new Set(['plus', 'pro', 'team', 'business', 'enterprise', 'edu']),
  xai: new Set(['supergrok', 'supergrok heavy', 'x premium+']),
  anthropic: new Set(['pro', 'max', 'max 5x', 'max 20x', 'team', 'enterprise']),
  google: new Set(['pro', 'ultra', 'google ai pro', 'google ai ultra']),
}

const PLAN_LABELS = {
  free: 'Free', go: 'Go', plus: 'Plus', pro: 'Pro', team: 'Team',
  business: 'Business', enterprise: 'Enterprise', edu: 'Edu', max: 'Max',
  'max 5x': 'Max 5x', 'max 20x': 'Max 20x', ultra: 'Ultra',
  supergrok: 'SuperGrok', 'supergrok heavy': 'SuperGrok Heavy',
  'supergrok lite': 'SuperGrok Lite', 'x basic': 'X Basic',
  'x premium': 'X Premium', 'x premium+': 'X Premium+',
  'google ai pro': 'Google AI Pro', 'google ai ultra': 'Google AI Ultra',
}

export function subscriptionPlan(account) {
  const raw = String(account.quota?.plan || '').trim().replace(/\s+/g, ' ')
  let normalized = raw.toLowerCase()
  if (account.provider === 'anthropic') normalized = normalized.replace(/_/g, ' ')
  // Antigravity appends its loadCodeAssist tier ID to the display name.
  if (account.provider === 'google') normalized = normalized.replace(/ \((?:standard|pro|ultra)-tier\)$/, '')
  const plan = ['unknown', 'none', 'null'].includes(normalized) ? '' : normalized
  return {
    key: `${account.provider}:${plan || 'unknown'}`,
    label: plan ? PLAN_LABELS[plan] || raw : 'Unknown plan',
    agentDefault: Boolean(AGENT_PLANS[account.provider]?.has(plan)),
    unknown: !plan,
  }
}

export function subscriptionPlanOptions(accounts) {
  const options = new Map()
  for (const account of accounts) {
    const plan = subscriptionPlan(account)
    const existing = options.get(plan.key)
    if (existing) existing.count += 1
    else options.set(plan.key, {
      ...plan,
      provider: account.provider,
      providerLabel: account.provider_label || account.provider,
      count: 1,
    })
  }
  return [...options.values()].sort((a, b) =>
    a.providerLabel.localeCompare(b.providerLabel) || Number(b.agentDefault) - Number(a.agentDefault) || a.label.localeCompare(b.label),
  )
}

export function selectedSubscriptionPlans(options, selection) {
  if (selection.mode === 'all') return options.map((option) => option.key)
  if (selection.mode === 'agents') return options.filter((option) => option.agentDefault).map((option) => option.key)
  return selection.keys
}
