import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Activity, ExternalLink, KeyRound, Loader2, Pencil, RefreshCw, SlidersHorizontal, Zap } from 'lucide-react'
import clsx from 'clsx'
import { tokenPoolsApi } from '../services/api'
import { useHive } from '../contexts/HiveContext'
import { useToast } from '../components/Toast'
import { EmptyState, ErrorState } from '../components/States'
import ClaudeOAuthPanel from '../components/ClaudeOAuthPanel'
import SubscriptionPlanFilter from '../components/SubscriptionPlanFilter'
import { selectedSubscriptionPlans, subscriptionPlan, subscriptionPlanOptions } from '../lib/subscriptionPlans'

/** Canonical Cortex console origin — never the shizuha.com/cortex path alias (CTX-523). */
export const CORTEX_DASHBOARD_URL = 'https://cortex.shizuha.com/cortex/'

export function fmtDateTime(value) {
  return value ? new Date(value).toLocaleString() : '—'
}

function fmtCountdown(totalSeconds) {
  if (totalSeconds == null || totalSeconds <= 0) return null
  const s = Math.floor(totalSeconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${sec}s`
  return `${sec}s`
}

function useCooldownRemaining(cooldownUntil) {
  const [remaining, setRemaining] = useState(0)
  useEffect(() => {
    if (!cooldownUntil) {
      setRemaining(0)
      return undefined
    }
    const tick = () => {
      setRemaining(Math.max(0, Math.ceil((new Date(cooldownUntil).getTime() - Date.now()) / 1000)))
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [cooldownUntil])
  return remaining
}

function healthClass(health) {
  if (health === 'active') return 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300'
  if (health === 'cooling') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
  if (health === 'revoked' || health === 'disabled' || health === 'expired') {
    return 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300'
  }
  return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
}

function quotaStatusClass(status) {
  if (status === 'ok') return 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300'
  if (status === 'partial' || status === 'stale') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
  if (status === 'error') return 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300'
  return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
}

// 'unsupported' = provider has no usage API for this credential (xAI/Grok,
// inference-only Claude OATs). Render it as a neutral, non-alarming label.
function quotaStatusLabel(status) {
  if (status === 'unsupported') return 'no usage API'
  if (status === 'missing') return 'no snapshot'
  return status
}

export function SubscriptionPlan({ provider, quota }) {
  const plan = typeof quota?.plan === 'string' ? quota.plan.trim() : ''
  if (!plan && provider !== 'xai') return null
  return (
    <div className="mb-1.5 flex items-center gap-2 text-[10px]">
      <span className="text-gray-500 dark:text-gray-400">Plan</span>
      <span
        className="rounded-full bg-gray-100 px-2 py-0.5 font-medium text-gray-700 dark:bg-white/[0.06] dark:text-gray-300"
        title={plan ? 'Subscription plan reported by the provider' : 'The provider has not reported a subscription plan. Quota remaining does not identify the plan.'}
      >
        {plan || 'Unknown'}
      </span>
    </div>
  )
}


function QuotaBars({ quota }) {
  const windows = quota?.windows || []
  if (!windows.length) {
    const detail = quota?.detail || ''
    // Provider has no usage/quota API (xAI, Claude inference-only OATs) — this is
    // an expected capability gap, not an error. Show a clean dash with a tooltip
    // instead of a raw status string.
    const unavailable = quota?.confidence === 'unsupported'
      || /usage_api_unavailable|limited_token|no_usage_endpoint|inference-only/i.test(detail)
    if (unavailable) {
      return (
        <div className="text-xs text-gray-400" title="Provider exposes no usage/quota API for this credential — usage can't be reported here.">
          <span className="text-gray-300 dark:text-gray-500">—</span>
        </div>
      )
    }
    const friendly = detail ? detail.replace(/_/g, ' ') : 'No snapshot yet — run Check now'
    return <div className="text-xs text-gray-400">{friendly}</div>
  }
  return (
    <div className="space-y-1.5 min-w-[160px]">
      {windows.map((w) => {
        const left = w.left_percent != null ? Number(w.left_percent) : null
        const used = w.used_percent != null ? Number(w.used_percent) : left != null ? 100 - left : null
        const pctLeft = left != null ? left : used != null ? 100 - used : null
        const tone =
          pctLeft == null ? 'bg-gray-400'
            : pctLeft > 40 ? 'bg-green-500'
              : pctLeft > 15 ? 'bg-amber-500'
                : 'bg-red-500'
        return (
          <div key={w.id || w.label}>
            <div className="flex items-baseline justify-between gap-2 text-[10px] text-gray-500 dark:text-gray-400">
              <span className="shrink-0 whitespace-nowrap">{w.label || w.id}</span>
              <span className="whitespace-nowrap tabular-nums">{pctLeft != null ? `${pctLeft.toFixed(0)}% left` : '—'}</span>
            </div>
            {w.resets_at && (
              <div className="text-right text-[9px] text-gray-400 dark:text-gray-500">resets {fmtDateTime(w.resets_at)}</div>
            )}
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
              <div
                className={clsx('h-full rounded-full transition-all', tone)}
                style={{ width: `${Math.max(0, Math.min(100, pctLeft ?? 0))}%` }}
              />
            </div>
          </div>
        )
      })}
      {quota?.reset_credits?.available != null && (
        <div
          className={clsx(
            'text-[10px] font-medium',
            quota.reset_credits.available > 0
              ? 'text-emerald-600 dark:text-emerald-400'
              : 'text-gray-400',
          )}
          title={(quota.reset_credits.credits || [])
            .map((c) => `${c.reset_type || 'credit'} ${c.status || ''}${c.expires_at ? ` (expires ${fmtDateTime(c.expires_at)})` : ''}`)
            .join('\n') || 'Rate-limit reset credits redeemable on this account'}
        >
          usage resets: {quota.reset_credits.available} available
          {quota.reset_credits.total_earned != null && ` · ${quota.reset_credits.total_earned} earned`}
        </div>
      )}
      {quota?.fetched_at && (
        <div className="text-[10px] text-gray-400">fetched {fmtDateTime(quota.fetched_at)}</div>
      )}
    </div>
  )
}

export function availableResetCount(account) {
  return Number(account.quota?.reset_credits?.available || 0)
}

export function canRedeemReset(account) {
  // Hive can redeem Codex/OpenAI reset credits. Grok/xAI pending counts are
  // display-only (grok.com Settings → Usage owns redeem).
  return account?.provider === 'openai' && availableResetCount(account) > 0
}

export function liveLeaseCount(account) {
  const count = Number(account?.live_leases)
  return Number.isFinite(count) && count > 0 ? count : 0
}

export function credentialHealthEmptyMessage(isStaff) {
  return isStaff
    ? 'Add tokens under Token Pools, or switch Org / Mine.'
    : 'Add tokens under Token Pools.'
}

export function accountReauthUrl(account) {
  if (account?.provider !== 'openai') return ''
  const url = String(account?.codex_reauth_url || '')
  return url.startsWith('/hive/credentials/codex/re-auth?') ? url : ''
}

export function accountReauthRoute(account) {
  return accountReauthUrl(account).replace(/^\/hive(?=\/)/, '')
}

function AccountRow({ account }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const coolRemaining = useCooldownRemaining(account.cooldown_until)
  const [editing, setEditing] = useState(false)
  const [editLabel, setEditLabel] = useState(account.label || '')
  const [editEmail, setEditEmail] = useState(account.account_email || '')
  const saveMutation = useMutation({
    mutationFn: () => tokenPoolsApi.updateEntry(account.pool_id, account.entry_id, {
      label: editLabel.trim() || account.label,
      account_email: editEmail.trim(),
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['credential-health'] })
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      toast.success('Account details saved')
      setEditing(false)
    },
    onError: (e) => toast.error(e.response?.data?.error || 'Save failed'),
  })
  const checkMutation = useMutation({
    mutationFn: () => tokenPoolsApi.checkEntry(account.pool_id, account.entry_id, {}),
    onSuccess: (resp) => {
      queryClient.invalidateQueries({ queryKey: ['credential-health'] })
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      const c = resp?.data?.check || {}
      const q = c.quota || {}
      const win = (q.windows || [])[0]
      const left = win?.left_percent
      if (c.outcome === 'ok') {
        toast.success(
          `${account.label}: OK${left != null ? ` · ${Number(left).toFixed(0)}% left (session)` : ''}`,
        )
      } else if (c.outcome === 'rate_limited' || c.outcome === 'cooling') {
        toast.error(`${account.label}: still limited${fmtCountdown(c.cooldown_remaining_seconds) ? ` · ${fmtCountdown(c.cooldown_remaining_seconds)}` : ''}`)
      } else toast.success(`${account.label}: ${c.outcome || 'checked'}`)
    },
    onError: (e) => toast.error(e.response?.data?.error || 'Check failed'),
  })

  const availableResets = availableResetCount(account)
  const showRedeem = canRedeemReset(account)
  const reauthUrl = accountReauthRoute(account)
  const redeemMutation = useMutation({
    mutationFn: () => tokenPoolsApi.redeemReset(account.pool_id, account.entry_id, {}),
    onSuccess: (resp) => {
      queryClient.invalidateQueries({ queryKey: ['credential-health'] })
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      const r = resp?.data?.redeem || {}
      if (r.ok) toast.success(`${account.label}: reset redeemed · ${r.windows_reset ?? 1} window reset`)
      else toast.error(`${account.label}: ${(r.detail || 'no reset available').replace(/_/g, ' ')}`)
    },
    onError: (e) => toast.error(e.response?.data?.redeem?.detail || e.response?.data?.error || 'Redeem failed'),
  })

  return (
    <tr className="border-b border-gray-100 transition-colors last:border-0 hover:bg-amber-500/[0.03] dark:border-white/[0.04]">
      <td className="p-3">
        {editing ? (
          <div className="space-y-1.5 min-w-[210px]">
            <input
              value={editLabel}
              onChange={(e) => setEditLabel(e.target.value)}
              placeholder="Label"
              className="w-full rounded border border-gray-300 bg-white px-2 py-1 text-xs dark:border-gray-600 dark:bg-gray-900"
            />
            <input
              value={editEmail}
              onChange={(e) => setEditEmail(e.target.value)}
              placeholder="Account email (which provider account owns this token)"
              className="w-full rounded border border-gray-300 bg-white px-2 py-1 text-xs dark:border-gray-600 dark:bg-gray-900"
            />
            <div className="flex gap-2">
              <button
                type="button"
                disabled={saveMutation.isPending}
                onClick={() => saveMutation.mutate()}
                className="rounded bg-primary-600 px-2 py-0.5 text-xs text-white disabled:opacity-40"
              >
                {saveMutation.isPending ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                onClick={() => { setEditing(false); setEditLabel(account.label || ''); setEditEmail(account.account_email || '') }}
                className="rounded border border-gray-300 px-2 py-0.5 text-xs dark:border-gray-600"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="flex items-center gap-1.5">
              <span className="font-medium">{account.label}</span>
              <button
                type="button"
                title="Edit label / account email"
                onClick={() => setEditing(true)}
                className="text-gray-400 hover:text-primary-600"
              >
                <Pencil className="h-3 w-3" />
              </button>
            </div>
            {account.account_email ? (
              <div className="text-xs text-gray-500">{account.account_email}</div>
            ) : (
              <button
                type="button"
                onClick={() => setEditing(true)}
                className="text-xs text-amber-600 hover:underline dark:text-amber-400"
              >
                set account email
              </button>
            )}
            <div className="text-xs text-gray-400 capitalize">{account.owner_kind} pool</div>
            {(account.token_hint || account.token_fingerprint) && (
              <div
                className="mt-0.5 font-mono text-[10px] text-gray-400 dark:text-gray-500"
                title={account.token_fingerprint ? `Fingerprint ${account.token_fingerprint.slice(0, 16)}…` : 'Token identifier'}
              >
                {account.token_hint || `#${account.token_fingerprint.slice(0, 10)}`}
              </div>
            )}
          </>
        )}
      </td>
      <td className="p-3">
        <span className={clsx('rounded-full px-2 py-0.5 text-xs font-medium', healthClass(account.health))}>
          {account.health}
          {(coolRemaining > 0 || account.health === 'cooling') && coolRemaining > 0
            ? ` · ${fmtCountdown(coolRemaining)}`
            : ''}
        </span>
      </td>
      <td className="p-3">
        <SubscriptionPlan provider={account.provider} quota={account.quota} />
        {account.quota_status && (
          <div className="mb-1 flex items-center gap-2">
            <span className={clsx('rounded-full px-2 py-0.5 text-[10px] font-medium', quotaStatusClass(account.quota_status.status))}>
              {quotaStatusLabel(account.quota_status.status)}
            </span>
            {account.quota_status.min_left_percent != null && (
              <span className="text-[10px] text-gray-500">
                min {Number(account.quota_status.min_left_percent).toFixed(0)}% left
              </span>
            )}
          </div>
        )}
        <QuotaBars quota={account.quota} />
      </td>
      <td className="p-3 text-sm text-gray-500">
        <div>{fmtDateTime(account.cooldown_until)}</div>
        {coolRemaining > 0 && (
          <div className="text-xs text-amber-600 dark:text-amber-400">active in {fmtCountdown(coolRemaining)}</div>
        )}
      </td>
      <td className="p-3 text-sm text-gray-500">{account.last_status || '—'}</td>
      <td className="p-3 text-sm text-gray-500">
        <div>{liveLeaseCount(account)} active leases</div>
        <div className="text-xs">last issued {fmtDateTime(account.last_leased_at)}</div>
      </td>
      <td className="p-3 text-sm text-gray-500">{fmtDateTime(account.access_expires_at)}</td>
      <td className="p-3 text-right">
        <div className="inline-flex items-center gap-1">
          {reauthUrl && (
            <Link
              to={reauthUrl}
              className="inline-flex items-center gap-1 rounded-lg border border-primary-200 px-2 py-1 text-xs text-primary-700 transition-colors hover:bg-primary-50 dark:border-primary-900 dark:text-primary-300 dark:hover:bg-primary-900/20"
            >
              <KeyRound className="h-3 w-3" />
              Re-auth
            </Link>
          )}
          {showRedeem && (
            <button
              type="button"
              disabled={redeemMutation.isPending}
              onClick={() => {
                if (window.confirm(`Redeem 1 rate-limit reset credit for ${account.label}? This immediately resets its usage window (${availableResets} available).`)) redeemMutation.mutate()
              }}
              title={`${availableResets} reset credit(s) available — redeem to reset the usage window now`}
              className="inline-flex items-center gap-1 rounded-lg border border-emerald-400/40 px-2 py-1 text-xs text-emerald-600 transition-colors hover:bg-emerald-500/10 disabled:opacity-40 dark:text-emerald-300"
            >
              {redeemMutation.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Zap className="h-3 w-3" />}
              Redeem reset ({availableResets})
            </button>
          )}
          <button
            type="button"
            disabled={checkMutation.isPending}
            onClick={() => checkMutation.mutate()}
            className="inline-flex items-center gap-1 rounded-lg border border-transparent px-2 py-1 text-xs text-gray-400 transition-colors hover:border-amber-400/40 hover:text-amber-500 disabled:opacity-40 dark:hover:text-amber-300"
          >
            {checkMutation.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
            Check now
          </button>
        </div>
      </td>
    </tr>
  )
}

