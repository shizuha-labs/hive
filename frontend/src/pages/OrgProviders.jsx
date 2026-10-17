import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  KeyRound, Loader2, Lock, Plus, RefreshCw, RotateCw, ShieldCheck,
  ShieldOff, Trash2, UserCog,
} from 'lucide-react'
import { useHive } from '../contexts/HiveContext'
import { useToast } from '../components/Toast'
import { EmptyState, ErrorState } from '../components/States'
import { orgProviderRefsApi } from '../services/api'

const STATUS_LABELS = {
  active: 'Active',
  verifying: 'Verifying',
  probation: 'Probation',
  disabled: 'Disabled',
  mismatch: 'Mismatch',
  invalid: 'Invalid',
  exhausted: 'Exhausted',
}

const STATUS_STYLES = {
  active: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
  verifying: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
  probation: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
  disabled: 'bg-gray-500/15 text-gray-600 dark:text-gray-400',
  mismatch: 'bg-rose-500/15 text-rose-600 dark:text-rose-400',
  invalid: 'bg-rose-500/15 text-rose-600 dark:text-rose-400',
  exhausted: 'bg-rose-500/15 text-rose-600 dark:text-rose-400',
}

const PROVIDER_TYPE_LABELS = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  openai_compatible: 'OpenAI-compatible',
}

// HIVE-2161 — canonical provider types have well-known endpoints; the Cortex
// endpoint_url is derived server-side when base_url is absent. Only
// openai_compatible credentials need an explicit base URL from the admin.
const NEEDS_BASE_URL = new Set(['openai_compatible'])

const EMPTY_FORM = { provider_type: 'openai', display_name: '', base_url: '', models: '', secret_key: '' }

