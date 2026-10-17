import { useState, useCallback, useRef, useEffect } from 'react'
import { AlertTriangle, KeyRound, Loader2, Plus, ShieldAlert, Trash2, ExternalLink, RefreshCw, CheckCircle2, XCircle } from 'lucide-react'
import { useHive } from '../contexts/HiveContext'
import { useCredentialGrants, useCredentialMutations, usePersonalOpenAIPool, useCodexDeviceAuth } from '../hooks/useAgents'
import { useToast } from '../components/Toast'

const emptyForm = {
  scope: 'team',
  team_slug: 'merge',
  agent: '',
  provider: 'github',
  purpose: 'merge-identity',
  secret_ref: '',
  fingerprint: '',
  requires_team_credential_eligible: true,
}

function CredentialForm({ onSubmit, onCancel, busy }) {
  const [form, setForm] = useState(emptyForm)
  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }))

  function submit(e) {
    e.preventDefault()
    const payload = {
      scope: form.scope,
      provider: form.provider,
      purpose: form.purpose.trim(),
      secret_ref: form.secret_ref.trim(),
      fingerprint: form.fingerprint.trim(),
      requires_team_credential_eligible: form.requires_team_credential_eligible,
    }
    if (form.scope === 'team') payload.team_slug = form.team_slug.trim()
    if (form.scope === 'agent' && form.agent.trim()) payload.agent = form.agent.trim()
    onSubmit(payload)
  }

  return (
    <form onSubmit={submit} className="grid gap-3 rounded-xl border border-dashed border-amber-300 bg-amber-50/50 p-4 dark:border-amber-800 dark:bg-amber-900/10 md:grid-cols-2">
      <label className="text-xs text-gray-500 dark:text-gray-400">Scope
        <select className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100" value={form.scope} onChange={(e) => set('scope', e.target.value)}>
          <option value="team">Team</option>
          <option value="agent">Agent</option>
        </select>
      </label>
      {form.scope === 'team' ? (
        <label className="text-xs text-gray-500 dark:text-gray-400">Team slug
          <input className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100" value={form.team_slug} onChange={(e) => set('team_slug', e.target.value)} placeholder="merge" />
        </label>
      ) : (
        <label className="text-xs text-gray-500 dark:text-gray-400">FleetAgent UUID
          <input className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100" value={form.agent} onChange={(e) => set('agent', e.target.value)} placeholder="agent UUID" />
        </label>
      )}
      <label className="text-xs text-gray-500 dark:text-gray-400">Provider
        <select className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100" value={form.provider} onChange={(e) => set('provider', e.target.value)}>
          <option value="github">GitHub</option>
          <option value="docker">Docker</option>
          <option value="kubeconfig">Kubeconfig</option>
          <option value="generic-env">Generic env</option>
        </select>
      </label>
      <label className="text-xs text-gray-500 dark:text-gray-400">Purpose
        <input className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100" value={form.purpose} onChange={(e) => set('purpose', e.target.value)} placeholder="merge-identity" />
      </label>
      <label className="text-xs text-gray-500 dark:text-gray-400 md:col-span-2">Broker secret ref (write-only; never displayed again)
        <input className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100" value={form.secret_ref} onChange={(e) => set('secret_ref', e.target.value)} placeholder="broker://github/merge-team/kai2574" />
      </label>
      <label className="text-xs text-gray-500 dark:text-gray-400">Fingerprint / last4
        <input className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100" value={form.fingerprint} onChange={(e) => set('fingerprint', e.target.value)} placeholder="ghp_...abcd" />
      </label>
      <label className="flex items-center gap-2 pt-6 text-sm text-gray-700 dark:text-gray-300">
        <input type="checkbox" checked={form.requires_team_credential_eligible} onChange={(e) => set('requires_team_credential_eligible', e.target.checked)} />
        Require team_credential_eligible
      </label>
      <div className="flex gap-2 md:col-span-2">
        <button type="submit" disabled={busy} className="rounded bg-primary-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-primary-700 disabled:opacity-50">Create grant</button>
        <button type="button" onClick={onCancel} className="rounded border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-700">Cancel</button>
      </div>
    </form>
  )
}

