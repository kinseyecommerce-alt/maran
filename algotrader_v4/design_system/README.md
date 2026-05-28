# AlgoTrader Pro — Design System

> **Product:** AlgoTrader Pro (a.k.a. *Nirma Trade v4*) — a production-grade
> algorithmic trading dashboard for the Indian equity markets (NSE/BSE).
> **Surface:** A single-page web application (React 18 + TypeScript + Vite +
> TailwindCSS, **light theme**) that traders use to watch live ticks, place
> orders, supervise autonomous strategy agents, monitor risk, and enforce SEBI
> compliance.

This design system captures the visual language, content voice, and component
vocabulary of that dashboard so new screens, marketing pages, slides, and
prototypes can be built on-brand.

---

## What the product does

AlgoTrader Pro is a real-time, tick-driven trading cockpit. The backend
(FastAPI + Zerodha Kite) streams live market data; the frontend renders it as a
dense, fast, numbers-first dashboard. Core jobs the UI supports:

- **Watch** — a live watchlist of NSE symbols with LTP, % change, sparklines, trend & RSI.
- **Chart** — a candlestick chart (TradingView Lightweight Charts) with EMA overlays.
- **Trade** — a BUY/SELL order panel (qty, order type, product, price) with keyboard shortcuts.
- **Supervise** — 4 autonomous strategy agents (Intraday, Scalping, Swing, F&O) you can pause/resume.
- **Control risk** — daily P&L, loss-limit gauges, open-position caps, and a hard trading halt.
- **Stay compliant** — a SEBI panel with an emergency **kill switch**, audit log, and IP whitelist.

A **PAPER / LIVE** mode badge is always visible — PAPER simulates orders, LIVE
places real money trades through Zerodha Kite.

### Audience & tone
The user is a **retail/semi-pro algo trader** in India. The UI assumes
fluency in trading jargon (LTP, VWAP, MIS/CNC/NRML, SL-M, square-off, bracket
orders). It is utilitarian, dense, and fast — closer to a Bloomberg terminal
than a consumer app. Currency is **₹ (INR)**, times are **IST**.

---

## Sources

This system was reverse-engineered from the product's own frontend code. Explore
these to build higher-fidelity work:

