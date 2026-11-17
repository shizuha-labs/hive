import { describe, expect, it } from 'vitest'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { act } from 'react'

import { AppSwitcher } from '@shizuha/ui'

// React 18 act() environment flag (silences the jsdom act warning).
globalThis.IS_REACT_ACT_ENVIRONMENT = true

// HIVE-716 regression: on /hive the app switcher must highlight HIVE (via
// currentAppId), not Home. HIVE-535 live QA recorded Home carrying the amber
// current-app highlight on the Hive route.
function renderSwitcher(currentAppId) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  let root = null
  act(() => {
    root = createRoot(host)
    root.render(
      createElement(AppSwitcher, {
        currentAppId,
        enabledServices: [],
        accessStatus: 'ready',
        variant: 'compact',
      }),
    )
  })
  return host
}

function openMenu(host) {
  const trigger = host.querySelector('button[aria-haspopup="menu"]')
  expect(trigger, 'switcher trigger button').toBeTruthy()
  act(() => {
    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  })
}

function highlightedApps(host) {
  return Array.from(host.querySelectorAll('[role="menuitem"]'))
    .filter((el) => el.classList.contains('bg-amber-50'))
    .map((el) => (el.textContent || '').trim())
}

describe('AppSwitcher current-app highlight (HIVE-716)', () => {
  it('highlights HIVE — not Home — when currentAppId is "hive" (the /hive route)', () => {
    const host = renderSwitcher('hive')
    openMenu(host)

    const highlighted = highlightedApps(host)
    expect(highlighted).toHaveLength(1)
    expect(highlighted[0]).toMatch(/^HIVE/)
    expect(highlighted[0]).not.toMatch(/^Home/)
  })

  it('highlights Home when currentAppId is "home" (the / route)', () => {
    const host = renderSwitcher('home')
    openMenu(host)

    const highlighted = highlightedApps(host)
    expect(highlighted).toHaveLength(1)
    expect(highlighted[0]).toMatch(/^Home/)
  })
})
