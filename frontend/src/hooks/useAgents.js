import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { capabilitiesApi, fleetApi, clawsApi, pulseOrgApi, fleetEfficiencyApi, teamsApi, daemonIdentityApi, credentialsApi, codexDeviceAuthApi, tokenPoolsApi, harnessVersionsApi } from '../services/api'
import { unwrapList } from '../lib/format'
import { setTransient, clearTransient, applyTransient, hasActiveTransient } from '../lib/transientStatus'
import { agentHasLiveActivity } from '../lib/activityPhase'

const POLL_MS = 15000
const WORKLOAD_POLL_MS = 30000
const FLEET_PAGE_SIZE = 100
// Game-tick polling while any agent is mid-transition (stopping/starting/…):
// the UI must converge to the real state in seconds, not the idle 15s cadence.
const TRANSITION_POLL_MS = 3000
const ACTIVITY_POLL_MS = 4000
const fleetPollInterval = (query) => {
  if (hasActiveTransient()) return TRANSITION_POLL_MS
  const agents = query?.state?.data
  if (Array.isArray(agents) && agents.some(agentHasLiveActivity)) return ACTIVITY_POLL_MS
  return POLL_MS
}

async function listAllFleetAgents({ ownerOnly = false, fleetRead = false, orgSlug = '' } = {}) {
  const agents = []
  const meta = { rosterDegraded: false, rosterStaleAgeSeconds: null }
  let page = 1

  for (let i = 0; i < 50; i += 1) {
    const params = { page_size: FLEET_PAGE_SIZE, page }
    if (ownerOnly) params.owner_only = 1
    if (fleetRead) params.fleet_read = 1
    if (orgSlug) params.org_slug = orgSlug
    // Cards poll frequently; use the compressed summary projection. Opening a
    // card already triggers useFleetAgent(id), which fetches the full editor
    // contract only when it is actually needed.
    const response = await fleetApi.cards(params, { timeout: 20000 })
    const batch = unwrapList(response.data)
    agents.push(...batch)

    if (response.data?.roster_degraded) meta.rosterDegraded = true
    if (response.data?.roster_stale_age_seconds != null) {
      meta.rosterStaleAgeSeconds = response.data.roster_stale_age_seconds
    }

    if (!response.data?.next || batch.length === 0) break
    page += 1
  }

  agents._hiveMeta = meta
  return agents
}

// Same-key transient failures may retain useful data; an explicit access denial
// must not expose that cached snapshot as a successful response.
function permittedSnapshot(query) {
  return [401, 403].includes(query.error?.response?.status) ? { ...query, data: undefined } : query
}

// ── Fleet ───────────────────────────────────────────────────────────────────
export function useFleetAgents({ enabled = true, ownerOnly = false, fleetRead = false, orgSlug = '' } = {}) {
  return permittedSnapshot(useQuery({
    queryKey: ['fleet', 'list', { ownerOnly, fleetRead, orgSlug }],
    queryFn: async () => {
      const agents = await listAllFleetAgents({ ownerOnly, fleetRead, orgSlug })
      // Transitional overlay (stopping/starting/…) survives poll refetches —
      // the stale backend status must not overwrite the in-flight transition.
      const mapped = agents.map(applyTransient)
      mapped._hiveMeta = agents._hiveMeta
      return mapped
    },
    refetchInterval: fleetPollInterval,
    retry: (count, error) => {
      const status = error?.response?.status
      if (status === 401 || status === 403) return false
      return count < 6
    },
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 8000),
    enabled,
  }))
}

export function useFleetAgent(id) {
  return useQuery({
    queryKey: ['fleet', 'detail', id],
    queryFn: () => fleetApi.get(id).then((r) => applyTransient(r.data)),
    refetchInterval: fleetPollInterval,
    enabled: !!id,
  })
}

export function useFleetEvents(id) {
  return useQuery({
    queryKey: ['fleet', 'events', id],
    queryFn: () => fleetApi.events(id, { page_size: 50 }).then((r) => unwrapList(r.data)),
    refetchInterval: POLL_MS,
    enabled: !!id,
  })
}

