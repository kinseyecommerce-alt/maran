import { defineConfig } from '@playwright/test'

// Allow pointing at a pre-installed Chromium when the Playwright CDN is
// unreachable (sandboxed CI). Set PW_CHROME_PATH to the chrome binary.
const executablePath = process.env.PW_CHROME_PATH || undefined

// E2E config: builds nothing — runs the Vite dev server and drives it with a
// fully mocked backend (see e2e/*.spec.ts route interception). No Python
// backend or real credentials are required.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: 'http://localhost:4173',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        browserName: 'chromium',
        viewport: { width: 1280, height: 720 },
        launchOptions: { executablePath },
      },
    },
  ],
  // E2E runs against the production build (vite preview) — a single bundled
  // asset that loads instantly, unlike dev mode's per-module HTTP waterfall.
  webServer: {
    command: 'npm run build && npm run preview',
    url: 'http://localhost:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