- **GitHub:** [`kinseyecommerce-alt/maran`](https://github.com/kinseyecommerce-alt/maran) — the AlgoTrader Pro monorepo.
  - Frontend lives at [`algotrader_v4/frontend/`](https://github.com/kinseyecommerce-alt/maran/tree/main/algotrader_v4/frontend) (React + Vite + Tailwind SPA).
  - `algotrader_v4/frontend/tailwind.config.js`, `src/index.css` — color & font tokens.
  - `src/components/ui/index.tsx` — the shared component primitives (Badge, Btn, Card, Input, Select, Pnl, Ltp, StatusDot, Modal).
  - `src/components/` — Header, Watchlist, MainChart, OrderPanel, and 6 bottom-tab panels.
  - `README.md` / `memory.md` at repo root — architecture & feature overview.

> The reader is encouraged to browse the repo above to recreate screens at
> higher fidelity than this kit — it is the source of truth for layout, copy,
> and interaction details.

---

## CONTENT FUNDAMENTALS

How copy is written across AlgoTrader Pro:

- **Voice — terse, operational, imperative.** UI copy is built from short verb
  phrases and nouns, not sentences. Buttons say **"Start Bot"**, **"Square Off
  All Positions"**, **"Resume Trading"**, **"Confirm Kill Switch"**. Labels are
  bare nouns: *Quantity*, *Order Type*, *Product*, *Daily P&L*.
- **Domain jargon is used freely and unexpanded.** *LTP, VWAP, RSI, EMA9/21,
  MIS / CNC / NRML, SL / SL-M, MARKET / LIMIT, bracket, square-off, kill switch,
  PAPER / LIVE.* The user is assumed expert; nothing is explained inline.
- **Casing.** Buttons & section titles use **Title Case** ("Order Panel",
  "Emergency Controls"). Field labels are Title Case too. Acronyms & states are
  **ALL CAPS** (BUY, SELL, MARKET, PAPER, LIVE, OPEN, CLOSED, HALTED, KILLED,
  ALLOWED). Tiny eyebrow labels are `uppercase` with letter-spacing
  ("WATCHLIST", "KITE (ZERODHA)").
- **Person.** Mostly **person-less / system voice** — "No open positions",
  "Trading HALTED", "Bot stopped". Occasionally **second-person** in
  confirmations: *"Square off ALL positions at market price?"*, *"Are you
  sure?"*. Never first-person.
- **Numbers are first-class.** Money always carries **₹** and uses Indian
  digit grouping (`₹1,23,456.00`). Percentages always show an explicit sign
  (`+1.24%`, `-0.80%`). Everything numeric is monospace + tabular.
- **Toasts** are ultra-short status confirmations: *"Bot stopped"*,
  *"Credentials updated"*, *"BUY 5 RELIANCE → 240517000123456"*, *"Squared off 3
  positions"*. Errors are equally terse: *"Invalid quantity"*, *"Order failed"*.
- **Emphasis via CAPS, not bold-words.** Critical words are capitalized for
  urgency: "This will HALT ALL trading immediately."
- **Emoji are used deliberately as functional iconography** (see Iconography) —
  e.g. tab glyphs (📋 📝 🎯 ⚠️ 🤖 🛡️), agent type markers (⚡ 📊 🌊 🎯), and a
  stop sign 🛑 / no-entry ⛔ for halts. They are status signifiers, not
  decoration, and sit alongside Lucide line icons.

**Example strings (verbatim from product):**
> `⛔ Trading HALTED — daily loss limit reached. Go to SEBI tab to resume.`
> `Bot started — 12 symbols` · `Min 8 characters` · `Leave blank to keep current`
> `In-memory only — restart reverts to environment variables.`
> `Start the bot to subscribe symbols` · `No symbol selected`

---

## VISUAL FOUNDATIONS

**Overall vibe.** A clean, dense, **light-theme financial cockpit**. White
cards float on a slate-50 canvas, separated by hairline slate borders. Color is
used sparingly and *meaningfully* — indigo for brand/navigation, green/red
strictly for trade direction and P&L. The feel is precise, calm, and
information-dense; no marketing flourish.

- **Color.**
  - **Brand = Indigo-600 `#4F46E5`** — logo tile, primary buttons, active tab
    underline + text, selected watchlist row (with `indigo-500` left border on a
    `indigo-50` wash), focus rings, progress fills.
  - **Green-600 `#16A34A` / Red-600 `#DC2626`** — reserved for *direction*: BUY
    vs SELL, gains vs losses, up vs down trend. Never used decoratively. Tinted
    backgrounds (`green-100` / `red-100`) carry badges; `green-50` / `red-50`
    carry status banners.
  - **Amber `#F59E0B`** — caution only: PAPER-mode badge, "approaching limit"
    gauges, warning inline blocks.
  - **Slate** is the entire neutral structure: `slate-50` canvas, `white` cards,
    `slate-100` inner dividers, `slate-200` borders, `slate-500/700/900` text.
- **Typography.** **Inter** for all UI text; **JetBrains Mono** for every number
  (prices, P&L, %, qty, clock, RSI, order IDs) with `tabular-nums` so digits
  don't jitter as ticks update. Sizes are small and tight: `text-xs` (12px) for
  labels/meta, `text-sm` (14px) for body & buttons, `text-lg/2xl` for the
  selected symbol price and big stats. Weights: 400 body, 500 medium labels,
  600 semibold, 700 bold titles/prices.
- **Spacing & density.** 4px base scale. Generous *inside* cards (`p-4`),
  tight *between* rows (`py-2`–`py-3`). The whole app is a fixed full-height
  flex layout — header `56px`, watchlist `208px`, order panel `256px`, bottom
  tabs `224px` — content panes scroll internally, the chrome never moves.
- **Backgrounds.** Flat fills only. **No gradients, no images, no textures, no
  patterns.** The canvas is solid `slate-50`; cards are solid `white`. Depth
  comes from borders + a faint shadow, not color washes.
- **Corner radii.** `rounded` 4px (badges/chips), `rounded-lg` 8px
  (buttons, inputs, selects), `rounded-xl` 12px (cards & panels — the dominant
  radius), `rounded-2xl` 16px (modals), `rounded-full` (dots, count badges,
  gauge tracks).
- **Cards.** `bg-white` + `rounded-xl` + `1px solid slate-200` + `shadow-sm`.
  That's the canonical card. Stat cards add `text-center` and a big mono number.
  Agent cards swap the border to `green-200` when running.
- **Borders & dividers.** Hairline `1px`. Structural borders `slate-200`; inner
  dividers and table rows `slate-100`/`slate-50`. The active watchlist row adds a
  `2px` indigo **left** accent border.
- **Shadows.** Restrained, neutral (slate-tinted, never colored): `shadow-sm`
  on cards, `shadow-lg` on toasts, `shadow-2xl` on the modal. No glow, no
  colored shadows.
- **Transparency & blur.** Used only for the modal scrim — `bg-black/40` (no
  blur). Slight opacity (`/50`, `opacity-70`) on the active-tab wash and inactive
  toggle hints. No frosted glass.
- **Animation.** Minimal and functional. `transition-colors` on every
  interactive element (buttons, tabs, rows). The market-open dot **pulses**
  (`animate-pulse`). Toasts **slide in from the right** (200ms). Numbers flash
  green/red on tick change. No bounces, no parallax, no decorative motion.
- **Hover states.** Buttons darken one step (indigo-600→700, green-600→700,
  red-600→700). Ghost/row hovers go to a light slate wash (`hover:bg-slate-50` /
  `hover:bg-slate-100`). Icon buttons gain a `slate-100` rounded background.
- **Press / focus.** Active toggles get `shadow-sm` + solid fill + white text.
  Focus shows a **2px indigo ring with a 1px white offset** (`focus:ring-2
  focus:ring-indigo-500 focus:ring-offset-1`). Disabled = `opacity-50` +
  `cursor-not-allowed`.
- **Imagery.** There is **none** — no photography, illustration, or stock art.
  The only "imagery" is data viz: candlestick charts, Recharts sparklines, and
  half-donut risk gauges, all drawn in the brand/semantic palette
  (indigo line, green/red series, slate-100 gauge track).
- **Tables.** `slate-50` header row, `text-xs` `slate-500` column heads,
  `slate-50`/`slate-100` row dividers, `hover:bg-slate-50`. Numeric cells are
  mono; symbol cells bold with a muted meta line beneath.

---

## ICONOGRAPHY

- **Primary icon set: [Lucide](https://lucide.dev)** (`lucide-react` in the
  app). Clean 2px-stroke, rounded line icons — no fills. Icons seen in product:
  `Activity` (logo), `Wifi`/`WifiOff`, `Settings`, `Zap`/`ZapOff` (bot
  start/stop), `Eye`/`EyeOff` (reveal secrets), `ShoppingCart` (order panel),
  `TrendingUp`/`TrendingDown`/`Minus` (direction), `AlertTriangle`, `Shield`
  (SEBI). Typical sizes: `w-3 h-3` (12px) to `w-6 h-6` (24px); inherit text
  color via `currentColor`. **Lucide is CDN-available** — load
  `https://unpkg.com/lucide@latest` (or `lucide-react`) and reuse the exact same
  names. No substitution needed.
- **Emoji are a deliberate second icon channel** for status & categories:
  - Bottom tabs: 📋 Positions · 📝 Orders · 🎯 Brackets · ⚠️ Risk · 🤖 Agents · 🛡️ SEBI
  - Agent types: ⚡ Intraday · 📊 F&O · 🌊 Swing · 🎯 Scalping
  - Alerts/halts: 🛑 (kill / halted banner) · ⛔ (trading halted bar) · ⚠ (approaching limit) · ▶ (resume)
  - Empty states: 📊 ("No symbols")
  Use them as *functional glyphs*, sparingly, matching these established
  meanings — not as decoration.
- **Unicode glyphs as micro-icons:** ✓ ("Set ✓"), `·` middot as a separator in
  meta lines, `→` in toast confirmations, `₹` as the currency mark.
- **The logo** is a `32×32` `rounded-lg` **indigo-600 tile** containing a white
  `Activity` line icon, set beside a two-line wordmark
  ("AlgoTrader Pro" / "Nirma Trade v4"). See `assets/logo-full.svg` &
  `assets/logo-mark.svg` (faithful recreations of the code-defined logo — the
  product has no raster logo file).

---

## INDEX — what's in this design system

| File / folder | What it is |
|---|---|
| `README.md` | This document — product context, content & visual foundations, iconography, index. |
| `colors_and_type.css` | CSS custom properties for the full color + type system (base tokens + semantic classes). Import this into any HTML you build. |
| `assets/` | Brand logo (`logo-full.svg`, `logo-mark.svg`). |
| `preview/` | Small HTML specimen cards that populate the Design System tab (colors, type, components, spacing). |
| `ui_kits/dashboard/` | High-fidelity, interactive recreation of the AlgoTrader Pro dashboard — `index.html` + JSX components. The primary reference for building screens. |
| `SKILL.md` | Agent Skill manifest so this system can be used directly in Claude Code. |

### Tech for recreations
React 18 + Babel (inline JSX), Tailwind via CDN, Lucide via CDN. Numbers in
JetBrains Mono with `tabular-nums`; ₹ + Indian grouping; IST clock.