function GrantCard({ grant, onRevoke, busy }) {
  const target = grant.scope === 'team' ? grant.team_slug : (grant.agent || 'agent')
  return (
    <div className="hv-panel p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-gray-900 dark:text-gray-100">{grant.provider} / {grant.purpose}</span>
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-700 dark:text-gray-300">{grant.scope}: {target}</span>
            <span className={grant.is_active ? 'rounded-full bg-green-100 px-2 py-0.5 text-xs text-green-700 dark:bg-green-900/30 dark:text-green-300' : 'rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-500 dark:bg-gray-700'}>
              {grant.is_active ? 'active' : 'revoked'}
            </span>
          </div>
          <div className="mt-2 grid gap-1 text-xs text-gray-500 dark:text-gray-400 sm:grid-cols-2">
            <div>Secret: {grant.has_secret_ref ? 'broker-backed (redacted)' : 'missing'}</div>
            <div>Fingerprint: {grant.fingerprint || '—'}</div>
            <div>Health: {grant.health || 'unknown'}{grant.health_message ? ` — ${grant.health_message}` : ''}</div>
            <div>Last applied: {grant.last_applied_at || 'never'}</div>
            <div>Audit events: {grant.audit_count ?? 0}</div>
            <div>Eligible flag required: {grant.requires_team_credential_eligible ? 'yes' : 'no'}</div>
          </div>
        </div>
        {grant.is_active && (
          <button type="button" onClick={() => onRevoke(grant.id)} disabled={busy} className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-40 dark:hover:bg-red-900/20" title="Revoke grant">
            <Trash2 className="h-4 w-4" />
          </button>
        )}
      </div>
    </div>
  )
}

function CodexDeviceAuthFlow({
  onDone,
  targetEmail = '',
  targetPoolId = '',
  targetEntryId = '',
  variant = 'add',
}) {
  const [step, setStep] = useState('idle') // idle | waiting | authorized | error
  const [userCode, setUserCode] = useState('')
  const [verificationUrl, setVerificationUrl] = useState('')
  const [intervalSec, setIntervalSec] = useState(3)
  const [errorMsg, setErrorMsg] = useState('')
  const [result, setResult] = useState(null)
  const pollTimer = useRef(null)
  const sessionRef = useRef(null)
  const deadline = useRef(0)
  const toast = useToast()
  const auth = useCodexDeviceAuth()

  const clearPoll = useCallback(() => {
    if (pollTimer.current) {
      clearTimeout(pollTimer.current)
      pollTimer.current = null
    }
  }, [])

  useEffect(() => {
    return () => clearPoll()
  }, [clearPoll])

  async function startFlow() {
    setStep('waiting')
    setErrorMsg('')
    setResult(null)
    try {
      const data = await auth.start.mutateAsync()
      const session = {
        device_auth_id: data.device_auth_id,
        user_code: data.user_code,
        interval: data.interval || 3,
      }
      sessionRef.current = session
      deadline.current = Date.now() + 15 * 60 * 1000
      setUserCode(data.user_code)
      setVerificationUrl(data.verification_url)
      setIntervalSec(session.interval)
      schedulePoll(session, session.interval)
    } catch (err) {
      setStep('error')
      setErrorMsg(err?.response?.data?.error || err?.message || 'Failed to start device auth')
    }
  }

  function schedulePoll(session, delaySec) {
    clearPoll()
    pollTimer.current = setTimeout(() => pollOnce(session, delaySec), delaySec * 1000)
  }

  async function pollOnce(session, delaySec) {
    if (Date.now() > deadline.current) {
      clearPoll()
      sessionRef.current = null
      deadline.current = 0
      setStep('error')
      setErrorMsg('Device auth timed out (15 min). Start again.')
      return
    }
    try {
      const data = await auth.poll.mutateAsync({
        device_auth_id: session.device_auth_id,
        user_code: session.user_code,
        ...(targetPoolId && targetEntryId ? {
          target_pool_id: targetPoolId,
          target_entry_id: targetEntryId,
        } : {}),
      })
      if (data.status === 'authorized') {
        clearPoll()
        sessionRef.current = null
        deadline.current = 0
        setStep('authorized')
        setResult(data)
        toast.success(`Codex account ${data.email || ''} ${variant === 'reauth' ? 're-authed' : 'added/re-authed'}`)
        onDone?.()
      } else if (data.status === 'slow_down') {
        const nextDelay = Math.min(delaySec * 2, 30)
        const nextSession = { ...session, interval: nextDelay }
        sessionRef.current = nextSession
        setIntervalSec(nextDelay)
        schedulePoll(nextSession, nextDelay)
      } else {
        schedulePoll(session, delaySec)
      }
    } catch (err) {
      clearPoll()
      sessionRef.current = null
      deadline.current = 0
      setStep('error')
      setErrorMsg(err?.response?.data?.error || err?.message || 'Poll failed')
    }
  }

  function cancel() {
    clearPoll()
    setStep('idle')
    setUserCode('')
    sessionRef.current = null
    deadline.current = 0
    setErrorMsg('')
  }

  if (step === 'idle') {
    return (
      <button type="button" onClick={startFlow} disabled={auth.start.isPending} className="inline-flex items-center gap-1.5 rounded-lg bg-primary-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-primary-700 disabled:opacity-50">
        {auth.start.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
        {variant === 'reauth' ? 'Re-auth' : 'Add Codex account'}
      </button>
    )
  }

  return (
    <div className="rounded-xl border border-dashed border-blue-300 bg-blue-50/50 p-4 dark:border-blue-800 dark:bg-blue-900/10">
      {step === 'waiting' && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <Loader2 className="h-5 w-5 animate-spin text-blue-600" />
            <span className="font-medium text-gray-900 dark:text-gray-100">Waiting for browser authorization…</span>
          </div>
          <div className="rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800">
            {targetEmail && (
              <p className="mb-2 rounded bg-amber-50 px-2 py-1 text-sm text-amber-800 dark:bg-amber-900/20 dark:text-amber-200">
                Re-auth requested for {targetEmail}; sign in to that ChatGPT account when OpenAI prompts.
              </p>
            )}
            <p className="text-sm text-gray-600 dark:text-gray-400">
              1. Open{' '}
              <a href={verificationUrl} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-medium text-primary-600 hover:underline">
                {verificationUrl} <ExternalLink className="h-3 w-3" />
              </a>
            </p>
            <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
              2. Enter code:{' '}
              <code className="rounded bg-gray-100 px-2 py-0.5 font-mono text-lg font-bold tracking-wider text-gray-900 dark:bg-gray-700 dark:text-gray-100">{userCode}</code>
            </p>
            <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
              3. Authorize the Shizuha Codex CLI app, then wait here — Hive will poll every {intervalSec}s and ingest the account once authorized.
            </p>
          </div>
          <button type="button" onClick={cancel} className="text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300">Cancel</button>
        </div>
      )}
      {step === 'authorized' && (
        <div className="flex items-center gap-2 text-green-700 dark:text-green-400">
          <CheckCircle2 className="h-5 w-5" />
          <span className="font-medium">Codex account authorized{result?.email ? ` (${result.email})` : ''}</span>
        </div>
      )}
      {step === 'error' && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-red-600">
            <XCircle className="h-5 w-5" />
            <span className="font-medium">Authorization failed</span>
          </div>
          <p className="text-sm text-red-500">{errorMsg}</p>
          <button type="button" onClick={cancel} className="text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300">Dismiss</button>
        </div>
      )}
    </div>
  )
}

