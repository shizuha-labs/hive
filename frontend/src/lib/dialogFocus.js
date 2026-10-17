const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export function focusableElements(container) {
  if (!container) return []
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter((element) => (
    !element.hidden
    && element.getAttribute('aria-hidden') !== 'true'
    && !element.closest('[inert]')
  ))
}

/** Keep Tab/Shift+Tab inside the active modal dialog. */
export function cycleDialogFocus(event, container) {
  if (event.key !== 'Tab' || !container) return false
  const focusable = focusableElements(container)
  if (!focusable.length) {
    event.preventDefault()
    container.focus()
    return true
  }

  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  const active = document.activeElement
  if (!container.contains(active)) {
    event.preventDefault()
    ;(event.shiftKey ? last : first).focus()
    return true
  }
  if (event.shiftKey && active === first) {
    event.preventDefault()
    last.focus()
    return true
  }
  if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
    return true
  }
  return false
}

/**
 * Make every body sibling outside a portaled modal inert, preserving any state
 * that was already present so nested app shells are restored exactly on close.
 */
export function inertBodySiblings(dialogElement) {
  if (!dialogElement?.parentElement) return () => {}
  const siblings = Array.from(document.body.children).filter((element) => element !== dialogElement)
  const snapshots = siblings.map((element) => ({
    element,
    ariaHidden: element.getAttribute('aria-hidden'),
    inert: element.hasAttribute('inert'),
  }))

  for (const { element } of snapshots) {
    element.setAttribute('aria-hidden', 'true')
    element.setAttribute('inert', '')
    if ('inert' in element) element.inert = true
  }

  return () => {
    for (const { element, ariaHidden, inert } of snapshots) {
      if (ariaHidden == null) element.removeAttribute('aria-hidden')
      else element.setAttribute('aria-hidden', ariaHidden)
      if (inert) element.setAttribute('inert', '')
      else element.removeAttribute('inert')
      if ('inert' in element) element.inert = inert
    }
  }
}
