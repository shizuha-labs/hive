import { useState } from 'react'
import {
  Users,
  Plus,
  Pencil,
  Trash2,
  UserPlus,
  UserMinus,
  ChevronDown,
  ChevronRight,
  Crown,
  Loader2,
  AlertTriangle,
  ShieldAlert,
} from 'lucide-react'
import clsx from 'clsx'
import { useCapabilities, useCapabilityMutations, useFleetRuntimeOptions, useTeams, useTeamCapabilities, useTeamMembers, useTeamMutations } from '../hooks/useAgents'
import { useHive } from '../contexts/HiveContext'
import { useToast } from '../components/Toast'
import { toastRuntimeReconcile } from '../lib/runtimeReconcile'
import { teamDisplayLabel } from '../lib/format'

function friendlyTeamName(value) {
  const raw = String(value || '').trim()
  if (!raw) return ''
  return raw
    .replace(/[_-]+/g, ' ')
    .split(' ')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

function teamKey(team) {
  return team?.team_name || team?.slug || team?.id || team?.name || ''
}

function memberEmail(value) {
  return String(value || '').trim().toLowerCase()
}

function flattenTierMembers(source) {
  const out = []
  const seen = new Set()
  const teamName = teamKey(source)

  ;(source?.tiers || []).forEach((tier, tierIndex) => {
    const members = tier || []
    members.forEach((rawEmail) => {
      const email = memberEmail(rawEmail)
      if (!email || seen.has(email)) return
      seen.add(email)
      out.push({
        email,
        team_name: teamName,
        tier: tierIndex,
        tier_index: tierIndex,
        is_junior: (source?.junior_emails || []).map(memberEmail).includes(email),
      })
    })
  })

  const juniorTier = out.length ? Math.max(...out.map((m) => Number(m.tier_index || 0))) + 1 : 0
  ;(source?.junior_emails || []).forEach((rawEmail) => {
    const email = memberEmail(rawEmail)
    if (!email || seen.has(email)) return
    seen.add(email)
    out.push({
      email,
      team_name: teamName,
      tier: juniorTier,
      tier_index: juniorTier,
      is_junior: true,
    })
  })

  return out
}

function normalizeMemberships(data, fallbackTeam) {
  const rows = Array.isArray(data?.memberships) ? data.memberships : []
  return rows
    .map((item) => {
      const email = memberEmail(item?.email)
      if (!email) return null
      const tier = item?.tier_index ?? item?.tier ?? 0
      return {
        ...item,
        email,
        team_name: item?.team_name || fallbackTeam,
        tier,
        tier_index: tier,
      }
    })
    .filter(Boolean)
}

function groupMembersByTier(members) {
  return members.reduce((groups, member) => {
    const tier = Number(member.tier_index ?? member.tier ?? 0)
    if (!groups.has(tier)) groups.set(tier, [])
    groups.get(tier).push(member)
    return groups
  }, new Map())
}

function tierLabel(tier) {
  if (tier === 0) return 'Primary tier'
  return `Fallback tier ${tier}`
}


function asArray(value) {
  return Array.isArray(value) ? value.filter(Boolean).map(String) : []
}

function optionId(option) {
  if (typeof option === 'string') return option
  return option?.id || option?.slug || option?.name || option?.label || ''
}

function optionLabel(option) {
  if (typeof option === 'string') return option
  const id = optionId(option)
  const label = option?.label || option?.name || option?.display_name || id
  return id && label !== id ? `${label} (${id})` : label
}

function errorText(error) {
  const data = error?.response?.data
  if (!data) return error?.message || 'Request failed'
  if (typeof data === 'string') return data
  if (data.error) return data.error
  return Object.entries(data)
    .map(([field, messages]) => `${field}: ${Array.isArray(messages) ? messages.join(', ') : String(messages)}`)
    .join('; ')
}

function MultiValueField({ label, values, options = [], placeholder, onChange }) {
  const [draft, setDraft] = useState('')
  const datalistId = `cap-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`
  const ids = new Set(asArray(values))
  const add = (raw) => {
    const next = String(raw || '').trim()
    if (!next || ids.has(next)) return
    onChange([...ids, next])
    setDraft('')
  }
  const remove = (value) => onChange([...ids].filter((item) => item !== value))

  return (
    <div className="space-y-1.5">
      <label className="text-xs font-medium text-gray-600 dark:text-gray-300">{label}</label>
      <div className="flex flex-wrap gap-1.5">
        {[...ids].map((value) => (
          <span key={value} className="inline-flex items-center gap-1 rounded-full border border-gray-200 bg-gray-50 px-2 py-0.5 text-xs dark:border-gray-700 dark:bg-gray-900">
            <span className="font-mono">{value}</span>
            <button type="button" onClick={() => remove(value)} className="rounded-full px-1 text-gray-400 hover:bg-gray-200 hover:text-gray-700 dark:hover:bg-gray-700 dark:hover:text-gray-100">×</button>
          </span>
        ))}
      </div>
      <div className="flex gap-2">
        <input
          list={datalistId}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { e.preventDefault(); add(draft) }
          }}
          placeholder={placeholder || `Add ${label.toLowerCase()}…`}
          className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-900"
        />
        <datalist id={datalistId}>
          {options.map((option) => {
            const id = optionId(option)
            if (!id) return null
            return <option key={id} value={id}>{optionLabel(option)}</option>
          })}
        </datalist>
        <button type="button" onClick={() => add(draft)} disabled={!draft.trim()} className="rounded border border-gray-300 px-2 py-1.5 text-xs hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-700">Add</button>
      </div>
    </div>
  )
}

