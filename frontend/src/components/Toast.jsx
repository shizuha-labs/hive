import { createContext, useContext, useState, useCallback } from 'react'
import { CheckCircle2, AlertCircle, Info, X } from 'lucide-react'
import clsx from 'clsx'

const ToastContext = createContext(null)

let _id = 0

const ICONS = {
  success: CheckCircle2,
  error: AlertCircle,
  info: Info,
}

const STYLES = {
  success: 'border-green-200 dark:border-green-900 text-green-800 dark:text-green-300',
  error: 'border-red-200 dark:border-red-900 text-red-800 dark:text-red-300',
  info: 'border-gray-200 dark:border-gray-700 text-gray-800 dark:text-gray-200',
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const dismiss = useCallback((id) => {
    setToasts((t) => t.filter((x) => x.id !== id))
  }, [])

  const push = useCallback(
    (message, type = 'info', ttl = 4000) => {
      const id = ++_id
      setToasts((t) => [...t, { id, message, type }])
      if (ttl) setTimeout(() => dismiss(id), ttl)
      return id
    },
    [dismiss]
  )

  const toast = {
    success: (m) => push(m, 'success'),
    error: (m) => push(m, 'error', 6000),
    info: (m) => push(m, 'info'),
  }

  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2 w-full max-w-sm pointer-events-none">
        {toasts.map((t) => {
          const Icon = ICONS[t.type] || Info
          return (
            <div
              key={t.id}
              className={clsx(
                'pointer-events-auto flex items-start gap-3 rounded-lg border bg-white dark:bg-gray-800 px-4 py-3 shadow-lg',
                STYLES[t.type]
              )}
            >
              <Icon className="h-5 w-5 shrink-0 mt-0.5" />
              <p className="text-sm flex-1 text-gray-700 dark:text-gray-200">{t.message}</p>
              <button
                onClick={() => dismiss(t.id)}
                className="shrink-0 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within a ToastProvider')
  return ctx
}