// HIVE-175: live runtime activity transcript for an agent (messages / tool calls
// / results). Polls a touch faster than lifecycle events so the operator can watch
// the agent work in near-real-time. `enabled` gates it to when the drawer is open.
export function useFleetActivity(id, { enabled = true } = {}) {
  return permittedSnapshot(useQuery({
    queryKey: ['fleet', 'activity', id],
    queryFn: () => fleetApi.activity(id, { limit: 200 }).then((r) => r.data),
    refetchInterval: 8000,
    enabled: !!id && enabled,
    retry: (count, error) => {
      const status = error?.response?.status
      if (status === 401 || status === 403) return false
      return count < 2
    },
  }))
}

export function useFleetMutations(id, { orgSlug = '' } = {}) {
  const qc = useQueryClient()
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['fleet'] })
    qc.invalidateQueries({ queryKey: ['pulse', 'org-chart-snapshot'] })
  }
  // Instant transitional paint (operator 2026-07-11 "think of hive like a
  // game"): the moment a lifecycle action fires, every cached view of this
  // agent flips to stopping/starting/restarting — no waiting for the poll.
  // setTransient keeps it sticky until the backend reports the terminal state.
  const paintStatus = (status) => {
    qc.setQueryData(['fleet', 'detail', id], (agent) => (agent ? { ...agent, status } : agent))
    qc.setQueriesData({ queryKey: ['fleet', 'list'] }, (agents) => {
      if (!Array.isArray(agents)) return agents
      const mapped = agents.map((a) => (String(a.id) === String(id) ? { ...a, status } : a))
      mapped._hiveMeta = agents._hiveMeta
      return mapped
    })
  }
  const transition = (status, doneStatuses, minMs = 0) => () => {
    setTransient(id, status, doneStatuses, minMs)
    paintStatus(status)
  }
  // Instant CRUD paint (operator 2026-07-12 "near instant, no delays"):
  // archive/unarchive flip the flag in every cached view the moment the click
  // lands; delete removes the card immediately. The backend now overlays
  // archived/deleted state at serve time too, so the follow-up refetch can
  // never paint the pre-mutation state back (the old "archived but nothing
  // happened" staleness).
  const paintPatch = (patch) => {
    qc.setQueryData(['fleet', 'detail', id], (agent) => (agent ? { ...agent, ...patch } : agent))
    qc.setQueriesData({ queryKey: ['fleet', 'list'] }, (agents) => {
      if (!Array.isArray(agents)) return agents
      const mapped = agents.map((a) => (String(a.id) === String(id) ? { ...a, ...patch } : a))
      mapped._hiveMeta = agents._hiveMeta
      return mapped
    })
  }
  const paintRemove = () => {
    qc.setQueriesData({ queryKey: ['fleet', 'list'] }, (agents) => {
      if (!Array.isArray(agents)) return agents
      const filtered = agents.filter((a) => String(a.id) !== String(id))
      filtered._hiveMeta = agents._hiveMeta
      return filtered
    })
  }
  // Action failed → drop the transitional state and refetch the truth.
  const revert = () => {
    clearTransient(id)
    invalidate()
  }
  return {
    provision: useMutation({
      mutationFn: () => fleetApi.provision(id),
      onMutate: transition('starting', ['running', 'failed', 'error', 'blocked']),
      onSuccess: invalidate,
      onError: revert,
    }),
    deprovision: useMutation({
      mutationFn: () => fleetApi.deprovision(id),
      onMutate: transition('stopping', ['hibernated', 'stopped', 'offline', 'failed', 'error']),
      onSuccess: invalidate,
      onError: revert,
    }),
    stop: useMutation({
      mutationFn: () => fleetApi.stop(id),
      onMutate: transition('stopping', ['stopped', 'offline', 'failed', 'error']),
      onSuccess: invalidate,
      onError: revert,
    }),
    restart: useMutation({
      mutationFn: () => fleetApi.restart(id),
      // running -> running: hold "restarting" ≥10s so the action visibly happened.
      onMutate: transition('restarting', ['running', 'failed', 'error'], 10_000),
      onSuccess: invalidate,
      onError: revert,
    }),
    archive: useMutation({
      mutationFn: (data) => fleetApi.archive(id, data),
      onMutate: () => paintPatch({ archived: true }),
      onSuccess: invalidate,
      onError: revert,
    }),
    unarchive: useMutation({
      mutationFn: () => fleetApi.unarchive(id),
      onMutate: () => paintPatch({ archived: false }),
      onSuccess: invalidate,
      onError: revert,
    }),
    claim: useMutation({ mutationFn: (data) => fleetApi.claim(id, data), onSuccess: invalidate }),
    remove: useMutation({
      mutationFn: () => fleetApi.remove(id),
      onMutate: () => { paintPatch({ status: 'deleting' }); paintRemove() },
      onSuccess: invalidate,
      onError: revert,
    }),
    update: useMutation({ mutationFn: (data) => fleetApi.update(id, data), onSuccess: invalidate }),
    create: useMutation({ mutationFn: (data) => fleetApi.create(data), onSuccess: invalidate }),
    setTeamPlacement: useMutation({ mutationFn: (data) => fleetApi.setTeamPlacement(id, { ...data, org_slug: orgSlug || data?.org_slug }), onSuccess: invalidate }),
    createTeam: useMutation({ mutationFn: (data) => fleetApi.createTeam({ ...data, org_slug: orgSlug || data?.org_slug }), onSuccess: invalidate }),
  }
}

