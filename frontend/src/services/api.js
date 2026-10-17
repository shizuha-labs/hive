import axios from 'axios'

// HIVE backend lives at /hive/api/ (Django REST). Override with VITE_API_URL in
// local dev. In production nginx proxies /hive/api/ -> hive-api:8030.
const API_BASE_URL = import.meta.env.VITE_API_URL || '/hive/api'
const ACCESS_TOKEN_KEY = 'shizuha_access_token'
const REFRESH_TOKEN_KEY = 'shizuha_refresh_token'
const USER_KEY = 'shizuha_user'
const REFRESH_URL = '/id/api/auth/refresh/'

let refreshPromise = null
let csrfBootstrapPromise = null

const CSRF_COOKIE_NAME = 'csrftoken'
const CSRF_HEADER_NAME = 'X-CSRFToken'

function cookieValue(name) {
  const prefix = `${encodeURIComponent(name)}=`
  const part = document.cookie.split(';').map((value) => value.trim()).find((value) => value.startsWith(prefix))
  return part ? decodeURIComponent(part.slice(prefix.length)) : ''
}

function isUnsafeMethod(method) {
  return !['get', 'head', 'options', 'trace'].includes(String(method || 'get').toLowerCase())
}

async function ensureCookieCsrf() {
  let token = cookieValue(CSRF_COOKIE_NAME)
  if (token) return token
  if (!csrfBootstrapPromise) {
    // Use raw axios so this bootstrap GET cannot recurse through the Hive
    // request interceptor. MeView calls Django get_token(), which causes
    // CsrfViewMiddleware to set the cookie on this same-origin response.
    csrfBootstrapPromise = axios.get(`${API_BASE_URL}/v1/me/`).finally(() => {
      csrfBootstrapPromise = null
    })
  }
  await csrfBootstrapPromise
  token = cookieValue(CSRF_COOKIE_NAME)
  if (!token) throw new Error('Hive did not issue a CSRF token')
  return token
}

function hiveContinuePath() {
  const path = `${window.location.pathname || '/hive/'}${window.location.search || ''}${window.location.hash || ''}`
  return path.startsWith('/hive') ? path : '/hive/'
}

function loginUrl() {
  return `/id/login?continue=${encodeURIComponent(hiveContinuePath())}`
}

