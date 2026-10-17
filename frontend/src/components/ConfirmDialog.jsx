import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, X } from 'lucide-react'
import clsx from 'clsx'
import { cycleDialogFocus } from '../lib/dialogFocus'

/**
 * Modal confirm for destructive lifecycle actions.
 *
 * Props:
 *  - open, title, message
 *  - confirmLabel, cancelLabel
 *  - destructive (red button)
 *  - requireText: if set, the user must type this exact string to enable confirm
 *    (used for claw delete which the backend gates on ?confirm=<name>)
 *  - loading: disables buttons + shows spinner text
 *  - onConfirm, onCancel
 */
export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  destructive = false,
  requireText = null,
  loading = false,
  onConfirm,
  onCancel,
}) {
  const [typed, setTyped] = useState('')
  const dialogRef = useRef(null)
  const initialFocusRef = useRef(null)
  const loadingRef = useRef(loading)
  const onCancelRef = useRef(onCancel)

  loadingRef.current = loading
  onCancelRef.current = onCancel

  useEffect(() => {
    if (open) setTyped('')
  }, [open])

  useEffect(() => {
    if (!open) return undefined
    const previousFocus = document.activeElement
    initialFocusRef.current?.focus()
    const onKey = (event) => {
      if (event.key === 'Escape') {
        // This confirmation is the top modal layer. Consume Escape even while
        // loading so it can never leak through and close the parent drawer.
        event.preventDefault()
        event.stopPropagation()
        if (!loadingRef.current) onCancelRef.current?.()
        return
      }
      if (event.key === 'Tab') {
        event.stopPropagation()
        cycleDialogFocus(event, dialogRef.current)
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [open])

  if (!open) return null

  const confirmDisabled = loading || (requireText != null && typed !== requireText)

  return (
    <div data-agent-drawer-nested-modal="true" className="fixed inset-0 z-[140] flex items-center justify-center p-4">
      <div aria-hidden="true" className="absolute inset-0 bg-black/50" onClick={() => !loading && onCancel?.()} />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={title || 'Confirm action'}
        tabIndex={-1}
        className="relative w-full max-w-md rounded-xl bg-white dark:bg-gray-800 shadow-xl border border-gray-200 dark:border-gray-700"
      >
        <div className="flex items-start gap-3 p-5">
          {destructive && (
            <div className="shrink-0 p-2 rounded-full bg-red-100 dark:bg-red-900/30">
              <AlertTriangle className="h-5 w-5 text-red-600 dark:text-red-400" />
            </div>
          )}
          <div className="flex-1 min-w-0">
            <h3 className="text-lg font-semibold">{title}</h3>
            {message && (
              <div className="mt-1 text-sm text-gray-500 dark:text-gray-400">{message}</div>
            )}
          </div>
          <button
            aria-label="Close confirmation"
            onClick={() => !loading && onCancel?.()}
            className="shrink-0 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {requireText != null && (
          <div className="px-5 pb-2">
            <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">
              Type <span className="font-mono font-semibold">{requireText}</span> to confirm
            </label>
            <input
              ref={initialFocusRef}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              className="w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 px-3 py-2 text-sm focus:ring-2 focus:ring-primary-500 focus:outline-none"
              placeholder={requireText}
            />
          </div>
        )}

        <div className="flex justify-end gap-2 px-5 py-4 border-t border-gray-200 dark:border-gray-700">
          <button
            ref={requireText == null ? initialFocusRef : undefined}
            onClick={() => onCancel?.()}
            disabled={loading}
            className="px-4 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            onClick={() => onConfirm?.()}
            disabled={confirmDisabled}
            className={clsx(
              'px-4 py-2 text-sm rounded-lg text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed',
              destructive
                ? 'bg-red-600 hover:bg-red-700'
                : 'bg-primary-600 hover:bg-primary-700'
            )}
          >
            {loading ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