export function useFleetTeamPlacement(id, { enabled = true, orgSlug = '' } = {}) {
  return useQuery({
    queryKey: ['fleet', 'team-placement', id, orgSlug],
    queryFn: () => fleetApi.teamPlacement(id, orgSlug ? { org_slug: orgSlug } : {}).then((r) => r.data),
    enabled: !!id && enabled,
    retry: false,
  })
}

export function useFleetCreate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => fleetApi.create(data).then((r) => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['fleet'] }),
  })
}

export function usePulseOrgSnapshot({ enabled = true, orgSlug = '' } = {}) {
  return useQuery({
    queryKey: ['pulse', 'org-chart-snapshot', { orgSlug }],
    queryFn: () => pulseOrgApi.snapshot(orgSlug ? { org_slug: orgSlug } : {}).then((r) => r.data),
    refetchInterval: WORKLOAD_POLL_MS,
    enabled,
    retry: false,
  })
}

export function useFleetRuntimeOptions({ enabled = true } = {}) {
  return useQuery({
    queryKey: ['fleet', 'runtime-options'],
    queryFn: () => fleetApi.runtimeOptions().then((r) => r.data),
    staleTime: 60_000,
    enabled,
    retry: false,
  })
}

export function useHarnessVersions({ enabled = true } = {}) {
  return useQuery({
    queryKey: ['fleet', 'harness-versions'],
    queryFn: () => harnessVersionsApi.list().then((r) => r.data),
    refetchInterval: 60_000,
    staleTime: 30_000,
    enabled,
    retry: false,
  })
}

export function useHarnessUpgrade() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ harness, targetVersion }) => harnessVersionsApi.upgrade(harness, targetVersion).then((r) => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['fleet', 'harness-versions'] }),
  })
}

export function useFleetEfficiency({ enabled = true } = {}) {
  return useQuery({
    queryKey: ['fleet', 'efficiency'],
    queryFn: () => fleetEfficiencyApi.snapshot().then((r) => r.data?.agents ?? {}),
    refetchInterval: WORKLOAD_POLL_MS,
    enabled,
    retry: false,
  })
}

// ── Claws ─────────────────────────────────────────────────────────────────
export function useClaws({ enabled = true } = {}) {
  return useQuery({
    queryKey: ['claws', 'list'],
    queryFn: () => clawsApi.list({ page_size: 100 }).then((r) => unwrapList(r.data).map(applyTransient)),
    refetchInterval: fleetPollInterval,
    enabled,
    retry: false,
  })
}

export function useClaw(id) {
  return useQuery({
    queryKey: ['claws', 'detail', id],
    queryFn: () => clawsApi.get(id).then((r) => applyTransient(r.data)),
    refetchInterval: fleetPollInterval,
    enabled: !!id,
  })
}

export function useClawWhatsAppLinks(id) {
  return useQuery({
    queryKey: ['claws', 'whatsapp-links', id],
    queryFn: () => clawsApi.whatsappLinks(id).then((r) => unwrapList(r.data)),
    refetchInterval: POLL_MS,
    enabled: !!id,
  })
}

export function useClawEvents(id) {
  return useQuery({
    queryKey: ['claws', 'events', id],
    queryFn: () => clawsApi.events(id).then((r) => unwrapList(r.data)),
    refetchInterval: POLL_MS,
    enabled: !!id,
  })
}