function StatusBadge({ status }) {
  const label = STATUS_LABELS[status] || status
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_STYLES[status] || STATUS_STYLES.disabled}`}>
      {label}
    </span>
  )
}

function TypeBadge({ type }) {
  return (
    <span className="inline-flex items-center rounded-full bg-sky-500/10 text-sky-600 dark:text-sky-400 px-2.5 py-0.5 text-xs font-medium">
      {PROVIDER_TYPE_LABELS[type] || type}
    </span>
  )
}

const inputClass = 'w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2 text-sm'
const secondaryBtnClass = 'inline-flex items-center gap-1.5 rounded-lg border border-gray-300 dark:border-gray-700 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors disabled:opacity-50'
const primaryBtnClass = 'inline-flex items-center gap-2 rounded-lg bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 transition-colors'

/**
 * HIVE-2161 (slice 2) — add-provider form driving the LIVE Cortex listing
 * lifecycle via POST /v1/org-provider-refs/provision. The secret key is a
 * one-time relay: it is posted to Hive → Cortex (draft → submit → validate)
 * and never stored, logged, or rendered again.
 */
function AddProviderForm({ orgId, pending, error, onSubmit, onCancel }) {
  const [form, setForm] = useState(EMPTY_FORM)
  const needsBaseUrl = NEEDS_BASE_URL.has(form.provider_type)

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value })

  const handleSubmit = (e) => {
    e.preventDefault()
    onSubmit({
      org_id: orgId,
      provider_type: form.provider_type,
      display_name: form.display_name.trim(),
      base_url: needsBaseUrl ? form.base_url.trim() : '',
      models: form.models.split(',').map((s) => s.trim()).filter(Boolean),
      secret_key: form.secret_key.trim(),
    })
  }

  return (
    <form onSubmit={handleSubmit} className="rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-5 space-y-4" data-testid="add-provider-form">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium mb-1" htmlFor="provider-type">Provider type</label>
          <select id="provider-type" value={form.provider_type} onChange={set('provider_type')} className={inputClass}>
            <option value="openai">OpenAI</option>
            <option value="anthropic">Anthropic</option>
            <option value="openai_compatible">OpenAI-compatible</option>
          </select>
        </div>
        <div>
          <label className="block text-sm font-medium mb-1" htmlFor="display-name">Display name</label>
          <input id="display-name" type="text" value={form.display_name} onChange={set('display_name')} placeholder="Acme OpenAI key" required className={inputClass} />
        </div>
      </div>
      {needsBaseUrl && (
        <div>
          <label className="block text-sm font-medium mb-1" htmlFor="base-url">Base URL</label>
          <input id="base-url" type="url" value={form.base_url} onChange={set('base_url')} placeholder="https://llm.internal.acme.com/v1" required className={inputClass} />
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
            Required for OpenAI-compatible endpoints. Canonical OpenAI/Anthropic use their well-known endpoints.
          </p>
        </div>
      )}
      <div>
        <label className="block text-sm font-medium mb-1" htmlFor="models">Models (comma-separated)</label>
        <input id="models" type="text" value={form.models} onChange={set('models')} placeholder="gpt-5.6, claude-sonnet-4" required className={inputClass} />
      </div>
      <div>
        <label className="block text-sm font-medium mb-1" htmlFor="secret-key">Provider API key (one-time — runs live probe, never stored)</label>
        <input id="secret-key" type="password" value={form.secret_key} onChange={set('secret_key')} placeholder="sk-… (relayed to Cortex for validation, then discarded)" required autoComplete="new-password" className={inputClass} />
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
          Adding runs the Cortex lifecycle (draft → submit → 1-token probe). Hive never stores or renders the key.
        </p>
      </div>
      {error && (
        <p className="text-sm text-rose-600 dark:text-rose-400" role="alert">{error}</p>
      )}
      <div className="flex gap-2">
        <button type="submit" disabled={pending} className={primaryBtnClass}>
          {pending && <Loader2 className="h-4 w-4 animate-spin" />}
          Add credential
        </button>
        <button type="button" onClick={onCancel} className="rounded-lg border border-gray-300 dark:border-gray-700 px-4 py-2 text-sm">
          Cancel
        </button>
      </div>
    </form>
  )
}

/**
 * One provider credential card: lifecycle actions (validate / rotate /
 * disable / remove) plus the slice-2 editors — default models for this
 * provider runtime and the same-org agent override editor.
 *
 * Mirror edits (default models, overrides) go through the HIVE-1889 upsert
 * endpoint; the current status is echoed so an edit never re-activates a
 * disabled/invalid credential (the upsert defaults status to active when
 * omitted).
 */
function RefCard({ cred, refs, busy, onValidate, onRotate, onDisable, onRemove, onMirrorUpdate }) {
  const [showRotate, setShowRotate] = useState(false)
  const [rotateKey, setRotateKey] = useState('')
  const [showModels, setShowModels] = useState(false)
  const [modelsText, setModelsText] = useState('')
  const [showOverrides, setShowOverrides] = useState(false)
  const [overrideAgent, setOverrideAgent] = useState('')
  const [overrideProvider, setOverrideProvider] = useState('')
  const [overrideModel, setOverrideModel] = useState('')

  const overrides = cred.per_agent_overrides || {}
  const providerLabel = (pid) => {
    const match = refs.find((r) => r.cortex_provider_id === pid)
    return match ? (match.display_name || match.cortex_provider_id) : pid
  }

  const startModelsEdit = () => {
    setModelsText((cred.default_models || []).join(', '))
    setShowModels(true)
  }
  const saveModels = () => {
    onMirrorUpdate({
      org_id: cred.org_id,
      cortex_provider_id: cred.cortex_provider_id,
      status: cred.status,
      default_models: modelsText.split(',').map((s) => s.trim()).filter(Boolean),
      per_agent_overrides: overrides,
    })
    setShowModels(false)
  }

  const addOverride = () => {
    const agentId = overrideAgent.trim()
    if (!agentId || !overrideProvider) return
    const entry = { provider_credential_id: overrideProvider }
    const model = overrideModel.trim()
    if (model) entry.default_model_id = model
    onMirrorUpdate({
      org_id: cred.org_id,
      cortex_provider_id: cred.cortex_provider_id,
      status: cred.status,
      default_models: cred.default_models || [],
      per_agent_overrides: { ...overrides, [agentId]: entry },
    })
    setOverrideAgent('')
    setOverrideProvider('')
    setOverrideModel('')
  }

  const removeOverride = (agentId) => {
    const next = { ...overrides }
    delete next[agentId]
    onMirrorUpdate({
      org_id: cred.org_id,
      cortex_provider_id: cred.cortex_provider_id,
      status: cred.status,
      default_models: cred.default_models || [],
      per_agent_overrides: next,
    })
  }

  const submitRotate = () => {
    if (!rotateKey.trim()) return
    onRotate({ id: cred.id, secret_key: rotateKey.trim() })
    setRotateKey('')
    setShowRotate(false)
  }

  return (
    <div className="rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-5 space-y-3" data-testid="provider-ref-card">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-semibold text-sm">{cred.display_name || cred.cortex_provider_id}</span>
            <TypeBadge type={cred.provider_type} />
            <StatusBadge status={cred.status} />
          </div>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
            provider id: <code>{cred.cortex_provider_id}</code>
            {cred.cortex_key_ref ? (<span> · key ref: <code>{cred.cortex_key_ref}</code></span>) : null}
          </p>
          {cred.last_validation_result && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              last validation: <code>{cred.last_validation_result}</code>
            </p>
          )}
          {(cred.default_models || []).length > 0 && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              default models: {cred.default_models.join(', ')}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0 flex-wrap justify-end">
          <button type="button" onClick={() => onValidate(cred.id)} disabled={busy.validate} title="Run live 1-token validation probe against Cortex" className={secondaryBtnClass}>
            {busy.validate ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            Validate
          </button>
          <button type="button" onClick={() => setShowRotate((v) => !v)} disabled={busy.rotate} title="Replace the provider key (relayed to Cortex, never stored)" className={secondaryBtnClass}>
            <RotateCw className="h-3.5 w-3.5" />
            Rotate
          </button>
          {cred.status !== 'disabled' && (
            <button type="button" onClick={() => onDisable(cred.id)} disabled={busy.disable} title="Disable — nothing routes against this credential" className={secondaryBtnClass}>
              <ShieldOff className="h-3.5 w-3.5" />
              Disable
            </button>
          )}
          <button type="button" onClick={() => onRemove(cred.id)} disabled={busy.remove} title="Remove this provider reference" className="inline-flex items-center gap-1.5 rounded-lg border border-rose-300 dark:border-rose-800 text-rose-600 dark:text-rose-400 px-3 py-1.5 text-xs font-medium hover:bg-rose-50 dark:hover:bg-rose-950/40 transition-colors disabled:opacity-50">
            <Trash2 className="h-3.5 w-3.5" />
            Remove
          </button>
        </div>
      </div>

      {showRotate && (
        <div className="rounded-lg border border-gray-200 dark:border-gray-800 p-3 space-y-2" data-testid="rotate-form">
          <label className="block text-xs font-medium" htmlFor={`rotate-key-${cred.id}`}>New provider API key (one-time, never stored)</label>
          <div className="flex gap-2">
            <input id={`rotate-key-${cred.id}`} type="password" value={rotateKey} onChange={(e) => setRotateKey(e.target.value)} placeholder="sk-…" autoComplete="new-password" className={inputClass} />
            <button type="button" onClick={submitRotate} disabled={busy.rotate || !rotateKey.trim()} className={primaryBtnClass}>
              {busy.rotate && <Loader2 className="h-4 w-4 animate-spin" />}
              Rotate key
            </button>
          </div>
        </div>
      )}

      <div className="flex items-center gap-3 text-xs">
        <button type="button" onClick={() => (showModels ? setShowModels(false) : startModelsEdit())} className="inline-flex items-center gap-1 text-sky-600 dark:text-sky-400 hover:underline">
          <KeyRound className="h-3.5 w-3.5" /> Default models
        </button>
        <button type="button" onClick={() => setShowOverrides((v) => !v)} className="inline-flex items-center gap-1 text-sky-600 dark:text-sky-400 hover:underline">
          <UserCog className="h-3.5 w-3.5" /> Agent overrides ({Object.keys(overrides).length})
        </button>
      </div>

      {showModels && (
        <div className="rounded-lg border border-gray-200 dark:border-gray-800 p-3 space-y-2" data-testid="models-editor">
          <label className="block text-xs font-medium" htmlFor={`models-${cred.id}`}>Default models for this provider runtime (comma-separated)</label>
          <div className="flex gap-2">
            <input id={`models-${cred.id}`} type="text" value={modelsText} onChange={(e) => setModelsText(e.target.value)} placeholder="gpt-5.6, claude-sonnet-4" className={inputClass} />
            <button type="button" onClick={saveModels} disabled={busy.mirror} className={primaryBtnClass}>Save</button>
          </div>
        </div>
      )}

      {showOverrides && (
        <div className="rounded-lg border border-gray-200 dark:border-gray-800 p-3 space-y-3" data-testid="overrides-editor">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Per-agent overrides resolve an agent to a specific same-org credential/model before the org default. Cross-org references are rejected.
          </p>
          {Object.keys(overrides).length === 0 ? (
            <p className="text-xs text-gray-400 dark:text-gray-500">No agent overrides — all agents use the org default.</p>
          ) : (
            <ul className="space-y-1">
              {Object.entries(overrides).map(([agentId, entry]) => (
                <li key={agentId} className="flex items-center justify-between gap-2 text-xs">
                  <span>
                    <code>{agentId}</code> → {providerLabel(entry.provider_credential_id)}
                    {entry.default_model_id ? ` · ${entry.default_model_id}` : ''}
                  </span>
                  <button type="button" onClick={() => removeOverride(agentId)} disabled={busy.mirror} className="text-rose-600 dark:text-rose-400 hover:underline">Remove</button>
                </li>
              ))}
            </ul>
          )}
          <div className="grid grid-cols-1 sm:grid-cols-4 gap-2 items-end">
            <div>
              <label className="block text-xs font-medium mb-1" htmlFor={`ov-agent-${cred.id}`}>Agent id</label>
              <input id={`ov-agent-${cred.id}`} type="text" value={overrideAgent} onChange={(e) => setOverrideAgent(e.target.value)} placeholder="agent@shizuha.com" className={inputClass} />
            </div>
            <div>
              <label className="block text-xs font-medium mb-1" htmlFor={`ov-provider-${cred.id}`}>Provider (same org only)</label>
              <select id={`ov-provider-${cred.id}`} value={overrideProvider} onChange={(e) => setOverrideProvider(e.target.value)} className={inputClass}>
                <option value="">Select…</option>
                {refs.map((r) => (
                  <option key={r.cortex_provider_id} value={r.cortex_provider_id}>
                    {r.display_name || r.cortex_provider_id}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium mb-1" htmlFor={`ov-model-${cred.id}`}>Model (optional)</label>
              <input id={`ov-model-${cred.id}`} type="text" value={overrideModel} onChange={(e) => setOverrideModel(e.target.value)} placeholder="gpt-5.6" className={inputClass} />
            </div>
            <button type="button" onClick={addOverride} disabled={busy.mirror || !overrideAgent.trim() || !overrideProvider} className={primaryBtnClass}>
              <Plus className="h-4 w-4" /> Add override
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * HIVE-1889 (S3) + HIVE-2161 (slice 2): org Settings > Providers.
 *
 * Full BYO credential lifecycle for the org: add (Cortex draft → submit →
 * 1-token probe), validate, rotate, disable, remove — plus default models per
 * provider runtime and the same-org agent override editor. Hive stores only
 * opaque references to Cortex-managed providers; the secret key is relayed
 * in-memory to Cortex and never stored, logged, or rendered.
 */
export default function OrgProviders() {
  const { currentOrganization } = useHive()
  const { showToast } = useToast()
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [formError, setFormError] = useState('')

  // The current org is required to scope the reference list. Without a
  // selected org the page shows an honest empty state rather than guessing.
  const orgId = currentOrganization?.id

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['org-provider-refs', orgId],
    queryFn: () => orgProviderRefsApi.list(orgId).then((r) => r.data?.results || r.data || []),
    enabled: Boolean(orgId),
  })
  const refs = data || []

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['org-provider-refs', orgId] })

  const provision = useMutation({
    mutationFn: (payload) => orgProviderRefsApi.provision(payload),
    onSuccess: () => {
      invalidate()
      setShowForm(false)
      setFormError('')
      showToast?.('Provider credential added — live probe passed.')
    },
    onError: (err) => {
      // A 502 may still have materialized a mirror row (invalid/verifying) —
      // refresh so the honest failure state is visible.
      invalidate()
      const data_ = err?.response?.data || {}
      const detail = data_.detail
      const msg = typeof detail === 'string' && detail
        ? detail
        : (data_.error || 'Could not add the provider credential.')
      setFormError(msg)
      showToast?.(data_.error || 'Could not add the provider credential.', 'error')
    },
  })

  const rotate = useMutation({
    mutationFn: ({ id, secret_key }) => orgProviderRefsApi.rotate(id, { secret_key }),
    onSuccess: () => {
      invalidate()
      showToast?.('Provider key rotated — live probe passed.')
    },
    onError: (err) => {
      invalidate()
      showToast?.(err?.response?.data?.error || 'Key rotation failed.', 'error')
    },
  })

  const disable = useMutation({
    mutationFn: (id) => orgProviderRefsApi.disable(id),
    onSuccess: () => {
      invalidate()
      showToast?.('Provider credential disabled — nothing routes against it.')
    },
    onError: (err) => {
      showToast?.(err?.response?.data?.error || 'Could not disable the credential.', 'error')
    },
  })

  const validate = useMutation({
    mutationFn: (id) => orgProviderRefsApi.validate(id),
    onSuccess: () => {
      invalidate()
      showToast?.('Validation probe passed — mirror status refreshed.')
    },
    onError: (err) => {
      invalidate()
      showToast?.(err?.response?.data?.error || 'Validation probe failed.', 'error')
    },
  })

  const remove = useMutation({
    mutationFn: (id) => orgProviderRefsApi.remove(id),
    onSuccess: () => {
      invalidate()
      showToast?.('Provider reference removed.')
    },
    onError: (err) => {
      showToast?.(err?.response?.data?.error || 'Could not remove provider reference.', 'error')
    },
  })

  const mirrorUpdate = useMutation({
    mutationFn: (payload) => orgProviderRefsApi.upsert(payload),
    onSuccess: () => {
      invalidate()
      showToast?.('Provider reference updated.')
    },
    onError: (err) => {
      invalidate()
      showToast?.(err?.response?.data?.error || 'Could not update provider reference.', 'error')
    },
  })

  const busy = {
    validate: validate.isPending,
    rotate: rotate.isPending,
    disable: disable.isPending,
    remove: remove.isPending,
    mirror: mirrorUpdate.isPending,
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <KeyRound className="h-6 w-6 text-primary-500" />
            Providers
          </h1>
          <p className="text-gray-500 dark:text-gray-400 text-sm mt-1">
            {currentOrganization
              ? `Bring-your-own provider credentials for ${currentOrganization.name || currentOrganization.slug}.`
              : 'Select an organization to manage its provider credentials.'}
          </p>
        </div>
        {orgId && (
          <button
            type="button"
            onClick={() => { setFormError(''); setShowForm((v) => !v) }}
            className="inline-flex items-center gap-2 rounded-lg bg-sky-600 hover:bg-sky-500 text-white text-sm font-medium px-4 py-2 transition-colors"
          >
            <Plus className="h-4 w-4" /> Add provider
          </button>
        )}
      </div>

      <div className="rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-4 text-sm text-gray-500 dark:text-gray-400 flex items-start gap-2">
        <ShieldCheck className="h-4 w-4 text-emerald-500 mt-0.5 shrink-0" />
        <span>
          Hive stores only opaque references to Cortex-managed credentials — no raw key
          material is stored or rendered here. Adding or rotating a key relays it to Cortex
          for a live 1-token probe and discards it. A failed or exhausted key affects only
          this organization, never fleet status.
        </span>
      </div>

      {showForm && orgId && (
        <AddProviderForm
          orgId={orgId}
          pending={provision.isPending}
          error={formError}
          onSubmit={(payload) => provision.mutate(payload)}
          onCancel={() => setShowForm(false)}
        />
      )}

      {isLoading ? (
        <div className="flex items-center gap-2 text-gray-500 text-sm py-8">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading provider credentials…
        </div>
      ) : isError ? (
        <ErrorState message="Could not load provider credentials." onRetry={() => refetch()} />
      ) : !orgId ? (
        <EmptyState
          icon={KeyRound}
          title="No organization selected"
          message="Use the organization switcher to select an org and manage its provider credentials."
        />
      ) : !refs.length ? (
        <EmptyState
          icon={Lock}
          title="No provider credentials yet"
          message="Add a BYO provider credential to run this org's agents on your own provider spend."
        />
      ) : (
        <div className="space-y-3">
          {refs.map((cred) => (
            <RefCard
              key={cred.id}
              cred={cred}
              refs={refs}
              busy={busy}
              onValidate={(id) => validate.mutate(id)}
              onRotate={(payload) => rotate.mutate(payload)}
              onDisable={(id) => disable.mutate(id)}
              onRemove={(id) => remove.mutate(id)}
              onMirrorUpdate={(payload) => mirrorUpdate.mutate(payload)}
            />
          ))}
        </div>
      )}
    </div>
  )
}
