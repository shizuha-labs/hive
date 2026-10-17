import React, { act, useState } from 'react'
import { Simulate } from 'react-dom/test-utils'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it } from 'vitest'
import ConfirmDialog from './ConfirmDialog'
import { Shell } from './AgentDrawer'

globalThis.IS_REACT_ACT_ENVIRONMENT = true

let container
let root

function DrawerHarness() {
  const [open, setOpen] = useState(false)
  const [confirm, setConfirm] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Open details</button>
      {open && (
        <Shell
          title="Jun"
          subtitle="@jun · Runtime agent"
          onClose={() => setOpen(false)}
          actions={<button type="button">Last drawer action</button>}
        >
          <button type="button" onClick={() => setConfirm(true)}>Delete agent</button>
          <a href="#agent-log">Agent log</a>
          <ConfirmDialog
            open={confirm}
            title="Delete agent permanently?"
            onCancel={() => setConfirm(false)}
            onConfirm={() => setConfirm(false)}
          />
        </Shell>
      )}
    </>
  )
}

function renderHarness() {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => root.render(<DrawerHarness />))
  const opener = container.querySelector('button')
  opener.focus()
  act(() => Simulate.click(opener))
  return { opener }
}

function keydown(key, options = {}) {
  act(() => {
    document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {
      key,
      bubbles: true,
      cancelable: true,
      ...options,
    }))
  })
}

afterEach(() => {
  if (root) act(() => root.unmount())
  container?.remove()
  document.body.querySelectorAll('[data-agent-drawer-root="true"]').forEach((node) => node.remove())
  root = null
  container = null
})

describe('Agent drawer modal contract', () => {
  it('portals to body, makes the app inert, enters focus, traps Tab, and restores focus on Escape', () => {
    const { opener } = renderHarness()
    const dialog = document.body.querySelector('[data-agent-drawer-root="true"]')
    const close = dialog.querySelector('[aria-label="Close agent details"]')
    const last = Array.from(dialog.querySelectorAll('button')).find((node) => node.textContent === 'Last drawer action')

    expect(dialog.parentElement).toBe(document.body)
    expect(dialog.className).toContain('fixed inset-0')
    expect(dialog.querySelector('[data-agent-drawer-panel="true"]').className).toContain('h-full')
    expect(container.getAttribute('aria-hidden')).toBe('true')
    expect(container.hasAttribute('inert')).toBe(true)
    expect(document.activeElement).toBe(close)

    last.focus()
    keydown('Tab')
    expect(document.activeElement).toBe(close)
    keydown('Tab', { shiftKey: true })
    expect(document.activeElement).toBe(last)

    keydown('Escape')
    expect(document.body.querySelector('[data-agent-drawer-root="true"]')).toBeNull()
    expect(container.hasAttribute('inert')).toBe(false)
    expect(container.hasAttribute('aria-hidden')).toBe(false)
    expect(document.activeElement).toBe(opener)
  })

  it('closes only the nested confirmation on the first Escape', () => {
    renderHarness()
    const drawer = document.body.querySelector('[data-agent-drawer-root="true"]')
    const deleteButton = Array.from(drawer.querySelectorAll('button')).find((node) => node.textContent === 'Delete agent')
    deleteButton.focus()
    act(() => Simulate.click(deleteButton))

    const confirmation = drawer.querySelector('[role="dialog"][aria-label="Delete agent permanently?"]')
    expect(confirmation).toBeTruthy()
    expect(document.activeElement.textContent).toBe('Cancel')

    keydown('Escape')
    expect(drawer.querySelector('[role="dialog"][aria-label="Delete agent permanently?"]')).toBeNull()
    expect(document.body.querySelector('[data-agent-drawer-root="true"]')).toBe(drawer)
    expect(document.activeElement).toBe(deleteButton)

    keydown('Escape')
    expect(document.body.querySelector('[data-agent-drawer-root="true"]')).toBeNull()
  })

  it('closes from the backdrop and exposes an accessible close-button name', () => {
    const { opener } = renderHarness()
    const dialog = document.body.querySelector('[data-agent-drawer-root="true"]')
    expect(dialog.querySelector('[aria-label="Close agent details"]')).toBeTruthy()
    act(() => Simulate.click(dialog.querySelector('[data-agent-drawer-backdrop="true"]')))
    expect(document.body.querySelector('[data-agent-drawer-root="true"]')).toBeNull()
    expect(document.activeElement).toBe(opener)
  })
})
