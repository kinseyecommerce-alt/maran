"""
segments.py — one supervising agent per market segment.

    NSE_EQ  NSE stocks (cash)   Kite exchange NSE   09:15–15:30 IST
    NSE_FO  NSE F&O             Kite exchange NFO   09:15–15:30 IST
    BSE_EQ  BSE stocks (cash)   Kite exchange BSE   09:15–15:30 IST
    MCX     MCX commodities     Kite exchange MCX   09:00–~23:30 IST
    CDS     NSE currency (CDS)  Kite exchange CDS   09:00–17:00 IST

Each segment agent owns its capital, risk limits (daily loss, max positions,
max trades/day), kill switch, P&L, instrument universe, trading-hours window
and its own PAPER/LIVE gate. Strategies run *inside* a segment:

    NSE_EQ: intraday, scalping, swing, momentum, mean_reversion, pairs
    NSE_FO: options, futures (+ option_scalping, not started by default)
    BSE_EQ / MCX / CDS: segment-native paper strategies (segment_engine.py).
            PAPER + live data: priced off Kite quotes (BSE cash, MCX/CDS
            front-month futures), per-instrument fallback to the simulator;
            every price is labelled KITE or SIMULATED. Orders stay paper.

This module is also the single server-side source for every agent/segment
state shown in the UI (`strategy_states()` / `segment_states()`), consumed by
main.engine_status() → /health, /bot/status, WS 'engine'.

Safety:
  • Per-segment gate: a segment trades LIVE only if the global mode is LIVE
    AND that segment was armed with confirm=true + typed "SEND". With the
    global mode LIVE and a segment still PAPER, the segment's *entries* are
    refused (fail closed; exits are never blocked).
  • BSE_EQ / MCX / CDS cannot be armed: their Kite order/instrument support is
    stubbed (see KITE_STUBS). Their orders only ever fill in the paper ledger.
  • Switching the global mode back to PAPER disarms every segment.
"""
from __future__ import annotations

import hmac
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as dtime
from typing import Optional

from loguru import logger

from config import settings

LIVE_CONFIRM_PHRASE = "SEND"


def _t(s: str) -> dtime:
    h, m = (int(x) for x in s.split(":"))
    return dtime(h, m)


@dataclass(frozen=True)
class SegmentSpec:
    code: str
    label: str
    kite_exchange: str
    open_s: str
    close_s: str                 # "MCX" → settings.mcx_close_time
    squareoff_min_before: int    # native engines flatten intraday this many min before close
    nse_holidays: bool           # follows the NSE trading-holiday list
    strategies: tuple
    native: bool                 # strategies run in segment_engine (simulated feed)
    live_supported: bool
    live_stub_reason: str = ""

    @property
    def open_t(self) -> dtime:
        return _t(self.open_s)

    @property
    def close_t(self) -> dtime:
        return _t(settings.mcx_close_time if self.close_s == "MCX" else self.close_s)


SEGMENTS: dict[str, SegmentSpec] = {
    "NSE_EQ": SegmentSpec("NSE_EQ", "NSE Stocks", "NSE", "09:15", "15:30", 20, True,
                          ("intraday", "scalping", "swing", "momentum", "mean_reversion", "pairs"),
                          native=False, live_supported=True),
    "NSE_FO": SegmentSpec("NSE_FO", "NSE F&O", "NFO", "09:15", "15:30", 20, True,
                          ("options", "futures", "option_scalping"),
                          native=False, live_supported=True),
    "BSE_EQ": SegmentSpec("BSE_EQ", "BSE Stocks", "BSE", "09:15", "15:30", 15, True,
                          ("bse_momentum", "bse_mean_reversion"),
                          native=True, live_supported=False,
                          live_stub_reason="no BSE quote feed or BSE order routing wired — paper only"),
    "MCX":    SegmentSpec("MCX", "MCX Commodities", "MCX", "09:00", "MCX", 10, False,
                          ("mcx_trend", "mcx_mean_reversion"),
                          native=True, live_supported=False,
                          live_stub_reason="Kite MCX instruments/contract roll, commodity margins "
                                           "and MCX tick feed are stubbed — paper only"),
    "CDS":    SegmentSpec("CDS", "Currency (NSE CDS)", "CDS", "09:00", "17:00", 10, True,
                          ("cds_trend", "cds_mean_reversion"),
                          native=True, live_supported=False,
                          live_stub_reason="Kite CDS instruments/contract roll and currency tick "
                                           "feed are stubbed — paper only"),
}
SEGMENT_ORDER = ["NSE_EQ", "NSE_FO", "BSE_EQ", "MCX", "CDS"]

