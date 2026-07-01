"""
test_all_agents_e2e.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pre-deployment end-to-end proof — ALL 8 strategy agents fire orders, every entry
is protected by an SL-M (bounded loss), and the PAPER→LIVE switch routes to the
real Kite API.

This complements test_full_pipeline.py (which drives the full async _run_loop for
5 agents). Here we cover all 8 agents — including mean_reversion, momentum and
pairs — through the production order path:

    agent.evaluate_tick(snap)            → BUY/SELL signal      (Stage 1)
        → agent._try_enter(snap, …)      → entry + SL-M order    (Stage 2)
            → kite_client.place_order()     PAPER: _paper_orders
            → trailing_sl_engine.register()
    every entry has a paired SL-M         → bounded loss          (Stage 3)
    settings.trading_mode = "LIVE"        → self.kite.place_order (Stage 4)

Run:
    python test_all_agents_e2e.py
"""
from __future__ import annotations

import asyncio
import os

# Force PAPER before importing config-bound singletons.
os.environ.setdefault("TRADING_MODE", "PAPER")

from unittest.mock import MagicMock, patch

from config import settings
from kite_client import kite_client
from risk_manager import risk_manager
from trailing_sl_engine import trailing_sl_engine
from order_guard import order_guard
import claude_trade_gate
from claude_trade_gate import GateDecision

from agents.strategy_agents import (
    IntradayAgent, ScalpingAgent, SwingAgent, OptionsAgent, FuturesAgent,
    MeanReversionAgent, MomentumAgent, PairsAgent,
)
from agents.base_agent import _otag

# Reuse the proven snapshot builder + harness from test_full_pipeline.
from test_full_pipeline import mk, ok, fail, check, section, summary, _orders_with_tag, MKT


# ── State isolation ───────────────────────────────────────────────────────────
def reset_state() -> None:
    """Clear broker + guard + risk so each agent's order placement is isolated."""
    kite_client._paper_orders.clear()
    kite_client._paper_positions.clear()
    order_guard._active.clear()
    order_guard._symbol_owner.clear()
    order_guard._symbol_cooldown.clear()
    order_guard._trade_count.clear()
    order_guard._cooldown_until.clear()
    trailing_sl_engine._positions.clear()
    risk_manager.open_position_count = 0
    risk_manager.daily_realised_pnl = 0.0
    risk_manager.is_trading_halted = False


# ── Per-agent signal recipes ──────────────────────────────────────────────────
# Each returns (agent, symbol, trigger_snap, action, signal).
def build_simple(agent, symbol, snaps):
    """Agents whose patterns fire on a snapshot (or prime→trigger transition)."""
    action, signal, trig = "HOLD", None, None
    with patch("agents.strategy_agents.now_ist", return_value=MKT):
        for s in snaps:
            trig = s
            action, signal = agent.evaluate_tick(s)
    return agent, symbol, trig, action, signal


def build_mean_reversion():
    agent = MeanReversionAgent()
    snap = mk("AXISBANK", 1150, rsi=25, volume_ratio=1.6, macd_hist=0.3, n_candles=30)
    # BB-lower bounce: ltp < bb_lower, rsi oversold, volume present
    snap.indicators.bb_lower = 1155.0
    snap.indicators.bb_upper = 1200.0
    snap.indicators.bb_mid = 1177.0
    snap.indicators.rsi_14 = 25.0
    snap.indicators.volume_ratio = 1.6
    with patch("agents.strategy_agents.now_ist", return_value=MKT):
        action, signal = agent.evaluate_tick(snap)
    return agent, "AXISBANK", snap, action, signal


def build_momentum():
    agent = MomentumAgent()
    # Volume-surge trend: vol≥2.0, ema9>ema21>ema50>0, macd_hist>0
    snap = mk("LT", 3600, rsi=58, volume_ratio=2.5, macd_hist=1.5,
              ema9=3620, ema21=3605, ema50=3580, ema200=3500, n_candles=30)
    with patch("agents.strategy_agents.now_ist", return_value=MKT):
        action, signal = agent.evaluate_tick(snap)
    return agent, "LT", snap, action, signal


def build_pairs():
    agent = PairsAgent()
    with patch("agents.strategy_agents.now_ist", return_value=MKT):
        # Prime INFY (leg b) price so the TCS/INFY ratio can be computed.
        agent.evaluate_tick(mk("INFY", 1600.0, n_candles=30))
        # Warm up a tight ratio cluster (≥20 samples → stable mean/std).
        for i in range(30):
            px = 3500.0 + ((i % 3) - 1)        # 3499..3501 micro-wiggle
            agent.evaluate_tick(mk("TCS", px, n_candles=30))
        # Divergent tick: TCS cheap → ratio drops far below mean → z ≤ −2 → BUY TCS.
        trig = mk("TCS", 3400.0, rsi=45, volume_ratio=1.6, macd_hist=0.4,
                  ema9=3405, ema21=3400, n_candles=30)
        action, signal = agent.evaluate_tick(trig)
    return agent, "TCS", trig, action, signal


