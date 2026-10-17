import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useLocation } from 'react-router-dom'
import { Activity, KeyRound, Loader2, Plus, RefreshCw, ShieldCheck, SlidersHorizontal, ToggleLeft, ToggleRight, Trash2 } from 'lucide-react'
import clsx from 'clsx'
import { codexDeviceAuthApi, grokDeviceAuthApi, tokenPoolsApi } from '../services/api'
import ClaudeOAuthPanel from '../components/ClaudeOAuthPanel'
import AntigravityOAuthPanel from '../components/AntigravityOAuthPanel'
import { useHive } from '../contexts/HiveContext'
import { useToast } from '../components/Toast'
import { EmptyState, ErrorState } from '../components/States'

const PROVIDERS = [
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'xai', label: 'xAI Grok' },
  { value: 'google', label: 'Google Antigravity' },
]

function fmtDateTime(value) {
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

/** Live countdown until cooldown_until. Returns seconds remaining or 0. */
function useCooldownRemaining(cooldownUntil) {
  const [remaining, setRemaining] = useState(() => {
    if (!cooldownUntil) return 0
    return Math.max(0, Math.ceil((new Date(cooldownUntil).getTime() - Date.now()) / 1000))
  })
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

function codexStatusClass(status) {
  if (status === 'active') return 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300'
  if (status === 'revoked' || status === 'expired') return 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300'
  if (status === 'cooling') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
  return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
}

function PoolBadge({ pool }) {
  const orgPool = pool.owner_kind === 'org'
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium',
        orgPool
          ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
          : 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300'
      )}
    >
      {orgPool && <ShieldCheck className="h-3 w-3" />}
      {orgPool ? 'Org shared (legacy)' : 'My credentials'}
    </span>
  )
}