const PROVIDER_ORDER = ['anthropic', 'openai', 'xai', 'google']

export default function CredentialHealth() {
  const { isStaff } = useHive()
  // Personal-ownership model (2026-07-11): credentials live in user pools now;
  // the org pool is legacy/shared — default everyone to Mine.
  const [scope, setScope] = useState('user')
  const [planSelection, setPlanSelection] = useState({ mode: 'agents' })
  const params = useMemo(() => (scope === 'org' ? { scope: 'org' } : { scope: 'user' }), [scope])
  const toast = useToast()
  const queryClient = useQueryClient()
  const refreshAllMutation = useMutation({
    mutationFn: () => tokenPoolsApi.credentialHealthRefreshAll(params),
    onSuccess: (resp) => {
      queryClient.invalidateQueries({ queryKey: ['credential-health'] })
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      const r = resp?.data?.refresh || {}
      const failed = Number(r.quota_failed || 0)
      const updated = Number(r.quota_updated || 0)
      if (failed > 0) {
        toast.error(`Refreshed ${updated} account(s), ${failed} failed`)
      } else {
        toast.success(`Refreshed ${updated} account(s)`)
      }
    },
    onError: () => toast.error('Refresh all failed'),
  })

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ['credential-health', scope],
    queryFn: () => tokenPoolsApi.credentialHealth(params).then((r) => r.data),
    refetchInterval: 15_000,
  })

  const summary = data?.summary || {}
  const accounts = data?.accounts || []
  const planOptions = subscriptionPlanOptions(accounts)
  const selectedPlans = selectedSubscriptionPlans(planOptions, planSelection)
  const selectedPlanSet = new Set(selectedPlans)
  const visibleAccounts = accounts.filter((account) => selectedPlanSet.has(subscriptionPlan(account).key))
  const byProvider = visibleAccounts.reduce((groups, account) => {
    if (!groups[account.provider]) groups[account.provider] = []
    groups[account.provider].push(account)
    return groups
  }, {})
  const quotaCounts = accounts.reduce((acc, account) => {
    const status = account.quota_status?.status || 'missing'
    acc[status] = (acc[status] || 0) + 1
    return acc
  }, {})
  const lowQuota = accounts.filter((account) => {
    const left = account.quota_status?.min_left_percent
    return left != null && Number(left) <= 15
  }).length

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="hv-kicker mb-1">Accounts · quotas</div>
          <h1 className="hv-page-title flex items-center gap-2.5">
            <Activity className="h-6 w-6 text-primary-500" />
            Subscriptions
          </h1>
          <p className="mt-1 max-w-3xl text-sm text-gray-500 dark:text-gray-400">
            Subscription accounts, token pools, quota snapshots, and lease health — the credentials Hive manages for the fleet.
            Router telemetry (providers, routes, API keys, usage, cost) lives in the Cortex dashboard.
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-4">
            <Link to="/token-pools" className="inline-flex items-center gap-1 text-sm text-amber-600 hover:underline dark:text-amber-400">
              <SlidersHorizontal className="h-4 w-4" />
              Manage token pools
            </Link>
            <a
              href={CORTEX_DASHBOARD_URL}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-sm text-amber-600 hover:underline dark:text-amber-400"
            >
              <ExternalLink className="h-4 w-4" />
              Cortex dashboard
            </a>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {isStaff && (
            <div className="hv-panel inline-flex rounded-lg p-1">
              {['user', 'org'].map((item) => (
                <button
                  key={item}
                  type="button"
                  onClick={() => setScope(item)}
                  className={clsx(
                    'rounded-md px-3 py-1.5 text-sm font-medium capitalize',
                    scope === item ? 'bg-gradient-to-b from-amber-400 to-amber-600 text-gray-950' : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-white/[0.06]',
                  )}
                >
                  {item === 'org' ? 'Org' : 'Mine'}
                </button>
              ))}
            </div>
          )}
          <button
            type="button"
            onClick={() => refetch()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm transition-colors hover:bg-gray-50 dark:border-white/10 dark:bg-white/[0.04] dark:hover:bg-white/[0.08]"
          >
            <RefreshCw className={clsx('h-4 w-4', isFetching && 'animate-spin')} />
            Refresh
          </button>
          {/* Force a live probe for EVERY visible account. The plain Refresh
              above (and the 15s poll) only re-read stored snapshots, and the
              background loop skips an `unsupported`-marked account for 24h —
              so rows could read "no usage API" while a per-row Check now
              returned real numbers (operator 2026-08-04). */}
          <button
            type="button"
            disabled={refreshAllMutation.isPending}
            onClick={() => refreshAllMutation.mutate()}
            title="Force a live quota probe for every account across all providers"
            className="inline-flex items-center gap-1.5 rounded-lg border border-amber-400/40 bg-white px-3 py-1.5 text-sm text-amber-600 transition-colors hover:bg-amber-500/10 disabled:opacity-40 dark:border-amber-400/30 dark:bg-white/[0.04] dark:text-amber-300 dark:hover:bg-amber-500/10"
          >
            {refreshAllMutation.isPending
              ? <Loader2 className="h-4 w-4 animate-spin" />
              : <RefreshCw className="h-4 w-4" />}
            Refresh all
          </button>
        </div>
      </div>

      {data?.note && (
        <details className="group">
          <summary className="hv-chip cursor-pointer select-none list-none hover:border-amber-400/40">
            how quotas work
          </summary>
          <div className="hv-panel mt-2 px-4 py-3 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
            {data.note}
          </div>
        </details>
      )}

      <div className="space-y-2">
        <div className="hv-kicker">Scope totals · all subscription plans</div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
          {[
            { label: 'Accounts', value: summary.total ?? '—' },
            { label: 'Leaseable', value: summary.leaseable ?? '—', tone: 'green' },
            { label: 'Cooling', value: summary.cooling ?? '—', tone: 'amber' },
            { label: 'Revoked / off', value: summary.revoked_or_disabled ?? '—', tone: 'red' },
            { label: 'Quota ok', value: quotaCounts.ok ?? 0, tone: 'green' },
            { label: 'Quota attention', value: (quotaCounts.error || 0) + (quotaCounts.stale || 0) + (quotaCounts.missing || 0) + lowQuota, tone: 'amber' },
          ].map((c) => (
            <div key={c.label} className="hv-panel p-3">
              <div className="hv-kicker flex items-center gap-1.5">
                <span
                  className={clsx(
                    'h-1.5 w-1.5 rounded-full',
                    c.tone === 'green' ? 'bg-emerald-500' : c.tone === 'amber' ? 'bg-amber-400' : c.tone === 'red' ? 'bg-red-500' : 'bg-gray-400',
                  )}
                />
                {c.label}
              </div>
              <div
                className={clsx(
                  'hv-stat-number mt-1',
                  c.tone === 'green' && '!text-emerald-500',
                  c.tone === 'amber' && '!text-amber-400',
                  c.tone === 'red' && '!text-red-500',
                )}
              >
                {c.value}
              </div>
            </div>
          ))}
        </div>
      </div>

      <SubscriptionPlanFilter
        options={planOptions}
        selected={selectedPlans}
        mode={planSelection.mode}
        onChange={setPlanSelection}
        visibleCount={visibleAccounts.length}
        totalCount={accounts.length}
      />

      {isError ? (
        <ErrorState message="Could not load credential health." onRetry={() => refetch()} />
      ) : isLoading ? (
        <div className="flex items-center gap-2 text-sm text-gray-500">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading…
        </div>
      ) : (data?.accounts || []).length === 0 ? (
        <EmptyState
          icon={Activity}
          title="No credentials in this scope"
          message={credentialHealthEmptyMessage(isStaff)}
        />
      ) : visibleAccounts.length === 0 ? (
        <div className="hv-panel px-4 py-8 text-center">
          <h2 className="text-sm font-semibold">No accounts match the selected plans</h2>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Include more subscription plans to see your other accounts.</p>
          <button type="button" onClick={() => setPlanSelection({ mode: 'all' })} className="mt-4 rounded-lg bg-amber-500/15 px-3 py-2 text-xs font-medium text-amber-700 transition-colors hover:bg-amber-500/25 dark:text-amber-300">Show all plans</button>
        </div>
      ) : (
        <div className="space-y-6">
          {PROVIDER_ORDER.filter((p) => (byProvider[p] || []).length > 0)
            .concat(Object.keys(byProvider).filter((p) => !PROVIDER_ORDER.includes(p)))
            .map((provider) => {
              const rows = byProvider[provider] || []
              const label = rows[0]?.provider_label || provider
              return (
                <div
                  key={provider}
                  className="hv-panel"
                >
                  <div className="flex items-center justify-between border-b border-gray-100 px-4 py-3 dark:border-white/[0.06]">
                    <h2 className="text-base font-bold tracking-tight text-gray-950 dark:text-white">{label}</h2>
                    <span className="hv-chip">{rows.length} account{rows.length !== 1 ? 's' : ''}</span>
                  </div>
                  {provider === 'anthropic' && isStaff && (
                    <div className="border-b border-gray-100 px-4 pb-3 dark:border-gray-700">
                      <ClaudeOAuthPanel />
                    </div>
                  )}
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[900px] text-left text-sm">
                      <thead>
                        <tr className="border-b border-gray-100 font-mono text-[10px] uppercase tracking-[0.14em] text-gray-400 dark:border-white/[0.08] dark:text-gray-500">
                          <th className="p-3 font-medium">Account</th>
                          <th className="p-3 font-medium">Health</th>
                          <th className="p-3 font-medium">Subscription quota</th>
                          <th className="p-3 font-medium">Hive cool / ETA</th>
                          <th className="p-3 font-medium">Last status</th>
                          <th className="p-3 font-medium">Hive leases</th>
                          <th className="p-3 font-medium">Access exp</th>
                          <th className="p-3" />
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((a) => (
                          <AccountRow key={a.entry_id} account={a} />
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )
            })}
        </div>
      )}

      <p className="text-xs text-gray-400">
        Generated {fmtDateTime(data?.generated_at)} · auto-refreshes every 15s
      </p>
    </div>
  )
}