// ── Teams (HIVE-239) ──────────────────────────────────────────────────────
function unwrapTeams(data) {
  // Pulse /admin/team-roster/ (the authoritative source the Hive backend proxies
  // verbatim) returns {teams: [...]}. Check that first; keep results/array for
  // any paginated or bare-array responses. Missing this is what showed "No teams
  // yet" while Pulse held 23 active TeamRoster rows (HIVE-239 shape mismatch).
  if (Array.isArray(data?.teams)) return data.teams
  if (Array.isArray(data?.results)) return data.results
  if (Array.isArray(data)) return data
  return []
}

export function useTeams({ enabled = true, orgSlug = '' } = {}) {
  return useQuery({
    queryKey: ['teams', 'list', { orgSlug }],
    queryFn: () => teamsApi.list(orgSlug ? { org_slug: orgSlug } : {}).then((r) => unwrapTeams(r.data)),
    refetchInterval: 30_000,
    enabled,
    retry: false,
  })
}

export function useTeamMembers(name, { enabled = true, orgSlug = '' } = {}) {
  return useQuery({
    queryKey: ['teams', 'members', name, orgSlug],
    queryFn: () => teamsApi.listMembers(name, orgSlug ? { org_slug: orgSlug } : {}).then((r) => r.data),
    refetchInterval: 30_000,
    enabled: !!name && enabled,
    retry: false,
  })
}

export function useTeamMutations({ orgSlug = '' } = {}) {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: ['teams'] })
  return {
    create: useMutation({ mutationFn: (data) => teamsApi.create({ ...data, org_slug: orgSlug || data?.org_slug }).then((r) => r.data), onSuccess: invalidate }),
    update: useMutation({ mutationFn: ({ name, ...data }) => teamsApi.update(name, { ...data, org_slug: orgSlug || data?.org_slug }).then((r) => r.data), onSuccess: invalidate }),
    remove: useMutation({ mutationFn: (name) => teamsApi.delete(name, orgSlug ? { org_slug: orgSlug } : {}).then((r) => r.data), onSuccess: invalidate }),
    manageMember: useMutation({ mutationFn: ({ name, ...data }) => teamsApi.manageMember(name, { ...data, org_slug: orgSlug || data?.org_slug }).then((r) => r.data), onSuccess: invalidate }),
    setCapabilities: useMutation({ mutationFn: ({ name, ...data }) => teamsApi.setCapabilities(name, { ...data, org_slug: orgSlug || data?.org_slug }).then((r) => r.data), onSuccess: invalidate }),
  }
}

export function useCapabilities({ enabled = true } = {}) {
  return useQuery({
    queryKey: ['capabilities', 'catalog'],
    queryFn: () => capabilitiesApi.list().then((r) => r.data?.capabilities ?? []),
    enabled,
    staleTime: 30_000,
    retry: false,
  })
}

export function useTeamCapabilities(name, { enabled = true, orgSlug = '' } = {}) {
  return useQuery({
    queryKey: ['teams', 'capabilities', name, orgSlug],
    queryFn: () => teamsApi.capabilities(name, orgSlug ? { org_slug: orgSlug } : {}).then((r) => r.data),
    enabled: !!name && enabled,
    retry: false,
  })
}

export function useEffectiveCapabilities(agentId, { enabled = true } = {}) {
  return useQuery({
    queryKey: ['fleet', 'effective-capabilities', agentId],
    queryFn: () => capabilitiesApi.effective(agentId).then((r) => r.data),
    enabled: !!agentId && enabled,
    retry: false,
  })
}

export function useCapabilityMutations() {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: ['capabilities'] })
  return {
    create: useMutation({ mutationFn: (data) => capabilitiesApi.create(data).then((r) => r.data), onSuccess: invalidate }),
    update: useMutation({ mutationFn: ({ slug, ...data }) => capabilitiesApi.update(slug, data).then((r) => r.data), onSuccess: invalidate }),
    action: useMutation({ mutationFn: ({ slug, action, ...data }) => capabilitiesApi.action(slug, action, data).then((r) => r.data), onSuccess: invalidate }),
  }
}

// ── Credential grants (HIVE-263) ─────────────────────────────────────────
function unwrapCredentials(data) {
  if (Array.isArray(data?.credentials)) return data.credentials
  if (Array.isArray(data?.results)) return data.results
  if (Array.isArray(data)) return data
  return []
}

export function useCredentialGrants({ enabled = true } = {}) {
  return useQuery({
    queryKey: ['credentials', 'list'],
    queryFn: () => credentialsApi.list().then((r) => unwrapCredentials(r.data)),
    refetchInterval: 30_000,
    enabled,
    retry: false,
  })
}