function CodexAccountCard({ poolId, entry, onDone }) {
  const [reauthOpen, setReauthOpen] = useState(false)
  const label = entry.label || ''
  const email = entry.codex_account_email
    || (label.startsWith('org-openai-') ? label.slice('org-openai-'.length) : label)
  return (
    <div className="hv-panel p-4">
      <div className="flex flex-col gap-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-gray-900 dark:text-gray-100">{email}</span>
              <span className={entry.active ? 'rounded-full bg-green-100 px-2 py-0.5 text-xs text-green-700 dark:bg-green-900/30 dark:text-green-300' : 'rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-500 dark:bg-gray-700'}>
                {entry.active ? 'active' : 'disabled'}
              </span>
            </div>
            <div className="mt-2 grid gap-1 text-xs text-gray-500 dark:text-gray-400 sm:grid-cols-3">
              <div>Usage: {entry.usage_count ?? 0} calls</div>
              <div>Last used: {entry.last_used_at || 'never'}</div>
              <div>Last status: {entry.last_status || '—'}</div>
              <div>Fingerprint: {entry.fingerprint_prefix || '—'}</div>
              <div>Tier: {entry.tier || 'default'}</div>
              <div>Priority: {entry.priority ?? 0}</div>
            </div>
          </div>
          <button type="button" onClick={() => setReauthOpen((prev) => !prev)} className="inline-flex items-center gap-1.5 rounded-lg border border-primary-200 px-3 py-1.5 text-sm font-medium text-primary-700 hover:bg-primary-50 dark:border-primary-900 dark:text-primary-300 dark:hover:bg-primary-900/20">
            <RefreshCw className="h-4 w-4" />
            Re-auth
          </button>
        </div>
        {reauthOpen && (
          <CodexDeviceAuthFlow
            targetEmail={email}
            targetPoolId={poolId}
            targetEntryId={entry.id}
            variant="reauth"
            onDone={() => {
              setReauthOpen(false)
              onDone?.()
            }}
          />
        )}
      </div>
    </div>
  )
}

