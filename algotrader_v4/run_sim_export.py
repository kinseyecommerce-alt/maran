"""One-shot: run NSE day simulation (₹1 lakh per trade) and export all trades to CSV."""
import sys, os, time as _time
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

import loguru; loguru.logger.remove()
import pandas as pd
from nse_day_simulation import run_simulation, print_report

t0 = _time.time()
print("Running simulation (₹1,00,000 per trade)…")
trackers, signals_log = run_simulation(seed=2026)
print_report(trackers, signals_log)

# Collect every closed trade into a flat list
rows = []
for tf_label, tf_data in trackers.items():
    for agent_name, tracker in tf_data.items():
        for t in tracker.trades:
            if not t.closed:
                continue
            rows.append({
                "agent":         t.agent,
                "timeframe":     t.tf,
                "symbol":        t.sym,
                "action":        t.action,
                "pattern":       t.pattern,
                "entry_time":    t.entry_ts.strftime("%H:%M"),
                "entry_price":   round(t.entry_px, 2),
                "exit_time":     t.exit_ts.strftime("%H:%M") if t.exit_ts else "",
                "exit_price":    round(t.exit_px, 2) if t.exit_px else "",
                "qty":           t.qty,
                "capital_deployed": round(t.entry_px * t.qty, 2),
                "sl_price":      round(t.sl_px, 2),
                "target_price":  round(t.tgt_px, 2),
                "sl_pct":        round(t.sl_pct, 2),
                "target_pct":    round(t.tgt_pct, 2),
                "pnl_pct":       round(t.pnl_pct, 3),
                "pnl_rs":        round(t.pnl_rs, 2),
                "exit_reason":   t.exit_reason,
                "win":           "YES" if t.pnl_pct > 0 else "NO",
            })

df = pd.DataFrame(rows).sort_values(["agent", "timeframe", "entry_time"])
out = "logs/simulation_trades_1lac.csv"
os.makedirs("logs", exist_ok=True)
df.to_csv(out, index=False)

print(f"\n  ✅  {len(df)} trades exported → {out}")
print(f"  Elapsed: {_time.time()-t0:.1f}s\n")