function clearAuthState() {
  localStorage.removeItem(ACCESS_TOKEN_KEY)
  localStorage.removeItem(REFRESH_TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

function redirectToLogin() {
  if (!window.location.pathname.startsWith('/id/login')) {
    window.location.href = loginUrl()
  }
}

function decodeJwtPayload(token) {
  try {
    const parts = token.split('.')
    if (parts.length !== 3) return null
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=')
    return JSON.parse(atob(padded))
  } catch {
    return null
  }
}

function tokenExpiresSoon(token, skewSeconds = 30) {
  const payload = decodeJwtPayload(token)
  return Boolean(payload?.exp && payload.exp <= Math.floor(Date.now() / 1000) + skewSeconds)
}

function isTokenAuthError(error) {
  const status = error.response?.status
  if (status === 401) return true
  if (status !== 403) return false

  const detail = error.response?.data?.detail || error.response?.data?.error || ''
  return /invalid token|token is expired|token_not_valid|authentication credentials/i.test(String(detail))
}

async function refreshAccessToken() {
  const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY)
  if (!refreshToken) {
    throw new Error('No refresh token available')
  }

  if (!refreshPromise) {
    refreshPromise = axios
      .post(REFRESH_URL, { refresh: refreshToken })
      .then((response) => {
        const { access, refresh } = response.data || {}
        if (!access) throw new Error('Refresh response did not include an access token')
        localStorage.setItem(ACCESS_TOKEN_KEY, access)
        // HIVE-2383: the token can arrive/rotate AFTER the SPA mounted (this
        // refresh path is the in-app write). Mount-time-only readers (the
        // HiveProvider claims decode) would stay stale/null forever — announce
        // the change so subscribers re-read.
        window.dispatchEvent(new CustomEvent('shizuha:token-changed'))
        if (refresh) {
          localStorage.setItem(REFRESH_TOKEN_KEY, refresh)
        }
        return access
      })
      .finally(() => {
        refreshPromise = null
      })
  }

  return refreshPromise
}

const api = axios.create({
  baseURL: API_BASE_URL,
  xsrfCookieName: CSRF_COOKIE_NAME,
  xsrfHeaderName: CSRF_HEADER_NAME,
  headers: {
    'Content-Type': 'application/json',
  },
})

const pulseApi = axios.create({
  baseURL: import.meta.env.VITE_PULSE_API_URL || '/pulse/api',
  headers: {
    'Content-Type': 'application/json',
  },
})

function attachAuthInterceptors(client, { cookieCsrf = false } = {}) {
  // Attach the shizuha-id access token (same key the rest of the platform uses).
  client.interceptors.request.use(async (config) => {
    let token = localStorage.getItem(ACCESS_TOKEN_KEY)
    if (token && tokenExpiresSoon(token)) {
      try {
        token = await refreshAccessToken()
      } catch (error) {
        clearAuthState()
        redirectToLogin()
        return Promise.reject(error)
      }
    }
    if (token) {
      config.headers = config.headers || {}
      config.headers.Authorization = `Bearer ${token}`
    } else if (cookieCsrf && isUnsafeMethod(config.method)) {
      const csrfToken = await ensureCookieCsrf()
      config.headers = config.headers || {}
      config.headers[CSRF_HEADER_NAME] = csrfToken
    }
    return config
  })

  // On expired-token failures, refresh once; otherwise bounce through Shizuha ID.
  client.interceptors.response.use(
    (response) => response,
    async (error) => {
      const originalRequest = error.config || {}

      if (isTokenAuthError(error) && !originalRequest._retry) {
        originalRequest._retry = true
        try {
          const access = await refreshAccessToken()
          originalRequest.headers = originalRequest.headers || {}
          originalRequest.headers.Authorization = `Bearer ${access}`
          return client(originalRequest)
        } catch (refreshError) {
          clearAuthState()
          redirectToLogin()
          return Promise.reject(refreshError)
        }
      }

      return Promise.reject(error)
    }
  )
}

attachAuthInterceptors(api, { cookieCsrf: true })
attachAuthInterceptors(pulseApi)

// ── Fleet agents (admin-scoped; the dev/GLM/Claude agents) ──────────────────
// GET   /hive/api/v1/fleet/agents/                 list all (admin) — paginated
// POST  /hive/api/v1/fleet/agents/                 create a FleetAgent spec
// GET   /hive/api/v1/fleet/agents/{id}/            detail
// PATCH /hive/api/v1/fleet/agents/{id}/            update (display_name, role, image, model/profile...)
// DELETE/hive/api/v1/fleet/agents/{id}/            deprovision + delete row
// POST  /hive/api/v1/fleet/agents/{id}/enable/     start workload, scale 0 -> 1 (status -> running)
// POST  /hive/api/v1/fleet/agents/{id}/disable/    hibernate workload, scale 1 -> 0 (status -> hibernated; PVC retained)
// POST  /hive/api/v1/fleet/agents/{id}/stop/       operator-only hard stop (status -> stopped)
// POST  /hive/api/v1/fleet/agents/{id}/switch-backend/  swap container image (model/backend)
// GET   /hive/api/v1/fleet/agents/{id}/status/     live k8s status
// GET   /hive/api/v1/fleet/agents/{id}/events/     lifecycle event log — paginated
//
// NOTE: the API's start/stop lifecycle verbs are enable/disable (HIVE-49). We keep
// the UI's "Provision"/"Deprovision" product vocabulary for the method names below
// but call the canonical endpoints — the semantics map 1:1: provision = enable
// (start -> running), deprovision = disable (stop -> stopped, workspace PVC retained
// and re-enableable). Full teardown + row removal is `remove` (DELETE). (HIVE-52)
export const fleetApi = {
  cards: async (params = {}, config = {}) => {
    try {
      return await api.get('/v1/fleet/agent-cards/', { params, ...config })
    } catch (error) {
      // Backend and frontend roll independently. Keep the Agents grid usable
      // while an older backend replica is still serving during a rollout (and
      // during rollback to a pre-projection backend).
      if (error?.response?.status === 404) {
        return api.get('/v1/fleet/agents/', { params, ...config })
      }
      throw error
    }
  },
  list: (params = {}) => api.get('/v1/fleet/agents/', { params }),
  get: (id) => api.get(`/v1/fleet/agents/${id}/`),
  create: (data) => api.post('/v1/fleet/agents/', data),
  update: (id, data) => api.patch(`/v1/fleet/agents/${id}/`, data),
  remove: (id) => api.delete(`/v1/fleet/agents/${id}/`),
  provision: (id) => api.post(`/v1/fleet/agents/${id}/enable/`),
  deprovision: (id) => api.post(`/v1/fleet/agents/${id}/disable/`),
  stop: (id) => api.post(`/v1/fleet/agents/${id}/stop/`),
  archive: (id, data) => api.post(`/v1/fleet/agents/${id}/archive/`, data || {}),
  unarchive: (id) => api.post(`/v1/fleet/agents/${id}/unarchive/`),
  restart: (id) => api.post(`/v1/fleet/agents/${id}/restart/`),
  claim: (id, data = {}) => api.post(`/v1/fleet/agents/${id}/claim/`, data),
  switchBackend: (id, data) => api.post(`/v1/fleet/agents/${id}/switch-backend/`, data),
  status: (id) => api.get(`/v1/fleet/agents/${id}/status/`),
  events: (id, params = {}) => api.get(`/v1/fleet/agents/${id}/events/`, { params }),
  // HIVE-175: live runtime activity transcript (messages / tool calls / results)
  activity: (id, params = {}) => api.get(`/v1/fleet/agents/${id}/activity/`, { params }),
  teamPlacement: (id, params = {}) => api.get(`/v1/fleet/agents/${id}/team-placement/`, { params }),
  setTeamPlacement: (id, data) => api.post(`/v1/fleet/agents/${id}/team-placement/`, data),
  createTeam: (data) => api.post('/v1/fleet/teams/', data),
  runtimeOptions: () => api.get('/v1/fleet/runtime-options/'),
  pulseSnapshot: (params = {}) => api.get('/v1/fleet/pulse-snapshot/', { params }),
  organizations: () => api.get('/v1/fleet/organizations/', { timeout: 8000 }),
}

// ── Fleet teams — Hive proxy → Pulse TeamRoster (HIVE-238 / HIVE-239) ────────
// GET    /hive/api/v1/fleet/teams/                   list all roster teams
// POST   /hive/api/v1/fleet/teams/                   create a team {team_name}
// GET    /hive/api/v1/fleet/teams/{name}/            team detail
// PATCH  /hive/api/v1/fleet/teams/{name}/            update {lead_email, display_name}
// DELETE /hive/api/v1/fleet/teams/{name}/            deactivate
// GET    /hive/api/v1/fleet/teams/{name}/members/    list members
// POST   /hive/api/v1/fleet/teams/{name}/members/    add/remove {action, email}
export const teamsApi = {
  list: (params = {}) => api.get('/v1/fleet/teams/', { params }),
  get: (name, params = {}) => api.get(`/v1/fleet/teams/${name}/`, { params }),
  create: (data) => api.post('/v1/fleet/teams/', data),
  update: (name, data) => api.patch(`/v1/fleet/teams/${name}/`, data),
  delete: (name, data = {}) => api.delete(`/v1/fleet/teams/${name}/`, { data }),
  listMembers: (name, params = {}) => api.get(`/v1/fleet/teams/${name}/members/`, { params }),
  manageMember: (name, data) => api.post(`/v1/fleet/teams/${name}/members/`, data),
  capabilities: (name, params = {}) => api.get(`/v1/fleet/teams/${name}/capabilities/`, { params }),
  setCapabilities: (name, data) => api.put(`/v1/fleet/teams/${name}/capabilities/`, data),
}

export const organizationsApi = {
  list: () => fleetApi.organizations(),
}

export const capabilitiesApi = {
  list: () => api.get('/v1/fleet/capabilities/'),
  create: (data) => api.post('/v1/fleet/capabilities/', data),
  update: (slug, data) => api.patch(`/v1/fleet/capabilities/${slug}/`, data),
  disable: (slug, data = {}) => api.delete(`/v1/fleet/capabilities/${slug}/`, { data }),
  action: (slug, action, data = {}) => api.post(`/v1/fleet/capabilities/${slug}/${action}/`, data),
  audit: (params = {}) => api.get('/v1/fleet/capabilities/audit/', { params }),
  effective: (agentId) => api.get(`/v1/fleet/agents/${agentId}/effective-capabilities/`),
  overrides: (agentId) => api.get(`/v1/fleet/agents/${agentId}/capability-overrides/`),
  setOverrides: (agentId, data) => api.put(`/v1/fleet/agents/${agentId}/capability-overrides/`, data),
}

// ── Fleet credential grants (HIVE-263) — redacted staff surface ─────────────
// GET    /hive/api/v1/fleet/credentials/             list redacted grants
// POST   /hive/api/v1/fleet/credentials/             create {scope, team_slug|agent, provider, purpose, secret_ref}
// PATCH  /hive/api/v1/fleet/credentials/{id}/        update metadata/handle
// DELETE /hive/api/v1/fleet/credentials/{id}/        revoke grant
// GET    /hive/api/v1/fleet/credentials/{id}/audit/  redacted audit events
export const credentialsApi = {
  list: (params = {}) => api.get('/v1/fleet/credentials/', { params }),
  create: (data) => api.post('/v1/fleet/credentials/', data),
  update: (id, data) => api.patch(`/v1/fleet/credentials/${id}/`, data),
  revoke: (id) => api.delete(`/v1/fleet/credentials/${id}/`),
  audit: (id) => api.get(`/v1/fleet/credentials/${id}/audit/`),
}

// ── Claws (per-owner OpenClaw customer assistants) ──────────────────────────
// NOTE (backend gap): GET /v1/claws/ is OWNER-SCOPED — even staff only see their
// own claws in the list. Per-id routes (detail/start/stop/events/status) DO honor
// staff bypass. There is no admin "list all claws" endpoint yet. See DEPLOY PLAN /
// report for the recommended backend addition.
// GET   /hive/api/v1/claws/                 list caller's claws — paginated
// POST  /hive/api/v1/claws/                 provision a claw {name, tier_id}
// GET   /hive/api/v1/claws/{id}/            detail (staff may read any)
// PATCH /hive/api/v1/claws/{id}/            rename {name}
// POST  /hive/api/v1/claws/{id}/start/      scale 0 -> 1
// POST  /hive/api/v1/claws/{id}/stop/       scale 1 -> 0
// DELETE/hive/api/v1/claws/{id}/?confirm=<name>  two-phase delete
// GET   /hive/api/v1/claws/{id}/events/     lifecycle events
// GET   /hive/api/v1/claws/{id}/status/     live k8s status
// GET   /hive/api/v1/tiers/                 tier catalog {tiers:[...]}
// GET   /hive/api/v1/me/                    owner context + entitlements
export const clawsApi = {
  list: (params = {}) => api.get('/v1/claws/', { params }),
  get: (id) => api.get(`/v1/claws/${id}/`),
  create: (data) => api.post('/v1/claws/', data),
  rename: (id, data) => api.patch(`/v1/claws/${id}/`, data),
  start: (id) => api.post(`/v1/claws/${id}/start/`),
  stop: (id) => api.post(`/v1/claws/${id}/stop/`),
  remove: (id, confirmName) =>
    api.delete(`/v1/claws/${id}/`, { params: { confirm: confirmName } }),
  events: (id) => api.get(`/v1/claws/${id}/events/`),
  status: (id) => api.get(`/v1/claws/${id}/status/`),
  whatsappLinks: (id) => api.get(`/v1/claws/${id}/whatsapp/links/`),
  updateWhatsappLink: (id, code, data) => api.patch(`/v1/claws/${id}/whatsapp/links/${code}/`, data),
  unlinkWhatsapp: (id, code) => api.delete(`/v1/claws/${id}/whatsapp/links/${code}/`),
  dashboardUrl: (id) => api.get(`/v1/claws/${id}/dashboard-url`),
  whatsappPairing: (id) => api.post(`/v1/claws/${id}/whatsapp/pairing`),
}

export const tiersApi = {
  list: () => api.get('/v1/tiers/'),
}


export const orgTemplatesApi = {
  list: (params = {}) => api.get('/v1/org-templates/', { params }),
  get: (slug, params = {}) => api.get(`/v1/org-templates/${slug}/`, { params }),
}

export const orgProvisioningApi = {
  list: () => api.get('/v1/org-provisioning-runs/'),
  create: (data) => api.post('/v1/org-provisioning-runs/', data),
  get: (id) => api.get(`/v1/org-provisioning-runs/${id}/`),
  advance: (id) => api.post(`/v1/org-provisioning-runs/${id}/advance/`),
}

export const meApi = {
  get: () => api.get('/v1/me/'),
  services: () => api.get('/v1/me/services/'),
}

// ── Pulse workload snapshot (read-only enrichment for fleet cards) ──────────
export const pulseOrgApi = {
  snapshot: (params = {}) => fleetApi.pulseSnapshot(params),
}

// ── Cortex inference API keys (HIVE-76) ─────────────────────────────────────
// Self-serve sk-cortex- keys for scli / external tools. Proxied SAME-ORIGIN via
// the Hive backend (claws/cortex_keys.py) to the caller's own Cortex
// /v1/api-keys — Cortex ships no CORS for the SPA origin, so the SPA must not
// call it directly. The caller's shizuha-id JWT is forwarded, so only the
// user's own keys are ever listed/minted/revoked.
// GET    /hive/api/v1/cortex/api-keys/        list own keys
// POST   /hive/api/v1/cortex/api-keys/        mint a key {name} -> {key} (once!)
// DELETE /hive/api/v1/cortex/api-keys/{id}/   revoke own key
export const cortexKeysApi = {
  list: () => api.get('/v1/cortex/api-keys/'),
  create: (data) => api.post('/v1/cortex/api-keys/', data),
  revoke: (id) => api.delete(`/v1/cortex/api-keys/${id}/`),
}


// ── Fleet efficiency metrics (HIVE-172) ─────────────────────────────────────
// GET /hive/api/v1/fleet/efficiency/ — staff-only proxy to the
// team-throughput-exporter Prometheus endpoint (:9101). Returns per-agent
// gauges keyed by email: tasks_completed_24h, avg_lead_time_seconds, wip_depth,
// blocked_depth, overdue_tasks, oldest_open_item_age_seconds.
export const fleetEfficiencyApi = {
  snapshot: () => api.get('/v1/fleet/efficiency/'),
}

// ── HIVE-600: fleet harness-version tracker ───────────────────────────────
// GET  /v1/fleet/harness-versions/ — staff-only; per-harness current-vs-latest
//   version + agent counts + upgrade-available.
// POST /v1/fleet/harness-versions/<harness>/upgrade/ — record fleet upgrade intent.
export const harnessVersionsApi = {
  list: () => api.get('/v1/fleet/harness-versions/'),
  upgrade: (harness, targetVersion) =>
    api.post(`/v1/fleet/harness-versions/${harness}/upgrade/`, targetVersion ? { target_version: targetVersion } : {}),
}

// ── Daemon identity health (HIVE-254 P5) ──────────────────────────────────
// GET /hive/api/v1/fleet/daemon/identity-health/ — staff-only, per-daemon
// status + list of unresolved rejected identity events (INV-ID violations).
export const daemonIdentityApi = {
  health: () => api.get('/v1/fleet/daemon/identity-health/'),
}

// ── HIVE Token Coordinator pools (HIVE-126) ────────────────────────────────
// Owner-scoped model-token pools. The backend derives owner_id from the JWT;
// callers never send owner_id in request bodies. Staff may opt into the internal
// org pool with ?scope=org.
export const tokenPoolsApi = {
  list: (params = {}) => api.get('/v1/token-pools/', { params }),
  create: (data) => api.post('/v1/token-pools/', data),
  update: (id, data) => api.patch(`/v1/token-pools/${id}/`, data),
  addEntry: (poolId, data) => api.post(`/v1/token-pools/${poolId}/entries/`, data),
  updateEntry: (poolId, entryId, data) => api.patch(`/v1/token-pools/${poolId}/entries/${entryId}/`, data),
  deleteEntry: (poolId, entryId) => api.delete(`/v1/token-pools/${poolId}/entries/${entryId}/`),
  disableEntry: (poolId, entryId) => api.post(`/v1/token-pools/${poolId}/entries/${entryId}/disable/`),
  /** Operator "Check now" — OAuth refresh / clear expired cool / probe where safe. */
  checkEntry: (poolId, entryId, data = {}) =>
    api.post(`/v1/token-pools/${poolId}/entries/${entryId}/check/`, data),
  /** Operator "Redeem reset" — consume one banked Codex rate-limit reset credit. */
  redeemReset: (poolId, entryId, data = {}) =>
    api.post(`/v1/token-pools/${poolId}/entries/${entryId}/redeem-reset/`, data),
  /** Multi-provider credential health board (Claude / Codex / Grok). */
  credentialHealth: (params = {}) => api.get('/v1/credential-health/', { params }),
  /** Operator "Refresh all" — force a live quota probe for every visible account.
   *  The 15s dashboard poll only re-reads stored snapshots; unsupported-marked
   *  rows are otherwise skipped for 24h. */
  credentialHealthRefreshAll: (params = {}) =>
    api.post('/v1/credential-health/', {}, { params }),
}

// HIVE-581: staff-only browser device-code auth for org Codex accounts.
export const codexDeviceAuthApi = {
  start: () => api.post('/v1/codex-device-auth/start/'),
  poll: (data) => api.post('/v1/codex-device-auth/poll/', data),
}

// SuperGrok / Grok Build device-code auth into org xAI TokenPool.
export const grokDeviceAuthApi = {
  start: () => api.post('/v1/grok-device-auth/start/'),
  poll: (data) => api.post('/v1/grok-device-auth/poll/', data),
}

// Claude / Anthropic OAuth code-paste auth (official Claude Code client) into org pool.
export const claudeOAuthApi = {
  start: () => api.post('/v1/claude-oauth/start/'),
  exchange: (data) => api.post('/v1/claude-oauth/exchange/', data),
}

// Google / Antigravity OAuth code-paste auth into Google pool.
export const antigravityOAuthApi = {
  start: () => api.post('/v1/antigravity-oauth/start/'),
  exchange: (data) => api.post('/v1/antigravity-oauth/exchange/', data),
  importTokens: (data) => api.post('/v1/antigravity-oauth/import/', data),
}

// HIVE-1889 (S3) + HIVE-2161 lifecycle: org Settings > Providers — thin mirror
// of org-scoped Cortex provider refs (HIVE-1862 contract). No raw keys are
// stored or rendered in Hive; the validate endpoint delegates the live 1-token
// probe to Cortex. HIVE-2161 adds the write path: provision/rotate/disable
// orchestrate the LIVE Cortex listing lifecycle — the secret key is relayed
// in-memory only (draft → submit → validate on Cortex) and never persisted.
// GET    /hive/api/v1/org-provider-refs/?org_id=…            list org-scoped refs
// POST   /hive/api/v1/org-provider-refs/                     upsert a ref (mirror)
// DELETE /hive/api/v1/org-provider-refs/{id}/                remove a ref
// POST   /hive/api/v1/org-provider-refs/{id}/validate/       live 1-token probe
// POST   /hive/api/v1/org-provider-refs/provision/           add BYO credential
// POST   /hive/api/v1/org-provider-refs/{id}/rotate/         replace the key
// POST   /hive/api/v1/org-provider-refs/{id}/disable/        org-scoped disable
export const orgProviderRefsApi = {
  list: (orgId) => api.get('/v1/org-provider-refs/', { params: { org_id: orgId } }),
  upsert: (data) => api.post('/v1/org-provider-refs/', data),
  remove: (id) => api.delete(`/v1/org-provider-refs/${id}/`),
  validate: (id, data = {}) => api.post(`/v1/org-provider-refs/${id}/validate/`, data),
  provision: (data) => api.post('/v1/org-provider-refs/provision/', data),
  rotate: (id, data = {}) => api.post(`/v1/org-provider-refs/${id}/rotate/`, data),
  disable: (id) => api.post(`/v1/org-provider-refs/${id}/disable/`),
}

export default api
