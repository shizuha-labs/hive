import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright-core'

const baseUrl = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:4173'
const artifactDir = process.env.PLAYWRIGHT_ARTIFACT_DIR || '/tmp/hive-agent-drawer'

const agent = {
  id: 'agent-1',
  agent_username: 'jun',
  display_name: 'Jun',
  display_title: 'Performance Engineer',
  role: 'Engineer',
  status: 'running',
  availability_status: 'available',
  lifecycle_managed: true,
  provisioned_at: '2026-07-16T12:00:00Z',
  created_at: '2026-07-01T12:00:00Z',
  last_active_at: '2026-07-17T00:00:00Z',
  model: 'gpt-5.3-codex-spark',
  effective_model: 'gpt-5.3-codex-spark',
  model_provider: 'openai',
  runtime_provider: 'openai',
  runtime_harness: 'codex',
  runtime_source: 'k3s-native',
  execution_method: 'codex_app_server',
  resource_profile: 'standard',
  namespace: 'agents',
  pod_name: 'agent-jun',
  arch: 'amd64',
  team_names: ['accounting'],
  team_memberships: [{
    team_name: 'accounting',
    team_slug: 'accounting',
    organization_slug: 'shizuha',
    organization_name: 'Shizuha Global Pvt. Ltd',
  }],
}

const placements = {
  agent: { display_title: agent.display_title },
  teams: [
    {
      id: 'global-accounting',
      name: 'accounting',
      display_name: 'Accounting',
      organization_slug: 'shizuha',
      organization_name: 'Shizuha Global Pvt. Ltd',
      selected: true,
      role_title: 'Ledger Engineer',
      membership_role: 'member',
    },
    {
      id: 'digital-accounting',
      name: 'accounting',
      display_name: 'Accounting',
      organization_slug: 'digital',
      organization_name: 'Shizuha Digital LLP',
      selected: false,
      membership_role: 'member',
    },
    {
      id: 'global-engineering',
      name: 'engineering',
      display_name: 'Engineering',
      organization_slug: 'shizuha',
      organization_name: 'Shizuha Global Pvt. Ltd',
      selected: false,
      membership_role: 'member',
    },
  ],
}

function assert(condition, message) {
  if (!condition) throw new Error(message)
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
  let body = {}
  if (pathname.endsWith('/v1/me/')) {
    body = { username: 'jun', email: 'jun@shizuha.com', is_staff: true }
  } else if (pathname.endsWith('/v1/me/services/')) {
    body = { service_ids: [] }
  } else if (pathname.endsWith('/v1/fleet/organizations/')) {
    body = { organizations: [
      { id: 1, slug: 'shizuha', name: 'Shizuha Global Pvt. Ltd' },
      { id: 2, slug: 'digital', name: 'Shizuha Digital LLP' },
    ] }
  } else if (pathname.endsWith('/v1/fleet/agents/agent-1/team-placement/')) {
    body = placements
  } else if (pathname.endsWith('/v1/fleet/agents/agent-1/effective-capabilities/')) {
    body = { capabilities: [], skills: ['engineering'], enabled_mcp_servers: ['pulse'] }
  } else if (pathname.endsWith('/v1/fleet/agents/agent-1/events/')) {
    body = { results: [] }
  } else if (pathname.endsWith('/v1/fleet/agents/agent-1/activity/')) {
    body = { events: [], total: 0 }
  } else if (pathname.endsWith('/v1/fleet/agents/agent-1/')) {
    body = agent
  } else if (pathname.includes('/v1/fleet/agent-cards')) {
    body = { results: [agent], next: null }
  } else if (pathname.endsWith('/v1/fleet/agents/')) {
    body = { results: [agent], next: null }
  } else if (pathname.endsWith('/v1/fleet/teams/')) {
    body = { teams: placements.teams }
  } else if (pathname.endsWith('/v1/fleet/pulse-snapshot/')) {
    body = { nodes: [], workload: {}, availability: { 'jun@shizuha.com': 'available' }, teams: placements.teams }
  } else if (pathname.endsWith('/v1/fleet/runtime-options/')) {
    body = { runtime_methods: [], resource_profiles: [], skills: [], mcp_servers: [] }
  } else if (pathname.includes('/effective-capabilities/')) {
    body = { capabilities: [] }
  } else if (pathname.includes('/harness')) {
    body = { harnesses: [], rollout_in_progress: false }
  } else if (pathname.includes('/efficiency')) {
    body = { agents: {} }
  } else if (pathname.includes('/daemon')) {
    body = { results: [] }
  } else {
    body = { results: [] }
  }
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
}

