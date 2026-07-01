---
name: Kite as primary OHLCV source
description: Architecture for OHLCV data — Kite first, yfinance last resort; which files changed and what to keep in mind.
---

## Priority order in YFinanceClient.historical() (market_data.py)
1. Disk/memory CSV cache
2. Bhavcopy (daily bars only — survivorship-bias-free)
3. TrueData (when credentials configured)
4. **Kite Connect** via `_kite_historical()` — calls `kite_client.get_instrument_tokens()` then `kite_client.historical_data()`
5. yfinance (last resort — unreliable for NSE .NS symbols)

## Kite interval mapping (yfinance → Kite)
`1m→minute`, `5m→5minute`, `15m→15minute`, `30m→30minute`, `60m/1h→60minute`, `1d→day`

## PAPER mode behaviour
- `kite_client.historical_data()` returns `[]` in PAPER mode automatically
- `kite_client.get_instrument_tokens()` throws TokenException (no access token) — caught silently
- Both cause `_kite_historical()` to return empty DataFrame → falls through to yfinance

## Files that keep yfinance intentionally
- `market_data.py` — last-resort fallback in `historical()`
- `macro_signals.py` — global symbols only: USDINR=X, CL=F, ES=F, ^VIX (Kite has no equivalent)
- `pre_market_report.py` — global futures: ES=F, NQ=F, CL=F, GC=F, DX-Y.NYB (same reason)

## Files migrated away from direct yf.download()
- `correlation_guard.py` — per-symbol yf_client loop in `_download_prices()`
- `levels_engine.py` — `_refresh_symbol()` uses yf_client.historical()
- `market_regime.py` — `_collect_nifty`, `_collect_vix`, `_collect_sectors` all use yf_client; SECTOR_TICKERS updated from `^CNXIT` style to `NIFTYIT` NSE style
- `pre_market_report.py` — India indices now use NSE allIndices API; sector ETFs use yf_client

**Why:** yfinance routinely fails for NSE `.NS` symbols (404 / "possibly delisted"). Kite historical API is the authoritative source for Indian equity OHLCV when the access token is set.

**How to apply:** Never add new `import yfinance as yf` + `yf.download()` calls for Indian equity symbols. Always use `yf_client.historical(symbol, "NSE", interval, period)` which will automatically use Kite when connected.
