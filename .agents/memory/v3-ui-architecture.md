---
name: V3 Dark UI Architecture
description: App.tsx is the entire V3 dark command center — no separate overlay component. Key layout decisions.
---

The entire app is the V3 dark layout. App.tsx contains:
- Dark header (via Header/index.tsx — fully rewritten to dark theme)
- Mood line (emerald/rose based on P&L direction)
- P&L strip (daily_pnl from botStatus.performance or positions sum)
- Main body: left (agent cards + activity log) + right sidebar (watchlist + NIFTY chart)
- Collapsible OPERATIONS PANEL (h-9 collapsed / h-52 expanded) with positions/orders/brackets/risk/SEBI tabs
- Footer (engine stats)

**Why:** User selected AgentCommandCenterV3 as the complete app UI. All existing functionality preserved via collapsible bottom ops panel.

**How to apply:** Do NOT create a separate AgentCommandCenter overlay component. All command center code lives in App.tsx.

Agent data: AGENT_ORDER = ['intraday', 'fno', 'swing', 'scalping'], keyed same as store.agents Record.
Activity log: store.agentActivity[], prepended by WS signal/regime_change events via prependActivityEntry().
Watchlist: Object.keys(ticks).slice(0,8) from store.
NIFTY chart: sparklines[niftyKey].slice(-30), normalized to 10-90% height range.
