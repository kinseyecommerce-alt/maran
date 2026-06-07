"""
test_safety_properties.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Safety properties that MUST hold before live deployment.
Each test proves exactly one invariant that, if violated, could lose real money.

Run:  python test_safety_properties.py

All 12 must pass.  Any failure = block deployment.
"""
from __future__ import annotations

import asyncio
import sys
import traceback
import types
import unittest.mock as mock
from typing import Callable

# ── Result harness ────────────────────────────────────────────────────────────
_results: list[tuple[str, bool, str]] = []


def check(name: str, fn: Callable) -> None:
    try:
        fn()
        _results.append((name, True, ""))
        print(f"  \033[92mPASS\033[0m  {name}")
    except AssertionError as e:
        _results.append((name, False, str(e)))
        print(f"  \033[91mFAIL\033[0m  {name}")
        print(f"        {e}")
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  \033[91mFAIL\033[0m  {name}")
        traceback.print_exc()


def acheck(name: str, coro) -> None:
    """Run an async test."""
    try:
        asyncio.run(coro)
        _results.append((name, True, ""))
        print(f"  \033[92mPASS\033[0m  {name}")
    except AssertionError as e:
        _results.append((name, False, str(e)))
        print(f"  \033[91mFAIL\033[0m  {name}")
        print(f"        {e}")
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  \033[91mFAIL\033[0m  {name}")
        traceback.print_exc()


# ── Import modules under test ─────────────────────────────────────────────────
print("\nImporting modules…")
from config import settings
from kite_client import KiteClient
from order_guard import OrderGuard
from risk_manager import RiskManager
from sebi_compliance import SEBICompliance, KillSwitchState
from trailing_sl_engine import TrailingSLEngine

print("OK\n")
print("=" * 64)
print("  SAFETY PROPERTY VERIFICATION")
print("=" * 64)

# ─────────────────────────────────────────────────────────────────────────────
# P-1  No live order in paper mode
# ─────────────────────────────────────────────────────────────────────────────
def p1_no_live_order_in_paper_mode():
    """kite_client.place_order MUST NOT call kite.place_order when mode=PAPER."""
    client = KiteClient()

    # Inject a sentinel: if kite.place_order is ever called, the test fails.
    _live_called = []
    fake_kite = types.SimpleNamespace(
        place_order=lambda **kw: _live_called.append(kw) or "LIVE-ID"
    )
    client._kite = fake_kite

    original_mode = settings.trading_mode
    try:
        settings.trading_mode = "PAPER"
        oid = client.place_order(
            tradingsymbol="RELIANCE", exchange="NSE",
            transaction_type="BUY", quantity=1,
        )
        assert oid.startswith("PAPER-"), f"Expected PAPER- prefix, got {oid}"
        assert len(_live_called) == 0, \
            f"kite.place_order was called {len(_live_called)} time(s) in PAPER mode!"
    finally:
        settings.trading_mode = original_mode

check("P-1  No live order in paper mode", p1_no_live_order_in_paper_mode)


# ─────────────────────────────────────────────────────────────────────────────
# P-2  No duplicate orders
# ─────────────────────────────────────────────────────────────────────────────
def p2_no_duplicate_orders():
    """OrderGuard must block a second claim for the same (symbol, strategy, side)."""
    guard = OrderGuard()

    # First claim must succeed
    ok1, reason1 = guard.try_claim("RELIANCE", "intraday", "BUY")
    assert ok1, f"First claim failed unexpectedly: {reason1}"

    # Second claim (same symbol/strategy/side) must be blocked
    ok2, reason2 = guard.try_claim("RELIANCE", "intraday", "BUY")
    assert not ok2, "Second duplicate claim was ALLOWED — should have been blocked"
    assert "Duplicate" in reason2 or "blocked" in reason2.lower(), \
        f"Unexpected block reason: {reason2}"

    # Opposite side must also be blocked (can't hold BUY and SELL simultaneously)
    ok3, reason3 = guard.try_claim("RELIANCE", "intraday", "SELL")
    assert not ok3, "Opposite-side claim was ALLOWED for same symbol/strategy"

    # Cross-agent block: a different strategy must also be blocked on this symbol
    ok4, reason4 = guard.try_claim("RELIANCE", "scalping", "BUY")
    assert not ok4, "Cross-agent claim was ALLOWED while intraday holds the symbol"

    # Completely different symbol must succeed
    ok5, _ = guard.try_claim("TCS", "intraday", "BUY")
    assert ok5, "Unrelated symbol was incorrectly blocked"

check("P-2  No duplicate orders", p2_no_duplicate_orders)


# ─────────────────────────────────────────────────────────────────────────────
# P-3  Kill switch blocks all new orders
# ─────────────────────────────────────────────────────────────────────────────
def p3_kill_switch_works():
    """After trigger_kill_switch(), pre_order_check must reject every symbol."""
    compliance = SEBICompliance()
    # "intraday" is in APPROVED_ALGO_IDS — no registration step needed

    # Baseline: should be ACTIVE
    assert compliance._state == KillSwitchState.ACTIVE, "Expected ACTIVE state"

    # Trigger the kill switch
    compliance.trigger_kill_switch("automated safety test")
    assert compliance._state == KillSwitchState.KILLED, "State should be KILLED"

    # Every pre_order_check must now fail
    ok, _, reason = compliance.pre_order_check(
        strategy="intraday", symbol="RELIANCE", exchange="NSE",
        transaction_type="BUY", quantity=10, order_type="MARKET",
        price_at_signal=1500.0, signal_source="test", regime="trending",
    )
    assert not ok, "pre_order_check allowed an order after kill switch!"
    assert "kill" in reason.lower() or "halted" in reason.lower() or "killed" in reason.lower(), \
        f"Kill switch rejection reason unclear: {reason}"

    # Verify resume is blocked until reset
    resumed, msg = compliance.resume_trading()
    assert not resumed, "resume_trading() should fail while kill switch is active"

check("P-3  Kill switch blocks all new orders", p3_kill_switch_works)


# ─────────────────────────────────────────────────────────────────────────────
# P-4  Broker reconnect does not repeat orders
# ─────────────────────────────────────────────────────────────────────────────
def p4_reconnect_does_not_repeat_orders():
    """
    The OrderGuard state persists across broker reconnections (it lives in memory,
    not in the broker session). After a simulated reconnect, already-active claims
    remain and block duplicate placement.
    """
    guard = OrderGuard()

    # Simulate: agent places order, claim is confirmed
    ok, _ = guard.try_claim("SBIN", "futures", "BUY")
    assert ok, "Initial claim failed"
    guard.confirm_claim("SBIN", "futures", "BUY", order_id="ORD-001")

    # Simulate broker reconnect: the KiteClient object is re-initialised,
    # but OrderGuard is a module-level singleton — its state is unchanged.
    new_kite_client = KiteClient()   # fresh client object
    assert new_kite_client._paper_orders == {}, "Fresh client should have no paper orders"

    # Attempt to place the same order again after reconnect → must be blocked
    ok2, reason2 = guard.try_claim("SBIN", "futures", "BUY")
    assert not ok2, \
        "Duplicate order was ALLOWED after broker reconnect — OrderGuard state was lost!"

    # Verify the original order_id is still tracked
    with guard._lock:
        active = guard._active.get(("SBIN", "futures", "BUY"))
    assert active is not None, "Claim disappeared from OrderGuard after reconnect"
    assert active.order_id == "ORD-001", f"Wrong order_id: {active.order_id}"

check("P-4  Broker reconnect does not repeat orders", p4_reconnect_does_not_repeat_orders)


# ─────────────────────────────────────────────────────────────────────────────
# P-5  Risk limit blocks trading
# ─────────────────────────────────────────────────────────────────────────────
def p5_risk_limit_blocks_trading():
    """RiskManager must refuse orders when daily loss limit is exceeded."""
    rm = RiskManager()
    original_mode = settings.trading_mode
    original_limit = settings.max_daily_loss

    try:
        settings.trading_mode = "PAPER"     # skip market-hours check
        settings.max_daily_loss = 5000.0

        # Baseline: should allow
        ok, msg = rm.check_before_order("RELIANCE", 10, 1500.0, "BUY")
        assert ok, f"Baseline check failed: {msg}"

        # Breach the daily loss limit
        rm.daily_realised_pnl = -5001.0

        ok2, msg2 = rm.check_before_order("RELIANCE", 10, 1500.0, "BUY")
        assert not ok2, "check_before_order allowed order despite daily loss limit breach!"
        assert "loss" in msg2.lower() or "limit" in msg2.lower() or "halted" in msg2.lower(), \
            f"Rejection reason doesn't mention limit: {msg2}"

        # Verify halted flag is set so subsequent checks also fail (even without re-checking P&L)
        # The first failed check sets is_trading_halted=True
        assert rm.is_trading_halted, "is_trading_halted flag should be set after limit breach"
        ok3, msg3 = rm.check_before_order("TCS", 5, 3800.0, "BUY")
        assert not ok3, "Trade allowed on a different symbol while trading is halted!"

        # Test max position size guard
        rm2 = RiskManager()
        settings.max_position_size = 100_000.0
        ok4, msg4 = rm2.check_before_order("BAJFINANCE", 1000, 9000.0, "BUY")
        # 1000 * 9000 = 9_000_000 >> 100_000
        assert not ok4, "Position size limit was not enforced!"
        assert "size" in msg4.lower() or "exceeds" in msg4.lower(), \
            f"Size rejection reason unclear: {msg4}"

        # Test max position count guard
        rm3 = RiskManager()
        rm3.open_position_count = settings.max_open_positions
        ok5, msg5 = rm3.check_before_order("INFY", 5, 1500.0, "BUY")
        assert not ok5, "Position count limit was not enforced!"
        assert "position" in msg5.lower() or "max" in msg5.lower(), \
            f"Count rejection reason unclear: {msg5}"

    finally:
        settings.trading_mode = original_mode
        settings.max_daily_loss = original_limit

check("P-5  Risk limit blocks trading", p5_risk_limit_blocks_trading)


# ─────────────────────────────────────────────────────────────────────────────
# P-6  Failed order does not create fake position
# ─────────────────────────────────────────────────────────────────────────────
async def p6_failed_order_no_fake_position():
    """
    When kite_client.place_order() raises an exception, the agent must NOT
    register a position in the TSL engine or increment open_position_count.
    """
    from agents.strategy_agents import IntradayAgent
    from tick_engine import MarketSnapshot, Tick, LiveIndicators
    from kite_client import kite_client
    from risk_manager import risk_manager
    from trailing_sl_engine import trailing_sl_engine
    from order_guard import order_guard

    agent = IntradayAgent()
    agent._approved.add("RELIANCE")

    original_mode = settings.trading_mode
    initial_positions = len(trailing_sl_engine._positions)
    initial_count = risk_manager.open_position_count
    initial_trades = risk_manager.trades_today

    # Force place_order to always raise
    exc_to_raise = Exception("Simulated broker rejection: margin insufficient")

    def _failing_place(*args, **kwargs):
        raise exc_to_raise

    original_paper_orders = kite_client._paper_orders.copy()

    try:
        settings.trading_mode = "PAPER"
        with mock.patch.object(kite_client, "place_order", side_effect=exc_to_raise):
            # Build a triggering snapshot with correct types
            from datetime import datetime as _dt
            tick = Tick(
                symbol="RELIANCE", ltp=1510.0, bid=1509.5, ask=1510.5,
                volume=600_000, change=5.0, change_pct=0.5,
                high=1515.0, low=1480.0, open=1485.0, timestamp=_dt.now(),
            )
            ind  = LiveIndicators(
                symbol="RELIANCE", ltp=1510.0,
                ema9=1505.0, ema21=1490.0, ema50=1470.0, ema200=1400.0,
                vwap=1500.0, rsi_14=58.0, macd=2.5, macd_signal=1.5,
                macd_hist=1.0, atr_14=8.0, adx_14=28.0,
                bb_upper=1530.0, bb_lower=1470.0, bb_mid=1500.0,
                volume_ratio=1.4, trend="UP",
            )
            # _try_enter is called directly — candle-count guard is in _run_loop
            snap = MarketSnapshot(symbol="RELIANCE", tick=tick, indicators=ind)

            # Reset claim state for RELIANCE
            order_guard.release_order("RELIANCE", "intraday", "BUY", 0.0)
            order_guard.release_order("RELIANCE", "intraday", "SELL", 0.0)

            await agent._try_enter(snap, "BUY", {
                "score": 10, "pattern": "TEST", "stop_loss": 1490.0, "target": 1530.0,
            })

        # Verify nothing was created
        assert len(trailing_sl_engine._positions) == initial_positions, \
            f"TSL position registered after failed order! ({len(trailing_sl_engine._positions)} positions)"
        assert risk_manager.open_position_count == initial_count, \
            f"open_position_count changed after failed order: {initial_count} → {risk_manager.open_position_count}"
        assert risk_manager.trades_today == initial_trades, \
            f"trades_today changed after failed order: {initial_trades} → {risk_manager.trades_today}"

    finally:
        settings.trading_mode = original_mode

acheck("P-6  Failed order does not create fake position", p6_failed_order_no_fake_position())


# ─────────────────────────────────────────────────────────────────────────────
# P-7  Secrets are not exposed in API responses
# ─────────────────────────────────────────────────────────────────────────────
def p7_secrets_not_exposed():
    """
    API responses and validate_credentials() must never include raw secret values.
    Only boolean presence/absence flags are acceptable.
    """
    import json

    # Inject fake secrets to detect if they leak
    sentinel_key    = "SENTINEL_API_KEY_DO_NOT_EXPOSE"
    sentinel_secret = "SENTINEL_API_SECRET_DO_NOT_EXPOSE"
    sentinel_totp   = "SENTINEL_TOTP_SECRET_DO_NOT_EXPOSE"
    sentinel_anthropic = "SENTINEL_ANTHROPIC_DO_NOT_EXPOSE"

    original_values = {
        "kite_api_key":     settings.kite_api_key,
        "kite_api_secret":  settings.kite_api_secret,
        "kite_totp_secret": settings.kite_totp_secret,
        "anthropic_api_key": settings.anthropic_api_key,
        "api_key":          settings.api_key,
    }

    try:
        settings.kite_api_key     = sentinel_key
        settings.kite_api_secret  = sentinel_secret
        settings.kite_totp_secret = sentinel_totp
        settings.anthropic_api_key = sentinel_anthropic
        settings.api_key          = sentinel_key

        from kite_client import kite_client
        creds = kite_client.validate_credentials()
        creds_json = json.dumps(creds)

        sentinels = [sentinel_key, sentinel_secret, sentinel_totp, sentinel_anthropic]
        for s in sentinels:
            assert s not in creds_json, \
                f"Secret value '{s[:20]}…' leaked in validate_credentials() response!"

        # validate_credentials should only return booleans
        for key, val in creds.items():
            if key == "trading_mode":
                continue
            assert isinstance(val, bool), \
                f"validate_credentials[{key!r}] returned {type(val).__name__} not bool — may expose secret length"

        # Check .gitignore covers .env
        gitignore = open(".gitignore").read()
        assert ".env" in gitignore, ".env not listed in .gitignore!"
        assert "kite_accounts.json" in gitignore, "kite_accounts.json not gitignored!"

        # Check no secret fields appear in the settings status endpoint response
        from risk_manager import risk_manager
        status = risk_manager.status()
        status_json = json.dumps(status)
        for s in sentinels:
            assert s not in status_json, \
                f"Secret '{s[:20]}…' leaked in risk_manager.status()!"

    finally:
        for k, v in original_values.items():
            setattr(settings, k, v)

check("P-7  Secrets are not exposed", p7_secrets_not_exposed)


# ─────────────────────────────────────────────────────────────────────────────
# P-8  Production build succeeds
# ─────────────────────────────────────────────────────────────────────────────
def p8_production_build_succeeds():
    """All critical modules import cleanly and expose their required public API."""
    import importlib

    required_modules = [
        ("config",            ["settings"]),
        ("kite_client",       ["kite_client", "KiteClient"]),
        ("risk_manager",      ["risk_manager", "RiskManager", "compute_tx_costs"]),
        ("order_guard",       ["order_guard", "OrderGuard"]),
        ("trailing_sl_engine",["trailing_sl_engine", "TrailingSLEngine", "TRAIL_CONFIGS"]),
        ("sebi_compliance",   ["sebi_compliance", "SEBICompliance", "KillSwitchState"]),
        ("backtest_engine",   ["backtest_engine"]),
        ("state_store",       ["init_db", "record_trade", "get_daily_pnl", "get_performance_report"]),
        ("agents.base_agent", ["BaseAgent", "_setup_tsl_callbacks"]),
        ("agents.strategy_agents", ["ALL_AGENTS", "IntradayAgent", "ScalpingAgent",
                                    "SwingAgent", "OptionsAgent", "FuturesAgent"]),
        ("atomic_bracket",    ["atomic_bracket_engine"]),
        ("tick_engine",       ["tick_engine", "MarketSnapshot"]),
        ("claude_trade_gate", ["assess"]),
    ]

    for mod_name, attrs in required_modules:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            raise AssertionError(f"Module '{mod_name}' failed to import: {e}")
        for attr in attrs:
            assert hasattr(mod, attr), \
                f"Module '{mod_name}' missing required attribute '{attr}'"

    # Verify ALL_AGENTS contains exactly the 5 production strategy names
    from agents.strategy_agents import ALL_AGENTS
    expected_agents = {"intraday", "scalping", "swing", "options", "futures"}
    missing = expected_agents - set(ALL_AGENTS.keys())
    assert not missing, f"ALL_AGENTS is missing strategies: {missing}"

    # Verify TSL has configs for all 5 strategies
    from trailing_sl_engine import TRAIL_CONFIGS
    for name in expected_agents:
        assert name in TRAIL_CONFIGS, f"TRAIL_CONFIGS missing strategy: {name}"

    # Verify settings defaults that are financially critical
    assert settings.trading_mode in ("PAPER", "LIVE"), \
        f"Invalid trading_mode default: {settings.trading_mode}"
    assert settings.max_daily_loss > 0, "max_daily_loss must be > 0"
    assert settings.max_open_positions > 0, "max_open_positions must be > 0"
    assert settings.max_position_size > 0, "max_position_size must be > 0"
    assert settings.stop_loss_pct > 0, "stop_loss_pct must be > 0"

    # Verify the new tradingsymbol routing is in place (BLK-1 fix)
    import ast, inspect
    from agents.base_agent import BaseAgent
    src = inspect.getsource(BaseAgent._place_orders)
    assert "trade_sym" in src or "futures_symbol" in src, \
        "BLK-1 fix not present: _place_orders must resolve trade_sym from signal"

    # Verify SL-M cancel on target-2 is in place (BLK-3 fix)
    from agents import base_agent as _ba_mod
    setup_src = inspect.getsource(_ba_mod._setup_tsl_callbacks)
    assert "cancel_order" in setup_src and "sl_order_id" in setup_src, \
        "BLK-3 fix not present: _on_target_hit must cancel SL-M before MARKET exit"

    # Verify exception default is sl_already_filled=False (BLK-4 fix)
    assert "sl_already_filled = False" in setup_src, \
        "BLK-4 fix not present: exception path must default to sl_already_filled=False"

check("P-8  Production build succeeds", p8_production_build_succeeds)


# ─────────────────────────────────────────────────────────────────────────────
# P-9  Network failure does not place a duplicate live order
# ─────────────────────────────────────────────────────────────────────────────
def p9_network_failure_no_duplicate_order():
    """
    A NetworkException can be raised AFTER the order reaches the exchange.
    place_order must reconcile against the order book and NEVER blind-retry:
      (a) error + order present in book   → return it, kite.place_order called once
      (b) error + book confirms not landed→ safe to retry (called > 1)
      (c) error + book ALSO unreachable   → raise, kite.place_order called once
    """
    from kiteconnect.exceptions import NetworkException

    original_mode = settings.trading_mode
    try:
        settings.trading_mode = "LIVE"

        # ── (a) order landed despite the network error → no duplicate ──────────
        kc = KiteClient()
        fake = mock.MagicMock()
        fake.place_order.side_effect = NetworkException("read timeout")
        fake.orders.return_value = [{
            "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
            "quantity": 10, "tag": "Agent-intraday", "status": "OPEN",
            "order_id": "BOOK-555",
        }]
        kc._kite = fake
        oid = kc.place_order(
            tradingsymbol="RELIANCE", exchange="NSE", transaction_type="BUY",
            quantity=10, order_type="MARKET", product="MIS", tag="Agent-intraday",
        )
        assert oid == "BOOK-555", f"reconcile should return the landed order id, got {oid!r}"
        assert fake.place_order.call_count == 1, \
            f"order was BLIND-RETRIED after it had landed: {fake.place_order.call_count} calls"

        # ── (b) order genuinely did not land → safe to retry ──────────────────
        kc2 = KiteClient()
        fake2 = mock.MagicMock()
        # fail twice, then succeed; book always empty (confirmed not landed)
        fake2.place_order.side_effect = [
            NetworkException("conn reset"),
            NetworkException("conn reset"),
            "OK-RETRY-1",
        ]
        fake2.orders.return_value = []
        kc2._kite = fake2
        import kite_client as _kcmod
        with mock.patch.object(_kcmod.time, "sleep", lambda *_: None):  # no real backoff wait
            oid2 = kc2.place_order(
                tradingsymbol="TCS", exchange="NSE", transaction_type="BUY",
                quantity=5, order_type="MARKET", product="MIS", tag="Agent-intraday",
            )
        assert oid2 == "OK-RETRY-1", f"expected retry to succeed, got {oid2!r}"
        assert fake2.place_order.call_count == 3, \
            f"expected 2 retries then success, got {fake2.place_order.call_count} calls"

        # ── (c) cannot confirm (book unreachable) → raise, never blind-retry ──
        kc3 = KiteClient()
        fake3 = mock.MagicMock()
        fake3.place_order.side_effect = NetworkException("timeout")
        fake3.orders.side_effect = NetworkException("book unreachable")
        kc3._kite = fake3
        raised = False
        try:
            kc3.place_order(
                tradingsymbol="INFY", exchange="NSE", transaction_type="BUY",
                quantity=5, order_type="MARKET", product="MIS", tag="Agent-intraday",
            )
        except Exception:
            raised = True
        assert raised, "place_order must raise when placement cannot be confirmed"
        assert fake3.place_order.call_count == 1, \
            f"order was BLIND-RETRIED while unconfirmable: {fake3.place_order.call_count} calls"
    finally:
        settings.trading_mode = original_mode

check("P-9  Network failure does not duplicate live order", p9_network_failure_no_duplicate_order)


# ─────────────────────────────────────────────────────────────────────────────
# P-10  Entry failure cancels the orphan SL-M (no naked position)
# ─────────────────────────────────────────────────────────────────────────────
async def p10_entry_fail_cancels_orphan_sl():
    """
    Entry and SL-M are placed in PARALLEL. If the entry FAILS but the SL-M
    SUCCEEDS, the orphan stop must be cancelled — otherwise hitting its trigger
    opens a naked (unbounded-risk) position with no underlying.
    """
    from agents.strategy_agents import IntradayAgent
    from tick_engine import MarketSnapshot, Tick, LiveIndicators
    from kite_client import kite_client
    from order_guard import order_guard
    from datetime import datetime as _dt

    agent = IntradayAgent()
    agent._approved.add("RELIANCE")

    cancel_calls: list[str] = []

    def _place_side(*args, **kwargs):
        # BUY = entry (fails);  SELL = protective SL-M (lands)
        if kwargs.get("transaction_type") == "BUY":
            raise Exception("entry rejected: insufficient margin")
        return "SL-ORPHAN-123"

    def _cancel(oid):
        cancel_calls.append(str(oid))
        return oid

    tick = Tick(
        symbol="RELIANCE", ltp=1510.0, bid=1509.5, ask=1510.5,
        volume=600_000, change=5.0, change_pct=0.5,
        high=1515.0, low=1480.0, open=1485.0, timestamp=_dt.now(),
    )
    ind = LiveIndicators(
        symbol="RELIANCE", ltp=1510.0, ema9=1505.0, ema21=1490.0,
        ema50=1470.0, ema200=1400.0, vwap=1500.0, rsi_14=58.0,
        macd=2.5, macd_signal=1.5, macd_hist=1.0, atr_14=8.0, adx_14=28.0,
        bb_upper=1530.0, bb_lower=1470.0, bb_mid=1500.0,
        volume_ratio=1.4, trend="UP",
    )
    snap = MarketSnapshot(symbol="RELIANCE", tick=tick, indicators=ind)

    original_mode = settings.trading_mode
    order_guard.release_order("RELIANCE", "intraday", "BUY", 0.0)
    order_guard.release_order("RELIANCE", "intraday", "SELL", 0.0)
    try:
        settings.trading_mode = "PAPER"   # gates simple; placement is mocked regardless
        with mock.patch.object(kite_client, "place_order", side_effect=_place_side), \
             mock.patch.object(kite_client, "cancel_order", side_effect=_cancel):
            loop = asyncio.get_running_loop()
            raised = False
            try:
                await agent._place_orders(
                    snap, "BUY",
                    {"stop_loss": 1490.0, "target": 1530.0, "score": 10, "pattern": "TEST"},
                    10, loop,
                )
            except RuntimeError as e:
                raised = True
                assert "entry_failed" in str(e), f"unexpected failure reason: {e}"
            assert raised, "expected entry_failed RuntimeError"
    finally:
        settings.trading_mode = original_mode
        order_guard.release_order("RELIANCE", "intraday", "BUY", 0.0)
        order_guard.release_order("RELIANCE", "intraday", "SELL", 0.0)

    assert "SL-ORPHAN-123" in cancel_calls, \
        "orphan SL-M was NOT cancelled after entry failure → naked-position risk"

acheck("P-10 Entry failure cancels orphan SL-M", p10_entry_fail_cancels_orphan_sl())


# ─────────────────────────────────────────────────────────────────────────────
# P-11  Partial fill resizes the SL-M (no over-hedge → no naked short)
# ─────────────────────────────────────────────────────────────────────────────
async def p11_partial_fill_resizes_sl():
    """
    LIVE: when the entry MARKET order only PARTIALLY fills, the SL-M (placed for
    the requested qty) must be resized DOWN to the filled qty. Otherwise the
    oversized stop flips to a naked short on the unfilled remainder when it fires.
    """
    from agents.strategy_agents import IntradayAgent
    from tick_engine import MarketSnapshot, Tick, LiveIndicators
    from kite_client import kite_client
    from order_guard import order_guard
    from risk_manager import risk_manager
    from sebi_compliance import sebi_compliance
    from datetime import datetime as _dt

    agent = IntradayAgent()
    agent._approved.add("RELIANCE")

    modify_calls: list[tuple[str, int]] = []

    def _place(*args, **kwargs):
        return "ENTRY-1" if kwargs.get("transaction_type") == "BUY" else "SL-1"

    def _order_history(oid):
        # entry filled only 5 of 10
        return [{"status": "COMPLETE", "filled_quantity": 5, "order_id": str(oid)}]

    def _modify(order_id, price=0.0, quantity=0, trigger_price=0.0):
        modify_calls.append((str(order_id), int(quantity)))
        return order_id

    tick = Tick(symbol="RELIANCE", ltp=1510.0, bid=1509.5, ask=1510.5,
                volume=600_000, change=5.0, change_pct=0.5,
                high=1515.0, low=1480.0, open=1485.0, timestamp=_dt.now())
    ind = LiveIndicators(symbol="RELIANCE", ltp=1510.0, ema9=1505.0, ema21=1490.0,
                         ema50=1470.0, ema200=1400.0, vwap=1500.0, rsi_14=58.0,
                         macd=2.5, macd_signal=1.5, macd_hist=1.0, atr_14=8.0,
                         adx_14=28.0, bb_upper=1530.0, bb_lower=1470.0, bb_mid=1500.0,
                         volume_ratio=1.4, trend="UP")
    snap = MarketSnapshot(symbol="RELIANCE", tick=tick, indicators=ind)

    original_mode = settings.trading_mode
    order_guard.release_order("RELIANCE", "intraday", "BUY", 0.0)
    order_guard.release_order("RELIANCE", "intraday", "SELL", 0.0)
    try:
        settings.trading_mode = "LIVE"
        # Isolate the SL-resize logic: the risk gate (P-5) and SEBI gate (P-3) are
        # covered elsewhere and would reject here purely on wall-clock market hours.
        with mock.patch.object(kite_client, "place_order", side_effect=_place), \
             mock.patch.object(kite_client, "order_history", side_effect=_order_history), \
             mock.patch.object(kite_client, "modify_order", side_effect=_modify), \
             mock.patch.object(kite_client, "cancel_order", side_effect=lambda o: o), \
             mock.patch.object(kite_client, "_check_margin", side_effect=lambda *a, **k: None), \
             mock.patch.object(risk_manager, "check_before_order", return_value=(True, "OK")), \
             mock.patch.object(sebi_compliance, "pre_order_check", return_value=(True, "ALGO", "OK")):
            loop = asyncio.get_running_loop()
            _oid, _sl, final_qty = await agent._place_orders(
                snap, "BUY",
                {"stop_loss": 1490.0, "target": 1530.0, "score": 10, "pattern": "TEST"},
                10, loop,
            )
    finally:
        settings.trading_mode = original_mode
        order_guard.release_order("RELIANCE", "intraday", "BUY", 0.0)
        order_guard.release_order("RELIANCE", "intraday", "SELL", 0.0)

    assert final_qty == 5, f"qty should be resized to filled 5, got {final_qty}"
    assert ("SL-1", 5) in modify_calls, \
        f"SL-M was not resized to the filled qty → over-hedge risk; modify_calls={modify_calls}"

acheck("P-11 Partial fill resizes the SL-M", p11_partial_fill_resizes_sl())


# ─────────────────────────────────────────────────────────────────────────────
# P-12  NSE holiday calendar blocks trading
# ─────────────────────────────────────────────────────────────────────────────
def p12_holiday_calendar_blocks_trading():
    """is_market_open() must return False on listed NSE holidays."""
    from datetime import date
    import inspect
    from ist_clock import is_nse_holiday, NSE_HOLIDAYS, is_market_open

    assert len(NSE_HOLIDAYS) > 0, "NSE holiday calendar is empty"
    assert is_nse_holiday(date(2026, 1, 26)), "Republic Day not flagged as a holiday"
    assert not is_nse_holiday(date(2026, 6, 8)), "a normal weekday flagged as a holiday"
    # is_market_open must actually consult the holiday set
    src = inspect.getsource(is_market_open)
    assert "NSE_HOLIDAYS" in src, "is_market_open() does not consult NSE_HOLIDAYS"

check("P-12 NSE holiday calendar blocks trading", p12_holiday_calendar_blocks_trading)


# ── Final summary ─────────────────────────────────────────────────────────────
print()
print("=" * 64)
passed = sum(1 for _, ok, _ in _results if ok)
failed = sum(1 for _, ok, _ in _results if not ok)

if failed == 0:
    print(f"  \033[92m\033[1mALL {passed} SAFETY PROPERTIES VERIFIED — SAFE TO PROCEED\033[0m")
else:
    print(f"  \033[91m\033[1mFAILED: {failed}/{passed+failed} properties not satisfied\033[0m")
    print()
    for name, ok, reason in _results:
        if not ok:
            print(f"  \033[91m✗\033[0m  {name}")
            print(f"       {reason}")
    print()
    print("  \033[91m\033[1mDEPLOYMENT BLOCKED\033[0m — fix the above before going LIVE")

print("=" * 64)
sys.exit(0 if failed == 0 else 1)
