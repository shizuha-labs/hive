import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, KeyRound, Loader2 } from 'lucide-react'
import { claudeOAuthApi } from '../services/api'
import { useToast } from './Toast'

/**
 * Connect a Claude account end-to-end from the browser:
 * 1. Start → Hive mints PKCE material and shows the OFFICIAL Claude Code
 *    sign-in link (the login itself happens entirely on claude.ai).
 * 2. The operator signs in; Anthropic's callback page displays a code.
 * 3. Paste the code here → Hive exchanges it, encrypts the bundle into the
 *    org Anthropic pool, auto-fills the account email from the profile API,
 *    and fetches the first quota snapshot.
 */
export default function ClaudeOAuthPanel({ compact = false }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [session, setSession] = useState(null) // {authorize_url, state}
  const [code, setCode] = useState('')
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')

  const startMutation = useMutation({
    mutationFn: () => claudeOAuthApi.start(),
    onSuccess: ({ data }) => {
      setError('')
      setResult(null)
      setCode('')
      setSession(data)
    },
    onError: (err) => setError(err.response?.data?.error || 'Could not start Claude sign-in.'),
  })

  const exchangeMutation = useMutation({
    mutationFn: () => claudeOAuthApi.exchange({ code: code.trim(), state: session?.state || '' }),
    onSuccess: ({ data }) => {
      setSession(null)
      setCode('')
      setResult(data)
      queryClient.invalidateQueries({ queryKey: ['credential-health'] })
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      toast.success(
        `Claude account ${data.email || ''} ${data.created ? 'connected' : 're-authenticated'}`
        + (data.quota_windows ? ` · ${data.quota_windows} quota windows` : ''),
      )
    },
    onError: (err) => setError(err.response?.data?.error || 'Code exchange failed.'),
  })

  return (
    <div className={compact ? '' : 'hv-panel mt-4 p-3'}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        {!compact && (
          <div className="text-sm">
            <span className="font-medium">Connect Claude account</span>
            <span className="ml-2 text-gray-500 dark:text-gray-400">
              Full OAuth (quota bars + account identity). Sign-in stays on claude.ai.
            </span>
          </div>
        )}
        <button
          type="button"
          onClick={() => startMutation.mutate()}
          disabled={startMutation.isPending || !!session}
          className="inline-flex items-center gap-1.5 rounded-lg bg-gradient-to-b from-amber-400 to-amber-600 px-3 py-1.5 text-sm font-semibold text-gray-950 shadow-[0_2px_12px_-2px_rgba(245,158,11,0.5)] transition-transform hover:-translate-y-px disabled:cursor-not-allowed disabled:opacity-50"
        >
          {startMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
          Connect Claude account
        </button>
      </div>

      {session && (
        <div className="hv-panel mt-3 p-4 text-sm">
          <p>
            1.{' '}
            <a
              href={session.authorize_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-medium text-primary-600 underline"
            >
              Open Claude sign-in <ExternalLink className="h-3.5 w-3.5" />
            </a>{' '}
            and log in with the account you want to connect.
          </p>
          <p className="mt-2">2. Paste the code the page shows after login:</p>
          <div className="mt-2 flex gap-2">
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="code#state"
              className="w-full max-w-md rounded border border-gray-300 bg-white px-2 py-1.5 font-mono text-xs dark:border-gray-600 dark:bg-gray-950"
            />
            <button
              type="button"
              disabled={exchangeMutation.isPending || !code.trim()}
              onClick={() => exchangeMutation.mutate()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-gradient-to-b from-amber-400 to-amber-600 px-3 py-1.5 text-sm font-semibold text-gray-950 shadow-[0_2px_12px_-2px_rgba(245,158,11,0.5)] transition-transform hover:-translate-y-px disabled:cursor-not-allowed disabled:opacity-50"
            >
              {exchangeMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
              Connect
            </button>
          </div>
        </div>
      )}

      {result && (
        <p className="mt-3 text-sm text-green-700 dark:text-green-400">
          Connected <span className="font-medium">{result.email || 'account'}</span> — bundle encrypted into your personal pool
          ({result.created ? 'new entry' : 'rotated in place'}
          {result.quota_windows ? `, ${result.quota_windows} quota windows live` : ''}).
        </p>
      )}
      {error && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{error}</p>}
    </div>
  )
}
