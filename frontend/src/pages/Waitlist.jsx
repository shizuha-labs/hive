import { useState } from 'react'
import { Navigate } from 'react-router-dom'
import { Bot, Sparkles, Zap, Shield, ArrowRight, CheckCircle } from 'lucide-react'
import { useHive } from '../contexts/HiveContext'
import api from '../services/api'

const FEATURES = [
  { icon: Bot, label: 'Personal AI agents', desc: 'Deploy your own OpenClaw instances on dedicated compute.' },
  { icon: Zap, label: 'On-demand scaling', desc: 'Start and stop agents with a click; pay only for what you use.' },
  { icon: Shield, label: 'Isolated namespaces', desc: 'Each agent runs in its own Kubernetes namespace with a private PVC.' },
  { icon: Sparkles, label: 'Unified control plane', desc: 'Manage agents, API keys, and token pools from one dashboard.' },
]

export default function Waitlist() {
  const { isAuthenticated } = useHive()
  const [email, setEmail] = useState('')
  const [status, setStatus] = useState('idle') // idle | submitting | success | error
  const [errorMsg, setErrorMsg] = useState('')

  if (isAuthenticated) {
    return <Navigate to="/overview" replace />
  }

  // Operator 2026-07-10 (shizuha.com tree audit): the old public waitlist page
  // pitched the legacy "personal OpenClaw instances" story, contradicting the
  // main landing. shizuha.com/ is the marketing surface; /hive is app-only —
  // unauthenticated visitors go to sign-in like every other /hive route.
  if (typeof window !== 'undefined') {
    window.location.replace('/id/login?continue=/hive/overview')
    return null
  }

  async function handleSubmit(e) {
    e.preventDefault()
    if (!email.trim()) return
    setStatus('submitting')
    setErrorMsg('')
    try {
      await api.post('/v1/waitlist/', { email: email.trim() })
      setStatus('success')
    } catch (err) {
      const detail = err?.response?.data?.detail || err?.response?.data?.email?.[0]
      if (err?.response?.status === 409 || /already/i.test(String(detail))) {
        setStatus('success')
        return
      }
      setErrorMsg(detail || 'Something went wrong. Please try again.')
      setStatus('error')
    }
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-indigo-950 text-white flex flex-col">
      {/* Nav */}
      <header className="flex items-center justify-between px-6 py-4 max-w-6xl mx-auto w-full">
        <div className="flex items-center gap-2 font-semibold text-lg tracking-tight">
          <Bot className="h-6 w-6 text-indigo-400" />
          <span>Shizuha Hive</span>
        </div>
        <a
          href="/id/login?continue=/hive/overview"
          className="text-sm text-gray-400 hover:text-white transition-colors"
        >
          Sign in
        </a>
      </header>

      {/* Hero */}
      <main className="flex-1 flex flex-col items-center justify-center px-6 py-16 text-center max-w-4xl mx-auto w-full">
        <div className="inline-flex items-center gap-2 bg-indigo-500/10 border border-indigo-500/30 rounded-full px-4 py-1.5 text-indigo-300 text-sm mb-8">
          <Sparkles className="h-3.5 w-3.5" />
          Early access
        </div>

        <h1 className="text-4xl sm:text-5xl font-bold leading-tight mb-4">
          Your personal AI agents,{' '}
          <span className="text-indigo-400">ready on demand</span>
        </h1>
        <p className="text-gray-400 text-lg max-w-xl mb-10">
          Hive provisions isolated OpenClaw assistant instances on Kubernetes.
          Full control, persistent workspaces, and a clean REST API — all from one dashboard.
        </p>

        {status === 'success' ? (
          <div className="flex flex-col items-center gap-3 text-emerald-400">
            <CheckCircle className="h-10 w-10" />
            <p className="text-lg font-medium">You're on the list!</p>
            <p className="text-gray-400 text-sm">We'll email you when early access opens.</p>
          </div>
        ) : (
          <div className="w-full max-w-md">
            <form onSubmit={handleSubmit} className="flex flex-col sm:flex-row gap-3">
              <input
                type="email"
                required
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                disabled={status === 'submitting'}
                aria-describedby="waitlist-privacy-notice"
                className="flex-1 px-4 py-3 rounded-xl bg-gray-800 border border-gray-700 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent disabled:opacity-60"
              />
              <button
                type="submit"
                disabled={status === 'submitting' || !email.trim()}
                className="flex items-center justify-center gap-2 px-6 py-3 rounded-xl bg-indigo-600 hover:bg-indigo-500 font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed whitespace-nowrap"
              >
                {status === 'submitting' ? 'Joining…' : 'Join waitlist'}
                {status !== 'submitting' && <ArrowRight className="h-4 w-4" />}
              </button>
            </form>
            <p id="waitlist-privacy-notice" className="mt-3 text-left text-xs leading-relaxed text-gray-500">
              By joining, you consent to Shizuha collecting your email address
              for the internal Hive early-access waitlist and contacting you
              about availability. If you later share a preferred channel or use
              case, we use it only to prioritize access and support onboarding.
              We do not collect IP/User-Agent analytics from this form. To
              withdraw consent or request removal, email{' '}
              <a href="mailto:privacy@shizuha.com" className="text-indigo-400 hover:text-indigo-300">
                privacy@shizuha.com
              </a>.
            </p>
          </div>
        )}

        {status === 'error' && (
          <p className="mt-3 text-sm text-red-400">{errorMsg}</p>
        )}

        <p className="mt-4 text-sm text-gray-500">
          Already have an account?{' '}
          <a href="/id/login?continue=/hive/overview" className="text-indigo-400 hover:text-indigo-300 transition-colors">
            Sign in
          </a>
        </p>
      </main>

      {/* Feature grid */}
      <section className="max-w-4xl mx-auto w-full px-6 pb-20 grid sm:grid-cols-2 gap-4">
        {FEATURES.map(({ icon: Icon, label, desc }) => (
          <div key={label} className="flex gap-4 p-5 rounded-xl bg-white/5 border border-white/10">
            <div className="flex-shrink-0 p-2 rounded-lg bg-indigo-500/20">
              <Icon className="h-5 w-5 text-indigo-400" />
            </div>
            <div>
              <p className="font-medium text-sm">{label}</p>
              <p className="text-gray-400 text-sm mt-0.5">{desc}</p>
            </div>
          </div>
        ))}
      </section>
    </div>
  )
}
