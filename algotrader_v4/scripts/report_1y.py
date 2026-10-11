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
    ubp = HERE / "logs" / "backtests" / "unified_backtest_1y.json"
    ub = json.loads(ubp.read_text()) if ubp.exists() else None
    mst = json.loads((K1Y / "mcx_stitch_coverage.json").read_text()) if (K1Y / "mcx_stitch_coverage.json").exists() else {}
    L = []
    w = L.append
    w("# AbiTrade — 1-year backtest on Kite data (PAPER research)\n")
    w(f"Generated {opt['generated']} IST. Kite account YFW795, PAPER mode, no orders placed. "
      f"Options walk-forward OOS window **{opt['oos_window'][0]} … {opt['oos_window'][1]} ({opt['oos_window'][2]} sessions)**, "
      f"12 expanding folds, 2-session embargo, first 40 sessions train-only.\n")

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
    w("| Strategy | Trades | Win % | Gross | Costs | Net | %/month | Max DD | PF | Sharpe | DSR | Verdict |\n"
      "|---|---|---|---|---|---|---|---|---|---|---|---|")
    tab = opt["table"]
    for k in sorted(tab, key=lambda k: (not k.startswith("AGENT"), -tab[k]["net"])):
        m = tab[k]
        if not m["trades"]:
            continue
        w(f"| {k} | {m['trades']} | {m['win_pct']} | {inr(m['gross'])} | {inr(m['costs_incl_spread'])} | {inr(m['net'])} | "
          f"{m['pct_per_month_10L']:+.2f}% | {inr(-m['max_dd'])} | {f(m['pf'])} | {f(m['sharpe_daily_ann'])} | {f(m['dsr'],3)} | {m['verdict']} |")
    zero = [k for k in tab if not tab[k]["trades"]]
    if zero:
        w(f"\nNo OOS trades (never validated on a train fold, or no setups): {', '.join(zero)}.")
    w("\nUntestable with this data: " + "; ".join(f"**{k}** — {v}" for k, v in opt["untestable"].items()) + "\n")

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

    # monthly
    w("## 5. Monthly OOS P&L (₹, after costs)\n")
    mon = opt["monthly"]
    months = sorted({mm for v in mon.values() for mm in v})
    keys = list(mon)
    w("| Month | " + " | ".join(k.split(":")[1] for k in keys) + " |\n|---|" + "---|" * len(keys))
    for mm in months:
        w(f"| {mm} | " + " | ".join(inr(mon[k].get(mm, 0)) for k in keys) + " |")
    w("| **Total** | " + " | ".join(f"**{inr(sum(mon[k].values()))}**" for k in keys) + " |")

    # gate
    w("\n## 6. Gate after the 1-year run (rules unchanged: pass needs OOS n ≥ 30, PF ≥ 1.2, DSR ≥ 0.95; probation needs train validation + OOS net > 0)\n")
    gw = opt.get("gate_written") or {}
    tradable = {k: s for k, s in gw.items() if s != "blocked"}
    w(f"Tradable now (paper): {', '.join(f'{k} = {s}' for k, s in tradable.items()) or '**none** — every signal is blocked'}.\n")
    prev = {"FIRST_HOUR_TREND": "probation", "X_ORB15_BUY": "probation", "X_ST_VWAP_BUY": "probation", "X_CPR_NARROW": "probation"}
    w("| Former probation signal | Before | After 1-year run | Why |\n|---|---|---|---|")
    gs = {**opt["gate_signals"], **opt["gate_methods"]}
    for k, b in prev.items():
        w(f"| {k} | {b} | {gw.get(k, (gs.get(k) or {}).get('status'))} | {(gs.get(k) or {}).get('why', '')} |")

    # other agents
    w("\n## 7. Other agents — unified backtest over the year (Nifty 50 stocks, NIFTY futures, MCX)\n")
    if ub:
        w(f"Sessions: NSE {len(ub['days'])} ({ub['days'][0]} … {ub['days'][-1]}), MCX {len(ub.get('mcx_days') or [])}. "
          f"Run time {ub.get('elapsed_s')} s. These agents stay paused by focus mode; this is for completeness.\n")
        w("| Agent | Trades | Win % | Gross | Costs | Net | %/month | Max DD | PF | DSR | Verdict |\n|---|---|---|---|---|---|---|---|---|---|---|")
        nd = max(1, len(ub["days"]))
        for a, r in ub["agents"].items():
            s = r.get("stats") or {}
            if not s:
                w(f"| {a} | — | — | — | — | — | — | — | — | — | {r.get('verdict', r.get('note', 'not run'))} |")
                continue
            dd = s.get("max_dd")
            w(f"| {a} | {s.get('trades')} | {s.get('win_rate')} | {inr(s.get('gross'))} | {inr(s.get('costs'))} | {inr(s.get('net'))} | "
              f"{100 * (s.get('net') or 0) / 1e6 / (len(ub.get('mcx_days') or []) if a in ('mcx_native','invent') and ub.get('mcx_days') else nd) * 21:+.2f}% | "
              f"{inr(dd)} | {f(s.get('profit_factor'))} | {f(s.get('dsr'),3)} | {r.get('verdict')} |")
        for n in ub.get("notes", []):
            w(f"- {n}")
    else:
        w("Not completed in this run — see remaining work.")
    w("\nFiles: `logs/backtest_1y_options.json`, `logs/backtests/unified_backtest_1y.json`, `logs/kite_1y/coverage.json`, "
      "`logs/kite_1y/iv_model.json`, chart `/workspace/abitrade_1y_equity.png`.")
    OUT.write_text("\n".join(L) + "\n")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
