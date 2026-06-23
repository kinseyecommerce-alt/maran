---
name: WS Signal Routing
description: How WebSocket signal/regime_change events flow into the activity log.
---

In websocket.ts onmessage handler:
- `signal` event → calls useStore.getState().prependActivityEntry(...) with cat:'SIG', type:'buy'|'sell'|'analyze'
- `regime_change` event → prependActivityEntry with cat:'WARN', type:'alert'
- `order_placed` → refreshes positions + orders via api calls (already existed)

prependActivityEntry keeps last 100 entries: [newEntry, ...existing].slice(0,100)

**Why:** Activity stream in App.tsx reads from store.agentActivity, so WS events must push to the same slice.

**How to apply:** Any new WS event type that should appear in the activity stream must call prependActivityEntry in websocket.ts.
