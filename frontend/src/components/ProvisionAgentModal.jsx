import { useState } from 'react'
import { X, Sparkles } from 'lucide-react'
import { useFleetCreate } from '../hooks/useAgents'
import { fleetApi } from '../services/api'
import { useToast } from './Toast'
import { errorMessage } from '../lib/format'

const RESOURCE_PROFILES = [
  { value: 'small', label: 'Small' },
  { value: 'standard', label: 'Standard' },
  { value: 'heavy', label: 'Heavy' },
]

const ARCHS = [
  { value: 'arm64', label: 'arm64 (DGX Spark / Longhorn pool)' },
  { value: 'amd64', label: 'amd64 (x86 nodes)' },
]

const FIELD =
  'w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 px-3 py-2 text-sm focus:ring-2 focus:ring-primary-500 focus:outline-none'
const LABEL = 'block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1'

export default function ProvisionAgentModal({ open, onClose }) {
  const toast = useToast()
  const create = useFleetCreate()
  const [provisionNow, setProvisionNow] = useState(true)
  const [form, setForm] = useState({
    agent_username: '',
    display_name: '',
    role: '',
    image: '',
    resource_profile: 'standard',
    arch: 'arm64',
  })

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  // We need the created agent's id to optionally provision; create returns it.
  const submit = async (e) => {
    e.preventDefault()
    const payload = {
      agent_username: form.agent_username.trim(),
      display_name: form.display_name.trim(),
      role: form.role.trim(),
      image: form.image.trim(),
      resource_profile: form.resource_profile,
      arch: form.arch,
    }
    // Strip empty optional strings so backend defaults apply.
    Object.keys(payload).forEach((k) => {
      if (payload[k] === '') delete payload[k]
    })
    try {
      const created = await create.mutateAsync(payload)
      if (provisionNow && created?.id) {
        try {
          // Provision immediately after create.
          await fleetApi.provision(created.id)
          toast.success(`Agent @${created.agent_username} created and provisioning`)
        } catch (perr) {
          toast.error(`Created, but provision failed: ${errorMessage(perr)}`)
        }
      } else {
        toast.success(`Agent @${created.agent_username} created (pending)`)
      }
      onClose(true)
      setForm({
        agent_username: '',
        display_name: '',
        role: '',
        image: '',
        resource_profile: 'standard',
        arch: 'arm64',
      })
    } catch (err) {
      toast.error(errorMessage(err, 'Failed to create agent'))
    }
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-[85] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50" onClick={() => onClose(false)} />
      <div className="relative w-full max-w-lg rounded-xl bg-white dark:bg-gray-800 shadow-xl border border-gray-200 dark:border-gray-700 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-5 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-2">
            <div className="p-2 rounded-lg bg-amber-100 dark:bg-amber-900/30">
              <Sparkles className="h-5 w-5 text-amber-600 dark:text-amber-400" />
            </div>
            <h2 className="text-lg font-bold">Provision new agent</h2>
          </div>
          <button onClick={() => onClose(false)} className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700">
            <X className="h-5 w-5" />
          </button>
        </div>

        <form onSubmit={submit} className="p-5 space-y-4">
          <div>
            <label className={LABEL}>
              Username <span className="text-red-500">*</span>
            </label>
            <input
              required
              value={form.agent_username}
              onChange={set('agent_username')}
              placeholder="e.g. banto"
              pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?"
              title="DNS-1123 label: lowercase letters, digits and hyphens"
              className={FIELD}
            />
            <p className="mt-1 text-xs text-gray-500">
              DNS-1123 handle (lowercase, ≤47 chars). Drives all k8s resource names.
            </p>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className={LABEL}>Display name</label>
              <input value={form.display_name} onChange={set('display_name')} placeholder="Banto" className={FIELD} />
            </div>
            <div>
              <label className={LABEL}>Role</label>
              <input value={form.role} onChange={set('role')} placeholder="Data Analyst" className={FIELD} />
            </div>
          </div>

          <div>
            <label className={LABEL}>Image / model</label>
            <input
              value={form.image}
              onChange={set('image')}
              placeholder="(blank → default shizuha-agent-runtime)"
              className={FIELD}
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className={LABEL}>Resource profile</label>
              <select value={form.resource_profile} onChange={set('resource_profile')} className={FIELD}>
                {RESOURCE_PROFILES.map((p) => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className={LABEL}>Architecture</label>
              <select value={form.arch} onChange={set('arch')} className={FIELD}>
                {ARCHS.map((a) => (
                  <option key={a.value} value={a.value}>{a.label}</option>
                ))}
              </select>
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <input
              type="checkbox"
              checked={provisionNow}
              onChange={(e) => setProvisionNow(e.target.checked)}
              className="rounded border-gray-300 text-primary-600 focus:ring-primary-500"
            />
            Provision to the cluster immediately after creating
          </label>

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={() => onClose(false)}
              className="px-4 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={create.isPending || !form.agent_username.trim()}
              className="px-4 py-2 text-sm rounded-lg bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-50"
            >
              {create.isPending ? 'Creating…' : 'Create agent'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
