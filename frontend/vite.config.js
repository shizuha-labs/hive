import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'
import { existsSync } from 'node:fs'
import { execSync } from 'node:child_process'

// Base path for deployment — HIVE is served at /hive/ behind nginx (mirrors how
// shizuha-admin sets VITE_BASE_PATH=/admin/). Defaults to '/' for local dev.
const basePath = process.env.VITE_BASE_PATH || '/'
const serviceName = 'hive'

// Resolve @shizuha/ui to its TS source for HMR + a single build.
//
// The actual module entrypoint lives in src/index.ts. We alias to that
// subdirectory so Vite can resolve named imports (`from '@shizuha/ui'`) without
// an EISDIR error when the alias resolves to a directory with no root index.
//
// In monorepo/dev images the shared source may be mounted at `/packages` (or as
// a sibling checkout), which gives HMR against the live source. Standalone CI and
// production frontend builds intentionally do NOT copy the monorepo packages;
// they install the published `@shizuha/ui` package from the npm cache instead.
// Only alias when the source directory really exists; otherwise let Node/Vite
// resolve the installed package from node_modules.
const ABS_UI_SRC = '/packages/shizuha-ui/src'
const REL_UI_SRC = fileURLToPath(new URL('../../packages/shizuha-ui/src', import.meta.url))
const uiSrc = existsSync(ABS_UI_SRC)
  ? ABS_UI_SRC
  : (existsSync(REL_UI_SRC) ? REL_UI_SRC : null)

// Common allowed hosts for all Shizuha services (copied from vite.shared.js).
const allowedHosts = [
  'localhost',
  `shizuha-${serviceName}-frontend`,
  'nginx',
  'shizuha-nginx',
  'shizuha-nginx-dev',
  '.shizuha.com',
  `shizuha-shizuha-${serviceName}-frontend`,
  '.svc.cluster.local',
]

// HIVE-1398: resolve the exact git commit this build was produced from so live
// QA verdicts are self-anchoring to the deployed build. Precedence: explicit
// build env (VITE_BUILD_COMMIT) > CI (GITHUB_SHA) > local git HEAD.
function resolveBuildCommit() {
  const explicit =
    process.env.VITE_BUILD_COMMIT || process.env.GITHUB_SHA || process.env.CI_COMMIT_SHA
  if (explicit && explicit.trim()) {
    return explicit.trim()
  }
  try {
    return execSync('git rev-parse HEAD', { stdio: ['ignore', 'pipe', 'ignore'] })
      .toString()
      .trim()
  } catch {
    return 'unknown'
  }
}

const buildCommit = resolveBuildCommit()

// HIVE-1398: inject <meta name="build-commit"> into the served index.html and
// emit a lightweight version.json (build_commit + built_at) so QA can curl the
// exact deployed SHA without cluster read access.
function buildCommitPlugin() {
  return {
    name: 'hive-build-commit',
    transformIndexHtml(html) {
      return html.replace(
        '<meta name="viewport"',
        `<meta name="build-commit" content="${buildCommit}" />\n    <meta name="viewport"`,
      )
    },
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'version.json',
        source: JSON.stringify(
          {
            build_commit: buildCommit,
            built_at: new Date().toISOString(),
          },
          null,
          2,
        ),
      })
    },
  }
}

export default defineConfig({
  base: basePath,
  plugins: [react(), buildCommitPlugin()],
  resolve: {
    alias: {
      ...(uiSrc ? { '@shizuha/ui': uiSrc } : {}),
      react: fileURLToPath(new URL('./node_modules/react', import.meta.url)),
      'lucide-react': fileURLToPath(new URL('./node_modules/lucide-react', import.meta.url)),
    },
  },
  optimizeDeps: {
    include: ['lucide-react'],
  },
  server: {
    host: '0.0.0.0',
    port: 80,
    allowedHosts,
    hmr: process.env.VITE_HMR_DISABLE === 'true' ? false : true,
    watch: { usePolling: true },
    // Local dev proxy: forward /hive/api to the hive backend when running the
    // dev server at root. In production nginx handles /hive/api routing.
    proxy: basePath === '/' ? {
      '/hive/api': {
        target: process.env.VITE_API_URL || 'http://localhost:8030',
        changeOrigin: true,
      },
    } : {},
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
