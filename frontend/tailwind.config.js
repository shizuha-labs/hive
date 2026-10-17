import { fileURLToPath, URL } from 'node:url'
import { existsSync } from 'node:fs'

// Include the shared @shizuha/ui source so Tailwind detects its classes.
// Mirror vite.config.js: prefer the Docker /packages path, fall back to sibling.
const ABS_UI = '/packages/shizuha-ui/src/**/*.{js,ts,jsx,tsx}'
const REL_UI = fileURLToPath(new URL('../../packages/shizuha-ui/src', import.meta.url)) + '/**/*.{js,ts,jsx,tsx}'
const uiContent = existsSync('/packages/shizuha-ui/src') ? ABS_UI : REL_UI

// The STANDALONE CI build (frontend/Dockerfile.ci) installs the *published*
// `@shizuha/ui` from the in-cluster npm-cache — that package ships `dist/`
// only (no `src/`), so the monorepo globs above don't exist there and every
// @shizuha/ui-only utility (e.g. the HiveWordmark hex mark's stroke-amber-500 /
// fill-amber-500) was silently purged, leaving the hive icon invisible on live.
// Scan the installed package's compiled dist too — works in BOTH layouts.
const INSTALLED_UI_DIST = fileURLToPath(new URL('./node_modules/@shizuha/ui/dist', import.meta.url)) + '/**/*.{js,mjs}'

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}',
    uiContent,
    INSTALLED_UI_DIST,
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Product-family accent = shizuha.com indigo/violet (HIVE-617 reimagining
        // 2026-07-11): Hive's interactive/brand accent matches the home + cortex
        // console. Amber is retained ONLY for the honeycomb mark + genuine
        // hot/warning semantics (needs-help, upgrade-available), never as chrome.
        primary: {
          50: '#eef2ff',
          100: '#e0e7ff',
          200: '#c7d2fe',
          300: '#a5b4fc',
          400: '#818cf8',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
          800: '#3730a3',
          900: '#312e81',
        },
      },
    },
  },
  plugins: [],
}