function assertBounds(box, viewport, label) {
  assert(box, `${label}: no rendered bounding box`)
  assert(box.x >= 0 && box.y >= 0, `${label}: starts off-screen (${JSON.stringify(box)})`)
  assert(box.x + box.width <= viewport.width + 1, `${label}: overflows viewport width (${JSON.stringify(box)})`)
  assert(box.y + box.height <= viewport.height + 1, `${label}: overflows viewport height (${JSON.stringify(box)})`)
}

async function runViewport(browser, name, viewport) {
  const context = await browser.newContext({ viewport })
  const claims = Buffer.from(JSON.stringify({
    username: 'jun',
    email: 'jun@shizuha.com',
    is_staff: true,
    exp: Math.floor(Date.now() / 1000) + 3600,
  })).toString('base64').replace(/=+$/, '')
  await context.addInitScript((token) => {
    localStorage.setItem('shizuha_access_token', token)
    localStorage.setItem('shizuha_current_org', '__all__')
  }, `header.${claims}.signature`)

  const page = await context.newPage()
  const browserErrors = []
  page.on('pageerror', (error) => browserErrors.push(error.message))
  await page.route('**/hive/api/**', fulfillApi)
  await page.route('**/pulse/api/**', fulfillApi)
  await page.goto(`${baseUrl}/hive/agents`, { waitUntil: 'networkidle' })

  const card = page.locator('.hive-agent-card', { hasText: 'Jun' }).first()
  await card.waitFor({ state: 'visible' })
  await card.focus()
  await card.click()

  const drawer = page.locator('[data-agent-drawer-root="true"]')
  const panel = drawer.locator('[data-agent-drawer-panel="true"]')
  await drawer.waitFor({ state: 'visible' })
  assert(await drawer.evaluate((element) => element.parentElement === document.body), `${name}: drawer is not portaled directly under body`)
  assert(await page.locator('#root').evaluate((element) => element.hasAttribute('inert') && element.getAttribute('aria-hidden') === 'true'), `${name}: app background is not inert`)
  assertBounds(await drawer.boundingBox(), viewport, `${name} drawer`)
  assertBounds(await panel.boundingBox(), viewport, `${name} panel`)
  assert(await page.locator(':focus').getAttribute('aria-label') === 'Close agent details', `${name}: focus did not enter the drawer`)

  const drawerText = await drawer.textContent()
  assert(drawerText.includes('Shizuha Global Pvt. Ltd › Accounting'), `${name}: selected org-qualified Accounting label missing`)
  assert(drawerText.includes('Ledger Engineer · member'), `${name}: team-scoped role label missing`)
  const options = await drawer.locator('select option').allTextContents()
  assert(options.includes('Shizuha Digital LLP › Accounting'), `${name}: colliding Digital Accounting option missing (${JSON.stringify(options)})`)
  assert(options.includes('Shizuha Global Pvt. Ltd › Engineering'), `${name}: org-qualified Engineering option missing (${JSON.stringify(options)})`)

  const focusable = drawer.locator('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')
  const first = focusable.first()
  const last = focusable.last()
  await last.focus()
  await page.keyboard.press('Tab')
  assert(await first.evaluate((element) => document.activeElement === element), `${name}: Tab escaped past the final drawer control`)
  await page.keyboard.press('Shift+Tab')
  assert(await last.evaluate((element) => document.activeElement === element), `${name}: Shift+Tab escaped before the first drawer control`)

  const deleteButton = drawer.getByRole('button', { name: 'Delete agent', exact: true })
  await deleteButton.click()
  const confirmation = drawer.getByRole('dialog', { name: 'Delete agent permanently?' })
  await confirmation.waitFor({ state: 'visible' })
  await page.keyboard.press('Escape')
  await confirmation.waitFor({ state: 'hidden' })
  assert(await drawer.isVisible(), `${name}: nested confirmation Escape closed the parent drawer`)
  assert(await deleteButton.evaluate((element) => document.activeElement === element), `${name}: confirmation did not restore trigger focus`)

  await page.screenshot({ path: `${artifactDir}/${name}.png`, fullPage: true })
  await page.keyboard.press('Escape')
  await drawer.waitFor({ state: 'hidden' })
  assert(await card.evaluate((element) => document.activeElement === element), `${name}: drawer close did not restore card focus`)
  assert(await page.locator('#root').evaluate((element) => !element.hasAttribute('inert') && !element.hasAttribute('aria-hidden')), `${name}: app background stayed inert after close`)

  await card.click()
  await drawer.waitFor({ state: 'visible' })
  const reopenedPanelBox = await panel.boundingBox()
  if (reopenedPanelBox.width < viewport.width) {
    await drawer.locator('[data-agent-drawer-backdrop="true"]').click({ position: { x: 4, y: 4 } })
  } else {
    // At the mobile breakpoint the full-width panel intentionally covers the
    // backdrop, so exercise the always-visible accessible close control.
    await drawer.getByRole('button', { name: 'Close agent details' }).click()
  }
  await drawer.waitFor({ state: 'hidden' })

  // HIVE-1728: a focused agent card must open the drawer with BOTH Enter and
  // Space (native button keyboard contract), without scrolling the page. The
  // card is a real <button type="button">, so the browser synthesizes a click
  // on Enter (keydown) and Space (keyup); assert the drawer opens each way,
  // stays closed-free between attempts, and that focus restores after Escape.
  for (const key of ['Enter', 'Space']) {
    await card.focus()
    assert(await card.evaluate((element) => document.activeElement === element), `${name}: card not focusable before ${key}`)
    const scrollBefore = await page.evaluate(() => window.scrollY)
    await page.keyboard.press(key)
    assert(await drawer.isVisible(), `${name}: ${key} did not open the drawer`)
    const scrollAfter = await page.evaluate(() => window.scrollY)
    assert(scrollAfter === scrollBefore, `${name}: ${key} scroll side effect (${scrollBefore} -> ${scrollAfter})`)
    assert(await page.locator(':focus').getAttribute('aria-label') === 'Close agent details', `${name}: ${key} did not move focus into the drawer`)
    await page.keyboard.press('Escape')
    await drawer.waitFor({ state: 'hidden' })
    assert(await card.evaluate((element) => document.activeElement === element), `${name}: drawer close did not restore card focus after ${key}`)
  }

  assert(browserErrors.length === 0, `${name}: browser page errors: ${browserErrors.join(' | ')}`)
  const pageText = await page.locator('body').textContent()
  assert(!pageText.includes('Team-scoped roles unavailable'), `${name}: team role request failed`)

  process.stdout.write(`${JSON.stringify({ viewport: name, portal: true, bounds: true, focusEntry: true, focusTrap: true, focusRestore: true, nestedEscape: true, enterActivation: true, spaceActivation: true, labels: ['Shizuha Global Pvt. Ltd › Accounting', 'Ledger Engineer · member'], options })}\n`)
  await context.close()
}

await mkdir(artifactDir, { recursive: true })
await waitForPreview()
const browser = await chromium.launch({ headless: true })
try {
  await runViewport(browser, 'desktop-1440x1000', { width: 1440, height: 1000 })
  await runViewport(browser, 'mobile-390x844', { width: 390, height: 844 })
} finally {
  await browser.close()
}