export function useCredentialAudit(id, { enabled = true } = {}) {
  return useQuery({
    queryKey: ['credentials', 'audit', id],
    queryFn: () => credentialsApi.audit(id).then((r) => r.data?.audit_events || []),
    enabled: !!id && enabled,
    retry: false,
  })
}

export function useCredentialMutations() {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: ['credentials'] })
  return {
    create: useMutation({ mutationFn: (data) => credentialsApi.create(data).then((r) => r.data), onSuccess: invalidate }),
    update: useMutation({ mutationFn: ({ id, ...data }) => credentialsApi.update(id, data).then((r) => r.data), onSuccess: invalidate }),
    revoke: useMutation({ mutationFn: (id) => credentialsApi.revoke(id), onSuccess: invalidate }),
  }
}

export function useClawMutations(id) {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: ['claws'] })
  // Same instant transitional paint as fleet agents (see useFleetMutations).
  const clawTransition = (status, doneStatuses) => () => {
    setTransient(id, status, doneStatuses)
    qc.setQueryData(['claws', 'detail', id], (claw) => (claw ? { ...claw, status } : claw))
    qc.setQueriesData({ queryKey: ['claws', 'list'] }, (claws) => (
      Array.isArray(claws) ? claws.map((c) => (String(c.id) === String(id) ? { ...c, status } : c)) : claws
    ))
  }
  const clawRevert = () => {
    clearTransient(id)
    invalidate()
  }
  return {
    start: useMutation({
      mutationFn: () => clawsApi.start(id),
      onMutate: clawTransition('starting', ['running', 'failed', 'error']),
      onSuccess: invalidate,
      onError: clawRevert,
    }),
    stop: useMutation({
      mutationFn: () => clawsApi.stop(id),
      onMutate: clawTransition('stopping', ['stopped', 'failed', 'error']),
      onSuccess: invalidate,
      onError: clawRevert,
    }),
    rename: useMutation({ mutationFn: (name) => clawsApi.rename(id, { name }), onSuccess: invalidate }),
    remove: useMutation({ mutationFn: (confirmName) => clawsApi.remove(id, confirmName), onSuccess: invalidate }),
    setWhatsappStatus: useMutation({ mutationFn: ({ code, status }) => clawsApi.updateWhatsappLink(id, code, { status }), onSuccess: invalidate }),
    unlinkWhatsapp: useMutation({ mutationFn: (code) => clawsApi.unlinkWhatsapp(id, code), onSuccess: invalidate }),
    whatsappPairing: useMutation({ mutationFn: () => clawsApi.whatsappPairing(id).then((r) => r.data) }),
  }
}

// ── Daemon identity health (HIVE-254 P5) ─────────────────────────────────────
export function useDaemonIdentityHealth({ enabled = true } = {}) {
  return useQuery({
    queryKey: ['fleet', 'daemon-identity-health'],
    queryFn: () => daemonIdentityApi.health().then((r) => r.data?.daemons ?? []),
    refetchInterval: WORKLOAD_POLL_MS,
    enabled,
    retry: false,
  })
}

// ── Codex device-code OAuth (HIVE-581/HIVE-582) ──────────────────────────
export function usePersonalOpenAIPool({ enabled = true } = {}) {
  return useQuery({
    queryKey: ['token-pools', 'personal-openai'],
    queryFn: () =>
      tokenPoolsApi.list({ scope: 'user' }).then((r) => {
        const pools = Array.isArray(r.data) ? r.data : r.data?.results || []
        return pools.find((p) => p.provider === 'openai') || null
      }),
    enabled,
    staleTime: 30_000,
    retry: false,
  })
}

export function useCodexDeviceAuth() {
  const qc = useQueryClient()
  return {
    start: useMutation({
      mutationFn: () => codexDeviceAuthApi.start().then((r) => r.data),
    }),
    poll: useMutation({
      mutationFn: (data) => codexDeviceAuthApi.poll(data).then((r) => r.data),
      onSuccess: (data) => {
        if (data?.status === 'authorized') {
          qc.invalidateQueries({ queryKey: ['token-pools', 'personal-openai'] })
          qc.invalidateQueries({ queryKey: ['token-pools'] })
          qc.invalidateQueries({ queryKey: ['credential-health'] })
        }
      },
    }),
  }
}
