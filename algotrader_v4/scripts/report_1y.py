"""report_1y.py — compose /workspace/abitrade_1y_backtest.md from the 1-year run outputs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
K1Y = HERE / "logs" / "kite_1y"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/workspace/abitrade_1y_backtest.md")


def inr(x):
    if x is None:
        return "—"
    s = f"{abs(x):,.0f}"
    return ("−₹" if x < 0 else "₹") + s


def f(x, nd=2):
    return "—" if x is None else (f"{x:.{nd}f}" if isinstance(x, (int, float)) else str(x))


def main():
    cov = json.loads((K1Y / "coverage.json").read_text())
    opt = json.loads((HERE / "logs" / "backtest_1y_options.json").read_text())
    mst = json.loads((K1Y / "mcx_stitch_coverage.json").read_text()) if (K1Y / "mcx_stitch_coverage.json").exists() else {}
    L = []
    w = L.append
    w("# AbiTrade — 1-year backtest on Kite data (PAPER research)\n")
    from datetime import datetime, timedelta
    gen = (datetime.fromisoformat(opt["generated"]) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M")
    w(f"Generated {gen} IST. Kite account YFW795, PAPER mode, no orders placed. "
      f"Options walk-forward OOS window **{opt['oos_window'][0]} … {opt['oos_window'][1]} ({opt['oos_window'][2]} sessions)**, "
      f"12 expanding folds, 2-session embargo, first 40 sessions train-only.\n")

    # summary
    tab_ = opt["table"]; cur_ = opt.get("_curves", {})
    ag = tab_["AGENT:validated_signals"]
    ex = {k: v for k, v in tab_.items() if k.startswith("EXEC:") or k.startswith("SIG:")}
    best = max(ex, key=lambda k: ex[k]["net"])
    bn = sorted((n for _, n in cur_.get(best, [])), reverse=True)
    vv = im_ = opt["iv_model"]["validation"]
    gw_ = opt.get("gate_written") or {}
    posx = [k for k, v in ex.items() if v["net"] > 0]

    def ex5_(k):
        return sum(sorted((n for _, n in cur_.get(k, [])), reverse=True)[5:])
    w("## Summary\n")
    w(f"- **Data:** 1 year of real Kite 1-minute data for the NIFTY 50 index, India VIX and all 50 Nifty stocks "
      f"(2025-09-01 … 2026-10-09). Real NIFTY option prices exist for only **{opt['iv_model']['real_option_sessions']} sessions** "
      f"(2026-07-29 … 2026-10-09), because Kite drops expired contracts. The weekly contract the agent trades has real prices "
      f"on just **{len(opt['real_option_days'])} sessions** (6–9 Oct). MCX and NIFTY futures minute data cover only "
      f"still-listed contracts, from about 3 to 12 months.")
    w(f"- **Option model:** it's now driven by India VIX and calibrated on those real prices. On held-out real days the "
      f"price error fell from {vv['old_flat_k_rv20']['mape_pct']}% to {vv['new_vix_surface']['mape_pct']}% (MAPE). For ATM weekly "
      f"options it is {vv['atm_weekly_new']['mape_pct']}%, though the model still prices them about {vv['atm_weekly_new']['bias_pct']:+.0f}% too high.")
    w(f"- **The NIFTY options agent as it would trade** (only signals validated on earlier data, 234 unseen sessions): "
      f"**{inr(ag['net'])}** after {inr(ag['costs_incl_spread'])} in costs, {ag['trades']} trades, win {ag['win_pct']}%, "
      f"PF {ag['pf']}, {ag['pct_per_month_10L']:+.2f}% a month on ₹10L. **No strategy passes** the gate: the best deflated "
      f"Sharpe (DSR) of any tradable variant is {max((v.get('why') or '').count('DSR') and float((v.get('why') or '').split('DSR ')[1].split()[0].rstrip(';,')) or 0 for v in {**opt['gate_signals'], **opt['gate_methods']}.values() if 'DSR ' in (v.get('why') or '') and v.get('status') != 'blocked'):.2f}, against the 0.95 needed.")
    w(f"- **Best single variant:** {best.split(':')[1]}, {inr(ex[best]['net'])} ({ex[best]['pct_per_month_10L']:+.2f}%/month). "
      f"Without its 5 best trades it is {inr(sum(bn[5:]))}. Of the {len(posx)} tradable variants that are positive, "
      f"{sum(1 for k in posx if ex5_(k) <= 0)} turn negative without their 5 best trades: the profit comes from a few "
      f"big trend days, so it's a fat-tailed bet, not a steady earner.")
    w(f"- **Gate (rules unchanged):** probation now = {', '.join(k for k, v in gw_.items() if v == 'probation') or 'none'}. "
      f"TWAP_REJECT, TOD_PE_1330 and X_ORB15_SEL moved up from blocked (positive OOS net, weak PF/DSR; TOD_PE_1330 is a plain "
      f"time-of-day baseline, treat with scepticism); FIRST_HOUR_TREND and X_CPR_NARROW dropped to blocked. Nothing is `pass`, and LIVE stays locked.")
    others = []
    for pth in ("unified_backtest_1y_nse.json", "unified_backtest_1y_mcx.json", "unified_backtest_1y_silverm.json"):
        q_ = HERE / "logs" / "backtests" / pth
        if q_.exists():
            for a_, r_ in json.loads(q_.read_text())["agents"].items():
                st_ = r_.get("stats") or {}
                if st_.get("trades"):
                    others.append((a_, st_.get("net") or 0))
    pos = [a_ for a_, n_ in others if n_ > 0]
    w(f"- **Other agents (section 7):** {len(others)} agent runs with trades; "
      + (f"positive after costs: {', '.join(pos)} (none pass the DSR gate)." if pos else "none makes money after costs over the year.")
      + "\n")

    # coverage
    w("## 1. Data coverage (Kite historical API, cached in `logs/kite_1y/`)\n")
    w("| Series | Interval | Sessions | First | Last | Bars |\n|---|---|---|---|---|---|")
    groups = {}
    for k, v in sorted(cov.items()):
        if "days" not in v:
            continue
        if k.startswith("OPT_"):
            groups.setdefault("opt", []).append(v)
            continue
        if k.startswith("EQ_"):
            groups.setdefault("eq", []).append((k, v))
            continue
        w(f"| {k} | {v.get('interval','minute')}{' (continuous)' if v.get('continuous') else ''} | {v['days']} | {v['first']} | {v['last']} | {v['bars']:,} |")
    eq = groups.get("eq", [])
    if eq:
        ds = [v["days"] for _, v in eq]
        w(f"| Nifty 50 stocks ({len(eq)}) | minute | {min(ds)}–{max(ds)} | {min(v['first'] for _, v in eq)} | "
          f"{max(v['last'] for _, v in eq)} | {sum(v['bars'] for _, v in eq):,} |")
    op = [v for v in groups.get("opt", []) if v["days"]]
    if op:
        w(f"| NIFTY options ({len(op)} contracts with data / {len(groups['opt'])} requested) | minute | 1–{max(v['days'] for v in op)} per contract | "
          f"{min(v['first'] for v in op)} | {max(v['last'] for v in op)} | {sum(v['bars'] for v in op):,} |")
    miss = [k for k, v in cov.items() if "error" in v]
    if miss:
        w(f"\nMissing: {', '.join(miss)}")
    im = opt["iv_model"]
    w(f"\n**Real option prices:** {im['real_option_sessions']} sessions of real NIFTY option 1-minute bars "
      f"({im['first']} … {im['last']}, {im['contracts']} contracts in the calibration sample). "
      f"Kite only serves history for contracts still listed, so expired weeklies are gone. "
      f"The contract the agent actually trades (nearest weekly with ≥1 DTE, ATM) is still listed on only "
      f"**{len(opt['real_option_days'])} sessions: {', '.join(opt['real_option_days'])}** — on every other day premia are modelled.\n")
    if mst:
        w("**MCX minute series** (continuous=1 works for daily candles only, so minute bars come from still-listed contracts, "
          "stitched by nearest expiry):\n")
        w("| MCX | Sessions | First | Last | Contracts used (sessions) |\n|---|---|---|---|---|")
        for k, v in mst.items():
            w(f"| {k} | {v['days']} | {v['first']} | {v['last']} | {', '.join(f'{c} ({n})' for c, n in v['contracts_days'].items())} |")
    w("\nNIFTY futures minute bars exist only for the 3 live contracts (Oct/Nov/Dec 2026); the futures agent decides on the "
      "index series as before. Continuous daily futures/MCX series were cached for basis/roll context.\n")

    # model
    v = im["validation"]
    w("## 2. Option price model — recalibrated on real prices\n")
    w("IV = VIX/100 × exp(f(log-DTE, standardized moneyness)), fitted on every real OTM/ATM option bar (5-min samples, "
      f"premium ≥ ₹1, ±10 strikes): {im['n_samples']:,} samples. Validated on held-out real sessions "
      f"{v['test_sessions'][0]} … {v['test_sessions'][1]} ({v['test_sessions'][2]} sessions; model fitted on the earlier "
      f"{v['train_sessions'][2]} only).\n")
    w("| Model (held-out real days, premium ≥ ₹5) | Samples | MAPE | Median abs error | Bias | IV RMSE (vol pts) |\n|---|---|---|---|---|---|")
    for lab, key in (("NEW: VIX-driven surface", "new_vix_surface"), ("OLD: flat k × RV20", "old_flat_k_rv20"),
                     ("NEW, ATM weekly (≤8 DTE)", "atm_weekly_new"), ("OLD, ATM weekly (≤8 DTE)", "atm_weekly_old")):
        e = v.get(key)
        if e:
            w(f"| {lab} | {e['n']:,} | {e['mape_pct']}% | {e['median_abs_pct']}% | {e['bias_pct']:+}% | {e['iv_rmse_volpts']} |")
    iv = im["intraday_iv_vs_vix"]
    if iv.get("n_5min_pairs"):
        w(f"\nIntraday ATM weekly IV vs VIX: 5-minute log-changes correlate {iv['corr_dlnIV_dlnVIX']} (beta {iv['beta']}, "
          f"{iv['n_5min_pairs']:,} pairs); ATM weekly IV / VIX median {iv['atm_weekly_iv_over_vix_median']} "
          f"(10–90%: {iv['atm_weekly_iv_over_vix_p10_p90'][0]}–{iv['atm_weekly_iv_over_vix_p10_p90'][1]}).\n")
    w("Model days now use the VIX minute series for IV (IV moves intraday with VIX and with moneyness/DTE); real option closes "
      "replace the model wherever the traded contract has a bar. Spread model + ₹0.05 slippage per order and full statutory "
      "costs (₹20/order, STT, exchange, SEBI, GST, stamp) unchanged. Lot 75 for expiries ≤ 30-Dec-2025, 65 after. "
      "Weekly expiry Tuesday (holiday → previous session); the engine never sells expiry-day contracts.\n")

    # per-strategy table
    w("## 3. NIFTY intraday options — 1-year walk-forward OOS, per strategy (each traded alone, after all costs)\n")
    w("SIG = harness signals (train-fold picks horizon/exits); METHOD = published rule set; EXEC = the agent-executable variant "
      "the live agent would run; AGENT = the agent trading only train-validated signals together. "
      "% per month = net ÷ ₹10L ÷ OOS sessions × 21.\n")
    w("| Strategy | Trades | Win % | Gross | Costs | Net | %/month | Max DD | PF | Sharpe | DSR | Net without best 5 trades | Verdict |\n"
      "|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    tab = opt["table"]
    cur = opt.get("_curves", {})
    gm = opt.get("gate_methods", {})
    for k in sorted(tab, key=lambda k: (not k.startswith("AGENT"), -tab[k]["net"])):
        m = tab[k]
        if not m["trades"]:
            continue
        nets = sorted((n for _, n in cur.get(k, [])), reverse=True)
        ex5 = sum(nets[5:]) if nets else None
        v = m["verdict"]
        if k.startswith("METHOD:"):
            v = f"{(gm.get(k[7:]) or {}).get('status', 'blocked')} (published rule set; method verdict: {v}; trades only via its EXEC variant)"
        w(f"| {k} | {m['trades']} | {m['win_pct']} | {inr(m['gross'])} | {inr(m['costs_incl_spread'])} | {inr(m['net'])} | "
          f"{m['pct_per_month_10L']:+.2f}% | {inr(-m['max_dd'])} | {f(m['pf'])} | {f(m['sharpe_daily_ann'])} | {f(m['dsr'],3)} | {inr(ex5)} | {v} |")
    zero = [k for k in tab if not tab[k]["trades"]]
    if zero:
        w(f"\nNo OOS trades (never validated on a train fold, or no setups): {', '.join(zero)}.")
    w("\nNot run this time: IV-rich-morning / post-event IV-crush sells and OI/PCR shifts. India VIX minute history and "
      "51 sessions of real option OI are now cached, so these can be built next (OI only for the last 51 sessions).\n")
    w("DSR here uses the combined trial count of everything tested in this run (stricter); the gate rows use each module's "
      "own trial count, as before — neither comes close to the 0.95 pass bar.\n")

    # real days
    w("## 4. Real-option-price days only\n")
    q = opt["real_quote_share"]
    w("| Session | Real option quotes | Modelled quotes |\n|---|---|---|")
    for d, x in q.items():
        w(f"| {d} | {x['real']:,} | {x['model']:,} |")
    w("\n| Strategy | Trades | Win % | Net | PF |\n|---|---|---|---|---|")
    for k, m in sorted(opt["real_days_table"].items(), key=lambda kv: -kv[1]["net"]):
        w(f"| {k} | {m['trades']} | {m['win_pct']} | {inr(m['net'])} | {f(m['pf'])} |")
    w("\nThis is a handful of sessions — far too few to judge any strategy; it is reported as asked, as a sanity check "
      "of the model against real fills, not as evidence.\n")

    px = opt.get("real_proxy") or {}
    if px.get("sessions"):
        w(f"### 4b. Real-price check on all {px['sessions']} sessions with real option bars ({px['first']} … {px['last']})\n")
        w(f"Same rules (latest train-fold exits / published parameters — NOT walk-forward), but each day trades the nearest "
          f"listed expiry whose ATM strikes actually traded ({', '.join(px['expiries_used'])}; DTE median {px['dte_median']}, "
          f"range {px['dte_range'][0]}–{px['dte_range'][1]} days — longer-dated than the agent's weekly). "
          f"{100 * px['real_quote_share']:.0f}% of option quotes came from real Kite 1-minute closes. Multi-leg sells are "
          f"marked on last-trade closes of each leg (not bid/ask), so their stop-outs here are noisier than live.\n")
        w("| Strategy | Trades | Win % | Net | PF |\n|---|---|---|---|---|")
        for k, m in sorted(px["table"].items(), key=lambda kv: -kv[1]["net"]):
            w(f"| {k} | {m['trades']} | {m['win_pct']} | {inr(m['net'])} | {f(m['pf'])} |")
        w("")

    # monthly
    w("## 5. Monthly OOS P&L (₹, after costs)\n")
    keys = ["AGENT:validated_signals", "SIG:FIRST_HOUR_TREND", "EXEC:X_ORB15_BUY", "EXEC:X_ST_VWAP_BUY", "EXEC:X_CPR_NARROW",
            "EXEC:X_ORB15_SEL", "SIG:TWAP_REJECT", "SIG:TOD_PE_1330", "EXEC:X_IFLY_920"]
    keys = [k for k in keys if k in cur]
    mon = {}
    for k in keys:
        d = {}
        for t, n in cur[k]:
            d[t[:7]] = d.get(t[:7], 0) + n
        mon[k] = d
    months = sorted({mm for v in mon.values() for mm in v})
    w("| Month | " + " | ".join(k.split(":")[1] for k in keys) + " |\n|---|" + "---|" * len(keys))
    for mm in months:
        w(f"| {mm} | " + " | ".join(inr(mon[k].get(mm, 0)) for k in keys) + " |")
    w("| **Total** | " + " | ".join(f"**{inr(sum(mon[k].values()))}**" for k in keys) + " |")
    w("| Losing months | " + " | ".join(f"{sum(1 for v in mon[k].values() if v < 0)}/{len(months)}" for k in keys) + " |")

    # gate
    w("\n## 6. Gate after the 1-year run (rules unchanged: pass needs OOS n ≥ 30, PF ≥ 1.2, DSR ≥ 0.95; probation needs train validation + OOS net > 0)\n")
    gw = opt.get("gate_written") or {}
    tradable = {k: s for k, s in gw.items() if s != "blocked"}
    w(f"Tradable now (paper): {', '.join(f'{k} = {s}' for k, s in tradable.items()) or '**none** — every signal is blocked'}.\n")
    prev = {"FIRST_HOUR_TREND": "probation", "X_ORB15_BUY": "probation", "X_ST_VWAP_BUY": "probation", "X_CPR_NARROW": "probation"}
    for k, st in gw.items():
        if st != "blocked" and k not in prev:
            prev[k] = "blocked"
    w("| Signal | Before | After 1-year run | Why |\n|---|---|---|---|")
    gs = {**opt["gate_signals"], **opt["gate_methods"]}
    for k, b in prev.items():
        w(f"| {k} | {b} | {gw.get(k, (gs.get(k) or {}).get('status'))} | {(gs.get(k) or {}).get('why', '')} |")

    # other agents
    w("\n## 7. Other agents — unified backtest over the year (Nifty 50 stocks, NIFTY futures, MCX)\n")
    w("Same unified_backtest engine as before (live agent decision code, agent_policy gates, exit_policy, full costs; "
      "walk-forward 4 expanding folds with 1-session embargo — the unified_backtest default). These agents stay paused by "
      "focus mode; this run is for completeness and changes no gate.\n")
    runs = [("NSE stocks + NIFTY futures/options agents", HERE / "logs/backtests/unified_backtest_1y_nse.json", False),
            ("MCX (5 contracts, sessions where all have bars)", HERE / "logs/backtests/unified_backtest_1y_mcx.json", True),
            ("MCX SILVERM alone (its full 248 sessions)", HERE / "logs/backtests/unified_backtest_1y_silverm.json", True)]
    for title, pth, is_mcx in runs:
        if not pth.exists():
            w(f"**{title}:** not finished in this run (see remaining work).\n")
            continue
        ub = json.loads(pth.read_text())
        ds = ub.get("mcx_days") if is_mcx else ub.get("days")
        if not ds:
            continue
        w(f"**{title}** — {len(ds)} sessions {ds[0]} … {ds[-1]}, run time {ub.get('elapsed_s')} s.\n")
        w("| Agent | Trades | Win % | Gross | Costs | Net | %/month on ₹10L | Max DD | PF | DSR | Verdict |\n|---|---|---|---|---|---|---|---|---|---|---|")
        for a, r in ub["agents"].items():
            s_ = r.get("stats") or {}
            if not s_ or not s_.get("trades"):
                why = r.get("verdict") or r.get("note") or "no trades"
                if is_mcx == (a in ("mcx_native", "invent")):
                    w(f"| {a} | 0 | — | — | — | — | — | — | — | — | {why} |")
                continue
            w(f"| {a} | {s_.get('trades')} | {s_.get('win_rate')} | {inr(s_.get('gross'))} | {inr(s_.get('costs'))} | "
              f"{inr(s_.get('net'))} | {100 * (s_.get('net') or 0) / 1e6 / len(ds) * 21:+.2f}% | {inr(s_.get('max_dd'))} | "
              f"{f(s_.get('profit_factor'))} | {f(s_.get('dsr'), 3)} | {r.get('verdict')} |")
        for n in ub.get("notes", []):
            w(f"- {n}")
        w("")
    w("\nFiles: `logs/backtest_1y_options.json`, `logs/backtests/unified_backtest_1y_{nse,mcx,silverm}.json`, `logs/kite_1y/coverage.json`, "
      "`logs/kite_1y/iv_model.json`, chart `/workspace/abitrade_1y_equity.png`.")
    OUT.write_text("\n".join(L) + "\n")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
