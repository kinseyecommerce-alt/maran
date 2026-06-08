"""
test_full_pipeline.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Full-pipeline integration test — ALL 5 agents, market-data ingestion → Kite order
placement → exit.

Drives the REAL async runtime (not isolated function calls):

    tick_engine._process_tick()            ← the exact entry point TrueData's
        → indicators + MarketSnapshot         _on_tick() feeds into
        → per-agent asyncio.Queue
    base_agent._run_loop()
        → agent.evaluate_tick()  (signal)
        → claude_trade_gate.assess()  (gate stage — stubbed deterministic)
        → _try_enter()
            → kite_client.place_order(MARKET entry)   tag "Agent-{name}"
            → kite_client.place_order(SL-M)           tag "Agent-{name}-SL"
            → trailing_sl_engine.register(...)
    trailing_sl_engine.on_tick()  (forced SL)
        → kite_client.place_order(MARKET exit)        tag "TSL-HIT-{name}"

Sandbox note: the real TrueData feed (SSL/host blocked) and real Kite LIVE
orders (needs live token + whitelisted IP) cannot run here, so this exercises
the identical code path in PAPER mode. TrueData's _on_tick() ultimately calls
the same _process_tick() that Stage A drives, so proving that path proves the
TrueData consumption point. Use the LIVE runbook (--live, or deploy docs) on the
VPS to verify the real TrueData→Kite path with a 1-share test order.

Run:
    python test_full_pipeline.py            # PAPER full-pipeline asserts
    python test_full_pipeline.py --live     # non-destructive LIVE checks (VPS only)
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

# ── Force PAPER before importing config-bound singletons ──────────────────────
os.environ.setdefault("TRADING_MODE", "PAPER")

from config import settings
from tick_engine import (
    tick_engine, MarketSnapshot, Tick, LiveIndicators, Candle, TickBuffer,
)
from kite_client import kite_client
from risk_manager import risk_manager
from trailing_sl_engine import trailing_sl_engine
import claude_trade_gate
from claude_trade_gate import GateDecision
from agents.strategy_agents import (
    IntradayAgent, OptionsAgent, SwingAgent, ScalpingAgent, FuturesAgent,
)

# ── Tiny test harness (mirrors test_sim_orders_flow.py) ───────────────────────
_results: list[tuple[str, bool, str]] = []


def ok(name: str):
    _results.append((name, True, ""))
    print(f"  ✅  {name}")


def fail(name: str, err: str):
    _results.append((name, False, err))
    print(f"  ❌  {name}: {err}")


def check(name: str, cond: bool, err: str = ""):
    if cond:
        ok(name)
    else:
        fail(name, err or "assertion failed")


def section(title: str):
    print(f"\n{'═'*62}")
    print(f"  {title}")
    print(f"{'═'*62}")


def summary() -> int:
    passed = sum(1 for _, o, _ in _results if o)
    failed = sum(1 for _, o, _ in _results if not o)
    print(f"\n{'═'*62}")
    print(f"  RESULTS: {len(_results)} checks — ✅ {passed} passed  ❌ {failed} failed")
    print(f"{'═'*62}")
    if failed:
        print("\nFailed:")
        for n, o, e in _results:
            if not o:
                print(f"  ❌ {n}: {e}")
    return failed


# ── Snapshot builder (same shape as test_pipeline._make_snap) ─────────────────
MKT = datetime(2026, 5, 29, 10, 30, 0)   # Friday, mid-session IST


def mk(symbol, ltp, *, rsi=55.0, trend="UP", vwap=None, macd_hist=1.5,
       volume_ratio=1.8, n_candles=30, ema9=None, ema21=None, ema50=None,
       ema200=None, spread=0.6, bar_seconds=60):
    vwap   = vwap   if vwap   is not None else ltp - 10
    ema9   = ema9   if ema9   is not None else ltp + 10
    ema21  = ema21  if ema21  is not None else ltp - 5
    ema50  = ema50  if ema50  is not None else ltp - 20
    ema200 = ema200 if ema200 is not None else ltp - 100
    half = spread / 2
    tick = Tick(symbol=symbol, ltp=ltp, bid=ltp - half, ask=ltp + half,
                volume=500000, change=0.0, change_pct=0.5,
                high=ltp + 10, low=ltp - 10, open=ltp - 5, timestamp=MKT)
    ind = LiveIndicators(
        symbol=symbol, ltp=ltp, bid=ltp - half, ask=ltp + half, spread=spread,
        ema9=ema9, ema21=ema21, ema50=ema50, ema200=ema200,
        vwap=vwap, rsi_14=rsi, rsi_7=rsi - 2,
        macd=2.5, macd_signal=1.5, macd_hist=macd_hist,
        bb_upper=ltp + 50, bb_lower=ltp - 50, bb_mid=ltp,
        atr_14=15.0, volume_ratio=volume_ratio, obv=1e6,
        day_high=ltp + 50, day_low=ltp - 50, day_open=ltp - 20, change_pct=0.5,
        trend=trend, momentum="UP", volatility="NORMAL", computed_at=MKT,
    )
    candles = [Candle(ltp, ltp + 5, ltp - 5, ltp, 500000, MKT - timedelta(minutes=i))
               for i in range(n_candles)]
    return MarketSnapshot(symbol=symbol, tick=tick, indicators=ind,
                          candles_1min=candles, candles_5min=candles[:12],
                          bar_seconds=bar_seconds)


# Per-agent recipes (verified to drive evaluate_tick → BUY). Each is a
# (prime_snap, trigger_snap) pair — patterns fire on a state transition.
def recipes():
    return {
        "intraday": (IntradayAgent(), "RELIANCE", [
            mk("RELIANCE", 2800, rsi=55, vwap=2790, macd_hist=1.5,
               volume_ratio=1.8, ema9=2810, ema21=2795, n_candles=30),
        ]),
        "scalping": (ScalpingAgent(), "TCS", [
            mk("TCS", 3490, rsi=54, vwap=3492, macd_hist=0.8, volume_ratio=1.8,
               ema9=3500, ema21=3495, ema50=3480, spread=0.6, n_candles=30),
            mk("TCS", 3512, rsi=60, vwap=3496, macd_hist=1.6, volume_ratio=2.4,
               ema9=3500, ema21=3496, ema50=3482, spread=0.6, n_candles=30),
        ]),
        "swing": (SwingAgent(), "HDFCBANK", [
            mk("HDFCBANK", 1700, rsi=50, vwap=1690, macd_hist=1.0,
               volume_ratio=1.5, ema9=1705, ema21=1700, ema50=1690,
               ema200=1600, n_candles=210),
        ]),
        "options": (OptionsAgent(), "INFY", [
            mk("INFY", 1620, rsi=50, vwap=1612, macd_hist=0.5, volume_ratio=1.6,
               ema9=1615, ema21=1618, ema50=1610, ema200=1500, n_candles=30),
            mk("INFY", 1626, rsi=58, vwap=1612, macd_hist=1.4, volume_ratio=1.9,
               ema9=1624, ema21=1618, ema50=1612, ema200=1500, n_candles=30),
        ]),
        "futures": (FuturesAgent(), "SBIN", [
            mk("SBIN", 790, rsi=55, vwap=785, macd_hist=0.3, volume_ratio=1.6,
               ema9=788, ema21=790, ema50=786, n_candles=30),
            mk("SBIN", 795, rsi=60, vwap=785, macd_hist=1.5, volume_ratio=2.0,
               ema9=798, ema21=792, ema50=787, n_candles=30),
        ]),
    }


def _orders_with_tag(symbol: str, tag: str, order_type: str | None = None):
    # OptionsAgent places orders for contract symbols (e.g. INFY2606041650CE),
    # so match by prefix so that _orders_with_tag("INFY", ...) also finds those.
    out = []
    for o in kite_client._paper_orders.values():
        ts = o.get("tradingsymbol", "")
        if ts != symbol and not ts.startswith(symbol):
            continue
        if o.get("tag") != tag:
            continue
        if order_type is not None and o.get("order_type") != order_type:
            continue
        out.append(o)
    return out


# ── Stage A — Ingestion (the TrueData entry point) ────────────────────────────
async def stage_ingestion():
    section("STAGE A — Market-data ingestion → indicators → broadcast")
    sym = "RELIANCE"
    # Create buffers directly (avoids paper_sim network seed in subscribe()).
    tick_engine._bufs_1min[sym] = TickBuffer(60, maxlen=400)
    tick_engine._bufs_5min[sym] = TickBuffer(300, maxlen=200)
    q = tick_engine.add_subscriber("test_ingest")

    base = 2800.0
    last_snap = None
    # Feed 40 ticks spaced 1 min apart so the buffer builds ~40 candles → real indicators.
    for i in range(40):
        px = base + i * 2.0            # gentle uptrend
        ts = MKT - timedelta(minutes=40 - i)
        tick = Tick(symbol=sym, ltp=px, bid=px - 0.3, ask=px + 0.3,
                    volume=500000 + i * 1000, change=0.0, change_pct=0.0,
                    high=px + 3, low=px - 3, open=px - 1, timestamp=ts)
        await tick_engine._process_tick(sym, tick, source="TEST")

    # Drain the queue; keep the most recent snapshot.
    while not q.empty():
        last_snap = q.get_nowait()

    check("ingestion: MarketSnapshot broadcast to subscriber", last_snap is not None,
          "no snapshot received")
    check("ingestion: snapshot symbol matches",
          last_snap is not None and last_snap.symbol == sym)
    check("ingestion: indicators computed (ema9 > 0)",
          last_snap is not None and last_snap.indicators.ema9 > 0,
          f"ema9={getattr(last_snap.indicators,'ema9',None) if last_snap else 'n/a'}")
    check("ingestion: candle buffer populated (>=20 candles)",
          last_snap is not None and len(last_snap.candles_1min) >= 20,
          f"candles={len(last_snap.candles_1min) if last_snap else 0}")
    tick_engine.remove_subscriber("test_ingest")


# ── Stages B & C — per-agent entry (evaluate→gate→place) + forced exit ────────
async def stage_agents():
    section("STAGE B/C — All 5 agents: signal → gate → entry+SL → exit")

    # Deterministic gate: exercise the gate STAGE without network/Claude flakiness.
    def _stub_assess(snap, action, signal, name):
        async def _coro():
            return GateDecision(confidence=80, enter=True, reason="test-stub")
        return _coro()

    for name, (agent, symbol, snaps) in recipes().items():
        agent._approved.add(symbol)
        q: asyncio.Queue = asyncio.Queue()
        agent.start(q)
        try:
            with patch("agents.strategy_agents.now_ist", return_value=MKT), \
                 patch.object(claude_trade_gate, "assess", _stub_assess):
                for s in snaps:
                    await q.put(s)
                # Let the async _run_loop drain the queue and place orders.
                entry = None
                for _ in range(60):                 # up to ~3s
                    await asyncio.sleep(0.05)
                    # Accept either LIMIT (use_limit_orders=True) or MARKET entry
                    hits = _orders_with_tag(symbol, f"Agent-{name}", "LIMIT")
                    if not hits:
                        hits = _orders_with_tag(symbol, f"Agent-{name}", "MARKET")
                    if hits:
                        entry = hits[0]
                        break
                # Give _try_enter() time to complete post-gather work (TSL register)
                await asyncio.sleep(0.15)
        finally:
            agent.stop()

        # ── Stage B asserts: entry + SL placed, TSL registered ──
        check(f"{name}: MARKET entry order placed (tag Agent-{name})",
              entry is not None,
              f"errors={list(agent.state.errors)[-1:] if agent.state.errors else 'none'}")
        if entry is None:
            continue
        oid = entry["order_id"]
        # OptionsAgent places orders for CE/PE contracts; get the actual tradingsymbol.
        actual_sym = entry["tradingsymbol"]
        sl_orders = _orders_with_tag(symbol, f"Agent-{name}-SL", "SL-M")
        check(f"{name}: SL-M protective order placed (tag Agent-{name}-SL)",
              len(sl_orders) >= 1)
        check(f"{name}: position registered with trailing-SL engine",
              oid in trailing_sl_engine._positions,
              f"order_id {oid} not in TSL positions")

        # ── Stage C: force SL hit → MARKET exit + deregister ──
        pos = trailing_sl_engine._positions.get(oid)
        if pos is None:
            continue
        # Use 95% of current_sl — guaranteed to cross the SL for any agent/SL-pct
        # (entry*0.90 fails for options whose initial_sl_pct=25% since 0.90 > 0.75).
        exit_px = round(pos.current_sl * 0.95, 2)
        # TSL is registered with pos.symbol (snap.symbol = underlying, e.g. "INFY"),
        # even for options which place orders for contract symbols.
        await trailing_sl_engine.on_tick(pos.symbol, exit_px, 15.0)
        await asyncio.sleep(0.05)

        exit_orders = _orders_with_tag(symbol, f"TSL-HIT-{name}", "MARKET")
        check(f"{name}: MARKET exit placed on SL hit (tag TSL-HIT-{name})",
              len(exit_orders) >= 1)
        check(f"{name}: position deregistered from trailing-SL engine after exit",
              oid not in trailing_sl_engine._positions)


# ── PAPER pipeline driver ─────────────────────────────────────────────────────
async def run_paper():
    print("\n" + "═" * 62)
    print("  FULL-PIPELINE TEST  —  AlgoTrader Pro  (PAPER)")
    print("  TrueData ingestion → 5 agents → Kite order placement → exit")
    print("═" * 62)

    check("mode is PAPER (no real orders)", settings.trading_mode == "PAPER",
          f"trading_mode={settings.trading_mode}")

    # Save/relax settings so risk limits don't mask the wiring (the checks still RUN).
    saved = {
        "mtf":       settings.use_multi_timeframe,
        "gate":      settings.use_claude_trade_gate,
        "maxpos":    settings.max_open_positions,
        "maxsize":   settings.max_position_size,
        "kelly":     settings.use_kelly_sizing,
        "maxloss":   settings.max_daily_loss,
    }
    settings.use_multi_timeframe = False          # MTF needs aligned 5m set
    settings.use_claude_trade_gate = True         # exercise gate stage (stubbed)
    settings.use_kelly_sizing = False             # deterministic qty
    settings.max_open_positions = 50
    settings.max_position_size = 10_000_000.0
    # The forced -10% exits accumulate losses across agents; raise limit so the
    # daily-loss guard doesn't block later agents (the guard logic still executes).
    settings.max_daily_loss = 10_000_000.0

    # Clean broker + risk state for a deterministic run.
    kite_client._paper_orders.clear()
    kite_client._paper_positions.clear()
    risk_manager.open_position_count = 0
    risk_manager.daily_realised_pnl = 0.0
    risk_manager.is_trading_halted = False

    try:
        await stage_ingestion()
        await stage_agents()
    finally:
        settings.use_multi_timeframe   = saved["mtf"]
        settings.use_claude_trade_gate = saved["gate"]
        settings.use_kelly_sizing      = saved["kelly"]
        settings.max_open_positions    = saved["maxpos"]
        settings.max_position_size     = saved["maxsize"]
        settings.max_daily_loss        = saved["maxloss"]

    return summary()


# ── LIVE non-destructive checks (VPS only) ────────────────────────────────────
def run_live():
    """Non-destructive LIVE verification — opt-in, run on the VPS with creds."""
    import httpx
    base = os.environ.get("E2E_BASE", "http://127.0.0.1:8000")
    api_key = os.environ.get("API_KEY", settings.api_key or "")
    section("LIVE pipeline checks (non-destructive)")

    try:
        v = httpx.get(f"{base}/config/validate", timeout=10).json()
        check("LIVE: /config/validate ready_to_trade", bool(v.get("ready_to_trade")),
              f"validate={v}")
        check("LIVE: Kite initialised", bool(v.get("kite_initialised")), f"validate={v}")
    except Exception as exc:
        fail("LIVE: /config/validate reachable", str(exc)[:120])

    try:
        r = httpx.post(f"{base}/bot/test-order",
                       headers={"X-API-Key": api_key},
                       json={"symbol": "SBIN", "qty": 1}, timeout=20).json()
        check("LIVE: /bot/test-order placed (1-share connectivity)",
              r.get("status") in ("ok", "success") or "order_id" in r, f"resp={r}")
    except Exception as exc:
        fail("LIVE: /bot/test-order reachable", str(exc)[:120])

    return summary()


if __name__ == "__main__":
    if "--live" in sys.argv:
        sys.exit(1 if run_live() else 0)
    sys.exit(1 if asyncio.run(run_paper()) else 0)
