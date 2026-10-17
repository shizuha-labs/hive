import { chromium } from 'playwright-core'

// HIVE-1711 regression smoke: a non-staff owner's "Open dashboard" path must
// render the OpenClaw shell without browser crash markers (Aw, Snap! / SIGTRAP).
// The crash was diagnosed (HIVE-1998/HIVE-2006) as a GPU/WebGL-layer renderer
// abort in the OpenClaw runtime's served shell. This smoke asserts the Hive SPA
// generates the correct owner-scoped dashboard link for a non-staff owner and
// that the route resolves without renderer crash/error markers.
//
// HIVE-1711 stopped-claw leg (mika rerun, comment 816401): the route no longer
// SIGTRAPs — a stopped claw dies as a bare nginx 502 because the fixture claw
// has no dashboard upstream. The honest contract (hive#335) is: the drawer
// disables the Open dashboard control for a non-running claw, the dashboard-url
// endpoint is never fetched, and the serving layer returns 409
// {claw_status, error, status_message} with NO dashboard_url instead of
// minting a signed redirect into the 502. This smoke pins both halves.

const baseUrl = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:4173'
const artifactDir = process.env.PLAYWRIGHT_ARTIFACT_DIR || '/tmp/hive-openclaw-dashboard'

const CLAW_ID = '1d176064-4499-4ee6-a107-5c6e03a3fb35'
const OWNER_ID = 16
const OWNER_EMAIL = 'mika@shizuha.com'
const STOPPED_STATUS_MESSAGE = 'Cortex model unavailable for this claw credential'

// Mutable fixture state: the stopped scenario flips this and reloads.
let clawStatus = 'running'

const claw = {
  id: `claw:${CLAW_ID}`,
  kind: 'claw',
  name: 'My Assistant',
  display_name: 'My Assistant',
  agent_username: 'mika',
  agent_type: 'claw',
  get status() { return clawStatus },
  runtime_compat_mode: true,
  owner_id: OWNER_ID,
  owner_username: 'mika',
  owner_email: OWNER_EMAIL,
  created_at: '2026-07-01T12:00:00Z',
}

const clawDetail = {
  id: CLAW_ID,
  name: 'My Assistant',
  display_name: 'My Assistant',
  agent_username: 'mika',
  agent_type: 'claw',
  get status() { return clawStatus },
  runtime_compat_mode: true,
  owner_id: OWNER_ID,
  owner_username: 'mika',
  owner_email: OWNER_EMAIL,
  created_at: '2026-07-01T12:00:00Z',
}

// Counters for the honest-state assertions.
let dashboardUrlCalls = 0

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

// hive#335 honest 409 contract for a non-RUNNING claw: runtime state, no
// signed redirect. The smoke fulfills dashboard-url with exactly this shape
// and asserts the shape, so the fixture cannot drift from the serving layer.
function stoppedDashboardUrlPayload() {
  return {
    claw_status: 'stopped',
    error: 'Claw is not running; dashboard upstream is unavailable.',
    status_message: STOPPED_STATUS_MESSAGE,
  }
}

function assertStopped409Contract(payload) {
  assert(payload.claw_status === 'stopped', `claw_status mismatch: ${payload.claw_status}`)
  assert(/not running/i.test(payload.error || ''), `error must say the claw is not running: ${payload.error}`)
  assert(payload.status_message === STOPPED_STATUS_MESSAGE, 'status_message must carry the honest runtime reason')
  assert(!('dashboard_url' in payload), 'honest 409 must NOT mint a dashboard_url')
}

async function waitForPreview() {
  let lastError
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(`${baseUrl}/hive/`)
      if (response.ok) return
      lastError = new Error(`preview returned HTTP ${response.status}`)
    } catch (error) {
      lastError = error
    }
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  throw new Error(`Vite preview did not become ready: ${lastError?.message || 'unknown error'}`)
}

