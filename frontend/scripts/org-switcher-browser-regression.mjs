import { chromium } from 'playwright-core'

const baseUrl = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:4173'
const organizations = [
  { id: 1, slug: 'shizuha', name: 'Shizuha Global Pvt. Ltd' },
  { id: 2, slug: 'digital', name: 'Shizuha Digital LLP' },
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
    body = { username: 'Kai', email: 'kai@shizuha.com' }
  } else if (pathname.endsWith('/v1/me/services/')) {
    body = { service_ids: [] }
  } else if (pathname.endsWith('/v1/fleet/organizations/')) {
    body = { organizations }
  } else if (pathname.endsWith('/v1/fleet/agents/')) {
    body = { results: [] }
  } else if (pathname.endsWith('/v1/fleet/teams/')) {
    body = { teams: [] }
  }
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
}

async function openMobileSidebar(page, mobile) {
  if (!mobile) return
  await page.locator('header button').first().click()
  await page.locator('aside').waitFor({ state: 'visible' })
}

async function assertFocusOutsideSwitcher(page, name, direction) {
  const focused = await page.evaluate(() => ({
    tag: document.activeElement?.tagName || '',
    text: document.activeElement?.textContent?.trim() || '',
    insideSwitcher: Boolean(document.activeElement?.closest('[data-org-switcher-state]')),
  }))
  assert(!focused.insideSwitcher, `${name}: ${direction} left focus inside the switcher (${JSON.stringify(focused)})`)
}