export default function Credentials() {
  const { isStaff } = useHive()
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState(false)
  const toast = useToast()
  const { data: grants, isLoading, isError, refetch } = useCredentialGrants({ enabled: isStaff })
  const mutations = useCredentialMutations()
  const { data: personalPool, isLoading: poolLoading, refetch: refetchPool } = usePersonalOpenAIPool({ enabled: isStaff })

  if (!isStaff) {
    return (
      <div className="flex flex-col items-center gap-3 py-20 text-center">
        <ShieldAlert className="h-10 w-10 text-gray-300 dark:text-gray-600" />
        <p className="text-gray-500 dark:text-gray-400">Credential grant management requires admin access.</p>
      </div>
    )
  }

  async function createGrant(payload) {
    setBusy(true)
    try {
      await mutations.create.mutateAsync(payload)
      toast.success('Credential grant created')
      setCreating(false)
    } catch (err) {
      toast.error(err?.response?.data?.error || err?.response?.data?.secret_ref || err?.message || 'Failed to create grant')
    } finally {
      setBusy(false)
    }
  }

  async function revokeGrant(id) {
    if (!window.confirm('Revoke this credential grant? The secret handle stays redacted and inactive for audit.')) return
    setBusy(true)
    try {
      await mutations.revoke.mutateAsync(id)
      toast.success('Credential grant revoked')
    } catch (err) {
      toast.error(err?.response?.data?.error || err?.message || 'Failed to revoke grant')
    } finally {
      setBusy(false)
    }
  }

  const codexEntries = personalPool?.entries?.filter(
    (e) => e.codex_account_email || e.label?.startsWith('org-openai-'),
  ) || []

  return (
    <div className="space-y-8">
      {/* ── Credential grants section ── */}
      <section className="space-y-5">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <div className="hv-kicker mb-1">Access · grants</div>
            <h1 className="hv-page-title flex items-center gap-2.5">
              <KeyRound className="h-6 w-6 text-amber-500" />
              Credential grants
            </h1>
            <p className="text-gray-500 dark:text-gray-400">
              Manage team/agent credential policy metadata. Secret handles are write-only; reads show redacted health and audit state.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button type="button" onClick={() => refetch()} className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-700">Refresh</button>
            <button type="button" onClick={() => setCreating(true)} className="inline-flex items-center gap-1.5 rounded-lg bg-primary-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-primary-700">
              <Plus className="h-4 w-4" /> New grant
            </button>
          </div>
        </div>

        {creating && <CredentialForm onSubmit={createGrant} onCancel={() => setCreating(false)} busy={busy} />}

        {isLoading && !grants && <div className="flex items-center gap-2 text-gray-500"><Loader2 className="h-4 w-4 animate-spin" /> Loading credential grants…</div>}
        {isError && (
          <div className="flex flex-col items-center gap-3 py-16 text-center">
            <AlertTriangle className="h-8 w-8 text-red-400" />
            <p className="text-gray-500">Failed to load credential grants.</p>
            <button type="button" onClick={() => refetch()} className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-700">Retry</button>
          </div>
        )}
        {grants && grants.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-16 text-center">
            <KeyRound className="h-10 w-10 text-gray-300 dark:text-gray-600" />
            <p className="text-gray-500 dark:text-gray-400">No credential grants yet. Add a Merge team GitHub grant to start.</p>
          </div>
        )}
        {grants && grants.length > 0 && (
          <div className="space-y-3">
            {grants.map((grant) => <GrantCard key={grant.id} grant={grant} onRevoke={revokeGrant} busy={busy} />)}
          </div>
        )}
      </section>

      {/* ── Codex / OpenAI accounts section (HIVE-581/HIVE-582) ── */}
      <section className="space-y-5 border-t border-gray-200 pt-8 dark:border-gray-700">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-xl font-bold">
              <ExternalLink className="h-5 w-5 text-blue-500" />
              Codex / OpenAI accounts
            </h2>
            <p className="text-gray-500 dark:text-gray-400">
              Your OpenAI/Codex OAuth accounts used by the fleet token coordinator. Add or re-auth accounts via the browser device-code flow; no host credentials are imported.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button type="button" onClick={() => refetchPool()} className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-700">
              <RefreshCw className="mr-1 inline h-3.5 w-3.5" /> Refresh
            </button>
            <CodexDeviceAuthFlow onDone={() => refetchPool()} />
          </div>
        </div>

        {poolLoading && !personalPool && <div className="flex items-center gap-2 text-gray-500"><Loader2 className="h-4 w-4 animate-spin" /> Loading Codex accounts…</div>}
        {!poolLoading && codexEntries.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-12 text-center">
            <ExternalLink className="h-10 w-10 text-gray-300 dark:text-gray-600" />
            <p className="text-gray-500 dark:text-gray-400">No Codex accounts added yet. Use the Add Codex account button to authorize an OpenAI account.</p>
          </div>
        )}
        {codexEntries.length > 0 && (
          <div className="space-y-3">
            {codexEntries.map((entry) => (
              <CodexAccountCard
                key={entry.id}
                poolId={personalPool.id}
                entry={entry}
                onDone={() => refetchPool()}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