def recipes():
    return [
        ("intraday", build_simple(IntradayAgent(), "RELIANCE", [
            mk("RELIANCE", 2800, rsi=55, vwap=2790, macd_hist=1.5,
               volume_ratio=1.8, ema9=2810, ema21=2795, n_candles=30),
        ])),
        ("scalping", build_simple(ScalpingAgent(), "TCS", [
            mk("TCS", 3490, rsi=54, vwap=3492, macd_hist=0.8, volume_ratio=1.8,
               ema9=3500, ema21=3495, ema50=3480, spread=0.6, n_candles=30),
            mk("TCS", 3512, rsi=60, vwap=3496, macd_hist=1.6, volume_ratio=2.4,
               ema9=3500, ema21=3496, ema50=3482, spread=0.6, n_candles=30),
        ])),
        ("swing", build_simple(SwingAgent(), "HDFCBANK", [
            mk("HDFCBANK", 1700, rsi=50, vwap=1690, macd_hist=1.0,
               volume_ratio=1.5, ema9=1705, ema21=1700, ema50=1690,
               ema200=1600, n_candles=210),
        ])),
        ("options", build_simple(OptionsAgent(), "INFY", [
            mk("INFY", 1620, rsi=50, vwap=1612, macd_hist=0.5, volume_ratio=1.6,
               ema9=1615, ema21=1618, ema50=1610, ema200=1500, n_candles=30),
            mk("INFY", 1626, rsi=58, vwap=1612, macd_hist=1.4, volume_ratio=1.9,
               ema9=1624, ema21=1618, ema50=1612, ema200=1500, n_candles=30),
        ])),
        # FuturesAgent only trades INDEX futures (LOT_SIZES) — a stock like SBIN is
        # rejected by the index-only guard. Use NIFTY (funded futures bucket below
        # covers its ~₹360k margin/lot; F&O is exempt from the ₹1M value cap).
        ("futures", build_simple(FuturesAgent(), "NIFTY", [
            mk("NIFTY", 24000, rsi=55, vwap=23850, macd_hist=0.3, volume_ratio=1.6,
               ema9=23960, ema21=24000, ema50=23880, n_candles=30),
            mk("NIFTY", 24150, rsi=60, vwap=23850, macd_hist=1.5, volume_ratio=2.0,
               ema9=24200, ema21=24080, ema50=23950, n_candles=30),
        ])),
        ("mean_reversion", build_mean_reversion()),
        ("momentum",       build_momentum()),
        ("pairs",          build_pairs()),
    ]


# ── Stage 1 + 2 + 3 — signal → order → protective SL, for all 8 agents ─────────
async def stage_all_agents():
    section("STAGE 1/2/3 — All 8 agents: signal → entry+SL-M → bounded-loss check")

    def _stub_assess(snap, action, signal, name):
        async def _coro():
            return GateDecision(confidence=80, enter=True, reason="test-stub")
        return _coro()

    fired = []
    for name, (agent, symbol, trig, action, signal) in recipes():
        # ── Stage 1: signal generated ──
        check(f"{name}: evaluate_tick produced a signal (BUY/SELL)",
              action in ("BUY", "SELL") and signal is not None,
              f"action={action} signal={'set' if signal else 'None'}")
        if action not in ("BUY", "SELL") or signal is None:
            continue

        # ── Stage 2: order placement through the production _try_enter path ──
        reset_state()
        with patch("agents.strategy_agents.now_ist", return_value=MKT), \
             patch.object(claude_trade_gate, "assess", _stub_assess):
            try:
                await agent._try_enter(trig, action, signal)
            except Exception as exc:
                fail(f"{name}: _try_enter raised", repr(exc))
                continue
            await asyncio.sleep(0.05)   # let executor-scheduled placements settle

        entry_tag = _otag(f"Agent-{name}")
        sl_tag    = _otag(f"Agent-{name}", "SL")
        entries = (_orders_with_tag(symbol, entry_tag, "MARKET")
                   or _orders_with_tag(symbol, entry_tag, "LIMIT"))
        check(f"{name}: entry order placed (tag {entry_tag})",
              len(entries) >= 1,
              f"errors={list(agent.state.errors)[-1:] if agent.state.errors else 'none'}")
        if not entries:
            continue
        entry = entries[0]

        # ── Stage 3: every entry has a paired SL-M (loss is bounded) ──
        sl_orders = _orders_with_tag(symbol, sl_tag, "SL-M")
        check(f"{name}: protective SL-M attached, tag distinct from entry ({sl_tag})",
              len(sl_orders) >= 1 and sl_tag != entry_tag,
              "no SL-M order found / SL tag collides with entry — position unidentifiable")

        # SL-M trigger must be on the loss side of entry for a long (BUY).
        if sl_orders and action == "BUY":
            entry_px = entry.get("price") or trig.tick.ltp
            sl_trig  = sl_orders[0].get("trigger_price", 0)
            check(f"{name}: SL-M trigger below entry (real protection)",
                  0 < sl_trig <= entry_px,
                  f"sl_trigger={sl_trig} entry={entry_px}")

        # TSL engine is tracking the position.
        oid = entry["order_id"]
        check(f"{name}: position registered with trailing-SL engine",
              oid in trailing_sl_engine._positions,
              f"order_id {oid} not tracked")
        fired.append(name)

    check("ALL 8 agents fired an order", len(fired) == 8,
          f"only fired: {fired}")


