# AlgoTrader Pro — Dashboard UI Kit

A high-fidelity, interactive recreation of the **AlgoTrader Pro** trading
dashboard (the product's single React SPA surface). Built to match
`algotrader_v4/frontend` pixel-for-pixel in layout, color, type, and copy —
with mock data and simulated interactions instead of a live backend.

## Run
Open `index.html`. Everything is client-side (React 18 + Babel via CDN,
Tailwind via CDN, Inter + JetBrains Mono from Google Fonts, inline Lucide icons).

## What's interactive
- **Live ticking** — every watchlist symbol random-walks each second; sparklines, LTP, % and the chart's price line update.
- **Select a symbol** — click any watchlist row → chart + order panel switch to it.
- **Place an order** — set qty / type / product, hit BUY or SELL → a toast fires, the order lands in the **Orders** tab, and a position opens in **Positions** (with live P&L).
- **Square off** — closes all open positions (with a confirm step).
- **Start/Stop Bot** — toggles the header state + agent activity; **Agents** tab lets you pause/resume each strategy.
- **Bottom tabs** — Positions · Orders · Brackets · Risk (gauges) · Agents · SEBI.
- **Settings modal** — Connection / API Keys / App Login tabs.

## Files
| File | Role |
|---|---|
| `index.html` | App shell — wires state, toasts, settings modal, live ticking, mounts everything. |
| `ds.jsx` | Shared primitives (`Badge`, `Btn`, `Card`, `Input`, `Select`, `Pnl`, `Modal`), inline Lucide `Icon`, `inr()` formatters, and the mock market-data engine (`seedTicks`, `stepTicks`). |
| `Header.jsx` | Top bar — logo, PAPER badge, market status, IST clock, WS indicator, Start/Stop Bot, settings. |
| `Watchlist.jsx` | Left sidebar — ticker rows with SVG sparkline, trend chip, RSI. |
| `MainChart.jsx` | Candlestick chart (SVG) with EMA9/EMA21 overlays + live price line. |
| `OrderPanel.jsx` | Right panel — BUY/SELL toggle, qty/type/product form, square-off. |
| `BottomTabs.jsx` | The 6 bottom-tab panels (Positions, Orders, Brackets, Risk, Agents, SEBI). |

## Notes & fidelity
- Components are **cosmetic recreations** — real layout/styling, simplified logic. No real orders, WebSocket, or Kite/SEBI calls.
- The chart is a hand-rolled SVG candlestick (the product uses TradingView
  Lightweight Charts v4); it matches the visual treatment, not the library.
- All numbers use JetBrains Mono + `tabular-nums`, ₹ with Indian digit grouping.
- To extend: import a component's `window.<Name>` global and compose. Reuse
  `Btn`/`Badge`/`Card`/`Input`/`Select` from `ds.jsx` for any new screen.
