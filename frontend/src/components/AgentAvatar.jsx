import { useState } from 'react'
import clsx from 'clsx'

/**
 * AgentAvatar — the same avatar identity as shizuha.com home (HIVE-617:
 * one product family).
 *
 * HIVE-1242: the portrait is rendered ONLY when the caller passes a resolved
 * `avatarUrl` (the API's `avatar_url` — a full URL for the ~42 agents that have
 * a published portrait, else null). When there is no `avatarUrl` we render the
 * deterministic gradient-initials fallback WITHOUT issuing an image request —
 * previously the component unconditionally requested `/avatars/<username>.png`
 * for every agent, 404-ing for the many avatar-less ones. `username` is still
 * used for the stable fallback gradient, the initials, and the img alt text.
 *
 * `status` adds the home LiveTheater presence dot: green ping = running,
 * amber pulse = transitional, red = stopped/failed. Pass nothing to omit.
 */

// Deterministic gradient per username so the fallback face is stable.
const GRADIENTS = [
  'from-indigo-500 to-purple-500',
  'from-amber-500 to-orange-600',
  'from-emerald-500 to-teal-600',
  'from-sky-500 to-blue-600',
  'from-rose-500 to-pink-600',
  'from-violet-500 to-fuchsia-600',
]

function gradientFor(username) {
  let h = 0
  const s = String(username || '?')
  for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return GRADIENTS[h % GRADIENTS.length]
}

const SIZES = {
  sm: 'h-6 w-6 text-[9px] rounded-md',
  md: 'h-9 w-9 text-[11px] rounded-lg',
  lg: 'h-14 w-14 text-lg rounded-xl',
}

const DOT = {
  running: 'bg-emerald-500',
  hibernated: 'bg-sky-500',
  transitional: 'bg-amber-400',
  stopped: 'bg-red-400',
}

function presenceKind(status) {
  const s = (status || '').toLowerCase()
  if (!s) return null
  if (s === 'running') return 'running'
  if (s === 'hibernated') return 'hibernated'
  if (['stopping', 'starting', 'restarting', 'provisioning', 'deleting', 'pending'].includes(s)) return 'transitional'
  return 'stopped'
}

export default function AgentAvatar({ username, name, avatarUrl, size = 'md', status, className }) {
  // Guards against a provided URL that still 404s (stale/removed portrait):
  // fall back to initials rather than showing a broken image.
  const [broken, setBroken] = useState(false)
  const initials = (name || username || '?').slice(0, 2)
  const kind = presenceKind(status)

  const face = (!avatarUrl || broken) ? (
    <span
      className={clsx(
        'flex items-center justify-center bg-gradient-to-br font-bold uppercase text-white shadow-sm',
        gradientFor(username), SIZES[size], className,
      )}
    >
      {initials}
    </span>
  ) : (
    <img
      src={avatarUrl}
      alt={name || username || 'agent'}
      onError={() => setBroken(true)}
      className={clsx('object-cover shadow-sm', SIZES[size], className)}
    />
  )

  if (!kind) return face
  return (
    <span className="relative inline-flex shrink-0">
      {face}
      <span className="absolute -right-0.5 -top-0.5 flex h-2.5 w-2.5">
        {kind === 'running' && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
        )}
        {kind === 'transitional' && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-300 opacity-75" />
        )}
        <span className={clsx(
          'relative inline-flex h-2.5 w-2.5 rounded-full border-2 border-white dark:border-gray-900',
          DOT[kind],
        )} />
      </span>
    </span>
  )
}