# ── Stage 4 — PAPER→LIVE switch routes to the real Kite API ───────────────────
async def stage_live_switch():
    section("STAGE 4 — PAPER→LIVE switch routes orders to the live Kite API")

    saved_mode = settings.trading_mode
    saved_kite = kite_client._kite          # backing attr; .kite is a read-only property
    try:
        mock_kite = MagicMock()
        mock_kite.place_order.return_value = "LIVE-ORDER-123"
        mock_kite.margins.return_value = {
            "equity": {"available": {"live_balance": 1e9}}
        }
        kite_client._kite = mock_kite       # set_access_token() normally assigns this

        # PAPER path: no real API call, returns a PAPER- id.
        settings.trading_mode = "PAPER"
        pid = kite_client.place_order(
            tradingsymbol="SBIN", exchange="NSE", transaction_type="BUY",
            quantity=1, order_type="MARKET", product="MIS", tag="switch-test",
        )
        check("PAPER mode: order simulated (id starts with PAPER-)",
              str(pid).startswith("PAPER-"), f"id={pid}")
        check("PAPER mode: real Kite API NOT called",
              mock_kite.place_order.call_count == 0,
              f"calls={mock_kite.place_order.call_count}")

        # LIVE path: routes to self.kite.place_order with the same args.
        settings.trading_mode = "LIVE"
        lid = kite_client.place_order(
            tradingsymbol="SBIN", exchange="NSE", transaction_type="BUY",
            quantity=1, order_type="MARKET", product="MIS", tag="switch-test",
        )
        check("LIVE mode: returns broker order id from Kite API",
              lid == "LIVE-ORDER-123", f"id={lid}")
        check("LIVE mode: self.kite.place_order called exactly once",
              mock_kite.place_order.call_count == 1,
              f"calls={mock_kite.place_order.call_count}")
        kwargs = mock_kite.place_order.call_args.kwargs
        check("LIVE mode: order forwarded with correct symbol/side/qty",
              kwargs.get("tradingsymbol") == "SBIN"
              and kwargs.get("transaction_type") == "BUY"
              and kwargs.get("quantity") == 1,
              f"kwargs={kwargs}")
    finally:
        settings.trading_mode = saved_mode
        kite_client._kite = saved_kite


# ── Driver ────────────────────────────────────────────────────────────────────
async def main() -> int:
    print("\n" + "═" * 62)
    print("  ALL-AGENTS E2E  —  AlgoTrader Pro  (pre-deployment)")
    print("  8 agents → signal → entry+SL-M → PAPER→LIVE switch")
    print("═" * 62)

    check("mode is PAPER (no real orders during test)",
          settings.trading_mode == "PAPER", f"mode={settings.trading_mode}")

    saved = {
        "mtf":     settings.use_multi_timeframe,
        "kelly":   settings.use_kelly_sizing,
        "maxpos":  settings.max_open_positions,
        "maxsize": settings.max_position_size,
        "maxloss": settings.max_daily_loss,
        "capital": settings.total_capital,
    }
    settings.use_multi_timeframe = False
    settings.use_kelly_sizing    = False
    settings.max_open_positions  = 50
    settings.max_position_size   = 10_000_000.0
    settings.max_daily_loss      = 10_000_000.0
    # Index futures size on NRML margin: fund the futures bucket so it covers
    # ≥1 NIFTY lot's margin (75 × ~24k × 20% ≈ ₹360k). bucket = total_capital ×
    # futures_capital_pct(10%); ₹5M → ₹500k bucket → 1 lot.
    settings.total_capital       = 5_000_000.0
    try:
        await stage_all_agents()
        await stage_live_switch()
    finally:
        settings.use_multi_timeframe = saved["mtf"]
        settings.use_kelly_sizing    = saved["kelly"]
        settings.max_open_positions  = saved["maxpos"]
        settings.max_position_size   = saved["maxsize"]
        settings.max_daily_loss      = saved["maxloss"]
        settings.total_capital       = saved["capital"]
        reset_state()

    return summary()


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(main()) else 0)
