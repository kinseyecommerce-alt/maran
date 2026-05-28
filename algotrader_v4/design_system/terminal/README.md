# NIRMA // TERMINAL — Dark Pro-Terminal UI Kit

A **bold dark-theme redesign** of AlgoTrader Pro — a neon-on-black,
Bloomberg/HFT-grade trading cockpit for NSE/BSE equities (₹). Same product DNA
as the light dashboard in `ui_kits/dashboard/`, dramatically different skin:
ultra-dense, glowing, monospace-forward, built for traders who live in the
screen all day.

## Run
Open `index.html` (best at ≥1280px wide — it's a desktop terminal). Pure
client-side: React 18 + Babel, Space Grotesk + JetBrains Mono (Google Fonts),
inline icons, no build step.

## Aesthetic direction
Ships with **light (default) and dark** themes — toggle via the sun/moon button
in the top bar (persists in localStorage). Light is a crisp premium-fintech
look (white panels, cool-gray canvas, legible green/red, soft shadows); dark is
the neon-on-black HFT cockpit.
- **Canvas:** light `#e8ecf3` / dark near-black `#05070b`, both with a faint blueprint grid + radial glows.
- **Semantics:** green (buy/up) + red (sell/down) — saturated neon w/ glow in dark, deeper & shadow-based in light; electric/royal blue accent; amber warnings.
- **Type:** Space Grotesk (techy display/UI) + JetBrains Mono for every number (`tabular-nums`, tight tracking). Heavy UPPERCASE micro-labels with wide letter-spacing.
- **Motion:** pulsing live dots, price-flash on tick, a continuously scrolling time & sales tape, glow/lift on primary actions.

## Signature elements
- **Depth-of-market (L2) ladder** in the right rail — bid/ask levels with size heat-bars; click a price to arm a LIMIT order.
- **Scrolling time & sales tape** across the bottom.
- **Glowing day-P&L** in the top bar that tracks your live positions.

## What's interactive
- Live ticking watchlist (heat bar + sparkline), click to load symbol.
- Candlestick chart with EMA9/EMA21 + volume + glowing last-price tag.
- Order ticket → BUY/SELL, qty, MARKET/LIMIT/SL-M, MIS/CNC/NRML → fills create toasts + live positions.
- Click any depth level to pull its price into a LIMIT order.
- ARM/HALT engine, pause/resume strategy agents, blotter tabs (Positions/Orders/Agents/SEBI).

## Files
| File | Role |
|---|---|
| `index.html` | Full dark-terminal stylesheet + App shell (state, ticking, tape, depth, toasts). |
| `term.jsx` | Inline icons, formatters, and the mock data engine (ticks, candles, EMA, depth ladder, tape). |
| `TopBar.jsx` | Brand, live index strip (NIFTY/SENSEX/BANKNIFTY/VIX), clock, P&L, ARM/HALT. |
| `WatchPanel.jsx` | Searchable watchlist with heat bars + sparklines. |
| `ChartPanel.jsx` | Symbol header, stat strip, candlestick/volume/EMA SVG chart. |
| `OrderRail.jsx` | Order ticket + depth-of-market ladder. |
| `Blotter.jsx` | Time & sales tape + Positions/Orders/Agents/SEBI tabs. |

## Notes
- Cosmetic recreation — real visuals, simulated logic. No live data/orders.
- This is a *new aesthetic direction* layered on the existing AlgoTrader Pro
  product, not a replacement for `colors_and_type.css` (which documents the
  shipped light theme). If this dark direction is adopted, promote these tokens
  into the root design system.