export function CapabilityMappingEditor({ capability, capabilities, runtimeOptions, mutations, onClose }) {
  const toast = useToast()
  const [draft, setDraft] = useState(() => ({
    display_name: capability.display_name || '',
    description: capability.description || '',
    risk_tier: capability.risk_tier || 'standard',
    skill_refs: asArray(capability.skill_refs),
    eager_skill_refs: asArray(capability.eager_skill_refs),
    inline_profile_refs: asArray(capability.inline_profile_refs),
    mcp_server_refs: asArray(capability.mcp_server_refs),
    sensitive_access_flags: asArray(capability.sensitive_access_flags),
    default_credential_grant_scopes: asArray(capability.default_credential_grant_scopes),
    requires: asArray(capability.requires),
    conflicts_with: asArray(capability.conflicts_with),
    runtime_flags: JSON.stringify(capability.runtime_flags || {}, null, 2),
    reason: 'Updated capability bundle mappings from Hive Teams UI',
  }))
  const [localError, setLocalError] = useState('')
  const skillOptions = runtimeOptions?.skills || []
  const mcpOptions = runtimeOptions?.mcp_servers || []
  const profileOptions = runtimeOptions?.resource_profiles || []
  const capabilityOptions = (capabilities || []).filter((cap) => cap.slug !== capability.slug).map((cap) => ({ id: cap.slug, label: cap.display_name || cap.slug }))

  const update = (field, value) => setDraft((current) => ({ ...current, [field]: value }))
  const save = () => {
    setLocalError('')
    let runtimeFlags = {}
    try {
      runtimeFlags = draft.runtime_flags.trim() ? JSON.parse(draft.runtime_flags) : {}
      if (!runtimeFlags || Array.isArray(runtimeFlags) || typeof runtimeFlags !== 'object') {
        throw new Error('runtime_flags must be a JSON object')
      }
    } catch (err) {
      setLocalError(err.message || 'runtime_flags must be valid JSON')
      return
    }
    mutations.update.mutate(
      {
        slug: capability.slug,
        display_name: draft.display_name.trim(),
        description: draft.description,
        risk_tier: draft.risk_tier,
        skill_refs: draft.skill_refs,
        eager_skill_refs: draft.eager_skill_refs,
        inline_profile_refs: draft.inline_profile_refs,
        mcp_server_refs: draft.mcp_server_refs,
        runtime_flags: runtimeFlags,
        sensitive_access_flags: draft.sensitive_access_flags,
        default_credential_grant_scopes: draft.default_credential_grant_scopes,
        requires: draft.requires,
        conflicts_with: draft.conflicts_with,
        reason: draft.reason,
      },
      {
        onSuccess: (data) => {
          const summary = toastRuntimeReconcile(toast, data, 'Capability mappings updated')
          if (summary.level === 'error') {
            setLocalError(summary.message)
            return
          }
          onClose()
        },
        onError: (e) => setLocalError(errorText(e)),
      }
    )
  }

  return (
    <div className="mt-3 rounded-xl border border-primary-200 bg-primary-50/40 p-4 dark:border-primary-900 dark:bg-primary-950/20">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">Edit capability bundle: {capability.display_name || capability.slug}</h3>
          <p className="text-xs text-gray-500">Mappings are audited; activation/security-review status is controlled by the existing workflow buttons.</p>
        </div>
        <button type="button" onClick={onClose} className="rounded border border-gray-300 px-2 py-1 text-xs hover:bg-white dark:border-gray-600 dark:hover:bg-gray-800">Close</button>
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <label className="space-y-1 text-xs font-medium text-gray-600 dark:text-gray-300">
          Display name
          <input value={draft.display_name} onChange={(e) => update('display_name', e.target.value)} className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm font-normal dark:border-gray-600 dark:bg-gray-900" />
        </label>
        <label className="space-y-1 text-xs font-medium text-gray-600 dark:text-gray-300">
          Risk tier
          <select value={draft.risk_tier} onChange={(e) => update('risk_tier', e.target.value)} className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm font-normal dark:border-gray-600 dark:bg-gray-900">
            <option value="base">Base</option>
            <option value="standard">Standard</option>
            <option value="sensitive">Sensitive</option>
            <option value="privileged">Privileged</option>
          </select>
        </label>
        <label className="space-y-1 text-xs font-medium text-gray-600 dark:text-gray-300 lg:col-span-2">
          Description
          <textarea value={draft.description} onChange={(e) => update('description', e.target.value)} rows={2} className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm font-normal dark:border-gray-600 dark:bg-gray-900" />
        </label>
        <MultiValueField label="Skills" values={draft.skill_refs} options={skillOptions} onChange={(v) => update('skill_refs', v)} />
        <MultiValueField label="Eager skills" values={draft.eager_skill_refs} options={skillOptions} onChange={(v) => update('eager_skill_refs', v)} />
        <MultiValueField label="MCP servers" values={draft.mcp_server_refs} options={mcpOptions} onChange={(v) => update('mcp_server_refs', v)} />
        <MultiValueField label="Inline profiles" values={draft.inline_profile_refs} options={profileOptions} onChange={(v) => update('inline_profile_refs', v)} />
        <MultiValueField label="Credential scopes" values={draft.default_credential_grant_scopes} onChange={(v) => update('default_credential_grant_scopes', v)} placeholder="credential scope…" />
        <MultiValueField label="Sensitive flags" values={draft.sensitive_access_flags} onChange={(v) => update('sensitive_access_flags', v)} placeholder="sensitive access flag…" />
        <MultiValueField label="Requires" values={draft.requires} options={capabilityOptions} onChange={(v) => update('requires', v)} />
        <MultiValueField label="Conflicts with" values={draft.conflicts_with} options={capabilityOptions} onChange={(v) => update('conflicts_with', v)} />
        <label className="space-y-1 text-xs font-medium text-gray-600 dark:text-gray-300 lg:col-span-2">
          Runtime flags (JSON object)
          <textarea value={draft.runtime_flags} onChange={(e) => update('runtime_flags', e.target.value)} rows={5} className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 font-mono text-xs font-normal dark:border-gray-600 dark:bg-gray-900" />
        </label>
        <label className="space-y-1 text-xs font-medium text-gray-600 dark:text-gray-300 lg:col-span-2">
          Audit reason
          <input value={draft.reason} onChange={(e) => update('reason', e.target.value)} className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm font-normal dark:border-gray-600 dark:bg-gray-900" />
        </label>
      </div>
      {localError && <div className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">{localError}</div>}
      <div className="mt-3 flex justify-end gap-2">
        <button type="button" onClick={onClose} className="rounded border border-gray-300 px-3 py-1.5 text-sm hover:bg-white dark:border-gray-600 dark:hover:bg-gray-800">Cancel</button>
        <button type="button" onClick={save} disabled={mutations.update.isPending} className="rounded bg-primary-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-primary-700 disabled:opacity-50">Save mappings</button>
      </div>
    </div>
  )
}

// ── Skeleton ──────────────────────────────────────────────────────────────────
function TeamSkeleton() {
  return (
    <div className="hv-panel p-4 animate-pulse">
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-2 flex-1">
          <div className="h-5 w-32 rounded bg-gray-200 dark:bg-gray-700" />
          <div className="h-3.5 w-48 rounded bg-gray-100 dark:bg-gray-700" />
        </div>
        <div className="flex gap-1">
          <div className="h-7 w-7 rounded bg-gray-100 dark:bg-gray-700" />
          <div className="h-7 w-7 rounded bg-gray-100 dark:bg-gray-700" />
        </div>
      </div>
    </div>
  )
}

// ── Inline text field helper ──────────────────────────────────────────────────
function InlineField({ label, value, onSave, onCancel }) {
  const [draft, setDraft] = useState(value)
  return (
    <div className="flex items-center gap-2">
      <label className="shrink-0 text-xs text-gray-500 dark:text-gray-400">{label}</label>
      <input
        className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') onSave(draft.trim())
          if (e.key === 'Escape') onCancel()
        }}
        autoFocus
      />
      <button
        type="button"
        onClick={() => onSave(draft.trim())}
        className="shrink-0 rounded bg-primary-600 px-2 py-1 text-xs text-white hover:bg-primary-700"
      >
        Save
      </button>
      <button
        type="button"
        onClick={onCancel}
        className="shrink-0 rounded border border-gray-300 px-2 py-1 text-xs hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-700"
      >
        Cancel
      </button>
    </div>
  )
}

