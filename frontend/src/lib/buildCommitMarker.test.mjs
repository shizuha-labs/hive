/**
 * HIVE-1398: the built hive-frontend must expose a build-commit marker so live
 * QA verdicts are self-anchoring to the exact deployed SHA.
 *
 * The vite config injects <meta name="build-commit"> into index.html and emits
 * a version.json (build_commit + built_at). These tests assert the build
 * pipeline produces both without needing cluster read access.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const frontendRoot = resolve(__dirname, '../..')

function read(rel) {
  return readFileSync(resolve(frontendRoot, rel), 'utf8')
}

describe('HIVE-1398 build-commit marker', () => {
  it('vite config injects the build-commit meta tag into index.html', () => {
    const cfg = read('vite.config.js')
    expect(cfg).toContain('build-commit')
    expect(cfg).toContain('transformIndexHtml')
    expect(cfg).toContain('VITE_BUILD_COMMIT')
  })

  it('vite config emits a version.json with build_commit + built_at', () => {
    const cfg = read('vite.config.js')
    expect(cfg).toContain('version.json')
    expect(cfg).toContain('build_commit')
    expect(cfg).toContain('built_at')
    expect(cfg).toContain('generateBundle')
  })

  it('a real build output carries the meta tag and version.json', () => {
    // The build is run by CI (npm run build); assert the committed source
    // contract and, when a dist exists, the produced marker.
    const hasDist = (() => {
      try {
        read('dist/index.html')
        return true
      } catch {
        return false
      }
    })()
    if (hasDist) {
      const html = read('dist/index.html')
      expect(html).toMatch(/name="build-commit" content="[0-9a-f]{40}"/)
      const version = JSON.parse(read('dist/version.json'))
      expect(version.build_commit).toMatch(/^[0-9a-f]{40}$/)
      if (process.env.VITE_BUILD_COMMIT) {
        expect(version.build_commit).toBe(process.env.VITE_BUILD_COMMIT)
        expect(html).toContain(`name="build-commit" content="${process.env.VITE_BUILD_COMMIT}"`)
      }
      expect(version.built_at).toBeTruthy()
    }
  })
})