function CreatePoolForm({ scope, isStaff }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [provider, setProvider] = useState('anthropic')
  const [displayName, setDisplayName] = useState('')

  const createMutation = useMutation({
    mutationFn: () => tokenPoolsApi.create({ provider, display_name: displayName, scope }),
    onSuccess: () => {
      setDisplayName('')
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      toast.success('Token pool ready.')
    },
    onError: (error) => toast.error(error.response?.data?.error || 'Could not create token pool.'),
  })

  return (
    <form
      className="grid gap-3 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800 md:grid-cols-[180px_1fr_auto]"
      onSubmit={(e) => {
        e.preventDefault()
        if (!createMutation.isPending) createMutation.mutate()
      }}
    >
      <select
        value={provider}
        onChange={(e) => setProvider(e.target.value)}
        className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-900"
      >
        {PROVIDERS.map((p) => (
          <option key={p.value} value={p.value}>{p.label}</option>
        ))}
      </select>
      <input
        value={displayName}
        onChange={(e) => setDisplayName(e.target.value)}
        placeholder={scope === 'org' && isStaff ? 'Internal fleet pool label' : 'Pool label (optional)'}
        maxLength={160}
        className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-900"
      />
      <button
        type="submit"
        disabled={createMutation.isPending}
        className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-gradient-to-b from-amber-400 to-amber-600 px-4 py-2 text-sm font-semibold text-gray-950 shadow-[0_2px_12px_-2px_rgba(245,158,11,0.5)] transition-transform hover:-translate-y-px disabled:cursor-not-allowed disabled:opacity-50"
      >
        {createMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
        Create pool
      </button>
    </form>
  )
}

function AddTokenForm({ pool }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [label, setLabel] = useState('')
  const [token, setToken] = useState('')
  const [tier, setTier] = useState('')
  const [priority, setPriority] = useState(0)

  const mutation = useMutation({
    mutationFn: () => tokenPoolsApi.addEntry(pool.id, { label, token, tier, priority: Number(priority) || 0 }),
    onSuccess: () => {
      setLabel('')
      setToken('')
      setTier('')
      setPriority(0)
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      toast.success('Token added. Raw token was encrypted and will not be shown again.')
    },
    onError: (error) => toast.error(error.response?.data?.error || 'Could not add token.'),
  })

  return (
    <form
      className="mt-4 grid gap-2 rounded-lg bg-gray-50 p-3 dark:bg-gray-900/40 lg:grid-cols-[1fr_1.6fr_120px_110px_auto]"
      onSubmit={(e) => {
        e.preventDefault()
        if (!mutation.isPending && label.trim() && token.trim()) mutation.mutate()
      }}
    >
      <input
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        placeholder="Label"
        maxLength={120}
        required
        className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-900"
      />
      <input
        value={token}
        onChange={(e) => setToken(e.target.value)}
        placeholder="Raw provider token (encrypted on save)"
        type="password"
        required
        className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-900"
      />
      <input
        value={tier}
        onChange={(e) => setTier(e.target.value)}
        placeholder="Tier"
        maxLength={50}
        className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-900"
      />
      <input
        value={priority}
        onChange={(e) => setPriority(e.target.value)}
        placeholder="Priority"
        type="number"
        className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-900"
      />
      <button
        type="submit"
        disabled={mutation.isPending || !label.trim() || !token.trim()}
        className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm font-medium hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:hover:bg-gray-700"
      >
        {mutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
        Add
      </button>
    </form>
  )
}

function EntryRow({ pool, entry }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [label, setLabel] = useState(entry.label)
  const [tier, setTier] = useState(entry.tier || '')
  const [priority, setPriority] = useState(entry.priority || 0)
  const serverLeasingEnabled = entry.agent_leasing_enabled ?? entry.active
  const [leasingEnabled, setLeasingEnabled] = useState(serverLeasingEnabled)
  useEffect(() => setLeasingEnabled(serverLeasingEnabled), [serverLeasingEnabled])
  const dirty = label !== entry.label || tier !== (entry.tier || '') || Number(priority) !== Number(entry.priority || 0)
  const isCodexManagedEntry = pool.provider === 'openai'
    && (!!entry.codex_account_email || entry.label?.startsWith('org-openai-'))
  const isGrokManagedEntry = pool.provider === 'xai'
    && (!!entry.account_email || entry.label?.startsWith('org-xai-'))
  const codexStatus = entry.codex_account_status || ''
  const coolRemaining = useCooldownRemaining(entry.cooldown_until)
  const cooling = coolRemaining > 0
  const effectiveStatus = cooling ? 'cooling' : (codexStatus || (entry.last_status === 'rate_limited' ? 'cooling' : entry.last_status || ''))

  const updateMutation = useMutation({
    mutationFn: (data) => tokenPoolsApi.updateEntry(pool.id, entry.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      toast.success('Token metadata updated.')
    },
    onError: (error) => toast.error(error.response?.data?.error || 'Could not update token.'),
  })

  const leasingMutation = useMutation({
    mutationFn: (enabled) => tokenPoolsApi.updateEntry(
      pool.id,
      entry.id,
      { agent_leasing_enabled: enabled },
    ),
    onMutate: (enabled) => {
      const previous = leasingEnabled
      setLeasingEnabled(enabled)
      return { previous }
    },
    onSuccess: (_response, enabled) => {
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      queryClient.invalidateQueries({ queryKey: ['credential-health'] })
      toast.success(
        enabled
          ? 'Account is now available to agents.'
          : 'Account reserved. Existing leases revoked; Hive will issue no new agent leases.',
      )
    },
    onError: (_error, _enabled, context) => {
      setLeasingEnabled(context?.previous ?? serverLeasingEnabled)
      toast.error('Could not update agent leasing.')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => tokenPoolsApi.deleteEntry(pool.id, entry.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      queryClient.invalidateQueries({ queryKey: ['credential-health'] })
      toast.success(`Token "${entry.label}" deleted from Hive.`)
    },
    onError: (error) => toast.error(error.response?.data?.error || 'Could not delete token.'),
  })

  const checkMutation = useMutation({
    mutationFn: () => tokenPoolsApi.checkEntry(pool.id, entry.id, {}),
    onSuccess: (resp) => {
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      queryClient.invalidateQueries({ queryKey: ['credential-health'] })
      const c = resp?.data?.check || {}
      const outcome = c.outcome || 'unknown'
      const detail = c.detail || ''
      if (outcome === 'ok') {
        toast.success(`Check OK${detail ? ` — ${detail}` : ''}. Cooldown cleared if usage is back.`)
      } else if (outcome === 'rate_limited' || outcome === 'cooling') {
        const eta = fmtCountdown(c.cooldown_remaining_seconds)
        toast.error(
          `Still limited${eta ? ` · active in ${eta}` : ''}${detail ? ` — ${detail}` : ''}`,
        )
      } else if (outcome === 'auth_failed') {
        toast.error(`Auth failed — re-auth needed${detail ? ` — ${detail}` : ''}`)
      } else {
        toast.success(`Check: ${outcome}${detail ? ` — ${detail}` : ''}`)
      }
    },
    onError: (error) => toast.error(error.response?.data?.error || 'Check failed.'),
  })

  return (
    <tr className="border-b border-gray-100 last:border-0 dark:border-gray-700/60">
      <td className="p-3">
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          className="w-full rounded border border-transparent bg-transparent px-2 py-1 font-medium hover:border-gray-300 focus:border-primary-500 focus:outline-none dark:hover:border-gray-600"
        />
        <div className="font-mono text-xs text-gray-400" title={entry.label}>
          {entry.token_hint || (entry.fingerprint_prefix ? `fp:${entry.fingerprint_prefix}` : '—')}
        </div>
        {isCodexManagedEntry && (
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
            <span>Account: {entry.codex_account_email || entry.label}</span>
            {(effectiveStatus || codexStatus) && (
              <span className={clsx('rounded-full px-2 py-0.5 font-medium', codexStatusClass(effectiveStatus || codexStatus))}>
                {cooling ? 'cooling' : codexStatus || effectiveStatus}
                {cooling && fmtCountdown(coolRemaining) ? ` · ${fmtCountdown(coolRemaining)}` : ''}
              </span>
            )}
            <span>Last refresh: {fmtDateTime(entry.codex_last_refresh_at)}</span>
            <span>Access exp: {fmtDateTime(entry.codex_access_expires_at)}</span>
          </div>
        )}
        {isGrokManagedEntry && (
          <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Account: {entry.account_email || entry.label.replace(/^org-xai-/, '')}
          </div>
        )}
        {!isCodexManagedEntry && cooling && (
          <div className="mt-1">
            <span className={clsx('rounded-full px-2 py-0.5 text-xs font-medium', codexStatusClass('cooling'))}>
              cooling · {fmtCountdown(coolRemaining) || '…'}
            </span>
          </div>
        )}
      </td>
      <td className="p-3">
        <input
          value={tier}
          onChange={(e) => setTier(e.target.value)}
          placeholder="default"
          className="w-24 rounded border border-gray-300 bg-white px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
        />
      </td>
      <td className="p-3">
        <input
          value={priority}
          onChange={(e) => setPriority(e.target.value)}
          type="number"
          className="w-20 rounded border border-gray-300 bg-white px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
        />
      </td>
      <td className="p-3 text-sm text-gray-500 dark:text-gray-400">
        <div>{fmtDateTime(entry.cooldown_until)}</div>
        {cooling && (
          <div className="mt-0.5 text-xs font-medium text-amber-600 dark:text-amber-400">
            active in {fmtCountdown(coolRemaining)}
          </div>
        )}
        {!cooling && entry.cooldown_until && (
          <div className="mt-0.5 text-xs text-green-600 dark:text-green-400">window elapsed</div>
        )}
      </td>
      <td className="p-3 text-sm text-gray-500 dark:text-gray-400">
        <div>{entry.usage_count || 0} uses</div>
        <div className="text-xs">last: {fmtDateTime(entry.last_used_at)}</div>
        {(entry.quota_snapshot?.windows || []).length > 0 && (
          <div className="mt-1 space-y-1">
            {entry.quota_snapshot.windows.map((w) => {
              const left = w.left_percent != null ? Number(w.left_percent)
                : w.used_percent != null ? 100 - Number(w.used_percent) : null
              return (
                <div key={w.id || w.label} className="text-[10px]">
                  <div className="flex justify-between gap-2">
                    <span>{w.label || w.id}</span>
                    <span>{left != null ? `${left.toFixed(0)}% left` : '—'}</span>
                  </div>
                  <div className="h-1 w-full rounded-full bg-gray-200 dark:bg-gray-700">
                    <div
                      className={clsx(
                        'h-full rounded-full',
                        left == null ? 'bg-gray-400' : left > 40 ? 'bg-green-500' : left > 15 ? 'bg-amber-500' : 'bg-red-500',
                      )}
                      style={{ width: `${Math.max(0, Math.min(100, left ?? 0))}%` }}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </td>
      <td className="p-3 text-sm text-gray-500 dark:text-gray-400">
        {entry.last_status || (entry.last_rate_limit_at ? 'rate-limited' : '—')}
      </td>
      <td className="p-3">
        <span className={clsx('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium', leasingEnabled ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300' : 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300')}>
          {leasingEnabled ? <ToggleRight className="h-3.5 w-3.5" /> : <ToggleLeft className="h-3.5 w-3.5" />}
          {leasingEnabled ? 'Available to agents' : 'Personal reserve'}
        </span>
      </td>
      <td className="p-3 text-right">
        <div className="flex flex-wrap justify-end gap-2">
          <button
            type="button"
            title="Re-check provider (Codex/Grok: OAuth refresh; Claude OAT: observational). Clears cool if usage is back."
            disabled={checkMutation.isPending}
            onClick={() => checkMutation.mutate()}
            className="inline-flex items-center gap-1 rounded-lg border border-transparent px-2 py-1 text-xs text-gray-400 transition-colors hover:border-amber-400/40 hover:text-amber-500 disabled:cursor-not-allowed disabled:opacity-40 dark:hover:text-amber-300"
          >
            {checkMutation.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
            Check now
          </button>
          {isCodexManagedEntry && (
            <Link
              to={(entry.codex_reauth_url || `/hive/credentials/codex/re-auth?email=${encodeURIComponent(entry.codex_account_email || '')}`).replace(/^\/hive(?=\/)/, '')}
              className="rounded-lg border border-primary-200 px-2 py-1 text-xs text-primary-700 hover:bg-primary-50 dark:border-primary-900 dark:text-primary-300 dark:hover:bg-primary-900/20"
            >
              Re-auth
            </Link>
          )}
          <button
            type="button"
            disabled={!dirty || updateMutation.isPending}
            onClick={() => updateMutation.mutate({ label, tier, priority: Number(priority) || 0 })}
            className="rounded-lg border border-gray-300 px-2 py-1 text-xs hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-600 dark:hover:bg-gray-700"
          >
            Save
          </button>
          <button
            type="button"
            disabled={leasingMutation.isPending}
            onClick={() => {
              if (!leasingEnabled) {
                leasingMutation.mutate(true)
                return
              }
              if (window.confirm(
                `Reserve "${entry.account_email || entry.label}" for personal use?\n\n`
                + 'Hive will stop issuing new agent leases. Existing leases expire naturally.',
              )) leasingMutation.mutate(false)
            }}
            className={clsx(
              'rounded-lg border px-2 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40',
              leasingEnabled
                ? 'border-blue-200 text-blue-700 hover:bg-blue-50 dark:border-blue-900 dark:text-blue-300 dark:hover:bg-blue-900/20'
                : 'border-emerald-200 text-emerald-700 hover:bg-emerald-50 dark:border-emerald-900 dark:text-emerald-300 dark:hover:bg-emerald-900/20',
            )}
          >
            {leasingEnabled ? 'Reserve' : 'Allow agents'}
          </button>
          <button
            type="button"
            disabled={deleteMutation.isPending}
            onClick={() => {
              const confirmation = window.prompt(
                `Permanently remove "${entry.label}" from Hive?\n\n`
                + 'This scrubs the encrypted credential while retaining audit and lease history. '
                + 'It does not revoke the account at the provider.\n\n'
                + `Type ${entry.label} to confirm.`,
              )
              if (confirmation === entry.label) deleteMutation.mutate()
            }}
            className="inline-flex items-center gap-1 rounded-lg border border-transparent px-2 py-1 text-xs text-gray-400 transition-colors hover:border-red-500/40 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {deleteMutation.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
            Delete
          </button>
        </div>
      </td>
    </tr>
  )
}


// Shared device-code auth panel for OAuth pools (Codex OpenAI + xAI Grok).
// Stateless server-side: client holds device code + user code and polls.
export function deviceAuthErrorMessage(err, fallback = 'Device auth failed.') {
  return err?.response?.data?.error
    || err?.response?.data?.detail
    || err?.message
    || fallback
}

export function retryableDeviceAuthPollError(err) {
  const providerVerdict = err?.response?.data?.retryable
  if (providerVerdict === true) return true
  if (providerVerdict === false) return false
  const status = Number(err?.response?.status || 0)
  return !status || status === 408 || status === 425 || status === 429 || status >= 500
}

function DeviceAuthPanel({
  title,
  helpText,
  startLabel,
  startApi,
  pollApi,
  buildPollBody,
  successLabel = 'account',
  autoStart = false,
  sessionKey = 'default',
  children = null,
}) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [session, setSession] = useState(null) // {device_auth_id|device_code, user_code, verification_url, interval}
  const [result, setResult] = useState(null)   // {email, created}
  const [error, setError] = useState('')
  const [pollWarning, setPollWarning] = useState('')
  const pollTimer = useRef(null)
  const deadline = useRef(0)
  const autoStarted = useRef(false)

  const stopPolling = () => {
    if (pollTimer.current) clearTimeout(pollTimer.current)
    pollTimer.current = null
  }
  useEffect(() => stopPolling, [])

  const startMutation = useMutation({
    mutationFn: () => startApi(),
    onSuccess: ({ data }) => {
      setError('')
      setPollWarning('')
      setResult(null)
      setSession(data)
      const expiresMs = Math.max(60, Number(data.expires_in) || 15 * 60) * 1000
      deadline.current = Date.now() + expiresMs
      schedulePoll(data, (data.interval || 3) * 1000)
    },
    onError: (err) => setError(deviceAuthErrorMessage(err, 'Could not start device auth.')),
  })

  useEffect(() => {
    stopPolling()
    deadline.current = 0
    autoStarted.current = false
    setSession(null)
    setResult(null)
    setError('')
    setPollWarning('')
  }, [sessionKey])

  useEffect(() => {
    if (!autoStart || autoStarted.current || session || result || startMutation.isPending) return
    autoStarted.current = true
    startMutation.mutate()
  }, [autoStart])

  function schedulePoll(sess, delayMs) {
    stopPolling()
    pollTimer.current = setTimeout(() => runPoll(sess, delayMs), delayMs)
  }

  async function runPoll(sess, delayMs) {
    if (Date.now() > deadline.current) {
      setSession(null)
      setPollWarning('')
      setError('Device auth timed out. Start again.')
      return
    }
    try {
      const { data } = await pollApi(buildPollBody(sess))
      if (data.status === 'authorized') {
        setSession(null)
        setPollWarning('')
        setResult(data)
        queryClient.invalidateQueries({ queryKey: ['token-pools'] })
        toast.success(`${successLabel} ${data.email} ${data.created ? 'added' : 're-authenticated'}.`)
        return
      }
      setPollWarning('')
      const next = data.status === 'slow_down' ? Math.min(delayMs * 2, 30000) : delayMs
      schedulePoll(sess, next)
    } catch (err) {
      if (retryableDeviceAuthPollError(err)) {
        const next = Math.min(Math.max(delayMs * 2, 3000), 30000)
        setPollWarning('Authorization check was interrupted; retrying automatically…')
        schedulePoll(sess, next)
        return
      }
      setSession(null)
      setPollWarning('')
      setError(deviceAuthErrorMessage(err))
    }
  }

  return (
    <div className="mt-4 rounded-lg border border-primary-200 bg-primary-50/50 p-3 dark:border-primary-900 dark:bg-primary-950/30">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm">
          <span className="font-medium">{title}</span>
          <span className="ml-2 text-gray-500 dark:text-gray-400">{helpText}</span>
        </div>
        <button
          type="button"
          onClick={() => startMutation.mutate()}
          disabled={startMutation.isPending || !!session}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary-600 px-3 py-2 text-sm font-medium text-white hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {startMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
          {startLabel}
        </button>
      </div>
      {typeof children === 'function' ? children({ inProgress: !!session }) : children}

      {session && (
        <div className="mt-3 rounded-lg border border-gray-200 bg-white p-4 text-sm dark:border-gray-700 dark:bg-gray-900">
          <p>
            1. Open{' '}
            <a href={session.verification_url} target="_blank" rel="noreferrer" className="font-medium text-primary-600 underline">
              {session.verification_url}
            </a>{' '}
            and sign in to the account you want to (re-)authorize.
          </p>
          <p className="mt-2">2. Enter this code:</p>
          <div className="mt-1 inline-block rounded-lg bg-gray-100 px-4 py-2 font-mono text-xl tracking-widest dark:bg-gray-800">
            {session.user_code}
          </div>
          <p className="mt-3 flex items-center gap-2 text-gray-500 dark:text-gray-400">
            <Loader2 className="h-4 w-4 animate-spin" /> Waiting for authorization…
          </p>
          {pollWarning && (
            <p className="mt-2 text-amber-700 dark:text-amber-300">{pollWarning}</p>
          )}
        </div>
      )}

      {result && (
        <p className="mt-3 text-sm text-green-700 dark:text-green-400">
          Authorized <span className="font-medium">{result.email}</span> — token encrypted into your personal pool
          ({result.created ? 'new entry' : 'rotated in place'})
          {result.available_to_agents === false ? ' · personal reserve' : ''}
          {result.available_to_agents === true ? ' · available to agents' : ''}.
        </p>
      )}
      {error && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{error}</p>}
    </div>
  )
}

function CodexDeviceAuthPanel({ target = null, autoStart = false }) {
  const targetEmail = target?.email || ''
  return (
    <DeviceAuthPanel
      title="Codex device auth"
      helpText={
        targetEmail
          ? `Re-auth requested for ${targetEmail}; use a private/incognito OpenAI tab or sign out of any other ChatGPT account first.`
          : 'Add or re-auth an org ChatGPT/Codex account in the browser — no host shell.'
      }
      startLabel={targetEmail ? 'Start re-auth' : 'Add / re-auth Codex account'}
      startApi={() => codexDeviceAuthApi.start()}
      pollApi={(body) => codexDeviceAuthApi.poll(body)}
      buildPollBody={(sess) => ({
        device_auth_id: sess.device_auth_id,
        user_code: sess.user_code,
        ...(target?.poolId && target?.entryId ? {
          target_pool_id: target.poolId,
          target_entry_id: target.entryId,
        } : {}),
      })}
      successLabel="Codex account"
      autoStart={autoStart}
      sessionKey={target?.entryId || 'codex-add'}
    />
  )
}

function GrokDeviceAuthPanel({ autoStart = false }) {
  const [availableToAgents, setAvailableToAgents] = useState(true)
  const availableToAgentsRef = useRef(true)
  const setLeasing = (value) => {
    availableToAgentsRef.current = value
    setAvailableToAgents(value)
  }
  return (
    <DeviceAuthPanel
      title="Grok device auth"
      helpText="Connect a SuperGrok / Grok Build account securely through xAI. No raw token required."
      startLabel="Connect Grok account"
      startApi={() => grokDeviceAuthApi.start()}
      pollApi={(body) => grokDeviceAuthApi.poll(body)}
      buildPollBody={(sess) => ({
        device_code: sess.device_code || sess.device_auth_id,
        device_auth_id: sess.device_auth_id || sess.device_code,
        available_to_agents: availableToAgentsRef.current,
      })}
      successLabel="Grok account"
      autoStart={autoStart}
    >
      {({ inProgress }) => (
        <label className="mt-3 flex max-w-2xl items-start gap-3 rounded-lg border border-primary-200/70 bg-white/70 p-3 text-sm dark:border-primary-900 dark:bg-gray-900/70">
          <input
            type="checkbox"
            checked={availableToAgents}
            disabled={inProgress}
            onChange={(event) => setLeasing(event.target.checked)}
            className="mt-0.5 h-4 w-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
          />
          <span>
            <span className="block font-medium text-gray-900 dark:text-gray-100">Available to agents immediately</span>
            <span className="block text-xs text-gray-500 dark:text-gray-400">
              Clear this to keep the account as a personal reserve. You can change it later per account.
            </span>
          </span>
        </label>
      )}
    </DeviceAuthPanel>
  )
}

function TokenPoolCard({ pool, codexReauthTarget = null, isStaff = false }) {
  const isCodexPool = pool.provider === 'openai'
  const isGrokPool = pool.provider === 'xai'
  const isGrokPersonalPool = pool.owner_kind === 'user' && isGrokPool
  const isClaudeOrgPool = pool.owner_kind === 'org' && pool.provider === 'anthropic'
  const isGooglePool = pool.provider === 'google'
  return (
    <div className="hv-panel p-4">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold capitalize">{pool.provider === 'xai' ? 'xAI Grok' : pool.provider === 'google' ? 'Google Antigravity' : pool.provider}</h2>
            <PoolBadge pool={pool} />
          </div>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {pool.display_name || 'Unnamed pool'} · {pool.active_entry_count || 0}/{pool.entry_count || 0} available to agents
          </p>
        </div>
        <div className="text-xs text-gray-500 dark:text-gray-400">Updated {fmtDateTime(pool.updated_at)}</div>
      </div>

      {(isStaff || codexReauthTarget) && isCodexPool && (
        <div id={codexReauthTarget ? 'codex-re-auth-panel' : undefined}>
          <CodexDeviceAuthPanel target={codexReauthTarget} autoStart={!!codexReauthTarget?.entryId} />
        </div>
      )}
      {isGrokPersonalPool && <GrokDeviceAuthPanel />}
      {isClaudeOrgPool && <ClaudeOAuthPanel />}
      {isGooglePool && <AntigravityOAuthPanel />}

      {isGrokPool ? (
        <details className="mt-4 rounded-lg border border-gray-200 bg-gray-50/60 p-3 dark:border-gray-700 dark:bg-gray-900/30">
          <summary className="cursor-pointer text-sm font-medium text-gray-600 dark:text-gray-300">
            Advanced: add a raw xAI credential
          </summary>
          <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            Prefer device auth above. Manual credentials are intended for recovery or service-account integrations.
          </p>
          <AddTokenForm pool={pool} />
        </details>
      ) : (
        <AddTokenForm pool={pool} />
      )}

      {(pool.entries || []).length === 0 ? (
        <div className="mt-4 rounded-lg border border-dashed border-gray-300 p-5 text-center text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">
          No tokens in this pool yet.
        </div>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[980px] text-left text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-xs uppercase text-gray-500 dark:border-gray-700 dark:text-gray-400">
                <th className="p-3 font-medium">Label</th>
                <th className="p-3 font-medium">Tier</th>
                <th className="p-3 font-medium">Priority</th>
                <th className="p-3 font-medium">Cooldown</th>
                <th className="p-3 font-medium">Usage</th>
                <th className="p-3 font-medium">Last status</th>
                <th className="p-3 font-medium">Agent leasing</th>
                <th className="p-3" />
              </tr>
            </thead>
            <tbody>
              {pool.entries.map((entry) => <EntryRow key={entry.id} pool={pool} entry={entry} />)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function TokenPools() {
  const { isStaff } = useHive()
  const location = useLocation()
  const query = useMemo(() => new URLSearchParams(location.search), [location.search])
  const codexReauthEmail = query.get('email') || ''
  const codexReauthPoolId = query.get('pool') || ''
  const codexReauthEntryId = query.get('entry') || ''
  const codexReauthScope = query.get('scope') === 'org' ? 'org' : 'user'
  const isCodexReauthDeepLink = location.pathname.includes('/credentials/codex/re-auth') || !!codexReauthEmail
  const [scope, setScope] = useState(isCodexReauthDeepLink ? codexReauthScope : 'user')
  useEffect(() => {
    if (isCodexReauthDeepLink) setScope(codexReauthScope)
  }, [isCodexReauthDeepLink, codexReauthScope])
  const params = useMemo(() => (scope === 'org' ? { scope: 'org' } : { scope: 'user' }), [scope])
  const { data: pools = [], isLoading, isError, refetch } = useQuery({
    queryKey: ['token-pools', scope],
    queryFn: () => tokenPoolsApi.list(params).then((r) => r.data),
    retry: false,
  })
  const hasOpenAiPool = pools.some((pool) => pool.provider === 'openai')
  const hasXaiPool = pools.some((pool) => pool.provider === 'xai')
  const hasGooglePool = pools.some((pool) => pool.provider === 'google')

  useEffect(() => {
    if (!isCodexReauthDeepLink || isLoading || !pools.length) return
    const frame = requestAnimationFrame(() => {
      document.getElementById('codex-re-auth-panel')?.scrollIntoView({ block: 'center' })
    })
    return () => cancelAnimationFrame(frame)
  }, [isCodexReauthDeepLink, isLoading, pools])

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="hv-kicker mb-1">Credentials · pools</div>
          <h1 className="hv-page-title flex items-center gap-2.5">
            <SlidersHorizontal className="h-6 w-6 text-primary-500" />
            Token Pools
          </h1>
          <p className="max-w-3xl text-gray-500 dark:text-gray-400">
            Manage HIVE Token Coordinator credentials. Tokens are encrypted at rest; ownership is derived from your sign-in token, not from form input.
            Cooling rows show a live countdown; <strong>Check now</strong> re-verifies Codex/Grok via OAuth refresh (clears cool when usage is back).
          </p>
          <Link
            to="/subscriptions"
            className="mt-2 inline-flex items-center gap-1.5 text-sm font-medium text-primary-600 hover:underline dark:text-primary-400"
          >
            <Activity className="h-4 w-4" />
            Multi-provider health board (Claude · Codex · Grok · Antigravity)
          </Link>
        </div>
        {isStaff && (
          <div className="hv-panel inline-flex rounded-lg p-1">
            {['user', 'org'].map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => setScope(item)}
                className={clsx('rounded-md px-3 py-1.5 text-sm font-medium capitalize', scope === item ? 'bg-gradient-to-b from-amber-400 to-amber-600 text-gray-950' : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-white/[0.06]')}
              >
                {item === 'org' ? 'Org pool' : 'My pool'}
              </button>
            ))}
          </div>
        )}
      </div>

      {!isLoading && !hasGooglePool && (
        <AntigravityOAuthPanel />
      )}
      {isStaff && !isLoading && !hasOpenAiPool && (
        <CodexDeviceAuthPanel />
      )}
      {scope === 'user' && !isLoading && !hasXaiPool && (
        <GrokDeviceAuthPanel />
      )}

      <CreatePoolForm scope={scope} isStaff={isStaff} />

      {isError ? (
        <ErrorState message="Could not load token pools." onRetry={() => refetch()} />
      ) : pools.length === 0 && !isLoading ? (
        <EmptyState icon={KeyRound} title="No token pools yet" message="Create a provider pool, then add encrypted tokens for Hive to rotate through." />
      ) : (
        <div className="space-y-4">
          {pools.map((pool) => (
            <TokenPoolCard
              key={pool.id}
              pool={pool}
              isStaff={isStaff}
              codexReauthTarget={
                pool.provider === 'openai'
                && (
                  (codexReauthPoolId && pool.id === codexReauthPoolId)
                  || (!codexReauthPoolId && codexReauthEmail)
                )
                  ? {
                    poolId: pool.id,
                    entryId: codexReauthEntryId || (
                      pool.entries || []
                    ).find((entry) => (
                      entry.codex_account_email?.toLowerCase() === codexReauthEmail.toLowerCase()
                      || entry.label?.toLowerCase() === `org-openai-${codexReauthEmail.toLowerCase()}`
                    ))?.id || '',
                    email: codexReauthEmail,
                  }
                  : null
              }
            />
          ))}
        </div>
      )}
    </div>
  )
}
