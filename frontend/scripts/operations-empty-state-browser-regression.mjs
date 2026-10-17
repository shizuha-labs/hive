import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright-core'

/**
 * HIVE-1661 browser regression — Operations Theater empty state contract.
 *
 * HIVE-726 / HIVE-1566 required the empty authenticated Operations Theater to
 * keep an icon/illustration, an explanatory subtitle, and an actionable CTA.
 * Regression bbda5661 collapsed it back to a bare paragraph. This test drives
 * `/hive/overview` with agents present (so the theater renders) but an empty
 * efficiency snapshot (so recent-activity is empty), then asserts the restored
 * icon, subtitle copy, and the Open Pulse CTA are visible and actionable — at
 * desktop and mobile viewports, with a full-page screenshot per name.
 */
const baseUrl = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:4173'
const artifactDir = process.env.PLAYWRIGHT_ARTIFACT_DIR || '/tmp/hive-operations-empty'

const agents = [
  { id: '1', display_name: 'Jun', agent_username: 'jun', status: 'running', team_names: ['engineering'], created_at: '2026-07-01T00:00:00Z' },
  { id: '2', display_name: 'Sara', agent_username: 'sara', status: 'idle', team_names: ['accounting'], created_at: '2026-07-01T00:00:00Z' },
]

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
  const pathname = new URL(route.request().url()).pathname
  let body = { results: [] }
  if (pathname.endsWith('/v1/me/')) {
    body = { username: 'jun', email: 'jun@shizuha.com', is_staff: true }
  } else if (pathname.endsWith('/v1/me/services/')) {
    body = { service_ids: [] }
  } else if (pathname.endsWith('/v1/fleet/organizations/')) {
    body = { organizations: [{ id: 1, slug: 'shizuha', name: 'Shizuha Global Pvt. Ltd' }] }
  } else if (pathname.includes('/v1/fleet/agent-cards') || pathname.endsWith('/v1/fleet/agents/')) {
    body = { results: agents, next: null }
  } else if (pathname.endsWith('/v1/fleet/teams/')) {
    body = { teams: [] }
  } else if (pathname.endsWith('/v1/fleet/pulse-snapshot/')) {
    body = { nodes: [], workload: {}, availability: {}, teams: [] }
  } else if (pathname.includes('/efficiency')) {
    // Intentionally empty: zero recent activity => the empty state must render.
    body = { agents: {} }
  } else {
    body = { results: [] }
  }
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
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

  await page.goto(`${baseUrl}/hive/overview`, { waitUntil: 'networkidle' })

  // The Operations Theater must still be present (HIVE-1566: never disappear).
  const theater = page.getByRole('heading', { name: 'Recent activity', exact: true })
  await theater.waitFor({ state: 'visible' })

  // Empty state: icon, subtitle, and CTA.
  const empty = page.locator('[data-testid="operations-empty-state"]')
  await empty.waitFor({ state: 'visible' })
  const icon = empty.locator('svg').first()
  assert(await icon.isVisible(), `${name}: empty-state icon is not visible`)
  assert((await empty.textContent()).includes('No recent activity'), `${name}: subtitle heading missing`)
  assert((await empty.textContent()).includes('records agent task progress'), `${name}: explanatory subtitle missing`)

  const cta = empty.getByRole('link', { name: 'Open Pulse' })
  assert(await cta.isVisible(), `${name}: Open Pulse CTA is not visible`)
  assert(await cta.isEnabled(), `${name}: Open Pulse CTA is not actionable`)
  const href = await cta.getAttribute('href')
  assert(href === '/pulse/', `${name}: Open Pulse CTA href is ${href}, expected /pulse/`)

  // CTA must be reachable by keyboard (actionable).
  await cta.focus()
  assert(await page.evaluate(() => document.activeElement?.textContent?.includes('Open Pulse')), `${name}: CTA not focusable`)

  // The theater heading + empty state must stay within the viewport.
  const box = await empty.boundingBox()
  assert(box, `${name}: empty-state has no bounding box`)
  assert(box.x >= 0 && box.y >= 0, `${name}: empty-state starts off-screen`)
  assert(box.x + box.width <= viewport.width + 1, `${name}: empty-state overflows viewport width`)

  assert(browserErrors.length === 0, `${name}: browser page errors: ${browserErrors.join(' | ')}`)
  await page.screenshot({ path: `${artifactDir}/${name}.png`, fullPage: true })

  process.stdout.write(`${JSON.stringify({ viewport: name, theater: true, icon: true, subtitle: true, cta: { href }, focusable: true, screenshot: `${name}.png` })}\n`)
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
