---
name: Live Price Data Pipeline
description: Tick engine live data source priority and Yahoo Finance removal
---

## Rule
Live price source priority (implemented in tick_engine.py `start_loop()`):
1. **TrueData WebSocket** — primary; activates when `truedata_username` + `truedata_password` are set
2. **Kite WebSocket** — automatic fallback when TrueData is absent or raises on start; activates when `kite_access_token` is set
3. **Kite REST polling** — last resort when both WS fail

Yahoo Finance (yfinance) is **completely removed** from the price pipeline. `market_data.py` `historical()` ends at Kite historical; no yfinance fallback.

**Why:** User explicitly requires TrueData → Kite only; no Yahoo Finance anywhere.

## How to apply
- `_live_data_enabled()` returns True in LIVE mode, or in PAPER mode if TrueData credentials present OR `paper_use_live_data=True` + Kite token.
- `subscribe()` now creates a `_backfill_bufs()` task for newly added symbols (post-startup watchlist additions). Previously only `start_loop()` triggered backfill.
- Without any credentials (pure PAPER/GBM): ScalpingAgent fires after ~10 min, IntradayAgent after ~21 min of GBM accumulation.

## Enabling TrueData in .env
```
TRUEDATA_USERNAME=your_user
TRUEDATA_PASSWORD=your_pass
USE_TRUEDATA_WEBSOCKET=true
USE_TRUEDATA_HISTORICAL=true
```
No `paper_use_live_data` needed — TrueData credentials alone activate the real feed.