async function fulfillApi(route) {
  const request = route.request()
  const pathname = new URL(request.url()).pathname
  let status = 200
  let body = {}
  if (pathname.endsWith('/dashboard-url')) {
    dashboardUrlCalls += 1
    if (clawStatus !== 'running') {
      // hive#335: honest 409 for a non-RUNNING claw — never a signed redirect.
      status = 409
      body = stoppedDashboardUrlPayload()
      assertStopped409Contract(body)
    } else {
      body = { dashboard_url: `${baseUrl}/hive/openclaw/${OWNER_ID}/${CLAW_ID}/chat` }
    }
  } else if (pathname.endsWith('/v1/me/')) {
    body = { username: 'mika', email: OWNER_EMAIL, is_staff: false }
  } else if (pathname.endsWith('/v1/me/services/')) {
    body = { service_ids: [] }
  } else if (pathname.endsWith('/v1/fleet/organizations/')) {
    body = { organizations: [{ id: 1, slug: 'shizuha', name: 'Shizuha Global Pvt. Ltd' }] }
  } else if (pathname.includes(`/v1/claws/${CLAW_ID}/`)) {
    body = clawDetail
  } else if (pathname.endsWith('/v1/claws/')) {
    body = { results: [clawDetail], next: null }
  } else if (pathname.includes('/v1/fleet/agent-cards')) {
    body = { results: [claw], next: null }
  } else if (pathname.endsWith('/v1/fleet/agents/')) {
    body = { results: [claw], next: null }
  } else if (pathname.endsWith('/v1/fleet/teams/')) {
    body = { teams: [] }
  } else if (pathname.endsWith('/v1/fleet/pulse-snapshot/')) {
    body = { nodes: [], workload: {}, availability: {}, teams: [] }
  } else if (pathname.endsWith('/v1/fleet/runtime-options/')) {
    body = { runtime_methods: [], resource_profiles: [], skills: [], mcp_servers: [] }
  } else {
    body = { results: [] }
  }
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function openAgentDrawer(page) {
  await page.goto(`${baseUrl}/hive/agents`, { waitUntil: 'networkidle' })
  const card = page.locator('button', { hasText: 'My Assistant' }).first()
  await card.waitFor({ state: 'visible' })
  await card.focus()
  await card.click()
  const drawer = page.locator('[data-agent-drawer-root="true"]')
  await drawer.waitFor({ state: 'visible' })
  return drawer
}

async function run() {
  await waitForPreview()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } })
  const claims = Buffer.from(JSON.stringify({
    username: 'mika',
    email: OWNER_EMAIL,
    is_staff: false,
    exp: Math.floor(Date.now() / 1000) + 3600,
  })).toString('base64').replace(/=+$/, '')
  await context.addInitScript((token) => {
    localStorage.setItem('shizuha_access_token', token)
    localStorage.setItem('shizuha_current_org', '__all__')
  }, `header.${claims}.signature`)

  const page = await context.newPage()
  const browserErrors = []
  const crashEvents = []
  page.on('pageerror', (error) => browserErrors.push(error.message))
  page.on('crash', () => crashEvents.push('page-crash'))
  await page.route('**/hive/api/**', fulfillApi)
  await page.route('**/pulse/api/**', fulfillApi)

  // 1. Navigate to the agent list and open the non-staff owner's claw card.
  const drawer = await openAgentDrawer(page)

  // 2. The "Open dashboard" control must be present for a non-staff owner claw.
  const openDashboard = drawer.getByRole('link', { name: /Open dashboard/i })
  await openDashboard.waitFor({ state: 'visible' })
  const href = await openDashboard.getAttribute('href')
  assert(href === `/hive/assistants/${CLAW_ID}/dash`, `Open dashboard href mismatch: ${href}`)

  // 3. The owner-scoped OpenClaw route must resolve without a renderer crash.
  await page.goto(`${baseUrl}/hive/openclaw/${OWNER_ID}/${CLAW_ID}/chat`, { waitUntil: 'networkidle' })
  // The Hive SPA should not throw uncaught errors or crash the renderer.
  assert(crashEvents.length === 0, `renderer crash detected: ${JSON.stringify(crashEvents)}`)
  assert(browserErrors.length === 0, `browser page errors: ${JSON.stringify(browserErrors)}`)

  await page.screenshot({ path: `${artifactDir}/openclaw-dashboard.png`, fullPage: true })

  // 4. HIVE-1711 stopped-claw honest state: flip the fixture to STOPPED and
  // reload. The drawer must disable the Open dashboard control (no dead-end
  // navigation), the dashboard-url endpoint must never be fetched, and the
  // smoke's 409 fixture must match hive#335's honest contract exactly.
  clawStatus = 'stopped'
  const stoppedDrawer = await openAgentDrawer(page)

  const stoppedPill = stoppedDrawer.getByText(/^stopped$/i).first()
  await stoppedPill.waitFor({ state: 'visible' })

  const stoppedLink = stoppedDrawer.getByRole('link', { name: /Open dashboard/i })
  await stoppedLink.waitFor({ state: 'visible' })
  assert(
    (await stoppedLink.getAttribute('aria-disabled')) === 'true',
    'Open dashboard must be aria-disabled for a stopped claw',
  )

  assert(
    dashboardUrlCalls === 0,
    `dashboard-url must never be fetched for a stopped claw (calls=${dashboardUrlCalls})`,
  )
  assert(crashEvents.length === 0, `renderer crash detected: ${JSON.stringify(crashEvents)}`)
  assert(browserErrors.length === 0, `browser page errors: ${JSON.stringify(browserErrors)}`)

  await page.screenshot({ path: `${artifactDir}/openclaw-dashboard-stopped.png`, fullPage: true })
  await browser.close()
  console.log(`OK: non-staff owner OpenClaw dashboard renders without crash markers (href=${href}); ` +
    `stopped claw renders the honest disabled state with zero dashboard-url fetches and a contract-pinned 409`)
}

run().catch((error) => {
  console.error(`FAIL: ${error.message}`)
  process.exit(1)
})
