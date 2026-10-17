import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertTriangle,
  ArrowRight,
  Building2,
  CheckCircle2,
  Clock,
  Loader2,
  Rocket,
  ShieldCheck,
  Sparkles,
  Users,
  Workflow,
} from 'lucide-react'
import clsx from 'clsx'
import { orgProvisioningApi, orgTemplatesApi } from '../services/api'
import { ErrorState, LoadingState } from '../components/States'
import { useToast } from '../components/Toast'

function slugify(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80)
}

const STEP_LABELS = [
  'Choose template',
  'Name organization',
  'Provision workspace',
  'Launch dashboard',
]

const STATUS_STYLES = {
  done: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
  running: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  pending: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
  manual_review: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  failed: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
}

function TemplateIcon({ template }) {
  const slug = template?.slug || ''
  if (slug.includes('solo')) return <Rocket className="h-5 w-5" />
  if (slug.includes('content')) return <Sparkles className="h-5 w-5" />
  if (slug.includes('ops')) return <Workflow className="h-5 w-5" />
  return <Building2 className="h-5 w-5" />
}

function Stepper({ current }) {
  return (
    <div className="grid gap-2 sm:grid-cols-4">
      {STEP_LABELS.map((label, index) => {
        const active = index === current
        const complete = index < current
        return (
          <div
            key={label}
            className={clsx(
              'rounded-xl border px-3 py-2 text-sm',
              complete && 'border-green-200 bg-green-50 text-green-700 dark:border-green-900 dark:bg-green-900/20 dark:text-green-300',
              active && 'border-primary-300 bg-primary-50 text-primary-700 dark:border-primary-800 dark:bg-primary-900/20 dark:text-primary-300',
              !active && !complete && 'border-gray-200 bg-white text-gray-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400'
            )}
          >
            <div className="flex items-center gap-2">
              {complete ? <CheckCircle2 className="h-4 w-4" /> : <span className="font-semibold">{index + 1}</span>}
              <span>{label}</span>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function TemplateCard({ template, selected, onSelect }) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={clsx(
        'rounded-2xl border bg-white p-5 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md dark:bg-gray-800',
        selected ? 'border-primary-400 ring-2 ring-primary-100 dark:border-primary-500 dark:ring-primary-900/40' : 'border-gray-200 dark:border-gray-700'
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-amber-500 text-white">
            <TemplateIcon template={template} />
          </div>
          <div>
            <h3 className="font-semibold text-gray-950 dark:text-white">{template.name}</h3>
            <p className="text-xs text-gray-500 dark:text-gray-400">{template.category || 'starter'} · {template.recommended_tier || 'pilot'}</p>
          </div>
        </div>
        {selected && <CheckCircle2 className="h-5 w-5 text-primary-600" />}
      </div>
      <p className="mt-4 min-h-12 text-sm text-gray-600 dark:text-gray-300">{template.description}</p>
      <div className="mt-4 grid grid-cols-3 gap-2 text-center text-xs">
        <div className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/50">
          <div className="font-semibold text-gray-900 dark:text-white">{template.team_count}</div>
          <div className="text-gray-500">teams</div>
        </div>
        <div className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/50">
          <div className="font-semibold text-gray-900 dark:text-white">{template.total_agent_count}</div>
          <div className="text-gray-500">agents</div>
        </div>
        <div className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/50">
          <div className="font-semibold text-gray-900 dark:text-white">{template.starter_task_count}</div>
          <div className="text-gray-500">tasks</div>
        </div>
      </div>
    </button>
  )
}

function TeamPreview({ template }) {
  const teams = template?.teams_preview || []
  if (!teams.length) return null
  return (
    <div className="hv-panel p-5">
      <div className="mb-4 flex items-center gap-2">
        <Users className="h-5 w-5 text-gray-500" />
        <h2 className="font-semibold">What gets created</h2>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {teams.map((team) => (
          <div key={team.slug} className="rounded-xl border border-gray-100 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40">
            <div className="flex items-center justify-between gap-3">
              <span className="font-medium text-gray-900 dark:text-white">{team.name}</span>
              <span className="rounded-full bg-white px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                {team.agent_count} agent{team.agent_count === 1 ? '' : 's'}
              </span>
            </div>
            <p className="mt-1 text-xs text-gray-500">{Array.isArray(team.capabilities) ? team.capabilities.join(', ') : team.capabilities}</p>
          </div>
        ))}
      </div>
    </div>
  )
}

function ProgressView({ run }) {
  if (!run) return null
  const progress = run.progress || { done: 0, total: 1, percent: 0 }
  const firstTask = run.first_task
  const ready = run.status === 'ready'
  const manualReview = run.status === 'manual_review'
  return (
    <div className="space-y-4">
      <div className="hv-panel p-5">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-lg font-semibold">Provisioning {run.org_name}</h2>
            <p className="text-sm text-gray-500">
              {run.dry_run ? 'Safe preview run — no external resources are mutated yet.' : 'Real provisioning request captured by the HIVE-320 saga.'}
            </p>
          </div>
          <span className={clsx('inline-flex w-fit items-center gap-1.5 rounded-full px-3 py-1 text-sm font-medium', STATUS_STYLES[run.status] || STATUS_STYLES.pending)}>
            {run.status === 'running' && <Loader2 className="h-4 w-4 animate-spin" />}
            {run.status === 'ready' && <CheckCircle2 className="h-4 w-4" />}
            {run.status === 'manual_review' && <Clock className="h-4 w-4" />}
            {run.status.replace('_', ' ')}
          </span>
        </div>
        <div className="mt-5 h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700">
          <div className="h-full rounded-full bg-primary-600 transition-all" style={{ width: `${progress.percent || 0}%` }} />
        </div>
        <p className="mt-2 text-xs text-gray-500">{progress.done} of {progress.total} steps complete</p>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        {(run.steps || []).map((step) => (
          <div key={step.slug} className="hv-panel p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h3 className="font-medium text-gray-950 dark:text-white">{step.title}</h3>
                {step.message && <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">{step.message}</p>}
              </div>
              <span className={clsx('shrink-0 rounded-full px-2 py-0.5 text-xs font-medium', STATUS_STYLES[step.status] || STATUS_STYLES.pending)}>
                {step.status.replace('_', ' ')}
              </span>
            </div>
          </div>
        ))}
      </div>

      {ready && (
        <div className="rounded-2xl border border-green-200 bg-green-50 p-5 dark:border-green-900 dark:bg-green-900/20">
          <div className="flex items-start gap-3">
            <CheckCircle2 className="mt-0.5 h-5 w-5 text-green-600 dark:text-green-300" />
            <div>
              <h3 className="font-semibold text-green-900 dark:text-green-100">Organization plan is ready</h3>
              <p className="mt-1 text-sm text-green-800 dark:text-green-200">
                {run.dry_run
                  ? 'This preview shows exactly what the live adapters will create once enabled.'
                  : 'Your organization is ready. Open the dashboard and give the team its first task.'}
              </p>
              {firstTask && (
                <div className="mt-4 rounded-xl bg-white p-4 text-sm text-gray-700 shadow-sm dark:bg-gray-800 dark:text-gray-200">
                  <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">Suggested first task</div>
                  <div className="mt-1 font-medium">{firstTask.title}</div>
                  <div className="text-xs text-gray-500">Assigned to {firstTask.assignment_group || 'the right team'} · {firstTask.workflow || 'autonomous-dev'}</div>
                </div>
              )}
              {run.dashboard_path && (
                <Link
                  to={run.dashboard_path}
                  className="mt-4 inline-flex items-center gap-2 rounded-lg bg-green-600 px-4 py-2 text-sm font-medium text-white hover:bg-green-700"
                >
                  Open dashboard preview <ArrowRight className="h-4 w-4" />
                </Link>
              )}
            </div>
          </div>
        </div>
      )}

      {manualReview && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50 p-5 dark:border-amber-900 dark:bg-amber-900/20">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 text-amber-600 dark:text-amber-300" />
            <div>
              <h3 className="font-semibold text-amber-900 dark:text-amber-100">Waiting for live provisioning adapters</h3>
              <p className="mt-1 text-sm text-amber-800 dark:text-amber-200">
                Hive captured the request but stopped before mutating external services. That fail-closed gate prevents the UI from claiming a customer org is live until ID/Pulse/Wiki/Connect/Drive/Fleet/Cortex adapters are verified.
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default function CreateOrganization() {
  const toast = useToast()
  const [templates, setTemplates] = useState([])
  const [selectedSlug, setSelectedSlug] = useState('')
  const [orgName, setOrgName] = useState('')
  const [planSlug, setPlanSlug] = useState('pilot')
  const [previewOnly, setPreviewOnly] = useState(true)
  const [run, setRun] = useState(null)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let mounted = true
    setLoading(true)
    orgTemplatesApi.list()
      .then((response) => {
        if (!mounted) return
        const results = response.data?.results || []
        setTemplates(results)
        if (results.length) {
          setSelectedSlug(results[0].slug)
          setPlanSlug(results[0].recommended_tier || 'pilot')
        }
      })
      .catch(() => {
        if (mounted) setError('Could not load organization templates.')
      })
      .finally(() => mounted && setLoading(false))
    return () => { mounted = false }
  }, [])

  const selectedTemplate = useMemo(
    () => templates.find((template) => template.slug === selectedSlug) || templates[0],
    [templates, selectedSlug]
  )

  const orgSlug = slugify(orgName)
  const canSubmit = selectedTemplate && orgName.trim().length >= 2 && !submitting
  const currentStep = run?.status === 'ready' ? 3 : run ? 2 : orgName ? 1 : 0

  useEffect(() => {
    if (selectedTemplate?.recommended_tier) setPlanSlug(selectedTemplate.recommended_tier)
  }, [selectedTemplate?.slug])

  useEffect(() => {
    if (!run?.id || ['ready', 'failed', 'manual_review', 'torn_down'].includes(run.status)) return undefined
    const interval = setInterval(() => {
      orgProvisioningApi.get(run.id).then((response) => setRun(response.data)).catch(() => {})
    }, 3000)
    return () => clearInterval(interval)
  }, [run?.id, run?.status])

  async function submit(e) {
    e.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError('')
    try {
      const response = await orgProvisioningApi.create({
        org_name: orgName.trim(),
        org_slug: orgSlug,
        template_slug: selectedTemplate.slug,
        plan_slug: planSlug || selectedTemplate.recommended_tier || 'pilot',
        dry_run: previewOnly,
      })
      setRun(response.data)
      toast.success(previewOnly ? 'Organization preview created.' : 'Provisioning request started.')
    } catch (err) {
      const detail = err.response?.data?.error || err.response?.data?.detail || 'Could not start organization provisioning.'
      setError(detail)
      toast.error(detail)
    } finally {
      setSubmitting(false)
    }
  }

  if (loading) return <LoadingState label="Loading organization templates…" />
  if (error && !templates.length) return <ErrorState message={error} onRetry={() => window.location.reload()} />

  return (
    <div className="space-y-6">
      <div className="rounded-3xl border border-primary-100 bg-gradient-to-br from-primary-50 via-white to-amber-50 p-6 shadow-sm dark:border-primary-900 dark:from-primary-950/40 dark:via-gray-900 dark:to-amber-950/20">
        <div className="flex flex-col gap-5 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="mb-3 inline-flex items-center gap-2 rounded-full bg-white/80 px-3 py-1 text-xs font-medium text-primary-700 shadow-sm dark:bg-gray-800 dark:text-primary-300">
              <Sparkles className="h-3.5 w-3.5" />
              Guided autonomous-org setup
            </div>
            <h1 className="text-3xl font-bold tracking-tight text-gray-950 dark:text-white">Create your organization</h1>
            <p className="mt-2 max-w-3xl text-gray-600 dark:text-gray-300">
              Pick a proven template, name the org, and watch Hive plan the teams, agents, workspaces, and first tasks. Defaults stay customer-safe and scrub internal-only access.
            </p>
          </div>
          <div className="rounded-2xl bg-white/80 p-4 text-sm shadow-sm dark:bg-gray-800/80">
            <div className="flex items-center gap-2 font-medium text-gray-900 dark:text-white">
              <ShieldCheck className="h-4 w-4 text-green-600" />
              Customer-safe by default
            </div>
            <p className="mt-1 max-w-xs text-gray-500 dark:text-gray-400">Sensitive access flags are stripped from template plans unless a tenant tier explicitly allows them.</p>
          </div>
        </div>
      </div>

      <Stepper current={currentStep} />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="space-y-6">
          <div className="grid gap-4 md:grid-cols-2">
            {templates.map((template) => (
              <TemplateCard
                key={template.slug}
                template={template}
                selected={selectedTemplate?.slug === template.slug}
                onSelect={() => setSelectedSlug(template.slug)}
              />
            ))}
          </div>

          <TeamPreview template={selectedTemplate} />

          {run && <ProgressView run={run} />}
        </div>

        <form onSubmit={submit} className="h-fit rounded-2xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-gray-800">
          <h2 className="text-lg font-semibold">Name and provision</h2>
          <p className="mt-1 text-sm text-gray-500">Sensible defaults are filled in; you can rename the org later.</p>

          <label className="mt-5 block text-sm font-medium text-gray-700 dark:text-gray-300">
            Organization name
            <input
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
              placeholder="Acme AI Ops"
              className="mt-2 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-primary-500 focus:ring-2 focus:ring-primary-100 dark:border-gray-600 dark:bg-gray-900 dark:focus:ring-primary-900/40"
            />
          </label>

          <div className="mt-3 rounded-lg bg-gray-50 px-3 py-2 text-sm text-gray-600 dark:bg-gray-900/40 dark:text-gray-300">
            Slug: <span className="font-mono">{orgSlug || 'organization-slug'}</span>
          </div>

          <label className="mt-4 block text-sm font-medium text-gray-700 dark:text-gray-300">
            Plan / size
            <select
              value={planSlug}
              onChange={(e) => setPlanSlug(e.target.value)}
              className="mt-2 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-primary-500 focus:ring-2 focus:ring-primary-100 dark:border-gray-600 dark:bg-gray-900 dark:focus:ring-primary-900/40"
            >
              <option value="pilot">Pilot / free</option>
              <option value="solo">Solo</option>
              <option value="team">Team</option>
            </select>
          </label>

          <label className="mt-4 flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm dark:border-amber-900 dark:bg-amber-900/20">
            <input
              type="checkbox"
              checked={previewOnly}
              onChange={(e) => setPreviewOnly(e.target.checked)}
              className="mt-1"
            />
            <span>
              <span className="font-medium text-amber-900 dark:text-amber-100">Run as safe preview</span>
              <span className="mt-1 block text-amber-800 dark:text-amber-200">Recommended while the live provisioning adapters are still gated; uncheck to create a real saga that stops at manual review today.</span>
            </span>
          </label>

          {error && (
            <div className="mt-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-900/20 dark:text-red-300">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={!canSubmit}
            className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-primary-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
            {previewOnly ? 'Create organization preview' : 'Start provisioning'}
          </button>

          {selectedTemplate && (
            <div className="mt-5 space-y-2 text-xs text-gray-500 dark:text-gray-400">
              {(selectedTemplate.what_you_get || []).map((item) => (
                <div key={item} className="flex items-center gap-2">
                  <CheckCircle2 className="h-3.5 w-3.5 text-green-600" />
                  <span>{item}</span>
                </div>
              ))}
            </div>
          )}
        </form>
      </div>
    </div>
  )
}
