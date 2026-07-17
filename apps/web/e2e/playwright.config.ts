import * as fs from 'node:fs'
import * as path from 'node:path'
import { defineConfig, devices } from '@playwright/test'

/**
 * Browser e2e for the platform SPA, driven through the real backend.
 *
 * Pipeline (what `make e2e-web` runs; each step is explicit — the webServer
 * block below builds nothing by itself):
 *   1. `cd apps/web && npm run build`        → apps/web/dist (the backend
 *      serves the built SPA; there is no vite dev server in this suite).
 *   2. `cd apps/web/e2e && npm install`      → this package's own Playwright
 *      toolchain (kept separate from apps/web's node_modules on purpose).
 *   3. `npx playwright test`                 → the webServer block boots
 *      `python -m noeta.agent` on a dedicated port with a throwaway data
 *      directory (wiped on every start), runs the specs, then kills it.
 *
 * The backend runs fully offline: mock LLM provider (deterministic demo
 * chain, see noeta/agent/host/mock_llm.py), sandbox disabled, dev-login on.
 * Every test logs in as a fresh unique user, so tests are isolated by the
 * per-user personal space and can run in parallel against one backend.
 */

const E2E_DIR = __dirname
const REPO_ROOT = path.resolve(E2E_DIR, '..', '..', '..')
const WEB_DIST = path.resolve(E2E_DIR, '..', 'dist')
const DATA_DIR = path.join(E2E_DIR, '.tmp', 'data')

const PORT = 8123
const BASE_URL = `http://127.0.0.1:${PORT}`

// The backend serves the SPA from apps/web/dist; without a build the suite
// can only fail later and less clearly, so fail fast here.
if (!fs.existsSync(path.join(WEB_DIST, 'index.html'))) {
  throw new Error(
    `Built SPA not found at ${WEB_DIST}. ` +
      'Run `npm run build` in apps/web first (or use `make e2e-web`, which does).',
  )
}

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 4,
  reporter: 'list',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    // Wipe the throwaway data dir, then boot the backend. The SPA must
    // already be built (checked above); readiness = the backend answering
    // on "/" with the served index.html.
    command: `rm -rf "${DATA_DIR}" && uv run python -m noeta.agent`,
    cwd: REPO_ROOT,
    url: `${BASE_URL}/`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      HOST: '127.0.0.1',
      PORT: String(PORT),
      LLM_PROVIDER: 'mock',
      SANDBOX_ENABLED: 'false',
      DEV_LOGIN_ENABLED: 'true',
      DATA_DIR,
      SHARED_DATA_DIR: path.join(DATA_DIR, 'shared'),
    },
  },
})
