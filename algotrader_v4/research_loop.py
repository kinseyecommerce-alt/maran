"""
research_loop.py — the master agent's nightly research loop (jag 2026-10-10:
"best self learning, self thinking").

    proposed → backtested → rejected
                          ↘ probation (PAPER, half size) → promoted | retired

1. Review probation: a promoted policy version is judged by self_learning's
   auto-rollback on the next ROLLBACK_K real-price (Kite, in-session) trades:
   confirmed → PROMOTED, rolled back → RETIRED (old params restored).
2. Hypotheses: bounded one-step variations of each agent's policy params,
   chosen from what the last real-data backtest and the journal say went
   wrong (costs eating the gross → higher edge multiple; time-stopped trades
   losing → shorter time stop; stops dominating → earlier breakeven; low hit
   rate → longer cool-down / fewer consecutive losses; …) plus inventor lessons.
3. Backtest: unified_backtest (live agent code, real Kite data, costs,
   walk-forward OOS, deflated Sharpe for the number of hypotheses tried).
4. Passing hypotheses (best per agent, at most one) go to PAPER PROBATION:
   learning.set_params(policy:<agent>, kind="probation") — versioned, bounded,
   guarded — and the allocator halves that agent's size until judged.

Guardrails: PAPER only (Guard.require_paper), never changes trading mode or
LIVE gates, policy specs cannot raise hard caps (HARD_DAILY_CAP, size ≤ 1.0)
or touch FORBIDDEN_KEYS, owner-paused segments / owner-retired strategies and
owner-pinned agents are skipped. Every decision is logged with a plain-English
explanation (research table + learning events).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
HYP_PATH = HERE / "logs" / "backtests" / "research_hypotheses.json"
OUT_PATH = HERE / "logs" / "backtests" / "research_backtest.json"
WL_PATH = HERE / "logs" / "agent_whitelist.json"
MAX_PER_AGENT = 4
MAX_STEP = 0.35
STATUSES = ("proposed", "backtested", "rejected", "probation", "promoted", "retired")
BACKTESTABLE = ("intraday", "scalping", "swing", "momentum", "mean_reversion", "futures", "options",
                "option_scalping", "mcx_native", "invent")


def _iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


class Research:
    def __init__(self, store=None) -> None:
        self._store = store
        self._lock = threading.RLock()
        self.running = False
        self.started: Optional[str] = None
        self.last_error = ""
        self.last_report: dict = {}
        self._ensure()

    def st(self):
        if self._store is not None:
            return self._store
        from self_learning import learning
        return learning.store

    def _ensure(self) -> None:
        try:
            self.st().x("""CREATE TABLE IF NOT EXISTS research (
                id TEXT PRIMARY KEY, ts TEXT, updated TEXT, cycle TEXT, agent TEXT, kind TEXT, title TEXT,
                params TEXT, why TEXT, status TEXT, evidence TEXT, outcome TEXT, history TEXT)""")
        except Exception:
            pass

    # ── rows ────────────────────────────────────────────────────────────────
    def add(self, cycle: str, agent: str, kind: str, title: str, params: dict, why: str) -> str:
        rid = f"R-{uuid.uuid4().hex[:8].upper()}"
        h = [{"ts": _iso(), "status": "proposed", "note": why}]
        self.st().x("INSERT INTO research(id, ts, updated, cycle, agent, kind, title, params, why, status, evidence, "
                    "outcome, history) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, _iso(), _iso(), cycle, agent, kind, title, json.dumps(params), why, "proposed", "{}", "",
                     json.dumps(h)))
        return rid

    def update(self, rid: str, status: str, note: str, evidence: Optional[dict] = None, outcome: str = "") -> None:
        assert status in STATUSES
        r = self.get(rid)
        if not r:
            return
        h = r["history"] + [{"ts": _iso(), "status": status, "note": note}]
        ev = {**(r["evidence"] or {}), **(evidence or {})}
        self.st().x("UPDATE research SET status=?, updated=?, evidence=?, outcome=?, history=? WHERE id=?",
                    (status, _iso(), json.dumps(ev, default=str), outcome or note, json.dumps(h), rid))
        try:
            from self_learning import learning
            learning.event(f"research_{status}", "", f"policy:{r['agent']}", f"{r['title']}: {note}")
        except Exception:
            pass

    @staticmethod
    def _row(r: dict) -> dict:
        out = dict(r)
        for k in ("params", "evidence", "history"):
            try:
                out[k] = json.loads(out.get(k) or ("[]" if k == "history" else "{}"))
            except Exception:
                out[k] = [] if k == "history" else {}
        return out

    def get(self, rid: str) -> Optional[dict]:
        r = self.st().q("SELECT * FROM research WHERE id=?", (rid,))
        return self._row(r[0]) if r else None

    def rows(self, status: Optional[str] = None, limit: int = 300) -> list[dict]:
        if status:
            rs = self.st().q("SELECT * FROM research WHERE status=? ORDER BY ts DESC LIMIT ?", (status, limit))
        else:
            rs = self.st().q("SELECT * FROM research ORDER BY ts DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rs]

    def pipeline(self) -> dict:
        rows = self.rows()
        counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}
        bt = {}
        try:
            if OUT_PATH.exists():
                d = json.loads(OUT_PATH.read_text())
                bt = {"generated": d.get("generated"), "days": [d.get("days", [None])[0], d.get("days", [None])[-1]],
                      "n_days": len(d.get("days") or []), "notes": d.get("notes"),
                      "agents": {a: {"stats": v["stats"], "verdict": v["verdict"]} for a, v in
                                 (d.get("agents") or {}).items()},
                      "elapsed_s": d.get("elapsed_s")}
        except Exception:
            pass
        alloc = {}
        try:
            alloc = (self.st().kv_get("allocator", {}) or {}).get("agents", {})
        except Exception:
            pass
        return {"counts": counts, "rows": rows, "last_report": self.last_report or self.st().kv_get("research_last", {}),
                "backtest": bt, "allocator": alloc,
                "runner": {"running": self.running, "started": self.started, "last_error": self.last_error},
                "guardrails": ["PAPER only — research never switches to LIVE or touches LIVE gates",
                               "bounded params; hard daily caps / size ≤ 1.0 cannot be raised",
                               "owner-paused segments, owner-retired strategies and owner-pinned agents are skipped",
                               "every change is a versioned learning param with automatic rollback"]}

    # ── owner / guardrails ──────────────────────────────────────────────────
    @staticmethod
    def eligible_agents() -> tuple[list[str], dict]:
        from agent_policy import AGENT_SEGMENT, owner_windows
        from owner_universe import owner_universe
        skipped = {}
        out = []
        pinned = set()
        try:
            cfg = json.loads((HERE / "logs" / "agent_policy.json").read_text())
            pinned = set(cfg.get("owner_pinned_agents") or [])
        except Exception:
            pass
        try:
            from self_learning import learning
            st = learning._state
        except Exception:
            st = {}
        for a in BACKTESTABLE:
            seg = AGENT_SEGMENT.get(a, "")
            if a in pinned:
                skipped[a] = "owner-pinned agent (logs/agent_policy.json)"
            elif seg and seg not in ("ANY", "*") and not owner_universe.segment_enabled(seg):
                skipped[a] = f"segment {seg} paused by owner"
            elif seg == "NSE_FO" and not owner_universe.fo_underlying_allowed("NIFTY"):
                skipped[a] = "NIFTY F&O paused by owner"
            elif (st.get(f"policy:{a}") or {}).get("retired_by_owner") or (st.get(a) or {}).get("retired_by_owner"):
                skipped[a] = "owner-retired"
            else:
                out.append(a)
        return out, skipped

    # ── hypotheses ──────────────────────────────────────────────────────────
    @staticmethod
    def _move(spec: tuple, cur: float, direction: int, frac: float = 0.25) -> Optional[float]:
        d, lo, hi = spec
        step = max(abs(cur) * min(frac, MAX_STEP), (hi - lo) * 0.1, 1 if isinstance(d, int) else 0.0)
        step = min(step, abs(cur) * MAX_STEP) if cur else step
        new = cur + direction * step
        new = max(lo, min(hi, new))
        if isinstance(d, int):
            new = int(round(new))
        return None if abs(new - cur) < 1e-9 else new

    def hypotheses(self, agents: list[str], baseline: Optional[dict] = None) -> list[dict]:
        """[{agent, title, params (delta), why}] — at most MAX_PER_AGENT per agent."""
        from agent_policy import POLICY_SPECS, live_params
        out = []
        lessons = {}
        try:
            from self_learning import learning
            lessons = learning.lessons()
        except Exception:
            pass
        for a in agents:
            spec = POLICY_SPECS[a]
            p = live_params(a)
            b = (baseline or {}).get(a) or {}
            st = b.get("stats") or {}
            by_exit = b.get("by_exit") or {}
            cand: list[tuple[str, dict, str]] = []

            def mv(k, direction, why, title):
                if k in spec:
                    v = self._move(spec[k], p[k], direction)
                    if v is not None:
                        cand.append((title, {k: v}, why))

            gross, costs = float(st.get("gross") or 0), float(st.get("costs") or 0)
            if costs > 0 and costs >= 0.4 * (abs(gross) + 1e-9):
                mv("edge_cost_mult", +1, f"costs ₹{costs:,.0f} were {costs / max(abs(gross), 1):.0%} of gross "
                   f"₹{gross:,.0f} in the last real-data backtest — demand a bigger expected move per trade",
                   "higher cost-edge multiple")
            ts = by_exit.get("time_stop") or {}
            if ts.get("n", 0) >= 3 and ts.get("net", 0) < 0:
                mv("time_stop_min", -1, f"{ts['n']} time-stopped trades lost ₹{-ts['net']:,.0f} — cut dead trades sooner",
                   "shorter time stop")
            sx = by_exit.get("stop") or {}
            if sx.get("n", 0) >= 3 and sx.get("net", 0) < 0:
                mv("be_r", -1, f"{sx['n']} stop-outs lost ₹{-sx['net']:,.0f} — lock breakeven earlier", "earlier breakeven")
            if st.get("win_rate") is not None and st["win_rate"] < 40 and st.get("trades", 0) >= 5:
                mv("cooldown_min", +1, f"win rate {st['win_rate']}% — wait longer after a loss", "longer cool-down")
                mv("max_consec_losses", -1, f"win rate {st['win_rate']}% — stop a symbol after fewer losses",
                   "fewer losses before symbol stop")
            if st.get("trades", 0) >= 10 and float(st.get("net") or 0) < 0:
                mv("daily_cap", -1, f"{st['trades']} OOS trades lost ₹{-st['net']:,.0f} — trade less", "lower daily cap")
            wins_exit = [k for k, v in by_exit.items() if v.get("net", 0) > 0 and v.get("n", 0) >= 3]
            if "trail" in wins_exit or "stop" in wins_exit:
                mv("trail_atr_mult", -1, "trailed exits are the profitable ones — trail tighter", "tighter chandelier")
            if a == "invent" and lessons.get("avoid"):
                bad = ", ".join(f"{x['idea']}@{x['regime']}" for x in lessons["avoid"][:3])
                mv("edge_cost_mult", +1, f"inventor lessons: losing ideas {bad} — require more edge", "inventor: more edge")
            # generic "trade less, better" probes when the data gave no specific lead
            if len(cand) < 2:
                mv("edge_cost_mult", +1, "probe: fewer, higher-edge trades", "higher cost-edge multiple")
                mv("whitelist_n", -1, "probe: only the most liquid instruments", "narrower whitelist")
            seen = set()
            for title, delta, why in cand:
                key = json.dumps(delta, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"agent": a, "title": title, "params": delta, "why": why})
                if sum(1 for x in out if x["agent"] == a) >= MAX_PER_AGENT:
                    break
        return out

    # ── probation review ────────────────────────────────────────────────────
    def review_probation(self) -> list[dict]:
        from self_learning import learning
        from allocator import allocator
        out = []
        learning.check_rollbacks()
        for r in self.rows("probation"):
            vid = (r["evidence"] or {}).get("version_id")
            v = learning.store.q("SELECT status, effect FROM versions WHERE version_id=?", (vid,)) if vid else []
            status = v[0]["status"] if v else "missing"
            eff = json.loads(v[0]["effect"] or "{}") if v else {}
            if status == "confirmed":
                self.update(r["id"], "promoted", f"PROMOTED — next {eff.get('live_trades')} real-price paper trades "
                            f"₹{eff.get('live_expectancy', 0):,.0f}/trade ≥ baseline ₹{eff.get('baseline') or 0:,.0f}",
                            {"live": eff})
                allocator.set_probation(r["agent"], False)
                out.append({"id": r["id"], "agent": r["agent"], "result": "promoted"})
            elif status in ("rolled_back", "superseded", "missing"):
                why = (f"RETIRED — real-price paper trades ₹{eff.get('live_expectancy', 0):,.0f}/trade < baseline; "
                       "old params restored automatically") if status == "rolled_back" else \
                      f"RETIRED — version {status} (replaced by a later change)"
                self.update(r["id"], "retired", why, {"live": eff})
                allocator.set_probation(r["agent"], False)
                out.append({"id": r["id"], "agent": r["agent"], "result": "retired"})
            else:
                n = len(learning._rows_for(f"policy:{r['agent']}", since=r["updated"]))
                out.append({"id": r["id"], "agent": r["agent"], "result": f"still on probation ({n} evidence trades)"})
        return out

    # ── the cycle ───────────────────────────────────────────────────────────
    def _backtest(self, agents: list[str], hyps: list[dict], days: int, workers: int,
                  runner: Optional[Callable] = None) -> dict:
        if runner is not None:
            return runner(agents, hyps, days, workers)
        HYP_PATH.parent.mkdir(parents=True, exist_ok=True)
        HYP_PATH.write_text(json.dumps(hyps, indent=1))
        cmd = [sys.executable, str(HERE / "unified_backtest.py"), "--agents", ",".join(agents), "--days", str(days),
               "--workers", str(workers), "--out", str(OUT_PATH), "--hypotheses", str(HYP_PATH)]
        log = HERE / "logs" / "backtests" / "research_backtest.log"
        with open(log, "w") as fh:
            p = subprocess.run(cmd, cwd=str(HERE), stdout=fh, stderr=subprocess.STDOUT, timeout=4 * 3600,
                               env={**os.environ, "PYTHONUNBUFFERED": "1"})
        if p.returncode != 0:
            raise RuntimeError(f"unified_backtest exited {p.returncode} (see {log})")
        return json.loads(OUT_PATH.read_text())

    def run_cycle(self, days: int = 20, workers: int = 6, agents: Optional[list[str]] = None,
                  runner: Optional[Callable] = None, write_whitelist: bool = True) -> dict:
        from self_learning import learning, Guard
        from allocator import allocator
        Guard.require_paper()
        t0 = time.time()
        cycle = datetime.now(IST).strftime("%Y%m%d-%H%M")
        rep: dict = {"cycle": cycle, "started": _iso(), "probation_review": [], "proposed": 0, "passed": 0,
                     "rejected": 0, "probation": [], "skipped_agents": {}, "notes": []}
        rep["probation_review"] = self.review_probation()
        elig, skipped = self.eligible_agents()
        if agents:
            elig = [a for a in elig if a in agents]
        rep["skipped_agents"] = skipped
        prev = {}
        try:
            src = OUT_PATH if OUT_PATH.exists() else HERE / "logs" / "backtests" / "unified_backtest.json"
            prev = (json.loads(src.read_text()).get("agents") or {}) if src.exists() else {}
        except Exception:
            prev = {}
        on_probation = {r["agent"] for r in self.rows("probation")}
        hyps = self.hypotheses([a for a in elig if a not in on_probation], prev)
        for h in hyps:
            h["id"] = self.add(cycle, h["agent"], "policy", h["title"], h["params"], h["why"])
        rep["proposed"] = len(hyps)
        res = self._backtest(elig, [{"id": h["id"], "agent": h["agent"], "params": h["params"]} for h in hyps],
                             days, workers, runner)
        rep["notes"] = res.get("notes") or []
        rep["baseline"] = {a: {"stats": v["stats"], "verdict": v["verdict"]} for a, v in (res.get("agents") or {}).items()}
        best: dict[str, dict] = {}
        for hr in res.get("hypotheses") or []:
            rid = hr.get("id")
            st = hr.get("stats") or {}
            ev = {"oos": {k: st.get(k) for k in ("trades", "net", "costs", "win_rate", "expectancy", "profit_factor",
                                                 "max_dd")},
                  "baseline": {k: (hr.get("baseline") or {}).get(k) for k in ("trades", "net", "expectancy")},
                  "n_trials": hr.get("n_trials"), "days": len(res.get("days") or [])}
            if hr.get("result") == "pass":
                rep["passed"] += 1
                self.update(rid, "backtested", f"PASSED out-of-sample: {hr['why']}", ev)
                a = hr["agent"]
                if a not in best or st.get("net", 0) > best[a]["stats"].get("net", 0):
                    best[a] = hr
            else:
                rep["rejected"] += 1
                self.update(rid, "backtested", "walk-forward backtest done", ev)
                self.update(rid, "rejected", f"REJECTED: {hr.get('why')}", ev)
        for a, hr in best.items():
            r = self.get(hr["id"])
            base = hr.get("baseline") or {}
            try:
                v = learning.set_params(f"policy:{a}", r["params"],
                                        f"research {hr['id']}: {r['title']} — {hr['why']}", kind="probation",
                                        metrics_before=base, metrics_after=hr.get("stats"),
                                        baseline_exp=base.get("expectancy"))
                allocator.set_probation(a, True, f"research {hr['id']}")
                self.update(hr["id"], "probation",
                            f"PAPER PROBATION at half size — judged on the next real-price paper trades "
                            f"(auto-rollback if worse than ₹{(base.get('expectancy') or 0):,.0f}/trade)",
                            {"version_id": v["version_id"]})
                rep["probation"].append({"agent": a, "id": hr["id"], "params": r["params"]})
            except Exception as exc:
                self.update(hr["id"], "rejected", f"REJECTED by guard: {exc}")
        for hr in res.get("hypotheses") or []:
            r = self.get(hr.get("id") or "")
            if r and r["status"] == "backtested":
                self.update(r["id"], "rejected", "passed, but another variation of this agent did better this cycle")
        if write_whitelist and res.get("whitelist"):
            try:
                cfg = json.loads((HERE / "logs" / "agent_policy.json").read_text()) \
                    if (HERE / "logs" / "agent_policy.json").exists() else {}
                if not cfg.get("whitelist_pinned"):
                    WL_PATH.write_text(json.dumps({**res["whitelist"], "generated": _iso(),
                                                   "source": "unified_backtest real Kite 1m data",
                                                   "days": [res["days"][0], res["days"][-1]] if res.get("days") else []},
                                                  indent=1))
                    rep["notes"].append("liquidity whitelist refreshed from real data")
            except Exception as exc:
                rep["notes"].append(f"whitelist not written: {exc}")
        allocator.refresh()
        rep["elapsed_s"] = round(time.time() - t0, 1)
        rep["finished"] = _iso()
        self.last_report = rep
        try:
            self.st().kv_set("research_last", rep)
            learning.event("research_cycle", "", "master", f"research cycle {cycle}: {rep['proposed']} proposed, "
                           f"{rep['passed']} passed OOS, {len(rep['probation'])} to probation, "
                           f"{rep['rejected']} rejected")
        except Exception:
            pass
        return rep

    def start(self, **kw) -> dict:
        with self._lock:
            if self.running:
                return {"ok": False, "why": "a research cycle is already running", "started": self.started}
            self.running, self.started, self.last_error = True, _iso(), ""

        def _go():
            try:
                self.run_cycle(**kw)
            except Exception as exc:
                self.last_error = str(exc)[:500]
            finally:
                self.running = False

        threading.Thread(target=_go, daemon=True, name="research-cycle").start()
        return {"ok": True, "started": self.started}


research = Research()
