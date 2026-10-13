import { test, expect, type Page } from '@playwright/test'

// The app talks to the FastAPI backend at http://localhost:8000 (store default).
// These tests run with NO real backend — every REST call is intercepted and
// answered with deterministic fixtures, so the suite is fully self-contained.

const API = 'http://localhost:8000'

const bookFixture = {
  as_of: '2026-10-06T10:00:00+05:30',
  positions: [{
    tradingsymbol: 'RELIANCE', exchange: 'NSE', product: 'MIS', segment: 'NSE_EQ',
    strategy: 'intraday', quantity: 10, average_price: 2980.0, last_price: 3010.0,
    pnl: 300.0, simulated: false, multiplier: 1,
  }],
  orders: [],
  strategies: {},
  summary: {
    total: { pnl: 300, realised: 0, unrealised: 300, positions: 1, orders: 0, closed: 0 },
    by_segment: {
      NSE_EQ: { label: 'NSE Equity', pnl: 300, realised: 0, unrealised: 300, positions: 1, orders: 0, closed: 0, simulated: false },
    },
  },
}

const fixtures: Record<string, unknown> = {
  '/health': {
    status: 'ok', mode: 'PAPER', market_open: true, ticker_source: 'PAPER', version: '4.0.0',
  },
  '/bot/status': { master_running: false, agents: {}, engine: { state: 'stopped', label: 'stopped', strategies: {}, segments: [], agents: {} } },
  '/config/validate': {
    kite_api_key: false, kite_api_secret: false, anthropic_api_key: false,
    truedata_username: false, truedata_password: false, admin_username: 'admin',
  },
  '/portfolio/book': bookFixture,
  '/portfolio/positions': { net: bookFixture.positions },
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
  '/auth/me': { username: 'admin' },
}

async function mockBackend(page: Page) {
  await page.route(`${API}/**`, route => {
    route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
  for (const [path, body] of Object.entries(fixtures)) {
    await page.route(`${API}${path}`, route => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    })
  }
}

test.beforeEach(async ({ page }) => {
  await mockBackend(page)
  await page.addInitScript(() => {
    localStorage.setItem('api_base', 'http://localhost:8000')
    localStorage.setItem('api_key', '')
  })
})

test('app shell renders with PAPER mode and Start Bot control', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('ALGOPRO').first()).toBeVisible()
  await expect(page.getByText('PAPER').first()).toBeVisible()
  await expect(page.getByRole('button', { name: /Start Bot/i })).toBeVisible()
})

test('desk chrome: sticky header, index area, today pnl testid', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('header')).toBeVisible()
  await expect(page.getByTestId('today-pnl')).toBeVisible()
  // Sidebar nav present
  await expect(page.getByRole('button', { name: /Dashboard/i })).toBeVisible()
  await expect(page.getByRole('button', { name: /Positions/i })).toBeVisible()
})

test('positions tab shows the mocked open position', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /^Positions/i }).click()
  await expect(page.getByTestId('positions-tab')).toBeVisible()
  await expect(page.getByText('RELIANCE').first()).toBeVisible()
  await expect(page.getByText(/Open Position/i).first()).toBeVisible()
})

test('switching to Risk tab loads mocked risk data', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /^Risk$/i }).click()
  await expect(page.getByText('Daily P&L').first()).toBeVisible()
  await expect(page.getByText(/Open Positions/i).first()).toBeVisible()
  await expect(page.getByText('1 / 5')).toBeVisible()
})

test('switching to Agents tab lists strategy agents', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /^Agents$/i }).click()
  await expect(page.getByText('intraday').first()).toBeVisible()
  await expect(page.getByText('scalping').first()).toBeVisible()
})

test('settings modal opens and shows connection fields', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Settings/i }).click()
  await expect(page.getByText('Brokers').first()).toBeVisible()
})

test('mobile viewport renders the app without blank screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByText('ALGOPRO').first()).toBeVisible()
  await expect(page.getByRole('button', { name: /^Risk$/i })).toBeVisible()
  const bodyLen = (await page.locator('body').innerText()).length
  expect(bodyLen).toBeGreaterThan(50)
})

test('backend down does not crash the UI (graceful degradation)', async ({ page }) => {
  await page.unroute(`${API}/**`).catch(() => {})
  await page.route(`${API}/**`, route => route.abort())
  await page.goto('/')
  await expect(page.getByText('ALGOPRO').first()).toBeVisible()
  await expect(page.getByText('PAPER').first()).toBeVisible()
})
