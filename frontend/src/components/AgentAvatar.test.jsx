import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it } from 'vitest'

import AgentAvatar from './AgentAvatar'

globalThis.IS_REACT_ACT_ENVIRONMENT = true

let container
let root

function render(el) {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  act(() => root.render(el))
}

afterEach(() => {
  if (root) act(() => root.unmount())
  container?.remove()
  root = null
  container = null
})

describe('AgentAvatar (HIVE-1242 — no 404 request for avatar-less agents)', () => {
  it('renders NO <img> when avatarUrl is absent — initials only, so no /avatars/ request is issued', () => {
    render(<AgentAvatar username="nobody" name="No Body" />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('No') // deterministic initials fallback
  })

  it('does not derive a src from username (the old unconditional /avatars/<username>.png request is gone)', () => {
    render(<AgentAvatar username="ghost" name="Ghost Agent" status="running" />)
    expect(container.querySelector('img')).toBeNull()
  })

  it('renders exactly the provided avatarUrl when one is given', () => {
    render(
      <AgentAvatar
        username="sara"
        name="Sara"
        avatarUrl="https://shizuha.com/avatars/sara.png"
      />,
    )
    const img = container.querySelector('img')
    expect(img).not.toBeNull()
    expect(img.getAttribute('src')).toBe('https://shizuha.com/avatars/sara.png')
  })

  it('still renders the presence dot without requesting an image when avatarUrl is absent', () => {
    render(<AgentAvatar username="idle" name="Idle" status="stopped" avatarUrl={null} />)
    expect(container.querySelector('img')).toBeNull()
    // presence wrapper present (relative inline-flex span with the dot)
    expect(container.querySelector('span.relative')).not.toBeNull()
  })

  it('uses the distinct blue presence dot for hibernated agents', () => {
    render(<AgentAvatar username="recon" name="Recon" status="hibernated" avatarUrl={null} />)
    expect(container.querySelector('.bg-sky-500')).not.toBeNull()
  })
})
