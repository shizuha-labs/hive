import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, KeyRound, Loader2, FileCode, CheckCircle2 } from 'lucide-react'
import { antigravityOAuthApi } from '../services/api'
import { useToast } from './Toast'

/**
 * Connect a Google Antigravity account effortlessly from the browser:
 * 1. Start → Hive mints PKCE material and generates the official Google Antigravity
 *    sign-in link (sign-in happens directly on accounts.google.com).
 * 2. The user signs in; Google displays the authorization code or redirects to the callback page.
 * 3. Paste the code or callback URL here → Hive exchanges it, encrypts the bundle into the
 *    Google TokenPool, auto-fills the account email, and fetches live quota.
 *
 * Optional secondary mode: import from `~/.gemini/oauth_creds.json` file.
 */
export default function AntigravityOAuthPanel({ compact = false }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [session, setSession] = useState(null) // { authorize_url, state }
  const [code, setCode] = useState('')
  const [showFileImport, setShowFileImport] = useState(false)
  const [tokenJson, setTokenJson] = useState('')
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')

  const startMutation = useMutation({
    mutationFn: () => antigravityOAuthApi.start(),
    onSuccess: ({ data }) => {
      setError('')
      setResult(null)
      setCode('')
      setSession(data)
    },
    onError: (err) => setError(err.response?.data?.error || 'Could not start Google Antigravity sign-in.'),
  })

  const exchangeMutation = useMutation({
    mutationFn: () => antigravityOAuthApi.exchange({ code: code.trim(), state: session?.state || '' }),
    onSuccess: ({ data }) => {
      setSession(null)
      setCode('')
      setResult(data)
      setError('')
      queryClient.invalidateQueries({ queryKey: ['credential-health'] })
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      toast.success(
        `Google Antigravity account ${data.email || ''} ${data.created ? 'connected' : 're-authenticated'}`,
      )
    },
    onError: (err) => setError(err.response?.data?.error || 'Code exchange failed.'),
  })

  const importMutation = useMutation({
    mutationFn: () => antigravityOAuthApi.importTokens({ token_json: tokenJson.trim() }),
    onSuccess: ({ data }) => {
      setTokenJson('')
      setResult(data)
      setError('')
      queryClient.invalidateQueries({ queryKey: ['credential-health'] })
      queryClient.invalidateQueries({ queryKey: ['token-pools'] })
      toast.success(
        `Google Antigravity account ${data.email || ''} ${data.created ? 'connected' : 're-authenticated'}`,
      )
    },
    onError: (err) => setError(err.response?.data?.error || err.response?.data?.detail || 'Token import failed.'),
  })

  return (
    <div className={compact ? '' : 'hv-panel mt-4 p-3'}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        {!compact && (
          <div className="text-sm">
            <span className="font-medium">Connect Google Antigravity account</span>
            <span className="ml-2 text-gray-500 dark:text-gray-400">
              Sign in via browser with your Google account.
            </span>
          </div>
        )}
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => startMutation.mutate()}
            disabled={startMutation.isPending || !!session}
            className="inline-flex items-center gap-1.5 rounded-lg bg-gradient-to-b from-blue-500 to-blue-700 px-3 py-1.5 text-sm font-semibold text-white shadow-[0_2px_12px_-2px_rgba(59,130,246,0.5)] transition-transform hover:-translate-y-px disabled:cursor-not-allowed disabled:opacity-50"
          >
            {startMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
            Connect Google account
          </button>
        </div>
      </div>

      {session && (
        <div className="hv-panel mt-3 p-4 text-sm">
          <p className="font-medium text-gray-900 dark:text-gray-100">
            Step 1 —{' '}
            <a
              href={session.authorize_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-semibold text-blue-600 dark:text-blue-400 underline hover:text-blue-700"
            >
              Open Google Sign-In <ExternalLink className="h-3.5 w-3.5" />
            </a>{' '}
            and authorize with your Google Antigravity account.
          </p>
          <p className="mt-2 text-gray-600 dark:text-gray-400">
            Step 2 — Paste the authorization code or callback URL shown after sign-in:
          </p>
          <div className="mt-2 flex gap-2">
            <input
              value={code}
              onChange={(e) => { setCode(e.target.value); setError(''); }}
              placeholder="Paste code or redirect URL here (e.g. 4/0AX4X...)"
              className="w-full max-w-lg rounded border border-gray-300 bg-white px-2.5 py-1.5 font-mono text-xs dark:border-gray-600 dark:bg-gray-950 focus:ring-2 focus:ring-blue-500 focus:outline-none"
            />
            <button
              type="button"
              disabled={exchangeMutation.isPending || !code.trim()}
              onClick={() => exchangeMutation.mutate()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 transition-colors disabled:cursor-not-allowed disabled:opacity-50"
            >
              {exchangeMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
              Connect
            </button>
          </div>
        </div>
      )}

      {/* Secondary file import toggle */}
      <div className="mt-3">
        <button
          type="button"
          onClick={() => setShowFileImport(!showFileImport)}
          className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
        >
          <FileCode className="h-3.5 w-3.5" />
          {showFileImport ? 'Hide file import' : 'Or import from ~/.gemini/oauth_creds.json'}
        </button>

        {showFileImport && (
          <div className="hv-panel mt-2 p-3 text-sm">
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Paste contents of <code className="rounded bg-gray-100 px-1 py-0.5 font-mono dark:bg-gray-800">~/.gemini/oauth_creds.json</code>:
            </p>
            <textarea
              value={tokenJson}
              onChange={(e) => { setTokenJson(e.target.value); setError(''); }}
              placeholder='{"access_token": "ya29...", "refresh_token": "1//...", ...}'
              rows={3}
              className="mt-2 w-full max-w-lg rounded border border-gray-300 bg-white px-2 py-1.5 font-mono text-xs dark:border-gray-600 dark:bg-gray-950 resize-y"
            />
            <div className="mt-2">
              <button
                type="button"
                disabled={importMutation.isPending || !tokenJson.trim()}
                onClick={() => importMutation.mutate()}
                className="inline-flex items-center gap-1.5 rounded bg-gray-800 px-2.5 py-1 text-xs font-medium text-white hover:bg-gray-700 dark:bg-gray-700 dark:hover:bg-gray-600 disabled:opacity-50"
              >
                {importMutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}
                Import JSON
              </button>
            </div>
          </div>
        )}
      </div>

      {result && (
        <div className="mt-3 flex items-center gap-2 text-sm text-green-700 dark:text-green-400">
          <CheckCircle2 className="h-4 w-4 shrink-0" />
          <span>
            Connected <span className="font-semibold">{result.email || 'Google account'}</span> — credential bundle encrypted into your pool.
          </span>
        </div>
      )}
      {error && <p className="mt-3 text-sm font-medium text-red-600 dark:text-red-400">{error}</p>}
    </div>
  )
}