// HIVE-297: canonical labels for routing_health issue_types surfaced in MemberRow.
const ROUTING_ISSUE_LABELS = {
  roster_unassignable: 'Pulse routing blocked',
  identity_unresolved: 'Identity drift',
  identity_email_mismatch: 'Identity drift',
  identity_missing: 'Identity drift',
  credential_missing: 'Credentials missing',
  provider_unavailable: 'Provider unavailable',
  provider_unavailable_or_quota: 'Provider unavailable',
  capabilities_drift: 'Capabilities drift',
  desired_state_not_applied: 'Desired state not applied',
}

// ── MemberRow ─────────────────────────────────────────────────────────────────
function MemberRow({ email, tier, isLead, onRemove, onSetLead, mutating, member }) {
  const roleTitle = member?.role_title || member?.role || member?.membership_role || ''
  // Deduplicate by issue_type so each distinct issue shows as one badge.
  const rawWarnings = member?.routing_health?.warnings || []
  const seenTypes = new Set()
  const healthBadges = rawWarnings.filter((w) => {
    const key = w.issue_type || w.service || 'unknown'
    if (seenTypes.has(key)) return false
    seenTypes.add(key)
    return true
  })

  return (
    <div className="rounded-lg px-3 py-1.5 text-sm hover:bg-gray-50 dark:hover:bg-gray-700/40">
      <div className="flex items-center gap-2">
      {isLead ? (
        <Crown className="h-3.5 w-3.5 shrink-0 text-amber-500" title="Team lead" />
      ) : (
        <div className="h-3.5 w-3.5 shrink-0" />
      )}
      <span className="min-w-0 flex-1 truncate text-gray-700 dark:text-gray-300">
        <span className="block truncate">{email}</span>
        {roleTitle && <span className="block truncate text-[11px] text-gray-400">{roleTitle}</span>}
      </span>
      <span className="shrink-0 rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500 dark:bg-gray-700 dark:text-gray-300">
        {tierLabel(Number(tier || 0))}
      </span>
      {!isLead && (
        <button
          type="button"
          title="Set as team lead"
          onClick={() => onSetLead(email)}
          disabled={mutating}
          className="shrink-0 rounded p-0.5 text-gray-400 hover:text-amber-500 disabled:opacity-40"
        >
          <Crown className="h-3.5 w-3.5" />
        </button>
      )}
      <button
        type="button"
        title="Remove from team"
        onClick={() => onRemove(email)}
        disabled={mutating}
        className="shrink-0 rounded p-0.5 text-gray-400 hover:text-red-500 disabled:opacity-40"
      >
        <UserMinus className="h-3.5 w-3.5" />
      </button>
      </div>
      {healthBadges.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1 pl-5">
          {healthBadges.map((w, idx) => {
            const issueType = w.issue_type || 'unknown'
            const label = ROUTING_ISSUE_LABELS[issueType] || w.message || issueType
            return (
              <span
                key={`${issueType}-${idx}`}
                title={`${w.service}: ${w.message || issueType}`}
                className="inline-flex items-center gap-1 rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700 ring-1 ring-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:ring-amber-800"
              >
                <AlertTriangle className="h-3 w-3 shrink-0" />
                {label}
              </span>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── AddMemberForm ─────────────────────────────────────────────────────────────
function AddMemberForm({ onAdd, onCancel }) {
  const [email, setEmail] = useState('')
  return (
    <div className="flex items-center gap-2 px-3 py-1.5">
      <input
        type="email"
        placeholder="agent@shizuha.com"
        className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && email.trim()) onAdd(email.trim())
          if (e.key === 'Escape') onCancel()
        }}
        autoFocus
      />
      <button
        type="button"
        onClick={() => email.trim() && onAdd(email.trim())}
        className="shrink-0 rounded bg-primary-600 px-2 py-1 text-xs text-white hover:bg-primary-700"
      >
        Add
      </button>
      <button
        type="button"
        onClick={onCancel}
        className="shrink-0 rounded border border-gray-300 px-2 py-1 text-xs hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-700"
      >
        Cancel
      </button>
    </div>
  )
}

function TeamCapabilitiesEditor({ teamName, expanded, mutations, orgSlug }) {
  const toast = useToast()
  const catalog = useCapabilities({ enabled: expanded })
  const teamCaps = useTeamCapabilities(teamName, { enabled: expanded, orgSlug })
  const assigned = new Set((teamCaps.data?.capabilities || []).map((c) => c.capability_slug))
  const [draft, setDraft] = useState(null)
  const [addSlug, setAddSlug] = useState('')
  const selected = draft ?? assigned

  if (!expanded) return null
  if (catalog.isLoading || teamCaps.isLoading) {
    return <div className="px-4 py-3 text-xs text-gray-500">Loading capabilities…</div>
  }
  if (catalog.isError || teamCaps.isError) {
    return <div className="px-4 py-2 text-xs text-red-500">Capability API unavailable.</div>
  }

  const capabilities = catalog.data || []
  const activeCapabilities = capabilities.filter((cap) => cap.activation_status === 'active')
  const capabilityBySlug = new Map(capabilities.map((cap) => [cap.slug, cap]))
  const selectedCapabilities = Array.from(selected)
    .map((slug) => capabilityBySlug.get(slug) || { slug, display_name: slug, risk_tier: 'unknown', activation_status: 'unknown' })
    .sort((a, b) => (a.display_name || a.slug).localeCompare(b.display_name || b.slug))
  const addableCapabilities = activeCapabilities
    .filter((cap) => !selected.has(cap.slug))
    .sort((a, b) => (a.display_name || a.slug).localeCompare(b.display_name || b.slug))

  const addCapability = (slug) => {
    if (!slug) return
    const next = new Set(selected)
    next.add(slug)
    setDraft(next)
    setAddSlug('')
  }
  const removeCapability = (slug) => {
    const next = new Set(selected)
    next.delete(slug)
    setDraft(next)
  }
  const save = () => {
    mutations.setCapabilities.mutate(
      {
        name: teamName,
        capabilities: Array.from(selected).map((slug) => ({ capability_slug: slug, reason: 'Updated from Hive Teams UI' })),
      },
      {
        onSuccess: (data) => { toastRuntimeReconcile(toast, data, 'Team capabilities updated'); setDraft(null) },
        onError: (e) => toast.error(e?.response?.data?.error || 'Failed to update capabilities'),
      }
    )
  }

  return (
    <div className="border-t border-gray-100 px-4 py-3 dark:border-gray-700/60">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Required capabilities</h4>
        {draft && (
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => { setDraft(null); setAddSlug('') }}
              className="rounded border border-gray-300 px-2 py-1 text-xs hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={mutations.setCapabilities.isPending}
              onClick={save}
              className="rounded bg-primary-600 px-2 py-1 text-xs text-white hover:bg-primary-700 disabled:opacity-50"
            >
              Save
            </button>
          </div>
        )}
      </div>
      {capabilities.length === 0 ? (
        <p className="text-xs text-gray-400">No capability definitions yet.</p>
      ) : (
        <div className="space-y-3">
          {selectedCapabilities.length === 0 ? (
            <p className="text-xs text-gray-400">No capabilities assigned to this team.</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {selectedCapabilities.map((cap) => {
                const active = cap.activation_status === 'active'
                return (
                  <span
                    key={cap.slug}
                    className={clsx(
                      'inline-flex max-w-full items-center gap-1 rounded-full border px-2.5 py-1 text-xs',
                      active
                        ? 'border-gray-200 bg-gray-50 text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200'
                        : 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300'
                    )}
                  >
                    <span className="min-w-0 truncate">{cap.display_name || cap.slug}</span>
                    <span className="shrink-0 text-gray-400">· {cap.risk_tier}</span>
                    <button
                      type="button"
                      onClick={() => removeCapability(cap.slug)}
                      className="ml-0.5 shrink-0 rounded-full px-1 text-gray-400 hover:bg-gray-200 hover:text-gray-700 dark:hover:bg-gray-700 dark:hover:text-gray-100"
                      title={`Remove ${cap.display_name || cap.slug}`}
                    >
                      &times;
                    </button>
                  </span>
                )
              })}
            </div>
          )}

          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <select
              value={addSlug}
              onChange={(e) => setAddSlug(e.target.value)}
              disabled={addableCapabilities.length === 0}
              className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-2 py-1.5 text-sm disabled:opacity-50 dark:border-gray-600 dark:bg-gray-900"
            >
              <option value="">
                {addableCapabilities.length === 0 ? 'No more active capabilities' : 'Add capability…'}
              </option>
              {addableCapabilities.map((cap) => (
                <option key={cap.slug} value={cap.slug}>
                  {cap.display_name || cap.slug} · {cap.risk_tier}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={!addSlug}
              onClick={() => addCapability(addSlug)}
              className="shrink-0 rounded border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-700"
            >
              Add capability
            </button>
          </div>
        </div>
      )}
      {activeCapabilities.length === 0 && capabilities.length > 0 && (
        <p className="mt-2 text-xs text-amber-600 dark:text-amber-400">
          No active capabilities are assignable yet. Use the catalog activation action first.
        </p>
      )}
    </div>
  )
}

function CapabilityCatalogPanel() {
  const toast = useToast()
  const catalog = useCapabilities()
  const runtimeOptions = useFleetRuntimeOptions()
  const mutations = useCapabilityMutations()
  const [editingCapability, setEditingCapability] = useState(null)
  const [draft, setDraft] = useState({ slug: '', display_name: '', risk_tier: 'standard' })
  const autoActivates = ['base', 'standard'].includes(draft.risk_tier)

  const create = () => {
    const slug = draft.slug.trim().toLowerCase().replace(/[_\s]+/g, '-')
    if (!slug) return
    mutations.create.mutate(
      {
        ...draft,
        slug,
        display_name: draft.display_name || slug.replace(/-/g, ' '),
      },
      {
        onSuccess: (cap) => {
          setDraft({ slug: '', display_name: '', risk_tier: 'standard' })
          if (['base', 'standard'].includes(cap.risk_tier)) {
            mutations.action.mutate(
              { slug: cap.slug, action: 'activate', reason: 'Activated from Hive Teams UI' },
              {
                onSuccess: (data) => toastRuntimeReconcile(toast, data, 'Capability created and activated'),
                onError: (e) => toast.error(e?.response?.data?.error || 'Capability created but activation failed'),
              }
            )
          } else {
            toast.success('Capability created as draft; security review is required before activation')
          }
        },
        onError: (e) => toast.error(e?.response?.data?.error || e?.response?.data?.slug?.[0] || 'Failed to create capability'),
      }
    )
  }

  const activate = (cap) => {
    mutations.action.mutate(
      { slug: cap.slug, action: 'activate', reason: 'Activated from Hive Teams UI' },
      {
        onSuccess: (data) => toastRuntimeReconcile(toast, data, 'Capability activated'),
        onError: (e) => toast.error(e?.response?.data?.error || 'Activation failed'),
      }
    )
  }

  const reconcile = (cap) => {
    mutations.action.mutate(
      { slug: cap.slug, action: 'reconcile', reason: 'Manual reconcile from Hive Teams UI' },
      {
        onSuccess: (data) => toastRuntimeReconcile(toast, data, 'Capability reconciled'),
        onError: (e) => toast.error(e?.response?.data?.error || 'Capability reconcile failed'),
      }
    )
  }

  const editMappings = (cap) => {
    setEditingCapability((current) => (current?.slug === cap.slug ? null : cap))
  }

  const capabilities = catalog.data || []
  const currentEditingCapability = editingCapability
    ? capabilities.find((cap) => cap.slug === editingCapability.slug) || editingCapability
    : null

  return (
    <div className="hv-panel p-5">
      <div className="mb-5">
        <h2 className="text-base font-semibold">Capability catalog</h2>
        <p className="text-sm text-gray-500 mt-1">Define reusable skills/MCP/profile bundles, then assign them to teams. A team's capabilities determine what its agents can do.</p>
      </div>

      {/* Create form */}
      <div className="mb-5 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 bg-gray-50/50 dark:bg-gray-900/50 p-4">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-3">New capability</h3>
        <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto_auto]">
          <input
            value={draft.slug}
            onChange={(e) => setDraft((v) => ({ ...v, slug: e.target.value }))}
            placeholder="capability-slug"
            className="rounded border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-900"
          />
          <input
            value={draft.display_name}
            onChange={(e) => setDraft((v) => ({ ...v, display_name: e.target.value }))}
            placeholder="Display name"
            className="rounded border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-900"
          />
          <select
            value={draft.risk_tier}
            onChange={(e) => setDraft((v) => ({ ...v, risk_tier: e.target.value }))}
            className="rounded border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-900"
          >
            <option value="base">Base</option>
            <option value="standard">Standard</option>
            <option value="sensitive">Sensitive</option>
            <option value="privileged">Privileged</option>
          </select>
          <button
            type="button"
            disabled={!draft.slug.trim() || mutations.create.isPending}
            onClick={create}
            className="rounded bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-700 disabled:opacity-50"
          >
            Add
          </button>
        </div>
        <p className="mt-2 text-xs text-gray-500">
          {autoActivates
            ? 'Base/standard capabilities are activated through the dedicated workflow action after creation.'
            : 'Sensitive/privileged capabilities stay draft until reviewed; they cannot be assigned while inactive.'}
        </p>
      </div>

      {/* Catalog list */}
      {catalog.isError ? (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
          Capability catalog unavailable.
        </div>
      ) : (
        <div className="space-y-2">
          {(catalog.data || []).length === 0 ? (
            <p className="text-sm text-gray-400 py-4 text-center">No capabilities defined yet. Create one above.</p>
          ) : (
            (catalog.data || []).map((cap) => (
              <div key={cap.slug} className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-4 py-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => editMappings(cap)}
                        className="font-medium text-sm text-gray-900 dark:text-gray-100 hover:text-primary-600 dark:hover:text-primary-300"
                        title="Edit capability bundle mappings"
                      >
                        {cap.display_name || cap.slug}
                      </button>
                      <span className="inline-flex items-center rounded-full bg-gray-100 dark:bg-gray-800 px-2 py-0.5 text-[11px] font-medium text-gray-600 dark:text-gray-400">
                        {cap.risk_tier}
                      </span>
                      <span className={clsx(
                        'inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium',
                        cap.activation_status === 'active'
                          ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/20 dark:text-emerald-300'
                          : 'bg-amber-50 text-amber-700 dark:bg-amber-900/20 dark:text-amber-300'
                      )}>
                        {cap.activation_status}
                      </span>
                    </div>
                    {cap.description && (
                      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400 line-clamp-1">{cap.description}</p>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {cap.activation_status !== 'active' && ['base', 'standard'].includes(cap.risk_tier) && (
                      <button
                        type="button"
                        disabled={mutations.action.isPending}
                        onClick={() => activate(cap)}
                        className="rounded bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
                      >
                        Activate
                      </button>
                    )}
                    {cap.activation_status !== 'active' && ['sensitive', 'privileged'].includes(cap.risk_tier) && (
                      <span className="text-xs text-amber-600 dark:text-amber-400 font-medium">Review required</span>
                    )}
                    <button
                      type="button"
                      disabled={mutations.action.isPending}
                      onClick={() => reconcile(cap)}
                      className="rounded border border-gray-300 dark:border-gray-600 px-2.5 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
                    >
                      Reconcile
                    </button>
                    <button
                      type="button"
                      onClick={() => editMappings(cap)}
                      className="rounded border border-gray-300 dark:border-gray-600 px-2.5 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800"
                    >
                      Mappings
                    </button>
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      )}
      {currentEditingCapability && (
        <CapabilityMappingEditor
          key={currentEditingCapability.slug}
          capability={currentEditingCapability}
          capabilities={capabilities}
          runtimeOptions={runtimeOptions.data || {}}
          mutations={mutations}
          onClose={() => setEditingCapability(null)}
        />
      )}
    </div>
  )
}

// ── TeamCard ──────────────────────────────────────────────────────────────────
function TeamCard({ team, orgSlug }) {
  const [expanded, setExpanded] = useState(false)
  const [editing, setEditing] = useState(null)
  const [addingMember, setAddingMember] = useState(false)
  const [mutating, setMutating] = useState(false)
  const toast = useToast()
  // Every card owns org-scoped mutations. The page can display multiple orgs,
  // so a single page-level hook would make same-named cards write through an
  // empty/bare team identity in All Organizations mode.
  const mutations = useTeamMutations({ orgSlug })

  const teamName = teamKey(team)
  const rosterMembers = flattenTierMembers(team)
  const teamDisplayName = team.display_name || friendlyTeamName(teamName)
  const displayName = teamDisplayLabel(team, teamDisplayName)
  const leadEmail = team.lead_email || rosterMembers[0]?.email || ''

  const members = useTeamMembers(teamName, { enabled: expanded, orgSlug })
  const apiMembers = normalizeMemberships(members.data, teamName)
  const visibleMembers = apiMembers.length > 0 ? apiMembers : rosterMembers
  const memberCount = visibleMembers.length
  const tierCount = new Set(visibleMembers.map((m) => Number(m.tier_index ?? m.tier ?? 0))).size
  const tierSummary = memberCount
    ? `${memberCount} member${memberCount === 1 ? '' : 's'} · ${tierCount} tier${tierCount === 1 ? '' : 's'}`
    : 'No roster members'
  const membersByTier = groupMembersByTier(visibleMembers)

  async function run(fn) {
    setMutating(true)
    try {
      await fn()
    } catch (err) {
      toast.error(err?.response?.data?.error || err?.message || 'Request failed')
    } finally {
      setMutating(false)
    }
  }

  function handleRename(newName) {
    if (!newName || newName === teamDisplayName) { setEditing(null); return }
    run(() => mutations.update.mutateAsync({ name: teamName, display_name: newName }))
      .then(() => { toast.success(`Team renamed to "${newName}"`); setEditing(null) })
  }

  function handleSetLead(email) {
    run(() => mutations.update.mutateAsync({ name: teamName, lead_email: email }))
      .then(() => toast.success(`Lead set to ${email}`))
  }

  function handleDelete() {
    if (!window.confirm(`Delete team "${displayName}"? This cannot be undone.`)) return
    run(() => mutations.remove.mutateAsync(teamName))
      .then(() => toast.success(`Team "${displayName}" deleted`))
  }

  function handleAddMember(email) {
    run(() => mutations.manageMember.mutateAsync({ name: teamName, action: 'add', email }))
      .then(() => { toast.success(`Added ${email}`); setAddingMember(false) })
  }

  function handleRemoveMember(email) {
    if (!window.confirm(`Remove ${email} from "${displayName}"?`)) return
    run(() => mutations.manageMember.mutateAsync({ name: teamName, action: 'remove', email }))
      .then(() => toast.success(`Removed ${email}`))
  }

  const ChevronIcon = expanded ? ChevronDown : ChevronRight

  return (
    <div className="hv-panel">
      {/* Header */}
      <div className="flex items-start gap-3 p-4">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-0.5 shrink-0 rounded p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
        >
          <ChevronIcon className="h-4 w-4" />
        </button>

        <div className="min-w-0 flex-1">
          {editing === 'name' ? (
            <InlineField
              label="Name"
              value={teamDisplayName}
              onSave={handleRename}
              onCancel={() => setEditing(null)}
            />
          ) : (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="text-left"
            >
              <div className="font-semibold text-gray-900 dark:text-gray-100">{displayName}</div>
              {leadEmail && (
                <div className="mt-0.5 flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
                  <Crown className="h-3 w-3 text-amber-400" />
                  {leadEmail}
                </div>
              )}
              <div className="mt-1 text-xs text-gray-400 dark:text-gray-500">
                {tierSummary}
                {teamName && <span className="ml-2 font-mono text-[11px]">slug: {teamName}</span>}
              </div>
            </button>
          )}
          {editing === 'lead' && (
            <div className="mt-2">
              <InlineField
                label="Lead email"
                value={leadEmail}
                onSave={(v) => { handleSetLead(v); setEditing(null) }}
                onCancel={() => setEditing(null)}
              />
            </div>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {mutating && <Loader2 className="h-3.5 w-3.5 animate-spin text-gray-400" />}
          <button
            type="button"
            title="Rename team"
            onClick={() => setEditing(editing === 'name' ? null : 'name')}
            className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <Pencil className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            title="Set team lead"
            onClick={() => setEditing(editing === 'lead' ? null : 'lead')}
            className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-amber-500 dark:hover:bg-gray-700"
          >
            <Crown className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            title="Delete team"
            onClick={handleDelete}
            disabled={mutating}
            className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-40 dark:hover:bg-red-900/20"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Expanded members panel */}
      {expanded && (
        <div className="border-t border-gray-100 pb-2 dark:border-gray-700/60">
          {members.isLoading && (
            <div className="flex items-center gap-2 px-4 py-3 text-sm text-gray-500">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading members…
            </div>
          )}
          {members.isError && (
            <div className="px-4 py-2 text-xs text-red-500">
              Roster API unavailable
              {rosterMembers.length > 0 ? '; showing TeamRoster snapshot from the teams list.' : '.'}
            </div>
          )}
          {(members.data || rosterMembers.length > 0 || members.isError) && (
            <>
              {visibleMembers.length === 0 && !addingMember && (
                <p className="px-4 py-2 text-xs text-gray-400">TeamRoster has no members.</p>
              )}
              {Array.from(membersByTier.entries())
                .sort(([a], [b]) => a - b)
                .map(([tier, tierMembers]) => (
                  <div key={tier} className="py-1">
                    <div className="px-4 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                      {tierLabel(tier)}
                    </div>
                    {tierMembers.map((member) => (
                      <MemberRow
                        key={member.email}
                        email={member.email}
                        tier={tier}
                        member={member}
                        isLead={member.email === leadEmail}
                        onRemove={handleRemoveMember}
                        onSetLead={handleSetLead}
                        mutating={mutating}
                      />
                    ))}
                  </div>
                ))}
            </>
          )}
          {addingMember ? (
            <AddMemberForm onAdd={handleAddMember} onCancel={() => setAddingMember(false)} />
          ) : (
            <button
              type="button"
              onClick={() => setAddingMember(true)}
              className="ml-3 mt-1 flex items-center gap-1 rounded px-2 py-1 text-xs text-primary-600 hover:bg-primary-50 dark:text-primary-400 dark:hover:bg-primary-900/20"
            >
              <UserPlus className="h-3.5 w-3.5" /> Add member
            </button>
          )}
        </div>
      )}
      <TeamCapabilitiesEditor teamName={teamName} expanded={expanded} mutations={mutations} orgSlug={orgSlug} />
    </div>
  )
}

// ── CreateTeamForm ─────────────────────────────────────────────────────────────
function CreateTeamForm({ onDone, onCreate }) {
  const [name, setName] = useState('')
  return (
    <div className="flex items-center gap-2 rounded-xl border border-dashed border-primary-300 bg-primary-50/50 p-3 dark:border-primary-800 dark:bg-primary-900/10">
      <input
        placeholder="team-name (lowercase, hyphens ok)"
        className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
        value={name}
        onChange={(e) => setName(e.target.value.toLowerCase().replace(/\s+/g, '-'))}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && name.trim()) onCreate(name.trim())
          if (e.key === 'Escape') onDone()
        }}
        autoFocus
      />
      <button
        type="button"
        onClick={() => name.trim() && onCreate(name.trim())}
        className="shrink-0 rounded bg-primary-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-primary-700"
      >
        Create
      </button>
      <button
        type="button"
        onClick={onDone}
        className="shrink-0 rounded border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-700"
      >
        Cancel
      </button>
    </div>
  )
}

// ── Tab navigation ────────────────────────────────────────────────────────────
const TABS = [
  { id: 'teams', label: 'Teams', icon: Users, description: 'Roster, membership, and agent team management' },
  { id: 'capabilities', label: 'Capabilities', icon: ShieldAlert, description: 'Capability catalog — define reusable skill/MCP bundles and assign them to teams' },
]

function TabBar({ activeTab, onTabChange }) {
  return (
    <div className="flex gap-1 rounded-xl bg-gray-100/80 dark:bg-gray-800/60 p-1" role="tablist">
      {TABS.map((tab) => {
        const Icon = tab.icon
        const isActive = activeTab === tab.id
        return (
          <button
            key={tab.id}
            role="tab"
            aria-selected={isActive}
            onClick={() => onTabChange(tab.id)}
            className={`
              flex items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-medium transition-all
              ${isActive
                ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 shadow-sm ring-1 ring-gray-200 dark:ring-gray-700'
                : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-white/50 dark:hover:bg-gray-800/50'
              }
            `}
          >
            <Icon className="h-4 w-4" />
            {tab.label}
          </button>
        )
      })}
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────
export default function Teams() {
  const { isStaff, currentOrgSlug, currentOrganization } = useHive()
  const [creating, setCreating] = useState(false)
  const [activeTab, setActiveTab] = useState('teams')
  const toast = useToast()

  const { data: teams, isLoading, isError, refetch } = useTeams({ enabled: isStaff, orgSlug: currentOrgSlug })
  const mutations = useTeamMutations({ orgSlug: currentOrgSlug })

  if (!isStaff) {
    return (
      <div className="flex flex-col items-center gap-3 py-20 text-center">
        <ShieldAlert className="h-10 w-10 text-gray-300 dark:text-gray-600" />
        <p className="text-gray-500 dark:text-gray-400">Teams management requires admin access.</p>
      </div>
    )
  }

  async function handleCreate(teamName) {
    try {
      await mutations.create.mutateAsync({ team_name: teamName })
      toast.success(`Team "${teamName}" created`)
      setCreating(false)
    } catch (err) {
      toast.error(err?.response?.data?.error || 'Failed to create team')
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="hv-kicker mb-1">Org · structure</div>
          <h1 className="hv-page-title flex items-center gap-2.5">
            <Users className="h-6 w-6 text-amber-500" />
            Teams &amp; Capabilities
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            {currentOrganization
              ? `Manage ${currentOrganization.name || currentOrganization.slug} teams and their capabilities.`
              : 'Manage global agent team rosters and capability bundles. Changes reflect immediately in Pulse routing.'}
          </p>
        </div>
        {activeTab === 'teams' && (
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={() => refetch()}
              className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-700"
            >
              Refresh
            </button>
            <button
              type="button"
              onClick={() => setCreating(true)}
              className="inline-flex items-center gap-1.5 rounded-lg bg-gradient-to-b from-amber-400 to-amber-600 px-3 py-2 text-sm font-semibold text-gray-950 shadow-[0_2px_12px_-2px_rgba(245,158,11,0.5)] transition-transform hover:-translate-y-px"
            >
              <Plus className="h-4 w-4" />
              New team
            </button>
          </div>
        )}
      </div>

      {/* Tab bar */}
      <TabBar activeTab={activeTab} onTabChange={setActiveTab} />

      {/* Tab: Teams */}
      {activeTab === 'teams' && (
        <div className="space-y-5">
          {/* Create form */}
          {creating && (
            <CreateTeamForm onDone={() => setCreating(false)} onCreate={handleCreate} />
          )}

          {/* List */}
          {isLoading && !teams && (
            <div className="space-y-3">
              {Array.from({ length: 4 }, (_, i) => <TeamSkeleton key={i} />)}
            </div>
          )}

          {isError && (
            <div className="flex flex-col items-center gap-3 py-16 text-center">
              <AlertTriangle className="h-8 w-8 text-red-400" />
              <p className="text-gray-500">Failed to load teams.</p>
              <button
                type="button"
                onClick={() => refetch()}
                className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-700"
              >
                Retry
              </button>
            </div>
          )}

          {teams && teams.length === 0 && (
            <div className="flex flex-col items-center gap-3 py-16 text-center">
              <Users className="h-10 w-10 text-gray-300 dark:text-gray-600" />
              <p className="text-gray-500 dark:text-gray-400">No teams yet. Create one to get started.</p>
            </div>
          )}

          {teams && teams.length > 0 && (
            <div className="space-y-3">
              {teams.map((team) => (
                <TeamCard
                  key={`${team.organization_slug || team.org_slug || currentOrgSlug || ''}:${team.team_name || team.slug}`}
                  team={team}
                  orgSlug={team.organization_slug || team.org_slug || currentOrgSlug}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Tab: Capabilities */}
      {activeTab === 'capabilities' && (
        <CapabilityCatalogPanel />
      )}
    </div>
  )
}
