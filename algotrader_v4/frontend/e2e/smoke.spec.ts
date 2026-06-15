import { test, expect, type Page } from '@playwright/test'

// The app talks to the FastAPI backend at http://localhost:8000 (store default).
// These tests run with NO real backend — every REST call is intercepted and
// answered with deterministic fixtures, so the suite is fully self-contained.

const API = 'http://localhost:8000'

const fixtures: Record<string, unknown> = {
  '/health': {
    status: 'ok', mode: 'PAPER', market_open: true, ticker_source: 'PAPER',
  },
  '/bot/status': { master_running: false, agents: {} },
  '/config/validate': {
    kite_api_key: false, kite_api_secret: false, anthropic_api_key: false,
    truedata_username: false, truedata_password: false, admin_username: 'admin',
  },
  '/portfolio/positions': {
    net: [{
      tradingsymbol: 'RELIANCE', exchange: 'NSE', product: 'MIS',
      quantity: 10, average_price: 2980.0, last_price: 3010.0, pnl: 300.0,
    }],
  },
  '/portfolio/orders': [],
  '/risk/status': {
    daily_pnl: -1200, max_daily_loss: 10000, open_positions: 1,
    max_open_positions: 5, trades_today: 3, is_halted: false,
  },
  '/agents': {
    intraday: { running: true }, scalping: { running: false },
    swing: { running: false }, fno: { running: false },
  },
  '/market/status': { open: true },
  // /market/live returns a flat { SYMBOL: {...} } map (tick_engine.all_latest()).
  '/market/live': {
    RELIANCE: {
      ltp: 3010.0, bid: 3009.5, ask: 3010.5, change_pct: 1.01,
      day_high: 3025.0, day_low: 2975.0, trend: 'UP', volatility: 'NORMAL',
      rsi: 58.0, vwap: 3005.0, ema9: 3008.0, ema21: 3000.0, macd_hist: 0.5,
      vol_ratio: 1.2, source: 'PAPER',
    },
  },
  '/brackets': [],
  '/regime/status': { regime: 'TRENDING_UP' },
  '/sebi/status': { kill_switch: false, audit_count: 0 },
}

async function mockBackend(page: Page) {
  // Catch-all first (lowest priority): any backend call returns {} so nothing
  // hits the network or hangs.
  await page.route(`${API}/**`, route => {
    route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
  // Specific fixtures registered after → take precedence over the catch-all.
  for (const [path, body] of Object.entries(fixtures)) {
    await page.route(`${API}${path}`, route => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    })
  }
}

test.beforeEach(async ({ page }) => {
  await mockBackend(page)
  // Pin the API base before the app boots so requests are deterministic.
  await page.addInitScript(() => {
    localStorage.setItem('api_base', 'http://localhost:8000')
    localStorage.setItem('api_key', '')
  })
})

test('app shell renders with PAPER mode and Start Bot control', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('AlgoTrader Pro')).toBeVisible()
  await expect(page.getByText('PAPER').first()).toBeVisible()
  await expect(page.getByRole('button', { name: /Start Bot/i })).toBeVisible()
})

test('positions tab shows the mocked open position', async ({ page }) => {
  await page.goto('/')
  // Positions is the default tab.
  await expect(page.getByText('RELIANCE').first()).toBeVisible()
  await expect(page.getByText(/Open Position/i).first()).toBeVisible()
})

test('switching to Risk tab loads mocked risk data', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Risk/i }).click()
  await expect(page.getByText('Daily P&L').first()).toBeVisible()
  await expect(page.getByText(/Open Positions/i).first()).toBeVisible()
  // 1 / 5 from the risk fixture.
  await expect(page.getByText('1 / 5')).toBeVisible()
})

test('switching to Agents tab lists strategy agents', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Agents/i }).click()
  await expect(page.getByText('intraday').first()).toBeVisible()
  await expect(page.getByText('scalping').first()).toBeVisible()
})

test('settings modal opens and shows connection fields', async ({ page }) => {
  await page.goto('/')
  // The gear button is the last icon-only button in the header.
  await page.locator('header button').last().click()
  await expect(page.getByText('API Base URL')).toBeVisible()
  await expect(page.getByText('X-API-Key')).toBeVisible()
})

test('mobile viewport renders the app without blank screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 }) // iPhone 12-ish
  await page.goto('/')
  await expect(page.getByText('AlgoTrader Pro')).toBeVisible()
  // Tab bar is still reachable on a narrow viewport (overflow-x-auto).
  await expect(page.getByRole('button', { name: /Risk/i })).toBeVisible()
  const bodyLen = (await page.locator('body').innerText()).length
  expect(bodyLen).toBeGreaterThan(50) // not a blank page
})

test('backend down does not crash the UI (graceful degradation)', async ({ page }) => {
  // Override every backend call with a hard failure — the shell must still render.
  await page.unroute(`${API}/**`).catch(() => {})
  await page.route(`${API}/**`, route => route.abort())
  await page.goto('/')
  await expect(page.getByText('AlgoTrader Pro')).toBeVisible()
  // Fallback mode badge still shows; no positions, no blank screen.
  await expect(page.getByText('PAPER').first()).toBeVisible()
  await expect(page.getByText(/No open positions/i)).toBeVisible()
})
