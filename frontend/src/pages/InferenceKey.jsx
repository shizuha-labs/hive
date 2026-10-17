import { KeyRound, ArrowRight, Cpu } from 'lucide-react'

/**
 * HIVE-76 → CTX-415 (operator 2026-07-10): personal Cortex inference keys are
 * managed in Cortex itself now — Cortex Router is the inference product, so
 * key self-serve lives at cortex.shizuha.com. This page stays as a signpost so
 * existing bookmarks and the Hive nav don't dead-end. The Hive→Cortex key
 * proxy API (/hive/api/v1/cortex/api-keys) remains for programmatic callers.
 */
const CORTEX_KEYS_URL = 'https://cortex.shizuha.com/cortex/inference-key'

export default function InferenceKey() {
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <KeyRound className="h-6 w-6 text-primary-500" />
          Inference Key
        </h1>
        <p className="text-gray-500 dark:text-gray-400">
          Inference keys have moved to <strong>Cortex Router</strong> — the inference marketplace.
        </p>
      </div>

      <div className="rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-6 max-w-xl">
        <div className="flex items-center gap-2 mb-2">
          <Cpu className="h-5 w-5 text-sky-500" />
          <span className="font-semibold">cortex.shizuha.com</span>
        </div>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Generate, copy and revoke your personal <code>sk-cortex-…</code> keys on the
          Cortex Inference Key page. Sign in with the same Shizuha ID — your existing
          keys are already there.
        </p>
        <a
          href={CORTEX_KEYS_URL}
          className="inline-flex items-center gap-2 rounded-lg bg-sky-600 hover:bg-sky-500 text-white text-sm font-medium px-4 py-2 transition-colors"
        >
          Open Cortex Inference Keys <ArrowRight className="h-4 w-4" />
        </a>
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-4">
          Tip: <code>shizuha auth cortex</code> accepts the key exactly as before.
        </p>
      </div>
    </div>
  )
}