async function runViewport(browser, name, viewport) {
  const context = await browser.newContext({ viewport })
  const claims = Buffer.from(JSON.stringify({
    username: 'Kai',
    email: 'kai@shizuha.com',
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

  const mobile = viewport.width < 1024
  await page.goto(`${baseUrl}/hive/agents`, { waitUntil: 'networkidle' })
  await openMobileSidebar(page, mobile)

  const sidebar = page.locator('aside')
  const switcher = sidebar.locator('[data-org-switcher-state]')
  const trigger = switcher.locator('button[aria-haspopup="menu"]')
  const menu = sidebar.locator('[role="menu"]')
  const items = menu.locator('[role="menuitemradio"]')

  // Exercise the real native keyboard event path, not jsdom-dispatched events.
  await trigger.focus()
  await page.keyboard.press('Enter')
  await menu.waitFor({ state: 'visible' })
  assert(await items.evaluateAll((nodes) => nodes.every((node) => node.tabIndex === -1)), `${name}: menu items are not managed composite stops`)
  assert((await page.locator(':focus').getAttribute('role')) === 'menuitemradio', `${name}: Enter did not move focus into the menu`)
  await page.keyboard.press('End')
  assert((await page.locator(':focus').textContent()).includes('Shizuha Digital LLP'), `${name}: End did not focus the last option`)
  await page.keyboard.press('Home')
  assert((await page.locator(':focus').textContent()).includes('All Organizations'), `${name}: Home did not focus the first option`)
  await page.keyboard.press('ArrowDown')
  assert((await page.locator(':focus').textContent()).includes('Shizuha Global Pvt. Ltd'), `${name}: ArrowDown did not focus the next option`)
  await page.keyboard.press('ArrowUp')
  assert((await page.locator(':focus').textContent()).includes('All Organizations'), `${name}: ArrowUp did not focus the previous option`)
  await page.keyboard.press('Escape')
  await menu.waitFor({ state: 'hidden' })
  assert(await trigger.evaluate((element) => document.activeElement === element), `${name}: Escape did not restore trigger focus`)

  await page.keyboard.press('Space')
  await menu.waitFor({ state: 'visible' })
  await page.keyboard.press('Tab')
  await menu.waitFor({ state: 'hidden' })
  await assertFocusOutsideSwitcher(page, name, 'Tab')

  await trigger.focus()
  await page.keyboard.press('Space')
  await menu.waitFor({ state: 'visible' })
  await page.keyboard.press('Shift+Tab')
  await menu.waitFor({ state: 'hidden' })
  await assertFocusOutsideSwitcher(page, name, 'Shift+Tab')

  await trigger.focus()
  await page.keyboard.press('Enter')
  await menu.waitFor({ state: 'visible' })
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Space')
  await menu.waitFor({ state: 'hidden' })
  assert(await trigger.evaluate((element) => document.activeElement === element), `${name}: Space selection did not restore trigger focus`)
  assert((await trigger.textContent()).includes('Shizuha Global Pvt. Ltd'), `${name}: Space did not select the focused organization`)

  await page.keyboard.press('Enter')
  await menu.waitFor({ state: 'visible' })
  const selected = menu.locator('[role="menuitemradio"][aria-checked="true"]')
  await selected.focus()
  await page.keyboard.press('Enter')
  await menu.waitFor({ state: 'hidden' })
  assert(await trigger.evaluate((element) => document.activeElement === element), `${name}: Enter on the selected item did not restore trigger focus`)

  // Return to all-org state so both viewports exercise the same geometry.
  await trigger.focus()
  await page.keyboard.press('Enter')
  await menu.waitFor({ state: 'visible' })
  await page.keyboard.press('Home')
  await page.keyboard.press('Enter')
  await menu.waitFor({ state: 'hidden' })

  await trigger.click()
  await menu.waitFor({ state: 'visible' })

  const overview = sidebar.getByRole('link', { name: 'Overview', exact: true })
  const overviewBox = await overview.boundingBox()
  assert(overviewBox, `${name}: Overview link has no rendered bounding box`)
  const hit = await page.evaluate(({ x, y }) => {
    const element = document.elementFromPoint(x, y)
    return {
      role: element?.closest('[role]')?.getAttribute('role') || '',
      text: element?.closest('button, a')?.textContent?.trim() || '',
    }
  }, { x: overviewBox.x + overviewBox.width / 2, y: overviewBox.y + overviewBox.height / 2 })
  assert(hit.role === 'menuitemradio', `${name}: open menu did not intentionally own the overlapping nav hit target (${JSON.stringify(hit)})`)
  assert(new URL(page.url()).pathname === '/hive/agents', `${name}: opening the menu navigated unexpectedly`)

  await page.keyboard.press('Escape')
  await menu.waitFor({ state: 'hidden' })
  assert(await trigger.evaluate((element) => document.activeElement === element), `${name}: Escape did not restore trigger focus`)

  const uncovered = await page.evaluate(({ x, y }) => {
    const element = document.elementFromPoint(x, y)
    return element?.closest('a')?.textContent?.trim() || ''
  }, { x: overviewBox.x + overviewBox.width / 2, y: overviewBox.y + overviewBox.height / 2 })
  assert(uncovered === 'Overview', `${name}: closed menu still blocked the Overview hit target (${uncovered || 'none'})`)
  await overview.click()
  await page.waitForURL('**/hive/overview')

  await page.goto(`${baseUrl}/hive/agents`, { waitUntil: 'networkidle' })
  await openMobileSidebar(page, mobile)
  assert((await switcher.getAttribute('data-org-switcher-state')) === 'closed', `${name}: switcher persisted open across navigation`)
  await trigger.click()
  await menu.waitFor({ state: 'visible' })

  const outsidePoint = { x: 252, y: 32 }
  const outsideIsSwitcher = await page.evaluate(({ x, y }) => (
    Boolean(document.elementFromPoint(x, y)?.closest('[data-org-switcher-state]'))
  ), outsidePoint)
  assert(!outsideIsSwitcher, `${name}: chosen outside-pointer coordinate is inside the switcher`)
  await page.mouse.click(outsidePoint.x, outsidePoint.y)
  await menu.waitFor({ state: 'hidden' })

  const teams = sidebar.getByRole('link', { name: 'Teams', exact: true })
  await teams.click()
  await page.waitForURL('**/hive/teams')
  assert(browserErrors.length === 0, `${name}: browser page errors: ${browserErrors.join(' | ')}`)

  process.stdout.write(`${JSON.stringify({ viewport: name, managedFocus: true, keyboard: ['Enter', 'Space', 'Tab', 'Shift+Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'Home', 'End'], openHit: hit.role, escapeFocus: true, outsideDismissal: true, sidebarNavigation: '/hive/teams' })}\n`)
  await context.close()
}

await waitForPreview()
const browser = await chromium.launch({ headless: true })
try {
  await runViewport(browser, 'desktop-1440', { width: 1440, height: 1000 })
  await runViewport(browser, 'mobile-390', { width: 390, height: 844 })
} finally {
  await browser.close()
}