# Strategies the dashboard lists by default. option_scalping exists in the
# registry but is not one of the 8 started strategies; it is listed only while
# it actually runs so a never-started agent doesn't clutter the panels.
HIDDEN_UNLESS_RUNNING = {"option_scalping"}

STRATEGY_SEGMENT: dict[str, str] = {s: spec.code for spec in SEGMENTS.values() for s in spec.strategies}
EXCHANGE_SEGMENT: dict[str, str] = {"NSE": "NSE_EQ", "NFO": "NSE_FO", "BSE": "BSE_EQ",
                                    "MCX": "MCX", "CDS": "CDS"}

# What the existing Kite client does NOT support for the new segments. Live
# orders there are impossible by construction (segments cannot be armed).
KITE_STUBS = [
    "MCX: instrument master + front-month contract resolution/rollover (GOLDM, CRUDEOILM, …)",
    "MCX: commodity margins (kite.margins('commodity')) and MCX lot sizes in _FON_LOT_SIZES",
    "CDS: instrument master + contract resolution (USDINR, EURINR, …) and CDS lot sizes",
    "MCX/CDS/BSE: tick subscription in tick_engine/kite_ticker (NSE/NFO only today)",
    "BSE: per-stock quotes (no public feed reachable; needs Kite/TrueData)",
    "Per-segment square-off times in master_agent (only NSE 15:10 is scheduled there)",
]


def segment_of(strategy: str = "", exchange: str = "") -> Optional[str]:
    if strategy and strategy in STRATEGY_SEGMENT:
        return STRATEGY_SEGMENT[strategy]
    return EXCHANGE_SEGMENT.get((exchange or "").upper())


def _limits(code: str) -> dict:
    k = code.lower()
    cap = float(getattr(settings, f"segment_capital_{k}"))
    return {
        "capital": cap,
        "max_daily_loss": round(cap * settings.segment_daily_loss_pct / 100.0, 2),
        "risk_per_trade": round(cap * float(getattr(settings, "segment_risk_per_trade_pct", 1.0)) / 100.0, 2),
        "max_positions": int(getattr(settings, f"segment_max_positions_{k}")),
        "max_trades_per_day": int(settings.segment_max_trades_per_day),
    }


def notional_caps(code: str) -> tuple[float, float]:
    """(max notional of ONE position, max gross notional of all open
    positions) in ₹ for the segment — exposure caps on top of margin and
    per-trade risk (audit X10). Per-segment override:
    segment_max_position_notional_x_<code>."""
    cap = _limits(code)["capital"]
    x = getattr(settings, f"segment_max_position_notional_x_{code.lower()}", None)
    x = float(x if x else getattr(settings, "segment_max_position_notional_x", 1.0) or 1.0)
    g = float(getattr(settings, "segment_max_gross_notional_x", 3.0) or 3.0)
    return cap * x, cap * max(g, x)


class SegmentModeError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


class SegmentManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mode: dict[str, str] = {c: "PAPER" for c in SEGMENTS}      # never persisted
        self._killed: dict[str, Optional[str]] = {c: None for c in SEGMENTS}
        self._killed_at: dict[str, Optional[str]] = {c: None for c in SEGMENTS}
        self._entries_today: dict[str, int] = {c: 0 for c in SEGMENTS}
        self._entries_day: date | None = None
        # strategies stopped by the segment supervisor (closed / killed) and
        # to be restarted by it when the window reopens / segment is re-armed
        self._held: dict[str, str] = {}
        self._last_supervise = 0.0
        self._now_fn = None           # test hook: () -> aware IST datetime

    # ── clock / hours ──────────────────────────────────────────────────────
    def now(self) -> datetime:
        if self._now_fn:
            return self._now_fn()
        from ist_clock import now_ist
        return now_ist()

    def is_open(self, code: str, now: datetime | None = None) -> bool:
        spec = SEGMENTS[code]
        n = now or self.now()
        if n.weekday() >= 5:
            return False
        if spec.nse_holidays:
            from ist_clock import NSE_HOLIDAYS
            if n.date() in NSE_HOLIDAYS:
                return False
        t = n.time().replace(tzinfo=None)
        return spec.open_t <= t < spec.close_t

    def next_open(self, code: str, now: datetime | None = None) -> datetime:
        spec = SEGMENTS[code]
        n = now or self.now()
        for add in range(0, 10):
            d = (n + timedelta(days=add)).date()
            cand = datetime.combine(d, spec.open_t, tzinfo=n.tzinfo)
            if cand <= n and not (add == 0 and n.time().replace(tzinfo=None) < spec.open_t):
                continue
            if self.is_open(code, cand + timedelta(minutes=1)):
                return cand
        return n

    def window_ok(self, code: str, now: datetime | None = None) -> bool:
        """True when the segment may trade now: inside its REAL exchange hours
        (weekdays, holidays excluded) — in PAPER too. The old
        segment_paper_after_hours switch (PAPER trading 24×7 on frozen last
        prices, labelled KITE) is ignored: after-hours paper fills were fake
        data that polluted the journal, learning and readiness (audit X1)."""
        return self.is_open(code, now)

    def squareoff_cut(self, code: str, now: datetime | None = None) -> datetime:
        """Start of the segment's square-off window (close − squareoff_min_before)."""
        spec = SEGMENTS[code]
        n = now or self.now()
        return (datetime.combine(n.date(), spec.close_t, tzinfo=n.tzinfo)
                - timedelta(minutes=spec.squareoff_min_before))

    def entry_window_ok(self, code: str, now: datetime | None = None) -> tuple[bool, str]:
        """New entries need the segment open AND before its square-off window
        (an entry at 15:15 on BSE was squared off 1 s later: pure cost churn)
        AND not in the last `segment_no_entry_before_close_min` minutes."""
        n = now or self.now()
        if not self.is_open(code, n):
            return False, f"{SEGMENTS[code].label} closed (hours {self.hours_text(code)})"
        cut = self.squareoff_cut(code, n)
        extra = int(getattr(settings, "segment_no_entry_before_squareoff_min", 5) or 0)
        if n >= cut - timedelta(minutes=extra):
            return False, (f"{SEGMENTS[code].label}: no new entries after "
                           f"{(cut - timedelta(minutes=extra)).strftime('%H:%M')} (square-off window)")
        return True, "OK"

    def hours_text(self, code: str) -> str:
        s = SEGMENTS[code]
        return f"{s.open_t.strftime('%H:%M')}–{s.close_t.strftime('%H:%M')} IST"

    # ── per-segment PAPER/LIVE gate ────────────────────────────────────────
    def mode(self, code: str) -> str:
        return self._mode.get(code, "PAPER")

    def effective_mode(self, code: str) -> str:
        return "LIVE" if (settings.trading_mode == "LIVE" and self.mode(code) == "LIVE") else "PAPER"

    def set_mode(self, code: str, mode: str, confirm: bool = False, confirm_text: str = "") -> dict:
        if code not in SEGMENTS:
            raise SegmentModeError(404, f"unknown segment {code!r}")
        mode = (mode or "").upper()
        if mode not in ("PAPER", "LIVE"):
            raise SegmentModeError(400, "mode must be PAPER or LIVE")
        spec = SEGMENTS[code]
        if mode == "LIVE":
            if not spec.live_supported:
                raise SegmentModeError(409, f"{spec.label}: LIVE not supported — {spec.live_stub_reason}")
            if settings.trading_mode != "LIVE":
                raise SegmentModeError(409, "global trading mode is PAPER — switch it to LIVE "
                                            "(typed SEND) before arming a segment")
            if not confirm:
                raise SegmentModeError(400, "confirm=true required to arm a segment for LIVE")
            if not hmac.compare_digest((confirm_text or "").strip().encode(), LIVE_CONFIRM_PHRASE.encode()):
                logger.warning("[segments] LIVE arm REFUSED for {} — typed confirmation missing", code)
                raise SegmentModeError(400, f"Type {LIVE_CONFIRM_PHRASE} (confirm_text) to arm "
                                            f"{spec.label} for LIVE — real orders will be sent")
        with self._lock:
            prev = self._mode[code]
            self._mode[code] = mode
        logger.warning("[segments] {} mode {} → {}", code, prev, mode)
        return {"segment": code, "mode": mode, "previous": prev,
                "effective_mode": self.effective_mode(code)}

    def disarm_all(self) -> None:
        with self._lock:
            for c in self._mode:
                self._mode[c] = "PAPER"

    # ── kill switch ────────────────────────────────────────────────────────
    def killed(self, code: str) -> Optional[str]:
        self.expire_daily_halts()
        return self._killed.get(code)

    DAILY_HALT = "daily_loss_limit"

    def expire_daily_halts(self) -> list[str]:
        """A daily-loss halt lasts for the rest of the IST day only: release
        it once the date has rolled (manual kills stay until re-armed)."""
        today = self.now().date().isoformat()
        released = []
        with self._lock:
            for c, r in self._killed.items():
                if r == self.DAILY_HALT and not str(self._killed_at.get(c) or "").startswith(today):
                    self._killed[c] = None
                    self._killed_at[c] = None
                    released.append(c)
        for c in released:
            logger.warning("[segments] {} daily-loss halt expired (new IST day) — segment re-armed", c)
        return released

    def check_loss_limits(self) -> list[str]:
        """Halt (and flatten) every segment whose P&L today (realised + open)
        is at or below its daily loss cap. Runs from the supervisor, so a cap
        is enforced on open positions too — not only when a new entry asks."""
        halted = []
        for code in SEGMENT_ORDER:
            if self._killed.get(code):
                continue
            try:
                lim = _limits(code)
                if self.net_total(code) <= -lim["max_daily_loss"]:
                    self.kill(code, reason=self.DAILY_HALT, flatten=True)
                    halted.append(code)
            except Exception as exc:
                logger.debug("[segments] loss-limit check {}: {}", code, exc)
        return halted

    def kill(self, code: str, reason: str = "manual", flatten: bool = True) -> dict:
        if code not in SEGMENTS:
            raise SegmentModeError(404, f"unknown segment {code!r}")
        with self._lock:
            self._killed[code] = reason
            self._killed_at[code] = self.now().isoformat(timespec="seconds")
        logger.warning("[segments] KILL SWITCH {} ({})", code, reason)
        flattened = self._flatten(code) if flatten else []
        self.supervise(force=True)
        try:
            from strategy_inventor import strategy_inventor
            strategy_inventor.on_segment_kill(code, reason)
        except Exception:
            pass
        return {"segment": code, "killed": True, "reason": reason, "flattened": flattened}

    def rearm(self, code: str) -> dict:
        if code not in SEGMENTS:
            raise SegmentModeError(404, f"unknown segment {code!r}")
        with self._lock:
            self._killed[code] = None
            self._killed_at[code] = None
        logger.warning("[segments] {} kill switch released", code)
        self.supervise(force=True)
        return {"segment": code, "killed": False}

    def _flatten(self, code: str) -> list:
        """Square off the segment's PAPER positions. LIVE positions are never
        auto-flattened from here (entries are blocked; exits stay with agents)."""
        spec = SEGMENTS[code]
        if spec.native:
            from segment_engine import native_engine
            return native_engine.flatten(code, reason="kill_switch")
        if settings.trading_mode != "PAPER":
            return []
        from kite_client import kite_client
        out = []
        if code == "NSE_FO":
            try:
                from options_engine import options_engine
                out.extend(options_engine.close_all(reason="kill_switch"))
            except Exception as exc:
                logger.error("[segments] options engine close_all failed: {}", exc)
        # shorts (BUY to close) first — see option_guard: hedges are sold last
        for p in sorted(list(getattr(kite_client, "_paper_positions", [])),
                        key=lambda r: int(r.get("quantity", 0) or 0)):
            if p.get("exchange") != spec.kite_exchange or not p.get("quantity"):
                continue
            q = int(p["quantity"])
            try:
                oid = kite_client.place_order(
                    tradingsymbol=p["tradingsymbol"], exchange=spec.kite_exchange,
                    transaction_type="SELL" if q > 0 else "BUY", quantity=abs(q),
                    order_type="MARKET", product=p.get("product", "MIS"), tag="SEG-KILL")
                out.append({"symbol": p["tradingsymbol"], "qty": q, "order_id": oid})
            except Exception as exc:
                logger.error("[segments] flatten {} {} failed: {}", code, p.get("tradingsymbol"), exc)
        return out

    # ── book: positions / P&L / capital ───────────────────────────────────
    def positions(self, code: str) -> list[dict]:
        spec = SEGMENTS[code]
        if spec.native:
            from segment_engine import native_engine
            return native_engine.positions(code)
        try:
            from kite_client import kite_client
            if settings.trading_mode == "PAPER":
                rows = list(getattr(kite_client, "_paper_positions", []))
            else:
                rows = (kite_client.positions_cached() or {}).get("net", [])
        except Exception:
            rows = []
        out = []
        for p in rows:
            if p.get("exchange") != spec.kite_exchange or not p.get("quantity"):
                continue
            out.append({"symbol": p.get("tradingsymbol"), "qty": int(p.get("quantity", 0)),
                        "avg": float(p.get("average_price") or 0), "ltp": float(p.get("last_price") or 0),
                        "pnl": float(p.get("pnl") or 0)})
        return out

    def pnl(self, code: str) -> dict:
        spec = SEGMENTS[code]
        if spec.native:
            from segment_engine import native_engine
            return native_engine.pnl(code)
        realised = 0.0
        trades = 0
        try:
            from agents.strategy_agents import ALL_AGENTS
            for s in spec.strategies:
                a = ALL_AGENTS.get(s)
                if a:
                    realised += float(a.state.pnl_today or 0)
                    trades += int(a.state.trades_today or 0)
        except Exception:
            pass
        # NOTE: displays use book.py (Σ pnl of listed exit fills); this gate
        # input stays on the agents' own realised counters (unchanged risk
        # semantics).
        # Master-approved invented strategies trade NSE/NFO through the paper
        # ledger, not through an agent — their realised P&L counts toward the
        # segment's daily loss cap too.
        try:
            from strategy_inventor import strategy_inventor
            inv = strategy_inventor.realised_today(code)
            realised += inv["realised"]
            trades += inv["entries"]
        except Exception:
            pass
        if code == "NSE_FO":
            # options engine (baskets / option buys) and the fast scalper's
            # paper-ledger scalps (futures + option scalps) realise P&L outside
            # the agents — it counts toward the NSE_FO daily loss cap.
            try:
                from options_engine import options_engine
                realised += options_engine.realised_today()
                trades += options_engine.entries_today()
            except Exception:
                pass
        if code in ("NSE_FO", "NSE_EQ"):
            try:
                from fast_scalper import fast_scalper
                d = fast_scalper.stats.get("by_segment", {}).get(code) or {}
                realised += float(d.get("pnl") or 0.0)
                trades += int(d.get("entries") or 0)
            except Exception:
                pass
        unreal = sum(p["pnl"] for p in self.positions(code))
        return {"realised": round(realised, 2), "unrealised": round(unreal, 2),
                "total": round(realised + unreal, 2), "trades_today": trades}

    def costs_today(self, code: str) -> float:
        """Transaction costs of today's closed trades in the segment (from the
        learning journal, which prices every fill with cost_model)."""
        try:
            from self_learning import learning
            day = self.now().date().isoformat()
            r = learning.store.q("SELECT SUM(costs) c FROM journal WHERE segment=? AND day=?", (code, day))
            return float((r[0] or {}).get("c") or 0.0) if r else 0.0
        except Exception:
            return 0.0

    def net_total(self, code: str) -> float:
        """Today's P&L AFTER costs (realised + open − today's costs) — the
        daily-loss cap works on net, not gross (audit #15)."""
        return round(self.pnl(code)["total"] - self.costs_today(code), 2)

    def position_count(self, code: str) -> int:
        """Open positions, with each multi-leg option basket counted once."""
        rows = self.positions(code)
        if code != "NSE_FO":
            return len(rows)
        try:
            from options_engine import options_engine
            legs = options_engine.leg_symbols()
            n_b = sum(1 for b in options_engine.baskets.values() if b.status == "OPEN")
            n_buy = sum(1 for p in options_engine.buys.values() if p.status == "OPEN")
        except Exception:
            return len(rows)
        return sum(1 for p in rows if p.get("symbol") not in legs) + n_b + n_buy

    def capital_used(self, code: str) -> float:
        spec = SEGMENTS[code]
        if spec.native:
            from segment_engine import native_engine
            return native_engine.margin_used(code)
        # NFO futures block margin, not notional (one NIFTY lot ≈ ₹17L notional
        # would otherwise exceed the whole ₹10L segment allocation).
        fut_m = float(getattr(settings, "futures_margin_pct", 20.0)) / 100.0
        used = 0.0
        legs: set = set()
        if code == "NSE_FO":
            # option baskets block their (hedged) margin estimate, not the
            # premium notional of each leg
            try:
                from options_engine import options_engine
                legs = options_engine.leg_symbols()
                used += options_engine.margin_used()
            except Exception:
                legs = set()
        for p in self.positions(code):
            if p.get("symbol") in legs:
                continue
            v = abs(p["qty"]) * (p["ltp"] or p["avg"])
            if code == "NSE_FO" and str(p.get("symbol") or "").endswith("FUT"):
                v *= fut_m
            used += v
        return round(used, 2)

    def futures_notional(self, code: str) -> float:
        """Full contract notional of open futures positions in the segment."""
        tot = 0.0
        for p in self.positions(code):
            if str(p.get("symbol") or "").endswith("FUT"):
                tot += abs(p["qty"]) * float(p["ltp"] or p["avg"] or 0)
        return round(tot, 2)

    def _roll_day(self) -> None:
        d = self.now().date()
        if self._entries_day != d:
            self._entries_day = d
            self._entries_today = {c: 0 for c in SEGMENTS}

    def note_entry(self, code: str) -> None:
        with self._lock:
            self._roll_day()
            self._entries_today[code] = self._entries_today.get(code, 0) + 1

    # ── the per-segment entry gate (paper gate + typed-SEND gate + risk) ──
    def entry_check(self, code: Optional[str], *, notional: float = 0.0,
                    transaction_type: str = "BUY", count: bool = True,
                    symbol: str = "", contract_notional: float = 0.0) -> tuple[bool, str]:
        if not code or code not in SEGMENTS:
            return True, "OK"
        self.expire_daily_halts()
        spec = SEGMENTS[code]
        # Orders that only reduce an existing position are exits — never
        # blocked by a segment gate (kill switch / hours / LIVE-arming).
        if symbol:
            for p in self.positions(code):
                if p["symbol"] == symbol and (
                        (p["qty"] > 0 and transaction_type == "SELL") or
                        (p["qty"] < 0 and transaction_type == "BUY")):
                    return True, "OK (reducing)"
        if self._killed.get(code):
            return False, f"{spec.label} kill switch active ({self._killed[code]})"
        if settings.trading_mode == "LIVE" and self.mode(code) != "LIVE":
            return False, (f"{spec.label} is PAPER-gated — arm it for LIVE with typed "
                           f"{LIVE_CONFIRM_PHRASE} before it may place live entries")
        okw, whyw = self.entry_window_ok(code)
        if not okw:
            return False, whyw
        lim = _limits(code)
        if self.net_total(code) <= -lim["max_daily_loss"]:
            if not self._killed.get(code):
                self.kill(code, reason=self.DAILY_HALT, flatten=True)
            return False, f"{spec.label} daily loss limit ₹{lim['max_daily_loss']:,.0f} hit"
        with self._lock:
            self._roll_day()
            if self._entries_today.get(code, 0) >= lim["max_trades_per_day"]:
                return False, f"{spec.label} max trades/day ({lim['max_trades_per_day']}) reached"
        if self.position_count(code) >= lim["max_positions"]:
            return False, f"{spec.label} max open positions ({lim['max_positions']}) reached"
        if contract_notional > 0:
            # futures: margin is not exposure — cap the CONTRACT notional of this
            # position and of all open futures in the segment (audit X10)
            one, gross = notional_caps(code)
            if contract_notional > one:
                return False, (f"{spec.label} position notional ₹{contract_notional:,.0f} > cap ₹{one:,.0f}")
            if self.futures_notional(code) + contract_notional > gross:
                return False, (f"{spec.label} gross futures notional cap ₹{gross:,.0f} reached "
                               f"(open ₹{self.futures_notional(code):,.0f} + ₹{contract_notional:,.0f})")
        if notional > 0 and self.capital_used(code) + notional > lim["capital"]:
            return False, (f"{spec.label} capital ₹{lim['capital']:,.0f} exhausted "
                           f"(used ₹{self.capital_used(code):,.0f} + ₹{notional:,.0f})")
        if count:
            self.note_entry(code)
        return True, "OK"

    def can_run(self, strategy: str) -> bool:
        code = STRATEGY_SEGMENT.get(strategy)
        if not code:
            return True
        return not self._killed.get(code) and self.window_ok(code)

    # ── strategy registry + start/stop helpers ────────────────────────────
    def _all(self) -> dict:
        from agents.strategy_agents import ALL_AGENTS
        from segment_engine import native_engine
        return {**ALL_AGENTS, **native_engine.strategies}

    def _start(self, name: str) -> bool:
        from segment_engine import native_engine
        if name in native_engine.strategies:
            native_engine.strategies[name].start()
            return True
        from agents.strategy_agents import ALL_AGENTS
        from master_agent_v5 import master_agent
        from tick_engine import tick_engine
        a = ALL_AGENTS.get(name)
        if not a or a.state.running or not master_agent._agent_watchlists.get(name):
            return False
        q = tick_engine.add_subscriber(f"agent_{name}")
        a.start(q)
        return True

    def _stop(self, name: str) -> None:
        from segment_engine import native_engine
        if name in native_engine.strategies:
            native_engine.strategies[name].stop()
            return
        from agents.strategy_agents import ALL_AGENTS
        from tick_engine import tick_engine
        a = ALL_AGENTS.get(name)
        if a and a.state.running:
            a.stop()
            tick_engine.remove_subscriber(f"agent_{name}")

    def hold(self, name: str, why: str) -> None:
        self._held[name] = why

    def release_holds(self) -> None:
        self._held.clear()

    def supervise(self, force: bool = False) -> None:
        """Stop strategies of closed/killed segments; restart the ones this
        supervisor stopped once their segment can run again (engine running,
        strategy still enabled, not regime-paused)."""
        now = time.monotonic()
        if not force and now - self._last_supervise < 5.0:
            return
        self._last_supervise = now
        self.expire_daily_halts()
        self.check_loss_limits()
        try:
            from master_agent_v5 import master_agent
            import bot_state
        except Exception:
            return
        if not master_agent.running:
            return
        allr = self._all()
        for name, a in allr.items():
            code = STRATEGY_SEGMENT.get(name)
            if not code:
                continue
            blocked = self._killed.get(code) and "killed" or (not self.window_ok(code) and "closed")
            if blocked and a.state.running:
                self._stop(name)
                self._held[name] = blocked
                logger.info("[segments] {} stopped — {} {}", name, code, blocked)
            elif not blocked and name in self._held:
                if (bot_state.is_agent_enabled(name)
                        and name not in getattr(master_agent, "regime_paused", set())
                        and name not in getattr(master_agent, "directive_paused", set())):
                    if self._start(name):
                        logger.info("[segments] {} restarted — {} open", name, code)
                self._held.pop(name, None)

    # ── THE single source of per-strategy / per-segment state ─────────────
    def strategy_states(self, phase: str, master_running: bool,
                        sbook: Optional[dict] = None) -> dict:
        import bot_state
        try:
            from master_agent_v5 import master_agent
            regime_paused = set(getattr(master_agent, "regime_paused", set()))
            directive_paused = set(getattr(master_agent, "directive_paused", set()))
            watchlists = master_agent._agent_watchlists
        except Exception:
            regime_paused, directive_paused, watchlists = set(), set(), {}
        from segment_engine import native_engine
        starting = phase in ("scanning_instruments", "loading_instruments")
        out = {}
        for name, a in self._all().items():
            code = STRATEGY_SEGMENT.get(name, "")
            running = bool(a.state.running)
            enabled = bot_state.is_agent_enabled(name)
            native = name in native_engine.strategies
            if starting:
                state, reason = "starting", "engine starting"
            elif running and master_running:
                state, reason = "running", ""
            elif not master_running:
                state, reason = "stopped", "engine stopped"
            elif code and self._killed.get(code):
                state, reason = "killed", f"segment kill switch ({self._killed[code]})"
            elif code and not self.window_ok(code):
                state, reason = "closed", f"segment closed · {self.hours_text(code)}"
            elif not enabled:
                state, reason = "paused", "paused manually"
            elif name in regime_paused:
                state, reason = "paused", "paused by regime plan"
            elif name in directive_paused:
                state, reason = "paused", "paused by master review"
            elif not native and not watchlists.get(name):
                state, reason = "paused", "no approved symbols"
            else:
                state, reason = "paused", "not started"
            meta = getattr(a, "meta", None) or {}
            bk = (sbook or {}).get(name) or {}
            out[name] = {
                "segment": code, "state": state, "reason": reason, "on": state == "running",
                "running": running, "enabled": enabled, "native": native,
                "hidden": name in HIDDEN_UNLESS_RUNNING and not running,
                # trades = entries today; P&L = realised + open (from book.py)
                "trades_today": int(bk.get("trades_today", getattr(a.state, "trades_today", 0)) or 0),
                "pnl_today": float(bk.get("total", round(float(getattr(a.state, "pnl_today", 0) or 0), 2))),
                "pnl_realised": float(bk.get("realised", 0.0)),
                "pnl_unrealised": float(bk.get("unrealised", 0.0)),
                "open_positions": int(bk.get("open_positions", 0)),
                "display": meta.get("display"), "desc": meta.get("desc"),
                "can_resume": state not in ("starting", "stopped", "killed", "closed"),
            }
        return out

    def segment_states(self, phase: str, master_running: bool, strategies: dict) -> list[dict]:
        from segment_engine import native_engine
        self.expire_daily_halts()
        starting = phase in ("scanning_instruments", "loading_instruments")
        now = self.now()
        rows = []
        for code in SEGMENT_ORDER:
            spec = SEGMENTS[code]
            kids = [s for s, v in strategies.items() if v["segment"] == code and not v["hidden"]]
            n_run = sum(1 for s in kids if strategies[s]["running"])
            is_open = self.is_open(code, now)
            if starting:
                state, reason = "starting", "engine starting"
            elif not master_running:
                state, reason = "stopped", "engine stopped"
            elif self._killed.get(code):
                state, reason = "killed", f"kill switch ({self._killed[code]})"
            elif not self.window_ok(code, now):
                nxt = self.next_open(code, now)
                state, reason = "closed", f"opens {nxt.strftime('%a %H:%M')} IST"
            elif n_run:
                state, reason = "running", ("after-hours simulation" if not is_open else "")
            else:
                state, reason = "paused", "no strategy running"
            lim = _limits(code)
            pnl = self.pnl(code)
            used = self.capital_used(code)
            rows.append({
                "code": code, "label": spec.label, "kite_exchange": spec.kite_exchange,
                "state": state, "reason": reason, "on": state == "running",
                "open": is_open, "hours": self.hours_text(code),
                "mode": self.mode(code), "effective_mode": self.effective_mode(code),
                "live_supported": spec.live_supported, "live_stub_reason": spec.live_stub_reason or None,
                "feed": native_engine.feed_label(code) if spec.native else native_engine.nse_feed_label(code),
                "killed": bool(self._killed.get(code)), "kill_reason": self._killed.get(code),
                "capital": lim["capital"], "capital_used": used,
                "limits": lim, "pnl": pnl,
                "positions": len(self.positions(code)),
                "entries_today": self._entries_today.get(code, 0),
                "strategies": kids, "strategies_running": n_run,
                "universe": native_engine.universe(code),
            })
        return rows


segment_manager = SegmentManager()
